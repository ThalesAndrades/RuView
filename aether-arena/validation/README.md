# RuView field-validation harness (ADR-151 P1 + P2)

Measure the semantic layer's **accuracy against ground truth**, instead of
trusting demos. This is the **Capture → Align → Score** pipeline from
[ADR-151](../../docs/adr/ADR-151-field-validation-harness.md). Pure Python 3
stdlib — no `pip install` (includes a minimal WebSocket client).

| File | Role |
|---|---|
| `record.py` | Subscribe to the sensing-server `/ws/sensing` and log every `sensing_update` + `semantic_event` to `session.jsonl`. |
| `mark.py` | **Tier-A** ground-truth marker — a human logs timestamped `present`/`absent` toggles and point events (`bed_exit`, …) to `groundtruth.jsonl`. |
| `reference.py` | **Tier-B** sensor converter (P2) — turn **independent reference sensors** into the same artifacts with *no human in the loop*: a bed load-cell → `bed_exit` events, a PIR/contact → presence toggles, a **Polar H10** → an HR `vitalsref.jsonl` oracle. |
| `score.py` | Align everything on wall-clock time → `report.md` + `metrics.json`: presence confusion (precision/recall/F1/false-alarm-rate/onset-latency, with **Wilson 95% CIs**), event matching (TP/FP/FN + latency), and **vitals Bland–Altman** (HR/BR bias, 95% limits of agreement, within-±N-BPM rate) when `--vitals-ref` is given. |
| `protocol.md` | How to run a session that yields *defensible* numbers (clocks, session script, sample-size calc, generalization axes). |

## 60-second smoke test (no hardware)

```bash
# 1. run the server on synthetic data (validates the HARNESS, not the sensor)
( cd v2 && cargo run -p wifi-densepose-sensing-server --bin sensing-server \
    --no-default-features -- --source simulate ) &

# 2. record 60s
python record.py --duration 60 --out session.jsonl

# 3. fabricate a tiny ground truth inside the recorded window, then score
python - <<'PY'
import json
t0 = min(json.loads(l)["recv_ts"] for l in open("session.jsonl"))
open("groundtruth.jsonl","w").write(
  "".join(json.dumps({"t":t0+dt,"label":l})+"\n"
          for dt,l in [(1,"present"),(30,"absent"),(45,"present")]))
PY
python score.py --session session.jsonl --truth groundtruth.jsonl
```

You'll get a `report.md` with a presence confusion matrix and CIs. (On simulate
data the numbers are meaningless — the point is the pipeline runs.)

## Real session

See [`protocol.md`](protocol.md): NTP the clocks, run `record.py` against real
ESP32 hardware, mark transitions on a phone with `mark.py` (or wire Tier-B
reference sensors — PIR / bed load-cell / Polar H10), and score. Report
held-out / cross-subject / cross-environment **separately**, each with CIs —
never a single in-room number.

## Tier-B reference sensors (P2) — ground truth without a human

A human pressing `mark.py` can't cover the **~50 overnight bed-exits** the power
calc asks for. `reference.py` converts independent reference-sensor logs into the
*same* `groundtruth.jsonl` / `vitalsref.jsonl` the scorer reads — so a multi-night
run self-labels. Each sensor is an **oracle on its own log**, never derived from
the system under test.

```bash
# Polar H10 chest strap → HR oracle for vitals scoring  (t,hr CSV)
python reference.py polar_h10   --in polar.csv --out vitalsref.jsonl

# Load-cell under the bed legs → bed_exit events + bed presence  (t,weight CSV)
python reference.py bed_loadcell --in bed.csv   --out groundtruth.jsonl --threshold 10

# PIR / door-contact → presence toggles  (t,motion 0/1 CSV)
python reference.py pir          --in pir.csv   --out groundtruth.jsonl

# score with the vitals oracle → adds a Bland–Altman HR/BR section
python score.py --session session.jsonl --truth groundtruth.jsonl \
    --vitals-ref vitalsref.jsonl --vitals-tolerance 5
```

Inputs accept epoch-seconds, epoch-ms, or ISO-8601 timestamps, and either a
named-header CSV (`--tcol`/`--vcol`) or a positional `t,value` CSV. Outputs are
appended, so several sensors populate one `groundtruth.jsonl`.

The **vitals** section reports `bias` (mean system−reference), the **95% limits
of agreement** (bias ± 1.96·SD — the band 95% of per-reading differences fall in),
MAE, and the within-±N-BPM rate with a Wilson CI. A small bias with wide LoA still
means poor per-reading agreement — read both.

## Honesty

This harness produces the *first defensible accuracy numbers* for RuView. It
does **not** itself make any claim — it measures. A single session in one room
is an anecdote; the ADR-151 protocol (≥3 environments, ≥N subjects, power-sized)
is what turns it into evidence. ADR-150 predicts cross-environment accuracy may
be low without per-room calibration — finding that out *is* the value.
