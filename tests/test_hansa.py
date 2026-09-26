"""Tests for hansa with a stand-in for claude, so no model is called."""
import contextlib
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hansa import cost, views  # noqa: E402
from hansa.cli import init_desk, main  # noqa: E402
from hansa.desk import Desk, public_settings, validate  # noqa: E402
from hansa.run import Dispatcher, provider_problem, tmux_bin, tmux_socket, waiting_reason  # noqa: E402
from hansa.web import start_web  # noqa: E402

FAKE = r'''#!/usr/bin/env python3
import json, os, subprocess, sys, time
prompt = sys.stdin.read() if "-p" in sys.argv else sys.argv[1]
if "-p" not in sys.argv:  # a design session, in a terminal the principal joins
    if os.environ.get("FAKE_SHOW_ARGV"):
        open(os.environ["FAKE_SHOW_ARGV"], "a").write(json.dumps(sys.argv) + "\n")
    sid = sys.argv[sys.argv.index("--session-id") + 1]
    rec = os.path.join(os.environ["CLAUDE_CONFIG_DIR"], "projects", "-design", sid + ".jsonl")
    os.makedirs(os.path.dirname(rec), exist_ok=True)
    open("badge.txt", "w").write("bigger\n")
    with open(rec, "w") as f:
        for i, t in enumerate(("Here is how it looks now.", "Hand-in: the badge is bigger; the checks pass.")):
            f.write(json.dumps({"type": "assistant", "message": {"id": f"d{i}", "model": "claude-x", "content": [
                {"type": "text", "text": t}], "usage": {"input_tokens": 100, "output_tokens": 20}}}) + "\n")
    time.sleep(float(os.environ.get("FAKE_DESIGN_SECONDS", "0")))
    sys.exit(0)
compat = bool(os.environ.get("ANTHROPIC_BASE_URL"))
if compat and os.environ.get("FAKE_BADKEY_ON_COMPAT"):
    print(json.dumps({"type": "result", "subtype": "success", "is_error": True, "api_error_status": 401,
                      "result": "API Error: 401 authentication_error", "session_id": "s-key", "num_turns": 1}))
    sys.exit(1)
if compat and os.environ.get("FAKE_NOLOGIN_ON_COMPAT"):
    print(json.dumps({"type": "result", "subtype": "success", "is_error": True, "api_error_status": None,
                      "terminal_reason": "api_error", "num_turns": 1,
                      "result": "Not logged in · Please run /login"}))
    sys.exit(1)
if compat and os.environ.get("FAKE_QUOTA_ON_COMPAT"):
    print(json.dumps({"type": "result", "subtype": "success", "is_error": True, "api_error_status": 429,
                      "result": "API Error: 429 rate_limit_error", "session_id": "s-quota", "num_turns": 1}))
    sys.exit(1)
if compat and os.environ.get("FAKE_REFUSE_ON_COMPAT"):
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "stop_reason": "content_filter",
                      "result": "The request was rejected because it was considered high risk", "num_turns": 1}))
    sys.exit(0)
if os.environ.get("FAKE_STOP_AT_TURNS"):
    sid = sys.argv[sys.argv.index("--session-id") + 1]
    rec = os.path.join(os.environ["CLAUDE_CONFIG_DIR"], "projects", "-work", sid + ".jsonl")
    os.makedirs(os.path.dirname(rec), exist_ok=True)
    with open(rec, "w") as f:
        for t in ("Opening the list page.", "Found: the week badge is still in English."):
            f.write(json.dumps({"type": "assistant", "message": {"id": t, "model": "mimo-x", "content": [
                {"type": "text", "text": t}], "usage": {"input_tokens": 700, "cache_read_input_tokens": 3000}}}) + "\n")
    print(json.dumps({"type": "result", "subtype": "error_max_turns", "is_error": True, "num_turns": 9,
                      "session_id": sid}))
    sys.exit(1)
if os.environ.get("FAKE_NO_CHANGES"):
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "Nothing to do.",
                      "num_turns": 1}))
    sys.exit(0)
if "single word OK" in prompt:
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "OK",
                      "modelUsage": {"fake-model": {"inputTokens": 1, "outputTokens": 1}}}))
    sys.exit(0)
if os.environ.get("FAKE_SAY_KEY"):  # an endpoint's error that repeats the key
    print("endpoint said: bad token " + os.environ.get("ANTHROPIC_AUTH_TOKEN", ""), file=sys.stderr, flush=True)
    time.sleep(float(os.environ["FAKE_SAY_KEY"]))
open("hello.txt", "w").write("hello from " + os.environ.get("HANSA_TASK", "?") + "\n")
if os.environ.get("FAKE_WRITE_BINARY"):
    open("blob.bin", "wb").write(b"\x00\x01PK" + os.environ["FAKE_WRITE_BINARY"].encode() + b"\x00\xff")
if os.environ.get("FAKE_LEAVE_CHILD"):  # something it started and left running, in its process group
    c = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    open(os.environ["FAKE_LEAVE_CHILD"], "w").write(str(c.pid))
if os.environ.get("FAKE_WRITE"):
    open("notes.txt", "w").write(os.environ["FAKE_WRITE"] + "\n")
if os.environ.get("FAKE_SHOW_ARGV"):
    open(os.environ["FAKE_SHOW_ARGV"], "a").write(json.dumps(sys.argv) + "\n")
print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "num_turns": 3,
    "result": "Added hello.txt. Checked it exists." + (" Notes: " + os.environ["FAKE_WRITE"] if os.environ.get("FAKE_WRITE") else "") + ("\nran:\n" * 70 + "VERDICT: " + os.environ["FAKE_VERDICT"]
                                                      if os.environ.get("FAKE_VERDICT") else ""),
    "session_id": "s-ok",
    "permission_denials": [{"tool_name": "Bash", "tool_input": {"command": "env JAVA_HOME=/x mvn test"}}],
    "modelUsage": {"claude-haiku-4-5-20251001": {"inputTokens": 1000, "outputTokens": 200,
        "cacheReadInputTokens": 5000, "cacheCreationInputTokens": 100, "costUSD": 0.0031, "costBasis": "list"}}}))
'''


def sh(*args, cwd):
    subprocess.run(args, cwd=cwd, check=True, capture_output=True)


class DeskTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        sh("git", "init", "-q", "-b", "main", cwd=self.repo)
        (self.repo / "README.md").write_text("demo\n")
        sh("git", "add", ".", cwd=self.repo)
        sh("git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init", cwd=self.repo)
        fake = self.tmp / "claude"
        fake.write_text(FAKE)
        fake.chmod(0o755)
        os.environ["HANSA_CLAUDE"] = str(fake)
        self.desk = init_desk(self.tmp / "desk", str(self.repo), "a")

    def settle(self, d, tid, timeout=20):
        end = time.time() + timeout
        while time.time() < end:
            d.tick()
            if self.desk.meta(tid)["state"] not in ("queued", "running"):
                return self.desk.meta(tid)
            time.sleep(0.1)
        self.fail(f"{tid} did not finish: {self.desk.meta(tid)}")

    def test_task_runs_is_recorded_and_merges(self):
        tid = self.desk.new_task("Add hello.txt")
        m = self.settle(Dispatcher(self.desk), tid)
        self.assertEqual(m["state"], "done", m)
        self.assertEqual(m["files"], ["hello.txt"])
        self.assertGreaterEqual(m["commits"], 1)  # left uncommitted by the worker, committed by hansa
        self.assertEqual(m["denials"], ["Bash: env JAVA_HOME=/x mvn test"])
        self.assertIn("hello.txt", views.show_text(self.desk, tid))
        self.assertEqual(main(["--desk", str(self.desk.root), "merge", tid]), 0)
        self.assertEqual(self.desk.meta(tid)["state"], "merged")
        self.assertTrue((self.repo / "hello.txt").exists())

    def test_hansa_commits_for_the_worker_and_keeps_git_rules_from_it(self):
        seen = self.tmp / "argv.jsonl"
        s = self.desk.settings()
        s["lanes"]["a"]["allow"] = ["Bash(git commit:*)", "Bash(pnpm test:*)"]  # saved before hansa took over git
        s["lanes"]["a"]["env"] = {"FAKE_SHOW_ARGV": str(seen)}
        self.desk.settings_path.write_text(json.dumps(s))
        self.assertTrue(any("git is handled by hansa" in p for p in validate(s)))  # the page refuses new ones
        tid = self.desk.new_task("Add hello.txt", title="Add a greeting")
        m = self.settle(Dispatcher(self.desk), tid)
        argv = json.loads(seen.read_text().splitlines()[-1])
        self.assertNotIn("Bash(git commit:*)", argv)
        self.assertIn("Bash(pnpm test:*)", argv)
        self.assertIn("hansa commits", argv[argv.index("--append-system-prompt") + 1])
        msg = subprocess.run(["git", "log", "-1", "--format=%s", m["head"]], cwd=self.repo,
                             capture_output=True, text=True).stdout.strip()
        self.assertEqual(msg, f"{tid}: Add a greeting")
        self.assertIn("ignored", views.explain_text(self.desk, "lane", "a"))

    def test_a_change_holding_a_stored_key_is_never_merged_and_the_key_is_never_shown(self):
        key = "sk-test-0123456789"
        self.desk.write_key("mimo", key)
        s = self.desk.settings()
        s["lanes"]["a"]["env"] = {"FAKE_WRITE": key}
        s["lanes"]["a"]["check"] = "cat notes.txt"  # a check that prints what the change holds
        self.assertEqual(self.desk.save_settings(s), [])
        tid = self.desk.new_task("Add hello.txt")
        m = self.settle(Dispatcher(self.desk), tid)
        self.assertEqual((m["state"], m["leaked"]), ("done", ["mimo"]), m)
        shown = views.show_text(self.desk, tid)
        self.assertIn("key of mimo", shown)
        self.assertNotIn(key, shown)
        self.assertNotIn(key, views.diff_text(self.desk, tid))
        self.assertIn("key of mimo", views.diff_text(self.desk, tid))
        self.assertNotIn(key, json.dumps(m["check"]))
        self.assertNotIn(key, (self.desk.tasks_dir / tid / "check.log").read_text())
        self.assertNotIn(key, (self.desk.tasks_dir / tid / "result.md").read_text())
        self.assertNotIn(key, self.desk.events_path.read_text())
        self.assertIn("key", views.status_text(views.status(self.desk)))
        with self.assertRaises(SystemExit):
            main(["--desk", str(self.desk.root), "merge", tid, "--despite-check"])
        self.assertFalse((self.repo / "notes.txt").exists())
        self.desk.event("anything", detail=f"the endpoint said: bad key {key}")
        self.assertNotIn(key, self.desk.events_path.read_text())

    def test_a_key_too_short_to_look_for_is_refused(self):
        with self.assertRaises(ValueError):
            self.desk.write_key("mimo", "sk-1234")
        self.assertFalse(self.desk.has_key("mimo"))

    def test_a_key_inside_a_binary_file_is_found_too(self):
        key = "sk-test-binary-0456"
        self.desk.write_key("mimo", key)
        s = self.desk.settings()
        s["lanes"]["a"]["env"] = {"FAKE_WRITE_BINARY": key}
        self.assertEqual(self.desk.save_settings(s), [])
        tid = self.desk.new_task("Add hello.txt")
        m = self.settle(Dispatcher(self.desk), tid)
        self.assertEqual((m["state"], m["leaked"]), ("done", ["mimo"]), m)

    def test_merge_looks_for_keys_again_when_it_merges(self):
        later = "sk-saved-later-0789"
        s = self.desk.settings()
        s["lanes"]["a"]["env"] = {"FAKE_WRITE": later}
        self.assertEqual(self.desk.save_settings(s), [])
        tid = self.desk.new_task("Add hello.txt")
        m = self.settle(Dispatcher(self.desk), tid)
        self.assertEqual((m["state"], m["leaked"]), ("done", []), m)
        self.desk.write_key("mimo", later)  # that text became a key after the task ended
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
            main(["--desk", str(self.desk.root), "merge", tid])
        self.assertFalse((self.repo / "notes.txt").exists())

    def test_what_a_worker_prints_stays_out_of_the_task_folder_until_hansa_removes_keys(self):
        key = "sk-test-printed-0321"
        s = self.desk.settings()
        s["providers"]["mimo"] = {"kind": "compatible", "model": "mimo-x", "effort": "", "base_url": "https://example.invalid"}
        s["lanes"]["a"]["providers"] = ["mimo"]
        s["lanes"]["a"]["env"] = {"FAKE_SAY_KEY": "2"}
        self.assertEqual(self.desk.save_settings(s), [])
        self.desk.write_key("mimo", key)
        tid = self.desk.new_task("Add hello.txt")
        d = Dispatcher(self.desk)
        d.tick()
        raw = self.desk.state_dir / "runs" / tid / "run.err"
        end = time.time() + 10
        while key not in (raw.read_text() if raw.exists() else "") and time.time() < end:
            time.sleep(0.1)
        self.assertIn(key, raw.read_text())  # raw output goes where no agent may read
        folder = self.desk.tasks_dir / tid
        self.assertFalse([f.name for f in folder.iterdir() if f.is_file() and key in f.read_text(errors="replace")])
        self.settle(d, tid)
        self.assertIn("key of mimo", (folder / "run.err").read_text())
        self.assertFalse([f.name for f in folder.iterdir() if f.is_file() and key in f.read_text(errors="replace")])
        self.assertFalse(raw.exists())  # no raw copy is kept once it is published

    def test_what_a_failing_setup_leaves_is_not_part_of_the_task(self):
        s = self.desk.settings()
        s["lanes"]["a"]["setup"] = "echo partial > config.txt; exit 1"
        self.assertEqual(self.desk.save_settings(s), [])
        tid = self.desk.new_task("Add hello.txt")
        m = self.settle(Dispatcher(self.desk), tid)
        self.assertEqual((m["state"], m["commits"], m["files"]), ("failed", 0, []), m)

    def test_a_worker_started_just_before_the_server_stopped_is_taken_over(self):
        token = "hansa-launch-test-5b9f"
        orphan = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", token], start_new_session=True)
        self.addCleanup(lambda: orphan.poll() is None and orphan.kill())
        tid = self.desk.new_task("Add hello.txt")
        self.desk.update(tid, state="running", phase="starting", lane="a", provider="claude", started=time.time(),
                         pid=None, launch=token)
        Dispatcher(self.desk).tick()
        m = self.desk.meta(tid)
        self.assertEqual((m["state"], m["phase"], m["pid"]), ("running", "", orphan.pid), m)
        self.assertIsNone(orphan.poll())  # still working, not stopped and started over

    def test_a_check_an_earlier_server_finished_is_not_run_again(self):
        marker = self.tmp / "checked"
        s = self.desk.settings()
        s["lanes"]["a"]["check"] = f"touch {marker}"
        self.assertEqual(self.desk.save_settings(s), [])
        tid = self.desk.new_task("Add hello.txt")
        self.desk.update(tid, state="running", phase="checking", lane="a", provider="claude", started=time.time(),
                         pid=None, launch="hansa-check-t0001-gone", worktree=str(self.repo), check_command=f"touch {marker}")
        raw = self.desk.raw_dir(tid)
        (raw / "check.exit").write_text("0\n")
        (raw / "check.log").write_text("all passed\n")
        m = self.settle(Dispatcher(self.desk), tid)
        self.assertEqual((m["state"], m["check"]["ok"], m["check"]["tail"]), ("done", True, ["all passed"]), m)
        self.assertFalse(marker.exists())

    def test_a_key_saved_later_is_removed_from_what_was_already_published(self):
        later = "sk-saved-later-0654"
        s = self.desk.settings()
        s["lanes"]["a"]["env"] = {"FAKE_WRITE": later}
        self.assertEqual(self.desk.save_settings(s), [])
        tid = self.desk.new_task("Add hello.txt")
        self.settle(Dispatcher(self.desk), tid)
        report = self.desk.tasks_dir / tid / "result.md"
        self.assertIn(later, report.read_text())  # not a key yet
        self.desk.write_key("mimo", later)
        self.assertNotIn(later, report.read_text())
        self.assertIn("[key of mimo]", report.read_text())
        self.assertNotIn(later, views.show_text(self.desk, tid))

    def test_a_key_that_only_a_merge_commit_brings_is_found(self):
        from hansa.run import keys_in_change
        key = "sk-test-in-merge-0987"
        self.desk.write_key("mimo", key)
        g = lambda *a: subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=self.repo,
                                      check=True, capture_output=True, text=True).stdout.strip()
        base = g("rev-parse", "HEAD")
        g("checkout", "-q", "-b", "side")
        (self.repo / "side.txt").write_text("side\n")
        g("add", ".")
        g("commit", "-q", "-m", "side")
        g("checkout", "-q", "main")
        g("merge", "--no-ff", "--no-commit", "side")
        (self.repo / "side.txt").write_text(key + "\n")  # changed in the merge itself
        g("add", ".")
        g("commit", "-q", "-m", "merge side")  # no other commit shows that line
        self.assertEqual(keys_in_change(self.desk, self.repo, base, g("rev-parse", "HEAD")), ["mimo"])

    def test_a_task_cancelled_before_its_check_starts_is_not_checked(self):
        marker = self.tmp / "checked"
        s = self.desk.settings()
        s["lanes"]["a"]["check"] = f"touch {marker}"
        self.assertEqual(self.desk.save_settings(s), [])
        tid = self.desk.new_task("Add hello.txt")
        self.desk.update(tid, state="running", phase="checking", lane="a", provider="claude", started=time.time(),
                         pid=None, cancel=True, worktree=str(self.repo))
        m = self.settle(Dispatcher(self.desk), tid)
        self.assertEqual(m["state"], "cancelled", m)
        started = [e for e in map(json.loads, self.desk.events_path.read_text().splitlines())
                   if e["event"] == "check_started" and e.get("task") == tid]
        self.assertEqual(started, [])  # not started and then stopped: never started
        self.assertFalse(marker.exists())

    def test_what_a_worker_leaves_running_ends_with_it(self):
        from hansa.run import alive
        pidfile = self.tmp / "child.pid"
        s = self.desk.settings()
        s["lanes"]["a"]["env"] = {"FAKE_LEAVE_CHILD": str(pidfile)}
        self.assertEqual(self.desk.save_settings(s), [])
        tid = self.desk.new_task("Add hello.txt")
        self.assertEqual(self.settle(Dispatcher(self.desk), tid)["state"], "done")
        child = int(pidfile.read_text())
        self.addCleanup(lambda: alive(child) and os.kill(child, 9))
        end = time.time() + 10
        while alive(child) and time.time() < end:
            time.sleep(0.1)
        self.assertFalse(alive(child))

    def test_git_behind_a_wrapper_is_still_git(self):
        from hansa.run import lane_allow
        s = self.desk.settings()
        s["lanes"]["a"]["allow"] = ["Bash(env git commit:*)", "Bash(GIT_DIR=x git log:*)", "Bash(command git push:*)",
                                    "Bash(/usr/bin/git show:*)", "Bash(sudo -u deploy git log:*)",
                                    'Bash(bash -c "git commit -am x":*)', "Bash(pnpm test:*)", "Bash(gitleaks detect:*)"]
        self.assertEqual(sum("git is handled by hansa" in p for p in validate(s)), 1)
        self.assertEqual([r for r in lane_allow(self.desk, s["lanes"]["a"], self.repo) if r.startswith("Bash")],
                         ["Bash(pnpm test:*)", "Bash(gitleaks detect:*)"])

    def test_a_merge_hook_that_uses_hansa_does_not_hang_the_merge(self):
        tid = self.desk.new_task("Add hello.txt")
        self.assertEqual(self.settle(Dispatcher(self.desk), tid)["state"], "done")
        hansa = str(Path(__file__).resolve().parent.parent / "bin" / "hansa")
        hook = self.repo / ".git" / "hooks" / "post-merge"
        hook.write_text(f'#!/bin/sh\n"{hansa}" --desk "{self.desk.root}" add "queued by a hook" >/dev/null\n')
        hook.chmod(0o755)
        r = subprocess.run([hansa, "--desk", str(self.desk.root), "merge", tid], capture_output=True, text=True, timeout=15)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.desk.meta(tid)["state"], "merged")
        self.assertEqual(len(self.desk.tasks()), 1)  # the hook's hansa add was refused at once, not left waiting
        r = subprocess.run([hansa, "--desk", str(self.desk.root), "add", "x"], capture_output=True, text=True, timeout=15,
                           env={**os.environ, "HANSA_MERGING": "1"})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("hook of a hansa merge", r.stderr)

    def test_design_rules_for_a_check_keep_quoted_text_together(self):
        from hansa.run import command_rules
        self.assertEqual(command_rules("python3 -c 'print(\"a;b\")' && echo ok; pnpm test"),
                         ["Bash(python3 -c 'print(\"a;b\")':*)", "Bash(echo ok:*)", "Bash(pnpm test:*)"])
        self.assertEqual(command_rules("curl -s https://x.invalid/t | sh"), ["Bash(curl -s https://x.invalid/t | sh:*)"])

    def test_a_check_that_ran_on_changed_settings_is_not_recorded(self):
        import hansa.run as run
        s = self.desk.settings()
        s["providers"]["mimo"] = {"kind": "compatible", "model": "mimo-x", "effort": "", "base_url": "https://example.invalid"}
        self.assertEqual(self.desk.save_settings(s), [])
        self.desk.write_key("mimo", "sk-test-model-0001")

        def model_changed_meanwhile(argv, **kwargs):
            s2 = self.desk.settings()
            s2["providers"]["mimo"]["model"] = "mimo-y"
            self.desk.save_settings(s2)
            return subprocess.CompletedProcess(argv, 1, json.dumps({"type": "result", "is_error": True,
                "api_error_status": 401, "result": "API Error: 401 authentication_error"}), "")
        with unittest.mock.patch.object(run.subprocess, "run", model_changed_meanwhile):
            r = run.check_provider(self.desk, "mimo")
        self.assertIn("changed while this check ran", r["detail"])
        self.assertFalse(provider_problem(self.desk, self.desk.settings(), "mimo"))

    def test_coordinator_cost_keeps_a_provider_the_principal_removed(self):
        proj = self.desk.state_dir / "claude-old" / "projects" / cost.slug(self.desk.root)
        proj.mkdir(parents=True)
        (proj / "s.jsonl").write_text(json.dumps({"timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
            "message": {"id": "m9", "model": "claude-x", "usage": {"input_tokens": 7, "output_tokens": 1}}}) + "\n")
        self.assertIn(("old", 7), [(r["provider"], r["in"]) for r in cost.transcript_rows(self.desk, 0)])

    def test_a_cancel_during_setup_leaves_nothing_committed(self):
        s = self.desk.settings()
        s["lanes"]["a"]["setup"] = "echo 'lock v2' >> README.md; sleep 5"
        self.assertEqual(self.desk.save_settings(s), [])
        tid = self.desk.new_task("Add hello.txt")
        (self.desk.raw_dir(tid) / "setup.log.done").touch()  # an earlier try's, left by a server that stopped
        d = Dispatcher(self.desk)
        d.tick()
        readme = self.desk.worktrees_dir / "a" / "README.md"
        end = time.time() + 10
        while "lock v2" not in readme.read_text() and time.time() < end:
            time.sleep(0.1)
        with contextlib.redirect_stdout(io.StringIO()):
            main(["--desk", str(self.desk.root), "cancel", tid])
        m = self.settle(d, tid)
        self.assertEqual((m["state"], m["commits"], m["files"]), ("cancelled", 0, []), m)

    def test_keys_are_kept_out_of_task_texts_and_requests(self):
        key = "sk-test-in-text-0111"
        self.desk.write_key("mimo", key)
        tid = self.desk.new_task(f"Use {key} to call the endpoint", title=f"call with {key}")
        n = self.desk.add_ask(f"Is {key} still valid?")
        self.assertNotIn(key, (self.desk.tasks_dir / tid / "task.md").read_text())
        self.assertNotIn(key, (self.desk.tasks_dir / tid / "meta.json").read_text())
        self.assertNotIn(key, self.desk.asks_path.read_text())
        later = "sk-saved-later-0222"
        tid2 = self.desk.new_task(f"Mention {later} in the notes")
        self.desk.close_ask(n, f"use {later} instead")
        self.desk.write_key("mimo", later)  # saved after it was written down
        self.assertNotIn(later, (self.desk.tasks_dir / tid2 / "task.md").read_text())
        self.assertNotIn(later, self.desk.asks_path.read_text())
        self.assertIn("[key of mimo]", (self.desk.tasks_dir / tid2 / "task.md").read_text())

    def test_a_second_server_on_the_same_desk_refuses_to_start(self):
        from hansa.cli import hold_server_lock
        held = hold_server_lock(self.desk)
        self.addCleanup(held.close)
        r = subprocess.run([str(Path(__file__).resolve().parent.parent / "bin" / "hansa"), "--desk", str(self.desk.root),
                            "serve", "--port", "0"], capture_output=True, text=True, timeout=20)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("another hansa serve", r.stdout + r.stderr)

    def test_the_web_command_ends_when_its_connection_does(self):
        # run over ssh to keep a tunnel to the page open; a closed window or a laptop gone to sleep
        # must not leave it running on the server
        env = {k: v for k, v in os.environ.items() if k not in ("HANSA_ROLE", "HANSA_TASK")}
        p = subprocess.Popen([str(Path(__file__).resolve().parent.parent / "bin" / "hansa"), "--desk", str(self.desk.root),
                              "web"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
        self.addCleanup(lambda: p.poll() is None and p.kill())
        self.addCleanup(p.stdout.close)
        self.assertIn("/?t=", p.stdout.readline().decode())
        p.stdin.close()  # what sshd does when the connection closes
        try:
            code = p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.fail("hansa web kept running after its connection closed")
        self.assertEqual(code, 0)

    def test_a_task_cancelled_while_hansa_prepares_it_never_runs(self):
        import hansa.run as run
        seen = self.tmp / "argv.jsonl"
        s = self.desk.settings()
        s["lanes"]["a"]["env"] = {"FAKE_SHOW_ARGV": str(seen)}
        self.assertEqual(self.desk.save_settings(s), [])
        tid = self.desk.new_task("Add hello.txt")
        real = run.prepare

        def cancelled_meanwhile(desk, *args, **kwargs):
            with contextlib.redirect_stdout(io.StringIO()):
                main(["--desk", str(desk.root), "cancel", tid])
            return real(desk, *args, **kwargs)
        with unittest.mock.patch.object(run, "prepare", cancelled_meanwhile):
            Dispatcher(self.desk).tick()
        self.assertEqual(self.desk.meta(tid)["state"], "cancelled")
        self.assertFalse(seen.exists())  # no worker was started

    def test_a_task_left_half_started_by_a_stopped_server_is_queued_again(self):
        tid = self.desk.new_task("Add hello.txt")
        self.desk.update(tid, state="running", phase="starting", lane="a", provider="claude", started=time.time(),
                         pid=None)
        m = self.settle(Dispatcher(self.desk), tid)
        self.assertEqual((m["state"], m["files"]), ("done", ["hello.txt"]), m)

    def test_hansa_only_stops_the_process_it_started_and_all_of_its_group(self):
        from hansa.run import alive, proc_start, stop
        leader = subprocess.Popen([sys.executable, "-c",
            "import subprocess, sys, time\n"
            "c = subprocess.Popen([sys.executable, '-c', 'import signal, time; "
            "signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)'])\n"
            "print(c.pid, flush=True); time.sleep(60)"], stdout=subprocess.PIPE, text=True, start_new_session=True)
        child = int(leader.stdout.readline())
        self.addCleanup(lambda: [os.kill(p, 9) for p in (child, leader.pid) if alive(p)])
        started = proc_start(leader.pid)
        self.assertTrue(started)
        self.assertFalse(alive(leader.pid, "not-this-process"))  # a pid reused by another process is not ours
        stop(leader.pid, "not-this-process")
        self.assertIsNone(leader.poll())
        stop(leader.pid, started)
        leader.wait(timeout=20)
        end = time.time() + 10
        while alive(child) and time.time() < end:
            time.sleep(0.1)
        self.assertFalse(alive(child))  # it ignored the polite signal, so the group was killed

    def test_the_lane_setup_runs_before_every_task_without_provider_keys(self):
        trace = self.tmp / "setup-trace.txt"
        s = self.desk.settings()
        s["providers"]["mimo"] = {"kind": "compatible", "model": "mimo-x", "effort": "", "base_url": "https://example.invalid"}
        s["lanes"]["a"]["providers"] = ["mimo"]
        s["lanes"]["a"]["setup"] = f'echo installing; echo "token=${{ANTHROPIC_AUTH_TOKEN:-none}}" >> "{trace}"'
        self.assertEqual(self.desk.save_settings(s), [])
        self.desk.write_key("mimo", "sk-test-setup-0001")
        d = Dispatcher(self.desk)
        first = self.desk.new_task("Add hello.txt")
        self.assertEqual(self.settle(d, first)["state"], "done")
        second = self.desk.new_task("Add hello.txt again")
        self.assertEqual(self.settle(d, second)["state"], "done")
        self.assertEqual(trace.read_text().splitlines(), ["token=none", "token=none"])  # the same worktree, again
        self.assertIn("installing", (self.desk.tasks_dir / second / "setup.log").read_text())

    def test_what_the_setup_changes_in_the_repository_is_not_part_of_the_task(self):
        s = self.desk.settings()
        s["lanes"]["a"]["setup"] = "echo generated > made-by-setup.txt; echo 'lock v2' >> README.md"
        self.assertEqual(self.desk.save_settings(s), [])
        tid = self.desk.new_task("Add hello.txt")
        m = self.settle(Dispatcher(self.desk), tid)
        self.assertEqual((m["state"], m["files"]), ("done", ["hello.txt"]), m)
        stashes = subprocess.run(["git", "stash", "list"], cwd=m["worktree"], capture_output=True, text=True).stdout
        self.assertIn("hansa: what the lane's setup changed", stashes)
        self.assertIn("set aside", (self.desk.tasks_dir / tid / "setup.log").read_text())

    def test_a_failing_setup_fails_the_task_with_its_output_and_never_holds_up_the_server(self):
        s = self.desk.settings()
        s["lanes"]["a"]["setup"] = "sleep 2; echo 'registry: API Error: 429 rate_limit_error'; exit 3"
        self.assertEqual(self.desk.save_settings(s), [])
        tid = self.desk.new_task("Add hello.txt")
        d = Dispatcher(self.desk)
        t0 = time.time()
        d.tick()
        self.assertLess(time.time() - t0, 1.5)  # the setup runs in the worker's own process, not in hansa serve
        m = self.settle(d, tid)
        self.assertEqual(m["state"], "failed", m)
        self.assertIn("setup command failed (exit 3)", m["error"])
        self.assertIn("registry: API Error: 429", views.show_text(self.desk, tid))
        self.assertFalse(provider_problem(self.desk, self.desk.settings(), "claude"))  # not the provider's quota
        self.assertEqual(m["attempts"], [])

    def test_retry_is_only_for_a_task_that_has_ended(self):
        tid = self.desk.new_task("Add hello.txt")
        with self.assertRaises(SystemExit):
            main(["--desk", str(self.desk.root), "retry", tid, "again"])
        self.assertEqual([m["id"] for m in self.desk.tasks()], [tid])

    def test_quota_moves_task_to_next_provider(self):
        s = self.desk.settings()
        s["providers"]["mimo"] = {"kind": "compatible", "model": "mimo-x", "effort": "", "base_url": "https://example.invalid"}
        s["lanes"]["a"]["providers"] = ["mimo", "claude"]
        s["lanes"]["a"]["env"] = {"FAKE_QUOTA_ON_COMPAT": "1"}
        self.assertEqual(self.desk.save_settings(s), [])
        self.desk.write_key("mimo", "sk-test-01")
        tid = self.desk.new_task("Add hello.txt")
        m = self.settle(Dispatcher(self.desk), tid)
        self.assertEqual(m["state"], "done", m)
        self.assertEqual(m["provider"], "claude")
        self.assertEqual(m["attempts"][0]["provider"], "mimo")
        self.assertIn("resting until", provider_problem(self.desk, self.desk.settings(), "mimo"))

    def test_rejected_key_rests_provider_until_a_new_key(self):
        s = self.desk.settings()
        s["providers"]["mimo"] = {"kind": "compatible", "model": "mimo-x", "effort": "", "base_url": "https://example.invalid"}
        s["lanes"]["a"]["providers"] = ["mimo", "claude"]
        s["lanes"]["a"]["env"] = {"FAKE_BADKEY_ON_COMPAT": "1"}
        self.assertEqual(self.desk.save_settings(s), [])
        self.desk.write_key("mimo", "sk-wrong")
        m = self.settle(Dispatcher(self.desk), self.desk.new_task("Add hello.txt"))
        self.assertEqual((m["state"], m["provider"]), ("done", "claude"), m)
        self.assertIn("saving a new key", provider_problem(self.desk, self.desk.settings(), "mimo"))
        self.desk.write_key("mimo", "sk-right")
        self.assertEqual(provider_problem(self.desk, self.desk.settings(), "mimo"), "")

    def test_not_logged_in_rests_provider_until_a_check_answers(self):
        from hansa import run
        s = self.desk.settings()
        s["providers"]["mimo"] = {"kind": "compatible", "model": "mimo-x", "effort": "", "base_url": "https://example.invalid"}
        s["lanes"]["a"]["providers"] = ["mimo", "claude"]
        s["lanes"]["a"]["env"] = {"FAKE_NOLOGIN_ON_COMPAT": "1"}
        self.assertEqual(self.desk.save_settings(s), [])
        self.desk.write_key("mimo", "sk-test-01")
        m = self.settle(Dispatcher(self.desk), self.desk.new_task("Add hello.txt"))
        self.assertEqual((m["state"], m["provider"]), ("done", "claude"), m)
        self.assertEqual(m["attempts"][0]["error"], "not logged in (Please run /login)")
        prob = provider_problem(self.desk, self.desk.settings(), "mimo")
        self.assertIn("not logged in (Please run /login)", prob)
        self.assertIn("hansa check mimo", prob)
        self.assertIn("saving a new key", prob)
        self.assertNotIn("CLAUDE_CONFIG_DIR", prob)
        self.assertNotIn("log in", prob)
        self.assertIn("hansa check mimo", views.explain_text(self.desk, "provider", "mimo"))

        def answer(argv, **kwargs):  # logged in now
            return subprocess.CompletedProcess(argv, 0, json.dumps({"type": "result", "result": "OK"}), "")
        with unittest.mock.patch.object(run.subprocess, "run", answer):
            self.assertTrue(run.check_provider(self.desk, "mimo")["ok"])
        self.assertEqual(provider_problem(self.desk, self.desk.settings(), "mimo"), "")

    def test_the_provider_check_says_not_logged_in(self):
        from hansa import run

        def answer(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 1, json.dumps({
                "type": "result", "subtype": "success", "is_error": True, "api_error_status": None,
                "terminal_reason": "api_error", "num_turns": 1, "result": "Not logged in · Please run /login"}), "")
        with unittest.mock.patch.object(run.subprocess, "run", answer):
            r = run.check_provider(self.desk, "claude")
        self.assertEqual((r["ok"], r["detail"]), (False, "not logged in (Please run /login)"))
        prob = provider_problem(self.desk, self.desk.settings(), "claude")
        self.assertTrue(prob.startswith("not logged in (Please run /login); log in with"), prob)
        shown = {p["name"]: p["problem"] for p in views.status(self.desk)["providers"]}
        self.assertEqual(shown["claude"], prob)
        self.assertIn("hansa check claude", prob)
        self.assertIn("log in with `claude`", prob)
        self.assertNotIn("CLAUDE_CONFIG_DIR", prob)
        s = self.desk.settings()
        s["providers"]["claude"]["config_dir"] = str(self.desk.root / "claude-home")
        self.assertEqual(self.desk.save_settings(s), [])
        prob = provider_problem(self.desk, self.desk.settings(), "claude")
        self.assertIn(f"CLAUDE_CONFIG_DIR={self.desk.root / 'claude-home'} claude", prob)
        self.assertIn("hansa check claude", prob)
        self.assertEqual(run.unusable_reason({"is_error": True, "result": "Invalid API key · Please run /login"}, "")[0],
                         "key")

    def test_wait_returns_when_a_task_ends(self):
        tid = self.desk.new_task("Add hello.txt")
        d, stop = Dispatcher(self.desk), threading.Event()

        def ticker():
            while not stop.is_set():
                d.tick()
                time.sleep(0.1)
        th = threading.Thread(target=ticker)
        th.start()
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                main(["--desk", str(self.desk.root), "wait", "--timeout", "0.5", "--every", "0.1"])
        finally:
            stop.set()
            th.join()
        self.assertIn(f"{tid} done", out.getvalue())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            main(["--desk", str(self.desk.root), "wait"])
        self.assertIn("nothing to wait for", out.getvalue())

    def test_coordinator_passes_its_options_to_claude(self):
        echo = self.tmp / "echo-claude"
        echo.write_text("#!/bin/sh\necho \"$@\"\n")
        echo.chmod(0o755)
        (self.desk.root / "CLAUDE.md").write_text("an older card\n")
        (self.desk.root / ".claude" / "agents").mkdir(parents=True)
        (self.desk.root / ".claude" / "agents" / "verifier.md").write_text("a retired helper\n")
        r = subprocess.run([str(Path(__file__).resolve().parent.parent / "bin" / "hansa"), "--desk", str(self.desk.root),
                            "coordinator", "--permission-mode", "dontAsk"],
                           capture_output=True, text=True, env={**os.environ, "HANSA_CLAUDE": str(echo)})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--permission-mode dontAsk", r.stdout)
        repo = self.repo.resolve()
        self.assertIn(f"--add-dir {repo}", r.stdout)
        self.assertIn(f"Edit(//{str(repo).lstrip('/')}/**)", r.stdout)
        self.assertIn("Agent(general-purpose) Agent(Explore)", r.stdout)  # Claude Code's own helpers are off
        code = Path(__file__).resolve().parent.parent / "hansa"
        self.assertEqual((self.desk.root / "CLAUDE.md").read_text(), (code / "card.md").read_text())
        agents = self.desk.root / ".claude" / "agents"
        for name in ("scout.md", "reviewer.md"):  # the desk's helpers come along
            self.assertEqual((agents / name).read_text(), (code / "agents" / name).read_text())
        self.assertIn("effort: medium", (agents / "scout.md").read_text())
        self.assertFalse((agents / "verifier.md").exists())  # retired, kept in .hansa/
        self.assertTrue(list(self.desk.state_dir.glob("verifier.md.*")))

    def test_coordinator_start_brings_the_desks_permissions_up_to_date(self):
        echo = self.tmp / "echo-claude"
        echo.write_text("#!/bin/sh\necho \"$@\"\n")
        echo.chmod(0o755)
        mine = self.desk.root / ".claude" / "settings.json"
        mine.write_text(json.dumps({"permissions": {"allow": ["Bash(bh:*)"], "deny": ["Read(//x/.bh/**)"]},
                                    "env": {"KEEP": "1"}}))
        start = [str(Path(__file__).resolve().parent.parent / "bin" / "hansa"), "--desk", str(self.desk.root), "coordinator"]
        for _ in range(2):  # the second start finds nothing to change
            r = subprocess.run(start, capture_output=True, text=True, env={**os.environ, "HANSA_CLAUDE": str(echo)})
            self.assertEqual(r.returncode, 0, r.stderr)
        s = json.loads(mine.read_text())
        root = str(self.desk.root).lstrip("/")
        self.assertIn("Bash(hansa:*)", s["permissions"]["allow"])
        self.assertNotIn("Bash(bh:*)", s["permissions"]["allow"])
        self.assertIn(f"Read(//{root}/.hansa/**)", s["permissions"]["deny"])
        self.assertEqual(s["env"], {"KEEP": "1"})  # other settings there stay
        self.assertEqual(len(list(self.desk.state_dir.glob("coordinator-settings.json.*"))), 1)  # the old one, kept once

    def test_workers_see_neither_the_desks_card_nor_the_users_own_setup(self):
        seen = self.tmp / "argv.jsonl"
        s = self.desk.settings()
        s["lanes"]["a"]["env"] = {"FAKE_SHOW_ARGV": str(seen)}
        self.assertEqual(self.desk.save_settings(s), [])
        m = self.settle(Dispatcher(self.desk), self.desk.new_task("Add hello.txt"))
        argv = json.loads(seen.read_text().splitlines()[-1])
        self.assertEqual(argv[argv.index("--setting-sources") + 1], "project,local")  # not the user's own
        self.assertIn("--strict-mcp-config", argv)  # no MCP servers but hansa's
        skipped = json.loads(argv[argv.index("--settings") + 1])["claudeMdExcludes"]
        self.assertIn(str(self.desk.root / "CLAUDE.md"), skipped)  # the desk's card is the coordinator's
        self.assertNotIn(str(Path(m["worktree"]) / "CLAUDE.md"), skipped)  # the checkout's own stays

    def test_the_provider_check_runs_the_way_workers_do(self):
        from hansa import run
        seen = {}

        def answer(argv, **kwargs):
            seen["argv"] = argv
            return subprocess.CompletedProcess(argv, 0, json.dumps({"type": "result", "result": "OK",
                                                                     "modelUsage": {"claude-x": {}}}), "")
        with unittest.mock.patch.object(run.subprocess, "run", answer):
            self.assertTrue(run.check_provider(self.desk, "claude")["ok"])
        self.assertIn("--strict-mcp-config", seen["argv"])
        self.assertIn(str(self.desk.root / "CLAUDE.md"),
                      json.loads(seen["argv"][seen["argv"].index("--settings") + 1])["claudeMdExcludes"])

    def test_lane_check_runs_after_the_worker_and_gates_the_merge(self):
        s = self.desk.settings()
        s["lanes"]["a"]["check"] = "test -f hello.txt && echo checked"
        self.assertEqual(self.desk.save_settings(s), [])
        d = Dispatcher(self.desk)
        m = self.settle(d, self.desk.new_task("Add hello.txt"))
        self.assertEqual((m["state"], m["check"]["ok"]), ("done", True), m)
        self.assertIn("checked", m["check"]["tail"])
        s = self.desk.settings()
        s["lanes"]["a"]["check"] = "echo not good; exit 3"
        self.assertEqual(self.desk.save_settings(s), [])
        m = self.settle(d, self.desk.new_task("Add hello.txt again"))
        self.assertEqual((m["state"], m["check"]["ok"], m["check"]["exit"]), ("done", False, 3), m)
        self.assertIn("FAILED (exit 3)", views.show_text(self.desk, m["id"]))
        self.assertIn("check FAILED", views.status_text(views.status(self.desk)))
        with self.assertRaises(SystemExit):
            main(["--desk", str(self.desk.root), "merge", m["id"]])
        main(["--desk", str(self.desk.root), "merge", m["id"], "--despite-check"])
        self.assertEqual(self.desk.meta(m["id"])["state"], "merged")

    def test_check_result_survives_a_server_restart(self):
        s = self.desk.settings()
        s["lanes"]["a"]["check"] = "sleep 0.5; exit 0"
        self.assertEqual(self.desk.save_settings(s), [])
        tid = self.desk.new_task("Add hello.txt")
        d, end = Dispatcher(self.desk), time.time() + 20
        while not self.desk.meta(tid).get("check_started") and time.time() < end:
            d.tick()
            time.sleep(0.05)
        d.procs[tid].wait()  # the old server is gone; its check still wrote the exit code
        m = self.settle(Dispatcher(self.desk), tid)
        self.assertEqual((m["state"], m["check"]["ok"]), ("done", True), m)

    def test_a_refused_task_moves_on_without_resting_the_provider(self):
        s = self.desk.settings()
        s["providers"]["mimo"] = {"kind": "compatible", "model": "mimo-x", "effort": "", "base_url": "https://example.invalid"}
        s["lanes"]["a"]["providers"] = ["mimo", "claude"]
        s["lanes"]["a"]["env"] = {"FAKE_REFUSE_ON_COMPAT": "1"}
        self.assertEqual(self.desk.save_settings(s), [])
        self.desk.write_key("mimo", "sk-test-01")
        m = self.settle(Dispatcher(self.desk), self.desk.new_task("Add hello.txt"))
        self.assertEqual((m["state"], m["provider"]), ("done", "claude"), m)
        self.assertEqual(m["attempts"][0]["kind"], "refused")
        self.assertEqual(provider_problem(self.desk, self.desk.settings(), "mimo"), "")
        s = self.desk.settings()
        s["lanes"]["a"]["providers"] = ["mimo"]
        self.assertEqual(self.desk.save_settings(s), [])
        m = self.settle(Dispatcher(self.desk), self.desk.new_task("Add hello.txt again"))
        self.assertEqual(m["state"], "failed", m)
        self.assertIn("no other provider", m["error"])

    def test_no_commits_means_no_check_and_the_task_can_be_closed(self):
        s = self.desk.settings()
        s["lanes"]["a"]["env"] = {"FAKE_NO_CHANGES": "1"}
        s["lanes"]["a"]["check"] = "false"
        self.assertEqual(self.desk.save_settings(s), [])
        m = self.settle(Dispatcher(self.desk), self.desk.new_task("Look around"))
        self.assertEqual((m["state"], m.get("commits"), m.get("check")), ("done", 0, None), m)
        self.assertIn("nothing to merge", views.show_text(self.desk, m["id"]))
        main(["--desk", str(self.desk.root), "cancel", m["id"]])
        self.assertEqual(self.desk.meta(m["id"])["state"], "cancelled")

    def test_retry_replaces_the_original(self):
        m = self.settle(Dispatcher(self.desk), self.desk.new_task("Add hello.txt"))
        main(["--desk", str(self.desk.root), "retry", m["id"], "also", "add", "a", "second", "line"])
        old = self.desk.meta(m["id"])
        self.assertEqual(old["state"], "replaced")
        self.assertNotIn(m["id"], [r["id"] for r in views.status(self.desk)["review"]])
        self.assertEqual(self.desk.meta(old["replaced_by"])["parent"], m["id"])

    def test_browser_is_given_to_one_task_at_a_time(self):
        with self.assertRaises(SystemExit):  # no browser set up: refuse, queue nothing
            main(["--desk", str(self.desk.root), "add", "Look at the page", "--browser"])
        self.assertEqual(self.desk.tasks(), [])
        seen = self.tmp / "argv.jsonl"
        s = self.desk.settings()
        s["browser"] = "/usr/bin/env true"
        s["lanes"]["a"]["env"] = {"FAKE_SHOW_ARGV": str(seen)}
        self.assertEqual(self.desk.save_settings(s), [])
        main(["--desk", str(self.desk.root), "add", "Look at the page", "--browser"])
        main(["--desk", str(self.desk.root), "add", "Plain task"])
        d = Dispatcher(self.desk)
        for m in self.desk.tasks():
            self.settle(d, m["id"])
        with_browser, plain = [json.loads(x) for x in seen.read_text().splitlines()]
        self.assertIn("--mcp-config", with_browser)
        self.assertIn("mcp__browser", with_browser)
        self.assertNotIn("--mcp-config", plain)
        self.assertNotIn("mcp__browser", plain)
        cfg = json.loads(Path(with_browser[with_browser.index("--mcp-config") + 1]).read_text())
        self.assertEqual(cfg["mcpServers"]["browser"]["args"][1:], [str(self.desk.tasks_dir / "t0001" / "browser"),
                                                                    "/usr/bin/env true"])
        self.assertIn("with a browser", views.show_text(self.desk, "t0001"))

    def test_stopped_worker_still_hands_in_what_it_wrote(self):
        s = self.desk.settings()
        s["providers"]["mimo"] = {"kind": "compatible", "model": "mimo-x", "effort": "", "base_url": "https://x"}
        s["lanes"]["a"]["providers"] = ["mimo"]
        s["lanes"]["a"]["env"] = {"FAKE_STOP_AT_TURNS": "1"}
        self.assertEqual(self.desk.save_settings(s), [])
        self.desk.write_key("mimo", "sk-test-01")
        tid = self.desk.new_task("Look at the page", model="sonnet")
        m = self.settle(Dispatcher(self.desk), tid)
        self.assertEqual(m["state"], "failed")
        self.assertEqual(m["model_asked"], "mimo-x")  # a task's --model is for Claude providers only
        self.assertTrue(m["error"].startswith("stopped at the ceiling of 300 turns: the task was probably too big"), m)
        shown = views.show_text(self.desk, tid)
        self.assertIn("Stopped at the ceiling before it wrote its report", shown)
        self.assertIn("week badge is still in English", shown)
        self.assertIn("split the task or make it clearer", shown)
        self.assertEqual((m["usage"]["mimo-x"]["in"], m["usage"]["mimo-x"]["cache_read"]), (1400, 6000))  # no totals
        with contextlib.redirect_stdout(io.StringIO()):
            main(["--desk", str(self.desk.root), "retry", tid, "only the detail page"])
        follow = (self.desk.tasks_dir / "t0002" / "task.md").read_text()
        self.assertIn("The previous attempt's report:", follow)
        self.assertIn("week badge is still in English", follow)

    def test_verify_runs_the_change_and_holds_the_merge(self):
        seen = self.tmp / "argv.jsonl"
        d = Dispatcher(self.desk)
        tid = self.desk.new_task("Add hello.txt")
        self.settle(d, tid)
        s = self.desk.settings()
        s["lanes"]["a"]["env"] = {"FAKE_VERDICT": "does not hold", "FAKE_SHOW_ARGV": str(seen)}
        s["lanes"]["a"]["check"] = "exit 1"  # a verification is never merged, so its branch is not checked
        self.assertEqual(self.desk.save_settings(s), [])
        self.assertIn(f"then hansa verify {tid} (code or tests) or hansa merge {tid}", views.show_text(self.desk, tid))
        with contextlib.redirect_stdout(io.StringIO()):
            main(["--desk", str(self.desk.root), "verify", tid, "the file's contents"])
        vid = self.desk.meta(tid)["verify"]
        v = self.desk.meta(vid)
        self.assertEqual((v["kind"], v["parent"], v["start_from"]), ("verify", tid, self.desk.meta(tid)["head"]))
        with self.assertRaises(SystemExit):  # not while the verification runs
            main(["--desk", str(self.desk.root), "merge", tid])
        self.assertIn(f"next: wait for the verification: hansa wait {vid}", views.show_text(self.desk, tid))
        v = self.settle(d, vid)
        self.assertEqual((v["state"], v["verdict"], v.get("check")), ("done", "does not hold", None))
        self.assertEqual((v["commits"], v["files"]), (0, []))  # its scratch changes are set aside, not committed
        wt = Path(v["worktree"])
        self.assertEqual(subprocess.run(["git", "status", "--porcelain"], cwd=wt, capture_output=True,
                                        text=True).stdout, "")
        self.assertIn(vid, subprocess.run(["git", "stash", "list"], cwd=wt, capture_output=True, text=True).stdout)
        argv = json.loads(seen.read_text().splitlines()[-1])
        self.assertIn("Bash(git checkout:*)", argv)
        self.assertNotIn("Bash(git stash:*)", argv)
        self.assertIn("checking another worker's finished task", argv[argv.index("--append-system-prompt") + 1])
        self.assertIn("Run first", (self.desk.tasks_dir / vid / "task.md").read_text())
        st = views.status(self.desk)
        self.assertEqual([m["id"] for m in st["review"]], [tid])  # the verification is not up for review itself
        self.assertIn(f"verification {vid}: does not hold", views.status_text(st))
        self.assertIn(f"{vid}: does not hold", views.show_text(self.desk, tid))
        self.assertIn(f"more lines; the whole report is {self.desk.tasks_dir / vid / 'result.md'}",
                      views.show_text(self.desk, vid))
        for args in (["merge", vid], ["merge", tid]):  # never merged itself; "does not hold" holds the task
            with self.assertRaises(SystemExit):
                main(["--desk", str(self.desk.root), *args])
        with contextlib.redirect_stdout(io.StringIO()):
            main(["--desk", str(self.desk.root), "merge", tid, "--despite-check"])
        self.assertEqual((self.desk.meta(tid)["state"], self.desk.meta(vid)["state"]), ("merged", "cancelled"))
        self.assertIn("verdict: does not hold", views.show_text(self.desk, vid))

    def test_design_session_opens_for_the_principal_and_comes_back_for_review(self):
        try:
            tmux = tmux_bin()
        except RuntimeError:
            self.skipTest("no tmux here")
        seen = self.tmp / "argv.jsonl"
        s = self.desk.settings()
        s["providers"]["claude"]["config_dir"] = str(self.tmp / "cc")
        s["lanes"]["a"]["env"] = {"FAKE_SHOW_ARGV": str(seen), "FAKE_DESIGN_SECONDS": "2"}
        s["lanes"]["a"]["preview"] = "npm run dev"
        s["lanes"]["a"]["check"] = "true && echo checked"
        s["lanes"]["a"]["setup"] = "echo installing"
        self.assertEqual(self.desk.save_settings(s), [])
        self.addCleanup(subprocess.run, [tmux, "-L", tmux_socket(self.desk), "kill-server"], capture_output=True)
        with contextlib.redirect_stdout(io.StringIO()):
            main(["--desk", str(self.desk.root), "design", "Make the week badge bigger", "--lane", "a"])
        tid = "t0001"
        with self.assertRaises(SystemExit):  # one design session per lane at a time
            main(["--desk", str(self.desk.root), "design", "Another one", "--lane", "a"])
        d = Dispatcher(self.desk)
        d.tick()
        m = self.desk.meta(tid)
        self.assertEqual((m["state"], m["kind"], Path(m["worktree"]).name), ("running", "design", "a-design"))
        st = views.status(self.desk)
        self.assertIn(f"attach -t design-{tid}", st["design"][0]["join"])
        self.assertEqual(st["running"], [])  # it does not take the lane's worker slot
        self.assertIn(f"design-{tid}", [a for a in st["asks"] if a["by"] == "hansa"][0]["text"])
        work = self.desk.new_task("Add hello.txt", lane="a")
        self.assertEqual(self.settle(d, work)["state"], "done")  # the lane kept working meanwhile
        m = self.settle(d, tid)
        self.assertEqual(m["state"], "done", m)
        self.assertEqual((m["files"], m["turns"]), (["badge.txt"], 2))
        self.assertEqual(m["usage"]["claude-x"]["in"], 200)
        self.assertIn("installing", (self.desk.tasks_dir / tid / "setup.log").read_text())  # set up before it opened
        shown = views.show_text(self.desk, tid)
        self.assertIn("Hand-in: the badge is bigger", shown)
        self.assertEqual([a for a in self.desk.asks() if a["by"] == "hansa" and not a["closed"]], [])
        runs = [json.loads(x) for x in seen.read_text().splitlines()]
        self.assertEqual(sum("-p" in a for a in runs), 1)  # the worker's; the design session is interactive
        argv = next(a for a in runs if "-p" not in a)
        self.assertIn("Make the week badge bigger", argv[1])
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "dontAsk")
        self.assertIn("Bash(npm run dev:*)", argv)
        self.assertIn("Bash(git merge:*)", argv)
        self.assertIn("Bash(git commit:*)", argv)  # the principal is there: the session commits each step itself
        self.assertIn("Bash(echo checked:*)", argv)  # and may run the lane's check, one rule per command in it
        self.assertIn("No browser is set up", argv[argv.index("--append-system-prompt") + 1])
        self.assertIn("--strict-mcp-config", argv)  # a design session sees no more of the account than a worker
        with contextlib.redirect_stdout(io.StringIO()):
            main(["--desk", str(self.desk.root), "merge", tid])
        self.assertTrue((self.repo / "badge.txt").exists())
        self.assertEqual({r["task"]: r["seat"] for r in cost.worker_rows(self.desk, 0)}, {tid: "design", work: "worker"})

    def test_a_running_task_shows_how_far_the_worker_has_got(self):
        s = self.desk.settings()
        s["providers"]["mimo"] = {"kind": "compatible", "model": "mimo-x", "effort": "", "base_url": "https://x"}
        self.assertEqual(self.desk.save_settings(s), [])
        tid = self.desk.new_task("Look at something slowly")
        self.desk.update(tid, state="running", lane="a", provider="mimo", model_asked="mimo-x", started=time.time() - 600,
                         session_id="sid-1", pid=None)
        rec = self.desk.state_dir / "claude-mimo" / "projects" / "-work" / "sid-1.jsonl"
        rec.parent.mkdir(parents=True)
        rec.write_text("".join(json.dumps({"type": "assistant", "message": {"id": f"m{i}", "content": [
            {"type": "text", "text": t}]}}) + "\n" for i, t in enumerate(["Reading the code.", "Now the tests."])))
        run = views.status(self.desk)["running"][0]
        self.assertEqual((run["progress"]["replies"], run["progress"]["idle_min"]), (2, 0))
        self.assertIn("so far: 2 model replies, last activity 0 min ago; last note: Now the tests.",
                      views.show_text(self.desk, tid))
        self.assertNotIn("0 turns", views.show_text(self.desk, tid))  # turns are counted when it ends
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            main(["--desk", str(self.desk.root), "wait", tid, "--timeout", "0.001", "--every", "0.01"])
        self.assertIn(f"no task finished in 0.001 min\n{tid} running 10 min, so far: 2 model replies", out.getvalue())

    def test_browser_tool_is_not_told_where_the_worker_works(self):
        folder = self.tmp / "browser"
        folder.mkdir()
        fake = self.tmp / "fake_browser.py"
        fake.write_text("import os, sys\nwith open('seen.txt', 'w') as f:\n"
                        "    f.write(os.getcwd() + '\\n')\n    f.writelines(sys.stdin)\nprint('bye')\n")
        init = {"jsonrpc": "2.0", "id": 0, "method": "initialize",
                "params": {"capabilities": {"roots": {"listChanged": True}}, "clientInfo": {"name": "claude-code"}}}
        call = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "browser_take_screenshot"}}
        relay = Path(__file__).resolve().parent.parent / "hansa" / "relay.py"
        out = subprocess.run([sys.executable, str(relay), str(folder), f"{sys.executable} {fake}"],
                             input="".join(json.dumps(m) + "\n" for m in (init, call)),
                             capture_output=True, text=True, timeout=30)
        self.assertEqual((out.returncode, out.stdout), (0, "bye\n"))
        cwd, *lines = (folder / "seen.txt").read_text().splitlines()
        self.assertEqual(Path(cwd).resolve(), folder.resolve())
        got = [json.loads(x) for x in lines]
        self.assertEqual(got[0]["params"]["capabilities"], {})
        self.assertEqual(got[1], call)

    def test_waiting_reason_names_the_missing_piece(self):
        s = self.desk.settings()
        s["providers"]["mimo"] = {"kind": "compatible", "model": "mimo-x", "effort": "", "base_url": ""}
        s["lanes"]["a"]["providers"] = ["mimo"]
        self.assertEqual(self.desk.save_settings(s), [])
        tid = self.desk.new_task("Anything")
        Dispatcher(self.desk).tick()
        self.assertEqual(self.desk.meta(tid)["state"], "queued")
        why = waiting_reason(self.desk, self.desk.settings(), self.desk.meta(tid), set())
        self.assertIn("mimo: no endpoint address", why)

    def test_settings_refuse_keys_and_unknown_fields(self):
        s = self.desk.settings()
        s["providers"]["claude"]["api_key"] = "sk-no"
        self.assertTrue(any("keys are not kept" in p for p in validate(s)))
        s = self.desk.settings()
        s["lanes"]["a"]["surprise"] = 1
        s["lanes"]["a"]["env"] = {"ANTHROPIC_BASE_URL": "x"}
        probs = validate(s)
        self.assertTrue(any("unknown field" in p for p in probs))
        self.assertTrue(any("set from the provider" in p for p in probs))

    def test_key_that_does_not_look_like_a_key_is_refused(self):
        for bad in ("MIMO_API_KEY=abc", "sk-abc def", '"sk-abc"', "sk-ab***yz", "Bearer sk-abc"):
            with self.assertRaises(ValueError):
                self.desk.write_key("mimo", bad)
        self.assertFalse(self.desk.has_key("mimo"))

    def test_keys_are_private_and_never_shown(self):
        self.desk.write_key("mimo", "sk-secret")
        mode = stat.S_IMODE(os.stat(self.desk.key_file("mimo")).st_mode)
        self.assertEqual(mode, 0o600)
        s = self.desk.settings()
        s["providers"]["mimo"] = {"kind": "compatible", "model": "m", "effort": "", "base_url": "https://x"}
        self.desk.save_settings(s)
        pub = json.dumps(public_settings(self.desk))
        self.assertNotIn("sk-secret", pub)
        self.assertTrue(public_settings(self.desk)["keys"]["mimo"])

    def test_cost_counts_workers_and_coordinator_once(self):
        tid = self.desk.new_task("Add hello.txt")
        self.settle(Dispatcher(self.desk), tid)
        conf = self.tmp / "conf"
        proj = conf / "projects" / cost.slug(self.desk.root)
        proj.mkdir(parents=True)
        line = {"timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()), "message": {
            "id": "msg_1", "model": "claude-opus-5", "usage": {"input_tokens": 100, "output_tokens": 10,
                                                               "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}}}
        (proj / "s.jsonl").write_text((json.dumps(line) + "\n") * 3)  # one message, logged three times
        helper = proj / "s" / "subagents"
        helper.mkdir(parents=True)
        (helper / "agent-1.jsonl").write_text(json.dumps({**line, "message": {**line["message"], "id": "msg_2"}}) + "\n")
        (helper / "agent-1.meta.json").write_text(json.dumps({"agentType": "scout"}))
        s = self.desk.settings()
        s["providers"]["claude"]["config_dir"] = str(conf)
        self.desk.save_settings(s)
        rep = cost.report(self.desk, 1)
        self.assertEqual(rep["by"]["seat"]["coordinator"]["tokens"], 110)
        self.assertEqual(rep["by"]["seat"]["helper:scout"]["tokens"], 110)
        later = cost.report(self.desk, 1, since=time.time() + 60)  # hansa cost --since: nothing after "now"
        self.assertEqual((later["total"]["tokens"], later["window"][:6]), (0, "since "))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            main(["--desk", str(self.desk.root), "cost", "--since", time.strftime("%H:%M"), "--by", "seat"])
        self.assertIn("since ", out.getvalue())
        self.assertAlmostEqual(rep["by"]["seat"]["coordinator"]["usd"], (100 * 5 + 10 * 25) / 1e6, places=6)
        self.assertEqual(rep["by"]["seat"]["worker"]["tokens"], 6300)
        self.assertEqual(rep["unpriced_models"], [])

    def test_verifications_are_their_own_seat_in_the_cost(self):
        tid = self.desk.new_task("Run the change", kind="verify", parent="t0000")
        self.desk.update(tid, state="done", finished=time.time(), provider="claude",
                         usage={"claude-x": {"in": 10, "out": 1, "cache_read": 0, "cache_write": 0}})
        self.assertEqual([r["seat"] for r in cost.worker_rows(self.desk, 0)], ["verify"])

    def test_coordinator_cost_counts_every_provider_it_ran_on(self):
        s = self.desk.settings()
        s["providers"]["claude"]["config_dir"] = str(self.tmp / "cc")
        s["providers"]["mimo"] = {"kind": "compatible", "model": "mimo-x", "effort": "", "base_url": "https://example.invalid"}
        self.assertEqual(self.desk.save_settings(s), [])
        for home, mid in ((self.tmp / "cc", "m1"), (self.desk.state_dir / "claude-mimo", "m2")):
            proj = home / "projects" / cost.slug(self.desk.root)
            proj.mkdir(parents=True)
            (proj / "s.jsonl").write_text(json.dumps({"timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
                "message": {"id": mid, "model": "claude-x", "usage": {"input_tokens": 10, "output_tokens": 2}}}) + "\n")
        rows = cost.transcript_rows(self.desk, 0)  # the coordinator ran on claude, then on mimo
        self.assertEqual(sorted((r["provider"], r["in"]) for r in rows), [("claude", 10), ("mimo", 10)])

    def test_settings_saved_from_a_stale_page_are_refused(self):
        first, second = public_settings(self.desk), public_settings(self.desk)  # two tabs, opened together
        first["settings"]["concurrency"] = 3
        self.assertEqual(self.desk.save_settings(first["settings"], rev=first["rev"]), [])
        second["settings"]["cool_minutes"] = 5
        problems = self.desk.save_settings(second["settings"], rev=second["rev"])
        self.assertTrue(problems and "changed since" in problems[0], problems)
        self.assertEqual((self.desk.settings()["concurrency"], self.desk.settings().get("cool_minutes", 30)), (3, 30))

    def test_a_check_that_ran_on_a_replaced_key_is_not_recorded(self):
        import hansa.run as run
        s = self.desk.settings()
        s["providers"]["mimo"] = {"kind": "compatible", "model": "mimo-x", "effort": "", "base_url": "https://example.invalid"}
        self.assertEqual(self.desk.save_settings(s), [])
        self.desk.write_key("mimo", "sk-old-key-0001")

        def key_replaced_meanwhile(argv, **kwargs):
            self.desk.write_key("mimo", "sk-new-key-0002")
            return subprocess.CompletedProcess(argv, 1, json.dumps({"type": "result", "is_error": True,
                "api_error_status": 401, "result": "API Error: 401 authentication_error"}), "")
        with unittest.mock.patch.object(run.subprocess, "run", key_replaced_meanwhile):
            r = run.check_provider(self.desk, "mimo")
        self.assertFalse(r["ok"])
        self.assertIn("changed while this check ran", r["detail"])
        self.assertFalse(provider_problem(self.desk, self.desk.settings(), "mimo"))  # the new key is not rested

    def test_status_says_which_build_runs_and_whether_cost_is_audited(self):
        st = views.status(self.desk)
        self.assertTrue(st["build"])
        self.assertIn(st["build"], views.status_text(st))
        self.assertNotIn("cost audit on", views.status_text(st))
        s = self.desk.settings()
        s["coordinator"]["audits_cost"] = True
        self.assertEqual(self.desk.save_settings(s), [])
        self.assertIn("cost audit on", views.status_text(views.status(self.desk)))
        self.assertIn("cost audit on", (Path(__file__).resolve().parent.parent / "hansa" / "card.md").read_text())

    def test_a_design_session_waiting_for_a_stopped_server_says_so(self):
        self.desk.new_task("Shape the badge", kind="design", lane="a")
        self.assertIn("`hansa serve` is not running", views.status(self.desk)["queued"][0]["waiting"])

    def test_helpers_name_only_tools_claude_code_has(self):
        code = Path(__file__).resolve().parent.parent / "hansa" / "agents"
        for f in code.glob("*.md"):
            tools = next(x for x in f.read_text().splitlines() if x.startswith("tools:"))
            self.assertEqual(tools, "tools: Read, Bash", f.name)

    def test_the_page_only_shows_tasks_by_their_id(self):
        (self.desk.root / "x").mkdir()
        (self.desk.root / "x" / "meta.json").write_text(json.dumps({"id": "x", "title": "not a task", "state": "done"}))
        httpd = start_web(self.desk, "127.0.0.1", 0)
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        try:
            with self.assertRaises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(f"{base}/api/task?t={self.desk.web_token()}&id=../x")
            self.assertEqual(e.exception.code, 404)
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_web_needs_token_and_header(self):
        httpd = start_web(self.desk, "127.0.0.1", 0)
        port = httpd.server_address[1]
        base = f"http://127.0.0.1:{port}"
        try:
            with self.assertRaises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(base + "/api/status")
            self.assertEqual(e.exception.code, 403)
            tok = self.desk.web_token()
            with urllib.request.urlopen(f"{base}/api/status?t={tok}") as r:
                self.assertIn("lanes", json.load(r))
            req = urllib.request.Request(f"{base}/api/pause?t={tok}", data=b"{}", method="POST")
            with self.assertRaises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(req)
            self.assertEqual(e.exception.code, 403)
            req.add_header("X-Hansa", "1")
            with urllib.request.urlopen(req) as r:
                self.assertTrue(json.load(r)["ok"])
            self.assertTrue(self.desk.paused())
        finally:
            httpd.shutdown()
            httpd.server_close()


if __name__ == "__main__":
    unittest.main()
