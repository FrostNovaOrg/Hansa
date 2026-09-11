"""Tests for the acceptance pipeline: isolation, the case pack, the session, the shadow.

Every gate gets a positive case and a negative control. The pipeline exists because a
run that stalled and a run that judged the work looked the same from outside, so a test
that only ever drives the stub's good path would reproduce exactly that mistake.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bellhop import accept as accept_mod  # noqa: E402
from bellhop import desk_ops, headers, ledger, ring, watch  # noqa: E402
from bellhop.config import Desk  # noqa: E402
from bellhop.packet import parse_packet  # noqa: E402
from bellhop.util import read, read_json, write, write_json  # noqa: E402

GIT_ID = ["-c", "user.name=test", "-c", "user.email=test@example.invalid"]
STUB = f"python3 {ROOT}/bellhop/stubaccept.py"


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *GIT_ID, *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def alive(pid):
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


class AcceptTest(unittest.TestCase):
    def setUp(self):
        os.environ["GIT_AUTHOR_NAME"] = os.environ["GIT_COMMITTER_NAME"] = "test"
        os.environ["GIT_AUTHOR_EMAIL"] = os.environ["GIT_COMMITTER_EMAIL"] = "test@example.invalid"
        self.tmp = Path(tempfile.mkdtemp(prefix="bellhop-accept-test.")).resolve()
        self.repo = self.tmp / "widget-service"
        shutil.copytree(ROOT / "examples" / "widget-service", self.repo)
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "initial")
        self.base = git(self.repo, "rev-parse", "HEAD")
        self.desk_dir = self.tmp / "desk"
        desk_ops.init_desk(self.desk_dir)
        self.cfg = read_json(self.desk_dir / "bellhop.json")
        self.cfg["lanes"]["api"]["repo"] = "../widget-service"
        self.cfg["lanes"]["api"]["check_cmd"] = "./check.sh"
        self.cfg["lanes"]["api"]["worker_cmd"] = f"python3 {ROOT}/bellhop/stubworker.py"
        self.cfg["lanes"]["api"]["accept_cmd"] = f"{STUB} --good"
        self.write_cfg()
        self.desk = Desk(self.desk_dir, self.cfg)
        self.packet_path = self.write_packet()
        # One real trip round the loop, so the receipt carries a machine header and the
        # lane branch carries the commit the verifier will be shown a copy of.
        ring.ring(self.desk, self.packet_path)
        watch.watch_once(self.desk, archive=False)
        self.receipt = self.desk.receipts / "001-add-a-note-file.md"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- fixtures ----------------------------------------------------------
    def write_cfg(self):
        write(self.desk_dir / "bellhop.json", json.dumps(self.cfg, indent=2))

    def write_packet(self, pid="001", title="Add a note file"):
        text = (
            f"---\nid: {pid}\nlane: api\ntitle: {title}\nbase: {self.base}\nbudget_minutes: 10\n"
            "acceptance:\n  - the note file exists\n  - the checks stay green\n---\n"
            "## What to do\n\nAdd a note file.\n\n## Do not touch\n\nThe release script.\n"
        )
        p = self.desk.inbox / "api" / f"{pid}-note.md"
        write(p, text)
        return p

    def desk_with(self, **cfg):
        """A desk whose config carries these overrides, on disk too, so the CLI sees them."""
        for key, value in cfg.items():
            if key == "accept_cmd":
                self.cfg["lanes"]["api"]["accept_cmd"] = value
            else:
                self.cfg[key] = value
        self.write_cfg()
        self.desk = Desk(self.desk_dir, self.cfg)
        return self.desk

    def run_accept(self, switches="--good", force=True, **cfg):
        """Run the pipeline with these stub switches. `force` is on because most tests
        pair a case with its control on the same task id."""
        desk = self.desk_with(accept_cmd=f"{STUB} {switches}", **cfg)
        return accept_mod.run_accept(desk, "001", force=force)

    def pack(self, shadow=False):
        return read(accept_mod.pack_path(self.desk, "001", shadow))

    def closings(self, shadow=False):
        p = accept_mod.closings_path(self.desk, shadow)
        return read(p) if p.exists() else ""

    def accept_closings(self, shadow=False):
        """Only the lines `bellhop accept` wrote. `state/closings.md` is shared: the
        watchdog's settle-bell writes its own close-outs there, in the state module's
        shape (`- <time> [<task>] ...`), and the two registers must not be read as one."""
        return [l for l in self.closings(shadow).splitlines() if accept_mod.parse_closing(l)]

    def cli(self, *args):
        env = dict(os.environ, BELLHOP_DESK=str(self.desk_dir))
        return subprocess.run([str(ROOT / "bin" / "bellhop"), *args], env=env,
                              capture_output=True, text=True)

    def worktrees(self):
        return [l for l in git(self.repo, "worktree", "list").splitlines() if l.strip()]

    # -- 1. isolation ------------------------------------------------------
    def test_the_verifier_never_runs_in_the_lane_worktree(self):
        meta = self.run_accept("--good --json")
        lane_wt = Path(self.desk.lane("api")["worktree"])
        self.assertNotEqual(Path(meta["copy"]).resolve(), lane_wt.resolve())
        self.assertFalse(str(lane_wt) in meta["copy"])
        self.assertEqual(git(lane_wt, "status", "--porcelain"), "")
        self.assertIsNone(meta["stall"])

    def test_a_verifier_that_writes_in_the_copy_is_a_stall_not_a_verdict(self):
        meta = self.run_accept("--touch-tree --good")
        self.assertEqual(meta["step"], "recheck")
        self.assertEqual(meta["stall"], accept_mod.STALL_TOUCHED)
        self.assertIn("verifier-scratch.txt", meta["reason"])
        self.assertIsNone(meta["verdict_path"])
        # negative control: the same stub without the switch produces a verdict
        meta = self.run_accept("--good")
        self.assertIsNone(meta["stall"])
        self.assertEqual(meta["verdict"], "pass")

    def test_a_verifier_that_commits_moves_the_tree_hash_and_is_caught(self):
        """`git status` stays empty after a commit; the tree hash is what tells."""
        meta = self.run_accept("--commit-tree --good")
        self.assertEqual(meta["status_after"], "")
        self.assertNotEqual(meta["tree_before"], meta["tree_after"])
        self.assertEqual(meta["stall"], accept_mod.STALL_TOUCHED)
        # negative control: an honest run leaves both readings equal
        meta = self.run_accept("--good")
        self.assertEqual(meta["tree_before"], meta["tree_after"])
        self.assertEqual(meta["status_after"], "")
        self.assertIsNone(meta["stall"])

    def test_the_copy_is_removed_after_a_verdict_and_after_a_stall(self):
        before = len(self.worktrees())
        meta = self.run_accept("--good")
        self.assertFalse(Path(meta["copy"]).exists(), "the copy outlived a clean run")
        self.assertEqual(len(self.worktrees()), before)
        meta = self.run_accept("--touch-tree --good")
        self.assertFalse(Path(meta["copy"]).exists(), "the copy outlived a stall")
        self.assertEqual(len(self.worktrees()), before)

    def test_the_lane_tree_is_untouched_even_when_the_verifier_writes(self):
        lane_wt = Path(self.desk.lane("api")["worktree"])
        head_before = git(lane_wt, "rev-parse", "HEAD")
        self.run_accept("--commit-tree --good")
        self.assertEqual(git(lane_wt, "rev-parse", "HEAD"), head_before)
        self.assertEqual(git(lane_wt, "status", "--porcelain"), "")
        self.assertFalse((lane_wt / "verifier-scratch.txt").exists())

    # -- 2. the case pack --------------------------------------------------
    def test_the_script_assembles_the_pack_from_the_four_sources(self):
        self.run_accept("--good")
        pack = self.pack()
        self.assertIn("- the note file exists", pack)          # criteria, verbatim
        self.assertIn("The release script.", pack)             # forbids, verbatim
        self.assertIn("bellhop-stub-001.txt", pack)            # the files changed
        self.assertIn("Receipt body", pack)                    # the receipt
        self.assertIn("One mutation per criterion", pack)      # the terms
        self.assertIn("./check.sh", pack)                      # the lane's check command

    def test_a_desks_own_instructions_are_used_unless_they_are_out_of_date(self):
        meta = self.run_accept("--good")
        self.assertEqual(meta["instructions_from"], "desk")
        self.assertNotIn("{{", self.pack())
        # negative control: a desk still carrying the older template, which asks the
        # verifier for a diff it is not given and a path it cannot write to
        write(self.desk_dir / "templates" / "acceptance-prompt.md",
              "Task {{id}}\n\n{{terms}}\n\nWrite your verdict to {{out}}\n\n{{diff}}\n")
        meta = self.run_accept("--good")
        self.assertTrue(meta["instructions_from"].startswith("framework"))
        self.assertNotIn("{{", self.pack())
        self.assertIn("your final message", self.pack())

    def test_the_receipt_body_is_capped_at_the_configured_line_count(self):
        write(self.receipt, read(self.receipt) + "\n" + "\n".join(f"filler {i}" for i in range(200)))
        self.run_accept("--good", casepack_receipt_lines=12)
        pack = self.pack()
        self.assertIn("Receipt body (12 of", pack)
        self.assertNotIn("filler 199", pack)
        # negative control: a generous cap keeps the tail
        self.run_accept("--good --json", casepack_receipt_lines=400, casepack_bytes=12000)
        self.assertIn("filler 199", self.pack())

    def test_over_the_cap_the_receipt_goes_first_and_the_criteria_never_go(self):
        write(self.receipt, read(self.receipt) + "\n" + ("a long line of receipt prose\n" * 300))
        meta = self.run_accept("--good", casepack_bytes=4500, casepack_receipt_lines=400)
        pack = self.pack()
        self.assertLessEqual(len(pack.encode("utf-8")), 4500)
        self.assertIn("- the note file exists", pack)
        self.assertIn("- the checks stay green", pack)
        self.assertEqual([t["part"] for t in meta["case_pack_trims"] if t["reason"] == "case-pack cap"],
                         ["receipt"])
        self.assertIn(accept_mod.TRIM_MARK, pack)
        self.assertIsNone(meta["stall"])
        # negative control: a receipt that fits is not trimmed at all
        meta = self.run_accept("--good", casepack_bytes=6000, casepack_receipt_lines=20)
        self.assertEqual([t for t in meta["case_pack_trims"] if t["reason"] == "case-pack cap"], [])
        self.assertNotIn(accept_mod.TRIM_MARK, self.pack())

    def test_the_packet_prose_is_trimmed_after_the_receipt(self):
        packet = parse_packet(self.packet_path)
        packet.body = packet.body.replace("Add a note file.", "Background prose. " * 400)
        instructions, _ = accept_mod.head_matter(self.desk, packet, self.base, self.base)
        pack, trims = accept_mod.build_case_pack(
            instructions, packet, self.desk.lane("api"), "receipt prose\n" * 200,
            read(accept_mod.terms_path(self.desk)), ["note.md"], 4500, 400)
        parts = [t["part"] for t in trims if t["reason"] == "case-pack cap"]
        self.assertEqual(parts[:2], ["receipt", "packet"])
        self.assertIn("- the note file exists", pack)
        self.assertIn("The release script.", pack)
        self.assertLessEqual(len(pack.encode("utf-8")), 4500)

    def test_packet_changes_are_a_prepare_stall_even_with_force(self):
        original = read(self.packet_path)
        write(self.packet_path, original.replace("the note file exists", "any file exists"))
        meta = self.run_accept("--good", force=True)
        self.assertEqual((meta["step"], meta["stall"], meta["attempts"]),
                         ("prepare", "packet provenance", 0))
        self.assertIn("restore the original", meta["reason"])
        self.assertIn("packet provenance: blocked", accept_mod.status_text(self.desk, "001"))
        write(self.packet_path, original)
        meta = self.run_accept("--good")
        self.assertTrue(meta["ok"], meta)
        self.assertEqual(meta["packet_sha256"], parse_packet(self.packet_path).sha256)

    def test_missing_worker_hash_is_not_invented_from_current_packet(self):
        path = self.desk.receipts / "001.meta.json"
        original = read(path)
        for value in ({}, {"packet_sha256": "bad"}, []):
            write_json(path, value)
            meta = self.run_accept("--good")
            self.assertEqual(meta["stall"], "packet provenance")
            self.assertEqual(meta["attempts"], 0)
        write(path, original)
        self.assertTrue(self.run_accept("--good")["ok"])

    def test_packet_changed_during_verification_invalidates_the_run(self):
        run_session = accept_mod.run_session
        def changed(*args, **kwargs):
            result = run_session(*args, **kwargs)
            write(self.packet_path, read(self.packet_path) + "\nChanged during verification.\n")
            return result
        with patch.object(accept_mod, "run_session", side_effect=changed):
            meta = self.run_accept("--good")
        self.assertEqual((meta["step"], meta["stall"]), ("recheck", "packet provenance"))
        self.assertFalse(meta["ok"])
        self.assertFalse(Path(meta["copy"]).exists())

    def test_required_context_and_forbids_are_preserved_or_prepare_refuses(self):
        path = self.write_packet(pid="002", title="Required context")
        required = "```text\n# Original source heading\n" + "required original text\n" * 350 + "```\n"
        write(path, read(path) + "\n## Required context\n\n### Source excerpt\n" + required)
        ring.ring(self.desk, path)
        watch.watch_once(self.desk)
        desk = self.desk_with(casepack_bytes=6000)
        meta = accept_mod.run_accept(desk, "002")
        self.assertEqual(meta["stall"], "case pack over the cap")
        self.assertEqual(meta["attempts"], 0)
        self.assertNotIn("required_context", [t["part"] for t in meta["case_pack_trims"]])
        desk = self.desk_with(casepack_bytes=20000)
        meta = accept_mod.run_accept(desk, "002", force=True)
        self.assertTrue(meta["ok"], meta)
        pack = read(accept_mod.pack_path(desk, "002"))
        self.assertIn(required.strip(), pack)
        self.assertIn("The release script.", pack)

    def test_status_handles_bad_metadata_without_a_model_run(self):
        write(accept_mod.meta_path(self.desk, "001"), "not json")
        status = accept_mod.status_text(self.desk, "001")
        self.assertIn("acceptance metadata: unreadable", status)
        self.assertIn("packet provenance: verified", status)

    def test_trimming_cuts_on_a_character_boundary(self):
        text = "—" * 10  # three bytes each
        cut = accept_mod.cut_utf8(text, 10)
        self.assertEqual(cut, "—" * 3)
        self.assertLessEqual(len(cut.encode("utf-8")), 10)
        # negative control: the same cut on bytes lands inside a character
        with self.assertRaises(UnicodeDecodeError):
            text.encode("utf-8")[:10].decode("utf-8")
        # and in the pipeline: a pack trimmed out of multi-byte prose still reads back
        write(self.receipt, read(self.receipt) + "\n" + ("— a receipt line with an em dash —\n" * 300))
        self.run_accept("--good", casepack_bytes=4500, casepack_receipt_lines=400)
        raw = accept_mod.pack_path(self.desk, "001").read_bytes()
        self.assertLessEqual(len(raw), 4500)
        raw.decode("utf-8")  # raises if the trim cut a character in half

    def test_a_pack_that_cannot_be_trimmed_under_the_cap_stalls_at_prepare(self):
        meta = self.run_accept("--good", casepack_bytes=300)
        self.assertEqual(meta["step"], "prepare")
        self.assertEqual(meta["stall"], "case pack over the cap")
        self.assertIn("stalled at: prepare", self.closings())
        # negative control: the default cap holds the same pack
        meta = self.run_accept("--good", casepack_bytes=6000)
        self.assertIsNone(meta["stall"])

    # -- 3. the tool list --------------------------------------------------
    def test_the_verifier_gets_read_only_tools_the_mutation_pair_and_the_lane_check(self):
        tools = accept_mod.allowed_tools(self.desk.lane("api"))
        for expected in ("Read", "Grep", "Glob", "Bash(git diff:*)", "Bash(git status:*)",
                         "Bash(git log:*)", "Bash(git show:*)", "Bash(./check.sh:*)",
                         # the mutation the checker's hard gate demands, inside the copy
                         "Edit", "Bash(git checkout:*)"):
            self.assertIn(expected, tools)
        # nothing that creates a file, and nothing that leaves the copy
        for forbidden in ("Write", "NotebookEdit", "WebFetch", "Bash(rm", "Bash(git push",
                          "Bash(git commit"):
            self.assertNotIn(forbidden, tools)
        # negative control: a lane with no check command gets no extra Bash entry
        lane = dict(self.desk.lane("api"))
        lane["check_cmd"] = ""
        self.assertEqual(accept_mod.allowed_tools(lane), accept_mod.VERIFIER_TOOLS)

    def test_a_mutation_that_is_restored_is_not_a_stall(self):
        """The terms ask the verifier to break something and put it back. The recheck
        has to pass that and fail the one that was not put back, or the mutation gate
        and the isolation gate cannot both be satisfied by an honest run."""
        meta = self.run_accept("--mutate --good")
        self.assertIsNone(meta["stall"], meta.get("reason"))
        self.assertEqual(meta["status_after"], "")
        self.assertEqual(meta["tree_before"], meta["tree_after"])
        self.assertEqual(meta["verdict"], "pass")
        # negative control: the same edit left in place is a stall
        meta = self.run_accept("--touch-tree --good")
        self.assertEqual(meta["stall"], accept_mod.STALL_TOUCHED)

    # -- 4. timeout and the process group ----------------------------------
    def test_a_slow_verifier_times_out_and_its_process_group_dies(self):
        pid_file = self.tmp / "child.pid"
        meta = self.run_accept(f"--sleep 30 --child-sleep 30 --pid-file {pid_file}", accept_timeout_s=1)
        self.assertEqual(meta["step"], "timeout")
        self.assertEqual(meta["stall"], accept_mod.STALL_TIMEOUT)
        self.assertEqual(meta["exit_code"], 124)
        self.assertIn("stalled at: timeout", self.closings())
        pid = int(read(pid_file).strip())
        for _ in range(50):  # the group is signalled, then reaped
            if not alive(pid):
                break
            time.sleep(0.1)
        self.assertFalse(alive(pid), "the verifier's child outlived the killed session")

    def test_a_verifier_inside_the_timeout_is_not_a_stall(self):
        meta = self.run_accept("--good --sleep 0.2", accept_timeout_s=60)
        self.assertIsNone(meta["stall"])
        self.assertEqual(meta["exit_code"], 0)
        self.assertGreaterEqual(meta["verifier_s"], 0.2)

    def test_the_session_result_json_is_kept(self):
        self.run_accept("--good --json")
        result = read_json(accept_mod.result_path(self.desk, "001"))
        self.assertEqual(result["session_id"], "stub-accept")
        self.assertEqual(result["num_turns"], 4)
        self.assertIn("VERDICT: pass", result["result"])
        # negative control: a session with no result object says so rather than vanishing
        self.run_accept("--good")
        result = read_json(accept_mod.result_path(self.desk, "001"))
        self.assertIsNone(result["result"])
        self.assertIn("no result object", result["note"])

    # -- 5. the verdict ----------------------------------------------------
    def test_a_verdict_in_the_wrong_shape_is_retried_once_then_stalls(self):
        meta = self.run_accept("--bad-format")
        self.assertEqual(meta["attempts"], 2)
        self.assertEqual(meta["step"], "verdict format")
        self.assertEqual(meta["stall"], accept_mod.STALL_FORMAT)
        self.assertIn("Reminder:", self.pack())
        self.assertIn("stalled at: verdict format", self.closings())

    def test_the_reminder_is_the_retry_and_a_good_second_answer_is_a_verdict(self):
        meta = self.run_accept("--retry-good")
        self.assertEqual(meta["attempts"], 2)
        self.assertIsNone(meta["stall"])
        self.assertEqual(meta["verdict"], "pass")
        self.assertIn("Reminder:", self.pack())
        self.assertEqual(read(accept_mod.check_path(self.desk, "001")).strip().splitlines()[-1], "ok")
        # negative control: a good first answer needs no second run
        meta = self.run_accept("--good")
        self.assertEqual(meta["attempts"], 1)

    def test_a_blocking_doubt_under_a_pass_is_a_red_and_then_a_stall(self):
        meta = self.run_accept("--good --blocking")
        self.assertEqual(meta["stall"], accept_mod.STALL_FORMAT)
        self.assertIn("blocking doubt", read(accept_mod.check_path(self.desk, "001")))
        # negative control: the same body with the doubt recorded instead passes
        meta = self.run_accept("--good")
        self.assertIsNone(meta["stall"])

    def test_a_verdict_writes_one_closing_line_under_two_hundred_characters(self):
        meta = self.run_accept("--good")
        lines = [l for l in self.closings().splitlines() if l.startswith("2")]
        self.assertEqual(len(lines), 1)
        line = lines[0]
        self.assertLessEqual(len(line), accept_mod.CLOSING_MAX)
        fields = [f.strip() for f in line.split("|")]
        self.assertEqual(fields[1], "001 api")
        self.assertEqual(fields[2], "pass")
        self.assertTrue(fields[3].startswith("head "))
        self.assertTrue(fields[4].startswith("doubts "))
        self.assertTrue(fields[5])
        self.assertEqual(meta["verdict"], "pass")

    def test_a_stall_writes_a_closing_line_too(self):
        """A task that stalled and a task nobody ran must stop looking the same."""
        self.assertEqual(self.accept_closings(), [])
        self.run_accept("--touch-tree --good")
        line = [l for l in self.closings().splitlines() if l.startswith("2")][0]
        self.assertIn("| stalled at: recheck |", line)
        self.assertIn("log ", line)
        self.assertLessEqual(len(line), accept_mod.CLOSING_MAX)

    def test_a_return_verdict_is_recorded_as_one(self):
        meta = self.run_accept("--verdict return")
        self.assertEqual(meta["verdict"], "return")
        self.assertIn("| return |", self.closings())
        self.assertEqual(len(self.closings().splitlines()[-1].split("|")), 6)

    def test_a_separator_in_the_verdict_sentence_cannot_shift_the_fields(self):
        line = accept_mod._closing("001", "api", "return", "abcdef1234", 1,
                                   "the check | printed | three reds\nand a warning")
        self.assertEqual(len(line.split("|")), 6)
        parsed = accept_mod.parse_closing(line)
        self.assertEqual(parsed["verdict"], "return")
        self.assertEqual(parsed["id"], "001")
        # negative control: the same sentence pasted in raw does shift them
        raw = "t | 001 api | return | head abcdef1 | doubts 1 | the check | printed | three reds"
        self.assertGreater(len(raw.split("|")), 6)

    # -- 6. shadow mode ----------------------------------------------------
    def test_shadow_lands_beside_the_real_files_and_changes_nothing(self):
        entries_before = ledger.stats(self.desk)[0]
        real_before = self.closings(shadow=False)
        main_before = git(self.repo, "rev-parse", "main")
        desk = self.desk_with(accept_cmd=f"{STUB} --good")
        meta = accept_mod.run_accept(desk, "001", shadow=True)
        self.assertIsNone(meta["stall"])
        self.assertTrue((desk.receipts / "shadow" / "001.acceptance.md").exists())
        self.assertFalse((desk.receipts / "001.acceptance.md").exists())
        self.assertIn("| pass |", self.closings(shadow=True))
        self.assertEqual(self.closings(shadow=False), real_before)
        self.assertEqual(ledger.stats(desk)[0], entries_before)
        self.assertEqual(git(self.repo, "rev-parse", "main"), main_before)

    def test_a_real_run_after_a_shadow_run_does_not_overwrite_it(self):
        desk = self.desk_with(accept_cmd=f"{STUB} --good")
        accept_mod.run_accept(desk, "001", shadow=True)
        accept_mod.run_accept(desk, "001")
        self.assertTrue((desk.receipts / "shadow" / "001.acceptance.md").exists())
        self.assertTrue((desk.receipts / "001.acceptance.md").exists())
        self.assertIn("(shadow)", read(desk.receipts / "shadow" / "001.acceptance.md"))
        self.assertNotIn("(shadow)", read(desk.receipts / "001.acceptance.md"))

    def test_a_standing_verdict_is_not_judged_again_without_force(self):
        self.run_accept("--good", force=False)
        with self.assertRaises(Exception):
            accept_mod.run_accept(self.desk, "001")
        meta = accept_mod.run_accept(self.desk, "001", force=True)
        self.assertIsNone(meta["stall"])

    def test_record_writes_the_coordinators_verdict_beside_the_verifiers(self):
        accept_mod.record(self.desk, "001", "pass")
        line = [l for l in self.closings().splitlines() if l.startswith("2")][0]
        self.assertIn("| by: coordinator", line)
        self.assertEqual(accept_mod.parse_closing(line)["by"], "coordinator")
        self.assertEqual(accept_mod.parse_closing(line)["verdict"], "pass")
        with self.assertRaises(Exception):
            accept_mod.record(self.desk, "001", "maybe")

    def _shadow_history(self, pairs, stalls=0, seconds=100.0):
        """pairs: (id, shadow verdict, coordinator verdict)."""
        for task_id, shadow_verdict, coordinator_verdict in pairs:
            accept_mod.append_closing(self.desk, accept_mod._closing(
                task_id, "api", shadow_verdict, "abcdef1234", 0, "stub"), shadow=True)
            accept_mod.append_closing(self.desk, (
                f"2026-09-11T09:00:00+0000 | {task_id} api | {coordinator_verdict} | by: coordinator"))
            write_json(accept_mod.meta_path(self.desk, task_id, True), {"verifier_s": seconds})
        for i in range(stalls):
            accept_mod.append_closing(self.desk, accept_mod._stall_closing(
                f"9{i:02d}", "api", "verdict format", Path("bells/logs/x.log")), shadow=True)

    def test_shadow_report_says_yes_when_the_criteria_are_met(self):
        desk = self.desk_with(shadow_min_samples=3, shadow_agreement=0.9,
                              shadow_stall_rate=0.5, shadow_median_s=480)
        self._shadow_history([("101", "pass", "pass"), ("102", "return", "return"),
                              ("103", "doubt", "doubt")], stalls=1, seconds=120.0)
        text, ready = accept_mod.shadow_report(desk)
        self.assertTrue(ready, text)
        self.assertEqual(text.count("✅"), 4)
        self.assertIn("agreement 1.00 (3/3)", text)
        self.assertIn("median verifier 120s", text)
        self.assertIn("switch: ready", text)

    def test_shadow_report_says_no_and_names_the_criterion_that_failed(self):
        desk = self.desk_with(shadow_min_samples=3, shadow_agreement=0.9,
                              shadow_stall_rate=0.1, shadow_median_s=60)
        self._shadow_history([("101", "pass", "return"), ("102", "return", "return"),
                              ("103", "doubt", "doubt")], stalls=2, seconds=900.0)
        text, ready = accept_mod.shadow_report(desk)
        self.assertFalse(ready, text)
        self.assertIn("agreement 0.67 (2/3)", text)
        self.assertIn("stall rate 0.40 (2/5)", text)
        self.assertIn("median verifier 900s", text)
        self.assertIn("stalls by step: verdict format 2", text)
        self.assertIn("switch: not yet", text)
        self.assertEqual(text.count("\U0001f6d1"), 3)

    def test_shadow_report_on_an_empty_desk_is_not_ready(self):
        text, ready = accept_mod.shadow_report(self.desk)
        self.assertFalse(ready)
        self.assertIn("samples 0", text)
        self.assertIn("no timings on disk", text)

    # -- 7. steps, status, the CLI -----------------------------------------
    def test_every_step_name_used_by_a_stall_is_one_of_the_named_steps(self):
        for switches, step in (("--touch-tree --good", "recheck"), ("--bad-format", "verdict format")):
            meta = self.run_accept(switches)
            self.assertIn(step, accept_mod.STEPS)
            self.assertEqual(meta["step"], step)

    def test_a_crash_inside_the_run_is_recorded_and_still_cleans_up(self):
        def boom(copy):
            raise RuntimeError("boom")

        original = accept_mod._tree_state
        accept_mod._tree_state = boom
        try:
            meta = self.run_accept("--good")
        finally:
            accept_mod._tree_state = original
        self.assertEqual(meta["stall"], "the run failed")
        self.assertIn("RuntimeError", meta["reason"])
        self.assertIn("stalled at:", self.closings())
        self.assertFalse(Path(meta["copy"]).exists())
        # negative control: with the reading working, the same run is not a stall
        meta = self.run_accept("--good")
        self.assertIsNone(meta["stall"])

    def test_status_prints_the_last_runs_meta(self):
        self.assertIn("no acceptance run recorded", accept_mod.status_text(self.desk, "001"))
        self.run_accept("--touch-tree --good")
        text = accept_mod.status_text(self.desk, "001")
        self.assertIn("step: recheck", text)
        self.assertIn(f"stall: {accept_mod.STALL_TOUCHED}", text)
        self.assertIn("log: ", text)
        self.assertIn("duration_s: ", text)
        self.assertIn("case_pack_bytes: ", text)
        self.assertIn("packet provenance: verified\n", text)
        self.assertIn("acceptance binding: verified\n", text)
        meta_path = accept_mod.meta_path(self.desk, "001")
        meta = read_json(meta_path)
        self.assertRegex(meta["packet_sha256"], r"^[0-9a-f]{64}$")
        self.assertNotIn(meta["packet_sha256"], text)
        self.assertNotIn("packet_sha256:", text)

        meta.pop("packet_sha256")
        write_json(meta_path, meta)
        text = accept_mod.status_text(self.desk, "001")
        self.assertIn("acceptance binding: missing or stale", text)
        self.assertIn("bellhop accept 001 --force", text)
        self.assertNotIn("acceptance binding: verified", text)

    def test_a_receipt_with_no_header_has_no_head_to_judge(self):
        stripped = headers.strip_header(read(self.receipt), headers.FENCE_RECEIPT)
        write(self.receipt, stripped)
        meta = self.run_accept("--good", force=False)
        self.assertEqual(meta["step"], "prepare")
        self.assertEqual(meta["stall"], "no head to judge")
        # negative control: --force falls back to the lane branch as it stands
        meta = accept_mod.run_accept(self.desk, "001", force=True)
        self.assertIsNone(meta["stall"])

    def test_cli_runs_the_pipeline_and_the_three_readings(self):
        self.desk_with(accept_cmd=f"{STUB} --good", shadow_min_samples=1,
                       shadow_agreement=0.9, shadow_stall_rate=0.9, shadow_median_s=900)
        out = self.cli("--no-commit", "accept", "001", "--shadow")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("receipts/shadow/001.acceptance.md", out.stdout)
        out = self.cli("--no-commit", "accept", "record", "001", "--verdict", "pass")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("by: coordinator", out.stdout)
        out = self.cli("accept", "status", "001")
        self.assertIn("mode: shadow", out.stdout)
        out = self.cli("accept", "shadow-report")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("switch: ready", out.stdout)

    def test_forced_rerun_marks_old_pass_incomplete_before_session_starts(self):
        self.assertTrue(self.run_accept("--good")["ok"])

        def interrupted(*args):
            current = read_json(accept_mod.meta_path(self.desk, "001"))
            self.assertIs(current["ok"], False)
            raise RuntimeError("interrupted verifier")

        with patch.object(accept_mod, "run_session", side_effect=interrupted):
            meta = self.run_accept("--good")
        self.assertFalse(meta["ok"])
        self.assertIn("interrupted verifier", meta["reason"])
        from bellhop.closetask import close_task, CloseStopped
        with self.assertRaisesRegex(CloseStopped, "incomplete or stalled"):
            close_task(self.desk, "001", say=lambda line: None)

    def test_verifier_settings_are_independent_of_worker_and_report_unknown_honestly(self):
        self.cfg["lanes"]["api"].update({"accept_cmd": "", "model": "worker-only-model",
            "provider": "generic", "heavy_model": "worker-heavy-model", "effort": "low"})
        self.desk = Desk(self.desk_dir, dict(self.cfg, accept_model="verifier-model", accept_effort="high"))
        original = accept_mod.run_session
        commands = []

        def stub_session(cmd, cwd, env, timeout):
            commands.append(cmd)
            result = original([sys.executable, str(ROOT / "bellhop/stubaccept.py"), "--good", "--json"],
                              cwd, env, timeout)
            data = json.loads(result["stdout"])
            data.pop("model", None)  # no actual model ID was reported
            result["stdout"] = json.dumps(data)
            return result

        with patch.object(accept_mod, "run_session", side_effect=stub_session):
            meta = accept_mod.run_accept(self.desk, "001")
        self.assertTrue(meta["ok"], meta)
        cmd = commands[0]
        self.assertEqual(cmd[cmd.index("--model") + 1], "verifier-model")
        self.assertEqual(cmd[cmd.index("--effort") + 1], "high")
        self.assertNotIn("--add-dir", cmd)
        self.assertEqual(meta["provider"], "claude")
        self.assertEqual(meta["model_asked"], "verifier-model")
        self.assertIsNone(meta["model"])
        self.desk.cfg["lanes"]["api"].update(accept_model="lane-verifier", accept_effort="medium")
        with patch.object(accept_mod, "run_session", side_effect=stub_session):
            meta = accept_mod.run_accept(self.desk, "001", force=True)
        self.assertEqual(meta["model_asked"], "lane-verifier")
        self.assertEqual(meta["effort"], "medium")

    def test_cli_exits_red_on_a_stall(self):
        self.desk_with(accept_cmd=f"{STUB} --touch-tree --good")
        out = self.cli("--no-commit", "accept", "001")
        self.assertEqual(out.returncode, headers.EXIT_RED)
        self.assertIn("stalled at recheck", out.stderr)
        # negative control: the same command on an honest verifier exits 0
        self.desk_with(accept_cmd=f"{STUB} --good")
        out = self.cli("--no-commit", "accept", "001")
        self.assertEqual(out.returncode, 0, out.stderr)


if __name__ == "__main__":
    unittest.main()
