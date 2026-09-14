"""integration_manager: run one custom integration in this headless HA headlessly and expose a small UI."""

from __future__ import annotations

import logging

import jsonio

from homeassistant.core import HomeAssistant
from homeassistant.helpers.typing import ConfigType

from .flows import FlowDriver
from .installer import Installer
from . import events, notifications
from .build_views import BuildCheckView, BuildOptionsView, BuildPageView, BuildPrepareView, DevInstallView, DevView, PreflightView
from .manage_views import EventsView, InstalledActionView, PatchActionView, PatchUploadView, PatchesView, ReleaseCheckView, ReleasePreviewView, RunView, SettingsView, YamlView
from .mqtt_publisher import MqttPublisher
from .backup_views import BackupActionView, BackupCreateView, BackupsView, BackupUploadView, RestoreCancelView
from .ha_import import RegistryAligner, async_finish_rebuild
from .import_views import ImportApplyAllView, ImportApplyView, ImportClearView, ImportInspectView, ImportUploadView
from .devices_page import DeviceActionView, DevicesApiView, DevicesPageView
from .entities_page import EntitiesApiView, EntitiesPageView, EntityActionView
from .logs_page import LogLevelView, LoggersApiView, LogsApiView, LogsPageView
from .logfiles_page import LogFilesPageView, LogFilesView, LogFileTailView
from .services_page import ServiceCallView, ServicesApiView, ServicesPageView
from .ha_updater import HaUpdater
from .scheduler import Scheduler
from .ui import StaticView
from .diagnostics import DiagnosticsView
from .memdiag import MemoryDiagView
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


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    events.EVENTS = events.Events(hass.config.path("integration_manager", "events.jsonl"))
    installer = Installer(hass)
    hass.data[DOMAIN] = installer

    # Requirements live in the image's site-packages; after a rebuild they
    # are gone while /config still has the integration and the recorded tag.
    # A background task: pip can take longer than HA's setup timeout for this
    # component, which would fail the whole boot; run.py waits for it before
    # it sets up the integration.
    async def _boot_reconcile() -> None:
        try:
            await installer.async_reconcile()
            await installer.async_run_pending_start()
        except Exception as err:  # noqa: BLE001 - the UI must come up so the user can fix it
            _LOGGER.exception("boot reconcile failed")
            installer.state.last_error = f"boot reconcile failed: {type(err).__name__}: {err}"
            installer._save_state()

    hass.data["integration_manager_ready"] = hass.async_create_background_task(_boot_reconcile(), "integration_manager boot reconcile")

    # DNS rebinding guard: the JSON/CORS gates only stop cross-origin pages; a
    # page whose hostname is re-pointed at this LAN IP is same-origin.  Only
    # Host values that cannot be an attacker's public name are served.
    from .hostguard import install_host_guard

    install_host_guard(hass, installer)

    ha_updater = HaUpdater(hass)

    aligner = RegistryAligner(hass)
    aligner.async_start()
    # after a downgrade with a clean start; a background task, because it waits for
    # EVENT_HOMEASSISTANT_STARTED and a tracked task would hold up that very start
    hass.async_create_background_task(async_finish_rebuild(hass, aligner, installer), "integration_manager rebuild")

    flows = FlowDriver(hass)

    async def _on_entry_created(result):
        """Exactly one running integration: an entry of any other domain is
        created disabled (it is enabled when that integration is started)."""
        domain = result.get("handler")
        entry = result.get("result")
        if not domain or entry is None or domain == DOMAIN or domain == installer.running:
            return None
        from homeassistant.config_entries import ConfigEntryDisabler
        from homeassistant.exceptions import HomeAssistantError

        try:
            await hass.config_entries.async_set_disabled_by(entry.entry_id, ConfigEntryDisabler.USER)
        except HomeAssistantError as err:  # UnknownEntry, OperationNotAllowed
            return f"entry created but could not be disabled ({err}); stop/start will sort it out"
        return f"entry created DISABLED: this container runs {installer.running or 'nothing'}; {domain} is not the integration installed here"

    flows.on_entry_created = _on_entry_created

    # Entity -> MQTT translator: publishes every entity (state, attributes,
    # registry metadata, integration tag) as retained JSON; LWT on
    # <base>/status.  Connects only if enabled in integration_manager/mqtt.json.
    publisher = MqttPublisher(hass, key_provider=lambda: installer.instance_key, health_provider=installer.health,
                              rules_provider=installer.settings.health_for)
    installer.health_source = publisher.build_health
    installer.on_domain_removed = publisher.async_clear_identity
    await publisher.async_start()

    scheduler = Scheduler(hass, installer)
    installer.scheduler = scheduler
    scheduler.start()
    notifications.async_watch(hass)

    from homeassistant.const import __version__ as ha_version

    ha_state = jsonio.read_json(hass.config.path("integration_manager", "ha.json"), {}) or {}
    last_restore = ha_state.get("last_restore") if isinstance(ha_state, dict) else None
    events.emit("boot", f"Home Assistant {ha_version}; running {installer.state.domain or 'nothing'} {installer.running_tag or ''}".strip()
                + (f"; restart required" if installer.state.restart_required else ""), ha=ha_version)
    if isinstance(last_restore, dict) and last_restore.get("ok") and installer.state.rollback_backup:
        installer.state.rollback_backup = None  # restored: the regular pruning applies to it again
        installer._save_state()
    if isinstance(last_restore, dict) and last_restore.get("at") and _recent(last_restore["at"]):
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
        BackupsView(hass, installer),
        BackupCreateView(hass, installer),
        SettingsView(installer),
        ReleaseCheckView(installer),
        BackupUploadView(hass),
        BackupActionView(hass, installer),
        RestoreCancelView(hass),
        DevicesPageView(),
        DevicesApiView(hass, publisher),
        DeviceActionView(hass),
        LogFilesPageView(),
        LogFilesView(hass, installer),
        LogFileTailView(hass, installer),
        MemoryDiagView(hass),
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
        StatusView(installer),
        RegistryView(installer),
        RunView(installer, publisher),
        InstalledActionView(installer, publisher),
        ReleasePreviewView(installer),
        PatchesView(hass, installer),
        YamlView(hass, installer),
        PatchUploadView(hass, installer),
        PatchActionView(hass, installer),
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
        InstallView(installer),
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
        DevInstallView(hass, installer),
        BuildPageView(),
        BuildOptionsView(installer, ha_updater),
        build_check,
        BuildPrepareView(hass, installer, ha_updater, build_check),
    ):
        hass.http.register_view(view)

    _LOGGER.info(
        "integration_manager ready (running: %s %s, installed: %s)",
        installer.state.domain, installer.running_tag, {d: sorted(v.get("versions", {})) for d, v in installer.state.installed.items()},
    )
    return True
