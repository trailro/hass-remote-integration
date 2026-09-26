"""The image's HEALTHCHECK: its shape in the Dockerfile, and what the probe it ships really does against
a server that answers the way the manager and the entrypoint do (404 or 401 from the manager, 200 from the
entrypoint's status server while Home Assistant installs, a 5xx, nothing at all).  It is liveness only: an
install in progress is alive, or a Supervisor watchdog would restart the app in the middle of one."""

import http.client
import http.server
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest

from tests.fakes import entrypoint_for

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCKERFILE = os.path.join(ROOT, "Dockerfile")
ALIVE = "/api/alive"


def _dockerfile() -> str:
    with open(DOCKERFILE, encoding="utf-8") as fh:
        return re.sub(r"\\\n\s*", " ", fh.read())  # line continuations joined, as Docker reads them


def _directive() -> str:
    line = next((ln for ln in _dockerfile().splitlines() if ln.startswith("HEALTHCHECK")), "")
    if not line:
        raise AssertionError("the Dockerfile has no HEALTHCHECK")
    return line


def _probe_argv() -> list:
    return json.loads(_directive().split("CMD", 1)[1].strip())


def _options() -> dict:
    return dict(re.findall(r"--([a-z-]+)=(\S+)", _directive().split("CMD", 1)[0]))


def _seconds(value: str) -> int:
    unit = {"s": 1, "m": 60, "h": 3600}[value[-1]]
    return int(value[:-1]) * unit


class Requires(unittest.TestCase):
    def setUp(self):
        if not os.path.isfile(DOCKERFILE):
            self.skipTest("the Dockerfile is not copied next to the tests")


class ShapeTest(Requires):
    def test_the_directive_is_there_and_is_exec_form(self):
        """A shell form would need a shell and lose the exit code of the probe itself."""
        argv = _probe_argv()
        self.assertEqual(argv[:2], ["python", "-c"])
        self.assertEqual(len(argv), 3)

    def test_it_uses_what_the_image_has(self):
        """No curl and no wget in python:3.14-slim: the probe is the image's own Python."""
        for absent in ("curl", "wget", "nc "):
            self.assertNotIn(absent, _directive())

    def test_the_port_is_read_at_runtime(self):
        """The file the entrypoint writes as the app (the Supervisor's port), else HRI_PORT: no port in the probe."""
        import entrypoint

        code = _probe_argv()[2]
        self.assertIn("HRI_PORT", code)
        self.assertIn("os.environ", code)
        self.assertIn(repr(entrypoint.PORT_FILE), code)
        self.assertNotIn("8087", code)

    def test_it_asks_the_liveness_path(self):
        """/api/alive: the entrypoint's status server answers it 200 while Home Assistant installs, the manager
        has no view for it (404, or 401 with a password), so no manager code runs for a probe."""
        code = _probe_argv()[2]
        self.assertIn(f"'{ALIVE}'", code)
        self.assertNotIn("/api/status", code)
        self.assertNotIn("X-Requested-With", code)

    def test_the_probe_is_valid_python(self):
        compile(_probe_argv()[2], "<healthcheck>", "exec")

    def test_the_start_period_covers_a_first_install(self):
        """An install that writes nothing for PIP_IDLE_TIMEOUT_S (15 min) is the longest the entrypoint
        waits for; the start period has to be at least that, plus the rest of the boot."""
        import entrypoint

        options = _options()
        self.assertGreaterEqual(_seconds(options["start-period"]), entrypoint.PIP_IDLE_TIMEOUT_S + 300)
        self.assertLessEqual(_seconds(options["timeout"]), _seconds(options["interval"]))
        self.assertGreaterEqual(int(options["retries"]), 2)  # one slow answer is not a dead manager


class _Handler(http.server.BaseHTTPRequestHandler):
    status = 200

    def do_GET(self):  # noqa: N802
        self.send_response(self.status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass


class ProbeTest(Requires):
    """The string the Dockerfile ships, run against a server that answers like the manager does."""

    def _run(self, port: int, port_file: str | None = None) -> int:
        """The probe with HRI_PORT=port, and ``port_file`` in place of the entrypoint's file (none by default: the file
        of the container the tests run in is not theirs)."""
        import entrypoint

        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        code = _probe_argv()[2].replace(repr(entrypoint.PORT_FILE), repr(port_file or os.path.join(tmp, "none")))
        env = dict(os.environ, HRI_PORT=str(port))
        return subprocess.run([sys.executable, "-c", code], env=env,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60).returncode

    def _serving(self, status: int) -> int:
        handler = type("H", (_Handler,), {"status": status})
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return self._run(srv.server_address[1])

    def test_an_answering_manager_is_healthy(self):
        self.assertEqual(self._serving(200), 0)

    def test_a_password_does_not_make_it_fail(self):
        """With HRI_PASSWORD set the auth layer answers 401 on /api/alive: the manager is up."""
        self.assertEqual(self._serving(401), 0)

    def test_no_view_does_not_make_it_fail(self):
        """Without a password Home Assistant answers 404 on /api/alive, which has no view: the manager is up."""
        self.assertEqual(self._serving(404), 0)

    def test_a_server_error_is_not_healthy(self):
        self.assertEqual(self._serving(503), 1)
        self.assertEqual(self._serving(500), 1)

    def test_the_apps_port_file_wins_over_hri_port(self):
        """As the app the Supervisor may give another port (ingress_port 0): Docker runs the probe with the image's
        HRI_PORT, so the port the entrypoint wrote is the one asked."""
        handler = type("H", (_Handler,), {"status": 404})
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        closed = sock.getsockname()[1]
        sock.close()
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        port_file = os.path.join(tmp, "hri-port")
        with open(port_file, "w", encoding="utf-8") as fh:
            fh.write(f"{srv.server_address[1]}\n")  # as entrypoint.write_port_file
        self.assertEqual(self._run(closed, port_file), 0)
        with open(port_file, "w", encoding="utf-8") as fh:
            fh.write(f"{closed}\n")
        self.assertEqual(self._run(srv.server_address[1], port_file), 1)

    def test_a_manager_that_answers_nothing_is_unhealthy(self):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()  # nothing listens there
        self.assertEqual(self._run(port), 1)


class StatusServerTest(Requires):
    """The entrypoint's status server, served while Home Assistant installs (and while a failed restore holds the
    boot): the probe the Dockerfile ships finds it alive, while every other /api/ path still answers 503."""

    def _server(self, held=False, **env):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        ep = entrypoint_for(self, tmp, **{"HRI_APP": "", "HRI_PASSWORD": "", "HRI_PASSWORD_FILE": "", **env})
        if held:
            ep._status.update(phase="retrying", version=None, kind="restore_hold", title="held")
        else:
            ep._status.update(phase="apt-get install ffmpeg", version="2026.9.3", kind="install", title=None)
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), ep._StatusHandler)
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return srv.server_address[1]

    def _get(self, port, path, host="localhost"):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            c.request("GET", path, headers={"Host": host})
            resp = c.getresponse()
            return resp.status, resp.read()
        finally:
            c.close()

    def test_the_probe_finds_an_install_alive(self):
        for env in ({}, {"HRI_PASSWORD": "pw"}):
            with self.subTest(env=env):
                self.assertEqual(ProbeTest._run(self, self._server(**env)), 0)
        self.assertEqual(ProbeTest._run(self, self._server(held=True)), 0)

    def test_alive_is_200_and_says_nothing_else(self):
        port = self._server(HRI_PASSWORD="pw")
        status, body = self._get(port, ALIVE)
        self.assertEqual(status, 200)
        self.assertLessEqual(len(body), 32)
        for leak in (b"2026.9.3", b"ffmpeg", b"phase"):
            self.assertNotIn(leak, body)
        self.assertEqual(self._get(port, ALIVE + "?x=1")[0], 200)

    def test_the_rest_of_the_api_is_still_not_up(self):
        port = self._server()
        self.assertEqual(self._get(port, "/api/status")[0], 503)
        self.assertEqual(self._get(port, "/api/alive/more")[0], 503)

    def test_the_host_guard_applies(self):
        self.assertEqual(self._get(self._server(), ALIVE, host="evil.example.com")[0], 403)


if __name__ == "__main__":
    unittest.main()
