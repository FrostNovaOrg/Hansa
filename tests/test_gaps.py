"""Gaps found by the first real end-to-end run.

Every gate here gets a positive case and a negative control, because the failure mode
they all share is silence: a file that was never archived looks exactly like a file that
never changed, and a lane that was quietly rewound looks exactly like a lane that was
already where it should be.
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

from bellhop import archive as archive_mod  # noqa: E402
from bellhop import desk_ops, exits, lanes, ring, watch  # noqa: E402
from bellhop.config import LANE_DEFAULTS, Desk  # noqa: E402
from bellhop.util import BellhopError, read, read_json, write, write_json  # noqa: E402

GIT_ID = ["-c", "user.name=test", "-c", "user.email=test@example.invalid"]


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *GIT_ID, *args], check=True, capture_output=True, text=True).stdout.strip()


def prefix_allows(rules, command):
    """Claude Code matches `Bash(<prefix>:*)` against the literal start of the command."""
    for rule in rules:
        if not (rule.startswith("Bash(") and rule.endswith(")")):
            continue
        body = rule[len("Bash("):-1]
        if body.endswith(":*"):
            if command.startswith(body[:-2]):
                return True
        elif command == body:
            return True
    return False


class GapTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bellhop-gaps."))
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
        cfg["lanes"]["api"]["worker_cmd"] = f"python3 {ROOT}/bellhop/stubworker.py"
        cfg["lanes"]["api"]["accept_cmd"] = f"python3 {ROOT}/bellhop/stubaccept.py"
        write(self.desk_dir / "bellhop.json", json.dumps(cfg, indent=2))
        git(self.desk_dir, "add", "-A")
        git(self.desk_dir, "commit", "-q", "-m", "configure")
        self.desk = Desk(self.desk_dir, cfg)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- helpers ----------------------------------------------------------

    def packet(self, pid="001", title="Add health check note", base=None):
        text = (
            f"---\nid: {pid}\nlane: api\ntitle: {title}\nbase: {base or self.base}\nbudget_minutes: 10\n"
            "acceptance:\n  - ./check.sh prints 'check.sh: all green'\n---\nAdd a note file. Keep checks green.\n"
        )
        p = self.desk.inbox / "api" / f"{pid}-note.md"
        write(p, text)
        return p

    def tracked(self):
        return set(git(self.desk_dir, "ls-files").splitlines())

    def commits(self):
        return len(git(self.desk_dir, "log", "--oneline").splitlines())

    def lane_worktree(self, base=None):
        """A lane worktree with one commit of its own, as a finished task leaves it."""
        wt = lanes.prepare_worktree(self.desk, self.desk.lane("api"), base or self.base)
        write(wt / "worker-change.txt", "the worker's work\n")
        git(wt, "add", "worker-change.txt")
        git(wt, "commit", "-q", "-m", "worker commit")
        return wt

    def record_task(self, pid="001", base=None):
        """The metadata a finished task leaves behind, which is where a reset reads its base."""
        write_json(self.desk.receipts / f"{pid}.meta.json",
                   {"id": pid, "lane": "api", "base": base or self.base, "finished_at": "2026-09-11T10:00:00+0800"})

    # -- 1. packets and bells enter git -----------------------------------

    def test_ring_commits_the_packet_and_the_bell(self):
        before = self.commits()
        p = self.packet()
        bell = ring.ring(self.desk, p)
        self.assertEqual(self.commits(), before + 1)
        self.assertEqual(git(self.desk_dir, "log", "-1", "--format=%s"), "[desk] ring 001 add-health-check-note")
        tracked = self.tracked()
        self.assertIn("inbox/api/001-note.md", tracked)
        self.assertIn(str(bell.relative_to(self.desk.root)), tracked)

    def test_ring_without_a_commit_leaves_the_packet_untracked(self):
        """Negative control: --no-commit is the only way a packet stays out of the history."""
        before = self.commits()
        bell = ring.ring(self.desk, self.packet("002", "Quiet packet"), commit=False)
        self.assertEqual(self.commits(), before)
        tracked = self.tracked()
        self.assertNotIn("inbox/api/002-note.md", tracked)
        self.assertNotIn(str(bell.relative_to(self.desk.root)), tracked)

    # -- 1b. the archive roster -------------------------------------------

    def test_roster_archives_new_desk_files_and_never_a_stray(self):
        write(self.desk.state / "scratch-note.md", "a new state file\n")
        write(self.desk_dir / "stray.txt", "someone's scratch file\n")
        archive_mod.archive(self.desk, [], "roster sweep")
        tracked = self.tracked()
        self.assertIn("state/scratch-note.md", tracked)
        self.assertNotIn("stray.txt", tracked)
        self.assertEqual(git(self.desk_dir, "status", "--porcelain"), "?? stray.txt")

    def test_archive_refuses_a_path_off_the_roster(self):
        """Negative control: naming the stray explicitly does not get it in either."""
        write(self.desk_dir / "stray.txt", "someone's scratch file\n")
        with self.assertRaises(BellhopError) as caught:
            archive_mod.archive(self.desk, [self.desk_dir / "stray.txt"], "sneak it in")
        self.assertIn("roster", str(caught.exception))
        self.assertNotIn("stray.txt", self.tracked())
        # positive: the same bytes under a roster directory are archived without argument
        write(self.desk.state / "stray.txt", "someone's scratch file\n")
        archive_mod.archive(self.desk, [self.desk.state / "stray.txt"], "on the roster")
        self.assertIn("state/stray.txt", self.tracked())

    def test_a_finished_task_leaves_the_desk_clean(self):
        """The bell moves pending/ -> rung/; the deletion has to be archived with it."""
        ring.ring(self.desk, self.packet("003", "Leave nothing behind"))
        result = watch.watch_once(self.desk)
        self.assertFalse(result["dead"])
        self.assertEqual(git(self.desk_dir, "status", "--porcelain"), "")
        tracked = self.tracked()
        self.assertFalse([t for t in tracked if t.startswith("bells/pending/2")], tracked)
        self.assertTrue([t for t in tracked if t.startswith("bells/rung/2")], tracked)

    # -- 5. the close archives desk configuration -------------------------

    def test_close_archives_configuration_changes(self):
        write(self.desk_dir / "rules" / "core.md", read(self.desk_dir / "rules" / "core.md") + "\n13. a new rule\n")
        cfg = read_json(self.desk_dir / "bellhop.json")
        cfg["sample_rate"] = 4
        write(self.desk_dir / "bellhop.json", json.dumps(cfg, indent=2))
        write(self.desk_dir / "stray.txt", "not a desk file\n")
        env = dict(os.environ, BELLHOP_DESK=str(self.desk_dir))
        out = subprocess.run([str(ROOT / "bin" / "bellhop"), "close"], env=env, capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        files = git(self.desk_dir, "log", "-1", "--name-only", "--format=")
        self.assertIn("rules/core.md", files)
        self.assertIn("bellhop.json", files)
        self.assertEqual(git(self.desk_dir, "status", "--porcelain"), "?? stray.txt")

    # -- 2. stuck lanes ---------------------------------------------------

    def test_lane_reset_refuses_unmerged_commits_until_forced(self):
        wt = self.lane_worktree()
        self.record_task()
        write(wt / "half-done.txt", "uncommitted\n")
        with self.assertRaises(lanes.LaneBlocked) as caught:
            lanes.lane_reset(self.desk, "api")
        self.assertIn("not on main", str(caught.exception))
        self.assertTrue((wt / "half-done.txt").exists())
        self.assertTrue((wt / "worker-change.txt").exists())
        report = lanes.lane_reset(self.desk, "api", force=True)
        self.assertEqual(report["head_after"], self.base)
        self.assertFalse((wt / "half-done.txt").exists())
        self.assertFalse((wt / "worker-change.txt").exists())
        self.assertEqual(git(wt, "status", "--porcelain"), "")
        self.assertTrue(report["last"].startswith("✅"))

    def test_lane_reset_needs_no_force_once_the_work_is_merged(self):
        wt = self.lane_worktree()
        self.record_task()
        git(self.repo, "merge", "-q", "--no-ff", "-m", "merge lane/api", "lane/api")
        write(wt / "leftover.txt", "untracked rubbish\n")
        write(wt / "README.md", "locally mangled\n")
        report = lanes.lane_reset(self.desk, "api")
        self.assertEqual(report["head_after"], self.base)
        self.assertEqual(report["dirty"], 2)
        self.assertFalse((wt / "leftover.txt").exists())
        self.assertNotEqual(read(wt / "README.md"), "locally mangled\n")
        self.assertIn("last task 001.meta.json", report["source"])

    def test_lane_reset_dry_run_changes_nothing(self):
        wt = self.lane_worktree()
        self.record_task()
        git(self.repo, "merge", "-q", "--no-ff", "-m", "merge lane/api", "lane/api")
        head = git(wt, "rev-parse", "HEAD")
        write(wt / "leftover.txt", "untracked rubbish\n")
        report = lanes.lane_reset(self.desk, "api", dry_run=True)
        self.assertTrue(report["dry_run"])
        self.assertEqual(git(wt, "rev-parse", "HEAD"), head)
        self.assertTrue((wt / "leftover.txt").exists())
        self.assertIn("dry run", report["last"])

    def test_lane_reset_uses_the_pending_packet_base_over_the_last_task(self):
        write(self.repo / "NOTE.md", "main moved on\n")
        git(self.repo, "add", "NOTE.md")
        git(self.repo, "commit", "-q", "-m", "main moves on")
        newer = git(self.repo, "rev-parse", "--short", "HEAD")
        wt = self.lane_worktree()
        self.record_task(base=self.base)
        ring.ring(self.desk, self.packet("004", "Next task", base=newer))
        report = lanes.lane_reset(self.desk, "api", force=True)
        self.assertIn("pending bell", report["source"])
        self.assertEqual(git(wt, "rev-parse", "--short", "HEAD"), newer)

    def test_lane_align_fast_forwards_only_a_merged_clean_lane(self):
        wt = self.lane_worktree()
        with self.assertRaises(lanes.LaneBlocked) as caught:
            lanes.lane_align(self.desk, "api")  # unmerged commit
        self.assertIn("commit(s) not on main", str(caught.exception))
        git(self.repo, "merge", "-q", "--no-ff", "-m", "merge lane/api", "lane/api")
        write(self.repo / "NOTE.md", "main moved on\n")
        git(self.repo, "add", "NOTE.md")
        git(self.repo, "commit", "-q", "-m", "main moves on")
        write(wt / "leftover.txt", "untracked rubbish\n")
        with self.assertRaises(lanes.LaneBlocked) as caught:
            lanes.lane_align(self.desk, "api")  # dirty worktree
        self.assertIn("uncommitted", str(caught.exception))
        (wt / "leftover.txt").unlink()
        report = lanes.lane_align(self.desk, "api")
        self.assertEqual(git(wt, "rev-parse", "HEAD"), git(self.repo, "rev-parse", "main"))
        self.assertTrue(report["moved"])
        self.assertTrue(report["last"].startswith("✅"))

    def test_lane_commands_refuse_with_the_gate_code(self):
        """A lane holding work is a gate, not a typo: docs/exit-codes.md says 4."""
        self.lane_worktree()
        env = dict(os.environ, BELLHOP_DESK=str(self.desk_dir))
        for cmd in (["lane", "reset", "api"], ["lane", "align", "api"]):
            out = subprocess.run([str(ROOT / "bin" / "bellhop"), *cmd], env=env, capture_output=True, text=True)
            self.assertEqual(out.returncode, exits.GATE, out.stdout + out.stderr)
            self.assertTrue(out.stdout.strip().splitlines()[-1].startswith("🛑"), out.stdout)
        # positive control: the same command succeeds and ends in a tick once forced
        out = subprocess.run([str(ROOT / "bin" / "bellhop"), "lane", "reset", "api", "--force"],
                             env=env, capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertTrue(out.stdout.strip().splitlines()[-1].startswith("✅"), out.stdout)

    # -- 3. the template .gitignore ---------------------------------------

    def test_init_copies_the_gitignore(self):
        gitignore = self.desk_dir / ".gitignore"
        self.assertTrue(gitignore.exists(), "init must copy dotfiles from the template")
        self.assertIn(".gitignore", self.tracked())
        body = read(gitignore)
        for pattern in (".worktrees/", "state/.previous/", "bells/logs/*.log", "__pycache__/"):
            self.assertIn(pattern, body)

    def test_worker_logs_are_ignored_but_their_results_are_not(self):
        write(self.desk.bells_logs / "20260911-000000-001.log", "noise\n")
        write(self.desk.bells_logs / "20260911-000000-001.prompt.md", "the words the worker got\n")
        write(self.desk.bells_logs / "20260911-000000-001.json", '{"cost_usd": 0.1}\n')
        archive_mod.archive(self.desk, [], "logs")
        tracked = self.tracked()
        self.assertNotIn("bells/logs/20260911-000000-001.log", tracked)
        self.assertIn("bells/logs/20260911-000000-001.json", tracked)
        self.assertIn("bells/logs/20260911-000000-001.prompt.md", tracked)

    # -- 4. tool allowlist prefix matching --------------------------------

    def test_lane_allowlist_covers_git_with_a_dash_c(self):
        rules = LANE_DEFAULTS["allowed_tools"]
        self.assertNotIn("Bash", rules, "a bare Bash wildcard allows everything")
        for command in ("git status --porcelain", "git -C /tmp/lane status --porcelain",
                        "git -C /tmp/lane commit -m 'work'", "git add -A"):
            self.assertTrue(prefix_allows(rules, command), command)
        example = read_json(ROOT / "template" / "bellhop.example.json")["lanes"]["api"]["allowed_tools"]
        self.assertTrue(prefix_allows(example, "git -C /tmp/lane commit -m 'work'"))
        self.assertNotIn("Bash", example)

    def test_a_narrow_git_rule_is_the_bug_being_fixed(self):
        """Negative control: this is what the first real run shipped with."""
        self.assertFalse(prefix_allows(["Bash(git status:*)"], "git -C /tmp/lane status"))
        self.assertTrue(prefix_allows(["Bash(git status:*)"], "git status"))
        # The rule is a literal prefix, not a word: `Bash(git:*)` covers `gitk` as well.
        # That is the second reason the allowlist cannot be the push gate.
        self.assertTrue(prefix_allows(["Bash(git:*)"], "gitk --all"))

    def test_the_push_gate_is_the_hook_not_the_allowlist(self):
        """`Bash(git:*)` does allow `git push`; the hook is what refuses it."""
        self.assertTrue(prefix_allows(LANE_DEFAULTS["allowed_tools"], "git push"))
        wt = self.lane_worktree()
        hook = Path(git(self.repo, "rev-parse", "--git-path", "hooks")) / "pre-push"
        if not hook.is_absolute():
            hook = self.repo / hook
        self.assertTrue(hook.exists())
        bare = self.tmp / "origin.git"
        git(self.repo, "init", "-q", "--bare", str(bare))
        git(wt, "remote", "add", "origin", str(bare))
        refused = subprocess.run(["git", "-C", str(wt), *GIT_ID, "push", "-q", "origin", "lane/api"],
                                 capture_output=True, text=True)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("push refused", refused.stderr)
        allowed = subprocess.run(["git", "-C", str(wt), *GIT_ID, "push", "-q", "origin", "lane/api"],
                                 capture_output=True, text=True, env=dict(os.environ, BELLHOP_MERGE="1"))
        self.assertEqual(allowed.returncode, 0, allowed.stderr)


if __name__ == "__main__":
    unittest.main()
