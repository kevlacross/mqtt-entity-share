"""A narrow WSS relay between this instance's MQTT broker and one peer."""
import asyncio
import logging
from homeassistant.components import mqtt
from homeassistant.core import callback
import paho.mqtt.client as paho

_LOGGER = logging.getLogger(__name__)


class ShareBridge:
    def __init__(self, hass, entry):
        self.hass, self.entry = hass, entry
        self.conf = entry.data['remote_bridge']
        self.own_base = entry.data['base_topic'].rstrip('/')
        self.peer_base = self.conf['peer_base_topic'].rstrip('/')
        self.discovery = entry.data['discovery_topic']
        self.own_id = entry.data['source_id']
        self.peer_id = self.conf['peer_source_id']
        self.local_prefix = self.conf.get('local_discovery_prefix', 'homeassistant')
        self.closed = False
        self.unsubscribers = []
        self.connected = asyncio.Event()
        self.client = paho.Client(paho.CallbackAPIVersion.VERSION2,
            client_id='mqtt-share-' + self.own_id, protocol=paho.MQTTv5, transport='websockets')
        self.client.ws_set_options(path='/mqtt')
        self.client.tls_set()
        self.client.username_pw_set(self.conf['username'], self.conf['password'])
        self.client.reconnect_delay_set(min_delay=2, max_delay=60)
        self.client.max_queued_messages_set(1000)
        self.client.on_connect = self._connected
        self.client.on_message = self._message

    def _connected(self, client, userdata, flags, reason, properties):
        if reason.is_failure:
            _LOGGER.error('MQTT share bridge connection refused: %s', reason)
            return
        client.subscribe([(f'{self.discovery}/+/{self.peer_id}/+/config', 1),
                          (self.peer_base + '/state/#', 1), (self.own_base + '/command/#', 1)])
        self.hass.loop.call_soon_threadsafe(self._ready)

    @callback
    def _ready(self):
        if self.closed:
            return
        self.connected.set()
        self.client.publish(self.peer_base + '/status', 'online', qos=1)
        self.hass.async_create_task(mqtt.async_publish(self.hass, 'homeassistant/status', 'online'))

    def incoming_topic(self, topic, retain=False):
        parts = topic.split('/')
        if len(parts) == 5 and parts[0] == self.discovery and parts[2] == self.peer_id and parts[4] == 'config':
            return self.local_prefix + '/' + '/'.join(parts[1:])
        if topic.startswith(self.peer_base + '/state/'):
            return topic
        if topic.startswith(self.own_base + '/command/') and not retain:
            return topic
        return None

    def _message(self, client, userdata, msg):
        topic = self.incoming_topic(msg.topic, msg.retain)
        if topic and not self.closed:
            self.hass.loop.call_soon_threadsafe(self._receive, topic, bytes(msg.payload), bool(msg.retain) or topic.endswith("/config") or topic.endswith("/availability"))

    @callback
    def _receive(self, topic, payload, retain):
        if not self.closed:
            self.hass.async_create_task(mqtt.async_publish(self.hass, topic, payload, qos=1, retain=retain))

    async def _outgoing(self, msg):
        if self.closed or getattr(msg, 'retain', False) and '/command/' in msg.topic:
            return
        if '/command/' in msg.topic and not self.client.is_connected():
            return
        # Discovery and availability remain retained; states remain transient.
        retain = msg.topic.endswith('/config') or msg.topic.endswith('/availability')
        self.client.publish(msg.topic, msg.payload, qos=1, retain=retain)

    async def async_start(self):
        for topic in (f'{self.discovery}/+/{self.own_id}/+/config', self.own_base + '/state/#', self.peer_base + '/command/#'):
            self.unsubscribers.append(await mqtt.async_subscribe(self.hass, topic, self._outgoing, qos=1))
        self.client.connect_async(self.conf['host'], self.conf.get('port', 443), keepalive=45)
        await self.hass.async_add_executor_job(self.client.loop_start)

    async def async_stop(self):
        self.closed = True
        for unsubscribe in self.unsubscribers:
            unsubscribe()
        self.unsubscribers.clear()
        self.client.disconnect()
        await self.hass.async_add_executor_job(self.client.loop_stop)
