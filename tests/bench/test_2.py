"""Admission snapshots use local Git and files; no network or VM."""
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from hansa import run
from hansa.bench.channel import Channel, claim, submit
from hansa.desk import Desk


class BenchSnapshotTest(unittest.TestCase):
    def setUp(self):
        from hansa.bench import snapshot
        self.s = snapshot
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
        clean = patch.dict(os.environ, env, clear=True)
        clean.start()
        self.addCleanup(clean.stop)
        self.repo, self.wt = self.root / "repo", self.root / "tree"
        self.git("init", "-q", str(self.repo))
        (self.repo / "tracked").write_text("committed")
        self.git("-C", str(self.repo), "add", ".")
        self.git("-C", str(self.repo), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                 "commit", "-qm", "initial")
        self.git("-C", str(self.repo), "worktree", "add", "-q", "--detach", str(self.wt))
        self.desk = Desk(self.root / "desk")
        self.desk.state_dir.mkdir(parents=True)
        run._remember_git_file(self.desk, self.wt, (self.wt / ".git").read_text().strip())
        self.queue = Channel(self.root, "recorded-run", self.wt)
        self.addCleanup(self.queue.close)
        denied = patch.object(run, "worker_read_denials", return_value=[str(self.wt / "blocked")])
        denied.start()
        self.addCleanup(denied.stop)

    def git(self, *args):
        subprocess.run(["git", *args], check=True, capture_output=True, timeout=15)

    def contents(self):
        with self.s.export(self.desk, self.queue) as stream, tarfile.open(fileobj=stream) as archive:
            return {f.name: archive.extractfile(f).read() for f in archive}

    def test_changed_untracked_included(self):
        rid = submit(self.queue.directory, self.queue.epoch, "true")
        (self.wt / "tracked").write_text("at admission")
        (self.wt / " new script").write_text("echo new")
        (self.wt / " new script").chmod(0o755)
        (self.wt / ".gitignore").write_text("ignored\n")
        (self.wt / "ignored").write_text("cache")
        for name in ("blocked", ".env", ".env.local", "CLAUDE.md", "AGENTS.md", ".claude/settings.json",
                     ".ssh/key", ".codex/auth.json", ".cache/data"):
            p = self.wt / name
            p.parent.mkdir(exist_ok=True)
            p.write_text("private")
        claim(self.queue, rid)
        self.assertEqual(self.contents(), {"tracked": b"at admission", " new script": b"echo new",
                                          ".gitignore": b"ignored\n"})
        with self.s.export(self.desk, self.queue) as stream, tarfile.open(fileobj=stream) as archive:
            self.assertEqual(archive.getmember(" new script").mode, 0o755)
            self.assertTrue(all((f.uid, f.gid, f.uname, f.gname, f.mtime) == (0, 0, "", "", 0) for f in archive))
        (self.wt / "tracked").unlink()
        self.assertNotIn("tracked", self.contents())

    def test_secret_link_and_mutation_refused(self):
        secret = self.root / "secret"
        secret.write_text("private")
        target = self.wt / "tracked"
        for kind in ("symlink", "hardlink", "fifo", "directory-link"):
            target.unlink()
            if kind == "symlink":
                target.symlink_to(secret)
            elif kind == "hardlink":
                os.link(secret, target)
            elif kind == "fifo":
                os.mkfifo(target)
            else:
                target.symlink_to(self.root, target_is_directory=True)
            with self.subTest(kind=kind), self.assertRaises((OSError, ValueError)):
                self.contents()
            target.unlink()
            target.write_text("committed")
        original = tarfile.TarFile.addfile
        for change in ("write", "replace", "new"):
            def racing(archive, info, fileobj=None):
                original(archive, info, fileobj)
                p = self.wt / ("new" if change == "new" else "tracked")
                if change == "replace":
                    p.unlink()
                p.write_text("mutation!")
            with self.subTest(change=change), patch.object(tarfile.TarFile, "addfile", racing), \
                    self.assertRaisesRegex(ValueError, "changed"):
                self.contents()

    def test_tracked_file_under_directory_link_refused(self):
        folder = self.wt / "inner"
        folder.mkdir()
        tracked = folder / "leak.txt"
        tracked.write_text("committed inside the worktree")
        self.git("-C", str(self.wt), "add", "inner/leak.txt")
        self.git("-C", str(self.wt), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                 "commit", "-qm", "nested tracked file")

        # Ignore the replacement link itself so the final-file guard cannot mask
        # a missing directory guard. The tracked child remains in Git's list.
        (self.wt / ".gitignore").write_text("/inner\n")
        outside = self.root / "outside"
        outside.mkdir()
        secret = b"outside-directory-secret-must-not-enter-the-snapshot"
        (outside / "leak.txt").write_bytes(secret)
        tracked.unlink()
        folder.rmdir()
        folder.symlink_to(outside, target_is_directory=True)

        # Git still lists inner/leak.txt; its parent now leads outside the worktree.
        # Refusing the export or omitting the path are both safe outcomes.
        try:
            stream = self.s.export(self.desk, self.queue)
        except (OSError, ValueError):
            return

        with stream:
            data = stream.read()
            self.assertFalse(secret in data, "outside file contents entered the snapshot")
            stream.seek(0)
            with tarfile.open(fileobj=stream) as archive:
                self.assertNotIn("inner/leak.txt", archive.getnames())

    def test_nested_paths_and_partial_cleanup(self):
        folder = self.wt / "nested"
        folder.mkdir()
        name = "nested/" + "é" * 80
        (self.wt / name).write_bytes(b"\0binary")
        self.git("-C", str(self.wt), "add", "nested")
        self.assertEqual(self.contents()[name], b"\0binary")
        original = tarfile.TarFile.addfile
        def swapped(archive, info, fileobj=None):
            original(archive, info, fileobj)
            if info.name == name:
                folder.rename(self.root / "moved")
                folder.symlink_to(self.root / "moved", target_is_directory=True)
        with tempfile.TemporaryFile() as partial, patch.object(self.s.tempfile, "TemporaryFile", return_value=partial), \
                patch.object(tarfile.TarFile, "addfile", swapped):
            with self.assertRaises((OSError, ValueError)):
                self.contents()
            self.assertTrue(partial.closed)
        with self.assertRaises((OSError, ValueError)):
            self.contents()

    def test_bounds_and_guard(self):
        for limit, value in (("MAX_ENTRIES", 0), ("MAX_BYTES", 4), ("MAX_ARCHIVE", 10239)):
            with self.subTest(limit=limit), patch.object(self.s, limit, value), \
                    self.assertRaisesRegex(ValueError, "limit"):
                self.contents()
        for name in ("../escape", "/absolute", "a/../b", "a\\b", "a\nname", "x" * 4097):
            with self.subTest(name=name[:20]), patch.object(run, "wgit", return_value="H " + name + "\0"), \
                    self.assertRaisesRegex(ValueError, "path"):
                self.contents()
        for output in ("H tracked", "H a\0H b\0"):
            with patch.object(run, "wgit", return_value=output), patch.object(self.s, "MAX_ENTRIES", 1), \
                    self.assertRaisesRegex(ValueError, "paths|limit"):
                self.contents()
        (self.wt / ".git").write_text("gitdir: missing\n")
        with self.assertRaises(run.GitFileChanged):
            self.contents()
