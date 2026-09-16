"""Home Assistant's persistent notifications, which a headless HA has no
front end to show: integrations use them for what they cannot say through
an entity (setup hints, faults, problems with the files it writes).
``GET /api/notifications`` lists them, ``POST /api/notifications/<id>/
dismiss`` and ``POST /api/notifications/dismiss_all`` remove them; every
new one is recorded in the timeline, the count is in the top bar, the
health document and the diagnostics zip."""

from __future__ import annotations

import asyncio
from typing import Any

from aiohttp import web
from homeassistant.components import persistent_notification as pn
from homeassistant.core import HomeAssistant, callback

from . import events
from .http_util import ManagerView, with_body

# One integration raising a hundred notifications at once filled the whole page /api/events returns (100
# rows) with `notify` lines and pushed every operational line off it.  Coalescing here rather than at the
# view keeps the file honest too: the timeline rotates at 512 KB, so a burst also cost the history on disk.
BURST_WINDOW_S = 2.0  # notifications raised within this of the first one go in as one line
BURST_MAX = 3         # up to this many still get a line each
BURST_TITLES = 3      # titles named in the summary line
BURST_IDS = 20        # notification_ids kept in its data


def _rows(hass: HomeAssistant) -> list[dict[str, Any]]:
    out = []
    for nid, n in pn._async_get_or_create_notifications(hass).items():  # noqa: SLF001 - what the websocket API reads
        created = n.get("created_at")
        out.append({"notification_id": nid, "title": n.get("title"), "message": n.get("message"),
                    "created_at": created.isoformat() if created else None})
    out.sort(key=lambda r: r["created_at"] or "", reverse=True)
    return out


def count(hass: HomeAssistant) -> int:
    return len(pn._async_get_or_create_notifications(hass))  # noqa: SLF001


@callback
def async_watch(hass: HomeAssistant) -> None:
    """Every notification an integration raises goes to the timeline, a burst
    of them as one line that still names how many there were."""

    seen: dict[str, tuple[Any, Any]] = {}
    pending: list[tuple[str, str, str]] = []  # (notification_id, title, line) waiting for the window to close
    timer: list[asyncio.TimerHandle] = []

    @callback
    def _flush() -> None:
        timer.clear()
        burst, pending[:] = list(pending), []
        if len(burst) <= BURST_MAX:
            for nid, _title, line in burst:
                events.emit("notify", line, notification_id=nid)
            return
        titles = list(dict.fromkeys(title for _nid, title, _line in burst))
        named = ", ".join(titles[:BURST_TITLES])
        if len(titles) > BURST_TITLES:
            named += f" and {len(titles) - BURST_TITLES} more"
        events.emit("notify", f"{len(burst)} notifications: {named}",
                    count=len(burst), notification_ids=[nid for nid, _, _ in burst][:BURST_IDS])

    @callback
    def _changed(update_type: pn.UpdateType, changed: dict[str, pn.Notification]) -> None:
        if update_type == getattr(pn.UpdateType, "REMOVED", None):
            for nid in changed:
                seen.pop(nid, None)
            return
        if update_type not in (pn.UpdateType.ADDED, pn.UpdateType.UPDATED):
            return
        for nid, n in changed.items():
            if seen.get(nid) == (n.get("title"), n.get("message")):
                continue  # re-created unchanged: one timeline line, not one per minute
            seen[nid] = (n.get("title"), n.get("message"))
            title = n.get("title") or nid
            pending.append((nid, title, f"{title}: {str(n.get('message') or '')[:160]}"))
        # the window runs from the first one pending, never restarted: a stream that does not let up
        # still costs one line per window instead of one per notification
        if pending and not timer:
            timer.append(hass.loop.call_later(BURST_WINDOW_S, _flush))

    pn.async_register_callback(hass, _changed)


class NotificationsView(ManagerView):
    url = "/api/notifications"

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request: web.Request) -> web.Response:
        return self.json({"notifications": _rows(self.hass)})


class NotificationActionView(ManagerView):
    url = "/api/notifications/{notification_id}/{action}"

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    @with_body
    async def post(self, request: web.Request, body: dict[str, Any], notification_id: str, action: str) -> web.Response:
        if action != "dismiss":
            return self.json_message("unknown action", status_code=400)
        if notification_id not in pn._async_get_or_create_notifications(self.hass):  # noqa: SLF001
            return self.json({"ok": False, "error": "no such notification"})
        pn.async_dismiss(self.hass, notification_id)
        return self.json({"ok": True, "dismissed": 1})


class NotificationsDismissAllView(ManagerView):
    url = "/api/notifications/dismiss_all"

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    @with_body
    async def post(self, request: web.Request, body: dict[str, Any]) -> web.Response:
        n = count(self.hass)
        pn.async_dismiss_all(self.hass)
        return self.json({"ok": True, "dismissed": n})
