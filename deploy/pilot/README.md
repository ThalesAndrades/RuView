# RuView pilot deploy kit

Stand up a **single-room / small ILPI (elderly-care) pilot** end-to-end:
ESP32 CSI nodes → RuView sensing-server → MQTT → **Home Assistant**, with the
ADR-115 §3.12 semantic primitives (`someone_sleeping`, `no_movement`,
`elderly_inactivity_anomaly`, `bed_exit`, `bathroom_occupied`, …) showing up as
first-class HA entities. No cameras, edge-only, `--privacy-mode`-friendly.

**What this kit gives you:** a wired `docker compose` (Mosquitto + sensing-server),
a zone-map template, an env template, and the runbook below.

**What it does NOT include** (you bring these): Home Assistant itself, the ESP32
hardware + flashed firmware, and any trained `.rvf` model (the server runs on
heuristics without one — presence/motion/semantics still work).

---

## Prerequisites

- A Linux/macOS host (or Raspberry Pi 4/5 — the image is multi-arch) with Docker + Docker Compose.
- An existing **Home Assistant** with the **MQTT integration** available.
- One or more **ESP32-S3** nodes flashed with the RuView CSI firmware (see the
  [firmware release process](../../firmware/esp32-csi-node/)). For `bed_exit`
  and `bathroom_occupied` you need **a node per zone** (one by the bed, one in
  the bathroom) — a single node can't tell the bed from the toilet.
- The host's LAN IP (the ESP32 nodes stream UDP to it).

---

## Step 1 — Provision the ESP32 node(s)

Set a **unique `--node-id`** per node (a `u8`, 0–255). These are the ids your
`zones.json` references.

```bash
python firmware/esp32-csi-node/provision.py \
  --port /dev/ttyUSB0 \
  --ssid "YourWiFi" --password "wifi-secret" \
  --target-ip 192.168.1.20 \   # the host running this compose
  --node-id 1 \
  --zone bedroom               # friendly label (optional)
```

Repeat for the bathroom node with `--node-id 2 --zone bathroom`.

## Step 2 — Configure the stack

```bash
cd deploy/pilot
cp .env.example .env            # edit: CSI_SOURCE, MQTT creds, privacy mode
cp zones.example.json zones.json
```

Edit `zones.json` so each zone's `nodes` lists the `--node-id`(s) you flashed:

```json
{ "zones": [
  { "name": "bedroom",  "nodes": ["1"], "tags": ["bed"] },
  { "name": "bathroom", "nodes": ["2"], "tags": ["bathroom"] }
] }
```

> Tip: to demo the HA entities **before** any hardware, set `CSI_SOURCE=simulate`
> in `.env`. The zone-gated primitives stay dormant (no per-node presence) but the
> 7 zone-free ones (sleeping, no-movement, inactivity, …) light up.

## Step 3 — Bring it up

```bash
docker compose up -d
docker compose logs -f sensing-server   # watch it connect to the broker
```

You should see `MQTT publisher started -> mosquitto:1883` and, once zones load,
`Semantic zones loaded from /config/zones.json — 2 zone(s)`.

## Step 4 — Verify MQTT is flowing

```bash
docker compose exec mosquitto mosquitto_sub -t 'homeassistant/#' -v
```

Expect retained discovery configs and live state, e.g.
`homeassistant/binary_sensor/wifi_densepose_<node>/no_movement/state ON`.

## Step 5 — Connect Home Assistant

1. HA → **Settings → Devices & Services → MQTT → Configure**.
2. Broker = this host's IP, port **1883** (+ username/password if you enabled auth
   in `mosquitto.conf` / `.env`).
3. Within ~5 s, HA auto-discovers a RuView device per node with its entities.
   Find them under the MQTT integration.

## Step 6 — Wire up alerts (blueprints)

Import the shipped blueprints (one click each) from
[`examples/ha-blueprints/`](../../examples/ha-blueprints/) — the pilot-relevant ones:

| Blueprint | Fires |
|---|---|
| `04-alert-elderly-inactivity-anomaly.yaml` | caregiver alert on unusual inactivity |
| `07-fall-risk-escalation.yaml` | escalating alert (Lovelace → phone) on fall risk |
| `03-wake-routine-on-bed-exit.yaml` | morning routine on overnight bed exit |
| `06-bathroom-fan-while-occupied.yaml` | exhaust fan while the bathroom is occupied |

Optional dashboards: [`examples/lovelace/`](../../examples/lovelace/) (see
`03-healthcare-aal-view.yaml`).

---

## What you should see after a night

- `binary_sensor.*_someone_sleeping` tracking the occupant overnight.
- `event.*_bed_exit` firing when they get up between 22:00–06:00.
- `binary_sensor.*_no_movement` / `*_elderly_inactivity_anomaly` as safety nets.
- `binary_sensor.*_bathroom_occupied` while the bathroom node sees presence.

## Security & honesty notes

- The default `mosquitto.conf` is **anonymous** for a frictionless bring-up on an
  isolated LAN. **Enable password auth before any real deployment** — instructions
  are in `mosquitto.conf`.
- This is a **non-clinical** ambient-sensing pilot, not a medical device. Vital
  signs from WiFi CSI are trend-grade, not diagnostic. Treat `fall_risk` /
  `no_movement` as *assistive* signals that prompt a human check, not alarms you
  bet a life on. Validate accuracy in *your* rooms before relying on it.
- `--privacy-mode` (set `RUVIEW_PRIVACY_MODE=true` in `.env`) keeps the inferred
  semantic states while stripping raw heart-rate / breathing-rate / pose from the
  wire — a good fit for bedrooms and bathrooms.

## Troubleshooting

- **No entities in HA** → check `mosquitto_sub` (Step 4). Nothing there → the
  server isn't publishing: confirm `RUVIEW_MQTT=true` in `.env` and re-read logs.
- **ESP32 frames not arriving** → confirm `--target-ip` matches this host and UDP
  5005 is open. On **Docker Desktop for Windows**, multi-node UDP needs the relay
  workaround in [`docs/TROUBLESHOOTING.md`](../../docs/TROUBLESHOOTING.md) §9.
- **bed_exit/bathroom never fire** → they need a node *in that zone* and the zone
  listed in `zones.json`; confirm the "zones loaded" log line and that the node's
  `--node-id` matches.

Full reference: [`docs/integrations/home-assistant.md`](../../docs/integrations/home-assistant.md).
