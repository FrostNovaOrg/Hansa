"""Codex's stdout: a regular file the shim follows, so that a non-blocking stdout and a long line do not break
codex's writes, and lines that arrive in parts are read whole."""
import json
from subprocess import TimeoutExpired

from .codexkit import Scratch, shim
from hansa.views import REPORT_CHARS

BIG = 1024 * 1024


class Output(Scratch):
    def test_output_files_live_only_for_the_run(self):
        run, code = self.run_worker()
        self.assertEqual((code, run["subtype"]), (0, "success"))
        for stream in self.codex_called()["streams"].values():
            self.assertEqual(stream, {"regular": True, "mode": 0o600, "links": 0})
        self.assertFalse(self.logs.exists())

    def test_non_blocking_stdout_and_a_long_line(self):
        # codex's stdout set O_NONBLOCK, then a line of about 1 MB, more items and turn.completed: a pipe fills
        # and the write fails with EAGAIN; a file takes it all
        run, code = self.run_worker("success.jsonl", FAKE_CODEX_NONBLOCK="1", FAKE_CODEX_BIG=str(BIG))
        self.assertEqual((code, run["subtype"], run["is_error"]), (0, "success", False))
        self.assertEqual(run["num_turns"], 4)  # the long line's command, two more and a file change
        self.assertIn("Added hello.txt", run["result"])

    def test_lines_arriving_in_two_parts(self):
        # each line's first half, a pause longer than the shim's poll, then the rest
        run, code = self.run_worker("success.jsonl", FAKE_CODEX_SPLIT="0.3")
        self.assertEqual((code, run["subtype"], run["num_turns"]), (0, "success", 3))
        self.assertEqual(run["codex_thread_id"], "0199f3a2-7c1e-7d40-9b6a-5c2f0e8d4a11")
        self.assertEqual(run["modelUsage"][shim.DEFAULT_MODEL]["outputTokens"], 122)

    def test_last_line_without_newline(self):
        # turn.completed, complete JSON but with no newline when codex ends, is still read
        run, code = self.run_worker("success.jsonl", FAKE_CODEX_NO_LAST_NEWLINE="1")
        self.assertEqual((code, run["subtype"]), (0, "success"))
        self.assertEqual(run["modelUsage"][shim.DEFAULT_MODEL]["outputTokens"], 122)

    def test_failure_before_any_message_preserves_the_error(self):
        for entry in ("", "empty", "invalid UTF-8"):
            with self.subTest(old_report=entry):
                run, code = self.run_worker("server_error.jsonl", FAKE_CODEX_OLD_REPORT=entry)
                self.assertEqual((code, run["subtype"], run["api_error_status"]), (1, "error_during_execution", 503))
                self.assertIn("503 Service Unavailable", run["result"])

    def test_long_agent_message_is_truncated(self):
        fixture = self.root / "long-message.jsonl"
        message = "Report begins.\n" + "长" * REPORT_CHARS + "\nEnd of oversized report."
        events = [{"type": "item.completed", "item": {"type": "agent_message", "text": message}},
                  {"type": "turn.completed"}]
        fixture.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
        run, code = self.run_worker(fixture)
        self.assertEqual((code, run["subtype"]), (0, "success"))
        kept, _, note = run["result"].rpartition("\n\n")
        self.assertIn("truncated", note.lower()[:80])
        self.assertLessEqual(len(run["result"]) + 1, REPORT_CHARS)
        self.assertNotIn("End of oversized report", run["result"])
        self.assertTrue(message.startswith(kept) and kept.startswith("Report begins.\n长"))

    def test_old_report_entries_are_ignored(self):
        target = self.secret / "outside-worker-tmp.txt"
        outside = "Synthetic text outside the worker's temporary folder."
        target.write_text(outside, encoding="utf-8")
        for entry in ("symlink", "directory", "fifo", "large", "invalid UTF-8"):
            with self.subTest(old_report=entry):
                p = self.start(self.worker_argv(), self.env(FAKE_CODEX_OLD_REPORT=entry,
                                                           FAKE_CODEX_REPORT_TARGET=str(target)))
                try:
                    out, err = p.communicate(timeout=20)
                except TimeoutExpired:
                    raise self.failureException(f"adapter blocked with {entry} at the former report path") from None
                finally:
                    if p.poll() is None:
                        p.kill()
                        p.communicate(timeout=10)
                self.assertEqual(p.returncode, 0, err.decode(errors="replace"))
                report = json.loads(out)["result"]
                self.assertNotIn(outside.encode(), out)
                self.assertTrue(report.startswith("Added hello.txt"), report[:40])
                self.assertEqual(target.read_text(encoding="utf-8"), outside)
