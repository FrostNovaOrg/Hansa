"""A lane added while the watchdog is up, without restarting it.

The resident watchdog read `bellhop.json` once, at start. A lane added to it afterwards
did not exist as far as the running process was concerned: its bells sat in
`bells/pending` and the board showed them waiting for a worker, which was true and
useless. On one desk two packets waited hours that way, and the fix was a service
restart nobody knew was needed.

The reload is deliberately narrow. Lanes are re-read; the worker limit, the thread pool
and the intervals are not, because those were fixed when the pool was built and changing
one mid-tick would mean two answers inside one tick. The log line says which lanes moved
and names any other setting that will need a restart, so "reloaded" never quietly means
"partly reloaded".
"""
import json
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import test_gaps as fixtures
from bellhop import daemon, ring, watch
from bellhop.config import Desk
from bellhop.util import read, read_json, write


class LaneReloadTest(unittest.TestCase):
    """The reader on its own: what it notices and what it refuses to do about it."""

    setUp = fixtures.GapTest.setUp
    tearDown = fixtures.GapTest.tearDown

    def cfg_on_disk(self):
        return read_json(self.desk.path("bellhop.json"))

    def rewrite(self, cfg):
        """Write the file the way an editor would, and make sure the stat moves."""
        p = self.desk.path("bellhop.json")
        before = p.stat().st_mtime_ns
        write(p, json.dumps(cfg, indent=2))
        while p.stat().st_mtime_ns == before:          # a coarse clock, not a spin
            time.sleep(0.005)
            p.touch()

    def test_an_untouched_file_is_not_parsed_again(self):
        reload = watch.LaneReload(self.desk)
        self.assertEqual(reload.tick(), "")
        self.assertEqual(reload.tick(), "")

    def test_a_new_lane_is_named_and_is_usable_at_once(self):
        reload = watch.LaneReload(self.desk)
        cfg = self.cfg_on_disk()
        cfg["lanes"]["docs"] = dict(cfg["lanes"]["api"])
        self.rewrite(cfg)
        self.assertEqual(reload.tick(), "lanes reloaded: +docs -none")
        self.assertEqual(self.desk.lane("docs")["name"], "docs")
        self.assertEqual(reload.tick(), "")

    def test_a_removed_lane_and_an_edited_lane_are_two_different_words(self):
        reload = watch.LaneReload(self.desk)
        cfg = self.cfg_on_disk()
        cfg["lanes"]["docs"] = dict(cfg["lanes"]["api"])
        self.rewrite(cfg)
        self.assertEqual(reload.tick(), "lanes reloaded: +docs -none")
        cfg["lanes"].pop("docs")
        cfg["lanes"]["api"]["check_cmd"] = "./check.sh --strict"
        self.rewrite(cfg)
        self.assertEqual(reload.tick(), "lanes reloaded: +none -docs ~api")
        self.assertEqual(self.desk.lane("api")["check_cmd"], "./check.sh --strict")

    def test_a_setting_that_is_not_a_lane_says_it_needs_a_restart(self):
        """The honest half of a narrow reload: name what did not reload."""
        reload = watch.LaneReload(self.desk)
        before = self.desk.cfg.get("max_parallel_workers")
        cfg = self.cfg_on_disk()
        cfg["max_parallel_workers"] = (before or 1) + 3
        self.rewrite(cfg)
        self.assertIn("restart to pick up: max_parallel_workers", reload.tick())
        self.assertEqual(self.desk.cfg.get("max_parallel_workers"), before,
                         "a setting the running pool cannot honour must not be swapped in behind it")

    def test_a_half_written_file_keeps_the_lanes_already_loaded(self):
        """Never raises: this runs inside the loop, and an editor saves in two steps."""
        reload = watch.LaneReload(self.desk)
        lanes_before = dict(self.desk.cfg["lanes"])
        write(self.desk.path("bellhop.json"), '{"lanes": {"api": ')
        line = reload.tick()
        self.assertIn("unreadable", line)
        self.assertEqual(self.desk.cfg["lanes"], lanes_before)
        write(self.desk.path("bellhop.json"), json.dumps({"worktrees_dir": "worktrees"}, indent=2))
        self.assertIn("no lanes table", reload.tick())
        self.assertEqual(self.desk.cfg["lanes"], lanes_before)


class LaneReloadInTheLoopTest(unittest.TestCase):
    """The loop itself: a lane declared mid-run answers its own bell."""

    setUp = fixtures.GapTest.setUp
    tearDown = fixtures.GapTest.tearDown
    packet = fixtures.GapTest.packet

    def wait_until(self, predicate, seconds=20):
        until = time.monotonic() + seconds
        while time.monotonic() < until:
            if predicate():
                return True
            time.sleep(0.05)
        return False

    def watchdog_log(self):
        p = self.desk.bells_logs / "watchdog.log"
        return read(p) if p.exists() else ""

    def test_a_lane_added_mid_loop_is_picked_up_without_a_restart(self):
        self.assertNotIn("docs", self.desk.cfg["lanes"], "the lane must not exist when the loop starts")
        loop_desk = Desk(self.desk_dir, json.loads(read(self.desk_dir / "bellhop.json")))
        thread = threading.Thread(
            target=lambda: watch.watch_loop(loop_desk, interval=0.05), daemon=True)
        with patch("bellhop.watch.signal"):            # signal handlers need the main thread
            thread.start()
            try:
                self.assertTrue(self.wait_until(lambda: self.desk.path(daemon.HEARTBEAT).exists()),
                                "the loop never started")
                # the coordinator's own process declares the lane and rings for it
                cfg = json.loads(read(self.desk_dir / "bellhop.json"))
                cfg["lanes"]["docs"] = dict(cfg["lanes"]["api"])
                write(self.desk_dir / "bellhop.json", json.dumps(cfg, indent=2))
                self.desk.cfg["lanes"]["docs"] = cfg["lanes"]["docs"]
                p = self.packet("002", "Task for a lane that did not exist a minute ago")
                write(p, read(p).replace("lane: api", "lane: docs"))
                ring.ring(self.desk, p, commit=False)
                picked = self.wait_until(
                    lambda: (self.desk.receipts / "002.meta.json").exists())
            finally:
                watch._stop = True
                thread.join(30)
        log = self.watchdog_log()
        self.assertIn("lanes reloaded: +docs -none", log)
        self.assertTrue(picked, "the bell in the new lane was never answered:\n" + log)
        self.assertEqual(json.loads(read(self.desk.receipts / "002.meta.json"))["lane"], "docs")


if __name__ == "__main__":
    unittest.main()
