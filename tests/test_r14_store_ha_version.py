"""Review round 14, store: a Home Assistant venv installed for this Python is accepted with the PyPI list too."""

import os
import shutil
import sys
import unittest

from tests.test_r4_lifecycle import make_venv
from tests import test_r12_backup_pypi as r12
from tests.test_r12_backup_pypi import B, _release, _Session

YANKED = "2026.9.3"
DELISTED = "2026.9.1"


class InstalledVenvWithReleaseListTest(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = r12.HaVersionCheckTest.asyncSetUp
    updater = r12.HaVersionCheckTest.updater

    def payload(self):
        return {"info": {"requires_python": ">=3.0"},
                "releases": {B: [_release()], YANKED: [_release(yanked=True)]}}

    async def test_an_installed_yanked_version_is_accepted(self):
        make_venv(self.cfg, YANKED)
        await self.updater(_Session(self.payload())).validate(YANKED)

    async def test_an_installed_version_missing_from_pypi_is_accepted(self):
        make_venv(self.cfg, DELISTED)
        await self.updater(_Session(self.payload())).validate(DELISTED)

    async def test_not_installed_or_for_another_python_is_still_refused(self):
        up = self.updater(_Session(self.payload()))
        for version in (YANKED, DELISTED):
            with self.assertRaises(ValueError) as ctx:
                await up.validate(version)
            self.assertIn("not a Home Assistant release", str(ctx.exception))
        venv = make_venv(self.cfg, YANKED)
        shutil.rmtree(os.path.join(venv, "lib", f"python{sys.version_info[0]}.{sys.version_info[1]}"))
        with self.assertRaises(ValueError):
            await up.validate(YANKED)
        make_venv(self.cfg, DELISTED)
        os.remove(os.path.join(self.cfg, f"venv-{DELISTED}", ".ok"))  # a half-made install
        with self.assertRaises(ValueError):
            await up.validate(DELISTED)

    async def test_the_baseline_still_applies_to_an_installed_venv(self):
        make_venv(self.cfg, "2026.7.1")
        with self.assertRaises(ValueError) as ctx:
            await self.updater(_Session(self.payload())).validate("2026.7.1")
        self.assertIn("baseline", str(ctx.exception))

