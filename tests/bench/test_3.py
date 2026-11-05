"""Archive boundary regressions use local files and in-memory peers only."""
import io
import os
from pathlib import Path
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from hansa.bench import archive as a
from hansa.bench.channel import Channel


class BenchArchiveTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.out, self.worker = self.root / "out", self.root / "worker"
        self.out.mkdir()
        self.worker.mkdir()
        self.fd = os.open(self.out, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, self.fd)
        self.channel = Channel(self.worker, "run", self.out)
        self.addCleanup(self.channel.close)

    def tar(self, *entries):
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w", format=tarfile.PAX_FORMAT) as archive:
            for name, attributes in entries:
                info = tarfile.TarInfo(name)
                info.size = 4
                for key, value in attributes.items():
                    setattr(info, key, value)
                archive.addfile(info, io.BytesIO(b"data"))
        return stream.getvalue()

    def returned(self, used=0, **kwargs):
        with a.collect(self.fd, {"status": "failed", "exit": 7}, **kwargs) as stream:
            length = stream.seek(0, 2)
            stream.seek(0)
            return a.deliver(stream, length, self.channel, used)

    def test_hostile_tar_refused(self):
        good = self.tar(("file", {}))
        attacks = [self.tar((name, {})) for name in ("../escape", "/escape", "a/../b", "a//b", "a\\b", "a\nb")]
        attacks += [self.tar(("file", {"type": kind})) for kind in
                    (tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE, tarfile.CHRTYPE, tarfile.GNUTYPE_SPARSE)]
        attacks += [self.tar(("file", {}), ("file", {})), good[:1024], good + b"x" * 512,
                    self.tar(("file", {"pax_headers": {"path": "../escape"}})),
                    self.tar(("file", {"pax_headers": {"GNU.sparse.size": "4"}}))]
        for data in attacks:
            with self.subTest(data=data[:20]), self.assertRaises((ValueError, tarfile.TarError)):
                a.unpack(io.BytesIO(data), len(data), self.root / "extracted")
            self.assertFalse((self.root / "extracted").exists())
        for limit in ("MAX_BYTES", "MAX_ENTRIES", "MAX_ARCHIVE", "MAX_METADATA"):
            with patch.object(a, limit, 0), self.assertRaises(ValueError):
                a.unpack(io.BytesIO(good), len(good), self.root / "extracted")
        with self.assertRaisesRegex(ValueError, "incomplete"):
            a.deliver(io.BytesIO(good[:600]), len(good), self.channel, 0)

    def test_guest_filter_and_exact_length(self):
        if not hasattr(tarfile, "data_filter"):
            self.skipTest("guest extraction needs the tarfile data filter, absent in this host Python")
        name = "nested/" + "é" * 80
        data = self.tar((name, {"mode": 0o755}))
        source = io.BytesIO(data + b"still open")
        with patch.object(tarfile.TarFile, "extract", autospec=True, side_effect=tarfile.TarFile.extract) as extract:
            a.unpack(source, len(data), self.root / "extracted")
            self.assertEqual(extract.call_args.kwargs["filter"], "data")
        self.assertEqual((self.root / "extracted" / name).read_bytes(), b"data")
        self.assertTrue((self.root / "extracted" / name).stat().st_mode & 0o111)
        self.assertEqual(source.read(), b"still open")

    def test_artifact_caps(self):
        name = "nested/" + "é" * 80
        (self.out / "nested").mkdir()
        for path in ("a", name):
            (self.out / path).write_bytes(b"data")
        with a.collect(self.fd, {"status": "ok"}) as stream:
            data = stream.read()
        for limit, value in (("FILE_BYTES", 3), ("RUN_BYTES", 4), ("ARTIFACT_ENTRIES", 1)):
            with patch.object(a, limit, value):
                result = self.returned()
                self.assertTrue(result["result"]["rejected"])
                self.assertLessEqual(len(result["artifacts"]), 1)
                before = set(self.worker.iterdir())
                with self.assertRaises(ValueError):
                    a.deliver(io.BytesIO(data), len(data), self.channel, 0)
                self.assertEqual(set(self.worker.iterdir()), before)
        (self.out / "link").symlink_to(self.out, target_is_directory=True)
        os.link(self.out / "a", self.out / "hard")
        os.mkfifo(self.out / "pipe")
        with patch.object(a, "OUTPUT_BYTES", 4), patch.object(tarfile.TarFile, "extract", side_effect=AssertionError):
            result = self.returned(stdout=b"abc", stderr=b"def")
        self.assertEqual((result["stdout"], result["stderr"]), (b"abc", b"d"))
        self.assertEqual(result["result"], dict(status="failed", exit=7, rejected=True, truncated=True))
        self.assertEqual({n: (self.worker / p).read_bytes() for n, p in result["artifacts"].items()}, {name: b"data"})
        filled = self.returned(used=a.RUN_BYTES - 4)
        self.assertEqual(filled["used"], a.RUN_BYTES)
        before = set(self.worker.iterdir())
        with self.assertRaisesRegex(ValueError, "allowance"):
            self.returned(used=filled["used"])
        self.assertEqual(set(self.worker.iterdir()), before)

    def test_delivery_never_follows_links(self):
        (self.out / "file").write_bytes(b"data")
        outside = self.root / "outside"
        outside.write_bytes(b"untouched")
        target = self.worker / "bench-fixed"
        for kind in ("symlink", "hardlink", "fifo"):
            if kind == "symlink":
                target.symlink_to(outside)
            elif kind == "hardlink":
                os.link(outside, target)
            else:
                os.mkfifo(target)
            with patch.object(a.uuid, "uuid4", return_value=SimpleNamespace(hex="fixed")), self.assertRaises(OSError):
                self.returned()
            self.assertEqual(outside.read_bytes(), b"untouched")
            target.unlink()
        original = a.safe_open
        def swap(fd, name, flags=os.O_RDONLY):
            if flags & os.O_CREAT:
                self.worker.rename(self.root / "moved")
                self.worker.symlink_to(self.out, target_is_directory=True)
            return original(fd, name, flags)
        with patch.object(a, "safe_open", swap), self.assertRaisesRegex(ValueError, "directory replaced"):
            self.returned()
        self.assertEqual(list((self.root / "moved").iterdir()), [])
        self.assertEqual([p.name for p in self.out.iterdir()], ["file"])
