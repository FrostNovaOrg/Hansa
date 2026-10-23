"""Progress shared with hansa, using Codex's events rather than a Claude Code session record."""
import json
import os
import time
import unittest
from unittest import mock

from .codexkit import Scratch, shim


class WorkerProgress(Scratch):
    def test_worker_events_write_progress(self):
        began = time.time()
        run, code = self.run_worker("progress.jsonl")
        self.assertEqual((code, run["num_turns"]), (0, 1))
        folder = self.root / "claude-home" / "progress"
        self.assertEqual([p.name for p in folder.iterdir()], [f"{self.sid}.json"])
        progress = json.loads((folder / f"{self.sid}.json").read_text())
        self.assertEqual(set(progress), {"replies", "last", "note"})
        self.assertEqual((progress["replies"], progress["note"]), (1, "Checking the greeting."))
        self.assertGreaterEqual(progress["last"], began)
        self.assertLessEqual(progress["last"], time.time())

    def test_empty_agent_message_keeps_previous_answer_and_progress_note(self):
        run, code = self.run_worker("progress_empty_message.jsonl", argv=self.worker_argv(max_turns=1))
        self.assertEqual((code, run["subtype"], run["num_turns"]), (1, "error_max_turns", 2))
        progress = json.loads((self.root / "claude-home" / "progress" / f"{self.sid}.json").read_text())
        self.assertEqual(progress["note"], "Real progress note.")
        self.assertEqual(run["result"], "Real progress note.")

    def test_progress_note_strips_message_before_taking_first_line(self):
        run, code = self.run_worker("progress_blank_line.jsonl")
        self.assertEqual((code, run["subtype"]), (0, "success"))
        progress = json.loads((self.root / "claude-home" / "progress" / f"{self.sid}.json").read_text())
        self.assertEqual(progress["note"], "Checking tests.")

    def test_progress_write_failure_does_not_fail_worker(self):
        (self.root / "claude-home").write_text("not a directory")
        run, code = self.run_worker("progress.jsonl")
        self.assertEqual((code, run["subtype"], run["num_turns"]), (0, "success", 1))


class ProgressWrites(Scratch):
    def test_progress_changes_and_ten_second_activity_updates_are_atomic(self):
        home = self.root / "claude-home"
        path = home / "progress" / f"{self.sid}.json"
        progress = shim.Progress(str(home), self.sid)
        self.assertFalse(path.exists())  # no events yet
        writes = []
        replace = os.replace

        def replaced(src, dst):
            self.assertEqual(src.parent, dst.parent)
            writes.append(json.loads(src.read_text()))  # complete before it becomes visible to hansa
            if dst.exists():
                self.assertEqual(json.loads(dst.read_text()), writes[-2])
            replace(src, dst)

        with mock.patch.object(shim.time, "time", side_effect=[100, 101, 102, 103, 112, 113, 114, 115]), \
                mock.patch.object(shim.os, "replace", side_effect=replaced):
            for replies, note in ((0, ""), (0, ""), (1, ""), (1, "Reading\nDetails"),
                                  (1, "Reading\nOther details"), (1, "Reading"), (2, "Reading"), (2, "")):
                progress.event(replies, note)
        self.assertEqual(writes, [
            {"replies": 0, "last": 100, "note": ""},
            {"replies": 1, "last": 102, "note": ""},
            {"replies": 1, "last": 103, "note": "Reading"},
            {"replies": 1, "last": 113, "note": "Reading"},
            {"replies": 2, "last": 114, "note": "Reading"},
            {"replies": 2, "last": 115, "note": ""}])
        self.assertEqual(list(path.parent.iterdir()), [path])

    def test_failed_progress_replace_keeps_previous_file_and_removes_temporary(self):
        home = self.root / "claude-home"
        path = home / "progress" / f"{self.sid}.json"
        progress = shim.Progress(str(home), self.sid)
        progress.event(0, "")
        previous = path.read_text()
        with mock.patch.object(shim.os, "replace", side_effect=OSError("disk full")):
            progress.event(1, "Reading")
        self.assertEqual(path.read_text(), previous)
        self.assertEqual(list(path.parent.iterdir()), [path])
        progress.event(2, "Testing")
        self.assertEqual(json.loads(path.read_text())["note"], "Testing")


if __name__ == "__main__":
    unittest.main()
