"""The streaming worker runner and the opt-in observation around it.

No model, no network: the "workers" here are short python programs that print, sleep,
or refuse to die, which is all the runner is being asked about.
"""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bellhop import health, sentinel  # noqa: E402
from bellhop.config import Desk  # noqa: E402
from bellhop.util import BellhopError, read, write  # noqa: E402
from bellhop.worker_session import run_worker  # noqa: E402


def python(source):
    return [sys.executable, "-c", source]


def alive(pid):
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


class FakeClock:
    """A clock a test moves by hand, so an idle test does not have to be a slow one."""

    def __init__(self):
        self.value = 1000.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class RunWorkerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bellhop-worker-session.")).resolve()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_worker(self, source, timeout_s=30, observation=None):
        return run_worker(python(source), self.tmp, dict(os.environ), timeout_s, observation)

    def test_it_returns_what_run_session_returns(self):
        session = self.run_worker("import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)")
        self.assertEqual(session["stdout"], "out\n")
        self.assertEqual(session["stderr"], "err\n")
        self.assertEqual(session["exit_code"], 3)
        self.assertFalse(session["timed_out"])
        self.assertGreaterEqual(session["duration_s"], 0)

    def test_a_character_split_across_two_reads_survives(self):
        """Decoding each block as it arrives would cut a code point in half."""
        session = self.run_worker(
            "import sys, time\n"
            "sys.stdout.buffer.write(b'\\xe4\\xb8'); sys.stdout.buffer.flush(); time.sleep(0.3)\n"
            "sys.stdout.buffer.write(b'\\x96\\n'); sys.stdout.buffer.flush()\n")
        self.assertEqual(session["stdout"], "世\n")

    def test_carriage_returns_are_normalized_like_text_mode(self):
        session = self.run_worker("import sys; sys.stdout.buffer.write(b'a\\r\\nb\\rc\\n')")
        self.assertEqual(session["stdout"], "a\nb\nc\n")

    def test_a_worker_past_its_clock_is_killed_with_its_children(self):
        pid_file = self.tmp / "child.pid"
        session = self.run_worker(
            "import subprocess, sys, time\n"
            f"child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
            f"open({str(pid_file)!r}, 'w').write(str(child.pid))\n"
            "print('working', flush=True)\n"
            "time.sleep(30)\n", timeout_s=1)
        self.assertTrue(session["timed_out"])
        self.assertEqual(session["exit_code"], 124)
        self.assertIn("working", session["stdout"])
        pid = int(read(pid_file).strip())
        for _ in range(50):  # the group is signalled, then reaped
            if not alive(pid):
                break
            time.sleep(0.1)
        self.assertFalse(alive(pid), "the worker's child outlived the killed session")

    def test_a_worker_that_launches_nothing_is_not_a_crash(self):
        session = run_worker(["/nonexistent/worker"], self.tmp, dict(os.environ), 5)
        self.assertEqual(session["exit_code"], 127)
        self.assertIn("could not launch the worker", session["stderr"])

    def test_an_observer_that_throws_stops_observing_and_the_worker_finishes(self):
        seen = []

        class Broken:
            def activity(self, stream, size):
                seen.append((stream, size))
                raise OSError("no space left on device")

            def check(self, force=False):
                pass

        errors = []
        session = run_worker(python("print('hello')"), self.tmp, dict(os.environ), 30,
                             Broken(), on_error=errors.append)
        self.assertEqual(session["stdout"], "hello\n")
        self.assertEqual(session["exit_code"], 0)
        self.assertFalse(session["observed"])
        self.assertEqual(len(errors), 1, "the failure is reported once, not per block")
        self.assertEqual(len(seen), 1, "and nothing is asked of it afterwards")


class HealthDeskCase(unittest.TestCase):
    """A desk made from the template, no git and no model."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bellhop-health.")).resolve()
        self.root = self.tmp / "desk"
        shutil.copytree(ROOT / "template", self.root)
        (self.root / "bellhop.example.json").rename(self.root / "bellhop.json")
        self.desk = self.make_desk(enabled=True, idle_s=100, soft_runtime_s=500, interval_s=10)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make_desk(self, **worker_health):
        cfg = json.loads(read(self.root / "bellhop.json"))
        cfg["worker_health"] = worker_health
        write(self.root / "bellhop.json", json.dumps(cfg, indent=2))
        return Desk(self.root, cfg)


class OptionsTest(HealthDeskCase):
    def test_a_desk_that_says_nothing_gets_the_defaults_and_is_off(self):
        desk = Desk(self.root, {})
        self.assertEqual(health.options(desk), health.DEFAULTS)
        self.assertIsNone(health.observation(desk, "001"))

    def test_a_setting_that_cannot_be_a_threshold_is_refused(self):
        for bad in ({"nope": 1}, {"enabled": "yes"}, {"idle_s": 0}, {"idle_s": -5},
                    {"idle_s": "600"}, {"interval_s": True}, {"soft_runtime_s": float("inf")}):
            with self.subTest(bad=bad):
                with self.assertRaises(BellhopError):
                    health.options(Desk(self.root, {"worker_health": bad}))
        with self.assertRaises(BellhopError):
            health.options(Desk(self.root, {"worker_health": []}))


class ObservationTest(HealthDeskCase):
    def observe(self, task="001"):
        self.clock = FakeClock()
        return health.Observation(self.desk, task, health.options(self.desk), clock=self.clock)

    def kinds(self):
        return [row["kind"] for row in health.entries(self.desk)]

    def test_silence_warns_once_per_episode_and_output_ends_the_episode(self):
        watched = self.observe()
        self.assertEqual(self.kinds(), ["started"])
        self.clock.advance(50)
        watched.check()
        self.assertEqual(self.kinds(), ["started"], "half the idle limit is not an alert")
        self.clock.advance(60)
        watched.check()
        watched.check(force=True)  # a second look at the same silence says nothing new
        self.assertEqual(self.kinds(), ["started", "idle_warning"])
        watched.activity("stdout", 40)
        self.assertEqual(self.kinds()[-1], "recovered")
        # a new silence after real output is a new episode, and warns again
        self.clock.advance(120)
        watched.check()
        self.assertEqual(self.kinds()[-1], "idle_warning")
        self.assertEqual(len(health.alerts(self.desk)), 2)
        self.assertEqual([row["episode"] for row in health.alerts(self.desk)], [1, 2])

    def test_a_long_run_warns_once_and_never_kills_anything(self):
        watched = self.observe("002")
        for _ in range(6):
            self.clock.advance(100)
            watched.activity("stdout", 10)  # never idle: this is the runtime alert alone
            watched.check()
        kinds = [k for k in self.kinds() if k == "runtime_warning"]
        self.assertEqual(kinds, ["runtime_warning"])
        summary = watched.finish(0)
        self.assertEqual(summary["bytes"], 60)
        self.assertEqual(summary["alerts"], 1)
        self.assertEqual(self.kinds()[-1], "finished")

    def test_the_check_interval_is_respected_unless_forced(self):
        watched = self.observe("003")
        self.clock.advance(200)
        watched.check()
        self.assertEqual(self.kinds()[-1], "idle_warning")
        health.ack(self.desk, health.alerts(self.desk)[0]["event_id"])
        self.clock.advance(5)  # inside interval_s: nothing is measured
        watched.check()
        self.assertEqual(self.kinds()[-1], "ack")

    def test_zero_sized_activity_is_not_activity(self):
        watched = self.observe("004")
        watched.activity("stdout", 0)
        self.assertEqual(watched.total(), 0)
        self.assertEqual(watched.blocks, 0)


class RecordTest(HealthDeskCase):
    def test_an_alert_stays_open_until_it_is_acknowledged_by_id(self):
        event = health.record(self.desk, "007", "idle_warning", "no output for 700s", episode=1)
        self.assertEqual([row["event_id"] for row in health.alerts(self.desk)], [event["event_id"]])
        health.ack(self.desk, event["event_id"])
        self.assertEqual(health.alerts(self.desk), [])
        # acknowledging what is not an open alert says so rather than pretending
        with self.assertRaises(BellhopError):
            health.ack(self.desk, event["event_id"])
        with self.assertRaises(BellhopError):
            health.ack(self.desk, "deadbeefdead")

    def test_a_half_written_line_does_not_stop_the_reader(self):
        health.record(self.desk, "007", "idle_warning", "no output for 700s", episode=1)
        with health.events_path(self.desk).open("a", encoding="utf-8") as fh:
            fh.write('{"kind": "idle_warn')  # a process killed mid-append
        health.record(self.desk, "008", "started")
        self.assertEqual([row["kind"] for row in health.entries(self.desk)],
                         ["idle_warning", "started"])
        self.assertEqual(len(health.alerts(self.desk)), 1)

    def test_an_invented_kind_is_refused(self):
        with self.assertRaises(BellhopError):
            health.record(self.desk, "007", "everything_is_fine")

    def test_status_reads_back_settings_alerts_and_history(self):
        health.record(self.desk, "007", "idle_warning", "quiet", episode=1)
        health.record(self.desk, "008", "started")
        report = health.status(self.desk)
        self.assertTrue(report["enabled"])
        self.assertEqual(report["settings"]["idle_s"], 100)
        self.assertEqual(len(report["alerts"]), 1)
        self.assertEqual(report["total"], 2)
        self.assertEqual(len(health.status(self.desk, task="008")["recent"]), 1)


class SentinelCheckTest(HealthDeskCase):
    def names(self, desk):
        return [name for name, _ok, _detail in sentinel.run_checks(desk, persist=False)]

    def test_an_unacknowledged_alert_is_red_until_it_is_read(self):
        event = health.record(self.desk, "007", "idle_warning", "no output for 700s", episode=1)
        name, ok, detail = sentinel.check_worker_health(self.desk)
        self.assertEqual(name, "worker-health")
        self.assertFalse(ok)
        self.assertIn("007", detail)
        self.assertIn(event["event_id"], detail)
        self.assertIn("worker-health", self.names(self.desk))
        health.ack(self.desk, event["event_id"])
        self.assertTrue(sentinel.check_worker_health(self.desk)[1])

    def test_a_setting_the_desk_got_wrong_is_a_red_line_not_a_crash(self):
        broken = self.make_desk(enabled=True, idle_s="ten minutes")
        checks = {name: (ok, detail) for name, ok, detail in sentinel.run_checks(broken, persist=False)}
        self.assertIn("worker-health", checks)
        self.assertFalse(checks["worker-health"][0])
        self.assertIn("idle_s", checks["worker-health"][1])

    def test_a_desk_that_did_not_ask_for_it_does_not_get_the_check(self):
        off = self.make_desk(enabled=False)
        self.assertNotIn("worker-health", self.names(off))
        # control: the same desk with it on carries the check, green
        on = self.make_desk(enabled=True)
        self.assertIn("worker-health", self.names(on))


if __name__ == "__main__":
    unittest.main()
