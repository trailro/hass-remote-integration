"""Uploads bigger than the HTTP server's client_max_size (Home Assistant sets 16 MB) must still arrive whole.

aiohttp applies client_max_size to request.read()/post()/json() (and, from 3.14, to BodyPartReader.read(); the
3.13.5 Home Assistant 2026.5.0 pins does not); the upload views stream the part with read_chunk(), which no
version caps.  Pinned against a real aiohttp server with a 1 MB cap,
so an aiohttp or view change that starts buffering the part fails here, not on an operator's 2 GB import."""

import asyncio
import io
import os
import shutil
import unittest
import zipfile

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import backupkit
from custom_components.integration_manager import backup_views, ha_import, import_views
from tests.test_review_backup import _hass, _volume

CAP = 1024**2
BIG = 3 * CAP


def _post(view_cls, filename, payload, cfg):
    hass = _hass(cfg)
    hass.config.path = lambda *p: os.path.join(cfg, *p)
    view = view_cls(hass)
    view.json = web.json_response

    async def buffered(request):  # the control: the same request read whole hits the cap, in every aiohttp version
        return web.json_response({"bytes": len(await request.read())})

    async def run():
        app = web.Application(client_max_size=CAP)
        app.router.add_post("/view", view.post)
        app.router.add_post("/buffered", buffered)
        out = {}
        async with TestClient(TestServer(app)) as client:
            for path in ("/view", "/buffered"):
                form = aiohttp.FormData()
                form.add_field("file", io.BytesIO(payload), filename=filename)
                resp = await client.post(path, data=form, headers={"X-Requested-With": "fetch"})
                out[path] = (resp.status, await resp.json() if resp.status == 200 else await resp.text())
        return out

    return asyncio.run(run())


class UploadAboveClientMaxSizeTest(unittest.TestCase):
    def setUp(self):
        self.cfg = _volume()
        self.addCleanup(shutil.rmtree, self.cfg, True)

    def test_a_home_assistant_backup_larger_than_the_cap_is_stored_whole(self):
        payload = os.urandom(BIG)
        out = _post(import_views.ImportUploadView, "backup.tar", payload, self.cfg)
        self.assertEqual(out["/buffered"][0], 413, out["/buffered"])
        self.assertEqual(out["/view"][0], 200, out["/view"])
        self.assertTrue(out["/view"][1]["ok"], out["/view"])
        self.assertEqual(out["/view"][1]["bytes"], BIG)
        with open(os.path.join(self.cfg, ha_import.IMPORT_TAR), "rb") as fh:
            self.assertEqual(fh.read(), payload)

    def test_a_manager_backup_larger_than_the_cap_is_stored_whole(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
            zf.writestr(backupkit.MARKER, "{}")
            zf.writestr(".storage/core.config_entries", "{}")
            zf.writestr(".storage/big", os.urandom(BIG))
        payload = buf.getvalue()
        out = _post(backup_views.BackupUploadView, "big.zip", payload, self.cfg)
        self.assertEqual(out["/buffered"][0], 413, out["/buffered"])
        self.assertEqual(out["/view"][0], 200, out["/view"])
        self.assertTrue(out["/view"][1]["ok"], out["/view"])
        self.assertEqual(out["/view"][1]["bytes"], len(payload))
        with open(os.path.join(self.cfg, backupkit.BACKUP_DIR, out["/view"][1]["name"]), "rb") as fh:
            self.assertEqual(fh.read(), payload)


if __name__ == "__main__":
    unittest.main()
