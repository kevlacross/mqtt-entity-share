"""Identify received MQTT shares without changing their MQTT ownership."""
import asyncio
import re

from homeassistant.core import callback
from homeassistant.helpers import device_registry as dr, entity_registry as er, label_registry as lr
from homeassistant.helpers.storage import Store

from .const import DOMAIN

RECEIVED_LABEL = "MQTT-Share received"
SHARED_UNIQUE_ID = re.compile(r"^share_[0-9a-f]{10}_")


class ReceivedShares:
    """Optionally mark imported entities without altering device ownership."""

    def __init__(self, hass, entry):
        self.hass, self.entry = hass, entry
        self.store = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}.received")
        self.entities = er.async_get(hass)
        self.labels = lr.async_get(hass)
        self.devices = dr.async_get(hass)
        self.owned_labels = {}
        self.owned_device_labels = {}
        self.label_enabled = False
        self.closed = False
        self.dirty = False
        self.task = None
        self.lock = asyncio.Lock()
        self.unsubscribers = []

    async def _load(self):
        stored = await self.store.async_load() or {}
        self.owned_labels = stored.get("labels", {})
        self.owned_device_labels = stored.get("device_labels", {})

    async def async_start(self):
        await self._load()
        self.label_enabled = self.entry.options.get("label_received", False)
        for event_type in ("entity_registry_updated", "device_registry_updated", "label_registry_updated"):
            self.unsubscribers.append(self.hass.bus.async_listen(event_type, self._changed))
        await self.async_reconcile()

    @callback
    def _changed(self, event):
        if self.closed:
            return
        self.dirty = True
        if self.task is None or self.task.done():
            self.task = self.hass.async_create_task(self._drain())

    async def _drain(self):
        while self.dirty and not self.closed:
            self.dirty = False
            await self.async_reconcile()

    async def async_apply(self, options):
        self.label_enabled = options.get("label_received", False)
        await self.async_reconcile()

    def _imported(self):
        return {entity.entity_id: entity for entity in self.entities.entities.values()
                if entity.platform == "mqtt" and SHARED_UNIQUE_ID.match(str(entity.unique_id))}

    async def async_reconcile(self):
        async with self.lock:
            if self.closed:
                return
            imported = self._imported()
            label_id = None
            if self.label_enabled and imported:
                label = self.labels.async_get_label_by_name(RECEIVED_LABEL)
                if label is None:
                    label = self.labels.async_create(RECEIVED_LABEL, icon="mdi:download-network")
                label_id = label.label_id
            self._reconcile_labels(imported, label_id)
            self._reconcile_device_labels({entity.device_id for entity in imported.values() if entity.device_id}, label_id)
            await self.store.async_save({"labels": self.owned_labels, "device_labels": self.owned_device_labels})

    def _reconcile_labels(self, imported, label_id):
        # Only remove assignments this integration actually added.
        for entity_id, previous in list(self.owned_labels.items()):
            if entity_id not in imported or previous != label_id:
                entity = self.entities.async_get(entity_id)
                if entity and previous in entity.labels:
                    self.entities.async_update_entity(entity_id, labels=set(entity.labels) - {previous})
                self.owned_labels.pop(entity_id)
        if label_id:
            for entity_id in imported:
                entity = self.entities.async_get(entity_id)
                if entity and label_id not in entity.labels:
                    self.entities.async_update_entity(entity_id, labels=set(entity.labels) | {label_id})
                    self.owned_labels[entity_id] = label_id

    def _reconcile_device_labels(self, imported_devices, label_id):
        for device_id, previous in list(self.owned_device_labels.items()):
            if device_id not in imported_devices or previous != label_id:
                device = self.devices.async_get(device_id)
                if device and previous in device.labels:
                    self.devices.async_update_device(device_id, labels=set(device.labels) - {previous})
                self.owned_device_labels.pop(device_id)
        if label_id:
            for device_id in imported_devices:
                device = self.devices.async_get(device_id)
                if device and label_id not in device.labels:
                    self.devices.async_update_device(device_id, labels=set(device.labels) | {label_id})
                    self.owned_device_labels[device_id] = label_id

    async def async_stop(self):
        self.closed = True
        for unsubscribe in self.unsubscribers:
            unsubscribe()
        self.unsubscribers.clear()
        if self.task and not self.task.done():
            await self.task

    async def async_remove(self):
        await self._load()
        self._reconcile_labels({}, None)
        self._reconcile_device_labels(set(), None)
        await self.store.async_remove()


def received_overview(hass):
    """Group real imported MQTT entities by their existing device."""
    registry = er.async_get(hass)
    devices = dr.async_get(hass)
    groups = {}
    loose = []
    for entity in registry.entities.values():
        if entity.platform != "mqtt" or not SHARED_UNIQUE_ID.match(str(entity.unique_id)):
            continue
        if entity.device_id and (device := devices.async_get(entity.device_id)):
            group = groups.setdefault(device.id, {"name": device.name_by_user or device.name or device.id, "entities": []})
            group["entities"].append(entity.entity_id)
        else:
            loose.append({"name": entity.name or entity.original_name or entity.entity_id, "entity_id": entity.entity_id})
    return groups, loose
