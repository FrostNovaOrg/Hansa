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
from tests.bench import test_3, test_8


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
        self.assertEqual(request["timeout"], int(deadline - self.now - broker.RETURN_ALLOWANCE))
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
        self.assertFalse(self.b.closed)
        self.assertIsNone(self.vm.port)

    def cancel_active(self):
        worker = self.add_worker()
        rid = channel.submit(worker.directory, worker.epoch, "true")
        self.release.clear()
        def connect(source, target, deadline):
            self.entered.set()
            while source.read(archive.HEADER_BYTES):
                pass  # Cancellation must close stdin and join this connection.
            self.release.set()
        with patch.object(self.vm, "connect", side_effect=connect):
            self.b.poll(self.workers, enabled=True)
            job = self.b.active
            self.addCleanup(job.thread.join, 10)
            self.addCleanup(job.cancel.set)
            self.assertTrue(self.entered.wait(10))
            self.b.revoke(worker)
        self.assertFalse(job.thread.is_alive())
        self.assertTrue(self.release.is_set())
        self.assertEqual(self.result(worker, rid)["status"], "cancelled")
        return worker

    def test_cancelled_run_recovers(self):
        worker = self.cancel_active()
        folder = worker.directories[0][0]
        before = {p.name: p.read_bytes() for p in folder.iterdir()}
        self.vm.reset_mock()
        self.vm.connect = Mock(side_effect=self.connect)
        self.assertEqual(self.complete(self.add_worker())["status"], "failed")
        self.assertEqual([c[0] for c in self.vm.mock_calls], ["stop", "reset", "start", "connect"])
        self.assertEqual({p.name: p.read_bytes() for p in folder.iterdir()}, before)
        self.assertFalse(self.b.closed)

    def test_cancel_reset_failure_replies_closed(self):
        self.cancel_active()
        self.vm.reset.side_effect = RuntimeError("reset failed")
        self.assertEqual(self.complete(self.add_worker())["status"], "infrastructure")
        for _ in range(2):
            worker = self.add_worker()
            rid = channel.submit(worker.directory, worker.epoch, "true")
            self.b.poll(self.workers, enabled=True)
            result = self.result(worker, rid)
            self.assertEqual(result["status"], "infrastructure")
            self.assertIn("closed", result["error"])
            self.assertIn("reset failed", result["error"])
        self.b = broker.Broker(self.b.desk, self.vm, clock=lambda: self.now)
        self.vm.stop.side_effect = RuntimeError("stop failed")
        worker = self.add_worker()
        rid = channel.submit(worker.directory, worker.epoch, "true")
        self.b.poll(self.workers, enabled=True)
        self.assertIn("stop failed", self.result(worker, rid)["error"])

    def test_worker_allowance_rejects_without_closing(self):
        worker = self.add_worker()
        self.b.used[worker.run] = archive.RUN_BYTES
        result = self.complete(worker)
        self.assertEqual(result["status"], "failed")
        folder = worker.directories[0][0]
        report = json.loads((folder / result["report"]).read_text())
        self.assertTrue(report["result"]["rejected"])
        self.assertEqual(report["artifacts"], {})
        self.assertEqual(list(folder.glob("bench-*")), [folder / result["report"]])
        self.assertEqual(self.b.used[worker.run], archive.RUN_BYTES)
        self.assertFalse(self.b.closed)
        self.assertEqual(self.complete(self.add_worker())["status"], "failed")


class BenchRecoveryTest(unittest.TestCase):
    setUp = test_8.BenchBrokerTest.setUp
    add_worker = test_8.BenchBrokerTest.add_worker
    result = test_8.BenchBrokerTest.result

    def complete(self, worker):
        rid = channel.submit(worker.directory, worker.epoch, "true")
        self.b.poll(self.workers, enabled=True)
        if self.b.active:
            self.b.active.thread.join(10)
            self.assertFalse(self.b.active.thread.is_alive())
        return self.result(worker, rid)

    def connect(self, source, target, deadline):
        request = _header(source)
        self.assertEqual(source.read(request["length"]), b"source")
        self.now += request["timeout"] + 15  # Script limit, teardown and return.
        with archive.collect(self.fd, dict(id=request["id"], status="timeout", exit=-9), self.output) as tar:
            data = tar.read()
        header = json.dumps(dict(version=1, id=request["id"], length=len(data))).encode()
        target.write(len(header).to_bytes(4, "big") + header + data)

    def test_script_timeout_returns_output_and_file(self):
        worker = self.add_worker()
        result = self.complete(worker)
        self.assertEqual(result["status"], "timeout")
        self.assertIn("report", result)
        folder = worker.directories[0][0]
        report = json.loads((folder / result["report"]).read_text())
        self.assertEqual(report["stdout"], self.output.decode())
        self.assertEqual(set(report["artifacts"]), {"evidence.txt"})
        self.assertEqual((folder / report["artifacts"]["evidence.txt"]).read_text(), "evidence")
        self.assertEqual(self.now, 285)  # 300 s total, less 30 s allowance, plus 15 s return.
        self.assertFalse(self.b.closed)

    def test_short_limit_refuses_without_vm_calls(self):
        self.b.ready, self.vm.clean, self.vm.port = True, True, 2200
        export = self.export.side_effect
        def snapshot(*args):
            self.now += elapsed
            return export(*args)
        self.export.side_effect = snapshot
        for limit, elapsed in ((30, 0), (30.9, 0), (300, 269.1)):
            with self.subTest(limit=limit):
                result = self.complete(self.add_worker(deadline=limit))
                self.assertEqual(result["status"], "timeout")
                self.assertIn("too short", result["error"])
                self.assertFalse(self.b.closed)
                self.assertEqual(self.vm.mock_calls, [])

    def test_transport_failure_resets_before_another_worker(self):
        self.vm.connect = Mock(side_effect=RuntimeError("transport failed"))
        result = self.complete(self.add_worker())
        self.assertEqual(result["status"], "infrastructure")
        self.assertTrue(result["error"])
        self.vm.reset_mock()
        self.vm.connect = Mock(side_effect=self.connect)
        result = self.complete(self.add_worker())
        self.assertIn("report", result)
        self.assertEqual([c[0] for c in self.vm.mock_calls], ["stop", "reset", "start", "connect"])

    def test_off_on_clears_stop_failure_and_runs_barrier(self):
        self.vm.stop.side_effect = RuntimeError("stop failed")
        self.assertIn("stop failed", self.complete(self.add_worker())["error"])
        self.assertTrue(self.b.closed)
        self.b.poll(self.workers, enabled=False)
        self.vm.stop.side_effect = lambda deadline: setattr(self.vm, "port", None)
        self.vm.reset_mock()
        self.vm.connect = Mock(side_effect=self.connect)
        self.assertIn("report", self.complete(self.add_worker()))
        self.assertEqual([c[0] for c in self.vm.mock_calls], ["stop", "reset", "start", "connect"])
