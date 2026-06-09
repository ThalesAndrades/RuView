# RuView field-validation harness (ADR-151 P1)

Measure the semantic layer's **accuracy against ground truth**, instead of
trusting demos. This is the **Capture → Align → Score** pipeline from
[ADR-151](../../docs/adr/ADR-151-field-validation-harness.md). Pure Python 3
stdlib — no `pip install` (includes a minimal WebSocket client).

| File | Role |
|---|---|
| `record.py` | Subscribe to the sensing-server `/ws/sensing` and log every `sensing_update` + `semantic_event` to `session.jsonl`. |
| `mark.py` | Tier-A ground-truth marker — log timestamped `present`/`absent` toggles and point events (`bed_exit`, …) to `groundtruth.jsonl`. |
| `score.py` | Align the two on wall-clock time → `report.md` + `metrics.json`: presence confusion (precision/recall/F1/false-alarm-rate/onset-latency, with **Wilson 95% CIs**) and event matching (TP/FP/FN + latency). |
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

## Honesty

This harness produces the *first defensible accuracy numbers* for RuView. It
does **not** itself make any claim — it measures. A single session in one room
is an anecdote; the ADR-151 protocol (≥3 environments, ≥N subjects, power-sized)
is what turns it into evidence. ADR-150 predicts cross-environment accuracy may
be low without per-room calibration — finding that out *is* the value.
