"""Getting unstuck: lock timeouts, hook paths, stale markers, bells nobody can answer.

Every case here is a pair of states that look the same from outside and need different
hands: a lock held by a live process and a lock held by a corpse, a hook that logged
nothing because nothing was said and one that logged nothing because it was waiting, a
bell being retried and a bell that will never be answered, a task whose packet binding
is missing because the desk was upgraded and one whose packet was edited after the fact.
"""
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import test_gaps as fixtures
from bellhop import desk_ops, hand, intake, lanes, locking, packet as packet_mod
from bellhop import ring, sentinel, watch
from bellhop.util import BellhopError, read, read_json, write, write_json

ROOT = Path(__file__).resolve().parent.parent

HOLDER = '''import pathlib, sys, time
sys.path.insert(0, {root!r})
from bellhop.locking import file_lock
lock, ready, release = (pathlib.Path(p) for p in sys.argv[1:4])
with file_lock(lock):
    ready.touch()
    until = time.monotonic() + 30
    while not release.exists() and time.monotonic() < until:
        time.sleep(0.02)
'''


class RecoveryTest(unittest.TestCase):
    setUp = fixtures.GapTest.setUp
    tearDown = fixtures.GapTest.tearDown
    packet = fixtures.GapTest.packet

    # -- helpers ----------------------------------------------------------

    def hold_the_desk_lock(self):
        """Another process holds the desk lock until `release` is touched."""
        script = self.tmp / "holder.py"
        write(script, HOLDER.format(root=str(ROOT)))
        ready, release = self.tmp / "ready", self.tmp / "release"
        proc = subprocess.Popen([sys.executable, str(script),
                                 str(locking.desk_lock_path(self.desk)), str(ready), str(release)])
        until = time.monotonic() + 15
        while not ready.exists() and time.monotonic() < until:
            if proc.poll() is not None:
                self.fail("the holder process exited before it took the lock")
            time.sleep(0.02)
        self.assertTrue(ready.exists(), "the holder never took the lock")
        def stop():
            # tearDown removes the temp tree before cleanups run, so the release file
            # may already be gone; the point is only that the holder does not outlive
            # the test.
            try:
                release.touch()
                proc.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                proc.kill()
                proc.wait(timeout=5)
        self.addCleanup(stop)
        return proc

    def sentinel_log(self):
        p = self.desk.state / "sentinel.log"
        return read(p).splitlines() if p.exists() else []

    # -- 1. the lock has a clock ------------------------------------------

    def test_a_held_lock_times_out_and_names_who_has_it(self):
        proc = self.hold_the_desk_lock()
        with self.assertRaises(BellhopError) as caught:
            with locking.file_lock(locking.desk_lock_path(self.desk), timeout=0.4):
                self.fail("the lock was taken while another process held it")
        message = str(caught.exception)
        self.assertIn("waited 0.4s", message)
        self.assertIn(f"pid {proc.pid}", message, message)

    def test_the_lock_is_free_again_afterwards(self):
        """Negative control: the same call goes straight through with nobody holding it."""
        with locking.file_lock(locking.desk_lock_path(self.desk), timeout=0.4):
            pass
        with locking.desk_lock(self.desk):
            pass

    def test_a_hook_path_goes_ahead_unlocked_and_leaves_one_line(self):
        """`intake.log` and `hand.note` run inside the coordinator's turn. A stuck
        holder may cost them the lock; it may not cost them the turn."""
        self.hold_the_desk_lock()
        started = time.monotonic()
        kind, number = intake.log(self.desk, "the principal said something")
        hand.note(self.desk, "written: something")
        waited = time.monotonic() - started
        self.assertEqual(kind, intake.LOGGED)
        self.assertLess(waited, 3 * locking.HOOK_LOCK_TIMEOUT_S,
                        "the hook paths waited far longer than their budget")
        self.assertIn("the principal said something", read(intake.path(self.desk)))
        self.assertIn("written: something", read(hand.ledger_path(self.desk)))
        lines = [l for l in self.sentinel_log() if " LOCK " in l]
        self.assertEqual(len(lines), 2, lines)
        self.assertTrue(any("intake.log" in l for l in lines), lines)
        self.assertTrue(any("hand.note" in l for l in lines), lines)

    def test_a_hook_path_writes_no_lock_line_when_the_lock_is_free(self):
        """Negative control: the line means contention, not "a hook ran"."""
        intake.log(self.desk, "a quiet prompt")
        self.assertEqual([l for l in self.sentinel_log() if " LOCK " in l], [])

    # -- 2. the day's close survives a sentinel with no seal check --------

    def test_the_daily_close_reports_a_missing_seal_check_instead_of_raising(self):
        checks = [("card", True, "ready=yes"), ("bells", True, "0 pending")]
        with patch.object(desk_ops.sentinel_mod, "run_sentinel",
                          return_value=(True, "all green\n", checks)):
            summary = desk_ops.close(self.desk)
        self.assertIn("Seals: not reported by this sentinel run", summary)

    # -- 3. a new file is readable by whoever reads the desk ---------------

    def test_a_new_file_is_written_0644(self):
        target = self.tmp / "fresh" / "note.md"
        write(target, "one line\n")
        self.assertEqual(target.stat().st_mode & 0o777, 0o644, oct(target.stat().st_mode))
        target.chmod(0o600)
        write(target, "rewritten\n")
        self.assertEqual(target.stat().st_mode & 0o777, 0o600,
                         "a rewrite must keep the mode the file already had")

    # -- 4. a stale launch marker has a command ---------------------------

    def marker(self, name="20260101-000000-001.bell", lane="api"):
        path = lanes.started_dir(self.desk) / (name + ".json")
        write_json(path, {"bell": name, "lane": lane, "started_at": "2026-09-11T10:00:00+0800"})
        return path

    def test_lane_recover_clears_a_stale_marker_and_logs_it(self):
        path = self.marker()
        report = lanes.lane_recover(self.desk, "api")
        self.assertEqual(report["cleared"], 1)
        self.assertFalse(path.exists())
        log = read(self.desk.bells_logs / "watchdog.log")
        self.assertIn("RECOVER", log)
        self.assertIn("20260101-000000-001.bell", log)
        # and it is honest about finding nothing the second time
        self.assertEqual(lanes.lane_recover(self.desk, "api")["cleared"], 0)

    def test_lane_recover_refuses_while_the_lane_claim_is_held(self):
        self.marker()
        with lanes.lane_lock(self.desk, "api"):
            with self.assertRaisesRegex(lanes.LaneBlocked, "may be running"):
                lanes.lane_recover(self.desk, "api")
            report = lanes.lane_recover(self.desk, "api", force=True)
        self.assertEqual(report["cleared"], 1)
        self.assertIn("the lane claim was still held", read(self.desk.bells_logs / "watchdog.log"))

    def test_lane_recover_dry_run_changes_nothing(self):
        path = self.marker()
        report = lanes.lane_recover(self.desk, "api", dry_run=True)
        self.assertEqual(report["cleared"], 0)
        self.assertTrue(path.exists())
        self.assertIn("its bell is in neither", report["steps"][0])

    # -- 5. a bell nobody can answer stops being rung ----------------------

    def test_a_bell_that_keeps_failing_is_parked_and_the_sentinel_says_so(self):
        bell = ring.ring(self.desk, self.packet())
        (self.desk.inbox / "api" / "001-note.md").unlink()  # the packet is gone: it cannot succeed
        limit = watch.bell_failure_limit(self.desk)
        self.assertEqual(limit, 3)
        for attempt in range(1, limit):
            with self.assertRaises(BellhopError):
                watch.watch_once(self.desk)
            self.assertTrue(bell.exists(), "parked too early")
            self.assertEqual(watch.bell_failures(self.desk, bell), attempt)
        with self.assertRaisesRegex(BellhopError, "parked"):
            watch.watch_once(self.desk)
        self.assertFalse(bell.exists())
        self.assertTrue((watch.dead_bells_dir(self.desk) / bell.name).exists())
        self.assertEqual(watch.pending_bells(self.desk), [])
        self.assertTrue(any("parked" in l for l in self.sentinel_log()), self.sentinel_log())
        name, ok, detail = sentinel.check_dead_bells(self.desk)
        self.assertFalse(ok, detail)
        self.assertIn(bell.name, detail)

    def test_a_bell_that_is_answered_forgets_its_earlier_failures(self):
        """Consecutive, not cumulative: a bell that works is not two thirds of the way
        to being parked."""
        bell = ring.ring(self.desk, self.packet())
        watch.record_bell_failure(self.desk, bell, RuntimeError("one bad tick"))
        watch.record_bell_failure(self.desk, bell, RuntimeError("another"))
        self.assertEqual(watch.bell_failures(self.desk, bell), 2)
        watch.clear_bell_failures(self.desk, bell)
        self.assertEqual(watch.bell_failures(self.desk, bell), 0)
        self.assertTrue(bell.exists())

    # -- 6. rebinding an upgraded desk's in-flight packets -----------------

    def bound_task(self, pid="001"):
        p = self.packet(pid)
        bell = ring.ring(self.desk, p)
        write_json(self.desk.receipts / f"{pid}.meta.json", {"id": pid, "lane": "api"})
        return p, bell

    def test_rebind_takes_the_digest_from_the_bell(self):
        p, bell = self.bound_task()
        rows = packet_mod.rebind(self.desk, "001")
        self.assertEqual([r["status"] for r in rows], ["rebound"])
        meta = read_json(self.desk.receipts / "001.meta.json")
        self.assertEqual(meta["packet_sha256"], read_json(bell)["sha256"])
        self.assertIn("pending bell", meta["packet_sha256_source"])
        self.assertTrue(any("REBIND 001" in l for l in self.sentinel_log()), self.sentinel_log())
        # the binding now verifies, which is the whole point
        packet_mod.verify_worker_packet(self.desk, packet_mod.parse_packet(p))
        # and a second run leaves it alone rather than rewriting it
        self.assertEqual([r["status"] for r in packet_mod.rebind(self.desk, "001")], ["already"])

    def test_rebind_refuses_a_packet_that_changed_after_its_bell(self):
        p, _ = self.bound_task()
        write(p, read(p) + "\nand one more thing the worker never saw.\n")
        rows = packet_mod.rebind(self.desk, "001")
        self.assertEqual(rows[0]["status"], "refused")
        self.assertIn("changed packet, not a missing binding", rows[0]["note"])
        self.assertNotIn("packet_sha256", read_json(self.desk.receipts / "001.meta.json"))

    def test_rebind_says_so_when_no_bell_recorded_the_digest(self):
        write_json(self.desk.receipts / "077.meta.json", {"id": "077", "lane": "api"})
        rows = packet_mod.rebind(self.desk, "077")
        self.assertEqual(rows[0]["status"], "no-evidence")
        self.assertIn("do not write a hash by hand", rows[0]["note"])

    def test_rebind_all_reports_every_task_it_looked_at(self):
        self.bound_task("001")
        self.bound_task("002")
        write_json(self.desk.receipts / "003.meta.json", {"id": "003", "lane": "api"})
        rows = packet_mod.rebind(self.desk, every=True, dry_run=True)
        self.assertEqual({r["task"] for r in rows}, {"001", "002", "003"})
        self.assertEqual([r["status"] for r in rows], ["would rebind", "would rebind", "no-evidence"])
        self.assertNotIn("packet_sha256", read_json(self.desk.receipts / "001.meta.json"))
        with self.assertRaisesRegex(BellhopError, "one task id, or --all"):
            packet_mod.rebind(self.desk, "001", every=True)


if __name__ == "__main__":
    unittest.main()
