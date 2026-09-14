"""Generic driver for Home Assistant config/options flows, in-process.

This is the same thing the official frontend does through
``/api/config/config_entries/flow``, minus the HTTP auth layer: we call
``hass.config_entries.flow`` / ``hass.config_entries.options`` directly
and serialise results exactly like ``helpers/data_entry_flow.py`` does,
so any integration's flow renders.
"""

from __future__ import annotations

from typing import Any


from homeassistant import data_entry_flow
from homeassistant.config_entries import SOURCE_USER, ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv


def prepare_result(result: data_entry_flow.FlowResult) -> dict[str, Any]:
    """Like HA's flow view serialisation, except that create_entry keeps a
    small {entry_id, title, domain} result (HA strips the entry object)."""
    if result["type"] is data_entry_flow.FlowResultType.CREATE_ENTRY:
        out = {k: v for k, v in result.items() if k not in ("data", "context")}
        entry = result.get("result")
        if entry is not None and hasattr(entry, "entry_id"):
            out["result"] = {"entry_id": entry.entry_id, "title": entry.title, "domain": entry.domain}
        return out
    data = dict(result)
    if "data_schema" not in result:
        return data
    schema = result["data_schema"]
    data["data_schema"] = (
        []
        if schema is None
        else _serialize_schema(schema)
    )
    return data


def _serialize_schema(schema: Any) -> Any:
    """HA 2026.9 replaced voluptuous with probatio: voluptuous_serialize
    returns the string "UNSUPPORTED" for its schemas; HA's own helper
    (to_field_list) knows them.  Older HA: voluptuous_serialize."""
    try:
        from homeassistant.helpers.data_entry_flow import to_field_list  # HA >= 2026.9
    except ImportError:
        to_field_list = None
    if to_field_list is not None:
        return to_field_list(schema, custom_serializer=cv.custom_serializer)  # errors surface to the UI, not an empty form
    import voluptuous_serialize  # HA < 2026.9 only

    out = voluptuous_serialize.convert(schema, custom_serializer=cv.custom_serializer)
    if not isinstance(out, list):
        raise ValueError(f"cannot serialise this form ({out!r}); the flow needs a newer Home Assistant")
    return out


def _jsonable(value: Any) -> Any:
    """Enums and other non-JSON values show up in results; stringify them."""
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return getattr(value, "value", str(value))


class FlowDriver:
    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    # ----- config flows ----------------------------------------------------

    on_entry_created = None  # async callable(result) -> str | None, set by the manager

    async def start(self, domain: str) -> dict[str, Any]:
        result = await self.hass.config_entries.flow.async_init(
            domain, context={"source": SOURCE_USER}
        )
        if result.get("type") == "create_entry" and self.on_entry_created is not None:
            # a flow that creates its entry on the first step
            note = await self.on_entry_created(result)
            if note:
                result = {**result, "manager_note": note}
        return _jsonable(prepare_result(result))

    async def configure(self, flow_id: str, user_input: dict[str, Any] | None) -> dict[str, Any]:
        result = await self.hass.config_entries.flow.async_configure(flow_id, user_input)
        if result.get("type") == "create_entry" and self.on_entry_created is not None:
            note = await self.on_entry_created(result)
            if note:
                result = {**result, "manager_note": note}
        return _jsonable(prepare_result(result))

    def abort(self, flow_id: str) -> None:
        self.hass.config_entries.flow.async_abort(flow_id)

    def in_progress(self) -> list[dict[str, Any]]:
        return _jsonable(
            [
                {"flow_id": f["flow_id"], "handler": f["handler"], "step_id": f.get("step_id")}
                for f in self.hass.config_entries.flow.async_progress()
            ]
        )

    # ----- options flows ---------------------------------------------------

    async def options_start(self, entry_id: str) -> dict[str, Any]:
        result = await self.hass.config_entries.options.async_init(entry_id)
        return _jsonable(prepare_result(result))

    async def options_configure(
        self, flow_id: str, user_input: dict[str, Any] | None
    ) -> dict[str, Any]:
        result = await self.hass.config_entries.options.async_configure(flow_id, user_input)
        return _jsonable(prepare_result(result))

    def options_abort(self, flow_id: str) -> None:
        self.hass.config_entries.options.async_abort(flow_id)

    # ----- entries ---------------------------------------------------------

    def entries(self, domain: str | None = None) -> list[dict[str, Any]]:
        out = []
        for e in self.hass.config_entries.async_entries(domain):
            out.append(self._entry_summary(e))
        return out

    def _entry_summary(self, e: ConfigEntry) -> dict[str, Any]:
        return {
            "entry_id": e.entry_id,
            "domain": e.domain,
            "title": e.title,
            "state": e.state.value,
            "reason": e.reason,
            "version": e.version,
            "minor_version": e.minor_version,
            "source": e.source,
            "supports_options": e.supports_options,
            "disabled_by": e.disabled_by.value if e.disabled_by else None,
            "data_keys": sorted(e.data.keys()),
            "options_keys": sorted(e.options.keys()),
        }

    async def remove_entry(self, entry_id: str) -> dict[str, Any]:
        return _jsonable(await self.hass.config_entries.async_remove(entry_id))

    async def reload_entry(self, entry_id: str) -> bool:
        return await self.hass.config_entries.async_reload(entry_id)
