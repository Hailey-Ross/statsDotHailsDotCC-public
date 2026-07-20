#!/usr/bin/env python3
# Developer tool. Renders every page twice, from the log on stdin and from events.db, and diffs the
# raw HTML. Needs $HAILS_SPLITDIR and $HAILS_AGGLOG rebuilt by hand first, since the regen deletes
# them.
#
# Usage:
#   hails-diffrender.py                       every scope in hosts.tsv order, plus the aggregate
#   hails-diffrender.py --scope example.com   one scope
#   hails-diffrender.py --keep /tmp/dr        keep both trees for eyeballing
import os
import re
import sys
import shutil
import difflib
import subprocess
import tempfile
import time

PANELS = os.environ.get("HAILS_PANELGEN", "/usr/local/bin/hails-panels.py")
MIN_PAGES = int(os.environ.get("HAILS_MIN_PAGES", "20"))
SPLITDIR = os.environ.get("HAILS_SPLITDIR", "")
AGGLOG = os.environ.get("HAILS_AGGLOG", "")

# Values that cannot match by construction, stripped before comparing. Everything else must be equal.
# Keep this list as short as possible and never add an unanchored pattern.
VOLATILE = [
    re.compile(rb"only [^<]*collected"),
]


def opt(name, default=None):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv and sys.argv.index(name) + 1 < len(sys.argv) else default


# hails-panels.py is not deterministic without all three of these pinned.
DETERMINISTIC = dict(os.environ, PYTHONHASHSEED="0", HAILS_SEED="0", HAILS_NOW=str(int(time.time())))


def norm(b):
    for rx in VOLATILE:
        b = rx.sub(b"", b)
    return b


def render_log(scope, logfile, outdir, title):
    os.makedirs(outdir, exist_ok=True)
    with open(logfile, "rb") as fh:
        p = subprocess.run([sys.executable, PANELS, title, outdir], stdin=fh, env=DETERMINISTIC,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return p


def render_db(scope, outdir, title):
    os.makedirs(outdir, exist_ok=True)
    return subprocess.run([sys.executable, PANELS, title, outdir, "--from-db", "--scope", scope],
                          stdin=subprocess.DEVNULL, env=DETERMINISTIC,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def compare(a_dir, b_dir):
    a = {f for f in os.listdir(a_dir) if f.endswith((".html", ".json"))}
    b = {f for f in os.listdir(b_dir) if f.endswith((".html", ".json"))}
    same, differing = [], []
    for f in sorted(a & b):
        x = norm(open(os.path.join(a_dir, f), "rb").read())
        y = norm(open(os.path.join(b_dir, f), "rb").read())
        (same if x == y else differing).append(f)
    return same, differing, sorted(a - b), sorted(b - a)


def first_diff(a_path, b_path, context=1):
    x = norm(open(a_path, "rb").read()).decode("utf-8", "replace").splitlines()
    y = norm(open(b_path, "rb").read()).decode("utf-8", "replace").splitlines()
    out = []
    for line in difflib.unified_diff(x, y, "log", "warehouse", n=context, lineterm=""):
        out.append(line)
        if len(out) > 24:
            break
    return out


def main():
    only = opt("--scope")
    keep = opt("--keep")
    root = keep or tempfile.mkdtemp(prefix="diffrender-")
    os.makedirs(root, exist_ok=True)

    scopes = []
    manifest = os.path.join(SPLITDIR, "hosts.tsv") if SPLITDIR else ""
    if only:
        # Resolved through the manifest, which is the only thing that knows the host to filename
        # mapping. "all" is not a host: its log is the aggregate.
        if only == "all":
            scopes = [("all", AGGLOG)]
        else:
            safe = only
            if manifest and os.path.exists(manifest):
                for line in open(manifest):
                    h, _, sf = line.strip().partition("\t")
                    if h == only:
                        safe = sf or h
                        break
            scopes = [(only, os.path.join(SPLITDIR, "%s.log" % safe) if SPLITDIR else "")]
    else:
        scopes = [("all", AGGLOG)]
        if manifest and os.path.exists(manifest):
            for line in open(manifest):
                h, _, safe = line.strip().partition("\t")
                if h:
                    scopes.append((h, os.path.join(SPLITDIR, "%s.log" % (safe or h))))

    print("comparing %d scope(s), trees under %s\n" % (len(scopes), root))
    total_diff = 0
    for scope, logfile in scopes:
        title = "All domains (aggregate)" if scope == "all" else scope
        a_dir = os.path.join(root, scope, "log")
        b_dir = os.path.join(root, scope, "db")

        if logfile and os.path.exists(logfile):
            pa = render_log(scope, logfile, a_dir, title)
        else:
            print("%-24s SKIP  no log file (%s)  COUNTED AS FAILURE"
                  % (scope, logfile or "HAILS_SPLITDIR unset"))
            total_diff += 1
            continue
        pb = render_db(scope, b_dir, title)

        if pa.returncode != 0 or pb.returncode != 0:
            print("%-24s ERROR log_rc=%d db_rc=%d" % (scope, pa.returncode, pb.returncode))
            for tag, p in (("log", pa), ("db", pb)):
                if p.returncode != 0:
                    print("   %s: %s" % (tag, p.stderr.decode("utf-8", "replace").strip()[:400]))
            total_diff += 1
            continue

        same, differing, only_a, only_b = compare(a_dir, b_dir)
        # A page count floor, because a renderer that stopped emitting a page would stop on both
        # sides and report a smaller, greener number.
        thin = len(same) + len(differing) < MIN_PAGES
        flag = "OK  " if not differing and not only_a and not only_b and not thin else "DIFF"
        print("%-24s %s %d same, %d differing, %d log only, %d db only%s"
              % (scope, flag, len(same), len(differing), len(only_a), len(only_b),
                 "  TOO FEW PAGES (expected >= %d)" % MIN_PAGES if thin else ""))
        if thin:
            total_diff += 1
        if only_a:
            print("   pages only the LOG produced : %s" % ", ".join(only_a))
        if only_b:
            print("   pages only the DB produced  : %s" % ", ".join(only_b))
        for f in differing[:6]:
            print("   --- %s" % f)
            for line in first_diff(os.path.join(a_dir, f), os.path.join(b_dir, f)):
                print("       %s" % line)
        if differing or only_a or only_b:
            total_diff += 1

    print()
    if total_diff:
        print("VERDICT: %d scope(s) differ or were skipped. The warehouse must not back the pages."
              % total_diff)
    else:
        print("VERDICT: the event replay reproduces the log path exactly, for the scopes and corpus")
        print("         compared. This does NOT cover the rollup tables or hails_query.py.")
    if not keep:
        shutil.rmtree(root, ignore_errors=True)
    return 1 if total_diff else 0


if __name__ == "__main__":
    sys.exit(main())
