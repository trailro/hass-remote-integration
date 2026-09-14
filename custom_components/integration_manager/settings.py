"""Manager settings that are neither MQTT config nor installer state:
``<config>/integration_manager/settings.json`` (mode 600, it may hold a
GitHub token).  The token is write-only through the API: status only says
whether one is set."""

from __future__ import annotations

import json
import os

from jsonio import write_json
from typing import Any

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
    "dev_source_dir": "/dev-src",  # bind-mounted directory to install an integration from (dev mode)
    "log_format": {},           # Log files page: {"pattern": regex with named groups, "hide", "dim", "color_by", "colors"}
}
HEALTH_MODES = ("periodic", "event")  # event: the integration only writes states on events, so silence is not a fault


class Settings:
    def __init__(self, state_dir: str) -> None:
        self.path = os.path.join(state_dir, "settings.json")
        self.data = dict(DEFAULTS)
        try:
            with open(self.path, encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                self.data.update({k: loaded[k] for k in DEFAULTS if k in loaded})
        except (OSError, ValueError):
            pass

    def save(self) -> None:
        write_json(self.path, self.data, indent=1, mode=0o600)

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
        return bool(self.data.get(key, DEFAULTS[key]))

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
                "log_format": self.data.get("log_format") if isinstance(self.data.get("log_format"), dict) else {}}
