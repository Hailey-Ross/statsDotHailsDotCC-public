#!/usr/bin/env python3
# Developer tool. Reads the regen timing tsv written by hails-stats.sh and reports where the regen
# spends its time. Read only.
#
# Usage:
#   hails-timing.py                 last 20 runs, per phase
#   hails-timing.py --runs 100      wider window
#   hails-timing.py --scopes        break the per scope phases out by scope instead of folding them
import sys, os, collections

PATH = os.environ.get("HAILS_TIMING", "/var/lib/hails-stats/regen-timing.tsv")
# Phases that run once per scope, folded together by default.
PER_SCOPE = ("goaccess", "panels", "bandwidth", "perf")


def opt(name, default=None):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv and sys.argv.index(name) + 1 < len(sys.argv) else default


def hb(n):
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return "%.1f %s" % (n, u)
        n /= 1024.0
    return "%.1f TB" % n


def main():
    try:
        want_runs = int(opt("--runs", "20"))
    except (TypeError, ValueError):
        sys.stderr.write("--runs wants an integer\n")
        return 2
    if want_runs < 1:
        sys.stderr.write("--runs must be at least 1\n")
        return 2
    by_scope = "--scopes" in sys.argv

    try:
        with open(PATH, "r", encoding="utf-8") as fh:
            raw = [ln.rstrip("\n").split("\t") for ln in fh if ln.strip()]
    except OSError as e:
        sys.stderr.write("no timing data at %s: %s\n" % (PATH, e))
        return 1

    rows = [r for r in raw if len(r) >= 4]
    if not rows:
        sys.stderr.write("no usable rows in %s\n" % PATH)
        return 1

    # Only a run that emitted a `total` row finished. Partial runs would drag every phase mean down.
    complete = {r[0] for r in rows if len(r) >= 2 and r[1] == "total"}
    incomplete = len({r[0] for r in rows}) - len(complete)
    runs = sorted(complete)[-want_runs:]
    keep = set(runs)
    rows = [r for r in rows if r[0] in keep]
    if not runs:
        sys.stderr.write("no complete runs in %s (%d aborted or partial run(s) seen)\n"
                         % (PATH, incomplete))
        return 1

    totals = collections.defaultdict(float)     # run_ts -> wall
    agg = collections.defaultdict(list)         # label -> [wall per run]
    per_run = collections.defaultdict(lambda: collections.defaultdict(float))
    corpus = {}

    for run_ts, phase, scope, wall, size in ((r + ["", ""])[:5] for r in rows):
        try:
            w = float(wall)
        except ValueError:
            continue
        if phase == "total":
            totals[run_ts] = w
            try:
                corpus[run_ts] = int(size)
            except (ValueError, TypeError):
                pass
            continue
        label = "%s[%s]" % (phase, scope) if (by_scope and scope) else phase
        per_run[run_ts][label] += w

    for run_ts, phases in per_run.items():
        for label, w in phases.items():
            agg[label].append(w)

    nruns = len(per_run) or 1
    mean_total = sum(totals.values()) / len(totals) if totals else 0.0

    print("timing from %s" % PATH)
    print("%d runs, mean total %.2fs" % (nruns, mean_total), end="")
    if corpus:
        vals = [corpus[k] for k in sorted(corpus)]
        latest = vals[-1]
        lo, hi = min(vals), max(vals)
        if hi > lo * 1.5:
            print(", corpus %s (RANGE %s to %s over this window, so it is not one number)"
                  % (hb(latest), hb(lo), hb(hi)))
        else:
            print(", corpus %s" % hb(latest))
    else:
        print()
    print()
    print("%-22s %8s %8s %8s %8s   %s" % ("phase", "mean", "min", "max", "share", "calls/run"))
    print("-" * 74)

    ordered = sorted(agg.items(), key=lambda kv: -sum(kv[1]) / len(kv[1]))
    acc = 0.0
    for label, vals in ordered:
        m = sum(vals) / len(vals)
        acc += m
        share = (m / mean_total * 100) if mean_total else 0.0
        calls = sum(1 for r in per_run.values() if label in r)
        print("%-22s %7.2fs %7.2fs %7.2fs %7.1f%%   %d/%d"
              % (label, m, min(vals), max(vals), share, calls, nruns))

    print("-" * 74)
    print("%-22s %7.2fs %25.1f%%" % ("instrumented", acc, (acc / mean_total * 100) if mean_total else 0))
    gap = mean_total - acc
    if gap < -0.01:
        print("%-22s %7.2fs %25s   OVERLAP: phases exceed total, check calls/run above"
              % ("unaccounted", gap, ""))
    else:
        print("%-22s %7.2fs %25.1f%%   (log discovery, nav.js, asset copies, landing page)"
              % ("unaccounted", gap, (gap / mean_total * 100) if mean_total else 0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
