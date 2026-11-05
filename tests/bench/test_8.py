"""Broker requests use real channel/archive code, pipes and a fake VM; no network."""
import io
import json
import select
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from hansa.bench import archive, channel, broker
from hansa.bench.guest import _header
from tests.bench import test_3


class BenchBrokerTest(unittest.TestCase):
    def setUp(self):
        test_3.BenchArchiveTest.setUp(self)
        self.now, self.workers = 0, {}
        (self.out / "evidence.txt").write_text("evidence")
        self.release, self.entered = threading.Event(), threading.Event()
        self.release.set()
        self.bad, self.output = False, b"hello" * 14000
        self.vm = Mock(port=None, clean=False, connect=self.connect)
        self.vm.stop.side_effect = lambda deadline: setattr(self.vm, "port", None)
        self.vm.reset.side_effect = lambda deadline: setattr(self.vm, "clean", True)
        self.vm.start.side_effect = lambda deadline: setattr(self.vm, "port", 2200)
        self.b = broker.Broker(SimpleNamespace(state_dir=self.root), self.vm, clock=lambda: self.now)
        self.addCleanup(lambda: [w.close() for w in self.workers])
        self.addCleanup(self.b.revoke)
        self.addCleanup(self.release.set)
        fake = patch.object(broker.snapshot, "export", side_effect=lambda *args: io.BytesIO(b"source"))
        self.export = fake.start()
        self.addCleanup(fake.stop)

    def add_worker(self, epoch=None, deadline=1000):
        folder = self.root / str(len(self.workers))
        folder.mkdir()
        worker = channel.Channel(folder, folder.name, self.root, epoch or self.b.epoch)
        self.workers[worker] = deadline
        return worker

    def complete(self, worker):
        rid = channel.submit(worker.directory, worker.epoch, "true")
        self.b.poll(self.workers, enabled=True)
        self.b.active.thread.join(10)
        self.assertFalse(self.b.active.thread.is_alive())
        return self.result(worker, rid)

    def result(self, worker, rid):
        return channel._read(worker.directory, rid + ".reply")["result"]

    def connect(self, source, target, deadline):
        request = _header(source)
        self.assertEqual(request["timeout"], int(deadline - self.now))
        self.assertEqual(source.read(request["length"]), b"source")
        self.assertFalse(select.select([source], [], [], 0)[0])  # EOF would cancel the guest.
        self.entered.set()
        self.assertTrue(self.release.wait(10))
        with archive.collect(self.fd, dict(id=request["id"], status="failed", exit=7), self.output) as tar:
            data = tar.read()
        header = json.dumps(dict(version=1, id=request["id"], length=len(data))).encode()
        target.write(len(header).to_bytes(4, "big") + header + (data[:-1] if self.bad else data))

    def test_fifth_waiter_busy(self):
        self.release.clear()
        workers = [self.add_worker() for _ in range(6)]
        channel.submit(workers[0].directory, workers[0].epoch, "true")
        self.b.poll(self.workers, enabled=True)
        self.assertTrue(self.entered.wait(10))
        ids = [channel.submit(w.directory, w.epoch, "true") for w in workers]
        self.b.poll(self.workers, enabled=True)
        self.assertEqual(len(self.b.waiting), 4)
        for worker, rid in ((workers[0], ids[0]), (workers[-1], ids[-1])):
            self.assertEqual(self.result(worker, rid)["status"], "busy")
        self.release.set()
        self.b.revoke()

    def test_restart_discards_work(self):
        stale = self.add_worker(epoch="0" * 32)
        for _ in range(2):
            rid = channel.submit(stale.directory, stale.epoch, "true")
        channel.claim(stale, rid)  # An earlier active request as well as pending work.
        stale.epoch, stale.claimed = self.b.epoch, set()
        self.complete(self.add_worker())
        self.assertEqual(self.export.call_count, 1)
        self.assertEqual([c[0] for c in self.vm.mock_calls][:3], ["stop", "reset", "start"])
        self.assertEqual(len(stale.claimed), 0)

    def test_idle_stops(self):
        worker = self.add_worker(deadline=50)
        rid = channel.submit(worker.directory, worker.epoch, "true")
        self.b.poll(self.workers)  # Off is the default, without VM calls.
        self.assertEqual(self.vm.mock_calls, [])
        self.b.poll(self.workers, enabled=True, held=True)
        self.assertEqual(self.export.call_count, 0)
        self.now = 50
        self.b.poll(self.workers, enabled=True)
        self.assertEqual(self.result(worker, rid)["status"], "timeout")
        self.workers[worker] = 1000
        self.complete(worker)
        for self.now, port in ((109, 2200), (110, None)):
            self.b.poll(self.workers, enabled=True)
            self.assertEqual(self.vm.port, port)

    def test_results_arrive_automatically(self):
        worker = self.add_worker()
        result = self.complete(worker)
        report = json.loads((worker.directories[0][0] / result["report"]).read_text())
        self.assertEqual((result["status"], report["result"]["exit"], report["stdout"]),
                         ("failed", 7, self.output.decode()))
        name = report["artifacts"]["evidence.txt"]
        self.assertEqual((worker.directories[0][0] / name).read_text(), "evidence")
        self.assertEqual(self.b.used[worker.run], 8)
        self.bad = True
        self.assertEqual(self.complete(worker)["status"], "infrastructure")
        self.assertTrue(self.b.closed)
        self.assertIsNone(self.vm.port)
