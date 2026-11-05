"""Queue tests use local files only; no VM, network or worker lifecycle."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from hansa import cli
from hansa.bench import channel as c


class BenchChannelTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.tmp, self.wt = self.root / "worker", self.root / "tree"
        self.tmp.mkdir()
        self.wt.mkdir()
        self.queue = self.register()

    def register(self, epoch=None):
        queue = c.Channel(self.tmp, "recorded-run", self.wt, epoch)
        self.addCleanup(queue.close)
        return queue

    def test_other_commands_need_no_channel(self):
        real_import = __import__
        for failure in (ModuleNotFoundError, SyntaxError):
            def importing(name, *args, **kwargs):
                if name.endswith("bench.channel"):
                    raise failure("unavailable channel")
                return real_import(name, *args, **kwargs)
            with self.subTest(failure=failure), patch("builtins.__import__", side_effect=importing), \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                with self.assertRaises(SystemExit) as ended:
                    cli.main(["--help"])
                self.assertEqual(ended.exception.code, 0)
                self.assertIn("usage: hansa", out.getvalue())
                with self.assertRaises(failure):
                    cli.main(["bench", "run", "--script", "unused"])

    def test_worker_needs_no_desk(self):
        script = self.root / "run.sh"
        script.write_text("echo bench\n")
        env = {"HANSA_BENCH_DIR": str(self.tmp), "HANSA_BENCH_EPOCH": self.queue.epoch}
        with patch.dict(os.environ, env, clear=True), patch.object(cli.Desk, "find", side_effect=AssertionError), \
                patch.object(cli, "_as_desk_account", side_effect=AssertionError), \
                patch.object(cli.notify, "session_here", side_effect=AssertionError), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.main(["--desk", str(self.root / "absent"), "bench", "run", "--script", str(script)]), 0)
            rid = out.getvalue().strip()
            request = c.claim(self.queue, rid)
            self.assertEqual((request["script"], request["cwd"], request["timeout"], request["wait"]),
                             ("echo bench\n", ".", 300, 60))
            self.assertNotIn("run", request)
            self.assertEqual(self.queue.run, "recorded-run")
            c.reply(self.queue, rid, {"status": "ok"})
            self.assertEqual(c._read(self.queue.directory, rid + ".reply")["result"], {"status": "ok"})
        with patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(SystemExit, "disabled"):
            cli.main(["bench", "run", "--script", str(script)])

    def test_link_swap_refused(self):
        rid = c.submit(self.queue.directory, self.queue.epoch, "true")
        path = self.tmp / (rid + ".request")
        saved = path.read_bytes()
        outside = self.root / "outside"
        outside.write_bytes(saved)
        for kind in ("symlink", "hardlink", "fifo"):
            path.unlink()
            if kind == "symlink":
                path.symlink_to(outside)
            elif kind == "hardlink":
                os.link(outside, path)
            else:
                os.mkfifo(path)
            with self.subTest(kind=kind), self.assertRaises((OSError, ValueError)):
                c.claim(self.queue, rid)
        path.unlink()
        path.write_bytes(saved)
        c.claim(self.queue, rid)
        (self.tmp / (rid + ".reply")).symlink_to(outside)
        with self.assertRaises(FileExistsError):
            c.reply(self.queue, rid, {"status": "ok"})
        self.assertEqual(outside.read_bytes(), saved)
        self.tmp.rename(self.root / "moved")
        self.tmp.symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "directory replaced"):
            c.reply(self.queue, rid, {})

    def test_epoch_never_replayed(self):
        rid = c.submit(self.queue.directory, self.queue.epoch, "true")
        c.claim(self.queue, rid)
        (self.tmp / (rid + ".claimed")).unlink()
        with self.assertRaises(ValueError):
            c.claim(self.queue, rid)
        (self.tmp / (rid + ".claimed")).touch()
        with self.assertRaises(FileExistsError):
            c.claim(self.register(self.queue.epoch), rid)
        pending = c.submit(self.queue.directory, self.queue.epoch, "pending")
        restarted = self.register()
        for stale in (rid, pending):
            with self.assertRaisesRegex(ValueError, "epoch"):
                c.claim(restarted, stale)
        fresh = c.submit(restarted.directory, restarted.epoch, "fresh")
        self.assertEqual(c.claim(restarted, fresh)["script"], "fresh")

    def test_bounded_strict_requests(self):
        for options in ({"script": "x" * c.LIMIT}, {"cwd": "../escape"}, {"cwd": "/absolute"},
                        {"timeout": 901}, {"timeout": True}, {"wait": 301}):
            with self.subTest(options=list(options)), self.assertRaises(ValueError):
                c.submit(self.queue.directory, self.queue.epoch, **{"script": "true", **options})
        self.assertEqual(list(self.tmp.iterdir()), [])
        rid = c.submit(self.queue.directory, self.queue.epoch, "true")
        path = self.tmp / (rid + ".request")
        data = json.loads(path.read_text())
        for bad in (json.dumps({**data, "worktree": "elsewhere"}), json.dumps({**data, "id": "0" * 32}),
                    " " * (c.LIMIT + 1), json.dumps(data)[:-1] + ', "version": 1}'):
            path.write_text(bad)
            with self.assertRaises(ValueError):
                c.claim(self.queue, rid)

    def test_publication_is_complete_and_exclusive(self):
        link = os.link
        def inspect(source, target, **kwargs):
            self.assertFalse((self.tmp / target).exists())
            self.assertEqual(json.loads((self.tmp / source).read_text())["script"], "true")
            return link(source, target, **kwargs)
        with patch.object(c.os, "link", side_effect=inspect):
            rid = c.submit(self.queue.directory, self.queue.epoch, "true")
        with self.assertRaises(FileExistsError):
            c._publish(self.queue.directory, rid + ".request", {})
        self.assertEqual(len(list(self.tmp.iterdir())), 1)
        path = self.tmp / (rid + ".request")
        before = path.stat()
        path.write_text("{}")
        with patch.object(c.os, "fstat", side_effect=[before, before, path.stat()]), \
                self.assertRaisesRegex(ValueError, "changed"):
            c._read(self.queue.directory, path.name)
