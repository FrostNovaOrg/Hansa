"""Tests for `bellhop close-task` and `bellhop settle-bell`.

Every gate gets a positive case and a negative control, because the whole point of
the command is that a task closed cleanly and a task closed halfway must stop looking
the same. A negative control here proves two things: that the run stopped at the
named step, and that the things the later steps would have written were not written.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bellhop import closetask, desk_ops, headers, merge as merge_mod, ring, state, watch  # noqa: E402
from bellhop.config import Desk  # noqa: E402
from bellhop.util import read, read_json, write  # noqa: E402

GIT_ID = ["-c", "user.name=test", "-c", "user.email=test@example.invalid"]

VERDICT = """# Acceptance 001-add-a-note-file

- duration_s: 4
- base: {base}

## Task
001 add a note file

## Criteria
- [x] the note file exists — `ls` printed it
- [x] the checks stay green — the lane check printed `board: green`

## Mutation
- broke: removed the note / the check printed `missing` / restored

## Extra findings
- none

## Doubts
- [recorded] the packet did not say where the note goes

## Hygiene
- file set equal: 1 file in the diff, 1 named in the receipt

## Recommendation
- merge

## Verdict
VERDICT: {verdict}{reason}
"""


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *GIT_ID, *args], check=True,
                          capture_output=True, text=True).stdout.strip()


class CloseTaskCase(unittest.TestCase):
    """A desk, a lane repository, and one task that has come back with a receipt."""

    def setUp(self):
        os.environ["GIT_AUTHOR_NAME"] = os.environ["GIT_COMMITTER_NAME"] = "test"
        os.environ["GIT_AUTHOR_EMAIL"] = os.environ["GIT_COMMITTER_EMAIL"] = "test@example.invalid"
        self.tmp = Path(tempfile.mkdtemp(prefix="bellhop-close-test.")).resolve()
        self.repo = self.tmp / "widget-service"
        shutil.copytree(ROOT / "examples" / "widget-service", self.repo)
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "initial")
        self.base = git(self.repo, "rev-parse", "HEAD")
        self.desk_dir = self.tmp / "desk"
        desk_ops.init_desk(self.desk_dir)
        cfg = read_json(self.desk_dir / "bellhop.json")
        cfg["lanes"]["api"]["repo"] = "../widget-service"
        cfg["lanes"]["api"]["check_cmd"] = "echo 'board: green'"
        cfg["lanes"]["api"]["worker_cmd"] = f"python3 {ROOT}/bellhop/stubworker.py"
        write(self.desk_dir / "bellhop.json", json.dumps(cfg, indent=2))
        git(self.desk_dir, "add", "bellhop.json")
        git(self.desk_dir, "commit", "-q", "-m", "configure")
        self.desk = Desk(self.desk_dir, cfg)
        self.packet_path = self.write_packet()
        ring.ring(self.desk, self.packet_path)
        self.footer = watch.watch_once(self.desk)
        self.assertFalse(self.footer["dead"], "the fixture task must come back alive")
        self.receipt = self.desk.receipts / "001-add-a-note-file.md"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- fixtures ----------------------------------------------------------
    def write_packet(self, pid="001", title="Add a note file"):
        text = (
            f"---\nid: {pid}\nlane: api\ntitle: {title}\nbase: {self.base}\nbudget_minutes: 10\n"
            "acceptance:\n  - the note file exists\n  - the checks stay green\n---\nAdd a note file.\n"
        )
        p = self.desk.inbox / "api" / f"{pid}-note.md"
        write(p, text)
        return p

    def verdict(self, verdict="pass", reason="", where="receipts", task="001"):
        text = VERDICT.format(base=self.base, verdict=verdict,
                              reason=f" — {reason}" if reason else "")
        p = (self.desk.receipts if where == "receipts" else self.desk.receipts / "shadow")
        p = p / f"{task}.acceptance.md"
        write(p, text)
        headers.write_acceptance_header(p, desk=self.desk, lane="api", task=task)
        # A synthetic verifier result with the same input identity as the real worker.
        write(p.with_name(f"{task}.acceptance.meta.json"), json.dumps({
            "ok": True, "stall": None, "base": self.base, "head": self.footer["head_after"],
            "packet_sha256": self.footer["packet_sha256"],
        }))
        return p

    def close(self, task="001", **kwargs):
        """-> (report or CloseStopped, printed lines)."""
        lines = []
        try:
            report = closetask.close_task(self.desk, task, say=lines.append, **kwargs)
        except closetask.CloseStopped as exc:
            lines.append(exc.last_line)
            return exc, lines
        return report, lines

    def record(self, task="001"):
        return read_json(self.desk.receipts / f"{task}.close.json", None)

    def desk_commits(self):
        return int(git(self.desk_dir, "rev-list", "--count", "HEAD"))

    def main_sha(self):
        return git(self.repo, "rev-parse", "main")

    def ledger_files(self):
        return sorted(p.name for p in self.desk.ledger.rglob("*.md"))

    def closings(self):
        return state.closings_rows(self.desk)

    def cli(self, *args):
        env = dict(os.environ, BELLHOP_DESK=str(self.desk_dir))
        return subprocess.run([str(ROOT / "bin" / "bellhop"), *args], env=env,
                              capture_output=True, text=True)

    def add_files_section(self, *paths):
        """Give the receipt a `## Files` section, which turns the gate into a set test."""
        body = read(self.receipt).rstrip("\n")
        write(self.receipt, body + "\n\n## Files\n\n" + "".join(f"- {p}\n" for p in paths))

    # ==================================================================
    # settle-bell: the three things done the moment the worker exits
    # ==================================================================
    def test_watchdog_settles_the_bell_when_the_worker_exits(self):
        rows = self.closings()
        self.assertEqual(len(rows), 1, rows)
        self.assertIn("[001]", rows[0])
        self.assertIn(closetask.AWAITING_LINE, rows[0])
        # the worker's own in-flight row was tagged before the watchdog took it off
        previous = sorted(state.previous_dir(self.desk).glob("in-flight.md.*"))
        self.assertTrue(previous, "settle-bell must have written through bellhop state")

    def test_the_watchdog_does_not_refresh_the_card(self):
        """The sentinel's only wake-up is a card older than the newest receipt."""
        from bellhop import sentinel
        name, ok, why = sentinel.check_card(self.desk)
        self.assertFalse(ok, "a landed receipt must leave the sentinel red")
        self.assertIn("older than the last receipt", why)
        self.assertEqual(state.read_card_keys(self.desk)["by"], "template")
        self.verdict()
        self.close()  # the hand that closes is the hand that refreshes the count
        card = state.read_card_keys(self.desk)
        self.assertIn("awaiting close", card["queue"])
        self.assertTrue(sentinel.check_card(self.desk)[1], sentinel.check_card(self.desk))

    def test_the_row_settle_bell_adds_carries_a_label_the_sentinel_reads(self):
        from bellhop import sentinel
        closetask.settle_bell(self.desk, "001", title="Add a note file")
        self.assertTrue(sentinel.check_inflight(self.desk)[1], sentinel.check_inflight(self.desk))

    def test_settle_bell_does_not_record_the_same_task_twice(self):
        closetask.settle_bell(self.desk, "001", title="Add a note file")
        rows = [r for r in self.closings() if closetask.AWAITING_LINE in r]
        self.assertEqual(len(rows), 1, rows)
        # the row the watchdog removed is put back, so the board still says who is owed a close
        inflight = closetask._inflight_rows(self.desk, "001")
        self.assertEqual(len(inflight), 1, inflight)
        self.assertIn(closetask.AWAITING_LINE, inflight[0])

    def test_settle_bell_never_stops_the_watchdog(self):
        """A card the state writer refuses is a warning here, not a lost receipt."""
        write(self.desk.path(state.CARD), "# Dispatch card\n\nqueue: 0\nqueue: 0\n")
        results = closetask.settle_bell(self.desk, "002", title="a second task", strict=False)
        self.assertTrue(any("closings" in r.note for r in results),
                        "the close-out must still be recorded when the card refuses")
        self.assertTrue(any("[002]" in r for r in state.closings_rows(self.desk)))
        with self.assertRaises(state.StateError):
            closetask.settle_bell(self.desk, "002", title="a second task", strict=True)

    # ==================================================================
    # the whole thing
    # ==================================================================
    def test_close_task_runs_every_step_in_one_commit(self):
        self.verdict()
        state.debts_add(self.desk, "verifier owed a second look at 001 by Friday")
        before_commits = self.desk_commits()
        before_main = self.main_sha()
        report, lines = self.close(next_card="ring 002", note="closed on the numbers")
        self.assertNotIsInstance(report, closetask.CloseStopped, lines)
        self.assertEqual(report.ran, list(closetask.STEPS), lines)
        self.assertTrue(lines[-1].startswith("✅ closed 001"), lines[-1])

        # merge: main moved, and it moved onto a merge commit
        self.assertNotEqual(self.main_sha(), before_main)
        self.assertIn("merge lane/api", git(self.repo, "log", "-1", "--format=%s", "main"))
        self.assertNotIn(".merge-api", git(self.repo, "worktree", "list"))

        # ledger: one entry, carrying the numbers and the note
        entries = self.ledger_files()
        self.assertEqual(len(entries), 1, entries)
        entry = read(sorted(self.desk.ledger.rglob("*.md"))[0])
        self.assertIn("closed 001 Add a note file", entry)
        self.assertIn("closed on the numbers", entry)
        self.assertIn(report.merged_sha, entry)
        self.assertIn("verdict: pass", entry)
        self.assertIn("verifier: 4s", entry)

        # state: debt settled, card refreshed, close-out recorded
        self.assertIn("- none", read(self.desk.path(state.DEBTS)))
        self.assertIn("second look at 001", read(self.desk.path(state.DEBTS_SETTLED)))
        card = state.read_card_keys(self.desk)
        self.assertEqual(card["next"], "ring 002")
        self.assertTrue(card["queue"].startswith("0 "), card["queue"])
        closed = [r for r in self.closings() if "| closed " in r]
        self.assertEqual(len(closed), 1, self.closings())

        # archive: one commit for the whole close, carrying every file it touched
        self.assertEqual(self.desk_commits(), before_commits + 1)
        touched = git(self.desk_dir, "show", "--pretty=format:", "--name-only", "HEAD").split()
        for name in ("receipts/001.close.json", "state/next.md", "state/closings.md",
                     "state/debts.md", "state/debts-settled.md", "receipts/001.acceptance.md"):
            self.assertIn(name, touched, touched)
        self.assertTrue(any(t.startswith("ledger/") for t in touched), touched)

        # the record says how far it got
        record = self.record()
        self.assertTrue(record["closed"])
        self.assertEqual([s["step"] for s in record["steps"]], list(closetask.STEPS))
        self.assertIsNone(record["stopped_at"])

    def test_rerunning_a_closed_task_writes_nothing_new(self):
        self.verdict()
        self.close()
        commits, entries, rows = self.desk_commits(), self.ledger_files(), len(self.closings())
        report, lines = self.close()
        self.assertNotIsInstance(report, closetask.CloseStopped, lines)
        self.assertEqual([s for s in report.ran if s not in closetask.GATES], [])
        self.assertIn("already closed", lines[-1])
        self.assertEqual(self.desk_commits(), commits)
        self.assertEqual(self.ledger_files(), entries)
        self.assertEqual(len(self.closings()), rows)

    def test_dry_run_writes_nothing_at_all(self):
        self.verdict()
        before = git(self.desk_dir, "status", "--porcelain")
        before_main, commits = self.main_sha(), self.desk_commits()
        report, lines = self.close(dry_run=True, next_card="ring 002")
        self.assertNotIsInstance(report, closetask.CloseStopped, lines)
        self.assertTrue(any("would merge" in l for l in lines), lines)
        self.assertTrue(lines[-1].startswith("✅ dry run"), lines[-1])
        self.assertIsNone(self.record())
        self.assertEqual(git(self.desk_dir, "status", "--porcelain"), before)
        self.assertEqual(self.main_sha(), before_main)
        self.assertEqual(self.desk_commits(), commits)
        self.assertEqual(self.ledger_files(), [])

    # ==================================================================
    # 1. evidence
    # ==================================================================
    def test_stalled_acceptance_metadata_blocks_an_old_passing_file(self):
        self.verdict()
        before = self.main_sha()
        meta = self.desk.receipts / "001.acceptance.meta.json"
        write(meta, json.dumps({"ok": False, "stall": "timeout", "head": self.footer["head_after"],
                               "base": self.base}))
        report, lines = self.close()
        self.assertIsInstance(report, closetask.CloseStopped)
        self.assertIn("incomplete or stalled", lines[-1])
        self.assertEqual(self.main_sha(), before)

    def test_acceptance_for_another_head_does_not_close(self):
        self.verdict()
        before = self.main_sha()
        meta = self.desk.receipts / "001.acceptance.meta.json"
        write(meta, json.dumps({"ok": True, "stall": None, "head": self.base, "base": self.base,
                               "packet_sha256": self.footer["packet_sha256"]}))
        report, lines = self.close()
        self.assertIsInstance(report, closetask.CloseStopped)
        self.assertIn("base/head does not match", lines[-1])
        self.assertEqual(self.main_sha(), before)

    def test_missing_verdict_stops_with_a_hint(self):
        stopped, lines = self.close()
        self.assertIsInstance(stopped, closetask.CloseStopped)
        self.assertEqual(stopped.step, "evidence")
        self.assertEqual(stopped.code, closetask.EXIT_ENVIRONMENT)
        self.assertIn("bellhop accept 001", stopped.reason)
        self.assertEqual(lines[-1], "🛑 stopped at: evidence — " + stopped.reason)
        self.assertEqual(self.ledger_files(), [])
        self.assertEqual(self.record()["stopped_at"], "evidence")

    def test_changed_packet_cannot_reuse_an_acceptance_for_the_same_head(self):
        self.verdict()
        before = self.main_sha()
        original = read(self.packet_path)
        write(self.packet_path, original.replace("the note file exists", "any file exists"))
        stopped, _ = self.close()
        self.assertIn("packet provenance", stopped.reason)
        self.assertEqual(self.main_sha(), before)
        self.assertEqual(self.ledger_files(), [])
        write(self.packet_path, original)
        report, _ = self.close()
        self.assertNotIsInstance(report, closetask.CloseStopped)
        self.assertEqual(self.record()["packet_sha256"], self.footer["packet_sha256"])

    def test_unbound_and_stale_acceptance_records_cannot_close(self):
        self.verdict()
        meta = self.desk.receipts / "001.acceptance.meta.json"
        original = read(meta)
        before = self.main_sha()
        for value in (None, {}, {"packet_sha256": "0" * 64}):
            if value is None:
                meta.unlink()
            else:
                data = json.loads(original)
                data.pop("packet_sha256")
                data.update(value)
                write(meta, json.dumps(data))
            stopped, _ = self.close()
            self.assertIn("packet binding", stopped.reason)
            self.assertEqual(self.main_sha(), before)
            self.assertEqual(self.ledger_files(), [])
        write(meta, original)
        report, _ = self.close()
        self.assertNotIsInstance(report, closetask.CloseStopped)

    def test_repeated_close_still_checks_packet_provenance(self):
        self.verdict()
        self.close()
        before, ledger = self.main_sha(), self.ledger_files()
        write(self.packet_path, read(self.packet_path) + "\nNew requirement.\n")
        stopped, _ = self.close()
        self.assertIn("packet provenance", stopped.reason)
        self.assertEqual(self.main_sha(), before)
        self.assertEqual(self.ledger_files(), ledger)

    def test_a_return_verdict_is_recorded_and_does_not_close(self):
        self.verdict(verdict="return", reason="the note is in the wrong directory")
        before_main = self.main_sha()
        stopped, lines = self.close()
        self.assertIsInstance(stopped, closetask.CloseStopped)
        self.assertEqual(stopped.step, "evidence")
        self.assertEqual(stopped.code, closetask.EXIT_REFUSED)
        rows = [r for r in self.closings() if "| return | not closed" in r]
        self.assertEqual(len(rows), 1, self.closings())
        self.assertEqual(self.main_sha(), before_main)
        self.assertEqual(self.ledger_files(), [])
        # and again: the register is append-only, so it must not say it twice
        self.close()
        self.assertEqual(len([r for r in self.closings() if "| return | not closed" in r]), 1)

    def test_a_doubt_verdict_does_not_close_either(self):
        self.verdict(verdict="doubt", reason="the second criterion was never run")
        stopped, _lines = self.close()
        self.assertEqual(stopped.code, closetask.EXIT_REFUSED)
        self.assertTrue(any("| doubt | not closed" in r for r in self.closings()))

    def test_a_shadow_verdict_closes_nothing_until_the_desk_says_so(self):
        self.verdict(where="shadow")
        stopped, _lines = self.close()
        self.assertEqual(stopped.step, "evidence")
        self.assertEqual(stopped.code, closetask.EXIT_REFUSED)
        self.assertIn("accept_from_shadow", stopped.reason)
        self.desk.cfg["accept_from_shadow"] = True
        report, lines = self.close()
        self.assertNotIsInstance(report, closetask.CloseStopped, lines)
        self.assertTrue(any("from shadow" in l for l in lines), lines)

    def test_a_receipt_that_fails_check_receipt_stops(self):
        self.verdict()
        write(self.receipt, headers.strip_header(read(self.receipt), headers.FENCE_RECEIPT))
        stopped, _lines = self.close()
        self.assertEqual(stopped.step, "evidence")
        self.assertEqual(stopped.code, closetask.EXIT_STOPPED)
        self.assertIn("check-receipt is red", stopped.reason)
        self.assertEqual(self.ledger_files(), [])

    def test_a_verdict_that_fails_check_acceptance_stops(self):
        path = self.verdict()
        body = headers.strip_header(read(path), headers.FENCE_ACCEPTANCE)
        write(path, body.replace("- broke: removed the note / the check printed `missing` / restored",
                                 "- looked at it"))
        headers.write_acceptance_header(path, desk=self.desk, lane="api", task="001")
        stopped, _lines = self.close()
        self.assertEqual(stopped.step, "evidence")
        self.assertIn("check-acceptance is red", stopped.reason)
        self.assertIn("mutation", stopped.reason)

    # ==================================================================
    # 2. fileset
    # ==================================================================
    def test_the_files_section_must_equal_the_branch(self):
        self.verdict()
        self.add_files_section("bellhop-stub-001.txt")
        report, lines = self.close()
        self.assertNotIsInstance(report, closetask.CloseStopped, lines)
        self.assertTrue(any("`## Files` section" in l for l in lines), lines)

    def test_a_files_section_that_names_the_wrong_file_stops(self):
        self.verdict()
        self.add_files_section("widget/__init__.py")
        stopped, lines = self.close()
        self.assertEqual(stopped.step, "fileset")
        printed = "\n".join(lines)
        self.assertIn("only on the branch: bellhop-stub-001.txt", printed)
        self.assertIn("only in the receipt: widget/__init__.py", printed)
        self.assertEqual(self.ledger_files(), [])

    def test_without_a_files_section_the_count_is_the_gate(self):
        self.verdict()
        report, lines = self.close()
        self.assertNotIsInstance(report, closetask.CloseStopped, lines)
        self.assertTrue(any("files_changed" in l for l in lines), lines)

    def test_a_count_that_disagrees_with_the_branch_stops(self):
        self.verdict()
        text = read(self.receipt)
        write(self.receipt, text.replace("files_changed: 1", "files_changed: 4"))
        stopped, _lines = self.close()
        self.assertEqual(stopped.step, "fileset")
        self.assertIn("the receipt header says 4", stopped.reason)
        self.assertEqual(self.ledger_files(), [])

    def test_a_branch_that_moved_after_the_receipt_stops(self):
        self.verdict()
        wt = Path(self.desk.lane("api")["worktree"])
        write(wt / "afterthought.txt", "not in any receipt\n")
        git(wt, "add", "afterthought.txt")
        git(wt, "commit", "-q", "-m", "one more, after the receipt")
        stopped, _lines = self.close()
        self.assertEqual(stopped.step, "fileset")
        self.assertIn("has moved since the receipt", stopped.reason)
        self.assertEqual(self.ledger_files(), [])

    # ==================================================================
    # 3. merge
    # ==================================================================
    def test_the_full_board_runs_on_the_merged_result(self):
        """The check sees the merged tree: it looks for a file only the merge brings."""
        self.verdict()
        self.desk.cfg["lanes"]["api"]["check_cmd"] = "test -f bellhop-stub-001.txt && echo 'board: green'"
        report, lines = self.close()
        self.assertNotIsInstance(report, closetask.CloseStopped, lines)
        self.assertTrue(any("full board exit 0" in l for l in lines), lines)

    def test_a_red_full_board_leaves_the_base_branch_untouched(self):
        self.verdict()
        before_main, commits = self.main_sha(), self.desk_commits()
        self.desk.cfg["lanes"]["api"]["check_cmd"] = "test ! -f bellhop-stub-001.txt"
        stopped, lines = self.close()
        self.assertEqual(stopped.step, "merge")
        self.assertEqual(stopped.code, closetask.EXIT_STOPPED)
        self.assertIn("full board failed", stopped.reason)
        self.assertEqual(self.main_sha(), before_main, "the base branch must not have moved")
        self.assertEqual(git(self.repo, "log", "-1", "--format=%s", "main"), "initial")
        self.assertNotIn(".merge-api", git(self.repo, "worktree", "list"))
        self.assertFalse(git(self.repo, "status", "--porcelain"))
        self.assertEqual(self.ledger_files(), [])
        self.assertEqual(self.desk_commits(), commits)
        record = self.record()
        self.assertEqual(record["stopped_at"], "merge")
        self.assertEqual([s["step"] for s in record["steps"]], ["evidence", "fileset"])
        self.assertFalse(record["closed"])

    def test_a_rerun_picks_up_at_the_step_that_stopped(self):
        self.verdict()
        self.desk.cfg["lanes"]["api"]["check_cmd"] = "test ! -f bellhop-stub-001.txt"
        self.close()
        self.desk.cfg["lanes"]["api"]["check_cmd"] = "echo 'board: green'"
        commits = self.desk_commits()
        report, lines = self.close()
        self.assertNotIsInstance(report, closetask.CloseStopped, lines)
        self.assertEqual([s for s in report.ran if s not in closetask.GATES],
                         ["merge", "ledger", "state", "archive"])
        self.assertEqual(self.desk_commits(), commits + 1)
        self.assertEqual(len(self.ledger_files()), 1)

    def test_merging_a_second_time_is_not_a_second_merge(self):
        """`merge_checked` on a branch already in the base branch reports it, and still checks."""
        self.verdict()
        first = merge_mod.merge_checked(self.desk, "001")
        self.assertFalse(first.already)
        again = merge_mod.merge_checked(self.desk, "001")
        self.assertTrue(again.already)
        self.assertEqual(first.sha, again.sha)
        self.assertEqual(again.check_exit, "0")

    # ==================================================================
    # 5. state
    # ==================================================================
    def test_two_in_flight_rows_for_one_task_stop_the_state_step(self):
        self.verdict()
        state.inflight_start(self.desk, "001 one row")
        state.inflight_start(self.desk, "001 another row")
        stopped, _lines = self.close()
        self.assertEqual(stopped.step, "state")
        self.assertEqual(stopped.code, closetask.EXIT_REFUSED)
        self.assertIn("2 in-flight rows", stopped.reason)
        # the merge and the ledger did run, and the record says exactly that
        record = self.record()
        self.assertEqual([s["step"] for s in record["steps"]], ["evidence", "fileset", "merge", "ledger"])
        self.assertEqual(len(self.ledger_files()), 1)
        # and the rerun, with the board tidied, still quotes the sha the merge produced
        path = self.desk.path(state.INFLIGHT)
        write(path, read(path).replace("- 001 another row\n", ""))
        state.inflight_finish(self.desk, "001")
        report, lines = self.close()
        self.assertNotIsInstance(report, closetask.CloseStopped, lines)
        self.assertEqual(report.merged_sha, record["merged_sha"])
        self.assertTrue(any(f"| closed {record['merged_sha']} |" in r for r in self.closings()),
                        self.closings())

    def test_every_debt_carrying_the_id_is_settled(self):
        self.verdict()
        state.debts_add(self.desk, "verifier owed a second look at 001")
        state.debts_add(self.desk, "principal owed the numbers for 001")
        state.debts_add(self.desk, "someone owed something about 002")
        report, lines = self.close()
        self.assertNotIsInstance(report, closetask.CloseStopped, lines)
        settled = read(self.desk.path(state.DEBTS_SETTLED))
        self.assertIn("second look at 001", settled)
        self.assertIn("the numbers for 001", settled)
        self.assertNotIn("002", settled)
        self.assertIn("about 002", read(self.desk.path(state.DEBTS)))

    def test_the_card_keeps_its_own_ready_flag(self):
        """Whether the desk can dispatch is the coordinator's judgement, not this command's."""
        self.verdict()
        before = state.read_card_keys(self.desk)
        self.close(next_card="ring 002")
        after = state.read_card_keys(self.desk)
        self.assertEqual(after["ready"], before["ready"])
        self.assertEqual(after["reason"], before["reason"])
        self.assertEqual(after["next"], "ring 002")

    # ==================================================================
    # the command line
    # ==================================================================
    def test_cli_prints_the_stop_line_and_carries_the_exit_code(self):
        out = self.cli("close-task", "001")
        self.assertEqual(out.returncode, closetask.EXIT_ENVIRONMENT, out.stdout + out.stderr)
        last = out.stdout.strip().splitlines()[-1]
        self.assertTrue(last.startswith("🛑 stopped at: evidence — "), last)
        self.assertIn("bellhop accept 001", last)

    def test_cli_closes_and_settles(self):
        self.verdict()
        out = self.cli("close-task", "001", "--next", "ring 002", "--note", "took it on the numbers")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("✅ closed 001", out.stdout)
        out = self.cli("settle-bell", "001", "--title", "Add a note file")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)

    def test_cli_dry_run_leaves_the_desk_where_it_was(self):
        self.verdict()
        before = git(self.desk_dir, "status", "--porcelain")
        out = self.cli("close-task", "001", "--dry-run")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("would", out.stdout)
        self.assertEqual(git(self.desk_dir, "status", "--porcelain"), before)


class ReceiptFilesTest(unittest.TestCase):
    """The `## Files` reader, on its own: one path per line, prose ignored."""

    def test_no_section_is_not_an_empty_section(self):
        self.assertIsNone(closetask.receipt_files("# Receipt\n\n## Doubts\n\n- none\n"))

    def test_paths_are_read_and_prose_is_not(self):
        body = ("## Files\n\n- `a/one.py`\n* b/two.py\nc/three.py\n"
                "and one sentence about all three\n\n## Doubts\n\n- none\n")
        self.assertEqual(closetask.receipt_files(body), ["a/one.py", "b/two.py", "c/three.py"])

    def test_none_means_no_files(self):
        self.assertEqual(closetask.receipt_files("## Files\n\n- none\n"), [])


if __name__ == "__main__":
    unittest.main()
