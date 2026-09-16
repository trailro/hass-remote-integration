"""Review round 12 (C6, C7): HRI_PORT, SIGTERM before the exec."""

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from unittest import mock

from tests.fakes import entrypoint_for

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class PortTest(unittest.TestCase):
    """C6: a typo in HRI_PORT was a bare traceback at import, in a restart loop."""

    def test_an_invalid_port_is_said_in_the_log(self):
        cfg = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, cfg, True)
        ep = entrypoint_for(self, cfg, HRI_PORT="80a")
        with mock.patch.object(ep, "restrict_umask"), self.assertRaises(SystemExit) as ctx:
            ep.main()
        self.assertEqual(ctx.exception.code, 2)
        with open(ep.LOG_FILE, encoding="utf-8") as fh:
            self.assertIn("HRI_PORT='80a' is not a TCP port", fh.read())

    def test_valid_ports(self):
        ep = entrypoint_for(self, tempfile.mkdtemp())
        self.assertEqual([ep._parse_port(v) for v in ("8087", "0", "65536", "-1", "")], [8087, None, None, None, None])  # noqa: SLF001


class SigtermTest(unittest.TestCase):
    """C7: as PID 1 without an init, the entrypoint ignored SIGTERM: docker stop during an install waited the whole
    grace period, and pip (in its own process group) was only killed by the SIGKILL of the container."""

    def test_sigterm_during_pip_stops_the_group_and_exits(self):
        cfg = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, cfg, True)
        pidfile = os.path.join(cfg, "pip.pid")
        child = textwrap.dedent(f"""
            import ast, os, sys
            sys.path.insert(0, {ROOT!r})
            import entrypoint as ep

            def main():
                with open({os.path.join(cfg, "pip.log")!r}, "w") as fh:
                    ep._run_pip(["sh", "-c", "echo $$ > {pidfile}.tmp; mv {pidfile}.tmp {pidfile}; exec sleep 60"], fh)

            ep.main = main
            tree = ast.parse(open(ep.__file__).read())
            block = next(n for n in tree.body if isinstance(n, ast.If) and "__main__" in ast.unparse(n.test))
            exec(compile(ast.Module(body=block.body, type_ignores=[]), ep.__file__, "exec"), ep.__dict__)
        """)
        proc = subprocess.Popen([sys.executable, "-c", child], env={**os.environ, "HRI_CONFIG": cfg}, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        pid = None
        try:
            deadline = time.monotonic() + 20
            while not os.path.isfile(pidfile) and time.monotonic() < deadline and proc.poll() is None:
                time.sleep(0.05)
            with open(pidfile, encoding="utf-8") as fh:
                pid = int(fh.read())
            proc.send_signal(signal.SIGTERM)
            _out, err = proc.communicate(timeout=15)
            self.assertEqual(proc.returncode, 128 + signal.SIGTERM, err.decode()[-2000:])
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)
        finally:
            proc.kill()
            proc.wait()
            if pid:
                try:
                    os.killpg(pid, signal.SIGKILL)
                except OSError:
                    pass


if __name__ == "__main__":
    unittest.main()
