# ADR-151 — Field validation harness: measuring semantic accuracy against ground truth

**Status:** Proposed · 2026-06-09

## Context

The semantic pipeline is, at this point, *demonstrably wired*: the 10 ADR-115 §3.12
primitives run live (ADR-115 wiring), publish to Home Assistant (MQTT), survive
restarts (semantic persistence), and resolve zones (node→zone model). The
`scenario_demo` example shows the elderly-care safety arc end to end. The full
test suite is green.

What we have **zero** of is **field accuracy**. Every product claim — "detects
presence through walls", "catches a bed-exit", "flags abnormal inactivity",
"breathing rate ±N BPM" — is currently *unbacked by measurement against a known
truth*. This is the single structural gap that:

- blocks any honest accuracy number for a pilot, an ANATEL/market pitch, or a
  clinician conversation;
- determines whether per-room calibration (the ADR-150 LoRA-adapter path) is
  *mandatory* before deployment — ADR-150 already found cross-environment
  generalization collapses without it, and retracted an earlier "100% presence"
  figure measured on a single-class recording. **Honesty about accuracy is a
  first-class requirement here**, not a nicety.

We are not starting from nothing. Reusable pieces already exist:

- **`/ws/sensing`** streams both `sensing_update` (presence / motion / vitals /
  zones / counts) and `semantic_event` (the inferred primitives, with reasons).
- **`POST /api/v1/recording/start`** records the raw CSI layer to
  `data/recordings/<id>.jsonl`.
- The **deterministic proof harness** (`archive/v1/data/proof/verify.py`) and the
  **witness-bundle** pattern (ADR-028) give us a template for signed,
  reproducible evidence.
- Eval-methodology precedent: ADR-145 (ablation eval harness), ADR-149
  (benchmarking methodology), ADR-011 (proof-of-reality).

## Decision

Build a three-stage harness — **Capture → Align → Score** — that compares the
live system output against an **independent** ground truth and emits a *signed
report with per-primitive metrics and confidence intervals*. Scope it under
`aether-arena/validation/`.

### 1. What we measure, and the metric per signal class

| Signal class | Examples | Primary metrics |
|---|---|---|
| **Binary presence** (per-second) | `presence`, `room_active` | Confusion matrix → accuracy / precision / **recall** / F1; **false-alarm rate (/h)**; **miss rate**; onset & offset **latency** |
| **Occupancy count** | `person_count` | MAE; % within ±1; confusion over {0,1,2,3} |
| **One-shot events** | `bed_exit`, `multi_room_transition`, `fall_risk` crossing | Event matching within tolerance window τ → TP/FP/FN → precision/recall; latency distribution; headline **missed-event rate** + **nuisance-alerts/night** |
| **Vitals** | breathing rate, heart rate | Bland–Altman vs reference + MAE/RMSE; % within ±k BPM; **coverage** (fraction of time a value emits at confidence ≥ c) |
| **Derived states** | `someone_sleeping`, `elderly_inactivity_anomaly`, `no_movement` | Scored against annotated truth — **but** these FSMs are deterministic given their inputs (already unit-tested), so the *primary* validation is of their **inputs** (presence / motion / zone). Document this explicitly so we don't double-count FSM logic as "accuracy". |

For every metric: report a **confidence interval** (bootstrap or Wilson for
proportions), never a bare point estimate.

### 2. Ground truth — three tiers (cheapest → richest)

Ground truth is the hard part. Use the cheapest tier that answers the question.

- **Tier A — Scripted protocol + marker log.** A subject follows a timed script;
  a tiny "marker" surface (a phone web page or an MQTT button) timestamps each
  transition: *enter bedroom → lie down → asleep → get up → go to bathroom →
  leave*. Produces an interval/event ground-truth JSONL. Best for **events and
  transitions** (bed_exit, multi_room, presence onsets). Cheap, privacy-clean,
  ANATEL-irrelevant.
- **Tier B — Independent reference sensors** (continuous, automated, 24/7).
  Cheap homologated devices on **separate** MQTT topics so they never feed the
  system under test: PIR + door/contact sensors (presence / occupancy / room
  transitions); a **bed load-cell / FSR mat** (bed occupancy = the bed_exit
  oracle); a **Polar H10 chest belt** (breathing & heart-rate oracle). Removes
  the human-in-the-loop for long runs.
- **Tier C — Camera, study-only** (richest; calibration study *only*). A camera
  in the test room under **explicit consent**, frames auto/hand-labeled for
  presence / pose / count, **deleted after labeling**. *Never* in production —
  this preserves the product's "no camera" promise; it is used once to bound
  accuracy where pose/count granularity is needed.

**Recommended for the pilot study: A + B.** Reserve C for a one-off pose/count
calibration.

### 3. Time alignment

- Run **NTP/chrony** on every node and every truth device (target < 100 ms skew).
- Add a **sync marker** at session start — a sharp wave/clap that both the CSI
  stream and a reference channel register — to compute and correct residual
  offset.
- Resample both streams to a common **1 Hz grid** before scoring. Record the
  measured clock offset in the report.

### 4. Artifacts to build (under `aether-arena/validation/`)

1. **`record.py`** — subscribes to `/ws/sensing` (or `mqtt homeassistant/#`) and
   appends timestamped `sensing_update` + `semantic_event` to `session.jsonl`.
   (The raw CSI can additionally be captured via `/api/v1/recording/start`; the
   WS recorder captures the *inferred* layer, which is what we score.)
2. **`groundtruth.schema.json`** + a Tier-A marker page / MQTT button publisher,
   and Tier-B MQTT topic adapters. One canonical interval/event JSONL format for
   all tiers.
3. **`score.py`** — loads system + ground-truth JSONL, aligns, computes every
   metric above, and emits `report.md`, plots (`confusion_*.png`,
   `bland_altman_*.png`, `event_latency_*.png`), and a machine-readable
   `metrics.json`.
4. **`protocol.md`** — the test protocol: **≥3 distinct rooms/environments**
   (ADR-150 says cross-environment is the axis that breaks), **≥N subjects** for
   cross-subject, the session script, durations, repetitions, and a
   **power / sample-size calculation** for the CI width we intend to claim.
5. **Witness bundle** (ADR-028 pattern): SHA-256 of raw recordings + ground truth
   + scorer version → a signed, reproducible accuracy attestation, self-verifiable
   by a recipient.

### 5. Acceptance gates (what "validated" means)

Set explicit, honest thresholds *with operators/clinicians before the study*
(illustrative starting points — a safety product weights recall over precision):

- **Presence:** recall ≥ 0.95 (don't miss an occupant), false-alarm ≤ 1/h.
- **bed_exit:** **missed-event rate ≤ 2 %** over N nights, nuisance ≤ 1/night.
- **elderly_inactivity_anomaly:** recall on injected long-idle episodes; bounded
  FP/24 h.
- **breathing rate:** MAE ≤ 2 BPM at ≥ 80 % coverage — explicitly **trend-grade,
  not clinical**.

**Honesty rule (inherited from ADR-150):** always report **held-out**,
**cross-subject**, and **cross-environment** results *separately* — never a
single in-room number — and always with CIs.

### 6. Phasing

- **P1 (~1 wk):** `record.py` + Tier-A marker + `score.py` for presence & events
  in one room → the *first real numbers*.
- **P2 (~2–3 wk):** Tier-B reference sensors (continuous), multi-room, vitals vs
  Polar H10.
- **P3:** power-sized study across ≥ 3 rooms / ≥ N subjects → the signed report
  that becomes the accuracy evidence for the pilot and the regulatory/market
  pitch.

## Consequences

**Positive.** First defensible accuracy; unblocks honest claims, ANATEL/market
conversations, and clinician engagement; fully reproducible via the witness
bundle; tells us definitively whether per-room calibration is mandatory before a
pilot.

**Costs.** Requires real deployments and subject time; reference sensors on the
order of a few hundred BRL; a carefully run protocol.

**Risks.** Cross-environment accuracy may come back low (ADR-150 predicts this).
That is not a failure of the harness — *knowing it* is precisely the value: it
decides whether the ADR-150 per-room LoRA-adapter path is a prerequisite for
shipping, rather than discovering it in a customer's bedroom.

## Alternatives considered

- **Synthetic only** (what `scenario_demo` does): proves the pipeline fires the
  right primitives in the right order, but says nothing about real-world
  accuracy. Necessary, not sufficient. Rejected as the validation answer.
- **Camera-only ground truth:** richest, but normalizing a camera in the room
  would betray the "no camera" product promise. Restricted to the Tier-C
  study-only role.
