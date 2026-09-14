"""Headless Home Assistant boot for hass-remote-integration.

Mirrors the sequence in ``homeassistant.bootstrap.async_from_config_dict``
but sets up only the integrations we need: the two core ones, ``http`` on
our own port, and ``integration_manager``.  No frontend, no recorder, no
default integrations.
"""

from __future__ import annotations

import asyncio
import logging
import os

# HA's http component reads SETUP_PORT when it is imported: set it BEFORE any
# homeassistant import.  A port passed as config would only become a pending
# "trial" that the frontend websocket must promote; SETUP_PORT changes the
# built-in default, so the stored stable port equals ours on a fresh volume.
os.environ["SETUP_PORT"] = os.environ.get("HRI_PORT", "8087")

if os.environ.get("HRI_TRACEMALLOC"):  # before the heavy imports, so they are traced too
    import tracemalloc

    tracemalloc.start(max(1, int(os.environ["HRI_TRACEMALLOC"])))

import logbuffer  # /app/logbuffer.py: the process log on disk, for the manager UI
from jsonio import read_json, write_json
import shutil
import sys

from homeassistant import config_entries, core, loader
from homeassistant import config as conf_util
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core_config import async_process_ha_core_config
from homeassistant.setup import async_setup_component

CONFIG_DIR = os.environ.get("HRI_CONFIG", "/config")
HTTP_PORT = int(os.environ.get("HRI_PORT", "8087"))
MANAGER_SRC = "/app/manager_src/integration_manager"


_LOGGER = logging.getLogger("hass_remote_integration")


def _sync_manager_component() -> None:
    """Copy the manager integration from the image into the config dir.

    The HA loader only scans ``<config_dir>/custom_components``, and we
    want image updates to propagate, so overwrite on every boot.
    """
    dst = os.path.join(CONFIG_DIR, "custom_components", "integration_manager")
    if os.path.isdir(dst):
        shutil.rmtree(dst)
    shutil.copytree(MANAGER_SRC, dst)


async def _boot() -> int:
    os.makedirs(os.path.join(CONFIG_DIR, "custom_components"), exist_ok=True)
    _sync_manager_component()
    # Like the stock image (WORKDIR /config): the loader imports the
    # namespace package `custom_components` once and then drops the config
    # dir from sys.path, so its __path__ re-resolves against cwd ('').
    os.chdir(CONFIG_DIR)

    hass = core.HomeAssistant(CONFIG_DIR)
    if os.environ.get("HRI_DEBUGPY"):
        hass.data["hri_debugpy"] = _start_debugpy(os.environ["HRI_DEBUGPY"])
    # First thing bootstrap.async_setup_hass does after creating hass:
    # initialises hass.data for the loader (components, integrations,
    # preload platforms). Without it condition/trigger platform
    # preloading raises KeyError('preload_platforms').
    loader.async_setup(hass)
    # The loader pre-imports these platforms of every integration it loads,
    # in the executor, "so they never block the loop later".  Headless, they
    # never run: recorder alone drags sqlalchemy in (~30 MB), logbook the
    # same via homeassistant/logbook.py.  Anything that does need one still
    # imports it on demand.
    hass.data[loader.DATA_PRELOAD_PLATFORMS] = [
        p for p in hass.data[loader.DATA_PRELOAD_PLATFORMS] if p not in ("recorder", "logbook", "backup", "energy", "media_source", "hardware")
    ]
    hass.config.skip_pip = True  # integration_manager owns dependency installs

    # bootstrap.async_setup_hass does these two in this order: the upgrade
    # step reads configuration.yaml, so a default one must exist first
    # (we never load YAML config; the file only satisfies HA's bookkeeping).
    if not await conf_util.async_ensure_config_exists(hass):
        _LOGGER.error("Could not create a default configuration.yaml")
        return 1
    await hass.async_add_executor_job(conf_util.process_ha_config_upgrade, hass)
    await _mount_local_lib_path(CONFIG_DIR)

    config = {
        "homeassistant": {
            "name": "hass-remote-integration",
            "time_zone": os.environ.get("TZ", "UTC"),
            "unit_system": "metric",
        },
        # No "http" section on purpose: it would be migrated into the http
        # store as a pending trial (see SETUP_PORT above). Defaults are fine:
        # bind 0.0.0.0, port from SETUP_PORT.
        "integration_manager": {},
    }

    hass.config_entries = config_entries.ConfigEntries(hass, config)
    custom = await loader.async_get_custom_components(hass)
    _LOGGER.info("custom components found: %s", sorted(custom))
    # Regression guard for the namespace-package shadowing bug: the files
    # are on disk but the loader cannot see them.
    wanted = _running_domain()
    if wanted and os.path.isdir(os.path.join(CONFIG_DIR, "custom_components", wanted)) and wanted not in custom:
        _LOGGER.error(
            "%s is installed on disk but the loader did not find it; "
            "check that no other 'custom_components' directory shadows %s",
            wanted, os.path.join(CONFIG_DIR, "custom_components"),
        )
    if not await _load_base_functionality(hass):
        _LOGGER.error("Base functionality failed to load")
        return 1

    for domain in CORE_INTEGRATIONS:
        if not await async_setup_component(hass, domain, config):
            _LOGGER.error("Core integration %s failed", domain)
            return 1

    # Creates hass.auth, applies name/time zone/units.
    await async_process_ha_core_config(hass, config["homeassistant"])

    # The running integration's YAML config (integration_manager/yaml/<domain>.yaml)
    # goes into the boot config like a configuration.yaml section: YAML-only
    # integrations run, and the YAML -> config entry import of newer versions
    # works the standard way.  Before integration_manager: its reconcile may
    # enable (= set up) entries, and config entries are set up with this
    # same dict (hass.config_entries._hass_config).
    running = _running_domain()
    yaml_cfg = await hass.async_add_executor_job(_yaml_config_for, hass, running) if running else None
    if yaml_cfg is not None:
        config[running] = yaml_cfg
        _LOGGER.info("YAML config applied for %s (%s keys)", running, len(yaml_cfg))

    for domain in ("http", "integration_manager"):
        if not await async_setup_component(hass, domain, config):
            _LOGGER.error("Integration %s failed to set up", domain)
            return 1

    # Fail loudly rather than silently serving on 8123: the http store's
    # "stable" config wins over anything we pass, so verify what it chose.
    actual_port = hass.config.api.port if hass.config.api else None
    if actual_port != HTTP_PORT:
        _LOGGER.error(
            "http bound to port %s, expected %s; delete /config/.storage/http "
            "and make sure SETUP_PORT=%s is set",
            actual_port,
            HTTP_PORT,
            HTTP_PORT,
        )
        return 1

    # HA's bootstrap normally sets up every domain that owns a config entry;
    # we replaced bootstrap, so do it here (this is what loads the running
    # integration once its entry exists).  It runs once HA starts, when http
    # already listens: an integration whose setup hangs must not keep the
    # manager UI down.  Failures are logged, not fatal.
    from homeassistant.const import EVENT_HOMEASSISTANT_START, EVENT_HOMEASSISTANT_STARTED
    from homeassistant.core import callback

    async def _setup_domains() -> None:
        ready = hass.data.get("integration_manager_ready")
        if ready is not None:
            await ready  # the manager's boot reconcile (requirements, entries) comes first
        domains = set(hass.config_entries.async_domains())
        if running and (yaml_cfg is not None or running not in domains):
            domains.add(running)  # a YAML-only integration has no config entry yet
        for domain in sorted(domains):
            if domain in hass.config.components:
                continue
            try:
                if not await async_setup_component(hass, domain, config):
                    _LOGGER.error("Integration %s failed to set up", domain)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Integration %s failed to set up", domain)

    @callback
    def _on_start(_event) -> None:
        hass.async_create_task(_setup_domains(), "hass-remote-integration setup")  # tracked: STARTED waits for it

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_START, _on_start)

    # "boots fine" is only true once HA is actually started (platforms up,
    # serial port opened, MQTT connected): mark it from the STARTED event,
    # so a crash in the start phase still counts towards the fallback.
    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, lambda _e: _mark_boot_ok())

    @callback
    def _trim_later(_e=None) -> None:  # on the loop: call_later is not thread-safe
        hass.loop.call_later(30, lambda: hass.async_add_executor_job(_malloc_trim))
        hass.loop.call_later(3600, _trim_later)

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _trim_later)
    _LOGGER.info(
        "hass-remote-integration ready: HA %s, http on :%s, components=%s",
        HA_VERSION,
        HTTP_PORT,
        sorted(hass.config.components),
    )
    return await hass.async_run()


def _mark_boot_ok() -> None:
    """Tell entrypoint.py this venv boots (it counts consecutive failures).
    Runs in an executor thread while the UI may already schedule a version
    change: only the records of THIS version go, and the file is read again
    right before the write so the window for a lost update stays tiny."""
    path = os.path.join(CONFIG_DIR, "integration_manager", "ha.json")

    def settle(state: dict) -> dict:
        state["boot_failures"] = 0
        state.pop("fallback_from", None)
        change, recovery = state.get("change"), state.get("recovery")
        if not isinstance(change, dict) or change.get("to") == HA_VERSION:
            state.pop("change", None)  # a version change is done once its version booted
        if not isinstance(recovery, dict) or recovery.get("for") == HA_VERSION:
            state.pop("recovery", None)  # and the recovery that brought this version back is done
        return state

    if not isinstance(read_json(path), dict):
        return
    state = read_json(path)
    if not isinstance(state, dict):
        return
    try:
        write_json(path, settle(state))
    except OSError:
        pass


def _quiet_loggers() -> list[str]:
    """Loggers the registry marks as chatty for the running integration: the
    image's registry.json, overridden by the user registry on the volume."""
    domain = _running_domain()
    if not domain:
        return []
    spec: dict = {}
    for path in ("/app/registry.json", os.path.join(CONFIG_DIR, "integration_manager", "registry.json")):
        own = ((read_json(path, {}) or {}).get("integrations") or {}).get(domain)
        if isinstance(own, dict):
            spec.update(own)
    return list(spec.get("quiet_loggers") or [f"custom_components.{domain}"])


CORE_INTEGRATIONS = ("homeassistant", "persistent_notification")  # bootstrap.CORE_INTEGRATIONS


async def _mount_local_lib_path(config_dir: str) -> str:
    """bootstrap.async_mount_local_lib_path without importing bootstrap."""
    from homeassistant.util.package import async_get_user_site

    deps_dir = os.path.join(config_dir, "deps")
    if (lib_dir := await async_get_user_site(deps_dir)) not in sys.path:
        sys.path.insert(0, lib_dir)
    return deps_dir


async def _load_base_functionality(hass) -> bool:
    """bootstrap.async_load_base_functionality, minus recovery mode.

    Importing homeassistant.bootstrap costs ~40 MB of RSS here (it
    pre-imports recorder/sqlalchemy, logbook, lovelace, PIL, ... for the
    frontend), none of which a headless instance ever uses.
    """
    import mimetypes
    import platform

    from homeassistant.helpers import (
        area_registry, category_registry, condition, device_registry, entity, entity_registry, floor_registry, frame,
        issue_registry, label_registry, restore_state, template, translation, trigger,
    )
    from homeassistant.helpers.storage import get_internal_store_manager
    from homeassistant.helpers.system_info import async_get_system_info
    from homeassistant.util.async_ import create_eager_task
    from homeassistant.util.hass_dict import HassKey
    from homeassistant.util.package import is_docker_env
    from homeassistant.util.system_info import is_official_image

    hass.data[HassKey("bootstrap_registries_loaded")] = None  # what bootstrap sets, for anything that checks it
    entity.async_setup(hass)
    frame.async_setup(hass)
    template.async_setup(hass)
    translation.async_setup(hass)
    device_registry.async_setup(hass)

    def _blocking_io_warmup() -> None:
        _ = platform.uname().processor
        mimetypes.init()
        is_official_image()
        is_docker_env()

    try:
        await asyncio.gather(
            create_eager_task(get_internal_store_manager(hass).async_initialize()),
            create_eager_task(area_registry.async_load(hass)),
            create_eager_task(category_registry.async_load(hass)),
            create_eager_task(device_registry.async_load(hass)),
            create_eager_task(entity_registry.async_load(hass)),
            create_eager_task(floor_registry.async_load(hass)),
            create_eager_task(issue_registry.async_load(hass)),
            create_eager_task(label_registry.async_load(hass)),
            hass.async_add_executor_job(_blocking_io_warmup),
            create_eager_task(template.async_load_custom_templates(hass)),
            create_eager_task(restore_state.async_load(hass)),
            create_eager_task(hass.config_entries.async_initialize()),
            create_eager_task(async_get_system_info(hass)),
            create_eager_task(condition.async_setup(hass)),
            create_eager_task(trigger.async_setup(hass)),
        )
    except Exception:  # noqa: BLE001
        _LOGGER.exception("loading the registries failed")
        return False
    return True


def _start_debugpy(port_s: str) -> dict:
    """HRI_DEBUGPY=<port>: listen for a debugger (VS Code "attach") on that
    port; debugpy is installed into the venv on first use.  Dev mode only."""
    try:
        port = int(port_s)
    except ValueError:
        return {"enabled": True, "listening": False, "error": f"HRI_DEBUGPY={port_s!r} is not a port"}
    try:
        try:
            import debugpy
        except ImportError:
            import subprocess

            _LOGGER.info("installing debugpy into the venv (HRI_DEBUGPY=%s)", port)
            subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "debugpy"], check=True, timeout=300)
            import debugpy
        debugpy.listen(("0.0.0.0", port))
        _LOGGER.warning("debugpy listening on :%s (attach a debugger; never expose this port)", port)
        return {"enabled": True, "listening": True, "port": port}
    except Exception as err:  # noqa: BLE001
        _LOGGER.error("debugpy could not start: %s", err)
        return {"enabled": True, "listening": False, "port": port, "error": f"{type(err).__name__}: {err}"}


def _malloc_trim() -> None:
    """Give freed heap pages back to the OS (glibc keeps them otherwise);
    once after boot, then hourly.  A few MB, sometimes tens after a big
    republish or an import."""
    try:
        import ctypes

        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except Exception:  # noqa: BLE001
        pass


def _install_import_tracer() -> None:
    """HRI_TRACE_IMPORT=<module prefix>: log the stack that first imports a
    module (which code path drags sqlalchemy/recorder into a headless HA?)."""
    want = tuple(x.strip() for x in os.environ.get("HRI_TRACE_IMPORT", "").split(",") if x.strip())
    if not want:
        return
    import importlib.abc
    import traceback

    class _Tracer(importlib.abc.MetaPathFinder):
        seen: set[str] = set()

        def find_spec(self, fullname, path, target=None):  # noqa: D401
            root = fullname.split(".")[0]
            if fullname.startswith(want) and root not in self.seen:
                self.seen.add(root)
                _LOGGER.warning("IMPORT TRACE %s\n%s", fullname, "".join(traceback.format_stack(limit=25)))
            return None

    sys.meta_path.insert(0, _Tracer())


def _running_domain() -> str | None:
    """The domain this boot runs: the running one, or the one a deferred
    start (environment builder) brings up at this boot on this HA version;
    its YAML goes into the boot config and it is set up after the manager."""
    state = read_json(os.path.join(CONFIG_DIR, "integration_manager", "state.json"), {})
    if not isinstance(state, dict):
        return None
    ps = state.get("pending_start")
    if isinstance(ps, dict) and ps.get("domain") and (not ps.get("ha") or ps["ha"] == HA_VERSION):
        return ps["domain"]
    return state.get("domain") or None


def _yaml_config_for(hass, domain: str) -> dict | None:
    """<config>/integration_manager/yaml/<domain>.yaml -> the integration's
    YAML section (what would sit under `<domain>:` in configuration.yaml).
    HA tags (!secret from secrets.yaml, !include) work.  Only the RUNNING
    integration's file is applied, at boot."""
    from homeassistant.util.yaml import load_yaml

    path = os.path.join(CONFIG_DIR, "integration_manager", "yaml", f"{domain}.yaml")
    if not os.path.isfile(path):
        return None
    try:
        from homeassistant.util.yaml.loader import Secrets

        data = load_yaml(path, Secrets(hass.config.path()))
    except Exception as err:  # noqa: BLE001 - the UI validated it, but the file is user-editable
        _LOGGER.error("YAML config for %s is invalid, ignored: %s", domain, err)
        return None
    if data is None or data == {}:
        return None
    if not isinstance(data, dict):
        _LOGGER.error("YAML config for %s must be a mapping (the content of the '%s:' section), ignored", domain, domain)
        return None
    return data


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    logbuffer.install(os.path.join(CONFIG_DIR, "integration_manager", "process.log"))  # before HA boots: /logs shows the boot too
    _install_import_tracer()
    # chatty loggers (the registry's quiet_loggers) would flood the
    # container log at INFO; it gets only their problems.
    for name in _quiet_loggers():
        logging.getLogger(name).setLevel(logging.WARNING)

    class _OwnLoaderNoise(logging.Filter):
        """HA warns about every custom integration it finds; the manager is
        part of this appliance, that line is noise here (integrations that
        are installed keep theirs)."""

        def filter(self, record: logging.LogRecord) -> bool:
            msg = record.getMessage()
            return not ("custom integration integration_manager" in msg and "not been tested" in msg)

    logging.getLogger("homeassistant.loader").addFilter(_OwnLoaderNoise())
    if os.environ.get("HRI_DEBUG"):
        logging.getLogger("custom_components.integration_manager").setLevel(logging.DEBUG)
    return asyncio.run(_boot())


if __name__ == "__main__":
    sys.exit(main())
