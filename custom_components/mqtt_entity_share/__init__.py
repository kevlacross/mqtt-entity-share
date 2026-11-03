"""Sender-side MQTT entity sharing with native Home Assistant selection."""
import asyncio
from homeassistant.components import mqtt
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.storage import Store

from .const import DOMAIN
from .runtime import ShareRuntime, cleanup_topics
from .bridge import ShareBridge
from .received import ReceivedShares


async def async_setup_entry(hass, entry):
    if not await mqtt.async_wait_for_mqtt_client(hass):
        raise ConfigEntryNotReady("MQTT is not connected")
    runtime = ShareRuntime(hass, entry)
    entry.runtime_data = runtime
    runtime.bridge = None
    if entry.data.get("remote_bridge"):
        runtime.bridge = await hass.async_add_executor_job(ShareBridge, hass, entry)
        await runtime.bridge.async_start()
        try:
            await asyncio.wait_for(runtime.bridge.connected.wait(), timeout=20)
        except TimeoutError as err:
            await runtime.bridge.async_stop()
            raise ConfigEntryNotReady("Secure MQTT share bridge is not connected") from err
    runtime.received = ReceivedShares(hass, entry)
    try:
        await runtime.async_start()
        await runtime.received.async_start()
    except Exception:
        await runtime.received.async_stop()
        await runtime.async_stop()
        if runtime.bridge:
            await runtime.bridge.async_stop()
        raise
    entry.async_on_unload(entry.add_update_listener(_options_updated))
    return True


async def _options_updated(hass, entry):
    await entry.runtime_data.async_apply(entry.options)
    await entry.runtime_data.received.async_apply(entry.options)


async def async_unload_entry(hass, entry):
    await entry.runtime_data.received.async_stop()
    await entry.runtime_data.async_stop()
    if entry.runtime_data.bridge:
        await entry.runtime_data.bridge.async_stop()
    return True


async def async_remove_entry(hass, entry):
    await ReceivedShares(hass, entry).async_remove()
    store = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}")
    data = await store.async_load() or {}
    await cleanup_topics(hass, data.get("topics", {}))
    await store.async_remove()
