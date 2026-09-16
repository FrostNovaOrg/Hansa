"""`bellhop dispatch`: one command for the fixed half of sending a task out.

Every gate here has a positive case and a negative control, because each of them
guards a pair of states that look identical from the outside: a packet with a stale
base and one with a fresh base, a bell that rang with no in-flight row and a task
that was never sent, a stop order that held and one that was talked past.
"""

import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bellhop import cli  # noqa: E402
from bellhop import dispatch as dispatch_mod  # noqa: E402
from bellhop import desk_ops, lanes, packet as packet_mod, state as state_mod  # noqa: E402
from bellhop.config import Desk  # noqa: E402
from bellhop.util import read, read_json, write, write_json  # noqa: E402

GIT_ID = ["-c", "user.name=test", "-c", "user.email=test@example.invalid"]

BODY = """## What to do

Add a short note file describing the health check. Keep the checks green.

## Do not touch

`check.sh`, and the lane rules.

## Acceptance

- ./check.sh prints 'check.sh: all green'
- a file named note.md exists in the working tree
"""


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *GIT_ID, *args],
                          check=True, capture_output=True, text=True).stdout.strip()


class DispatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bellhop-dispatch."))
        self.repo = self.tmp / "widget-service"
        shutil.copytree(ROOT / "examples" / "widget-service", self.repo)
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "initial")
        self.base = git(self.repo, "rev-parse", "--short", "HEAD")
        self.desk_dir = self.tmp / "desk"
        os.environ["GIT_AUTHOR_NAME"] = os.environ["GIT_COMMITTER_NAME"] = "test"
        os.environ["GIT_AUTHOR_EMAIL"] = os.environ["GIT_COMMITTER_EMAIL"] = "test@example.invalid"
        desk_ops.init_desk(self.desk_dir)
        cfg = read_json(self.desk_dir / "bellhop.json")
        cfg["lanes"]["api"]["repo"] = "../widget-service"
        write(self.desk_dir / "bellhop.json", json.dumps(cfg, indent=2))
        git(self.desk_dir, "add", "-A")
        git(self.desk_dir, "commit", "-q", "-m", "configure")
        self.desk = Desk(self.desk_dir, cfg)
        self.body = self.tmp / "body.md"
        write(self.body, BODY)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- helpers ----------------------------------------------------------

    def run_cli(self, *argv):
        """(exit code, stdout). `bellhop dispatch` always exits with its own code."""
        buf, err = io.StringIO(), io.StringIO()
        code = 0
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
            try:
                code = cli.main(["--desk", str(self.desk_dir), *argv]) or 0
            except SystemExit as exc:
                code = exc.code or 0
        return code, buf.getvalue() + err.getvalue()

    def dispatch(self, title="Add health check note", body=None, *extra):
        return self.run_cli("dispatch", "--lane", "api", "--title", title,
                            "--body", str(body or self.body), *extra)

    def body_file(self, text, name="other.md"):
        p = self.tmp / name
        write(p, text)
        return p

    def four_lines(self, out):
        return {int(m.group(1)): m.group(2).strip()
                for m in re.finditer(r"^(\d) \w+\s+(.*)$", out, flags=re.M)}

    def clear_bells(self):
        """Pretend the task in flight came back and was merged."""
        for bell in self.desk.bells_pending.glob("*.bell"):
            bell.rename(self.desk.bells_rung / bell.name)

    def lane_worktree(self, base=None):
        return lanes.prepare_worktree(self.desk, self.desk.lane("api"), base or self.base)

    def inflight(self):
        return read(self.desk.path("state/in-flight.md"))

    def card(self):
        return state_mod.read_card_keys(self.desk)

    # -- the happy path and the four lines --------------------------------

    def test_dispatch_writes_packet_bell_inflight_and_card(self):
        code, out = self.dispatch("Add health check note", None,
                                  "--inflight", "first task on the lane",
                                  "--next", "002 wire the metrics reader")
        self.assertEqual(code, 0, out)
        lines = self.four_lines(out)
        self.assertEqual(lines[1], "inbox/api/001-add-health-check-note.md")
        self.assertTrue(lines[2].endswith("-001.bell"), lines[2])
        self.assertIn("base " + self.base, lines[3])
        self.assertIn("first task on the lane", lines[3])
        self.assertEqual(lines[4], "next: 002 wire the metrics reader")
        self.assertTrue(out.rstrip().endswith(f"✅ dispatched 001 {lines[2]}"), out)
        # every one of the four is a real file, not a sentence about one
        pkt = packet_mod.parse_packet(self.desk_dir / lines[1])
        self.assertEqual((pkt.id, pkt.lane, pkt.base), ("001", "api", self.base))
        self.assertEqual(pkt.acceptance,
                         ["./check.sh prints 'check.sh: all green'",
                          "a file named note.md exists in the working tree"])
        self.assertTrue((self.desk.bells_pending / lines[2]).exists())
        self.assertIn("- 001 api Add health check note |", self.inflight())
        self.assertEqual(self.card()["next"], "002 wire the metrics reader")

    def test_four_lines_are_read_back_from_the_files(self):
        self.dispatch()
        state_mod.inflight_start(self.desk, "002 api something else")
        _code, out = self.dispatch("Second thing", self.body, "--continue", "001")
        row = self.four_lines(out)[3]
        self.assertIn(row.lstrip("- "), self.inflight())

    def test_body_and_head_and_tail_are_three_parts(self):
        self.dispatch()
        text = read(self.desk.inbox / "api" / "001-add-health-check-note.md")
        self.assertIn("# 001 — Add health check note", text)          # generated head
        self.assertIn("Add a short note file describing the health", text)  # body verbatim
        self.assertIn("## How to report (fixed)", text)               # generated tail
        self.assertIn("receipts/001-add-health-check-note.md", text)
        self.assertIn("bellhop receipt-header", text)
        self.assertIn("`build.sh`", text)                             # the forbidden list

    # -- the base is never typed ------------------------------------------

    def test_base_is_the_lane_head_at_dispatch_time(self):
        self.dispatch()
        first = packet_mod.parse_packet(self.desk.inbox / "api" / "001-add-health-check-note.md").base
        self.clear_bells()
        write(self.repo / "moved.txt", "the lane moved on\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "second commit")
        moved = git(self.repo, "rev-parse", "--short", "HEAD")
        self.dispatch("Second thing")
        second = packet_mod.parse_packet(self.desk.inbox / "api" / "002-second-thing.md").base
        self.assertEqual(first, self.base)
        self.assertEqual(second, moved)
        self.assertNotEqual(first, second)

    def test_no_flag_can_set_the_base_or_the_id(self):
        for flag in ("--base", "--id"):
            with self.assertRaises(SystemExit) as caught:
                with contextlib.redirect_stderr(io.StringIO()):
                    cli.build_parser().parse_args(
                        ["--desk", str(self.desk_dir), "dispatch", "--lane", "api",
                         "--title", "x", "--body", str(self.body), flag, "whatever"])
            self.assertEqual(caught.exception.code, 2)

    def test_ids_come_from_the_counter_and_never_go_backwards(self):
        self.dispatch()
        self.clear_bells()
        self.dispatch("Second thing")
        self.assertEqual(packet_mod.next_id(self.desk), "003")
        # the packet is archived out of the inbox but its receipt stays: the counter
        # must not hand 002 out again, or two receipts share one id.
        write(self.desk.receipts / "002-second-thing.md", "# receipt\n")
        (self.desk.inbox / "api" / "002-second-thing.md").unlink()
        self.assertEqual(packet_mod.next_id(self.desk), "003")

    # -- the lane gates ----------------------------------------------------

    def test_refuses_while_a_bell_is_pending_for_the_lane(self):
        self.dispatch()
        code, out = self.dispatch("Second thing")
        self.assertEqual(code, 2)
        self.assertIn("a bell is still pending for packet 001", out)
        self.assertFalse((self.desk.inbox / "api" / "002-second-thing.md").exists())
        # control: once it is answered, the same command goes through
        self.clear_bells()
        code, out = self.dispatch("Second thing")
        self.assertEqual(code, 0, out)

    def test_refuses_a_dirty_lane_worktree(self):
        wt = self.lane_worktree()
        write(wt / "half-finished.txt", "someone was in the middle of this\n")
        code, out = self.dispatch()
        self.assertEqual(code, 2)
        self.assertIn("uncommitted path(s)", out)
        self.assertFalse(any(self.desk.inbox.rglob("*.md")))
        # control: a clean worktree is not a blocker
        (wt / "half-finished.txt").unlink()
        code, out = self.dispatch()
        self.assertEqual(code, 0, out)

    def test_refuses_commits_the_lane_has_not_merged(self):
        wt = self.lane_worktree()
        write(wt / "done.txt", "finished work waiting for a merge\n")
        git(wt, "add", "-A")
        git(wt, "commit", "-q", "-m", "lane work")
        code, out = self.dispatch()
        self.assertEqual(code, 2)
        self.assertIn("commit(s) not on main", out)
        # control: merge it and the lane is free again
        git(self.repo, "merge", "-q", "--no-edit", "lane/api")
        code, out = self.dispatch()
        self.assertEqual(code, 0, out)

    def test_unknown_lane_is_refused_before_anything_is_written(self):
        code, out = self.run_cli("dispatch", "--lane", "nope", "--title", "x",
                                 "--body", str(self.body))
        self.assertEqual(code, 2)
        self.assertIn("unknown lane", out)
        self.assertFalse(any(self.desk.inbox.rglob("*.md")))

    # -- continue and return -----------------------------------------------

    def test_continue_names_the_task_in_flight_and_reuses_its_base(self):
        self.dispatch()
        first = self.desk.inbox / "api" / "001-add-health-check-note.md"
        wt = self.lane_worktree()
        write(wt / "half-finished.txt", "the worker stopped here\n")
        write(self.repo / "moved.txt", "main moved on meanwhile\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "main moves")
        code, out = self.dispatch("Continue 001", self.body, "--continue", "001")
        self.assertEqual(code, 0, out)
        self.assertIn("waived by --continue 001", out)
        second = packet_mod.parse_packet(self.desk.inbox / "api" / "002-continue-001.md")
        self.assertEqual(second.base, packet_mod.parse_packet(first).base)
        self.assertEqual(second.extra.get("continues"), "001")
        self.assertEqual(second.extra.get("continues_receipt"), "none")

    def test_continue_refuses_a_task_that_reported(self):
        self.dispatch()
        write(self.desk.receipts / "001-add-health-check-note.md", "# receipt\n")
        code, out = self.dispatch("Continue 001", self.body, "--continue", "001")
        self.assertEqual(code, 2)
        self.assertIn("it did not die", out)
        self.assertIn("--return 001", out)

    def test_return_refuses_a_task_that_never_reported(self):
        self.dispatch()
        code, out = self.dispatch("Return 001", self.body, "--return", "001")
        self.assertEqual(code, 2)
        self.assertIn("no receipt", out)
        # control: with a receipt on the desk the same command goes through
        write(self.desk.receipts / "001-add-health-check-note.md", "# receipt\n")
        write_json(self.desk.receipts / "001.meta.json", {
            "packet_sha256": packet_mod.parse_packet(
                self.desk.inbox / "api" / "001-add-health-check-note.md").sha256})
        code, out = self.dispatch("Return 001", self.body, "--return", "001")
        self.assertEqual(code, 0, out)
        pkt = packet_mod.parse_packet(self.desk.inbox / "api" / "002-return-001.md")
        self.assertEqual(pkt.extra.get("returns"), "001")
        self.assertEqual(pkt.extra.get("returns_receipt"), "receipts/001-add-health-check-note.md")

    def test_continue_and_return_are_opposites(self):
        # argparse refuses the pair at the CLI; the module refuses it too, because a
        # caller that reached past the parser would otherwise get one of them silently.
        code, out = self.dispatch("x", self.body, "--continue", "001", "--return", "001")
        self.assertEqual(code, 2)
        report = dispatch_mod.dispatch(self.desk, "api", "x", self.body,
                                       continue_id="001", return_id="001")
        self.assertEqual(report.code, 2)
        self.assertIn("opposites", report.last)

    def test_only_the_named_headings_are_carried(self):
        """A heading is carried because it is one of the two names, not because a word
        in it sounds like a prohibition."""
        body = ("## Forbidden paths\n\nkeyword match, not a name\n\n"
                "## Forbids\n\nthe named one\n\n"
                "## Do not touch\n\nthe template's name for it\n")
        section, rest = packet_mod.packet_section(body, packet_mod.FORBID_HEADINGS)
        self.assertIn("the named one", section)
        self.assertIn("the template's name for it", section)
        self.assertNotIn("keyword match", section)
        self.assertIn("Forbidden paths", rest)
        carried, _ = packet_mod.packet_section("## Required context\n\nexcerpt\n",
                                               packet_mod.CONTEXT_HEADINGS)
        self.assertIn("excerpt", carried)

    def test_the_return_note_names_only_what_was_inherited(self):
        """The first version said "acceptance, forbids and required context inherited"
        on every return, so a packet that had neither section and one that had both
        left the same line behind."""
        plain = self.body_file("## What to do\n\nAdd a note.\n\n## Acceptance\n\n"
                               "- ./check.sh prints 'check.sh: all green'\n", "plain.md")
        code, out = self.dispatch("Original plain", plain)
        self.assertEqual(code, 0, out)
        prior = packet_mod.parse_packet(self.desk.inbox / "api" / "001-original-plain.md")
        write(self.desk.receipts / "001-original-plain.md", "# receipt\n")
        write_json(self.desk.receipts / "001.meta.json", {"packet_sha256": prior.sha256})
        again = self.body_file("## Why returned\n\nThe note is missing.\n", "again.md")
        code, out = self.dispatch("Return plain", again, "--return", "001")
        self.assertEqual(code, 0, out)
        self.assertIn("↩ inherited from the original packet: acceptance", out)
        self.assertNotIn("forbids", out)
        self.assertNotIn("required context", out)

    def test_return_inherits_required_material_without_retyping_criteria(self):
        body = self.body_file(BODY + "\n## Required context\n\n### Approved source\nRetry limit: 3.\n")
        code, out = self.dispatch("Original", body)
        self.assertEqual(code, 0, out)
        original = self.desk.inbox / "api" / "001-original.md"
        before = read(original)
        prior = packet_mod.parse_packet(original)
        write(self.desk.receipts / "001-original.md", "# receipt\n")
        write_json(self.desk.receipts / "001.meta.json", {"packet_sha256": prior.sha256})
        returned = self.body_file("## Criteria (unchanged)\nSee original.\n\n"
                                  "## Why returned\nThe note is missing.\n\n"
                                  "## Change\nAdd the note.\n\n"
                                  "## Do not change\nKeep existing behavior.\n", "return.md")
        code, out = self.dispatch("Return original", returned, "--return", "001")
        self.assertEqual(code, 0, out)
        new = packet_mod.parse_packet(self.desk.inbox / "api" / "002-return-original.md")
        self.assertEqual(new.acceptance, prior.acceptance)
        self.assertEqual(new.base, prior.base)
        self.assertIn("Retry limit: 3.", new.body)
        self.assertIn("`check.sh`, and the lane rules.", new.body)
        self.assertEqual(read(original), before)
        changed = self.body_file(BODY.replace("note.md", "different.md"), "changed.md")
        code, out = self.dispatch("Changed criteria", changed, "--return", "001")
        self.assertEqual(code, 2)
        self.assertIn("keep the original acceptance", out)
        write(original, before + "\nAnother requirement.\n")
        code, out = self.dispatch("Changed packet", returned, "--return", "001")
        self.assertEqual(code, 2)
        self.assertIn("return packet provenance", out)

    def test_continue_needs_a_packet_that_exists(self):
        code, out = self.dispatch("x", self.body, "--continue", "042")
        self.assertEqual(code, 2)
        self.assertIn("0 packet(s) match id 042", out)

    # -- the forbidden-command gate ----------------------------------------

    def test_forbidden_command_in_the_body_is_refused(self):
        bad = self.body_file(BODY.replace("Keep the checks green.", "Then run build.sh to publish."))
        code, out = self.dispatch("Bad thing", bad)
        self.assertEqual(code, 2)
        self.assertIn("build.sh", out)
        self.assertFalse(any(self.desk.inbox.rglob("*.md")))
        # control: the same body without that line dispatches
        code, out = self.dispatch("Good thing")
        self.assertEqual(code, 0, out)

    def test_allow_waives_one_pattern_for_one_packet_and_stores_nothing(self):
        bad = self.body_file(BODY.replace("Keep the checks green.", "Then run build.sh to publish."))
        before = read(self.desk_dir / "bellhop.json")
        code, out = self.dispatch("Wire the build script", bad, "--allow", "build.sh")
        self.assertEqual(code, 0, out)
        self.assertIn("⚠ allowed for this packet only: build.sh", out)
        self.assertIn("nothing is stored", out)
        self.assertEqual(read(self.desk_dir / "bellhop.json"), before)
        # control: the next packet is gated again
        self.clear_bells()
        code, out = self.dispatch("Second bad thing", bad)
        self.assertEqual(code, 2)

    def test_allow_refuses_a_pattern_nobody_configured(self):
        code, out = self.dispatch("x", self.body, "--allow", "rm -rf")
        self.assertEqual(code, 2)
        self.assertIn("names no configured pattern", out)

    def test_forbidden_patterns_are_configurable(self):
        cfg = read_json(self.desk_dir / "bellhop.json")
        cfg["packet_forbidden_patterns"] = [r"ship-it\.sh"]
        write(self.desk_dir / "bellhop.json", json.dumps(cfg, indent=2))
        code, _out = self.dispatch("Uses build sh",
                                   self.body_file(BODY + "\nRun build.sh at the end.\n"))
        self.assertEqual(code, 0)   # no longer on the list
        self.clear_bells()
        code, out = self.dispatch("Uses ship it",
                                  self.body_file(BODY + "\nRun ship-it.sh at the end.\n", "s.md"))
        self.assertEqual(code, 2)
        self.assertIn("ship-it.sh", out)

    # -- the body's own shape ---------------------------------------------

    def test_body_may_not_carry_its_own_front_matter(self):
        bad = self.body_file("---\nid: 999\nlane: api\n---\n" + BODY)
        code, out = self.dispatch("Pasted packet", bad)
        self.assertEqual(code, 2)
        self.assertIn("front matter", out)
        self.assertFalse(any(self.desk.inbox.rglob("*.md")))
        code, out = self.dispatch("Plain body")     # control
        self.assertEqual(code, 0, out)

    def test_body_without_acceptance_items_is_refused(self):
        bad = self.body_file("## What to do\n\nSomething, somehow.\n")
        code, out = self.dispatch("No checks", bad)
        self.assertEqual(code, 2)
        self.assertIn("Acceptance", out)
        # control: an Acceptance heading with bullets lifts them into the front matter
        good = self.body_file("## What to do\n\nSomething.\n\n## Acceptance\n\n- ./check.sh exits 0\n", "g.md")
        code, out = self.dispatch("With checks", good)
        self.assertEqual(code, 0, out)
        pkt = packet_mod.parse_packet(self.desk.inbox / "api" / "001-with-checks.md")
        self.assertEqual(pkt.acceptance, ["./check.sh exits 0"])

    def test_empty_body_is_refused(self):
        code, out = self.dispatch("Nothing", self.body_file("   \n"))
        self.assertEqual(code, 2)
        self.assertIn("empty", out)

    def test_oversized_body_warns_but_still_dispatches(self):
        big = self.body_file(BODY + "\n" + ("filler sentence about the task. " * 200))
        code, out = self.dispatch("Big one", big)
        self.assertEqual(code, 0, out)
        self.assertIn("over the 3000-byte guide", out)
        self.clear_bells()
        code, out = self.dispatch("Small one")     # control
        self.assertEqual(code, 0, out)
        self.assertNotIn("byte guide", out)

    # -- the stop-dispatch switch ------------------------------------------

    def stop_order(self, text="# Stop dispatch\n\nPrincipal stopped dispatch on Tuesday.\n"):
        p = self.desk.path("state/stop-dispatch.md")
        write(p, text)
        return p

    def test_stop_order_refuses_dispatch(self):
        stop = self.stop_order()
        code, out = self.dispatch()
        self.assertEqual(code, 2)
        self.assertIn("dispatch is stopped", out)
        self.assertIn("--exception", out)
        self.assertFalse(any(self.desk.inbox.rglob("*.md")))
        self.assertEqual(dispatch_mod.exception_count(stop), 0)
        # control: lift the order and the same command goes through
        stop.unlink()
        code, out = self.dispatch()
        self.assertEqual(code, 0, out)

    def test_exceptions_are_numbered_counted_and_kept_in_the_order_file(self):
        stop = self.stop_order()
        code, out = self.dispatch("First exception", self.body, "--exception", "hotfix for the outage")
        self.assertEqual(code, 0, out)
        self.assertIn("⚠ exception 1 under stop order", out)
        self.clear_bells()
        code, out = self.dispatch("Second exception", self.body, "--exception", "one more, honestly the last")
        self.assertEqual(code, 0, out)
        self.assertIn("⚠ exception 2 under stop order", out)
        text = read(stop)
        self.assertIn("## Exceptions", text)
        self.assertIn("hotfix for the outage", text)
        self.assertIn("one more, honestly the last", text)
        self.assertEqual(dispatch_mod.exception_count(stop), 2)
        self.assertIn("001 api First exception", text)

    def test_a_refused_dispatch_records_no_exception(self):
        stop = self.stop_order()
        code, out = self.dispatch("Blocked", self.body_file("## What to do\n\nno checks here\n"),
                                  "--exception", "urgent")
        self.assertEqual(code, 2)
        self.assertEqual(dispatch_mod.exception_count(stop), 0)

    def test_demo_under_a_stop_order_looks_but_excepts_nothing(self):
        stop = self.stop_order()
        code, out = self.dispatch("Just looking", self.body, "--demo")
        self.assertEqual(code, 0, out)
        self.assertIn("stop-dispatch order in force", out)
        self.assertEqual(dispatch_mod.exception_count(stop), 0)
        code, out = self.dispatch("Contradiction", self.body, "--demo", "--exception", "why")
        self.assertEqual(code, 2)
        self.assertIn("nothing to except", out)

    # -- demo ---------------------------------------------------------------

    def test_demo_renders_and_writes_nothing(self):
        before_card = read(self.desk.path("state/next.md"))
        before_flight = self.inflight()
        code, out = self.dispatch("Look at this", self.body, "--demo",
                                  "--inflight", "not going to happen", "--next", "999 nope")
        self.assertEqual(code, 0, out)
        self.assertIn("## How to report (fixed)", out)
        self.assertIn("id: 001", out)
        self.assertFalse(any(self.desk.inbox.rglob("*.md")))
        self.assertFalse(any(self.desk.bells_pending.glob("*.bell")))
        self.assertEqual(read(self.desk.path("state/next.md")), before_card)
        self.assertEqual(self.inflight(), before_flight)
        self.assertIn("nothing written", out)
        # control: the same command without --demo writes all four
        code, out = self.dispatch("Look at this", self.body,
                                  "--inflight", "this one is real", "--next", "999 yes")
        self.assertEqual(code, 0, out)
        self.assertTrue((self.desk.inbox / "api" / "001-look-at-this.md").exists())
        self.assertNotEqual(self.inflight(), before_flight)

    # -- heavy --------------------------------------------------------------

    def test_heavy_marks_the_packet(self):
        code, out = self.dispatch("Heavy one", self.body, "--heavy")
        self.assertEqual(code, 0, out)
        pkt = packet_mod.parse_packet(self.desk.inbox / "api" / "001-heavy-one.md")
        self.assertEqual(pkt.extra.get("effort"), "heavy")
        self.clear_bells()
        code, out = self.dispatch("Light one")     # control
        self.assertEqual(code, 0, out)
        light = packet_mod.parse_packet(self.desk.inbox / "api" / "002-light-one.md")
        self.assertNotIn("effort", light.extra)

    # -- ring ---------------------------------------------------------------

    def test_ring_commits_the_packet_and_the_bell(self):
        code, out = self.dispatch()
        self.assertEqual(code, 0, out)
        subject = git(self.desk_dir, "log", "-1", "--format=%s")
        self.assertIn("ring 001", subject)
        tracked = git(self.desk_dir, "ls-files")
        self.assertIn("inbox/api/001-add-health-check-note.md", tracked)

    def test_a_bell_already_taken_stops_at_ring_and_leaves_in_flight_alone(self):
        before_flight, before_card = self.inflight(), read(self.desk.path("state/next.md"))
        # a bell whose file name carries no id (hand-copied, say): the counter cannot see
        # it, so the collision this step exists to catch is the one that reaches ring.
        stale = self.desk.bells_pending / "stale.bell"
        write_json(stale, {"id": "001", "packet": "inbox/api/001-x.md"})
        code, out = self.dispatch("Add health check note", None, "--next", "002 never set")
        self.assertEqual(code, 3)
        self.assertIn("stopped at: ring", out)
        self.assertIn("in flight and the card were not touched", out)
        self.assertEqual(self.inflight(), before_flight)
        self.assertEqual(read(self.desk.path("state/next.md")), before_card)
        # control: without that bell the same command rings and the chain completes
        stale.unlink()
        (self.desk.inbox / "api" / "001-add-health-check-note.md").unlink()
        code, out = self.dispatch("Add health check note", None, "--next", "002 now set")
        self.assertEqual(code, 0, out)
        self.assertNotEqual(self.inflight(), before_flight)

    def test_a_rung_bell_or_a_worker_log_also_counts_as_taken(self):
        pkt = self.desk.inbox / "api" / "001-x.md"
        self.assertIsNone(dispatch_mod._bell_taken(self.desk, "001", pkt))
        write_json(self.desk.bells_rung / "20260101-000000-001.bell", {"id": "001"})
        self.assertIn("already run", dispatch_mod._bell_taken(self.desk, "001", pkt))
        (self.desk.bells_rung / "20260101-000000-001.bell").unlink()
        write(self.desk.bells_logs / "20260101-000000-001.log", "worker output\n")
        self.assertIn("worker log", dispatch_mod._bell_taken(self.desk, "001", pkt))

    # -- in flight ----------------------------------------------------------

    def test_in_flight_start_is_idempotent(self):
        self.dispatch()
        rows = self.inflight().count("- 001 api")
        line = dispatch_mod.inflight_text("001", "api", "Add health check note", self.base, "bell-1")
        row, skipped = dispatch_mod.start_inflight(self.desk, "001", line)
        self.assertTrue(skipped)
        self.assertEqual(self.inflight().count("- 001 api"), rows)
        # control: a different id is a different row
        row, skipped = dispatch_mod.start_inflight(
            self.desk, "002", dispatch_mod.inflight_text("002", "api", "Other", self.base, "bell-2"))
        self.assertFalse(skipped)
        self.assertIn("- 002 api Other", self.inflight())

    def test_a_long_sentence_is_trimmed_and_the_base_and_bell_survive(self):
        line = dispatch_mod.inflight_text("001", "api", "Add health check note", self.base,
                                          "20260911-010203-001", "x" * 400, cap=160)
        self.assertLessEqual(len("- " + line), 160)
        self.assertIn(f"base {self.base}", line)
        self.assertIn("bell 20260911-010203-001", line)
        self.assertTrue(line.endswith("…"))
        # control: a short sentence is kept whole
        short = dispatch_mod.inflight_text("001", "api", "T", self.base, "b", "all of it", cap=160)
        self.assertTrue(short.endswith("all of it"))

    # -- the card -----------------------------------------------------------

    def test_the_card_is_touched_only_with_next(self):
        before = read(self.desk.path("state/next.md"))
        code, out = self.dispatch()
        self.assertEqual(code, 0, out)
        self.assertEqual(read(self.desk.path("state/next.md")), before)
        self.assertIn("(not touched)", self.four_lines(out)[4])
        # control: with --next the card moves and the fourth line says so
        self.clear_bells()
        code, out = self.dispatch("Second thing", self.body, "--next", "003 the next one")
        self.assertEqual(code, 0, out)
        self.assertNotEqual(read(self.desk.path("state/next.md")), before)
        self.assertEqual(self.four_lines(out)[4], "next: 003 the next one")


if __name__ == "__main__":
    unittest.main()
