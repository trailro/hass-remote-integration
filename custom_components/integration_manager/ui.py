"""Shared chrome for every page: the top bar (brand, navigation with the
active page, live chips from /api/summary) and the static assets under
``static/`` (shared css/js plus one css file per page), served by
``StaticView`` with immutable cache headers keyed by content hash.
``render()`` injects the links and the bar into a page's HTML."""

from __future__ import annotations

import hashlib
import html
import json
import os

from aiohttp import web

from .http_util import ManagerView

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
TEMPLATE_DIR = os.path.join(os.path.dirname(__file__), "templates")


def load_template(page: str) -> str:
    """The page's HTML (templates/<page>.html), read once at import; its
    script lives in static/<page>.js and is linked by render()."""
    with open(os.path.join(TEMPLATE_DIR, f"{page}.html"), encoding="utf-8") as fh:
        return fh.read()
PAGES = [
    ("/", "Overview", "index"), ("/config", "Integration", "config"), ("/install", "Install", "install"), ("/mqtt", "MQTT", "mqtt"),
    ("/parity", "Cutover", "parity"), ("/entities", "Entities", "entities"), ("/devices", "Devices", "devices"),
    ("/services", "Services", "services"), ("/logs", "Logs", "logs"), ("/logfiles", "Log files", "logfiles"), ("/system", "System", "system"),
]
_TYPES = {".css": "text/css", ".js": "application/javascript"}


def _version() -> str:
    """Content hash of every static file: the cache key in the asset URLs."""
    h = hashlib.sha1()
    for name in sorted(os.listdir(STATIC_DIR)):
        with open(os.path.join(STATIC_DIR, name), "rb") as fh:
            h.update(name.encode() + fh.read())
    return h.hexdigest()[:10]


ASSET_VERSION = _version()
RELEASES_URL = "https://github.com/trailro/hass-remote-integration/releases"


def _manager_version() -> str:
    try:
        with open(os.path.join(os.path.dirname(__file__), "manifest.json"), encoding="utf-8") as fh:
            return str(json.load(fh).get("version") or "")
    except (OSError, ValueError):
        return ""


MANAGER_VERSION = _manager_version()
MANAGER_BUILD = (os.environ.get("HRI_BUILD") or "local").strip() or "local"  # commit the image was built from


def version_info() -> dict[str, str]:
    return {"version": MANAGER_VERSION, "build": MANAGER_BUILD, "build_short": MANAGER_BUILD[:7] if MANAGER_BUILD != "local" else "local"}
_ASSETS = frozenset(os.listdir(STATIC_DIR))  # no directory listing on the event loop per request


class StaticView(ManagerView):
    url = "/static/{name}"

    async def get(self, request: web.Request, name: str) -> web.Response:
        ext = os.path.splitext(name)[1]
        if ext not in _TYPES or name not in _ASSETS:
            return web.Response(status=404, text="no such asset")
        headers = {"Cache-Control": "public, max-age=31536000, immutable"} if request.query.get("v") else {"Cache-Control": "no-cache"}
        return web.FileResponse(os.path.join(STATIC_DIR, name), headers={**headers, "Content-Type": _TYPES[ext]})


def topbar(active: str) -> str:
    links = "".join(f'<a class="nav{" active" if path == active else ""}" href="{path}"'
                    f'{" id=\"nav-logfiles\" hidden" if path == "/logfiles" else ""}>{label}</a>' for path, label, _ in PAGES)
    v = version_info()
    href = f"{RELEASES_URL}/tag/v{v['version']}" if v["version"] else RELEASES_URL
    ver = (f'<a class="ver" href="{html.escape(href)}" target="_blank" rel="noopener" '
           f'title="hass-remote-integration {html.escape(v["version"])}, build {html.escape(v["build"])}">'
           f'v{html.escape(v["version"])} · {html.escape(v["build_short"])}</a>')
    return (f'<nav class="topbar"><a class="brand" href="/">hass<b>-remote-</b>integration</a>{ver}{links}'
            f'<span class="spacer"></span><span id="tb-chips"></span></nav>')


def render(page_html: str, active: str) -> str:
    """Inject the stylesheets, the shared script and the bar into a page."""
    page = next((p for path, _, p in PAGES if path == active), "index")
    links = (f'<link rel="stylesheet" href="/static/hri.css?v={ASSET_VERSION}">'
             f'<link rel="stylesheet" href="/static/{page}.css?v={ASSET_VERSION}">')
    out = page_html.replace("<!--css-->", links, 1)
    out = out.replace("<!--js-->", f'<script src="/static/{page}.js?v={ASSET_VERSION}"></script>', 1)
    return out.replace("<body>", f'<body><script src="/static/hri.js?v={ASSET_VERSION}"></script>' + topbar(active), 1)
