"""Queue tests use local files only; no VM, network or worker lifecycle."""
import contextlib
import errno
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

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

    def assert_closed(self, *descriptors):
        for fd in descriptors:
            with self.assertRaises(OSError) as ended:
                os.fstat(fd)
            self.assertEqual(ended.exception.errno, errno.EBADF)

    def test_owner_check_closes_rejected_file(self):
        path = self.tmp / "foreign"
        path.write_text("{}")
        opened, real_open = [], os.open
        def opening(*args, **kwargs):
            fd = real_open(*args, **kwargs)
            opened.append(fd)
            return fd
        with patch.object(c.os, "open", opening), patch.object(c.os, "getuid", return_value=path.stat().st_uid + 1), \
                self.assertRaisesRegex(ValueError, "owned"):
            c._read(self.queue.directory, path.name)
        self.assert_closed(*opened)

    def test_filename_guard_precedes_open(self):
        for name in ("", ".", "..", "/absolute", "nested/file", "back\\slash"):
            with self.subTest(name=name), patch.object(c.os, "open", side_effect=AssertionError("opened unsafe name")), \
                    self.assertRaisesRegex(ValueError, "filename"):
                c.safe_open(self.queue.directory, name)

    def test_request_version_and_tokens(self):
        request = dict(version=1, id="a" * 32, epoch=self.queue.epoch, script="true", cwd=".", timeout=30, wait=10)
        for key, values in (("version", (0, 2, True, 1.0, "1")),
                            ("id", (None, 1, "A" * 32, "a" * 31, "a" * 33, "g" * 32, "a" * 32 + "\n")),
                            ("epoch", (None, 1, "A" * 32, "a" * 31, "a" * 33, "g" * 32))):
            for value in values:
                with self.subTest(key=key, value=value), self.assertRaisesRegex(ValueError, "version|identity"):
                    c._validate(dict(request, **{key: value}))
        for value in (None, 1, "A" * 32, "../escape", "a" * 31):
            with self.subTest(id=value), patch.object(c, "_read", side_effect=AssertionError("read invalid id")), \
                    self.assertRaisesRegex(ValueError, "request id"):
                c.claim(self.queue, value)

    def test_second_reply_refused_after_worker_unlinks_first(self):
        rid = c.submit(self.queue.directory, self.queue.epoch, "true")
        c.claim(self.queue, rid)
        c.reply(self.queue, rid, {"status": "ok"})
        (self.tmp / (rid + ".reply")).unlink()
        with self.assertRaisesRegex(ValueError, "awaiting"):
            c.reply(self.queue, rid, {"status": "failed"})
        self.assertFalse((self.tmp / (rid + ".reply")).exists())

    def test_size_checked_before_read(self):
        stream = io.BytesIO(b"{}")
        with patch.object(c, "safe_open", return_value=123), patch.object(c.os, "fdopen", return_value=stream), \
                patch.object(stream, "fileno", return_value=123), \
                patch.object(c.os, "fstat", return_value=SimpleNamespace(st_size=c.LIMIT + 1)), \
                patch.object(stream, "read", side_effect=AssertionError("read oversized file")), \
                self.assertRaisesRegex(ValueError, "size limit"):
            c._read(self.queue.directory, "large.request")
        self.assertTrue(stream.closed)

    def test_growth_after_size_check_refused(self):
        path = self.tmp / "grow.request"
        path.write_bytes(b"{}")
        stats, fstat = [], os.fstat
        def stale(fd):  # Grow after the size check; report the first stat, as a coarse clock might.
            stats.append(fstat(fd))
            path.write_bytes(b" " * (c.LIMIT + 1) + b"{}")
            return stats[0]
        with patch.object(c.os, "fstat", stale), self.assertRaisesRegex(ValueError, "size limit"):
            c._read(self.queue.directory, path.name)

    def test_script_read_is_bounded(self):
        script = self.root / "large.sh"
        script.write_bytes(b"x" * (c.LIMIT + 2))
        streams, fdopen = [], os.fdopen
        def opening(*args, **kwargs):
            stream = fdopen(*args, **kwargs)
            stream.read = Mock(wraps=stream.read)
            streams.append(stream)
            return stream
        with patch.dict(os.environ, HANSA_BENCH_DIR=str(self.tmp), HANSA_BENCH_EPOCH=self.queue.epoch), \
                patch.object(c.os, "fdopen", opening), self.assertRaisesRegex(SystemExit, "refused"):
            c.command(SimpleNamespace(script=script, cwd=".", timeout=30, wait=10))
        streams[0].read.assert_called_once_with(c.LIMIT + 1)
        self.assertTrue(streams[0].closed)
        self.assertEqual(list(self.tmp.iterdir()), [])

    def test_command_closes_descriptors_on_success_and_errors(self):
        script, real_open, fdopen = self.root / "run.sh", os.open, os.fdopen
        for mode in ("ok", "parent", "script", "read", "decode", "submit", "write", "flush", "publish"):
            opened, streams = [], []
            def opening(*args, **kwargs):
                fd = real_open(*args, **kwargs)
                opened.append(fd)
                return fd
            def streaming(*args, **kwargs):
                stream = fdopen(*args, **kwargs)
                streams.append(stream)  # Keep it alive: finalizers must not hide a missing close.
                if mode in ("read", "write", "flush"):
                    setattr(stream, mode, Mock(side_effect=OSError("refused")))
                return stream
            script.write_bytes(b"\xff" if mode == "decode" else b"true")
            path = {"parent": self.root / "missing/run.sh", "script": script.with_name("missing.sh")}.get(mode, script)
            with self.subTest(mode=mode), contextlib.ExitStack() as stack:
                stack.enter_context(patch.dict(os.environ, HANSA_BENCH_DIR=str(self.tmp), HANSA_BENCH_EPOCH=self.queue.epoch))
                stack.enter_context(patch.object(c.os, "open", opening))
                stack.enter_context(patch.object(c.os, "fdopen", streaming))
                output = stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
                if mode == "submit":
                    stack.enter_context(patch.object(c, "submit", side_effect=ValueError("refused")))
                if mode == "publish":
                    stack.enter_context(patch.object(c.os, "link", side_effect=OSError("refused")))
                args = SimpleNamespace(script=path, cwd=".", timeout=30, wait=10)
                if mode == "ok":
                    c.command(args)
                    rid = output.getvalue().strip()
                    c.claim(self.queue, rid)
                    c.reply(self.queue, rid, {})
                else:
                    with self.assertRaisesRegex(SystemExit, "refused"):
                        c.command(args)
                self.assertTrue(all(stream.closed for stream in streams))
                self.assert_closed(*opened)

    def test_read_error_closes_descriptor(self):
        path = self.tmp / "bad.request"
        for data in (b"{}", b"not json", b" " * (c.LIMIT + 1), b"read error"):
            path.write_bytes(data)
            fd = c.safe_open(self.queue.directory, path.name)
            stream = os.fdopen(fd, "rb")
            if data == b"read error":
                stream.read = Mock(side_effect=OSError("refused"))
            with self.subTest(size=len(data)), patch.object(c, "safe_open", return_value=fd), \
                    patch.object(c.os, "fdopen", return_value=stream):
                if data == b"{}":
                    self.assertEqual(c._read(self.queue.directory, path.name), {})
                else:
                    with self.assertRaises((ValueError, OSError)):
                        c._read(self.queue.directory, path.name)
                self.assertTrue(stream.closed)
                self.assert_closed(fd)

    def test_directory_descriptors_close_on_partial_init_and_teardown(self):
        fd = os.open(self.tmp, os.O_RDONLY | os.O_DIRECTORY)
        with patch.object(c.os, "open", side_effect=[fd, OSError("second directory refused")]), self.assertRaises(OSError):
            self.register()
        self.assert_closed(fd)
        queue = self.register()
        descriptors = [entry[1] for entry in queue.directories]
        queue.close()
        self.assert_closed(*descriptors)
        queue.close()

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
