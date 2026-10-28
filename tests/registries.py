"""Home Assistant's real (empty) device and entity registries and config entries, loaded for a test."""

import asyncio

from homeassistant import config_entries as ce
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er


async def load_registries(hass):
    """The device and entity registries plus a real ConfigEntries, loaded as bootstrap loads them: side by side,
    since from Home Assistant 2026.11 the entity registry load waits for the config entries to be initialised
    (it derives disabled_by from them) and a hass without config_entries fails it."""
    dr.async_setup(hass)
    await dr.async_load(hass, load_empty=True)
    hass.config_entries = ce.ConfigEntries(hass, {})
    await asyncio.gather(er.async_load(hass, load_empty=True), hass.config_entries.async_initialize())
