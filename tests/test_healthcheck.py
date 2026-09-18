"""The image's HEALTHCHECK: its shape in the Dockerfile, and what the probe it ships really does against
a server that answers the way the manager and the entrypoint do (200, 401 with a password, 503 while Home
Assistant installs, nothing at all)."""

import http.server
import json
import os
import re
import socket
import subprocess
import sys
import threading
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCKERFILE = os.path.join(ROOT, "Dockerfile")


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
        code = _probe_argv()[2]
        self.assertIn("HRI_PORT", code)
        self.assertIn("os.environ", code)

    def test_it_asks_the_cheap_status_path(self):
        """/api/status without X-Requested-With: a copy at most 10 s old, and no patch code run."""
        code = _probe_argv()[2]
        self.assertIn("/api/status", code)
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

    def _run(self, port: int) -> int:
        env = dict(os.environ, HRI_PORT=str(port))
        return subprocess.run([sys.executable, "-c", _probe_argv()[2]], env=env,
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
        """With HRI_PASSWORD set the auth layer answers 401 on /api/status: the manager is up."""
        self.assertEqual(self._serving(401), 0)

    def test_the_install_page_is_not_healthy(self):
        """While Home Assistant installs the entrypoint answers 503 under /api/: no manager API yet."""
        self.assertEqual(self._serving(503), 1)

    def test_a_manager_that_answers_nothing_is_unhealthy(self):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()  # nothing listens there
        self.assertEqual(self._run(port), 1)


if __name__ == "__main__":
    unittest.main()
