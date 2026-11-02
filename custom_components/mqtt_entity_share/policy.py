"""Resolve source-owned export permissions for list, label and hybrid modes."""
from dataclasses import dataclass
import re

from homeassistant.helpers import device_registry as dr, entity_registry as er, label_registry as lr
from .const import SUPPORTED_ENTITY_TYPE_COMMANDS

READ = 'read'
CONTROL = 'control'


def is_imported(hass, entity_id):
    state = hass.states.get(entity_id)
    if state and state.attributes.get('_mqtt_share_origin'):
        return True
    entry = er.async_get(hass).async_get(entity_id)
    return bool(entry and getattr(entry, 'platform', None) == 'mqtt'
                and re.match(r'^share_[0-9a-f]{10}_', str(getattr(entry, 'unique_id', ''))))


def supported(hass, entity_id):
    if entity_id.split('.', 1)[0] not in SUPPORTED_ENTITY_TYPE_COMMANDS or is_imported(hass, entity_id):
        return False
    entry = er.async_get(hass).async_get(entity_id)
    if entry and (entry.disabled or entry.hidden_by):
        return False
    if entry and entry.device_id:
        device = dr.async_get(hass).async_get(entry.device_id)
        if device and getattr(device, "disabled_by", None):
            return False
    return True


@dataclass
class Permissions:
    rights: dict
    reasons: dict

    @property
    def readable(self):
        return set(self.rights)

    @property
    def controllable(self):
        return {e for e, right in self.rights.items() if right == CONTROL}


def resolve_permissions(hass, options, entity_ids=None):
    mode = options.get('mode', 'list')
    registry = er.async_get(hass)
    devices = dr.async_get(hass)
    label_registry = lr.async_get(hass)
    manual = set(options.get('entities', []))
    candidates = set(entity_ids) if entity_ids is not None else {s.entity_id for s in hass.states.async_all()} | manual
    rights, reasons = {}, {}
    for entity_id in candidates:
        if not supported(hass, entity_id):
            continue
        right = reason = None
        entry = registry.async_get(entity_id)
        labels = set(getattr(entry, 'labels', ()) or ()) if entry else set()
        if entry and entry.device_id:
            device = devices.async_get(entry.device_id)
            if device:
                labels.update(getattr(device, "labels", ()) or ())
        if mode in ('labels', 'hybrid'):
            if options.get('read_label') and options['read_label'] in labels and label_registry.async_get_label(options['read_label']):
                right, reason = READ, 'Lese-Label'
            if options.get('control_label') and options['control_label'] in labels and label_registry.async_get_label(options['control_label']):
                right, reason = CONTROL, 'Steuer-Label'
        if mode in ('list', 'hybrid') and entity_id in manual:
            # Legacy entries preserve their existing global permission.
            controls = set(options.get('control_entities', manual if options.get('allow_commands', False) else []))
            right = CONTROL if entity_id in controls else READ
            reason = 'Manuelle Auswahl'
        if mode == 'hybrid' and entity_id in options.get('exclude_entities', []):
            right = reason = None
        if right:
            rights[entity_id], reasons[entity_id] = right, reason
    return Permissions(rights, reasons)
