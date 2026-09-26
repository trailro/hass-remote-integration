"""Review of b4cd1a1.  S3-3 (manager part): the error of the last manager action went into the retained manager
document as the integration wrote it, a token URL included; the UI masks the same text."""

import unittest

from custom_components.integration_manager import manager_device as md
from tests.test_manager_device import device

TOKEN = "s3cr3tT0kenValue42"


class LastActionMaskedTest(unittest.IsolatedAsyncioTestCase):
    async def test_the_error_of_a_failed_action(self):
        dev = device()

        async def boom():
            raise RuntimeError(f"cannot reach https://example.invalid/api?access_token={TOKEN}")

        dev._do_check_updates = boom
        with self.assertLogs(md._LOGGER, "ERROR"):
            await dev.async_action("check_updates")
        last = dev.document()["last_action"]
        self.assertNotIn(TOKEN, str(last))
        self.assertIn("RuntimeError: cannot reach", last["error"])
        self.assertEqual((last["action"], last["ok"]), ("check_updates", False))
        self.assertIn(TOKEN, dev.last_action["error"])  # the caller's own answer is not what is retained

    def test_no_action_yet(self):
        self.assertIsNone(device().document()["last_action"])


if __name__ == "__main__":
    unittest.main()
