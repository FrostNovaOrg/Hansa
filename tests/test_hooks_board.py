"""The board, the intake hook and the hand-window gate.

Every gate here gets a positive case and a negative control, and the negative
control asserts the refusal actually refused: the exit code, and the file it
would have written left byte-for-byte unchanged. A gate that blocked and a gate
that blocked *and also wrote half of something* look the same in the word
"blocked".

Nothing here touches the network or the user's real desk: each case builds a desk
from `template/` in a tempfile sandbox.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bellhop import board as board_mod  # noqa: E402
from bellhop import hand as hand_mod  # noqa: E402
from bellhop import intake as intake_mod  # noqa: E402
from bellhop import state as state_mod  # noqa: E402
from bellhop.config import Desk  # noqa: E402
from bellhop.util import BellhopError, read, write  # noqa: E402

MAIN = str(ROOT / "bellhop_main.py")


class DeskCase(unittest.TestCase):
    """A desk copied from the template into a tempfile sandbox. No git, no network."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bellhop-hooks-test.")).resolve()
        self.root = self.tmp / "desk"
        shutil.copytree(ROOT / "template", self.root)
        cfg_path = self.root / "bellhop.json"
        (self.root / "bellhop.example.json").rename(cfg_path)
        self.cfg = json.loads(read(cfg_path))
        self.cfg.setdefault("lanes", {})
        self.desk = Desk(self.root, self.cfg)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- helpers ----------------------------------------------------------
    def reload(self):
        self.desk = Desk(self.root, self.cfg)
        return self.desk

    def add_lane(self, name="api"):
        repo = self.tmp / name
        (repo / "src").mkdir(parents=True, exist_ok=True)
        write(repo / "src" / "app.py", "print('hi')\n")
        self.cfg["lanes"][name] = {"repo": str(repo)}
        write(self.root / "bellhop.json", json.dumps(self.cfg, indent=2) + "\n")
        self.reload()
        return repo

    def hook(self, event, payload):
        """Run a hook the way Claude Code does: JSON on stdin, exit code out."""
        proc = subprocess.run(
            [sys.executable, MAIN, "hook", event],
            input=json.dumps(payload), text=True, capture_output=True,
        )
        return proc

    def cli(self, *args):
        return subprocess.run(
            [sys.executable, MAIN, "--desk", str(self.root), "--no-commit", *args],
            text=True, capture_output=True,
        )


# ==========================================================================
# the board
# ==========================================================================
class BoardTest(DeskCase):
    def test_fresh_desk_names_every_section(self):
        text = board_mod.board_text(self.desk)
        for heading in ("== card", "== in flight", "== own list", "== debts",
                        "== close-outs", "== intake", "== hot rules",
                        "== sentinel", "== bells", "== watchdog"):
            self.assertIn(heading, text)
        for key in state_mod.CARD_KEYS:
            self.assertIn(f"{key}:", text)
        self.assertLessEqual(len(text.encode()), self.desk.cfg["board_bytes"])

    def test_quotes_the_rows_it_shows(self):
        state_mod.inflight_start(self.desk, "[api] 017 health endpoint")
        state_mod.own_add(self.desk, "[doing] answer the principal on pricing -> next: draft it")
        text = board_mod.board_text(self.desk)
        self.assertIn("[api] 017 health endpoint", text)
        self.assertIn("answer the principal on pricing", text)
        self.assertIn("== in flight (1) ==", text)

    def test_missing_file_and_empty_file_do_not_look_alike(self):
        """The negative control for the whole page: a page believed without checking
        must never render "not on disk" and "nothing in it" the same way."""
        empty = board_mod.board_text(self.desk)
        self.assertIn("== debts (0) ==\nnone", empty)
        (self.root / "state" / "debts.md").unlink()
        gone = board_mod.board_text(self.reload())
        self.assertIn("state/debts.md is not on disk", gone)
        self.assertNotIn("== debts (0) ==\nnone", gone)

    def test_baseline_row_is_kept_and_only_last_five_others_shown(self):
        state_mod.debts_add(self.desk, "BASELINE: 14 open, merged through sweep 7")
        for i in range(1, 8):
            state_mod.debts_add(self.desk, f"owe the principal an answer on question {i}")
        text = board_mod.board_text(self.desk)
        self.assertIn("BASELINE: 14 open", text)
        self.assertIn("question 7", text)
        self.assertNotIn("question 1 ", text + " ")
        self.assertNotIn("question 2 ", text + " ")

    def test_over_the_cap_trims_in_order_and_says_how_many_bytes(self):
        for i in range(1, 8):
            state_mod.debts_add(self.desk, f"owe the principal an answer on question {i}")
        for i in range(1, 5):
            state_mod.closings_add(self.desk, f"0{i}", f"closed task {i} after acceptance, merged clean")
        for i in range(1, 4):
            intake_mod.log(self.desk, f"message number {i} from the principal")
        full = board_mod.board_text(self.desk, cap=10_000)
        self.assertNotIn("(trimmed", full)
        tight = board_mod.board_text(self.desk, cap=700)
        self.assertLessEqual(len(tight.encode()), 700)
        self.assertRegex(tight, r"\(trimmed \d+ bytes\)\n$")
        # the fixed order: debts go first, then intake titles, then close-outs
        self.assertNotIn("owe the principal an answer", tight)
        self.assertNotIn("## Intake", tight)
        # and never the card or in flight
        self.assertIn("== card", tight)
        for key in state_mod.CARD_KEYS:
            self.assertIn(f"{key}:", tight)

    def test_trim_takes_the_oldest_row_of_a_kind_first(self):
        for i in range(1, 6):
            state_mod.debts_add(self.desk, f"debt row number {i} owed to the principal")
        page = board_mod.rows(self.desk)
        debts = [t for tag, t in page if tag == "D"]
        self.assertEqual(len(debts), 5)
        cap = len(board_mod.board_text(self.desk, cap=10_000).encode()) - 20
        trimmed = board_mod.board_text(self.desk, cap=cap)
        self.assertNotIn("debt row number 1 ", trimmed)
        self.assertIn("debt row number 5", trimmed)

    def test_long_row_is_cut_on_a_character_boundary(self):
        state_mod.inflight_start(self.desk, "é" * 200)
        text = board_mod.board_text(self.desk, cap=10_000)
        text.encode("utf-8").decode("utf-8")  # would raise on a split codepoint
        self.assertIn("…", text)

    def test_board_command_prints_the_page(self):
        proc = self.cli("board")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("== card", proc.stdout)
        self.assertIn("== watchdog", proc.stdout)


# ==========================================================================
# intake
# ==========================================================================
class IntakeTest(DeskCase):
    def test_logs_the_words_verbatim_under_a_number(self):
        kind, n = intake_mod.log(self.desk, "  Ship the health endpoint by Friday.  ")
        self.assertEqual((kind, n), (intake_mod.LOGGED, 1))
        text = read(intake_mod.path(self.desk))
        self.assertIn("## Intake 1 (", text)
        self.assertIn("Ship the health endpoint by Friday.", text)

    def test_number_is_max_plus_one_not_the_count(self):
        """A removed entry must never cause a number to be issued twice: a fresh
        entry and a reissued old number look the same in the phrase 'intake 2'."""
        for i in range(3):
            intake_mod.log(self.desk, f"message {i}")
        p = intake_mod.path(self.desk)
        kept = [b for b in read(p).split("\n## ") if not b.startswith("Intake 2 ")]
        write(p, "\n## ".join(kept))
        self.assertEqual([e.number for e in intake_mod.entries(self.desk)], [1, 3])
        kind, n = intake_mod.log(self.desk, "a new message")
        self.assertEqual((kind, n), (intake_mod.LOGGED, 4))

    def test_duplicate_inside_the_window_is_not_logged(self):
        intake_mod.log(self.desk, "Same words, twice.")
        before = intake_mod.entries(self.desk)
        kind, n = intake_mod.log(self.desk, "  same   WORDS,  twice.  ")
        self.assertEqual((kind, n), (intake_mod.DUPLICATE, 1))
        # No new entry, and the one that stands is untouched.
        self.assertEqual(intake_mod.entries(self.desk), before)
        # But the book carries the marker, because `bellhop readings` counts
        # duplicates from this file: one that only reached a terminal and one that
        # never happened look the same in next week's reading.
        self.assertRegex(read(intake_mod.path(self.desk)), r"\(duplicate of 1, not logged; ")

    def test_a_third_repeat_still_matches_the_first_entry(self):
        """Negative control for the marker: it must not become part of the entry
        body, or the next repeat would no longer match and would be logged as new."""
        intake_mod.log(self.desk, "Same words, three times.")
        intake_mod.log(self.desk, "Same words, three times.")
        kind, n = intake_mod.log(self.desk, "Same words, three times.")
        self.assertEqual((kind, n), (intake_mod.DUPLICATE, 1))
        self.assertEqual([e.number for e in intake_mod.entries(self.desk)], [1])
        self.assertEqual(intake_mod.entries(self.desk)[0].text, "Same words, three times.")

    def test_the_same_words_outside_the_window_are_a_new_message(self):
        """Negative control for the dedupe: it must not swallow a real repeat."""
        long_ago = datetime.now() - timedelta(minutes=90)
        intake_mod.log(self.desk, "Status?", when=long_ago)
        kind, n = intake_mod.log(self.desk, "Status?")
        self.assertEqual((kind, n), (intake_mod.LOGGED, 2))

    def test_skips_slash_commands_empty_prompts_and_machine_output(self):
        p = intake_mod.path(self.desk)
        before = read(p)
        for prompt in ("", "   ", "/status", "Continue from where you left off",
                       "2026-09-11T10:00:00+0800 RED (2) | card=RED inflight=ok",
                       "<system-reminder>read your files</system-reminder>"):
            kind, why = intake_mod.log(self.desk, prompt)
            self.assertEqual(kind, intake_mod.SKIPPED, prompt)
        self.assertEqual(read(p), before, "a skipped prompt must leave the file byte-for-byte")
        self.assertEqual(intake_mod.entries(self.desk), [])

    def test_hook_logs_and_reports_a_duplicate(self):
        first = self.hook("intake", {"cwd": str(self.root), "prompt": "Raise the cap to twenty."})
        self.assertEqual(first.returncode, 0)
        self.assertIn("logged as intake 1", first.stdout)
        again = self.hook("intake", {"cwd": str(self.root), "prompt": "Raise the cap to twenty."})
        self.assertEqual(again.returncode, 0)
        self.assertIn("(duplicate of 1, not logged)", again.stdout)
        self.assertEqual(len(intake_mod.entries(self.reload())), 1)

    def test_hook_is_silent_and_writes_nothing_outside_a_desk(self):
        outside = self.tmp / "elsewhere"
        outside.mkdir()
        before = read(intake_mod.path(self.desk))
        env = dict(os.environ)
        env.pop("BELLHOP_DESK", None)
        proc = subprocess.run(
            [sys.executable, MAIN, "hook", "intake"],
            input=json.dumps({"cwd": str(outside), "prompt": "hello"}),
            text=True, capture_output=True, env=env,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.strip(), "")
        self.assertEqual(read(intake_mod.path(self.desk)), before)

    def test_list_and_show(self):
        intake_mod.log(self.desk, "first message")
        intake_mod.log(self.desk, "second message")
        listed = self.cli("intake", "list", "--last", "1")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertIn("second message", listed.stdout)
        self.assertNotIn("first message", listed.stdout)
        shown = self.cli("intake", "show", "1")
        self.assertIn("first message", shown.stdout)
        missing = self.cli("intake", "show", "9")
        self.assertEqual(missing.returncode, 1)
        self.assertIn("no intake 9", missing.stderr)


# ==========================================================================
# the hand-window gate
# ==========================================================================
class HandGateTest(DeskCase):
    def payload(self, tool, **tool_input):
        return {"cwd": str(self.root), "tool_name": tool, "tool_input": tool_input}

    def test_write_into_a_lane_with_no_window_is_blocked_and_recorded(self):
        repo = self.add_lane()
        target = repo / "src" / "app.py"
        before = target.read_bytes()
        code, message = hand_mod.gate(self.desk, self.payload("Write", file_path=str(target)))
        self.assertEqual(code, 2)
        self.assertIn("lane 'api'", message)
        self.assertIn('bellhop hand open --minutes 30 --reason', message)
        self.assertEqual(target.read_bytes(), before)  # the gate itself writes nothing there
        rows = hand_mod.rows(self.desk)
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0].startswith("- blocked: "))
        self.assertIn("Write", rows[0])

    def test_write_into_a_lane_with_a_window_open_is_allowed_and_recorded(self):
        repo = self.add_lane()
        hand_mod.open_window(self.desk, minutes=30, reason="one-line revert")
        code, message = hand_mod.gate(self.desk, self.payload("Write", file_path=str(repo / "src" / "app.py")))
        self.assertEqual(code, 0)
        self.assertEqual(message, "")
        written = [r for r in hand_mod.rows(self.desk) if r.startswith("- written: ")]
        self.assertEqual(len(written), 1)
        self.assertIn("src/app.py", written[0])

    def test_writes_outside_every_lane_are_not_gated(self):
        """Negative control: the gate must not fire on the desk's own files."""
        self.add_lane()
        code, _ = hand_mod.gate(self.desk, self.payload("Write", file_path=str(self.root / "inbox" / "note.md")))
        self.assertEqual(code, 0)
        self.assertEqual(hand_mod.rows(self.desk), [])

    def test_reads_are_never_gated(self):
        repo = self.add_lane()
        for tool in ("Read", "Grep", "Glob"):
            code, _ = hand_mod.gate(self.desk, self.payload(tool, file_path=str(repo / "src" / "app.py")))
            self.assertEqual(code, 0, tool)
        self.assertEqual(hand_mod.rows(self.desk), [])

    def test_bash_absolute_path_and_cd_are_both_found(self):
        for shape in (
            "sed -i '' s/a/b/ {repo}/src/app.py",
            "cd {repo} && git commit -am fix",
            "cd {repo}/src; python3 - <<'PY' open('app.py','w') PY",
            "cp /etc/hosts {repo}/src/app.py",
        ):
            self.tearDown(); self.setUp()
            repo = self.add_lane()
            command = shape.format(repo=repo)
            code, message = hand_mod.gate(self.desk, self.payload("Bash", command=command))
            self.assertEqual(code, 2, command)
            self.assertIn("lane 'api'", message)

    def test_bash_that_never_touches_a_lane_is_not_gated(self):
        self.add_lane()
        for command in ("git -C . status", "ls receipts", "python3 -m unittest discover tests"):
            code, _ = hand_mod.gate(self.desk, self.payload("Bash", command=command))
            self.assertEqual(code, 0, command)
        self.assertEqual(hand_mod.rows(self.desk), [])

    def test_the_frameworks_own_command_is_never_gated(self):
        repo = self.add_lane()
        code, _ = hand_mod.gate(self.desk, self.payload("Bash", command=f"bellhop merge 017 {repo}"))
        self.assertEqual(code, 0)
        self.assertEqual(hand_mod.rows(self.desk), [])

    def test_command_head_is_capped_at_sixty_characters(self):
        repo = self.add_lane()
        command = f"cd {repo} && " + "echo very-long-command " * 20
        hand_mod.gate(self.desk, self.payload("Bash", command=command))
        row = hand_mod.rows(self.desk)[0]
        head = row.rsplit(" | ", 1)[-1]
        self.assertLessEqual(len(head.rstrip("…")), hand_mod.COMMAND_HEAD_CHARS)
        self.assertTrue(head.endswith("…"))

    def test_an_expired_window_counts_as_closed(self):
        repo = self.add_lane()
        hand_mod.open_window(self.desk, minutes=1, reason="quick fix")
        win = hand_mod.window_path(self.desk)
        write(win, read(win).replace(
            [l for l in read(win).splitlines() if l.startswith("opened_epoch:")][0],
            "opened_epoch: %d" % (time.time() - 3600)))
        self.assertEqual(hand_mod.window(self.desk)["state"], "expired")
        code, message = hand_mod.gate(self.desk, self.payload("Write", file_path=str(repo / "src" / "app.py")))
        self.assertEqual(code, 2)
        self.assertIn("expired", message)
        self.assertTrue(any(r.startswith("- blocked:") and r.endswith("| expired") for r in hand_mod.rows(self.desk)))

    def test_an_unreadable_window_file_counts_as_open(self):
        """A gate that blocks on its own confusion is worse than one that lets an
        edit through: refusing to parse a file is not evidence of a breach."""
        repo = self.add_lane()
        write(hand_mod.window_path(self.desk), "this is not a window\n")
        self.assertEqual(hand_mod.window(self.desk)["state"], "open")
        code, _ = hand_mod.gate(self.desk, self.payload("Write", file_path=str(repo / "src" / "app.py")))
        self.assertEqual(code, 0)

    def test_hand_gate_can_be_switched_off(self):
        repo = self.add_lane()
        self.cfg["hand_gate"] = False
        code, _ = hand_mod.gate(self.reload(), self.payload("Write", file_path=str(repo / "src" / "app.py")))
        self.assertEqual(code, 0)

    def test_a_broken_gate_never_blocks(self):
        repo = self.add_lane()
        payload = self.payload("Write", file_path=str(repo / "src" / "app.py"))
        payload["tool_input"] = "not a dict"  # makes gate() raise
        proc = subprocess.run([sys.executable, MAIN, "hook", "pre-tool"],
                              input=json.dumps(payload), text=True, capture_output=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(any("gate-failed" in r for r in hand_mod.rows(self.reload())))

    def test_hook_exit_code_two_is_what_claude_code_sees(self):
        repo = self.add_lane()
        payload = self.payload("Write", file_path=str(repo / "src" / "app.py"))
        proc = subprocess.run([sys.executable, MAIN, "hook", "pre-tool"],
                              input=json.dumps(payload), text=True, capture_output=True)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("bellhop hand gate", proc.stderr)
        self.assertEqual(proc.stdout, "")


class HandWindowTest(DeskCase):
    def test_open_status_close(self):
        out = hand_mod.open_window(self.desk, minutes=15, reason="one-line revert")
        self.assertIn("15 minutes", out)
        self.assertEqual(hand_mod.window(self.desk)["state"], "open")
        self.assertIn("hand window: open", hand_mod.status_text(self.desk))
        closed = hand_mod.close_window(self.desk)
        self.assertIn("0 write(s) recorded", closed)
        self.assertFalse(hand_mod.window_path(self.desk).exists())
        rows = hand_mod.rows(self.desk)
        self.assertTrue(rows[0].startswith("- opened: "))
        self.assertTrue(rows[-1].startswith("- closed: "))

    def test_a_second_window_is_refused_and_the_first_survives(self):
        hand_mod.open_window(self.desk, minutes=15, reason="first")
        before = read(hand_mod.window_path(self.desk))
        rows_before = hand_mod.rows(self.desk)
        with self.assertRaises(BellhopError) as ctx:
            hand_mod.open_window(self.desk, minutes=15, reason="second")
        self.assertIn("already open", str(ctx.exception))
        self.assertEqual(read(hand_mod.window_path(self.desk)), before)  # byte-for-byte
        self.assertEqual(hand_mod.rows(self.desk), rows_before)

    def test_a_window_needs_a_reason(self):
        with self.assertRaises(BellhopError):
            hand_mod.open_window(self.desk, minutes=15, reason="   ")
        self.assertFalse(hand_mod.window_path(self.desk).exists())
        self.assertEqual(hand_mod.rows(self.desk), [])

    def test_closing_with_no_window_writes_nothing(self):
        """Not an error, and also not a licence to forge a closing row."""
        out = hand_mod.close_window(self.desk)
        self.assertIn("nothing closed", out)
        self.assertEqual(hand_mod.rows(self.desk), [])

    def test_the_closing_row_counts_writes_from_the_ledger_not_from_the_window(self):
        repo = self.add_lane()
        hand_mod.open_window(self.desk, minutes=30, reason="two quick edits")
        for name in ("a.py", "b.py"):
            hand_mod.gate(self.desk, {"cwd": str(self.root), "tool_name": "Write",
                                      "tool_input": {"file_path": str(repo / name)}})
        closed = hand_mod.close_window(self.desk)
        self.assertIn("2 write(s) recorded", closed)
        self.assertIn("| 2 written", hand_mod.rows(self.desk)[-1])

    def test_cli_open_close_status(self):
        opened = self.cli("hand", "open", "--minutes", "5", "--reason", "hotfix")
        self.assertEqual(opened.returncode, 0, opened.stderr)
        status = self.cli("hand", "status")
        self.assertIn("hand window: open", status.stdout)
        closed = self.cli("hand", "close")
        self.assertEqual(closed.returncode, 0, closed.stderr)
        self.assertIn("hand window: none open", self.cli("hand", "status").stdout)


# ==========================================================================
# session start
# ==========================================================================
class SessionStartTest(DeskCase):
    def test_prints_the_reading_list_then_the_board(self):
        state_mod.inflight_start(self.desk, "[api] 017 health endpoint")
        proc = self.hook("session-start", {"cwd": str(self.root), "source": "clear"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Reading list, in order:", proc.stdout)
        self.assertIn("state/next.md", proc.stdout)
        self.assertLess(proc.stdout.index("Reading list"), proc.stdout.index("== card"))
        self.assertIn("[api] 017 health endpoint", proc.stdout)

    def test_silent_outside_a_desk(self):
        outside = self.tmp / "elsewhere"
        outside.mkdir()
        env = dict(os.environ)
        env.pop("BELLHOP_DESK", None)
        proc = subprocess.run([sys.executable, MAIN, "hook", "session-start"],
                              input=json.dumps({"cwd": str(outside)}), text=True,
                              capture_output=True, env=env)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.strip(), "")


# ==========================================================================
# registration
# ==========================================================================
class RegistrationTest(unittest.TestCase):
    def test_plugin_registers_all_four_events(self):
        data = json.loads(read(ROOT / "hooks" / "hooks.json"))["hooks"]
        self.assertEqual(set(data), {"SessionStart", "UserPromptSubmit", "PreToolUse", "Stop"})
        self.assertEqual(data["SessionStart"][0]["matcher"], "startup|clear|compact")
        self.assertEqual(data["PreToolUse"][0]["matcher"], "Edit|MultiEdit|Write|NotebookEdit|Bash")
        for event, entries in data.items():
            for entry in entries:
                for hook in entry["hooks"]:
                    self.assertIn("bellhop_main.py", hook["command"])

    def test_template_settings_register_the_same_four(self):
        data = json.loads(read(ROOT / "template" / ".claude" / "settings.json"))["hooks"]
        self.assertEqual(set(data), {"SessionStart", "UserPromptSubmit", "PreToolUse", "Stop"})
        self.assertEqual(data["SessionStart"][0]["matcher"], "startup|clear|compact")
        self.assertEqual(data["PreToolUse"][0]["matcher"], "Edit|MultiEdit|Write|NotebookEdit|Bash")
        commands = [h["command"] for entries in data.values() for e in entries for h in e["hooks"]]
        self.assertEqual(sorted(commands), [
            "bellhop hook intake", "bellhop hook pre-tool",
            "bellhop hook session-start", "bellhop hook stop",
        ])

    def test_template_headers_match_what_the_code_writes(self):
        """A file created by `bellhop init` and one created by the hook must be the
        same file. Two headers that drift are two formats nobody declared."""
        self.assertEqual(read(ROOT / "template" / "state" / "intake.md"), intake_mod.HEADER)
        self.assertEqual(read(ROOT / "template" / "state" / "hand-ledger.md"), hand_mod.LEDGER_HEADER)


if __name__ == "__main__":
    unittest.main()
