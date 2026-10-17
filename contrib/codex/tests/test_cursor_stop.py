"""Stopping a Cursor run: a signal, the shim killed outright, the turn ceiling and the end of every run stop Cursor,
what runs below it and what it leaves running (a cursorsandbox in a group of its own, a detached worker-server),
and nothing of another run's (README, "The Cursor backend"). The fake cursor-agent leaves both, as Cursor does."""
import json
import os
import signal
import subprocess
import sys
import time

from .codexkit import FAKES, alive
from .test_cursor import CursorScratch, status


def wait_for(what, seconds):
    """what()'s answer once it is true, or its last after `seconds`."""
    end = time.time() + seconds
    while time.time() < end:
        got = what()
        if got:
            return got
        time.sleep(0.05)
    return what()


class CursorStop(CursorScratch):
    def setUp(self):
        super().setUp()
        self.pids_file = self.root / "cursor-pids.json"
        self.pids = []
        self.addCleanup(self.kill, self.pids)

    def kill(self, pids):
        for pid in pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass

    def started(self, script="", **extra):
        """The shim, its fake Cursor leaving what Cursor leaves; this run's pids in self.pids once all started:
        Cursor, its command (`script`), the sandbox and its sleep, the worker-server."""
        p = self.start(self.worker_argv(), self.cursor_env(script, FAKE_CURSOR_LEAVE="1",
                                                           FAKE_CURSOR_PIDS=str(self.pids_file), **extra))
        self.addCleanup(self.reap, p)
        got = wait_for(lambda: self.pids_file.exists() and json.loads(self.pids_file.read_text()), 30)
        self.assertTrue(got, "the fake Cursor did not start")
        self.pids += [got["cursor"], *got["sandbox"], got["worker_server"], *([got["script"]] if got["script"] else [])]
        return p

    def reap(self, p):
        if p.poll() is None:
            p.kill()
        p.communicate()

    def left(self, seconds=0.0):
        """This run's processes still running, after up to `seconds` for them to go."""
        wait_for(lambda: not any(alive(x) for x in self.pids), seconds)
        return [x for x in self.pids if alive(x)]

    def test_sigterm_stops_cursor_and_what_it_leaves(self):
        # row 1: the shim's own handling (no watchdog), while Cursor runs a long command
        p = self.started("sleep 120", HANSA_CODEX_TEST_NO_WATCHDOG="1")
        self.assertEqual(self.left(), self.pids, "all running")
        os.kill(p.pid, signal.SIGTERM)
        out, _ = p.communicate(timeout=30)
        self.assertEqual((p.returncode, out.strip()), (128 + signal.SIGTERM, b""), "no answer, a killed process's exit")
        self.assertEqual(self.left(2), [], "Cursor, its command, the sandbox and its sleep, the worker-server")
        self.assert_folder_gone()

    def test_leftovers_stopped_before_the_answer(self):
        # row 2: the run ends by itself, leaving a worker-server and a cursorsandbox
        p = self.started()
        out, _ = p.communicate(timeout=60)
        left = self.left()  # as the answer is read
        run = json.loads(out)
        self.assertEqual(run["subtype"], "success", run["result"])
        self.assertEqual(left, [])
        self.assert_folder_gone()

    def test_shim_killed_outright(self):
        # row 3: the watchdog stops them and removes the run's folder
        p = self.started("sleep 120")
        os.kill(p.pid, signal.SIGKILL)
        p.wait(10)
        self.assertEqual(self.left(15), [], "Cursor, its command, the sandbox and its sleep, the worker-server")
        self.assertEqual(wait_for(lambda: not list(self.tmp.glob("cursor-*")), 15), True, "the run's folder removed")

    def test_shim_killed_outright_puts_the_boundary_back(self):
        # the hold keeps the main repository's git folders read-only while Cursor runs; when the shim is killed
        # outright, the watchdog puts the modes back and the lock goes with it
        common = self.root / "repo" / ".git"
        want = os.stat(common).st_mode & 0o222
        p = self.started("sleep 120")
        self.assertEqual(os.stat(common).st_mode & 0o222, 0, "held read-only while the run runs")
        self.assertTrue(list((common / "hansa-cursor-hold").glob("owner-*")), "the hold's lock, the run's share")
        os.kill(p.pid, signal.SIGKILL)
        p.wait(10)
        self.assertEqual(self.left(15), [], "Cursor, its command, the sandbox and its sleep, the worker-server")
        self.assertEqual(wait_for(lambda: os.stat(common).st_mode & 0o222, 15), want, "the modes put back")
        # the watchdog removes the lock after it has put the modes back
        self.assertTrue(wait_for(lambda: not (common / "hansa-cursor-hold").exists(), 15), "the lock removed")

    def test_turn_ceiling(self):
        # row 4: past --max-turns, Cursor is stopped; the changes made so far come back. Cursor's next step comes
        # a minute after the call past the ceiling, so that the stop is seen however slow the machine is
        done, began = self.root / "cursor-finished", time.time()
        run, code = self.run_cursor(argv=self.worker_argv(max_turns=3), FAKE_CURSOR_TOOLS="5", FAKE_CURSOR_HOLD="3:60",
                                    FAKE_CURSOR_STEPS="1", FAKE_CURSOR_DONE=str(done), FAKE_CURSOR_LEAVE="1",
                                    FAKE_CURSOR_PIDS=str(self.pids_file))
        self.assertLess(time.time() - began, 45, "the run ended at the stop, not at Cursor's next step")
        got = json.loads(self.pids_file.read_text())
        self.pids += [got["cursor"], *got["sandbox"], got["worker_server"]]
        self.assertEqual((code, run["subtype"], run["is_error"], run["num_turns"]), (1, "error_max_turns", True, 4))
        self.assertEqual(run["result"], "Doing step 3.", "the last assistant text")
        self.assertEqual(self.left(), [], "Cursor and what it left")
        self.assertFalse(done.exists(), "Cursor never got to its end")
        self.assertEqual(status(self.wt), ["?? step-0.txt", "?? step-1.txt", "?? step-2.txt", "?? step-3.txt"])
        self.assert_folder_gone()

    def test_another_runs_processes_left_running(self):
        # row 9: a sandbox under another HOME and a worker-server in another folder are not this run's
        other_home, other_ws = self.root / "other-home", self.root / "other-ws"
        other_ws.mkdir()
        box = subprocess.Popen([str(FAKES / "cursorsandbox"), "--policy",
                                str(other_home / ".cursor" / "sandbox-policies" / "x.sb"), "--", "sleep", "300"],
                               stdout=subprocess.PIPE, text=True, start_new_session=True)
        server = subprocess.Popen([sys.executable, str(FAKES / "cursor-agent"), "worker-server"], cwd=str(other_ws),
                                  start_new_session=True)
        others = [box.pid, int(box.stdout.readline()), server.pid]
        box.stdout.close()
        self.addCleanup(lambda: (self.kill(others), box.wait(), server.wait()))
        p = self.started()
        out, _ = p.communicate(timeout=60)
        self.assertEqual(json.loads(out)["subtype"], "success")
        self.assertEqual(self.left(), [], "this run's stopped")
        self.assertEqual([x for x in others if not alive(x)], [], "another run's left running")
