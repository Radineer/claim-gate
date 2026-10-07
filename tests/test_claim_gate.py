"""Real work passes, fabricated claims fail, and the gate never writes."""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import claim_gate  # noqa: E402

GATE = os.path.join(os.path.dirname(HERE), "claim_gate.py")


class ClaimGateTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.src = os.path.join(self.d, "scheduler.py")
        with open(self.src, "w") as f:
            f.write("def as_naive(dt):\n    return dt.replace(tzinfo=None)\n" + "#" * 2000)
        self.out = os.path.join(self.d, "report-2026.csv")
        with open(self.out, "w") as f:
            f.write("id,value\n" + "1,2\n" * 50)
        self.empty = os.path.join(self.d, "empty-2026.csv")
        open(self.empty, "w").close()
        self.old = os.path.join(self.d, "old-2026.csv")
        with open(self.old, "w") as f:
            f.write("x" * 200)
        week_ago = time.time() - 7 * 86400
        os.utime(self.old, (week_ago, week_ago))
        self.log = os.path.join(self.d, "job.log")
        with open(self.log, "w") as f:
            f.write("start\ninserted=0\ndone\n")
        self.db = os.path.join(self.d, "app.db")
        con = sqlite3.connect(self.db)
        con.execute("CREATE TABLE jobs(id INTEGER, status TEXT)")
        con.executemany("INSERT INTO jobs VALUES(?,?)", [(1, "done"), (2, "done"), (3, "failed")])
        con.commit()
        con.close()

    def run_gate(self, checks):
        return claim_gate.verify({"task": "t", "checks": checks})

    def test_real_work_passes(self):
        rep = self.run_gate([
            {"type": "file_exists", "path": self.src, "min_size": 1000},
            {"type": "file_contains", "path": self.src, "substring": "as_naive"},
            {"type": "sqlite_scalar", "db": self.db, "query": "SELECT count(*) FROM jobs", "op": "==", "value": 3},
            {"type": "output_fresh", "path": os.path.join(self.d, "report-*.csv"), "max_age_hours": 1, "min_size": 100},
            {"type": "glob_count", "path": os.path.join(self.d, "*.csv"), "op": ">=", "value": 3},
            {"type": "file_absent", "path": os.path.join(self.d, "should-not-exist.txt")},
        ])
        self.assertTrue(rep["passed"], rep)

    def test_each_fabricated_claim_fails(self):
        fabricated = {
            "file never created": {"type": "file_exists", "path": os.path.join(self.d, "new_module.py")},
            "change never made": {"type": "file_contains", "path": self.src, "substring": "retry_with_backoff"},
            "row count overstated": {"type": "sqlite_scalar", "db": self.db,
                                     "query": "SELECT count(*) FROM jobs WHERE status='failed'", "op": "==", "value": 0},
            "stale artifact": {"type": "output_fresh", "path": os.path.join(self.d, "old-*.csv"), "max_age_hours": 24},
            "empty artifact": {"type": "output_fresh", "path": os.path.join(self.d, "empty-*.csv"), "min_size": 1},
            "ran but did nothing": {"type": "log_not_contains", "path": self.log, "substring": ["inserted=0"]},
        }
        for name, check in fabricated.items():
            with self.subTest(name):
                self.assertFalse(self.run_gate([check])["passed"], name)

    def test_one_false_claim_fails_whole_manifest(self):
        rep = self.run_gate([
            {"type": "file_exists", "path": self.src},
            {"type": "file_exists", "path": os.path.join(self.d, "missing.py")},
        ])
        self.assertFalse(rep["passed"])
        self.assertEqual(rep["failed"], 1)

    def test_empty_manifest_does_not_pass(self):
        self.assertFalse(self.run_gate([])["passed"])

    def test_sql_is_read_only(self):
        rep = self.run_gate([{"type": "sqlite_scalar", "db": self.db,
                              "query": "UPDATE jobs SET status='done'", "op": "==", "value": 0}])
        self.assertFalse(rep["passed"])
        con = sqlite3.connect(self.db)
        failed = con.execute("SELECT count(*) FROM jobs WHERE status='failed'").fetchone()[0]
        con.close()
        self.assertEqual(failed, 1)

    def test_gate_connection_cannot_change_the_data(self):
        with open(self.db, "rb") as f:
            before = f.read()
        conn = claim_gate._ro_connect(self.db)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("INSERT INTO jobs VALUES (99, 'x')")
        finally:
            conn.close()
        self.run_gate([{"type": "sqlite_scalar", "db": self.db, "query": "SELECT count(*) FROM jobs", "op": ">=", "value": 0}])
        with open(self.db, "rb") as f:
            self.assertEqual(f.read(), before)

    def test_unreadable_manifest_exits_2_with_json(self):
        for src in (os.path.join(self.d, "no_such.json"),):
            r = subprocess.run([sys.executable, GATE, src, "--json"], capture_output=True, text=True)
            self.assertEqual(r.returncode, 2)
            self.assertFalse(json.loads(r.stdout)["passed"])
        bad = os.path.join(self.d, "broken.json")
        with open(bad, "w") as f:
            f.write("{not json")
        r = subprocess.run([sys.executable, GATE, bad, "--json"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)
        self.assertFalse(json.loads(r.stdout)["passed"])

    def test_unknown_check_type_fails(self):
        self.assertFalse(self.run_gate([{"type": "trust_me"}])["passed"])

    def test_no_http_response_never_passes(self):
        # Port 9 (discard) is closed on a normal machine: curl gets no response, code 000.
        r = self.run_gate([{"type": "http_status", "url": "http://127.0.0.1:9/", "op": "!=", "value": 200, "timeout": 3}])
        self.assertFalse(r["passed"])

    def test_forbidden_string_early_in_a_very_long_last_line_is_found(self):
        log = os.path.join(self.d, "long.log")
        with open(log, "w") as f:
            f.write("start\n" + "inserted=0 " + "x" * 2_000_000 + "\n")
        r = self.run_gate([{"type": "log_not_contains", "path": log, "substring": "inserted=0", "tail_lines": 1}])
        self.assertFalse(r["passed"])

    def test_zero_max_age_is_not_ignored(self):
        old = os.path.join(self.d, "old.txt")
        with open(old, "w") as f:
            f.write("x")
        t = time.time() - 3600
        os.utime(old, (t, t))
        r = self.run_gate([{"type": "glob_count", "path": old, "max_age_hours": 0, "op": ">=", "value": 1}])
        self.assertFalse(r["passed"])

    def test_output_from_the_future_is_not_fresh(self):
        fut = os.path.join(self.d, "future.csv")
        with open(fut, "w") as f:
            f.write("x" * 100)
        t = time.time() + 86400
        os.utime(fut, (t, t))
        r = self.run_gate([{"type": "output_fresh", "path": fut, "max_age_hours": 24}])
        self.assertFalse(r["passed"])

    def test_query_with_no_rows_or_null_never_passes(self):
        for q in ("SELECT 1 WHERE 0", "SELECT NULL"):
            r = self.run_gate([{"type": "sqlite_scalar", "db": self.db, "query": q, "op": "!=", "value": 0}])
            self.assertFalse(r["passed"], q)

    def test_sql_without_op_or_value_fails(self):
        r = self.run_gate([{"type": "sqlite_scalar", "db": self.db, "query": "SELECT count(*) FROM jobs"}])
        self.assertFalse(r["passed"])

    def test_url_globbing_is_off(self):
        b = self._fake_command("curl", 'for a in "$@"; do [ "$a" = "--globoff" ] && { printf 404; exit 0; }; done; printf 200200\n')
        r = self._with_path(b, [{"type": "http_status", "url": "http://example.invalid/[1-2]", "op": "!=", "value": 200}])
        self.assertTrue(r["passed"])  # with --globoff the fake answers one 404
        b = self._fake_command("curl", "printf 200200\n")
        r = self._with_path(b, [{"type": "http_status", "url": "http://example.invalid/x", "op": "!=", "value": 200}])
        self.assertFalse(r["passed"])  # two codes glued together are never one status

    def test_url_cannot_smuggle_curl_options(self):
        r = self.run_gate([{"type": "http_status", "url": "--config=/dev/null", "op": "!=", "value": 200}])
        self.assertFalse(r["passed"])

    def test_path_that_looks_like_an_option_is_not_a_change(self):
        repo = os.path.join(self.d, "repo")
        os.makedirs(repo)
        subprocess.run(["git", "init", "-q", repo], check=True)
        r = self.run_gate([{"type": "git_changed", "repo": repo, "path": "-h"}])
        self.assertFalse(r["passed"])
        with open(os.path.join(repo, "a.txt"), "w") as f:
            f.write("x")
        subprocess.run(["git", "-C", repo, "add", "a.txt"], check=True)
        subprocess.run(["git", "-C", repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                        "commit", "-qm", "c"], check=True)
        r = self.run_gate([{"type": "git_changed", "repo": repo, "path": ":(exclude)no_such_file"}])
        self.assertFalse(r["passed"])
        self.assertTrue(self.run_gate([{"type": "git_changed", "repo": repo, "path": "a.txt"}])["passed"])

    def test_dangling_symlink_is_not_absent(self):
        link = os.path.join(self.d, "legacy_link")
        os.symlink(os.path.join(self.d, "gone"), link)
        self.assertFalse(self.run_gate([{"type": "file_absent", "path": link}])["passed"])
        self.assertFalse(self.run_gate([{"type": "file_absent", "path": os.path.join(self.d, "legacy_*")}])["passed"])
        self.assertFalse(self.run_gate([{"type": "file_exists", "path": link}])["passed"])

    def test_unreadable_directory_is_not_absent(self):
        if os.geteuid() == 0:
            self.skipTest("root ignores permissions")
        locked = os.path.join(self.d, "locked")
        os.makedirs(locked)
        open(os.path.join(locked, "legacy.py"), "w").close()
        os.chmod(locked, 0)
        try:
            self.assertFalse(self.run_gate([{"type": "file_absent", "path": os.path.join(locked, "legacy.py")}])["passed"])
            self.assertFalse(self.run_gate([{"type": "file_absent", "path": os.path.join(locked, "*.py")}])["passed"])
            self.assertFalse(self.run_gate([{"type": "file_absent", "path": os.path.join(self.d, "*", "*.py")}])["passed"])
            inner = os.path.join(locked, "inner")
            link = os.path.join(self.d, "link_to_inner")
            os.symlink(inner, link)
            self.assertFalse(self.run_gate([{"type": "file_absent", "path": os.path.join(link, "*.py")}])["passed"])
        finally:
            os.chmod(locked, 0o755)

    def test_unreadable_directory_does_not_count_as_zero_files(self):
        if os.geteuid() == 0:
            self.skipTest("root ignores permissions")
        locked = os.path.join(self.d, "locked2")
        os.makedirs(locked)
        open(os.path.join(locked, "err.log"), "w").close()
        os.chmod(locked, 0)
        try:
            r = self.run_gate([{"type": "glob_count", "path": os.path.join(locked, "*.log"), "op": "==", "value": 0}])
            self.assertFalse(r["passed"])
            link = os.path.join(self.d, "count_link.log")
            os.symlink(os.path.join(locked, "err.log"), link)
            r = self.run_gate([{"type": "glob_count", "path": os.path.join(self.d, "count_*.log"), "op": "==", "value": 0}])
            self.assertFalse(r["passed"])
        finally:
            os.chmod(locked, 0o755)

    def test_unreadable_newer_output_is_not_skipped(self):
        if os.geteuid() == 0:
            self.skipTest("root ignores permissions")
        out = os.path.join(self.d, "outs")
        os.makedirs(out)
        with open(os.path.join(out, "report-old.csv"), "w") as f:
            f.write("x" * 100)
        locked = os.path.join(self.d, "locked3")
        os.makedirs(locked)
        open(os.path.join(locked, "report-new.csv"), "w").close()  # newest, but empty
        os.symlink(os.path.join(locked, "report-new.csv"), os.path.join(out, "report-new.csv"))
        os.chmod(locked, 0)
        try:
            for t in ("output_fresh", "file_exists"):
                r = self.run_gate([{"type": t, "path": os.path.join(out, "report-*.csv"), "min_size": 50}])
                self.assertFalse(r["passed"], t)
        finally:
            os.chmod(locked, 0o755)

    def test_db_path_with_hash_is_opened_read_only_as_is(self):
        db = os.path.join(self.d, "a#b.db")
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE t(x)")
        con.execute("INSERT INTO t VALUES (7)")
        con.commit()
        con.close()
        r = self.run_gate([{"type": "sqlite_scalar", "db": db, "query": "SELECT x FROM t", "op": "==", "value": 7}])
        self.assertTrue(r["passed"])
        self.assertFalse(os.path.exists(os.path.join(self.d, "a")))

    def test_nan_and_infinity_are_rejected(self):
        old = os.path.join(self.d, "old.csv")
        with open(old, "w") as f:
            f.write("x" * 100)
        t = time.time() - 30 * 86400
        os.utime(old, (t, t))
        for age in ("NaN", float("nan"), "inf"):
            self.assertFalse(self.run_gate([{"type": "output_fresh", "path": old, "max_age_hours": age}])["passed"], age)
        log = os.path.join(self.d, "bad.log")
        with open(log, "w") as f:
            f.write("inserted=0\n")
        for n in (0.5, 0, -1, "1.5", True):
            self.assertFalse(self.run_gate([{"type": "log_not_contains", "path": log, "substring": "inserted=0",
                                             "tail_lines": n}])["passed"], n)
        self.assertFalse(self.run_gate([{"type": "output_fresh", "path": old, "max_age_hours": -1}])["passed"])
        self.assertFalse(self.run_gate([{"type": "sqlite_scalar", "db": self.db, "query": "SELECT 1",
                                         "op": "!=", "value": float("nan")}])["passed"])

    def test_fractional_min_size_is_not_rounded_down(self):
        f100 = os.path.join(self.d, "f100.csv")
        with open(f100, "w") as f:
            f.write("x" * 100)
        for bad in (100.9, "1e-324", -1, "100.0"):
            self.assertFalse(self.run_gate([{"type": "output_fresh", "path": f100, "min_size": bad}])["passed"], bad)
        self.assertTrue(self.run_gate([{"type": "output_fresh", "path": f100, "min_size": 100}])["passed"])

    def test_malformed_manifest_fails_without_crashing(self):
        for m in ({"checks": [None]}, {"checks": "x"}, [], {"checks": [1, "a"]}, {"checks": [{"type": []}]},
                  {"checks": [{"type": "file_exists"}]}, {"checks": [{"type": "file_exists", "path": 3}]}):
            self.assertFalse(claim_gate.verify(m)["passed"], m)
        bad = os.path.join(self.d, "bad_shape.json")
        with open(bad, "w") as f:
            json.dump({"checks": [None]}, f)
        r = subprocess.run([sys.executable, GATE, bad, "--json"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 1)
        self.assertFalse(json.loads(r.stdout)["passed"])

    def test_mtime_after_is_strict(self):
        import datetime
        f = os.path.join(self.d, "stamped.txt")
        open(f, "w").close()
        thr = datetime.datetime(2026, 1, 1, 0, 0, 0)
        ts = thr.timestamp()
        os.utime(f, (ts, ts))
        self.assertFalse(self.run_gate([{"type": "file_exists", "path": f, "mtime_after": thr.isoformat()}])["passed"])
        os.utime(f, (ts + 1, ts + 1))
        self.assertTrue(self.run_gate([{"type": "file_exists", "path": f, "mtime_after": thr.isoformat()}])["passed"])

    def test_multiline_forbidden_string_is_found(self):
        log = os.path.join(self.d, "ml.log")
        with open(log, "w") as f:
            f.write("start\ninserted=0\ndone\n")
        r = self.run_gate([{"type": "log_not_contains", "path": log, "substring": "inserted=0\ndone"}])
        self.assertFalse(r["passed"])

    def test_trailing_slash_means_directory(self):
        self.assertFalse(self.run_gate([{"type": "file_exists", "path": self.src + "/"}])["passed"])
        self.assertTrue(self.run_gate([{"type": "file_absent", "path": self.src + "/"}])["passed"])

    def test_substring_and_regex_together_is_rejected(self):
        r = self.run_gate([{"type": "file_contains", "path": self.src, "substring": "nope", "regex": "."}])
        self.assertFalse(r["passed"])
        self.assertIn("not both", r["results"][0]["detail"])

    def _fake_command(self, name, script):
        bindir = os.path.join(self.d, "bin")
        os.makedirs(bindir, exist_ok=True)
        path = os.path.join(bindir, name)
        with open(path, "w") as f:
            f.write("#!/bin/sh\n" + script)
        os.chmod(path, 0o755)
        return bindir

    def _with_path(self, bindir, checks):
        old = os.environ["PATH"]
        os.environ["PATH"] = bindir + os.pathsep + old
        try:
            return self.run_gate(checks)
        finally:
            os.environ["PATH"] = old

    def test_pm2_all_same_name_processes_must_match(self):
        jl = '[{"name":"api","pm2_env":{"status":"stopped"}},{"name":"api","pm2_env":{"status":"online"}}]'
        b = self._fake_command("pm2", "echo '%s'\n" % jl)
        self.assertFalse(self._with_path(b, [{"type": "pm2_status", "name": "api"}])["passed"])
        b = self._fake_command("pm2", "echo '[{\"name\":\"api\",\"pm2_env\":{\"status\":\"online\"}}]'; exit 3\n")
        self.assertFalse(self._with_path(b, [{"type": "pm2_status", "name": "api"}])["passed"])

    def test_failing_pgrep_is_not_a_match(self):
        b = self._fake_command("pgrep", "echo 123; exit 2\n")
        self.assertFalse(self._with_path(b, [{"type": "process_running", "pattern": "anything"}])["passed"])

    def test_cli_exit_codes(self):
        good = os.path.join(self.d, "good.json")
        bad = os.path.join(self.d, "bad.json")
        with open(good, "w") as f:
            json.dump({"task": "g", "checks": [{"type": "file_exists", "path": self.src}]}, f)
        with open(bad, "w") as f:
            json.dump({"task": "b", "checks": [{"type": "file_exists", "path": self.src + ".nope"}]}, f)
        self.assertEqual(subprocess.run([sys.executable, GATE, good], capture_output=True).returncode, 0)
        self.assertEqual(subprocess.run([sys.executable, GATE, bad], capture_output=True).returncode, 1)
        r = subprocess.run([sys.executable, GATE, bad, "--json"], capture_output=True, text=True)
        self.assertFalse(json.loads(r.stdout)["passed"])


if __name__ == "__main__":
    unittest.main()
