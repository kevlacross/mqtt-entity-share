"""Run an explicit, persisted allowlist; empty always means share nothing."""
import asyncio
from datetime import timedelta
import logging

from homeassistant.components import mqtt
from homeassistant.const import EVENT_STATE_CHANGED, EVENT_HOMEASSISTANT_STOP, EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import callback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store

from .const import DOMAIN
from .discovery import Discovery
from .policy import resolve_permissions
from .utils import retain_setup

_LOGGER = logging.getLogger(__name__)


async def cleanup_topics(hass, topics):
    """Remove retained discovery configurations belonging to this share only."""
    for topic in topics.values():
        await mqtt.async_publish(hass, topic, "", qos=1, retain=True)


class ShareRuntime:
    def __init__(self, hass, entry):
        self.hass = hass
        self.entry = entry
        self.allowed = set()
        self.options = dict(entry.options)
        self._policy_pending = False
        self._policy_dirty = False
        self.lock = asyncio.Lock()
        self.closed = False
        self.unsubscribers = []
        self.store = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}")
        self.topics = {}
        self.conf = {
            "base_topic": entry.data["base_topic"].rstrip("/") + "/state",
            "discovery_topic": entry.data["discovery_topic"],
            "command_topic": entry.data["base_topic"].rstrip("/") + "/command",
            "unique_prefix": entry.data["source_id"],
            "unique_entity_prefix": entry.data["source_id"],
            "retain_discovery": True,
            "retain_state": False,
            "availability_mode": "latest",
            # Discovery's include check must remain true, even for an empty selection.
            "include": {"entities": ["sensor.__share_none__"]},
        }
        self.base = self.conf["base_topic"].rstrip("/") + "/"
        self.conf["_command_authorizer"] = self.can_control
        self.discovery = Discovery(hass, self.conf, retain_setup(self.conf))

    async def async_start(self):
        stored = await self.store.async_load() or {}
        self.topics = stored.get("topics", {})
        await self.async_apply(self.entry.options)

        @callback
        def changed(event):
            entity_id = event.data["entity_id"]
            if self.closed:
                return
            if entity_id in self.allowed:
                self.hass.async_create_task(self.async_publish(entity_id))
            elif entity_id in resolve_permissions(self.hass, self.options, [entity_id]).readable:
                self._schedule_policy_update()

        self.unsubscribers.append(self.hass.bus.async_listen(EVENT_STATE_CHANGED, changed))
        for event_type in ("entity_registry_updated", "device_registry_updated", "label_registry_updated"):
            self.unsubscribers.append(self.hass.bus.async_listen(event_type, self._registry_changed))
        self.unsubscribers.append(self.hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_STARTED, self._refresh
        ))
        self.unsubscribers.append(async_track_time_interval(
            self.hass, self._refresh, timedelta(minutes=5)
        ))
        self.unsubscribers.append(self.hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_STOP, self._shutdown
        ))
        # Receiver discovery is retained, states are refreshed after its MQTT birth.
        self.unsubscribers.append(await mqtt.async_subscribe(
            self.hass, "homeassistant/status", self._birth
        ))

    def can_control(self, entity_id):
        return not self.closed and entity_id in resolve_permissions(self.hass, self.options, [entity_id]).controllable

    @callback
    def _registry_changed(self, event):
        self._schedule_policy_update()

    @callback
    def _schedule_policy_update(self):
        if self.closed:
            return
        self._policy_dirty = True
        if self._policy_pending:
            return
        self._policy_pending = True
        async def apply():
            try:
                while self._policy_dirty and not self.closed:
                    self._policy_dirty = False
                    await self.async_apply(self.options)
            finally:
                self._policy_pending = False
        self.hass.async_create_task(apply())

    async def _birth(self, message):
        if message.payload == "online":
            await self._refresh()

    async def _refresh(self, now=None):
        # Also reconcile labels periodically, recovering missed registry events.
        if resolve_permissions(self.hass, self.options).readable != self.allowed:
            await self.async_apply(self.options)
            return
        for entity_id in sorted(self.allowed):
            await self.async_publish(entity_id)

    async def async_apply(self, options):
        async with self.lock:
            self.options = dict(options)
            permissions = resolve_permissions(self.hass, self.options)
            self.allowed = permissions.readable
            allow_commands = bool(permissions.controllable)
            self.discovery.subscribe_possible = allow_commands
            if not allow_commands:
                for entityclass in self.discovery.discovery_classes.values():
                    entityclass.unsubscribe()
                self.discovery._subscribed.clear()
            # The same list object is used by every command handler.
            self.discovery.discovered_entities[:] = [
                e for e in self.discovery.discovered_entities if e in self.allowed
            ]
            removed = {e: t for e, t in self.topics.items() if e not in self.allowed}
            # Persist before deletion; interrupted cleanup is retried on the next start.
            await cleanup_topics(self.hass, removed)
            for entity_id in removed:
                self.topics.pop(entity_id, None)
                self.discovery.published_topics.pop(entity_id, None)
            await self.store.async_save({"topics": self.topics})
        await self._refresh()

    async def async_publish(self, entity_id):
        async with self.lock:
            if self.closed or entity_id not in self.allowed or entity_id not in resolve_permissions(self.hass, self.options, [entity_id]).readable:
                return
            state = self.hass.states.get(entity_id)
            if state is None:
                await self._mark_unavailable(entity_id)
                return
            mybase = self.base + entity_id.replace(".", "/") + "/"
            try:
                valid = await self.discovery.async_discovery_publish(
                    entity_id, state.attributes, mybase
                )
                # Save the intended topic even if the initial publication is interrupted.
                self.topics.update(self.discovery.published_topics)
                await self.store.async_save({"topics": self.topics})
                if not valid:
                    return
                if state.state not in ("unavailable", "unknown"):
                    entityclass = self.discovery.discovery_classes[entity_id.split(".")[0]]
                    await entityclass.async_publish_state(state, mybase)
                await mqtt.async_publish(
                    self.hass, mybase + "availability",
                    "offline" if state.state in ("unavailable", "unknown") else "online",
                    qos=1, retain=True,
                )
            except Exception:
                _LOGGER.exception("Could not publish shared entity %s", entity_id)
                self.topics.update(self.discovery.published_topics)
                await self.store.async_save({"topics": self.topics})

    async def _mark_unavailable(self, entity_id):
        await mqtt.async_publish(
            self.hass, self.base + entity_id.replace(".", "/") + "/availability",
            "offline", qos=1, retain=True,
        )

    async def _shutdown(self, event):
        await self.async_stop()

    async def async_stop(self):
        self.closed = True
        for unsubscribe in self.unsubscribers:
            unsubscribe()
        self.unsubscribers.clear()
        async with self.lock:
            for entityclass in self.discovery.discovery_classes.values():
                entityclass.unsubscribe()
            self.discovery.discovered_entities.clear()
            for entity_id in self.allowed:
                await self._mark_unavailable(entity_id)
