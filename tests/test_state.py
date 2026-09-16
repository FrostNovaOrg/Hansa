"""Tests for the single state-write entry.

Every operation gets a positive case and a negative control, and the negative
control asserts that the refusal actually refused: the exit code, and the file
byte-for-byte unchanged. A refusal that quietly wrote half of the change and a
refusal that wrote nothing look the same in the message that says "refused".
"""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bellhop import seal as seal_mod  # noqa: E402
from bellhop import state  # noqa: E402
from bellhop.config import Desk  # noqa: E402
from bellhop.util import read, write  # noqa: E402


class DeskCase(unittest.TestCase):
    """A desk made from the template in a tempfile sandbox. No git, no network."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bellhop-state-test."))
        self.root = self.tmp / "desk"
        shutil.copytree(ROOT / "template", self.root)
        cfg_path = self.root / "bellhop.json"
        (self.root / "bellhop.example.json").rename(cfg_path)
        self.cfg = json.loads(read(cfg_path))
        self.desk = Desk(self.root, self.cfg)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- helpers ----------------------------------------------------------
    def body(self, rel):
        return read(self.root / rel)

    def rows(self, rel):
        """The data rows: the `- none` placeholder is not one."""
        return [l for _i, l in state.data_lines(self.body(rel).split("\n"))]

    def backups(self):
        d = state.previous_dir(self.desk)
        return sorted(p.name for p in d.iterdir()) if d.exists() else []

    def refuses(self, code, fn, *args, **kwargs):
        """Run something that must refuse, and prove it changed nothing."""
        watched = [
            state.CARD, state.INFLIGHT, state.DEBTS, state.DEBTS_SETTLED, state.TODO,
            state.OWN, state.SELFCATCH, state.SELFCATCH_HOOKS, state.CLOSINGS,
            state.CLAUDE_MD, state.HOT_RETIRED,
        ]
        before = {rel: (self.root / rel).read_bytes() for rel in watched if (self.root / rel).exists()}
        backups_before = self.backups()
        with self.assertRaises(state.StateError) as caught:
            fn(*args, **kwargs)
        self.assertEqual(caught.exception.code, code, f"expected exit {code}: {caught.exception}")
        after = {rel: (self.root / rel).read_bytes() for rel in watched if (self.root / rel).exists()}
        self.assertEqual(before, after, "a refusal wrote to a state file")
        self.assertEqual(backups_before, self.backups(), "a refusal left a previous-version copy behind")
        return caught.exception

    def cli(self, *args):
        return subprocess.run(
            [sys.executable, str(ROOT / "bellhop_main.py"), "--desk", str(self.root), "state", *args],
            capture_output=True, text=True,
        )


# --------------------------------------------------------------------------
class GeneralRulesTest(DeskCase):
    def test_previous_version_is_kept_before_an_overwrite(self):
        old = self.body(state.INFLIGHT)
        state.inflight_start(self.desk, "worker on 017")
        copies = [p for p in state.previous_dir(self.desk).iterdir() if p.name.startswith("in-flight.md.")]
        self.assertEqual(len(copies), 1)
        self.assertEqual(read(copies[0]), old)
        self.assertNotEqual(self.body(state.INFLIGHT), old)

    def test_previous_versions_are_pruned_to_the_configured_count(self):
        self.desk.cfg["previous_versions_keep"] = 3
        for i in range(6):
            state.inflight_start(self.desk, f"worker on {100 + i}")
        copies = [p for p in state.previous_dir(self.desk).iterdir() if p.name.startswith("in-flight.md.")]
        self.assertEqual(len(copies), 3)

    def test_anchored_replacement_needs_exactly_one_match(self):
        state.debts_add(self.desk, "one debt about widgets")
        state.debts_add(self.desk, "another debt about widgets")
        exc = self.refuses(state.REFUSED, state.debts_set, self.desk, "widgets", "- replaced")
        self.assertIn("2 line(s) matched", str(exc))
        self.assertEqual(len(exc.extra), 2)

    def test_zero_matches_is_also_a_refusal(self):
        exc = self.refuses(state.REFUSED, state.debts_set, self.desk, "nothing here", "- replaced")
        self.assertIn("0 line(s) matched", str(exc))

    def test_every_write_echoes_what_it_wrote(self):
        result = state.inflight_start(self.desk, "worker on 017 health endpoint")
        self.assertEqual(result.echo, ["- worker on 017 health endpoint"])
        self.assertIn("- worker on 017 health endpoint", result.render())

    def test_echo_is_capped_so_one_row_cannot_flood_the_screen(self):
        long_line = "x" * 900
        self.assertLessEqual(len(state.echo(long_line).encode("utf-8")), state.ECHO_MAX_BYTES + 8)
        self.assertTrue(state.echo(long_line).endswith(" ..."))

    def test_missing_state_file_is_an_environment_error(self):
        (self.root / state.CARD).unlink()
        self.refuses(state.ENVIRONMENT, state.card_set, self.desk, lane="api")


# --------------------------------------------------------------------------
class LedgerTest(DeskCase):
    def entry(self, text):
        p = self.tmp / "entry.md"
        write(p, text)
        return p

    def test_add_assigns_the_number_from_the_last_existing_entry(self):
        first = state.ledger_add(self.desk, self.entry("# Took in 017\n\nTwo items green.\n"), refs=["017"])
        second = state.ledger_add(self.desk, self.entry("# Took in 018\n\nOne item green.\n"))
        self.assertTrue(first.paths[0].name.startswith("001-"))
        self.assertTrue(second.paths[0].name.startswith("002-"))
        self.assertIn("017", read(first.paths[0]))
        self.assertIn("bytes", first.echo[0])

    def test_add_refuses_a_missing_file(self):
        self.refuses(state.ENVIRONMENT, state.ledger_add, self.desk, self.tmp / "not-there.md")

    def test_add_refuses_an_empty_entry(self):
        self.refuses(state.MALFORMED, state.ledger_add, self.desk, self.entry("\n \n"))

    def test_over_the_cap_it_compresses_rather_than_refusing(self):
        raw = "".join(f"note   {i} with trailing   \n\n\n" for i in range(12))
        stamp = "x" * 30
        loose = len(state._render_entry("Took in 019", raw, [], stamp).encode("utf-8"))
        tight = len(state._render_entry("Took in 019", state.compress_entry(raw), [], stamp).encode("utf-8"))
        self.desk.cfg["ledger_entry_bytes"] = tight + 8
        self.assertGreater(loose, tight + 8, "the sandbox entry is not actually over the cap")
        result = state.ledger_add(self.desk, self.entry("# Took in 019\n\n" + raw))
        self.assertIn("compressed", result.note)
        self.assertEqual(len(result.paths), 1)
        written = read(result.paths[0])
        self.assertNotIn("   ", written)
        self.assertNotIn("\n\n\n", written)

    def test_still_over_the_cap_it_splits_into_two_numbered_parts(self):
        self.desk.cfg["ledger_entry_bytes"] = 200
        text = "# Took in 020\n\n" + "".join(f"a sentence of some length number {i}\n" for i in range(30))
        result = state.ledger_add(self.desk, self.entry(text))
        self.assertEqual(len(result.paths), 2)
        self.assertIn("part 1 of 2", read(result.paths[0]))
        self.assertIn("part 2 of 2", read(result.paths[1]))
        self.assertTrue(result.paths[0].name.startswith("001-"))
        self.assertTrue(result.paths[1].name.startswith("002-"))
        # split at a line boundary: no line is cut in half
        joined = read(result.paths[0]) + read(result.paths[1])
        for i in range(30):
            self.assertIn(f"a sentence of some length number {i}", joined)

    def test_compression_is_deterministic(self):
        raw = "one   two  \n\n\n\nthree\n\n"
        self.assertEqual(state.compress_entry(raw), state.compress_entry(raw))
        self.assertEqual(state.compress_entry(raw), "one two\n\nthree")

    def test_entries_are_never_rewritten(self):
        first = state.ledger_add(self.desk, self.entry("# One\n\nbody\n"))
        before = read(first.paths[0])
        state.ledger_add(self.desk, self.entry("# Two\n\nbody\n"))
        self.assertEqual(read(first.paths[0]), before)


# --------------------------------------------------------------------------
class CardTest(DeskCase):
    def test_set_changes_only_the_given_keys_and_stamps_the_rest(self):
        before = dict(state.read_card_keys(self.desk))
        result = state.card_set(self.desk, next="021 packet", dispatchable="yes", lane="api")
        after = state.read_card_keys(self.desk)
        self.assertEqual(after["next"], "021 packet")
        self.assertEqual(after["ready"], "yes")
        self.assertEqual(after["lane"], "api")
        self.assertEqual(after["reason"], before["reason"], "an untouched key was rewritten")
        self.assertEqual(after["by"], "bellhop state")
        self.assertNotEqual(after["refreshed"], before["refreshed"])
        self.assertEqual(len(result.echo), len(state.CARD_KEYS))

    def test_the_rules_do_not_live_in_the_data_file(self):
        card = self.body(state.CARD)
        self.assertIn("rules/card.md", card)
        self.assertNotIn("must say", card)
        rules = read(self.root / "rules" / "card.md")
        for key in state.CARD_KEYS:
            self.assertIn(f"`{key}`", rules)
        state.card_set(self.desk, next="021 packet", dispatchable="yes")
        self.assertIn("rules/card.md", self.body(state.CARD), "a refresh ate the pointer to the rules")

    def test_pool_must_start_with_a_digit(self):
        self.refuses(state.MALFORMED, state.card_set, self.desk, pool="lots of things")

    def test_pool_word_none_becomes_a_machine_readable_zero(self):
        state.card_set(self.desk, pool="none")
        self.assertEqual(state.read_card_keys(self.desk)["queue"], "0 (none)")

    def test_dispatchable_yes_without_next_is_refused(self):
        self.refuses(state.REFUSED, state.card_set, self.desk, dispatchable="yes")

    def test_dispatchable_no_without_reason_is_refused(self):
        self.refuses(state.REFUSED, state.card_set, self.desk, dispatchable="no", reason="")

    def test_dispatchable_takes_yes_or_no_only(self):
        self.refuses(state.MALFORMED, state.card_set, self.desk, dispatchable="maybe")

    def test_unknown_key_is_refused(self):
        self.refuses(state.MALFORMED, state.card_set, self.desk, colour="blue")

    def test_a_duplicated_key_line_is_refused_not_guessed(self):
        text = self.body(state.CARD).replace("lane: \n", "lane: \nlane: api\n")
        write(self.root / state.CARD, text)
        exc = self.refuses(state.REFUSED, state.card_set, self.desk, lane="web")
        self.assertIn("2 `lane: ` line(s)", str(exc))


# --------------------------------------------------------------------------
class InflightTest(DeskCase):
    def test_start_adds_a_line_and_drops_the_placeholder(self):
        state.inflight_start(self.desk, "worker on 017 health endpoint")
        self.assertEqual(self.rows(state.INFLIGHT), ["- worker on 017 health endpoint"])

    def test_start_refuses_empty_text(self):
        self.refuses(state.MALFORMED, state.inflight_start, self.desk, "   ")

    def test_a_long_line_is_truncated_not_refused(self):
        self.desk.cfg["inflight_line_chars"] = 40
        result = state.inflight_start(self.desk, "worker on 017 " + "x" * 200)
        self.assertLessEqual(len(result.echo[0]), 40)
        self.assertTrue(result.echo[0].endswith(" ..."))
        self.assertTrue(result.warnings)

    def test_finish_removes_the_line_and_echoes_it(self):
        state.inflight_start(self.desk, "worker on 017 health endpoint")
        state.inflight_start(self.desk, "worker on 018 docs")
        result = state.inflight_finish(self.desk, "017")
        self.assertEqual(result.echo, ["- worker on 017 health endpoint"])
        self.assertEqual(self.rows(state.INFLIGHT), ["- worker on 018 docs"])

    def test_finish_restores_the_placeholder_when_the_file_empties(self):
        state.inflight_start(self.desk, "worker on 017")
        state.inflight_finish(self.desk, "017")
        self.assertIn("- none", self.body(state.INFLIGHT))

    def test_finish_refuses_an_id_that_matches_no_line(self):
        state.inflight_start(self.desk, "worker on 017")
        self.refuses(state.REFUSED, state.inflight_finish, self.desk, "999")

    def test_finish_refuses_an_id_that_matches_two_lines(self):
        state.inflight_start(self.desk, "worker on 017 first half")
        state.inflight_start(self.desk, "helper waiting on 017 to land")
        exc = self.refuses(state.REFUSED, state.inflight_finish, self.desk, "017")
        self.assertIn("2 line(s) matched", str(exc))

    def test_an_id_matches_as_a_whole_token_only(self):
        state.inflight_start(self.desk, "worker on 2017 the other one")
        state.inflight_start(self.desk, "worker on 017 the right one")
        result = state.inflight_finish(self.desk, "017")
        self.assertEqual(result.echo, ["- worker on 017 the right one"])

    def test_tag_marks_the_line(self):
        state.inflight_start(self.desk, "worker on 017")
        result = state.inflight_tag(self.desk, "017", "settled-awaiting-close")
        self.assertIn("[settled-awaiting-close]", result.echo[0])
        self.assertIn("[settled-awaiting-close]", self.body(state.INFLIGHT))

    def test_tag_refuses_a_line_that_already_carries_it(self):
        state.inflight_start(self.desk, "worker on 017")
        state.inflight_tag(self.desk, "017", "settled-awaiting-close")
        self.refuses(state.REFUSED, state.inflight_tag, self.desk, "017", "settled-awaiting-close")


# --------------------------------------------------------------------------
class DebtsTest(DeskCase):
    def test_add_appends_a_line(self):
        state.debts_add(self.desk, "owe the principal a release note")
        self.assertEqual(self.rows(state.DEBTS), ["- owe the principal a release note"])

    def test_add_refuses_the_same_line_twice(self):
        state.debts_add(self.desk, "owe the principal a release note")
        self.refuses(state.REFUSED, state.debts_add, self.desk, "owe the principal a release note")

    def test_settle_moves_the_line_to_the_settled_file(self):
        state.debts_add(self.desk, "owe the principal a release note")
        result = state.debts_settle(self.desk, "release note")
        self.assertEqual(self.rows(state.DEBTS), [])
        self.assertIn("- none", self.body(state.DEBTS))
        self.assertIn("release note", self.body(state.DEBTS_SETTLED))
        self.assertIn("settled", result.note)

    def test_settle_with_keep_leaves_a_struck_copy(self):
        state.debts_add(self.desk, "owe the principal a release note")
        state.debts_settle(self.desk, "release note", keep=True)
        self.assertIn("~~owe the principal a release note~~", self.body(state.DEBTS))
        self.assertIn("release note", self.body(state.DEBTS_SETTLED))

    def test_settle_refuses_when_nothing_matches(self):
        self.refuses(state.REFUSED, state.debts_settle, self.desk, "no such debt")

    def test_set_replaces_in_place_and_keeps_the_position(self):
        state.debts_add(self.desk, "BASELINE sha abc1234 suite 2410")
        state.debts_add(self.desk, "owe the principal a release note")
        state.debts_set(self.desk, "BASELINE", "BASELINE sha def5678 suite 2411")
        self.assertEqual(
            self.rows(state.DEBTS),
            ["- BASELINE sha def5678 suite 2411", "- owe the principal a release note"],
        )

    def test_set_refuses_an_empty_replacement(self):
        state.debts_add(self.desk, "BASELINE sha abc1234")
        self.refuses(state.MALFORMED, state.debts_set, self.desk, "BASELINE", "  ")

    def test_settled_file_is_only_appended_to(self):
        state.debts_add(self.desk, "first debt")
        state.debts_add(self.desk, "second debt")
        state.debts_settle(self.desk, "first debt")
        before = self.body(state.DEBTS_SETTLED)
        state.debts_settle(self.desk, "second debt")
        self.assertTrue(self.body(state.DEBTS_SETTLED).startswith(before))


# --------------------------------------------------------------------------
class TodoTest(DeskCase):
    LINE = "sign the vendor contract - why only you: it needs your signature"

    def test_add_requires_a_why_only_you_clause(self):
        state.todo_add(self.desk, self.LINE)
        self.assertIn(self.LINE, self.body(state.TODO))

    def test_add_refuses_a_line_without_the_clause(self):
        self.refuses(state.MALFORMED, state.todo_add, self.desk, "sign the vendor contract")

    def test_settle_moves_the_line_to_done(self):
        state.todo_add(self.desk, self.LINE)
        state.todo_settle(self.desk, "vendor contract")
        text = self.body(state.TODO)
        open_at, done_at = text.index("## Open"), text.index("## Done")
        self.assertGreater(text.index("vendor contract"), done_at)
        self.assertIn("- none", text[open_at:done_at])

    def test_settle_refuses_when_nothing_matches(self):
        state.todo_add(self.desk, self.LINE)
        self.refuses(state.REFUSED, state.todo_settle, self.desk, "no such thing")


# --------------------------------------------------------------------------
class OwnListTest(DeskCase):
    def test_add_and_list(self):
        state.own_add(self.desk, "[doing] write the weekly -> next: send it")
        result = state.own_list(self.desk)
        self.assertEqual(result.echo, ["- [doing] write the weekly -> next: send it"])

    def test_add_refuses_empty_text(self):
        self.refuses(state.MALFORMED, state.own_add, self.desk, "")

    def test_settle_removes_the_line(self):
        state.own_add(self.desk, "[doing] write the weekly -> next: send it")
        state.own_add(self.desk, "[waiting] hear back on lane c -> next: chase")
        result = state.own_settle(self.desk, "weekly")
        self.assertEqual(result.echo, ["- [doing] write the weekly -> next: send it"])
        self.assertEqual(len(state.own_list(self.desk).echo), 1)

    def test_settle_refuses_when_nothing_matches(self):
        self.refuses(state.REFUSED, state.own_settle, self.desk, "no such item")

    def test_more_than_seven_open_lines_warns_but_does_not_refuse(self):
        for i in range(7):
            result = state.own_add(self.desk, f"[doing] item {i} -> next: keep going")
            self.assertEqual(result.warnings, [])
        result = state.own_add(self.desk, "[doing] item 8 -> next: keep going")
        self.assertTrue(result.warnings)
        self.assertEqual(len(state.own_list(self.desk).echo), 8, "the eighth line was refused, not warned")


# --------------------------------------------------------------------------
class SelfCatchTest(DeskCase):
    def catch(self, root="R1", kind="judgement", cause="dispatched before reading the lane rules"):
        return state.selfcatch_add(self.desk, root, kind, cause)

    def test_add_appends_a_row(self):
        result = self.catch()
        self.assertIn("[R1]", result.echo[0])
        self.assertIn("dispatched before reading the lane rules", self.body(state.SELFCATCH))

    def test_add_refuses_an_empty_cause(self):
        self.refuses(state.MALFORMED, state.selfcatch_add, self.desk, "R1", "judgement", "   ")

    def test_add_refuses_a_cause_shorter_than_twelve_characters(self):
        self.refuses(state.MALFORMED, state.selfcatch_add, self.desk, "R1", "judgement", "too short")

    def test_add_refuses_a_cause_that_is_the_root_id(self):
        exc = self.refuses(state.MALFORMED, state.selfcatch_add, self.desk, "R1", "judgement", "R1")
        self.assertIn("placeholder", str(exc))

    def test_add_refuses_an_unknown_kind(self):
        self.refuses(state.MALFORMED, state.selfcatch_add, self.desk, "R1", "vibes", "a perfectly good sentence here")

    def test_hook_hits_never_touch_the_human_count(self):
        self.catch()
        for _ in range(5):
            state.selfcatch_hook_hit(self.desk, "R1")
        counts = state.selfcatch_counts(self.desk)["R1"]
        self.assertEqual(counts["count"], 1)
        self.assertEqual(counts["hook_hits"], 5)
        self.assertEqual(len(self.rows(state.SELFCATCH)), 1, "a hook hit landed in the human book")

    def test_hook_hit_refuses_an_empty_root(self):
        self.refuses(state.MALFORMED, state.selfcatch_hook_hit, self.desk, " ")

    def test_table_prints_the_recomputed_counts(self):
        self.catch()
        self.catch(cause="rang a bell before the packet was validated")
        state.selfcatch_hook_hit(self.desk, "R2")
        text = state.selfcatch_table(self.desk).render()
        self.assertIn("| R1 | 2 | 0 |", text)
        self.assertIn("| R2 | 0 | 1 |", text)

    def test_the_book_is_append_only(self):
        self.catch()
        before = self.body(state.SELFCATCH)
        self.catch(cause="a second, quite different mistake")
        self.assertTrue(self.body(state.SELFCATCH).startswith(before))


# --------------------------------------------------------------------------
class HotRulesTest(DeskCase):
    RULE = "read the lane file before writing the packet"

    def qualify(self, root="R1", times=3):
        for i in range(times):
            state.selfcatch_add(self.desk, root, "judgement", f"dispatched before reading the lane rules {i}")

    def test_promote_inserts_between_the_markers(self):
        self.qualify()
        result = state.hotrules_promote(self.desk, "R1", self.RULE)
        text = self.body(state.CLAUDE_MD)
        start, end = text.index(state.HOT_START), text.index(state.HOT_END)
        self.assertGreater(text.index(self.RULE), start)
        self.assertLess(text.index(self.RULE), end)
        self.assertIn("1/10", result.note)

    def test_promote_refuses_under_the_threshold(self):
        self.qualify(times=2)
        exc = self.refuses(state.REFUSED, state.hotrules_promote, self.desk, "R1", self.RULE)
        self.assertIn("under the promotion threshold", str(exc))

    def test_hook_hits_do_not_earn_a_slot(self):
        for _ in range(9):
            state.selfcatch_hook_hit(self.desk, "R1")
        self.refuses(state.REFUSED, state.hotrules_promote, self.desk, "R1", self.RULE)

    def test_promote_refuses_a_case_record_instead_of_a_rule(self):
        self.qualify()
        for bad in (
            "on 2026-09-11 remember to read the lane file",
            "task 017 showed we must read the lane file",
            "the incident proves the lane file matters",
            "a failure to read the lane file again",
        ):
            self.refuses(state.MALFORMED, state.hotrules_promote, self.desk, "R1", bad)

    def test_promote_refuses_when_every_slot_is_taken(self):
        self.desk.cfg["hot_rule_max_lines"] = 1
        self.qualify("R1")
        self.qualify("R2")
        state.hotrules_promote(self.desk, "R1", self.RULE)
        exc = self.refuses(state.REFUSED, state.hotrules_promote, self.desk, "R2", "ring only after the packet validates")
        self.assertIn("maximum is 1", str(exc))

    def test_promote_refuses_a_second_slot_for_the_same_root(self):
        self.qualify()
        state.hotrules_promote(self.desk, "R1", self.RULE)
        self.refuses(state.REFUSED, state.hotrules_promote, self.desk, "R1", "read the lane file, really")

    def test_retire_removes_the_line_and_records_it_with_the_reason(self):
        self.qualify()
        state.hotrules_promote(self.desk, "R1", self.RULE)
        state.hotrules_retire(self.desk, "R1", "the packet template now carries the lane file")
        self.assertNotIn(self.RULE, self.body(state.CLAUDE_MD))
        register = self.body(state.HOT_RETIRED)
        self.assertIn(self.RULE, register)
        self.assertIn("count 3", register)
        self.assertIn("the packet template now carries the lane file", register)

    def test_retire_refuses_a_root_that_holds_no_slot(self):
        self.refuses(state.REFUSED, state.hotrules_retire, self.desk, "R1", "never held a slot")

    def test_retire_refuses_an_empty_reason(self):
        self.qualify()
        state.hotrules_promote(self.desk, "R1", self.RULE)
        self.refuses(state.MALFORMED, state.hotrules_retire, self.desk, "R1", "  ")

    def test_list_reports_the_slots(self):
        self.assertIn("0/10", state.hotrules_list(self.desk).note)
        self.qualify()
        state.hotrules_promote(self.desk, "R1", self.RULE)
        self.assertIn("1/10", state.hotrules_list(self.desk).note)

    def test_a_file_without_markers_is_an_environment_error(self):
        text = self.body(state.CLAUDE_MD).replace(state.HOT_END, "")
        write(self.root / state.CLAUDE_MD, text)
        self.qualify()
        self.refuses(state.ENVIRONMENT, state.hotrules_promote, self.desk, "R1", self.RULE)


# --------------------------------------------------------------------------
class ClosingsTest(DeskCase):
    def test_add_appends_one_line(self):
        state.closings_add(self.desk, "017", "merged at abc1234, two items green")
        self.assertIn("[017]", self.body(state.CLOSINGS))

    def test_add_refuses_a_line_over_the_cap(self):
        self.refuses(state.MALFORMED, state.closings_add, self.desk, "017", "x" * 201)

    def test_add_refuses_an_empty_task(self):
        self.refuses(state.MALFORMED, state.closings_add, self.desk, " ", "merged")

    def test_unread_prints_the_new_lines_and_advances_the_pointer(self):
        state.closings_add(self.desk, "017", "merged at abc1234")
        state.closings_add(self.desk, "018", "rejected, sent back")
        first = state.closings_unread(self.desk)
        self.assertEqual(len(first.echo), 2)
        second = state.closings_unread(self.desk)
        self.assertIn("0 unread", second.note)
        self.assertEqual(second.echo, ["(none)"])

    def test_peek_does_not_advance_the_pointer(self):
        state.closings_add(self.desk, "017", "merged at abc1234")
        state.closings_unread(self.desk, advance=False)
        self.assertEqual(len(state.closings_unread(self.desk).echo), 1)

    def test_the_register_is_append_only(self):
        state.closings_add(self.desk, "017", "merged at abc1234")
        before = self.body(state.CLOSINGS)
        state.closings_add(self.desk, "018", "rejected, sent back")
        self.assertTrue(self.body(state.CLOSINGS).startswith(before))


# --------------------------------------------------------------------------
class WrapupTest(DeskCase):
    def test_it_runs_own_then_inflight_then_debts_then_card(self):
        state.inflight_start(self.desk, "worker on 020 refactor")
        results = state.wrapup(
            self.desk,
            own="[doing] write the weekly -> next: send it",
            inflight_finish_id="020",
            debts_add_text="owe the principal a decision on lane c",
            card={"next": "021 packet", "dispatchable": "yes"},
        )
        self.assertEqual([r.note.split("]")[0] + "]" for r in results],
                         ["[own]", "[in-flight]", "[debts]", "[card]"])
        self.assertEqual(self.rows(state.INFLIGHT), [])
        self.assertIn("lane c", self.body(state.DEBTS))
        self.assertEqual(state.read_card_keys(self.desk)["next"], "021 packet")

    def test_it_stops_at_the_first_refusal_and_names_the_step(self):
        card_before = self.body(state.CARD)
        debts_before = self.body(state.DEBTS)
        with self.assertRaises(state.WrapupStopped) as caught:
            state.wrapup(
                self.desk,
                own="[doing] write the weekly -> next: send it",
                inflight_finish_id="999",
                debts_add_text="owe the principal a decision on lane c",
                card={"next": "021 packet", "dispatchable": "yes"},
            )
        exc = caught.exception
        self.assertEqual(exc.step, "in-flight")
        self.assertEqual(exc.code, state.REFUSED)
        self.assertEqual(len(exc.done), 1, "a step after the refusal ran anyway")
        self.assertEqual(self.body(state.DEBTS), debts_before)
        self.assertEqual(self.body(state.CARD), card_before)

    def test_it_refuses_when_given_nothing_to_do(self):
        self.refuses(state.MALFORMED, state.wrapup, self.desk)


# --------------------------------------------------------------------------
class CliTest(DeskCase):
    def test_the_command_is_registered(self):
        out = subprocess.run([sys.executable, str(ROOT / "bellhop_main.py"), "--help"],
                             capture_output=True, text=True)
        self.assertIn("state", out.stdout)

    def test_a_successful_write_exits_zero_and_echoes(self):
        out = self.cli("inflight", "start", "--text", "worker on 017")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("- worker on 017", out.stdout)

    def test_a_precondition_refusal_is_a_gate_refusal(self):
        """The numbers are docs/exit-codes.md's; the names are what this module means."""
        out = self.cli("inflight", "finish", "--task", "999")
        self.assertEqual(out.returncode, state.REFUSED)
        self.assertTrue(out.stderr.startswith("refused:"), out.stderr)

    def test_malformed_input_is_a_usage_error(self):
        out = self.cli("selfcatch", "add", "--root", "R1", "--kind", "judgement", "--cause", "R1")
        self.assertEqual(out.returncode, state.MALFORMED)
        self.assertTrue(out.stderr.startswith("refused:"), out.stderr)

    def test_a_missing_file_is_a_not_found_and_says_error(self):
        out = self.cli("ledger", "add", "--file", str(self.tmp / "nope.md"))
        self.assertEqual(out.returncode, state.ENVIRONMENT)
        self.assertTrue(out.stderr.startswith("error:"), out.stderr)


# --------------------------------------------------------------------------
class SealTest(DeskCase):
    """A hot-rule write is expected to change .claude/CLAUDE.md; the seal must not
    call that tampering, and must still call everything else tampering."""

    RULE = "read the lane file before writing the packet"

    def qualify(self, root="R1"):
        for i in range(3):
            state.selfcatch_add(self.desk, root, "judgement", f"dispatched before reading the lane rules {i}")

    def test_promoting_a_hot_rule_does_not_break_the_seal(self):
        self.qualify()
        seal_mod.seal(self.desk)
        self.assertEqual(seal_mod.verify(self.desk), [])
        state.hotrules_promote(self.desk, "R1", self.RULE)
        self.assertIn(self.RULE, self.body(state.CLAUDE_MD), "the rule was not actually written")
        self.assertEqual(seal_mod.verify(self.desk), [], "a promotion turned the seal red")

    def test_retiring_a_hot_rule_does_not_break_the_seal(self):
        self.qualify()
        state.hotrules_promote(self.desk, "R1", self.RULE)
        seal_mod.seal(self.desk)
        state.hotrules_retire(self.desk, "R1", "the packet template now carries the lane file")
        self.assertIn(self.RULE, self.body(state.HOT_RETIRED), "the register was not actually written")
        self.assertEqual(seal_mod.verify(self.desk), [], "a retirement turned the seal red")

    def test_the_retirement_register_is_not_in_the_manifest(self):
        seal_mod.seal(self.desk)
        manifest = read(seal_mod.manifest_path(self.desk))
        self.assertNotIn("hot-rules-retired.md", manifest)
        self.assertIn("desk:rules/card.md", manifest, "the rest of rules/ is still sealed")

    # -- negative controls ------------------------------------------------
    def test_a_line_outside_the_segment_still_breaks_the_seal(self):
        seal_mod.seal(self.desk)
        self.assertEqual(seal_mod.verify(self.desk), [])
        write(self.root / state.CLAUDE_MD, self.body(state.CLAUDE_MD) + "\n- a line nobody declared\n")
        self.assertEqual(seal_mod.verify(self.desk), ["desk:.claude/CLAUDE.md"])

    def test_editing_a_line_above_the_segment_still_breaks_the_seal(self):
        seal_mod.seal(self.desk)
        text = self.body(state.CLAUDE_MD).replace("# Coordinator", "# Coordinator (quietly amended)", 1)
        write(self.root / state.CLAUDE_MD, text)
        self.assertEqual(seal_mod.verify(self.desk), ["desk:.claude/CLAUDE.md"])

    def test_removing_a_marker_still_breaks_the_seal(self):
        self.qualify()
        state.hotrules_promote(self.desk, "R1", self.RULE)
        seal_mod.seal(self.desk)
        write(self.root / state.CLAUDE_MD, self.body(state.CLAUDE_MD).replace(state.HOT_END, "", 1))
        self.assertEqual(seal_mod.verify(self.desk), ["desk:.claude/CLAUDE.md"])

    def test_another_sealed_file_still_breaks_the_seal(self):
        seal_mod.seal(self.desk)
        write(self.root / "rules" / "card.md", read(self.root / "rules" / "card.md") + "\nsmuggled in\n")
        self.assertEqual(seal_mod.verify(self.desk), ["desk:rules/card.md"])


if __name__ == "__main__":
    unittest.main()
