"""A hand-written file nested too deep must not end the boot: the parser and the walk both recurse."""

import os
import tempfile
import unittest

import jsonio
import run


def _deep(depth: int) -> str:
    return "[" * depth + "]" * depth


class DeepJsonTest(unittest.TestCase):
    def setUp(self):
        self.cfg = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.cfg, ".storage"))
        self.path = os.path.join(self.cfg, ".storage", "http")

    def test_read_json_treats_a_too_deep_file_as_unreadable(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write(_deep(200000))
        self.assertEqual(jsonio.read_json(self.path, "default"), "default")

    def test_a_store_too_deep_to_walk_is_dropped_instead_of_ending_the_boot(self):
        depth = 5000  # the parser takes it, the walk over it would not (json.dump could not write it either)
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write('{"key": "http", "data": ' + '{"data": ' * depth + '{"server_port": 9999}' + "}" * depth + "}")
        run.drop_foreign_http_port(self.cfg, 8087)
        self.assertFalse(os.path.exists(self.path))


if __name__ == "__main__":
    unittest.main()
