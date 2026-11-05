"""Guest gateway exercised through local pipes and a fake bwrap; no VM/network."""
import fcntl
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from unittest.mock import patch
from hansa.bench import guest as g

POPEN, RMTREE, TERMINATE = subprocess.Popen, g.shutil.rmtree, g.terminate


class BenchGuestTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.request = dict(version=1, id="a" * 32, script="echo submitted", cwd=".", timeout=30, length=1024)
        self.source, self.writer = os.pipe()
        self.addCleanup(lambda: os.close(self.source))
        self.addCleanup(lambda: os.close(self.writer) if self.writer is not None else None)
        self.code, self.events, self.info = "print('done')", [], True
        for mock in (patch.object(g.subprocess, "Popen", self.spawn),
                     patch.object(g.os, "sync"),
                     patch.dict(os.environ, SSH_ORIGINAL_COMMAND="bench-v1")):
            mock.start()
            self.addCleanup(mock.stop)

    def spawn(self, command, **kwargs):
        self.command, self.options = command, kwargs
        prefix = "import os, sys; fd=int(sys.argv[1]); os.write(fd, b'{\"child-pid\":1}'); os.close(fd)\n"
        return POPEN([sys.executable, "-c", (prefix if self.info else "") + self.code,
                      command[command.index("--info-fd") + 1], command[command.index("/out") - 1]], **kwargs)

    def box(self, **kwargs):
        return g.run_box(self.root, self.request, self.source, time.monotonic() + 20, **kwargs)

    def test_slot_deletion_syncs_before_unlock(self):
        def synced():
            self.assertEqual([p.name for p in self.root.iterdir()], ["lock"])
            with (self.root / "lock").open("rb") as lock, self.assertRaises(BlockingIOError):
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for failed in (False, True):
            header = json.dumps(self.request).encode()
            os.write(self.writer, len(header).to_bytes(4, "big") + header + bytes(1024))
            with tempfile.TemporaryFile() as target, patch.object(g.os, "sync", side_effect=synced) as sync, \
                    patch.object(g, "run_box", return_value=(dict(status="ok"), b"", b"")):
                if failed:
                    with patch.object(g.archive, "collect", side_effect=ValueError("collect failed")), \
                            self.assertRaisesRegex(ValueError, "collect failed"):
                        g.main(self.source, target.fileno(), self.root)
                else:
                    g.main(self.source, target.fileno(), self.root)
                sync.assert_called_once_with()

    def test_original_command_not_evaluated(self):
        touch = "touch " + shlex.quote(str(self.root / "sentinel"))
        for command in ("", "bench-v1\n", "bench-v1; " + touch, "$(" + touch + ")"):
            with patch.dict(os.environ, SSH_ORIGINAL_COMMAND=command), self.assertRaisesRegex(ValueError, "literal"):
                g.main(-1, -1, self.root)
            self.assertEqual(list(self.root.iterdir()), [])
        for change in ({"extra": 1}, {"version": True}, {"id": "bad"}, {"script": "\0"},
                       {"cwd": "../escape"}, {"timeout": float("nan")}):
            data = json.dumps(dict(self.request, **change)).encode()
            with self.assertRaises(ValueError):
                g._header(io.BytesIO(len(data).to_bytes(4, "big") + data))
        for data in (b'{"version":1,"version":1}', b'[]', b'\xff'):
            with self.assertRaises(ValueError):
                g._header(io.BytesIO(len(data).to_bytes(4, "big") + data))
        for size in (0, g.archive.HEADER_BYTES + 1, 10):
            with self.assertRaises(ValueError):
                g._header(io.BytesIO(size.to_bytes(4, "big")))

    def test_hard_nproc(self):
        self.code = ("import resource\nprint(resource.getrlimit(resource.RLIMIT_NPROC), flush=True)\n"
                     "try: resource.setrlimit(resource.RLIMIT_NPROC, (257, 257))\n"
                     "except ValueError: print('locked')\nelse: raise RuntimeError('raised hard limit')")
        result, stdout, _ = self.box()
        self.assertEqual(result["status"], "ok")
        self.assertIn(b"(256, 256)", stdout)
        self.assertIn(b"locked", stdout)
        self.assertEqual(self.options["env"], {})
        self.assertTrue(self.options["close_fds"])
        for option in ("--unshare-all", "--die-with-parent", "--new-session", "--clearenv"):
            self.assertIn(option, self.command)
        self.assertIn("--cap-drop ALL", " ".join(self.command))
        self.assertIn("--ro-bind " + str(self.root / "run.sh") + " /run.sh", " ".join(self.command))
        self.code = "import os; os.write(1, b'x' * 1100000); os.write(2, b'y' * 1100000)"
        result, stdout, stderr = self.box()
        self.assertEqual((result["status"], result["truncated"]), ("ok", True))
        self.assertEqual(len(stdout) + len(stderr), g.archive.OUTPUT_BYTES)
        self.info, self.code = False, "pass"
        self.assertEqual(self.box()[0]["status"], "infrastructure")

    def test_kill_and_cleanup_before_unlock(self):
        def locked():
            with (self.root / "lock").open("rb") as lock, self.assertRaises(BlockingIOError):
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        def reap(process):
            TERMINATE(process)
            self.assertIsNotNone(process.returncode)
            locked()
            self.events.append("reaped")
        def purge(path):
            locked()
            self.assertEqual(self.events[-1], "reaped")
            if mode == "purge-failed":
                raise PermissionError("cleanup refused")
            self.events.append("purged")
            RMTREE(path)
        def uncertain(process):
            reap(process)
            raise g.CleanupError()
        for mode in ("ok", "failed", "timeout", "cancelled", "purge-failed", "uncertain"):
            self.events.clear()
            header = json.dumps(self.request).encode()
            os.write(self.writer, len(header).to_bytes(4, "big") + header + bytes(1024))
            if mode == "cancelled":
                os.close(self.writer)
                self.writer = None
            self.code = {"ok": "from pathlib import Path; Path(sys.argv[2], 'proof').write_text('evidence')",
                         "failed": "raise SystemExit(7)"}.get(mode, "import signal; signal.pause()")
            ticks = iter([0, 0, 0])
            clock = time.monotonic if mode in ("ok", "failed", "cancelled") else lambda: next(ticks, 1000)
            with tempfile.TemporaryFile() as target, patch.object(g, "terminate", reap), \
                    patch.object(g.shutil, "rmtree", purge):
                if mode in ("purge-failed", "uncertain"):
                    with patch.object(g, "terminate", uncertain if mode == "uncertain" else reap), \
                            self.assertRaises((g.CleanupError, PermissionError)):
                        g.main(self.source, target.fileno(), self.root, clock)
                    with self.assertRaisesRegex(g.CleanupError, "dirty"):
                        g.main(self.source, target.fileno(), self.root)
                    self.assertEqual(self.events, ["reaped"])
                    self.assertEqual(target.tell(), 0)
                    for leftover in self.root.glob("slot-*"):
                        RMTREE(leftover)
                    continue
                g.main(self.source, target.fileno(), self.root, clock)
                self.assertEqual(self.events, ["reaped", "purged"])
                self.assertEqual([p.name for p in self.root.iterdir()], ["lock"])
                with (self.root / "lock").open("rb") as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                target.seek(0)
                reply = json.loads(target.read(int.from_bytes(target.read(4), "big")))
                data = target.read()
                self.assertEqual(reply, dict(version=1, id=self.request["id"], length=len(data)))
                with tarfile.open(fileobj=io.BytesIO(data)) as returned:
                    self.assertEqual(json.load(returned.extractfile("result.json"))["status"], mode)
                    if mode == "ok":
                        self.assertEqual(returned.extractfile("artifacts/proof").read(), b"evidence")
            if self.writer is None:
                os.close(self.source)
                self.source, self.writer = os.pipe()
