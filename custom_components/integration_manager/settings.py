"""Manager settings that are neither MQTT config nor installer state:
``<config>/integration_manager/settings.json`` (mode 600, it may hold a
GitHub token).  The token is write-only through the API: status only says
whether one is set."""

from __future__ import annotations

import json
import logging
import os
import shutil
import time

from typing import Any

from . import events, writer

_LOGGER = logging.getLogger(__name__)
CORRUPT_KEEP = 3  # settings.json.corrupt-<stamp> copies kept, as state.json's

DEFAULTS: dict[str, Any] = {
    "backup_keep": 5, "github_token": "",
    "smoke_test_s": 300,        # after a start: verify health after this many seconds (0 = off)
    "auto_rollback": True,      # ...and run a full rollback when it fails after a version switch
    "release_check": True,      # weekly check of GitHub releases for every installed integration
    "backup_daily": False,      # daily backup at backup_daily_hour (local time)
    "backup_daily_hour": 3,
    "parent_ha_url": "",        # e.g. http://homeassistant.local:8123 (the consuming HA)
    "parent_ha_token": "",      # long-lived access token, write-only
    "allowed_hosts": "",        # extra Host names accepted by the DNS-rebinding guard (comma separated)
    "health_stale_s": 900,      # degraded when no entity reported for this long (default for every integration)
    "health_unavailable_pct": 50,  # degraded when at least this share of the entities is unavailable
    "health": {},               # per integration: {"<domain>": {"stale_s": 900, "mode": "periodic"|"event", "unavailable_pct": 50}}
    # Health watchdog: restart the process when the verdict stays "error" (never degraded, stopped or unconfigured)
    "watchdog": False,             # off by default: an automatic restart is never a surprise
    "watchdog_after_min": 15,      # the verdict must be "error" for this long without interruption
    "watchdog_min_interval_min": 60,  # at most one automatic restart in this many minutes
    "watchdog_max_per_day": 3,     # and at most this many in 24 h; then it gives up and says so
    "dev_source_dir": "/dev-src",  # bind-mounted directory to install an integration from (dev mode)
    "log_format": {},           # Log files page: {"pattern": regex with named groups, "hide", "dim", "color_by", "colors"}
    "resource_history_h": 48,   # Overview resource history: hours kept, one sample a minute (1-120)
}
# (lo, hi) for the watchdog numbers: the API and Settings.watchdog() clamp to the same bounds.
# after_min is never under 5: the health verdict is only republished every minute, and anything
# shorter would act on one or two samples.
WATCHDOG_BOUNDS = {"watchdog_after_min": (5, 720), "watchdog_min_interval_min": (15, 1440), "watchdog_max_per_day": (1, 24)}
HEALTH_MODES = ("periodic", "event")  # event: the integration only writes states on events, so silence is not a fault


class Settings:
    def __init__(self, state_dir: str, hass=None) -> None:
        """``hass``: raises the notification when the file cannot be used (reading it is thread-safe)."""
        self.path = os.path.join(state_dir, "settings.json")
        self.data = dict(DEFAULTS)
        self.load_error: str | None = None
        try:
            with open(self.path, encoding="utf-8") as fh:
                loaded = json.load(fh)
        except FileNotFoundError:
            return
        except OSError as err:
            self._report(f"settings.json cannot be read ({type(err).__name__}: {err}): the defaults are used until it can", hass)
            return
        except ValueError:  # also a text that is not UTF-8
            self._report(f"settings.json is not valid JSON: the defaults are used (tokens included) {self._keep_corrupt()}", hass)
            return
        if not isinstance(loaded, dict):
            self._report(f"settings.json is not a JSON object: the defaults are used (tokens included) {self._keep_corrupt()}", hass)
            return
        self.data.update({k: loaded[k] for k in DEFAULTS if k in loaded})

    def _keep_corrupt(self) -> str:
        """A copy of the file the next save replaces (it may hold the tokens): the newest CORRUPT_KEEP are kept."""
        kept = f"{self.path}.corrupt-{time.strftime('%Y%m%d-%H%M%S')}"
        try:
            shutil.copyfile(self.path, kept)
            os.chmod(kept, 0o600)
        except OSError as err:
            return f"and the damaged file could not be copied ({err}): the next save replaces it"
        folder, prefix = os.path.dirname(self.path), os.path.basename(self.path) + ".corrupt-"
        try:
            for old in sorted(n for n in os.listdir(folder) if n.startswith(prefix))[:-CORRUPT_KEEP]:
                os.remove(os.path.join(folder, old))
        except OSError:
            pass
        return f"and the damaged file is kept as {os.path.basename(kept)}: the next save replaces settings.json"

    def _report(self, message: str, hass) -> None:
        self.load_error = message
        _LOGGER.warning("%s", message)
        events.emit("error", message)
        if hass is not None:
            from homeassistant.components import persistent_notification as ha_pn

            ha_pn.create(hass, message, title="Manager settings reset", notification_id="hri_settings_unreadable")

    async def async_save(self) -> None:
        """Copied now, on the loop that changes it; written by the ordered writer."""
        await writer.async_write(self.path, self.data, indent=1, mode=0o600)

    @property
    def backup_keep(self) -> int:
        """Backups to keep after an automatic prune; 0 = all."""
        try:
            return max(0, int(self.data.get("backup_keep", 5)))
        except (TypeError, ValueError):
            return 5

    @property
    def github_token(self) -> str:
        return str(self.data.get("github_token") or "")

    def github_headers(self) -> dict[str, str]:
        h = {"Accept": "application/vnd.github+json", "User-Agent": "hass-remote-integration"}
        if self.github_token:
            h["Authorization"] = f"Bearer {self.github_token}"
        return h

    def int_(self, key: str, lo: int = 0, hi: int = 10**9) -> int:
        try:
            return min(hi, max(lo, int(self.data.get(key, DEFAULTS[key]))))
        except (TypeError, ValueError):
            return int(DEFAULTS[key])

    def bool_(self, key: str) -> bool:
        """A hand-edited settings.json may say "false": that is False, not a non-empty string."""
        value = self.data.get(key, DEFAULTS[key])
        if isinstance(value, str):
            word = value.strip().lower()
            if word in ("1", "true", "yes", "on"):
                return True
            if word in ("", "0", "false", "no", "off"):
                return False
            return bool(DEFAULTS[key])
        return bool(value)

    def health_for(self, domain: str | None) -> dict[str, Any]:
        """The effective health rules of one integration: its overrides on
        top of the global defaults."""
        base = {"stale_s": self.int_("health_stale_s", 60, 86400), "mode": "periodic",
                "unavailable_pct": self.int_("health_unavailable_pct", 1, 100)}
        per = self.data.get("health") or {}
        own = per.get(domain or "") if isinstance(per, dict) else None
        if isinstance(own, dict):
            try:
                if own.get("stale_s") is not None:
                    base["stale_s"] = min(86400, max(60, int(own["stale_s"])))
                if own.get("unavailable_pct") is not None:
                    base["unavailable_pct"] = min(100, max(1, int(own["unavailable_pct"])))
            except (TypeError, ValueError):
                pass
            if own.get("mode") in HEALTH_MODES:
                base["mode"] = own["mode"]
        return base

    def watchdog(self) -> dict[str, Any]:
        """The effective watchdog rules.  The bounds are the ones SettingsView
        enforces, applied again here: settings.json can be edited by hand."""
        return {"enabled": self.bool_("watchdog"),
                "after_min": self.int_("watchdog_after_min", *WATCHDOG_BOUNDS["watchdog_after_min"]),
                "min_interval_min": self.int_("watchdog_min_interval_min", *WATCHDOG_BOUNDS["watchdog_min_interval_min"]),
                "max_per_day": self.int_("watchdog_max_per_day", *WATCHDOG_BOUNDS["watchdog_max_per_day"])}

    @property
    def dev_source_dir(self) -> str:
        return str(self.data.get("dev_source_dir") or DEFAULTS["dev_source_dir"])

    def public(self) -> dict[str, Any]:
        return {"backup_keep": self.backup_keep, "github_token_set": bool(self.github_token),
                "smoke_test_s": self.int_("smoke_test_s", 0, 86400), "auto_rollback": self.bool_("auto_rollback"),
                "release_check": self.bool_("release_check"), "backup_daily": self.bool_("backup_daily"),
                "backup_daily_hour": self.int_("backup_daily_hour", 0, 23), 
                "parent_ha_url": str(self.data.get("parent_ha_url") or ""), "parent_ha_token_set": bool(self.data.get("parent_ha_token")),
                "allowed_hosts": str(self.data.get("allowed_hosts") or ""),
                "health_stale_s": self.int_("health_stale_s", 60, 86400), "health_unavailable_pct": self.int_("health_unavailable_pct", 1, 100),
                "health": self.data.get("health") if isinstance(self.data.get("health"), dict) else {},
                "dev_source_dir": self.dev_source_dir,
                "watchdog": self.bool_("watchdog"),
                "watchdog_after_min": self.int_("watchdog_after_min", *WATCHDOG_BOUNDS["watchdog_after_min"]),
                "watchdog_min_interval_min": self.int_("watchdog_min_interval_min", *WATCHDOG_BOUNDS["watchdog_min_interval_min"]),
                "watchdog_max_per_day": self.int_("watchdog_max_per_day", *WATCHDOG_BOUNDS["watchdog_max_per_day"]),
                "resource_history_h": self.int_("resource_history_h", 1, 120),
                "log_format": self.data.get("log_format") if isinstance(self.data.get("log_format"), dict) else {}}
