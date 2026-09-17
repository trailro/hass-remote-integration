"""Review round 14, store: boot sweeps of what a killed write leaves behind."""

import os
import shutil
import tempfile
import time
import unittest

from tests.fakes import entrypoint_for


class YamlTmpSweepTest(unittest.TestCase):
    """Installer.yaml_write's temporary file, left by a kill between its mkstemp and its replace, stayed for good."""

    def setUp(self):
        self.cfg = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.cfg, True)
        self.ep = entrypoint_for(self, self.cfg)
        self.ydir = os.path.join(self.cfg, "integration_manager", "yaml")
        os.makedirs(self.ydir)

    def make(self, name, age):
        path = os.path.join(self.ydir, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("a: 1\n")
        old = time.time() - age
        os.utime(path, (old, old))
        return name

    def test_old_leftovers_go_everything_else_stays(self):
        fd, real = tempfile.mkstemp(dir=self.ydir, prefix=".my_domain.yaml.", suffix=".tmp")  # as yaml_write names it
        os.close(fd)
        gone = [self.make(os.path.basename(real), 3600), self.make(".demo.yaml.ab12_x9z.tmp", 3600)]
        kept = [self.make(".demo.yaml.fresh123.tmp", 5), self.make("demo.yaml", 3600), self.make("x.json.abc.tmp", 3600),
                self.make(".demo.yaml.abc.tmp.bak", 3600), self.make(".Demo.yaml.abc.tmp.d", 3600)]
        os.symlink(os.path.join(self.ydir, "demo.yaml"), os.path.join(self.ydir, ".link.yaml.abcdefgh.tmp"))
        old = time.time() - 3600
        os.utime(os.path.join(self.ydir, ".link.yaml.abcdefgh.tmp"), (old, old), follow_symlinks=False)
        kept.append(".link.yaml.abcdefgh.tmp")
        self.ep.sweep_json_tmp_files()
        self.assertEqual(sorted(os.listdir(self.ydir)), sorted(kept))
        self.assertFalse(any(g in os.listdir(self.ydir) for g in gone))

    def test_no_yaml_directory(self):
        shutil.rmtree(self.ydir)
        self.ep.sweep_json_tmp_files()
        self.assertFalse(os.path.exists(self.ydir))


if __name__ == "__main__":
    unittest.main()
