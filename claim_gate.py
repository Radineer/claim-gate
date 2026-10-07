#!/usr/bin/env python3
"""claim-gate: verify an AI agent's "done" claims against the real system, with plain code.

An agent's completion report is generated text, not an execution result. claim-gate checks a
machine-readable manifest of claims (files, DB rows, processes, git changes, outputs, HTTP status,
logs) against primary data. If any single check fails, the whole manifest fails (exit code 1).

Does not change your data by design: SQL is a SELECT on a mode=ro connection (mode=rw + query_only only
when a WAL-mode file fails to open read-only), git runs without
optional locks, process checks only observe (pgrep / pm2 jlist), HTTP checks use GET.
Caveats: reading a WAL-mode SQLite database may create its -wal/-shm side files; `pm2 jlist` starts
the pm2 daemon if it is not running; a GET is only as side-effect free as the server; the external
commands (git, pm2, pgrep, curl) run with your environment and their own configuration.
Python 3 standard library only.

Usage:
  python3 claim_gate.py manifest.json           # human-readable PASS/FAIL, exit 0/1
  python3 claim_gate.py manifest.json --json    # machine-readable report
  echo '<json>' | python3 claim_gate.py -        # read manifest from stdin

Manifest example:
{
  "task": "fix scheduler timezone bug",
  "checks": [
    {"type": "file_exists",   "path": "app/scheduler.py", "min_size": 1000},
    {"type": "file_contains", "path": "app/scheduler.py", "substring": "as_naive"},
    {"type": "file_exists",   "path": "app/scheduler.py.bak-*"},
    {"type": "sqlite_scalar", "db": "data/app.db",
     "query": "SELECT count(*) FROM jobs WHERE status='failed'", "op": "==", "value": 0},
    {"type": "output_fresh",  "path": "out/report-*.csv", "max_age_hours": 24, "min_size": 100}
  ]
}
"""
import sys, os, json, re, glob, fnmatch, stat, sqlite3, urllib.parse, subprocess, operator, collections, math

OPS = {"==": operator.eq, "!=": operator.ne, ">": operator.gt, ">=": operator.ge,
       "<": operator.lt, "<=": operator.le}


def _glob1(path):
    """Glob allowed. Returns the newest existing match, or None.

    Every lookup goes through _strict_glob / os.stat, so a path that cannot be read raises
    (and the check fails) instead of being skipped in favour of an older, readable one.
    """
    hits = []
    for h in _strict_glob(path):
        try:
            hits.append((os.stat(h).st_mtime, h))
        except FileNotFoundError:  # broken symlink: nothing there
            continue
    return max(hits)[1] if hits else None


def _cmp(actual, op, expected):
    fn = OPS.get(op)
    if not fn:
        return False, f"unknown operator {op}"
    try:
        return bool(fn(actual, expected)), ""
    except Exception as e:
        return False, str(e)


def check_file_exists(c):
    p = _glob1(c["path"])
    if not p:
        return False, f"missing: {c['path']}"
    size = os.path.getsize(p)
    if "min_size" in c and size < int(c["min_size"]):
        return False, f"size {size} < min {c['min_size']} ({p})"
    if "mtime_after" in c:
        import datetime as _dt
        mt = os.path.getmtime(p)
        thr = _dt.datetime.fromisoformat(c["mtime_after"]).timestamp()
        if mt <= thr:  # strictly after: a file stamped exactly at the threshold is not "after"
            return False, f"mtime {mt:.0f} is not after {c['mtime_after']} ({p})"
    return True, f"exists: {p} ({size}B)"


def check_file_absent(c):
    # "Absent" must mean "looked everywhere and found nothing", not "could not look".
    try:
        hits = _strict_glob(c["path"])
    except OSError as e:
        return False, f"cannot check {c['path']}: {e}"
    return (not hits), ("absent as expected" if not hits else f"must not exist: {hits[0]}")


def _is_file_strict(path):
    """Regular file (following links)? A broken link is not a file; an unreadable target raises."""
    try:
        return stat.S_ISREG(os.stat(path).st_mode)
    except FileNotFoundError:
        return False


def _strict_glob(pattern):
    """Like glob.glob (broken symlinks included), but raises OSError on any directory it cannot read.

    glob.glob silently treats an unreadable directory as empty, which would let file_absent pass
    on a file it never saw.
    """
    absolute = pattern.startswith(os.sep)
    parts = [x for x in pattern.split(os.sep) if x]
    paths = [os.sep if absolute else ""]
    for k, part in enumerate(parts):
        last = k == len(parts) - 1
        nxt = []
        for base in paths:
            if glob.has_magic(part):
                names = os.listdir(base or ".")  # PermissionError propagates
                if not part.startswith("."):
                    names = [n for n in names if not n.startswith(".")]
                cands = [os.path.join(base, n) for n in fnmatch.filter(names, part)]
            else:
                cands = [os.path.join(base, part)]
            for cand in cands:
                try:
                    os.lstat(cand)  # PermissionError propagates; a broken symlink still counts
                    if last:
                        nxt.append(cand)
                    elif stat.S_ISDIR(os.stat(cand).st_mode):  # follows links; PermissionError propagates
                        nxt.append(cand)
                except (FileNotFoundError, NotADirectoryError):
                    continue
        paths = nxt
        if not paths:
            break
    if pattern.endswith(os.sep):  # "x/" names a directory, never a file called x
        paths = [p for p in paths if stat.S_ISDIR(os.stat(p).st_mode)] if paths else []
    return paths


def check_file_contains(c):
    p = _glob1(c["path"])
    if not p:
        return False, f"missing: {c['path']}"
    try:
        with open(p, encoding="utf-8", errors="replace") as fh:
            txt = fh.read()
    except Exception as e:
        return False, f"read failed: {e}"
    if "substring" in c and "regex" in c:
        return False, "give substring or regex, not both"
    if "substring" in c:
        ok = c["substring"] in txt
        return ok, ("contains" if ok else f"substring not found: {c['substring'][:40]!r}")
    if "regex" in c:
        ok = re.search(c["regex"], txt) is not None
        return ok, ("matches" if ok else f"regex not matched: {c['regex'][:40]}")
    return False, "substring/regex not given"


def _is_wal_file(db):
    """SQLite header bytes 18 and 19 are both 2 for a WAL-mode database."""
    try:
        with open(db, "rb") as f:
            head = f.read(20)
    except OSError:
        return False
    return head[:16] == b"SQLite format 3\x00" and head[18:20] == b"\x02\x02"


def _ro_connect(db):
    """A connection that cannot change the data: mode=ro on the file (or mode=rw for a WAL file with no
    side files, see below), plus query_only on the connection.

    The path is percent-encoded so "#" or "?" in a file name cannot change the URI (and drop mode=ro).
    """
    path = urllib.parse.quote(os.path.abspath(db))
    conn = None
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=20)
        conn.execute("PRAGMA query_only=ON")
        conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchall()
        return conn
    except sqlite3.Error as e:  # always close, whatever went wrong (e.g. "file is not a database")
        if conn is not None:
            conn.close()
        # A WAL-mode file with no -shm file can fail to open read-only (seen on a production database).
        # Only that case falls back; any other error is a failed check.
        if not isinstance(e, sqlite3.OperationalError) or "unable to open" not in str(e) or not _is_wal_file(db):
            raise
    # Fallback: a normal connection that refuses writes (query_only). mode=rw never creates a missing
    # database; SQLite may create its -wal/-shm side files, the data itself is not changed.
    conn = sqlite3.connect("file:%s?mode=rw" % path, uri=True, timeout=20)
    conn.execute("PRAGMA query_only=ON")
    return conn


def check_sqlite_scalar(c):
    db = c["db"]
    if not os.path.exists(db):
        return False, f"DB missing: {db}"
    q = c["query"].strip()
    if "op" not in c or "value" not in c:
        return False, "sqlite_scalar needs both op and value (a bare query would pass on any row)"
    if not q.lower().startswith("select"):
        return False, "only SELECT is allowed (read-only)"
    note = ""
    # Opened read-only (mode=ro) and with query_only, so the connection itself cannot write.
    conn = _ro_connect(db)
    try:
        row = conn.execute(q).fetchone()
    except Exception as e:  # noqa: BLE001
        return False, f"SQL failed: {e}"
    finally:
        conn.close()
    # No row, or NULL, is never a pass: "the query returned nothing" must not satisfy op "!=".
    if not row or row[0] is None:
        return False, f"query returned {'no rows' if not row else 'NULL'}{note}"
    actual = row[0]
    ok, err = _cmp(actual, c["op"], c["value"])
    return ok, (f"actual={actual} {c['op']} {c['value']}{note}" if ok else
                f"mismatch: actual={actual} expected {c['op']}{c['value']} {err}{note}")


def check_pm2_status(c):
    name = c["name"]
    expect = c.get("expect", "online")
    try:
        r = subprocess.run(["pm2", "jlist"], capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            return False, f"pm2 jlist exited {r.returncode}: {r.stderr.strip()[:80]}"
        arr = json.loads(r.stdout)
    except Exception as e:
        return False, f"pm2 jlist failed: {e}"
    # Every process with that name (cluster instances, duplicates) must be in the expected state.
    sts = [p.get("pm2_env", {}).get("status", "?") for p in arr if isinstance(p, dict) and p.get("name") == name]
    if not sts:
        return False, f"no pm2 process {name}"
    ok = all(st == expect for st in sts)
    return ok, f"status={','.join(sts)} (expected all {expect})"


def check_process_running(c):
    pat = c["pattern"]
    try:
        r = subprocess.run(["pgrep", "-f", "--", pat], capture_output=True, text=True, timeout=15)
    except Exception as e:
        return False, f"pgrep failed: {e}"
    if r.returncode not in (0, 1):  # 0 = found, 1 = none, anything else = pgrep itself failed
        return False, f"pgrep exited {r.returncode}: {r.stderr.strip()[:80]}"
    pids = [x for x in r.stdout.split() if x]
    return (r.returncode == 0 and len(pids) > 0), (f"PID {','.join(pids)}" if pids else f"no process: {pat}")


def check_git_changed(c):
    """True if `path` in `repo` has a working-tree change or any commit touching it (read-only)."""
    repo = c["repo"]; path = c.get("path", "")
    # "--" and --literal-pathspecs: "-h" or ":(exclude)x" is a file name, never an option or pathspec magic
    spec = ["--", path] if path else []
    try:
        # --no-optional-locks: do not refresh the index while looking.
        rs = subprocess.run(["git", "--no-optional-locks", "--literal-pathspecs", "-c", "core.fsmonitor=false", "-C", repo, "status", "--porcelain"] + spec,
                            capture_output=True, text=True, timeout=20)
        rl = subprocess.run(["git", "--no-optional-locks", "--literal-pathspecs", "-c", "core.fsmonitor=false", "-C", repo, "log", "-1", "--oneline"] + spec,
                            capture_output=True, text=True, timeout=20)
    except Exception as e:
        return False, f"git failed: {e}"
    if rs.returncode != 0:
        return False, f"git status failed (exit {rs.returncode}): {rs.stderr.strip()[:80]}"
    # git log exits non-zero in a repository with no commits yet: that only means "no history".
    diff, log = rs.stdout.strip(), (rl.stdout.strip() if rl.returncode == 0 else "")
    if diff:
        return True, f"working tree changed: {diff[:60]}"
    if log:
        return True, f"has commit history: {log[:60]}"
    return False, f"no change and no history: {path}"


FUTURE_SKEW_SECONDS = 300


def check_output_fresh(c):
    """The newest file matching `path` is younger than max_age_hours and at least min_size bytes.

    Judges the newest output, not the exit code: a stale or empty newest output fails here.
    It cannot tell this run's output from an earlier run's that is still within max_age_hours;
    to narrow it to files updated after the run started, use file_exists with mtime_after.
    """
    import time
    pattern = c["path"]
    hits = [h for h in _strict_glob(pattern) if _is_file_strict(h)]
    if not hits:
        return False, f"no output: {pattern}"
    newest = max(hits, key=os.path.getmtime)
    age_h = (time.time() - os.path.getmtime(newest)) / 3600
    size = os.path.getsize(newest)
    min_size = int(c.get("min_size", 1))
    max_age = float(c.get("max_age_hours", 24))
    if size < min_size:
        return False, f"newest {os.path.basename(newest)} is {size}B < {min_size}B (empty)"
    if age_h > max_age:
        return False, f"newest {os.path.basename(newest)} is {age_h:.1f}h old > {max_age}h (stale)"
    # A timestamp in the future (beyond 5 minutes of clock skew) is not evidence of a new output.
    if age_h < -FUTURE_SKEW_SECONDS / 3600:
        return False, f"newest {os.path.basename(newest)} has a modification time {-age_h:.1f}h in the future"
    return True, f"{os.path.basename(newest)} {size}B / {age_h:.1f}h old"


def check_glob_count(c):
    """Number of files matching `path` (optionally filtered by min_size / max_age_hours) vs value (default op >=)."""
    pattern = c["path"]
    try:
        # strict: an unreadable directory or file must not count as "0 files" (op "==" 0 would pass)
        hits = [h for h in _strict_glob(pattern) if _is_file_strict(h)]
    except OSError as e:
        return False, f"cannot list {pattern}: {e}"
    if c.get("min_size") is not None:
        hits = [h for h in hits if os.path.getsize(h) >= int(c["min_size"])]
    if c.get("max_age_hours") is not None:
        import time
        cutoff = time.time() - float(c["max_age_hours"]) * 3600
        hits = [h for h in hits if os.path.getmtime(h) >= cutoff]
    ok, err = _cmp(len(hits), c.get("op", ">="), c["value"])
    return ok, err or f"{len(hits)} files (expected {c.get('op', '>=')} {c['value']})"


def check_http_status(c):
    """HTTP status of a GET request (never use on endpoints with side effects)."""
    url = c["url"]
    expect = c.get("value", 200)
    if not url.startswith(("http://", "https://")):
        return False, f"url must start with http:// or https:// (got {url[:40]!r})"
    try:
        r = subprocess.run(
            # -q (must be first): ignore ~/.curlrc, so no config can turn this into a write or a PUT/POST.
            ["curl", "-q", "-s", "--globoff", "-o", "/dev/null", "-w", "%{http_code}", "-L", "--proto", "=http,https",
             "-m", str(c.get("timeout", 20)), "--url", url],
            capture_output=True, text=True, timeout=float(c.get("timeout", 20)) + 10)
        out = r.stdout.strip()
        # exactly one 3-digit status: "[1-2]" globbing is off, but never trust a concatenated "200200"
        code = int(out) if len(out) == 3 and out.isdigit() else 0
    except Exception as e:  # noqa: BLE001
        return False, f"fetch failed: {e}"
    # No HTTP response at all (DNS, refused, timeout) is never a pass, whatever op/value say.
    if r.returncode != 0 or code == 0:
        return False, f"no HTTP response (curl exit {r.returncode}, code {code:03d})"
    ok, err = _cmp(code, c.get("op", "=="), expect)
    return ok, err or f"HTTP {code} (expected {c.get('op', '==')} {expect})"


def check_log_not_contains(c):
    """None of the forbidden substrings appears in the last tail_lines lines of the log.

    Examples of "ran but did nothing" signals: "graceful no-op", "inserted=0", "0 dirs deleted", "401".
    """
    path = _glob1(c["path"])
    if not path:
        return False, f"log missing: {c['path']}"
    n = int(c.get("tail_lines", 50))
    try:
        with open(path, "rb") as f:
            tail = [line.decode("utf-8", "replace") for line in collections.deque(f, maxlen=n)]
    except Exception as e:  # noqa: BLE001
        return False, f"unreadable: {e}"
    subs = c["substring"] if isinstance(c["substring"], list) else [c["substring"]]
    text = "".join(tail)  # lines keep their newlines, so a multi-line forbidden string is found too
    hit = [s for s in subs if s in text]
    if hit:
        return False, f"last {n} lines contain forbidden string: {hit}"
    return True, f"last {n} lines: no {subs} "


CHECKERS = {
    "file_exists": check_file_exists,
    "file_absent": check_file_absent,
    "file_contains": check_file_contains,
    "sqlite_scalar": check_sqlite_scalar,
    "pm2_status": check_pm2_status,
    "process_running": check_process_running,
    "git_changed": check_git_changed,
    "output_fresh": check_output_fresh,
    "glob_count": check_glob_count,
    "http_status": check_http_status,
    "log_not_contains": check_log_not_contains,
}


NUMERIC_KEYS = ("max_age_hours", "min_size", "timeout", "tail_lines", "value")


def _bad_number(c):
    """NaN / infinity would make every comparison false (so "!=" and age limits silently pass)."""
    for k in NUMERIC_KEYS:
        v = c.get(k)
        if isinstance(v, bool) or v is None:
            continue
        if isinstance(v, float) or (isinstance(v, str) and k != "value"):
            try:
                f = float(v)
            except ValueError:
                return f"{k} is not a number: {v!r}"
            if not math.isfinite(f):
                return f"{k} must be a finite number, got {v!r}"
    for k in ("max_age_hours", "timeout"):
        if k in c and float(c[k]) < (0 if k != "timeout" else 1e-9):
            return f"{k} must not be negative, got {c[k]!r}"
    # Byte and line counts are whole numbers: a float like 100.9 or 1e-324 would round silently.
    for k, low in (("min_size", 0), ("tail_lines", 1)):
        if k in c:
            v = c[k]
            if isinstance(v, bool) or not (isinstance(v, int) or (isinstance(v, str) and v.isdigit())) or int(v) < low:
                return f"{k} must be a whole number of at least {low}, got {v!r}"
    return None


def _run_one(i, c):
    """One check. Anything unexpected (bad shape, bad type, an exception) is a FAIL, never a crash."""
    if not isinstance(c, dict):
        return {"i": i, "type": None, "ok": False, "detail": f"check must be an object, got {c!r}"[:120], "label": ""}
    t = c.get("type")
    label = c.get("label", "") if isinstance(c.get("label", ""), str) else ""
    fn = CHECKERS.get(t) if isinstance(t, str) else None
    if not fn:
        return {"i": i, "type": str(t), "ok": False, "detail": f"unknown check type: {t!r}"[:120], "label": label}
    try:
        bad = _bad_number(c)
        ok, detail = (False, bad) if bad else fn(c)
    except Exception as e:  # noqa: BLE001
        ok, detail = False, f"check raised: {e}"
    return {"i": i, "type": t, "ok": bool(ok), "detail": detail, "label": label}


def verify(manifest):
    if not isinstance(manifest, dict) or not isinstance(manifest.get("checks", []), list):
        return {"task": "", "passed": False, "total": 0, "failed": 0,
                "results": [], "error": "manifest must be an object with a list of checks"}
    checks = manifest.get("checks", [])
    results = []
    for i, c in enumerate(checks):
        results.append(_run_one(i, c))
    passed = all(r["ok"] for r in results) if results else False
    return {"task": manifest.get("task", ""), "passed": passed,
            "total": len(results), "failed": sum(1 for r in results if not r["ok"]),
            "results": results}


def _usage_error(msg, as_json):
    """Exit 2: the manifest could not even be read. With --json the error is still JSON on stdout."""
    if as_json:
        print(json.dumps({"passed": False, "error": msg}, ensure_ascii=False))
    print(msg, file=sys.stderr)
    sys.exit(2)


def main():
    args = [a for a in sys.argv[1:] if a != "--json"]
    as_json = "--json" in sys.argv
    if not args:
        _usage_error("usage: claim_gate.py <manifest.json|-> [--json]", as_json)
    src = args[0]
    try:
        if src == "-":
            raw = sys.stdin.read()
        else:
            with open(src, encoding="utf-8") as fh:
                raw = fh.read()
        manifest = json.loads(raw)
    except (OSError, UnicodeDecodeError, ValueError) as e:
        _usage_error(f"cannot read manifest {src}: {e}", as_json)
    rep = verify(manifest)
    if as_json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    else:
        mark = "✅ PASS" if rep["passed"] else "🔴 FAIL"
        print(f"{mark}  {rep['task']}  ({rep['total']-rep['failed']}/{rep['total']} checks)")
        for r in rep["results"]:
            m = "  ✓" if r["ok"] else "  ✗"
            lbl = f"[{r['label']}] " if r.get("label") else ""
            print(f"{m} {lbl}{r['type']}: {r['detail']}")
    sys.exit(0 if rep["passed"] else 1)


if __name__ == "__main__":
    main()
