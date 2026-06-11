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


# System vital metric → its field in the sensing_update `vital_signs` object.
VITALS_FIELD = {"hr": "heart_rate_bpm", "br": "breathing_rate_bpm"}


def load_session(path, offset):
    presence = []   # (t, bool)
    events = []     # (t, primitive, on)
    vitals = []     # (t, metric, value)  -- system-estimated HR/BR
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
                vs = msg.get("vital_signs")
                if isinstance(vs, dict):
                    for metric, field in VITALS_FIELD.items():
                        val = vs.get(field)
                        if isinstance(val, (int, float)) and val > 0:
                            vitals.append((t, metric, float(val)))
    presence.sort()
    events.sort()
    vitals.sort()
    return presence, events, vitals


def load_vitals_ref(path):
    """Reference vitals stream from reference.py: {"t","metric","value"} per metric."""
    ref = {}   # metric -> [(t, value)]
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            if "metric" not in rec:
                raise ValueError(f"vitals-ref record missing 'metric' field: {rec}")
            m = rec["metric"]
            ref.setdefault(m, []).append((float(rec["t"]), float(rec["value"])))
    for m in ref:
        ref[m].sort()
    return ref


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


def score_vitals(sys_vitals, ref, tol, window):
    """Bland–Altman agreement of system HR/BR vs an independent reference.

    Pairs each reference sample with the nearest system sample of the same
    metric within `window` seconds, then reports bias, 95% limits of agreement,
    MAE, and the within-±`tol`-BPM rate (with a Wilson CI).
    """
    sys_by_metric = {}
    for (t, m, v) in sys_vitals:
        sys_by_metric.setdefault(m, []).append((t, v))
    out = {}
    for metric, ref_series in ref.items():
        cand = sys_by_metric.get(metric, [])
        cand_t = [c[0] for c in cand]
        diffs = []        # sys - ref
        pairs = 0
        for (rt, rv) in ref_series:
            i = bisect.bisect_left(cand_t, rt)
            best = None
            for j in (i - 1, i):
                if 0 <= j < len(cand_t) and abs(cand_t[j] - rt) <= window:
                    if best is None or abs(cand_t[j] - rt) < abs(cand_t[best] - rt):
                        best = j
            if best is not None:
                diffs.append(cand[best][1] - rv)
                pairs += 1
        if pairs == 0:
            out[metric] = {"pairs": 0}
            continue
        bias = sum(diffs) / pairs
        var = sum((d - bias) ** 2 for d in diffs) / (pairs - 1) if pairs > 1 else 0.0
        sd = math.sqrt(var)
        mae = sum(abs(d) for d in diffs) / pairs
        within = sum(1 for d in diffs if abs(d) <= tol)
        w_p, w_lo, w_hi = wilson(within, pairs)
        out[metric] = {
            "pairs": pairs,
            "bias_bpm": round(bias, 2),
            "sd_bpm": round(sd, 2),
            "loa95_bpm": [round(bias - 1.96 * sd, 2), round(bias + 1.96 * sd, 2)],
            "mae_bpm": round(mae, 2),
            "tol_bpm": tol,
            "within_tol_rate": round(w_p, 4),
            "within_tol_ci95": [round(w_lo, 4), round(w_hi, 4)],
        }
    return out


def render(presence, events, vitals, meta):
    L = []
    L.append("# RuView field-validation report (ADR-151)\n")
    L.append(f"- session: `{meta['session']}`  ·  ground truth: `{meta['truth']}`"
             + (f"  ·  vitals ref: `{meta['vitals_ref']}`" if meta.get("vitals_ref") else ""))
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

    L.append("## Vitals (vs reference — Bland–Altman)\n")
    if not vitals:
        L.append("_No vitals reference supplied (`--vitals-ref`)._\n")
    else:
        L.append("| metric | pairs | bias | 95% LoA | MAE | within ±tol | tol |")
        L.append("|---|---|---|---|---|---|---|")
        for m, v in sorted(vitals.items()):
            if v.get("pairs", 0) == 0:
                L.append(f"| {m} | 0 | — | — | — | — | — |")
                continue
            loa = v["loa95_bpm"]
            wci = v["within_tol_ci95"]
            L.append(f"| {m} | {v['pairs']} | {v['bias_bpm']} | "
                     f"[{loa[0]}, {loa[1]}] | {v['mae_bpm']} | "
                     f"{v['within_tol_rate']} (CI {wci[0]}–{wci[1]}) | ±{v['tol_bpm']} |")
        L.append("\n_Bias = mean(system − reference); LoA = bias ± 1.96·SD (the band 95% of "
                 "differences fall within). A small bias with wide LoA still means poor "
                 "per-reading agreement._\n")

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
    ap.add_argument("--vitals-ref", default=None,
                    help="vitalsref.jsonl from reference.py → Bland–Altman HR/BR agreement")
    ap.add_argument("--vitals-tolerance", type=float, default=5.0, help="±BPM agreement band")
    ap.add_argument("--vitals-window", type=float, default=5.0,
                    help="max seconds between a reference and a system sample to pair them")
    args = ap.parse_args()

    sys_presence, sys_events, sys_vitals = load_session(args.session, args.offset)
    truth_toggles, truth_events = load_truth(args.truth)
    presence = score_presence(sys_presence, truth_toggles)
    events = score_events(sys_events, truth_events, args.event_tolerance)
    vitals = {}
    if args.vitals_ref:
        vitals = score_vitals(sys_vitals, load_vitals_ref(args.vitals_ref),
                              args.vitals_tolerance, args.vitals_window)

    meta = {"session": args.session, "truth": args.truth, "offset": args.offset}
    if args.vitals_ref:
        meta["vitals_ref"] = args.vitals_ref
    report = render(presence, events, vitals, meta)
    with open(args.out, "w") as f:
        f.write(report)
    with open(args.json, "w") as f:
        json.dump({"presence": presence, "events": events, "vitals": vitals, "meta": meta}, f, indent=2)

    sys.stdout.write(report)
    print(f"\n[score] wrote {args.out} + {args.json}", file=sys.stderr)


if __name__ == "__main__":
    main()
