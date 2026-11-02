"""Sender-side MQTT entity sharing with native Home Assistant selection."""
from homeassistant.components import mqtt
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.storage import Store

from .const import DOMAIN
from .runtime import ShareRuntime, cleanup_topics


async def async_setup_entry(hass, entry):
    if not await mqtt.async_wait_for_mqtt_client(hass):
        raise ConfigEntryNotReady("MQTT is not connected")
    runtime = ShareRuntime(hass, entry)
    entry.runtime_data = runtime
    await runtime.async_start()
    entry.async_on_unload(entry.add_update_listener(_options_updated))
    return True


async def _options_updated(hass, entry):
    await entry.runtime_data.async_apply(entry.options)


async def async_unload_entry(hass, entry):
    await entry.runtime_data.async_stop()
    return True


async def async_remove_entry(hass, entry):
    store = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}")
    data = await store.async_load() or {}
    await cleanup_topics(hass, data.get("topics", {}))
    await store.async_remove()
