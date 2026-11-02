# MQTT Entity Share

Custom Home Assistant integration for selectively exporting entities through MQTT discovery. Version 0.3.0 is an early test release, tested with Home Assistant 2026.10.4.

## Install with HACS

1. Open HACS → menu → Custom repositories.
2. Add `https://github.com/kevlacross/mqtt-entity-share` with category **Integration**.
3. Download **MQTT Entity Share** and restart Home Assistant.
4. Open Settings → Devices & services → Add integration → MQTT Entity Share.

## Requirements and transport

The built-in MQTT integration must already be configured. This integration publishes through that existing connection: it does not configure a broker, bridge, TLS, WebSockets or a remote MQTT connection. Configure those separately. The receiver must receive the shared discovery and state topics, and its MQTT discovery prefix must match the configured sharing prefix. Do not change an existing discovery prefix without checking effects on existing MQTT devices.

For two-way sharing, install on both instances and use different state/command roots (for example `ha_share/source_a` and `ha_share/source_b`). Configure discovery routing, bridge directions and broker ACLs for both sources. Installing the integration alone does not create a bidirectional connection.

## Sharing modes

- **Liste**: manually select entities in rounds, filtered by type, room and search. Select which of those entities additionally accept control commands.
- **Labels**: choose existing labels or create labels on final save. Suggested names are `MQTT-Share read` and `MQTT-Share control`. Labels on entities and devices are supported; device labels apply to supported entities belonging to that device. The control label wins when both apply.
- **Hybrid**: labels plus manual permissions and exclusions. Exclusion wins over manual selection; manual permissions win over labels.

Each source chooses its own labels. Label assignments are reevaluated automatically. Received MQTT Entity Share entities are excluded from export to prevent loops. The final review shows effective permissions and their origin.

## Permissions and limitations

The sending instance checks each incoming command against its current permissions. Read-only shares reject commands at the source. The receiver may still display actuator controls; these controls do not grant permission. Retained commands are rejected. Use authenticated brokers with narrow topic ACLs and encrypted remote transport.

Only one integration entry per instance is supported. The setup UI is currently German, including the English translation fallback. Supported device behavior inherits the upstream MQTT DiscoveryStream implementation; not all entity types and commands have been tested end to end. No production reliability guarantee is implied by this early release.

## Credits

Based on [MQTT DiscoveryStream Alt](https://github.com/RogerSelwyn/mqtt_discoverystream_ha) by Roger Selwyn. The original MIT license and copyright notice are preserved. This project adds a source-side setup/options UI, explicit selection, label policies, hybrid overrides and command permission enforcement. Please report issues with this fork here, rather than to the upstream project.
