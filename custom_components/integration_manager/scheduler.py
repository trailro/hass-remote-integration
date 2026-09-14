"""Time-based jobs: a daily backup and a weekly release check, both
switchable in settings (nothing is ever installed automatically)."""

from __future__ import annotations

import logging
import time

from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_call_later, async_track_time_change

from .installer import Installer

_LOGGER = logging.getLogger(__name__)
WEEK_S = 6.5 * 86400


class Scheduler:
    def __init__(self, hass: HomeAssistant, installer: Installer) -> None:
        self.hass = hass
        self.installer = installer
        self._unsub = None
        self._hour = None

    def start(self) -> None:
        self.rearm()
        # boot: a release check if the last one is older than a week
        async_call_later(self.hass, 120, self._boot_check)

    def rearm(self) -> None:
        """(Re)arm the daily tick at the configured hour; called at boot and
        whenever the settings change."""
        hour = self.installer.settings.int_("backup_daily_hour", 0, 23)
        if self._unsub is not None and self._hour == hour:
            return
        if self._unsub is not None:
            self._unsub()
        self._hour = hour
        self._unsub = async_track_time_change(self.hass, self._daily, hour=hour, minute=0, second=30)

    def _release_check_due(self) -> bool:
        st = self.installer.settings
        return st.bool_("release_check") and time.time() - float(self.installer.state.last_release_check or 0) > WEEK_S

    async def _boot_check(self, _now) -> None:
        if self._release_check_due():
            await self._release_check()

    async def _daily(self, _now) -> None:
        st = self.installer.settings
        if st.bool_("backup_daily") and self.installer.busy:
            _LOGGER.info("daily backup skipped: an install/start is running; retrying in 30 min")
            async_call_later(self.hass, 1800, self._daily)
            return
        if st.bool_("backup_daily"):
            try:
                import backupkit

                rec = await self.installer.async_backup_exclusive("daily")
                pruned = await self.hass.async_add_executor_job(
                    backupkit.prune, self.installer.config_dir, st.backup_keep, self.installer.protected_backups())
                _LOGGER.info("daily backup %s (%s bytes), pruned %s", rec["name"], rec["bytes"], pruned or "nothing")
            except Exception as err:  # noqa: BLE001
                _LOGGER.error("daily backup failed: %s", err)
        if self._release_check_due():
            await self._release_check()

    async def _release_check(self) -> None:
        try:
            found = await self.installer.check_updates(force=True)
            _LOGGER.info("weekly release check: %s", found or "everything up to date")
        finally:
            self.installer.state.last_release_check = int(time.time())  # runtime state belongs in state.json, not the token file
            self.installer._save_state()
