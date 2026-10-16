"""Codex's stdout: a regular file the shim follows, so that a non-blocking stdout and a long line do not break
codex's writes, and lines that arrive in parts are read whole."""
import json

from .codexkit import FIXTURES, Scratch, shim

BIG = 1024 * 1024


class Output(Scratch):
    def log_lines(self):
        return (self.logs / f"{self.sid}.jsonl").read_text().splitlines()

    def test_non_blocking_stdout_and_a_long_line(self):
        # codex's stdout set O_NONBLOCK, then a line of about 1 MB, more items and turn.completed: a pipe fills
        # and the write fails with EAGAIN; a file takes it all
        run, code = self.run_worker("success.jsonl", FAKE_CODEX_NONBLOCK="1", FAKE_CODEX_BIG=str(BIG))
        self.assertNotIn("panicked", (self.logs / f"{self.sid}.err").read_text())
        self.assertEqual((code, run["subtype"], run["is_error"]), (0, "success", False))
        self.assertEqual(run["num_turns"], 4)  # the long line's command, two more and a file change
        self.assertEqual(run["result"], "Added hello.txt with a greeting; checked it with cat. Nothing left undone.")
        lines = self.log_lines()
        want = (FIXTURES / "success.jsonl").read_text().splitlines()
        self.assertEqual(len(lines), len(want) + 1)
        self.assertEqual([lines[0], *lines[2:]], want)
        big = json.loads(lines[1])
        self.assertEqual((big["item"]["id"], len(big["item"]["aggregated_output"])), ("item_big", BIG))

    def test_lines_arriving_in_two_parts(self):
        # each line's first half, a pause longer than the shim's poll, then the rest
        run, code = self.run_worker("success.jsonl", FAKE_CODEX_SPLIT="0.3")
        self.assertEqual((code, run["subtype"], run["num_turns"]), (0, "success", 3))
        self.assertEqual(run["codex_thread_id"], "0199f3a2-7c1e-7d40-9b6a-5c2f0e8d4a11")
        self.assertEqual(run["modelUsage"][shim.DEFAULT_MODEL]["outputTokens"], 122)
        self.assertEqual(self.log_lines(), (FIXTURES / "success.jsonl").read_text().splitlines())

    def test_last_line_without_newline(self):
        # turn.completed, complete JSON but with no newline when codex ends, is still read
        run, code = self.run_worker("success.jsonl", FAKE_CODEX_NO_LAST_NEWLINE="1")
        self.assertEqual((code, run["subtype"]), (0, "success"))
        self.assertEqual(run["modelUsage"][shim.DEFAULT_MODEL]["outputTokens"], 122)
