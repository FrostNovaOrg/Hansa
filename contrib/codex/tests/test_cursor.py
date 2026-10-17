"""The Cursor backend: a worker run on the Cursor provider runs cursor-agent in a copy of the worktree, and the copy's
changes come back as a patch (README, "The Cursor backend"). The fake cursor-agent does each row's commands in its
workspace."""
import json
import os
import pwd
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path

from .codexkit import FAKES, WRAPPER, Scratch, git, hansa_run, shim

CURSOR_URL = "http://cursor.invalid"
VERSION = "2026.10.01-e373342"  # the fake's -v, as measured
CLI_CONFIG = {"version": 1, "permissions": {"allow": [], "deny": ["Mcp(*:*)", "WebFetch(*)"]},
              "sandbox": {"mode": "enabled", "networkAccess": "user_config_only"}, "approvalMode": "allowlist",
              "channel": "static", "autoAcceptWebSearch": False}
RESULT = "Did as the task said; nothing left undone."  # the fake's result line
USAGE = {"inputTokens": 1200, "outputTokens": 340, "cacheReadInputTokens": 5000, "cacheCreationInputTokens": 60}
# The Cursor backend is macOS's: a test that makes a worktree for it skips elsewhere
ONLY_MACOS = "the Cursor backend is for macOS, where Cursor's login is in the keychain and its sandbox was measured"


def status(wt) -> list:
    out = subprocess.run(["git", "status", "--porcelain", "--untracked-files=all"], cwd=str(wt), check=True,
                         capture_output=True, text=True).stdout
    return sorted(out.splitlines())


class CursorScratch(Scratch):
    """Scratch, with files in the worktree's commit to edit, delete and change the mode of."""

    def setUp(self):
        if sys.platform != "darwin":
            self.skipTest(ONLY_MACOS)
        super().setUp()
        for name, text in (("keep.txt", "one\n"), ("gone.txt", "bye\n"), ("run.sh", "echo hi\n")):
            (self.wt / name).write_text(text)
        git("add", "-A", cwd=self.wt)
        git("commit", "-q", "-m", "files", cwd=self.wt)
        self.cursor_record = self.root / "cursor-record.json"
        # the shim keeps the main repository's git folders read-only while a run runs (the copy's git boundary);
        # if a stop test kills it before it or its watchdog puts the modes back, give the owner write again so
        # the scratch can be removed
        self.addCleanup(subprocess.run, ["chmod", "-R", "u+rwX", str(self.root / "repo")], stderr=subprocess.DEVNULL)

    def cursor_env(self, script="", **extra):
        env = self.env()
        env.update({"ANTHROPIC_BASE_URL": CURSOR_URL, "HANSA_CURSOR_ENABLE": "on",  # off unless a desk turns it on
                    "HANSA_CURSOR_BIN": str(FAKES / "cursor-agent"),
                    "HANSA_CURSOR_VERSION": VERSION, "HANSA_CURSOR_LOGS": str(self.logs),
                    "FAKE_CURSOR_RECORD": str(self.cursor_record), "FAKE_CURSOR_SCRIPT": script,
                    # the fake's own python would cache its bytecode in the run's HOME (macOS: Library/Caches)
                    "PYTHONDONTWRITEBYTECODE": "1"})
        env.update(extra)
        return env

    def run_cursor(self, script="", argv=None, task="Add hello.txt.\n", cwd=None, **extra):
        p = self.start(argv or self.worker_argv(), self.cursor_env(script, **extra), task=task, cwd=cwd)
        out, err = p.communicate(timeout=120)
        self.err = err.decode(errors="replace")
        self.assertTrue(out.strip(), f"no answer; stderr: {self.err}")
        return json.loads(out), p.returncode

    def cursor_called(self):
        return json.loads(self.cursor_record.read_text())

    def assert_folder_gone(self):
        self.assertEqual(list(self.tmp.glob("cursor-*")), [], "the run's folder is removed")

    def refused(self, script="printf 'x\\n' > made.txt", **extra):
        """A run refused before Cursor starts: its answer's text."""
        before = status(self.wt)
        run, code = self.run_cursor(script, **extra)
        self.assertEqual((code, run["type"], run["subtype"], run["is_error"]),
                         (1, "result", "error_during_execution", True), run["result"])
        self.assertTrue(run["result"].startswith("hansa-cursor: "), run["result"])
        self.assertFalse(self.cursor_record.exists(), "Cursor never started")
        self.assertEqual(status(self.wt), before)
        self.assertEqual(hansa_run.unusable_reason(run, self.err), ("", ""), "the run's problem, not the provider's")
        self.assert_folder_gone()
        return run["result"]


class CursorRun(CursorScratch):
    def test_changes_come_back(self):
        # row 1: an edit, a new file, a deletion and a mode change; nothing else
        run, code = self.run_cursor("printf 'two\\n' >> keep.txt && printf 'new\\n' > new.txt && rm gone.txt && "
                                    "chmod +x run.sh")
        self.assertEqual(self.cursor_called()["script_exit"], 0)
        self.assertEqual((code, run["type"], run["subtype"], run["is_error"]), (0, "result", "success", False))
        self.assertEqual(status(self.wt), [" D gone.txt", " M keep.txt", " M run.sh", "?? new.txt"])
        self.assertEqual((self.wt / "keep.txt").read_text(), "one\ntwo\n")
        self.assertEqual((self.wt / "new.txt").read_text(), "new\n")
        self.assertEqual((self.wt / "run.sh").read_text(), "echo hi\n")
        self.assertTrue(os.access(self.wt / "run.sh", os.X_OK))
        self.assertEqual(run["result"], RESULT)
        self.assertEqual(run["session_id"], self.sid)
        self.assertEqual(run["cursor_session_id"], "c0ffee00-cafe-4bad-8000-000000000001")
        self.assertEqual(run["num_turns"], 2, "the tool calls started")
        self.assertEqual(run["modelUsage"], {"grok-4.7-high": USAGE})
        self.assertEqual(run["usage"], {"grok-4.7-high": USAGE})
        self.assertEqual(hansa_run.usage_of(run), {"grok-4.7-high": {
            "in": 1200, "out": 340, "cache_read": 5000, "cache_write": 60, "claude_code_usd": None, "basis": ""}})
        self.assertEqual(hansa_run.unusable_reason(run, self.err), ("", ""))
        self.assertEqual(hansa_run.network_reason(run, self.err), "")
        patch = (self.logs / f"{self.sid}.patch").read_text()
        for p in ("keep.txt", "new.txt", "gone.txt", "run.sh", "new mode 100755"):
            self.assertIn(p, patch)
        self.assertTrue((self.logs / f"{self.sid}.jsonl").read_text().strip())
        self.assertTrue((self.logs / f"{self.sid}.err").exists())
        self.assert_folder_gone()

    def repository_state(self):
        repo = self.root / "repo"
        refs = subprocess.run(["git", "for-each-ref", "--format=%(refname) %(objectname)"], cwd=str(repo),
                              check=True, capture_output=True, text=True).stdout
        objects = sorted(str(p.relative_to(repo / ".git" / "objects")) for p in (repo / ".git" / "objects").rglob("*"))
        heads = [subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(d), check=True, capture_output=True,
                                text=True).stdout for d in (repo, self.wt)]
        return refs, objects, heads

    def test_git_in_the_copy_leaves_the_repository_alone(self):
        # row 2: Cursor's sandbox opens the workspace's git folder; the copy's is a repository of its own
        before = self.repository_state()
        run, _ = self.run_cursor("printf 'x\\n' > f.txt && git add -A && "
                                 "git -c user.name=c -c user.email=c@c commit -q -m in-copy && "
                                 "git update-ref refs/heads/main HEAD && git branch x && "
                                 "test \"$(git rev-parse main)\" = \"$(git rev-parse HEAD)\" && "
                                 "test \"$(git rev-list --count HEAD)\" = 2")
        self.assertEqual(self.cursor_called()["script_exit"], 0, "the copy's git took the commit, main and x")
        self.assertEqual(run["subtype"], "success", run["result"])
        self.assertEqual(self.repository_state(), before)
        self.assertEqual(status(self.wt), ["?? f.txt"])
        self.assertEqual((self.wt / "f.txt").read_text(), "x\n")

    def test_copy_is_a_repository_of_its_own(self):
        # what git status and git diff show in the copy is what they show in the worktree; one commit, shallow
        (self.wt / "keep.txt").write_text("changed\n")
        (self.wt / "untracked.txt").write_text("u\n")
        (self.wt / ".gitignore").write_text("*.log\n")
        (self.root / "repo" / ".git" / "info" / "exclude").write_text("*.tmp\n")
        out = self.root / "seen"
        run, _ = self.run_cursor(f"git status --porcelain --untracked-files=all > {out}.status && "
                                 f"git symbolic-ref HEAD > {out}.head && git log --format=%H > {out}.log && "
                                 f"git config --get-regexp 'remote|alternates' > {out}.remote; "
                                 f"cat .git/shallow > {out}.shallow; ls .git > {out}.ls; "
                                 f"cp .git/info/exclude {out}.exclude")
        self.assertEqual(run["subtype"], "success", run["result"])
        self.assertEqual(sorted(Path(f"{out}.status").read_text().splitlines()),
                         [" M keep.txt", "?? .gitignore", "?? untracked.txt"])
        self.assertEqual(Path(f"{out}.head").read_text(), "refs/heads/task/t1\n")
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(self.wt), capture_output=True,
                              text=True).stdout
        self.assertEqual(Path(f"{out}.log").read_text(), head)
        self.assertEqual(Path(f"{out}.shallow").read_text(), head)
        self.assertEqual(Path(f"{out}.remote").read_text(), "")
        self.assertNotIn("FETCH_HEAD", Path(f"{out}.ls").read_text().split())
        self.assertEqual(Path(f"{out}.exclude").read_text(), "*.tmp\n")
        self.assertEqual(status(self.wt), [" M keep.txt", "?? .gitignore", "?? untracked.txt"], "nothing changed")

    def test_ignored_files_do_not_come_back_tracked_ones_do(self):
        (self.wt / ".gitignore").write_text("build/\n*.log\n")
        (self.wt / "kept.log").write_text("tracked though ignored\n")
        git("add", "-f", ".gitignore", "kept.log", cwd=self.wt)
        git("commit", "-q", "-m", "ignore", cwd=self.wt)
        (self.root / "repo" / ".git" / "info" / "exclude").write_text("*.tmp\n")
        run, _ = self.run_cursor("mkdir build && printf b > build/out.o && printf t > x.tmp && printf l > new.log && "
                                 "printf 'more\\n' >> kept.log && printf 'a\\r\\nb\\r\\n' > crlf.txt && "
                                 "printf '* text eol=lf\\n' > .gitattributes")
        self.assertEqual(run["subtype"], "success", run["result"])
        self.assertEqual(status(self.wt), [" M kept.log", "?? .gitattributes", "?? crlf.txt"])
        self.assertEqual((self.wt / "kept.log").read_text(), "tracked though ignored\nmore\n")
        self.assertEqual((self.wt / "crlf.txt").read_bytes(), b"a\r\nb\r\n", "the copy's bytes, as written")
        for p in ("build", "x.tmp", "new.log"):
            self.assertFalse((self.wt / p).exists(), p)

    def test_failed_run_changes_still_come_back(self):
        # as a Claude worker's changes stay in the worktree when it fails
        run, code = self.run_cursor("printf 'half\\n' > half.txt", FAKE_CURSOR_NO_RESULT="1", FAKE_CURSOR_EXIT="1",
                                    FAKE_CURSOR_STDERR="Error: stopped")
        self.assertEqual((code, run["subtype"]), (1, "error_during_execution"))
        self.assertEqual(status(self.wt), ["?? half.txt"])


GIT = "git -c user.name=c -c user.email=c@c"  # a worker's git, meaning to land a commit in the main repository


class CursorGitBoundary(CursorScratch):
    """A commit made in the copy cannot reach a repository outside it, however the copy's .git is pointed at the
    main repository: by rewriting the copy's .git, by a .git file in a subfolder, by a symlink, by a '..' relative
    path or by a nested repository. The shim itself keeps the worktree's git folders read-only for the run's
    length (the fake runs no sandbox, so the hold is the shim's own here). Each row left the main repository
    CHANGED before the boundary was held; each must leave it the same now."""

    def main_state(self):
        repo = self.root / "repo"
        refs = subprocess.run(["git", "for-each-ref", "--format=%(refname) %(objectname)"], cwd=str(repo),
                              capture_output=True, text=True).stdout
        objects = sorted(str(p.relative_to(repo)) for p in (repo / ".git" / "objects").rglob("*") if p.is_file())
        return refs, objects

    def leaves_main_alone(self, script):
        """Run the redirect `script`, then assert nothing of the main repository's refs or objects moved: the
        commit it tried to make landed nowhere outside the copy."""
        before = self.main_state()
        run, _ = self.run_cursor(script)
        self.assertTrue(self.cursor_record.exists(), f"Cursor ran: {run['result']}")
        self.assertEqual(self.main_state(), before, f"the main repository is untouched: {run['result']}")
        return run

    def test_rewrite_the_copys_dot_git(self):
        # the copy's own .git rewritten to a file pointing at the main repository
        main = self.root / "repo" / ".git"
        self.leaves_main_alone(f"rm -rf .git && printf 'gitdir: {main}\\n' > .git && printf 'x\\n' >> keep.txt && "
                               f"{GIT} add -A && {GIT} commit -q -m escape")

    def test_dot_git_file_in_a_subfolder(self):
        # a .git file in a subfolder, so a command run there acts on the main repository
        main = self.root / "repo" / ".git"
        self.leaves_main_alone(f"mkdir sub && printf 'gitdir: {main}\\n' > sub/.git && cd sub && "
                               f"printf 'z\\n' > z.txt && {GIT} add -A && {GIT} commit -q -m escape")

    def test_symlinked_dot_git(self):
        # the copy's .git replaced by a symlink to the main repository's git folder
        main = self.root / "repo" / ".git"
        self.leaves_main_alone(f"rm -rf .git && ln -s '{main}' .git && printf 'x\\n' >> keep.txt && "
                               f"{GIT} add -A && {GIT} commit -q -m escape")

    def test_dot_dot_relative_path(self):
        # a .git file whose gitdir is a '..' relative path climbing out of the copy to the main repository
        main = self.root / "repo" / ".git"
        self.leaves_main_alone(
            "mkdir -p a/b && rel=$(python3 -c 'import os,sys; print(os.path.relpath(sys.argv[1], sys.argv[2]))' "
            f"'{main}' \"$PWD/a/b\") && printf 'gitdir: %s\\n' \"$rel\" > a/b/.git && cd a/b && "
            f"printf 'z\\n' > z.txt && {GIT} add -A && {GIT} commit -q -m escape")

    def test_nested_repository_reaching_out(self):
        # a nested repository that pushes to the main repository (a new ref there would change it)
        main = self.root / "repo" / ".git"
        self.leaves_main_alone(f"{GIT} init -q nested && cd nested && printf 'z\\n' > z.txt && "
                               f"{GIT} add -A && {GIT} commit -q -m local && "
                               f"{GIT} push -q '{main}' HEAD:refs/heads/injected")

    def test_a_command_that_puts_the_write_bits_back(self):
        # the hold is the same account's modes: a command in the copy can restore them where no sandbox stops it
        # (the fake runs none). The after-run check is the net then: the run fails and nothing comes back.
        main = self.root / "repo" / ".git"
        before = self.main_state()
        run, _ = self.run_cursor(f"chmod -R u+w '{main}' && rm -rf .git && printf 'gitdir: {main}\\n' > .git && "
                                 f"printf 'x\\n' >> keep.txt && {GIT} add -A && {GIT} commit -q -m escape")
        self.assertEqual(self.cursor_called()["script_exit"], 0, "the command got past the hold")
        self.assertNotEqual(self.main_state(), before, "the commit landed: detected, not prevented")
        self.assertEqual(run["subtype"], "error_during_execution", run["result"])
        self.assertIn("reached a repository outside the copy", run["result"])
        self.assertIn("nothing was brought back", run["result"])
        self.assertEqual(status(self.wt), [], "nothing came back")
        self.assert_folder_gone()

    def test_a_separate_git_dir_layout_is_held(self):
        # a worktree made with --separate-git-dir: its .git file names a folder that is the whole repository
        # (no commondir). The boundary must hold that folder too, or a commit lands in it unhindered.
        sep_wt, gitdir = self.root / "sep-wt", self.root / "sep.git"
        git("init", "-q", "-b", "main", f"--separate-git-dir={gitdir}", str(sep_wt), cwd=self.root)
        (sep_wt / "keep.txt").write_text("one\n")
        git("add", "-A", cwd=sep_wt)
        git("commit", "-q", "-m", "files", cwd=sep_wt)

        def state():
            refs = subprocess.run(["git", "for-each-ref", "--format=%(refname) %(objectname)"], cwd=str(sep_wt),
                                  capture_output=True, text=True).stdout
            objects = sorted(str(p.relative_to(gitdir)) for p in (gitdir / "objects").rglob("*") if p.is_file())
            return refs, objects
        before = state()
        run, _ = self.run_cursor(f"rm -rf .git && printf 'gitdir: {gitdir}\\n' > .git && printf 'x\\n' >> keep.txt"
                                 f" && {GIT} add -A && {GIT} commit -q -m escape", cwd=sep_wt)
        self.assertTrue(self.cursor_record.exists(), f"Cursor ran: {run['result']}")
        self.assertEqual(state(), before, f"the repository is untouched: {run['result']}")

    def test_the_hold_puts_back_every_mode(self):
        # not just the top folder's: every file's and subfolder's mode in the worktree's git folders, as it was
        def modes():
            out = {}
            for folder, dirs, files in os.walk(self.root / "repo" / ".git"):
                for p in (folder, *(os.path.join(folder, n) for n in files)):
                    out[p] = os.lstat(p).st_mode
            return out
        before = modes()
        run, _ = self.run_cursor("printf 'x\\n' > x.txt")
        self.assertEqual(run["subtype"], "success", run["result"])
        self.assertEqual(modes(), before, "every mode back as it was, nothing left behind")

    def test_a_restore_that_cannot_finish_fails_the_run_and_keeps_its_record(self):
        # the tests' fault injection: the objects folder's own mode refuses to be put back. The run must not
        # be a success, and the lock must stay with what is left to restore, for a later run to finish.
        common = self.root / "repo" / ".git"
        want = os.stat(common / "objects").st_mode & 0o7777  # as git made it, whatever the umask
        run, code = self.run_cursor("printf 'x\\n' > x.txt", HANSA_CURSOR_TEST_FAIL_CHMOD=f"{os.sep}objects")
        self.assertEqual((code, run["subtype"], run["is_error"]), (1, "error_during_execution", True), run)
        self.assertIn("the copy's git boundary could not be put back", run["result"])
        self.assertIn("still read-only", run["result"])
        self.assertEqual(status(self.wt), ["?? x.txt"], "the changes came back; the failure is after that")
        lock = common / "hansa-cursor-hold"
        self.assertTrue(lock.exists(), "the record is kept")
        left = json.loads((lock / "modes").read_text())
        self.assertEqual([[os.path.realpath(common / "objects"), want]], left, "only what is left to restore")
        self.assertEqual(os.stat(common / "objects").st_mode & 0o222, 0, "still read-only")
        self.assert_folder_gone()


class CursorGitHold(CursorScratch):
    """The hold is shared by the runs on one repository: a live run's established hold is joined, not
    refused; a lock half set up (a maker died) is waited out while its maker may be alive, then repaired;
    what cannot be proven alive or dead refuses the run; and a release touches the run's own share alone."""

    def make_lock(self, owner=None):
        lock = self.root / "repo" / ".git" / "hansa-cursor-hold"
        lock.mkdir()
        if owner:
            (lock / "owner").write_text(owner)
        return lock

    def test_a_live_runs_hold_is_joined_not_refused(self):
        # several runs on one repository go at once: a run that finds this process's established hold adds
        # its own owner file and runs, and the boundary is put back when no owner is left
        import hansa_cursor
        common = self.root / "repo" / ".git"
        mine = self.root / "mine-live"
        mine.mkdir()
        hansa_cursor.hold_boundary(str(mine), str(self.wt))  # this process's share: alive for the run's length
        lock = common / "hansa-cursor-hold"
        owners = {p.name for p in lock.glob("owner-*")}
        run, _ = self.run_cursor()
        self.assertEqual(run["subtype"], "success", run["result"])
        self.assertEqual({p.name for p in lock.glob("owner-*")}, owners,
                         "the run's share given back, this process's untouched")
        self.assertEqual(os.stat(common).st_mode & 0o222, 0, "still held: an owner is left")
        self.assertEqual(hansa_cursor.release_boundary(str(mine)), "")
        self.assertFalse(lock.exists(), "the last release put the boundary back and removed the lock")
        self.assertTrue(os.stat(common).st_mode & 0o222, "the modes back")

    def test_a_lock_without_its_owner_is_waited_out_then_repaired(self):
        # a maker killed (or still writing) in the first instant: not repaired at once — the run waits the
        # lock's first seconds out, and only then repairs it and goes
        self.make_lock()
        began = time.time()
        run, _ = self.run_cursor(HANSA_CURSOR_TEST_MUTEX_WAIT="10")
        self.assertGreaterEqual(time.time() - began, 4, "the lock's first seconds waited out")
        self.assertEqual(run["subtype"], "success", run["result"])
        self.assertFalse((self.root / "repo" / ".git" / "hansa-cursor-hold").exists(),
                         "repaired, held and put back: nothing of the lock left")

    def test_a_half_set_up_hold_with_a_live_maker_refuses_after_the_wait(self):
        # a maker that never finishes (still alive, no ready marker): the run waits for the setup, then is
        # refused, never repaired from under a live maker
        import hansa_cursor
        start = shim.process_table().get(os.getpid(), (0, ""))[1]
        self.make_lock(f"{os.getpid()} {start}\n")
        mine = self.root / "mine"
        mine.mkdir()
        with unittest.mock.patch.object(hansa_cursor, "MUTEX_WAIT", 1):
            with self.assertRaises(hansa_cursor.Problem) as e:
                hansa_cursor.hold_boundary(str(mine), str(self.wt))
        self.assertIn("another Cursor run holds this repository's git boundary", str(e.exception))
        shutil.rmtree(self.root / "mine", ignore_errors=True)

    def test_a_dead_runs_lock_is_taken_over(self):
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        start = shim.process_table().get(proc.pid, (0, ""))[1]
        proc.wait(10)
        lock = self.make_lock(f"{proc.pid} {start}\n" if start else f"{1 << 30} 1.000000\n")
        run, _ = self.run_cursor()
        self.assertEqual(run["subtype"], "success", run["result"])
        self.assertFalse(lock.exists(), "the stale lock went with the run it would have blocked")

    def test_a_holder_that_cannot_be_checked_refuses_the_run(self):
        # the process table unreadable (the tests' HANSA_CURSOR_TEST_NO_TABLE, in the shim's own process, as
        # HANSA_CURSOR_TEST_FAIL_CHMOD does a chmod): a live holder would look dead to a repair — the run is
        # refused after the wait, the lock never repaired
        start = shim.process_table().get(os.getpid(), (0, ""))[1]
        lock = self.make_lock(f"{os.getpid()} {start}\n")
        why = self.refused(HANSA_CURSOR_TEST_NO_TABLE="1", HANSA_CURSOR_TEST_MUTEX_WAIT="2")
        self.assertIn("another Cursor run holds this repository's git boundary", why)
        self.assertTrue(lock.exists(), "never repaired: the lock is still there")
        shutil.rmtree(lock, ignore_errors=True)

    def test_a_late_cleanup_of_an_old_run_cannot_release_a_new_runs_hold(self):
        # an old run's end or watchdog fires after a new run holds the same repository: its note names the
        # same lock path but not the new run's instance of it, so nothing of the new run's protection goes
        import hansa_cursor
        common = self.root / "repo" / ".git"
        new = self.root / "mine-new"
        new.mkdir()
        hansa_cursor.hold_boundary(str(new), str(self.wt))
        lock = common / "hansa-cursor-hold"
        self.assertTrue(lock.exists())
        self.assertEqual(os.stat(common).st_mode & 0o222, 0, "held read-only")
        for n, note in enumerate((f"{lock}\n", f"{lock}\nnot-the-runs-token\n")):
            with self.subTest(note=n):
                old = self.root / f"mine-old-{n}"
                old.mkdir()
                (old / hansa_cursor.GIT_BOUNDARY).write_text(note)
                hansa_cursor.release_boundary(str(old))
                self.assertTrue(lock.exists(), "the new run's lock untouched")
                self.assertEqual(os.stat(common).st_mode & 0o222, 0, "still held read-only")
                self.assertTrue((lock / "modes").exists(), "its record intact")
        self.assertEqual(hansa_cursor.release_boundary(str(new)), "")
        self.assertFalse(lock.exists())
        self.assertTrue(os.stat(common).st_mode & 0o222, "the run's own release puts it back")

    def test_two_runs_sharing_one_hold_end_with_the_boundary_back(self):
        # a dead run's established hold (its maker gone, the modes stripped for real): two runs taking it at
        # once both hold the boundary — each its own owner file — and the last release puts the modes back,
        # exactly once
        import threading
        import hansa_cursor
        common = self.root / "repo" / ".git"
        lock = common / "hansa-cursor-hold"
        dead = self.root / "mine-dead"
        dead.mkdir()
        code = ("import sys; sys.path.insert(0, sys.argv[1]); import hansa_cursor; "
                "hansa_cursor.hold_boundary(sys.argv[2], sys.argv[3])")
        shim_dir = str(Path(__file__).resolve().parent.parent / "shim")
        for round_ in range(3):
            subprocess.run([sys.executable, "-c", code, shim_dir, str(dead), str(self.wt)], check=True)
            self.assertTrue((lock / "ready").exists(), "a hold whose maker is now dead")
            results = {}

            def hold(name):
                mine = self.root / f"mine-{name}"
                mine.mkdir(exist_ok=True)
                try:
                    hansa_cursor.hold_boundary(str(mine), str(self.wt))
                    results[name] = "held"
                except Exception as e:
                    results[name] = f"refused ({str(e).splitlines()[0][:60]})"
            threads = [threading.Thread(target=hold, args=(n,)) for n in ("a", "b")]
            for t in threads:
                t.start()
            for t in threads:
                t.join(30)
            self.assertEqual(sorted(results.values()), ["held", "held"], f"round {round_}: {results}")
            self.assertEqual(len(list(lock.glob("owner-*"))), 3,
                             "the dead maker's and both runs' shares of the one hold")
            self.assertEqual(os.stat(common).st_mode & 0o222, 0, "held read-only")
            for n in ("a", "b"):
                self.assertEqual(hansa_cursor.release_boundary(str(self.root / f"mine-{n}")), "")
                shutil.rmtree(self.root / f"mine-{n}", ignore_errors=True)
            self.assertFalse(lock.exists(), "the last release put the boundary back and removed the lock")
            self.assertTrue(os.stat(common).st_mode & 0o222, "the modes back")
        shutil.rmtree(dead, ignore_errors=True)

    def test_a_failed_restore_keeps_its_record_and_the_next_release_finishes(self):
        # a chmod that refuses mid-release: the failure is said, the lock stays with only what is left to
        # restore, and a later release (the next run's, say) finishes it
        import hansa_cursor
        mine = self.root / "mine"
        mine.mkdir()
        common = self.root / "repo" / ".git"
        want = os.stat(common / "objects").st_mode & 0o7777  # as git made it, whatever the umask
        hansa_cursor.hold_boundary(str(mine), str(self.wt))
        lock = common / "hansa-cursor-hold"
        real = hansa_cursor.restore_chmod

        def refusing(path, mode):
            if str(path).endswith(f"{os.sep}objects"):
                raise PermissionError(f"injected failure: {path} (the tests)")
            return real(path, mode)
        with unittest.mock.patch.object(hansa_cursor, "restore_chmod", refusing):
            why = hansa_cursor.release_boundary(str(mine))
        self.assertIn("could not be put back", why)
        self.assertIn("still read-only", why)
        self.assertTrue(lock.exists(), "the record is kept")
        self.assertEqual(os.stat(common / "objects").st_mode & 0o222, 0, "still read-only")
        left = json.loads((lock / "modes").read_text())
        self.assertEqual([[os.path.realpath(common / "objects"), want]], left, "only what is left to restore")
        self.assertEqual(hansa_cursor.release_boundary(str(mine)), "", "the retry finishes it")
        self.assertFalse(lock.exists())
        self.assertTrue(os.stat(common / "objects").st_mode & 0o222)


class CursorAtOnce(CursorScratch):
    """Several Cursor runs at once on one repository: each finishes with its own result, in its own copy of
    the worktree, its own temporary home and its own processes; none waits for another, and the patch each
    sends back is its own. The runs share the copy's git boundary (README, "The copy's git boundary")."""

    def test_three_runs_at_once(self):
        # a rendezvous: each run's commands mark it ready and wait for the other two; were the runs
        # serialized, no run would ever see three marks
        marks = self.root / "at-once"
        marks.mkdir()
        repo = self.root / "repo"
        refs0 = subprocess.run(["git", "for-each-ref", "--format=%(refname) %(objectname)"], cwd=str(repo),
                               check=True, capture_output=True, text=True).stdout
        objects0 = sorted(str(p.relative_to(repo)) for p in (repo / ".git" / "objects").rglob("*") if p.is_file())
        runs = []
        for n in range(3):
            script = (f"printf x > '{marks}/ready-{n}'; i=0; "
                      f"while [ $i -lt 300 ] && [ $(ls '{marks}'/ready-* 2>/dev/null | wc -l | tr -d ' ') -lt 3 ]"
                      f"; do i=$((i+1)); sleep 0.1; done; "
                      f"ls '{marks}'/ready-* 2>/dev/null | wc -l | tr -d ' ' > '{marks}/seen-{n}'; "
                      f"printf 'from run {n}\\n' > made-by-{n}.txt")
            argv = self.worker_argv()
            sid = f"{self.sid}-{n}"
            argv[argv.index("--session-id") + 1] = sid
            env = self.cursor_env(script, FAKE_CURSOR_RECORD=str(self.root / f"record-{n}.json"))
            runs.append((n, sid, argv, env))
        procs = [(n, sid, self.start(argv, env)) for n, sid, argv, env in runs]
        answers = {}
        for n, sid, p in procs:
            out, err = p.communicate(timeout=150)
            self.assertTrue(out.strip(), f"run {n}: no answer; stderr: {err.decode(errors='replace')}")
            answers[n] = (sid, json.loads(out))
        copies = set()
        for n, (sid, run) in answers.items():
            self.assertEqual((run["type"], run["subtype"], run["is_error"]), ("result", "success", False),
                             f"run {n}: {run['result']}")
            self.assertEqual(run["session_id"], sid, f"run {n}'s own result")
            self.assertEqual((marks / f"seen-{n}").read_text().strip(), "3",
                             f"run {n} ran while the other two ran: none waited for another")
            rec = json.loads((self.root / f"record-{n}.json").read_text())
            self.assertEqual(rec["script_exit"], 0)
            self.assertEqual(os.path.basename(rec["cwd"]), "ws")
            copies.add(rec["cwd"])
            patch = (self.logs / f"{sid}.patch").read_text()
            self.assertIn(f"made-by-{n}.txt", patch, f"run {n}'s patch is its own")
            for m in (0, 1, 2):
                if m != n:
                    self.assertNotIn(f"made-by-{m}.txt", patch, f"nothing of run {m}'s in run {n}'s patch")
        self.assertEqual(len(copies), 3, "each run in its own copy of the worktree, its own temporary home")
        for n in (0, 1, 2):
            self.assertEqual((self.wt / f"made-by-{n}.txt").read_text(), f"from run {n}\n")
        self.assertEqual(status(self.wt), ["?? made-by-0.txt", "?? made-by-1.txt", "?? made-by-2.txt"])
        refs1 = subprocess.run(["git", "for-each-ref", "--format=%(refname) %(objectname)"], cwd=str(repo),
                               check=True, capture_output=True, text=True).stdout
        objects1 = sorted(str(p.relative_to(repo)) for p in (repo / ".git" / "objects").rglob("*") if p.is_file())
        self.assertEqual((refs1, objects1), (refs0, objects0), "the main repository untouched")
        common = repo / ".git"
        self.assertFalse((common / "hansa-cursor-hold").exists(), "the last run put the boundary back")
        self.assertTrue(os.stat(common).st_mode & 0o222, "the modes back")
        self.assert_folder_gone()


class CursorGitDirs(CursorScratch):
    """The boundary's git folders, in each layout: a plain checkout, a linked worktree (a .git file whose
    folder holds commondir) and a --separate-git-dir layout (a .git file whose folder is the whole
    repository, no commondir). The last was once seen as no boundary at all."""

    def dirs(self, wt):
        import hansa_cursor
        return hansa_cursor.boundary_folders(str(wt))

    def test_a_linked_worktree(self):
        self.assertEqual(self.dirs(self.wt),
                         [os.path.realpath(self.root / "repo" / ".git" / "worktrees" / "wt"),
                          os.path.realpath(self.root / "repo" / ".git")])

    def test_a_main_checkout(self):
        self.assertEqual(self.dirs(self.root / "repo"), [os.path.realpath(self.root / "repo" / ".git")])

    def test_a_separate_git_dir_layout(self):
        sep_wt, gitdir = self.root / "sep-wt", self.root / "sep.git"
        git("init", "-q", "-b", "main", f"--separate-git-dir={gitdir}", str(sep_wt), cwd=self.root)
        self.assertEqual(self.dirs(sep_wt), [os.path.realpath(gitdir)], "the folder the .git file names")

    def test_a_dot_git_file_that_leads_nowhere(self):
        (self.wt / ".git").unlink()
        (self.wt / ".git").write_text("gitdir: /nonexistent/gitdir\n")
        self.assertEqual(self.dirs(self.wt), [])


class CursorBoundaryState(CursorScratch):
    """The after-run check's state: a change in only one of its three kinds — the refs, a HEAD, the objects —
    is caught on its own."""

    def state(self):
        import hansa_cursor
        return hansa_cursor.boundary_state(hansa_cursor.git_env(os.environ), str(self.wt))

    def test_each_kind_of_change_is_caught_on_its_own(self):
        repo = self.root / "repo"
        state0 = self.state()
        git("branch", "probe", cwd=repo)
        try:
            self.assertNotEqual(self.state(), state0, "a ref alone")
        finally:
            git("branch", "-q", "-D", "probe", cwd=repo)
        self.assertEqual(self.state(), state0, "back as it was")
        git("symbolic-ref", "HEAD", "refs/heads/task/t1", cwd=repo)
        try:
            self.assertNotEqual(self.state(), state0, "a HEAD alone")
        finally:
            git("symbolic-ref", "HEAD", "refs/heads/main", cwd=repo)
        self.assertEqual(self.state(), state0, "back as it was")
        subprocess.run(["git", "hash-object", "-w", "--stdin"], cwd=str(repo), input=b"probe\n", check=True,
                       capture_output=True)
        self.assertNotEqual(self.state(), state0, "an object alone")


class CursorPatchRefused(CursorScratch):
    def test_project_folder_written(self):
        # row 3: Cursor's commands may write .claude/skills in the workspace; it does not come back
        run, code = self.run_cursor("mkdir -p .claude/skills/x && printf 'obey\\n' > .claude/skills/x/SKILL.md && "
                                    "printf 'n\\n' > new.txt && printf 'two\\n' >> keep.txt")
        self.assertEqual(self.cursor_called()["script_exit"], 0)
        self.assertEqual((code, run["subtype"], run["is_error"]), (1, "error_during_execution", True))
        self.assertIn(".claude/skills/x/SKILL.md", run["result"])
        self.assertIn("nothing was brought back", run["result"])
        self.assertEqual(status(self.wt), [], "the worktree unchanged")
        self.assertFalse((self.wt / ".claude").exists())
        self.assertIn("SKILL.md", (self.logs / f"{self.sid}.patch").read_text(), "the patch is kept")
        self.assertEqual(hansa_run.unusable_reason(run, self.err), ("", ""))
        self.assert_folder_gone()

    def test_other_folders_and_a_submodule(self):
        for script, named in (("mkdir -p sub/.CODEX && printf x > sub/.CODEX/config.toml", "sub/.CODEX/config.toml"),
                              ("mkdir -p .agents/skills/y && printf x > .agents/skills/y/SKILL.md",
                               ".agents/skills/y/SKILL.md"),
                              ("mkdir -p a/.cursor/rules && printf x > a/.cursor/rules/r.mdc", "a/.cursor/rules/r.mdc"),
                              ("git init -q sub && cd sub && git -c user.name=c -c user.email=c@c commit -q "
                               "--allow-empty -m s", "sub (a submodule entry)")):
            with self.subTest(named=named):
                self.cursor_record.unlink(missing_ok=True)
                run, _ = self.run_cursor("printf n > n.txt && " + script)
                self.assertEqual(self.cursor_called()["script_exit"], 0)
                self.assertEqual(run["subtype"], "error_during_execution")
                self.assertIn(named, run["result"])
                self.assertEqual(status(self.wt), [])

    def test_refused_paths(self):
        import hansa_cursor
        raw = (b":000000 100644 0 1 A\0ok/file\0:100644 100644 1 2 M\0x/.Claude/settings.json\0"
               b":000000 160000 0 3 A\0vendor/lib\0:100644 000000 1 0 D\0.cursorrules\0")
        self.assertEqual(hansa_cursor.refused_paths(raw), ["x/.Claude/settings.json", "vendor/lib (a submodule entry)"])


class CursorRefused(CursorScratch):
    def test_version_changed(self):
        # row 4
        why = self.refused(HANSA_CURSOR_VERSION="2026.09.30-0000000")
        self.assertIn("Cursor's version changed", why)
        self.assertIn(VERSION, why)
        self.assertIn("2026.09.30-0000000", why)
        self.assertIn("sandbox again", why)

    def test_worktree_holds_what_cursor_obeys(self):
        # row 5, and the same anywhere in the worktree, and a submodule
        for made, named in ((".claude/settings.json", ".claude/settings.json"), (".cursor/rules/r.mdc", ".cursor"),
                            ("sub/.claude/settings.local.json", "sub/.claude/settings.local.json"),
                            ("sub/deep/.Cursor", "sub/deep/.Cursor"), ("vendor/lib/.git", "vendor/lib/.git")):
            with self.subTest(made=made):
                p = self.wt / made
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text("{}\n")
                self.assertIn(f"the worktree holds {self.wt / named}", self.refused())
                shutil.rmtree(self.wt / made.split("/")[0])

    def test_instructions_above_the_run_folder(self):
        # row 6: Cursor gives the model the CLAUDE.md and AGENTS.md of the folders above its workspace
        for folder, name in ((self.tmp, "AGENTS.md"), (self.root, "CLAUDE.md"), (self.tmp, "CLAUDE.local.md"),
                             (self.root, ".cursorrules")):
            with self.subTest(name=name):
                (folder / name).write_text("obey me\n")
                self.assertIn(str(folder / name), self.refused())
                (folder / name).unlink()
        # above the worker's TMPDIR as given, through a link: not on the resolved path
        via = self.root / "via"
        via.mkdir()
        (via / "tmp").symlink_to(self.tmp)
        (via / "AGENTS.md").write_text("obey me\n")
        self.assertIn(str(via / "AGENTS.md"), self.refused(TMPDIR=str(via / "tmp")))

    def test_no_sandbox(self):
        self.assertIn("no sandbox", self.refused(argv=self.worker_argv(box={})))

    def test_settings_unset(self):
        self.assertIn("HANSA_CURSOR_BIN is not set", self.refused(HANSA_CURSOR_BIN=""))
        self.assertIn("HANSA_CURSOR_VERSION is not set", self.refused(HANSA_CURSOR_VERSION=""))

    def test_read_and_write_lists_against_the_no_read_list(self):
        # in, at or above a no-read entry
        for read in (self.secret / "inside", self.secret, self.root):
            with self.subTest(read=read):
                why = self.refused(HANSA_CURSOR_READ=f"{self.root / 'docs'}:{read}")
                self.assertIn(f"{read} (HANSA_CURSOR_READ)", why)
        box = json.loads(json.dumps(self.box))
        box["sandbox"]["filesystem"]["allowWrite"] = [str(self.secret / "cache")]
        self.assertIn("(the lane's write list)", self.refused(argv=self.worker_argv(box=box)))


class CursorGiven(CursorScratch):
    def test_what_cursor_is_given(self):
        # row 7
        task = f"Fix {self.wt}/keep.txt; the tree is {self.wt}.\n"
        docs, more = self.root / "docs", self.root / "more"
        run, _ = self.run_cursor("printf '%s' \"$FROM_LANE_ENV\" > lane.txt", task=task,
                                 HOME=str(self.root / "elsewhere"), HANSA_CURSOR_READ=f"{docs}:{more}")
        self.assertEqual(run["subtype"], "success", run["result"])
        rec = self.cursor_called()
        argv, ws = rec["argv"], rec["cwd"]
        mine = os.path.dirname(ws)
        self.assertEqual(os.path.basename(ws), "ws")
        self.assertTrue(mine.startswith(str(self.tmp) + "/cursor-"), mine)
        self.assertEqual(argv[:-1], ["-p", "--output-format", "stream-json", "--sandbox", "enabled", "--trust",
                                     "--workspace", ws, "--model", "grok-4.7-high"])
        for flag in ("--force", "--yolo", "--approve-mcps", "disabled"):
            self.assertNotIn(flag, argv[:-1])
        text = argv[-1]
        system = "You are a worker on task t1.\nSay \"done\" when done; C:\\path stays."
        self.assertIn(f"<instructions>\n{system}\n</instructions>", text)
        self.assertIn(f"<task>\nFix {ws}/keep.txt; the tree is {ws}.\n\n</task>", text)
        self.assertLess(text.index("<instructions>"), text.index("<task>"))
        self.assertNotIn(str(self.wt), text)
        env = rec["env"]
        self.assertEqual((env["HOME"], env["CURSOR_CONFIG_DIR"], env["TMPDIR"]),
                         (f"{mine}/home", f"{mine}/cfg", f"{mine}/tmp"))
        self.assertEqual(env["FROM_LANE_ENV"], "lane value")
        for k in ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "HANSA_CURSOR_BIN", "HANSA_CURSOR_VERSION",
                  "CLAUDE_ENV_FILE", "CLAUDE_CONFIG_DIR"):
            self.assertIsNone(env[k], k)
        self.assertEqual(env["HANSA_TASK"], "t1")
        self.assertEqual((self.wt / "lane.txt").read_text(), "lane value", "the lane's env reached its commands")
        self.assertEqual(rec["home"], [".cursor", ".cursor/sandbox.json", "Library", "Library/Keychains"])
        self.assertEqual(rec["keychains"], os.path.join(pwd.getpwuid(os.getuid()).pw_dir, "Library", "Keychains"))
        sandbox = {"type": "workspace_readwrite", "readBoundary": "workspace",
                   "additionalReadPaths": [str(docs), str(more)],
                   "additionalReadwritePaths": [f"{mine}/tmp", *hansa_run.full_paths([self.cache])],
                   "disableTmpWrite": True,
                   "networkPolicy": {"version": 1, "default": "deny", "allow": ["pypi.org"],
                                     "deny": ["127.0.0.0/8", "::1/128", "localhost"]}}
        self.assertEqual(rec["sandbox"], sandbox)
        self.assertEqual(rec["cli_config"], CLI_CONFIG)
        self.assertEqual(json.loads((self.logs / f"{self.sid}.sandbox.json").read_text()), sandbox)
        self.assertEqual(json.loads((self.logs / f"{self.sid}.cli-config.json").read_text()), CLI_CONFIG,
                         "as given: Cursor's own keys are not in the copy")
        self.assert_folder_gone()

    def test_lane_write_list_less_what_cursor_may_never_write(self):
        box = json.loads(json.dumps(self.box))
        common = self.root / "repo" / ".git"
        box["sandbox"]["filesystem"]["allowWrite"] = [str(self.cache), str(self.wt), str(self.wt / "sub"),
                                                      str(common), str(common / "worktrees" / "wt"), str(self.tmp),
                                                      str(self.tmp / "x"), str(self.root)]
        run, _ = self.run_cursor(argv=self.worker_argv(box=box))
        self.assertEqual(run["subtype"], "success", run["result"])
        self.assertEqual(self.cursor_called()["sandbox"]["additionalReadwritePaths"][1:], [str(self.cache)])
        self.assertIn(f"left out of the lane's write list for this Cursor run, as above the worktree, its git "
                      f"folders or the worker's TMPDIR: {self.root}", self.err)

    def test_models(self):
        # row 8
        for effort, model in (("low", "grok-4.7-low"), ("medium", "grok-4.7-medium"), ("high", "grok-4.7-high"),
                              ("xhigh", "grok-4.7-xhigh"), ("max", "grok-4.7-xhigh"), ("", "grok-4.7-high")):
            with self.subTest(effort=effort):
                run, _ = self.run_cursor(argv=self.worker_argv(effort=effort))
                argv = self.cursor_called()["argv"]
                self.assertEqual(argv[argv.index("--model") + 1], model)
                self.assertEqual(list(run["modelUsage"]), [model])
        for effort in ("max", ""):
            with self.subTest(model="composer-2.5", effort=effort):
                run, _ = self.run_cursor(argv=self.worker_argv(effort=effort), HANSA_CURSOR_MODEL="composer-2.5")
                argv = self.cursor_called()["argv"]
                self.assertEqual(argv[argv.index("--model") + 1], "composer-2.5")
                self.assertEqual(list(run["modelUsage"]), ["composer-2.5"])

    def test_no_result_line(self):
        # row 9
        said = "Error: the connection to Cursor's servers was lost (ECONNRESET)"
        run, code = self.run_cursor(FAKE_CURSOR_NO_RESULT="1", FAKE_CURSOR_EXIT="1", FAKE_CURSOR_STDERR=said)
        self.assertEqual((code, run["type"], run["subtype"], run["is_error"]),
                         (1, "result", "error_during_execution", True))
        self.assertIn(said, run["result"])
        self.assertIn("exit 1", run["result"])
        self.assertEqual(run["session_id"], self.sid)
        self.assert_folder_gone()

    def test_result_success_but_exit_nonzero(self):
        run, code = self.run_cursor(FAKE_CURSOR_EXIT="2", FAKE_CURSOR_STDERR="late failure")
        self.assertEqual((code, run["subtype"]), (1, "error_during_execution"))
        self.assertIn("late failure", run["result"])


def other_case(p) -> str:
    """`p` with its last name in capitals: the same folder on a volume that matches names without regard to case."""
    return str(Path(p).parent / Path(p).name.upper())


class CursorErrors(CursorScratch):
    """Cursor's failures as hansa's own readings class Claude Code's (row 5)."""

    def failed(self, **extra):
        run, code = self.run_cursor(**extra)
        self.assertEqual((code, run["type"], run["subtype"], run["is_error"], run["session_id"]),
                         (1, "result", "error_during_execution", True, self.sid))
        return run

    def test_a_missing_login(self):
        for said in ("Error: Authentication required. Please run 'cursor-agent login' first.", "Not logged in"):
            with self.subTest(said=said):
                run = self.failed(FAKE_CURSOR_NO_RESULT="1", FAKE_CURSOR_EXIT="1", FAKE_CURSOR_STDERR=said)
                self.assertEqual(hansa_run.unusable_reason(run, self.err), ("login", "not logged in (Please run /login)"))
                self.assertIn(said, run["result"])

    def test_a_usage_limit_in_a_failed_result_line(self):
        run = self.failed(FAKE_CURSOR_FAILED="1", FAKE_CURSOR_EXIT="1",
                          FAKE_CURSOR_RESULT="Rate limit exceeded for grok-4.7-high. Try again in 2 hours.")
        self.assertEqual(hansa_run.unusable_reason(run, self.err)[0], "quota", run["result"])
        self.assertAlmostEqual(hansa_run.reset_time(f"{run['result']}\n{self.err}"), time.time() + 7200, delta=120)

    def test_a_dropped_connection(self):
        run = self.failed(FAKE_CURSOR_NO_RESULT="1", FAKE_CURSOR_EXIT="1",
                          FAKE_CURSOR_STDERR="Error: stream closed before the response completed")
        self.assertEqual(hansa_run.unusable_reason(run, self.err), ("", ""))
        self.assertEqual(hansa_run.network_reason(run, self.err),
                         "the connection to the provider failed (Connection dropped)")


class CursorCheck(CursorScratch):
    """hansa's check_provider itself on a desk whose `cursor` provider has the placeholder (row 6). hansa gives a
    check a clean environment, so the adapter's settings and the fake's reach the shim through provider_env."""

    def setUp(self):
        super().setUp()
        from hansa.cli import init_desk
        self.desk = init_desk(self.root / "desk")
        s = self.desk.settings()
        s["providers"]["cursor"] = {"kind": "compatible", "model": "cursor", "effort": "", "base_url": CURSOR_URL}
        self.assertEqual(self.desk.save_settings(s), [])
        self.desk.write_key("cursor", "placeholder-0001")

    def check(self, **extra):
        given_settings = {k: v for k, v in self.cursor_env(HANSA_CURSOR_READ=str(self.root / "docs"),
                                                           FAKE_CURSOR_TOOLS="0", **extra).items()
                          if k.startswith(("HANSA_CURSOR_", "FAKE_", "PYTHON"))}  # no tool calls: hansa's --max-turns 1
        given = hansa_run.provider_env
        with unittest.mock.patch.dict(os.environ, {"HANSA_CLAUDE": str(WRAPPER), "TMPDIR": str(self.tmp)}), \
                unittest.mock.patch.object(hansa_run, "provider_env", lambda *a: {**given(*a), **given_settings}):
            return hansa_run.check_provider(self.desk, "cursor", timeout=60)

    def test_a_check_that_answers_ok(self):
        r = self.check(FAKE_CURSOR_RESULT="ok")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["detail"], "grok-4.7-high", "the models of modelUsage")
        rec = self.cursor_called()
        ws = rec["cwd"]
        mine = os.path.dirname(ws)
        self.assertTrue(mine.startswith(str(self.tmp) + "/cursor-check-"), mine)
        self.assertEqual((rec["argv"][-1], rec["workspace"]), ("Reply with the single word: ok", []), "empty, no git")
        self.assertEqual(rec["argv"][rec["argv"].index("--workspace") + 1], ws)
        self.assertEqual(rec["home"], [".cursor", ".cursor/sandbox.json", "Library", "Library/Keychains"])
        self.assertEqual(rec["cli_config"], CLI_CONFIG)
        self.assertEqual(rec["sandbox"], {"type": "workspace_readwrite", "readBoundary": "workspace",
                                          "additionalReadPaths": [], "additionalReadwritePaths": [f"{mine}/tmp"],
                                          "disableTmpWrite": True,
                                          "networkPolicy": {"version": 1, "default": "deny", "allow": [],
                                                            "deny": ["127.0.0.0/8", "::1/128", "localhost"]}})
        self.assertIsNone(rec["env"]["FROM_LANE_ENV"], "no lane env")
        self.assertFalse(os.path.exists(mine), "the check's folder is removed")
        logs = sorted(p.name for p in self.logs.iterdir())
        self.assertTrue(logs and all(n.startswith("check-") for n in logs), logs)

    def test_a_check_not_logged_in(self):
        r = self.check(FAKE_CURSOR_NO_RESULT="1", FAKE_CURSOR_EXIT="1", FAKE_CURSOR_STDERR="Not logged in")
        self.assertFalse(r["ok"])
        self.assertEqual(r["detail"], "not logged in (Please run /login)")
        self.assertEqual(list(self.tmp.glob("cursor-check-*")), [])


class CursorPaths(CursorScratch):
    def test_prompt_names_the_runs_tmp(self):
        # row 7: hansa's own system prompt names the worker's TMPDIR (commands_tmp), which Cursor's commands
        # cannot write
        named = hansa_run.commands_tmp(str(self.tmp))
        argv = self.worker_argv()
        argv[argv.index("--append-system-prompt") + 1] = hansa_run.PROTOCOL.format(tid="t1", lane="k", tmp=named,
                                                                                   long="")
        run, _ = self.run_cursor(argv=argv, task=f"Put scratch files in {named}/x or {self.tmp}.\n")
        self.assertEqual(run["subtype"], "success", run["result"])
        rec = self.cursor_called()
        text, rtmp = rec["argv"][-1], rec["env"]["TMPDIR"]
        self.assertEqual(rtmp, os.path.join(os.path.dirname(rec["cwd"]), "tmp"))
        self.assertNotIn(named, text)
        self.assertIn(f"Your temporary folder, {rtmp}, is yours", text)
        self.assertIn(f"`sh {rtmp}/probe.sh`", text)
        self.assertIn(f"Put scratch files in {rtmp}/x or {rtmp}.", text)

    def test_tmpprefix_in_the_runs_tmp(self):
        # row 8: zsh's here-documents go under TMPPREFIX (/tmp/zsh by default, which sandbox.json denies), whatever
        # the shim's environment or the lane's env said
        self.lane_env.write_text("export FROM_LANE_ENV='lane value' TMPPREFIX=/tmp/zsh\n")
        run, _ = self.run_cursor(TMPPREFIX="/tmp/zsh")
        rec = self.cursor_called()
        self.assertEqual(os.path.dirname(rec["env"]["TMPPREFIX"]), os.path.join(os.path.dirname(rec["cwd"]), "tmp"))

    def test_write_entries_spelt_in_other_case(self):
        # row 10: the volume matches names without regard to case, so .../WT is the worktree
        box = json.loads(json.dumps(self.box))
        common = self.root / "repo" / ".git"
        box["sandbox"]["filesystem"]["allowWrite"] = [str(self.cache), other_case(self.wt), other_case(common),
                                                      other_case(self.tmp), str(self.wt / "SUB"), other_case(self.root)]
        run, _ = self.run_cursor(argv=self.worker_argv(box=box))
        self.assertEqual(run["subtype"], "success", run["result"])
        self.assertEqual(self.cursor_called()["sandbox"]["additionalReadwritePaths"][1:], [str(self.cache)])
        self.assertIn(f"as above the worktree, its git folders or the worker's TMPDIR: {other_case(self.root)}",
                      self.err)

    def test_read_entries_that_would_open_the_run(self):
        # row 11: the worker's TMPDIR holds the run's snap/ and cfg/; the worktree and the main repository likewise
        docs = self.root / "docs"
        left = [str(self.tmp), other_case(self.tmp), str(self.wt / "sub"), str(self.root / "repo"),
                str(self.root / "repo" / ".git" / "worktrees")]
        run, _ = self.run_cursor(HANSA_CURSOR_READ=":".join([str(docs), *left]))
        self.assertEqual(run["subtype"], "success", run["result"])
        self.assertEqual(self.cursor_called()["sandbox"]["additionalReadPaths"], [str(docs)])
        self.assertIn(f"left out of HANSA_CURSOR_READ for this Cursor run, as the worktree, its git folders or the "
                      f"worker's TMPDIR, or inside or above one: {', '.join(left)}", self.err)

    def test_what_the_run_made_unwritable_or_unreadable(self):
        # row 12: the run's folder is removed all the same; what git cannot read is an error, never left out
        self.addCleanup(subprocess.run, ["chmod", "-R", "u+rwX", str(self.tmp)])
        for script, named in (("mkdir ro hid && printf r > ro/r && printf h > hid/h && chmod a-w ro && "
                               "chmod 000 hid/h", "hid/h"),
                              ("mkdir ro hd && printf r > ro/r && printf x > hd/x && chmod a-w ro && chmod 000 hd",
                               "'hd/'")):
            with self.subTest(named=named):
                run, code = self.run_cursor(script)
                self.assertEqual(self.cursor_called()["script_exit"], 0)
                self.assertEqual((code, run["subtype"], run["is_error"]), (1, "error_during_execution", True))
                self.assertIn(named, run["result"])
                self.assertIn("nothing was brought back", run["result"])
                self.assertEqual(status(self.wt), [])
                self.assert_folder_gone()


class CursorCalls(unittest.TestCase):
    def call(self, argv, **extra):
        env = {"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "/"), "ANTHROPIC_BASE_URL": CURSOR_URL,
               "HANSA_CURSOR_ENABLE": "on", "HANSA_REAL_CLAUDE": "/nonexistent", **extra}
        p = subprocess.run([str(WRAPPER), *argv], env=env, input=b"Reply with the single word OK.",
                           capture_output=True, timeout=60)
        return json.loads(p.stdout), p.returncode

    def test_resume(self):
        run, code = self.call(["-p", "--output-format", "json", "--resume", "sid-9", "--max-turns", "3"])
        self.assertEqual((code, run["subtype"], run["is_error"], run["session_id"]),
                         (1, "error_during_execution", True, "sid-9"))
        self.assertIn("resume is not supported on the Cursor provider", run["result"])

    def test_provider_check_without_cursor(self):
        logs = Path(tempfile.mkdtemp(prefix="cursor-logs-")).resolve()
        self.addCleanup(shutil.rmtree, logs, True)
        run, code = self.call(["-p", "--output-format", "json", "--max-turns", "1", "--model", "cursor", "--tools", ""],
                              HANSA_CURSOR_BIN=str(logs / "no-cursor-agent"), HANSA_CURSOR_VERSION=VERSION,
                              HANSA_CURSOR_LOGS=str(logs), TMPDIR=str(logs))
        self.assertEqual((code, run["subtype"], run["is_error"]), (1, "error_during_execution", True))
        self.assertIn("could not run", run["result"])
        self.assertEqual(list(logs.glob("cursor-check-*")), [], "the check's folder is removed")

    def test_call_kinds(self):
        cur = {"ANTHROPIC_BASE_URL": CURSOR_URL + "/", "HANSA_CURSOR_ENABLE": "on"}  # off: test_cursor_switch
        self.assertEqual(shim.call_kind(["-p", "--output-format", "json", "--session-id", "x"], cur), "cursor")
        self.assertEqual(shim.call_kind(["-p", "--output-format", "json"], cur), "cursor-check")
        self.assertEqual(shim.call_kind(["-p", "--output-format", "json", "--resume", "x"], cur), "cursor-resume")
        self.assertEqual(shim.call_kind(["--version"], cur), "pass")
        self.assertEqual(shim.call_kind(["-p", "--output-format", "text", "--session-id", "x"], cur), "pass")
