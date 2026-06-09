# Field-validation protocol (ADR-151 P1)

How to run a session that yields **defensible** numbers, not a single in-room
anecdote.

## Clocks first

The scorer aligns on wall-clock time. Before any session:

```bash
# every machine: the sensing-server host AND the marker device
sudo timedatectl set-ntp true     # or: sudo chronyc makestep
```

Target < 100 ms skew. If you can't NTP the marker device, do a **sync clap**:
at session start, clap once and press the marker for `present` at the same
instant — later pass the residual offset to `score.py --offset <s>`.

## One session

1. **Start the system** on the host (real hardware):
   ```bash
   sensing-server --source esp32 --semantic-zones-file zones.json
   ```
   (Use `--source simulate` only to validate the *harness*, never to claim
   sensor accuracy.)

2. **Record** the inference layer:
   ```bash
   python record.py --url ws://<host>:8080/ws/sensing --out session.jsonl
   ```

3. **Mark ground truth** on the subject's phone/laptop (NTP-synced), pressing
   the marker the instant each real transition happens:
   ```bash
   python mark.py            # interactive: p=present a=absent b=bed_exit e/x=bathroom …
   ```
   Tier B (recommended for long runs): replace the human marker with independent
   reference sensors — PIR/contact (presence/zones), a bed load-cell (bed_exit
   oracle), a Polar H10 (vitals oracle). Log each to a CSV and convert them with
   `reference.py`, which emits the same `groundtruth.jsonl` plus a `vitalsref.jsonl`
   for vitals:
   ```bash
   python reference.py pir          --in pir.csv   --out groundtruth.jsonl
   python reference.py bed_loadcell --in bed.csv   --out groundtruth.jsonl --threshold 10
   python reference.py polar_h10    --in polar.csv --out vitalsref.jsonl
   ```
   This is what makes the ~50-bed-exit / ~75-presence-sample power targets below
   feasible — the sensors self-label while you sleep.

4. **Score** (add `--vitals-ref` to get the HR/BR Bland–Altman section):
   ```bash
   python score.py --session session.jsonl --truth groundtruth.jsonl \
       --vitals-ref vitalsref.jsonl --vitals-tolerance 5
   ```

## A session script (single-occupant room)

A repeatable ~40-min beat sheet — press the marker at each step:

| min | action | marker |
|---|---|---|
| 0 | enter empty room | `present` |
| 2 | sit / move around | (none) |
| 6 | go to bathroom | `bathroom_enter` |
| 8 | leave bathroom | `bathroom_leave` |
| 10 | lie in bed | `lie_down` |
| 12 | (still / resting) | (none) |
| 35 | get up, leave bed | `bed_exit` `stand_up` |
| 38 | leave room | `absent` |

Repeat with motion-only, with someone sleeping, and an *empty-room* baseline
(mark `absent` for the whole window → measures the false-alarm rate directly).

## Sample size (so the CI is honest)

For a proportion (e.g. presence recall) the Wilson 95% half-width is roughly
`≈ z·√(p(1−p)/n)`. To claim **recall ≥ 0.95 with ±0.05**, you need on the order
of **~75 presence-positive samples** per cell — i.e. minutes of occupied time,
not seconds. For **events** (bed_exit), each session yields ~1; to bound a
missed-event rate of ≤ 2 %, you need **≳ 50 marked events** (≈ 50 overnight
bed-exits across nights/subjects). Plan the run accordingly.

## Generalization axes (report separately — ADR-150)

Never average these into one number:

- **held-out** — same room, unseen time.
- **cross-subject** — ≥ N subjects; report per-subject and pooled.
- **cross-environment** — ≥ 3 distinct rooms/homes. ADR-150 found this is where
  WiFi-CSI accuracy collapses without per-room calibration. If cross-env recall
  is low, that is the *finding*: per-room calibration (the ADR-150 LoRA adapter)
  is a deployment prerequisite, not optional.

## Bundle the evidence

Hash `session.jsonl` + `groundtruth.jsonl` + `metrics.json` + the scorer commit
into a witness bundle (ADR-028 pattern) so the report is reproducible and
tamper-evident — the artifact you hand a clinician, an operator, or a regulator.
