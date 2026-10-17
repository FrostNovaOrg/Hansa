"""A run killed from outside (the system's out-of-memory killer, say) leaves no answer and a killed process's exit,
as a killed claude does, so that hansa runs it again; the shim's own stops and codex's own exits answer as before."""
import json
import os
import signal
import time

from .codexkit import Scratch, alive


class Killed(Scratch):
    def run_killed(self, **extra):
        p = self.start(self.worker_argv(), self.env("many_items.jsonl", **extra))
        out, err = p.communicate(timeout=60)
        self.err = err.decode(errors="replace")
        return out.decode(errors="replace"), p.returncode

    def wait_gone(self, pids, seconds=5):
        end = time.time() + seconds
        while time.time() < end and any(alive(x) for x in pids):
            time.sleep(0.1)
        left = [x for x in pids if alive(x)]
        for pid in left:
            os.kill(pid, signal.SIGKILL)
        return left

    def assert_no_answer(self, out):
        self.assertNotIn('"type": "result"', out, f"no answer on stdout; stderr: {self.err}")
        self.assertEqual(out.strip(), "")

    def test_codex_killed_mid_turn(self):
        out, code = self.run_killed(FAKE_CODEX_KILL=str(int(signal.SIGKILL)))
        self.assert_no_answer(out)
        self.assertEqual(code, 128 + signal.SIGKILL)
        self.assertIn("SIGKILL", self.err)
        self.assertEqual(list(self.tmp.glob("codex-*")), [], "the run's own folder is removed")
        rec = self.codex_called()
        self.assertEqual(self.wait_gone([self.srt_called()["pid"], rec["pid"], *rec["children"]]), [],
                         "srt, codex and the child it left")

    def test_codex_terminated_from_outside(self):
        out, code = self.run_killed(FAKE_CODEX_KILL=str(int(signal.SIGTERM)))
        self.assert_no_answer(out)
        self.assertEqual(code, 128 + signal.SIGTERM)
        self.assertEqual(self.wait_gone(self.codex_called()["children"]), [])

    def test_srt_itself_killed(self):
        out, code = self.run_killed(FAKE_SRT_KILL=str(int(signal.SIGKILL)), FAKE_CODEX_HANG="1")
        self.assert_no_answer(out)
        self.assertEqual(code, 128 + signal.SIGKILL)
        self.assertEqual(list(self.tmp.glob("codex-*")), [])
        rec = self.codex_called()
        self.assertEqual(self.wait_gone([rec["pid"], *rec["children"][:2]]), [], "codex and its children in srt's group")


class NotKilled(Scratch):
    """The shim's own stops and codex's own exits: answered as before (the turn limit, TERM to the shim: also in
    test_processes)."""

    def test_turn_limit(self):
        run, code = self.run_worker("many_items.jsonl", argv=self.worker_argv(max_turns=3), FAKE_CODEX_DELAY="0.1")
        self.assertEqual((code, run["subtype"]), (1, "error_max_turns"))

    def test_term_to_the_shim(self):
        p = self.start(self.worker_argv(), self.env(FAKE_CODEX_HANG="1"))
        rec, end = {}, time.time() + 20
        while time.time() < end and not rec.get("children"):
            try:
                rec = json.loads(self.codex_record.read_text())
            except (OSError, ValueError):
                time.sleep(0.1)
        self.assertTrue(rec.get("children"), "the fake codex did not start")
        os.kill(p.pid, signal.SIGTERM)
        out, _ = p.communicate(timeout=20)
        self.assertEqual(p.returncode, 128 + signal.SIGTERM)
        self.assertEqual(out.strip(), b"")
        self.assertEqual(Killed.wait_gone(self, [rec["pid"], *rec["children"]]), [])

    def test_codex_exits_1_by_itself(self):
        run, code = self.run_worker("strict_config.jsonl")
        self.assertEqual((code, run["subtype"]), (1, "error_during_execution"))
        self.assertIn("refused its configuration", run["result"])

    def test_success(self):
        run, code = self.run_worker()
        self.assertEqual((code, run["subtype"], run["is_error"]), (0, "success", False))
