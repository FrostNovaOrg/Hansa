"""Tests for bh with a stand-in for claude, so no model is called."""
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
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bh import cost, views  # noqa: E402
from bh.cli import init_desk, main  # noqa: E402
from bh.desk import Desk, public_settings, validate  # noqa: E402
from bh.run import Dispatcher, provider_problem, waiting_reason  # noqa: E402
from bh.web import start_web  # noqa: E402

FAKE = r'''#!/usr/bin/env python3
import json, os, sys
prompt = sys.stdin.read()
compat = bool(os.environ.get("ANTHROPIC_BASE_URL"))
if compat and os.environ.get("FAKE_BADKEY_ON_COMPAT"):
    print(json.dumps({"type": "result", "subtype": "success", "is_error": True, "api_error_status": 401,
                      "result": "API Error: 401 authentication_error", "session_id": "s-key", "num_turns": 1}))
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
            f.write(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": t}]}}) + "\n")
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
open("hello.txt", "w").write("hello from " + os.environ.get("BH_TASK", "?") + "\n")
if os.environ.get("FAKE_SHOW_ARGV"):
    open(os.environ["FAKE_SHOW_ARGV"], "a").write(json.dumps(sys.argv) + "\n")
print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "num_turns": 3,
    "result": "Added hello.txt. Checked it exists." + ("\nran:\n" * 70 + "VERDICT: " + os.environ["FAKE_VERDICT"]
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
        os.environ["BH_CLAUDE"] = str(fake)
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
        self.assertGreaterEqual(m["commits"], 1)  # left uncommitted by the worker, committed by bh
        self.assertEqual(m["denials"], ["Bash: env JAVA_HOME=/x mvn test"])
        self.assertIn("hello.txt", views.show_text(self.desk, tid))
        self.assertEqual(main(["--desk", str(self.desk.root), "merge", tid]), 0)
        self.assertEqual(self.desk.meta(tid)["state"], "merged")
        self.assertTrue((self.repo / "hello.txt").exists())

    def test_quota_moves_task_to_next_provider(self):
        s = self.desk.settings()
        s["providers"]["mimo"] = {"kind": "compatible", "model": "mimo-x", "effort": "", "base_url": "https://example.invalid"}
        s["lanes"]["a"]["providers"] = ["mimo", "claude"]
        s["lanes"]["a"]["env"] = {"FAKE_QUOTA_ON_COMPAT": "1"}
        self.assertEqual(self.desk.save_settings(s), [])
        self.desk.write_key("mimo", "sk-test")
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
        r = subprocess.run([str(Path(__file__).resolve().parent.parent / "bin" / "bh"), "--desk", str(self.desk.root),
                            "coordinator", "--permission-mode", "dontAsk"],
                           capture_output=True, text=True, env={**os.environ, "BH_CLAUDE": str(echo)})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--permission-mode dontAsk", r.stdout)
        repo = self.repo.resolve()
        self.assertIn(f"--add-dir {repo}", r.stdout)
        self.assertIn(f"Edit(//{str(repo).lstrip('/')}/**)", r.stdout)
        self.assertIn("Agent(general-purpose) Agent(Explore)", r.stdout)  # Claude Code's own helpers are off
        code = Path(__file__).resolve().parent.parent / "bh"
        self.assertEqual((self.desk.root / "CLAUDE.md").read_text(), (code / "card.md").read_text())
        agents = self.desk.root / ".claude" / "agents"
        for name in ("scout.md", "reviewer.md"):  # the desk's helpers come along
            self.assertEqual((agents / name).read_text(), (code / "agents" / name).read_text())
        self.assertIn("effort: medium", (agents / "scout.md").read_text())
        self.assertFalse((agents / "verifier.md").exists())  # retired, kept in .bh/
        self.assertTrue(list(self.desk.state_dir.glob("verifier.md.*")))

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
        self.desk.write_key("mimo", "sk-test")
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
        self.desk.write_key("mimo", "sk-test")
        tid = self.desk.new_task("Look at the page")
        m = self.settle(Dispatcher(self.desk), tid)
        self.assertEqual(m["state"], "failed")
        self.assertTrue(m["error"].startswith("stopped at the ceiling of 300 turns: the task was probably too big"), m)
        shown = views.show_text(self.desk, tid)
        self.assertIn("Stopped at the ceiling before it wrote its report", shown)
        self.assertIn("week badge is still in English", shown)
        self.assertIn("split the task or make it clearer", shown)
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
        with contextlib.redirect_stdout(io.StringIO()):
            main(["--desk", str(self.desk.root), "verify", tid, "the file's contents"])
        vid = self.desk.meta(tid)["verify"]
        v = self.desk.meta(vid)
        self.assertEqual((v["kind"], v["parent"], v["start_from"]), ("verify", tid, self.desk.meta(tid)["head"]))
        with self.assertRaises(SystemExit):  # not while the verification runs
            main(["--desk", str(self.desk.root), "merge", tid])
        v = self.settle(d, vid)
        self.assertEqual((v["state"], v["verdict"], v.get("check")), ("done", "does not hold", None))
        argv = json.loads(seen.read_text().splitlines()[-1])
        self.assertIn("Bash(git checkout:*)", argv)
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

    def test_browser_tool_is_not_told_where_the_worker_works(self):
        folder = self.tmp / "browser"
        folder.mkdir()
        fake = self.tmp / "fake_browser.py"
        fake.write_text("import os, sys\nwith open('seen.txt', 'w') as f:\n"
                        "    f.write(os.getcwd() + '\\n')\n    f.writelines(sys.stdin)\nprint('bye')\n")
        init = {"jsonrpc": "2.0", "id": 0, "method": "initialize",
                "params": {"capabilities": {"roots": {"listChanged": True}}, "clientInfo": {"name": "claude-code"}}}
        call = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "browser_take_screenshot"}}
        relay = Path(__file__).resolve().parent.parent / "bh" / "relay.py"
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
        later = cost.report(self.desk, 1, since=time.time() + 60)  # bh cost --since: nothing after "now"
        self.assertEqual((later["total"]["tokens"], later["window"][:6]), (0, "since "))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            main(["--desk", str(self.desk.root), "cost", "--since", time.strftime("%H:%M"), "--by", "seat"])
        self.assertIn("since ", out.getvalue())
        self.assertAlmostEqual(rep["by"]["seat"]["coordinator"]["usd"], (100 * 5 + 10 * 25) / 1e6, places=6)
        self.assertEqual(rep["by"]["seat"]["worker"]["tokens"], 6300)
        self.assertEqual(rep["unpriced_models"], [])

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
            req.add_header("X-BH", "1")
            with urllib.request.urlopen(req) as r:
                self.assertTrue(json.load(r)["ok"])
            self.assertTrue(self.desk.paused())
        finally:
            httpd.shutdown()
            httpd.server_close()


if __name__ == "__main__":
    unittest.main()
