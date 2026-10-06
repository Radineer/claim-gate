# claim-gate

**Don't trust an AI agent's "done". Check it.**

When you hand work to a coding agent, a sub-agent, or an unattended `claude -p` / `codex exec` run, the last thing you get back is a report like "Done — updated the scheduler, backfilled the table, restarted the worker." That report is generated text, not an execution result.

claim-gate makes the agent hand over a **manifest of verifiable claims** instead, and checks each claim against the real system: files, database rows, processes, git history, fresh outputs, HTTP status, logs. If any single claim does not hold, the whole manifest fails and the exit code is non-zero, so a pipeline can refuse the work automatically.

- One file, Python 3 standard library only
- Does not change your data: SQL is a `SELECT` on a read-only connection, git runs without optional locks, process checks only observe, HTTP checks use `GET` (caveats: reading a SQLite database in WAL mode may create its `-wal`/`-shm` side files; `pm2 jlist` starts the pm2 daemon if it is not running; a `GET` is only as side-effect free as the server; the external commands `git`, `pm2`, `pgrep` and `curl` still run with your environment and their own configuration, such as git hooks)
- No model in the loop: every check is plain code you can read (results can still change with time, e.g. age limits, and with what your SQL or commands return)

[日本語の説明はこちら](README.ja.md)

## Install

```bash
pip install git+https://github.com/Radineer/claim-gate
claim-gate manifest.json
```

Or just copy `claim_gate.py` — it is one file with no dependencies.

## Quick start

```bash
python3 claim_gate.py manifest.json          # PASS/FAIL, exit 0 (pass) or 1 (fail); 2 if the manifest cannot be read
python3 claim_gate.py manifest.json --json   # machine-readable report
```

```json
{
  "task": "fix scheduler timezone bug",
  "checks": [
    {"type": "file_contains", "path": "app/scheduler.py", "substring": "as_naive"},
    {"type": "file_exists",   "path": "app/scheduler.py.bak-*"},
    {"type": "sqlite_scalar", "db": "data/app.db",
     "query": "SELECT count(*) FROM jobs WHERE status='failed'", "op": "==", "value": 0},
    {"type": "output_fresh",  "path": "out/report-*.csv", "max_age_hours": 24, "min_size": 100}
  ]
}
```

```
🔴 FAIL  fix scheduler timezone bug  (3/4 checks)
  ✓ file_contains: contains
  ✓ file_exists: exists: app/scheduler.py.bak-1006 (22B)
  ✗ sqlite_scalar: mismatch: actual=3 expected ==0
  ✓ output_fresh: report-1006.csv 2405B / 0.0h old
```

## Check types

| type | passes when |
|---|---|
| `file_exists` | the path (glob allowed) exists; optional `min_size`, `mtime_after` |
| `file_absent` | the path does not exist |
| `file_contains` | the file contains `substring`, or matches `regex` (give one of them, not both) |
| `sqlite_scalar` | a `SELECT` returns a value satisfying `op` / `value` (`==` `!=` `>` `>=` `<` `<=`) |
| `pm2_status` | every pm2 process with that name has the expected status |
| `process_running` | `pgrep -f pattern` finds a process |
| `git_changed` | `path` in `repo` has a working-tree change or commit history |
| `output_fresh` | the newest matching file is younger than `max_age_hours` and at least `min_size` bytes |
| `glob_count` | the number of matching files satisfies `op` / `value` |
| `http_status` | a GET returns the expected status code |
| `log_not_contains` | none of the given strings appears in the last `tail_lines` lines |

An empty manifest never passes, and an unknown check type fails.

## How we use it

Add one line to the prompt when delegating:

> Don't report "done". Output a JSON manifest of what you changed: files, DB tables with expected row counts, process names, output paths.

Then run claim-gate on the manifest **before reading the report**. In unattended jobs (cron, CI, agent loops), treat a non-zero exit as "the claims could not be confirmed" and do not accept the work. (It does not prove the work never happened: a missing permission or a network error also fails a check.)

Two rules matter more than the tool:

1. **The receiver decides what to check.** An agent that writes its own manifest can still choose easy claims. For important work, the person or pipeline receiving the result should define the checks.
2. **Check outputs, not only exit codes.** A job can exit 0 and still produce nothing. `output_fresh`, `glob_count` and `log_not_contains` let you make the output part of the check.

## Evaluation

`evaluation/benchmark.py` is a reproducible, synthetic benchmark (fixed seed, local temp dirs). It builds a small project, creates the files, database rows, outputs and logs that a finished job would leave behind (it does not run any real job), and hands claim-gate either a truthful manifest or one with exactly one false claim of a known type: a file never created, an edit never made, an overstated row count, a stale output, an empty output, a log line saying the job did nothing (`inserted=0`), a backup never taken, a deleted file that is still there.

| | result |
|---|---|
| manifests with one false claim (8 types × 20) | 160/160 failed |
| truthful manifests | 0/20 failed |
| of the 160 failures, failed on the injected claim and nothing else | 160/160 |

Each false case differs from the truthful one in exactly one thing: one extra false claim, one overstated expected value, or one change to the files that makes one existing claim false. The last row counts cases where exactly that one claim failed and every other claim passed. Same result on Python 3.9, 3.11, 3.13 and 3.14. This is the result on these 180 prepared manifests. It does **not** show that every false claim of these types will be caught, how often real agents fabricate, or whether a manifest covers everything that matters.

```bash
python3 evaluation/benchmark.py
```

## Limits

- It only sees what the manifest names. Changes nobody declared are invisible.
- A file's modification time can be changed, so `mtime_after` narrows a check to files touched after a point in time; it does not prove this run generated them.
- It verifies only the conditions you wrote, not that the requested work was done or done well: a fresh, non-empty output can still be incomplete or wrong.
- We have not measured how many real incidents it would have prevented.
- `output_fresh` cannot tell this run's output from an earlier one that is still within `max_age_hours`. To narrow it to files updated after this run started, use `file_exists` with `mtime_after` (see below).
- There is no overall time limit. A huge file, a pathological regex or a slow query can block; run it under your scheduler's timeout and treat a timeout as a failure.
- `pm2_status`, `process_running` and `http_status` call external commands (`pm2`, `pgrep`, `curl`).

## Tests

```bash
python3 tests/test_claim_gate.py -v
```

## License

MIT. See [LICENSE](LICENSE).
