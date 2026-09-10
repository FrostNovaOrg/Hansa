"""Tests for the context, cost and chain readings.

Every metric gets a positive case and a negative control, because the failure mode
these readings exist to prevent is a number that looks computed and is not: a desk
with no transcripts and a desk whose turns were all small must not print the same
thing, and a transcript with no usage fields must yield `n/a` rather than a zero or
a traceback.

The fixtures are small synthetic transcripts written by the test. TZ is pinned to
UTC for the duration, because a turn's day is a local calendar day and the test
asserts which day a turn landed on.
"""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bellhop import headers, readings  # noqa: E402
from bellhop import transcripts as tr  # noqa: E402
from bellhop.config import Desk, price_table  # noqa: E402
from bellhop.util import read, write, write_json  # noqa: E402

OPUS = "claude-opus-5-20260501"
DAY = "2026-09-08"          # a Tuesday, inside 2026-W37
WEEK = "2026-W37"


# -- fixture builders ------------------------------------------------------

def usage(inp=0, cache_read=0, cache_write=0, out=0):
    return {"input_tokens": inp, "cache_read_input_tokens": cache_read,
            "cache_creation_input_tokens": cache_write, "output_tokens": out}


def assistant(stamp, mid, model=OPUS, use=None, blocks=(), compact=False):
    entry = {"type": "assistant", "timestamp": stamp,
             "message": {"id": mid, "model": model, "content": list(blocks)}}
    if use is not None:
        entry["message"]["usage"] = use
    if compact:
        entry["isCompactSummary"] = True
    return entry


def user(stamp, blocks, meta=False):
    entry = {"type": "user", "timestamp": stamp, "message": {"content": list(blocks)}}
    if meta:
        entry["isMeta"] = True
    return entry


def tool_use(uid, name, payload):
    return {"type": "tool_use", "id": uid, "name": name, "input": payload}


def tool_result(uid, content):
    return {"type": "tool_result", "tool_use_id": uid, "content": content}


def text(body):
    return {"type": "text", "text": body}


def at(hhmm, day=DAY):
    return f"{day}T{hhmm}:00.000Z"


class TZCase(unittest.TestCase):
    """A fixed clock zone: a day is a local day, so the test has to pin the zone."""

    def setUp(self):
        self.tz = os.environ.get("TZ")
        os.environ["TZ"] = "UTC"
        time.tzset()
        self.tmp = Path(tempfile.mkdtemp(prefix="bellhop-readings-test."))

    def tearDown(self):
        if self.tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = self.tz
        time.tzset()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def transcript(self, name, entries, raw_lines=()):
        p = self.tmp / name
        with p.open("w", encoding="utf-8") as fh:
            for e in entries:
                fh.write(json.dumps(e, ensure_ascii=False) + "\n")
            for line in raw_lines:
                fh.write(line + "\n")
        return p


# -- the parser ------------------------------------------------------------

class TranscriptTests(TZCase):

    def test_usage_is_counted_once_per_message_id(self):
        """Three lines, one message: one turn, and output tokens not tripled."""
        u = usage(inp=10, cache_read=100_000, out=50)
        p = self.transcript("a.jsonl", [
            assistant(at("09:00"), "msg-1", use=u, blocks=[text("one")]),
            assistant(at("09:00"), "msg-1", use=u, blocks=[tool_use("t1", "Bash", {"command": "ls"})]),
            assistant(at("09:00"), "msg-1", use=u, blocks=[text("two")]),
        ])
        parsed = tr.parse([p])
        self.assertEqual(len(parsed.turns), 1)
        self.assertEqual(parsed.turns[0].output_tokens, 50)
        self.assertEqual(parsed.turns[0].tools, ("Bash",))

    def test_context_is_the_three_input_fields(self):
        p = self.transcript("a.jsonl", [
            assistant(at("09:00"), "m1", use=usage(inp=1_000, cache_read=200_000, cache_write=50_000, out=7)),
        ])
        turn = tr.parse([p]).turns[0]
        self.assertEqual(turn.context, 251_000)
        self.assertEqual(turn.day, DAY)

    def test_tail_share_of_turns_and_of_cost(self):
        """Two long turns and two short ones, priced so the cost share is checkable."""
        p = self.transcript("a.jsonl", [
            assistant(at("09:00"), "m1", use=usage(cache_read=300_000)),
            assistant(at("09:01"), "m2", use=usage(cache_read=300_000)),
            assistant(at("09:02"), "m3", use=usage(cache_read=100_000)),
            assistant(at("09:03"), "m4", use=usage(cache_read=100_000)),
        ])
        r = tr.overall(tr.parse([p]))
        self.assertEqual(r.turns, 4)
        self.assertEqual(r.context_median, 200_000)
        self.assertEqual(r.context_max, 300_000)
        self.assertEqual(r.tail_turns, 2)
        self.assertAlmostEqual(r.tail_turn_share, 0.5)
        self.assertAlmostEqual(r.tail_cost_share, 0.75)   # 600k of 800k cache-read tokens
        self.assertAlmostEqual(r.cost, 800_000 * 0.5 / 1_000_000)

    def test_unknown_model_has_no_cost(self):
        """Negative control: a model with no price row yields None, never a guess."""
        p = self.transcript("a.jsonl", [
            assistant(at("09:00"), "m1", model="mystery-3", use=usage(inp=400_000)),
        ])
        r = tr.overall(tr.parse([p]))
        self.assertEqual(r.turns, 1)
        self.assertEqual(r.context_median, 400_000)       # the size is still known
        self.assertIsNone(r.cost)                          # the price is not
        self.assertIsNone(r.tail_cost_share)
        self.assertEqual(r.priced_turns, 0)

    def test_transcript_without_usage_yields_na(self):
        """Negative control: no usage fields at all -> no turns, no numbers, no crash."""
        p = self.transcript("a.jsonl", [
            assistant(at("09:00"), "m1", blocks=[text("hello")]),
            user(at("09:01"), [text("hello back")]),
        ])
        parsed = tr.parse([p])
        r = tr.overall(parsed)
        self.assertEqual(r.turns, 0)
        self.assertIsNone(r.context_median)
        self.assertIsNone(r.cost)
        self.assertIsNone(r.tail_turn_share)
        self.assertTrue(r.intake_total > 0)                # the characters still counted

    def test_broken_lines_are_counted_not_fatal(self):
        p = self.transcript("a.jsonl", [assistant(at("09:00"), "m1", use=usage(inp=10))],
                            raw_lines=["{not json", "", "12"])
        parsed = tr.parse([p])
        self.assertEqual(len(parsed.turns), 1)
        self.assertEqual(parsed.unreadable_lines, 2)       # the blank line is not an error

    def test_intake_is_attributed_by_source(self):
        reminder = "<system-reminder>be brief</system-reminder>"
        p = self.transcript("a.jsonl", [
            assistant(at("09:00"), "m1", use=usage(inp=10), blocks=[
                text("abcd"), tool_use("t1", "Bash", {"command": "ls"})]),
            user(at("09:01"), [tool_result("t1", "x" * 100)]),
            user(at("09:02"), [text("hello" + reminder)]),
            user(at("09:03"), [text("injected")], meta=True),
        ])
        counts = tr.parse([p]).intake[DAY]
        self.assertEqual(counts[tr.SRC_ASSISTANT], 4)
        self.assertEqual(counts[tr.SRC_TOOL_RESULT.format("Bash")], 100)
        self.assertEqual(counts[tr.SRC_TOOL_CALL], len(json.dumps({"command": "ls"})))
        self.assertEqual(counts[tr.SRC_PRINCIPAL], len("hello"))
        self.assertEqual(counts[tr.SRC_HOOK], len(reminder) + len("injected"))

    def test_compactions_and_day_window(self):
        p = self.transcript("a.jsonl", [
            assistant(at("09:00"), "m1", use=usage(inp=10), compact=True),
            assistant(at("09:00", day="2026-09-01"), "m2", use=usage(inp=10)),
        ])
        parsed = tr.parse([p], days=[DAY])
        self.assertEqual(parsed.compactions[DAY], 1)
        self.assertEqual([t.day for t in parsed.turns], [DAY])

    def test_prices_match_by_prefix_and_merge_with_the_desk(self):
        self.assertEqual(tr.price_for("claude-sonnet-5-20260101")["input"], 3.0)
        self.assertEqual(tr.price_for("claude-haiku-4-5-20251001")["output"], 5.0)
        self.assertEqual(tr.price_for("claude-fable-5-1")["cache_write"], 12.5)
        self.assertIsNone(tr.price_for("gpt-9"))
        self.assertIsNone(tr.price_for(""))
        desk = Desk(self.tmp, {"prices": {"fable": {"input": 9.0}, "zeta": {"input": 1.0}}})
        table = price_table(desk)
        self.assertEqual(table["fable"]["input"], 9.0)      # overridden
        self.assertEqual(table["fable"]["output"], 50.0)    # and the rest kept
        self.assertEqual(table["claude-opus-5"]["input"], 5.0)
        self.assertIsNone(tr.price_for("zeta-1", table))    # a half-filled row is not a price


# -- the desk --------------------------------------------------------------

RECEIPT_BODY = "# Receipt {id}\n\nDid the thing.\n"


class DeskTests(TZCase):
    """A desk made from the template in a sandbox. No git, no network, no model."""

    def setUp(self):
        super().setUp()
        self.root = self.tmp / "desk"
        shutil.copytree(ROOT / "template", self.root)
        cfg = json.loads(read(self.root / "bellhop.example.json"))
        (self.root / "bellhop.example.json").unlink()
        cfg["lanes"]["desk"] = {"repo": "", "branch": "main", "check_cmd": ""}
        write(self.root / "bellhop.json", json.dumps(cfg, indent=2))
        self.desk = Desk(self.root, cfg)

    # -- fixtures ------------------------------------------------------
    def task(self, tid, lane="api", day=DAY, worker_cost=15.0, verifier_cost=2.0, sessions=1):
        receipt = self.desk.receipts / f"{tid}-a-task.md"
        write(receipt, RECEIPT_BODY.format(id=tid))
        headers.write_header(receipt, "receipt", {
            "task": tid, "lane": lane, "base": "b" * 40, "head": "h" * 40, "commits": 1,
            "files_changed": 1, "insertions": 1, "deletions": 0, "left_in_tree": 0,
            "check_exit": "0", "doubts": 0, "extras": 0, "provider": "claude",
            "model": OPUS, "turns": 12, "cost_usd": worker_cost, "duration_s": 60,
            "worker_exit": 0, "dead": "no", "generated": f"{day}T10:00:00+0000"})
        write_json(self.desk.receipts / f"{tid}.meta.json", {
            "id": tid, "lane": lane, "base": "b" * 40, "head_after": "h" * 40,
            "dead": False, "finished_at": f"{day}T10:00:00+0000", "provider": "claude",
            "model": OPUS, "turns": 12, "cost_usd": worker_cost, "duration_s": 60,
            "exit_code": 0})
        for n in range(sessions):
            name = f"{tid}.acceptance.json" if n == 0 else f"{tid}.acceptance.{n + 1}.json"
            write_json(self.desk.receipts / name,
                       {"cost_usd": verifier_cost} if verifier_cost is not None else {})
        return receipt

    def coordinator_day(self, dollars_per_turn=4.0, turns=2, day=DAY):
        """A transcript whose day cost is exactly `turns * dollars_per_turn`."""
        tokens = int(dollars_per_turn * 1_000_000 / 5.0)   # opus input is $5/M
        entries = [assistant(at("09:%02d" % i, day=day), f"m{i}", use=usage(inp=tokens))
                   for i in range(turns)]
        return self.transcript("coordinator.jsonl", entries)

    def report_text(self, transcripts=None, week=WEEK):
        return read(readings.report(self.desk, week=week, transcripts=transcripts))

    def readings_rows(self):
        path = self.desk.path(readings.READINGS_FILE)
        return [l for l in read(path).split("\n") if l.startswith("| 20")]

    # -- full-chain cost ------------------------------------------------
    def test_full_chain_cost_has_three_components(self):
        self.task("001", lane="desk")
        self.task("002", lane="api")
        body = self.report_text([self.coordinator_day()])
        self.assertIn("$21.00 = coordinator $4.00 + worker $15.00 + verifier $2.00", body)
        self.assertIn("(median over 2/2 tasks)", body)
        self.assertIn("$21.00", self.readings_rows()[0])

    def test_full_chain_is_na_without_a_coordinator_cost(self):
        """Negative control: no transcript means no coordinator share, so no chain."""
        self.task("001")
        body = self.report_text()
        self.assertIn(f"| Full-chain cost per task | {readings.NOT_AVAILABLE} (0/1 tasks", body)
        self.assertIn(f"| {readings.NOT_AVAILABLE} |", self.readings_rows()[0])

    def test_verifier_cost_falls_back_to_the_acceptance_header(self):
        self.task("001", verifier_cost=None)
        acceptance = self.desk.receipts / "001.acceptance.md"
        write(acceptance, "## Task\n001\n\nVERDICT: pass\n")
        headers.write_header(acceptance, "acceptance", {"task": "001", "verdict": "pass"})
        self.assertIsNone(readings.verifier_cost(self.desk, "001"))
        write(acceptance, "```acceptance-header\ntask: 001\ncost_usd: 3.5\n```\n\nbody\n")
        self.assertEqual(readings.verifier_cost(self.desk, "001"), 3.5)

    # -- repair share ----------------------------------------------------
    def test_repair_share_by_lane_and_by_files(self):
        meta_desk = {"id": "001", "lane": "desk"}
        meta_api = {"id": "002", "lane": "api"}
        self.assertIs(readings.is_repair(self.desk, meta_desk), True)
        self.assertIs(readings.is_repair(self.desk, meta_api, files=["state/next.md", "bellhop/cli.py"]), True)
        self.assertIs(readings.is_repair(self.desk, meta_api, files=["state/next.md", "src/app.py"]), False)
        self.assertIsNone(readings.is_repair(self.desk, meta_api, files=None))
        self.assertIsNone(readings.is_repair(self.desk, meta_api, files=[]))

    def test_repair_share_reaches_the_report(self):
        self.task("001", lane="desk")
        self.task("002", lane="api")
        body = self.report_text([self.coordinator_day()])
        self.assertIn("| Repair share | 50% (1/2 packets, 1 could not be listed) |", body)

    # -- verifier sessions ------------------------------------------------
    def test_verifier_sessions_per_task(self):
        self.task("001", sessions=2)
        self.task("002", sessions=2)
        self.assertEqual(readings.verifier_sessions(self.desk, "001"), 2)
        body = self.report_text([self.coordinator_day()])
        self.assertIn("| Verifier sessions per task | median 2, max 2 |", body)
        self.assertIn("| 2/2 |", self.readings_rows()[0])

    def test_verifier_sessions_is_zero_when_nothing_was_verified(self):
        """Negative control: no result file and no verdict is zero, not one."""
        self.task("001", sessions=0)
        self.assertEqual(readings.verifier_sessions(self.desk, "001"), 0)

    # -- intake -----------------------------------------------------------
    def intake_book(self):
        write(self.desk.path(readings.INTAKE_FILE),
              "# Principal intake (append-only, written by the hook)\n\n"
              f"## Entry 1 ({DAY} 09:00)\nwhat is waiting on me?\n\n"
              f"## Entry 2 ({DAY} 09:05)\nship the health endpoint today\n\n"
              f"## Entry 3 ({DAY} 09:06)\nship the health endpoint today\n(duplicate of 2)\n\n"
              f"## Entry 4 ({DAY} 09:20)\nmerge it when the checks are green\n\n"
              f"## Entry 5 ({DAY} 10:00)\nuse the smaller model for the next one\n\n"
              f"## Entry 6 ({DAY} 11:00)\nthe endpoint should return a version too\n")

    def test_intake_duplicates_and_status_questions(self):
        self.intake_book()
        self.task("001")
        body = self.report_text([self.coordinator_day()])
        self.assertIn("| Intake duplicates | 1 |", body)
        self.assertIn("| Status questions | 17% of 6 intake entries |", body)
        row = self.readings_rows()[0]
        self.assertIn("| 1 | 17% |", row)

    def test_intake_absent_is_na(self):
        """Negative control: no book at all reads n/a, never 0.

        The desk template now ships a header-only `state/intake.md` (the intake hook
        declares its format there), so "absent" has to be made absent on purpose.
        A desk that predates the hook, or one whose book was removed, still lands here.
        """
        book = self.desk.path(readings.INTAKE_FILE)
        if book.exists():
            book.unlink()
        self.task("001")
        body = self.report_text([self.coordinator_day()])
        self.assertIn(f"| Intake duplicates | {readings.NOT_AVAILABLE} |", body)
        self.assertIn(f"| Status questions | {readings.NOT_AVAILABLE} |", body)

    def test_intake_book_with_no_entries_yet(self):
        """A fresh desk from the template: the book exists, the hook has not run.

        The question share is `n/a` (no entries to take a share of); the duplicate
        count reads 0, which is what a book with no rows in it honestly holds.
        """
        self.task("001")
        body = self.report_text([self.coordinator_day()])
        self.assertIn("| Intake duplicates | 0 |", body)
        self.assertIn(f"| Status questions | {readings.NOT_AVAILABLE} of 0 intake entries |", body)

    # -- the daily file ----------------------------------------------------
    def test_a_day_measured_twice_replaces_its_line(self):
        self.task("001")
        self.report_text([self.coordinator_day(turns=2)])
        first = self.readings_rows()
        self.assertEqual(len(first), 1)
        self.assertIn("| 2 |", first[0])
        self.report_text([self.coordinator_day(turns=3)])
        second = self.readings_rows()
        self.assertEqual(len(second), 1)                       # replaced, not appended
        self.assertIn("| 3 |", second[0])
        self.assertTrue(read(self.desk.path(readings.READINGS_FILE)).startswith("# Readings"))
        kept = list((self.desk.state / ".previous").glob("readings.md.*"))
        self.assertTrue(kept, "the previous version must be kept before an overwrite")

    def test_other_days_are_left_alone(self):
        path = self.desk.path(readings.READINGS_FILE)
        write(path, readings.READINGS_HEADER + "| 2026-09-07 | 9 | 1 | 1% | 1% | $1.00 | 0 | 0 | n/a | n/a | n/a | n/a | n/a |\n")
        self.task("001")
        self.report_text([self.coordinator_day()])
        rows = self.readings_rows()
        self.assertEqual(len(rows), 2)
        self.assertTrue(rows[0].startswith("| 2026-09-07 |"))
        self.assertTrue(rows[1].startswith(f"| {DAY} |"))

    def test_daily_file_matches_the_template(self):
        self.assertEqual(read(ROOT / "template" / "state" / "readings.md"), readings.READINGS_HEADER)

    # -- the weekly report -------------------------------------------------
    def test_report_keeps_the_old_sections_and_gains_two(self):
        self.task("001")
        body = self.report_text([self.coordinator_day()])
        for heading in ("## Outcome numbers (fill by hand)", "## Process numbers (diagnostics)",
                        "## Cost and context", "## Where the coordinator's turns went"):
            self.assertIn(heading, body)
        template = read(ROOT / "template" / "templates" / "weekly-report.md")
        for line in template.split("\n"):
            if line.startswith("## "):
                self.assertIn(line, body, f"the template promises a section the report does not write: {line}")

    def test_report_without_transcripts_says_na_and_does_not_crash(self):
        """Negative control: every context number is n/a, and nothing is zero."""
        self.task("001")
        body = self.report_text()
        self.assertIn(f"| Coordinator turns | {readings.NOT_AVAILABLE} |", body)
        self.assertIn(f"| Context per turn (median / p90 / max) | {readings.NOT_AVAILABLE} / "
                      f"{readings.NOT_AVAILABLE} / {readings.NOT_AVAILABLE} |", body)
        self.assertIn(f"| Coordinator cost | {readings.NOT_AVAILABLE} |", body)
        self.assertIn(f"| {readings.NOT_AVAILABLE} (no transcripts given) |", body)

    def test_turns_went_table_names_the_sources(self):
        self.task("001")
        p = self.transcript("c.jsonl", [
            assistant(at("09:00"), "m1", use=usage(inp=800_000), blocks=[
                text("ok"), tool_use("t1", "Read", {"file_path": "/x"})]),
            user(at("09:01"), [tool_result("t1", "y" * 500)]),
        ])
        body = self.report_text([p])
        self.assertIn("| tool result: Read | 500 |", body)
        self.assertIn("| Turns at or above 250,000 tokens |", body)
        self.assertIn(tr.SRC_ASSISTANT, body)

    def test_a_directory_of_transcripts_is_read(self):
        self.task("001")
        folder = self.tmp / "project"
        folder.mkdir()
        for i in range(2):
            with (folder / f"s{i}.jsonl").open("w", encoding="utf-8") as fh:
                fh.write(json.dumps(assistant(at("09:0%d" % i), f"m{i}", use=usage(inp=100))) + "\n")
        body = self.report_text([folder])
        self.assertIn("| Coordinator turns | 2 |", body)


if __name__ == "__main__":
    unittest.main()
