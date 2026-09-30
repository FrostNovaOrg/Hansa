"""Tests for hansa with a stand-in for claude, so no model is called."""
import contextlib
import fcntl
import functools
import io
import json
import os
import pwd
import re
import shlex
import shutil
import signal
import socket
import stat
import struct
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

from hansa import account, cli, cost, notify, views  # noqa: E402
from hansa.cli import init_desk, main  # noqa: E402
from hansa.desk import Desk, now, public_settings, validate, write_json, write_problems  # noqa: E402
from hansa.run import (Dispatcher, join_command, provider_problem, text_only, tmux_bin, tmux_socket,  # noqa: E402
                       waiting_reason)
from hansa.web import start_web  # noqa: E402
from hansa.web import _save_settings as save_from_page  # noqa: E402
from hansa.run import WORKER_TMP_ROOT  # noqa: E402 - as hansa has it, before a test points it elsewhere

def skip_unless_ports(test):
    """Skip a test that binds a local port where the sandbox refuses to (macOS's, with no localhost on the
    lane's network list), as tests that make a Unix socket skip where one is refused."""
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
    except PermissionError as e:
        test.skipTest(f"cannot bind a local port here ({e}; a sandbox refuses to)")
    finally:
        s.close()


# What Claude Code writes when a worker starts work in the background: the tool result's text and the row's
# toolUseResult, used to pair background launches with completion notices.
BG_SHAPES = r'''
def BG_BASH(bid):
    return ("Command running in background with ID: " + bid + ". Output is being written to: /tmp/x/" + bid
            + ".output", {"stdout": "", "stderr": "", "interrupted": False, "isImage": False,
                          "noOutputExpected": False, "backgroundTaskId": bid})
def BG_AGENT(aid, what):
    return ("Async agent launched successfully. (This tool result is internal metadata — never quote or paste "
            "any part of it, including the agentId below, into a user-facing reply.)\nagentId: " + aid
            + " (internal ID - do not mention to user. Use SendMessage with to: '" + aid + "' to continue this agent.)",
            {"status": "async_launched", "agentId": aid, "description": what, "prompt": "Do it"})
def BG_NOTICE(bid, what):
    return ("<task-notification>\n<task-id>" + bid + "</task-id>\n<tool-use-id>toolu_" + bid + "</tool-use-id>\n"
            "<status>completed</status>\n<summary>Agent \"" + what + "\" completed</summary>\n"
            "<result>done</result>\n</task-notification>")
'''
exec(BG_SHAPES)

FAKE = r'''#!/usr/bin/env python3
import json, os, shlex, subprocess, sys, time
if os.environ.get("CLAUDE_ENV_FILE"):  # the lane's env: the real one runs the file in front of each Bash command
    for word in shlex.split(open(os.environ["CLAUDE_ENV_FILE"]).read()):
        if "=" in word:
            os.environ[word.split("=", 1)[0]] = word.split("=", 1)[1]
''' + BG_SHAPES + r'''
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
if os.environ.get("FAKE_NET"):  # "what:times:count file": the run ends on a provider failure its first `times` times
    what, times, count = os.environ["FAKE_NET"].split(":", 2)
    n = int(open(count).read()) if os.path.exists(count) else 0
    open(count, "w").write(str(n + 1))
    if n < int(times):
        open("partial.txt", "w").write("half done\n")
        status = int(what) if what.isdigit() else None
        text = {"econnreset": "API Error: Connection dropped (ECONNRESET)", "529": "API Error: Repeated 529 Overloaded errors",
                "500": "API Error: 500 Internal server error", "plain": "I could not do it", "quota": "API Error: 429 rate_limit_error"}[what]
        print(json.dumps({"type": "result", "subtype": "success", "is_error": True,
                          "api_error_status": 429 if what == "quota" else status,
                          "result": text, "session_id": "s-net", "num_turns": 3}))
        sys.exit(1)
if os.environ.get("FAKE_SEE_TMP"):  # the temporary folder Claude Code was given, as the worker started
    t = os.environ.get("CLAUDE_CODE_TMPDIR", "")
    open(os.environ["FAKE_SEE_TMP"], "a").write(json.dumps({"tmp": t, "tmpdir": os.environ.get("TMPDIR"), "there": os.path.isdir(t),
        "mode": os.stat(t).st_mode & 0o777 if os.path.isdir(t) else None}) + "\n")
    time.sleep(float(os.environ.get("FAKE_TMP_WAIT", "0")))
if os.environ.get("FAKE_REFUSED"):  # a call Claude Code refused, with its error text in the session record
    sid = sys.argv[sys.argv.index("--session-id") + 1]
    rec = os.path.join(os.environ["CLAUDE_CONFIG_DIR"], "projects", "-work", sid + ".jsonl")
    os.makedirs(os.path.dirname(rec), exist_ok=True)
    cmd = os.environ["FAKE_REFUSED"]
    rows = [{"type": "assistant", "message": {"id": "r1", "content": [
                {"type": "tool_use", "id": "toolu_r1", "name": "Bash", "input": {"command": cmd}}]}},
            {"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_r1", "is_error": True,
                 "content": "<tool_use_error>Contains expansion</tool_use_error>"}]}}]
    with open(rec, "w") as f:
        f.writelines(json.dumps(r) + "\n" for r in rows)
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "num_turns": 2, "session_id": sid,
                      "result": "Could not run the build: it was refused.",
                      "permission_denials": [{"tool_name": "Bash", "tool_use_id": "toolu_r1",
                                              "tool_input": {"command": cmd}}]}))
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
            notice("early"), say("a2", "The watch said the tests pass.")]
    kind = os.environ["FAKE_TRANSCRIPT"]
    if kind.startswith("bg-"):  # "bg-cmd", "bg-agent": work started in the background; "-noticed": and seen to end
        cmd = kind.startswith("bg-cmd")
        use = {"type": "tool_use", "id": "u2", "name": "Bash" if cmd else "Agent", "input": {
            "description": "Run the full test suite" if cmd else "Review the badge change", "run_in_background": True,
            **({"command": "python3 -m unittest"} if cmd else {"prompt": "Review it", "subagent_type": "general-purpose"})}}
        said, result = BG_BASH("bk3j9x") if cmd else BG_AGENT("a7f2c1", "Review the badge change")
        rows += [{"type": "assistant", "message": {"id": "a2b", "content": [use]}},
                 {"type": "user", "message": {"role": "user", "content": [
                     {"type": "tool_result", "tool_use_id": "u2", "content": [{"type": "text", "text": said}]}]},
                  "toolUseResult": result}]
        if kind.endswith("-noticed"):
            rows += [{"type": "user", "turnOrigin": "task_notification", "promptSource": "system", "message": {
                "role": "user", "content": BG_NOTICE("bk3j9x", "") if cmd else BG_NOTICE("a7f2c1", "")}}]
    rows.append(say("a3", report))
    late = kind == "late"
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
os.makedirs(os.path.dirname(hello) or ".", exist_ok=True)
open(hello, "w").write("hello from " + os.environ.get("HANSA_TASK", "?") + "\n")
if os.environ.get("FAKE_WRITE_BINARY"):
    open("blob.bin", "wb").write(b"\x00\x01PK" + os.environ["FAKE_WRITE_BINARY"].encode() + b"\x00\xff")
if os.environ.get("FAKE_LEAVE_CHILD"):  # something it started and left running, in its process group
    c = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    open(os.environ["FAKE_LEAVE_CHILD"], "w").write(str(c.pid))
if os.environ.get("FAKE_LEAVE_DETACHED"):  # the same, in a session of its own: out of hansa's process group
    c = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "left-behind"], start_new_session=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    open(os.environ["FAKE_LEAVE_DETACHED"], "w").write(str(c.pid))
if os.environ.get("FAKE_LEAVE_IN_TMP"):  # left running without HANSA_TASK, naming folders: its run's, and others
    tmp, bare = os.environ["CLAUDE_CODE_TMPDIR"], {k: v for k, v in os.environ.items() if k not in ("HANSA_TASK", "TMPDIR")}
    open(os.path.join(tmp, "load.sh"), "w").write("while :; do sleep 1; done\n")
    sleep = [sys.executable, "-c", "import time; time.sleep(60)"]
    near, other = tmp + "2", os.path.join(os.path.dirname(tmp), "hansa-t9999-abcdefgh")
    for name, argv, env in (("script", ["/bin/sh", os.path.join(tmp, "load.sh")], bare),
                            ("tmpdir", sleep, {**bare, "TMPDIR": os.path.join(tmp, "claude-501")}),
                            ("near", sleep + [near + "/load.sh"], {**bare, "TMPDIR": near}),
                            ("other", sleep + [other + "/load.sh"], {**bare, "TMPDIR": other + "/claude-501"})):
        c = subprocess.Popen(argv, env=env, start_new_session=True,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        open(os.path.join(os.environ["FAKE_LEAVE_IN_TMP"], name + ".pid"), "w").write(str(c.pid))
if os.environ.get("FAKE_SANDBOX_LEFT"):  # what a sandbox on Linux leaves in the worktree: empty placeholders
    os.makedirs(".claude", exist_ok=True)
    for f in (".claude/.cc-writes", ".claude/settings.json", ".bashrc", ".gitconfig", ".mcp.json", ".vscode", ".idea"):
        open(f, "w").close()
if os.environ.get("FAKE_WRITE"):
    open(os.environ.get("FAKE_WRITE_FILE", "notes.txt"), "w").write(os.environ["FAKE_WRITE"] + "\n")
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
                        "env": dict(os.environ),
                        "stdout": "pipe" if stat.S_ISFIFO(mode) else "file" if stat.S_ISREG(mode) else "other"}) + "\n")
os.execvp(args[cut + 1], args[cut + 1:])
'''


@functools.lru_cache(maxsize=None)
def tmp_writable() -> bool:
    """Whether a folder can be made in /tmp, where hansa makes each worker run's own."""
    try:
        os.rmdir(tempfile.mkdtemp(dir="/tmp"))
    except OSError:
        return False
    return True


def stop_started_on(desk_root: Path, since: float, wait=10) -> None:
    """Stop what still runs with `--desk <desk_root>` on its command line and started after `since` (a follower
    hansa notify on started), and wait until it has ended: it writes its last state in the desk on its way out."""
    from hansa import run
    try:
        pids = {p for d in {str(desk_root), str(desk_root.resolve())} for p in run.pids_with_arg(d, since)}
    except run.CannotLook:
        return
    for p in pids:
        try:
            os.kill(p, signal.SIGTERM)
        except OSError:
            pass
    end = time.time() + wait
    while pids and time.time() < end:
        pids = {p for p in pids if run.alive(p)}
        time.sleep(0.05)
    for p in pids:
        try:
            os.kill(p, signal.SIGKILL)
        except OSError:
            pass


def sh(*args, cwd):
    subprocess.run(args, cwd=cwd, check=True, capture_output=True)



@contextlib.contextmanager
def as_home(home, **env):
    """With this folder as the home folder of the account the tests run as, in the password database (where
    hansa takes it from) and in $HOME; for a hansa started as a command too (tests/fakehome)."""
    real = pwd.getpwuid

    def getpwuid(uid):
        pw = real(uid)
        return pwd.struct_passwd((*pw[:5], str(home), *pw[6:]))

    fake = str(Path(__file__).resolve().parent / "fakehome")
    path = fake + (os.pathsep + os.environ["PYTHONPATH"] if os.environ.get("PYTHONPATH") else "")
    with unittest.mock.patch.dict(os.environ, {"HOME": str(home), "HANSA_TESTS_HOME": str(home), "PYTHONPATH": path,
                                               **env}), unittest.mock.patch("pwd.getpwuid", getpwuid):
        yield


class DeskTest(unittest.TestCase):
    def setUp(self):
        started = time.time()
        self.tmp = Path(tempfile.mkdtemp())
        # registered first, so it runs last: after the other cleanups have stopped what the test started, and
        # after the followers of its desk (detached, each ends a moment after its session) have ended
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.addCleanup(stop_started_on, self.tmp / "desk", started)
        env = unittest.mock.patch.dict(os.environ)  # what setUp and the test set in it is put back when it ends
        env.start()
        self.addCleanup(env.stop)
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
        # not the Claude Code session the tests may run in: a stand-in (self.session) plays one where needed,
        # and what hansa would post to an inbox is caught in a file (self.posts)
        for k in ("CLAUDE_PID", "CLAUDE_CODE_MESSAGING_SOCKET", "HANSA_TASK"):
            os.environ.pop(k, None)
        os.environ.update(HANSA_NOTIFY_CAPTURE=str(self.tmp / "posted.jsonl"), HANSA_NOTIFY_POLL="0.2",
                          HANSA_NOTIFY_GATHER="0.5",
                          XDG_RUNTIME_DIR=str(self.tmp))  # the inboxes' folder (notify.socket_path), not the account's
        self.desk = init_desk(self.tmp / "desk", str(self.repo), "a")
        if not tmp_writable():  # inside a sandbox that keeps /tmp closed: the workers' folders go in the test's own
            root = unittest.mock.patch("hansa.run.WORKER_TMP_ROOT", str(self.tmp))
            root.start()
            self.addCleanup(root.stop)

    def srt_calls(self, tries=False) -> list:
        """What srt was called to run; tries: also hansa's runs of `true` to see that srt starts."""
        calls = [json.loads(x) for x in self.srt_log.read_text().splitlines()] if self.srt_log.exists() else []
        return [c for c in calls if tries or c["argv"][-2:] != ["--", "true"]]

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

    def test_show_gives_the_text_hansa_read_queued_running_and_finished(self):
        f = self.tmp / "task.md"
        f.write_text("Add hello.txt\n" + "".join(f"line {i}\n" for i in range(2, 13)))
        self.hansa("add", "--file", str(f))
        f.write_text("Edited after it was queued\n")  # read when it was queued: this changes nothing
        tid = "t0001"
        cut = f"  (9 more lines; the whole text: hansa show {tid} --full)"
        for state in ("queued", "running"):
            if state == "running":
                self.desk.update(tid, state="running", phase="starting", started=now())
            shown = self.hansa("show", tid)
            self.assertIn("text (as hansa read it when it was queued", shown)
            self.assertIn("\n  Add hello.txt\n  line 2\n", shown)
            self.assertIn("\n  line 3\n" + cut + "\n", shown)
            self.assertNotIn("line 4", shown)  # not the whole text each time: enough to know the task
            self.assertNotIn("Edited", shown)
            self.assertIn("  line 12\n", self.hansa("show", tid, "--full"))
            self.assertEqual(self.hansa("show", tid, "--report").strip(), f"(no report: {tid} is {state})")
        self.desk.update(tid, state="queued", phase="", started=None)
        m = self.settle(Dispatcher(self.desk), tid)
        self.assertEqual(m["state"], "done", m)
        report = "".join(f"said {i}\n" for i in range(70))
        (self.desk.tasks_dir / tid / "result.md").write_text(report)
        shown = self.hansa("show", tid)
        self.assertIn("\n  Add hello.txt\n", shown)
        self.assertIn(cut, shown)
        self.assertIn("  said 0\n", shown)
        self.assertIn("  said 69\n", shown)  # the report whole: it is what show is run for
        self.assertNotIn("more lines; the whole report", shown)
        self.assertNotIn(str(self.desk.tasks_dir), shown)
        full = self.hansa("show", tid, "--full")
        self.assertIn("  line 12\n", full)
        self.assertIn("  said 69\n", full)
        self.assertNotIn("more lines", full)
        self.assertEqual(self.hansa("show", tid, "--report").strip(), report.strip())  # the report and nothing else
        long = "".join(f"said {i}\n" for i in range(views.REPORT_LINES + 5))
        (self.desk.tasks_dir / tid / "result.md").write_text(long)
        shown = self.hansa("show", tid)
        self.assertIn(f"  ... 5 more lines; the whole report: hansa show {tid} --report", shown)
        self.assertNotIn(f"said {views.REPORT_LINES}\n", shown)
        self.assertEqual(self.hansa("show", tid, "--report").strip(), long.strip())
        wide = "".join("x" * 1000 + f" {i}\n" for i in range(60))  # few lines, but too long to show whole
        (self.desk.tasks_dir / tid / "result.md").write_text(wide)
        shown = self.hansa("show", tid)
        self.assertIn(f"more lines; the whole report: hansa show {tid} --report", shown)
        self.assertLess(len(shown), views.REPORT_CHARS + 5000)
        self.assertEqual(self.hansa("show", tid, "--report").strip(), wide.strip())
        one = "y" * 50000  # one line alone longer than the limit: cut there too
        (self.desk.tasks_dir / tid / "result.md").write_text(one + "\n")
        shown = self.hansa("show", tid)
        self.assertIn("  " + "y" * views.REPORT_CHARS + "\n", shown)
        self.assertNotIn("y" * (views.REPORT_CHARS + 1), shown)
        self.assertIn(f"  ... {50000 - views.REPORT_CHARS} more characters of this line; the whole report: "
                      f"hansa show {tid} --report", shown)
        self.assertEqual(self.hansa("show", tid, "--report").strip(), one)
        (self.desk.tasks_dir / tid / "result.md").write_text(f"first\nsecond\n{one}\nfourth\n")
        shown = self.hansa("show", tid)
        self.assertIn("  first\n  second\n  yyy", shown)
        self.assertNotIn("fourth", shown)
        kept = views.REPORT_CHARS - len("first\nsecond\n")
        self.assertIn("  " + "y" * kept + "\n", shown)
        self.assertNotIn("y" * (kept + 1), shown)
        self.assertIn(f"  ... {50000 - kept} more characters of this line and 1 more line; the whole report: "
                      f"hansa show {tid} --report", shown)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            main(["add", "--help"])
        self.assertIn("It is read when the task is queued", " ".join(out.getvalue().split()))

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

    def test_hansas_commit_leaves_out_the_claude_folder_a_sandbox_left(self):
        from hansa import run
        s = self.desk.settings()
        s["lanes"]["a"]["env"] = {"FAKE_SANDBOX_LEFT": "1", "FAKE_WRITE": "worker's own", "FAKE_WRITE_FILE": ".claude/notes.md"}
        self.assertEqual(self.desk.save_settings(s), [])
        tid = self.desk.new_task("Add hello.txt")
        m = self.settle(Dispatcher(self.desk), tid)
        self.assertEqual((m["state"], m["files"]), ("done", [".claude/notes.md", "hello.txt"]), m)  # what has content
        wt = Path(m["worktree"])
        self.assertTrue((wt / ".claude" / ".cc-writes").exists())  # left where it was, uncommitted
        self.assertTrue((wt / ".bashrc").exists())
        run.commit_work(self.desk, wt, "nothing but placeholders")  # with only those there, nothing to commit
        self.assertEqual(self.git_out("rev-parse", "HEAD", cwd=wt), m["head"])
        (wt / ".gitconfig").write_text("[user]\n")  # one with content, or deeper down, is someone's work
        (wt / "sub").mkdir()
        (wt / "sub" / ".bashrc").write_text("")
        run.commit_work(self.desk, wt, "a real one")
        self.assertEqual(sorted(self.git_out("show", "--name-only", "--format=", "HEAD", cwd=wt).split()),
                         [".gitconfig", "sub/.bashrc"])
        for text in (run.PROTOCOL, run.VERIFY_PROTOCOL):  # workers are told each name, and to leave them
            flat = " ".join(text.split())
            for name in (*run.SANDBOX_PLACEHOLDERS, ".claude/"):
                self.assertIn(name, flat)
            self.assertIn("hansa never commits them", flat)

    def test_workers_are_told_git_stash_is_refused_and_background_work_ends_in_its_command(self):
        from hansa import run
        for text in (run.PROTOCOL, run.VERIFY_PROTOCOL):
            flat = " ".join(text.split())
            self.assertIn("`git stash` is refused, `git stash list` too, and one refused part refuses the whole "
                          "command line", flat)
            self.assertIn("`git show HEAD:path`", flat)
            self.assertIn("stop background work within the command that started it, or wait for it there", flat)
            text.format(tid="t1", lane="a", tmp="/tmp/x")  # still a template with nothing else to fill

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

    def hansa_git(self, *args):
        """(exit code, standard output, standard error) of hansa git ARGS."""
        out, err, code = io.StringIO(), io.StringIO(), 0
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                main(["--desk", str(self.desk.root), "git", *args])
            except SystemExit as e:
                code = e.code if isinstance(e.code, int) else 1
                err.write("" if isinstance(e.code, int) else str(e.code))
        return code, out.getvalue(), err.getvalue()

    def test_hansa_git_reads_a_lanes_repository_and_worktree_as_git_prints_them(self):
        key = "sk-test-0123456789"
        self.desk.write_key("mimo", key)
        s = self.desk.settings()
        s["lanes"]["a"]["env"] = {"FAKE_WRITE": key}
        self.assertEqual(self.desk.save_settings(s), [])
        tid = self.desk.new_task("Add hello.txt")
        m = self.settle(Dispatcher(self.desk), tid)
        self.assertEqual(self.hansa_git("a", "log", "--oneline", "-1"),
                         (0, self.git_out("log", "--oneline", "-1") + "\n", ""))
        code, out, _ = self.hansa_git("a", "show", f"task/{tid}", "--", "hello.txt")
        self.assertEqual((code, out), (0, self.git_out("show", f"task/{tid}", "--", "hello.txt") + "\n"))
        code, out, _ = self.hansa_git("a", "grep", "-n", "demo")
        self.assertEqual((code, out), (0, "README.md:1:demo\n"))
        both = (f"task/{tid}:hello.txt", "main:README.md")  # several files in one call, as the card says
        code, out, _ = self.hansa_git("a", "show", *both)
        self.assertEqual((code, out), (0, self.git_out("show", *both) + "\n"))
        self.assertIn("demo\n", out)
        self.assertIn(self.git_out("show", both[0]), out)
        code, out, _ = self.hansa_git("a", "grep", "-n", "-e", "demo", "-e", "nothing-here", "main", "--", ".")
        self.assertEqual((code, out), (0, "main:README.md:1:demo\n"))  # several things at once, as the card says
        code, out, _ = self.hansa_git("a", "show", f"task/{tid}:notes.txt")  # a stored key is never shown
        self.assertEqual(code, 0)
        self.assertNotIn(key, out)
        self.assertIn("[key of mimo]", out)
        (Path(m["worktree"]) / "scratch.txt").write_text("x\n")
        for args in (("a", "--worktree", "status", "--short"), ("--worktree", "a", "status", "--short")):
            self.assertEqual(self.hansa_git(*args), (0, "?? scratch.txt\n", ""))
        self.assertEqual(self.hansa_git("a", "status", "--short"), (0, "", ""))  # the repository, not the worktree
        self.assertEqual(self.hansa_git("a", "branch", "--list", "task/*")[:2], (0, f"+ task/{tid}\n"))  # in a worktree
        self.assertEqual(self.hansa_git("a", "log", "--bogus")[0], 128)  # git's own failure, as git ends
        code, _, err = self.hansa_git("z", "log")
        self.assertIn("no lane z", err)

    def hansa_notes(self, *args):
        """(exit code, standard output, what it said when it refused) of hansa notes ARGS."""
        out, code, err = io.StringIO(), 0, ""
        with contextlib.redirect_stdout(out):
            try:
                main(["--desk", str(self.desk.root), "notes", *args])
            except SystemExit as e:
                code, err = (e.code, "") if isinstance(e.code, int) else (1, str(e.code))
        return code, out.getvalue(), err

    NOTES = ("# Plan\nintro\n\n## Round 1\ndone one\n### Detail\nx\n```\n## in code, not a heading\n```\n\n"
             "## Round 2\nopen\n\n## Round 12\nlast\n")

    def notes_state(self):
        return {str(p.relative_to(self.desk.notes_dir)): p.read_bytes() for p in self.desk.notes_dir.rglob("*")
                if p.is_file()}

    def test_hansa_notes_lists_files_with_sizes_lines_and_headings(self):
        (self.desk.notes_dir / "plan.md").write_text(self.NOTES)
        (self.desk.notes_dir / "archive").mkdir()
        (self.desk.notes_dir / "archive" / "plan.md").write_text("## Round 0\nold\n")
        code, out, _ = self.hansa_notes()
        self.assertEqual(code, 0)
        self.assertIn(f"notes/plan.md  {len(self.NOTES)} bytes, 16 lines", out)
        self.assertRegex(out, r"\n +4 ## Round 1\n +6 ### Detail\n +12 ## Round 2\n +15 ## Round 12")
        self.assertNotIn("in code", out)
        self.assertIn("notes/archive/plan.md  15 bytes, 2 lines (headings not listed", out)
        self.assertNotIn("Round 0", out)
        self.assertIn("1 ## Round 0", self.hansa_notes("archive/plan.md")[1])

    def test_hansa_notes_archive_moves_a_section_with_its_subsections(self):
        src, dst = self.desk.notes_dir / "plan.md", self.desk.notes_dir / "archive" / "plan.md"
        src.write_text(self.NOTES)
        code, out, _ = self.hansa_notes("archive", "plan.md", "Round 1")  # in the middle, with a subsection
        self.assertEqual(code, 0, out)
        self.assertIn("lines 4-11", out)
        self.assertIn("made now", out)
        self.assertEqual(src.read_text(), "# Plan\nintro\n\n## Round 2\nopen\n\n## Round 12\nlast\n")
        moved = dst.read_text().splitlines()
        self.assertEqual(moved[0], "## Round 1")
        self.assertRegex(moved[1], r"moved here from notes/plan.md on \d{4}-\d\d-\d\d \d\d:\d\d")
        self.assertEqual(moved[2:], ["done one", "### Detail", "x", "```", "## in code, not a heading", "```", ""])
        # the whole heading wins over the longer one it starts; the last section; added to the file now there
        self.assertEqual(self.hansa_notes("archive", "plan.md", "## Round 12")[0], 0)
        self.assertEqual(src.read_text(), "# Plan\nintro\n\n## Round 2\nopen\n\n")
        self.assertIn("\n## Round 12\n", dst.read_text())
        self.assertTrue(dst.read_text().startswith("## Round 1\n"))
        self.assertTrue(dst.read_text().endswith("last\n"))
        # a prefix that matches one heading; --to another file, made with its folder
        self.assertEqual(self.hansa_notes("archive", "plan.md", "Round", "--to", "done/r2.md")[0], 0)
        self.assertTrue((self.desk.notes_dir / "done" / "r2.md").read_text().startswith("## Round 2\n"))
        self.assertEqual(src.read_text(), "# Plan\nintro\n\n")
        self.assertFalse(list(self.desk.notes_dir.rglob("*.hansa-tmp")))

    def test_hansa_notes_archive_writes_the_other_file_first(self):
        from hansa import notes
        src, dst = self.desk.notes_dir / "plan.md", self.desk.notes_dir / "archive" / "plan.md"
        src.write_text(self.NOTES)
        real, calls = notes._write, []

        def crash_second(p, data, mode):
            calls.append(p)
            if len(calls) == 2:
                raise KeyboardInterrupt  # stopped between the two
            real(p, data, mode)
        with unittest.mock.patch.object(notes, "_write", crash_second), self.assertRaises(KeyboardInterrupt):
            self.hansa_notes("archive", "plan.md", "Round 2")
        self.assertEqual(src.read_text(), self.NOTES)  # the section is in both files, not in neither
        self.assertIn("## Round 2\n", dst.read_text())

    def test_hansa_notes_archive_picks_a_headings_level_by_its_hashes(self):
        src = self.desk.notes_dir / "plan.md"
        src.write_text("# Round 1\ntop\n## Round 1\nsub\n# Round 2\nnext\n")
        code, _, err = self.hansa_notes("archive", "plan.md", "Round 1")
        self.assertEqual(code, 1)
        self.assertIn("2 headings in notes/plan.md match 'Round 1'", err)
        code, out, _ = self.hansa_notes("archive", "plan.md", "## Round 1")
        self.assertEqual(code, 0, out)
        self.assertEqual(src.read_text(), "# Round 1\ntop\n# Round 2\nnext\n")
        self.assertTrue((self.desk.notes_dir / "archive" / "plan.md").read_text().startswith("## Round 1\n"))
        self.assertEqual(self.hansa_notes("archive", "plan.md", "# Round")[0], 1)  # two at level 1 start so
        self.assertEqual(self.hansa_notes("archive", "plan.md", "#Round 1")[0], 0)
        self.assertEqual(src.read_text(), "# Round 2\nnext\n")

    def test_hansa_notes_archive_refuses_and_changes_nothing(self):
        (self.desk.notes_dir / "plan.md").write_text(self.NOTES)
        (self.desk.notes_dir / "twice.md").write_text("# Todo\na\n# Todo\nb\n")
        (self.desk.notes_dir / "archive").mkdir()
        (self.desk.notes_dir / "archive" / "plan.md").write_text("## Round 0\n")
        outside = self.tmp / "outside"
        outside.mkdir()
        (self.desk.notes_dir / "out").symlink_to(outside)
        (self.desk.notes_dir / "out.md").symlink_to(self.tmp / "elsewhere.md")
        before = self.notes_state()
        for args, says in ((["plan.md", "Round 3"], "no heading in notes/plan.md matches 'Round 3'"),
                           (["plan.md", "Round"], "3 headings in notes/plan.md match 'Round'"),
                           (["twice.md", "Todo"], "2 headings in notes/twice.md match 'Todo'"),
                           (["plan.md", "in code"], "no heading"),
                           (["../settings.json", "Plan"], "../settings.json is refused"),
                           ([str(self.desk.notes_dir / "plan.md"), "Plan"], "is refused"),
                           (["plan.md", "Round 1", "--to", "../x.md"], "../x.md is refused"),
                           (["plan.md", "Round 1", "--to", str(self.tmp / "x.md")], "is refused"),
                           (["plan.md", "Round 1", "--to", "out/x.md"], "a link that leads out of notes/"),
                           (["plan.md", "Round 1", "--to", "out.md"], "a link that leads out of notes/"),
                           (["out/a.md", "Round 1"], "a link that leads out of notes/"),
                           (["missing.md", "Round 1"], "there is no file notes/missing.md"),
                           (["plan.md", "Round 1", "--to", "plan.md"], "the file it is in"),
                           (["plan.md", "Round 1", "--to", "twice.md/z"], "notes/twice.md is a file"),
                           (["plan.md", "Round 1", "--to", "twice.md/y/z.md"], "notes/twice.md is a file"),
                           (["plan.md", "### Round 1"], "no heading in notes/plan.md matches '### Round 1'")):
            code, _, err = self.hansa_notes("archive", *args)
            self.assertEqual(code, 1, args)
            self.assertIn(says, err, args)
            self.assertIn("othing was changed", err, args)
            self.assertEqual(self.notes_state(), before, args)
        self.assertFalse((self.tmp / "x.md").exists())
        self.assertEqual(list(outside.iterdir()), [])
        err = self.hansa_notes("archive", "plan.md", "Round")[2]
        self.assertRegex(err, r"12 ## Round 2\n +15 ## Round 12$")  # the headings it matched, to pick from
        err = self.hansa_notes("archive", "plan.md", "Nothing")[2]
        self.assertIn("4 ## Round 1", err)  # all the file's headings

    def test_hansa_git_refuses_commands_and_options_that_write_or_run_and_changes_nothing(self):
        tid = self.desk.new_task("Add hello.txt")
        m = self.settle(Dispatcher(self.desk), tid)
        wt = Path(m["worktree"])

        def state():
            return (self.git_out("for-each-ref"), self.git_out("status", "--porcelain", "--ignored"),
                    self.git_out("status", "--porcelain", "--ignored", cwd=wt), sorted(os.listdir(self.repo)),
                    sorted(os.listdir(wt)), sorted(os.listdir(self.tmp)))
        before = state()
        here = os.getcwd()
        os.chdir(self.tmp)
        self.addCleanup(os.chdir, here)
        for args, says in ((["commit", "--allow-empty", "-m", "x"], "git commit is not one hansa git runs"),
                           (["push"], "git push is not one"), (["checkout", f"task/{tid}"], "git checkout is not one"),
                           (["branch", "-D", f"task/{tid}"], "git branch -D is refused"),
                           (["branch", "new"], "would make a branch"), (["tag", "v1"], "would make a tag"),
                           (["tag", "-d", "v1"], "git tag -d is refused"),
                           (["diff", "--output=x"], "--output=x is refused"), (["diff", "--outp=x"], "refused"),
                           (["log", "--output", "x"], "refused"), (["grep", "-O", "demo"], "-O is refused"),
                           (["grep", "-iOvim", "demo"], "refused"),
                           (["grep", "--open-files-in-pager", "demo"], "refused"), (["diff", "--ext-diff"], "refused"),
                           (["cat-file", "--textconv", "HEAD:README.md"], "refused"),
                           (["diff", "--no-index", "/etc/hosts", "README.md"], "--no-index is refused"),
                           (["grep", "--no-index", "demo"], "--no-index is refused"),
                           (["grep", "--no-ind", "demo"], "outside the lane's repository"),
                           (["diff", "/etc/hosts", "README.md"], "/etc/hosts is refused"),
                           (["diff", "README.md", "../x"], "outside the lane's repository"),
                           (["blame", "--contents", "/etc/hosts", "README.md"], "--contents is refused"),
                           (["blame", "--cont=/etc/hosts", "README.md"], "outside the lane's repository"),
                           (["blame", "-S", "/etc/hosts", "README.md"], "-S is refused"),
                           (["blame", "-wS/etc/hosts", "README.md"], "outside the lane's repository"),
                           (["blame", "--ignore-revs-file", "/etc/hosts", "README.md"], "--ignore-revs-file is refused"),
                           (["blame", "--ignore-revs-file=/etc/hosts", "README.md"], "outside the lane's repository"),
                           (["blame", "--ignore-revs=/etc/hosts", "README.md"], "outside the lane's repository"),
                           (["log", "--show-signature"], "--show-signature is refused"),
                           (["show", "--show-sig", "HEAD"], "it would run gpg"),
                           (["-c", "core.pager=x", "log"], "-c before the git command is refused"),
                           (["--exec-path=x", "log"], "refused"), (["-C", str(self.tmp), "log"], "refused"),
                           ([], "name a")):
            for where in ([], ["--worktree"]):
                code, out, err = self.hansa_git("a", *where, *args)
                self.assertEqual((code, out), (1, ""), args)
                self.assertIn(says, err, args)
                self.assertIn("Nothing was run", err)
        self.assertIn("code changes go through workers", self.hansa_git("a", "commit")[2])
        self.assertEqual(state(), before)
        self.assertFalse((self.tmp / "x").exists())
        for args in (["branch"], ["branch", "-a", "-v"], ["branch", "--contains", "HEAD"], ["tag"], ["tag", "-l", "v*"],
                     ["tag", "-n"], ["grep", "-c", "demo"], ["ls-files", "-o"], ["diff", "--output-indicator-new=+"],
                     ["diff", "main..HEAD"], ["diff", "HEAD", "--", "README.md"], ["blame", "-s", "-L1,1", "README.md"],
                     ["blame", "--ignore-rev", "HEAD", "README.md"]):
            self.assertEqual(self.hansa_git("a", *args)[0], 0, args)
        self.assertIn("hansa git a", views.explain_text(self.desk, "lane", "a"))

    def test_hansa_git_runs_no_program_the_repositorys_own_config_names(self):
        made = {}
        for what, key in (("external", "diff.external"), ("fsmonitor", "core.fsmonitor"),
                          ("textconv", "diff.conv.textconv")):
            made[what] = self.tmp / f"{what}-ran"
            script = self.tmp / f"{what}.sh"
            script.write_text(f"#!/bin/sh\ntouch '{made[what]}'\ncat \"$1\" 2>/dev/null\n")
            script.chmod(0o755)
            sh("git", "config", key, str(script), cwd=self.repo)
        (self.repo / ".git" / "info").mkdir(exist_ok=True)
        (self.repo / ".git" / "info" / "attributes").write_text("*.md diff=conv\n")
        (self.repo / "README.md").write_text("changed\n")
        for args in (["diff"], ["diff", "HEAD"], ["status"], ["status", "--short"], ["log", "-p", "-1"],
                     ["show", "HEAD"], ["blame", "README.md"], ["grep", "-n", "demo", "HEAD"]):
            self.assertEqual(self.hansa_git("a", *args)[0], 0, args)
        self.assertEqual([w for w, p in made.items() if p.exists()], [])
        self.assertIn("+changed", self.hansa_git("a", "diff")[1])  # git's own diff, not the configured program's

    def test_hansa_git_runs_no_gpg_for_a_signed_commit(self):
        made = self.tmp / "gpg-ran"
        script = self.tmp / "gpg.sh"
        script.write_text(f"#!/bin/sh\ntouch '{made}'\n")
        script.chmod(0o755)
        sh("git", "config", "gpg.program", str(script), cwd=self.repo)
        sh("git", "config", "log.showSignature", "true", cwd=self.repo)
        tree, head = self.git_out("rev-parse", "HEAD^{tree}"), self.git_out("rev-parse", "HEAD")
        commit = (f"tree {tree}\nparent {head}\n"
                  "author A <a@example.com> 1700000000 +0000\ncommitter A <a@example.com> 1700000000 +0000\n"
                  "gpgsig -----BEGIN PGP SIGNATURE-----\n \n iQEzBAABCAAdFiEE\n -----END PGP SIGNATURE-----\n\nsigned\n")
        signed = subprocess.run(["git", "hash-object", "-t", "commit", "-w", "--stdin"], cwd=self.repo, input=commit,
                                capture_output=True, text=True, check=True).stdout.strip()
        sh("git", "update-ref", "refs/heads/signed", signed, cwd=self.repo)
        for args in (["log", "-1", "signed"], ["log", "--format=%s", "signed"], ["show", "signed"]):
            code, out, _ = self.hansa_git("a", *args)
            self.assertEqual(code, 0, args)
            self.assertIn("signed", out)
        self.assertFalse(made.exists())

    def test_hansa_git_runs_no_signature_program_for_a_format_that_asks_for_the_signature(self):
        tree, head = self.git_out("rev-parse", "HEAD^{tree}"), self.git_out("rev-parse", "HEAD")
        (self.tmp / "allowed").write_text("")
        sh("git", "config", "gpg.ssh.allowedSignersFile", str(self.tmp / "allowed"), cwd=self.repo)
        made = {}
        # git writes the signed text to the signature program; a text larger than a pipe holds, written to a program
        # that ended at once (false), killed git with SIGPIPE every time, and a short one now and then
        body = "".join(f"line {n} of a long message\n" for n in range(12000))
        for kind, key, begin in (("openpgp", "gpg.program", "PGP SIGNATURE"), ("ssh", "gpg.ssh.program", "SSH SIGNATURE"),
                                 ("x509", "gpg.x509.program", "SIGNED MESSAGE")):
            made[kind] = self.tmp / f"{kind}-ran"
            script = self.tmp / f"{kind}.sh"
            script.write_text(f"#!/bin/sh\ntouch '{made[kind]}'\n")
            script.chmod(0o755)
            sh("git", "config", key, str(script), cwd=self.repo)
            for name, message in ((kind, "signed\n"), (f"{kind}-long", f"signed\n\n{body}")):
                commit = (f"tree {tree}\nparent {head}\n"
                          "author A <a@example.com> 1700000000 +0000\ncommitter A <a@example.com> 1700000000 +0000\n"
                          f"gpgsig -----BEGIN {begin}-----\n \n iQEzBAABCAAdFiEE\n -----END {begin}-----\n\n{message}")
                signed = subprocess.run(["git", "hash-object", "-t", "commit", "-w", "--stdin"], cwd=self.repo,
                                        input=commit, capture_output=True, text=True, check=True).stdout.strip()
                sh("git", "update-ref", f"refs/heads/{name}", signed, cwd=self.repo)
                for _ in range(5):
                    for args in (["log", "--format=%G? %s", name], ["show", "-s", "--format=%GS %GK", name]):
                        code, _, err = self.hansa_git("a", *args)
                        self.assertEqual(code, 0, (args, err))
        self.assertEqual([k for k, p in made.items() if p.exists()], [])

    def test_hansa_git_runs_as_the_desks_account_with_its_arguments_as_they_are(self):
        log, ran = self.stand_in_runuser()
        self.as_root()
        git_args = ["grep", "-e", "--file=y", "--or", "-e", "--repo", "--", "README.md"]  # hansa's options, not git's
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as done:
            main(["--desk", str(self.desk.root), "git", "a", *git_args])
        self.assertEqual(done.exception.code, 1)  # git grep found neither: the account's hansa ended as git did
        me = account.name_of(os.getuid())
        args = json.loads(log.read_text().splitlines()[-1])
        self.assertEqual(args, ["-u", me, "--", sys.executable, "-m", "hansa", f"--desk={self.desk.root}", "git", "a",
                                *git_args])
        self.assertEqual((ran[-1].stdout, ran[-1].stderr), ("", ""))

    @unittest.skipUnless(sys.platform.startswith("linux") and os.getuid() == 0, "needs root on Linux")
    def test_as_root_hansa_git_reads_an_accounts_repository_as_the_account(self):
        user, src = "nobody", Path(__file__).resolve().parent.parent
        pw = account.pwd.getpwnam(user)
        if not account.can_enter(src / "hansa", user):
            self.skipTest(f"{user} cannot read this hansa at {src}")
        os.chmod(self.tmp, 0o755)
        for d, _, files in os.walk(self.repo):
            for f in [d, *(os.path.join(d, x) for x in files)]:
                os.chown(f, pw.pw_uid, pw.pw_gid)
        desk, env = self.tmp / "account-desk", {**os.environ, "PYTHONPATH": str(src)}
        hansa = [sys.executable, "-m", "hansa"]
        r = subprocess.run([*hansa, "init", str(desk), "--repo", str(self.repo), "--user", user],
                           capture_output=True, text=True, cwd=str(self.tmp), env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = subprocess.run([*hansa, "--desk", str(desk), "git", "a", "log", "--format=%s"],
                           capture_output=True, text=True, cwd=str(self.tmp), env=env)
        self.assertEqual((r.returncode, r.stdout), (0, "init\n"), r.stderr)  # git refuses root itself there
        r = subprocess.run([*hansa, "--desk", str(desk), "git", "a", "commit", "-m", "x"],
                           capture_output=True, text=True, cwd=str(self.tmp), env=env)
        self.assertIn("code changes go through workers", r.stderr)

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

    def orphan_starting(self, **fields):
        """A worker an earlier server started, and a task it left in `starting`: (task, worker)."""
        token = f"hansa-launch-test-{uuid.uuid4().hex[:8]}"  # another suite running meanwhile has its own
        claimed = time.time()  # as a server claims a task before it starts the worker
        orphan = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", token], start_new_session=True)
        self.addCleanup(lambda: orphan.poll() is None and (orphan.kill(), orphan.wait()))
        tid = self.desk.new_task("Add hello.txt")
        self.desk.update(tid, state="running", phase="starting", lane="a", provider="claude", started=claimed,
                         pid=None, launch=token, **fields)
        return tid, orphan

    def test_a_worker_started_just_before_the_server_stopped_is_taken_over(self):
        tid, orphan = self.orphan_starting()
        Dispatcher(self.desk).tick()
        m = self.desk.meta(tid)
        self.assertEqual((m["state"], m["phase"], m["pid"]), ("running", "", orphan.pid), m)
        self.assertIsNone(orphan.poll())  # still working, not stopped and started over

    def test_a_test_leaves_neither_its_folder_nor_its_environment(self):
        before = dict(os.environ)
        case = DeskTest("test_a_task_that_ends_after_its_check_is_posted_once_to_its_session")  # a worker, a follower
        result = unittest.TestResult()
        case.run(result)
        self.assertEqual((result.testsRun, result.errors, result.failures), (1, [], []))
        self.assertNotEqual(case.tmp, self.tmp)
        self.assertFalse(case.tmp.exists())
        time.sleep(1.5)  # a follower ends a moment after its session: none is left to write there again
        self.assertFalse(case.tmp.exists())
        self.assertEqual(dict(os.environ), before)

    def test_a_worker_is_taken_over_every_time_while_processes_start_and_end_around_it(self):
        stop = threading.Event()

        def churn():  # short-lived processes, as on a busy machine
            while not stop.is_set():
                subprocess.run(["/bin/sleep", "0.01"])
        churners = [threading.Thread(target=churn, daemon=True) for _ in range(3)]
        for t in churners:
            t.start()
        self.addCleanup(lambda: (stop.set(), [t.join() for t in churners]))
        for i in range(20):
            tid, orphan = self.orphan_starting()
            Dispatcher(self.desk).tick()
            m = self.desk.meta(tid)
            self.assertEqual((m["state"], m["phase"], m["pid"]), ("running", "", orphan.pid), (i, m))
            self.assertIsNone(orphan.poll())
            orphan.kill()
            orphan.wait()
            self.desk.update(tid, state="failed", phase="", pid=None)  # out of the way of the next one

    def test_while_the_process_list_cannot_be_read_a_starting_task_is_left_as_it_is(self):
        from hansa import run
        tid, orphan = self.orphan_starting()
        d = Dispatcher(self.desk)
        with unittest.mock.patch.object(run, "pids_with_arg", side_effect=run.CannotLook("the process list could not be read")):
            d.tick()
            d.tick()
        m = self.desk.meta(tid)
        self.assertEqual((m["state"], m["phase"], m["pid"]), ("running", "starting", None), m)  # not queued, not started
        self.assertTrue(m["lookup_failing_since"])
        self.assertFalse(self.desk.raw_dir(tid).joinpath("run.json").exists())  # no second worker
        failed = [e for e in map(json.loads, self.desk.events_path.read_text().splitlines())
                  if e["event"] == "process_lookup_failed"]
        self.assertEqual([(e["task"], e["looking_for"]) for e in failed], [(tid, "worker")] * 2)
        d.tick()  # the list reads again
        m = self.desk.meta(tid)
        self.assertEqual((m["state"], m["phase"], m["pid"], m["lookup_failing_since"]), ("running", "", orphan.pid, None), m)
        self.assertIsNone(orphan.poll())

    def test_a_task_hansa_cannot_look_for_for_minutes_fails_and_is_not_started_again(self):
        from hansa import run
        tid, orphan = self.orphan_starting(lookup_failing_since=time.time() - run.LOOK_LIMIT - 1)
        with unittest.mock.patch.object(run, "pids_with_arg", side_effect=run.CannotLook("the process list could not be read")):
            Dispatcher(self.desk).tick()
        m = self.desk.meta(tid)
        self.assertEqual((m["state"], m["phase"], m["pid"]), ("failed", "", None), m)
        self.assertIn("could not read the process list for 5 minutes", m["error"])
        self.assertFalse(self.desk.raw_dir(tid).joinpath("run.json").exists())

    def test_a_process_whose_arguments_cannot_be_read_is_not_taken_for_gone(self):
        from hansa import run
        if not run._ON_MAC:
            self.skipTest("Linux reads /proc")
        tid, orphan = self.orphan_starting()
        real = run._mac_procargs
        with unittest.mock.patch.object(run, "_mac_procargs", side_effect=lambda p: None if p == orphan.pid else real(p)), \
                unittest.mock.patch.object(run.time, "sleep"), self.assertRaises(run.CannotLook):
            run.pids_with_arg(self.desk.meta(tid)["launch"])
        self.assertEqual(run.pids_with_arg(self.desk.meta(tid)["launch"]), [orphan.pid])

    def test_a_process_read_half_way_through_an_exec_is_read_again(self):
        from hansa import run
        if not run._ON_MAC:
            self.skipTest("Linux reads /proc")
        tid, orphan = self.orphan_starting()
        real, halves = run._mac_procargs, [([b"ython3", b"", b""], [], False), ([b""], [], False)]  # as mid-exec
        with unittest.mock.patch.object(run, "_mac_procargs",
                                        side_effect=lambda p: halves.pop(0) if p == orphan.pid and halves else real(p)), \
                unittest.mock.patch.object(run.time, "sleep"):
            self.assertEqual(run.pids_with_arg(self.desk.meta(tid)["launch"]), [orphan.pid])

    def test_a_process_never_read_whole_is_not_the_worker_and_does_not_stop_the_lookup(self):
        from hansa import run
        if not run._ON_MAC:
            self.skipTest("Linux reads /proc")
        tid, orphan = self.orphan_starting()
        other = subprocess.Popen(["/bin/sleep", "60"], start_new_session=True)
        self.addCleanup(lambda: other.poll() is None and (other.kill(), other.wait()))
        real, reads = run._mac_procargs, {orphan.pid: 0, other.pid: 0}

        def procargs(p):  # the worker half written once; the other half written, and differently, every time
            if p not in reads:
                return real(p)
            reads[p] += 1
            if p == other.pid:
                return [b"x" * reads[p]], [], False
            return ([b"ython3", b"", b""], [], False) if reads[p] == 1 else real(p)
        with unittest.mock.patch.object(run, "_mac_procargs", side_effect=procargs), \
                unittest.mock.patch.object(run.time, "sleep"):
            self.assertEqual(run.pids_with_arg(self.desk.meta(tid)["launch"], self.desk.meta(tid)["started"]),
                             [orphan.pid])
        self.assertEqual(reads[other.pid], 1 + run.LOOK_TRIES)

    def test_a_process_read_alike_twice_is_taken_as_it_reads_after_the_second_read(self):
        from hansa import run
        if not run._ON_MAC:
            self.skipTest("Linux reads /proc")
        tid, orphan = self.orphan_starting()
        # an empty first argument reads as half written every time
        other = subprocess.Popen(["perl", "-e", 'exec {"/bin/sleep"} "", 60'], start_new_session=True)
        self.addCleanup(lambda: other.poll() is None and (other.kill(), other.wait()))
        end = time.time() + 10
        while (run._mac_procargs(other.pid) or [[b"perl"]])[0][:1] != [b"60"] and time.time() < end:
            time.sleep(0.02)
        real, reads = run._mac_procargs, []
        with unittest.mock.patch.object(run, "_mac_procargs", side_effect=lambda p: (reads.append(p), real(p))[1]):
            m = self.desk.meta(tid)
            self.assertEqual(run.pids_with_arg(m["launch"], m["started"]), [orphan.pid])
        self.assertEqual(reads.count(other.pid), 2)

    def test_whole_arguments_are_told_from_half_written_ones(self):
        from hansa import run
        if not run._ON_MAC:
            self.skipTest("Linux reads /proc")
        tid, orphan = self.orphan_starting()
        end = time.time() + 10
        while orphan.pid not in run.pids_with_arg(self.desk.meta(tid)["launch"]) and time.time() < end:
            time.sleep(0.05)
        data = run._sysctl(run._CTL_KERN, run._KERN_PROCARGS2, orphan.pid)
        argv, _, whole = run._mac_parse_args(data)  # the first argument is the Python the executable ran
        self.assertEqual((argv[1:], whole), ([b"-c", b"import time; time.sleep(60)", self.desk.meta(tid)["launch"].encode()],
                                             True))
        path = b"/Applications/Xcode.app/Contents/Developer/usr/bin/python3"  # 58 bytes: 6 NULs to 64
        argv = [path, b"-c", b"import time; time.sleep(3)", b"tok"]
        read = struct.pack("@i", 4) + path + b"\0" * 6 + b"\0".join(argv + [b"HOME=/h", b"", b""]) + b"\0" * 40
        self.assertEqual(run._mac_parse_args(read), (argv, [b"HOME=/h"], True))
        # a process that rewrote its own arguments, as postgres does: whole
        title = struct.pack("@i", 3) + b"/opt/pg/bin/postgres" + b"\0" * 4 + b"postgres: walwriter " + b"\0" * 40
        self.assertEqual(run._mac_parse_args(title), ([b"postgres: walwriter ", b"", b""], [], True))
        # as macOS gave them in the middle of an exec: all NULs, and a NUL in the path
        self.assertFalse(run._mac_parse_args(struct.pack("@i", 4) + b"\0" * 200)[2])
        cut = read[:4 + 51] + b"\0" + read[4 + 52:]
        self.assertEqual(run._mac_parse_args(cut)[0], [b"ython3", b"", b"", b""])
        self.assertFalse(run._mac_parse_args(cut)[2])

    def test_a_process_that_keeps_execing_itself_does_not_stop_the_lookup(self):
        from hansa import run
        tid, orphan = self.orphan_starting()
        again = ("import os, sys, time; time.sleep(0.01); "
                 "os.execv(sys.executable, [sys.executable, '-c', sys.argv[1], sys.argv[1], str(int(sys.argv[2]) + 1)])")
        other = subprocess.Popen([sys.executable, "-c", again, again, "0"], start_new_session=True)
        self.addCleanup(lambda: other.poll() is None and (other.kill(), other.wait()))
        m, real, first = self.desk.meta(tid), run._mac_procargs, []

        def procargs(p):  # the worker is read half written the first time in each lookup, as mid-exec
            if p == orphan.pid and not first:
                first.append(p)
                return [b"ython3", b"", b""], [], False
            return real(p)
        with unittest.mock.patch.object(run, "_mac_procargs", side_effect=procargs):
            for _ in range(20):
                first.clear()
                self.assertEqual(run.pids_with_arg(m["launch"], m["started"]), [orphan.pid])
        self.assertIsNone(other.poll())

    def unreadable_beside(self, change):
        """On macOS: a worker left in `starting`, and beside it a process of this account that leads its
        own group and never lets its arguments be read, whose kinfo_proc record `change` alters as it is
        read: (launch word, claim time, worker)."""
        from hansa import run
        if not run._ON_MAC:
            self.skipTest("Linux reads /proc")
        other = subprocess.Popen(["/bin/sleep", "60"], start_new_session=True)
        self.addCleanup(lambda: other.poll() is None and (other.kill(), other.wait()))
        tid, orphan = self.orphan_starting()
        real_sysctl, real_args = run._sysctl, run._mac_procargs

        def sysctl(*mib, **kw):
            data = real_sysctl(*mib, **kw)
            if data and mib[1:3] == (run._KERN_PROC, run._KERN_PROC_ALL):
                size, recs = run._KINFO_PROC_SIZE, bytearray(data)
                for i in range(0, len(recs) - size + 1, size):
                    if struct.unpack_from("@i", recs, i + 40)[0] == other.pid:
                        change(recs, i)
                data = bytes(recs)
            return data
        patches = [unittest.mock.patch.object(run, "_sysctl", side_effect=sysctl),
                   unittest.mock.patch.object(run, "_mac_procargs", side_effect=lambda p: None if p == other.pid else real_args(p)),
                   unittest.mock.patch.object(run.time, "sleep")]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        m = self.desk.meta(tid)
        return m["launch"], m["started"], orphan

    def test_an_unreadable_process_older_than_the_claim_does_not_stop_the_lookup(self):
        from hansa import run
        token, claimed, orphan = self.unreadable_beside(lambda recs, i: struct.pack_into("@l", recs, i, int(claimed) - 60))
        with self.assertRaises(run.CannotLook):  # were it as new as the claim, it might be the worker
            run.pids_with_arg(token)
        self.assertEqual(run.pids_with_arg(token, claimed), [orphan.pid])

    def test_an_unreadable_setuid_process_does_not_stop_the_lookup(self):
        from hansa import run
        token, claimed, orphan = self.unreadable_beside(lambda recs, i: struct.pack_into("@I", recs, i + 420, 0))
        self.assertEqual(run.pids_with_arg(token, claimed), [orphan.pid])  # root as its effective user: sudo, say

    def test_an_unreadable_process_as_new_as_the_claim_still_stops_the_lookup(self):
        from hansa import run
        token, claimed, orphan = self.unreadable_beside(lambda recs, i: None)
        with self.assertRaises(run.CannotLook):
            run.pids_with_arg(token, claimed)

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

    def leave_detached(self):
        """Have the stand-in worker leave a process running in a session of its own; the file that gets its pid."""
        from hansa.run import alive
        pidfile = self.tmp / "detached.pid"
        s = self.desk.settings()
        s["lanes"]["a"]["env"] = {"FAKE_LEAVE_DETACHED": str(pidfile)}
        self.assertEqual(self.desk.save_settings(s), [])
        self.addCleanup(lambda: pidfile.exists() and alive(int(pidfile.read_text())) and
                        os.kill(int(pidfile.read_text()), 9))
        return pidfile

    def test_what_a_worker_leaves_running_in_a_session_of_its_own_is_stopped_and_listed(self):
        from hansa.run import alive
        pidfile = self.leave_detached()
        tid = self.desk.new_task("Add hello.txt")
        m = self.settle(Dispatcher(self.desk), tid)
        self.assertEqual(m["state"], "done", m)
        child = int(pidfile.read_text())
        self.assertFalse(alive(child))  # stopped before the task was recorded as ended
        [x] = m["left_running"]
        self.assertEqual(x["pid"], child)
        self.assertIn("python", x["command"].lower())
        self.assertLessEqual(len(x["command"]), 120)
        self.assertIn(f"pid {child}: ", views.show_text(self.desk, tid))
        [e] = [e for e in map(json.loads, self.desk.events_path.read_text().splitlines())
               if e["event"] == "left_running_stopped"]
        self.assertEqual((e["task"], len(e["processes"])), (tid, 1))

    def test_a_process_naming_another_task_or_only_like_this_one_is_left_alone(self):
        from hansa.run import task_processes
        pidfile = self.leave_detached()
        tid = self.desk.new_task("Add hello.txt")
        bare = {k: v for k, v in os.environ.items() if k != "HANSA_TASK"}
        others = [self.sleeper(env={**bare, "HANSA_TASK": "t9999"}),
                  self.sleeper(env={**bare, "HANSA_TASK": tid + "0"}),
                  self.sleeper(env={**bare, "OLD_HANSA_TASK": tid, "HANSA_TASKS": tid})]
        m = self.settle(Dispatcher(self.desk), tid)
        self.assertEqual([x["pid"] for x in m["left_running"]], [int(pidfile.read_text())])
        self.assertEqual([p.poll() for p in others], [None, None, None])
        self.assertEqual([p for p, _ in task_processes("t9999")], [others[0].pid])  # found, when it is that task's

    def test_what_is_left_running_without_hansa_task_is_found_by_its_runs_own_folder(self):
        from hansa.run import alive
        pids = self.tmp / "left"
        pids.mkdir()
        s = self.desk.settings()
        s["lanes"]["a"]["env"] = {"FAKE_LEAVE_IN_TMP": str(pids)}
        self.assertEqual(self.desk.save_settings(s), [])
        self.addCleanup(lambda: [alive(int(f.read_text())) and os.kill(int(f.read_text()), 9) for f in pids.iterdir()])
        tid = self.desk.new_task("Add hello.txt")
        m = self.settle(Dispatcher(self.desk), tid)
        self.assertEqual(m["state"], "done", m)
        pid = {f.stem: int(f.read_text()) for f in pids.iterdir()}
        self.assertEqual(sorted(x["pid"] for x in m["left_running"]), sorted([pid["script"], pid["tmpdir"]]))
        self.assertFalse(alive(pid["script"]) or alive(pid["tmpdir"]))
        self.assertTrue(alive(pid["near"]) and alive(pid["other"]))  # /tmp/hansa-<id>-<random>2, another run's
        [x] = [x for x in m["left_running"] if x["pid"] == pid["script"]]
        self.assertIn(f"{m['worker_tmps'][0]['path']}/load.sh", x["command"])
        shown = views.show_text(self.desk, tid)
        self.assertIn(f"pid {pid['script']}: ", shown)
        self.assertIn(f"pid {pid['tmpdir']}: ", shown)

    def test_a_verification_and_a_design_session_are_swept_at_their_end_too(self):
        from hansa import run
        tid = self.desk.new_task("Make the badge bigger")
        self.desk.update(tid, lane="a", provider="claude", worktree=str(self.repo), session_id="sid-d",
                         model_asked="sonnet", kind="design")
        _, env, _ = run.design_command(self.desk, tid)
        tmp = self.desk.meta(tid)["worker_tmp"]
        self.assertEqual(env["CLAUDE_CODE_TMPDIR"], tmp)  # a folder of its own, as a worker's
        self.assertEqual([r["path"] for r in self.desk.meta(tid)["worker_tmps"]], [tmp])
        bare = {k: v for k, v in os.environ.items() if k != "HANSA_TASK"}
        left = self.sleeper(env={**bare, "TMPDIR": os.path.join(tmp, "claude-501")})
        other = self.sleeper(env={**bare, "TMPDIR": tmp + "2"})
        with unittest.mock.patch("hansa.run.end_design"):
            m = run.finish(self.desk, tid, 0)
        self.assertEqual([x["pid"] for x in m["left_running"]], [left.pid])
        self.assertIsNotNone(left.poll())
        self.assertIsNone(other.poll())
        self.assertFalse(os.path.exists(tmp))
        vid = self.desk.new_task("Check the badge")
        self.desk.update(vid, lane="a", provider="claude", worktree=str(self.repo), kind="verify",
                         worker_tmps=[{"path": tmp + "v", "removed": True}])
        left = self.sleeper(tmp + "v/load.sh", env=bare)
        m = run.finish(self.desk, vid, 0)
        self.assertEqual([x["pid"] for x in m["left_running"]], [left.pid])

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

    def two_lanes(self, check="", names=("a.txt", "b.txt")):
        """Lanes a and b on the one repository, each worker writing a file of its own (a.txt, b.txt), and one
        finished task on each, both made from the same base."""
        s = self.desk.settings()
        s["lanes"]["b"] = json.loads(json.dumps(s["lanes"]["a"]))
        s["lanes"]["a"].update(env={"FAKE_HELLO": names[0]}, check=check)
        s["lanes"]["b"].update(env={"FAKE_HELLO": names[1]}, check=check)
        self.assertEqual(self.desk.save_settings(s), [])
        d = Dispatcher(self.desk)
        t1, t2 = self.desk.new_task(f"Add {names[0]}", lane="a"), self.desk.new_task(f"Add {names[1]}", lane="b")
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
        self.assertEqual((setup["argv"][-1], check["argv"][-1]),  # the lane's env is set inside the sandbox
                         ("exec /usr/bin/env FAKE_HELLO=b.txt /bin/sh -c 'echo set up for the merge'",
                          "exec /usr/bin/env FAKE_HELLO=b.txt /bin/sh -c 'echo merged badly; exit 3'"))
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

    def move_main(self, repo=None):
        sh("git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "moved",
           cwd=repo or self.repo)
        return self.git_out("rev-parse", "main", cwd=repo)

    def test_merge_checks_on_one_repository_run_one_after_the_other_in_the_order_asked(self):
        d, t1, t2 = self.two_lanes(check="test -f README.md")
        s = self.desk.settings()
        s["concurrency"] = 2
        self.assertEqual(self.desk.save_settings(s), [])
        tip = self.move_main()  # so both need a merge check
        self.hansa("merge", t1)
        out = self.hansa("merge", t2)
        self.assertIn(f"One merge check runs at a time on main, so it waits for {t1}'s merge check on lane a", out)
        d.tick()
        self.assertEqual(self.desk.meta(t1)["merge_check"]["phase"], "running")
        self.assertEqual(self.desk.meta(t2)["merge_check"]["phase"], "queued")  # lane b is free, but it waits
        waits = f"merge check queued: waits for {t1}'s merge check on lane a"
        self.assertIn(waits, views.show_text(self.desk, t2))
        self.assertIn(waits, views.status_text(views.status(self.desk)))
        self.assertEqual(views.status(self.desk)["merging"][1]["waits_for"], {"id": t1, "lane": "a"})
        self.assertIn(f"waits for {t1}'s merge check on lane a", notify.task_line(self.desk, self.desk.meta(t2)))
        m1 = self.settle_merge(d, t1)
        m2 = self.settle_merge(d, t2)
        self.assertEqual((m1["state"], m2["state"]), ("merged", "merged"), (m1, m2))
        self.assertEqual(m1["merge_check_result"]["onto"], tip)
        self.assertEqual(m2["merge_check_result"]["onto"], m1["merge_commit"])  # checked on the base t1 left
        self.assertEqual(self.git_out("rev-parse", "main"), m2["merge_commit"])
        self.assertEqual(self.events("merge_check_base_moved"), [])  # neither was checked for nothing
        started = [e["task"] for e in self.events("merge_check_started")]
        self.assertEqual(started, [t1, t2])

    def test_a_merge_check_on_another_repository_is_not_held_up(self):
        d, t1, t2 = self.two_lanes(check="test -f README.md")
        other = self.tmp / "other"
        other.mkdir()
        sh("git", "init", "-q", "-b", "main", cwd=other)
        (other / "README.md").write_text("other\n")
        sh("git", "add", ".", cwd=other)
        sh("git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init", cwd=other)
        s = self.desk.settings()
        s["lanes"]["c"] = {**json.loads(json.dumps(s["lanes"]["a"])), "repo": str(other)}
        s["concurrency"] = 3
        self.assertEqual(self.desk.save_settings(s), [])
        t3 = self.desk.new_task("Add a.txt there", lane="c")
        self.assertEqual(self.settle(d, t3)["state"], "done")
        self.move_main()
        self.move_main(other)
        for t in (t1, t2, t3):
            self.hansa("merge", t)
        d.tick()
        phases = {t: self.desk.meta(t)["merge_check"]["phase"] for t in (t1, t2, t3)}
        self.assertEqual(phases, {t1: "running", t2: "queued", t3: "running"})
        for t in (t1, t2, t3):
            self.assertEqual(self.settle_merge(d, t)["state"], "merged")

    def test_a_merge_needing_no_check_waits_its_turn_behind_a_merge_check(self):
        d, t1, t2 = self.two_lanes(check="sleep 1; test -f README.md")
        s = self.desk.settings()
        s["lanes"]["b"]["check"] = ""  # t2 needs no check
        s["concurrency"] = 2
        self.assertEqual(self.desk.save_settings(s), [])
        self.move_main()  # so t1 needs a merge check
        self.hansa("merge", t1)
        d.tick()
        self.assertEqual(self.desk.meta(t1)["merge_check"]["phase"], "running")
        out = self.hansa("merge", t2)
        self.assertIn(f"{t2} is not merged yet", out)
        self.assertIn(f"so it waits for {t1}'s merge check on lane a", out)
        self.assertIn(f"hansa wait {t2}", out)
        m2 = self.desk.meta(t2)
        self.assertEqual((m2["state"], m2["merge_check"]["phase"], m2["merge_check"]["plain"]), ("merging", "queued", True))
        waits = f"merge queued, to go in without a check if main has still not moved: waits for {t1}'s merge check on lane a"
        self.assertIn(f"{t2} b: Add b.txt\n      {waits}", views.status_text(views.status(self.desk)))
        self.assertIn(waits, views.show_text(self.desk, t2))
        self.assertIn(f"its merge is queued and waits for {t1}'s merge check", notify.task_line(self.desk, m2))
        d.tick()
        self.assertEqual(self.desk.meta(t2)["state"], "merging")  # t1's check still runs
        self.assertFalse((self.repo / "b.txt").exists())
        m1 = self.settle_merge(d, t1)
        m2 = self.settle_merge(d, t2)
        self.assertEqual((m1["state"], m2["state"]), ("merged", "merged"), (m1, m2))
        self.assertEqual(self.git_out("rev-parse", "main"), m2["merge_commit"])
        self.assertEqual(self.git_out("rev-list", "--parents", "-n", "1", "main").split()[1:], [m1["merge_commit"], m2["head"]])
        self.assertEqual(self.events("merge_check_base_moved"), [])  # t1's check was not run again
        self.assertEqual([e["task"] for e in self.events("merge_check_started")], [t1])
        merged = [e for e in self.events("task_merged") if e["task"] == t2]
        self.assertTrue(merged[0]["waited"])
        self.assertIn(f"{t2} merged into main in its turn", notify._item(self.desk, merged[0])[1])

    def test_a_merge_that_waited_its_turn_and_was_refused_says_no_check_ran(self):
        d, t1, t2 = self.two_lanes(check="sleep 1; test -f README.md")
        s = self.desk.settings()
        s["lanes"]["b"]["check"] = ""  # t2 needs no check, and its lane has none
        s["concurrency"] = 2
        self.assertEqual(self.desk.save_settings(s), [])
        self.move_main()  # so t1 needs a merge check
        self.hansa("merge", t1)
        d.tick()
        self.hansa("merge", t2)
        sh("git", "checkout", "-q", "-b", "elsewhere", cwd=self.repo)  # the principal switched the checkout meanwhile
        self.settle_merge(d, t1)
        m2 = self.settle_merge(d, t2)
        self.assertEqual((m2["state"], m2["merge_check_result"]["waited"]), ("done", True), m2)
        says = "it waited its turn (it needed no check, and none ran), then was not merged: the checkout at"
        shown = views.show_text(self.desk, t2)
        self.assertIn(f"merge: {says}", shown)
        self.assertIn("is on elsewhere, not main", shown)
        self.assertNotIn("ran the lane's check there", shown)
        self.assertNotIn("FAILED", shown)
        self.assertNotIn("it checks the merge again", shown)
        self.assertIn(f"hansa merge {t2} again", shown)
        status = views.status_text(views.status(self.desk))
        self.assertIn(f"merge: {says}", status)
        self.assertNotIn("merge check FAILED", status.split(f"{t2} b:")[1].splitlines()[0])
        ended = [e for e in self.events("merge_check_ended") if e["task"] == t2]
        post = notify._item(self.desk, ended[0])[1]
        self.assertIn(f"{t2}'s merge: {says}", post)
        self.assertIn(f"hansa merge {t2} again", post)

    def test_a_merge_that_waited_is_checked_when_the_base_moved_meanwhile(self):
        d, t1, _ = self.two_lanes(check="sleep 1; test -f README.md")
        self.move_main()  # so t1 needs a merge check
        t3 = self.desk.new_task("Add b.txt again", lane="b")  # made on the moved base: needs no check now
        self.assertEqual(self.settle(d, t3)["state"], "done")
        self.hansa("merge", t1)
        d.tick()
        self.assertIn(f"so it waits for {t1}'s merge check on lane a", self.hansa("merge", t3))
        m1 = self.settle_merge(d, t1)
        m3 = self.settle_merge(d, t3)
        self.assertEqual((m1["state"], m3["state"], m3["merge_check_result"]["ok"]), ("merged", "merged", True))
        self.assertEqual(m3["merge_check_result"]["onto"], m1["merge_commit"])  # checked on the base t1 left
        self.assertEqual([e["task"] for e in self.events("merge_check_started")], [t1, t3])
        self.assertEqual(self.events("merge_check_base_moved"), [])

    def test_a_merge_needing_no_check_on_another_repository_is_not_held_up(self):
        d, t1, _ = self.two_lanes(check="sleep 1; test -f README.md")
        other = self.tmp / "other"
        other.mkdir()
        sh("git", "init", "-q", "-b", "main", cwd=other)
        (other / "README.md").write_text("other\n")
        sh("git", "add", ".", cwd=other)
        sh("git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init", cwd=other)
        s = self.desk.settings()
        s["lanes"]["c"] = {**json.loads(json.dumps(s["lanes"]["a"])), "repo": str(other)}
        self.assertEqual(self.desk.save_settings(s), [])
        t3 = self.desk.new_task("Add a.txt there", lane="c")
        self.assertEqual(self.settle(d, t3)["state"], "done")
        self.move_main()
        self.hansa("merge", t1)
        d.tick()
        self.assertEqual(self.desk.meta(t1)["merge_check"]["phase"], "running")
        self.assertIn(f"{t3} merged into main", self.hansa("merge", t3))
        self.assertEqual(self.desk.meta(t3)["state"], "merged")
        self.assertEqual(self.settle_merge(d, t1)["state"], "merged")

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

    def recheck_after_a_failed_check(self, check):
        """A task whose check failed ("exit 1"), on a base that has not moved, then the lane's check set to
        `check` (the machine fixed, say) and hansa merge --recheck on a server."""
        s = self.desk.settings()
        s["lanes"]["a"]["check"] = "exit 1"
        self.assertEqual(self.desk.save_settings(s), [])
        d = Dispatcher(self.desk)
        tid = self.desk.new_task("Add hello.txt")
        self.assertEqual(self.settle(d, tid)["check"]["ok"], False)
        s = self.desk.settings()
        s["lanes"]["a"]["check"] = check
        self.assertEqual(self.desk.save_settings(s), [])
        tip, calls = self.git_out("rev-parse", "main"), len(self.srt_calls())
        with unittest.mock.patch.dict(os.environ, {"HANSA_ROLE": "coordinator"}):
            out = self.hansa("merge", tid, "--recheck")
        self.assertIn(f"{tid} is not merged yet: hansa runs the lane's check again", out)
        self.assertEqual(self.desk.meta(tid)["state"], "merging")
        m = self.settle_merge(d, tid)
        self.assertEqual([c["argv"][-1] for c in self.srt_calls()[calls:]], [check])  # it ran again
        [queued] = self.events("check_rerun_queued")
        self.assertEqual((queued["task"], queued["by"]), (tid, "coordinator"))
        [checked] = self.events("merge_checked")
        self.assertEqual((checked["recheck"], checked["by"]), (True, "coordinator"))
        return tid, tip, m

    def test_recheck_runs_the_check_again_on_an_unmoved_base_and_merges_when_it_passes(self):
        tid, tip, m = self.recheck_after_a_failed_check("test -f hello.txt")
        self.assertEqual((m["state"], m["merge_check_result"]["ok"]), ("merged", True), m)
        self.assertEqual(self.git_out("rev-parse", "main"), m["merge_check_result"]["commit"])
        self.assertEqual(self.git_out("rev-list", "--parents", "-n", "1", "main").split()[1:], [tip, m["head"]])
        self.assertTrue((self.repo / "hello.txt").exists())
        self.assertTrue(self.events("task_merged")[-1]["recheck"])

    def test_recheck_that_fails_merges_nothing(self):
        tid, tip, m = self.recheck_after_a_failed_check("echo still broken; exit 2")
        self.assertEqual((m["state"], m["merge_check_result"]["ok"], m["merge_check_result"]["exit"]), ("done", False, 2))
        self.assertEqual(self.git_out("rev-parse", "main"), tip)
        self.assertFalse((self.repo / "hello.txt").exists())
        self.assertIn("still broken", views.show_text(self.desk, tid))

    def test_a_merge_check_tells_a_session_that_hears_from_hansa_that_it_will_be_told(self):
        s = self.desk.settings()
        s["lanes"]["a"]["check"] = "true"
        self.assertEqual(self.desk.save_settings(s), [])
        tid = self.desk.new_task("Add hello.txt")
        self.settle(Dispatcher(self.desk), tid)
        p, sock = self.session()
        with self.in_session(p, sock):  # no follower: it waits, as before
            out = self.hansa("merge", tid, "--recheck")
            self.assertIn(f"Wait for it: hansa wait {tid} (hansa show {tid} says where it is", out)
            self.assertIn(f"next: hansa wait {tid}: it merges by itself", views.show_text(self.desk, tid))
            self.assertIn("hansa wait <id>", views.status_text(views.status(self.desk)))
            with self.assertRaises(SystemExit) as e:
                main(["--desk", str(self.desk.root), "merge", tid])
            self.assertIn(f"Wait for it: hansa wait {tid}", str(e.exception.code))
        self.hansa("cancel", tid)
        self.hearing(p)
        with self.in_session(p, sock):
            out = self.hansa("merge", tid, "--recheck")
            self.assertIn(f"hansa tells this session when {tid} ends (hansa show {tid} says where it is", out)
            self.assertNotIn("hansa wait", out)
            shown = views.show_text(self.desk, tid)
            self.assertIn(f"next: hansa tells this session when {tid} ends: it merges by itself", shown)
            self.assertNotIn("hansa wait", shown)
            self.assertIn("hansa tells this session when each ends", views.status_text(views.status(self.desk)))
            with self.assertRaises(SystemExit) as e:
                main(["--desk", str(self.desk.root), "merge", tid])
            self.assertIn(f"hansa tells this session when {tid} ends", str(e.exception.code))
            self.assertNotIn("hansa wait", str(e.exception.code))

    def test_status_counts_the_last_24_hours_each_thing_by_when_it_happened(self):
        now, h = time.time(), 3600
        across = self.desk.new_task("finished before midnight, merged after it")
        self.desk.update(across, state="merged", finished=now - 2 * h, merged=now - 10 * 60)
        old = self.desk.new_task("finished 25 h ago")
        self.desk.update(old, state="done", finished=now - 25 * h,
                         usage={"m": {"in": 1000, "out": 1000, "cache_read": 0, "cache_write": 0}})
        ran = self.desk.new_task("its run ended 23 h ago")
        self.desk.update(ran, state="done", finished=now - 23 * h,
                         usage={"m": {"in": 5, "out": 7, "cache_read": 0, "cache_write": 0}})
        long_merged = self.desk.new_task("finished and merged 30 h ago")
        self.desk.update(long_merged, state="merged", finished=now - 31 * h, merged=now - 30 * h)
        # a merge counts by when it was merged, not when its task finished: these fail at any hour if it did
        late = self.desk.new_task("finished 25 h ago, merged 10 min ago")
        self.desk.update(late, state="merged", finished=now - 25 * h, merged=now - 10 * 60)
        just_out = self.desk.new_task("finished 26 h ago, merged 25 h ago")
        self.desk.update(just_out, state="merged", finished=now - 26 * h, merged=now - 25 * h)
        t = views.status(self.desk)["last_24h"]
        self.assertEqual((t["finished"], t["merged"], t["worker_tokens"]), (2, 2, 12))
        self.assertIn("last 24 h: 2 finished, 2 merged; workers used 12 tokens",
                      views.status_text(views.status(self.desk)))

    def test_lane_list_lines_up_its_columns(self):
        s = self.desk.settings()
        s["lanes"]["shop-mimo-cpp"] = {**s["lanes"]["a"], "base": "dev-v2"}
        self.assertEqual(self.desk.save_settings(s), [])
        rows = self.hansa("lane").splitlines()[:-1]
        self.assertEqual(len(rows), 2)
        for word in (str(self.repo), "base ", "providers "):
            self.assertEqual(len({r.index(word) for r in rows}), 1, rows)

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

    def test_lane_add_says_how_the_new_lane_differs_from_the_others_on_its_repository(self):
        out = self.hansa("lane", "add", "b", "--repo", str(self.repo))
        self.assertNotIn("differs", out)  # the same as lane a
        self.assertNotIn("try it", out)
        self.hansa("lane", "set", "a", "network", "localhost")
        self.hansa("lane", "set", "b", "network", "localhost")
        self.hansa("lane", "set", "b", "timeout_min", "60")
        self.hansa("lane", "set", "a", "env", "PORT_BASE=4000")
        self.hansa("lane", "set", "a", "write", str(self.tmp / "cache"))
        other = self.tmp / "other-repo"
        other.mkdir()
        self.git_out("init", "-q", "-b", "main", cwd=other)
        self.git_out("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "init", cwd=other)
        self.assertNotIn("differs", self.hansa("lane", "add", "o", "--repo", str(other)))  # alone on its repository
        before = self.desk.settings()["lanes"]
        out = self.hansa("lane", "add", "c", "--repo", str(self.repo))
        self.assertIn(f"Lane c differs from the other lanes on {self.repo.resolve()}; nothing was copied", out)
        self.assertIn('  network: c has []; a, b have ["localhost"]\n', out)
        self.assertIn(f'  write: c has []; a has {json.dumps([str(self.tmp / "cache")])}\n', out)
        self.assertIn('  env: c has {}; a has {"PORT_BASE": "4000"}\n', out)
        self.assertIn("  timeout_min: c has 240; b has 60\n", out)
        self.assertNotIn("max_turns", out)
        self.assertTrue(out.endswith("try it: hansa lane try c\n"), out)
        c = self.desk.settings()["lanes"]["c"]
        self.assertEqual((c["network"], c["env"], c["timeout_min"], c.get("write", [])), ([], {}, 240, []))
        self.assertEqual({k: v for k, v in self.desk.settings()["lanes"].items() if k != "c"}, before)

    def test_lane_add_like_copies_another_lane_and_says_what_was_changed(self):
        self.hansa("lane", "set", "a", "check", "make test && make lint")
        self.hansa("lane", "set", "a", "setup", "make deps")
        self.hansa("lane", "set", "a", "network", "registry.npmjs.org")
        self.hansa("lane", "set", "a", "env", "PORT_BASE=4000")
        self.hansa("lane", "set", "a", "write", str(self.tmp / "cache"))
        self.hansa("lane", "set", "a", "timeout_min", "90")
        self.hansa("lane", "brief", "a", "Use C++20.")
        s = self.desk.settings()
        s["providers"]["mimo"] = {**s["providers"]["claude"]}
        self.assertEqual(self.desk.save_settings(s), [])
        a = self.desk.settings()["lanes"]["a"]
        before = len(self.events("lane_command_changed"))
        out = self.hansa("lane", "add", "m", "--like", "a", "--providers", "mimo")
        m = self.desk.settings()["lanes"]["m"]
        self.assertEqual(m, {**a, "providers": ["mimo"]})
        self.assertEqual((self.desk.lanes_dir / "m" / "brief.md").read_text(), (self.desk.lanes_dir / "a" /
                                                                                 "brief.md").read_text())
        self.assertIn("Use C++20.", (self.desk.lanes_dir / "m" / "brief.md").read_text())
        self.assertIn("Lane m is a copy of lane a as it is now; later changes to a do not reach it.", out)
        self.assertIn('Changed from a:\n  providers: m has ["mimo"]; a has ["claude"]\n', out)
        self.assertNotIn("differs from the other lanes", out)  # a is the only other lane, and it was copied
        self.assertNotIn("untested", out)
        self.assertIn("they are recorded", out)
        self.assertIn('registry.npmjs.org', out)
        self.assertTrue(out.endswith("try it: hansa lane try m\n"), out)
        self.assertEqual(sorted(e["field"] for e in self.events("lane_command_changed")[before:]), ["check", "setup"])
        self.assertEqual(self.events("lane_added")[-1].get("like"), "a")
        self.hansa("lane", "set", "a", "check", "make check")  # a copy: a's later changes do not reach m
        self.assertEqual(self.desk.settings()["lanes"]["m"]["check"], "make test && make lint")
        out = self.hansa("lane", "add", "n", "--like", "a", "--check", "none", "--network", "none")
        n = self.desk.settings()["lanes"]["n"]
        self.assertEqual((n.get("check"), n["network"], n["setup"]), (None, [], "make deps"))
        self.assertIn("Lane n has no check", out)
        self.assertIn('  network: n has []; a has ["registry.npmjs.org"]\n', out)
        self.assertIn("Lane n differs from the other lanes on", out)  # from m, which was not copied
        self.assertIn("nothing was copied from them but a", out)
        out = self.hansa("lane", "add", "p", "--like", "m")
        self.assertIn("Nothing was changed from it.", out)
        lanes = self.desk.settings()["lanes"]
        for extra, said in ((["--like", "z"], "there is no lane z to copy"),
                            (["--like", "a", "--repo", str(self.repo)], "cannot come with --repo"),
                            ([], "--repo PATH"), (["--like", "a", "--providers", "none"], "needs a provider")):
            with self.assertRaises(SystemExit) as e:
                main(["--desk", str(self.desk.root), "lane", "add", "q", *extra])
            self.assertIn(said, str(e.exception))
        self.assertEqual(self.desk.settings()["lanes"], lanes)
        with contextlib.redirect_stdout(io.StringIO()) as helped, self.assertRaises(SystemExit):
            main(["lane", "add", "--help"])
        self.assertIn("later changes to LANE do not reach the new lane", " ".join(helped.getvalue().split()))

    def test_lane_add_like_refuses_an_empty_repo_and_sets_an_earlier_brief_aside(self):
        with self.assertRaises(SystemExit) as e:
            main(["--desk", str(self.desk.root), "lane", "add", "q", "--like", "a", "--repo", ""])
        self.assertIn("cannot come with --repo", str(e.exception))
        self.assertNotIn("q", self.desk.settings()["lanes"])
        self.hansa("lane", "brief", "a", "Use C++20.")
        (self.desk.lanes_dir / "q").mkdir(parents=True)
        (self.desk.lanes_dir / "q" / "brief.md").write_text("Left by an earlier q.\n")
        out = self.hansa("lane", "add", "q", "--like", "a")
        self.assertEqual((self.desk.lanes_dir / "q" / "brief.md").read_text(), "Use C++20.")
        kept = list((self.desk.lanes_dir / "q").glob("brief-before-*.md"))
        self.assertEqual([k.read_text() for k in kept], ["Left by an earlier q.\n"])
        self.assertIn(f"the earlier ones are kept in {kept[0]}", out)
        self.hansa("lane", "remove", "q")
        out = self.hansa("lane", "add", "q", "--like", "a")  # the same text: nothing to set aside
        self.assertNotIn("kept in", out)
        self.assertEqual(len(list((self.desk.lanes_dir / "q").glob("brief-before-*.md"))), 1)

    def run_on(self, lane: str) -> Path:
        """Run one task on the lane and cancel it (no review waits); the lane's worktree."""
        tid = self.desk.new_task("Add hello.txt", lane=lane)
        self.assertEqual(self.settle(Dispatcher(self.desk), tid)["state"], "done")
        self.desk.update(tid, state="cancelled")
        wt = self.desk.worktrees_dir / lane
        self.assertTrue((wt / ".git").is_file())
        return wt

    def worktrees(self) -> str:
        return subprocess.run(["git", "worktree", "list"], cwd=self.repo, capture_output=True, text=True).stdout

    def test_lane_remove_removes_its_worktree_as_git_does(self):
        wt = self.run_on("a")
        (wt / ".bashrc").write_text("")  # a sandbox's placeholder: nobody's work
        out = self.hansa("lane", "remove", "a")
        self.assertNotIn("a", self.desk.settings()["lanes"])
        self.assertFalse(os.path.lexists(wt))
        self.assertNotIn(str(wt.resolve()), self.worktrees())
        self.assertIn(f"Its worktree {wt} is removed, with git worktree remove", out)
        self.assertNotIn("dropped", out)
        self.assertNotIn(str(wt), json.loads((self.desk.state_dir / "worktrees.json").read_text()))
        self.assertEqual(self.events("lane_worktree_removed")[-1]["worktree"], str(wt))

    def test_lane_remove_keeps_work_no_branch_holds_until_told(self):
        wt = self.run_on("a")
        sh("git", "checkout", "-q", "--detach", cwd=wt)
        (wt / "loose.txt").write_text("loose\n")
        sh("git", "add", "loose.txt", cwd=wt)
        sh("git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "on no branch", cwd=wt)
        (wt / "README.md").write_text("changed\n")
        (wt / "new.txt").write_text("new\n")
        with self.assertRaises(SystemExit) as e:
            main(["--desk", str(self.desk.root), "lane", "remove", "a"])
        said = str(e.exception.code)
        self.assertIn("lane a was not removed", said)
        self.assertIn("not committed: README.md, new.txt", said)
        self.assertIn("on no branch: ", said)
        self.assertIn(" on no branch\n", said)
        self.assertIn("hansa lane remove a --keep", said)
        self.assertIn("hansa lane remove a --discard", said)
        self.assertIn("a", self.desk.settings()["lanes"])
        self.assertTrue((wt / "new.txt").exists())
        with self.assertRaises(SystemExit) as e:
            main(["--desk", str(self.desk.root), "lane", "remove", "a", "--keep", "--discard"])
        self.assertIn("choose one", str(e.exception.code))
        out = self.hansa("lane", "remove", "a", "--keep")
        self.assertFalse(wt.exists())
        branch = re.search(r"on branch (hansa-kept/a-\S+) there", out).group(1)
        files = subprocess.run(["git", "show", "--format=", "--name-only", branch], cwd=self.repo,
                               capture_output=True, text=True).stdout.split()
        self.assertEqual(sorted(files), ["README.md", "new.txt"])
        shown = subprocess.run(["git", "show", f"{branch}~1:loose.txt"], cwd=self.repo, capture_output=True, text=True)
        self.assertEqual(shown.stdout, "loose\n")

    def test_lane_remove_clears_the_worktree_of_a_lane_already_gone(self):
        self.hansa("lane", "add", "b", "--like", "a")
        wt = self.run_on("b")
        s = self.desk.settings()  # removed as lane remove did before it removed worktrees
        del s["lanes"]["b"]
        self.assertEqual(self.desk.save_settings(s), [])
        (wt / "new.txt").write_text("new\n")
        with self.assertRaises(SystemExit) as e:
            main(["--desk", str(self.desk.root), "lane", "remove", "b"])
        self.assertIn("the worktree of lane b was not removed", str(e.exception.code))
        self.assertIn("not committed: new.txt", str(e.exception.code))
        self.assertTrue(wt.exists())
        tid = self.desk.new_task("Something for b", lane="b")
        with self.assertRaises(SystemExit) as e:
            main(["--desk", str(self.desk.root), "lane", "remove", "b", "--discard"])
        self.assertIn(f"lane b, which is gone, still has {tid} queued", str(e.exception.code))
        self.desk.update(tid, state="cancelled")
        out = self.hansa("lane", "remove", "b", "--discard")
        self.assertNotIn("lane b removed", out)
        self.assertIn("what it held that no branch kept is dropped", out)
        self.assertFalse(wt.exists())
        self.assertNotIn(str(wt.resolve()), self.worktrees())
        self.assertIn("a", self.desk.settings()["lanes"])
        with self.assertRaises(SystemExit) as e:
            main(["--desk", str(self.desk.root), "lane", "remove", "b"])
        self.assertIn("there is no lane b, and no worktree of one", str(e.exception.code))

    def test_lane_remove_leaves_a_worktree_whose_git_file_was_changed(self):
        wt = self.run_on("a")
        (wt / ".git").write_text(f"gitdir: {self.tmp}\n")
        out = self.hansa("lane", "remove", "a")
        self.assertNotIn("a", self.desk.settings()["lanes"])
        self.assertIn(f"Its worktree {wt} stays: the worktree's .git file was changed", out)
        self.assertTrue(wt.exists())

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
        held = cli.hold_server_lock(self.desk)  # as hansa serve holds it: it says the server runs
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
            held.close()
        said = said.getvalue()
        self.assertEqual(e.exception.code, 1)
        self.assertIn("set up for the try", said)
        self.assertIn("checked badly", said)
        self.assertIn("exit 3", said)
        self.assertIn("exit 0", said)  # the setup's
        tried_srt, setup, check = self.srt_calls(tries=True)
        self.assertEqual(tried_srt["argv"][-2:], ["--", "true"])  # srt is tried first, in the lane's sandbox
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

    def test_a_lane_try_no_server_started_is_withdrawn_when_the_command_gives_up(self):
        from types import SimpleNamespace
        from hansa import cli, run
        self.hansa("lane", "set", "a", "check", "echo checked")
        clock = [1000.0]
        fake_time = SimpleNamespace(time=lambda: clock[0], sleep=lambda s: clock.__setitem__(0, clock[0] + 30))
        up = {"state": "up", "pid": 4321, "quiet": 0, "answers": True}
        for later in ("gone", "stuck"):  # a stuck server runs no rounds: it neither starts nor ends the try
            for phase in ("queued", "running"):
                sts = iter([up])  # up when the try is asked for, then gone or stuck before it starts
                then = {"state": later, "pid": 4321 if later == "stuck" else None, "quiet": 400, "answers": True}
                if phase == "running":  # a try that started is left to the server
                    run.write_try(self.desk, "a", {"id": "t1", "phase": "running", "asked": time.time()})
                with unittest.mock.patch.object(cli, "server_state", lambda desk: next(sts, then)), \
                        unittest.mock.patch.object(cli, "time", fake_time), \
                        contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as e:
                    main(["--desk", str(self.desk.root), "lane", "try", "a"])
                said = str(e.exception.code)
                self.assertEqual("busy or stuck" in said, later == "stuck", said)
                if phase == "queued":
                    self.assertIn("so it was withdrawn and lane a is free again; nothing was run", said)
                    self.assertFalse(run.try_file(self.desk, "a").exists())
                    self.assertEqual(run.tried_lanes(self.desk), set())
                else:
                    self.assertIn("waits for this try's result", said)
                    self.assertEqual(run.read_try(self.desk, "a")["phase"], "running")
                    run.try_file(self.desk, "a").unlink()
        self.assertEqual(len(self.events("lane_try_withdrawn")), 2)

    def test_lane_try_against_a_stuck_server_runs_nothing(self):
        from hansa import run
        self.hansa("lane", "set", "a", "check", "echo checked")
        held = cli.hold_server_lock(self.desk)
        self.addCleanup(held.close)
        self.round_at(run.STUCK_AFTER + 60)
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as e:
            main(["--desk", str(self.desk.root), "lane", "try", "a"])
        self.assertIn(f"hansa serve (pid {os.getpid()}) is busy or stuck: no round finished for 6 minutes",
                      str(e.exception.code))
        self.assertIn("nothing was run", str(e.exception.code))
        self.assertFalse(run.try_file(self.desk, "a").exists())

    def test_lane_try_holds_the_lane_and_says_could_not_run_when_srt_cannot_start(self):
        from hansa import run
        ran = self.tmp / "check-ran"
        self.hansa("lane", "set", "a", "check", f"touch {ran}")
        run.write_try(self.desk, "a", {"id": "t1", "phase": "queued", "asked": time.time()})
        row = next(ln for ln in views.status_text(views.status(self.desk)).splitlines() if ln.startswith("  a "))
        self.assertIn("held by a try", row)
        broken = self.tmp / "nonode"  # srt is a node script; this PATH finds it but has no node
        broken.mkdir()
        (broken / "srt").write_text("#!/bin/sh\necho 'env: node: No such file or directory' >&2\nexit 127\n")
        (broken / "srt").chmod(0o755)
        with unittest.mock.patch.dict(os.environ, {"PATH": f"{broken}{os.pathsep}{os.environ['PATH']}"}):
            Dispatcher(self.desk).tick()
        t = run.read_try(self.desk, "a")
        self.assertEqual((t["phase"], t["ok"], t["could_not_run"]), ("done", False, True), t)
        said = views.try_text("a", t)
        self.assertIn("could not run", said)
        self.assertIn("env: node: No such file or directory", said)
        self.assertIn("nothing was run", said)
        self.assertFalse(ran.exists())
        self.assertEqual(self.srt_calls(tries=True), [])
        self.assertEqual(run.tried_lanes(self.desk), set())
        self.assertNotIn("held by a try", views.status_text(views.status(self.desk)))

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

    def merge_check_left_running(self, seconds):
        """Two lanes' tasks, the first merged, and the second's merge check (a check that counts its runs in
        runs.txt and takes `seconds`) started by a server that is then gone."""
        runs = self.tmp / "runs.txt"
        d, t1, t2 = self.two_lanes(check="test -f README.md")
        s = self.desk.settings()
        s["lanes"]["b"]["check"] = f"echo run >> {runs}; sleep {seconds}"
        self.assertEqual(self.desk.save_settings(s), [])
        self.hansa("merge", t1)
        self.hansa("merge", t2)
        end = time.time() + 20
        while not runs.exists() and time.time() < end:
            d.tick()
            time.sleep(0.05)
        self.assertEqual(self.desk.meta(t2)["merge_check"]["phase"], "running")
        return d.merge_procs[t2], t2, runs

    def test_a_merge_check_still_running_is_taken_over_by_the_next_server(self):
        proc, t2, runs = self.merge_check_left_running(2)
        reaper = threading.Thread(target=proc.wait)  # as init does for a check whose server is gone
        reaper.start()
        self.addCleanup(reaper.join)
        d = Dispatcher(self.desk)
        d.tick()
        self.assertEqual(self.desk.meta(t2)["state"], "merging")  # left to run, not started again
        m = self.settle_merge(d, t2)
        self.assertEqual((m["state"], m["merge_check_result"]["ok"]), ("merged", True), m)
        self.assertEqual(self.git_out("rev-parse", "main"), m["merge_check_result"]["commit"])
        self.assertEqual(runs.read_text(), "run\n")

    def test_a_merge_check_that_ended_while_no_server_ran_is_finished_by_the_next(self):
        proc, t2, runs = self.merge_check_left_running(0)
        proc.wait()  # it wrote its exit code with no server there
        m = self.settle_merge(Dispatcher(self.desk), t2)
        self.assertEqual((m["state"], m["merge_check_result"]["ok"]), ("merged", True), m)
        self.assertEqual(self.git_out("rev-parse", "main"), m["merge_check_result"]["commit"])
        self.assertEqual(runs.read_text(), "run\n")

    def test_a_merge_check_marked_starting_but_never_started_is_started_again(self):
        runs = self.tmp / "runs.txt"
        d, t1, t2 = self.two_lanes(check=f"echo run >> {runs}")
        runs.unlink()  # the two tasks' own checks
        self.hansa("merge", t1)
        self.hansa("merge", t2)
        mc = self.desk.meta(t2)["merge_check"]  # a server stopped between marking it starting and starting it
        self.desk.update(t2, merge_check={**mc, "phase": "starting", "launch": f"hansa-merge-{t2}-{uuid.uuid4().hex[:8]}",
                                          "started": time.time()})
        m = self.settle_merge(Dispatcher(self.desk), t2)
        self.assertEqual((m["state"], m["merge_check_result"]["ok"]), ("merged", True), m)
        self.assertEqual(runs.read_text(), "run\n")
        self.assertEqual(len(self.events("merge_check_started")), 1)

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

    def test_diff_takes_paths_and_new_shows_only_what_a_follow_up_added(self):
        s = self.desk.settings()
        s["lanes"]["a"]["env"] = {"FAKE_WRITE": "some notes"}  # the worker writes hello.txt and notes.txt
        self.assertEqual(self.desk.save_settings(s), [])
        d = Dispatcher(self.desk)
        t1 = self.desk.new_task("Add hello.txt")
        self.settle(d, t1)
        only = self.hansa("diff", t1, "notes.txt")
        self.assertIn("+some notes", only)
        self.assertNotIn("hello.txt", only)
        stat = self.hansa("diff", t1, "notes.txt", "--stat")
        self.assertIn("notes.txt", stat)
        self.assertNotIn("hello.txt", stat)
        self.assertIn("(no changes in nothing.txt)", self.hansa("diff", t1, "nothing.txt"))
        self.assertIn(f"{t1} did not continue from an earlier attempt", self.hansa("diff", t1, "--new"))
        (self.repo / "base.txt").write_text("the base moved\n")  # the follow-up gets this merged in
        sh("git", "add", ".", cwd=self.repo)
        sh("git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "base moves", cwd=self.repo)
        self.hansa("retry", t1, "--update", "say it again")
        t2 = self.desk.meta(t1)["replaced_by"]
        m = self.settle(d, t2)
        self.assertEqual((m["state"], m["update_conflicts"]), ("done", []), m)
        self.assertIn("notes.txt", self.hansa("diff", t2))  # against the base: t1's work too
        new = self.hansa("diff", t2, "--new")
        self.assertIn(f"what {t2} added on top of {t1}'s last commit ({m['start_from'][:10]}), without the base", new)
        self.assertIn(f"+hello from {t2}", new)
        self.assertNotIn("notes.txt", new)  # t1's
        self.assertNotIn("base.txt", new)  # the base's
        self.assertIn("hello.txt", self.hansa("diff", t2, "--new", "--stat", "hello.txt"))
        self.assertNotIn("hello.txt", self.hansa("diff", t2, "--new", "notes.txt"))

    def test_conflict_markers_a_worker_leaves_are_shown_and_never_merged(self):
        d = Dispatcher(self.desk)
        t1, t2 = self.desk.new_task("Add hello.txt"), self.desk.new_task("Add hello.txt too")
        for t in (t1, t2):
            self.settle(d, t)
        self.hansa("merge", t1)
        s = self.desk.settings()  # the worker writes another file and leaves the conflict in hello.txt as it is
        s["lanes"]["a"]["env"] = {"FAKE_HELLO": "other.txt"}
        self.assertEqual(self.desk.save_settings(s), [])
        self.hansa("retry", t2, "--update", "keep the newer greeting")
        t3 = self.desk.meta(t2)["replaced_by"]
        m = self.settle(d, t3)
        self.assertEqual((m["state"], m["markers"]), ("done", {"hello.txt": [1, 3, 5]}), m)
        shown = views.show_text(self.desk, t3)
        self.assertIn("CONFLICT MARKERS IN THE CHANGE", shown)
        self.assertIn("hello.txt lines 1, 3, 5", shown)
        self.assertIn(f"next: hansa retry {t3} --update \"settle the markers in hello.txt\"", shown)
        self.assertIn("holds conflict markers in hello.txt: cannot be merged", views.status_text(views.status(self.desk)))
        tip = self.git_out("rev-parse", "main")
        for flags in ([], ["--despite-check"], ["--recheck"]):  # no flag overrides it
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as e:
                main(["--desk", str(self.desk.root), "merge", t3, *flags])
            said = str(e.exception.code)
            self.assertIn("holds git's conflict markers (hello.txt lines 1, 3, 5); nothing was merged", said)
            self.assertIn(f"hansa retry {t3} --update \"settle the markers in hello.txt\"", said)
        self.assertEqual((self.desk.meta(t3)["state"], self.git_out("rev-parse", "main")), ("done", tip))

    def marker_fixture_task(self) -> str:
        s = self.desk.settings()  # a test fixture that holds marker-like lines on purpose
        s["lanes"]["a"]["env"] = {"FAKE_WRITE": "<<<<<<< ours\nkept\n>>>>>>> theirs", "FAKE_WRITE_FILE": "fixture.txt"}
        self.assertEqual(self.desk.save_settings(s), [])
        tid = self.desk.new_task("Add a fixture")
        m = self.settle(Dispatcher(self.desk), tid)
        self.assertEqual((m["state"], m["markers"]), ("done", {"fixture.txt": [1, 3]}), m)
        return tid

    def test_markers_kept_on_purpose_have_a_way_out_through_gitattributes(self):
        tid = self.marker_fixture_task()
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as e:
            main(["--desk", str(self.desk.root), "merge", tid])
        self.assertIn("conflict-marker-size in the base's .gitattributes", str(e.exception.code))
        (self.repo / ".gitattributes").write_text("fixture.txt conflict-marker-size=100\n")
        sh("git", "add", ".gitattributes", cwd=self.repo)
        sh("git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "fixtures", cwd=self.repo)
        self.assertIn(f"{tid} merged into main", self.hansa("merge", tid))
        self.assertEqual(self.desk.meta(tid)["markers"], {})

    def test_show_looks_for_markers_in_a_task_that_ended_before_hansa_looked(self):
        tid = self.marker_fixture_task()
        f = self.desk.tasks_dir / tid / "meta.json"
        m = json.loads(f.read_text())
        del m["markers"]  # as a task that finished before the check existed
        f.write_text(json.dumps(m))
        shown = views.show_text(self.desk, tid)
        self.assertIn("CONFLICT MARKERS IN THE CHANGE", shown)
        self.assertIn("fixture.txt lines 1, 3", shown)
        self.assertIn(f"next: hansa retry {tid} --update \"settle the markers in fixture.txt\"", shown)
        self.assertNotIn("markers", self.desk.meta(tid))  # shown, not stored

    def test_a_markdown_heading_underline_is_not_a_conflict_marker(self):
        s = self.desk.settings()
        s["lanes"]["a"]["env"] = {"FAKE_WRITE": "Notes\n=======\n\nplain text", "FAKE_WRITE_FILE": "notes.md"}
        self.assertEqual(self.desk.save_settings(s), [])
        tid = self.desk.new_task("Write notes.md")
        m = self.settle(Dispatcher(self.desk), tid)
        self.assertEqual((m["state"], m["markers"]), ("done", {}), m)
        self.assertNotIn("CONFLICT MARKERS", views.show_text(self.desk, tid))
        self.assertIn(f"{tid} merged into main", self.hansa("merge", tid))
        self.assertEqual((self.repo / "notes.md").read_text(), "Notes\n=======\n\nplain text\n")

    def test_retry_options_may_come_before_or_after_the_note(self):
        tid = self.desk.new_task("Add hello.txt")
        self.settle(Dispatcher(self.desk), tid)
        self.assertIn("from the base branch", self.hansa("retry", tid, "--fresh", "the", "same", "change"))
        t2 = self.desk.meta(tid)["replaced_by"]
        self.assertIn("What to change: the same change", (self.desk.tasks_dir / t2 / "task.md").read_text())
        self.assertNotIn("start_from", self.desk.meta(t2))
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
            main(["--desk", str(self.desk.root), "merge", tid, "stray"])  # other commands still refuse extra words

    def test_a_retry_note_can_come_from_a_file_or_standard_input(self):
        d = Dispatcher(self.desk)
        tid = self.desk.new_task("Add hello.txt")
        self.settle(d, tid)
        note = self.tmp / "note.md"
        note.write_text("Make the greeting longer.\n\n- keep it on one line\n- say `hello` first\n")
        self.assertIn("from the base branch", self.hansa("retry", "--file", str(note), tid, "--fresh"))
        t2 = self.desk.meta(tid)["replaced_by"]
        text = (self.desk.tasks_dir / t2 / "task.md").read_text()
        self.assertIn("What to change: Make the greeting longer.\n\n- keep it on one line\n- say `hello` first", text)
        self.settle(d, t2)
        with unittest.mock.patch("sys.stdin", io.StringIO("Now in French, from standard input.")):
            self.assertIn("with the current main merged", self.hansa("retry", t2, "--update", "--file", "-"))
        t3 = self.desk.meta(t2)["replaced_by"]
        self.assertIn("What to change: Now in French, from standard input.",
                      (self.desk.tasks_dir / t3 / "task.md").read_text())
        self.settle(d, t3)
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
            main(["--desk", str(self.desk.root), "retry", t3, "words", "--file", str(note)])  # one or the other
        self.assertEqual(self.desk.meta(t3)["state"], "done")

    def test_a_file_that_is_missing_or_a_folder_is_said_in_one_line_and_nothing_changes(self):
        tid = self.desk.new_task("Add hello.txt")
        self.settle(Dispatcher(self.desk), tid)
        before = sorted(x["id"] for x in self.desk.tasks())
        for path, why in ((self.tmp / "no-such.md", "does not exist"), (self.tmp, "is a folder, not a file")):
            for argv in (["add", "--file", str(path)], ["retry", tid, "--file", str(path)]):
                with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as e:
                    main(["--desk", str(self.desk.root), *argv])
                self.assertEqual(str(e.exception.code), f"hansa: {path} {why}; nothing was changed")
        self.assertEqual(sorted(x["id"] for x in self.desk.tasks()), before)
        self.assertEqual(self.desk.meta(tid)["state"], "done")

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

    def test_a_second_server_waits_for_the_lock_by_the_clock_not_by_counting_sleeps(self):
        held = cli.hold_server_lock(self.desk)
        self.addCleanup(held.close)
        real = time.sleep
        with unittest.mock.patch.object(cli.time, "sleep", lambda s: real(s * 3)):  # as macOS overshoots short sleeps
            t0 = time.monotonic()
            self.assertIsNone(cli.hold_server_lock(self.desk))
            took = time.monotonic() - t0
        self.assertGreaterEqual(took, cli.LOCK_WAIT)
        self.assertLess(took, cli.LOCK_WAIT + 1)  # counting 40 sleeps of 0.15 s would take 6 s

    def test_a_server_on_a_port_another_holds_says_so_in_one_line(self):
        skip_unless_ports(self)
        busy = socket.socket()
        self.addCleanup(busy.close)
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = busy.getsockname()[1]
        r = subprocess.run([str(Path(__file__).resolve().parent.parent / "bin" / "hansa"), "--desk", str(self.desk.root),
                            "serve", "--port", str(port)], capture_output=True, text=True, timeout=20)
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("Traceback", r.stdout + r.stderr)
        self.assertEqual(r.stdout, "")
        self.assertEqual(r.stderr.strip().splitlines(), [
            f"hansa: port {port} is in use, perhaps by another desk's hansa serve; start this desk's server on a free "
            f"port with hansa serve --port N (hansa web then finds it)"])

    def test_the_web_command_opens_the_port_this_desks_server_serves_on(self):
        skip_unless_ports(self)
        env = {k: v for k, v in os.environ.items() if k not in ("HANSA_ROLE", "HANSA_TASK")}
        bin_ = str(Path(__file__).resolve().parent.parent / "bin" / "hansa")
        (self.tmp / "t").mkdir()  # where the server tries srt when it starts: in the test's folder, gone with it
        serve = subprocess.Popen([bin_, "--desk", str(self.desk.root), "serve", "--port", "0"],
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                                 env={**env, "TMPDIR": str(self.tmp / "t")})

        def stop():  # as Ctrl+C does: a server stopped while it tries srt removes the folder it tried it in
            if serve.poll() is None:
                serve.send_signal(signal.SIGINT)
                try:
                    serve.wait(20)
                except subprocess.TimeoutExpired:
                    serve.kill()
                    serve.wait()
        self.addCleanup(stop)
        self.addCleanup(serve.stdout.close)
        said = serve.stdout.readline()
        port = int(re.search(r"web page on 127\.0\.0\.1:(\d+) ", said).group(1))
        self.assertNotEqual(port, 0)
        self.assertEqual((self.desk.state_dir / "web-port").read_text(), str(port))
        r = subprocess.run([bin_, "--desk", str(self.desk.root), "web"], input="", capture_output=True, text=True,
                           env=env, timeout=20)
        address = r.stdout.splitlines()[0]
        self.assertTrue(address.startswith(f"Open http://127.0.0.1:{port}/?t="), address)
        with urllib.request.urlopen(address.partition(" ")[2], timeout=10) as page:  # the page of this desk
            self.assertEqual(page.status, 200)
        r = subprocess.run([bin_, "--desk", str(self.desk.root), "web", "--port", "9999"], input="",
                           capture_output=True, text=True, env=env, timeout=20)
        self.assertIn("http://127.0.0.1:9999/", r.stdout)
        stop()
        self.assertEqual(os.listdir(self.tmp / "t"), [])

    def test_bin_hansa_runs_its_own_checkout_whatever_the_current_directory(self):
        from hansa import build, installed_text
        other = self.tmp / "other-checkout"
        (other / "hansa").mkdir(parents=True)
        (other / "hansa" / "__init__.py").write_text("__version__ = '9.9.9'\n")
        (other / "hansa" / "__main__.py").write_text("print('hansa 9.9.9 from the other checkout')\n")
        r = subprocess.run([str(Path(__file__).resolve().parent.parent / "bin" / "hansa"), "--version"], cwd=other,
                           capture_output=True, text=True, timeout=30)
        self.assertEqual((r.returncode, r.stdout.strip()), (0, f"hansa {build()}, {installed_text()}"), r.stderr)

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

    def test_the_web_command_with_no_input_prints_the_address_and_returns_as_its_help_says(self):
        env = {k: v for k, v in os.environ.items() if k not in ("HANSA_ROLE", "HANSA_TASK")}
        r = subprocess.run([str(Path(__file__).resolve().parent.parent / "bin" / "hansa"), "--desk", str(self.desk.root),
                            "web"], stdin=subprocess.DEVNULL, capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("/?t=", r.stdout)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            main(["web", "--help"])
        self.assertIn("hansa web </dev/null", " ".join(out.getvalue().split()))

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
        self.assertEqual(argv[2:6], ["--", "/bin/sh", "-c", f"exec /usr/bin/env FAKE_SHOW_ARGV={seen} /bin/sh -c "
                                     + shlex.quote('echo "tmp=$TMPDIR"')])
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

    def test_without_srt_a_lane_with_setup_or_check_runs_neither_and_one_with_neither_runs(self):
        from hansa.run import try_srt_at_start
        d = Dispatcher(self.desk)
        with self.without_srt():
            m = self.settle(d, self.desk.new_task("Add hello.txt"))
            self.assertEqual(m["state"], "done", m)  # no setup, no check: no srt needed
            self.assertNotIn("srt MISSING", views.status_text(views.status(self.desk)))
            s = self.desk.settings()
            s["lanes"]["a"]["check"] = "echo checked"
            self.assertEqual(self.desk.save_settings(s), [])
            said = views.status_text(views.status(self.desk))  # no server has tried srt: this command's PATH
            self.assertIn("srt MISSING (on the PATH of this command; hansa serve has not tried srt yet", said)
            m = self.settle(d, self.desk.new_task("Add hello.txt again"))
            self.assertEqual(m["state"], "done", m)  # not judged on a check that never ran
            self.assertTrue(m["check"]["could_not_run"])
            self.assertIn("could not run: hansa needs srt", m["check"]["note"])
            self.assertIn("coordinator can do this", m["check"]["note"])
            self.assertIn("npm install -g @anthropic-ai/sandbox-runtime", views.show_text(self.desk, m["id"]))
            self.assertIn("srt MISSING (as hansa serve saw it", views.status_text(views.status(self.desk)))
            s = self.desk.settings()
            s["lanes"]["a"].update(check="", setup="echo installing")
            self.assertEqual(self.desk.save_settings(s), [])
            m = self.settle(d, self.desk.new_task("Add hello.txt once more"))
            self.assertEqual(m["state"], "failed", m)  # the setup could not run, so the worker never started
            self.assertIn("hansa needs srt", m["error"])
            self.assertIn("the machine's problem, not the task's", views.show_text(self.desk, m["id"]))
        self.assertEqual(self.srt_calls(tries=True), [])
        self.assertIn("srt MISSING", views.status_text(views.status(self.desk)))  # the server has not tried again
        self.assertTrue(try_srt_at_start(self.desk)["ran"])  # as a restarted server does
        self.assertNotIn("srt MISSING", views.status_text(views.status(self.desk)))

    def test_status_reports_srt_as_the_server_sees_it(self):
        from hansa.run import try_srt_at_start
        s = self.desk.settings()
        s["lanes"]["a"]["check"] = "echo checked"
        self.assertEqual(self.desk.save_settings(s), [])
        rec = try_srt_at_start(self.desk)  # the server's PATH has srt
        self.assertEqual((rec["found"], rec["ran"], rec["path"]), (True, True, str(self.tmp / "bin" / "srt")))
        with self.without_srt():  # the coordinator's has not
            st = views.status(self.desk)
            self.assertEqual(st["srt_missing"], "")
            self.assertNotIn("srt MISSING", views.status_text(st))
            self.assertTrue(st["srt"]["ran"])  # the page reads the same record
        self.assertIn("hansa serve tried srt", views.explain_text(self.desk, "server"))

    def test_an_srt_record_without_a_time_shows_no_time(self):
        from hansa.desk import write_json
        s = self.desk.settings()
        s["lanes"]["a"]["check"] = "echo checked"
        self.assertEqual(self.desk.save_settings(s), [])
        self.desk.state_dir.mkdir(exist_ok=True)
        write_json(self.desk.state_dir / "srt.json", {"found": True, "path": "/x/srt", "ran": False, "said": "boom"})
        problem = views.status(self.desk)["srt_missing"]
        self.assertIn("srt CANNOT RUN (as hansa serve saw it): it is at /x/srt", problem)
        server = views.explain_text(self.desk, "server")
        self.assertIn("now: hansa serve tried srt: found at /x/srt", server)
        self.assertNotIn("01-01", problem + server)
        self.assertIn("on the PATH hansa serve starts with, never on a lane's", server)

    def test_an_srt_that_is_found_but_cannot_start_runs_no_check_and_does_not_fail_the_task(self):
        ran = self.tmp / "check-ran"
        s = self.desk.settings()
        s["lanes"]["a"]["check"] = f"touch {ran}"
        self.assertEqual(self.desk.save_settings(s), [])
        broken = self.tmp / "nonode"  # srt is a node script; this PATH finds it but has no node
        broken.mkdir()
        (broken / "srt").write_text("#!/bin/sh\necho 'env: node: No such file or directory' >&2\nexit 127\n")
        (broken / "srt").chmod(0o755)
        with unittest.mock.patch.dict(os.environ, {"PATH": f"{broken}{os.pathsep}{os.environ['PATH']}"}):
            m = self.settle(Dispatcher(self.desk), self.desk.new_task("Add hello.txt"))
            st = views.status(self.desk)
        self.assertEqual(m["state"], "done", m)
        self.assertIsNone(m["check"]["ok"])  # not marked as failing its check
        self.assertIn("could not run: srt", m["check"]["note"])
        self.assertIn("env: node: No such file or directory", m["check"]["note"])
        self.assertFalse(ran.exists())  # nothing ran
        self.assertEqual(self.srt_calls(tries=True), [])
        self.assertIn("srt CANNOT RUN", st["srt_missing"])
        self.assertNotIn("check FAILED", views.status_text(st))
        self.assertIn("check could not run", views.status_text(st))
        shown = views.show_text(self.desk, m["id"])
        self.assertIn("the machine's problem, not the task's", shown)
        self.assertIn("hansa explain server", shown)
        self.assertIn(f"hansa merge {m['id']} --recheck", shown)
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as e:
            main(["--desk", str(self.desk.root), "merge", m["id"]])
        said = str(e.exception.code)
        self.assertLess(said.index("--recheck"), said.index("--despite-check"))
        self.assertEqual(self.desk.meta(m["id"])["state"], "done")

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

    def net_lane(self, what: str, times: int) -> Path:
        count = self.tmp / f"net-{what}-{times}"
        s = self.desk.settings()
        s["lanes"]["a"]["env"] = {"FAKE_NET": f"{what}:{times}:{count}"}
        self.assertEqual(self.desk.save_settings(s), [])
        return count

    def test_a_dropped_connection_is_tried_once_more_then_fails_plainly(self):
        from hansa import run
        for what, why in (("econnreset", "the connection to the provider failed (Connection dropped)"),
                          ("529", "the provider was overloaded (HTTP 529)"),
                          ("500", "the provider had a server error (HTTP 500)")):
            with self.subTest(what, times=1):
                count = self.net_lane(what, 1)
                tid = self.desk.new_task("Add hello.txt")
                m = self.settle(Dispatcher(self.desk), tid)
                self.assertEqual((m["state"], m["provider"], m["error"]), ("done", "claude", ""), m)
                self.assertEqual(count.read_text(), "2")
                self.assertEqual([(a["provider"], a["error"], a["kind"]) for a in m["attempts"]], [("claude", why, "network")])
                self.assertEqual(m["files"], ["hello.txt"])  # what the first run left is not in the second's change
                self.assertEqual(provider_problem(self.desk, self.desk.settings(), "claude"), "")  # it does not rest
                self.assertIn(f"earlier try on claude: {why}", views.show_text(self.desk, tid))
                self.assertEqual(main(["--desk", str(self.desk.root), "merge", tid]), 0)
            with self.subTest(what, times=2):
                count = self.net_lane(what, 2)
                tid = self.desk.new_task("Add hello.txt")
                m = self.settle(Dispatcher(self.desk), tid)
                self.assertEqual(m["state"], "failed", m)
                self.assertEqual(count.read_text(), "2")  # once more, not a third time
                self.assertEqual(len(m["attempts"]), 2)
                self.assertEqual(m["error"], f"claude: {why}; the connection to the provider failed twice, so hansa "
                                 "stopped trying. This is the machine's or the provider's problem, not the task's: "
                                 f'when the connection is back, hansa retry {tid} "try again"')
                self.assertNotIn("success", m["error"])
                self.assertEqual(provider_problem(self.desk, self.desk.settings(), "claude"), "")
        for text in ("API Error: Connection error.", "API Error: socket hang up", "API Error: Request timed out.",
                     'API Error: 529 {"type":"error","error":{"type":"overloaded_error","message":"Overloaded"}}',
                     "API Error: 502 Bad Gateway"):
            self.assertTrue(run.network_reason({"is_error": True, "result": text}, ""), text)
        self.assertEqual(run.network_reason({"is_error": False, "result": "Fixed the ECONNRESET on a dropped connection"}, ""), "")
        self.assertEqual(run.network_reason({"is_error": True, "result": "I could not do it"}, ""), "")

    def test_a_verification_that_lost_the_connection_twice_says_how_to_run_it_again(self):
        tid = self.desk.new_task("Add hello.txt")
        self.assertEqual(self.settle(Dispatcher(self.desk), tid)["state"], "done")
        self.net_lane("econnreset", 2)
        self.assertEqual(main(["--desk", str(self.desk.root), "verify", tid]), 0)
        vid = [m["id"] for m in self.desk.tasks() if m.get("kind") == "verify"][0]
        m = self.settle(Dispatcher(self.desk), vid)
        self.assertEqual(m["state"], "failed", m)
        self.assertTrue(m["error"].endswith(f"when the connection is back, hansa verify {tid}"), m["error"])

    def test_a_quota_error_and_a_plain_failure_are_not_a_connection_failure(self):
        count = self.net_lane("quota", 1)
        tid = self.desk.new_task("Add hello.txt")
        d = Dispatcher(self.desk)
        end = time.time() + 20
        while time.time() < end and not self.desk.meta(tid).get("attempts"):
            d.tick()
            time.sleep(0.1)
        m = self.desk.meta(tid)
        self.assertEqual((m["state"], m["attempts"][0]["error"], m["attempts"][0].get("kind")),
                         ("queued", "out of quota (HTTP 429)", None), m)
        self.assertIn("resting until", provider_problem(self.desk, self.desk.settings(), "claude"))
        self.desk.set_provider_state("claude", cool_until=0, reason="")
        self.assertEqual(self.settle(d, tid)["state"], "done")
        count = self.net_lane("plain", 1)
        tid = self.desk.new_task("Add hello.txt")
        m = self.settle(Dispatcher(self.desk), tid)
        self.assertEqual((m["state"], m["error"], m["attempts"]), ("failed", "error: I could not do it", []), m)
        self.assertEqual(count.read_text(), "1")

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

    def test_runs_ride_out_a_short_network_drop_and_the_provider_check_answers_at_once(self):
        from hansa import cli, run
        retries, popen = {}, run.subprocess.Popen

        def started(argv, **kwargs):  # a Claude Code run has the task's id in its environment
            env = kwargs.get("env") or {}
            if "HANSA_TASK" in env:
                retries.setdefault(env["HANSA_TASK"], set()).add(env.get("CLAUDE_CODE_MAX_RETRIES"))
            return popen(argv, **kwargs)
        s = self.desk.settings()
        s["lanes"]["a"]["env"] = {"FAKE_VERDICT": "holds"}
        self.assertEqual(self.desk.save_settings(s), [])
        d = Dispatcher(self.desk)
        tid = self.desk.new_task("Add hello.txt")
        with unittest.mock.patch.object(run.subprocess, "Popen", started):
            self.settle(d, tid)
            self.hansa("verify", tid, "the file's contents")
            vid = self.desk.meta(tid)["verify"]
            self.assertEqual(self.settle(d, vid)["verdict"], "holds")
        self.assertEqual(retries, {tid: {"8"}, vid: {"8"}})  # a task and a verification
        did = self.desk.new_task("Make the badge bigger")
        self.desk.update(did, lane="a", provider="claude", worktree=str(self.repo), session_id="sid-d",
                         model_asked="sonnet", kind="design")
        self.assertEqual(run.design_command(self.desk, did)[1]["CLAUDE_CODE_MAX_RETRIES"], "8")
        self.assertEqual(cli.coordinator_desk(self.desk, os.getuid())["env"]["CLAUDE_CODE_MAX_RETRIES"], "8")
        seen = {}

        def answer(argv, **kwargs):
            seen.update(kwargs["env"])
            return subprocess.CompletedProcess(argv, 0, json.dumps({"type": "result", "result": "OK"}), "")
        with unittest.mock.patch.object(run.subprocess, "run", answer):
            self.assertTrue(run.check_provider(self.desk, "claude")["ok"])
        self.assertEqual(seen["CLAUDE_CODE_MAX_RETRIES"], "0")

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

    # -- hansa posts to a coordinator's session (hansa/notify.py) ------------------------------------------
    def session(self):
        """A stand-in for a Claude Code session: a process that runs until it is stopped, and its inbox path."""
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        self.addCleanup(p.wait)
        self.addCleanup(p.kill)
        return p, str(self.tmp / f"inbox-{p.pid}.sock")

    def in_session(self, p, sock):
        """Commands run from that session, as its Bash tool runs them."""
        return unittest.mock.patch.dict(os.environ, {"CLAUDE_PID": str(p.pid), "CLAUDE_CODE_MESSAGING_SOCKET": sock})

    def hearing(self, p):
        """The desk's register holds a live follower for that session, as hansa notify on leaves it; none runs, so
        nothing is posted: this is only what commands read. Seen in the future, so it stays live for the test."""
        sess = {"pid": p.pid, "start": notify.proc_start(p.pid)}
        key = notify.key_of(sess)
        notify._write_record(self.desk, key, {"key": key, "session": sess, "seen": time.time() + 3600, "ended": None})

    def hansa_out(self, *args) -> str:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            main(["--desk", str(self.desk.root), *args])
        return out.getvalue()

    def posts(self, sock) -> list:
        """What hansa posted to this inbox, each message's text."""
        f = self.tmp / "posted.jsonl"
        rows = [json.loads(x) for x in f.read_text().splitlines()] if f.exists() else []
        for r in rows:  # each is one line of JSON, as the inbox takes it
            self.assertTrue(r["line"].endswith("\n") and r["line"].count("\n") == 1, r["line"])
        return [json.loads(r["line"])["message"]["content"] for r in rows if r["socket"] == sock]

    def soon(self, what, timeout=20):
        end = time.time() + timeout
        while time.time() < end:
            got = what()
            if got:
                return got
            time.sleep(0.1)
        self.fail(f"not within {timeout} s: {what()!r}")

    def test_a_task_that_ends_after_its_check_is_posted_once_to_its_session(self):
        s = self.desk.settings()
        s["lanes"]["a"]["check"] = "true"
        self.assertEqual(self.desk.save_settings(s), [])
        p, sock = self.session()
        with self.in_session(p, sock):
            self.assertIn("now posts to this session", self.hansa_out("notify", "on"))
            self.hansa_out("add", "Add hello.txt")
        self.assertEqual(self.desk.meta("t0001")["session"]["pid"], p.pid)
        m = self.settle(Dispatcher(self.desk), "t0001")
        self.assertEqual((m["state"], m["check"]["ok"]), ("done", True))
        first = self.soon(lambda: self.posts(sock))
        time.sleep(1.5)  # nothing more comes: not when the worker ended, before the check
        self.assertEqual(self.posts(sock), first)
        self.assertEqual(len(first), 1, first)
        self.assertTrue(first[0].startswith(f"[hansa] desk {self.desk.root}\n"), first[0])
        self.assertIn("- t0001 done: Add hello.txt (check passed) · next: hansa show t0001 · hansa diff t0001 --stat, "
                      "then hansa verify t0001", first[0])
        self.assertIn(f"since {time.strftime('%m-%d')}", self.hansa_out("notify"))
        self.assertIn("last post: ", self.hansa_out("notify"))
        self.assertIn("t0001 done", self.hansa_out("notify"))
        self.assertIn(f"hansa posts to: pid {p.pid} (no name yet)\n", self.hansa_out("status"))

    def test_a_session_is_named_by_its_pid_and_then_also_by_the_name_claude_code_gave_it(self):
        p, _ = self.session()
        self.hearing(p)
        key = notify.key_of({"pid": p.pid, "start": notify.proc_start(p.pid)})
        self.assertIn(f"hansa posts to: pid {p.pid} (no name yet)\n", self.hansa_out("status"))
        self.assertIn(f"  pid {p.pid} (no name yet) · since ", self.hansa_out("notify"))
        rec = notify.records(self.desk)[0]
        notify._write_record(self.desk, key, {**rec, "name": "desk-b4"})
        self.assertIn(f"hansa posts to: pid {p.pid} (desk-b4)\n", self.hansa_out("status"))
        self.assertIn(f"  pid {p.pid} (desk-b4) · since ", self.hansa_out("notify"))
        self.assertIn(f"session pid {p.pid} (desk-b4) ended", notify.missed_head(
            {"pid": p.pid, "name": "desk-b4", "session_ended": True, "at": time.time()}))

    def test_status_and_notify_start_no_claude_and_show_the_name_the_follower_recorded(self):
        p, sock = self.session()
        # the follower (its own process) names the session as on a desk: by `claude agents --json`, here a
        # stand-in that lists it once the test says so. Its record is the follower's: written with each feed.
        listed = self.tmp / "agents.json"
        claude = self.tmp / "named" / "claude"
        claude.parent.mkdir()
        claude.write_text("#!/usr/bin/env python3\nimport os, sys\n"
                          "if sys.argv[1:3] != ['agents', '--json']:\n    sys.exit('only agents --json here')\n"
                          f"sys.stdout.write(open({str(listed)!r}).read() if os.path.exists({str(listed)!r}) "
                          "else '[]')\n")
        claude.chmod(0o755)
        os.environ["HANSA_CLAUDE"] = str(claude)
        from hansa import build
        build()  # hansa's own commit, which status shows: git once, not claude, whichever test runs first
        with unittest.mock.patch.object(notify, "agents_rows", side_effect=AssertionError("claude was started")), \
                unittest.mock.patch.object(notify.subprocess, "run", side_effect=AssertionError("a program ran")):
            with self.in_session(p, sock):
                self.assertIn(f"now posts to this session, pid {p.pid} (no name yet), through",
                              self.hansa_out("notify", "on"))
            self.assertIn(f"hansa posts to: pid {p.pid} (no name yet)\n", self.hansa_out("status"))
            self.assertIn(f"  pid {p.pid} (no name yet) · since ", self.hansa_out("notify"))
            write_json(listed, [{"pid": p.pid, "name": "desk-87"}])
            # its first lookup is 5 s after it starts; one that came before the listing tries again 15 s later
            self.soon(lambda: f"hansa posts to: pid {p.pid} (desk-87)\n" in self.hansa_out("status"), timeout=60)
            self.assertIn(f"hansa posts to: pid {p.pid} (desk-87)\n", self.hansa_out("status"))
            self.assertIn(f"  pid {p.pid} (desk-87) · since ", self.hansa_out("notify"))
            with self.in_session(p, sock):
                self.assertIn(f"already notifies this session, pid {p.pid} (desk-87);", self.hansa_out("notify", "on"))
        with self.in_session(p, sock):
            self.hansa_out("notify", "off")

    def test_a_follower_looks_a_missing_name_up_again_later_each_time_up_to_five_minutes(self):
        clock, looked = [1000.0], []
        with unittest.mock.patch.object(notify.time, "time", side_effect=lambda: clock[0]):
            namer = notify.Namer(1, first=5, look=lambda: looked.append(clock[0]) or "")
            for _ in range(2000):
                namer.tick()
                if namer.thread:
                    namer.thread.join()
                clock[0] += 1
        self.assertEqual([b - a for a, b in zip(looked, looked[1:])][:8],
                         [16, 31, 61, 121, 241, 301, 301, 301])  # one second of the loop's own after each
        self.assertEqual(looked[0], 1005)
        n = len(looked)
        namer.look = lambda: looked.append(clock[0]) or "desk-87"
        with unittest.mock.patch.object(notify.time, "time", side_effect=lambda: clock[0]):
            for _ in range(700):
                self.assertIn(namer.tick(), ("", "desk-87"))
                if namer.thread:
                    namer.thread.join()
                clock[0] += 1
        self.assertEqual(namer.name, "desk-87")
        self.assertLessEqual(len(looked) - n, 3)  # once named, every five minutes: Claude Code may rename it

    def test_posting_does_not_wait_for_a_name_lookup(self):
        release = threading.Event()
        namer = notify.Namer(1, "old", first=0, look=lambda: release.wait(10) and "desk-87")
        t0 = time.time()
        self.assertEqual(namer.tick(), "old")  # the lookup started, and runs on by itself
        self.assertTrue(namer.thread.is_alive())
        self.assertEqual(namer.tick(), "old")
        self.assertLess(time.time() - t0, 1)
        release.set()
        namer.thread.join()
        self.assertEqual(namer.tick(), "desk-87")
        self.assertEqual(notify.NAME_LIMIT, 10)  # and a lookup that hangs is cut short
        with unittest.mock.patch.object(notify.subprocess, "run", side_effect=subprocess.TimeoutExpired("claude", 10)) \
                as run:
            self.assertEqual(notify.session_name(1, notify.NAME_LIMIT), "")
        self.assertEqual(run.call_args.kwargs["timeout"], 10)

    def test_a_follower_posts_while_its_name_lookup_hangs(self):
        s = self.desk.settings()
        s["providers"]["mimo"] = {"kind": "compatible", "model": "m", "effort": "", "base_url": "https://x"}
        self.assertEqual(self.desk.save_settings(s), [])
        self.desk.add_ask("Please fill in mimo's key")
        p, sock = self.session()
        sess = {"pid": p.pid, "start": notify.proc_start(p.pid)}
        key = notify.key_of(sess)
        started, release, got, broke = threading.Event(), threading.Event(), {}, []

        def look(pid, timeout=15):
            started.set()
            return release.wait(60) and "desk-87"

        def meanwhile():  # the follower runs in this test's own thread; this plays the desk around it
            try:
                self.assertTrue(started.wait(30), "the follower never looked the name up")
                self.desk.write_key("mimo", "sk-mimo-12345678")  # a line for the session, while the lookup hangs
                got["posts"] = self.soon(lambda: self.posts(sock), timeout=10)
                self.assertFalse(release.is_set())
                release.set()
                got["name"] = self.soon(lambda: [r.get("name") for r in notify.records(self.desk)
                                                 if r.get("name")], timeout=10)
            except BaseException as e:  # noqa: BLE001 - raised again once the follower ended
                broke.append(e)
            finally:
                release.set()
                p.kill()
                p.wait()

        was = signal.getsignal(signal.SIGTERM)
        self.addCleanup(signal.signal, signal.SIGTERM, was)
        th = threading.Thread(target=meanwhile)
        with unittest.mock.patch.object(notify, "session_name", look), \
                unittest.mock.patch.object(notify, "Namer", functools.partial(notify.Namer, first=0)), \
                contextlib.redirect_stdout(io.StringIO()):
            th.start()
            self.assertEqual(notify.follow(self.desk, key, sess, sock, "n"), 0)
        th.join()
        if broke:
            raise broke[0]
        self.assertIn("- mimo is ready now: done? Still open: request #1", got["posts"][0])
        self.assertEqual(got["name"], ["desk-87"])  # and what the lookup found is recorded once it ends

    def test_two_sessions_each_hear_their_own_tasks_and_both_hear_one_from_a_shell(self):
        (a, sa), (b, sb) = self.session(), self.session()
        for p, sock in ((a, sa), (b, sb)):
            with self.in_session(p, sock):
                self.hansa_out("notify", "on")
        with self.in_session(a, sa):
            self.hansa_out("add", "Task of a")
        with self.in_session(b, sb):
            self.hansa_out("add", "Task of b")
        self.hansa_out("add", "Task from a shell")  # no session: nobody's own
        d = Dispatcher(self.desk)
        for tid in ("t0001", "t0002", "t0003"):
            self.settle(d, tid)
        heard_a = self.soon(lambda: "t0003 done" in "".join(self.posts(sa)) and "t0001 done" in "".join(self.posts(sa))
                            and "".join(self.posts(sa)))
        heard_b = self.soon(lambda: "t0003 done" in "".join(self.posts(sb)) and "t0002 done" in "".join(self.posts(sb))
                            and "".join(self.posts(sb)))
        time.sleep(1)
        self.assertNotIn("t0002", "".join(self.posts(sa)), heard_a)
        self.assertNotIn("t0001", "".join(self.posts(sb)), heard_b)
        self.assertEqual(sorted(r["session"]["pid"] for r in notify.live_records(self.desk)), sorted([a.pid, b.pid]))
        self.assertIn("hansa notifies 2 session(s)", self.hansa_out("notify"))

    def test_a_cancel_is_not_posted_to_the_session_that_made_it(self):
        (a, sa), (b, sb) = self.session(), self.session()
        with self.in_session(a, sa):
            for title in ("Cancelled by a", "Cancelled by b", "Cancelled from a shell", "Verified, then cancelled"):
                self.hansa_out("add", title)
        vid = self.desk.new_task("Verify t0004", kind="verify", parent="t0004", lane="a")
        self.desk.update("t0004", state="done", verify=vid)
        self.desk.update(vid, state="running")
        with self.in_session(a, sa):
            self.hansa_out("cancel", "t0001")
            self.hansa_out("cancel", "t0004")  # and so its running verification, which hansa serve then stops
        self.desk.update(vid, state="cancelled", error="cancelled")
        with self.in_session(b, sb):
            self.hansa_out("cancel", "t0002")
        self.hansa_out("cancel", "t0003")
        sess = {p: {"pid": p.pid, "start": notify.proc_start(p.pid)} for p in (a, b)}
        events = [{"event": "task_finished", "task": t, "state": "cancelled"}
                  for t in ("t0001", "t0002", "t0003", "t0004", vid)]
        heard = {p: [x["key"][1] for x in notify.items_for(self.desk, events, {"key": "k", "session": sess[p]})]
                 for p in (a, b)}
        self.assertEqual(heard[a], ["t0002", "t0003"])  # neither what a cancelled nor the verification that went with it
        self.assertEqual(heard[b], ["t0001", "t0003", "t0004", vid])
        self.assertEqual(self.desk.meta(vid)["cancel_by"], sess[a])

    def test_a_follower_ends_with_its_session_and_the_next_one_first_says_what_was_missed(self):
        a, sa = self.session()
        with self.in_session(a, sa):
            self.hansa_out("notify", "on")
        self.soon(lambda: notify.live_records(self.desk))
        a.kill()
        a.wait()
        self.soon(lambda: all(r.get("ended") for r in notify.records(self.desk)))  # it ended with the session
        self.assertIsNone(notify.running(self.desk, notify.key_of({"pid": a.pid, "start": notify.proc_start(a.pid)})))
        self.assertEqual(notify.live_records(self.desk), [])
        self.hansa_out("add", "Added while nobody listened")
        self.assertEqual(self.settle(Dispatcher(self.desk), "t0001")["state"], "done")
        with self.in_session(a, sa):
            self.assertIn("does not notify this session", self.hansa_out("notify", "off"))
        b, sb = self.session()
        with self.in_session(b, sb):
            self.hansa_out("notify", "on")
        first = self.soon(lambda: self.posts(sb))
        self.assertEqual(len(first), 1, first)
        # whose it is: the session that ended and when, not "no session heard" (another may have heard all along)
        head = first[0].splitlines()[0]
        self.assertRegex(head, rf": session pid {a.pid} \(no name yet\) ended at \d\d:\d\d; since then, for its tasks and requests:$")
        self.assertEqual(notify.missed_head({"pid": 16686, "name": "ntA", "session_ended": True,
                                             "at": time.mktime((2026, 9, 26, 22, 55, 0, 0, 0, -1))}),
                         "session pid 16686 (ntA) ended at 22:55; since then, for its tasks and requests:")
        self.assertIn("- t0001 done: Added while nobody listened", first[0])
        self.hansa_out("add", "Added after")  # then it carries on from the end of the log
        self.settle(Dispatcher(self.desk), "t0002")
        later = self.soon(lambda: self.posts(sb)[1:])
        self.assertIn("- t0002 done: Added after", later[0])
        self.assertNotIn("t0001", later[0])
        self.assertEqual(self.posts(sa), [])

    def test_what_an_ended_session_missed_is_posted_once(self):
        a, sa = self.session()
        with self.in_session(a, sa):
            self.hansa_out("notify", "on")
        self.soon(lambda: notify.live_records(self.desk))
        a.kill()
        a.wait()
        self.soon(lambda: all(r.get("ended") for r in notify.records(self.desk)))
        self.hansa_out("add", "Added while nobody listened")
        self.settle(Dispatcher(self.desk), "t0001")
        b, sb = self.session()
        with self.in_session(b, sb):
            self.hansa_out("notify", "on")
        self.assertIn("t0001 done", self.soon(lambda: self.posts(sb))[0])
        ended = self.soon(lambda: [r for r in notify.records(self.desk) if r["session"]["pid"] == a.pid
                                   and r.get("caught_up")])
        self.assertTrue(ended[0]["ended"])
        c, sc = self.session()  # a third session: B told it already, and has heard everything since
        with self.in_session(c, sc):
            self.hansa_out("notify", "on")
        self.soon(lambda: [r for r in notify.live_records(self.desk) if r["session"]["pid"] == c.pid])
        time.sleep(1)
        self.assertEqual(self.posts(sc), [])
        self.assertEqual(len(self.posts(sb)), 1)

    def test_a_follower_killed_outright_counts_as_ended_when_the_next_one_starts(self):
        a, sa = self.session()
        with self.in_session(a, sa):
            self.hansa_out("notify", "on")
        live = self.soon(lambda: notify.live_records(self.desk))
        follower = notify.running(self.desk, live[0]["key"])
        self.assertEqual(live[0]["follower"], {"pid": follower["pid"], "start": follower["start"]})
        os.kill(follower["pid"], signal.SIGKILL)
        self.soon(lambda: not notify.alive(follower["pid"], follower["start"]))
        self.assertTrue(notify.live_records(self.desk))  # seen seconds ago: it would count as live for 30 s
        self.hansa_out("add", "Added while its follower was dead")
        self.settle(Dispatcher(self.desk), "t0001")
        with self.in_session(a, sa):
            self.assertIn("now posts to this session", self.hansa_out("notify", "on"))
        first = self.soon(lambda: self.posts(sa))
        self.assertIn(f"hansa stopped posting to session pid {a.pid} (no name yet) at ", first[0])
        self.assertIn("- t0001 done: Added while its follower was dead", first[0])

    def test_srt_that_cannot_run_is_posted_once_until_it_runs_again(self):
        s = self.desk.settings()
        s["lanes"]["a"]["check"] = "true"  # a lane that needs srt
        self.assertEqual(self.desk.save_settings(s), [])
        p, sock = self.session()
        with self.in_session(p, sock):
            self.hansa_out("notify", "on")
        self.soon(lambda: notify.live_records(self.desk))

        def tried(ran):  # a try of srt, as before each setup or check: its record, and the event
            said = "" if ran else "env: node: No such file or directory (exit 127)"
            write_json(self.desk.state_dir / "srt.json", {"found": True, "path": "/x/srt", "ran": ran, "said": said,
                                                          "at": now()})
            self.desk.event("srt_tried", found=True, path="/x/srt", ran=ran, said=said)

        def told():
            return [x for x in self.posts(sock) if "could not run srt" in x]
        tried(False)
        self.soon(told)
        tried(False)
        tried(False)
        time.sleep(1.5)
        self.assertEqual(len(told()), 1)
        tried(True)
        time.sleep(1)
        tried(False)  # it ran in between: this is news again
        self.soon(lambda: len(told()) == 2)

    def serving(self, quiet=0.0):
        """A stand-in for hansa serve: a live process that serve.lock names, whose last round was quiet s ago."""
        self.desk.state_dir.mkdir(exist_ok=True)
        lock = self.desk.state_dir / "serve.lock"
        p = subprocess.Popen([sys.executable, "-c", "import fcntl, sys, time; f = open(sys.argv[1], 'a'); "
                              "fcntl.flock(f, fcntl.LOCK_EX); print('held', flush=True); time.sleep(120)", str(lock)],
                             stdout=subprocess.PIPE, text=True)
        self.addCleanup(p.wait)
        self.addCleanup(p.kill)
        self.assertEqual(p.stdout.readline().strip(), "held")
        p.stdout.close()
        lock.write_text(f"{p.pid} {notify.proc_start(p.pid)}")
        self.round_at(quiet)
        return p

    def round_at(self, quiet):
        """The server's last round was quiet s ago; the server started before it (a quiet longer than it has
        been running is never said)."""
        at = time.time() - quiet
        (self.desk.state_dir / "heartbeat").write_text(str(at))
        lock = self.desk.state_dir / "serve.lock"
        if lock.exists() and lock.stat().st_mtime > at - 1:
            os.utime(lock, (at - 1, at - 1))

    def test_a_heartbeat_being_written_is_not_read_as_a_server_quiet_since_1970(self):
        from hansa import run
        srv = self.serving()
        p, sock = self.session()
        with self.in_session(p, sock):
            self.hansa_out("notify", "on")
        self.soon(lambda: notify.live_records(self.desk))
        lock = self.desk.state_dir / "serve.lock"
        started = time.time() - 60
        os.utime(lock, (started, started))  # up for a minute
        (self.desk.state_dir / "heartbeat").write_text("")  # the moment a rewrite in place emptied it
        st = run.server_state(self.desk)
        self.assertEqual((st["state"], st["pid"]), ("busy", srv.pid))
        self.assertLess(st["quiet"], 70)  # counted from the server's start, not from 1970
        self.assertLess(views.status(self.desk)["server_seconds_ago"], 70)
        (self.desk.state_dir / "heartbeat").unlink()  # none at all: the same
        self.assertLess(run.server_state(self.desk)["quiet"], 70)
        self.assertLess(views.status(self.desk)["server_seconds_ago"], 70)
        time.sleep(4)  # the follower looked a few times, and would have posted by now
        self.assertNotIn("busy or stuck", self.server_posts(sock))
        self.assertNotIn("stuck", self.hansa_out("status"))

    def test_a_heartbeat_from_before_the_server_started_counts_from_its_start(self):
        from hansa import run
        self.serving()
        lock = self.desk.state_dir / "serve.lock"
        started = time.time() - 10
        os.utime(lock, (started, started))
        (self.desk.state_dir / "heartbeat").write_text(str(time.time() - run.STUCK_AFTER - 600))  # an earlier server's
        st = run.server_state(self.desk)
        self.assertEqual(st["state"], "up")
        self.assertLess(st["quiet"], 20)
        self.assertLess(views.status(self.desk)["server_seconds_ago"], 20)
        started = time.time() - run.STUCK_AFTER - 60  # this one, up long enough to be stuck by its own count
        os.utime(lock, (started, started))
        self.assertEqual(run.server_state(self.desk)["state"], "stuck")

    def test_the_heartbeat_is_replaced_whole_not_rewritten_in_place(self):
        from hansa import run
        self.desk.state_dir.mkdir(exist_ok=True)
        hb = self.desk.state_dir / "heartbeat"
        hb.write_text("1")
        before = hb.stat().st_ino
        held = open(hb)  # a reader that opened it before the beat
        self.addCleanup(held.close)
        run.beat(self.desk)
        self.assertNotEqual(hb.stat().st_ino, before)  # a new file renamed over the old one
        self.assertEqual(held.read(), "1")  # what the reader opened was never emptied
        self.assertAlmostEqual(float(hb.read_text()), time.time(), delta=5)  # a plain number, as readers expect
        self.assertEqual([f.name for f in self.desk.state_dir.iterdir() if f.name.startswith("heartbeat")],
                         ["heartbeat"])

    def server_posts(self, sock) -> str:
        return "".join(self.posts(sock))

    def test_a_live_server_in_a_long_round_is_not_down_and_one_quiet_for_long_is_busy_or_stuck(self):
        from hansa import run
        srv = self.serving()
        p, sock = self.session()
        with self.in_session(p, sock):
            self.hansa_out("notify", "on")
        self.soon(lambda: notify.live_records(self.desk))
        self.round_at(45)  # a round past half a minute: stopping a worker's group, sweeping, on a busy machine
        self.assertEqual(run.server_state(self.desk)["state"], "busy")
        self.assertTrue(views.status(self.desk)["server_up"])
        self.assertIn("server up (pid %d) but busy: no round finished for 45 seconds" % srv.pid,
                      self.hansa_out("status"))
        time.sleep(4)  # the follower looked a few times, and would have posted by now
        self.assertNotIn("hansa serve", self.server_posts(sock))
        self.round_at(run.STUCK_AFTER + 60)
        told = self.soon(lambda: "busy or stuck" in self.server_posts(sock) and self.server_posts(sock))
        self.assertIn(f"hansa serve (pid {srv.pid}) has not finished a round for 6 minutes: busy or stuck · next: "
                      f"hansa explain server", told)
        self.assertNotIn("not running", told)
        self.assertIn(f"hansa serve (pid {srv.pid}) has not finished a round for 6 minutes: busy or stuck",
                      self.hansa_out("status"))
        self.assertIn("now: hansa serve (pid", self.hansa_out("explain", "server"))
        self.assertEqual(views.status(self.desk)["server"]["state"], "stuck")
        time.sleep(2)
        self.assertEqual(self.server_posts(sock).count("busy or stuck"), 1)  # said once
        self.round_at(0)  # its round came
        self.soon(lambda: "hansa serve runs again" in self.server_posts(sock))
        time.sleep(2)
        self.assertEqual(self.server_posts(sock).count("hansa serve runs again"), 1)
        self.assertIn("server up · ", self.hansa_out("status"))

    def test_a_gone_server_is_posted_and_so_is_the_next_one_that_runs(self):
        srv = self.serving()
        p, sock = self.session()
        with self.in_session(p, sock):
            self.hansa_out("notify", "on")
        self.soon(lambda: notify.live_records(self.desk))
        srv.kill()
        srv.wait()  # its heartbeat is fresh, but the process serve.lock names is gone
        told = self.soon(lambda: "not running" in self.server_posts(sock) and self.server_posts(sock))
        self.assertIn("hansa serve is not running (it stopped without a word): no task starts or ends until it runs "
                      "again · next: hansa explain server", told)
        self.assertFalse(views.status(self.desk)["server_up"])
        self.assertIn("server NOT running", self.hansa_out("status"))
        self.assertIn("now: server NOT running", self.hansa_out("explain", "server"))
        self.assertNotIn("hansa serve runs again", told)
        again = self.serving()  # a server started again
        self.soon(lambda: "hansa serve runs again" in self.server_posts(sock))
        again.kill()
        again.wait()
        self.assertFalse(views.status(self.desk)["server_up"])

    def test_a_pid_passed_to_another_process_is_not_the_server_for_either_kind_of_lock(self):
        from hansa import run
        other, _ = self.session()  # alive, and given the number a server that stopped had
        self.desk.state_dir.mkdir(exist_ok=True)
        lock = self.desk.state_dir / "serve.lock"
        for words in (f"{other.pid}", f"{other.pid} {notify.proc_start(other.pid)}"):  # before and since the start
            lock.write_text(words)
            self.round_at(0)  # its last round was just now
            self.assertEqual(run.server_state(self.desk)["state"], "gone", words)
            self.round_at(run.STUCK_AFTER + 60)
            self.assertEqual(run.server_state(self.desk)["state"], "gone", words)  # never stuck either
        srv = self.serving(quiet=run.STUCK_AFTER + 60)
        self.assertEqual((run.server_state(self.desk)["state"], run.server_state(self.desk)["pid"]), ("stuck", srv.pid))

    def test_where_the_lock_cannot_be_asked_the_start_and_then_the_heartbeat_tell(self):
        from hansa import run
        other, _ = self.session()
        start = notify.proc_start(other.pid)
        self.desk.state_dir.mkdir(exist_ok=True)
        lock = self.desk.state_dir / "serve.lock"
        real_kill = os.kill

        def not_ours(pid, sig):  # another account's process, as root's server is to a follower on Linux
            if pid == other.pid:
                raise PermissionError(1, "Operation not permitted")
            return real_kill(pid, sig)
        for kill in (real_kill, not_ours):
            with unittest.mock.patch.object(run, "lock_held", lambda desk: None), \
                    unittest.mock.patch.object(run.os, "kill", kill):
                lock.write_text(f"{other.pid} {start}")
                self.round_at(run.STUCK_AFTER + 60)
                self.assertEqual(run.server_state(self.desk)["state"], "stuck" if start else "gone")
                if start:  # a start that does not match: another process given the number since
                    lock.write_text(f"{other.pid} {start}0")
                    self.assertEqual(run.server_state(self.desk)["state"], "gone")
                lock.write_text(f"{other.pid}")  # a number alone: the heartbeat, as before
                self.round_at(0)
                self.assertEqual(run.server_state(self.desk)["state"], "up")
                self.round_at(run.ROUND_QUIET + 5)
                self.assertEqual(run.server_state(self.desk)["state"], "gone")

    def test_asking_whether_a_server_runs_does_not_keep_one_from_starting(self):
        self.desk.state_dir.mkdir(exist_ok=True)
        asking = open(self.desk.state_dir / "serve.lock", "a")  # as run.lock_held holds it for a moment
        fcntl.flock(asking, fcntl.LOCK_SH | fcntl.LOCK_NB)
        threading.Timer(0.3, asking.close).start()
        held = cli.hold_server_lock(self.desk)
        self.assertIsNotNone(held)
        self.addCleanup(held.close)
        from hansa import run
        self.assertEqual(run.lock_held(self.desk), True)
        self.assertEqual(run.server_state(self.desk)["pid"], os.getpid())

    def test_a_server_that_said_it_stopped_is_not_said_to_run_again_before_its_next_round(self):
        stopped = [{"key": ["server"], "line": "hansa serve stopped"}]  # read while its process is still ending
        items, down, stuck = notify.server_news({"server": {"state": "up", "quiet": 1}}, stopped, True, 0.0, 0.0)
        self.assertEqual([x["line"] for x in items], ["hansa serve stopped"])  # its last round came before
        self.assertTrue(down)
        items, down, stuck = notify.server_news({"server": {"state": "up", "quiet": 0}}, [], True, down - 5, 0.0)
        self.assertEqual(([x["line"] for x in items], down, stuck), (["hansa serve runs again"], 0.0, 0.0))

    def test_a_follower_restarts_on_new_code_and_keeps_its_position(self):
        code = self.tmp / "code"  # hansa's code as installed: a copy, so the test can upgrade it
        shutil.copytree(Path(__file__).resolve().parent.parent / "hansa", code / "hansa",
                        ignore=shutil.ignore_patterns("__pycache__"))
        p, sock = self.session()
        r = subprocess.run([sys.executable, "-m", "hansa", "--desk", str(self.desk.root), "notify", "on"], cwd=self.tmp,
                           env={**os.environ, "PYTHONPATH": str(code), "CLAUDE_PID": str(p.pid),
                                "CLAUDE_CODE_MESSAGING_SOCKET": sock}, capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        live = self.soon(lambda: notify.live_records(self.desk))
        follower = notify.running(self.desk, live[0]["key"])
        log = notify.private_dir(self.desk) / f"{live[0]['key']}.log"
        self.hansa_out("add", "Before the upgrade")
        self.settle(Dispatcher(self.desk), "t0001")
        self.soon(lambda: self.posts(sock))
        f = code / "hansa" / "notify.py"
        old = f.read_text()
        f.write_text(old.replace('f"[hansa] desk {desk_root}"', 'f"[hansa, upgraded] desk {desk_root}"'))
        self.assertNotEqual(f.read_text(), old)
        self.soon(lambda: "restarted on hansa's new code" in log.read_text())
        at = re.search(r"restarting on it from byte (\d+)", log.read_text())[1]
        self.assertIn(f"restarted on hansa's new code; following desk {self.desk.root} for session {p.pid} at {sock}, "
                      f"from byte {at}", log.read_text())
        self.assertEqual(notify.running(self.desk, live[0]["key"])["pid"], follower["pid"])  # the same process
        self.hansa_out("add", "After the upgrade")
        self.settle(Dispatcher(self.desk), "t0002")
        later = self.soon(lambda: [x for x in self.posts(sock) if "t0002" in x])
        self.assertTrue(later[0].startswith(f"[hansa, upgraded] desk {self.desk.root}\n"), later[0])
        self.assertEqual(sum("t0001" in x for x in self.posts(sock)), 1)  # nothing said twice, no catch-up

    # the daily clear: a stand-in tmux answers capture-pane from a file and records send-keys; a stand-in claude
    # answers agents --json from a file
    RULE = "\x1b[38;5;244m" + "─" * 60 + "\x1b[39m"
    BOX_EMPTY = "\x1b[39m❯\xa0"
    BOX_TYPED = "\x1b[39m❯\xa0\x1b[38;5;153m/clear\x1b[39m"
    # the bottom lines of a session started with --name cleartest: the rule above the box carries its name
    NAMED_RULE = "\x1b[38;5;244m" + "─" * 100 + " \x1b[38;5;246mcleartest\x1b[38;5;244m ─\x1b[39m"
    PLAIN_RULE = "\x1b[38;5;244m" + "─" * 113 + "\x1b[39m"
    FOOTER = "  \x1b[38;5;211m⏵⏵ auto mode on\x1b[38;5;246m (shift+tab to cycle) · ← for agents\x1b[39m"

    def test_box_text_reads_the_box_under_a_rule_with_the_sessions_name_or_none(self):
        def capture(*box):
            return "\n".join(["\x1b[39m⏺ Done.", "", *box, self.FOOTER, "", ""])
        for name, box, want in (
                ("empty", self.BOX_EMPTY, ""),
                ("a dimmed placeholder", '\x1b[39m❯\xa0\x1b[2mTry "fix lint errors"\x1b[0m', ""),
                ("typed words", "\x1b[39m❯\xa0half-typed words", "half-typed words")):
            with self.subTest(name):
                self.assertEqual(notify.box_text(capture(self.NAMED_RULE, box, self.PLAIN_RULE)).strip(" "), want)
                self.assertEqual(notify.box_text(capture(self.PLAIN_RULE, box, self.PLAIN_RULE)).strip(" "), want)
        self.assertEqual(notify.ESC.sub("", self.NAMED_RULE).split("─ ")[-1], "cleartest ─")
        question = ["\x1b[38;5;153m Which colour?\x1b[39m",
                    "\x1b[38;5;153m❯\x1b[39m \x1b[38;5;246m1.\x1b[39m \x1b[38;5;153mRed\x1b[39m",
                    "  \x1b[38;5;246m2.\x1b[39m Blue", "", "\x1b[2mEnter to select · Esc to cancel\x1b[0m"]
        self.assertIsNone(notify.box_text("\n".join(["\x1b[39m⏺ Done.", "", self.PLAIN_RULE, *question, ""])))
        self.assertIsNone(notify.box_text(capture(self.NAMED_RULE, *question, self.PLAIN_RULE)))
        for line in ("──", "── cleartest ─", "──────x", "─────── cleartest", "❯\xa0"):
            self.assertFalse(notify._rule(line), line)

    def test_box_text_needs_a_rule_above_the_box_and_the_prompt_with_a_no_break_space(self):
        def capture(*box):
            return "\n".join(["\x1b[39m⏺ Done.", "", *box, self.FOOTER, "", ""])
        self.assertEqual(notify.box_text(capture(self.PLAIN_RULE, self.BOX_EMPTY, self.PLAIN_RULE)), "")
        earlier = "\x1b[38;5;239m\x1b[48;5;237m❯ \x1b[38;5;231mUse the scout on lane a\x1b[0m"  # a message sent
        for name, box in (("an earlier message", [self.PLAIN_RULE, earlier, self.PLAIN_RULE]),
                          ("an ordinary space", [self.PLAIN_RULE, "\x1b[39m❯ ", self.PLAIN_RULE]),
                          ("words after an ordinary space", [self.PLAIN_RULE, "\x1b[39m❯ half-typed", self.PLAIN_RULE]),
                          ("no rule above", [self.BOX_EMPTY, self.PLAIN_RULE]),
                          ("a line above", ["\x1b[39m  some output", self.BOX_EMPTY, self.PLAIN_RULE])):
            with self.subTest(name):
                self.assertIsNone(notify.box_text(capture(*box)))

    def pane(self, *box) -> str:
        """A capture of the session's pane: the conversation, then the input box (these lines), and the footer."""
        return "\n".join(["\x1b[38;5;239m\x1b[48;5;237m❯ \x1b[38;5;231mUse the scout on lane a\x1b[0m",
                          "\x1b[39m⏺ Done: the scout reported.", "", *box, self.FOOTER, "", ""])

    def clearing(self, *, status="idle", waiting=None, listed=True, fails=False, box=None, typed=None,
                 keys_ago=None, window="00-24", pane_pid=None, quiet="0", send_fails="", busy_typed=False,
                 keys_said=None):
        """A session in a stand-in tmux pane, and hansa notify on --clear-daily from it: (what it said, the
        stand-ins' folder, the follower's log). After Enter the pane shows an empty box again.
        quiet: HANSA_CLEAR_QUIET, or None for its default. send_fails: the key whose send-keys fails.
        busy_typed: the session goes busy when /clear is typed. keys_said: what list-clients prints ('fails':
        it fails)."""
        d = self.tmp / f"clear-{uuid.uuid4().hex[:6]}"
        (d / "bin").mkdir(parents=True)
        (d / "bin" / "tmux").write_text(
            "#!/usr/bin/env python3\nimport json, os, sys, time\n"
            f"D = {str(d)!r}\n"
            "a = sys.argv[1:]\nf = lambda n: os.path.join(D, n)\n"
            "open(f('calls.jsonl'), 'a').write(json.dumps(a) + '\\n')\n"
            "sys.stdout.reconfigure(encoding='utf-8')\n"
            "if a[0] == 'display-message':\n"
            "    print(a[-1].replace('#{pane_pid}', open(f('pane_pid')).read()).replace('#{session_id}', '$3'))\n"
            "elif a[0] == 'list-clients':\n"
            "    if os.path.exists(f('keys-said')):\n"
            "        said = open(f('keys-said')).read()\n"
            "        sys.exit('no current client') if said == 'fails' else print(said)\n"
            "    else:\n"
            "        print('\\n'.join(str(int(time.time()) - int(x)) for x in open(f('keys')).read().split()))\n"
            "elif a[0] == 'capture-pane':\n"
            "    sys.stdout.write(open(f('pane.txt'), encoding='utf-8').read())\n"
            "elif a[0] == 'send-keys':\n"
            "    if os.path.exists(f('send-fails')) and a[-1] == open(f('send-fails')).read():\n"
            "        sys.exit('no server running on the socket')\n"
            "    if '-l' in a and os.path.exists(f('pane-typed.txt')):\n"
            "        os.replace(f('pane-typed.txt'), f('pane.txt'))\n"
            "    if '-l' in a and os.path.exists(f('agents-typed.json')):\n"
            "        os.replace(f('agents-typed.json'), f('agents.json'))\n"
            "    if a[-1] == 'Enter' and os.path.exists(f('agents-after.json')):\n"
            "        os.replace(f('agents-after.json'), f('agents.json'))\n"
            "    if a[-1] == 'Enter' and os.path.exists(f('pane-after.txt')):\n"
            "        os.replace(f('pane-after.txt'), f('pane.txt'))\n")
        (d / "bin" / "claude").write_text(
            "#!/usr/bin/env python3\nimport os, sys\n"
            f"D = {str(d)!r}\n"
            "if sys.argv[1:3] == ['agents', '--json']:\n"
            "    if os.path.exists(os.path.join(D, 'fails')):\n"
            "        sys.exit('agents: something broke')\n"
            "    sys.stdout.write(open(os.path.join(D, 'agents.json')).read())\n")
        for x in ("tmux", "claude"):
            (d / "bin" / x).chmod(0o755)
        p, sock = self.session()
        row = {"pid": p.pid, "name": "ntA", "status": status, "sessionId": "s-before",
               **({"waitingFor": waiting} if waiting else {})}
        (d / "agents.json").write_text(json.dumps([row] if listed else []))
        (d / "agents-after.json").write_text(json.dumps([{**row, "sessionId": "s-after"}]))
        if fails:
            (d / "fails").write_text("")
        if send_fails:
            (d / "send-fails").write_text(send_fails)
        if busy_typed:
            (d / "agents-typed.json").write_text(json.dumps([{**row, "status": "busy"}]))
        if keys_said is not None:
            (d / "keys-said").write_text(keys_said)
        (d / "pane_pid").write_text(str(pane_pid or p.pid))
        empty = self.pane(self.NAMED_RULE, self.BOX_EMPTY, self.PLAIN_RULE)
        (d / "pane.txt").write_text(self.pane(*box) if box else empty, encoding="utf-8")
        (d / "pane-after.txt").write_text(empty, encoding="utf-8")
        if typed:  # the box line after /clear is typed, or all the lines then
            (d / "pane-typed.txt").write_text(self.pane(*typed) if isinstance(typed, list) else
                                              self.pane(self.NAMED_RULE, typed, self.PLAIN_RULE), encoding="utf-8")
        (d / "keys").write_text("" if keys_ago is None else str(keys_ago))
        env = {"PATH": f"{d / 'bin'}{os.pathsep}{os.environ['PATH']}", "HANSA_CLAUDE": str(d / "bin" / "claude"),
               "TMUX_PANE": "%7", "TMUX": f"{d}/tmux-socket,1,0", "HANSA_CLEAR_EVERY": "0.3",
               "HANSA_CLEAR_STARTED": "1",  # a session of long ago: today's clear is due
               **({"HANSA_CLEAR_QUIET": quiet} if quiet is not None else {})}
        with unittest.mock.patch.dict(os.environ, env), self.in_session(p, sock):
            if quiet is None:
                os.environ.pop("HANSA_CLEAR_QUIET", None)
            said = self.hansa_out("notify", "on", "--clear-daily", window)
        key = notify.key_of({"pid": p.pid, "start": notify.proc_start(p.pid)})
        return said, d, notify.private_dir(self.desk) / f"{key}.log"

    def keys_sent(self, d) -> list:
        f = d / "calls.jsonl"
        return [json.loads(x)[3:] for x in f.read_text().splitlines() if json.loads(x)[0] == "send-keys"] \
            if f.exists() else []

    def held(self, log, why, times=3):
        """Wait until the follower's log holds the clear back for this reason a few times."""
        self.soon(lambda: log.exists() and log.read_text().count(f"daily clear held: {why}") >= times)

    def test_daily_clear_types_clear_then_enter_when_the_session_is_idle_and_nobody_types(self):
        said, d, log = self.clearing(typed=self.BOX_TYPED, keys_ago=200)
        self.assertIn("hansa types /clear into tmux pane %7", said)
        self.assertIn("cannot add a hook to a session that is already running", said)
        self.soon(lambda: "cleared the session (sessionId s-before -> s-after)" in log.read_text())
        self.assertIn("daily clear held: idle on one reading", log.read_text())  # two readings, then it typed
        self.assertEqual(self.keys_sent(d), [["-l", "/clear"], ["Enter"]])
        self.soon(lambda: re.search(r"daily clear 00-24: cleared at \d\d:\d\d", self.hansa_out("notify")))
        checks = len((d / "calls.jsonl").read_text().splitlines())
        time.sleep(2.5)  # the box is empty and the session idle again, and still: once a day
        self.assertIn(self.BOX_EMPTY, (d / "pane.txt").read_text(encoding="utf-8"))
        self.assertEqual(self.keys_sent(d), [["-l", "/clear"], ["Enter"]])
        self.assertEqual(len((d / "calls.jsonl").read_text().splitlines()), checks)  # not even looked at

    def test_daily_clear_waits_five_minutes_after_hansa_posted_to_the_session(self):
        _, d, log = self.clearing(typed=self.BOX_TYPED, keys_ago=30, quiet=None)  # HANSA_CLEAR_QUIET's default
        self.held(log, "a key was pressed", 1)
        self.desk.event("server_stopped")
        self.soon(lambda: "posted: " in log.read_text())
        (d / "keys").write_text("")  # nobody types now; all else holds but the post
        self.held(log, "hansa posted to the session ", 3)
        self.assertRegex(log.read_text(), r"daily clear held: hansa posted to the session \d+ s ago\n")
        self.assertEqual(self.keys_sent(d), [])

    def test_daily_clear_leaves_it_for_the_day_when_tmux_fails_or_the_box_holds_something_else(self):
        for name, kw, what in (("tmux fails", {"typed": self.BOX_TYPED, "send_fails": "Enter"},
                                r"tmux failed \(tmux send-keys ended with 1: no server running on the socket\)"),
                               ("something else", {"typed": "\x1b[39m❯\xa0/clea"}, r"the box read '/clea'")):
            with self.subTest(name):
                _, d, log = self.clearing(**kw)
                self.soon(lambda: "/clear may be left in the box" in log.read_text())
                self.soon(lambda: re.search(rf"daily clear 00-24: /clear typed at \d\d:\d\d, but then {what}: "
                                            r"/clear may be left in the box", self.hansa_out("notify")))
                (d / "pane.txt").write_text((d / "pane-after.txt").read_text(encoding="utf-8"), encoding="utf-8")
                time.sleep(2.5)  # the box is empty again, and still no second try that day
                self.assertEqual(self.keys_sent(d), [["-l", "/clear"], *([["Enter"]] if "send_fails" in kw else [])])

    def test_daily_clear_types_nothing_unless_claude_agents_shows_the_session_idle(self):
        for kw, why in (({"status": "busy"}, "the session is 'busy', not idle"),
                        ({"status": "shell"}, "the session is 'shell', not idle"),
                        ({"status": "waiting", "waiting": "input needed"}, "the session is 'waiting', not idle"),
                        ({"status": "idle", "waiting": "input needed"}, "the session waits: input needed"),
                        ({"listed": False}, "claude agents --json lists no session with pid"),
                        ({"fails": True}, "claude agents --json ended with 1: agents: something broke")):
            with self.subTest(**kw):
                _, d, log = self.clearing(typed=self.BOX_TYPED, **kw)
                self.held(log, why)
                self.assertEqual(self.keys_sent(d), [])

    def test_daily_clear_types_only_into_an_empty_input_box(self):
        _, d, log = self.clearing(box=[self.RULE, '\x1b[39m❯\xa0\x1b[2mTry "fix lint errors"\x1b[0m', self.RULE],
                                  typed=self.BOX_TYPED)  # a dimmed placeholder: empty
        self.soon(lambda: "cleared the session" in log.read_text())
        self.assertEqual(self.keys_sent(d), [["-l", "/clear"], ["Enter"]])
        dialog = ["\x1b[38;5;244m Do you want to make this edit?\x1b[39m",
                  "\x1b[38;5;153m❯\x1b[39m \x1b[38;5;246m1.\x1b[39m \x1b[38;5;153mYes\x1b[39m",
                  "  \x1b[38;5;246m2.\x1b[39m No"]
        for name, box, why in (
                ("typed text", [self.RULE, "\x1b[39m❯\xa0half-typed words", self.RULE], "there are words in the input box"),
                ("a dialog", [self.RULE, *dialog], "no input box alone at the bottom of the pane"),
                ("a box of lines", [self.RULE, "\x1b[39m❯\xa0first line", "  second line", self.RULE],
                 "no input box alone at the bottom of the pane")):
            with self.subTest(name):
                _, d, log = self.clearing(box=box, typed=self.BOX_TYPED)
                self.held(log, why)
                self.assertEqual(self.keys_sent(d), [])

    def test_daily_clear_waits_while_someone_pressed_a_key_lately(self):
        _, d, log = self.clearing(typed=self.BOX_TYPED, keys_ago=30)
        self.held(log, "a key was pressed in a terminal on the session's tmux session 30 s ago")
        self.assertEqual(self.keys_sent(d), [])

    def test_daily_clear_counts_a_key_press_time_tmux_cannot_give_as_busy(self):
        for said, why in (("now", "could not read when a key was last pressed (invalid literal"),
                          ("fails", "could not read when a key was last pressed (tmux list-clients ended with 1: "
                                    "no current client)")):
            with self.subTest(said):
                _, d, log = self.clearing(typed=self.BOX_TYPED, keys_said=said)
                self.held(log, why)
                self.assertEqual(self.keys_sent(d), [])

    def test_daily_clear_takes_its_clear_back_when_the_session_went_busy_as_it_typed(self):
        _, d, log = self.clearing(typed=self.BOX_TYPED, busy_typed=True)
        self.soon(lambda: "the /clear was taken back" in log.read_text())
        self.assertIn("daily clear held: after /clear was typed the box read '/clear' and the session was not idle: "
                      "the session is 'busy', not idle; no Enter; the /clear was taken back", log.read_text())
        self.held(log, "the session is 'busy', not idle", 2)
        self.assertEqual(self.keys_sent(d), [["-l", "/clear"], ["BSpace"] * 6])

    def test_daily_clear_says_what_it_knows_when_typing_or_taking_back_clear_fails(self):
        failed = r"tmux failed \(tmux send-keys ended with 1: no server running on the socket\)"
        for name, kw, logged, shown in (
                ("typing", {"typed": self.BOX_TYPED, "send_fails": "/clear"},
                 rf"daily clear: hansa tried to type /clear, but then {failed}; no Enter, nothing taken back, and no "
                 r"other try today: /clear may be left in the box",
                 rf"daily clear 00-24: hansa tried to type /clear at \d\d:\d\d, but then {failed}: /clear may be "
                 r"left in the box"),
                ("taking it back", {"typed": "\x1b[39m❯\xa0half-typed words/clear", "send_fails": "BSpace"},
                 rf"daily clear: typed /clear, but then {failed}; no Enter, it is not known how many of the six "
                 r"backspaces taking it back went through, and no other try today: /clear may be left in the box",
                 rf"daily clear 00-24: /clear typed at \d\d:\d\d, but then {failed}; it is not known how many of "
                 r"the six backspaces taking it back went through: /clear may be left in the box")):
            with self.subTest(name):
                _, d, log = self.clearing(**kw)
                self.soon(lambda: re.search(logged, log.read_text()))
                self.soon(lambda: re.search(shown, self.hansa_out("notify")))
                self.assertNotIn("the /clear was taken back", log.read_text())
                time.sleep(1.5)  # no other try that day
                self.assertEqual(self.keys_sent(d), [["-l", "/clear"], *([["BSpace"] * 6] if name != "typing" else [])])

    def daily(self, every="60"):
        """A DailyClear for window 00-24 in this process, on pane %7 of pid 1; what it says is left out."""
        say = unittest.mock.patch.object(notify, "_say")
        say.start()
        self.addCleanup(say.stop)
        with unittest.mock.patch.dict(os.environ, {"HANSA_CLEAR_EVERY": every}):
            return notify.DailyClear("00-24", 1, {"window": "00-24", "pane": "%7", "off": None})

    def test_daily_clear_checks_a_minute_apart(self):
        c, t0, clock = self.daily(), time.time(), [0.0]
        fake = unittest.mock.Mock(wraps=time, time=lambda: t0 + clock[0])
        reading = unittest.mock.Mock(return_value=(False, "the session is 'busy', not idle"))
        with unittest.mock.patch.object(notify, "time", fake), unittest.mock.patch.object(notify, "idle_reading", reading):
            for clock[0] in (0, 1, 30, 59.9, 60, 61, 100, 120.5):
                c.tick(0, False)
        self.assertEqual(reading.call_count, 3)  # at 0, 60 and 120.5

    def fresh_clear(self, window, now, began, rec=None):
        """A DailyClear with no record carried over (unless rec), made at local time now ("2026-09-27 06:27") for a
        session that started at began (the same form, or None when that cannot be read); and the fake time."""
        at = lambda x: time.mktime(time.strptime(x, "%Y-%m-%d %H:%M"))
        clock = [at(now)]
        fake = unittest.mock.Mock(wraps=time, time=lambda: clock[0])
        say = unittest.mock.patch.object(notify, "_say")
        say.start()
        self.addCleanup(say.stop)
        with unittest.mock.patch.object(notify, "time", fake), \
                unittest.mock.patch.object(notify, "clear_pane", return_value=("%7", "")), \
                unittest.mock.patch.object(notify, "started_at", return_value=began and at(began)):
            return notify.DailyClear(window, 1, rec), fake, clock, at

    def test_daily_clear_leaves_alone_a_session_that_started_inside_todays_window(self):
        c, fake, clock, at = self.fresh_clear("04-09", "2026-09-27 06:27", "2026-09-27 05:56")
        self.assertEqual((c.rec["day"], c.rec["state"]), ("2026-09-27", "new"))
        self.assertEqual(notify.clear_text(c.rec, clock[0]),
                         "daily clear 04-09: started 05:56, inside today's window; next clear tomorrow")
        reading = unittest.mock.Mock(return_value=(True, "s-before"))
        with unittest.mock.patch.object(notify, "time", fake), unittest.mock.patch.object(notify, "idle_reading", reading):
            for clock[0] in (at("2026-09-27 06:27"), at("2026-09-27 07:30"), at("2026-09-27 08:59")):
                c.tick(0, False)
            self.assertEqual(reading.call_count, 0)  # today is done for it
            c.next, clock[0] = 0, at("2026-09-28 04:10")
            c.tick(0, False)  # tomorrow: cleared as any other
        self.assertEqual(reading.call_count, 1)
        self.assertEqual(c.rec["day"], "2026-09-28")
        self.assertIsNone(c.rec["state"])
        c, *_ = self.fresh_clear("22-03", "2026-09-28 01:00", "2026-09-27 23:30")  # a window past midnight
        self.assertEqual((c.rec["day"], c.rec["state"]), ("2026-09-27", "new"))

    def test_notify_says_the_next_clear_is_tomorrow_for_a_session_that_started_after_todays_window(self):
        for window, now, began, said in (
                ("04-09", "2026-09-27 10:00", "2026-09-27 09:30", "started 09:30, after today's window"),
                ("04-09", "2026-09-27 10:00", "2026-09-27 05:56", "started 05:56, inside today's window"),
                ("22-03", "2026-09-28 10:00", "2026-09-28 04:00", "started 04:00, after today's window"),
                ("22-03", "2026-09-28 10:00", "2026-09-27 23:30", "started 23:30, inside today's window")):
            with self.subTest(window=window, began=began):
                c, fake, clock, at = self.fresh_clear(window, now, began)
                self.assertIsNone(c.rec.get("state"))  # its follower started after the window: nothing done today
                self.assertEqual(notify.clear_text(c.rec, clock[0]),
                                 f"daily clear {window}: {said}; next clear tomorrow")
        for began in ("2026-09-27 03:59", None):  # it was due, or it may have been: said as before
            c, fake, clock, at = self.fresh_clear("04-09", "2026-09-27 10:00", began)
            self.assertEqual(notify.clear_text(c.rec, clock[0]), "daily clear 04-09: window passed without a clear")
        c, fake, clock, at = self.fresh_clear("04-09", "2026-09-27 10:00", "2026-09-27 09:30")
        self.assertEqual(notify.clear_text(c.rec, at("2026-09-28 10:00")),  # the next day's window passed: as before
                         "daily clear 04-09: window passed without a clear")
        with unittest.mock.patch.object(notify, "started_at", return_value=at("2026-09-27 09:30")):
            old = notify.DailyClear("04-09", 1, {"window": "04-09", "pane": "%7", "off": None})  # kept no start
        self.assertEqual(old.rec["began"], at("2026-09-27 09:30"))

    def test_daily_clear_goes_on_for_a_session_older_than_todays_window_or_of_unknown_start(self):
        for name, window, now, began, rec in (
                ("started before the window opened", "04-09", "2026-09-27 06:27", "2026-09-27 03:59", None),
                ("started yesterday", "04-09", "2026-09-27 06:27", "2026-09-26 05:00", None),
                ("before a window past midnight", "22-03", "2026-09-28 01:00", "2026-09-27 21:30", None),
                ("start cannot be read", "04-09", "2026-09-27 06:27", None, None),
                ("follower started outside the window", "04-09", "2026-09-27 10:00", "2026-09-27 05:56", None),
                ("record carried over", "04-09", "2026-09-27 06:27", "2026-09-27 05:56",
                 {"window": "04-09", "pane": "%7", "off": None})):
            with self.subTest(name):
                c, *_ = self.fresh_clear(window, now, began, rec)
                self.assertIsNone(c.rec.get("state"))
        c, fake, clock, at = self.fresh_clear("04-09", "2026-09-27 10:00", "2026-09-27 05:56")
        reading = unittest.mock.Mock(return_value=(False, "the session is 'busy', not idle"))
        with unittest.mock.patch.object(notify, "time", fake), unittest.mock.patch.object(notify, "idle_reading", reading):
            clock[0] = at("2026-09-28 04:30")
            c.tick(0, False)
        self.assertEqual(reading.call_count, 1)  # its next window: checked as always

    def test_a_sessions_start_is_read_from_its_process(self):
        began = notify.started_at(os.getpid())
        if began is None:
            self.skipTest("this process's start cannot be read here")
        self.assertLessEqual(began, time.time())
        self.assertGreater(began, time.time() - 86400)
        self.assertIsNone(notify.started_at(2 ** 22 + 12345))  # no such process

    def test_daily_clear_needs_two_idle_readings_a_minute_apart(self):
        c, t0 = self.daily(), time.time()
        busy, idle = (False, "the session is 'busy', not idle"), (True, "s-before")
        with unittest.mock.patch.object(notify, "idle_reading") as reading, \
                unittest.mock.patch.object(notify, "tmux", side_effect=RuntimeError("a stand-in")):
            for dt, said, why in ((0, idle, "idle on one reading; a second one comes a minute later"),
                                  (30, idle, "idle on readings less than a minute apart"),
                                  (59, idle, "idle on readings less than a minute apart"),
                                  (60, idle, "could not read when a key was last pressed (a stand-in)"),
                                  (70, busy, "the session is 'busy', not idle"),
                                  (140, idle, "idle on one reading; a second one comes a minute later"),
                                  (150, idle, "idle on readings less than a minute apart")):
                reading.return_value = said
                self.assertEqual(c.why_not(t0 + dt, 0, False), why, dt)

    def test_daily_clear_counts_an_unchanged_session_id_after_enter_as_not_cleared_and_tries_no_more_that_day(self):
        c, sent = self.daily("0.01"), []
        rule, empty, typed = self.PLAIN_RULE, self.BOX_EMPTY, self.BOX_TYPED

        def tmux(*a):
            if a[0] == "send-keys":
                sent.append(list(a[3:]))
            if a[0] == "capture-pane":
                return self.pane(rule, typed if sent else empty, rule)
            return "$3" if a[0] == "display-message" else ""
        with unittest.mock.patch.object(notify, "tmux", tmux), \
                unittest.mock.patch.object(notify, "idle_reading", return_value=(True, "s-before")), \
                unittest.mock.patch.object(notify, "session_row", return_value={"sessionId": "s-before"}), \
                unittest.mock.patch.object(notify, "CLEARED_WAIT", 0.2), \
                unittest.mock.patch.object(notify, "TYPED_WAIT", 0), \
                unittest.mock.patch.object(notify, "KEYS_QUIET", 0):
            for _ in range(6):
                c.tick(0, False)
                time.sleep(0.02)
        self.assertEqual(sent, [["-l", "/clear"], ["Enter"]])
        self.assertEqual(c.rec["state"], "unconfirmed")
        self.assertRegex(notify.clear_text(c.rec), r"^daily clear 00-24: Enter pressed at \d\d:\d\d, but no new "
                                                   r"session was seen$")

    def test_daily_clear_takes_its_clear_back_when_the_box_reads_more_than_that(self):
        _, d, log = self.clearing(typed="\x1b[39m❯\xa0half-typed words\x1b[38;5;153m/clear\x1b[39m")
        self.soon(lambda: "the /clear was taken back" in log.read_text())
        self.held(log, "there are words in the input box", 2)  # the next checks find the words and wait
        self.assertEqual(self.keys_sent(d), [["-l", "/clear"], ["BSpace"] * 6])

    def test_daily_clear_leaves_it_for_the_day_when_the_box_cannot_be_read_after_clear_is_typed(self):
        _, d, log = self.clearing(typed=[self.NAMED_RULE, "\x1b[39m❯\xa0/clear", "\x1b[39m  /clear  Clear the "
                                         "conversation", "  /compact  Compact it", self.PLAIN_RULE])
        self.soon(lambda: "/clear may be left in the box" in log.read_text())
        self.assertEqual(len([x for x in (d / "calls.jsonl").read_text().splitlines() if "capture-pane" in x]),
                         1 + 1 + notify.TYPED_READS)  # the check, then the reads over two seconds after typing
        self.soon(lambda: re.search(r"daily clear 00-24: /clear typed at \d\d:\d\d, but then the input box could not be "
                                    r"read: /clear may be left in the box", self.hansa_out("notify")))
        (d / "pane.txt").write_text((d / "pane-after.txt").read_text(encoding="utf-8"), encoding="utf-8")
        time.sleep(2.5)  # the box is empty again, and still no second try that day
        self.assertEqual(self.keys_sent(d), [["-l", "/clear"]])

    def test_daily_clear_types_nothing_outside_its_window_or_outside_the_sessions_pane(self):
        h = time.localtime().tm_hour
        said, d, log = self.clearing(typed=self.BOX_TYPED, window=f"{(h + 2) % 24:02d}-{(h + 3) % 24:02d}")
        self.soon(lambda: f"outside the window {(h + 2) % 24:02d}-" in log.read_text())
        time.sleep(1)
        self.assertEqual(self.keys_sent(d), [])
        said, d, log = self.clearing(typed=self.BOX_TYPED, pane_pid=1)  # the pane runs something else
        self.assertIn("The daily clear is off: the session (pid ", said)
        self.soon(lambda: "daily clear 00-24: off: the session" in log.read_text())
        self.assertRegex(self.hansa_out("notify"), r"daily clear 00-24: off \(the session \(pid \d+\) does not run in")
        time.sleep(1)
        self.assertEqual(self.keys_sent(d), [])

    def test_notify_on_twice_from_one_session_starts_one_follower(self):
        with self.assertRaises(SystemExit) as e:  # not from a Claude Code session: it says why, starts nothing
            main(["--desk", str(self.desk.root), "notify", "on"])
        self.assertIn("not run from one", str(e.exception.code))
        p, sock = self.session()
        hansa = str(Path(__file__).resolve().parent.parent / "bin" / "hansa")
        env = {**os.environ, "CLAUDE_PID": str(p.pid), "CLAUDE_CODE_MESSAGING_SOCKET": sock}
        # it returns at once: the follower keeps none of the caller's files, so its output pipe closes
        r = subprocess.run([hansa, "--desk", str(self.desk.root), "notify", "on"], capture_output=True, text=True,
                           env=env, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("now posts to this session", r.stdout)
        with self.in_session(p, sock):
            self.assertIn("already notifies this session", self.hansa_out("notify", "on"))
        live = self.soon(lambda: notify.live_records(self.desk))
        self.assertEqual([x["session"]["pid"] for x in live], [p.pid])
        follower = notify.running(self.desk, live[0]["key"])
        with self.in_session(p, sock):
            self.assertIn("no longer posts", self.hansa_out("notify", "off"))
            self.assertIn("does not notify this session", self.hansa_out("notify", "off"))
        self.assertFalse(notify.alive(follower["pid"], follower["start"]))
        self.assertEqual(notify.live_records(self.desk), [])
        self.assertIn("notifies no session", self.hansa_out("notify"))

    def test_a_request_answered_on_the_page_reaches_the_session_that_asked_with_the_answer(self):
        skip_unless_ports(self)
        (a, sa), (b, sb) = self.session(), self.session()
        for p, sock in ((a, sa), (b, sb)):
            with self.in_session(p, sock):
                self.hansa_out("notify", "on")
        with self.in_session(a, sa):
            self.hansa_out("ask", "May the docs lane use the npm registry?")
        self.assertEqual(self.desk.asks()[0]["session"]["pid"], a.pid)
        self.desk.write_key("mimo", "sk-mimo-12345678")
        httpd = start_web(self.desk, "127.0.0.1", 0)
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{httpd.server_address[1]}/api/ask/close?t="
                                         f"{self.desk.web_token()}", method="POST", headers={"X-Hansa": "1"},
                                         data=json.dumps({"id": 1, "answer": "Yes; its key is sk-mimo-12345678"}).encode())
            with urllib.request.urlopen(req) as r:
                self.assertTrue(json.load(r)["ok"])
        finally:
            httpd.shutdown()
            httpd.server_close()
        heard = self.soon(lambda: self.posts(sa))
        self.assertIn("- request #1 answered: Yes; its key is [key of mimo] (you asked: May the docs lane use the npm "
                      "registry?)", heard[0])
        self.assertNotIn("sk-mimo-12345678", heard[0])
        time.sleep(1)
        self.assertEqual(self.posts(sb), [])

    def test_an_open_request_naming_a_provider_that_is_ready_now_asks_whether_it_is_done(self):
        s = self.desk.settings()
        s["providers"]["mimo"] = {"kind": "compatible", "model": "m", "effort": "", "base_url": "https://x"}
        self.assertEqual(self.desk.save_settings(s), [])
        self.desk.add_ask("Please fill in mimo's key")
        self.desk.add_ask("Is mimosa a good lane name?")  # names no provider: mimo only as a whole word
        self.assertEqual([a["note"] for a in views.status(self.desk)["asks"]], ["", ""])  # mimo has no key yet
        self.desk.write_key("mimo", "sk-mimo-12345678")
        st = views.status(self.desk)
        self.assertEqual([a["note"] for a in st["asks"]], ["mimo is ready now: done?", ""])
        text = views.status_text(st)
        self.assertIn("  #1: Please fill in mimo's key · mimo is ready now: done?\n", text)
        self.assertIn("  #2: Is mimosa a good lane name?\n", text)
        self.assertEqual([a["closed"] for a in self.desk.asks()], [None, None])  # hansa closes nothing itself
        heard = notify.items_for(self.desk, self.events("ask_ready"), {"key": "k", "session": None})
        self.assertEqual([x["line"] for x in heard], ["mimo is ready now: done? Still open: request #1 (Please fill in "
                                                      "mimo's key) (the principal closes a request on the page; if it is done, "
                                                      "close it: hansa ask --close N \"why\")"])
        self.desk.close_ask(1)
        self.assertEqual(notify.items_for(self.desk, self.events("ask_ready"), {"key": "k", "session": None}), [])

    def test_the_ready_now_line_comes_once_and_again_only_after_the_provider_stopped_being_ready(self):
        s = self.desk.settings()
        s["providers"]["mimo"] = {"kind": "compatible", "model": "m", "effort": "", "base_url": "https://x"}
        self.assertEqual(self.desk.save_settings(s), [])
        self.desk.add_ask("Please fill in mimo's key")
        self.desk.write_key("mimo", "sk-mimo-12345678")
        self.desk.write_key("mimo", "sk-mimo-87654321")  # saved again: still ready, nothing new
        self.desk.event("provider_checked", provider="mimo", ok=True, detail="")
        self.desk.ready_news()  # as a passing check does
        self.desk.save_settings(self.desk.settings())
        self.assertEqual([e["ids"] for e in self.events("ask_ready")], [[1]])
        self.assertEqual(len(notify.items_for(self.desk, self.events("ask_ready"), {"key": "k", "session": None})), 1)
        self.desk.write_key("mimo", "")  # it stops being ready, then is ready again: said once more
        self.desk.write_key("mimo", "sk-mimo-12345678")
        self.desk.write_key("mimo", "sk-mimo-87654321")
        self.assertEqual([e["ids"] for e in self.events("ask_ready")], [[1], [1]])
        self.desk.close_ask(1)
        self.desk.write_key("mimo", "")
        self.desk.write_key("mimo", "sk-mimo-12345678")  # a closed request is not told
        self.assertEqual(len(self.events("ask_ready")), 2)

    def test_a_request_naming_a_provider_not_set_up_yet_gets_the_note_once_it_is_ready(self):
        self.desk.add_ask("Please set up provider kimi and its key")
        self.assertNotIn("kimi", self.desk.asks()[0]["ready_then"])
        s = self.desk.settings()
        s["providers"]["kimi"] = {"kind": "compatible", "model": "m", "effort": "", "base_url": "https://x"}
        self.assertEqual(self.desk.save_settings(s), [])
        self.assertEqual([a["note"] for a in views.status(self.desk)["asks"]], [""])  # no key yet
        self.desk.write_key("kimi", "sk-kimi-12345678")
        self.assertEqual([a["note"] for a in views.status(self.desk)["asks"]], ["kimi is ready now: done?"])
        self.assertEqual([(e["provider"], e["ids"]) for e in self.events("ask_ready")], [("kimi", [1])])

    def test_a_provider_ready_again_after_its_rest_is_posted_once(self):
        s = self.desk.settings()
        s["providers"]["mimo"] = {"kind": "compatible", "model": "m", "effort": "", "base_url": "https://x"}
        self.assertEqual(self.desk.save_settings(s), [])
        self.desk.write_key("mimo", "sk-mimo-12345678")
        self.desk.set_provider_state("mimo", cool_until=time.time() + 600, reason="out of quota")
        self.desk.add_ask("mimo hit its limit: please raise it")
        self.assertNotIn("mimo", self.desk.asks()[0]["ready_then"])
        d = Dispatcher(self.desk)
        d.tick()
        self.assertEqual(self.events("ask_ready"), [])  # still resting
        self.desk.set_provider_state("mimo", cool_until=time.time() - 1)  # its rest ran out: no save, no check
        d.tick()
        d.tick()
        self.assertEqual([(e["provider"], e["ids"]) for e in self.events("ask_ready")], [("mimo", [1])])
        heard = notify.items_for(self.desk, self.events("ask_ready"), {"key": "k", "session": None})
        self.assertEqual([x["line"][:47] for x in heard], ["mimo is ready now: done? Still open: request #1"])
        Dispatcher(self.desk).tick()  # a server started again: told already, so nothing new
        self.assertEqual(len(self.events("ask_ready")), 1)
        self.desk.set_provider_state("mimo", cool_until=time.time() + 600)  # rests again, then is ready again
        d.tick()
        self.desk.set_provider_state("mimo", cool_until=time.time() - 1)
        d.tick()
        self.assertEqual(len(self.events("ask_ready")), 2)

    def test_the_coordinator_closes_its_own_request_with_a_reason(self):
        s = self.desk.settings()
        s["providers"]["mimo"] = {"kind": "compatible", "model": "m", "effort": "", "base_url": "https://x"}
        self.assertEqual(self.desk.save_settings(s), [])
        self.desk.add_ask("Please fill in mimo's key")
        self.desk.add_ask("May the docs lane use the npm registry?")
        with self.assertRaises(SystemExit):  # the reason is required
            self.hansa_out("ask", "--close", "1")
        with self.assertRaises(SystemExit) as e:
            self.hansa_out("ask", "--close", "9", "done")
        self.assertIn("there is no request #9", str(e.exception.code))
        with unittest.mock.patch.dict(os.environ, {"HANSA_TASK": "t0001"}):  # a worker, verification or design session
            with self.assertRaises(SystemExit) as e:
                self.hansa_out("ask", "--close", "1", "done")
            self.assertIn("only the coordinator closes a request", str(e.exception.code))
        self.assertEqual([a["closed"] for a in self.desk.asks()], [None, None])
        self.desk.write_key("mimo", "sk-mimo-12345678")  # ready now: the note and one post
        with unittest.mock.patch.dict(os.environ, {"HANSA_ROLE": "coordinator"}):
            out = self.hansa_out("ask", "--close", "1", "The principal filled it in: sk-mimo-12345678")
        self.assertIn("ask #1 is closed", out)
        a = self.desk.asks()[0]
        self.assertTrue(a["closed"])
        self.assertEqual((a["closed_by"], a["reason"]), ("coordinator", "The principal filled it in: [key of mimo]"))
        st = views.status(self.desk)
        self.assertEqual([x["id"] for x in st["asks"]], [2])  # status stops listing it
        self.assertNotIn("#1:", views.status_text(st))
        self.assertEqual([(x["id"], x["reason"]) for x in st["asks_closed"]],
                         [(1, "The principal filled it in: [key of mimo]")])  # the page shows it, with the reason
        closed = self.events("ask_closed")
        self.assertEqual([(e["id"], e["by"], e["coordinator"]) for e in closed], [(1, "coordinator", True)])
        me = {"key": "k", "session": None}
        self.assertEqual(notify.items_for(self.desk, closed, me), [])  # the coordinator knows: no post
        self.assertEqual(notify.items_for(self.desk, self.events("ask_ready"), me), [])  # nor that it may be done
        with self.assertRaises(SystemExit) as e:
            self.hansa_out("ask", "--close", "1", "again")
        self.assertIn("request #1 is already closed by the coordinator", str(e.exception.code))
        self.desk.close_ask(2, "yes", by="page")  # closed on the page: refused as already closed too
        with self.assertRaises(SystemExit) as e:
            self.hansa_out("ask", "--close", "2", "done")
        self.assertIn("request #2 is already closed: yes", str(e.exception.code))
        self.assertEqual(len(self.events("ask_closed")), 2)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            main(["ask", "--help"])
        self.assertIn('the coordinator closes it: hansa ask --close N "why"; the reason is required',
                      " ".join(out.getvalue().split()))

    def test_a_request_made_while_its_provider_was_ready_gets_no_note(self):
        self.assertIn("claude", views.ready_now(self.desk))
        self.desk.add_ask("claude login please, for the docs lane")
        self.assertIn("claude", self.desk.asks()[0]["ready_then"])
        self.assertEqual([a["note"] for a in views.status(self.desk)["asks"]], [""])
        self.desk.ready_news()
        self.assertEqual(self.events("ask_ready"), [])
        asks = self.desk.asks()  # a request from before hansa recorded what was ready: it cannot tell, so no note
        asks.append({"id": 2, "text": "Please fill in claude's login", "by": "", "created": 0, "closed": None,
                     "answer": ""})
        write_json(self.desk.asks_path, asks)
        self.assertEqual([a["note"] for a in views.status(self.desk)["asks"]], ["", ""])
        self.desk.ready_news()
        self.assertEqual(self.events("ask_ready"), [])
        self.assertIn('a.note ? h("span"', (Path(__file__).resolve().parent.parent / "hansa" / "ui.html").read_text())

    def test_a_post_reaches_a_real_inbox(self):
        path = str(self.tmp / "inbox.sock")
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(srv.close)
        try:
            srv.bind(path)
        except OSError as e:
            self.skipTest(f"cannot make a Unix socket here ({e}; a sandbox refuses to)")
        srv.listen(1)
        srv.settimeout(10)
        os.environ.pop("HANSA_NOTIFY_CAPTURE")
        notify.post(path, "[hansa] desk /d\n- t0001 done")
        conn, _ = srv.accept()
        with conn:
            data = b""
            while not data.endswith(b"\n"):
                chunk = conn.recv(4096)
                if not chunk:
                    break
                data += chunk
        self.assertEqual(json.loads(data), {"type": "user", "message": {"role": "user",
                                                                        "content": "[hansa] desk /d\n- t0001 done"}})
        with self.assertRaises(OSError):  # no inbox there (yet): the follower tries again for a minute
            notify.post(str(self.tmp / "none.sock"), "x")

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

    @unittest.skipIf(os.getuid() == 0, "root may write anywhere")
    def test_the_coordinator_turns_off_claude_codes_updates_only_where_this_account_cannot_write_its_install(self):
        def install(where, npm):  # a stand-in claude as npm or the native installer puts it, reached by a link
            pkg = where / "lib" / "node_modules" / "@anthropic-ai" / "claude-code" if npm else where / "versions"
            pkg.mkdir(parents=True)
            (pkg / "cli").write_text("#!/bin/sh\necho \"updater=${DISABLE_AUTOUPDATER-unset}\"\n")
            (pkg / "cli").chmod(0o755)
            (where / "bin").mkdir()
            (where / "bin" / "claude").symlink_to(pkg / "cli")
            return where / "bin" / "claude", where / "lib" / "node_modules" if npm else pkg

        def start(claude, **extra):
            env = {k: v for k, v in os.environ.items() if k != "DISABLE_AUTOUPDATER"}
            r = subprocess.run([str(Path(__file__).resolve().parent.parent / "bin" / "hansa"), "--desk",
                                str(self.desk.root), "coordinator"], capture_output=True, text=True,
                               env={**env, "HANSA_CLAUDE": str(claude), **extra})
            self.assertEqual(r.returncode, 0, r.stderr)
            return r.stdout.strip().splitlines()[0]

        for npm in (True, False):
            claude, held = install(self.tmp / f"own-{npm}", npm)
            self.assertEqual(start(claude), "updater=unset")  # the user's own install keeps updating
            claude, held = install(self.tmp / f"theirs-{npm}", npm)
            held.chmod(0o555)  # another account's: this one cannot write there
            self.addCleanup(held.chmod, 0o755)
            self.assertEqual(start(claude), "updater=1")
            self.assertEqual(start(claude, DISABLE_AUTOUPDATER="0"), "updater=0")  # a value of the user's stays
        (self.tmp / "own-True" / "bin").chmod(0o555)  # the command's own folder counts too
        self.addCleanup((self.tmp / "own-True" / "bin").chmod, 0o755)
        self.assertEqual(start(self.tmp / "own-True" / "bin" / "claude"), "updater=1")

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

    def page_save(self, change):
        """Load the settings as the page does, change them, save (what POST /api/settings does); what the page
        is told."""
        loaded = json.loads(json.dumps(public_settings(self.desk)))
        change(loaded["settings"])
        return save_from_page(self.desk, {"settings": loaded["settings"], "rev": loaded["rev"]})

    def echo_coordinator(self):
        echo = self.tmp / "echo-claude"
        echo.write_text("#!/bin/sh\necho \"$@\"\n")
        echo.chmod(0o755)
        return subprocess.run([str(Path(__file__).resolve().parent.parent / "bin" / "hansa"), "--desk", str(self.desk.root),
                               "coordinator"], capture_output=True, text=True, env={**os.environ, "HANSA_CLAUDE": str(echo)})

    def test_a_folder_the_principal_lets_the_coordinator_read_reaches_its_start_command(self):
        inc = (self.tmp / "opt" / "include").resolve()
        inc.mkdir(parents=True)
        self.assertEqual(self.desk.settings()["coordinator_read"], [])
        self.assertEqual(self.page_save(lambda s: s.update(coordinator_read=[str(inc)])), {"ok": True, "problems": []})
        self.assertEqual(public_settings(self.desk)["settings"]["coordinator_read"], [str(inc)])  # the page shows it
        ui = (Path(__file__).resolve().parent.parent / "hansa" / "ui.html").read_text()
        self.assertIn("S.coordinator_read", ui)
        self.assertIn("T.coordinatorRead", ui)
        r = self.echo_coordinator()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f"--add-dir {self.repo.resolve()} {inc} --disallowedTools", r.stdout)
        self.assertIn(f"Edit(//{str(inc).lstrip('/')}/**)", r.stdout.split("--disallowedTools")[1].split("--model")[0])
        self.assertNotIn("skipped", r.stderr)
        told = self.hansa("explain")
        self.assertIn(str(inc), told)
        self.assertIn("principal", told)
        self.assertIn("hansa ask", (Path(__file__).resolve().parent.parent / "hansa" / "card.md").read_text()
                      .split("read folders outside the desk without asking")[1][:120])
        # the coordinator's own changes leave it as the principal set it
        self.hansa("concurrency", "2")
        self.assertEqual(self.desk.settings()["coordinator_read"], [str(inc)])
        # a folder removed later: the settings still save, and the start skips it and says so
        shutil.rmtree(inc)
        self.hansa("concurrency", "1")
        r = self.echo_coordinator()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn(str(inc), r.stdout)
        self.assertIn(f"{inc}: there is no such folder; skipped", r.stderr)
        self.assertIn(f"not given: {inc}", self.hansa("explain"))

    def test_folders_the_coordinator_may_not_be_let_read_are_refused_on_saving_and_skipped_at_start(self):
        home = Path(os.path.expanduser("~"))
        cc = self.tmp / "logins" / "cc"
        (cc / "sessions").mkdir(parents=True)
        s = self.desk.settings()
        s["providers"]["claude"]["config_dir"] = str(cc)
        self.assertEqual(self.desk.save_settings(s), [])
        (self.tmp / "afile").write_text("x")
        link = self.tmp / "link-to-secrets"
        link.symlink_to(self.desk.secrets_dir)
        bad = {"relative/path": "full path", str(self.tmp / "nowhere"): "no such folder",
               str(self.tmp / "afile"): "not a folder", "/": "whole machine",
               str(home): "home folder", str(home.parent): "home folder",
               str(self.desk.root): "the desk", str(self.desk.secrets_dir): "the desk", str(self.tmp): "the desk",
               str(link): "the desk",  # where a link leads
               str(cc): "Claude Code folder", str(cc / "sessions"): "Claude Code folder",
               str(cc.parent): "Claude Code folder",
               str(home / ".ssh"): "login or key", str(home / ".ssh" / "keys"): "login or key",
               str(home / ".config"): "login or key"}
        good = self.tmp / "opt"
        good.mkdir()
        for path, why in bad.items():
            told = self.page_save(lambda s: s.update(coordinator_read=[str(good), path]))
            self.assertFalse(told["ok"], path)
            self.assertTrue(any(path in p and why in p for p in told["problems"]), (path, told))
            self.assertEqual(self.desk.settings()["coordinator_read"], [])  # nothing was saved
        self.assertFalse(self.page_save(lambda s: s.update(coordinator_read="/opt"))["ok"])
        self.assertFalse(self.page_save(lambda s: s.update(coordinator_read=[["/opt"]]))["ok"])
        # written into settings.json another way: the start skips each and says so, and gives the good one
        s = self.desk.settings()
        s["coordinator_read"] = [*bad, str(good)]
        write_json(self.desk.settings_path, s)
        r = self.echo_coordinator()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f"--add-dir {self.repo.resolve()} {good} --disallowedTools", r.stdout)
        for path, why in bad.items():
            self.assertTrue(any(f"read {path}: " in x and why in x and "skipped" in x for x in r.stderr.splitlines()),
                            (path, r.stderr))
        self.assertEqual(len(self.events("coordinator_read_skipped")), len(bad))

    def ignores_case(self) -> bool:
        return os.path.exists(str(self.tmp).swapcase()) and os.path.samefile(self.tmp, str(self.tmp).swapcase())

    def test_no_spelling_in_another_case_opens_a_fenced_folder_where_the_disk_ignores_case(self):
        if not self.ignores_case():
            self.skipTest("this disk tells names apart by case")
        home = self.tmp / "home"
        (home / ".ssh").mkdir(parents=True)
        (home / ".claude" / "projects").mkdir(parents=True)
        good = self.tmp / "opt"
        good.mkdir()
        up = lambda p: Path(p).parent / Path(p).name.upper()
        bad = {str(up(home)): "home folder", str(up(self.tmp)): "home folder",
               str(home / ".SSH"): "login or key", str(up(home) / ".ssh"): "login or key",
               str(home / ".AWS"): "login or key",  # not there: by its name, in any case
               str(home / ".Config"): "login or key", str(home / ".CLAUDE"): "Claude Code folder",
               str(home / ".Claude" / "PROJECTS"): "Claude Code folder",
               str(up(self.desk.root)): "the desk", str(self.desk.root / "SECRETS"): "the desk",
               str(up(self.desk.root) / "Notes"): "the desk"}
        with as_home(home):
            for path, why in bad.items():
                told = self.page_save(lambda s: s.update(coordinator_read=[str(good), path]))
                self.assertFalse(told["ok"], path)
                self.assertTrue(any(path in p and why in p for p in told["problems"]), (path, told))
            self.assertEqual(self.page_save(lambda s: s.update(coordinator_read=[str(up(good))])), {"ok": True, "problems": []})
            s = self.desk.settings()
            s["coordinator_read"] = [*bad, str(good), str(up(good))]
            write_json(self.desk.settings_path, s)
            r = self.echo_coordinator()
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn(f"--add-dir {self.repo.resolve()} {good} --disallowedTools", r.stdout)  # one folder once
            for path, why in bad.items():
                self.assertTrue(any(f"read {path}: " in x and why in x and "skipped" in x for x in r.stderr.splitlines()),
                                (path, r.stderr))
            # a lane's write list: the same spellings
            s = self.desk.settings()
            s["coordinator_read"] = []
            for path, why in {str(up(home)): "home folder", str(home / ".SSH"): "login or key",
                              str(home / ".AWS" / "sso"): "login or key", str(home / ".CLAUDE"): "Claude Code folder",
                              str(up(self.desk.root)): "the desk's", str(self.desk.root / "SECRETS"): "the desk's",
                              str(self.repo / ".GIT" / "hooks"): ".git folder",
                              str(up(self.repo)): "the lane's repository"}.items():
                s["lanes"]["a"]["write"] = [path]
                told = validate(s, self.desk.root)
                self.assertTrue(len(told) == 1 and path in told[0] and why in told[0], (path, told))
            s["lanes"]["a"]["write"] = [str(home / "Hansa-Cache" / "A" / "Pip"), str(up(good))]
            self.assertEqual(validate(s, self.desk.root), [])
            s["lanes"]["a"]["write"] = [str(home / ".M2" / "REPOSITORY"), str(home / ".CACHE" / "Pip")]  # a user's caches
            self.assertEqual(len(validate(s, self.desk.root)), 2)
            s["lanes"]["a"]["write"] = [str(home / "LIBRARY" / "x")]  # the rule for the home folder, in any case
            self.assertEqual(len(validate(s, self.desk.root)), 1)

    def test_the_machine_owners_claude_code_folder_is_never_let_read_nor_a_name_a_rule_cannot_hold(self):
        home = self.tmp / "home"
        (home / ".claude" / "projects").mkdir(parents=True)
        s = self.desk.settings()
        s["providers"]["claude"]["config_dir"] = str(self.tmp / "logins" / "cc")  # not ~/.claude
        self.assertEqual(self.desk.save_settings(s), [])
        odd = [self.tmp / n for n in ("a)b (x", "star*", "what?", "[br]", "{a,b}")]
        for d in odd:
            d.mkdir()
        (self.tmp / "plain").symlink_to(odd[0])
        good = self.tmp / "opt"
        good.mkdir()
        bad = {str(home / ".claude"): "machine owner's Claude Code folder",
               str(home / ".claude" / "projects"): "machine owner's Claude Code folder",
               **{str(d): "permission rule" for d in odd}, str(self.tmp / "plain"): "permission rule"}
        with as_home(home):
            for path, why in bad.items():
                told = self.page_save(lambda s: s.update(coordinator_read=[str(good), path]))
                self.assertFalse(told["ok"], path)
                self.assertTrue(any(path in p and why in p for p in told["problems"]), (path, told))
            s = self.desk.settings()
            s["coordinator_read"] = [*bad, str(good)]
            write_json(self.desk.settings_path, s)
            r = self.echo_coordinator()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f"--add-dir {self.repo.resolve()} {good} --disallowedTools", r.stdout)
        for path, why in bad.items():
            self.assertTrue(any(f"read {path}: " in x and why in x and "skipped" in x for x in r.stderr.splitlines()),
                            (path, r.stderr))

    def test_a_saved_folder_that_went_bad_is_said_on_the_page_and_keeps_nothing_else_from_saving(self):
        good, other = self.tmp / "opt", self.tmp / "other"
        good.mkdir()
        other.mkdir()
        link = self.tmp / "lk"
        link.symlink_to(good)
        self.assertTrue(self.page_save(lambda s: s.update(coordinator_read=[str(link), str(good)]))["ok"])
        self.assertEqual(public_settings(self.desk)["read_skipped"], [])
        link.unlink()
        link.symlink_to(self.desk.secrets_dir)  # pointed elsewhere after it was saved
        shown = public_settings(self.desk)
        self.assertEqual(shown["problems"], [])
        self.assertEqual(len(shown["read_skipped"]), 1)
        self.assertIn(f"{link}: it would open {self.desk.root}, the desk", shown["read_skipped"][0])
        self.assertIn("READ_SKIPPED = r.read_skipped", (Path(__file__).resolve().parent.parent / "hansa" / "ui.html").read_text())
        self.assertEqual(self.page_save(lambda s: s.update(cool_minutes=45)), {"ok": True, "problems": []})  # another setting
        self.hansa("concurrency", "3")  # the coordinator's changes
        self.assertEqual(self.desk.settings()["concurrency"], 3)
        self.assertEqual(self.desk.settings()["cool_minutes"], 45)
        told = self.page_save(lambda s: s.update(coordinator_read=[str(link), str(good), str(other)]))  # the list itself
        self.assertFalse(told["ok"])
        self.assertIn(str(link), told["problems"][0])
        self.assertTrue(self.page_save(lambda s: s.update(coordinator_read=[str(good), str(other)]))["ok"])
        self.assertEqual(public_settings(self.desk)["read_skipped"], [])

    def test_hansa_explain_checks_the_folders_the_coordinator_reads_as_its_start_does(self):
        own = self.tmp / "own-claude"
        (own / "projects").mkdir(parents=True)
        good = self.tmp / "opt"
        good.mkdir()
        s = self.desk.settings()
        s["coordinator_read"] = [str(own / "projects"), str(good)]
        write_json(self.desk.settings_path, s)
        with unittest.mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(own)}):
            r = self.echo_coordinator()
            told = self.hansa("explain")
        self.assertIn(f"--add-dir {self.repo.resolve()} {good} --disallowedTools", r.stdout)
        self.assertIn(f"{own / 'projects'}: it would open {own}, the Claude Code folder the coordinator runs from; skipped",
                      r.stderr)
        self.assertIn(f"without asking, besides the lanes' repositories: {good} (", told)
        self.assertIn(f"not given: {own / 'projects'}: it would open {own}, the Claude Code folder", told)

    def test_macos_data_volume_opens_nothing_in_either_list(self):
        data = "/System/Volumes/Data"
        if sys.platform != "darwin" or not os.path.isdir(data):
            self.skipTest("only macOS has /System/Volumes/Data")
        home = (self.tmp / "home").resolve()
        (home / ".ssh").mkdir(parents=True)
        good = (self.tmp / "opt").resolve()
        good.mkdir()
        if not os.path.isdir(data + str(home)):
            self.skipTest("the tests' folder is not on the data volume")
        bad = {data: data, data + "/": data, "/System/Volumes": data, "/System": data, "/SYSTEM/Volumes/../Volumes": data,
               data + str(home): "home folder", data + str(home.parent): "home folder",
               data + str(home / ".ssh"): "login or key", data + str(home / ".aws" / "sso"): "login or key",
               data + str(home / ".claude"): "Claude Code folder", data + str(self.desk.root / "secrets"): "the desk"}
        with as_home(home):
            for path, why in bad.items():
                told = self.page_save(lambda s: s.update(coordinator_read=[str(good), path]))
                self.assertFalse(told["ok"], path)
                self.assertTrue(any(path in p and why in p for p in told["problems"]), (path, told))
            s = self.desk.settings()
            bad[data + str(good)] = "the system's own"  # a good folder is listed from /, not through the volume
            s["coordinator_read"] = [*bad, str(good)]
            write_json(self.desk.settings_path, s)
            r = self.echo_coordinator()
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn(f"--add-dir {self.repo.resolve()} {good} --disallowedTools", r.stdout)
            for path, why in bad.items():
                self.assertTrue(any(f"read {path}: " in x and why in x and "skipped" in x for x in r.stderr.splitlines()),
                                (path, r.stderr))
            told = self.hansa("explain")
            for path in bad:
                self.assertIn(f"not given: {path}: ", told)
            s["coordinator_read"] = []
            for path, why in bad.items():
                s["lanes"]["a"]["write"] = [path]
                told = validate(s, self.desk.root)
                self.assertTrue(len(told) == 1 and path in told[0] and why.replace("the desk", "the desk's") in told[0],
                                (path, told))
            s["lanes"]["a"]["write"] = [data + str(home / "work" / "cache"), str(home / "work" / "cache"), str(good)]
            told = validate(s, self.desk.root)
            self.assertTrue(len(told) == 1 and data in told[0] and "the system's own" in told[0], told)

    def test_a_path_with_slashes_doubled_at_its_start_is_answered_like_the_path_it_is(self):
        good = (self.tmp / "opt").resolve()
        good.mkdir()
        two, three = "/" + str(good), "//" + str(good)
        code = ("import sys, json; from hansa.desk import read_split, write_problems; "
                "print(json.dumps([[read_split([x]), write_problems([x])] for x in sys.argv[1:]]))")
        r = subprocess.run([sys.executable, "-c", code, "//", "//opt", "///opt", two, three],
                           cwd=Path(__file__).resolve().parent.parent,
                           capture_output=True, text=True, timeout=60)  # each ends: none walks upward for ever
        self.assertEqual(r.returncode, 0, r.stderr)
        top, *_, (read2, write2), (read3, write3) = json.loads(r.stdout)
        self.assertIn("it is the whole machine", top[0][1][0])
        self.assertIn("the home folder", top[1][0])
        self.assertEqual((read2, write2, read3, write3), ([[str(good)], []], [], [[str(good)], []], []))
        told = self.page_save(lambda s: s.update(coordinator_read=["//", two]))
        self.assertTrue(not told["ok"] and any("read //: it is the whole machine" in x for x in told["problems"]), told)
        s = self.desk.settings()
        s["coordinator_read"] = ["//", "//nowhere", two, three]
        write_json(self.desk.settings_path, s)
        r = self.echo_coordinator()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f"--add-dir {self.repo.resolve()} {good} --disallowedTools", r.stdout)  # once, from /
        self.assertTrue(any("read //: it is the whole machine" in x and "skipped" in x for x in r.stderr.splitlines()),
                        r.stderr)
        self.assertIn("not given: //nowhere: there is no such folder", self.hansa("explain"))
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as e:
            main(["--desk", str(self.desk.root), "lane", "set", "a", "write", "//", "//x"])
        self.assertIn("write cannot include //: it would open", str(e.exception.code))
        self.hansa("lane", "set", "a", "write", two)

    def test_macos_volumes_and_library_open_nothing_in_either_list(self):
        if sys.platform != "darwin" or not os.path.isdir("/Volumes") or not os.path.isdir("/.vol"):
            self.skipTest("only macOS has /Volumes, /.vol and ~/Library")
        home = (self.tmp / "home").resolve()
        lib = home / "Library"
        for d in ("Keychains", "Application Support/Claude", "LaunchAgents", "Caches/pip"):
            (lib / d).mkdir(parents=True)
        good = (self.tmp / "opt").resolve()
        good.mkdir()
        for name, to in (("lv", "/Volumes"), ("lvol", "/.vol"), ("lk", lib / "Keychains"), ("lc", lib / "Caches")):
            (self.tmp / name).symlink_to(to)
        (lib / "Caches" / "out").symlink_to(lib / "LaunchAgents")
        st = good.stat()
        bad = {"/Volumes": "/Volumes", "/Volumes/": "/Volumes", "/VOLUMES": "/Volumes", str(self.tmp / "lv"): "/Volumes",
               "/.vol": "/.vol", "/.vol/1/2": "/.vol", f"/.vol/{st.st_dev}/{st.st_ino}": "/.vol",
               str(self.tmp / "lvol"): "/.vol",
               str(lib): str(lib), str(home / "LIBRARY"): str(lib), str(lib / "Keychains"): str(lib),
               str(lib / "Application Support" / "Claude"): str(lib), str(lib / "LaunchAgents"): str(lib),
               str(lib / "Caches" / ".." / "Keychains"): str(lib), str(self.tmp / "lk"): str(lib),
               str(lib / "Caches" / "out"): str(lib)}  # a link inside Caches that leads out of it
        if os.path.isdir("/System/Volumes/Data/Volumes"):
            bad["/System/Volumes/Data/Volumes"] = "/Volumes"
            bad["/System/Volumes/Data" + str(lib / "Keychains")] = str(lib)
        root = next((str(v) for v in Path("/Volumes").iterdir() if v.is_symlink() and os.path.samefile(v, "/")), "")
        if root and not set(root) & set("()[]{}*?"):
            bad[root + "/Volumes"] = "/Volumes"
        fine = [good, lib / "Caches", lib / "Caches" / "pip", self.tmp / "lc"]
        with as_home(home):
            for path, why in bad.items():
                told = self.page_save(lambda s: s.update(coordinator_read=[str(good), path]))
                self.assertFalse(told["ok"], path)
                self.assertTrue(any(f"read {path}: it would open {why}" in p for p in told["problems"]), (path, told))
            self.assertTrue(self.page_save(lambda s: s.update(coordinator_read=[str(x) for x in fine]))["ok"])
            s = self.desk.settings()
            for some in (list(bad)[:10], list(bad)[10:]):  # a list holds 20 at most
                s["coordinator_read"] = [*some, *(str(x) for x in fine)]
                write_json(self.desk.settings_path, s)
                r = self.echo_coordinator()
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertIn(f"--add-dir {self.repo.resolve()} {good} {lib / 'Caches'} {lib / 'Caches' / 'pip'} "
                              "--disallowedTools", r.stdout)  # the link to Caches is that folder: given once
                told = self.hansa("explain")
                for path in some:
                    self.assertTrue(any(f"read {path}: " in x and "skipped" in x for x in r.stderr.splitlines()),
                                    (path, r.stderr))
                    self.assertIn(f"not given: {path}: ", told)
            s["coordinator_read"] = []
            for path, why in bad.items():
                s["lanes"]["a"]["write"] = [path]
                told = validate(s, self.desk.root)
                self.assertTrue(len(told) == 1 and f"write cannot include {path}: it would open {why}" in told[0],
                                (path, told))
            s["lanes"]["a"]["write"] = [str(good), "/Volumes/Build/cache"]
            self.assertEqual(validate(s, self.desk.root), [])  # another volume
            # the write list takes nothing in ~/Library: its Caches are shared with what runs outside the sandbox
            for x in (lib / "Caches", self.tmp / "lc", lib / "Caches" / "pip", lib / "Caches" / "new" / "x",
                      lib / "Caches" / "Homebrew", lib / "Caches" / "com.anthropic.claudefordesktop.ShipIt"):
                s["lanes"]["a"]["write"] = [str(x)]
                told = validate(s, self.desk.root)
                self.assertTrue(len(told) == 1 and f"it would open {lib}, which holds" in told[0], told)
                self.assertIn("nothing in it can be listed", told[0])
                self.assertIn("~/hansa-cache/<lane>/<tool>", told[0])
                self.assertNotIn("Caches and folders inside it can be listed", told[0])

    def test_other_names_for_the_whole_disk_open_nothing_in_either_list(self):
        good = (self.tmp / "opt").resolve()
        good.mkdir()
        hidden, own = "its first folder below / is hidden", "the system's own"
        bad = {"/.cache-of-mine": hidden, "/.cache-of-mine/x/y": hidden, "//.hidden/": hidden, "/opt/../.x": hidden,
               "/dev": own, "/dev/fd": own, "/proc": own, "/proc/1/root": own, "/proc/self/root/opt": own,
               "/sys": own, "/sys/kernel": own}
        if sys.platform != "darwin":  # there the volume everything is on is named first
            bad.update({"/System": own, "/System/Library": own})
        else:
            bad.update({"/System/Library": own, "/System/Volumes/Data" + str(good): own, "/SYSTEM/library/": own,
                        "/Library/Keychains": "/Library/Keychains", "/Library": "/Library/Keychains",
                        "/LIBRARY/keychains/x": "/Library/Keychains"})
            for top in ("/.nofollow", "/.resolve"):
                if os.path.isdir(top):
                    (self.tmp / f"l{top[2:]}").symlink_to(top)
                    bad.update({top: hidden, top.upper() + "/": hidden, top + str(good): hidden,
                                str(self.tmp / f"l{top[2:]}"): hidden})
            if os.path.isdir("/.resolve/1"):
                bad["/.resolve/1" + str(good)] = hidden
            root = next((str(v) for v in Path("/Volumes").iterdir() if v.is_symlink() and os.path.samefile(v, "/")), "") \
                if os.path.isdir("/Volumes") else ""
            if root and not set(root) & set("()[]{}*?") and os.path.isdir("/.nofollow"):
                bad[root + "/.nofollow"] = hidden
        (self.tmp / "ldev").symlink_to("/dev")
        bad[str(self.tmp / "ldev")] = own
        for path, why in bad.items():
            told = self.page_save(lambda s: s.update(coordinator_read=[str(good), path]))
            self.assertFalse(told["ok"], path)
            self.assertTrue(any(f"read {path}: " in p and why in p for p in told["problems"]), (path, told))
        s = self.desk.settings()
        names = list(bad)
        for some in (names[i:i + 19] for i in range(0, len(names), 19)):  # a list holds 20 at most
            s["coordinator_read"] = [*some, str(good)]
            write_json(self.desk.settings_path, s)
            r = self.echo_coordinator()
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn(f"--add-dir {self.repo.resolve()} {good} --disallowedTools", r.stdout)
            told = self.hansa("explain")
            for path in some:
                self.assertTrue(any(f"read {path}: " in x and "skipped" in x for x in r.stderr.splitlines()),
                                (path, r.stderr))
                self.assertIn(f"not given: {path}: ", told)
        s["coordinator_read"] = []
        for path, why in bad.items():
            s["lanes"]["a"]["write"] = [path]
            told = validate(s, self.desk.root)
            self.assertTrue(len(told) == 1 and f"write cannot include {path}: " in told[0] and why in told[0], (path, told))
        self.assertEqual(write_problems([str(good), "/opt/include", "/var/cache/x"]), [])
        self.assertIn("it would open /usr, where programs are installed", write_problems(["/usr/local/include/x"])[0])

    def test_a_name_with_an_invisible_or_direction_changing_character_is_in_neither_list(self):
        good = (self.tmp / "opt").resolve()
        good.mkdir()
        odd = self.tmp.resolve() / "inc\u200blude"
        odd.mkdir()
        (self.tmp / "lodd").symlink_to(odd)
        bad = [str(odd), str(good) + "\u202e", str(good / "\ufeffx" / "y"), str(self.tmp.resolve() / "a\u2066b\u2069"),
               str(good) + "/\u00adx", str(self.tmp.resolve() / "lodd")]  # and a link that leads to such a name
        why = "invisible or direction-changing character"
        for path in bad:
            told = self.page_save(lambda s: s.update(coordinator_read=[str(good), path]))
            self.assertFalse(told["ok"], ascii(path))
            self.assertTrue(any(f"read {json.dumps(path)}: " in p and why in p for p in told["problems"]), told)
        s = self.desk.settings()
        s["coordinator_read"] = [*bad, str(good)]
        write_json(self.desk.settings_path, s)
        r = self.echo_coordinator()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f"--add-dir {self.repo.resolve()} {good} --disallowedTools", r.stdout)
        told = self.hansa("explain")
        for path in bad:
            self.assertTrue(any(f"read {json.dumps(path)}: " in x and "skipped" in x for x in r.stderr.splitlines()),
                            (ascii(path), r.stderr))
            self.assertIn(f"not given: {json.dumps(path)}: ", told)
        s["coordinator_read"] = []
        for path in bad:
            s["lanes"]["a"]["write"] = [str(good), path]
            told = validate(s, self.desk.root)
            self.assertTrue(len(told) == 1 and f"write cannot include {json.dumps(path)}: " in told[0] and why in told[0],
                            (ascii(path), told))

    def test_a_read_folder_is_given_to_claude_code_as_the_disk_spells_it(self):
        if not self.ignores_case():
            self.skipTest("this disk tells names apart by case")
        good = (self.tmp / "Opt" / "include").resolve()
        good.mkdir(parents=True)
        (self.tmp / "LK").symlink_to(str(good).upper())  # a link, and where it leads, in other spellings
        typed = [str(good.parent).upper() + "/INCLUDE/", str(self.tmp / "lk")]
        self.assertEqual(self.page_save(lambda s: s.update(coordinator_read=typed)), {"ok": True, "problems": []})
        r = self.echo_coordinator()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f"--add-dir {self.repo.resolve()} {good} --disallowedTools", r.stdout)  # the link is the same folder
        rules = r.stdout.split("--disallowedTools")[1].split()
        self.assertIn(f"Edit(//{str(good).lstrip('/')}/**)", rules)
        self.assertFalse([x for x in rules if "INCLUDE" in x or "OPT" in x], rules)
        self.assertIn(f"besides the lanes' repositories: {good} (", self.hansa("explain"))
        s = self.desk.settings()
        s["coordinator_read"] = [str(self.tmp / "lk")]  # the link alone: by its stored name, and where it leads
        write_json(self.desk.settings_path, s)
        rules = self.echo_coordinator().stdout.split("--disallowedTools")[1].split()
        self.assertIn(f"Edit(//{str(self.tmp / 'LK').lstrip('/')}/**)", rules)
        self.assertIn(f"Edit(//{str(good).lstrip('/')}/**)", rules)

    def test_hansa_explain_says_a_list_of_read_folders_that_is_not_a_list_in_whole_words(self):
        s = self.desk.settings()
        s["coordinator_read"] = "/opt/include"
        write_json(self.desk.settings_path, s)
        told = self.hansa("explain")
        self.assertIn("  not given: the folders the coordinator may read must be a list of up to 20 full paths", told)
        self.assertNotIn("not given: d must", told)

    def test_root_gives_the_coordinator_the_folders_of_the_accounts_plan_and_checks_them_again(self):
        self.as_root()
        self.stand_in_runuser()
        inc = self.tmp / "opt"
        inc.mkdir()
        s = self.desk.settings()
        s["coordinator_read"] = [str(inc)]
        self.assertEqual(self.desk.save_settings(s), [])
        started = self.coordinator_as_root({})
        argv = started["argv"]
        self.assertEqual(argv[argv.index("--add-dir") + 1:argv.index("--disallowedTools")], [str(self.repo.resolve()), str(inc)])
        self.assertIn(f"Edit(//{str(inc.resolve()).lstrip('/')}/**)", argv)
        # a plan the account wrote itself: root looks again, with its own home and Claude Code folder
        own = self.tmp / "root-claude"
        own.mkdir()
        plan = {"repos": [], "model": "opus", "effort": "", "env": {}, "read_refused": ["said by the account"],
                "read": [str(inc), "-p", "rel", str(self.desk.root), "/", os.path.expanduser("~"), str(own), 7]}
        done = unittest.mock.Mock(returncode=0, stdout=json.dumps(plan), stderr="")
        err = io.StringIO()
        with unittest.mock.patch.object(account, "run", lambda *a, **k: done), contextlib.redirect_stderr(err):
            started = self.coordinator_as_root({"CLAUDE_CONFIG_DIR": str(own)})
        argv = started["argv"]
        self.assertEqual(argv[argv.index("--add-dir") + 1:argv.index("--disallowedTools")], [str(inc)])
        self.assertEqual(len([x for x in err.getvalue().splitlines() if "skipped" in x]), 8)
        self.assertIn("said by the account; skipped", err.getvalue())

    @staticmethod
    def covered(rules, tool, path) -> bool:
        """Whether a rule of hansa's covers the path, in the two forms hansa's rules use: an exact path, and
        DIR/** for all under DIR at any depth (names starting with a dot too)."""
        for r in rules:
            if not r.startswith(f"{tool}(//"):
                continue
            p = "/" + r[len(tool) + 3:-1]
            if p == path or (p.endswith("/**") and path.startswith(p[:-2])):
                return True
        return False

    def own_folder(self, cc=None):
        cc = cc or self.tmp / "own-claude"
        for f in ("settings.json", ".credentials.json", "history.jsonl", "sessions/4242.json",
                  "projects/-desk/memory/MEMORY.md", "projects/-desk/s.jsonl"):
            (cc / f).parent.mkdir(parents=True, exist_ok=True)
            (cc / f).write_text("{}")
        return cc

    def assert_own_folder_closed(self, rules, cc, dot=None):
        for tool in ("Read", "Edit"):
            for f in ("settings.json", ".credentials.json", "history.jsonl", "settings.local.json",
                      "sessions/4242.json", "sessions/a/b.json", *([] if dot else [".claude.json"])):
                self.assertTrue(self.covered(rules, tool, f"{cc}/{f}"), (tool, f))
            if dot:
                self.assertTrue(self.covered(rules, tool, str(dot)), (tool, dot))
            for f in ("projects/-desk/memory/MEMORY.md", "projects/-desk/s.jsonl", "projects"):
                self.assertFalse(self.covered(rules, tool, f"{cc}/{f}"), (tool, f))  # the session's memory: its own
        # hansa relies on no other form: a lone * is not in them
        self.assertEqual([r for r in rules if "*" in r.replace("/**)", ")")], [])

    def test_the_coordinator_neither_reads_nor_edits_its_own_claude_code_folder(self):
        echo = self.tmp / "echo-claude"
        echo.write_text("#!/bin/sh\nfor x in \"$@\"; do echo \"$x\"; done\n")
        echo.chmod(0o755)
        cc = self.own_folder()
        s = self.desk.settings()
        s["providers"]["claude"]["config_dir"] = str(cc)
        self.assertEqual(self.desk.save_settings(s), [])
        r = subprocess.run([str(Path(__file__).resolve().parent.parent / "bin" / "hansa"), "--desk", str(self.desk.root),
                            "coordinator"], capture_output=True, text=True, env={**os.environ, "HANSA_CLAUDE": str(echo)})
        self.assertEqual(r.returncode, 0, r.stderr)
        argv = r.stdout.splitlines()
        deny = json.loads(argv[argv.index("--settings") + 1])["permissions"]["deny"]
        self.assert_own_folder_closed(deny, cc.resolve())
        self.assertFalse(self.covered(deny, "Read", str(Path.home() / ".claude.json")))  # not the folder it runs from
        home = self.tmp / "own-home"  # no CLAUDE_CONFIG_DIR: ~/.claude, and ~/.claude.json beside it
        self.own_folder(home / ".claude")
        (home / ".claude" / "stats.json").write_text("{}")
        rules = cli.own_folder_rules({"HOME": str(home)})
        self.assert_own_folder_closed(rules, home.resolve() / ".claude", dot=home.resolve() / ".claude.json")
        self.assertTrue(self.covered(rules, "Read", f"{home.resolve()}/.claude/stats.json"))

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

    def test_the_coordinator_writes_its_notes_without_asking_and_loads_no_connectors_or_guesses(self):
        show = self.tmp / "argv-claude"  # the session: the one variable, then its options one per line
        show.write_text("#!/bin/sh\necho \"$CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION\"\n"
                        "for x in \"$@\"; do echo \"$x\"; done\n")
        show.chmod(0o755)
        notes = self.desk.root / "notes"
        self.assertTrue(notes.is_dir())  # a new desk has it
        notes.rmdir()  # a desk made before notes/
        mine = self.desk.root / ".claude" / "settings.json"
        mine.write_text(json.dumps({"permissions": {"allow": ["Bash(hansa:*)"], "deny": []}}))
        r = subprocess.run([str(Path(__file__).resolve().parent.parent / "bin" / "hansa"), "--desk", str(self.desk.root),
                            "coordinator"], capture_output=True, text=True,
                           env={**os.environ, "HANSA_CLAUDE": str(show), "CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(notes.is_dir())  # made when it starts
        self.assertEqual(notes.stat().st_uid, self.desk.root.stat().st_uid)
        root = str(self.desk.root).lstrip("/")
        rules = json.loads(mine.read_text())["permissions"]
        self.assertIn(f"Edit(//{root}/notes/**)", rules["allow"])  # one Edit rule: Write and Edit alike
        self.assertTrue(self.covered(rules["allow"], "Edit", f"/{root}/notes/plan.md"))
        for closed in ("settings.json", "tasks/t0001/task.md", "lanes/a/brief.md", "events.jsonl", "secrets/x.key",
                       ".hansa/web-token"):  # only notes/ is allowed; these stay denied
            self.assertFalse(self.covered(rules["allow"], "Edit", f"/{root}/{closed}"), closed)
            self.assertTrue(self.covered(rules["deny"], "Edit", f"/{root}/{closed}"), closed)
        out = r.stdout.splitlines()
        self.assertEqual(out[0], "false")  # over the principal's own setting
        argv = out[1:]
        given = json.loads(argv[argv.index("--settings") + 1])
        self.assertIs(given["disableClaudeAiConnectors"], True)
        self.assertIs(given["promptSuggestionEnabled"], False)
        self.assertNotIn("--strict-mcp-config", argv)  # the machine's MCP servers stay

    def test_the_coordinator_session_gets_a_follower_at_an_inbox_path_hansa_chose(self):
        echo = self.tmp / "echo-claude"  # the session: its pid (hansa's own, which it became) and its options
        echo.write_text("#!/bin/sh\necho \"$$ $@\"\nsleep 2\n")
        echo.chmod(0o755)
        start = [str(Path(__file__).resolve().parent.parent / "bin" / "hansa"), "--desk", str(self.desk.root), "coordinator"]
        r = subprocess.run(start, capture_output=True, text=True, env={**os.environ, "HANSA_CLAUDE": str(echo)},
                           timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        pid, *argv = r.stdout.split()
        sock = argv[argv.index("--messaging-socket-path") + 1]
        self.assertLess(len(sock.encode()), 104)
        self.assertEqual(Path(sock).name, f"c{pid}.sock")
        self.assertEqual(stat.S_IMODE(os.stat(Path(sock).parent).st_mode), 0o700)  # only this account opens it
        rec = self.soon(lambda: [x for x in notify.records(self.desk) if x["session"]["pid"] == int(pid)])
        self.assertEqual((rec[0]["socket"], rec[0]["name"]), (sock, ""))  # named once Claude Code names it
        self.soon(lambda: all(x.get("ended") for x in notify.records(self.desk)))  # it ended with the session
        r = subprocess.run(start + ["--messaging-socket-path", str(self.tmp / "mine.sock")], capture_output=True,
                           text=True, env={**os.environ, "HANSA_CLAUDE": str(echo)}, timeout=30)
        self.assertEqual(r.stdout.split().count("--messaging-socket-path"), 1)  # the path given is the one followed
        self.soon(lambda: [x for x in notify.records(self.desk) if x["socket"] == str(self.tmp / "mine.sock")])

    def test_coordinator_takes_clear_daily_for_itself_and_gives_the_session_a_hook_after_a_clear(self):
        show = self.tmp / "argv-claude"  # the session: its options, one per line
        show.write_text("#!/bin/sh\nfor x in \"$@\"; do echo \"$x\"; done\nsleep 1\n")
        show.chmod(0o755)
        env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
        r = subprocess.run([str(Path(__file__).resolve().parent.parent / "bin" / "hansa"), "--desk", str(self.desk.root),
                            "coordinator", "--clear-daily", "04-09", "--permission-mode", "dontAsk"], capture_output=True,
                           text=True, env={**env, "HANSA_CLAUDE": str(show)}, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        argv = r.stdout.splitlines()
        self.assertNotIn("--clear-daily", argv)
        self.assertNotIn("04-09", argv)
        self.assertIn("dontAsk", argv)
        self.assertIn("The daily clear is off: the session does not run in tmux", r.stderr)  # notices still go out
        hook = json.loads(argv[argv.index("--settings") + 1])["hooks"]["SessionStart"][0]
        self.assertEqual(hook["matcher"], "clear")
        code = Path(__file__).resolve().parent.parent
        self.assertEqual(shlex.split(hook["hooks"][0]["command"]),
                         [str(code / "bin" / "hansa"), "--desk", str(self.desk.root), "status"])  # hansa's own code
        rec = self.soon(lambda: [x for x in notify.records(self.desk) if (x.get("clear") or {}).get("window") == "04-09"])
        self.assertIn("does not run in tmux", rec[0]["clear"]["off"])
        with self.assertRaises(SystemExit) as e:
            main(["--desk", str(self.desk.root), "coordinator", "--clear-daily", "9am"])
        self.assertIn("such as 04-09", str(e.exception.code))

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
                                      f"open({str(log)!r} + '.cwd', 'a').write(os.getcwd() + '\\n')\n"
                                      f"open({str(log)!r} + '.build', 'a').write(os.environ.get('HANSA_BUILD', '') + '\\n')\n"
                                      "os.execvp(sys.argv[4], sys.argv[4:])\n")
        (bin_ / "runuser").chmod(0o755)
        path = unittest.mock.patch.dict(os.environ, {"PATH": f"{bin_}{os.pathsep}{os.environ['PATH']}"})
        path.start()
        self.addCleanup(path.stop)
        self.addCleanup(os.chdir, os.getcwd())  # hansa goes to / before it becomes runuser
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

    def test_the_accounts_hansa_starts_from_the_root_folder_with_the_callers_relative_paths_made_full(self):
        log, ran = self.stand_in_runuser()
        here = os.getcwd()
        caller = self.tmp / "caller"
        (caller / "hansa").mkdir(parents=True)  # another checkout of hansa where root runs the command
        (caller / "hansa" / "__init__.py").write_text("")
        (caller / "hansa" / "__main__.py").write_text("raise SystemExit('the other checkout ran')\n")
        (caller / "task.md").write_text("Make the badge bigger\n")
        os.symlink(self.desk.root, caller / "desk")
        os.chdir(caller)
        self.addCleanup(os.chdir, here)
        self.as_root()
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as done:
            main(["--desk", "desk", "add", "--file", "task.md", "--lane", "a"])
        self.assertEqual(done.exception.code, 0)  # this checkout's hansa, not the one in the caller's folder
        args = json.loads(log.read_text().splitlines()[-1])
        self.assertIn(f"--desk={Path.cwd() / 'desk'}", args)  # the caller's relative paths, made full
        self.assertIn("--file=-", args)  # root read the relative file from the caller's folder
        self.assertEqual((self.desk.tasks_dir / "t0001" / "task.md").read_text(), "Make the badge bigger\n")
        os.chdir(self.desk.root)
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as done:
            main(["--desk", ".", "status"])  # hansa replaces itself with runuser here
        self.assertEqual(done.exception.code, 0, ran[-1].stderr)
        from hansa import build
        self.assertIn(f" · hansa {build()} · ", ran[-1].stdout.splitlines()[0])  # the account's, as root's --version
        self.assertEqual(Path(str(log) + ".build").read_text().splitlines()[-1], build())  # root handed its answer over
        self.assertIn(f"--desk={self.desk.root}", json.loads(log.read_text().splitlines()[-1]))
        self.assertEqual(Path(str(log) + ".cwd").read_text().splitlines(), ["/", "/"])

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
                unittest.mock.patch.dict(os.environ, env), unittest.mock.patch("os.chdir"), \
                unittest.mock.patch("hansa.notify.start", lambda *x, **k: started.update(follower=x)):
            main(["--desk", str(self.desk.root), "coordinator"])
        return started

    def test_roots_follower_reads_the_desk_through_the_account_and_keeps_its_own_files_at_home(self):
        self.as_root()
        log, _ = self.stand_in_runuser()
        started = self.coordinator_as_root({})
        _, sess, sock = started["follower"]
        self.assertEqual(sess["pid"], os.getpid())  # root's session, which this process becomes
        self.assertEqual(started["argv"][started["argv"].index("--messaging-socket-path") + 1], sock)
        with unittest.mock.patch.object(notify.Path, "home", lambda: self.tmp / "root-home"):
            self.assertTrue(str(notify.private_dir(self.desk)).startswith(str(self.tmp / "root-home")))
        ask = notify.source(self.desk)
        me = {"key": notify.key_of(sess), "session": sess, "socket": sock, "uid": 0}
        self.assertEqual(ask({"op": "start", "me": me})["summary"], [])
        self.assertIn("notify-feed", json.loads(log.read_text().splitlines()[-1]))  # the account's hansa did it
        self.assertEqual([x["key"] for x in notify.live_records(self.desk)], [me["key"]])
        self.assertEqual(ask({"op": "feed", "me": me, "pos": 0, "done": 0})["items"], [])

    def test_the_coordinator_stays_root_leaves_the_desk_to_the_account_and_uses_roots_claude_folder(self):
        self.as_root()
        log, _ = self.stand_in_runuser()
        self.own_folder(self.tmp / "root-claude")
        self.own_folder(self.tmp / "root-home" / ".claude")
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
        given = json.loads(started["argv"][started["argv"].index("--settings") + 1])
        rules = given["permissions"]
        self.assertIn(f"Read(//{str(self.desk.root).lstrip('/')}/secrets/**)", rules["deny"])
        # its notes without asking, no claude.ai connectors and no guessed next message: from what hansa passes
        self.assertIn(f"Edit(//{str(self.desk.root).lstrip('/')}/notes/**)", rules["allow"])
        self.assertTrue((self.desk.root / "notes").is_dir())
        self.assertEqual((given["disableClaudeAiConnectors"], given["promptSuggestionEnabled"]), (True, False))
        self.assertEqual(started["env"]["CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION"], "false")
        self.assertNotIn("--strict-mcp-config", started["argv"])
        # root's own Claude Code folder, the one this session runs from, is closed to it; the account's is not
        self.assert_own_folder_closed(rules["deny"], self.tmp / "root-claude")
        self.assertFalse(self.covered(rules["deny"], "Read", str(self.tmp / "account-claude" / "settings.json")))
        without = {k: v for k, v in os.environ.items() if k != "CLAUDE_CONFIG_DIR"}
        with unittest.mock.patch.dict(os.environ, {**without, "HOME": str(self.tmp / "root-home")}, clear=True):
            started = self.coordinator_as_root({})
        self.assertNotIn("CLAUDE_CONFIG_DIR", started["env"])  # root's ~/.claude, as root starts Claude Code
        rules = json.loads(started["argv"][started["argv"].index("--settings") + 1])["permissions"]
        root_home = (self.tmp / "root-home").resolve()
        self.assert_own_folder_closed(rules["deny"], root_home / ".claude", dot=root_home / ".claude.json")
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
                    "env": {"ANTHROPIC_BASE_URL": "https://x.invalid", "CLAUDE_CODE_MAX_RETRIES": "8",
                            "CLAUDE_CONFIG_DIR": "/account/claude", "CLAUDE_CODE_SHELL_PREFIX": "/tmp/evil",
                            "CLAUDE_CODE_SHELL": "/tmp/evil", "HOME": "/account"}}
            done = subprocess.CompletedProcess([], 0, json.dumps({**plan, **over}), "")
            return unittest.mock.patch.object(account, "run", lambda *x, **k: done)
        without = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE_", "ANTHROPIC_"))}
        with plan_says(), unittest.mock.patch.dict(os.environ, without, clear=True):
            started = self.coordinator_as_root({})
        env = started["env"]
        self.assertEqual((env["ANTHROPIC_BASE_URL"], env["CLAUDE_CODE_MAX_RETRIES"]), ("https://x.invalid", "8"))
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

    def test_a_new_desks_claude_provider_and_coordinator_run_on_opus(self):
        with contextlib.redirect_stdout(io.StringIO()):
            main(["init", str(self.tmp / "new-desk")])
        s = Desk.find(str(self.tmp / "new-desk")).settings()
        self.assertEqual((s["providers"]["claude"]["model"], s["coordinator"]["model"]), ("opus", "opus"))

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

    @staticmethod
    def allowed_by(argv):
        return argv[argv.index("--allowedTools") + 1:]

    def test_workers_and_verifiers_are_told_the_lanes_setup_and_check_as_hansa_runs_them(self):
        from hansa import run
        seen = self.tmp / "argv.jsonl"
        key = "sk-registry-0123456789abcdef"
        self.desk.secrets_dir.mkdir(parents=True, exist_ok=True)
        (self.desk.secrets_dir / "registry.key").write_text(key)
        setup, check = f"echo installing --token {key}", "test -f hello.txt && echo 'tests  pass'"
        s = self.desk.settings()
        s["lanes"]["a"].update(env={"FAKE_SHOW_ARGV": str(seen)}, setup=setup, check=check)
        self.assertEqual(self.desk.save_settings(s), [])

        def prompts(verdict=None):
            tid = self.desk.new_task("Add hello.txt")
            m = self.settle(Dispatcher(self.desk), tid)
            self.assertEqual((m["state"], m["check"]["command"]), ("done", self.desk.settings()["lanes"]["a"]["check"]), m)
            if verdict is not None:
                self.desk.change(tid, lambda m: {"check": {**m["check"], "ok": verdict}})
            with contextlib.redirect_stdout(io.StringIO()):
                main(["--desk", str(self.desk.root), "verify", tid])
            self.settle(Dispatcher(self.desk), self.desk.meta(tid)["verify"])
            return [a[a.index("--append-system-prompt") + 1] for a in map(json.loads, seen.read_text().splitlines()[-2:])]
        task, verify = prompts()
        hidden = "`echo installing --token [key of registry]`"
        for prompt in (task, verify):
            self.assertIn(f"hansa ran the lane's setup in this worktree: {hidden}", prompt)
            self.assertIn(f"`{check}`", prompt)  # as run, its two spaces too
            self.assertNotIn(key, prompt)
        self.assertIn(f"After you end, hansa runs the lane's check here, in the same sandbox, and judges the task by it: `{check}`", task)
        self.assertIn("checking another worker's finished task", verify)
        self.assertIn(f"`{check}` The task's own run of it passed. You may run the same command yourself.", verify)
        self.assertNotIn("After you end, hansa runs", verify)  # a verification is never checked
        self.assertIn("The task's own run of it failed.", prompts(verdict=False)[1])

        lane = {"check": "make test"}  # a check of another lane, or one changed since: hansa does not know how it went
        other = self.desk.new_task("Done elsewhere")
        verify = {"kind": "verify", "parent": other}
        self.desk.update(other, lane="a", check={"command": "make other", "ok": True})
        note = run.around_note(self.desk, lane, verify, "a")
        self.assertIn("`make test` You may run", note)
        self.desk.update(other, check={"command": "make test", "ok": True})
        self.assertIn("`make test` The task's own run of it passed.", run.around_note(self.desk, lane, verify, "a"))
        # the same text on another lane ran under that lane's env and sandbox: no result of it here
        self.assertIn("`make test` You may run", run.around_note(self.desk, lane, verify, "b"))
        self.assertNotIn("own run", run.around_note(self.desk, lane, {"kind": "verify", "parent": "t9999"}, "a"))
        self.assertNotIn("setup", note)
        # a check that could not run is the machine's problem: the verifier does not read that it failed
        self.desk.update(other, check={"command": "make test", "ok": None, "could_not_run": True})
        self.assertIn("`make test` hansa could not run it for the task itself. You may run",
                      run.around_note(self.desk, lane, verify, "a"))
        for record in ({"command": "make test"}, {"command": "make test", "ok": None}):  # no result known: none said
            self.desk.update(other, check=record)
            self.assertIn("`make test` You may run", run.around_note(self.desk, lane, verify, "a"))

        long = "echo " + "x" * 700 + key  # a very long command is cut, after its keys are hidden
        note = run.around_note(self.desk, {"check": long}, {})
        self.assertIn(f"{'x' * (run.COMMAND_SHOWN - 5)}` (cut: the command is ", note)
        self.assertLess(len(note), run.COMMAND_SHOWN + 200)
        self.assertEqual(run.around_note(self.desk, {"check": "echo " + "x" * 594 + key}, {}).count(key[:6]), 0)

        s = self.desk.settings()  # a lane without a check says so, in one line
        s["lanes"]["a"].update(setup="", check="")
        self.assertEqual(self.desk.save_settings(s), [])
        tid = self.desk.new_task("Add hello.txt again")
        self.assertEqual(self.settle(Dispatcher(self.desk), tid)["state"], "done")
        with contextlib.redirect_stdout(io.StringIO()):
            main(["--desk", str(self.desk.root), "verify", tid])
        self.settle(Dispatcher(self.desk), self.desk.meta(tid)["verify"])
        for a in map(json.loads, seen.read_text().splitlines()[-2:]):
            prompt = a[a.index("--append-system-prompt") + 1]
            self.assertIn("- This lane has no check command: hansa runs nothing here after you end.\n", prompt)
            self.assertNotIn("lane's setup", prompt)

    def test_workers_may_run_any_command_in_the_sandbox_and_design_sessions_may_not(self):
        from hansa import run
        seen = self.tmp / "argv.jsonl"
        s = self.desk.settings()
        s["lanes"]["a"]["env"] = {"FAKE_SHOW_ARGV": str(seen)}
        self.assertEqual(self.desk.save_settings(s), [])
        tid = self.desk.new_task("Add hello.txt")
        self.assertEqual(self.settle(Dispatcher(self.desk), tid)["state"], "done")
        with contextlib.redirect_stdout(io.StringIO()):
            main(["--desk", str(self.desk.root), "verify", tid])
        self.settle(Dispatcher(self.desk), self.desk.meta(tid)["verify"])
        task, verify = [json.loads(x) for x in seen.read_text().splitlines()[-2:]]
        self.assertIn("checking another worker's finished task", verify[verify.index("--append-system-prompt") + 1])
        for argv, t in ((task, tid), (verify, self.desk.meta(tid)["verify"])):
            self.assertIn("Bash", self.allowed_by(argv))
            deny = self.denied_by(argv)  # weighed before the allow rules, so they still hold
            for r in ("Bash(git commit:*)", "Bash(git push:*)", "Bash(git stash:*)", "ScheduleWakeup",
                      run.rule("Read", self.desk.secrets_dir), run.rule("Read", self.desk.state_dir)):
                self.assertIn(r, deny)
            box = self.sandbox_of(argv)  # `Bash` is safe only while no command can leave the sandbox
            self.assertIs(box["allowUnsandboxedCommands"], False)
            self.assertNotIn("excludedCommands", box)
            prompt = argv[argv.index("--append-system-prompt") + 1]
            self.assertIn("any command runs", prompt)
            self.assertIn("scratch files there, not in this directory", prompt)
            flat = " ".join(prompt.split())  # its own run's folder, in full, as its commands see it; no bare $TMPDIR
            own = run.commands_tmp(self.desk.meta(t)["worker_tmps"][-1]["path"])
            self.assertIn(f"Your temporary folder, {own}, is yours alone", flat)
            self.assertIn("not as $TMPDIR: Claude Code cannot see what a variable stands for", flat)
            self.assertEqual(flat.count("$TMPDIR"), 1)
            self.assertIn("If a command you need is refused", prompt)
        self.assertIn("Bash(git commit:*)", self.denied_by(task))
        self.assertIn("Bash(git checkout:*)", self.allowed_by(verify))
        self.desk.update(tid, lane="a", provider="claude", worktree=str(self.repo), session_id="sid-d",
                         model_asked="sonnet")
        argv, _, _ = run.design_command(self.desk, tid)
        self.addCleanup(run.remove_sandbox_tmp, self.desk, tid)  # the run folder it made for the session
        self.assertNotIn("Bash", self.allowed_by(argv))  # outside the sandbox: the lane's rules and its own git
        self.assertIn("Bash(git commit:*)", self.allowed_by(argv))

    def test_each_worker_run_gets_its_own_temporary_folder_gone_when_it_ends(self):
        from hansa import run
        seen, argvs = self.tmp / "tmp.jsonl", self.tmp / "argv.jsonl"
        s = self.desk.settings()
        s["lanes"]["a"]["env"] = {"FAKE_SEE_TMP": str(seen), "FAKE_SHOW_ARGV": str(argvs), "TMPDIR": "/tmp"}
        self.assertEqual(self.desk.save_settings(s), [])
        server_tmp = unittest.mock.patch.dict(os.environ, {"TMPDIR": str(self.tmp)})  # the server's own, not the run's
        server_tmp.start()
        self.addCleanup(server_tmp.stop)
        d = Dispatcher(self.desk)
        t1 = self.desk.new_task("Add hello.txt")
        self.assertEqual(self.settle(d, t1)["state"], "done")
        t2 = self.desk.new_task("Add hello.txt again")
        self.assertEqual(self.settle(d, t2)["state"], "done")
        runs = [json.loads(x) for x in seen.read_text().splitlines()]
        argv = [json.loads(x) for x in argvs.read_text().splitlines()]
        self.assertEqual(len(runs), 2)
        self.assertEqual(WORKER_TMP_ROOT, "/tmp")  # as hansa has it: not the server's TMPDIR
        for r, a, tid in zip(runs, argv, (t1, t2)):
            where, name = os.path.split(r["tmp"])
            self.assertEqual(where, run.WORKER_TMP_ROOT)  # /tmp, or the test's own folder when a sandbox keeps /tmp closed
            self.assertTrue(name.startswith(f"hansa-{tid}-"), r)
            self.assertLessEqual(len(f"/tmp/{name}".encode()), 30, r)  # short: Claude Code makes sockets in it
            self.assertEqual((r["there"], r["mode"]), (True, 0o700), r)
            # also its TMPDIR, where Claude Code's own sandbox makes its sockets and empty folders, which it leaves
            self.assertEqual(r["tmpdir"], r["tmp"])
            # told in full what its commands see as $TMPDIR (Claude Code's own folder in it), never to write $TMPDIR:
            # Claude Code cannot place a path after `cd "$TMPDIR/x"`, and refuses the command
            prompt = " ".join(a[a.index("--append-system-prompt") + 1].split())
            self.assertEqual(run.commands_tmp(r["tmp"]), os.path.join(r["tmp"], f"claude-{os.getuid()}"))
            self.assertIn(f"Your temporary folder, {run.commands_tmp(r['tmp'])}, is yours alone", prompt)
            self.assertIn("Write its path out in full in commands, not as $TMPDIR", prompt)
            self.assertEqual(prompt.count("$TMPDIR"), 1)
            self.assertFalse(os.path.exists(r["tmp"]))  # removed once the worker ended
            allowed = self.allowed_by(a)
            for tool in ("Read", "Edit"):
                self.assertIn(run.rule(tool, Path(r["tmp"])), allowed)
        self.assertNotEqual(runs[0]["tmp"], runs[1]["tmp"])
        self.assertEqual(self.desk.meta(t1).get("worker_tmp"), "")
        for r, tid in zip(runs, (t1, t2)):  # where it was, kept after the run, in the part hansa writes
            shown = views.show_text(self.desk, tid)
            self.assertIn(f"\ntemporary folder: {r['tmp']} (removed when the run ended)\n", shown)
            self.assertLess(shown.index("temporary folder:"), shown.index("next:"))

        t5 = self.desk.new_task("Run twice")  # two runs of one task (another provider took over); one not removed
        first = run.make_worker_tmp(self.desk, t5)
        run.remove_sandbox_tmp(self.desk, t5)
        second = run.make_worker_tmp(self.desk, t5)
        self.assertIn(f"temporary folder of run 2 of 2: {second} (not removed yet)", views.show_text(self.desk, t5))
        with unittest.mock.patch.object(run.shutil, "rmtree"):
            run.remove_sandbox_tmp(self.desk, t5)
        self.addCleanup(shutil.rmtree, second, True)
        shown = views.show_text(self.desk, t5)
        self.assertIn(f"temporary folder of run 1 of 2: {first} (removed when the run ended)\n"
                      f"temporary folder of run 2 of 2: {second} (still there: removing it failed)", shown)
        self.desk.update(t5, state="cancelled")

        made = []  # a worker that could not start: the lane's setup needs srt, and there is none

        def making(desk, tid):
            made.append(real(desk, tid))
            return made[-1]
        real = run.make_worker_tmp
        s = self.desk.settings()
        s["lanes"]["a"]["setup"] = "true"
        self.assertEqual(self.desk.save_settings(s), [])
        t3 = self.desk.new_task("Add hello.txt once more")
        with self.without_srt(), unittest.mock.patch.object(run, "make_worker_tmp", making):
            m = self.settle(d, t3)
        self.assertEqual(m["state"], "failed", m)
        self.assertIn("could not start", m["error"])
        self.assertEqual(len(made), 1)
        self.assertFalse(os.path.exists(made[0]))

        s = self.desk.settings()  # cancelled while the worker runs
        del s["lanes"]["a"]["setup"]
        s["lanes"]["a"]["env"]["FAKE_TMP_WAIT"] = "30"
        self.assertEqual(self.desk.save_settings(s), [])
        t4 = self.desk.new_task("Add hello.txt slowly")
        d.tick()
        end = time.time() + 10
        while len(seen.read_text().splitlines()) < 3 and time.time() < end:
            time.sleep(0.1)
        tmp = json.loads(seen.read_text().splitlines()[-1])["tmp"]
        self.assertTrue(os.path.isdir(tmp))
        with contextlib.redirect_stdout(io.StringIO()):
            main(["--desk", str(self.desk.root), "cancel", t4])
        self.assertEqual(self.settle(d, t4)["state"], "cancelled")
        self.assertFalse(os.path.exists(tmp))

    def test_show_lists_a_refused_call_with_the_reason_claude_code_gave(self):
        s = self.desk.settings()  # on mimo: its session records are in the desk
        s["providers"]["mimo"] = {"kind": "compatible", "model": "mimo-x", "effort": "", "base_url": "https://x"}
        s["lanes"]["a"]["providers"] = ["mimo"]
        s["lanes"]["a"]["env"] = {"FAKE_REFUSED": 'B="${TMPDIR:-/tmp}/x" && make'}
        self.assertEqual(self.desk.save_settings(s), [])
        self.desk.write_key("mimo", "sk-test-01")
        m = self.settle(Dispatcher(self.desk), self.desk.new_task("Build it"))
        self.assertEqual(m["state"], "done", m)
        self.assertEqual(m["denial_reasons"], ["Contains expansion"])
        shown = views.show_text(self.desk, m["id"])
        self.assertIn("calls the worker had no permission for (1):", shown)
        self.assertIn('Bash: B="${TMPDIR:-/tmp}/x" && make; Claude Code said: Contains expansion', shown)
        self.assertNotIn("allowed commands", shown)

    def test_show_full_gives_a_long_refused_call_whole(self):
        s = self.desk.settings()
        s["providers"]["mimo"] = {"kind": "compatible", "model": "mimo-x", "effort": "", "base_url": "https://x"}
        s["lanes"]["a"]["providers"] = ["mimo"]
        long = "cd sub && " + " && ".join(f"echo step{i:03d}" for i in range(40)) + " && git stash list"
        self.assertGreater(len(long), 400)
        s["lanes"]["a"]["env"] = {"FAKE_REFUSED": long}
        self.assertEqual(self.desk.save_settings(s), [])
        self.desk.write_key("mimo", "sk-test-01")
        m = self.settle(Dispatcher(self.desk), self.desk.new_task("Build it"))
        self.assertEqual(m["denials"], [f"Bash: {long}"])  # kept whole on the task
        short = views.show_text(self.desk, m["id"])
        self.assertNotIn("git stash list", short)  # cut, as before
        self.assertIn(f"(whole: hansa show {m['id']} --full)", short)
        full = views.show_text(self.desk, m["id"], full=True)
        self.assertIn(f"  Bash: {long}\n    Claude Code said: Contains expansion", full)

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
                             ["lane a: write must be a list of up to 20 full paths starting with /, such as "
                              "/Users/me/hansa-cache/a/pip"])
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

        with as_home(home, PATH=f"{bin_}:relative/bin:/usr/bin"):
            for ok in (home / "hansa-cache" / "a" / "pip", home / "work" / "cache", self.tmp / "tools" / "cache"):  # a tool's cache
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
            for x in (bin_ / "git", bin_ / "sub", bin_ / "sub" / "x"):  # inside one: a program hansa may run
                self.assertEqual(refused(x), [
                    f"lane a: write cannot include {x}: it is inside {bin_}, a folder on the PATH hansa runs with: "
                    "a program there runs outside the sandbox (a cache folder goes elsewhere, such as a tools "
                    "folder of the desk's, another volume or /tmp)"], x)
            for x in (self.repo, self.repo / "src"):  # the lane's repository, or inside it
                self.assertEqual(refused(x), [f"lane a: write cannot include {x}: it would open {s['lanes']['a']['repo']}, "
                                              "the lane's repository; workers change code only in their worktree"])
            s["providers"]["claude"]["config_dir"] = ""  # ~/.claude by default
            self.assertIn("provider claude's Claude Code folder", refused(home / ".claude")[0])
            s["providers"]["claude"]["config_dir"] = str(self.tmp / "cfg")  # elsewhere: ~/.claude all the same
            for x in (home / ".claude", home / ".claude" / "hooks", home / ".claude" / ".." / ".claude"):
                self.assertEqual(refused(x), [f"lane a: write cannot include {x}: it would open {home / '.claude'}, "
                                              "the machine owner's Claude Code folder"], x)
            self.assertEqual(refused(home / "claude-cache"), [])  # another folder, by its name
            # a name the sandbox may read as a pattern, there or not, or where a link leads
            odd = [home / n for n in ("a)b", "(x", "star*", "what?", "[br", "br]", "{a,b", "b}")]
            (home / "odd (1)").mkdir()
            (home / "plain").symlink_to(home / "odd (1)")
            for x in (*odd, home / "cache" / "v*" / "pip", home / "odd (1)", home / "plain"):
                told = refused(x)
                self.assertTrue(len(told) == 1 and f"write cannot include {x}: " in told[0]
                                and "may read as a pattern" in told[0], (x, told))
            for x in (f"{home}/.m2\n/etc", f"{home}/.m2\r", f"{home}/be\x07ll", f"{home}/.m2\u2028"):  # as the read list
                told = refused(x)
                self.assertTrue(len(told) == 1 and f"write cannot include {json.dumps(x)}: it has a line break" in told[0],
                                (x, told))
            s["lanes"]["a"]["write"] = [str(home / "work" / "cache"), str(home / "star*")]  # the good one beside it
            self.assertEqual(len(validate(s, self.desk.root)), 1)
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as e:
                main(["--desk", str(self.desk.root), "lane", "set", "a", "write", str(home / "star*")])
            self.assertIn("may read as a pattern", str(e.exception.code))
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

    def test_a_run_leaves_out_a_saved_write_folder_that_no_longer_passes_the_fences(self):
        from hansa import run
        cache, lk = self.tmp / "m2", self.tmp / "lk"
        cache.mkdir()
        (self.tmp / "other").mkdir()
        lk.symlink_to(self.tmp / "other")
        self.hansa("lane", "set", "a", "write", str(cache), str(lk))
        lane = self.desk.settings()["lanes"]["a"]
        p = self.desk.settings()["providers"]["claude"]
        self.assertEqual(run.write_list(self.desk, lane), ([str(cache), str(lk)], []))
        lk.unlink()
        lk.symlink_to(self.desk.secrets_dir)  # pointed elsewhere since it was saved
        good, left = run.write_list(self.desk, lane)
        self.assertEqual(good, [str(cache)])
        self.assertEqual(len(left), 1)
        self.assertIn(f"left out of this run: {lk}: it is the desk's", left[0])
        for fs in (run.sandbox_settings(self.desk, lane, "claude", p, self.repo)["sandbox"]["filesystem"],
                   run.srt_settings(self.desk, lane, "claude", p, self.repo, str(self.tmp / "t"))["filesystem"]):
            self.assertIn(str(cache), fs["allowWrite"])
            self.assertFalse({str(lk), str(self.desk.secrets_dir)} & set(fs["allowWrite"]), fs["allowWrite"])
        # a list saved before a fence was there: written as it then was
        s = self.desk.settings()
        s["lanes"]["a"]["write"] = [str(Path.home() / ".claude" / "hooks"), str(self.tmp / "star*"), str(cache)]
        write_json(self.desk.settings_path, s)
        good, left = run.write_list(self.desk, s["lanes"]["a"])
        self.assertEqual((good, len(left)), ([str(cache)], 2))
        tid = self.desk.new_task("Add hello.txt")
        m = self.settle(Dispatcher(self.desk), tid)
        self.assertEqual(m["state"], "done", m)
        self.assertEqual(m["write_left_out"], left)  # a line in the task for each
        shown = views.show_text(self.desk, tid)
        self.assertIn(f"write list: left out of this run: {Path.home() / '.claude' / 'hooks'}: it would open", shown)
        self.assertIn(f"write list: left out of this run: {self.tmp / 'star*'}: a name in it", shown)
        self.assertEqual([e["task"] for e in self.events("write_left_out")], [tid, tid])

    def refused_and_left_out(self, path, why):
        """A write folder is refused on saving, and one saved earlier is left out at a run's start with a line."""
        from hansa import run
        s = self.desk.settings()
        s["lanes"]["a"]["write"] = [str(path)]
        told = validate(s, self.desk.root)
        self.assertTrue(len(told) == 1 and "write cannot include " in told[0] and why in told[0], (path, told))
        write_json(self.desk.settings_path, s)  # saved before the fence was there
        good, left = run.write_list(self.desk, s["lanes"]["a"])
        self.assertEqual(good, [], path)
        self.assertTrue(len(left) == 1 and left[0].startswith("left out of this run: ") and why in left[0], (path, left))
        p = s["providers"]["claude"]
        for fs in (run.sandbox_settings(self.desk, s["lanes"]["a"], "claude", p, self.repo)["sandbox"]["filesystem"],
                   run.srt_settings(self.desk, s["lanes"]["a"], "claude", p, self.repo, str(self.tmp / "t"))["filesystem"]):
            self.assertNotIn(str(path), fs.get("allowWrite", []))
        return told[0]

    def programs_elsewhere(self):
        """Stand-ins for the programs hansa runs, installed as they are on a real machine; the PATH that finds them."""
        def program(at, text="#!/bin/sh\n"):
            at.parent.mkdir(parents=True, exist_ok=True)
            at.write_text(text)
            at.chmod(0o755)
            return at

        tools, brew, share = self.tmp / "tools", self.tmp / "brew", self.tmp / "share"
        for d in (tools / "bin", brew / "bin"):
            d.mkdir(parents=True)
        (tools / "bin" / "claude").symlink_to(program(share / "claude" / "versions" / "2.1.0"))  # a folder of versions
        program(tools / "lib" / "node_modules" / "@anthropic-ai" / "sandbox-runtime" / "dist" / "cli.js")
        (tools / "bin" / "srt").symlink_to("../lib/node_modules/@anthropic-ai/sandbox-runtime/dist/cli.js")  # an npm package
        program(brew / "Cellar" / "tmux" / "3.4" / "bin" / "tmux")
        (brew / "bin" / "tmux").symlink_to("../Cellar/tmux/3.4/bin/tmux")  # Homebrew
        program(self.tmp / "nodejs" / "bin" / "node")
        core = self.tmp / "gitp" / "libexec" / "git-core"
        core.mkdir(parents=True)
        real = shutil.which("git")  # it says where its own programs are, and is git for the rest
        program(self.tmp / "gitp" / "bin" / "git", f"#!/bin/sh\n[ \"$1\" = --exec-path ] && echo {shlex.quote(str(core))} "
                                                   f"&& exit 0\nexec {shlex.quote(real)} \"$@\"\n")
        return os.pathsep.join(str(d) for d in (tools / "bin", brew / "bin", self.tmp / "nodejs" / "bin",
                                                self.tmp / "gitp" / "bin", "/usr/bin", "/bin"))

    def test_a_lanes_write_list_opens_no_program_that_runs_outside_the_sandbox(self):
        home = self.tmp / "home"
        home.mkdir()
        path = self.programs_elsewhere()
        tools, brew, share, gitp = self.tmp / "tools", self.tmp / "brew", self.tmp / "share", self.tmp / "gitp"
        (self.tmp / "lv").symlink_to(share / "claude" / "versions")
        (self.tmp / "lpkg").symlink_to(tools / "lib" / "node_modules" / "@anthropic-ai")
        os.environ.pop("HANSA_CLAUDE", None)
        os.environ.pop("HANSA_TMUX", None)
        bad = {share / "claude": f"it would open {share / 'claude'}, where claude is installed",
               share / "claude" / "versions": f"it would open {share / 'claude'}, where claude is installed",
               share / "claude" / "versions" / "2.1.1": f"it would open {share / 'claude'}, where claude is installed",
               share: f"it would open {share / 'claude'}, where claude is installed",  # a folder above it
               self.tmp / "lv": f"it would open {share / 'claude'}, where claude is installed",  # through a link
               tools / "lib" / "node_modules": "where srt is installed",
               tools / "lib" / "node_modules" / "@anthropic-ai" / "sandbox-runtime" / "dist": "where srt is installed",
               tools / "lib" / "node_modules" / "left-pad": "where srt is installed",  # a package srt may load
               self.tmp / "lpkg": "where srt is installed",
               brew / "Cellar": f"it would open {brew / 'Cellar'}, where tmux is installed",
               brew / "Cellar" / "tmux" / "3.4": f"it would open {brew / 'Cellar'}, where tmux is installed",
               brew / "Cellar" / "libevent": f"it would open {brew / 'Cellar'}, where tmux is installed",
               brew / "lib": f"it would open {brew / 'lib'}, where tmux is installed",
               brew / "etc": f"it would open {brew / 'etc'}, where tmux is installed",
               brew / "opt" / "libevent": f"it would open {brew / 'opt'}, where tmux is installed",
               self.tmp / "nodejs" / "lib" / "node_modules": "which holds what node loads when it starts",
               self.tmp / "nodejs" / "bin" / "node": "the program node, which runs outside the sandbox",
               gitp / "libexec" / "git-core": "git's own programs",
               gitp / "libexec": "git's own programs", gitp / "lib": "which holds what git loads when it starts",
               gitp / "etc" / "gitconfig": "the settings git reads for the whole system",
               gitp / "etc": "the settings git reads for the whole system"}
        with as_home(home, PATH=path, XDG_CACHE_HOME="/srv/x/tools/xdg-cache"):
            for x, why in bad.items():
                told = self.refused_and_left_out(x, why)
                self.assertIn("outside the sandbox" if "git reads" not in why else "a command git runs", told)
            self.refused_and_left_out("/srv/x/tools/xdg-cache/claude-cli-nodejs", "Claude Code's own files")
            # a build or test tool's cache, in a plain folder of the home folder or outside it, beside the programs too
            fine = ["/srv/x/tools/xdg-cache/pnpm", "/srv/x/tools/xdg-cache/pip", "/srv/x/tools/cache", "/var/cache/x",
                    "/opt/build-cache", str(home / "hansa-cache" / "a" / "pip"), str(home / "work" / "cache"),
                    str(tools / "cache"), str(brew / "var" / "cache"), str(self.tmp / "nodejs" / "cache")]
            self.assertEqual(write_problems(fine, str(self.repo), self.desk.root, self.desk.settings()["providers"]), [])
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as e:
                main(["--desk", str(self.desk.root), "lane", "set", "a", "write", str(share / "claude" / "versions")])
            self.assertIn("where claude is installed", str(e.exception.code))
            self.hansa("lane", "set", "a", "write", *fine)
            self.assertEqual(self.desk.settings()["lanes"]["a"]["write"], fine)

    def test_a_lanes_write_list_opens_no_startup_or_settings_file_nor_where_python_loads_from(self):
        import site
        import sysconfig
        import hansa as package
        home = self.tmp / "home"
        home.mkdir()
        (self.tmp / "lrc").symlink_to(home / ".zshrc")
        shell, git, tmux = "a shell's startup file", "git's settings", "tmux's settings"
        bad = {**{home / n: shell for n in (".bashrc", ".bash_profile", ".profile", ".zshrc", ".zshenv", ".zprofile",
                                           ".zlogin", ".bash_login", ".config/fish/conf.d")},
               self.tmp / "lrc": shell,
               home / ".gitconfig": git, home / ".config" / "git": "it would open", home / ".config" / "git" / "hooks": git,
               home / ".tmux.conf": tmux, home / ".config" / "tmux": tmux,
               home / ".local" / "share" / "claude": "Claude Code's own files",
               home / ".local" / "share": "it would open",
               home / ".cache" / "claude-cli-nodejs" / "x": "Claude Code's own files",
               home / ".cache": "it would open",
               home / "Library" / "Caches" / "claude-cli-nodejs":  # on macOS all of ~/Library is closed before it
                   "it would open" if sys.platform == "darwin" else "Claude Code's own files",
               home / ".srt-settings.json": "srt's settings", home / ".hansa" / "followers": "hansa's own files",
               "/etc/gitconfig": "it would open /etc", "/usr/lib/x": "it would open /usr/lib"}
        loads = "which the python that runs hansa loads modules from"
        version = f"{sys.version_info[0]}.{sys.version_info[1]}"
        user = home / ".local" / "lib" / f"python{version}" / "site-packages"
        mods = {user: loads, user.parent: loads, user / "x": loads, home / ".local" / "lib": loads,
                Path(sysconfig.get_paths()["stdlib"]): loads, Path(sysconfig.get_paths()["stdlib"]) / "json": loads,
                Path(sysconfig.get_paths()["purelib"]): loads, Path(site.getsitepackages()[0]) / "pkg": loads,
                Path(package.__file__).resolve().parent: "hansa's own code",
                Path(package.__file__).resolve().parent / "sub": "hansa's own code"}
        with as_home(home):
            for x, why in {**bad, **mods}.items():
                self.assertIn("it would open", self.refused_and_left_out(x, why))
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as e:
                main(["--desk", str(self.desk.root), "lane", "set", "a", "write", str(home / ".m2"), str(home / ".zshenv")])
            self.assertIn(shell, str(e.exception.code))
            with unittest.mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.tmp / "xdg"), "ZDOTDIR": str(self.tmp / "z"),
                                                       "BASH_ENV": str(self.tmp / "env.sh")}):
                for x, why in {self.tmp / "xdg" / "git": git, self.tmp / "xdg" / "tmux": tmux,
                               self.tmp / "z" / ".zshenv": shell, self.tmp / "env.sh": shell}.items():
                    self.refused_and_left_out(x, why)
                self.assertEqual(write_problems([str(self.tmp / "xdg" / "pip"), str(self.tmp / "z" / "cache")]), [])

    def test_a_lanes_write_list_holds_in_the_home_folder_only_plain_folders(self):
        from hansa import desk, run
        home = (self.tmp / "home").resolve()
        (home / "work").mkdir(parents=True)
        (home / "work" / "lz").symlink_to(home / ".zsh")
        (self.tmp / "lbun").symlink_to(home / ".bun")
        (self.tmp / "lpip").symlink_to(home / ".cache" / "pip")
        (home / ".cache").mkdir()
        (home / ".cache" / "ln").symlink_to(home / ".nvm")
        only = f"it is inside {home}, the home folder, where a write list may hold only plain folders"
        hidden = [".zsh", ".bun", ".zcompdump", ".gitconfig.local", ".nvm", ".pyenv", ".p10k.zsh", ".m2", ".gradle",
                  ".npm", ".local/share/pnpm", ".cargo/bin", ".config/x", ".local/bin", ".bun/install",
                  "work/.hidden", "work/plain/.venv/lib", "work/lz", ".cache/ln", "Applications", "Applications/X.app"]
        # a user's caches: shared with what runs outside the sandbox (an updater, corepack, the user's own builds)
        caches = [".cache/pip", ".cache/pip/http", ".cache/node", ".cache/node/corepack", ".cache/codex-runtimes",
                  ".m2/repository", ".m2/repository/org", ".gradle/caches", ".npm/_cacache", ".pnpm-store",
                  ".local/share/pnpm/store", ".cargo/registry", ".cargo/git/db", ".nuget/packages", ".ivy2/cache",
                  ".conan2/p", ".bun/install/cache"]
        with as_home(home):
            for x in [home / n for n in hidden + caches] + [self.tmp.resolve() / "lbun", self.tmp.resolve() / "lpip"]:
                told = self.refused_and_left_out(x, only)
                self.assertIn("such as ~/work/cache: a user's caches", told)
                self.assertIn("are shared with what runs outside the sandbox; give the lane a folder of its own, such "
                              "as ~/hansa-cache/<lane>/<tool>, and point the tool there with the lane's env: "
                              "PIP_CACHE_DIR, npm_config_cache", told)  # what can be written instead
            in_library = ("Library/Anything", "Library/LaunchAgents", "Library/Application Support/x", "Library/Caches/pip",
                          "Library/Caches/Homebrew", "Library/Caches/com.anthropic.claudefordesktop.ShipIt")
            for x in in_library:
                told = self.refused_and_left_out(home / x, str(home / "Library") if sys.platform == "darwin" else only)
                self.assertNotIn("Caches and folders inside it can be listed", told)  # they cannot
                self.assertIn("~/hansa-cache/<lane>/<tool>", told)
            for system in ("linux", "darwin"):  # on either system, whichever fence says it
                with unittest.mock.patch.object(sys, "platform", system):
                    for x in in_library:
                        told = write_problems([str(home / x)])
                        self.assertTrue(len(told) == 1 and "~/hansa-cache/<lane>/<tool>" in told[0], (system, told))
                        self.assertNotIn("can be listed)", told[0].replace("nothing in it can be listed", ""))
            self.refused_and_left_out(home / "node_modules", "a folder node loads packages from")
            self.assertFalse(hasattr(desk, "PACKAGE_CACHES") or hasattr(desk, "HOME_CACHES"))  # no exception by name
            fine = [str(home / n) for n in ("hansa-cache/a/pip", "hansa-cache/a/npm", "work/cache", "gopher",
                                            "work/go/pkg/mod", "build cache")]
            providers = self.desk.settings()["providers"]
            for x in fine + ["/srv/x/tools/xdg-cache/pnpm", "/tmp/cache"]:
                self.assertEqual(write_problems([x], str(self.repo), self.desk.root, providers), [], x)
            self.hansa("lane", "set", "a", "write", *fine)
            self.assertEqual(self.desk.settings()["lanes"]["a"]["write"], fine)
            self.assertEqual(run.write_list(self.desk, self.desk.settings()["lanes"]["a"]), (fine, []))
            # a list saved earlier, when ~/.cache/pip passed: left out at a run's start, with a line; the rest stays
            s = self.desk.settings()
            s["lanes"]["a"]["write"] = [str(home / ".cache" / "pip"), str(home / "hansa-cache" / "a" / "pip")]
            write_json(self.desk.settings_path, s)
            good, left = run.write_list(self.desk, s["lanes"]["a"])
            self.assertEqual(good, [str(home / "hansa-cache" / "a" / "pip")])
            self.assertEqual(len(left), 1)
            self.assertIn(f"left out of this run: {home / '.cache' / 'pip'}: {only}", left[0])
            p = s["providers"]["claude"]
            for fs in (run.sandbox_settings(self.desk, s["lanes"]["a"], "claude", p, self.repo)["sandbox"]["filesystem"],
                       run.srt_settings(self.desk, s["lanes"]["a"], "claude", p, self.repo, str(self.tmp / "t"))["filesystem"]):
                self.assertIn(str(home / "hansa-cache" / "a" / "pip"), fs["allowWrite"])
                self.assertNotIn(str(home / ".cache" / "pip"), fs["allowWrite"])

    def test_a_lanes_write_list_opens_no_well_known_tool_home_with_a_plain_name(self):
        home = (self.tmp / "home").resolve()
        (home / "work").mkdir(parents=True)
        (home / "work" / "lgo").symlink_to(home / "go")
        with as_home(home):
            for top in ("go", "miniconda3", "anaconda3", "miniforge3", "mambaforge"):
                for x in (home / top, home / top / "pkg" / "mod", home / top / "bin"):
                    told = self.refused_and_left_out(x, f"it would open {home / top}, ")
                    self.assertIn("~/hansa-cache/<lane>/<tool>", told)
            self.assertIn("Go's default GOPATH", self.refused_and_left_out(home / "work" / "lgo" / "pkg", str(home / "go")))
            self.assertIn("a conda install", write_problems([str(home / "miniconda3" / "envs")])[0])
            fine = [str(home / n) for n in ("gopher", "go-cache", "work/go", "miniconda3x", "hansa-cache/a/go")]
            self.assertEqual(write_problems(fine, str(self.repo), self.desk.root, self.desk.settings()["providers"]), [])

    def test_a_lanes_env_names_may_be_lower_case_and_the_managed_ones_stay_refused_as_spelled(self):
        from hansa import run
        names = {"npm_config_cache": "/c/npm", "npm_config_store_dir": "/c/pnpm", "http_proxy": "http://p:1",
                 "PIP_CACHE_DIR": "/c/pip", "Mixed_Case9": "x", "_x": "y"}
        self.hansa("lane", "set", "a", "env", *[f"{k}={v}" for k, v in names.items()])
        self.assertEqual(self.desk.settings()["lanes"]["a"]["env"], names)
        s = self.desk.settings()
        for bad in ("9x", "a-b", "a.b", "a b", "", "é"):
            s["lanes"]["a"]["env"] = {bad: "x"}
            self.assertEqual(validate(s, self.desk.root), [f"lane a: env {bad} must be NAME=text"])
        for managed in ("CLAUDE_CONFIG_DIR", "HANSA_TASK", "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL"):
            s["lanes"]["a"]["env"] = {managed: "x"}
            self.assertEqual(validate(s, self.desk.root), [f"lane a: env {managed} is set from the provider, not the lane"])
        for other in ("claude_config_dir", "hansa_task", "anthropic_api_key"):  # another spelling: another variable
            s["lanes"]["a"]["env"] = {other: "x"}
            self.assertEqual(validate(s, self.desk.root), [])

    def test_explain_lane_marks_a_write_entry_a_run_would_leave_out(self):
        home = (self.tmp / "home").resolve()
        home.mkdir()
        own, pip = str(home / "hansa-cache" / "a" / "pip"), str(home / ".cache" / "pip")
        with as_home(home):
            s = self.desk.settings()
            s["lanes"]["a"]["write"] = [own]
            write_json(self.desk.settings_path, s)
            text = views.explain_text(self.desk, "lane", "a")
            self.assertIn(f"  folders a worker may also write (a build or test tool's cache): {own}\n", text)
            self.assertNotIn("a run leaves out", text)
            s["lanes"]["a"]["write"] = [pip, own, str(home / "go" / "pkg" / "mod")]  # saved before the fences
            write_json(self.desk.settings_path, s)
            lines = views.explain_text(self.desk, "lane", "a").splitlines()
            at = lines.index(f"  folders a worker may also write (a build or test tool's cache): {own}")
            self.assertTrue(lines[at + 1].startswith(f"    a run leaves out {pip}: it is inside {home}, the home folder, "
                                                     "where a write list may hold only plain folders"), lines[at + 1])
            self.assertTrue(lines[at + 2].startswith(f"    a run leaves out {home / 'go' / 'pkg' / 'mod'}: it would open "
                                                     f"{home / 'go'}, Go's default GOPATH"), lines[at + 2])
            self.assertTrue(lines[at + 1].endswith("(the write list is changed with hansa lane set)"))
            s["lanes"]["a"]["write"] = [pip]
            write_json(self.desk.settings_path, s)
            self.assertIn("  folders a worker may also write (a build or test tool's cache): none\n    a run leaves out ",
                          views.explain_text(self.desk, "lane", "a"))

    def lane_problems(self, env, write=()):
        s = self.desk.settings()
        s["lanes"]["a"].update(env=env, write=[str(x) for x in write])
        return validate(s, self.desk.root)

    def test_a_folder_on_a_lanes_own_path_is_closed_to_workers(self):
        from hansa import run
        home = (self.tmp / "home").resolve()
        own = home / "hansa-cache" / "a"
        (own / "bin").mkdir(parents=True)
        (self.tmp / "lbin").symlink_to(own / "bin")
        with as_home(home):
            env = {"PATH": f"{own / 'bin'}:/usr/bin:/bin", "CARGO_HOME": str(own / "cargo")}
            self.assertEqual(self.lane_problems(env, [own / "cargo", own / "pip"]), [])
            # the folder, inside it, above it, and through a link
            for x in (own / "bin", own / "bin" / "srt", own, home / "hansa-cache", self.tmp.resolve() / "lbin" / "srt"):
                told = self.lane_problems(env, [x])
                self.assertTrue(len(told) == 1 and f"lane a: write cannot include {x}: it would open {own / 'bin'}, a "
                                "folder on the lane's PATH (its env)" in told[0], (x, told))
                self.assertIn("inside the check or setup command", told[0])
            self.hansa("lane", "set", "a", "env", *[f"{k}={v}" for k, v in env.items()])  # as the coordinator would
            self.assertEqual(self.desk.settings()["lanes"]["a"]["env"], env)  # the env is saved
            with self.assertRaises(SystemExit):  # and the write entry is refused
                self.hansa("lane", "set", "a", "write", str(own / "bin"))
            self.assertNotIn(str(own / "bin"), self.desk.settings()["lanes"]["a"].get("write", []))
            # saved earlier: left out at a run's start, with a line
            s = self.desk.settings()
            s["lanes"]["a"].update(env=env, write=[str(own / "bin"), str(own / "pip")])
            write_json(self.desk.settings_path, s)
            good, left = run.write_list(self.desk, s["lanes"]["a"])
            self.assertEqual(good, [str(own / "pip")])
            self.assertTrue(len(left) == 1 and left[0].startswith(f"left out of this run: {own / 'bin'}: it would open "
                                                                  f"{own / 'bin'}, a folder on the lane's PATH"), left)
            p = s["providers"]["claude"]
            for fs in (run.sandbox_settings(self.desk, s["lanes"]["a"], "claude", p, self.repo)["sandbox"]["filesystem"],
                       run.srt_settings(self.desk, s["lanes"]["a"], "claude", p, self.repo, str(self.tmp / "t"))["filesystem"]):
                self.assertIn(str(own / "pip"), fs["allowWrite"])
                self.assertNotIn(str(own / "bin"), fs["allowWrite"])
            tid = self.desk.new_task("Add hello.txt")
            m = self.settle(Dispatcher(self.desk), tid)
            self.assertEqual(m["state"], "done", m)
            self.assertEqual(m["write_left_out"], left)
            self.assertIn(f"write list: left out of this run: {own / 'bin'}: it would open", views.show_text(self.desk, tid))

    def popens(self):
        """Patch for subprocess.Popen that keeps the environment of each process hansa starts, and what the
        lane's env file held then: (the patch, the environments, the files' texts)."""
        envs, files, real = [], [], subprocess.Popen

        def popen(argv, *args, **kw):
            env = dict(kw.get("env") or os.environ)
            envs.append(env)
            if env.get("CLAUDE_ENV_FILE"):
                files.append((env["CLAUDE_ENV_FILE"], Path(env["CLAUDE_ENV_FILE"]).read_text()))
            return real(argv, *args, **kw)
        return unittest.mock.patch("subprocess.Popen", popen), envs, files

    def test_a_worker_starts_without_the_lanes_env_and_its_commands_get_it_from_a_file(self):
        value = "two words, 'single' \"double\" $HOME `id` \\ and\ta tab"
        s = self.desk.settings()
        s["lanes"]["a"]["env"] = {"LANE_VAR": value, "npm_config_cache": "/srv/x/cache", "FAKE_WRITE": "from the file",
                                  "TMPDIR": "/tmp"}
        self.assertEqual(self.desk.save_settings(s), [])
        patch, envs, files = self.popens()
        with patch:
            m = self.settle(Dispatcher(self.desk), self.desk.new_task("Add hello.txt"))
        self.assertEqual(m["state"], "done", m)
        [claude] = [e for e in envs if e.get("CLAUDE_ENV_FILE")]
        self.assertFalse({"LANE_VAR", "npm_config_cache", "FAKE_WRITE"} & set(claude), claude)  # no lane variable
        self.assertFalse([e for e in envs if value in e.values()])
        self.assertNotEqual(claude["TMPDIR"], "/tmp")  # the run's own
        [(path, text)] = files
        self.assertTrue(Path(path).resolve().is_relative_to(self.desk.state_dir.resolve()), path)  # no worker writes there
        self.assertEqual(text, f"export LANE_VAR={shlex.quote(value)}\nexport npm_config_cache=/srv/x/cache\n"
                               "export FAKE_WRITE='from the file'\n")  # quoted; the temporary folder stays the run's
        r = subprocess.run(["/bin/sh", "-c", text + 'printf %s "$LANE_VAR"'], capture_output=True, text=True,
                           env={"PATH": "/usr/bin:/bin"})
        self.assertEqual(r.stdout, value)  # as a shell reads it back
        self.assertIn("notes.txt", m["files"])  # the stand-in read the file, as Claude Code does for its commands
        # a lane with no env: no file
        s["lanes"]["a"]["env"] = {}
        self.assertEqual(self.desk.save_settings(s), [])
        patch, envs, files = self.popens()
        with patch:
            self.settle(Dispatcher(self.desk), self.desk.new_task("Add hello.txt"))
        self.assertEqual(files, [])

    def test_setup_check_lane_try_and_the_merge_check_get_the_lanes_env_inside_the_sandbox_only(self):
        from hansa import run
        value, out = "it's $HOME and \"more\"", self.tmp / "seen"
        out.mkdir()
        d, t1, t2 = self.two_lanes()
        s = self.desk.settings()
        step = lambda name: f'printf %s "$LANE_VAR" > {out}/{name}-$HANSA_TASK'
        s["lanes"]["b"].update(env={"LANE_VAR": value, "FAKE_HELLO": "c.txt", "TMPDIR": "/tmp"}, setup=step("setup"),
                               check=step("check"))
        self.assertEqual(self.desk.save_settings(s), [])
        calls = len(self.srt_calls(tries=True))
        patch, envs, _ = self.popens()
        with patch:
            t3 = self.desk.new_task("Add c.txt", lane="b")
            m = self.settle(d, t3)  # a task's setup and check
            self.assertEqual((m["state"], m["check"]["ok"]), ("done", True), m)
            self.hansa("merge", t1)
            self.hansa("merge", t3)  # the base has moved: the setup and the check again, for the merge
            self.assertEqual(self.settle_merge(d, t3)["state"], "merged")
            run.write_try(self.desk, "b", {"id": "y1", "phase": "queued", "asked": time.time()})  # hansa lane try
            end = time.time() + 20
            while run.read_try(self.desk, "b").get("phase") != "done" and time.time() < end:
                d.tick()
                time.sleep(0.1)
        self.assertTrue(run.read_try(self.desk, "b").get("ok"), run.read_try(self.desk, "b"))
        # inside the sandbox: each command saw the variable
        self.assertEqual({f.name: f.read_text() for f in out.iterdir()},
                         {f"setup-{t3}": value, f"check-{t3}": value, "setup-": value, "check-": value})
        ran = self.srt_calls(tries=True)[calls:]
        self.assertEqual(len([c for c in ran if c["argv"][-2:] != ["--", "true"]]), 6)  # 2 for each of the three
        for c in ran:  # srt itself: hansa's environment, the variable only in the command it is given to run
            self.assertNotIn("LANE_VAR", c["env"])
            self.assertNotEqual(c["env"].get("TMPDIR"), "/tmp")
            if c["argv"][-2:] != ["--", "true"]:
                self.assertTrue(c["argv"][-1].startswith(f"exec /usr/bin/env {shlex.quote('LANE_VAR=' + value)} "
                                                         "FAKE_HELLO=c.txt /bin/sh -c "), c["argv"])
        self.assertTrue(envs)
        self.assertFalse([e for e in envs if "LANE_VAR" in e or "FAKE_HELLO" in e])  # nor the scripts around it

    def test_a_relative_xdg_config_home_in_a_lanes_env_does_not_reach_the_git_hansa_runs(self):
        ran = self.tmp / "monitor-ran"
        monitor = self.tmp / "monitor"
        monitor.write_text(f"#!/bin/sh\necho ran >> {ran}\n")
        monitor.chmod(0o755)
        (self.repo / "cfg" / "git").mkdir(parents=True)  # as a worker's change would hold it
        (self.repo / "cfg" / "git" / "config").write_text(f"[core]\n\tfsmonitor = {monitor}\n")
        sh("git", "add", ".", cwd=self.repo)
        sh("git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "cfg", cwd=self.repo)
        self.hansa("lane", "set", "a", "env", "XDG_CONFIG_HOME=cfg")
        self.hansa("lane", "set", "a", "setup", "true")
        self.hansa("lane", "set", "a", "check", "true")
        m = self.settle(Dispatcher(self.desk), self.desk.new_task("Add hello.txt"))
        self.assertEqual(m["state"], "done", m)
        self.assertFalse(ran.exists(), "git, run by hansa outside the sandbox, ran the repository's fsmonitor command")
        self.assertEqual([c["env"].get("XDG_CONFIG_HOME") for c in self.srt_calls(tries=True)
                          if c["env"].get("XDG_CONFIG_HOME") == "cfg"], [])

    def test_an_srt_on_a_lanes_path_is_never_run(self):
        planted, ran = self.tmp / "planted", self.tmp / "planted-ran"
        planted.mkdir()
        (planted / "srt").write_text(f'#!/bin/sh\necho "$@" >> {ran}\nshift 3\nexec "$@"\n')  # put there beforehand
        (planted / "srt").chmod(0o755)
        s = self.desk.settings()
        s["lanes"]["a"].update(env={"PATH": f"{planted}{os.pathsep}{os.environ['PATH']}"}, setup="true",
                               check=f"command -v srt > {self.tmp}/where.txt")
        write_json(self.desk.settings_path, s)  # saved earlier
        from hansa import run
        d = Dispatcher(self.desk)
        m = self.settle(d, self.desk.new_task("Add hello.txt"))
        run.write_try(self.desk, "a", {"id": "y1", "phase": "queued", "asked": time.time()})
        end = time.time() + 20
        while run.read_try(self.desk, "a").get("phase") != "done" and time.time() < end:
            d.tick()
            time.sleep(0.1)
        self.assertEqual((m["state"], m["check"]["ok"]), ("done", True), m)
        self.assertFalse(ran.exists(), "hansa ran the srt on the lane's PATH")
        calls = self.srt_calls(tries=True)  # srt came from hansa's own PATH
        self.assertEqual(len(calls), 7, [c["argv"] for c in calls])  # the task's and the try's setup and check
        self.assertFalse([c for c in calls if str(planted) in c["env"].get("PATH", "")])
        # the lane's PATH still reaches the command in the sandbox
        self.assertEqual((self.tmp / "where.txt").read_text().strip(), str(planted / "srt"))

    def test_what_another_lanes_env_names_is_closed_to_this_lanes_workers(self):
        from hansa import run
        shared = self.tmp.resolve() / "shared"
        (shared / "bin").mkdir(parents=True)
        s = self.desk.settings()
        s["lanes"]["b"] = json.loads(json.dumps(s["lanes"]["a"]))
        s["lanes"]["b"]["env"] = {"PATH": f"{shared / 'bin'}:/usr/bin:/bin", "XDG_CONFIG_HOME": str(shared / "cfg"),
                                  "PIP_CACHE_DIR": str(shared / "pip")}
        self.assertEqual(self.desk.save_settings(s), [])
        for x, why in ((shared / "bin", f"it would open {shared / 'bin'}, a folder on lane b's PATH (its env)"),
                       (shared / "bin" / "srt", f"it would open {shared / 'bin'}, a folder on lane b's PATH (its env)"),
                       (shared, f"it would open {shared / 'bin'}, a folder on lane b's PATH (its env)"),
                       (shared / "cfg" / "git", f"it would open {shared / 'cfg'}, which lane b's env names in "
                                                "XDG_CONFIG_HOME")):
            s["lanes"]["a"]["write"] = [str(x)]
            told = validate(s, self.desk.root)
            self.assertTrue(len(told) == 1 and told[0].startswith(f"lane a: write cannot include {x}: {why}"), (x, told))
            with self.assertRaises(SystemExit):
                self.hansa("lane", "set", "a", "write", str(x))
        self.assertEqual(self.desk.settings()["lanes"]["a"].get("write", []), [])
        self.hansa("lane", "set", "a", "write", str(shared / "pip"), str(shared / "other"))  # a cache variable's folder
        # the other way round: lane b's env may not come to name what lane a's workers write
        with self.assertRaises(SystemExit):
            self.hansa("lane", "set", "b", "env", f"PATH={shared / 'other'}:/usr/bin")
        self.assertEqual(self.desk.settings()["lanes"]["b"]["env"], s["lanes"]["b"]["env"])
        # saved earlier: left out at a run's start, with a line
        s["lanes"]["a"]["write"] = [str(shared / "bin"), str(shared / "pip")]
        write_json(self.desk.settings_path, s)
        good, left = run.write_list(self.desk, self.desk.settings()["lanes"]["a"])
        self.assertEqual(good, [str(shared / "pip")])
        self.assertTrue(len(left) == 1 and "a folder on lane b's PATH" in left[0], left)
        m = self.settle(Dispatcher(self.desk), self.desk.new_task("Add hello.txt", lane="a"))
        self.assertEqual((m["state"], m["write_left_out"]), ("done", left), m)

    def test_a_lanes_path_holds_only_full_paths_outside_the_desk(self):
        for path, entry in (("bin:/usr/bin", "bin"), ("/usr/bin::/bin", ""), (":/usr/bin", ""), ("/usr/bin:", ""),
                            ("./node_modules/.bin:/usr/bin", "./node_modules/.bin"), ("", ""), ("~/bin:/bin", "~/bin")):
            told = self.lane_problems({"PATH": path})
            self.assertTrue(len(told) == 1 and told[0].startswith(f"lane a: env PATH cannot have the entry {entry!r}: "
                                                                  "each folder on a lane's PATH must be a full path"), told)
        for inside in (self.desk.root / "tools" / "bin", self.desk.worktrees_dir / "a" / "node_modules" / ".bin",
                       self.desk.root):
            told = self.lane_problems({"PATH": f"/usr/bin:{inside}"})
            self.assertTrue(len(told) == 1 and told[0].startswith(f"lane a: env PATH cannot name {inside}: it is the "
                                                                  "desk or inside it"), told)
            told = self.lane_problems({"FOO_DIR": str(inside), "PIP_CACHE_DIR": f"{inside}"})  # any variable
            self.assertEqual([t.split(" cannot name ")[0] for t in told], ["lane a: env FOO_DIR", "lane a: env PIP_CACHE_DIR"])
        self.assertEqual(self.lane_problems({"PATH": "/srv/x/tools/node/bin:/usr/local/bin:/usr/bin:/bin"}), [])
        with self.assertRaises(SystemExit):
            self.hansa("lane", "set", "a", "env", "PATH=bin:/usr/bin")
        self.assertEqual(self.desk.settings()["lanes"]["a"].get("env", {}), {})

    def test_what_a_lanes_env_names_is_closed_to_workers(self):
        from hansa import run
        home = (self.tmp / "home").resolve()
        own = home / "hansa-cache" / "a"
        own.mkdir(parents=True)
        with as_home(home):
            # any variable, also one a lane may not set (saved earlier): the named folder, inside it, above it
            for name, value, at in (("LD_LIBRARY_PATH", f"/usr/lib:{own / 'lib'}", own / "lib"),
                                    ("NODE_PATH", f"{own / 'node'}", own / "node"),
                                    ("GIT_CONFIG_GLOBAL", f"{own / 'gitconfig'}", own / "gitconfig"),
                                    ("FOO_DIR", f"{own / 'foo'}", own / "foo"),
                                    ("FOO_OPTS", f"-x --dir={own / 'foo'},b -I {own / 'inc'}", own / "inc"),
                                    ("FOO_HOME", "~/hansa-cache/a/foo", own / "foo")):
                for x in (at, at / "sub" / "x", own):
                    s = self.desk.settings()
                    s["lanes"]["a"].update(env={name: value}, write=[str(x)])
                    told = [t for t in validate(s, self.desk.root) if "write cannot include" in t]
                    self.assertTrue(len(told) == 1 and f"write cannot include {x}: it would open {own}" in told[0],
                                    (name, x, told))
                    write_json(self.desk.settings_path, s)  # saved earlier
                    good, left = run.write_list(self.desk, s["lanes"]["a"])
                    self.assertEqual((good, len(left)), ([], 1), (name, x, left))
                self.assertEqual([t for t in self.lane_problems({name: value}, [own / "elsewhere"]) if "write" in t], [])
            told = self.lane_problems({"FOO_DIR": str(own / "foo")}, [own / "foo"])
            self.assertIn(f"it would open {own / 'foo'}, which the lane's env names in FOO_DIR", told[0])
            # a cache variable points a tool at the lane's own folder, which is on the write list
            caches = {"npm_config_cache": own / "npm", "npm_config_store_dir": own / "pnpm", "PIP_CACHE_DIR": own / "pip",
                      "YARN_CACHE_FOLDER": own / "yarn", "CARGO_HOME": own / "cargo", "GOMODCACHE": own / "go" / "mod",
                      "GOCACHE": own / "go" / "build", "GRADLE_USER_HOME": own / "gradle", "CCACHE_DIR": own / "ccache"}
            env = {**{k: str(v) for k, v in caches.items()}, "MAVEN_OPTS": f"-Dmaven.repo.local={own / 'm2'}"}
            self.assertEqual(self.lane_problems(env, [*caches.values(), own / "m2"]), [])
            self.hansa("lane", "set", "a", "env", *[f"{k}={v}" for k, v in env.items()])
            self.hansa("lane", "set", "a", "write", *map(str, caches.values()))
            self.assertEqual(run.write_list(self.desk, self.desk.settings()["lanes"]["a"]), ([str(v) for v in caches.values()], []))
            self.assertEqual(run.lane_env(self.desk, self.desk.settings()["lanes"]["a"]), (env, []))

    def test_the_xdg_folders_of_a_lane_may_be_listed_but_not_claude_codes_own_files_in_them(self):
        tools = (self.tmp / "srv" / "tools").resolve()
        tools.mkdir(parents=True)
        env = {"CI": "1", "PATH": f"{tools / 'node' / 'bin'}:/usr/local/bin:/usr/bin:/bin",
               **{f"XDG_{n.upper()}_HOME": str(tools / f"xdg-{n}") for n in ("data", "cache", "config", "state")}}
        fine = [tools / "xdg-data" / "pnpm", tools / "xdg-cache" / "pnpm", tools / "xdg-state" / "pnpm"]
        with as_home((self.tmp / "home").resolve()):
            self.assertEqual(self.lane_problems(env, fine), [])
            self.assertEqual(self.lane_problems({"CPLUS_INCLUDE_PATH": "/srv/x/deps/src/lib/include"}), [])
            self.assertEqual(self.lane_problems({}), [])
            for x in (tools / "xdg-data" / "claude", tools / "xdg-data" / "claude" / "versions", tools / "xdg-data",
                      tools / "xdg-cache" / "claude-cli-nodejs", tools / "xdg-state" / "claude"):
                told = self.lane_problems(env, [x])
                self.assertTrue(len(told) == 1 and "Claude Code's own files" in told[0], (x, told))
            for x in (tools / "xdg-config" / "pnpm", tools / "xdg-config", tools / "node" / "bin" / "node", tools):
                self.assertEqual(len(self.lane_problems(env, [x])), 1, x)  # XDG_CONFIG_HOME and the PATH stay closed

    def test_a_lanes_env_holds_no_name_that_tells_a_loader_git_a_shell_or_claude_code_what_to_run(self):
        from hansa import run
        names = ["LD_PRELOAD", "LD_LIBRARY_PATH", "DYLD_INSERT_LIBRARIES", "DYLD_LIBRARY_PATH", "GIT_CONFIG_GLOBAL",
                 "GIT_EXEC_PATH", "GIT_SSH_COMMAND", "GIT_DIR", "CLAUDE_CODE_SHELL", "CLAUDE_CODE_GIT_BASH_PATH",
                 "NODE_OPTIONS", "BASH_ENV", "ENV", "SHELL", "ZDOTDIR", "HOME"]
        for name in names:
            told = self.lane_problems({name: "x", "CI": "1"})
            self.assertTrue(len(told) == 1 and told[0].startswith(f"lane a: env {name} cannot be set for a lane: it tells "
                                                                  "a loader, git, a shell or Claude Code what to run"), told)
            self.assertIn("set it inside the check or setup command instead, where it runs in the sandbox", told[0])
            with self.assertRaises(SystemExit):
                self.hansa("lane", "set", "a", "env", f"{name}=x")
        fine = {"CI": "1", "NODE_ENV": "test", "LDFLAGS": "-L/x", "GITHUB_ACTIONS": "1", "git_dir": "x", "ENVIRONMENT": "ci",
                "HOMEBREW_NO_AUTO_UPDATE": "1", "MY_SHELL": "x", "CPLUS_INCLUDE_PATH": "/srv/x/deps/src/lib/include"}
        self.assertEqual(self.lane_problems(fine), [])
        # saved earlier: each is left out at a run's start, with a line, and the others are passed on
        s = self.desk.settings()
        s["lanes"]["a"]["env"] = {**{n: "/x" for n in names}, "PATH": "bin:/usr/bin", "HANSA_TASK": "t1", **fine}
        write_json(self.desk.settings_path, s)
        good, left = run.lane_env(self.desk, s["lanes"]["a"])
        self.assertEqual(good, fine)
        self.assertEqual(len(left), len(names) + 2)
        self.assertTrue(left[0].startswith("left out of this run: LD_PRELOAD cannot be set for a lane") and
                        left[0].endswith("(the env is changed with hansa lane set)"), left[0])
        seen, files = {}, []
        real = subprocess.Popen

        def popen(argv, *args, **kw):
            seen.update(kw.get("env") or {})
            if (kw.get("env") or {}).get("CLAUDE_ENV_FILE"):  # read now: the run's folder is gone when it ends
                files.append(Path(kw["env"]["CLAUDE_ENV_FILE"]).read_text())
            return real(argv, *args, **kw)

        tid = self.desk.new_task("Add hello.txt")
        with unittest.mock.patch("subprocess.Popen", popen):
            m = self.settle(Dispatcher(self.desk), tid)
        self.assertEqual(m["state"], "done", m)
        self.assertNotIn("NODE_ENV", seen)  # the lane's env is not Claude Code's: its commands get it from the file
        self.assertIn("export NODE_ENV=test\n", files[0])
        self.assertFalse([n for n in (*names, "PATH", "HANSA_TASK") if f"export {n}=" in files[0]], files[0])
        self.assertNotEqual(seen.get("GIT_CONFIG_GLOBAL"), "/x")
        self.assertFalse([n for n in names if seen.get(n) == "/x"])
        self.assertNotEqual(seen.get("PATH"), "bin:/usr/bin")
        self.assertEqual(seen.get("HANSA_TASK"), tid)
        self.assertEqual(m["env_left_out"], left)
        self.assertIn("env: left out of this run: LD_PRELOAD cannot be set for a lane", views.show_text(self.desk, tid))
        self.assertEqual([e["task"] for e in self.events("env_left_out")], [tid] * len(left))
        lines = views.explain_text(self.desk, "lane", "a").splitlines()
        at = lines.index("  environment set for workers: " + ", ".join(f"{k}={v}" for k, v in fine.items()))
        self.assertTrue(lines[at + 1].startswith("    a run leaves out LD_PRELOAD cannot be set for a lane"), lines[at + 1])

    def test_a_lanes_write_list_opens_nothing_in_the_folder_hansa_is_installed_in(self):
        top = self.tmp.resolve() / "inst"  # an install: the package and bin/hansa, which the coordinator runs
        here = Path(views.__file__).resolve().parent
        shutil.copytree(here, top / "hansa", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(here.parent / "bin", top / "bin")
        code = ("import importlib.util, json, sys\n"  # loaded by its place, so not through a folder python loads from
                "spec = importlib.util.spec_from_file_location('hansa', sys.argv[1] + '/hansa/__init__.py',"
                " submodule_search_locations=[sys.argv[1] + '/hansa'])\n"
                "sys.modules['hansa'] = m = importlib.util.module_from_spec(spec)\n"
                "spec.loader.exec_module(m)\n"
                "from hansa.desk import write_problems\n"
                "print(json.dumps([write_problems([x]) for x in sys.argv[2:]]))")
        asked = [top / "bin", top / "bin" / "hansa", top / "cache", top, top / "hansa" / "x", self.tmp.resolve() / "cache"]
        (self.tmp / "cwd").mkdir()
        r = subprocess.run([sys.executable, "-c", code, str(top), *map(str, asked)], capture_output=True, text=True,
                           cwd=str(self.tmp / "cwd"), env={k: v for k, v in os.environ.items() if k != "PYTHONPATH"})
        self.assertEqual(r.returncode, 0, r.stderr)
        told = json.loads(r.stdout)
        self.assertEqual(len(told[3]), 1)
        for x, said in zip(asked[:3], told):
            self.assertTrue(len(said) == 1 and f"write cannot include {x}: it would open {top}, the folder hansa is "
                            "installed in (its code and bin/hansa" in said[0], (x, said))
        self.assertIn("hansa's own code", told[4][0])
        self.assertEqual(told[5], [])

    def test_more_tool_homes_are_refused_and_explain_says_a_malformed_write_list_in_one_line(self):
        home = (self.tmp / "home").resolve()
        home.mkdir()
        with as_home(home):
            for top, what in (("miniconda", "a conda install"), ("anaconda", "a conda install"),
                              ("micromamba", "a conda install"), ("sdk", "where Go keeps the Go versions it downloads")):
                self.assertIn(what, self.refused_and_left_out(home / top / "bin", f"it would open {home / top}, "))
            self.assertEqual(write_problems([str(home / "sdks"), str(home / "work" / "sdk")]), [])
            for bad in ([str(home / "work"), 7], "/tmp/cache", [["/tmp/cache"]], ["cache"]):
                s = self.desk.settings()
                s["lanes"]["a"]["write"] = bad  # only by editing the settings by hand
                write_json(self.desk.settings_path, s)
                lines = views.explain_text(self.desk, "lane", "a").splitlines()
                at = lines.index("  folders a worker may also write (a build or test tool's cache): none")
                self.assertEqual(lines[at + 1], "    a run leaves out the whole write list: it must be a list of full "
                                                "paths starting with / (the write list is changed with hansa lane set)")
                self.assertFalse(lines[at + 2].startswith("    a run leaves out"), lines[at + 2])

    def test_a_lanes_write_list_opens_nothing_inside_a_folder_on_the_path(self):
        home, bin_ = (self.tmp / "home").resolve(), (self.tmp / "home").resolve() / "bin"
        bin_.mkdir(parents=True)
        tools = (self.tmp / "srv" / "tools").resolve()
        (tools / "bin").mkdir(parents=True)
        (self.tmp / "lbin").symlink_to(tools / "bin")
        with as_home(home, PATH=f"{bin_}:{tools / 'bin'}:/usr/bin:/bin"):
            for x, d in {bin_ / "git": bin_, bin_ / "sub" / "x": bin_, tools / "bin" / "git": tools / "bin",
                         tools / "bin" / "new" / "tool": tools / "bin",
                         self.tmp.resolve() / "lbin" / "git": tools / "bin"}.items():  # a git hansa would run
                told = self.refused_and_left_out(x, f"it is inside {d}, a folder on the PATH hansa runs with")
                self.assertIn("a program there runs outside the sandbox (a cache folder goes elsewhere", told)
            for x, d in {bin_: bin_, tools / "bin": tools / "bin", tools: tools / "bin"}.items():  # the folder, and above
                self.refused_and_left_out(x, f"it would open {d}, a folder on the PATH hansa runs with")
            self.assertEqual(len(write_problems(["/bin/sh"])), 1)
            fine = [str(tools / "xdg-cache" / "pnpm"), str(tools / "cache"), str(home / "work" / "cache"), "/tmp/cache"]
            self.assertEqual(write_problems(fine, str(self.repo), self.desk.root, self.desk.settings()["providers"]), [])
        with as_home(home, PATH="/:/usr/bin"):  # a PATH that names the top closes no more than the top
            self.assertEqual(write_problems(["/tmp/cache", str(home / "work" / "cache")]), [])
            self.assertEqual(len(write_problems(["/"])), 1)

    def test_a_lanes_write_list_opens_no_place_programs_are_installed(self):
        home = (self.tmp / "home").resolve()
        home.mkdir()
        prefix = (self.tmp / "brewp").resolve()  # Homebrew somewhere of its own: brew on the PATH says where
        (prefix / "Homebrew" / "bin").mkdir(parents=True)
        (prefix / "bin").mkdir()
        (prefix / "Homebrew" / "bin" / "brew").write_text("#!/bin/sh\n")
        (prefix / "Homebrew" / "bin" / "brew").chmod(0o755)
        (prefix / "bin" / "brew").symlink_to("../Homebrew/bin/brew")
        (self.tmp / "lshare").symlink_to(prefix / "share")
        where = "where programs are installed: what is there runs outside the sandbox"
        bad = {prefix / "Library" / "Homebrew": prefix, prefix / "share" / "zsh" / "site-functions": prefix,
               prefix / "var" / "cache": prefix, prefix / "Homebrew": prefix, self.tmp.resolve() / "lshare" / "zsh": prefix,
               "/opt/homebrew/Library/Homebrew": "/opt/homebrew", "/opt/homebrew/share/zsh/site-functions": "/opt/homebrew",
               "/opt/homebrew/Caskroom": "/opt/homebrew", "/home/linuxbrew/.linuxbrew/share": "/home/linuxbrew/.linuxbrew",
               "/Applications/Xcode.app/Contents/Developer/usr/bin/git": "/Applications",
               "/Applications/Xcode.app/Contents/Developer/Library/Frameworks/Python3.framework/Versions/3.9/Resources":
                   "/Applications",
               "/Library/Developer/CommandLineTools/usr/bin": "/Library", "/usr/local/lib": "/usr",
               "/usr/local/share/zsh/site-functions": "/usr", "/usr/local/Cellar": "/usr", "/usr/share/x": "/usr",
               "/nix/store": "/nix", "/snap/node": "/snap", "/var/lib/x": "/var/lib",
               "/opt/local": "/opt/local", "/opt/local/var/macports/build": "/opt/local", "/opt/local/bin/git": "/opt/local",
               "/opt/X11": "/opt/X11", "/opt/X11/lib": "/opt/X11", "/opt/X11/share/cache": "/opt/X11"}
        with as_home(home, PATH=f"{prefix / 'bin'}:/usr/bin:/bin"):
            for x, top in bad.items():
                if sys.platform == "darwin" and str(x).startswith("/home/"):  # there /home is the system's own
                    top = self.refused_and_left_out(x, "") and "/usr"
                told = self.refused_and_left_out(x, "it would open " if top != "/usr" else "")
                if top not in ("/Applications", "/Library", "/usr") or "/usr/local/share" in str(x):  # else a nearer fence may say it
                    self.assertIn(f"it would open {top}, {where}", told)
            for x in ("/usr/local/lib", "/usr/local/lib/x", "/usr/lib/x"):  # the system's own: says where a cache goes
                self.assertIn("(a cache folder goes elsewhere, such as a tools folder of the desk's, another volume or "
                              "/tmp)", self.refused_and_left_out(x, "the system's own settings, libraries or programs"))
            fine = ["/srv/x/tools/xdg-cache/pnpm", "/tmp/cache", "/opt/cache", "/var/cache/x", "/mnt/big/cache",
                    str(self.tmp.resolve() / "elsewhere" / "cache"), str(home / "work" / "cache")]
            self.assertEqual(write_problems(fine, str(self.repo), self.desk.root, self.desk.settings()["providers"]), [])
        # an account of the system's, whose home folder is where programs are installed too: its own rule holds there
        with as_home("/var/lib/builder"):
            self.assertEqual(write_problems(["/var/lib/builder/work/cache", "/var/lib/builder/hansa-cache/a/pip"]), [])
            for x in ("/var/lib/builder/.zsh", "/var/lib/builder/.cache/pip", "/var/lib/other", "/var/lib/builder", "/var/lib"):
                self.assertEqual(len(write_problems([x])), 1, x)

    def test_a_name_that_normalizes_to_a_pattern_character_is_in_neither_list(self):
        good = (self.tmp / "opt").resolve()
        good.mkdir()
        why = "has one of ()[]{}*?\\ or a look-alike of one"
        for c in ("\uff3c", "\ufe68", "\uff0a", "\uff08", "\ufe5b", "\uff1f"):
            there = good / f"a{c}b"
            there.mkdir()
            (self.tmp / "lodd").symlink_to(there)
            for path in (str(there), str(good / "new" / f"{c}x"), str(self.tmp.resolve() / "lodd")):  # where a link leads
                told = self.page_save(lambda s: s.update(coordinator_read=[str(good), path]))
                self.assertTrue(not told["ok"] and any(why in p for p in told["problems"]), (ascii(path), told))
                self.refused_and_left_out(path, why)
            (self.tmp / "lodd").unlink()
        self.assertEqual(write_problems([str(good / "caf\u00e9"), str(good / "\u30ad\u30e3\u30c3\u30b7\u30e5")]), [])

    def test_the_home_folder_is_the_accounts_whatever_home_says(self):
        from hansa import run
        from hansa.desk import read_split
        home, other = (self.tmp / "home").resolve(), (self.tmp / "elsewhere").resolve()
        for d in (home / ".ssh", other / ".ssh", home / "Library" / "Keychains"):
            d.mkdir(parents=True)
        p = self.desk.settings()["providers"]["claude"]
        with as_home(home), unittest.mock.patch.dict(os.environ, {"HOME": str(other)}):
            self.assertEqual(os.path.expanduser("~"), str(other))
            self.refused_and_left_out(home, f"it would open {home}, the home folder of the user hansa runs as")
            self.refused_and_left_out(home / ".ssh", f"it would open {home / '.ssh'}, a login or key")
            self.refused_and_left_out(home / ".zshrc", "a shell's startup file")
            if sys.platform == "darwin":
                self.refused_and_left_out(home / "Library" / "Keychains", f"it would open {home / 'Library'}")
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as e:
                main(["--desk", str(self.desk.root), "lane", "set", "a", "write", str(home / ".ssh")])
            self.assertIn("a login or key", str(e.exception.code))
            told = self.page_save(lambda s: s.update(coordinator_read=[str(home)]))  # the coordinator's read list
            self.assertTrue(not told["ok"] and "the home folder of the user hansa runs as" in told["problems"][0], told)
            self.assertIn("a login or key", read_split([str(home / ".ssh")])[1][0])
            if sys.platform == "darwin":
                self.assertIn(f"it would open {home / 'Library'}", read_split([str(home / "Library" / "Keychains")])[1][0])
            self.assertEqual(read_split([str(other / ".ssh")]), ([str(other / ".ssh")], []))  # not the account's
            for fs in (run.sandbox_settings(self.desk, {"network": []}, "claude", p, self.repo)["sandbox"]["filesystem"],
                       run.srt_settings(self.desk, {"network": []}, "claude", p, self.repo, str(self.tmp / "t"))["filesystem"]):
                self.assertIn(str(home / ".ssh"), fs["denyRead"])  # the sandbox's denials too
                self.assertIn(str(home / ".npmrc"), fs["denyRead"])
        # a hansa started as a command with HOME set elsewhere: the account's logins stay closed
        mine = Path(pwd.getpwuid(os.getuid()).pw_dir)
        before = self.desk.settings_path.read_text()
        r = subprocess.run([str(Path(__file__).resolve().parent.parent / "bin" / "hansa"), "--desk", str(self.desk.root),
                            "lane", "set", "a", "write", str(mine / ".ssh")], capture_output=True, text=True,
                           env={**os.environ, "HOME": str(other)}, timeout=120)
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertIn(f"write cannot include {mine / '.ssh'}: it would open", r.stderr)
        self.assertEqual(self.desk.settings_path.read_text(), before)

    def test_a_name_with_a_backslash_or_a_character_that_shows_as_nothing_is_in_neither_list(self):
        good = (self.tmp / "opt").resolve()
        (good / "a b").mkdir(parents=True)  # the plain space is a name like another
        self.assertTrue(self.page_save(lambda s: s.update(coordinator_read=[str(good / "a b")]))["ok"])
        self.assertEqual(write_problems([str(good / "a b")]), [])
        unseen = "invisible or direction-changing character"
        odd = {"\\": "has one of ()[]{}*?\\", "\u00a0": unseen, "\u2003": unseen, "\u202f": unseen, "\u3000": unseen,
               "\u1680": unseen, "\u115f": unseen, "\u1160": unseen, "\u3164": unseen, "\uffa0": unseen, "\u2800": unseen}
        for c, why in odd.items():
            there = good / f"a{c}b"
            there.mkdir()
            (self.tmp / "lodd").symlink_to(there)
            for path in (str(there), str(good / "new" / f"{c}x"), str(self.tmp.resolve() / "lodd")):  # where a link leads
                told = self.page_save(lambda s: s.update(coordinator_read=[str(good), path]))
                self.assertTrue(not told["ok"] and any(why in p for p in told["problems"]), (ascii(path), told))
                self.refused_and_left_out(path, why)
            s = self.desk.settings()
            s["lanes"]["a"]["write"] = []
            s["coordinator_read"] = [str(there), str(good)]  # written into settings.json another way
            write_json(self.desk.settings_path, s)
            r = self.echo_coordinator()
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn(f"--add-dir {self.repo.resolve()} {good} --disallowedTools", r.stdout)
            self.assertTrue(any(why in x and "skipped" in x for x in r.stderr.splitlines()), (ascii(c), r.stderr))
            (self.tmp / "lodd").unlink()

    def test_a_worker_on_a_key_provider_cannot_read_another_providers_claude_code_folder(self):
        seen, cc = self.tmp / "argv.jsonl", self.tmp / "logins" / "cc"
        cc.mkdir(parents=True)
        s = self.desk.settings()
        s["providers"]["claude"]["config_dir"] = str(cc)  # the subscription's login and session records
        s["providers"]["mimo"] = {"kind": "compatible", "model": "mimo-x", "effort": "", "base_url": "https://example.invalid"}
        s["lanes"]["a"].update(providers=["mimo"], setup="true", check="true", env={"FAKE_SHOW_ARGV": str(seen)})
        self.assertEqual(self.desk.save_settings(s), [])
        self.desk.write_key("mimo", "sk-test-other-company-01")
        tid = self.desk.new_task("Add hello.txt")
        m = self.settle(Dispatcher(self.desk), tid)
        self.assertEqual((m["state"], m["provider"]), ("done", "mimo"), m)
        worker = self.sandbox_of(json.loads(seen.read_text().splitlines()[-1]))["filesystem"]["denyRead"]
        setup, check = [c["settings"]["filesystem"]["denyRead"] for c in self.srt_calls()]
        for deny in (worker, setup, check):
            self.assertIn(str(cc), deny)  # not the provider this run uses
            self.assertIn(str(Path.home() / ".claude"), deny)  # the machine owner's, whatever the providers use
            self.assertIn(str(self.desk.state_dir / "claude-mimo"), deny)  # its own, as before

    def test_more_logins_and_keys_are_kept_from_workers_and_the_macos_ones_only_on_macos(self):
        from hansa import run
        from hansa.desk import read_split
        home, p = Path.home(), self.desk.settings()["providers"]["claude"]
        everywhere = (".ssh", ".tsh", ".tbot", ".codex", ".gemini", ".cursor", ".grok", ".config/gcloud", ".azure", ".oci",
                      ".git-credentials", ".config/git/credentials", ".cargo/credentials", ".cargo/credentials.toml",
                      ".gem/credentials", ".config/hub", ".config/glab-cli", ".terraform.d", ".vault-token",
                      ".password-store", ".local/share/keyrings", ".config/op", ".mozilla", ".config/google-chrome",
                      ".config/chromium", ".bash_history", ".zsh_history", ".python_history", ".psql_history")
        mac = ("Library/Keychains", "Library/Cookies", "Library/Application Support/Claude",
               "Library/Application Support/Google/Chrome", "Library/Application Support/Chromium",
               "Library/Application Support/BraveSoftware", "Library/Application Support/Microsoft Edge",
               "Library/Application Support/Arc", "Library/Application Support/Firefox", "Library/Safari",
               "Library/Messages", "Library/Mail")
        for system in ("linux", "darwin"):
            with unittest.mock.patch.object(sys, "platform", system):
                lane = self.desk.settings()["lanes"]["a"]
                deny = run.lane_sandbox(self.desk, lane, "claude", p, self.repo)[1]["denyRead"]
                srt = run.srt_settings(self.desk, lane, "claude", p, self.repo, str(self.tmp / "t"))["filesystem"]["denyRead"]
            self.assertEqual(deny, srt)  # a setup's and a check's, as a worker's
            for f in everywhere:
                self.assertIn(str(home / f), deny, (system, f))
            for f in mac:
                (self.assertIn if system == "darwin" else self.assertNotIn)(str(home / f), deny, (system, f))
            # nothing a build or test tool reads
            for f in (".gitconfig", ".m2", ".gradle", ".cargo", ".cargo/config.toml", ".npm", ".cache", ".config", "Library"):
                self.assertNotIn(str(home / f), deny, (system, f))
        # the write list and the coordinator's read list refuse the new names, as they did the old
        for f in (".tsh", ".cargo/credentials.toml", ".cargo", ".config/gcloud/x"):
            self.assertIn("a login or key that workers may not read", write_problems([str(home / f)])[0], f)
            self.assertIn("a login or key", read_split([str(home / f)])[1][0], f)
        self.assertEqual(write_problems([str(home / "hansa-cache" / "a" / "cargo"), str(home / "work" / "m2")]), [])
        # the coordinator's Read tool is denied them too, but no Claude Code folder: its memory lives in one
        r = self.echo_coordinator()
        self.assertEqual(r.returncode, 0, r.stderr)
        denied = r.stdout.split("--disallowedTools")[1].split("--model")[0]
        for f in (".ssh", ".tsh", ".config/gcloud"):
            self.assertIn(f"Read(//{str(home / f).lstrip('/')}/**)", denied)
        self.assertIn(f"Read(//{str(home / '.git-credentials').lstrip('/')})", denied)  # a file, by its own path
        (self.assertIn if sys.platform == "darwin" else self.assertNotIn)(
            f"Read(//{str(home / 'Library/Keychains').lstrip('/')}/**)", denied)
        self.assertNotIn(f"Read(//{str(home / '.claude').lstrip('/')}/**)", denied)
        told = self.hansa("explain", "lane", "a")
        self.assertIn("may not read", told)
        self.assertIn("~/.tsh", told)

    def test_anthropics_tool_settings_and_proxy_clients_settings_are_kept_from_workers(self):
        from hansa import run
        home, p = Path.home(), self.desk.settings()["providers"]["claude"]
        everywhere = (".config/anthropic", ".config/clash", ".config/clash.meta", ".config/mihomo", ".config/clash-verge",
                      ".config/io.github.clash-verge-rev.clash-verge-rev", ".config/sing-box", ".config/v2ray",
                      ".config/xray", ".config/nekoray")
        mac = ("Library/Application Support/io.github.clash-verge-rev.clash-verge-rev",
               "Library/Application Support/com.nssurge.surge-mac", "Library/Application Support/clash-verge")
        for system in ("linux", "darwin"):
            with unittest.mock.patch.object(sys, "platform", system):
                lane = self.desk.settings()["lanes"]["a"]
                deny = run.lane_sandbox(self.desk, lane, "claude", p, self.repo)[1]["denyRead"]
                srt = run.srt_settings(self.desk, lane, "claude", p, self.repo, str(self.tmp / "t"))["filesystem"]["denyRead"]
            self.assertEqual(deny, srt)  # a setup's and a check's, as a worker's
            for f in everywhere:
                self.assertIn(str(home / f), deny, (system, f))
            for f in mac:
                (self.assertIn if system == "darwin" else self.assertNotIn)(str(home / f), deny, (system, f))
            self.assertNotIn(str(home / ".config"), deny, system)  # not the whole folder: build tools read there
        # the write list refuses them, and the coordinator's Read tool is denied them (the macOS ones on macOS)
        for f in everywhere:
            self.assertIn("a login or key that workers may not read", write_problems([str(home / f)])[0], f)
        r = self.echo_coordinator()
        self.assertEqual(r.returncode, 0, r.stderr)
        denied = r.stdout.split("--disallowedTools")[1].split("--model")[0]
        for f in everywhere + (mac if sys.platform == "darwin" else ()):
            self.assertIn(f"Read(//{str(home / f).lstrip('/')}/**)", denied, f)
        told = self.hansa("explain", "lane", "a")
        for f in everywhere:
            self.assertIn(f"~/{f}", told)

    def test_what_the_principal_keeps_from_workers_reaches_every_sandbox_and_every_read_tool(self):
        from hansa import run
        seen = self.tmp / "argv.jsonl"
        keys, token = (self.tmp / "my project" / "keys").resolve(), self.tmp.resolve() / "token.txt"  # one not there yet
        keys.mkdir(parents=True)
        self.assertEqual(self.desk.settings()["workers_may_not_read"], [])
        told = self.page_save(lambda s: s.update(workers_may_not_read=[str(keys), str(token), str(keys) + "/"]))
        self.assertEqual(told, {"ok": True, "problems": []})
        shown = public_settings(self.desk)
        self.assertEqual(shown["settings"]["workers_may_not_read"], [str(keys), str(token), str(keys) + "/"])
        self.assertEqual(shown["deny_skipped"], [])
        ui = (Path(__file__).resolve().parent.parent / "hansa" / "ui.html").read_text()
        for x in ("S.workers_may_not_read", "T.workersMayNotRead", "DENY_SKIPPED = r.deny_skipped"):
            self.assertIn(x, ui)
        self.assertEqual(ui.count("workersMayNotRead:"), 2)  # said in both languages
        rules = [f"Read(//{str(keys).lstrip('/')})", f"Read(//{str(keys).lstrip('/')}/**)",
                 f"Read(//{str(token).lstrip('/')})", f"Read(//{str(token).lstrip('/')}/**)"]
        # the coordinator cannot set it, and its own changes leave it as the principal set it
        self.hansa("lane", "set", "a", "setup", "true")
        self.hansa("lane", "set", "a", "check", "true")
        s = self.desk.settings()
        s["lanes"]["a"]["env"] = {"FAKE_SHOW_ARGV": str(seen)}
        self.assertEqual(self.desk.save_settings(s), [])
        self.assertEqual(len(self.desk.settings()["workers_may_not_read"]), 3)
        # a worker and a verifier: the sandbox of their commands, and their Read tool
        tid = self.desk.new_task("Add hello.txt")
        m = self.settle(Dispatcher(self.desk), tid)
        self.assertEqual(m["state"], "done", m)
        self.assertEqual(m["no_read_left_out"], [])
        with contextlib.redirect_stdout(io.StringIO()):
            main(["--desk", str(self.desk.root), "verify", tid])
        self.settle(Dispatcher(self.desk), self.desk.meta(tid)["verify"])
        task, verify = [json.loads(x) for x in seen.read_text().splitlines()[-2:]]
        self.assertIn("checking another worker's finished task", verify[verify.index("--append-system-prompt") + 1])
        for argv in (task, verify):
            deny = self.sandbox_of(argv)["filesystem"]["denyRead"]
            self.assertEqual((deny.count(str(keys)), deny.count(str(token))), (1, 1))
            for r in rules:
                self.assertIn(r, self.denied_by(argv))
        # the setup and the check of each (srt's settings)
        calls = self.srt_calls()
        self.assertEqual(len(calls), 3)  # the task's setup and check, the verification's setup
        for c in calls:
            self.assertIn(str(keys), c["settings"]["filesystem"]["denyRead"])
            self.assertIn(str(token), c["settings"]["filesystem"]["denyRead"])
        # a design session
        self.desk.update(tid, lane="a", provider="claude", session_id="sid-d", model_asked="sonnet")
        argv, _, _ = run.design_command(self.desk, tid)
        self.addCleanup(run.remove_sandbox_tmp, self.desk, tid)
        design = argv[argv.index("--disallowedTools") + 1:argv.index("--allowedTools")]
        box = json.loads(Path(argv[argv.index("hansa-setup") + 7]).read_text())  # its setup's sandbox (srt's settings)
        self.assertIn(str(keys), box["filesystem"]["denyRead"])
        for r in rules:
            self.assertIn(r, design)
        # the coordinator
        r = self.echo_coordinator()
        self.assertEqual(r.returncode, 0, r.stderr)
        for x in rules:
            self.assertIn(x, r.stdout.split("--disallowedTools")[1].split("--model")[0])
        # where the coordinator looks
        told = self.hansa("explain", "lane", "a")
        self.assertIn(f"folders and files the principal keeps from workers", told)
        self.assertIn(f"{keys}, {token}", told)
        self.assertIn("you cannot set", told)
        # no write list, no folder the coordinator reads and no lane's repository is one, inside one or above one
        for x in (keys, keys / "sub", keys.parent, token):
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as e:
                main(["--desk", str(self.desk.root), "lane", "set", "a", "write", str(x)])
            self.assertIn("which the principal keeps workers from reading", str(e.exception.code), x)
        self.assertEqual(self.desk.settings()["lanes"]["a"].get("write", []), [])
        (keys / "sub").mkdir()
        for x in (keys, keys / "sub", keys.parent):
            told = self.page_save(lambda s: s.update(coordinator_read=[str(x)]))
            self.assertFalse(told["ok"], x)
            self.assertIn("which is on the list of what workers may not read", told["problems"][0])
        s = self.desk.settings()
        s["lanes"]["a"]["repo"] = str(keys / "sub")
        self.assertTrue(any("repo cannot be" in x and "the principal keeps workers from reading" in x
                            for x in validate(s, self.desk.root)))
        # settings saved before there was such a list work as before
        s = self.desk.settings()
        del s["workers_may_not_read"]
        write_json(self.desk.settings_path, s)
        self.assertEqual(validate(json.loads(self.desk.settings_path.read_text()), self.desk.root), [])
        self.assertEqual(self.desk.settings()["workers_may_not_read"], [])
        tid = self.desk.new_task("Add more.txt")
        self.assertEqual(self.settle(Dispatcher(self.desk), tid)["state"], "done")
        argv = json.loads(seen.read_text().splitlines()[-1])
        self.assertNotIn(str(keys), self.sandbox_of(argv)["filesystem"]["denyRead"])
        self.assertIn(str(self.desk.secrets_dir), self.sandbox_of(argv)["filesystem"]["denyRead"])
        self.assertTrue(self.page_save(lambda s: s.update(cool_minutes=31))["ok"])

    def test_what_workers_cannot_be_kept_from_reading_is_refused_on_saving_and_left_out_at_start(self):
        home, tools = Path(os.path.expanduser("~")), self.tmp.resolve() / "tools"
        (tools / "bin").mkdir(parents=True)
        seen, good, tmp = self.tmp / "argv.jsonl", self.tmp.resolve() / "keys", self.tmp.resolve()
        link = self.tmp / "link-to-repo"
        link.symlink_to(self.repo)
        s = self.desk.settings()
        s["lanes"]["a"]["env"] = {"FAKE_SHOW_ARGV": str(seen)}
        self.assertEqual(self.desk.save_settings(s), [])
        bad = {"relative/path": "must be a full path", "/": "the whole machine",
               str(home): "the home folder", str(home.parent): "the home folder",
               str(self.desk.root): "the desk, or a folder above it", str(tmp): "the desk, or a folder above it",
               str(self.repo.resolve()): "lane a's repository", str(link): "lane a's repository",  # where a link leads
               str(tools / "bin"): "a folder on the PATH", str(tools): "a folder on the PATH",
               f"{tmp}/a\nb": "control character", f"{tmp}/be\x07ll": "control character",
               f"{tmp}/a​b": "invisible or direction-changing", f"{tmp}/star*": "read as a pattern",
               f"{tmp}/(x)": "read as a pattern"}
        with unittest.mock.patch.dict(os.environ, {"PATH": f"{tools / 'bin'}{os.pathsep}{os.environ['PATH']}"}):
            for path, why in bad.items():
                told = self.page_save(lambda s: s.update(workers_may_not_read=[str(good), path]))
                self.assertFalse(told["ok"], path)
                self.assertEqual(len(told["problems"]), 1, (path, told))
                self.assertIn(why, told["problems"][0], path)
                self.assertIn("workers cannot be kept from reading", told["problems"][0])
                self.assertEqual(self.desk.settings()["workers_may_not_read"], [])  # nothing was saved
            self.assertFalse(self.page_save(lambda s: s.update(workers_may_not_read="/opt/keys"))["ok"])
            self.assertFalse(self.page_save(lambda s: s.update(workers_may_not_read=[["/opt/keys"]]))["ok"])
            self.assertFalse(self.page_save(lambda s: s.update(workers_may_not_read=["/k"] * 51))["ok"])
            # inside a PATH folder, the desk or the repository, and one that is not there: accepted
            fine = [str(good), str(tools / "bin" / "sub"), str(self.repo.resolve() / "private"), "/nowhere/at/all"]
            self.assertEqual(self.page_save(lambda s: s.update(workers_may_not_read=fine)), {"ok": True, "problems": []})
            # written into settings.json another way: a run leaves each out and says so in the task
            s = self.desk.settings()
            s["workers_may_not_read"] = [*bad, str(good)]
            write_json(self.desk.settings_path, s)
            self.assertEqual(len(public_settings(self.desk)["deny_skipped"]), len(bad))  # the page says which
            tid = self.desk.new_task("Add hello.txt")
            m = self.settle(Dispatcher(self.desk), tid)
            self.assertEqual(m["state"], "done", m)
            argv = json.loads(seen.read_text().splitlines()[-1])
            deny = self.sandbox_of(argv)["filesystem"]["denyRead"]
            self.assertIn(str(good), deny)
            self.assertFalse(set(bad) & set(deny), deny)
            self.assertEqual([r for r in self.denied_by(argv) if r.startswith("Read(") and "keys" not in r],
                             [f"Read(//{str(d).lstrip('/')}/**)" for d in (self.desk.secrets_dir, self.desk.state_dir)])
            self.assertEqual(len(m["no_read_left_out"]), len(bad))
            shown = views.show_text(self.desk, tid)
            for path, why in bad.items():
                lines = [x for x in shown.splitlines() if why in x and x.startswith(tuple(
                    f"what workers may not read: left out of this run: {p}: " for p in (path, json.dumps(path))))]
                self.assertEqual(len(lines), 1, (path, shown))
            self.assertEqual([e["task"] for e in self.events("no_read_left_out")], [tid] * len(bad))
            told = self.hansa("explain", "lane", "a")
            self.assertIn(f"not applied: {home}: it is {home}, the home folder", told)
            # the coordinator's start leaves them out too, and says so
            r = self.echo_coordinator()
            self.assertEqual(r.returncode, 0, r.stderr)
            denied = r.stdout.split("--disallowedTools")[1].split("--model")[0]
            self.assertIn(f"Read(//{str(good).lstrip('/')}/**)", denied)
            self.assertNotIn(f"Read(//{str(home).lstrip('/')}/**)", denied)
            self.assertNotIn("Read(///**)", denied)
            self.assertIn(f"{home}: it is {home}, the home folder of the user hansa runs as, or a folder above it; skipped",
                          r.stderr)
            self.assertEqual(len(self.events("no_read_skipped")), len(bad))

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
        self.addCleanup(run.remove_sandbox_tmp, self.desk, tid)  # the run folder it made for the session
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

    def test_the_check_runs_to_its_end_and_what_the_worker_left_running_is_stopped_after_it(self):
        from hansa.run import alive
        pidfile = self.leave_detached()
        s = self.desk.settings()
        s["lanes"]["a"]["check"] = "sleep 1; test -f hello.txt"
        self.assertEqual(self.desk.save_settings(s), [])
        m = self.settle(Dispatcher(self.desk), self.desk.new_task("Add hello.txt"))
        self.assertEqual((m["state"], m["check"]["ok"]), ("done", True), m)
        self.assertEqual([x["pid"] for x in m["left_running"]], [int(pidfile.read_text())])
        self.assertFalse(alive(int(pidfile.read_text())))

    def test_a_check_that_fails_printing_nothing_says_nothing_came_out(self):
        s = self.desk.settings()
        s["lanes"]["a"]["check"] = "exit 127"
        self.assertEqual(self.desk.save_settings(s), [])
        m = self.settle(Dispatcher(self.desk), self.desk.new_task("Add hello.txt"))
        self.assertEqual((m["state"], m["check"]["ok"], m["check"]["tail"]), ("done", False, []), m)
        shown = views.show_text(self.desk, m["id"])
        self.assertIn("nothing came out: it printed nothing at all, which usually means the command could not start",
                      shown)
        self.assertIn(f"hansa merge {m['id']} --recheck", shown)

    def test_a_check_that_fails_silently_on_purpose_is_not_called_the_machines_problem(self):
        s = self.desk.settings()
        s["lanes"]["a"]["check"] = "exit 1"
        self.assertEqual(self.desk.save_settings(s), [])
        m = self.settle(Dispatcher(self.desk), self.desk.new_task("Add hello.txt"))
        self.assertEqual((m["state"], m["check"]["ok"], m["check"]["tail"]), ("done", False, []), m)
        shown = views.show_text(self.desk, m["id"])
        self.assertIn("usually means the command could not start", shown)
        self.assertIn("a check can also fail silently on purpose (a plain exit 1)", shown)
        self.assertNotIn("machine's problem", shown)
        self.assertIn(f"if it should have printed something, once that is fixed: hansa merge {m['id']} --recheck", shown)
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as e:
            main(["--desk", str(self.desk.root), "merge", m["id"]])
        said = str(e.exception.code)
        self.assertNotIn("machine's problem", said)
        self.assertIn(f"if it should have printed something, once that is fixed: hansa merge {m['id']} --recheck", said)

    def test_the_last_lines_shown_of_a_setup_or_check_lose_their_colour_codes(self):
        s = self.desk.settings()
        s["lanes"]["a"]["check"] = r"printf '\033[32m✓\033[39m passed\n\033[31mFAIL\033[0m one\n'; exit 1"
        self.assertEqual(self.desk.save_settings(s), [])
        m = self.settle(Dispatcher(self.desk), self.desk.new_task("Add hello.txt"))
        shown = views.show_text(self.desk, m["id"])
        self.assertIn("  ✓ passed\n  FAIL one", shown)
        self.assertNotIn("\x1b", shown)
        self.assertIn("\x1b[32m", (self.desk.tasks_dir / m["id"] / "check.log").read_text())  # the log keeps them
        said = views.try_text("a", {"ok": False, "onto": "abc", "steps": {"check": {
            "ran": True, "command": "npm test", "exit": 1, "seconds": 2, "tail": ["\x1b[32m✓\x1b[39m one"]}}})
        self.assertIn("    ✓ one", said)
        self.assertNotIn("\x1b", said)

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
        [tried] = [c for c in self.srt_calls(tries=True) if c not in self.srt_calls()]  # srt was tried first
        self.assertEqual((tried["settings"]["network"], tried["cwd"]), (got["network"], call["cwd"]))
        self.assertTrue(self.desk.state_dir.joinpath("srt.json").exists())
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

    def test_status_shows_how_long_the_check_itself_has_run(self):
        tid = self.desk.new_task("Add hello.txt")
        self.desk.update(tid, state="running", lane="a", phase="checking", started=time.time() - 8 * 60 - 5,
                         check_started=time.time() - 60 - 5)
        said = views.status_text(views.status(self.desk))
        self.assertIn(f"{tid} a worker done, hansa is running the lane's check, 1 min so far (8 min in all): ", said)

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

    def test_the_verification_tells_tests_for_a_defect_from_coverage_tests(self):
        from hansa.run import VERIFY_TASK
        text = " ".join(VERIFY_TASK.split())
        self.assertIn("Each defect the task names needs a test that fails with the code undone", text)
        self.assertIn("A test the task asks for to cover behaviour that already worked (coverage) may pass either "
                      "way: judge it on whether it tests what its name says", text)
        self.assertIn("a test whose name promises one case and tests another does, coverage or not", text)
        self.assertIn("VERDICT: does not hold", text)

    def test_retry_replaces_the_original(self):
        m = self.settle(Dispatcher(self.desk), self.desk.new_task("Add hello.txt"))
        main(["--desk", str(self.desk.root), "retry", m["id"], "also", "add", "a", "second", "line"])
        old = self.desk.meta(m["id"])
        self.assertEqual(old["state"], "replaced")
        self.assertNotIn(m["id"], [r["id"] for r in views.status(self.desk)["review"]])
        self.assertEqual(self.desk.meta(old["replaced_by"])["parent"], m["id"])

    def test_a_retry_gives_its_worker_the_latest_verification_report_whatever_its_verdict(self):
        d = Dispatcher(self.desk)
        tid = self.settle(d, self.desk.new_task("Add hello.txt"))["id"]
        main(["--desk", str(self.desk.root), "retry", tid, "no", "verification", "yet"])
        tid2 = self.desk.meta(tid)["replaced_by"]
        self.assertNotIn("verification", (self.desk.tasks_dir / tid2 / "task.md").read_text().split("---")[-1]
                         .replace("no verification yet", ""))
        tid = self.settle(d, tid2)["id"]
        for verdict in ("does not hold", "holds"):
            s = self.desk.settings()
            s["lanes"]["a"]["env"] = {"FAKE_VERDICT": verdict}
            self.assertEqual(self.desk.save_settings(s), [])
            with contextlib.redirect_stdout(io.StringIO()):
                main(["--desk", str(self.desk.root), "verify", tid])
            vid = self.desk.meta(tid)["verify"]
            self.assertEqual(self.settle(d, vid)["verdict"], verdict)
            s["lanes"]["a"]["env"] = {}
            self.assertEqual(self.desk.save_settings(s), [])
            vreport = (self.desk.tasks_dir / vid / "result.md").read_text().strip()
            self.assertGreater(len(vreport), 400)
            with contextlib.redirect_stdout(io.StringIO()):
                main(["--desk", str(self.desk.root), "retry", tid, "fix", "what", "it", "found"])
            new = self.desk.meta(tid)["replaced_by"]
            text = (self.desk.tasks_dir / new / "task.md").read_text()
            self.assertIn(f"The report of {vid}, the verification of the previous attempt", text)
            self.assertIn(f"its verdict: {verdict}):\n{vreport}", text)  # whole, marked as the verification's
            self.settle(d, new)
            tid = new

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

    def test_workers_are_not_given_tools_that_need_someone_there(self):
        from hansa import run
        tools = ["ScheduleWakeup", "CronCreate", "CronDelete", "CronList", "RemoteTrigger", "PushNotification",
                 "AskUserQuestion", "Monitor"]
        seen = self.tmp / "argv.jsonl"
        s = self.desk.settings()
        s["lanes"]["a"]["env"] = {"FAKE_SHOW_ARGV": str(seen)}
        self.assertEqual(self.desk.save_settings(s), [])
        d = Dispatcher(self.desk)
        tid = self.desk.new_task("Add hello.txt")
        self.settle(d, tid)
        with contextlib.redirect_stdout(io.StringIO()):
            main(["--desk", str(self.desk.root), "verify", tid])
        self.settle(d, self.desk.meta(tid)["verify"])
        task, verify = [json.loads(x) for x in seen.read_text().splitlines()]
        for argv in (task, verify):
            for t in tools:
                self.assertIn(t, self.denied_by(argv))
            self.assertIn("Nobody will wake you", argv[argv.index("--append-system-prompt") + 1])
        did = self.desk.new_task("Make the badge bigger")
        self.desk.update(did, lane="a", provider="claude", worktree=str(self.repo), session_id="sid-d",
                         model_asked="sonnet")
        argv, _, _ = run.design_command(self.desk, did)
        self.addCleanup(run.remove_sandbox_tmp, self.desk, did)  # the run folder it made for the session
        for t in tools:
            self.assertNotIn(t, self.denied_by(argv))

    def test_background_work_still_running_when_the_worker_ended_is_said(self):
        line = ("the worker ended while its background work was still running ({}); its report may be "
                "incomplete: read the changes before merging")
        for kind, what in (("bg-cmd", "Run the full test suite"), ("bg-agent", "Review the badge change")):
            with self.subTest(kind):
                self.assertEqual(self.transcript_run(kind), "Report: fixed the badge; the tests pass.\n")
                tid = self.desk.tasks()[-1]["id"]
                m = self.desk.meta(tid)
                self.assertEqual(m["state"], "done")  # not failed for it: the work may be complete
                self.assertIn(line.format(what), views.show_text(self.desk, tid))
                self.assertTrue(notify.task_line(self.desk, m).endswith(line.format(what)))
                self.transcript_run(kind + "-noticed")
                tid = self.desk.tasks()[-1]["id"]
                self.assertNotIn("background work was still running", views.show_text(self.desk, tid))
                self.assertNotIn("background work", notify.task_line(self.desk, self.desk.meta(tid)))

    def test_background_agents_with_notices_are_not_listed_as_still_running(self):
        # Four background agents each have a "completed" notice; a test run has none.
        from hansa import run
        home = self.tmp / "t0061-home"
        rec = home / "projects" / "-work" / "s61.jsonl"
        rec.parent.mkdir(parents=True)
        agents = [("aaeaa54cef8eb6489", "Review the catalog schema"), ("a1b2c3d4e5f60718", "Review the checkout flow"),
                  ("a9f8e7d6c5b4a392", "Review the receipt text"), ("a0c1e2a3b4c5d6e7", "Read the page texts")]
        rows, notices = [], []
        for i, (aid, what) in enumerate(agents):
            said, result = BG_AGENT(aid, what)
            rows += [{"type": "assistant", "message": {"id": f"m{i}", "content": [
                         {"type": "tool_use", "id": f"toolu_{i}", "name": "Agent", "input": {
                             "description": what, "prompt": "Do it", "subagent_type": "general-purpose"}}]}},
                     {"type": "user", "toolUseResult": result, "message": {"role": "user", "content": [
                         {"type": "tool_result", "tool_use_id": f"toolu_{i}",
                          "content": [{"type": "text", "text": said}]}]}}]
            notices.append(BG_NOTICE(aid, what))
        command = "python3 -m unittest tests.test_hansa 2>&1 | tail -15"
        said, result = BG_BASH("b2w1815is")
        rows += [{"type": "assistant", "message": {"id": "mb", "content": [
                     {"type": "tool_use", "id": "toolu_b", "name": "Bash", "input": {
                         "command": command, "run_in_background": True}}]}},
                 {"type": "user", "toolUseResult": result, "message": {"role": "user", "content": [
                     {"type": "tool_result", "tool_use_id": "toolu_b", "content": said}]}}]
        for n in notices:  # each notice is written twice: queued, then as the user row that opens the turn
            rows += [{"type": "queue-operation", "operation": "enqueue", "sessionId": "s61", "content": n},
                     {"type": "user", "message": {"role": "user", "content": n}}]
        rows.append({"type": "assistant", "message": {"id": "mz", "content": [
            {"type": "text", "text": "I'm waiting for the full test suite to finish before I write the report."}]}})
        rec.write_text("".join(json.dumps(r) + "\n" for r in rows))
        self.assertEqual(run.background_left(home, "s61"), [command])
        # Without the structured ids, the text is read: still only the test run
        for r in rows:
            r.pop("toolUseResult", None)
        rec.write_text("".join(json.dumps(r) + "\n" for r in rows))
        self.assertEqual(run.background_left(home, "s61"), [command])

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
        shown = views.show_text(self.desk, vid)
        self.assertIn(f"more lines; the whole text: hansa show {vid} --full", shown)
        self.assertNotIn("Run first", shown)  # hansa's instructions to the worker are not reprinted
        self.assertNotIn(f"## Task {tid} as it was given", shown)
        report = (self.desk.tasks_dir / vid / "result.md").read_text().strip()
        only = io.StringIO()
        with contextlib.redirect_stdout(only):
            main(["--desk", str(self.desk.root), "show", vid, "--report"])
        self.assertEqual(only.getvalue().strip(), f"verdict: does not hold\n\n{report}")
        self.assertNotIn(str(self.desk.tasks_dir), views.show_text(self.desk, vid))
        for args in (["merge", vid], ["merge", tid]):  # never merged itself; "does not hold" holds the task
            with self.assertRaises(SystemExit):
                main(["--desk", str(self.desk.root), *args])
        with contextlib.redirect_stdout(io.StringIO()):
            main(["--desk", str(self.desk.root), "merge", tid, "--despite-check"])
        self.assertEqual((self.desk.meta(tid)["state"], self.desk.meta(vid)["state"]), ("merged", "done"))
        shown = views.show_text(self.desk, vid)  # kept as it ended, with a plain line about the merge
        self.assertIn(f"{vid} · done · ", shown)
        self.assertIn("verdict: does not hold", shown)
        self.assertIn(f"\nclosed: {tid} was merged\n", shown)
        self.assertNotIn("error:", shown)
        self.assertNotIn("next:", shown)  # nothing left to do with it
        self.assertNotIn(vid, [m["id"] for m in views.status(self.desk)["review"]])

    def test_verify_on_another_lane_runs_there_and_stays_a_verification(self):
        seen = self.tmp / "argv.jsonl"
        d, t1, t2 = self.two_lanes()
        s = self.desk.settings()
        s["providers"]["other"] = {**s["providers"]["claude"]}
        s["lanes"]["b"].update(providers=["other"], check="exit 1",
                               env={"FAKE_VERDICT": "holds", "FAKE_SHOW_ARGV": str(seen)})
        other = self.tmp / "other"  # a lane on another repository cannot reach the change
        shutil.copytree(self.repo, other)
        s["lanes"]["c"] = {**json.loads(json.dumps(s["lanes"]["a"])), "repo": str(other)}
        self.assertEqual(self.desk.save_settings(s), [])
        before = len(self.desk.tasks())
        for lane, said in (("c", f"lane c cannot reach {t1}'s change: it is on {other}"), ("z", "there is no lane z")):
            with self.assertRaises(SystemExit) as e:
                main(["--desk", str(self.desk.root), "verify", t1, "--lane", lane])
            self.assertIn(said, str(e.exception))
            self.assertIn("nothing was queued", str(e.exception))
        self.assertEqual((len(self.desk.tasks()), self.desk.meta(t1).get("verify")), (before, None))
        with contextlib.redirect_stdout(io.StringIO()) as helped, self.assertRaises(SystemExit):
            main(["verify", "--help"])
        self.assertIn("By default it runs on the task's own lane", " ".join(helped.getvalue().split()))
        self.assertIn("--lane LANE", helped.getvalue())
        out = self.hansa("verify", t1, "--lane", "b")
        vid = self.desk.meta(t1)["verify"]
        self.assertIn(f"{vid} queued on lane b:", out)
        v = self.settle(d, vid)
        self.assertEqual((v["lane"], v["provider"], v["kind"], v["verdict"], v.get("check")),
                         ("b", "other", "verify", "holds", None))  # b's provider; never checked
        self.assertEqual((v["commits"], v["files"]), (0, []))  # its scratch changes are set aside
        self.assertIn(vid, subprocess.run(["git", "stash", "list"], cwd=v["worktree"], capture_output=True,
                                          text=True).stdout)
        self.assertEqual(Path(v["worktree"]), self.desk.worktrees_dir / "b")
        argv = json.loads(seen.read_text().splitlines()[-1])
        self.assertIn("checking another worker's finished task", argv[argv.index("--append-system-prompt") + 1])
        shown = views.show_text(self.desk, vid)
        self.assertIn(f"{vid} · done · lane b · other", shown)
        self.assertIn(f"it ran on lane b, not {t1}'s own lane a", shown)
        self.assertIn(f"verification (a worker ran the change): {vid}: holds; on lane b", views.show_text(self.desk, t1))
        with self.assertRaises(SystemExit):
            main(["--desk", str(self.desk.root), "merge", vid])  # never merged, on any lane
        with contextlib.redirect_stdout(io.StringIO()):  # verified again on its own lane: the first one is closed
            main(["--desk", str(self.desk.root), "verify", t1])
        v2 = self.desk.meta(t1)["verify"]
        self.assertEqual(self.desk.meta(v2)["lane"], "a")
        self.assertEqual(self.desk.meta(vid)["closed"], f"closed: {v2} verifies {t1} again")
        self.assertNotIn("; on lane", views.show_text(self.desk, t1))
        self.assertNotIn("own lane", views.show_text(self.desk, v2))
        with contextlib.redirect_stdout(io.StringIO()):
            main(["--desk", str(self.desk.root), "verify", t2, "--lane", "a"])
        v3 = self.desk.meta(t2)["verify"]
        with contextlib.redirect_stdout(io.StringIO()):
            main(["--desk", str(self.desk.root), "cancel", t2])
        self.assertEqual((self.desk.meta(v3)["lane"], self.desk.meta(v3)["state"], self.desk.meta(v3)["error"]),
                         ("a", "cancelled", f"closed: {t2} was cancelled"))  # stopped with its task, from another lane

    def test_verify_of_a_task_whose_lane_is_gone(self):
        d, t1, t2 = self.two_lanes()
        s = self.desk.settings()
        other = self.tmp / "other"  # a copy of the repository without the task's branch
        subprocess.run(["git", "clone", "-q", str(self.repo), str(other)], check=True)
        s["lanes"]["c"] = {**json.loads(json.dumps(s["lanes"]["a"])), "repo": str(other)}
        del s["lanes"]["a"]  # removed after t1 ran
        self.assertEqual(self.desk.save_settings(s), [])
        before = len(self.desk.tasks())
        for lane, said in (([], f"{t1}'s lane a is no longer on the desk; verify it with --lane L"),
                           (["--lane", "a"], f"{t1}'s lane a is no longer on the desk; verify it with --lane L"),
                           (["--lane", "c"], f"lane c's repository {other} has no branch task/{t1} at {t1}'s change"),
                           (["--lane", "z"], "there is no lane z")):
            with self.assertRaises(SystemExit) as e:
                main(["--desk", str(self.desk.root), "verify", t1, *lane])
            self.assertIsInstance(e.exception.code, str)
            self.assertIn(said, str(e.exception))
            self.assertIn("nothing was queued", str(e.exception))
        self.assertEqual((len(self.desk.tasks()), self.desk.meta(t1).get("verify")), (before, None))
        out = self.hansa("verify", t1, "--lane", "b")  # b is on the repository where t1's branch is
        vid = self.desk.meta(t1)["verify"]
        self.assertIn(f"{vid} queued on lane b:", out)
        v = self.settle(d, vid)
        self.assertEqual((v["lane"], v["state"], v["kind"]), ("b", "done", "verify"))

    def test_a_verification_that_ended_stays_as_it_ended_when_its_task_closes(self):
        d = Dispatcher(self.desk)
        t1, t2, t3 = (self.desk.new_task(f"Add hello.txt {n}") for n in (1, 2, 3))
        for t in (t1, t2, t3):
            self.settle(d, t)
        s = self.desk.settings()
        s["lanes"]["a"]["env"] = {"FAKE_VERDICT": "holds"}
        self.assertEqual(self.desk.save_settings(s), [])
        with contextlib.redirect_stdout(io.StringIO()):
            for t in (t1, t2):
                main(["--desk", str(self.desk.root), "verify", t])
        v1, v2 = (self.desk.meta(t)["verify"] for t in (t1, t2))
        for v in (v1, v2):
            self.assertEqual(self.settle(d, v)["verdict"], "holds")
        with contextlib.redirect_stdout(io.StringIO()):
            main(["--desk", str(self.desk.root), "verify", t3])
        v3 = self.desk.meta(t3)["verify"]
        self.assertEqual(self.desk.meta(v3)["state"], "queued")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            main(["--desk", str(self.desk.root), "merge", t1])
            main(["--desk", str(self.desk.root), "cancel", t2])
            main(["--desk", str(self.desk.root), "cancel", t3])
        self.assertNotIn("nothing verified", out.getvalue())  # v1 holds, kept after the merge
        self.assertIsNone(self.desk.meta(t1).get("unverified"))
        for v, t, why in ((v1, t1, "merged"), (v2, t2, "cancelled")):
            m = self.desk.meta(v)
            self.assertEqual((m["state"], m["verdict"], m.get("error", ""), m["closed"]),
                             ("done", "holds", "", f"closed: {t} was {why}"))
            self.assertIn(f"verification (a worker ran the change): {v}: holds", views.show_text(self.desk, t))
        m = self.desk.meta(v3)  # still queued: stopped with its task, as before
        self.assertEqual((m["state"], m["error"]), ("cancelled", f"closed: {t3} was cancelled"))
        st = views.status(self.desk)
        self.assertEqual((st["review"], st["queued"], st["running"]), ([], [], []))
        self.assertNotIn("waiting for review", views.status_text(st))
        self.assertIn(f"{v1}, the verification of {t1}, ended: verdict: holds",
                      notify.task_line(self.desk, self.desk.meta(v1)))
        self.assertNotIn("merge or send back", notify.task_line(self.desk, self.desk.meta(v1)))
        t4 = self.desk.new_task("Add hello.txt 4")  # hansa cancel on an ended verification itself: kept as well
        self.settle(d, t4)
        with contextlib.redirect_stdout(io.StringIO()):
            main(["--desk", str(self.desk.root), "verify", t4])
        v4 = self.desk.meta(t4)["verify"]
        self.settle(d, v4)
        for v, why in ((v4, f"closed: {v4} was closed with hansa cancel"), (v1, f"closed: {t1} was merged")):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                main(["--desk", str(self.desk.root), "cancel", v])
            self.assertEqual(out.getvalue(), f"{v} closed; it stays done, verdict: holds\n")
            m = self.desk.meta(v)
            self.assertEqual((m["state"], m["verdict"], m.get("error", ""), m["closed"]), ("done", "holds", "", why))
            shown = views.show_text(self.desk, v)
            self.assertIn(f"\n{why}\n", shown)
            self.assertNotIn("error:", shown)
        evs = [e for e in map(json.loads, self.desk.events_path.read_text().splitlines()) if e.get("task") in (v1, v4)]
        self.assertEqual([e["event"] for e in evs if e["event"] in ("task_cancel", "verification_closed")],
                         ["verification_closed"])  # v4, which it closed; v1 was closed already, and neither cancelled
        self.assertEqual(evs[-1]["task"], v4)
        self.assertEqual(self.desk.meta(t4)["state"], "done")  # its task still waits for review, verified
        from hansa.run import unverified_note
        self.assertEqual(unverified_note(self.desk, self.desk.meta(t4)), "")  # v4's verdict still counts
        self.assertEqual([m["id"] for m in views.status(self.desk)["review"]], [t4])

    def test_a_verification_closed_the_old_way_shows_as_the_rule_has_it_now(self):
        d = Dispatcher(self.desk)
        t = self.desk.new_task("Add hello.txt")
        self.settle(d, t)
        s = self.desk.settings()
        s["lanes"]["a"]["env"] = {"FAKE_VERDICT": "holds"}
        self.assertEqual(self.desk.save_settings(s), [])
        with contextlib.redirect_stdout(io.StringIO()):
            main(["--desk", str(self.desk.root), "verify", t])
        v = self.desk.meta(t)["verify"]
        self.settle(d, v)
        # as hansa recorded an ended verification when its task closed, before it kept one as it ended
        self.desk.update(v, state="cancelled", error=f"closed: {t} was merged")
        before = (self.desk.tasks_dir / v / "meta.json").read_text()
        shown = views.show_text(self.desk, v)
        self.assertTrue(shown.startswith(f"{v} · done · "), shown)
        self.assertIn(f"\nclosed: {t} was merged\n", shown)
        self.assertIn("verdict: holds", shown)
        self.assertNotIn("error:", shown)
        self.assertNotIn("cancelled", shown)
        self.assertNotIn("next:", shown)  # nothing is left to do with it
        self.assertIn(f"verification (a worker ran the change): {v}: holds", views.show_text(self.desk, t))
        self.assertEqual((self.desk.tasks_dir / v / "meta.json").read_text(), before)  # the record stays
        t2 = self.desk.new_task("Add hello.txt 2")  # one closed while still queued was never done: as it is
        self.settle(d, t2)
        with contextlib.redirect_stdout(io.StringIO()):
            main(["--desk", str(self.desk.root), "verify", t2])
            main(["--desk", str(self.desk.root), "cancel", t2])
        v2 = self.desk.meta(t2)["verify"]
        shown = views.show_text(self.desk, v2)
        self.assertTrue(shown.startswith(f"{v2} · cancelled · "), shown)
        self.assertIn(f"error: closed: {t2} was cancelled", shown)

    def test_add_verify_retry_and_show_tell_a_session_that_hears_from_hansa_it_will_be_told(self):
        d = Dispatcher(self.desk)
        t1, t2 = self.desk.new_task("Add hello.txt"), self.desk.new_task("Add hello.txt too")
        for t in (t1, t2):
            self.settle(d, t)
        (a, sa), (b, sb) = self.session(), self.session()
        self.hearing(a)
        with self.in_session(b, sb):  # no follower: hansa wait, as before
            out = self.hansa("verify", t1)
            v1 = self.desk.meta(t1)["verify"]
            self.assertIn(f"Follow it with: hansa wait {v1}; the verdict shows in hansa show {t1}", out)
            self.assertIn(f"next: wait for the verification: hansa wait {v1}", views.show_text(self.desk, t1))
            self.assertIn("Follow it with: hansa show t0004", self.hansa("add", "Queued from b"))
            with self.assertRaises(SystemExit) as e:
                main(["--desk", str(self.desk.root), "retry", "t0004", "again"])
            self.assertIn("Wait for it: hansa wait t0004, or cancel it first", str(e.exception.code))
        with self.in_session(a, sa):
            out = self.hansa("verify", t2)
            v2 = self.desk.meta(t2)["verify"]
            self.assertIn(f"hansa tells this session when {v2} ends; the verdict shows in hansa show {t2}", out)
            self.assertIn(f"next: wait for the verification: hansa tells this session when {v2} ends",
                          views.show_text(self.desk, t2))
            # b's own verification: no other session hears it, so a does
            self.assertIn(f"hansa tells this session when {v1} ends", views.show_text(self.desk, t1))
            self.assertIn("t0006 queued. hansa tells this session when t0006 ends; hansa show t0006 says where it is",
                          self.hansa("add", "Queued from a"))
            with self.assertRaises(SystemExit) as e:
                main(["--desk", str(self.desk.root), "retry", "t0006", "again"])
            self.assertIn("hansa tells this session when t0006 ends, or cancel it first", str(e.exception.code))
            out = self.hansa("retry", t2, "again")
            self.assertIn("t0007 queued as a follow-up", out)
            self.assertIn("hansa tells this session when t0007 ends", out)
            self.assertNotIn("hansa wait", out)
            self.hearing(b)  # now b hears its own: a is not told of them
            self.assertIn(f"next: wait for the verification: hansa wait {v1}", views.show_text(self.desk, t1))
            with self.assertRaises(SystemExit) as e:
                main(["--desk", str(self.desk.root), "retry", "t0004", "again"])
            self.assertIn("Wait for it: hansa wait t0004", str(e.exception.code))

    def test_merge_says_when_nothing_verified_a_change_to_more_than_text(self):
        d = Dispatcher(self.desk)

        def done(name, text="Add a file"):
            s = self.desk.settings()
            s["lanes"]["a"]["env"] = {"FAKE_HELLO": name, "FAKE_VERDICT": "holds"}
            self.assertEqual(self.desk.save_settings(s), [])
            return self.settle(d, self.desk.new_task(text))["id"]

        def verified(tid):
            self.hansa("verify", tid)
            self.assertEqual(self.settle(d, self.desk.meta(tid)["verify"])["verdict"], "holds")
            return self.desk.meta(tid)["verify"]

        t1 = done("a.py")
        verified(t1)
        out = self.hansa("merge", t1)
        self.assertIn(f"{t1} merged into main", out)
        self.assertNotIn("note:", out)
        t2 = done("b.py")
        out = self.hansa("merge", t2)
        self.assertIn(f"{t2} merged into main", out)  # merged all the same
        self.assertIn(f"note: nothing verified this change: {t2} changed b.py, and no verification of it holds", out)
        self.assertEqual(self.desk.meta(t2)["state"], "merged")
        t3 = done("c.py")
        v3 = verified(t3)
        self.hansa("retry", t3, "more")
        t4 = self.desk.meta(t3)["replaced_by"]
        self.assertEqual(self.settle(d, t4)["files"], ["c.py"])
        out = self.hansa("merge", t4)
        self.assertIn(f"{t4} merged into main", out)
        self.assertIn(f"note: {t4} changed c.py, and no verification of {t4} held; {v3} verified {t3}, the attempt "
                      f"it retries, not this one", out)
        t5 = done("d.py")
        self.hansa("retry", t5, "more")
        t6 = self.desk.meta(t5)["replaced_by"]
        self.settle(d, t6)
        self.assertIn(f"neither it nor {t5}, the attempt it retries, has a verification that holds",
                      self.hansa("merge", t6))
        t8 = done("e.py")  # verified; retried twice: the verification of the first attempt still counts, named
        v8 = verified(t8)
        self.hansa("retry", t8, "more")
        t9 = self.desk.meta(t8)["replaced_by"]
        self.settle(d, t9)
        self.hansa("retry", t9, "more again")
        t10 = self.desk.meta(t9)["replaced_by"]
        self.settle(d, t10)
        out = self.hansa("merge", t10)
        self.assertIn(f"{t10} merged into main", out)
        self.assertIn(f"note: {t10} changed e.py, and no verification of {t10} held; {v8} verified {t8}, an earlier "
                      f"attempt of this change, not this one: what came after that verification (a conflict settled, "
                      f"code changed) is not the code it checked", out)
        self.assertNotIn("nothing verified", out)
        t11 = done("f.py")  # retried twice, never verified: every attempt is named
        self.hansa("retry", t11, "more")
        t12 = self.desk.meta(t11)["replaced_by"]
        self.settle(d, t12)
        self.hansa("retry", t12, "more again")
        t13 = self.desk.meta(t12)["replaced_by"]
        self.settle(d, t13)
        self.assertIn(f"nothing verified this change: {t13} changed f.py, and neither it nor any attempt it retries "
                      f"({t12}, {t11}) has a verification that holds", self.hansa("merge", t13))
        t7 = done("guide.md")
        out = self.hansa("merge", t7)
        self.assertIn(f"{t7} merged into main", out)
        self.assertNotIn("note:", out)  # only text: nothing to verify

    def test_the_unverified_note_names_the_nearest_verdict_along_the_chain_even_one_that_did_not_hold(self):
        from hansa.run import unverified_note
        ids = []
        for i, verdict in enumerate(("holds", "does not hold", None)):  # t1 verified; its retry t2 failed; t3 retries t2
            tid = self.desk.new_task(f"Add a.py {i}", parent=ids[-1] if ids else None)
            fields = {"state": "done", "files": ["a.py"]}
            if verdict:
                v = self.desk.new_task("Run it", kind="verify", parent=tid)
                self.desk.update(v, state="done", verdict=verdict)
                fields["verify"] = v
            self.desk.update(tid, **fields)
            ids.append(tid)
        t1, t2, t3 = ids
        v2 = self.desk.meta(t2)["verify"]
        note = unverified_note(self.desk, self.desk.meta(t3))
        self.assertEqual(note, f"{t3} changed a.py, and no verification of {t3} held; {v2} verified {t2}, the attempt "
                               f"it retries, and found it does not hold")
        self.assertNotIn(t1, note)  # the older verification that held says nothing of what came after
        self.desk.update(self.desk.meta(t2)["verify"], verdict="unsure")
        self.assertIn(f"{v2} verified {t2}, the attempt it retries, and could not tell whether it holds",
                      unverified_note(self.desk, self.desk.meta(t3)))
        self.assertEqual(unverified_note(self.desk, self.desk.meta(t2)),
                         f"{t2} changed a.py, and its verification {v2} could not tell whether it holds")

    def test_a_merge_through_a_merge_check_says_when_nothing_verified_it(self):
        d, t1, t2 = self.two_lanes(check="true", names=("a.py", "b.py"))
        s = self.desk.settings()
        s["lanes"]["a"]["env"]["FAKE_VERDICT"] = "holds"
        self.assertEqual(self.desk.save_settings(s), [])
        self.hansa("verify", t1)
        self.assertEqual(self.settle(d, self.desk.meta(t1)["verify"])["verdict"], "holds")
        out = self.hansa("merge", t1, "--recheck")  # verified: nothing to say
        self.assertIn(f"{t1} is not merged yet", out)
        self.assertNotIn("note:", out)
        self.assertEqual(self.settle_merge(d, t1)["state"], "merged")
        out = self.hansa("merge", t2)  # main moved: a merge check, and nothing verified it
        self.assertIn(f"{t2} is not merged yet: main has", out)
        self.assertIn(f"note: nothing verified this change: {t2} changed b.py, and no verification of it holds", out)
        self.assertIn("this is a note, not a stop", out)
        self.assertEqual(self.settle_merge(d, t2)["state"], "merged")
        heard = {x["key"][1]: x["line"] for x in notify.items_for(self.desk, self.events("task_merged"),
                                                                   {"key": "k", "session": None})}
        self.assertNotIn("note:", heard[t1])
        self.assertIn(f"after its merge check passed: Add b.py · note: nothing verified this change: {t2} changed "
                      f"b.py", heard[t2])
        self.assertNotIn("note:", views.show_text(self.desk, t1))
        self.assertIn(f"note: nothing verified this change: {t2} changed b.py", views.show_text(self.desk, t2))

    def test_what_is_text_goes_by_the_kind_of_file_not_its_folder(self):
        for p in ("guide.md", "docs/guide.md", "notes.txt", "doc/index.rst", "README", "LICENSE", "sub/COPYING",
                  "LICENSE-COMMERCIAL.md"):
            self.assertTrue(text_only(p), p)
        for p in ("docs/conf.py", "doc/app.js", "a.py", "README.py", "docs/Makefile"):
            self.assertFalse(text_only(p), p)
        s = self.desk.settings()
        s["lanes"]["a"]["env"] = {"FAKE_HELLO": "docs/conf.py"}
        self.assertEqual(self.desk.save_settings(s), [])
        tid = self.settle(Dispatcher(self.desk), self.desk.new_task("Add docs/conf.py"))["id"]
        self.assertIn(f"note: nothing verified this change: {tid} changed docs/conf.py", self.hansa("merge", tid))

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

    def test_waiting_reason_says_a_stuck_server_is_busy_or_stuck(self):
        from hansa import run
        tid = self.desk.new_task("Anything")
        design = self.desk.new_task("Talk it over", lane="a", kind="design")
        held = cli.hold_server_lock(self.desk)
        self.addCleanup(held.close)
        for quiet, said in ((0, "starts when `hansa serve` next looks (within seconds)"),
                            (run.ROUND_QUIET + 5, "starts when `hansa serve` next looks (within seconds)"),
                            (run.STUCK_AFTER + 60, "but `hansa serve` is busy or stuck: no round finished for 6 minutes")):
            self.round_at(quiet)
            why = waiting_reason(self.desk, self.desk.settings(), self.desk.meta(tid), set())
            self.assertIn(said, why)
            self.assertEqual("within seconds" in why, quiet < run.STUCK_AFTER, why)
            why = waiting_reason(self.desk, self.desk.settings(), self.desk.meta(design), set())
            self.assertEqual("within seconds" in why, quiet < run.STUCK_AFTER, why)
        self.assertIn("busy or stuck", why)
        held.close()
        why = waiting_reason(self.desk, self.desk.settings(), self.desk.meta(tid), set())
        self.assertIn("but `hansa serve` is not running", why)

    def test_hansa_design_says_what_the_waiting_lines_say_when_the_server_does_not_look(self):
        from hansa import run
        held = cli.hold_server_lock(self.desk)
        self.addCleanup(held.close)
        for quiet, said in ((0, "hansa opens the design session in seconds and asks the principal to join it."),
                            (run.STUCK_AFTER + 60, "hansa opens the design session once `hansa serve` gets through its "
                                                   "rounds; `hansa serve` is busy or stuck: no round finished for 6 "
                                                   "minutes. Then it asks the principal to join it.")):
            self.round_at(quiet)
            out = self.hansa("design", "Talk it over", "--lane", "a")
            self.assertIn(said, out)
            self.hansa("cancel", out.split()[0])
        held.close()
        out = self.hansa("design", "Talk it over", "--lane", "a")
        self.assertIn("hansa opens the design session once `hansa serve` gets through its rounds; `hansa serve` is "
                      "not running. Then it asks the principal to join it.", out)
        self.assertNotIn("in seconds", out)

    def test_add_retry_and_verify_say_when_no_provider_of_the_lane_can_start_the_task(self):
        d = Dispatcher(self.desk)
        tid = self.settle(d, self.desk.new_task("Add hello.txt"))["id"]
        out = self.hansa("add", "Ready at once")
        self.assertNotIn("waits", out)  # claude is ready
        s = self.desk.settings()
        s["providers"]["mimo"] = {"kind": "compatible", "model": "mimo-x", "effort": "", "base_url": ""}
        s["lanes"]["b"] = {**json.loads(json.dumps(s["lanes"]["a"])), "providers": ["claude"]}
        s["lanes"]["a"]["providers"] = ["mimo"]
        self.assertEqual(self.desk.save_settings(s), [])
        self.assertNotIn("waits", self.hansa("add", "On lane b", "--lane", "b"))
        for args in (["add", "Waits on lane a", "--lane", "a"], ["verify", tid], ["retry", tid, "again"]):
            out = self.hansa(*args)
            new = max(m["id"] for m in self.desk.tasks())
            why = waiting_reason(self.desk, self.desk.settings(), self.desk.meta(new), set())
            self.assertIn(f"\n{new} waits: lane a has no provider ready (mimo: no endpoint address", out)
            self.assertIn(f"{new} waits: {why}\n", out)
            self.assertIn(f"waiting: {why}", views.status_text(views.status(self.desk)))  # in the same words

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

    def test_cost_turns_shows_where_the_coordinators_money_goes(self):
        conf = self.tmp / "conf"
        proj = conf / "projects" / cost.slug(self.desk.root)
        proj.mkdir(parents=True)
        t0 = time.time() - 3600

        def at(n):
            return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(t0 + n * 60))

        def call(n, mid, u, tool=None, side=False):
            usage = {"input_tokens": u[0], "output_tokens": u[1], "cache_read_input_tokens": u[2],
                     "cache_creation_input_tokens": u[3],
                     "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": u[3]}}
            content = [{"type": "tool_use", "id": f"tu_{mid}_{tool}", "name": tool, "input": {}}] if tool else \
                [{"type": "text", "text": "SECRET-ANSWER"}]
            return {"type": "assistant", "timestamp": at(n), "isSidechain": side,
                    "message": {"id": mid, "model": "claude-opus-5", "content": content, "usage": usage}}

        def user(n, kind=None, meta=False, content="SECRET-PROMPT", side=False):
            return {"type": "user", "timestamp": at(n), "isSidechain": side, **({"isMeta": True} if meta else {}),
                    **({"origin": {"kind": kind}} if kind else {}), "message": {"role": "user", "content": content}}

        result = [{"type": "tool_result", "tool_use_id": "x", "content": "SECRET-OUTPUT"}]
        lines = [
            user(0, "human"),  # typed: one message over three lines, then a tool result and a second step
            {**call(0, "m1", (10, 50, 1000, 200)), "message": {**call(0, "m1", (10, 50, 1000, 200))["message"],
                                                               "content": [{"type": "thinking", "thinking": "SECRET"}]}},
            call(0, "m1", (10, 50, 1000, 200), "Bash"),
            call(0, "m1", (10, 50, 1000, 200), "Read"),
            user(1, content=result),
            call(1, "m2", (5, 20, 1400, 100), "Bash"),
            {"type": "queue-operation", "timestamp": at(2)},
            user(3, "peer", meta=True),  # a hansa post: isMeta, yet a turn of its own
            call(3, "m3", (4, 30, 1500, 300)),
            {"type": "system", "subtype": "scheduled_task_fire", "timestamp": at(5)},  # a scheduled wakeup
            user(5, meta=True),  # the note that follows a wakeup: no origin, carries on
            call(5, "m4", (3, 10, 1800, 50)),
            user(5, "peer", meta=True),  # a post right after a wakeup: its own turn, not the wakeup's
            call(5, "m6", (1, 5, 1860, 20), "Bash"),
            user(6, "task-notification"),  # a helper's result
            user(6, content="SECRET-SIDE", side=True),  # a sidechain: a helper's, not a turn
            call(6, "s1", (7, 70, 700, 70), "Grep", side=True),
            call(7, "m5", (2, 40, 1900, 400), "Agent"),
        ]
        text = "\n".join(json.dumps(x) for x in lines) + "\n" + json.dumps(user(8, "human"))[:40]  # half-written
        (proj / "sess-1234abcd.jsonl").write_text(text)
        helper = proj / "sess-1234abcd" / "subagents"
        helper.mkdir(parents=True)
        (helper / "agent-1.jsonl").write_text(json.dumps(call(6, "h1", (1, 10, 100, 10), "Glob", side=True)) + "\n")
        (helper / "agent-1.meta.json").write_text(json.dumps({"agentType": "scout"}))
        s = self.desk.settings()
        s["providers"]["claude"]["config_dir"] = str(conf)
        self.desk.save_settings(s)
        rep = cost.turns(self.desk, 1)
        got = [(t["kind"], t["steps"], t["length"], t["tools"]) for t in rep["turns"]]
        self.assertEqual(got, [("typed", 2, 1210, {"Bash": 2, "Read": 1}), ("peer", 1, 1804, {}),
                               ("wakeup", 1, 1853, {}), ("peer", 1, 1881, {"Bash": 1}),
                               ("helper result", 1, 2302, {"Agent": 1})])
        prices = s["prices"]

        def worth(u):
            return cost.value("claude-opus-5", {"in": u[0], "out": u[1], "cache_read": u[2], "cache_write": u[3],
                                                "cache_write_5m": 0, "cache_write_1h": u[3]}, prices)
        typed = worth((10, 50, 1000, 200)) + worth((5, 20, 1400, 100))
        self.assertAlmostEqual(rep["turns"][0]["usd"], typed, places=6)
        self.assertEqual({k: (v["turns"], v["steps"]) for k, v in rep["by_kind"].items()},
                         {"typed": (1, 2), "peer": (2, 2), "wakeup": (1, 1), "helper result": (1, 1)})
        self.assertAlmostEqual(sum(v["share"] for v in rep["by_kind"].values()), 1.0, places=6)
        self.assertAlmostEqual(rep["by_kind"]["typed"]["share"], typed / rep["total"]["usd"], places=4)
        self.assertAlmostEqual(sum(rep["token_shares"].values()), 1.0, places=6)
        steps = [(10, 50, 1000, 200), (5, 20, 1400, 100), (4, 30, 1500, 300), (3, 10, 1800, 50), (1, 5, 1860, 20),
                 (2, 40, 1900, 400)]
        pr = cost.price_for("claude-opus-5", prices)
        reads = sum(u[2] for u in steps) * pr["cache_read"] / 1e6
        self.assertAlmostEqual(rep["token_shares"]["cache_read"], reads / rep["total"]["usd"], places=4)
        self.assertEqual((rep["total"]["turns"], rep["total"]["steps"]), (5, 6))
        self.assertEqual({k: v["steps"] for k, v in rep["helpers"].items()}, {"helper": 1, "helper:scout": 1})
        seat = cost.report(self.desk, 1)["by"]["seat"]
        self.assertAlmostEqual(rep["total"]["usd"], seat["coordinator"]["usd"], places=6)
        self.assertEqual(seat["coordinator"]["tokens"], sum(sum(u) for u in steps))
        self.assertAlmostEqual(rep["helpers"]["helper"]["usd"], seat["helper"]["usd"], places=6)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            main(["--desk", str(self.desk.root), "cost", "--turns", "--days", "1"])
        text = out.getvalue()
        self.assertNotIn("SECRET", text)
        self.assertIn("helper result", text)
        self.assertIn("Bash 2, Read 1", text)
        self.assertIn(f"worth ${seat['coordinator']['usd']:.2f}", text)
        later = cost.turns(self.desk, 1, since=t0 + 4 * 60)  # --since: the wakeup, the post and the helper's result
        self.assertEqual([t["kind"] for t in later["turns"]], ["wakeup", "peer", "helper result"])

    def test_cost_rows_add_up_to_the_total_in_every_view(self):
        tid = self.desk.new_task("Add hello.txt")
        self.settle(Dispatcher(self.desk), tid)
        conf = self.tmp / "conf"
        proj = conf / "projects" / cost.slug(self.desk.root)
        proj.mkdir(parents=True)
        (proj / "s.jsonl").write_text(json.dumps({"timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
            "message": {"id": "msg_1", "model": "claude-opus-5", "usage": {"input_tokens": 100, "output_tokens": 10}}}) + "\n")
        s = self.desk.settings()
        s["providers"]["claude"]["config_dir"] = str(conf)
        self.desk.save_settings(s)
        rep = cost.report(self.desk, 1)
        self.assertEqual(rep["total"]["tokens"], 6300 + 110)
        for key in ("day", "seat", "lane", "task", "model", "provider"):
            rows = rep["by"][key].values()
            self.assertEqual(sum(v["tokens"] for v in rows), rep["total"]["tokens"], key)
            self.assertAlmostEqual(sum(v["usd"] for v in rows), rep["total"]["usd"], places=5, msg=key)
        self.assertEqual({k: v["tokens"] for k, v in rep["by"]["task"].items()}, {tid: 6300, "(coordinator)": 110})
        self.assertEqual(rep["by"]["lane"]["(coordinator)"]["tokens"], 110)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            main(["--desk", str(self.desk.root), "cost", "--days", "1", "--by", "task"])
        self.assertIn("  (coordinator) ", out.getvalue())
        with contextlib.redirect_stdout(out := io.StringIO()):
            main(["--desk", str(self.desk.root), "cost", "--days", "1", "--json"])
        self.assertEqual(json.loads(out.getvalue())["by"]["task"]["(coordinator)"]["tokens"], 110)
        with contextlib.redirect_stdout(out := io.StringIO()), self.assertRaises(SystemExit):
            main(["--desk", str(self.desk.root), "cost", "--help"])
        self.assertIn("counted only when it runs with the workers' Claude login", " ".join(out.getvalue().split()))

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

    def test_status_names_the_same_build_as_version_even_as_the_desks_account(self):
        import hansa
        first = views.status_text(views.status(self.desk)).splitlines()[0]
        self.assertIn(f" · hansa {hansa.build()} · ", first)  # as hansa --version says it
        with unittest.mock.patch.object(account, "runuser_bin", lambda: "/usr/sbin/runuser"):
            env, calls = account._as("someone", ["status"], self.desk.root)[1], []
        self.assertEqual(env["HANSA_BUILD"], hansa.build())  # root's hansa hands its answer to the account's
        self.addCleanup(hansa.build.cache_clear)

        def refused(argv, **kw):  # git refuses a checkout the account does not own
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 128, "", "fatal: detected dubious ownership")
        with unittest.mock.patch.object(hansa.subprocess, "run", refused), \
                unittest.mock.patch.object(hansa.Path, "read_text", side_effect=OSError):
            for handed, said in (("", hansa.__version__), (f"{hansa.__version__} abc1234", f"{hansa.__version__} abc1234"),
                                 ("9.9.9 abc1234", hansa.__version__)):  # another version's answer is not taken
                hansa.build.cache_clear()
                with unittest.mock.patch.object(hansa, "_handed", handed):
                    self.assertEqual(hansa.build(), said)
        self.assertIn(f"safe.directory={Path(hansa.__file__).resolve().parent.parent}", calls[0])

    def test_the_handed_build_is_read_once_and_not_passed_on_to_what_hansa_starts(self):
        import hansa
        handed = f"{hansa.__version__} abc1234"
        env = {**os.environ, "HANSA_BUILD": handed,
               "PYTHONPATH": str(Path(hansa.__file__).resolve().parent.parent)}
        out = subprocess.run([sys.executable, "-c", "import hansa, os, subprocess, sys; print(hansa.build()); "
                              "print(repr(os.environ.get('HANSA_BUILD'))); sys.stdout.flush(); "
                              "subprocess.run([sys.executable, '-c', 'import os; print(repr(os.environ.get(\"HANSA_BUILD\")))'])"],
                             env=env, capture_output=True, text=True, cwd="/")
        self.assertEqual(out.stdout.splitlines(), [handed, "None", "None"], out.stderr)  # a restart asks git again

    def test_installed_is_when_build_was_written_else_when_the_newest_code_file_changed(self):
        import hansa
        pkg = self.tmp / "installed" / "hansa"
        (pkg / "agents").mkdir(parents=True)
        (pkg / "__pycache__").mkdir()
        for name, at in (("__init__.py", 1_000_000), ("agents/scout.md", 2_000_000), ("ui.html", 1_500_000),
                         ("__pycache__/run.cpython-312.pyc", 9_000_000)):  # what Python writes as it runs: not code
            (pkg / name).write_text("x")
            os.utime(pkg / name, (at, at))
        self.addCleanup(hansa.installed.cache_clear)
        with unittest.mock.patch.object(hansa, "__file__", str(pkg / "__init__.py")):
            hansa.installed.cache_clear()
            self.assertEqual(hansa.installed(), 2_000_000)  # a checkout: when its files last changed, not a commit time
            (pkg / "BUILD").write_text("abc1234 2026-09-01\n")
            os.utime(pkg / "BUILD", (3_000_000, 3_000_000))
            hansa.installed.cache_clear()
            self.assertEqual(hansa.installed(), 3_000_000)  # an install: when it wrote BUILD
        at = time.mktime((2026, 9, 29, 14, 3, 59, 0, 0, -1))
        self.assertEqual(hansa.installed_text(at), "installed 2026-09-29 14:03")  # to the minute
        self.assertEqual(hansa.installed_text(0), "")

    def test_version_and_status_say_when_the_running_code_was_installed(self):
        import hansa
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            main(["--version"])
        self.assertEqual(out.getvalue().strip(), f"hansa {hansa.build()}, {hansa.installed_text()}")
        self.assertRegex(hansa.installed_text(), r"^installed \d{4}-\d\d-\d\d \d\d:\d\d$")
        first = views.status_text(views.status(self.desk)).splitlines()[0]
        self.assertIn(f" · hansa {hansa.build()} · {hansa.installed_text()} · now ", first)

    def test_status_says_when_hansa_serve_runs_older_code_than_is_installed(self):
        import hansa
        import hansa.run as run
        self.assertEqual(run.serve_code(self.desk), "")  # no server: nothing to restart
        self.serving()
        pid = run.server_holder(self.desk)[0]
        rec = self.desk.state_dir / "serve-code"
        write_json(rec, {"pid": pid, "build": hansa.build(), "installed": hansa.installed()})
        self.assertEqual(run.serve_code(self.desk), "")  # the code it runs is the code installed
        self.assertEqual(views.status(self.desk)["serve_code"], "")
        old = hansa.installed() - 3600
        write_json(rec, {"pid": pid, "build": "0.5.0a1 0ld0000", "installed": old})
        said = run.serve_code(self.desk)
        self.assertEqual(said, f"hansa serve runs 0.5.0a1 0ld0000 ({hansa.installed_text(old)}), but {hansa.build()} "
                               f"was {hansa.installed_text()}: restart it")
        self.assertEqual(views.status_text(views.status(self.desk)).splitlines()[1], said)
        write_json(rec, {"pid": pid, "build": hansa.build(), "installed": old})  # the same commit, put in place again
        self.assertIn("restart it", run.serve_code(self.desk))
        write_json(rec, {"pid": pid + 1, "build": hansa.build(), "installed": hansa.installed()})  # another server's
        lock = self.desk.state_dir / "serve.lock"  # one that recorded nothing: by when it started
        os.utime(lock, (old, old))
        self.assertEqual(run.serve_code(self.desk), f"hansa serve started "
                         f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(old))}, before {hansa.build()} was "
                         f"{hansa.installed_text()}: restart it")
        os.utime(lock, None)  # started after the code was installed
        self.assertEqual(run.serve_code(self.desk), "")

    def test_hansa_serve_records_the_code_it_runs(self):
        import hansa
        import hansa.run as run
        run.record_serve_code(self.desk)
        self.assertEqual(json.loads((self.desk.state_dir / "serve-code").read_text()),
                         {"pid": os.getpid(), "build": hansa.build(), "installed": hansa.installed()})

    def test_status_sizes_its_columns_to_the_longest_lane_and_provider_names(self):
        s = self.desk.settings()
        s["providers"]["shop-claude"] = {"kind": "subscription", "model": "claude-opus-4-5[1m]", "effort": "",
                                        "config_dir": ""}
        s["lanes"]["shop-claude"] = {**s["lanes"]["a"], "providers": ["shop-claude", "claude"]}
        self.assertEqual(self.desk.save_settings(s), [])
        lines = views.status_text(views.status(self.desk)).splitlines()
        rows = lines[lines.index("lanes:") + 1:lines.index("lanes:") + 3]
        self.assertEqual([r.index("free") for r in rows], [rows[0].index("free")] * 2, rows)
        self.assertEqual([r.split()[:2] for r in rows], [["a", "claude"], ["shop-claude", "shop-claude"]])
        provs = lines[lines.index("providers:") + 1:lines.index("providers:") + 3]
        self.assertEqual(len({r.index("subscription") for r in provs}), 1, provs)
        models = [("claude", "opus"), ("shop-claude", "claude-opus-4-5[1m]")]
        ends = [r.index(m) + len(m) for r, (_, m) in zip(provs, models)]
        self.assertEqual(len({e + len(r[e:]) - len(r[e:].lstrip()) for r, e in zip(provs, ends)}), 1, provs)

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
        skip_unless_ports(self)
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

    def test_the_page_shows_merge_checks_with_what_they_wait_for(self):
        skip_unless_ports(self)
        d, t1, t2 = self.two_lanes(check="test -f README.md")
        t3 = self.desk.new_task("Add b.txt again", lane="b")
        self.settle(d, t3)
        s = self.desk.settings()
        s["lanes"]["b"]["check"] = "sleep 30"
        self.assertEqual(self.desk.save_settings(s), [])
        self.hansa("merge", t1)
        self.hansa("merge", t2)
        self.hansa("merge", t3)
        end = time.time() + 20
        while (self.desk.meta(t2).get("merge_check") or {}).get("phase") != "running" and time.time() < end:
            d.tick()
            time.sleep(0.1)
        self.addCleanup(lambda: (self.hansa("cancel", t2), d.tick()))
        httpd = start_web(self.desk, "127.0.0.1", 0)
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        tok = self.desk.web_token()
        try:
            with urllib.request.urlopen(f"{base}/api/status?t={tok}") as r:
                merging = {m["id"]: m for m in json.load(r)["merging"]}
            with urllib.request.urlopen(f"{base}/?t={tok}") as r:
                page = r.read().decode()
        finally:
            httpd.shutdown()
            httpd.server_close()
        self.assertEqual((merging[t2]["phase"], merging[t2]["minutes"], merging[t2]["lane"]), ("running", 0, "b"))
        # t3 waits for t2's merge check on the same repository and base, which also keeps lane b busy
        self.assertEqual((merging[t3]["phase"], merging[t3]["busy_with"], merging[t3]["waits_for"]),
                         ("queued", t2, {"id": t2, "lane": "b"}))
        self.assertEqual(merging[t3]["where"], f"merge check queued: waits for {t2}'s merge check on lane b")  # as hansa status says
        # shown with the tasks waiting for review, in both languages, as hansa status puts it
        self.assertIn("st.review.length || (st.merging || []).length ? section(T.review", page)
        self.assertIn("m.waits_for ? T.mcQueuedAhead(m.waits_for) : m.busy_with ? T.mcQueuedBusy(m.lane, m.busy_with)", page)
        self.assertIn("`merge check running for ${n} min`", page)
        self.assertIn("`merge check queued: waits for ${w.id}'s merge check on lane ${w.lane}`", page)
        self.assertIn("`merge check queued: waits for lane ${lane}, busy with ${id}`", page)
        self.assertIn("`合并验收已跑了 ${n} 分钟`", page)
        self.assertIn("`合并验收在排队：同一个仓库和基础分支一次只跑一个合并验收，先等工作线 ${w.lane} 上 ${w.id} 的合并验收`", page)
        self.assertIn("`合并验收在排队：等工作线 ${lane} 空出来（现在在做 ${id}）`", page)

    def test_the_page_shows_how_long_the_check_itself_has_run(self):
        skip_unless_ports(self)
        tid = self.desk.new_task("Add hello.txt")
        self.desk.update(tid, state="running", lane="a", phase="checking", started=time.time() - 8 * 60 - 5,
                         check_started=time.time() - 60 - 5)
        httpd = start_web(self.desk, "127.0.0.1", 0)
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        tok = self.desk.web_token()
        try:
            with urllib.request.urlopen(f"{base}/api/status?t={tok}") as r:
                running = {m["id"]: m for m in json.load(r)["running"]}
            with urllib.request.urlopen(f"{base}/?t={tok}") as r:
                page = r.read().decode()
        finally:
            httpd.shutdown()
            httpd.server_close()
        self.assertEqual((running[tid]["checking"], running[tid]["check_minutes"], running[tid]["minutes"]), (True, 1, 8))
        # the minutes column shows the check's own minutes then, as hansa status does, in both languages
        self.assertIn("m.checking ? T.checkMinutes(m.check_minutes, m.minutes) : m.minutes", page)
        self.assertIn("checkMinutes:(c, n) => `${c} min so far (${n} min in all)`", page)
        self.assertIn("checkMinutes:(c, n) => `验收已跑 ${c} 分钟（一共 ${n} 分钟）`", page)

    def test_web_needs_token_and_header(self):
        skip_unless_ports(self)
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
        skip_unless_ports(self)
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
        self.assertIn(f"a  {self.repo.resolve()}   base main  providers claude", listed)  # "repo" pads to "other"
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

    def test_explain_lane_names_the_command_that_made_or_last_changed_it(self):
        first = views.explain_text(self.desk, "lane", "a").splitlines()[0]
        self.assertEqual(first, "lane a (made with hansa init; not by the principal on the web page)")
        other = self.tmp / "other"
        other.mkdir()
        sh("git", "init", "-q", "-b", "dev", cwd=other)
        sh("git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "init", cwd=other)
        self.hansa("lane", "add", "b", "--repo", str(other))
        self.assertEqual(views.explain_text(self.desk, "lane", "b").splitlines()[0],
                         "lane b (made with hansa lane add; not by the principal on the web page)")
        self.hansa("lane", "set", "b", "max_turns", "50")
        self.hansa("lane", "set", "a", "note", "the shop front")
        self.assertEqual(views.explain_text(self.desk, "lane", "b").splitlines()[0],
                         "lane b (made with hansa lane add; last changed with hansa lane set b max_turns; "
                         "not by the principal on the web page)")
        self.desk.events_path.write_text("")  # a desk from before hansa recorded them
        self.assertIn("set by the coordinator with hansa lane add or hansa lane set",
                      views.explain_text(self.desk, "lane", "b").splitlines()[0])

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

    def test_none_empties_a_lane_field_as_the_help_says(self):
        self.hansa("lane", "set", "a", "network", "registry.npmjs.org")
        self.hansa("lane", "set", "a", "check", "make test")
        self.hansa("lane", "set", "a", "env", "A=1")
        out = self.hansa("lane", "set", "a", "network", "none")
        self.assertIn('network: ["registry.npmjs.org"] -> []', out)
        self.assertEqual(self.events("lane_network_changed")[-1]["new"], [])
        self.hansa("lane", "set", "a", "check", "none")
        self.hansa("lane", "set", "a", "env", "none")
        ln = self.desk.settings()["lanes"]["a"]
        self.assertEqual((ln["network"], ln["check"], ln["env"]), ([], "", {}))
        self.assertIn("network during a task: none", views.explain_text(self.desk, "lane", "a"))
        for field, said in (("max_turns", "takes one whole number"), ("base", "takes the name of a branch"),
                            ("providers", "list at least one provider")):
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as e:
                main(["--desk", str(self.desk.root), "lane", "set", "a", field, "none"])
            self.assertIn(said, str(e.exception.code))

    def test_none_with_other_values_is_refused_by_lane_set_and_lane_add_alike(self):
        self.hansa("lane", "set", "a", "network", "*.pypi.org")
        before = self.desk.settings_path.read_text()
        for field, values in (("network", ["none", "registry.npmjs.org"]), ("allow", ["Bash(ls)", "none"]),
                              ("write", ["none", "/x"]), ("providers", ["none,claude"]), ("env", ["A=1", "none"]),
                              ("check", ["none", "make"]), ("note", ["x", "none"]), ("max_turns", ["none", "3"])):
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as e:
                main(["--desk", str(self.desk.root), "lane", "set", "a", field, *values])
            self.assertIn(f"none means an empty {field}, so it cannot come with other values", str(e.exception.code))
            self.assertEqual(self.desk.settings_path.read_text(), before)
        other = self.tmp / "other"
        other.mkdir()
        sh("git", "init", "-q", "-b", "dev", cwd=other)
        sh("git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "i", cwd=other)
        for extra in (["--network", "none", "registry.npmjs.org"], ["--allow", "none", "Bash(ls)"],
                      ["--providers", "claude,none"]):
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as e:
                main(["--desk", str(self.desk.root), "lane", "add", "b", "--repo", str(other), *extra])
            self.assertIn("none means an empty", str(e.exception.code))
            self.assertEqual(self.desk.settings_path.read_text(), before)
        self.hansa("lane", "add", "b", "--repo", str(other), "--network", "none", "--check", "none", "--allow", "none")
        ln = self.desk.settings()["lanes"]["b"]
        self.assertEqual((ln["network"], ln.get("check", ""), ln.get("allow", [])), ([], "", []))

    def test_localhost_on_a_lanes_network_list_on_macos_prints_what_listens_on_the_loopback(self):
        found = ([("postgres", 552, "me", "127.0.0.1", 5432), ("postgres", 552, "me", "[::1]", 5432),
                  ("NimbusProxy", 1846, "me", "127.0.0.1", 2086), ("rapportd", 561, "me", "*", 49185)], 3)
        found[0].sort(key=lambda x: x[4])
        listeners = unittest.mock.Mock(return_value=found)
        with unittest.mock.patch.object(cli, "ON_MAC", True), \
                unittest.mock.patch.object(cli, "mac_loopback_listeners", listeners):
            out = self.hansa("lane", "set", "a", "network", "registry.npmjs.org", "localhost")
            self.assertIn("Warning: on macOS, localhost on lane a's network list", out)
            self.assertIn("a local proxy among them reaches the internet", out)
            self.assertIn("What listens on this machine's loopback now; a worker can reach every one:\n"
                          "  NimbusProxy (pid 1846, me): 127.0.0.1:2086\n"
                          "  postgres (pid 552, me): 127.0.0.1:5432, [::1]:5432\n"
                          "  rapportd (pid 561, me): *:49185\n"
                          "3 processes of other accounts could not be looked at (only root can)", out)
            # localhost already on the list, the list the same or changed around it: told once is enough
            self.assertNotIn("Warning", self.hansa("lane", "set", "a", "network", "registry.npmjs.org", "localhost"))
            self.assertNotIn("Warning", self.hansa("lane", "set", "a", "network", "localhost", "*.pypi.org"))
            self.assertNotIn("Warning", self.hansa("lane", "set", "a", "note", "x"))
            self.assertNotIn("Warning", self.hansa("lane", "set", "a", "network", "registry.npmjs.org"))
            self.assertEqual(listeners.call_count, 1)
            listeners.return_value = None
            self.assertIn("What listens on the loopback now could not be read here.",
                          self.hansa("lane", "set", "a", "network", "localhost:3000"))
            listeners.return_value = ([], 0)
            other = self.tmp / "other"
            other.mkdir()
            sh("git", "init", "-q", "-b", "dev", cwd=other)
            sh("git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "i", cwd=other)
            out = self.hansa("lane", "add", "b", "--repo", str(other), "--network", "127.0.0.1")
            self.assertIn('network ["127.0.0.1"]', out)
            self.assertIn("Tell the principal", out)
            self.assertIn("Warning: on macOS, localhost on lane b's network list", out)
            self.assertIn("Nothing that hansa can look at listens on the loopback now.", out)
            self.assertEqual(self.events("lane_network_changed")[-1]["new"], ["127.0.0.1"])
            self.assertEqual(self.desk.settings()["lanes"]["b"]["network"], ["127.0.0.1"])
            self.assertIn("they need localhost on the list", views.explain_text(self.desk, "lane", "b"))
        with unittest.mock.patch.object(cli, "ON_MAC", False), \
                unittest.mock.patch.object(cli, "mac_loopback_listeners", listeners):
            out = self.hansa("lane", "set", "a", "network", "localhost")  # Linux: nothing more
            self.assertNotIn("Warning", out)
            self.assertNotIn("listens", out)
        self.assertEqual(listeners.call_count, 3)
        with unittest.mock.patch.object(views.sys, "platform", "linux"):
            self.assertIn("local servers a worker starts (on Linux): they need nothing on the list",
                          views.explain_text(self.desk, "lane", "a"))

    @unittest.skipUnless(sys.platform == "darwin", "macOS reads listeners through libproc")
    def test_the_servers_listening_on_the_loopback_are_read_through_libproc(self):
        import pwd
        from hansa import run
        self.assertEqual(run._loopback_address(1, bytes(12) + bytes([127, 0, 0, 1])), "127.0.0.1")
        self.assertEqual(run._loopback_address(1, bytes(16)), "*")
        self.assertEqual(run._loopback_address(1, bytes(12) + bytes([10, 0, 0, 2])), "")
        self.assertEqual(run._loopback_address(2, bytes(15) + b"\1"), "[::1]")
        self.assertEqual(run._loopback_address(2, bytes(16)), "*")
        skip_unless_ports(self)
        srv = socket.socket()
        self.addCleanup(srv.close)
        srv.bind(("127.0.0.1", 0))
        srv.listen()
        port = srv.getsockname()[1]
        got = run.mac_loopback_listeners()
        self.assertIsNotNone(got)
        mine = [x for x in got[0] if x[1] == os.getpid()]
        self.assertIn(("127.0.0.1", port), [(x[3], x[4]) for x in mine])
        self.assertTrue(all(x[2] == pwd.getpwuid(os.getuid()).pw_name for x in mine))
        srv.close()
        self.assertNotIn(port, [x[4] for x in run.mac_loopback_listeners()[0] if x[1] == os.getpid()])

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


    # -- releases (hansa release; hansa/release.py) --------------------------------------------------------------
    def commit_in_repo(self, name, text, message=None):
        (self.repo / name).parent.mkdir(parents=True, exist_ok=True)
        (self.repo / name).write_text(text)
        sh("git", "add", name, cwd=self.repo)
        sh("git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", message or f"Add {name}",
           cwd=self.repo)
        return self.git_out("rev-parse", "HEAD")

    def with_release(self, command, **lane):
        s = self.desk.settings()
        s["lanes"]["a"].update(release=command, **lane)
        self.assertEqual(self.desk.save_settings(s), [])

    def settle_release(self, d, rid, timeout=20):
        from hansa import release
        end = time.time() + timeout
        while time.time() < end:
            d.tick()
            r = release.get(self.desk, rid)
            if r["state"] not in ("approved", "starting", "running"):
                return r
            time.sleep(0.1)
        self.fail(f"release #{rid} did not end: {release.get(self.desk, rid)}")

    def refused(self, *args) -> str:
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as e:
            main(["--desk", str(self.desk.root), *args])
        return str(e.exception.code)

    def test_a_release_waits_for_the_principals_yes_then_runs_and_the_coordinator_hears_how_it_went(self):
        from hansa import release
        out = self.tmp / "released.txt"
        self.desk.write_key("demo", "sk-demo-release-123456")
        sha = self.commit_in_repo("deploy.sh", "echo deploying\n")
        self.hansa("lane", "set", "a", "release", f"sh ./deploy.sh && echo $HANSA_RELEASE_COMMIT > {out} "
                                                  "&& echo token $DEMO_TOKEN")
        os.environ["DEMO_TOKEN"] = "sk-demo-release-123456"  # hansa's own environment: a push's login, say
        self.assertEqual([e["field"] for e in self.events("lane_command_changed")], ["release"])
        p, sock = self.session()
        with self.in_session(p, sock):
            told = self.hansa("release", "a", "first", "cut")
        self.assertIn("release #1: lane a's main at", told)
        self.assertIn("its last 2 commit(s) (no release of it went out before)", told)
        self.assertIn("The command names changed files: deploy.sh", told)
        self.assertIn("waits for the principal's yes on the web page", told)
        r = release.get(self.desk, 1)
        self.assertEqual((r["state"], r["lane"], r["commit"], r["note"]), ("waiting", "a", sha, "first cut"))
        self.assertEqual([c["subject"] for c in r["commits"]], ["Add deploy.sh", "init"])
        self.assertEqual((r["files"], r["named"]), (["README.md", "deploy.sh"], ["deploy.sh"]))
        d = Dispatcher(self.desk)
        for _ in range(3):  # nothing runs before the yes
            d.tick()
        self.assertEqual(release.get(self.desk, 1)["state"], "waiting")
        self.assertFalse(out.exists())
        # the page shows it, in both its languages, with the file the command names
        [row] = views.status(self.desk)["releases"]
        self.assertEqual((row["id"], row["state"], row["named"], row["moved"]), (1, "waiting", ["deploy.sh"], ""))
        ui = (Path(__file__).resolve().parent.parent / "hansa" / "ui.html").read_text()
        for x in ("releaseYes:", "releaseNo:", "releaseNamed:", "releasesWithoutAsking:", "releaseForbidden:"):
            self.assertEqual(ui.count(x), 2, x)
        self.assertIn('"/api/release/answer"', ui)
        self.assertIn("#1 lane a at", views.status_text(views.status(self.desk)))
        # the page's yes: to the release as the page showed it
        self.assertFalse(release.answer(self.desk, 1, True, "", "0" * 40, r["command"])["ok"])
        self.assertEqual(release.answer(self.desk, 1, True, "", sha, r["command"]), {"ok": True})
        self.assertFalse(release.answer(self.desk, 1, True, "", sha, r["command"])["ok"])  # answered already
        r = self.settle_release(d, 1)
        self.assertEqual((r["state"], r["exit"]), ("done", 0), r)
        self.assertEqual(out.read_text().strip(), sha)  # at the commit the principal saw, in the repository
        log = (self.desk.state_dir / "releases" / "1.log").read_text()
        self.assertIn("deploying", log)
        self.assertIn("token [key of demo]", log)  # kept, keys removed
        self.assertNotIn("sk-demo-release", log + json.dumps(r))
        self.assertEqual([e["event"] for e in map(json.loads, self.desk.events_path.read_text().splitlines())
                          if e["event"].startswith("release_")],
                         ["release_asked", "release_answered", "release_started", "release_ended"])
        # the coordinator's session hears the yes and the end
        events = [e for e in map(json.loads, self.desk.events_path.read_text().splitlines())]
        heard = [x["line"] for x in notify.items_for(self.desk, events, {"key": "k", "session": {
            "pid": p.pid, "start": notify.proc_start(p.pid)}})]
        self.assertTrue(any("release #1 of lane a" in x and "the principal said yes" in x for x in heard), heard)
        self.assertTrue(any("release #1 of lane a" in x and "done" in x for x in heard), heard)
        self.assertIn("done", self.hansa("release"))
        told = self.hansa("explain", "lane", "a")
        self.assertIn("release: sh ./deploy.sh", told)
        self.assertIn("last release: #1 lane a at " + sha[:10], told)
        # the next one shows only what came since
        self.commit_in_repo("b.txt", "b\n")
        with self.in_session(p, sock):
            self.assertIn("1 commit(s) since release #1", self.hansa("release", "a"))
        self.assertEqual([c["subject"] for c in release.get(self.desk, 2)["commits"]], ["Add b.txt"])

    def test_the_page_answers_a_release_through_its_own_address(self):
        skip_unless_ports(self)
        from hansa import release
        self.with_release("true")
        self.hansa("release", "a")
        r = release.get(self.desk, 1)
        httpd = start_web(self.desk, "127.0.0.1", 0)
        base, tok = f"http://127.0.0.1:{httpd.server_address[1]}", self.desk.web_token()
        try:
            body = json.dumps({"id": 1, "yes": True, "commit": r["commit"], "command": r["command"]}).encode()
            req = urllib.request.Request(f"{base}/api/release/answer?t={tok}", data=body, method="POST")
            req.add_header("X-Hansa", "1")
            with urllib.request.urlopen(req) as got:
                self.assertEqual(json.load(got), {"ok": True})
        finally:
            httpd.shutdown()
            httpd.server_close()
        self.assertEqual(release.get(self.desk, 1)["state"], "approved")

    def test_a_refused_release_runs_nothing_and_the_coordinator_hears_why(self):
        from hansa import release
        out = self.tmp / "ran.txt"
        self.with_release(f"touch {out}")
        self.hansa("release", "a")
        r = release.get(self.desk, 1)
        self.assertEqual(release.answer(self.desk, 1, False, "not before the demo", r["commit"], r["command"]),
                         {"ok": True})
        d = Dispatcher(self.desk)
        for _ in range(3):
            d.tick()
        r = release.get(self.desk, 1)
        self.assertEqual((r["state"], r["reason"]), ("refused", "not before the demo"))
        self.assertFalse(out.exists())
        heard = [x["line"] for x in notify.items_for(self.desk, self.events("release_answered"), {"key": "k", "session": None})]
        self.assertEqual(len(heard), 1)
        self.assertIn("the principal refused it: not before the demo", heard[0])
        self.assertIn("refused by the principal", self.hansa("release"))

    def test_a_lane_the_principal_lists_goes_out_without_asking(self):
        from hansa import release
        out = self.tmp / "ran.txt"
        self.with_release(f"touch {out}")
        self.assertEqual(self.page_save(lambda s: s.update(releases_without_asking=["a"])), {"ok": True, "problems": []})
        told = self.hansa("release", "a")
        self.assertIn("on the principal's list of lanes released without asking", told)
        self.assertEqual(release.get(self.desk, 1)["state"], "approved")
        r = self.settle_release(Dispatcher(self.desk), 1)
        self.assertEqual(r["state"], "done", r)
        self.assertTrue(out.exists())
        self.assertEqual(self.events("release_answered"), [])  # nobody was asked
        # taken off the list before it ran: it waits for the yes after all
        self.assertEqual(self.page_save(lambda s: s.update(releases_without_asking=["a"])), {"ok": True, "problems": []})
        self.commit_in_repo("b.txt", "b\n")
        self.hansa("release", "a")
        self.assertEqual(self.page_save(lambda s: s.update(releases_without_asking=[])), {"ok": True, "problems": []})
        Dispatcher(self.desk).tick()
        self.assertEqual(release.get(self.desk, 2)["state"], "waiting")

    def test_a_base_that_moved_or_a_command_that_changed_voids_the_yes(self):
        from hansa import release
        out = self.tmp / "ran.txt"
        self.with_release(f"touch {out}")
        d = Dispatcher(self.desk)
        # the base moved before the yes
        self.hansa("release", "a")
        r = release.get(self.desk, 1)
        self.commit_in_repo("b.txt", "b\n")
        self.assertIn("main moved since it was asked", views.status(self.desk)["releases"][0]["moved"])
        told = release.answer(self.desk, 1, True, "", r["commit"], r["command"])
        self.assertFalse(told["ok"])
        self.assertIn("main moved since it was asked", told["void"])
        self.assertEqual(release.get(self.desk, 1)["state"], "void")
        # the base moved after the yes, before it ran
        self.hansa("release", "a")
        r = release.get(self.desk, 2)
        self.assertEqual(release.answer(self.desk, 2, True, "", r["commit"], r["command"]), {"ok": True})
        self.commit_in_repo("c.txt", "c\n")
        d.tick()
        r = release.get(self.desk, 2)
        self.assertEqual(r["state"], "void")
        self.assertIn("main moved since it was asked", r["void"])
        # the command changed before the yes, and after it
        self.hansa("release", "a")
        r = release.get(self.desk, 3)
        self.hansa("lane", "set", "a", "release", f"touch {out}; echo more")
        told = release.answer(self.desk, 3, True, "", r["commit"], r["command"])
        self.assertIn("release command changed since it was asked", told["void"])
        self.hansa("release", "a")
        r = release.get(self.desk, 4)
        self.assertEqual(release.answer(self.desk, 4, True, "", r["commit"], r["command"]), {"ok": True})
        self.hansa("lane", "set", "a", "release", f"touch {out}")
        d.tick()
        self.assertIn("release command changed since it was asked", release.get(self.desk, 4)["void"])
        self.assertFalse(out.exists())  # none of them ran
        heard = [x["line"] for x in notify.items_for(self.desk, self.events("release_void"), {"key": "k", "session": None})]
        self.assertEqual(len(heard), 4)
        self.assertTrue(all("is void" in x and "hansa release a" in x for x in heard), heard)
        # a newer request replaces one still waiting, quietly: the coordinator made it
        self.hansa("release", "a")
        self.hansa("release", "a")
        self.assertEqual(release.get(self.desk, 5)["void"], "replaced by release #6")
        self.assertEqual(len(notify.items_for(self.desk, self.events("release_void"), {"key": "k", "session": None})), 4)

    def test_words_the_principal_keeps_from_going_out_refuse_a_release(self):
        from hansa import release
        self.with_release("true")
        self.assertEqual(self.page_save(lambda s: s.update(release_forbidden=["Project Falcon"])),
                         {"ok": True, "problems": []})
        self.commit_in_repo("notes.txt", "public line\nthe PROJECT FALCON plan\n+++ not a header: project falcon\n")
        self.commit_in_repo("c.txt", "clean\n", message="Tidy\n\nfor project falcon only")
        self.commit_in_repo("notes.txt", "public line\n")  # taken out again: still in the history that goes out
        said = self.refused("release", "a")
        self.assertIn("refused: the commits going out hold words the principal keeps from going out", said)
        self.assertIn("notes.txt: the PROJECT FALCON plan", said)
        self.assertIn("notes.txt: +++ not a header: project falcon", said)
        self.assertIn("commit message: for project falcon only", said)
        r = release.get(self.desk, 1)
        self.assertEqual((r["state"], len(r["hits"])), ("forbidden", 3))
        d = Dispatcher(self.desk)
        d.tick()
        self.assertEqual(release.get(self.desk, 1)["state"], "forbidden")
        # a word the principal adds after the yes stops it before it runs
        self.assertEqual(self.page_save(lambda s: s.update(release_forbidden=[])), {"ok": True, "problems": []})
        self.hansa("release", "a")
        r = release.get(self.desk, 2)
        self.assertEqual(release.answer(self.desk, 2, True, "", r["commit"], r["command"]), {"ok": True})
        self.assertEqual(self.page_save(lambda s: s.update(release_forbidden=["public LINE"])), {"ok": True, "problems": []})
        d.tick()
        self.assertEqual(release.get(self.desk, 2)["state"], "forbidden")
        self.assertEqual(self.events("release_started"), [])
        heard = [x["line"] for x in notify.items_for(self.desk, self.events("release_forbidden"), {"key": "k", "session": None})]
        self.assertEqual(len(heard), 1)  # the one found at the ask, the coordinator saw itself

    def test_a_worker_cannot_ask_for_a_release(self):
        from hansa import release
        self.with_release("true")
        with unittest.mock.patch.dict(os.environ, {"HANSA_TASK": "t0001"}):
            self.assertIn("only the coordinator asks for a release", self.refused("release", "a"))
        self.assertEqual(release.releases(self.desk), [])
        self.hansa("lane", "set", "a", "release", "none")
        self.assertIn("lane a has no release command", self.refused("release", "a"))
        self.assertIn("no releases yet", self.hansa("release"))

    def test_a_release_stops_at_the_lanes_time_limit(self):
        from hansa import release
        self.with_release("echo started; sleep 60", timeout_min=1)
        self.assertEqual(self.page_save(lambda s: s.update(releases_without_asking=["a"])), {"ok": True, "problems": []})
        self.hansa("release", "a")
        d = Dispatcher(self.desk)
        d.tick()
        r = release.get(self.desk, 1)
        self.assertEqual(r["state"], "running")
        self.soon(lambda: "started" in (self.desk.state_dir / "releases" / "1.log").read_text())
        release.change(self.desk, 1, lambda x: {"started": x["started"] - 61})  # a minute and more went by
        start = time.time()
        r = self.settle_release(d, 1)
        self.assertLess(time.time() - start, 15)
        self.assertEqual(r["state"], "failed", r)
        self.assertEqual(r["problem"], "stopped at the lane's time limit (1 min)")
        self.assertIn("started", r["tail"])
        heard = [x["line"] for x in notify.items_for(self.desk, self.events("release_ended"), {"key": "k", "session": None})]
        self.assertIn("FAILED: stopped at the lane's time limit (1 min)", heard[0])

    def test_a_release_runs_with_hansas_environment_not_the_lanes(self):
        from hansa import release
        env = self.tmp / "env.txt"
        self.with_release(f"env > {env}", env={"LANE_ONLY_VAR": "from-the-lane"})
        self.assertEqual(self.page_save(lambda s: s.update(releases_without_asking=["a"])), {"ok": True, "problems": []})
        self.hansa("release", "a")
        with unittest.mock.patch.dict(os.environ, {"HANSA_OWN_VAR": "from-hansa"}):
            r = self.settle_release(Dispatcher(self.desk), 1)
        self.assertEqual(r["state"], "done", r)
        seen = dict(x.split("=", 1) for x in env.read_text().splitlines() if "=" in x)
        self.assertNotIn("LANE_ONLY_VAR", seen)
        self.assertEqual((seen.get("HANSA_OWN_VAR"), seen.get("HANSA_RELEASE")), ("from-hansa", "1"))
        self.assertEqual(seen.get("HANSA_RELEASE_COMMIT"), r["commit"])

    def test_a_release_and_a_merge_on_one_repository_wait_for_each_other(self):
        from hansa import release
        go = self.tmp / "go"
        tid = self.desk.new_task("Add hello.txt")
        d = Dispatcher(self.desk)
        self.assertEqual(self.settle(d, tid)["state"], "done")
        self.with_release(f"while [ ! -f {go} ]; do sleep 0.1; done")
        self.assertEqual(self.page_save(lambda s: s.update(releases_without_asking=["a"])), {"ok": True, "problems": []})
        self.hansa("release", "a")
        d.tick()
        self.assertEqual(release.get(self.desk, 1)["state"], "running")
        # a merge asked while it runs waits for it
        told = self.hansa("merge", tid)
        self.assertIn(f"{tid} is not merged yet", told)
        self.assertIn("release #1 of lane a", told)
        self.assertEqual(self.desk.meta(tid)["state"], "merging")
        for _ in range(3):
            d.tick()
        self.assertEqual(self.desk.meta(tid)["state"], "merging")
        self.assertIn("release #1 of lane a", views.merge_check_text(self.desk, self.desk.meta(tid)))
        go.write_text("")
        self.assertEqual(self.settle_release(d, 1)["state"], "done")
        self.assertEqual(self.settle_merge(d, tid)["state"], "merged")
        # a release approved while a merge check runs there waits for it; the merge moves the base, so it is void
        s = self.desk.settings()
        s["lanes"]["a"].update(check=f"while [ ! -f {go}2 ]; do sleep 0.1; done")
        self.assertEqual(self.desk.save_settings(s), [])
        t2 = self.desk.new_task("Add more.txt")
        self.desk.update(t2, lane="a")
        os.environ["FAKE_HELLO"] = "more.txt"
        (self.tmp / (go.name + "2")).write_text("")
        self.assertEqual(self.settle(d, t2)["state"], "done")
        (self.tmp / (go.name + "2")).unlink()
        self.commit_in_repo("base.txt", "moved\n")  # the base moved: the merge is checked first
        self.hansa("merge", t2)
        d.tick()
        self.assertEqual((self.desk.meta(t2).get("merge_check") or {}).get("phase"), "running")
        self.hansa("release", "a")
        rid = release.releases(self.desk)[-1]["id"]
        for _ in range(3):
            d.tick()
        self.assertEqual(release.get(self.desk, rid)["state"], "approved")
        self.assertIn(f"waits for {t2}'s merge check", self.hansa("release"))
        (self.tmp / (go.name + "2")).write_text("")
        self.assertEqual(self.settle_merge(d, t2)["state"], "merged")
        d.tick()
        self.assertIn("main moved since it was asked", release.get(self.desk, rid)["void"])

    def test_the_coordinator_cannot_change_the_principals_release_settings(self):
        s = self.desk.settings()
        self.assertEqual((s["releases_without_asking"], s["release_forbidden"]), ([], []))
        for k, v in (("releases_without_asking", ["a"]), ("release_forbidden", ["secret"])):
            s = self.desk.settings()
            s[k] = v
            said = self.desk.save_settings(s, by="coordinator")
            self.assertTrue(any(k in x and "principal's" in x for x in said), said)
            self.assertEqual(self.desk.settings()[k], [])
        for field in ("releases_without_asking", "release_forbidden"):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                main(["--desk", str(self.desk.root), "lane", "set", "a", field, "a"])
        # the page sets them, and the coordinator's own changes leave them as they are
        self.assertEqual(self.page_save(lambda s: s.update(releases_without_asking=["a"], release_forbidden=["secret"])),
                         {"ok": True, "problems": []})
        self.hansa("lane", "set", "a", "release", "git push origin main")
        self.hansa("concurrency", "2")
        s = self.desk.settings()
        self.assertEqual((s["releases_without_asking"], s["release_forbidden"], s["lanes"]["a"]["release"]),
                         (["a"], ["secret"], "git push origin main"))
        self.assertFalse(self.page_save(lambda s: s.update(release_forbidden=["two\nlines"]))["ok"])
        self.assertFalse(self.page_save(lambda s: s.update(releases_without_asking=["Not A Lane"]))["ok"])
        told = self.hansa("explain")
        self.assertIn("you cannot set them", told)
        self.assertIn("for lanes a, which the principal lets go out without asking", told)


if __name__ == "__main__":
    unittest.main()
