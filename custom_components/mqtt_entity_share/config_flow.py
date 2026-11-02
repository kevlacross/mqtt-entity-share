"""Configure sharing on the sending instance with native HA selectors."""
import re
from collections import Counter
import uuid

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import area_registry as ar, device_registry as dr, entity_registry as er, label_registry as lr, selector

from .const import DOMAIN, SUPPORTED_ENTITY_TYPE_COMMANDS
from .policy import resolve_permissions, supported


def eligible_entities(hass):
    registry = er.async_get(hass)
    result = []
    for state in hass.states.async_all():
        entity_id = state.entity_id
        entry = registry.async_get(entity_id)
        if not supported(hass, entity_id):
            continue
        if entry and (entry.disabled or entry.hidden_by):
            continue
        result.append(entity_id)
    return sorted(result)


TYPE_NAMES = {
    "light": "Licht", "cover": "Rollläden und Abdeckungen", "switch": "Schalter",
    "sensor": "Sensoren", "binary_sensor": "Binäre Sensoren", "climate": "Heizung und Klima",
    "fan": "Ventilatoren", "lock": "Schlösser", "button": "Tasten", "scene": "Szenen",
    "script": "Skripte", "input_boolean": "Schalter-Helfer", "input_number": "Zahlen-Helfer",
    "input_select": "Auswahl-Helfer", "input_text": "Text-Helfer", "input_button": "Tasten-Helfer",
    "input_datetime": "Datum-/Zeit-Helfer", "number": "Zahlen", "select": "Auswahllisten",
    "text": "Text", "vacuum": "Saugroboter", "lawn_mower": "Rasenmäher", "update": "Updates",
    "device_tracker": "Anwesenheit", "alarm_control_panel": "Alarmanlagen", "event": "Ereignisse",
    "humidifier": "Luftbefeuchter", "siren": "Sirenen", "water_heater": "Warmwasser",
    "valve": "Ventile", "image": "Bilder", "date": "Datum", "time": "Uhrzeit",
    "datetime": "Datum und Uhrzeit", "tag": "Tags", "infrared": "Infrarot",
}
PAGE_SIZE = 100
NO_AREA = "__no_area__"


def entity_details(hass, entity_id):
    """Use an entity area override first, then inherit the device's area."""
    entry = er.async_get(hass).async_get(entity_id)
    device = dr.async_get(hass).async_get(entry.device_id) if entry and entry.device_id else None
    area_id = entry.area_id if entry and entry.area_id else device.area_id if device else None
    area = ar.async_get(hass).async_get_area(area_id) if area_id else None
    state = hass.states.get(entity_id)
    name = (state.attributes.get("friendly_name") if state else None) or (entry.name or entry.original_name if entry else None) or entity_id
    kind = entity_id.split(".", 1)[0]
    return {"id": entity_id, "name": name, "area": area_id or NO_AREA,
            "area_name": area.name if area else "Ohne Raum", "kind": kind,
            "kind_name": TYPE_NAMES.get(kind, kind)}


class SelectionSteps:
    """Filter locally and keep an explicit draft until the final confirmation."""
    selected = None
    allow_commands = False
    mode = "list"
    control_entities = None
    exclude_entities = None
    label_values = None
    filters = None
    page = 0
    visible_ids = None
    filtered_ids = None

    async def async_step_devices(self, user_input=None):
        # Retain the old step as an entry point for config/options compatibility.
        return await self.async_step_filters()

    async def async_step_mode(self, user_input=None):
        if user_input is not None:
            self.mode = user_input["mode"]
            if self.mode in ("labels", "hybrid"):
                return await self.async_step_labels()
            return await self.async_step_filters()
        return self.async_show_form(step_id="mode", data_schema=vol.Schema({
            vol.Required("mode", default=self.mode): selector.SelectSelector(selector.SelectSelectorConfig(
                options=[{"value": "list", "label": "Liste – manuell auswählen"},
                         {"value": "labels", "label": "Labels – automatisch freigeben"},
                         {"value": "hybrid", "label": "Hybrid – Labels und manuelle Ausnahmen"}],
                mode=selector.SelectSelectorMode.LIST)),
        }))

    async def async_step_labels(self, user_input=None):
        values = self.label_values or {}
        errors = {}
        if user_input is not None:
            self.label_values = values = dict(user_input)
            registry = lr.async_get(self.hass)
            resolved = {}
            names = {}
            for role in ("read", "control"):
                label_id = values.get(role + "_label")
                name = values.get(role + "_label_name", "").strip()
                existing = registry.async_get_label(label_id) if label_id else registry.async_get_label_by_name(name) if name else None
                if label_id and existing is None:
                    errors[role + "_label"] = "missing_label"
                if not label_id and not name:
                    errors[role + "_label_name"] = "label_required"
                resolved[role] = existing.label_id if existing else None
                names[role] = existing.name.casefold() if existing else name.casefold()
            if names["read"] == names["control"] or resolved["read"] and resolved["read"] == resolved["control"]:
                errors["base"] = "same_label"
            if not errors:
                if self.mode == "labels":
                    return await self.async_step_review()
                return await self.async_step_filters()
        fields = {}
        for role, suggestion in (("read", "MQTT-Share read"), ("control", "MQTT-Share control")):
            key = role + "_label"
            label_field = vol.Optional(key, default=values[key]) if values.get(key) else vol.Optional(key)
            fields[label_field] = selector.LabelSelector(selector.LabelSelectorConfig(multiple=False))
            fields[vol.Optional(role + "_label_name", default=values.get(role + "_label_name", suggestion))] = str
        return self.async_show_form(step_id="labels", data_schema=vol.Schema(fields), errors=errors)

    def _options(self, create_labels=False):
        options = {"mode": self.mode, "entities": self.selected or [],
                   "control_entities": sorted(set(self.control_entities or []) & set(self.selected or [])),
                   "exclude_entities": self.exclude_entities or []}
        values = self.label_values or {}
        if self.mode in ("labels", "hybrid"):
            registry = lr.async_get(self.hass)
            for role in ("read", "control"):
                label_id = values.get(role + "_label")
                name = values.get(role + "_label_name", "").strip()
                label = registry.async_get_label(label_id) if label_id else registry.async_get_label_by_name(name) if name else None
                if label is None and name and not label_id and create_labels:
                    label = registry.async_create(name, icon="mdi:share-variant", description="MQTT Entity Share: " + role)
                if label:
                    options[role + "_label"] = label.label_id
        return options

    def _effective(self):
        return resolve_permissions(self.hass, self._options())

    def _permission_preview(self):
        permissions = self._effective()
        lines = []
        for entity_id in sorted(permissions.rights):
            detail = entity_details(self.hass, entity_id)
            right = "steuerbar" if permissions.rights[entity_id] == "control" else "nur lesen"
            lines.append(f'- {detail["name"]} (`{entity_id}`) – **{right}**, {permissions.reasons[entity_id]}')
        return "\n".join(lines) or "Noch keine wirksamen Freigaben. Neue Labels können nach dem Speichern an Geräte oder Entitäten vergeben werden."

    def _summary(self):
        counts = Counter(e.split(".", 1)[0] for e in self.selected or [])
        names = {"light": "Lampen", "switch": "Schalter", "cover": "Rollläden"}
        return " · ".join(f"{n} {names.get(k, TYPE_NAMES.get(k, k))}" for k, n in sorted(counts.items())) or "Noch keine Entitäten ausgewählt"

    def _details(self):
        return [entity_details(self.hass, e) for e in eligible_entities(self.hass)]

    async def async_step_filters(self, user_input=None):
        if user_input is not None:
            self.filters = {"areas": list(user_input.get("areas", [])),
                            "types": list(user_input.get("types", [])),
                            "search": user_input.get("search", "").strip()}
            self.page = 0
            return await self.async_step_entities()
        filters = self.filters or {}
        details = self._details()
        areas = {e["area"]: e["area_name"] for e in details}
        types = {e["kind"]: e["kind_name"] for e in details}
        # Keep filters valid even when an entity temporarily disappears.
        for area_id in filters.get("areas", []):
            area = ar.async_get(self.hass).async_get_area(area_id)
            areas.setdefault(area_id, area.name if area else "Ohne Raum" if area_id == NO_AREA else area_id)
        for kind in filters.get("types", []):
            types.setdefault(kind, TYPE_NAMES.get(kind, kind))
        def multi(options):
            return selector.SelectSelector(selector.SelectSelectorConfig(
                options=[{"value": k, "label": v} for k, v in sorted(options.items(), key=lambda i: i[1].casefold())],
                multiple=True, mode=selector.SelectSelectorMode.DROPDOWN))
        return self.async_show_form(step_id="filters", data_schema=vol.Schema({
            vol.Optional("areas", default=filters.get("areas", [])): multi(areas),
            vol.Optional("types", default=filters.get("types", [])): multi(types),
            vol.Optional("search", default=filters.get("search", "")): str,
        }), description_placeholders={"selected": str(len(self.selected or [])), "summary": self._summary()})

    def _matches(self):
        filters = self.filters or {}
        words = filters.get("search", "").casefold().split()
        def match(e):
            text = " ".join((e["id"], e["name"], e["area_name"], e["kind_name"])).casefold()
            return ((not filters.get("areas") or e["area"] in filters["areas"])
                    and (not filters.get("types") or e["kind"] in filters["types"])
                    and all(word in text for word in words))
        return sorted((e for e in self._details() if match(e)), key=lambda e: (e["area_name"].casefold(), e["name"].casefold(), e["id"]))

    async def async_step_entities(self, user_input=None):
        if user_input is not None:
            checked = set(user_input.get("entities", []))
            if not checked <= set(self.visible_ids or []):
                return self.async_abort(reason="invalid_selection")
            selected = set(self.selected or [])
            selected.difference_update(self.visible_ids or [])
            selected.update(checked)
            action = user_input.get("action", "round")
            if action == "all":
                selected.update(self.filtered_ids or [])
            elif action == "none":
                selected.difference_update(self.filtered_ids or [])
            self.selected = sorted(selected)
            if action == "round":
                return await self.async_step_round()
            if action == "filters":
                return await self.async_step_filters()
            if action == "review":
                return await self.async_step_review()
            if action == "next":
                self.page += 1
            elif action == "previous":
                self.page -= 1
        matches = self._matches()
        self.filtered_ids = [e["id"] for e in matches]
        pages = max(1, (len(matches) + PAGE_SIZE - 1) // PAGE_SIZE)
        self.page = max(0, min(self.page, pages - 1))
        visible = matches[self.page * PAGE_SIZE:(self.page + 1) * PAGE_SIZE]
        self.visible_ids = [e["id"] for e in visible]
        options = [{"value": e["id"], "label": f'{e["name"]} · {e["area_name"]} · {e["kind_name"]} ({e["id"]})'} for e in visible]
        fields = {}
        if options:
            fields[vol.Optional("entities", default=[e for e in self.selected or [] if e in self.visible_ids])] = selector.SelectSelector(
                selector.SelectSelectorConfig(options=options, multiple=True, mode=selector.SelectSelectorMode.LIST))
        return self.async_show_form(step_id="entities", data_schema=vol.Schema(fields),
            description_placeholders={"count": str(len(matches)), "selected": str(len(self.selected or [])),
                                      "page": str(self.page + 1), "pages": str(pages), "summary": self._summary()})

    async def async_step_round(self, user_input=None):
        """Prominent next steps after a checkbox round, with no action dropdown."""
        menu = ["more", "review"]
        if self.filtered_ids:
            menu += ["select_all", "clear_matches"]
        pages = max(1, (len(self.filtered_ids or []) + PAGE_SIZE - 1) // PAGE_SIZE)
        if self.page + 1 < pages:
            menu.append("next_page")
        if self.page:
            menu.append("previous_page")
        return self.async_show_menu(step_id="round", menu_options=menu,
            description_placeholders={"selected": str(len(self.selected or [])),
                                      "summary": self._summary(),
                                      "matches": str(len(self.filtered_ids or []))})

    async def async_step_more(self, user_input=None):
        # Start the next selection round without the preceding type/room filter.
        self.filters = {}
        self.page = 0
        return await self.async_step_filters()

    async def async_step_select_all(self, user_input=None):
        self.selected = sorted(set(self.selected or []) | set(self.filtered_ids or []))
        return await self.async_step_round()

    async def async_step_clear_matches(self, user_input=None):
        self.selected = sorted(set(self.selected or []) - set(self.filtered_ids or []))
        return await self.async_step_round()

    async def async_step_next_page(self, user_input=None):
        self.page += 1
        return await self.async_step_entities()

    async def async_step_previous_page(self, user_input=None):
        self.page -= 1
        return await self.async_step_entities()

    async def async_step_review(self, user_input=None):
        if user_input is not None:
            if self.mode != "labels":
                checked = set(user_input.get("entities", []))
                if not checked <= set(self.selected or []):
                    return self.async_abort(reason="invalid_selection")
                self.selected = sorted(checked)
                controls = set(user_input.get("control_entities", self.selected if user_input.get("allow_commands") else []))
                self.control_entities = sorted(controls & checked)
            if self.mode == "hybrid":
                self.exclude_entities = sorted(set(user_input.get("exclude_entities", [])))
            if user_input.get("action") == "filters":
                return await self.async_step_more()
            if user_input.get("action") == "labels":
                return await self.async_step_labels()
            return await self.async_step_confirm()
        fields = {}
        if self.mode != "labels" and self.selected:
            options = []
            for entity_id in self.selected:
                e = entity_details(self.hass, entity_id)
                options.append({"value": entity_id, "label": f'{e["name"]} · {e["area_name"]} ({entity_id})'})
            def checkboxes():
                return selector.SelectSelector(selector.SelectSelectorConfig(options=options, multiple=True, mode=selector.SelectSelectorMode.LIST))
            fields[vol.Optional("entities", default=self.selected)] = checkboxes()
            fields[vol.Optional("control_entities", default=[e for e in self.control_entities or [] if e in self.selected])] = checkboxes()
        if self.mode == "hybrid":
            candidates = sorted(set(eligible_entities(self.hass)) | set(self.exclude_entities or []))
            fields[vol.Optional("exclude_entities", default=self.exclude_entities or [])] = selector.EntitySelector(
                selector.EntitySelectorConfig(multiple=True, include_entities=candidates))
        actions = [{"value": "confirm", "label": "Zur Bestätigung"}]
        if self.mode != "labels":
            actions.append({"value": "filters", "label": "Weitere Entitäten hinzufügen"})
        if self.mode in ("labels", "hybrid"):
            actions.append({"value": "labels", "label": "Labels bearbeiten"})
        fields[vol.Required("action", default="confirm")] = selector.SelectSelector(
            selector.SelectSelectorConfig(options=actions, mode=selector.SelectSelectorMode.DROPDOWN))
        effective = self._effective()
        return self.async_show_form(step_id="review", data_schema=vol.Schema(fields),
            description_placeholders={"selected": str(len(effective.readable)), "summary": self._summary(),
                "read": str(len(effective.readable - effective.controllable)), "control": str(len(effective.controllable)),
                "preview": self._permission_preview()})

    async def async_step_confirm(self, user_input=None):
        if user_input is not None:
            if user_input.get("action") == "review":
                return await self.async_step_review()
            if self.mode in ("labels", "hybrid"):
                registry = lr.async_get(self.hass)
                if any((self.label_values or {}).get(role + "_label") and not registry.async_get_label(self.label_values[role + "_label"]) for role in ("read", "control")):
                    return await self.async_step_labels()
            return self._finish()
        permissions = self._effective()
        return self.async_show_form(step_id="confirm", data_schema=vol.Schema({
            vol.Required("action", default="save"): selector.SelectSelector(selector.SelectSelectorConfig(
                options=[{"value": "save", "label": "Freigabe speichern"}, {"value": "review", "label": "Auswahl nochmals bearbeiten"}],
                mode=selector.SelectSelectorMode.DROPDOWN)),
        }), description_placeholders={
            "count": str(len(permissions.readable)), "read": str(len(permissions.readable - permissions.controllable)),
            "control": str(len(permissions.controllable)), "mode": self.mode,
            "entities": self._permission_preview(),
        })


class ShareConfigFlow(SelectionSteps, config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self):
        self.settings = {}
        self.selected = []
        self.allow_commands = False

    async def async_step_user(self, user_input=None):
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")
        errors = {}
        if user_input is not None:
            prefix = user_input["discovery_topic"].strip("/")
            base = user_input["base_topic"].strip("/")
            # Dedicated roots avoid importing the sender's own published entities.
            if (not re.fullmatch(r"[a-zA-Z0-9_-]+", prefix)
                    or prefix == "homeassistant"
                    or not re.fullmatch(r"[a-zA-Z0-9_/-]+", base)):
                errors["base"] = "invalid_topic"
            else:
                self.settings = {"base_topic": base, "discovery_topic": prefix,
                                 "source_id": "share_" + uuid.uuid4().hex[:10]}
                if user_input.get("use_remote_bridge", False):
                    return await self.async_step_bridge()
                return await self.async_step_mode()
        return self.async_show_form(step_id="user", errors=errors, data_schema=vol.Schema({
            vol.Required("base_topic", default="ha_share/source_a"): str,
            vol.Required("discovery_topic", default="ha_share"): str,
            vol.Optional("use_remote_bridge", default=False): bool,
        }))

    async def async_step_bridge(self, user_input=None):
        errors = {}
        if user_input is not None:
            peer_base = user_input["peer_base_topic"].strip("/")
            peer_id = user_input["peer_source_id"]
            if (not re.fullmatch(r"[a-zA-Z0-9_/-]+", peer_base)
                    or peer_base == self.settings["base_topic"]
                    or not re.fullmatch(r"share_[0-9a-f]{10}", peer_id)):
                errors["base"] = "invalid_peer"
            else:
                self.settings["remote_bridge"] = {**user_input, "peer_base_topic": peer_base}
                return await self.async_step_mode()
        return self.async_show_form(step_id="bridge", errors=errors, data_schema=vol.Schema({
            vol.Required("host"): str,
            vol.Required("port", default=443): vol.All(vol.Coerce(int), vol.Range(min=1,max=65535)),
            vol.Required("username"): str,
            vol.Required("password"): selector.TextSelector(selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)),
            vol.Required("peer_base_topic"): str,
            vol.Required("peer_source_id"): str,
            vol.Required("local_discovery_prefix", default="homeassistant"): vol.All(str, vol.Match(r"^[a-zA-Z0-9_-]+$")),
        }))

    def _finish(self):
        return self.async_create_entry(title="MQTT Entity Share", data=self.settings,
            options=self._options(create_labels=True))

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return ShareOptionsFlow()


class ShareOptionsFlow(SelectionSteps, config_entries.OptionsFlow):
    async def async_step_init(self, user_input=None):
        self.selected = list(self.config_entry.options.get("entities", []))
        self.allow_commands = self.config_entry.options.get("allow_commands", False)
        self.mode = self.config_entry.options.get("mode", "list")
        self.control_entities = list(self.config_entry.options.get("control_entities", self.selected if self.allow_commands else []))
        self.exclude_entities = list(self.config_entry.options.get("exclude_entities", []))
        self.label_values = {key: self.config_entry.options[key] for key in ("read_label", "control_label") if key in self.config_entry.options}
        return await self.async_step_mode(user_input)

    def _finish(self):
        return self.async_create_entry(title="", data=self._options(create_labels=True))
