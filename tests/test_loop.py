import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bellhop import __version__, card, decisions, desk_ops, ledger, ring, sampling, seal, sentinel, watch  # noqa: E402
from bellhop.config import Desk  # noqa: E402
from bellhop.packet import PacketError, parse_packet  # noqa: E402
from bellhop.util import BellhopError, read, read_json, write  # noqa: E402

GIT_ID = ["-c", "user.name=test", "-c", "user.email=test@example.invalid"]


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *GIT_ID, *args], check=True, capture_output=True, text=True).stdout.strip()


class LoopTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bellhop-test."))
        self.repo = self.tmp / "widget-service"
        shutil.copytree(ROOT / "examples" / "widget-service", self.repo)
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "initial")
        self.base = git(self.repo, "rev-parse", "--short", "HEAD")
        self.desk_dir = self.tmp / "desk"
        desk_ops.init_desk(self.desk_dir)
        cfg = read_json(self.desk_dir / "bellhop.json")
        cfg["lanes"]["api"]["repo"] = "../widget-service"
        cfg["lanes"]["api"]["worker_cmd"] = f"python3 {ROOT}/bellhop/stubworker.py"
        cfg["lanes"]["api"]["accept_cmd"] = f"python3 {ROOT}/bellhop/stubaccept.py --good"
        write(self.desk_dir / "bellhop.json", json.dumps(cfg, indent=2))
        git(self.desk_dir, "add", "-A")
        git(self.desk_dir, "commit", "-q", "-m", "configure")
        self.desk = Desk(self.desk_dir, cfg)
        os.environ["GIT_AUTHOR_NAME"] = os.environ["GIT_COMMITTER_NAME"] = "test"
        os.environ["GIT_AUTHOR_EMAIL"] = os.environ["GIT_COMMITTER_EMAIL"] = "test@example.invalid"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def packet(self, pid="001", title="Add health check note"):
        text = (
            f"---\nid: {pid}\nlane: api\ntitle: {title}\nbase: {self.base}\nbudget_minutes: 10\n"
            "acceptance:\n  - ./check.sh prints 'check.sh: all green'\n  - a note file exists\n---\nAdd a note file. Keep checks green.\n"
        )
        p = self.desk.inbox / "api" / f"{pid}-note.md"
        write(p, text)
        return p

    def test_packet_validation(self):
        bad = self.desk.inbox / "api" / "002-bad.md"
        write(bad, "---\nid: 002\nlane: api\ntitle: x\n---\nbody\n")
        with self.assertRaises(PacketError):
            parse_packet(bad)
        good = parse_packet(self.packet())
        self.assertEqual(good.acceptance[0], "./check.sh prints 'check.sh: all green'")

    def test_full_loop(self):
        p = self.packet()
        bell = ring.ring(self.desk, p)
        self.assertTrue(bell.exists())
        with self.assertRaises(BellhopError):
            ring.ring(self.desk, p)  # duplicate id
        card.refresh_card(self.desk, ready="yes", next="001", lane="api", reason="")
        time.sleep(1.1)  # sentinel timestamps have one-second resolution
        result = watch.watch_once(self.desk)
        self.assertIsNotNone(result)
        self.assertFalse(result["dead"])
        receipt = self.desk.receipts / "001-add-health-check-note.md"
        self.assertTrue(receipt.exists())
        self.assertIn("bellhop: {", read(receipt))
        meta = read_json(self.desk.receipts / "001.meta.json")
        self.assertEqual(meta["exit_code"], 0)
        self.assertNotEqual(meta["head_before"], meta["head_after"])
        self.assertFalse(list(self.desk.bells_pending.glob("*.bell")))
        self.assertEqual(len(list(self.desk.bells_rung.glob("*.bell"))), 1)
        self.assertIn("- none", read(self.desk.state / "in-flight.md"))
        log = git(self.desk_dir, "log", "--oneline")
        self.assertIn("receipt 001-add-health-check-note", log)
        # lane worktree has the stub commit; pre-push hook installed
        wt = Path(self.desk.lane("api")["worktree"])
        self.assertTrue((wt / "bellhop-stub-001.txt").exists())
        hook = Path(git(self.repo, "rev-parse", "--git-path", "hooks")) / "pre-push"
        if not hook.is_absolute():
            hook = self.repo / hook
        self.assertTrue(hook.exists())
        # sentinel: card older than receipt -> red; refresh -> green
        green, text = sentinel.sentinel(self.desk, wake=False)
        self.assertFalse(green)
        self.assertIn("RED card", text)
        card.refresh_card(self.desk, ready="no", reason="queue empty")
        green, text = sentinel.sentinel(self.desk, wake=False)
        self.assertTrue(green, text)
        # acceptance in a "fresh session" (stub)
        from bellhop import accept as accept_mod
        out = accept_mod.accept(self.desk, "001")
        self.assertIn("VERDICT: pass", read(out))
        # merge: the acceptance-gated path closes it, and the branch is on main
        from bellhop import closetask as closetask_mod
        report = closetask_mod.close_task(self.desk, "001", say=lambda line: None)
        self.assertTrue(report.merged_sha)
        self.assertIn("merge lane/api", git(self.repo, "log", "-1", "--format=%s"))

    def test_dead_task_is_counted_until_acknowledged(self):
        p = self.packet("003", "Crash on purpose")
        ring.ring(self.desk, p)
        os.environ["BELLHOP_STUB_MODE"] = "crash"
        try:
            cfg = self.desk.cfg
            cfg["lanes"]["api"]["env_allow"] = ["BELLHOP_STUB_MODE"]
            result = watch.watch_once(self.desk)
        finally:
            os.environ.pop("BELLHOP_STUB_MODE", None)
        self.assertTrue(result["dead"])
        self.assertTrue((self.desk.receipts / "003-crash-on-purpose.md").exists())
        card.refresh_card(self.desk, ready="no", reason="investigating dead task")
        checks = {c[0]: c for c in sentinel.run_checks(self.desk)}
        self.assertFalse(checks["dead-tasks"][1])
        ledger.add(self.desk, "dead task 003 acknowledged", "Worker crashed; re-dispatch as 004.", refs=["003"])
        checks = {c[0]: c for c in sentinel.run_checks(self.desk)}
        self.assertTrue(checks["dead-tasks"][1])

    def test_uncommitted_work_is_a_dead_task(self):
        """A worker that edits but never commits leaves the lane wedged; it is not a success."""
        p = self.packet("005", "Leave work uncommitted")
        ring.ring(self.desk, p)
        os.environ["BELLHOP_STUB_MODE"] = "nocommit"
        self.desk.cfg["lanes"]["api"]["env_allow"] = ["BELLHOP_STUB_MODE"]
        try:
            result = watch.watch_once(self.desk)
        finally:
            os.environ.pop("BELLHOP_STUB_MODE", None)
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["head_before"], result["head_after"])
        self.assertTrue(result["dirty_worktree"])
        self.assertTrue(result["dead"], "a dirty lane worktree must be reported as a dead task")
        meta = read_json(self.desk.receipts / "005.meta.json")
        self.assertTrue(meta["dead"])
        self.assertTrue(meta["dirty_worktree"])
        self.assertIn('"dirty_worktree": true', read(self.desk.receipts / "005-leave-work-uncommitted.md"))
        card.refresh_card(self.desk, ready="no", reason="dead task 005")
        checks = {c[0]: c for c in sentinel.run_checks(self.desk)}
        self.assertFalse(checks["dead-tasks"][1])

    def test_default_lane_lets_a_worker_run_checks_and_commit(self):
        """`acceptEdits` alone refuses every Bash call in print mode, so lanes need an allowlist."""
        from bellhop.config import LANE_DEFAULTS
        from bellhop.watch import _claude_cmd
        self.assertTrue(any(t.startswith("Bash(") for t in LANE_DEFAULTS["allowed_tools"]))
        example = read_json(ROOT / "template" / "bellhop.example.json")
        self.assertTrue(any(t.startswith("Bash(") for t in example["lanes"]["api"]["allowed_tools"]))
        lane = dict(self.desk.lane("api"))
        lane["allowed_tools"] = LANE_DEFAULTS["allowed_tools"]
        cmd = _claude_cmd(self.desk, lane, parse_packet(self.packet("006", "Cmd shape")), "prompt")
        self.assertIn("--allowedTools", cmd)
        self.assertTrue(any(t.startswith("Bash(") for t in cmd[cmd.index("--allowedTools") + 1].split(",")))

    def test_second_task_starts_from_its_own_base(self):
        """An existing lane worktree must be reset to the new packet's base, not left where it was."""
        from bellhop import lanes
        ring.ring(self.desk, self.packet("007", "First"))
        card.refresh_card(self.desk, ready="yes", next="007", lane="api", reason="")
        first = watch.watch_once(self.desk)
        self.assertFalse(first["dead"])
        # a new base appears on main while the lane branch still carries task 007
        write(self.repo / "NOTE.md", "moved on\n")
        git(self.repo, "add", "NOTE.md")
        git(self.repo, "commit", "-q", "-m", "main moves on")
        new_base = git(self.repo, "rev-parse", "--short", "HEAD")
        wt = lanes.prepare_worktree(self.desk, self.desk.lane("api"), new_base)
        self.assertEqual(git(wt, "rev-parse", "HEAD"), git(self.repo, "rev-parse", new_base))
        self.assertFalse((wt / "bellhop-stub-007.txt").exists())

    def test_ledger_and_decisions_are_append_only(self):
        ledger.add(self.desk, "first", "body")
        ledger.add(self.desk, "second", "body")
        card.refresh_card(self.desk, ready="no", reason="x")
        sentinel.run_checks(self.desk)
        decisions.decide(self.desk, "ship it", who="principal")
        sentinel.run_checks(self.desk)
        # tamper: delete a ledger entry and truncate decisions
        entries = sorted(self.desk.ledger.rglob("*.md"))
        entries[0].unlink()
        dp = self.desk.state / "decisions.md"
        write(dp, read(dp)[:20])
        checks = {c[0]: c for c in sentinel.run_checks(self.desk)}
        self.assertFalse(checks["ledger"][1])
        self.assertFalse(checks["decisions"][1])

    def test_sampling_is_deterministic(self):
        self.desk.cfg["weekly_seed"] = "seed-A"
        self.desk.cfg["sample_rate"] = 3
        a = [sampling.sampled(self.desk, str(i))[0] for i in range(60)]
        b = [sampling.sampled(self.desk, str(i))[0] for i in range(60)]
        self.assertEqual(a, b)
        self.assertTrue(5 <= sum(a) <= 35)
        self.desk.cfg["sample_rate"] = 1
        self.assertTrue(all(sampling.sampled(self.desk, str(i))[0] for i in range(10)))

    def test_seals_detect_changes(self):
        seal.seal(self.desk)
        self.assertEqual(seal.verify(self.desk), [])
        rules = self.desk.path("rules", "core.md")
        write(rules, read(rules) + "\n13. sneaky rule\n")
        changed = seal.verify(self.desk)
        self.assertIn("desk:rules/core.md", changed)

    def test_daily_close_scans_seals_once_and_reports_changes(self):
        card.refresh_card(self.desk, ready="no", reason="queue empty")
        seal.seal(self.desk)
        with patch.object(seal, "verify", wraps=seal.verify) as verify:
            report = desk_ops.close(self.desk)
        self.assertEqual(verify.call_count, 1)
        self.assertIn("Seals: all seals hold", report)

        rules = self.desk.path("rules", "core.md")
        write(rules, read(rules) + "\nUnexpected edit.\n")
        with patch.object(seal, "verify", wraps=seal.verify) as verify:
            report = desk_ops.close(self.desk)
        self.assertEqual(verify.call_count, 1)
        self.assertIn("Sentinel: RED", report)
        self.assertIn("Seals: 1 sealed file(s) changed: desk:rules/core.md", report)
        self.assertTrue(any("desk:rules/core.md" in read(p) for p in self.desk.ledger.rglob("*.md")))

        seal.manifest_path(self.desk).unlink()
        with patch.object(seal, "verify", wraps=seal.verify) as verify:
            report = desk_ops.close(self.desk)
        self.assertEqual(verify.call_count, 1)
        self.assertIn("no seals yet", report)
        self.assertIn("bellhop seal", report)
        self.assertNotIn("all seals hold", report)

    def test_card_requires_reason_when_not_ready(self):
        with self.assertRaises(BellhopError):
            card.refresh_card(self.desk, ready="no", reason="")

    def test_cli_smoke(self):
        env = dict(os.environ, BELLHOP_DESK=str(self.desk_dir))
        out = subprocess.run([str(ROOT / "bin" / "bellhop"), "coldstart"], env=env, capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("Cold-start reading list", out.stdout)
        out = subprocess.run([str(ROOT / "bin" / "bellhop"), "--version"], capture_output=True, text=True)
        self.assertIn(f"bellhop {__version__}", out.stdout)


if __name__ == "__main__":
    unittest.main()
