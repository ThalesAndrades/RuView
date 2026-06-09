#!/usr/bin/env python3
"""ADR-151 P1 — Tier-A ground-truth marker.

Logs timestamped ground-truth labels to a JSONL file, against which the recorded
system output is scored. Pure stdlib. Two modes:

    python mark.py present              # one-shot: append one labelled event now
    python mark.py                      # interactive: type labels (or shortcuts) + Enter

Convention (interpreted by score.py):
  * `present` / `absent` toggle the ground-truth PRESENCE state (interval truth)
  * any other label is a point EVENT at that instant (e.g. `bed_exit`, `bathroom_enter`)

Run `mark.py` on the subject's phone (over SSH/Termux) or a laptop; press the
marker the instant each real transition happens. Keep clocks in NTP sync with
the sensing-server host (see protocol.md).
"""
import argparse, json, sys, time

SHORTCUTS = {
    "p": "present",
    "a": "absent",
    "b": "bed_exit",
    "e": "bathroom_enter",
    "x": "bathroom_leave",
    "s": "sit",
    "l": "lie_down",
    "u": "stand_up",
}


def append(path, label):
    rec = {"t": time.time(), "label": label}
    with open(path, "a", buffering=1) as f:
        f.write(json.dumps(rec) + "\n")
    return rec


def main():
    ap = argparse.ArgumentParser(description="Append timestamped ground-truth markers.")
    ap.add_argument("label", nargs="?", help="single label to log now; omit for interactive mode")
    ap.add_argument("--out", default="groundtruth.jsonl")
    args = ap.parse_args()

    if args.label:
        rec = append(args.out, args.label)
        print(f"[mark] {rec['label']} @ {rec['t']:.3f}", file=sys.stderr)
        return

    legend = "  ".join(f"{k}={v}" for k, v in SHORTCUTS.items())
    print(f"[mark] interactive → {args.out}", file=sys.stderr)
    print(f"[mark] shortcuts: {legend}", file=sys.stderr)
    print("[mark] type a label (or shortcut) + Enter; 'q' or empty line to quit.", file=sys.stderr)
    try:
        for line in sys.stdin:
            tok = line.strip()
            if not tok or tok == "q":
                break
            label = SHORTCUTS.get(tok, tok)
            rec = append(args.out, label)
            print(f"[mark] {rec['label']:<16} @ {time.strftime('%H:%M:%S')}", file=sys.stderr)
    except KeyboardInterrupt:
        pass
    print("[mark] done.", file=sys.stderr)


if __name__ == "__main__":
    main()
