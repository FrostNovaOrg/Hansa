"""Timeout results and recovery use fake transport, local pipes and an injected clock."""
import json
import unittest
from unittest.mock import Mock

from hansa.bench import archive, channel
from hansa.bench.guest import _header
from tests.bench import test_8


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
