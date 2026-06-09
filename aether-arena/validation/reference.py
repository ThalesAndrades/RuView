#!/usr/bin/env python3
"""ADR-151 P2 — Tier-B reference-sensor → canonical ground-truth converter.

The Tier-A marker (`mark.py`) needs a human pressing a button at each transition,
which doesn't scale to the **~50 overnight bed-exits** the power calc in
`protocol.md` asks for. Tier B replaces the human with **independent reference
sensors** whose logs are converted here into the *same* artifacts the scorer
already consumes:

  * `groundtruth.jsonl`  — `{"t": <epoch_s>, "label": <str>}`  (presence toggles +
    point events like `bed_exit`), read by `score.py` / `load_truth`.
  * `vitalsref.jsonl`    — `{"t": <epoch_s>, "metric": "hr"|"br", "value": <bpm>}`,
    the vitals oracle read by `score.py --vitals-ref` for Bland–Altman.

Each sensor is an **independent oracle on its own log** — never derived from the
system under test — so the comparison stays honest. Outputs are appended, so
several sensors can populate one `groundtruth.jsonl`.

Pure stdlib. Adapters:

    # Polar H10 (or any t,hr CSV) → HR reference stream for vitals scoring
    python reference.py polar_h10 --in polar.csv --out vitalsref.jsonl

    # Bed load-cell (t,weight_kg or t,occupied) → bed_exit events + bed presence
    python reference.py bed_loadcell --in bed.csv --out groundtruth.jsonl --threshold 10

    # PIR / contact (t,motion 0/1) → present/absent toggles
    python reference.py pir --in pir.csv --out groundtruth.jsonl

Input timestamps may be epoch seconds, epoch milliseconds, or ISO-8601.
CSV files may have a header (columns selected by name) or be positional `t,value`.
"""
import argparse, csv, json, sys
from datetime import datetime


# ── timestamp parsing ──────────────────────────────────────────────────────────
def parse_ts(raw):
    """Epoch seconds (float), epoch ms (int>1e11), or ISO-8601 → epoch seconds."""
    s = str(raw).strip()
    try:
        v = float(s)
        return v / 1000.0 if v > 1e11 else v   # ms heuristic
    except ValueError:
        pass
    iso = s.replace("Z", "+00:00")
    return datetime.fromisoformat(iso).timestamp()


def read_rows(path, tcol, vcol):
    """Yield (t, value_str) from a CSV. Named columns if a header matches, else
    positional (col 0 = time, col 1 = value)."""
    with open(path, newline="") as f:
        sample = f.read(2048)
        f.seek(0)
        has_header = csv.Sniffer().has_header(sample) if sample.strip() else False
        if has_header:
            r = csv.DictReader(f)
            for row in r:
                if tcol not in row or vcol not in row:
                    raise SystemExit(
                        f"[reference] columns {tcol!r}/{vcol!r} not in header "
                        f"{list(row.keys())}; pass --tcol/--vcol")
                if row[tcol] in (None, "") or row[vcol] in (None, ""):
                    continue
                yield parse_ts(row[tcol]), row[vcol]
        else:
            for row in csv.reader(f):
                if len(row) < 2 or not row[0].strip():
                    continue
                yield parse_ts(row[0]), row[1]


def write_jsonl(path, records):
    with open(path, "a", buffering=1) as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")
    return len(records)


# ── adapters ────────────────────────────────────────────────────────────────────
def adapt_vitals(path, tcol, vcol, metric):
    """t,value CSV → vitals reference stream. metric ∈ {hr, br}."""
    out = []
    for t, v in read_rows(path, tcol, vcol):
        try:
            val = float(v)
        except ValueError:
            continue
        if val <= 0:
            continue
        out.append({"t": t, "metric": metric, "value": val})
    return out


def _to_occupied(value, threshold):
    """Interpret a load-cell/PIR value as a boolean occupied/active state.
    Numeric → compared against threshold; textual true/false/on/off honoured."""
    s = str(value).strip().lower()
    if s in ("true", "occupied", "on", "present", "yes"):
        return True
    if s in ("false", "empty", "off", "absent", "no"):
        return False
    try:
        return float(s) >= threshold
    except ValueError:
        return False


def adapt_transitions(path, tcol, vcol, threshold, on_label, off_label, leave_event):
    """Boolean-state sensor → emit a label only on *change* (debounced truth).

    on_label / off_label: presence toggles emitted on each rising / falling edge.
    leave_event: if set, additionally emit this point event on a falling edge
                 (e.g. `bed_exit` when the bed load-cell goes occupied→empty).
    """
    out = []
    prev = None
    for t, v in read_rows(path, tcol, vcol):
        occ = _to_occupied(v, threshold)
        if occ == prev:
            continue
        if occ:
            if on_label:
                out.append({"t": t, "label": on_label})
        else:
            if leave_event and prev is not None:
                out.append({"t": t, "label": leave_event})
            if off_label:
                out.append({"t": t, "label": off_label})
        prev = occ
    return out


# ── CLI ──────────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description="Convert Tier-B reference-sensor logs to canonical ground truth.")
    sub = ap.add_subparsers(dest="sensor", required=True)

    p = sub.add_parser("polar_h10", help="t,hr CSV → HR vitals reference")
    p.add_argument("--in", dest="inp", required=True)
    p.add_argument("--out", default="vitalsref.jsonl")
    p.add_argument("--tcol", default="time")
    p.add_argument("--vcol", default="hr")
    p.add_argument("--metric", default="hr", choices=["hr", "br"])

    p = sub.add_parser("vitals", help="generic t,value CSV → vitals reference (--metric)")
    p.add_argument("--in", dest="inp", required=True)
    p.add_argument("--out", default="vitalsref.jsonl")
    p.add_argument("--tcol", default="time")
    p.add_argument("--vcol", default="value")
    p.add_argument("--metric", default="hr", choices=["hr", "br"])

    p = sub.add_parser("bed_loadcell", help="t,weight|occupied CSV → bed_exit + bed presence")
    p.add_argument("--in", dest="inp", required=True)
    p.add_argument("--out", default="groundtruth.jsonl")
    p.add_argument("--tcol", default="time")
    p.add_argument("--vcol", default="weight")
    p.add_argument("--threshold", type=float, default=10.0,
                   help="kg (or any unit) at/above which the bed reads occupied")

    p = sub.add_parser("pir", help="t,motion 0/1 CSV → present/absent toggles")
    p.add_argument("--in", dest="inp", required=True)
    p.add_argument("--out", default="groundtruth.jsonl")
    p.add_argument("--tcol", default="time")
    p.add_argument("--vcol", default="motion")
    p.add_argument("--threshold", type=float, default=0.5)

    args = ap.parse_args()

    if args.sensor in ("polar_h10", "vitals"):
        recs = adapt_vitals(args.inp, args.tcol, args.vcol, args.metric)
        n = write_jsonl(args.out, recs)
        print(f"[reference] {args.sensor}: wrote {n} {args.metric} samples → {args.out}",
              file=sys.stderr)
    elif args.sensor == "bed_loadcell":
        recs = adapt_transitions(args.inp, args.tcol, args.vcol, args.threshold,
                                 on_label="present", off_label=None, leave_event="bed_exit")
        n = write_jsonl(args.out, recs)
        print(f"[reference] bed_loadcell: wrote {n} labels "
              f"({sum(1 for r in recs if r['label']=='bed_exit')} bed_exit) → {args.out}",
              file=sys.stderr)
    elif args.sensor == "pir":
        recs = adapt_transitions(args.inp, args.tcol, args.vcol, args.threshold,
                                 on_label="present", off_label="absent", leave_event=None)
        n = write_jsonl(args.out, recs)
        print(f"[reference] pir: wrote {n} presence toggles → {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
