"""Review of b4cd1a1.  S3-3 (manager part): the error of the last manager action went into the retained manager
document as the integration wrote it, a token URL included; the UI masks the same text.  The same text went into the
manager result published on the broker and into the timeline event."""

import unittest
from unittest import mock

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


class ResultAndTimelineMaskedTest(unittest.IsolatedAsyncioTestCase):
    async def test_the_error_of_a_failed_action(self):
        dev = device()

        async def boom():
            raise RuntimeError(f"cannot reach https://example.invalid/api?access_token={TOKEN}")

        dev._do_check_updates = boom
        with self.assertLogs(md._LOGGER, "ERROR"), mock.patch.object(md.events, "emit") as emit:
            await dev.async_action("check_updates")
        [result] = [e[1] for e in dev.publisher.log if e[0] == "result"]
        self.assertNotIn(TOKEN, str(result))
        self.assertIn("RuntimeError: cannot reach", result["error"])
        [event] = emit.call_args_list
        self.assertNotIn(TOKEN, str(event))
        self.assertIn("failed: RuntimeError: cannot reach", event.args[1])


if __name__ == "__main__":
    unittest.main()
