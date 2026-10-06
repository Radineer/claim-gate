#!/usr/bin/env python3
"""Reproducible benchmark: does claim-gate pass real work and fail fabricated claims?

Synthetic, fully local and deterministic (fixed seed). For each scenario we build a small
project in a temp dir, create what a finished job would leave behind (no real job runs), then hand claim-gate a manifest that is either
truthful or contains exactly one false claim of a known type. We count:

  - detection rate  : manifests with a false claim that FAIL   (higher is better)
  - false-fail rate : truthful manifests that FAIL               (lower is better)

This measures the checker on known claim types. It does NOT measure how often real agents
fabricate, nor whether a manifest covers everything that matters. See README "Limits".

Usage:
  python3 evaluation/benchmark.py            # table + exit 1 if any expectation is violated
  python3 evaluation/benchmark.py --json
"""
import json
import os
import random
import sqlite3
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import claim_gate  # noqa: E402

SEED = 20261006
N_PER_TYPE = 20

# Each false-claim type: how the agent's claim differs from reality.
FALSE_TYPES = [
    "file_never_created",     # claims a new file that does not exist
    "edit_never_made",        # claims an edit whose text is not in the file
    "row_count_overstated",   # claims N rows, table has fewer
    "stale_output",           # claims a fresh output, newest output is days old
    "empty_output",           # claims an output, the file is empty
    "silent_no_op",           # log says the job did nothing (tests the forbidden-string check, not a real no-op)
    "backup_not_taken",       # claims a backup, none exists
    "deleted_file_still_there",  # claims a removal, file still present
]


def build_project(root, rng):
    n_rows = rng.randint(3, 40)
    os.makedirs(os.path.join(root, "app"))
    os.makedirs(os.path.join(root, "out"))
    os.makedirs(os.path.join(root, "logs"))
    src = os.path.join(root, "app", "worker.py")
    with open(src, "w") as f:
        f.write("def handle(job):\n    return retry_with_backoff(job)\n")
    with open(src + ".bak-1", "w") as f:
        f.write("old")
    con = sqlite3.connect(os.path.join(root, "app.db"))
    con.execute("CREATE TABLE items(id INTEGER)")
    con.executemany("INSERT INTO items VALUES(?)", [(i,) for i in range(n_rows)])
    con.commit()
    con.close()
    with open(os.path.join(root, "out", "report-new.csv"), "w") as f:
        f.write("id\n" + "1\n" * 100)
    with open(os.path.join(root, "logs", "job.log"), "w") as f:
        f.write("start\ninserted=%d\ndone\n" % n_rows)
    return {"n_rows": n_rows, "src": src}


def truthful_checks(root, p):
    checks = [
        {"type": "file_exists", "path": p["src"]},
        {"type": "file_contains", "path": p["src"], "substring": "retry_with_backoff"},
        {"type": "file_exists", "path": p["src"] + ".bak-*"},
        {"type": "sqlite_scalar", "db": os.path.join(root, "app.db"), "query": "SELECT count(*) FROM items",
         "op": "==", "value": p["n_rows"]},
        {"type": "output_fresh", "path": os.path.join(root, "out", "report-*.csv"), "max_age_hours": 24, "min_size": 50},
        {"type": "log_not_contains", "path": os.path.join(root, "logs", "job.log"), "substring": ["inserted=0"]},
        {"type": "file_absent", "path": os.path.join(root, "app", "legacy.py")},
    ]
    for c, lab in zip(checks, LABELS):
        c["label"] = lab
    return checks


# Which check must be the one (and only one) that fails for each injected false claim.
EXPECTED_FAIL = {
    "file_never_created": "injected",
    "edit_never_made": "injected",
    "row_count_overstated": "rows",
    "stale_output": "output",
    "empty_output": "output",
    "silent_no_op": "log",
    "backup_not_taken": "backup",
    "deleted_file_still_there": "absent",
}
LABELS = ["src", "edit", "backup", "rows", "output", "log", "absent"]


def inject(root, p, kind, rng):
    """Return a manifest with exactly one false claim of `kind` (and mutate the world if needed)."""
    checks = truthful_checks(root, p)
    if kind == "file_never_created":
        checks.append({"type": "file_exists", "path": os.path.join(root, "app", "new_module_%d.py" % rng.randint(1, 999)), "label": "injected"})
    elif kind == "edit_never_made":
        checks.append({"type": "file_contains", "path": p["src"], "substring": "circuit_breaker(%d)" % rng.randint(1, 99), "label": "injected"})
    elif kind == "row_count_overstated":
        checks[3] = dict(checks[3], value=p["n_rows"] + rng.randint(1, 5))
    elif kind == "stale_output":
        old = os.path.join(root, "out", "report-new.csv")
        t = time.time() - rng.randint(2, 30) * 86400
        os.utime(old, (t, t))
    elif kind == "empty_output":
        open(os.path.join(root, "out", "report-new.csv"), "w").close()
    elif kind == "silent_no_op":
        with open(os.path.join(root, "logs", "job.log"), "w") as f:
            f.write("start\ninserted=0\ndone\n")
    elif kind == "backup_not_taken":
        os.remove(p["src"] + ".bak-1")
    elif kind == "deleted_file_still_there":
        with open(os.path.join(root, "app", "legacy.py"), "w") as f:
            f.write("still here")
    return checks


def main():
    rng = random.Random(SEED)
    rows, violations = [], []
    for kind in ["truthful"] + FALSE_TYPES:
        fails = attributed = 0
        for _ in range(N_PER_TYPE):
            with tempfile.TemporaryDirectory() as root:
                p = build_project(root, rng)
                checks = truthful_checks(root, p) if kind == "truthful" else inject(root, p, kind, rng)
                rep = claim_gate.verify({"task": kind, "checks": checks})
                fails += 0 if rep["passed"] else 1
                failed_labels = [r["label"] for r in rep["results"] if not r["ok"]]
                # 落ちた理由が埋め込んだ作り話そのものか（落ちた項目がちょうど1つで、それが期待の項目）
                if kind != "truthful" and failed_labels == [EXPECTED_FAIL[kind]]:
                    attributed += 1
        rows.append({"type": kind, "n": N_PER_TYPE, "failed": fails,
                     "failed_only_on_injected_claim": attributed if kind != "truthful" else None})
        expected_fail = kind != "truthful"
        if (fails != N_PER_TYPE or rows[-1]["failed_only_on_injected_claim"] != N_PER_TYPE) if expected_fail else (fails != 0):
            violations.append(kind)
    false_rows = [r for r in rows if r["type"] != "truthful"]
    summary = {
        "seed": SEED,
        "n_types": len(FALSE_TYPES),
        "n_per_type": N_PER_TYPE,
        "n_truthful": N_PER_TYPE,
        "n_false": N_PER_TYPE * len(FALSE_TYPES),
        "false_fail_rate": rows[0]["failed"] / N_PER_TYPE,
        "detection_rate": sum(r["failed"] for r in false_rows) / (N_PER_TYPE * len(FALSE_TYPES)),
        "failed_only_on_injected_claim": sum(r["failed_only_on_injected_claim"] for r in false_rows),
        "by_type": rows,
        "violations": violations,
    }
    if "--json" in sys.argv:
        print(json.dumps(summary, indent=2))
    else:
        print("%-26s %4s %7s" % ("scenario", "n", "FAILed"))
        for r in rows:
            print("%-26s %4d %7d" % (r["type"], r["n"], r["failed"]))
        print("detection rate  : %d/%d" % (sum(r["failed"] for r in false_rows), summary["n_false"]))
        print("false-fail rate : %d/%d" % (rows[0]["failed"], N_PER_TYPE))
        print("failed only on the injected claim: %d/%d" % (summary["failed_only_on_injected_claim"], summary["n_false"]))
    sys.exit(1 if violations else 0)


if __name__ == "__main__":
    main()
