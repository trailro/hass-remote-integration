"""Time-based jobs: a daily backup, a weekly release check and the health
watchdog, all switchable in settings (nothing is ever installed
automatically)."""

from __future__ import annotations

import logging
import os
import time

from datetime import timedelta

import homeassistant.const

from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event, HassJob, HomeAssistant, callback
from homeassistant.helpers.event import async_call_later, async_track_time_change, async_track_time_interval

from . import events
from .installer import Installer

_LOGGER = logging.getLogger(__name__)
WEEK_S = 6.5 * 86400
# The health verdict is rebuilt and republished every 60 s (discovery.HEALTH_INTERVAL_S); the watchdog reads it
# on the same period, so what it judges is never more than one publication old.  Its own windows are minutes,
# so nothing finer would change a decision.
WATCHDOG_TICK_S = 60
# After a boot the integration gets a full grace window to set up before an "error" can cost it a restart.
# Same value as the publisher's health grace: a verdict is only trusted once that has passed.
WATCHDOG_BOOT_GRACE_S = 900


class Scheduler:
    def __init__(self, hass: HomeAssistant, installer: Installer) -> None:
        self.hass = hass
        self.installer = installer
        self._unsub = None
        self._hour = None
        self._retry = None  # the pending busy-retry of the daily backup
        self._boot = None  # the pending release check at boot
        self._watchdog_unsub = None
        self._started = time.monotonic()
        self._bad_since: float | None = None   # monotonic: when the verdict first became "error", in this process
        self._refused = False                  # a refusal already on the timeline for this unhealthy stretch
        # The ladder: the first step of an episode reloads the config entries, the next one restarts.  A reload
        # makes the entities write fresh states, so the verdict may read ok for a minute or two after it even when
        # nothing is fixed: the ladder is only reset once ok has held for a whole window, never by that blip,
        # or a zombie would be reloaded again and again and never escalate.
        self._reloaded_at: float | None = None  # monotonic: the reload of this episode
        self._ok_since: float | None = None     # monotonic: when the verdict became ok after that reload
        self._reload_skip_said = False          # "no reload: <why>" already on the timeline for this episode
        self._announced = False                # the notification of the last automatic restart, raised once per boot

    def start(self) -> None:
        self.rearm()
        self._started = time.monotonic()
        # boot: the watchdog tick and a release check if the last one is older than a week
        self._boot = async_call_later(self.hass, 120, HassJob(self._boot_check, "watchdog and release check at boot"))
        # HassJob(cancel_on_shutdown=True) does not reach async_call_later's timer (HA only cancels handles whose
        # first argument is the job; call_later passes hass first): cancel the timers ourselves
        self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, self._on_stop)

    @callback
    def _on_stop(self, _event: Event | None = None) -> None:
        for unsub in (self._boot, self._retry, self._unsub, self._watchdog_unsub):
            if unsub is not None:
                unsub()
        self._boot = self._retry = self._unsub = self._watchdog_unsub = None

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
        self._arm_watchdog()
        if self._release_check_due():
            await self._release_check()

    def _arm_watchdog(self) -> None:
        """The watchdog tick, armed with the boot check rather than at setup: for the
        first two minutes of a boot it would refuse anyway (the boot grace), and the
        one timer is cancelled with the others at EVENT_HOMEASSISTANT_STOP."""
        if self._watchdog_unsub is None:
            self._watchdog_unsub = async_track_time_interval(
                self.hass, self._watchdog_tick, timedelta(seconds=WATCHDOG_TICK_S), name="hri health watchdog")

    async def _daily(self, _now) -> None:
        st = self.installer.settings
        if self._retry is not None:
            self._retry()  # one pending retry at most: the daily tick and a retry both land here
            self._retry = None
        if st.bool_("backup_daily") and self.installer.busy and not self.installer.backup_running:
            _LOGGER.info("daily backup skipped: another action is running; retrying in 30 min")
            self._retry = async_call_later(self.hass, 1800, HassJob(self._daily, "daily backup retry"))
            return
        if st.bool_("backup_daily"):
            try:
                import backupkit

                rec = await self.installer.async_backup_exclusive("daily")
                pruned = await self.hass.async_add_executor_job(
                    backupkit.prune, self.installer.config_dir, st.backup_keep, self.installer.protected_backups() | {rec["name"]})
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

    # ----- health watchdog ---------------------------------------------------

    def _verdict(self) -> dict | None:
        """The health document the MQTT publisher builds (with the boot grace),
        or None when the check itself failed: an exception in the manager says
        nothing about the integration and must never cost it a restart."""
        try:
            source = self.installer.health_source
            return source(grace=True) if source else self.installer.health()
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("health watchdog: the health check failed (%s: %s); no verdict this minute", type(err).__name__, err)
            return None

    def _watchdog_clear(self) -> None:
        self._bad_since, self._refused = None, False
        self.installer.watchdog_pending = None

    def _ladder_reset(self) -> None:
        self._reloaded_at = self._ok_since = None
        self._reload_skip_said = False

    def _live_refusal(self) -> str | None:
        """What the manager is doing right now, from memory only (no file is read
        on a tick that is not going to act anyway).  ``busy`` covers an install,
        start, stop, import, restore, backup, full rollback -- and a restart that
        is already taking the process down, which sets it and never clears it."""
        from . import preflight
        from .views import _ha_change_lock_taken
        from .import_views import _IMPORT_LOCK

        inst = self.installer
        if not inst.state.domain:
            return "no integration is running"
        if not self.hass.is_running:
            return "Home Assistant is not running yet (or is stopping)"
        booted = time.monotonic() - self._started
        if booted < WATCHDOG_BOOT_GRACE_S:
            return f"the process booted {int(booted)} s ago: the integration is given {WATCHDOG_BOOT_GRACE_S} s to set up"
        if inst.busy:
            return "another action is running (an install, start, stop, import, restore or full rollback), or the process is already going down"
        if _ha_change_lock_taken():
            return "a Home Assistant version change, a restore or a full rollback is being prepared"
        if _IMPORT_LOCK.locked():
            return "an import or upload from a Home Assistant backup is running"
        # A preflight sets no `busy` and takes minutes (a pip resolution), and an integration in error is exactly
        # when an operator starts another version: the restart it asked for would vanish, unanswered.
        if preflight.LOCK.locked():
            return "a preflight of an integration version is running"
        if preflight._HA_LOCK.locked():  # noqa: SLF001
            return "a Home Assistant preflight is running"
        manager = getattr(inst, "manager", None)
        # a manager action from MQTT owes the consumer a manager/result; one hung past ACTION_MAX_S does not
        # (_action_held says so), and a stuck process is what the watchdog is for
        if manager is not None and manager._action_held() is not None:  # noqa: SLF001
            return f"a manager action is running ({manager._running})"  # noqa: SLF001
        if inst.smoke.get("pending") or inst.state.pending_smoke:
            return "a smoke test is pending: its verdict decides, not the watchdog"
        if inst.state.pending_start:
            return "a start is deferred to the next boot: restart on System to run it"
        if inst.state.pending_rollback:
            return "a full rollback is waiting for the restart"
        if any(e.state.value == "setup_in_progress" for e in inst._entries_of(inst.state.domain) if not e.disabled_by):  # noqa: SLF001
            return "a config entry is still setting up"
        return None

    def _scheduled_refusal(self) -> str | None:
        """Blocking: what the next restart would apply.  A watchdog restart must
        never be the restart that carries out a restore, a rebuild or a Home
        Assistant version change nobody asked it to carry out now."""
        import backupkit
        import jsonio

        from . import ha_import

        inst = self.installer
        try:
            rollback = inst.rollback_restore_refusal()
            if rollback:
                return rollback
            if backupkit.pending(inst.config_dir):
                return "a restore is scheduled for the next restart"
            ha_state = jsonio.read_json(os.path.join(inst.state_dir, "ha.json"), {})
            if isinstance(ha_state, dict):
                change = ha_state.get("change")
                desired = ha_state.get("desired")
                if isinstance(change, dict) and change.get("to"):
                    return f"a switch to Home Assistant {change['to']} is scheduled for the next restart"
                if desired and desired != homeassistant.const.__version__:
                    return f"a switch to Home Assistant {desired} is scheduled for the next restart"
            if ha_import.load_summary(inst.config_dir):
                return "an import from a Home Assistant backup is waiting on System: apply or clear it first"
        except Exception as err:  # noqa: BLE001
            # what the next restart would apply could not be read: refuse, never restart on a guess
            return f"what the next restart would apply could not be read ({type(err).__name__}: {err})"
        return None

    async def _watchdog_tick(self, _now=None) -> None:
        """Once a minute: is the verdict still ``error`` (or, opted in, ``degraded``), for long
        enough, with nothing in the way?  Then reload the integration's config entries; if the
        verdict is still not ok a window after that, restart -- at most as often as the settings
        allow, with the window doubling after every restart that did not help."""
        inst = self.installer
        if not self._announced:
            # a restart the watchdog decided on ends the process before a persistent notification can show:
            # the boot after it raises the one recorded in state.json, whatever the setting says now
            self._announced = True
            inst.announce_watchdog()
        cfg = inst.settings.watchdog()
        if not cfg["enabled"]:
            self._watchdog_clear()
            self._ladder_reset()
            return
        verdict = self._verdict()
        if verdict is None:
            return  # unknown: the stretch keeps running, nothing is decided on it
        state = verdict.get("state")
        if state == "error" and "no config entry, no YAML setup" in (verdict.get("reason") or ""):
            # an integration installed but never configured reports error, not the smoke test's "unconfigured":
            # a restart cannot configure it, so the watchdog leaves it alone (the Integration page says what to do)
            self._watchdog_clear()
            self._ladder_reset()
            return
        now = time.monotonic()
        if state not in ("error", "degraded") or (state == "degraded" and not cfg["on_degraded"]):
            # degraded is kept on purpose unless the operator opted in (a version that did set up), and stopped
            # is an operator's decision: neither is acted on
            self._watchdog_clear()
            if state == "ok":
                inst.watchdog_recovered()
                if self._reloaded_at is not None:
                    self._ok_since = self._ok_since or now
                    if now - self._ok_since >= cfg["after_min"] * 60:
                        events.emit("health", f"health watchdog: {inst.state.domain or 'the integration'} has been ok for "
                                              f"{int((now - self._ok_since) / 60)} min since the reload; the ladder starts "
                                              "again from the reload", integration=inst.state.domain)
                        self._ladder_reset()
            elif state == "stopped":
                self._ladder_reset()
            return
        self._ok_since = None  # an ok blip after a reload is over: the ladder stays where it is
        if self._bad_since is None:
            self._bad_since = now
        unhealthy = now - self._bad_since
        window = inst.watchdog_window_s()
        reason = verdict.get("reason") or "no reason given"
        was = "in error" if state == "error" else state
        domain = inst.state.domain or "the integration"
        skip = None
        if self._reloaded_at is None:
            # a YAML-only integration has nothing to reload, and the reloads have a daily cap of their own
            skip = (None if inst.watchdog_reloadable() else "no config entry to reload") or inst.watchdog_reload_cap_refusal()
        reload_step = self._reloaded_at is None and not skip
        # the restart waits a whole window after the reload, whatever the verdict did in between
        since_reload = None if self._reloaded_at is None else now - self._reloaded_at
        inst.watchdog_pending = {"bad_for_s": int(unhealthy), "window_s": window, "reason": verdict.get("reason") or "",
                                 "state": state, "next": "reload" if reload_step else "restart"}
        if unhealthy < window or (since_reload is not None and since_reload < window):
            return
        if skip and not self._reload_skip_said:
            self._reload_skip_said = True
            events.emit("health", f"health watchdog: no reload for {domain} ({skip}): the next step is a restart",
                        domain=inst.state.domain)
        if not reload_step:
            cap = inst.watchdog_cap_refusal()
            if cap:
                why, give_up = cap
                if give_up:
                    inst.watchdog_give_up(why)  # said once, then quiet until an ok verdict or the window moves on
                elif not self._refused:
                    self._refused = True
                    events.emit("restart", f"health watchdog: {domain} is {was} ({reason}) but nothing is restarted: {why}",
                                domain=inst.state.domain)
                return
        refusal = self._live_refusal() or await self.hass.async_add_executor_job(self._scheduled_refusal)
        if refusal:
            if not self._refused:  # one line for this stretch, not one a minute
                self._refused = True
                events.emit("restart", f"health watchdog: {domain} is {was} ({reason}) but nothing is "
                                       f"{'reloaded' if reload_step else 'restarted'}: {refusal}",
                            domain=inst.state.domain)
            return
        self._refused = False
        inst.watchdog_pending = None
        if reload_step:
            self._reloaded_at = now  # before the await: a tick that lands while it runs goes on to the restart step
            await inst.watchdog_reload(state, reason, unhealthy, window)
            return
        self._bad_since = None  # the next stretch is measured from the boot this restart leads to
        self._ladder_reset()
        await inst.watchdog_restart(reason, unhealthy, state)
