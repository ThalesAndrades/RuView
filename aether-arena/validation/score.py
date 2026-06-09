#!/usr/bin/env python3
"""ADR-151 P1 — score recorded system output against ground truth.

Reads a `session.jsonl` (from record.py) + a `groundtruth.jsonl` (from mark.py),
aligns them on wall-clock time, and writes a `report.md` + `metrics.json` with:

  * Presence: per-second confusion matrix → accuracy / precision / recall / F1,
    false-alarm rate (/h), miss rate, onset latency — with Wilson 95% CIs.
  * Events (bed_exit, bathroom_occupied, …): event matching within a tolerance
    window → TP/FP/FN → precision/recall + latency.

Pure stdlib.

    python score.py --session session.jsonl --truth groundtruth.jsonl \
        --out report.md --json metrics.json [--offset 0] [--event-tolerance 120]
"""
import argparse, bisect, json, math, sys, time

# Truth label → the system semantic primitive that should fire for it.
EVENT_MAP = {
    "bed_exit": "bed_exit",
    "bathroom_enter": "bathroom_occupied",
    "bathroom_leave": "bathroom_occupied",
    "multi_room": "multi_room_transition",
}


def wilson(k, n, z=1.96):
    if n == 0:
        return (None, None, None)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = (z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / d
    return (p, max(0.0, c - h), min(1.0, c + h))


def load_session(path, offset):
    presence = []   # (t, bool)
    events = []     # (t, primitive, on)
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            t = rec["recv_ts"] + offset
            msg = rec["msg"]
            if msg.get("type") == "semantic_event":
                st = msg.get("state", {})
                on = st.get("active", True) if st.get("kind") == "boolean" else True
                events.append((t, msg.get("primitive", "?"), bool(on)))
            else:
                cls = msg.get("classification")
                if isinstance(cls, dict) and "presence" in cls:
                    presence.append((t, bool(cls["presence"])))
    presence.sort()
    events.sort()
    return presence, events


def load_truth(path):
    toggles = []    # (t, bool)  present/absent
    events = []     # (t, label)
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            t, label = rec["t"], rec["label"]
            if label in ("present", "absent"):
                toggles.append((t, label == "present"))
            else:
                events.append((t, label))
    toggles.sort()
    events.sort()
    return toggles, events


def carry_value(series, t):
    """Last boolean value in `series` (list of (t,val)) at or before t, else None."""
    ts = [x[0] for x in series]
    i = bisect.bisect_right(ts, t) - 1
    return series[i][1] if i >= 0 else None


def score_presence(sys_presence, truth_toggles, step=1.0):
    if not sys_presence or not truth_toggles:
        return None
    lo = max(sys_presence[0][0], truth_toggles[0][0])
    hi = min(sys_presence[-1][0], time.time() + 1e9)  # session end ~ last sample
    hi = min(sys_presence[-1][0], hi)
    # ground truth is only defined from its first marker onward
    hi = min(hi, sys_presence[-1][0])
    if hi <= lo:
        return None
    tp = tn = fp = fn = 0
    t = lo
    while t <= hi:
        s = carry_value(sys_presence, t)
        g = carry_value(truth_toggles, t)
        if s is not None and g is not None:
            if g and s:
                tp += 1
            elif g and not s:
                fn += 1
            elif (not g) and s:
                fp += 1
            else:
                tn += 1
        t += step
    n = tp + tn + fp + fn
    rec_p, rec_lo, rec_hi = wilson(tp, tp + fn)
    pre_p, pre_lo, pre_hi = wilson(tp, tp + fp)
    # onset latency: for each truth present-onset, time until system reads present
    lat = []
    for i, (tt, on) in enumerate(truth_toggles):
        if not on:
            continue
        # first system present sample at/after tt
        for (st, sv) in sys_presence:
            if st >= tt and sv:
                lat.append(st - tt)
                break
    lat.sort()
    dur_h = (hi - lo) / 3600.0
    return {
        "window_s": round(hi - lo, 1),
        "samples": n,
        "confusion": {"tp": tp, "tn": tn, "fp": fp, "fn": fn},
        "accuracy": round((tp + tn) / n, 4) if n else None,
        "recall": None if rec_p is None else round(rec_p, 4),
        "recall_ci95": None if rec_p is None else [round(rec_lo, 4), round(rec_hi, 4)],
        "precision": None if pre_p is None else round(pre_p, 4),
        "precision_ci95": None if pre_p is None else [round(pre_lo, 4), round(pre_hi, 4)],
        "f1": round(2 * pre_p * rec_p / (pre_p + rec_p), 4) if (pre_p and rec_p) else None,
        "miss_rate": None if rec_p is None else round(1 - rec_p, 4),
        "false_alarm_per_h": round(fp / dur_h, 2) if dur_h > 0 else None,
        "onset_latency_s": {
            "n": len(lat),
            "median": round(lat[len(lat) // 2], 2) if lat else None,
            "p90": round(lat[int(len(lat) * 0.9)], 2) if lat else None,
            "max": round(lat[-1], 2) if lat else None,
        },
    }


def score_events(sys_events, truth_events, tau):
    by_primitive = {}
    for (tt, label) in truth_events:
        prim = EVENT_MAP.get(label, label)
        by_primitive.setdefault(prim, {"truth": [], "matched_sys": set()})
        by_primitive[prim]["truth"].append((tt, label))
    # index system events per primitive
    sys_by_prim = {}
    for idx, (st, prim, on) in enumerate(sys_events):
        if not on:
            continue
        sys_by_prim.setdefault(prim, []).append((st, idx))
    out = {}
    for prim, info in by_primitive.items():
        cand = sorted(sys_by_prim.get(prim, []))
        cand_t = [c[0] for c in cand]
        used = set()
        tp = 0
        lat = []
        for (tt, label) in info["truth"]:
            # nearest unused system event within tau
            best = None
            for j, st in enumerate(cand_t):
                if j in used:
                    continue
                if abs(st - tt) <= tau:
                    if best is None or abs(st - tt) < abs(cand_t[best] - tt):
                        best = j
            if best is not None:
                used.add(best)
                tp += 1
                lat.append(cand_t[best] - tt)
        fn = len(info["truth"]) - tp
        fp = len([1 for j in range(len(cand)) if j not in used])
        rec_p, rec_lo, rec_hi = wilson(tp, tp + fn)
        pre_p, pre_lo, pre_hi = wilson(tp, tp + fp)
        lat.sort()
        out[prim] = {
            "truth_events": len(info["truth"]),
            "tp": tp, "fp": fp, "fn": fn,
            "recall": None if rec_p is None else round(rec_p, 4),
            "recall_ci95": None if rec_p is None else [round(rec_lo, 4), round(rec_hi, 4)],
            "precision": None if pre_p is None else round(pre_p, 4),
            "latency_s": {
                "median": round(lat[len(lat) // 2], 2) if lat else None,
                "max": round(lat[-1], 2) if lat else None,
            },
        }
    return out


def render(presence, events, meta):
    L = []
    L.append("# RuView field-validation report (ADR-151 P1)\n")
    L.append(f"- session: `{meta['session']}`  ·  ground truth: `{meta['truth']}`")
    L.append(f"- generated: {time.strftime('%Y-%m-%d %H:%M:%S')}  ·  clock offset applied: {meta['offset']:+.2f}s\n")

    L.append("## Presence (per-second)\n")
    if not presence:
        L.append("_No overlapping presence data + ground truth._\n")
    else:
        c = presence["confusion"]
        L.append(f"Window {presence['window_s']}s, {presence['samples']} samples.\n")
        L.append("| | truth present | truth absent |")
        L.append("|---|---|---|")
        L.append(f"| **sys present** | TP {c['tp']} | FP {c['fp']} |")
        L.append(f"| **sys absent**  | FN {c['fn']} | TN {c['tn']} |\n")
        def ci(x):
            return f" (95% CI {x[0]}–{x[1]})" if x else ""
        L.append(f"- **recall** {presence['recall']}{ci(presence['recall_ci95'])}  — miss rate {presence['miss_rate']}")
        L.append(f"- **precision** {presence['precision']}{ci(presence['precision_ci95'])}")
        L.append(f"- accuracy {presence['accuracy']}  ·  F1 {presence['f1']}")
        L.append(f"- **false-alarm rate** {presence['false_alarm_per_h']}/h")
        ol = presence["onset_latency_s"]
        L.append(f"- onset latency (n={ol['n']}): median {ol['median']}s · p90 {ol['p90']}s · max {ol['max']}s\n")

    L.append("## Events\n")
    if not events:
        L.append("_No ground-truth events to score._\n")
    else:
        L.append("| primitive | truth | TP | FP | FN | recall | precision | latency (med/max) |")
        L.append("|---|---|---|---|---|---|---|---|")
        for prim, e in sorted(events.items()):
            lat = e["latency_s"]
            L.append(f"| {prim} | {e['truth_events']} | {e['tp']} | {e['fp']} | {e['fn']} | "
                     f"{e['recall']} | {e['precision']} | {lat['median']}/{lat['max']}s |")
        L.append("")

    L.append("---")
    L.append("> **Read honestly.** This is *one* session in *one* room. A defensible accuracy "
             "claim requires the ADR-151 protocol: ≥3 environments, ≥N subjects, held-out / "
             "cross-subject / cross-environment reported separately, each with CIs. Numbers from "
             "`--source simulate` validate the harness, not the sensor.")
    return "\n".join(L) + "\n"


def main():
    ap = argparse.ArgumentParser(description="Score RuView output against ground truth.")
    ap.add_argument("--session", default="session.jsonl")
    ap.add_argument("--truth", default="groundtruth.jsonl")
    ap.add_argument("--out", default="report.md")
    ap.add_argument("--json", default="metrics.json")
    ap.add_argument("--offset", type=float, default=0.0, help="seconds added to system timestamps")
    ap.add_argument("--event-tolerance", type=float, default=120.0)
    args = ap.parse_args()

    sys_presence, sys_events = load_session(args.session, args.offset)
    truth_toggles, truth_events = load_truth(args.truth)
    presence = score_presence(sys_presence, truth_toggles)
    events = score_events(sys_events, truth_events, args.event_tolerance)

    meta = {"session": args.session, "truth": args.truth, "offset": args.offset}
    report = render(presence, events, meta)
    with open(args.out, "w") as f:
        f.write(report)
    with open(args.json, "w") as f:
        json.dump({"presence": presence, "events": events, "meta": meta}, f, indent=2)

    sys.stdout.write(report)
    print(f"\n[score] wrote {args.out} + {args.json}", file=sys.stderr)


if __name__ == "__main__":
    main()
