"""Tests for the machine headers and the checkers that read them.

Every gate gets a positive case and a negative control: the point of the module is
that a bad file and a good one must stop looking the same to a machine.
"""
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

from bellhop import accept as accept_mod  # noqa: E402
from bellhop import desk_ops, headers, lanes, ring, watch  # noqa: E402
from bellhop.config import Desk  # noqa: E402
from bellhop.packet import parse_packet  # noqa: E402
from bellhop.util import read, read_json, write  # noqa: E402

GIT_ID = ["-c", "user.name=test", "-c", "user.email=test@example.invalid"]

GOOD_VERDICT = """# Acceptance 001

## Task
001 add a note file

## Criteria
- [x] the note file exists — `ls note.txt` printed `note.txt`
- [x] the checks stay green — `./check.sh` printed `check.sh: all green`

## Mutation
- broke: deleted note.txt / `ls note.txt` printed `No such file` / restored, tree hash 3f1c2a9
- broke: emptied the note / the check printed `empty note` / restored, tree hash 3f1c2a9

## Extra findings
- the worker also fixed a typo in README.md, which the packet did not ask for

## Doubts
- [recorded] the packet said 45 minutes and the receipt says 12; nothing turns on it

## Hygiene
- file set equal: 2 files in the diff, 2 files named in the receipt

## Recommendation
- merge

## Verdict
VERDICT: pass
"""


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *GIT_ID, *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def fits(path, cap):
    """A size gate written once, so the test can also show it failing."""
    return Path(path).stat().st_size <= cap


class HeadersTest(unittest.TestCase):
    def setUp(self):
        os.environ["GIT_AUTHOR_NAME"] = os.environ["GIT_COMMITTER_NAME"] = "test"
        os.environ["GIT_AUTHOR_EMAIL"] = os.environ["GIT_COMMITTER_EMAIL"] = "test@example.invalid"
        self.tmp = Path(tempfile.mkdtemp(prefix="bellhop-headers-test.")).resolve()
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
        cfg["lanes"]["api"]["check_cmd"] = "echo 'lane check: green'"
        cfg["lanes"]["api"]["worker_cmd"] = f"python3 {ROOT}/bellhop/stubworker.py"
        cfg["lanes"]["api"]["accept_cmd"] = f"python3 {ROOT}/bellhop/stubaccept.py"
        write(self.desk_dir / "bellhop.json", json.dumps(cfg, indent=2))
        self.desk = Desk(self.desk_dir, cfg)
        self.packet_path = self.write_packet()
        self.packet = parse_packet(self.packet_path)
        self.wt = lanes.prepare_worktree(self.desk, self.desk.lane("api"), self.base)

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

    def work(self, lines=2):
        note = self.wt / "note.txt"
        note.write_text("".join(f"line {i}\n" for i in range(lines)), encoding="utf-8")
        git(self.wt, "add", "note.txt")
        git(self.wt, "commit", "-q", "-m", "add note")
        return git(self.wt, "rev-parse", "HEAD")

    def receipt(self, body=None):
        p = self.desk.receipts / "001-add-a-note-file.md"
        write(p, body if body is not None else (
            "# Receipt 001\n\n- base: deadbeefdeadbeef\n- changed: everything\n\n"
            "## Acceptance\n\n- the note file exists — checked: `ls` → `note.txt`\n\n"
            "## Doubts\n\nDOUBT: the packet did not say where the note goes\nDOUBT: the second one\n\n"
            "## Extra\n\nEXTRA: also fixed a typo\n"))
        return p

    def verdict(self, text=GOOD_VERDICT, name="001.acceptance.md"):
        p = self.desk.receipts / name
        write(p, text)
        return p

    def cli(self, *args):
        env = dict(os.environ, BELLHOP_DESK=str(self.desk_dir))
        return subprocess.run([str(ROOT / "bin" / "bellhop"), *args], env=env,
                              capture_output=True, text=True)

    # -- 1. the receipt header comes from commands, not from the prose -----
    def test_receipt_header_is_computed_from_real_commands(self):
        head = self.work(lines=3)
        (self.wt / "scratch.txt").write_text("left behind\n", encoding="utf-8")
        receipt = self.receipt()
        result = {"provider": "claude", "model": "opus", "turns": 7, "cost_usd": 0.42,
                  "duration_s": 12.5, "exit_code": 0, "dead": False}
        values, action = headers.write_receipt_header(self.desk, receipt, "api",
                                                      packet=self.packet, result=result)
        self.assertEqual(action, "new")
        parsed, _span = headers.parse_header(read(receipt), headers.FENCE_RECEIPT)
        # keys are written in spec order, and only spec keys are written
        self.assertEqual(list(parsed), [f.key for f in headers.RECEIPT_SPEC])
        self.assertEqual(parsed["head"], head)
        self.assertEqual(parsed["base"], self.base)
        self.assertEqual(parsed["commits"], "1")
        self.assertEqual(parsed["files_changed"], "1")
        self.assertEqual(parsed["insertions"], "3")
        self.assertEqual(parsed["deletions"], "0")
        self.assertEqual(parsed["left_in_tree"], "1")  # the untracked scratch file
        self.assertEqual(parsed["check_exit"], "0")
        self.assertEqual(parsed["check_summary"], "lane check: green")
        self.assertEqual(parsed["doubts"], "2")
        self.assertEqual(parsed["extras"], "1")
        self.assertEqual(parsed["model"], "opus")
        self.assertEqual(parsed["turns"], "7")
        self.assertEqual(parsed["cost_usd"], "0.42")
        self.assertEqual(parsed["duration_s"], "12.5")
        self.assertEqual(parsed["dead"], "no")
        # negative control: the body claims another base and another file count,
        # and not one of those claims reaches the header
        self.assertIn("deadbeefdeadbeef", read(receipt))
        self.assertNotEqual(parsed["base"], "deadbeefdeadbeef")
        self.assertEqual(values["head"], parsed["head"])

    def test_check_exit_tells_not_run_from_zero(self):
        self.work()
        receipt = self.receipt()
        parsed, _ = headers.write_receipt_header(self.desk, receipt, "api", packet=self.packet)
        self.assertEqual(parsed["check_exit"], "0")  # positive: a check that ran green
        cfg = dict(self.desk.cfg)
        cfg["lanes"]["api"]["check_cmd"] = ""
        quiet = Desk(self.desk_dir, cfg)
        parsed, _ = headers.write_receipt_header(quiet, receipt, "api", packet=self.packet)
        self.assertEqual(parsed["check_exit"], headers.CHECK_NOT_RUN)  # negative control
        cfg["lanes"]["api"]["check_cmd"] = "echo 'two tests failed'; exit 3"
        red = Desk(self.desk_dir, cfg)
        parsed, _ = headers.write_receipt_header(red, receipt, "api", packet=self.packet)
        self.assertEqual(parsed["check_exit"], "3")
        self.assertEqual(parsed["check_summary"], "two tests failed")

    # -- 2. idempotence ----------------------------------------------------
    def test_writing_the_header_twice_leaves_the_body_byte_for_byte(self):
        self.work()
        receipt = self.receipt()
        body_before = receipt.read_bytes()
        first, action1 = headers.write_receipt_header(self.desk, receipt, "api", packet=self.packet)
        after_first = receipt.read_bytes()
        second, action2 = headers.write_receipt_header(self.desk, receipt, "api", packet=self.packet)
        after_second = receipt.read_bytes()
        self.assertEqual((action1, action2), ("new", "replaced"))
        text = after_second.decode("utf-8")
        self.assertEqual(text.count("```" + headers.FENCE_RECEIPT), 1)
        self.assertEqual(headers.strip_header(text, headers.FENCE_RECEIPT).encode("utf-8"), body_before)
        moved = {k for k in first if first[k] != second[k]}
        self.assertTrue(moved <= {"generated"}, moved)
        # negative control: the file did change between the two runs, so "unchanged
        # body" is not just the same bytes all the way through
        self.assertNotEqual(after_first, body_before)
        self.assertTrue(after_second.decode("utf-8").startswith("```" + headers.FENCE_RECEIPT))

    def test_an_existing_header_is_replaced_where_it_stands(self):
        self.work()
        receipt = self.receipt()
        headers.write_receipt_header(self.desk, receipt, "api", packet=self.packet)
        text = read(receipt)
        # a human moved the block into the middle of the file
        block, span = headers.parse_header(text, headers.FENCE_RECEIPT)
        body = headers.strip_header(text, headers.FENCE_RECEIPT)
        marker = "## Doubts"
        moved_text = body.replace(marker, text[span[0]:span[1]] + "\n" + marker)
        write(receipt, moved_text)
        before = read(receipt).index("```" + headers.FENCE_RECEIPT)
        headers.write_receipt_header(self.desk, receipt, "api", packet=self.packet)
        after = read(receipt)
        self.assertEqual(after.index("```" + headers.FENCE_RECEIPT), before)
        self.assertEqual(headers.strip_header(after, headers.FENCE_RECEIPT), body)
        self.assertEqual(after.count("```" + headers.FENCE_RECEIPT), 1)

    # -- 3. receipt gates --------------------------------------------------
    def test_check_receipt_passes_a_generated_header(self):
        self.work()
        receipt = self.receipt()
        headers.write_receipt_header(self.desk, receipt, "api", packet=self.packet)
        rep = headers.check_receipt(receipt, packet=self.packet)
        self.assertEqual(rep.reds, [])
        self.assertEqual(rep.code, headers.EXIT_OK)
        self.assertEqual(rep.summary, "ok")

    def test_check_receipt_reds_a_missing_header(self):
        receipt = self.receipt()  # never given a header
        rep = headers.check_receipt(receipt, packet=self.packet)
        self.assertEqual(rep.code, headers.EXIT_RED)
        self.assertEqual(len(rep.reds), 1)
        self.assertIn("no ```receipt-header```", rep.reds[0])

    def test_check_receipt_reds_a_missing_head_and_a_missing_check_exit(self):
        self.work()
        receipt = self.receipt()
        headers.write_receipt_header(self.desk, receipt, "api", packet=self.packet)
        text = read(receipt)
        write(receipt, re.sub(r"^head: .*$", "head:", text, flags=re.M))
        rep = headers.check_receipt(receipt, packet=self.packet)
        self.assertTrue(any("head is missing" in r for r in rep.reds))
        self.assertEqual(rep.code, headers.EXIT_RED)
        write(receipt, re.sub(r"^check_exit: .*$", "check_exit:", text, flags=re.M))
        rep = headers.check_receipt(receipt, packet=self.packet)
        self.assertTrue(any("check_exit is missing" in r for r in rep.reds))
        # positive control: with both lines back, neither red fires
        write(receipt, text)
        rep = headers.check_receipt(receipt, packet=self.packet)
        self.assertEqual(rep.reds, [])

    def test_check_receipt_reds_a_base_that_is_not_the_packets(self):
        self.work()
        receipt = self.receipt()
        headers.write_receipt_header(self.desk, receipt, "api", packet=self.packet)
        rep = headers.check_receipt(receipt, packet=self.packet)
        self.assertEqual(rep.reds, [])  # positive: the real base matches
        text = read(receipt)
        write(receipt, re.sub(r"^base: .*$", "base: 0000000000000000", text, flags=re.M))
        rep = headers.check_receipt(receipt, packet=self.packet)
        self.assertTrue(any("the packet says" in r for r in rep.reds))
        self.assertEqual(rep.code, headers.EXIT_RED)
        # a short sha of the same commit is the same base
        write(receipt, re.sub(r"^base: .*$", f"base: {self.base[:8]}", text, flags=re.M))
        self.assertEqual(headers.check_receipt(receipt, packet=self.packet).reds, [])

    def test_soft_findings_warn_and_keep_the_exit_at_zero(self):
        self.work()
        (self.wt / "scratch.txt").write_text("left behind\n", encoding="utf-8")
        receipt = self.receipt()
        cfg = dict(self.desk.cfg)
        cfg["lanes"]["api"]["check_cmd"] = "echo 'one test failed'; exit 1"
        headers.write_receipt_header(Desk(self.desk_dir, cfg), receipt, "api", packet=self.packet)
        rep = headers.check_receipt(receipt, packet=self.packet)
        self.assertEqual(rep.reds, [])
        self.assertEqual(rep.code, headers.EXIT_OK)
        self.assertTrue(any("lane check exited 1" in w for w in rep.warns))
        self.assertTrue(any("left uncommitted" in w for w in rep.warns))
        self.assertEqual(rep.summary, f"red 0 warn {len(rep.warns)}")

    def test_a_receipt_that_is_not_there_is_malformed_not_red(self):
        rep = headers.check_receipt(self.desk.receipts / "nope.md")
        self.assertEqual(rep.code, headers.EXIT_MALFORMED)

    # -- 4. the acceptance header ------------------------------------------
    def test_acceptance_header_counts_the_body(self):
        v = self.verdict()
        values, action = headers.write_acceptance_header(v, desk=self.desk, lane="api", task="001")
        parsed, _ = headers.parse_header(read(v), headers.FENCE_ACCEPTANCE)
        self.assertEqual(action, "new")
        self.assertEqual(list(parsed), [f.key for f in headers.ACCEPTANCE_SPEC])
        self.assertEqual(parsed["verdict"], "pass")
        self.assertEqual(parsed["criteria_total"], "2")
        self.assertEqual(parsed["criteria_pass"], "2")
        self.assertEqual(parsed["criteria_fail"], "0")
        self.assertEqual(parsed["mutations"], "2")
        self.assertEqual(parsed["doubts_blocking"], "0")
        self.assertEqual(parsed["doubts_recorded"], "1")
        self.assertEqual(parsed["extra_findings"], "1")
        self.assertEqual(parsed["sections_missing"], "none")
        self.assertEqual(parsed["paths_outside"], "0")
        self.assertEqual(int(parsed["bytes"]), len(GOOD_VERDICT.encode("utf-8")))
        self.assertEqual(values["task"], "001")

    def test_acceptance_header_reads_the_failures_too(self):
        body = (GOOD_VERDICT
                .replace("- [x] the checks stay green", "- [ ] the checks stay green")
                .replace("- [recorded] the packet said", "- [blocking] the packet said")
                .replace("VERDICT: pass", "VERDICT: return — the checks do not pass"))
        v = self.verdict(body, name="002.acceptance.md")
        parsed, _action = headers.write_acceptance_header(v, desk=self.desk, lane="api")
        self.assertEqual(parsed["criteria_pass"], 1)
        self.assertEqual(parsed["criteria_fail"], 1)
        self.assertEqual(parsed["doubts_blocking"], 1)
        self.assertEqual(parsed["doubts_recorded"], 0)
        self.assertEqual(parsed["verdict"], "return")
        self.assertEqual(parsed["verdict_reason"], "the checks do not pass")

    def test_a_verdict_that_is_not_the_last_line_is_not_a_verdict(self):
        body = GOOD_VERDICT.replace("VERDICT: pass\n", "VERDICT: pass\n\nOne more thought.\n")
        v = self.verdict(body, name="003.acceptance.md")
        parsed, _ = headers.write_acceptance_header(v)
        self.assertEqual(parsed["verdict"], headers.NOT_AVAILABLE)
        # positive control: the same body with nothing after the verdict line
        v2 = self.verdict(GOOD_VERDICT, name="004.acceptance.md")
        parsed2, _ = headers.write_acceptance_header(v2)
        self.assertEqual(parsed2["verdict"], "pass")

    def test_acceptance_header_is_idempotent(self):
        v = self.verdict()
        before = v.read_bytes()
        first, _ = headers.write_acceptance_header(v, desk=self.desk, lane="api")
        second, _ = headers.write_acceptance_header(v, desk=self.desk, lane="api")
        text = read(v)
        self.assertEqual(text.count("```" + headers.FENCE_ACCEPTANCE), 1)
        self.assertEqual(headers.strip_header(text, headers.FENCE_ACCEPTANCE).encode("utf-8"), before)
        self.assertTrue({k for k in first if first[k] != second[k]} <= {"generated"})
        # negative control: `bytes` counts the body, so it does not grow with the header
        self.assertEqual(first["bytes"], second["bytes"])

    # -- 5. acceptance gates -----------------------------------------------
    def test_check_acceptance_passes_a_complete_verdict(self):
        v = self.verdict()
        headers.write_acceptance_header(v, desk=self.desk, lane="api")
        rep = headers.check_acceptance(v)
        self.assertEqual(rep.reds, [])
        self.assertEqual(rep.warns, [])
        self.assertEqual(rep.summary, "ok")

    def test_check_acceptance_reds_a_missing_header(self):
        v = self.verdict()  # a perfect body, but no header
        rep = headers.check_acceptance(v)
        self.assertEqual(rep.code, headers.EXIT_RED)
        self.assertIn("no ```acceptance-header```", rep.reds[0])

    def test_check_acceptance_reds_a_verdict_outside_the_set(self):
        v = self.verdict(GOOD_VERDICT.replace("VERDICT: pass", "VERDICT: looks fine to me"))
        headers.write_acceptance_header(v)
        rep = headers.check_acceptance(v)
        self.assertTrue(any("verdict is" in r for r in rep.reds))
        self.assertEqual(rep.code, headers.EXIT_RED)

    def test_check_acceptance_reds_a_verdict_with_no_mutation(self):
        body = re.sub(r"## Mutation\n(- broke:.*\n)+", "## Mutation\n- none\n", GOOD_VERDICT)
        v = self.verdict(body)
        parsed, _ = headers.write_acceptance_header(v)
        self.assertEqual(parsed["mutations"], 0)
        rep = headers.check_acceptance(v)
        self.assertTrue(any("no mutation" in r for r in rep.reds))
        self.assertEqual(rep.code, headers.EXIT_RED)
        # positive control: put one mutation back and the red goes away
        v2 = self.verdict(GOOD_VERDICT, name="005.acceptance.md")
        headers.write_acceptance_header(v2)
        self.assertEqual(headers.check_acceptance(v2).reds, [])

    def test_check_acceptance_reds_a_blocking_doubt_under_a_pass(self):
        body = GOOD_VERDICT.replace("- [recorded] the packet said", "- [blocking] the packet said")
        v = self.verdict(body)
        headers.write_acceptance_header(v)
        rep = headers.check_acceptance(v)
        self.assertTrue(any("blocking doubt" in r for r in rep.reds))
        # positive control: the same blocking doubt under a doubt verdict is fine
        v2 = self.verdict(body.replace("VERDICT: pass", "VERDICT: doubt — waiting on the coordinator"),
                          name="006.acceptance.md")
        headers.write_acceptance_header(v2)
        self.assertEqual(headers.check_acceptance(v2).reds, [])

    def test_acceptance_soft_checks_warn_only(self):
        long_body = GOOD_VERDICT.replace("## Hygiene\n", "## Hygiene\n" + "- a long note about the tree\n" * 120)
        long_body = long_body.replace("## Recommendation\n- merge\n", "")
        long_body += "\nSee /somewhere/else/entirely/report.md\n\nVERDICT: pass\n"
        long_body = long_body.replace("## Verdict\nVERDICT: pass\n", "## Verdict\n")
        v = self.verdict(long_body)
        headers.write_acceptance_header(v, desk=self.desk, lane="api")
        rep = headers.check_acceptance(v, max_bytes=3000)
        self.assertEqual(rep.reds, [])
        self.assertEqual(rep.code, headers.EXIT_OK)
        self.assertTrue(any("over the 3000-byte cap" in w for w in rep.warns))
        self.assertTrue(any("recommendation" in w for w in rep.warns))
        self.assertTrue(any("outside the lane worktree" in w for w in rep.warns))
        # negative control: the plain verdict trips none of the three
        v2 = self.verdict(GOOD_VERDICT, name="007.acceptance.md")
        headers.write_acceptance_header(v2, desk=self.desk, lane="api")
        self.assertEqual(headers.check_acceptance(v2, max_bytes=3000).warns, [])

    def test_the_checker_reads_the_header_and_not_the_body(self):
        """The body is for people. Change the header, the reading changes; change the
        body, it does not."""
        v = self.verdict()
        headers.write_acceptance_header(v)
        self.assertEqual(headers.check_acceptance(v).reds, [])
        text = read(v)
        write(v, re.sub(r"^mutations: .*$", "mutations: 0", text, flags=re.M))
        self.assertTrue(any("no mutation" in r for r in headers.check_acceptance(v).reds))
        # the body still has both mutation lines, and it changes nothing
        self.assertIn("- broke: deleted note.txt", read(v))
        # the other direction: wreck the prose, keep the header, stay green
        head_block = read(v)[:read(v).index("```\n", 20) + 4]
        write(v, head_block.replace("mutations: 0", "mutations: 2") + "\nnot a section in sight\n")
        self.assertEqual(headers.check_acceptance(v).reds, [])

    # -- 6. one spec, one implementation -----------------------------------
    def test_the_terms_file_fits_in_two_thousand_bytes(self):
        terms = ROOT / "template" / "rules" / "acceptance-terms.md"
        self.assertTrue(fits(terms, 2000), f"{terms} is {terms.stat().st_size} bytes")
        for phrase in ("Read only", "broke:", "[blocking]", "VERDICT: pass", "not verified"):
            self.assertIn(phrase, read(terms))
        # negative control: the same gate fails on a file that is over the cap
        padded = self.tmp / "padded.md"
        write(padded, read(terms) + "x" * 2000)
        self.assertFalse(fits(padded, 2000))

    def test_the_docs_table_is_the_spec_from_the_code(self):
        doc = read(ROOT / "docs" / "headers.md")
        for kind in ("receipt", "acceptance"):
            self.assertIn(headers.spec_table(kind), doc,
                          f"docs/headers.md is behind the {kind} spec; run `bellhop header-spec`")
        # negative control: a table with one key renamed is not in the doc
        self.assertNotIn(headers.spec_table("receipt").replace("`head`", "`head_sha`"), doc)

    def test_only_headers_py_describes_the_header(self):
        fences = ["```" + headers.FENCE_RECEIPT, "```" + headers.FENCE_ACCEPTANCE]
        others = [p.name for p in (ROOT / "bellhop").glob("*.py")
                  if p.name != "headers.py" and any(f in read(p) for f in fences)]
        self.assertEqual(others, [])
        spec_src = read(ROOT / "bellhop" / "headers.py")
        self.assertIn(f'FENCE_RECEIPT = "{headers.FENCE_RECEIPT}"', spec_src)
        self.assertIn(f'FENCE_ACCEPTANCE = "{headers.FENCE_ACCEPTANCE}"', spec_src)
        # the templates may describe the header; they may not carry a second copy of it
        for tpl in ("receipt.md", "acceptance-prompt.md"):
            text = read(ROOT / "template" / "templates" / tpl)
            self.assertFalse(any(f in text for f in fences), tpl)
            self.assertIn("bellhop/headers.py", text)

    # -- 7. the CLI --------------------------------------------------------
    def test_cli_generates_and_checks(self):
        self.work()
        receipt = self.receipt()
        out = self.cli("receipt-header", "--receipt", str(receipt), "--lane", "api")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("```receipt-header", out.stdout)
        self.assertIn("header new in", out.stdout)
        out = self.cli("check-receipt", str(receipt), "--packet", str(self.packet_path))
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertTrue(out.stdout.strip().endswith("ok"))
        v = self.verdict()
        out = self.cli("acceptance-header", "--file", str(v), "--lane", "api")
        self.assertEqual(out.returncode, 0, out.stderr)
        out = self.cli("check-acceptance", str(v))
        self.assertEqual(out.returncode, 0, out.stdout)
        # negative control: the same two commands on a file with no header
        bare = self.verdict(GOOD_VERDICT, name="008.acceptance.md")
        out = self.cli("check-acceptance", str(bare))
        self.assertEqual(out.returncode, 1)
        self.assertIn("red:", out.stdout)
        out = self.cli("header-spec", "receipt")
        self.assertEqual(out.returncode, 0)
        self.assertIn("| `head` | str | yes |", out.stdout)

    def test_cli_dry_run_writes_nothing(self):
        self.work()
        receipt = self.receipt()
        before = receipt.read_bytes()
        out = self.cli("receipt-header", "--receipt", str(receipt), "--lane", "api", "--dry-run")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("```receipt-header", out.stdout)
        self.assertEqual(receipt.read_bytes(), before)

    # -- 8. wiring ---------------------------------------------------------
    def test_the_watchdog_writes_the_header_and_keeps_the_footer(self):
        ring.ring(self.desk, self.packet_path)
        result = watch.watch_once(self.desk, archive=False)
        self.assertIsNotNone(result)
        self.assertEqual(result["header"], "ok")
        receipt = self.desk.receipts / "001-add-a-note-file.md"
        text = read(receipt)
        self.assertTrue(text.startswith("```receipt-header"))
        self.assertIn("bellhop: {", text)  # the JSON footer is still there
        parsed, _ = headers.parse_header(text, headers.FENCE_RECEIPT)
        self.assertEqual(parsed["base"], self.base)
        self.assertEqual(parsed["check_exit"], "0")
        self.assertEqual(parsed["commits"], "1")
        self.assertEqual(parsed["worker_exit"], "0")
        self.assertEqual(parsed["dead"], "no")
        rep = headers.check_receipt(receipt, packet=self.packet)
        self.assertEqual(rep.reds, [])

    def test_accept_records_what_the_checker_found(self):
        ring.ring(self.desk, self.packet_path)
        watch.watch_once(self.desk, archive=False)
        # the stub acceptance session writes prose with no mutation: red, recorded
        accept_mod.accept(self.desk, "001")
        check = accept_mod.check_path(self.desk, "001")
        self.assertTrue(check.exists())
        self.assertIn("red:", read(check))
        self.assertTrue(read(check).strip().splitlines()[-1].startswith("red "))
        # positive control: a session that writes a complete verdict is recorded ok
        good = self.tmp / "good_accept.py"
        write(good, "import os\nprint(open(os.environ['BELLHOP_GOOD_VERDICT']).read())\n")
        body = self.tmp / "verdict.md"
        write(body, GOOD_VERDICT)
        cfg = read_json(self.desk_dir / "bellhop.json")
        cfg["lanes"]["api"]["accept_cmd"] = f"python3 {good}"
        cfg["lanes"]["api"]["env_allow"] = ["BELLHOP_GOOD_VERDICT"]
        os.environ["BELLHOP_GOOD_VERDICT"] = str(body)
        try:
            accept_mod.accept(Desk(self.desk_dir, cfg), "001")
        finally:
            os.environ.pop("BELLHOP_GOOD_VERDICT", None)
        self.assertEqual(read(check).strip().splitlines()[-1], "ok")


if __name__ == "__main__":
    unittest.main()
