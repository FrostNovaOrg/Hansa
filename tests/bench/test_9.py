"""Worker integration through fake model commands and VM; no network or VM."""
import contextlib
import io
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import Mock, patch

from hansa import cli, run
from hansa.desk import write_json


class BenchIntegrationTest(unittest.TestCase):
    def setUp(self):
        from tests.test_hansa import DeskTest
        self.c = DeskTest()
        self.addCleanup(self.c.doCleanups)
        self.c.setUp()
        self.desk, self.dispatch = self.c.desk, run.Dispatcher(self.c.desk)
        from hansa.bench import manage, selftest
        settings = self.desk.settings()
        settings["bench"].update(build="ready", generation=1, recipe=manage.recipe_hash(), acceptance=dict(
            generation=1, status="passed", results={n: dict(status="passed", checks=dict.fromkeys(f, "passed"))
                                                  for n, f in selftest.mandatory().items()}))
        write_json(self.desk.settings_path, settings)

    def test_serve_rechecks_acceptance_and_reports_once(self):
        import copy
        good = self.desk.settings()["bench"]
        good["enabled"] = True
        bad = [dict(good, **change) for change in (dict(build="failed"), dict(generation=0),
               dict(generation=2), dict(recipe="stale"), dict(acceptance=None))]
        for change in (dict(status="failed"), dict(status="incomplete"), dict(results={}), dict(error="uncertain")):
            bad.append(dict(good, acceptance=dict(good["acceptance"], **change)))
        missing = copy.deepcopy(good); missing["acceptance"]["results"]["1/root-edge"]["checks"].pop("host")
        bad.append(missing)
        for block in bad:
            for existing in (False, True):
                with self.subTest(block=block, existing=existing), patch.object(run, "WorkerBench") as factory, \
                        contextlib.redirect_stderr(io.StringIO()) as out:
                    self.dispatch.bench_errors.clear()
                    self.desk._bench = Mock() if existing else None
                    settings = self.desk.settings(); settings["bench"] = block
                    write_json(self.desk.settings_path, settings)
                    self.dispatch.tick(); self.dispatch.tick()
                    factory.assert_not_called()
                    self.assertTrue(self.desk._bench_off)
                    if existing: self.assertEqual([c.args[0] for c in self.desk._bench.poll.call_args_list], [False, False])
                    self.assertEqual(out.getvalue().count("selftest"), 1)
        settings["bench"] = good; write_json(self.desk.settings_path, settings)
        self.dispatch.tick()
        self.assertFalse(self.desk._bench_off)
        self.assertTrue(self.desk._bench.poll.call_args.args[0])

    def enable(self):
        with patch("hansa.bench.vm.VM") as machine:
            self.dispatch.tick()
            machine.assert_not_called()  # default off must not touch Lima, even during serve startup.
        settings = self.desk.settings()
        settings["bench"]["enabled"] = True
        write_json(self.desk.settings_path, settings)
        with patch("hansa.bench.vm.VM", return_value=Mock(port=None)):
            self.dispatch.tick()
        self.bench = self.desk._bench
        self.addCleanup(self.bench.close)
        self.bench.pending.result(timeout=10)

    def bench_failure_starts(self, block):
        settings = self.desk.settings()
        settings["bench"] = block
        write_json(self.desk.settings_path, settings)
        with contextlib.redirect_stderr(io.StringIO()) as out:
            for _ in range(2):
                tid = self.desk.new_task("keep serving", lane="a")
                self.dispatch.tick()
                self.assertEqual(self.desk.meta(tid)["state"], "running")
                self.assertNotIn("Use the enabled bench", (self.desk.raw_dir(tid) / run.RESUME_ARGV).read_text())
                self.assertEqual(self.dispatch.procs[tid].wait(timeout=30), 0)
            self.dispatch.tick()
        self.assertEqual(len(out.getvalue().splitlines()), 1)
        self.assertIn("bench", out.getvalue())
        return out.getvalue()

    def test_malformed_bench_keeps_queue_moving(self):
        self.assertIn("KeyError", self.bench_failure_starts({}))

    def test_non_macos_bench_keeps_queue_moving(self):
        settings = self.desk.settings()["bench"]
        settings["enabled"] = True
        with patch("hansa.bench.vm.sys.platform", "linux"):
            self.assertIn("macOS", self.bench_failure_starts(settings))
        self.bench_failure_starts({})  # A different reason on the same server must also be reported.

    def test_poll_failure_keeps_queue_moving(self):
        self.desk._bench = Mock()
        self.desk._bench.poll.side_effect = ValueError("poll failed")
        settings = self.desk.settings()["bench"]
        settings["enabled"] = True
        self.assertIn("poll failed", self.bench_failure_starts(settings))
        self.desk._bench.register.assert_not_called()

    def test_both_workers_submit(self):
        from contrib.codex.tests.codexkit import WRAPPER, FAKES, shim
        self.enable()
        fake = self.c.tmp / "submit-worker"
        fake.write_text(f"#!{sys.executable}\n" +
            "import contextlib, io, json, os, sys\nfrom pathlib import Path\n" +
            f"sys.path.insert(0, {str(cli.DOCS.parent)!r})\nfrom hansa.cli import main\n" +
            "folder = Path(os.environ['HANSA_BENCH_DIR'])\nscript = folder / 'run.sh'\n" +
            "script.write_text('echo evidence')\n" +
            "with contextlib.redirect_stdout(io.StringIO()): main(['bench', 'run', '--script', str(script)])\n" +
            "print(json.dumps({'type': 'turn.completed'} if 'exec' in sys.argv else {'result': 'done'}))\n")
        fake.chmod(0o755)
        for kind in ("claude", "codex"):
            with self.subTest(worker=kind):
                if kind == "codex" and sys.platform != "darwin":
                    self.skipTest("Codex adapter workers require macOS; its sandbox guard is unchanged")
                tid = self.desk.new_task("submit evidence", lane="a")
                env = run.provider_env(self.desk, "claude", self.desk.settings()["providers"]["claude"])
                if kind == "codex":
                    env.update(ANTHROPIC_BASE_URL=shim.PLACEHOLDER, HANSA_CODEX_BIN=str(fake),
                               HANSA_CODEX_HOME=str(self.c.tmp / "codex-home"),
                               HANSA_CODEX_LOGS=str(self.c.tmp / "logs"), HANSA_CODEX_SRT=str(FAKES / "srt"))
                with patch.object(run, "claude_bin", return_value=str(WRAPPER if kind == "codex" else fake)), \
                        patch.object(run, "provider_env", return_value=env):
                    proc = run.start(self.desk, tid, "a", "claude")
                    self.assertEqual(proc.wait(timeout=30), 0, (self.desk.raw_dir(tid) / "run.err").read_text())
                worker, deadline = self.bench.workers[tid]
                request = next(worker.directories[0][0].glob("*.request"))
                self.bench.poll(True, True)  # a resource hold still queues, without starting a VM.
                self.bench.pending.result(timeout=10)
                self.assertEqual(self.bench.broker.waiting[0].request["script"], "echo evidence")
                self.assertIn(request.stem, worker.claimed)
                self.bench.broker.vm.start.assert_not_called()
                self.assertLessEqual(deadline - run.time.monotonic(), 60 * 240)
                self.assertIn("/out", (self.desk.raw_dir(tid) / run.RESUME_ARGV).read_text())
                run.remove_sandbox_tmp(self.desk, tid)
                self.desk.update(tid, state="done", pid=None)

    def test_teardown_precedes_commit(self):
        self.enable()
        tid = self.desk.new_task("finish evidence", lane="a")
        proc = run.start(self.desk, tid, "a", "claude")
        self.assertEqual(proc.wait(timeout=30), 0)
        self.dispatch.procs[tid] = proc
        folder = Path(self.desk.meta(tid)["worker_tmp"])
        entered, released = threading.Event(), threading.Event()
        def revoke(worker):
            entered.set()
            self.assertTrue(released.wait(15))
            self.assertTrue(folder.exists())
        self.addCleanup(released.set)
        with patch.object(self.bench.broker, "revoke", side_effect=revoke), \
                patch.object(run, "commit_work", wraps=run.commit_work) as commit:
            self.desk.update(tid, cancel=True)
            with patch.object(run, "has_ended", return_value=False), \
                    patch.object(run, "run_running", return_value=True), patch.object(run, "stop") as stop:
                self.dispatch.tick()
                stop.assert_called_once()  # worker cancellation proceeds while guest teardown waits.
            self.dispatch.tick()  # must return even while guest cleanup is waiting.
            self.assertTrue(entered.wait(10))
            self.assertTrue(folder.exists())
            commit.assert_not_called()
            released.set()
            self.bench.revoking[tid].result(timeout=10)
            self.dispatch.tick()
            self.assertFalse(folder.exists())
            commit.assert_called_once()
        self.assertFalse(self.bench.workers)

    def test_cancel_before_start_defers_cleanup_without_bench_wait(self):
        self.enable()
        tid = self.desk.new_task("cancel while preparing", lane="a")
        entered, released = threading.Event(), threading.Event()
        self.addCleanup(released.set)
        def revoke(worker):
            entered.set()
            self.assertTrue(released.wait(15))
        def cancel(*args):
            self.desk.update(tid, cancel=True)
            return []
        real_revoke = run.bench_revoke
        def nonblocking(desk, task, wait=False):
            self.assertFalse(wait, "serve must not wait for bench teardown")
            return real_revoke(desk, task, wait=wait)
        with patch.object(self.bench.broker, "revoke", side_effect=revoke), \
                patch.object(run, "bench_revoke", side_effect=nonblocking), \
                patch.object(run, "with_setup", side_effect=cancel):
            self.dispatch.tick()
            self.assertTrue(entered.wait(10))
            folder = Path(self.desk.meta(tid)["worker_tmp"])
            self.assertTrue(folder.is_dir())
            with self.desk.lock():
                self.dispatch.tick()  # pending cleanup must also avoid waiting under the desk lock.
            self.desk.update(tid, state="queued", cancel=False)
            self.assertIsNone(run.start(self.desk, tid, "a", "claude"))
            self.assertEqual(self.desk.meta(tid)["state"], "queued")
            self.desk.update(tid, state="cancelled", cancel=True)
            released.set()
            self.bench.revoking[tid].result(timeout=10)
            self.dispatch.tick()
        self.assertFalse(folder.exists())
        self.assertEqual(self.desk.meta(tid)["state"], "cancelled")

    def test_bench_help(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            cli.main(["help", "test-bench"])
        for phrase in ("off by default", "--script", "ID.reply", "64 MiB", "selftest", "fresh epoch"):
            self.assertIn(phrase, out.getvalue())
        with contextlib.redirect_stdout(io.StringIO()) as out, self.assertRaises(SystemExit):
            cli.main(["bench", "run", "--help"])
        for phrase in ("request ID", "900", "300"):
            self.assertIn(phrase, out.getvalue())
