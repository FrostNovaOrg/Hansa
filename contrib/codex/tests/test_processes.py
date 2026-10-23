"""The turn ceiling stops codex; hansa's kill at its time ceiling reaches srt, codex and what codex started."""
import os
import signal
import time

from .codexkit import Scratch, alive, hansa_run


class TurnCeiling(Scratch):
    def test_more_items_than_max_turns_stops_codex(self):
        done = self.root / "codex-finished"
        t0 = time.time()
        # many_items_long.jsonl: many_items.jsonl with 150 more lines before its end, 30 s more at 0.2 s a line,
        # so codex cannot reach its end before the turn ceiling stops it, however slow the shim is to react
        run, code = self.run_worker("many_items_long.jsonl", argv=self.worker_argv(max_turns=3),
                                    FAKE_CODEX_DELAY="0.2", FAKE_CODEX_DONE=str(done))
        self.assertLess(time.time() - t0, 60, "a hang guard only: that codex was stopped, not waited for, is below")
        self.assertEqual(code, 1)
        self.assertEqual((run["subtype"], run["is_error"], run["num_turns"]), ("error_max_turns", True, 4))
        self.assertEqual(run["result"], "Done step 2.")
        pid, end = self.codex_called()["pid"], time.time() + 10
        while alive(pid) and time.time() < end:
            time.sleep(0.1)
        self.assertFalse(alive(pid), "codex was stopped")
        self.assertFalse(done.exists(), "codex never got to its end")
        # hansa's finish: error_max_turns is its turn ceiling, whatever the rest says
        self.assertEqual(run.get("subtype"), "error_max_turns")


class Kill(Scratch):
    def started(self, **extra):
        p = self.start(self.worker_argv(), self.env(FAKE_CODEX_HANG="1", **extra))
        self.addCleanup(self.reap, p)
        end = time.time() + 20
        while time.time() < end:
            try:
                rec = self.codex_called()
                if rec.get("children"):
                    return p, [self.srt_called()["pid"], rec["pid"], *rec["children"]]
            except (OSError, ValueError):
                pass
            time.sleep(0.1)
        self.fail("the fake codex did not start")

    def reap(self, p):
        for pid in getattr(self, "pids", []):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        if p.poll() is None:
            p.kill()
        p.communicate()

    def wait_gone(self, pids, seconds):
        end = time.time() + seconds
        while time.time() < end and any(alive(x) for x in pids):
            time.sleep(0.1)
        return [x for x in pids if alive(x)]

    def test_hansas_group_kill_reaches_the_children(self):
        p, self.pids = self.started()
        self.assertTrue(all(alive(x) for x in self.pids))
        hansa_run.stop_group(p.pid)  # what hansa does at a run's time ceiling: SIGTERM, 5 s, SIGKILL, to the group
        p.wait(10)
        self.assertEqual(self.wait_gone(self.pids, 3), [], "codex, its child and a grandchild in its own session")

    def test_shim_killed_outright_still_stops_them(self):
        p, self.pids = self.started()
        os.kill(p.pid, signal.SIGKILL)  # no chance to pass anything on: the watchdog in srt's group does
        p.wait(10)
        self.assertEqual(self.wait_gone(self.pids, 10), [])

    def test_sigterm_to_the_shim_alone_with_no_watchdog(self):
        # the shim's own handler, nothing else, passes the signal on: srt does not, and no watchdog runs
        p, self.pids = self.started(HANSA_CODEX_TEST_NO_WATCHDOG="1")
        os.kill(p.pid, signal.SIGTERM)
        p.wait(10)
        self.assertEqual(p.returncode, 128 + signal.SIGTERM)
        self.assertEqual(self.wait_gone(self.pids, 2), [], "srt, codex, its child and a grandchild")

    def test_signal_between_srts_start_and_run_proc(self):
        # SIGTERM lands while the shim pauses after Popen returns, before Run.proc is set: held back till then
        marker = self.root / "srt-started"
        p = self.start(self.worker_argv(), self.env(FAKE_CODEX_HANG="1", HANSA_CODEX_TEST_NO_WATCHDOG="1",
                                                    HANSA_CODEX_TEST_PAUSE=str(marker)))
        self.addCleanup(self.reap, p)
        end = time.time() + 20
        while time.time() < end and not (marker.exists() and marker.read_text()):
            time.sleep(0.01)
        self.pids = [int(marker.read_text())]
        os.kill(p.pid, signal.SIGTERM)
        p.wait(10)
        self.assertEqual(p.returncode, 128 + signal.SIGTERM)
        time.sleep(1)  # codex, had srt lived on, would have started by now
        if self.codex_record.exists():
            rec = self.codex_called()
            self.pids += [rec["pid"], *rec.get("children", [])]
        self.assertEqual(self.wait_gone(self.pids, 2), [], "srt and whatever it started")
