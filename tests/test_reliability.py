"""Real Git/process checks for parallel execution, shared writers and fail-closed gates.

No model or service manager is called. A worker barrier fails if the launcher is
serial; subprocess writers exercise the lock across processes, not just threads.
"""
import json
import os
import shlex
import subprocess
import sys
import threading
import time
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import test_gaps as fixtures
from bellhop import archive, daemon, exceptions, ledger, mcp_server, merge, ring, sentinel, watch
from bellhop.config import Desk
from bellhop.locking import desk_lock, worker_limit, worker_slot
from bellhop.util import BellhopError, now, read, write

ROOT = Path(__file__).resolve().parent.parent


class ReliabilityTest(unittest.TestCase):
    setUp = fixtures.GapTest.setUp
    tearDown = fixtures.GapTest.tearDown
    packet = fixtures.GapTest.packet

    def wait_until(self, predicate, seconds=8):
        until = time.monotonic() + seconds
        while time.monotonic() < until:
            if predicate():
                return
            time.sleep(0.02)
        self.fail("condition did not become true before the bounded timeout")

    def test_the_shipped_default_runs_one_worker_at_a_time(self):
        """The default is serial. The test below raises it on purpose, which is the
        only honest way to exercise the pool: a desk that overlapped two lanes because
        nobody chose to and a desk that was configured for it look the same in a log."""
        self.assertEqual(worker_limit(Desk(self.desk.root, {})), 1)
        self.assertEqual(watch.WatchPool(Desk(self.desk.root, {})).limit, 1)

    def test_independent_lanes_overlap_but_third_waits_and_receipts_archive(self):
        self.desk.cfg["max_parallel_workers"] = 2  # the default is 1; this tests the pool
        helper = self.tmp / "barrier.py"
        release = self.tmp / "release"
        write(helper, f'''import os, pathlib, runpy, time
root = pathlib.Path({str(self.tmp)!r})
(root / (os.environ["BELLHOP_ID"] + ".ready")).touch()
until = time.monotonic() + 12
while not (root / "release").exists():
    if time.monotonic() > until:
        raise RuntimeError("other worker never arrived")
    time.sleep(.02)
runpy.run_path({str(ROOT / "bellhop/stubworker.py")!r}, run_name="__main__")
''')
        for i, lane in enumerate(("api", "web", "docs"), 1):
            self.desk.cfg["lanes"][lane] = dict(self.desk.cfg["lanes"]["api"],
                worker_cmd=f"{shlex.quote(sys.executable)} {shlex.quote(str(helper))}")
            p = self.packet(f"{i:03}", f"Task {i}")
            write(p, read(p).replace("lane: api", f"lane: {lane}"))
            ring.ring(self.desk, p)
        pool = watch.WatchPool(self.desk)
        try:
            pool.tick()
            self.wait_until(lambda: len(list(self.tmp.glob("*.ready"))) == 2)
            self.assertEqual(len(pool.active), 2)
            self.assertFalse((self.tmp / "003.ready").exists())
            self.assertEqual(read(self.desk.state / "in-flight.md").count("[worker]"), 2)
        finally:
            release.touch()
            pool.close()
        for pid in ("001", "002"):
            meta = json.loads(read(self.desk.receipts / f"{pid}.meta.json"))
            self.assertFalse(meta["dead"], meta)
            self.assertIn(pid, read(self.desk.state / "closings.md"))
        self.assertNotIn("[worker]", read(self.desk.state / "in-flight.md"))
        self.assertEqual(len(list(self.desk.bells_rung.glob("*.bell"))), 2)
        self.assertEqual(len(list(self.desk.bells_pending.glob("*.bell"))), 1)
        self.assertEqual(fixtures.git(self.desk.root, "diff", "--cached", "--name-only"), "")

    def test_session_watchdog_keeps_heartbeat_during_worker_and_drains(self):
        ring.ring(self.desk, self.packet())
        entered, release = threading.Event(), threading.Event()
        launch = watch.launch_worker

        def blocked(*args, **kwargs):
            entered.set()
            if not release.wait(8):
                raise RuntimeError("test release did not arrive")
            return launch(*args, **kwargs)

        server = mcp_server.Server(self.desk.root)
        server.desk.cfg["session_watchdog_interval"] = .05
        with patch.object(watch, "launch_worker", side_effect=blocked):
            server.start_session_watchdog()
            try:
                self.assertTrue(entered.wait(5))
                p = self.desk.path(daemon.HEARTBEAT)
                first = p.stat().st_mtime_ns
                self.wait_until(lambda: p.stat().st_mtime_ns > first)
                self.assertTrue(server._thread.is_alive())
            finally:
                server._stop.set()
                release.set()
                server._thread.join(8)
        self.assertFalse(server._thread.is_alive())
        self.assertEqual(len(list(self.desk.bells_rung.glob("*.bell"))), 1)

    def test_shared_slots_cover_other_watch_processes(self):
        self.desk.cfg["max_parallel_workers"] = 1
        code = """from pathlib import Path
from bellhop.config import Desk
from bellhop.locking import worker_slot
from bellhop.util import BellhopError
import sys
desk = Desk(Path(sys.argv[1]), {"max_parallel_workers": 1})
try:
    with worker_slot(desk):
        print('acquired')
except BellhopError:
    print('busy')
"""
        def probe():
            return subprocess.check_output([sys.executable, "-c", code, str(self.desk.root)],
                                           cwd=ROOT, text=True).strip()
        with worker_slot(self.desk):
            self.assertEqual(probe(), "busy")
        self.assertEqual(probe(), "acquired")

    def test_process_writers_do_not_lose_state_rows_or_reuse_ledger_sequences(self):
        code = """from pathlib import Path
from bellhop.config import Desk
from bellhop import state, ledger
import sys
desk = Desk(Path(sys.argv[1]), {})
for n in range(8):
    text = sys.argv[2] + '-' + str(n)
    state.todo_add(desk, text + ' — why only you: approve the budget')
    ledger.add(desk, text, 'written by a concurrent process')
"""
        children = [subprocess.Popen([sys.executable, "-c", code, str(self.desk.root), str(i)],
                                    cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                    for i in range(3)]
        results = []
        try:
            for child in children:
                out, err = child.communicate(timeout=15)
                results.append((child.returncode, out + err))
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                child.communicate()
        for code, output in results:
            self.assertEqual(code, 0, output)
        paths = list(self.desk.ledger.rglob("*.md"))
        self.assertEqual(len(paths), 24)
        self.assertEqual(len({p.name.split('-')[0] for p in paths}), 24)
        text = read(self.desk.state / "human-todo.md")
        for i in range(3):
            for n in range(8):
                self.assertIn(f"{i}-{n}", text)

    def test_archive_preserves_unrelated_staging_and_noop_does_not_commit_it(self):
        unrelated = self.desk.path("private-note.txt")
        write(unrelated, "do not archive\n")
        fixtures.git(self.desk.root, "add", "private-note.txt")
        entry = ledger.add(self.desk, "a task", "the result")
        archive.archive(self.desk, [entry], "one entry")
        self.assertEqual(fixtures.git(self.desk.root, "show", "--format=", "--name-only", "HEAD"),
                         str(entry.relative_to(self.desk.root)))
        self.assertEqual(fixtures.git(self.desk.root, "diff", "--cached", "--name-only"), "private-note.txt")
        before = fixtures.git(self.desk.root, "rev-parse", "HEAD")
        archive.archive(self.desk, [entry], "nothing new")
        self.assertEqual(fixtures.git(self.desk.root, "rev-parse", "HEAD"), before)

    def test_busy_probe_errors_block_wake_clear_and_compact(self):
        for code in (2, 42, 126, 127, 130):
            self.desk.cfg["busy_probe_cmd"] = f"exit {code}"
            busy, why = sentinel.probe_busy(self.desk)
            self.assertEqual(busy, sentinel.BUSY)
            self.assertIn(str(code), why)
        self.desk.cfg["busy_probe_cmd"] = "exit 1"
        self.assertEqual(sentinel.probe_busy(self.desk)[0], sentinel.IDLE)

    def test_merge_on_judgement_refuses_without_a_reason(self):
        """The judgement is allowed; taking it silently is not."""
        ring.ring(self.desk, self.packet())
        watch.watch_once(self.desk)
        before = fixtures.git(self.repo, "rev-parse", "main")
        for reason in ("", "   "):
            with self.assertRaisesRegex(BellhopError, "--reason"):
                merge.merge(self.desk, "001", reason)
        self.assertEqual(fixtures.git(self.repo, "rev-parse", "main"), before)
        self.assertFalse(exceptions.path(self.desk).exists(), "a refusal wrote an exception line")

    def test_merge_on_judgement_merges_and_records_the_exception(self):
        """No acceptance file anywhere, and the merge still happens - with its reason on
        the ledger, in state/exceptions.md and in the close-out register."""
        ring.ring(self.desk, self.packet())
        watch.watch_once(self.desk)
        result = merge.merge(self.desk, "001", "verifier is down and the release is waiting")
        self.assertIn("merge lane/api", fixtures.git(self.repo, "log", "-1", "--format=%s", "main"))
        self.assertEqual(result.count, 1)
        self.assertFalse(result.red)
        line = read(exceptions.path(self.desk)).strip().splitlines()[-1]
        self.assertIn("| merge |", line)
        self.assertIn("verifier is down", line)
        entries = ledger.entries_mentioning(self.desk, "merge exception: 001")
        self.assertEqual(len(entries), 1, entries)
        self.assertIn("coordinator's judgement", read(entries[0]))
        self.assertIn("exception: verifier is down", read(self.desk.state / "closings.md"))

    def test_merge_exceptions_turn_the_sentinel_red_at_two_in_seven_days(self):
        name, ok, detail = exceptions.check(self.desk, "merge")
        self.assertTrue(ok, detail)
        exceptions.record(self.desk, "merge", "001 api one", "first")
        self.assertTrue(exceptions.check(self.desk, "merge")[1])
        exceptions.record(self.desk, "merge", "002 api two", "second")
        name, ok, detail = exceptions.check(self.desk, "merge")
        self.assertFalse(ok, detail)
        self.assertIn("2 merge exception(s)", detail)
        self.assertIn(("merge-exceptions", False), [(c[0], c[1]) for c in sentinel.run_checks(self.desk)])

    def test_merge_exceptions_older_than_the_window_do_not_count(self):
        """A rolling seven days, not a running total: two last month is not two this week."""
        exceptions.record(self.desk, "merge", "001 api one", "long ago")
        exceptions.record(self.desk, "merge", "002 api two", "also long ago")
        self.assertFalse(exceptions.check(self.desk, "merge")[1])
        later = now() + timedelta(days=30)
        self.assertTrue(exceptions.check(self.desk, "merge", at=later)[1])

    def test_settlement_failure_never_automatically_relaunches_worker(self):
        bell = ring.ring(self.desk, self.packet())
        with patch.object(watch.closetask_mod, "settle_bell", side_effect=OSError("disk fault")):
            with self.assertRaisesRegex(OSError, "disk fault"):
                watch.process_bell(self.desk, bell)
        with patch.object(watch, "launch_worker") as launch:
            with self.assertRaisesRegex(BellhopError, "unfinished launch"):
                watch.process_bell(self.desk, bell)
            launch.assert_not_called()

    def test_completed_bell_is_not_replayed(self):
        bell = ring.ring(self.desk, self.packet())
        watch.process_bell(self.desk, bell)
        with patch.object(watch, "launch_worker") as launch:
            with self.assertRaisesRegex(BellhopError, "completed"):
                watch.process_bell(self.desk, bell)
            launch.assert_not_called()

    def test_same_lane_contender_cannot_launch_a_second_worker(self):
        bell = ring.ring(self.desk, self.packet())
        entered, release = threading.Event(), threading.Event()
        launch = watch.launch_worker

        def blocked(*args, **kwargs):
            entered.set()
            if not release.wait(8):
                raise RuntimeError("test release missing")
            return launch(*args, **kwargs)

        pool = watch.WatchPool(self.desk)
        with patch.object(watch, "launch_worker", side_effect=blocked) as launched:
            try:
                pool.tick()
                self.assertTrue(entered.wait(5))
                with self.assertRaisesRegex(BellhopError, "busy"):
                    watch.process_bell(self.desk, bell)
                self.assertEqual(launched.call_count, 1)
            finally:
                release.set()
                pool.close()
        self.assertEqual(len(list(self.desk.bells_rung.glob("*.bell"))), 1)
