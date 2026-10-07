# claim-gate

**Don't trust an AI agent's "done". Check it.**

When you hand work to a coding agent, a sub-agent, or an unattended `claude -p` / `codex exec` run, the last thing you get back is a report like "Done — updated the scheduler, backfilled the table, restarted the worker." That report is generated text, not an execution result.

claim-gate makes the agent hand over a **manifest of verifiable claims** instead, and checks each claim against the real system: files, database rows, processes, git history, fresh outputs, HTTP status, logs. If any single claim does not hold, the whole manifest fails and the exit code is non-zero, so a pipeline can refuse the work automatically (the one exception is a check you have marked untrusted with `--known-good`, see below).

- One file, Python 3 standard library only
- Designed not to change your data (see the caveats at the end of this line): SQL is a `SELECT` on a read-only (`mode=ro`) connection, or, only when a WAL-mode database fails to open read-only (this happened on a production database whose `-shm` file was absent), on a `mode=rw` connection with `query_only` on; git runs without optional locks, process checks only observe, HTTP checks use `GET` (caveats: reading a SQLite database in WAL mode may create its `-wal`/`-shm` side files; `pm2 jlist` starts the pm2 daemon if it is not running; a `GET` is only as side-effect free as the server; the external commands `git`, `pm2`, `pgrep` and `curl` still run with your environment and their own configuration, such as git hooks)
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

An empty manifest never passes. An unknown check type, a field the type does not take (a typo such as `min_szie`), a missing required field, or a field of the wrong type makes that check fail.

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

## Related work

claim-gate is not a new idea. It is a small, dependency-free version of things that already exist:

- **Server state testing.** [goss](https://github.com/goss-org/goss), [Serverspec](https://serverspec.org/), [Chef InSpec](https://github.com/inspec/inspec) and [Testinfra](https://github.com/pytest-dev/pytest-testinfra) check a declared state (files, processes, services, HTTP) against a real machine and fail with an exit code. claim-gate uses the same mechanism; the difference is only that the declaration is the receiving side's acceptance check for an AI agent's report.
- **State-based grading of agents.** Benchmarks such as [τ-bench](https://arxiv.org/abs/2406.12045), [AppWorld](https://aclanthology.org/2024.acl-long.850/) and [OSWorld](https://arxiv.org/abs/2404.07972) score an agent by the final state of its environment, not by what it says.
- **False success in agents.** [Advani (2026)](https://arxiv.org/abs/2606.09863) measures agents that report completion while the state says otherwise, and finds LLM judges unreliable at detecting it. [Smyth et al. (2026)](https://arxiv.org/abs/2609.20812) measure overclaiming against the agent's own transcript. [Zhu et al. (2026)](https://arxiv.org/abs/2609.35732) reduce false success with a structured evidence contract. [Nguyen and Tran (2026)](https://arxiv.org/abs/2605.17998) describe a runtime where agents propose completion and a read-only verifier decides admission, which is the same design as claim-gate.
- **Similar tools.** [agent-completion-verifier](https://github.com/Luca-1304/agent-completion-verifier) and [AgentVerify](https://github.com/aliasfoxkde/AgentVerify) check completion claims deterministically against state; [agent-claimcheck](https://github.com/B0yko/agent-claimcheck) combines rules, a classifier and an LLM judge over traces. If you need signed receipts, trace adapters or classifiers, look at those.

What claim-gate adds is small: one file with only the Python standard library, check types aimed at unattended jobs (SQLite rows, pm2, fresh outputs, "did nothing" log lines), and failing closed when a claim cannot be checked.

## Learning from known-good runs

Checks written by a person or a model are sometimes too strict and reject work that was fine. If a job runs repeatedly, you can let past runs that were confirmed correct overrule those checks:

```bash
claim-gate manifest.json --history history.jsonl        # appends one line per run: which checks held
# after someone confirms a run was correct, copy its line into known-good.jsonl
claim-gate manifest.json --known-good known-good.jsonl  # a check that failed on a known-good run only warns
```

A check that failed on a known-good run of the same task is untrusted: it is still reported but does not count either way, and the verdict comes from the remaining checks. If no trusted check is left, the manifest fails. Only runs with the same `task` name count, so give the manifest a `task`; only a check that looked at the system and found the condition false can be excused: a malformed check (unknown type, bad or missing fields) and a check that could not look (unreadable file, failing command, no HTTP response) never are. A check is identified by its `label` together with a hash of its content, so editing a check makes it a new check. This is the idea of filtering generated postconditions against known-correct executions from [nl2postcond](https://arxiv.org/abs/2310.01831) and [DeCon](https://arxiv.org/abs/2501.02901).

What we measured: on public tau2-bench trajectories, three LLMs wrote manifests from the customer's request, the policy and the records before the work, for 60 false successes and 60 correct runs. Dropping malformed checks and checks that failed on a known-correct earlier run of the same task cut correct runs rejected from 19, 38 and 7 to 0, 2 and 1 (out of 60), while false successes rejected went from 37, 41 and 30 to 33, 27 and 28 ([evaluation/calibration-tau2.json](evaluation/calibration-tau2.json)). In that experiment malformed checks were dropped; claim-gate instead fails on them, so with the CLI malformed checks would add rejections. It only applies when correct earlier runs of the same task exist (105 of the 120 runs had them), and the sample was balanced by design, so these are not production rates.

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
