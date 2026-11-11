"""A requested stop keeps Codex's answer and accounting, through hansa's finish, show and cost readings."""
import contextlib
import io
import json
import os
import signal
import sys
import time
from types import SimpleNamespace
from unittest import mock

from .codexkit import FIXTURES, Scratch, alive, hansa_run, shim
from hansa import cost, views
from hansa.cli import cmd_cancel, init_desk


class Stopped(Scratch):
    def waiting(self, worker=False, fixture="progress.jsonl", **extra):
        # Checks use the same run/stop code and can exercise it on Linux without bypassing the worker guard.
        argv = self.worker_argv() if worker else ["-p", "--output-format", "json", "--max-turns", "300"]
        self.codex_record.unlink(missing_ok=True)
        p = self.start(argv, self.env(fixture, FAKE_CODEX_HANG_AFTER="1", **extra))
        self.addCleanup(self.reap, p)
        end = time.monotonic() + 20
        while time.monotonic() < end:
            try:
                rec = self.codex_called()
                if rec.get("children"):
                    p.children = [self.srt_called()["pid"], rec["pid"], *rec["children"]]
                    return p
            except (OSError, ValueError):
                pass
            time.sleep(0.01)
        self.fail("Codex did not finish replaying the fixture and start its children")

    def reap(self, p):
        if p.poll() is None:
            p.terminate()
        p.communicate(timeout=20)
        for pid in getattr(p, "children", []):
            if alive(pid):
                os.kill(pid, signal.SIGKILL)

    def answer(self, p, sig=signal.SIGTERM):
        out, err = p.communicate(timeout=20)
        self.assertEqual(p.returncode, 128 + sig, err.decode(errors="replace"))
        self.assertNotIn(b"Traceback", err)
        self.assertTrue(out.strip(), "the requested stop must emit an answer")
        run = json.loads(out)
        self.assertEqual(run["result"], "Checking the greeting.\nDetails on another line.")
        self.assertEqual((run["num_turns"], run["subtype"]), (1, "error_during_execution"))
        self.assertEqual(run["codex_thread_id"], "progress-thread")
        self.assertEqual(hansa_run.usage_of(run), {shim.DEFAULT_MODEL: {
            "in": 10, "out": 5, "cache_read": 0, "cache_write": 0, "claude_code_usd": None, "basis": ""}})
        end = time.monotonic() + 10
        while time.monotonic() < end and any(alive(pid) for pid in p.children):
            time.sleep(0.05)
        self.assertFalse(any(alive(pid) for pid in p.children), "the whole process tree must stop")
        self.assertEqual(list(self.tmp.glob("codex-*")), [], "the run's temporary folder must be removed")
        return out, err, run

    def finish_stop(self, cancel):
        p = self.waiting(worker=sys.platform == "darwin")
        desk = init_desk(self.root / "desk", repo=str(self.root / "repo"))
        settings = desk.settings()
        settings["providers"]["codex"] = {"kind": "compatible", "model": shim.DEFAULT_MODEL,
                                           "effort": "", "base_url": shim.PLACEHOLDER}
        settings["lanes"]["a"]["providers"] = ["codex"]
        self.assertEqual(desk.save_settings(settings), [])
        tid = desk.new_task("Check the greeting", lane="a")
        base = hansa_run.git("rev-parse", "HEAD", cwd=self.wt)
        desk.change(tid, lambda m: {"state": "running", "provider": "codex", "session_id": self.sid,
                                   "worktree": str(self.wt), "branch": "task/t1", "base": base,
                                   "started": time.time(), "pid": p.pid, "pid_start": hansa_run.proc_start(p.pid)})
        if cancel:
            with contextlib.redirect_stdout(io.StringIO()):
                cmd_cancel(SimpleNamespace(desk=str(desk.root), id=tid))
        else:
            desk.change(tid, lambda m: {"timed_out": True})
            hansa_run.stop_group(p.pid)  # SIGKILL after five seconds would lose the answer and change its exit
        out, err, run = self.answer(p)
        raw = desk.state_dir / "runs" / tid
        raw.mkdir(parents=True, exist_ok=True)
        (raw / "run.json").write_bytes(out)
        (raw / "run.err").write_bytes(err)
        # No Claude session record exists; all three fields must come from the shim's stdout.
        with mock.patch.object(hansa_run, "usage_from_record", side_effect=AssertionError("no Codex session")) as record:
            meta = hansa_run.finish(desk, tid, p.returncode)
        record.assert_not_called()
        self.assertEqual((meta["state"], meta["turns"], meta["exit"]),
                         ("cancelled" if cancel else "failed", 1, 143))
        self.assertEqual(meta["usage"], hansa_run.usage_of(run))
        self.assertEqual((desk.tasks_dir / tid / "result.md").read_text(), run["result"] + "\n")
        shown = views.show_text(desk, tid, full=True)
        self.assertIn("Checking the greeting.", shown)
        self.assertIn("Details on another line.", shown)
        self.assertIn("cancelled" if cancel else "stopped at the ceiling", shown)
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.root / "claude-home")}):
            report = cost.report(desk, days=None)
        self.assertEqual((report["total"]["tokens"], report["total"]["turns"]), (15, 1))
        self.assertIn("15 tokens", views.cost_text(report))

    def test_time_ceiling_keeps_report_turns_and_usage(self):
        self.finish_stop(cancel=False)

    def test_cancel_keeps_report_turns_and_usage(self):
        self.finish_stop(cancel=True)

    def test_stop_signals_keep_the_last_message_and_usage(self):
        for sig in shim.STOP_SIGNALS:
            with self.subTest(sig=sig):
                p = self.waiting(HANSA_CODEX_TEST_NO_WATCHDOG="1")
                p.send_signal(sig)
                self.answer(p, sig)

    def test_stop_drains_unread_events_without_a_final_newline(self):
        # The signal is held until Run.proc is set, before the first read of Codex's log.
        p = self.waiting(HANSA_CODEX_TEST_PAUSE=str(self.root / "started"), FAKE_CODEX_NO_LAST_NEWLINE="1")
        p.send_signal(signal.SIGTERM)
        self.answer(p)

    def test_stop_keeps_latest_cumulative_usage_and_last_nonempty_message(self):
        fixture = self.root / "cumulative.jsonl"
        fixture.write_text(json.dumps({"type": "turn.completed", "usage": {
            "input_tokens": 2, "output_tokens": 1}}) + "\n" + (FIXTURES / "progress.jsonl").read_text()
            + json.dumps({"type": "item.completed", "item": {
                "type": "agent_message", "text": ""}}) + "\n")
        p = self.waiting(fixture=fixture)
        p.send_signal(signal.SIGTERM)
        self.answer(p)  # the latest 10 input / 5 output, not the sum of the cumulative counts

    def test_stop_before_usage_keeps_message_and_zero_usage(self):
        p = self.waiting(fixture="progress_empty_message.jsonl")
        p.send_signal(signal.SIGTERM)
        out, err = p.communicate(timeout=20)
        self.assertEqual(p.returncode, 143)
        self.assertNotIn(b"Traceback", err)
        run = json.loads(out)
        self.assertEqual((run["result"], run["num_turns"]), ("Real progress note.", 2))
        self.assertEqual(hansa_run.usage_of(run), {})

    def test_stop_before_codex_says_anything_leaves_no_answer(self):
        p = self.waiting(FAKE_CODEX_HANG="1")
        p.send_signal(signal.SIGTERM)
        out, err = p.communicate(timeout=20)
        self.assertEqual((p.returncode, out), (143, b""))
        self.assertNotIn(b"Traceback", err)
