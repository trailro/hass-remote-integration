"""integration_manager: run one custom integration in this headless HA headlessly and expose a small UI."""

from __future__ import annotations

import functools
import logging
from collections.abc import Callable
from typing import Any

import jsonio

from homeassistant.core import HomeAssistant
from homeassistant.helpers.typing import ConfigType

from .flows import FlowDriver
from .installer import Installer, track_delayed_stores
from . import events, notifications, writer
from .build_views import BuildCheckView, BuildOptionsView, BuildPageView, BuildPrepareView, DevInstallView, DevView, PreflightView
from .manage_views import EventsView, InstalledActionView, PatchActionView, PatchEditView, PatchReadView, PatchUploadView, PatchesView, ReleaseCheckView, ReleasePreviewView, RunView, SettingsView, YamlView
from .mqtt_publisher import MqttPublisher
from .backup_views import BackupActionView, BackupCreateView, BackupsView, BackupUploadView, RestoreCancelView
from .ha_import import RegistryAligner, async_finish_rebuild
from .import_views import ImportApplyAllView, ImportApplyView, ImportClearView, ImportInspectView, ImportUploadView
from .devices_page import DeviceActionView, DevicesApiView, DevicesPageView
from .entities_page import EntitiesApiView, EntitiesPageView, EntityActionView
from .logs_page import LogLevelView, LoggersApiView, LogsApiView, LogsPageView
from .logfiles_page import LogFileDownloadView, LogFilesPageView, LogFilesView, LogFileTailView
from .services_page import ServiceCallView, ServicesApiView, ServicesPageView
from .ha_updater import HaUpdater
from .scheduler import Scheduler
from .ui import StaticView
from .diagnostics import DiagnosticsView
from .memdiag import MemoryDiagView
from .manager_device import ManagerDevice, ManagerHistoryView, ManagerStatusView
from .catalog import Catalog, CatalogView
from .change_report import ChangeReportsView
from .parity import CutoverView, ParityActionView, ParityPageView, ParityView
from .views import (
    HaActionView,
    HaStatusView,
    RegistryView,
    MqttDiscoveryPreviewView,
    EntriesView,
    EntryActionView,
    FlowPageView,
    FlowProgressView,
    FlowResourceView,
    FlowStartView,
    IndexView,
    MqttPageView,
    SystemPageView,
    InstallView,
    MqttActionView,
    MqttRulesView,
    MqttCommandsView,
    MqttConfigView,
    MqttStatusView,
    OptionsResourceView,
    ReleasesView,
    SummaryView,
    RestartView,
    StatusView,
)

DOMAIN = "integration_manager"
_LOGGER = logging.getLogger(__name__)


def _recent(stamp: str, window_s: int = 900) -> bool:
    """A restore outcome written by the entrypoint minutes ago belongs to this boot."""
    import time

    try:
        return time.time() - time.mktime(time.strptime(stamp[:19], "%Y-%m-%dT%H:%M:%S")) < window_s
    except (TypeError, ValueError):
        return False


# Next to last_error in ha.json, not in state.json: a restore brings state.json back and leaves ha.json alone, so
# a marker there announced the same error again after every restore.  An ha.json the entrypoint rebuilds (the
# corrupt case) has no marker, so that error is announced again when it happens again.
HA_ERROR_REPORTED = "last_error_reported"


def _mark_ha_error_reported(path: str, error: str) -> None:
    """Blocking: under ha.json's update lock (run.py and the version-change views write it too); only while the
    file still holds that error, and never over a file that does not read as an object."""
    jsonio.update_json(path, lambda state: {**state, HA_ERROR_REPORTED: error}
                       if isinstance(state, dict) and state.get("last_error") == error else None)


async def async_announce_ha_error(hass: HomeAssistant, ha_state: Any) -> None:
    """Boot: the error the entrypoint left in ha.json (a failed version change, a rebuilt ha.json), once."""
    from homeassistant.const import __version__ as ha_version

    ha_error = ha_state.get("last_error") if isinstance(ha_state, dict) else None
    if not ha_error or ha_error == ha_state.get(HA_ERROR_REPORTED):
        return
    from homeassistant.components import persistent_notification as ha_pn

    events.emit("ha", ha_error, version=ha_version)
    ha_pn.async_create(hass, ha_error, title="Home Assistant version", notification_id="hri_ha_version_error")
    try:
        await hass.async_add_executor_job(_mark_ha_error_reported, hass.config.path("integration_manager", "ha.json"), ha_error)
    except OSError as err:
        _LOGGER.warning("ha.json: the announced error is not recorded (%s): it is announced again at the next boot", err)


async def async_disable_foreign_entry(installer: Installer, result: dict) -> str | None:
    """Exactly one running integration: an entry a flow creates for any other
    domain is disabled (and enabled when that integration is started)."""
    domain = result.get("handler")
    entry = result.get("result")
    if not domain or entry is None or domain == DOMAIN or domain == installer.running:
        return None
    from homeassistant.exceptions import HomeAssistantError

    try:
        unloaded = await installer.async_suspend_entry(entry)
    except HomeAssistantError as err:  # UnknownEntry, OperationNotAllowed
        if entry.disabled_by is not None:
            # disabled and recorded, but Home Assistant refused to unload it: it runs until the process restarts
            return _restart_to_unload(installer, f"entry created DISABLED, but it did not unload ({err}): restart the process")
        return f"entry created but could not be disabled ({err}); stop/start will sort it out"
    if not unloaded or entry.state.value == "failed_unload":
        # as Installer._disable_entries: disabled, but its unload returned False and it keeps running
        return _restart_to_unload(installer, f"entry created DISABLED, but it did not unload ({entry.state.value}): restart the process")
    return f"entry created DISABLED: this container runs {installer.running or 'nothing'}; {domain} is not the integration installed here"


def _restart_to_unload(installer: Installer, note: str) -> str:
    installer.state.restart_required = True
    installer._save_state()  # noqa: SLF001
    return note


def boot_step(what: str, step: Callable[[], Any]) -> None:
    """A boot step that writes state.json, on a disk that is full.  entrypoint.save_state
    logs and boots anyway; the setup path here does the same, because the write failing
    used to raise out of async_setup, and then this component was not set up at all: the
    operator lost the very UI they free the disk with - right after a rollback or a
    restore, which is when the disk is most likely to be full.  What was not written is
    still in memory and is saved again at the next change; a boot after this one starts
    from the file, which holds the state from before this step."""
    try:
        step()
    except OSError as err:
        _LOGGER.error("state.json not written (%s): %s is kept in memory only; the manager comes up anyway", err, what)


async def async_hand_identity_over(installer: Installer, publisher: Any, before: str | None) -> None:
    """An integration that starts while the boot reconcile runs - the Environment
    builder's deferred start, or one adopted from its enabled config entries - is the one
    start path that does not go through a view, so nothing hands the publisher the new
    identity.  The publisher connects during that same reconcile, with the identity the
    installer had then: without this it keeps it, and either never connects at all ("no
    integration is running") while the UI already shows the integration started, or
    publishes the new integration's entities under hass_<old domain> - the main Home
    Assistant creates them with the wrong unique ids, and the identity sweep at the next
    restart deletes and recreates them, losing whatever was customised there.

    No result to pass on: async_run_pending_start returns nothing, so the stale-document
    clean-up a version switch does (res["pre_update_backup"]) is not asked for here; the
    reconnect below is what carries the identity."""
    if installer.instance_key == before:
        return
    try:
        await publisher.async_after_start({})
    except Exception:  # noqa: BLE001 - a broker that is down must not cost the boot its UI
        _LOGGER.exception("MQTT: the identity of the integration started at boot was not applied")


async def async_boot_reconcile(installer: Installer, publisher: Any) -> None:
    """Requirements live in the image's site-packages; after a rebuild they are gone while
    /config still has the integration and the recorded tag.  Runs as a background task:
    pip can take longer than Home Assistant's setup timeout for this component, which
    would fail the whole boot; run.py waits for it before it sets up the integration."""
    identity = installer.instance_key
    try:
        await installer.async_reconcile()
        await installer.async_run_pending_start()
    except Exception as err:  # noqa: BLE001 - the UI must come up so the user can fix it
        _LOGGER.exception("boot reconcile failed")
        installer.state.last_error = f"boot reconcile failed: {type(err).__name__}: {err}"
        boot_step("the boot reconcile error", installer._save_state)  # noqa: SLF001
    # not in a `finally`: a cancelled boot (Home Assistant stopping) has no identity to hand over
    await async_hand_identity_over(installer, publisher, identity)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    events.EVENTS = events.Events(hass.config.path("integration_manager", "events.jsonl"))
    track_delayed_stores()  # before the integration is set up: backups write its pending saves
    installer = await hass.async_add_executor_job(Installer, hass)  # reads state.json and settings.json, sweeps the version store
    hass.data[DOMAIN] = installer
    writer.async_register(hass)  # the ordered JSON writer (settings, MQTT config/rules) drains at the final write

    # DNS rebinding guard: the JSON/CORS gates only stop cross-origin pages; a
    # page whose hostname is re-pointed at this LAN IP is same-origin.  Only
    # Host values that cannot be an attacker's public name are served.
    from .hostguard import install_host_guard

    install_host_guard(hass, installer)

    # optional password (HRI_PASSWORD / HRI_PASSWORD_FILE): checked after the host guard
    from .auth import LoginPageView, LoginView, LogoutView, async_setup_auth

    auth = await async_setup_auth(hass)

    ha_updater = HaUpdater(hass)

    aligner = RegistryAligner(hass)
    aligner.async_start()
    # after a downgrade with a clean start; a background task, because it waits for
    # EVENT_HOMEASSISTANT_STARTED and a tracked task would hold up that very start
    hass.async_create_background_task(async_finish_rebuild(hass, aligner, installer), "integration_manager rebuild")

    flows = FlowDriver(hass)

    flows.on_entry_created = functools.partial(async_disable_foreign_entry, installer)

    # Entity -> MQTT translator: publishes every entity (state, attributes,
    # registry metadata, integration tag) as retained JSON; LWT on
    # <base>/status.  Connects only if enabled in integration_manager/mqtt.json.
    publisher = MqttPublisher(hass, key_provider=lambda: installer.instance_key, health_provider=installer.health,
                              rules_provider=installer.settings.health_for)
    installer.health_source = publisher.build_health
    manager_device = await hass.async_add_executor_job(ManagerDevice, hass, installer, ha_updater, publisher)  # reads its JSON files
    publisher.manager = manager_device
    manager_device.start()
    installer.on_domain_removed = publisher.async_clear_identity
    await publisher.async_start()

    # After the publisher exists, so a start this reconcile makes can hand it the new
    # identity (async_hand_identity_over); run.py waits for this task before it sets the
    # integration up, so the identity is right before the first entity is published.
    hass.data["integration_manager_ready"] = hass.async_create_background_task(
        async_boot_reconcile(installer, publisher), "integration_manager boot reconcile")

    scheduler = Scheduler(hass, installer)
    installer.scheduler = scheduler
    scheduler.start()
    notifications.async_watch(hass)

    from homeassistant.const import __version__ as ha_version

    ha_state = await hass.async_add_executor_job(jsonio.read_json, hass.config.path("integration_manager", "ha.json"), {}) or {}
    last_restore = ha_state.get("last_restore") if isinstance(ha_state, dict) else None
    events.emit("boot", f"Home Assistant {ha_version}; running {installer.state.domain or 'nothing'} {installer.running_tag or ''}".strip()
                + (f"; restart required" if installer.state.restart_required else ""), ha=ha_version)
    # each on its own: a state write that fails must not skip the steps after it either
    boot_step("the announced smoke verdict", installer.announce_smoke)  # a failed verdict whose automatic rollback restarted before it could be shown
    await async_announce_ha_error(hass, ha_state)
    boot_step("the released rollback backup", lambda: installer.release_rollback_backup(last_restore))
    if isinstance(last_restore, dict) and last_restore.get("at") and _recent(last_restore["at"]) \
            and last_restore["at"] != installer.state.last_restore_reported:
        installer.state.last_restore_reported = last_restore["at"]  # a later boot within the window must not report it again
        boot_step("the reported restore outcome", installer._save_state)
        events.emit("restore", f"{'applied' if last_restore.get('ok') else 'FAILED'}: {last_restore.get('files', 0)} files"
                    + (f" ({last_restore.get('error')})" if not last_restore.get("ok") else "")
                    + (f", pre-restore copy {last_restore.get('pre_restore')}" if last_restore.get("pre_restore") else ""))

    preflight_view = PreflightView(hass, installer)
    build_check = BuildCheckView(hass, installer, ha_updater, preflight_view)
    for view in (
        ImportUploadView(hass),
        ImportInspectView(hass, installer),
        ImportApplyView(hass, aligner, installer),
        ImportApplyAllView(hass, aligner, installer),
        ImportClearView(hass),
        BackupsView(hass, installer, ha_updater),
        BackupCreateView(hass, installer),
        SettingsView(installer),
        ReleaseCheckView(installer),
        BackupUploadView(hass),
        BackupActionView(hass, installer, ha_updater),
        RestoreCancelView(hass, installer),
        DevicesPageView(),
        DevicesApiView(hass, publisher),
        DeviceActionView(hass),
        LogFilesPageView(),
        LogFilesView(hass, installer),
        LogFileTailView(hass, installer),
        LogFileDownloadView(hass, installer),
        MemoryDiagView(hass),
        ManagerStatusView(manager_device),
        ManagerHistoryView(manager_device),
        CatalogView(Catalog(hass), installer),
        ChangeReportsView(hass, installer),
        LogsPageView(),
        LogsApiView(hass),
        LoggersApiView(hass),
        LogLevelView(),
        EntitiesPageView(),
        EntitiesApiView(hass, publisher),
        EntityActionView(hass, publisher),
        ServicesPageView(),
        ServicesApiView(hass),
        ServiceCallView(hass),
        MqttConfigView(publisher),
        MqttStatusView(publisher),
        MqttCommandsView(publisher),
        MqttDiscoveryPreviewView(publisher),
        MqttActionView(publisher),
        IndexView(),
        SystemPageView(),
        MqttPageView(),
        StaticView(),
        LoginPageView(auth),
        LoginView(auth),
        LogoutView(auth),
        StatusView(installer),
        RegistryView(installer),
        RunView(installer, publisher),
        InstalledActionView(installer, publisher),
        ReleasePreviewView(installer),
        PatchesView(hass, installer),
        YamlView(hass, installer),
        PatchUploadView(hass, installer),
        PatchActionView(hass, installer),
        PatchReadView(hass),
        PatchEditView(hass, installer),
        HaStatusView(ha_updater),
        DiagnosticsView(hass, installer, publisher, ha_updater),
        ParityPageView(),
        ParityView(hass, installer, publisher),
        ParityActionView(hass, installer, publisher),
        CutoverView(hass, installer, publisher),
        MqttRulesView(publisher),
        HaActionView(ha_updater, installer),
        ReleasesView(installer),
        SummaryView(installer, publisher),
        InstallView(installer, publisher),
        RestartView(installer),
        FlowPageView(),
        FlowStartView(flows, installer),
        FlowProgressView(flows),
        FlowResourceView(flows),
        OptionsResourceView(flows),
        EntriesView(flows),
        EntryActionView(flows),
        EventsView(hass),
        notifications.NotificationsView(hass),
        notifications.NotificationActionView(hass),
        notifications.NotificationsDismissAllView(hass),
        preflight_view,
        DevView(hass, installer),
        DevInstallView(hass, installer, publisher),
        BuildPageView(),
        BuildOptionsView(installer, ha_updater),
        build_check,
        BuildPrepareView(hass, installer, ha_updater, build_check, publisher),
    ):
        hass.http.register_view(view)

    _LOGGER.info(
        "integration_manager ready (running: %s %s, installed: %s)",
        installer.state.domain, installer.running_tag, {d: sorted(v.get("versions", {})) for d, v in installer.state.installed.items()},
    )
    return True
