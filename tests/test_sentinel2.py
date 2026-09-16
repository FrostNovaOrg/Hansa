"""The sentinel's second layer: the log line, the wake brake, the stop switch, the
daily clear window, idle compaction, and whether the resident services start at boot.

Every gate gets a positive case and a negative control, and the negative control proves
the gate actually held: no fake command was invoked, no marker was written. A gate that
declined and a gate that fired and failed look the same in a line that only says "not
sent", which is exactly the confusion these mechanisms exist to remove.

The fake commands are small shell scripts written into the sandbox. Each one appends a
line to its own log file, so "did it run" is a fact on disk rather than a mock's memory.
No model is called and no service is loaded.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bellhop import daemon as daemon_mod  # noqa: E402
from bellhop import sentinel as sentinel_mod  # noqa: E402
from bellhop import stopdispatch as stop_mod  # noqa: E402
from bellhop.config import Desk  # noqa: E402
from bellhop.util import BellhopError, now, read, read_json, ts, write  # noqa: E402


class SentinelCase(unittest.TestCase):
    """A desk made from the template in a tempfile sandbox. No git, no network, no model."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bellhop-sentinel-test.")).resolve()
        self.root = self.tmp / "desk"
        shutil.copytree(ROOT / "template", self.root)
        (self.root / "bellhop.example.json").rename(self.root / "bellhop.json")
        self.cfg = json.loads(read(self.root / "bellhop.json"))
        self.desk = Desk(self.root, self.cfg)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- sandbox helpers ---------------------------------------------------
    def set_cfg(self, **fields):
        self.cfg.update(fields)
        write(self.root / "bellhop.json", json.dumps(self.cfg, indent=2))
        self.desk = Desk(self.root, self.cfg)
        return self.desk

    def fake_cmd(self, name, exit_code=0):
        """A command that records every invocation and exits with a fixed code."""
        log = self.tmp / f"{name}.calls"
        script = self.tmp / f"{name}.sh"
        write(script, f"#!/bin/sh\necho \"{name} $*\" >> '{log}'\nexit {exit_code}\n")
        script.chmod(0o755)
        return str(script), log

    def probe_cmd(self, busy=True):
        """A busy probe whose answer is a file: `0` means busy, `1` means idle."""
        answer = self.tmp / "probe.answer"
        write(answer, "0\n" if busy else "1\n")
        log = self.tmp / "probe.calls"
        script = self.tmp / "probe.sh"
        write(script, f"#!/bin/sh\necho probe >> '{log}'\nexit \"$(cat '{answer}')\"\n")
        script.chmod(0o755)
        self.probe_answer = answer
        return str(script), log

    def set_busy(self, busy):
        write(self.probe_answer, "0\n" if busy else "1\n")

    def calls(self, log):
        return read(log).splitlines() if log.exists() else []

    def make_red(self, marker="a"):
        """A card that is red for a stated reason, with `next` as the board's fingerprint knob."""
        write(
            self.root / "state" / "next.md",
            "# Dispatch card\n\nready: no\nnext: %s\nlane: \nreason: \nqueue: 0 (none)\n"
            "refreshed: 2026-01-01T00:00:00+0000\nby: test\n\n---\nKeys only.\n" % marker,
        )

    def log_lines(self):
        p = self.root / "state" / "sentinel.log"
        return read(p).splitlines() if p.exists() else []

    def at(self, hour, minute=0, days=0):
        return (now() + timedelta(days=days)).replace(hour=hour, minute=minute, second=0, microsecond=0)

    def run_sentinel(self, **kw):
        return sentinel_mod.sentinel(self.desk, **kw)


# --------------------------------------------------------------------------
class LogLineTest(SentinelCase):
    """"The sentinel did not run" and "nothing happened" must not look the same."""

    def test_a_green_run_logs_one_line_too(self):
        green, _text = self.run_sentinel(wake=False)
        self.assertTrue(green)
        lines = self.log_lines()
        self.assertEqual(len(lines), 1, lines)
        self.assertIn(" GREEN ", lines[0])
        self.assertIn("checks", lines[0])
        self.assertIn("all green", lines[0])

    def test_one_line_per_run_and_no_more(self):
        for _ in range(3):
            self.run_sentinel(wake=False)
        self.assertEqual(len(self.log_lines()), 3)

    def test_the_line_starts_with_a_parsable_time(self):
        self.run_sentinel(wake=False)
        stamp = self.log_lines()[0].split()[0]
        self.assertIsNotNone(sentinel_mod._parse_ts(stamp), stamp)

    def test_a_red_run_names_the_failing_checks(self):
        self.make_red()
        green, _ = self.run_sentinel(wake=False)
        self.assertFalse(green)
        line = self.log_lines()[0]
        self.assertIn(" RED ", line)
        self.assertIn("card", line)
        self.assertNotIn("GREEN", line)

    def test_the_line_says_what_was_done_about_it(self):
        cmd, log = self.fake_cmd("wake")
        self.set_cfg(wake_cmd=cmd)
        self.make_red()
        self.run_sentinel()
        self.assertIn("wake: attempting", self.log_lines()[0])
        self.assertIn("WAKE-RESULT outcome=submitted exit_code=0 handled=unknown", self.log_lines()[1])
        self.assertEqual(len(self.calls(log)), 1)

    def test_a_wake_command_that_failed_is_not_recorded_as_a_wake(self):
        """A wake that landed and a wake whose command was missing are different facts."""
        self.make_red()
        for code, outcome in ((75, "deferred"), (2, "failed")):
            with self.subTest(exit_code=code):
                self.set_cfg(wake_cmd=f"exit {code}")
                _green, text = self.run_sentinel()
                self.assertIn(f"wake result: {outcome}", text)
                self.assertNotIn("wake: sent", text)
                self.assertIn(f"WAKE-RESULT outcome={outcome} exit_code={code}", self.log_lines()[-1])
                self.assertEqual(read_json(self.root / "state" / ".sentinel" / "wake.json")["outcome"], outcome)


class DryRunTest(SentinelCase):
    def _arm_everything(self):
        wake, wake_log = self.fake_cmd("wake")
        clear, clear_log = self.fake_cmd("clear")
        compact, compact_log = self.fake_cmd("compact")
        probe, _ = self.probe_cmd(busy=False)
        transcript = self.tmp / "coordinator.jsonl"
        write(transcript, json.dumps({
            "type": "assistant",
            "message": {"id": "m1", "usage": {"input_tokens": 10, "cache_read_input_tokens": 300000}},
        }) + "\n")
        self.set_cfg(
            wake_cmd=wake, clear_cmd=clear, compact_cmd=compact, busy_probe_cmd=probe,
            daily_clear_window="00:00-23:59", idle_compact=True,
            coordinator_transcript=str(transcript),
        )
        self.make_red()
        return wake_log, clear_log, compact_log

    def test_dry_run_says_what_it_would_do_and_does_none_of_it(self):
        wake_log, clear_log, compact_log = self._arm_everything()
        green, text = self.run_sentinel(dry_run=True, at=self.at(12))
        self.assertFalse(green)
        self.assertIn("[dry-run]", text)
        for word in ("wake:", "clear:", "compact:"):
            self.assertIn(word, text)
        self.assertIn("log line:", text)
        for log in (wake_log, clear_log, compact_log):
            self.assertEqual(self.calls(log), [], f"{log.name} ran during a dry run")
        self.assertEqual(self.log_lines(), [], "a dry run wrote to state/sentinel.log")
        self.assertFalse((self.root / "state" / ".sentinel" / "status.txt").exists())
        self.assertFalse(sentinel_mod.daily_clear_marker(self.desk, self.at(12)).exists())
        self.assertFalse((self.root / "state" / ".sentinel" / "wake.json").exists())

    def test_the_same_run_without_dry_run_does_all_three(self):
        wake_log, clear_log, compact_log = self._arm_everything()
        self.run_sentinel(at=self.at(12))
        self.assertEqual(len(self.calls(wake_log)), 1)
        self.assertEqual(len(self.calls(clear_log)), 1)
        self.assertEqual(len(self.calls(compact_log)), 1)
        self.assertEqual(len(self.log_lines()), 2)  # the decision, then the wake result


class WakeBrakeTest(SentinelCase):
    """A wake that repeats forever against a stuck board is noise, not a wake-up."""

    def setUp(self):
        super().setUp()
        self.wake_cmd, self.wake_log = self.fake_cmd("wake")
        self.set_cfg(wake_cmd=self.wake_cmd)
        self.make_red()

    def test_the_wake_fires_up_to_the_limit_on_an_unchanged_board(self):
        for _ in range(3):
            self.run_sentinel()
        self.assertEqual(len(self.calls(self.wake_log)), 3)

    def test_the_fourth_wake_on_the_same_board_is_braked_and_says_so(self):
        for _ in range(3):
            self.run_sentinel()
        green, text = self.run_sentinel()
        self.assertFalse(green)
        self.assertEqual(len(self.calls(self.wake_log)), 3, "the brake let a fourth wake through")
        self.assertIn("wake brake: same board 3 times, ask the principal", text)
        self.assertIn("braked", self.log_lines()[-1])

    def test_a_changed_board_lifts_the_brake(self):
        for _ in range(4):
            self.run_sentinel()
        self.assertEqual(len(self.calls(self.wake_log)), 3)
        self.make_red(marker="b")  # the board moved: new fingerprint
        self.run_sentinel()
        self.assertEqual(len(self.calls(self.wake_log)), 4)

    def test_a_pending_bell_is_part_of_the_board(self):
        before = sentinel_mod.board_fingerprint(self.desk)
        write(self.root / "bells" / "pending" / "001.bell", "{}\n")
        self.assertNotEqual(before, sentinel_mod.board_fingerprint(self.desk))

    def test_an_in_flight_line_is_part_of_the_board(self):
        before = sentinel_mod.board_fingerprint(self.desk)
        write(self.root / "state" / "in-flight.md", "# In flight\n\n- [worker] 017 doing a thing\n")
        self.assertNotEqual(before, sentinel_mod.board_fingerprint(self.desk))

    def test_the_limit_is_configurable(self):
        self.set_cfg(wake_max_repeats=1)
        self.run_sentinel()
        _green, text = self.run_sentinel()
        self.assertEqual(len(self.calls(self.wake_log)), 1)
        self.assertIn("wake brake: same board once", text)

    def test_a_green_desk_is_never_woken(self):
        write(self.root / "state" / "next.md", read(ROOT / "template" / "state" / "next.md"))
        green, _ = self.run_sentinel()
        self.assertTrue(green)
        self.assertEqual(self.calls(self.wake_log), [])

    def test_no_wake_suppresses_the_command_entirely(self):
        self.run_sentinel(wake=False)
        self.assertEqual(self.calls(self.wake_log), [])


class YieldGateTest(SentinelCase):
    """A wake that lands mid-turn is worse than a late one, and does not spend a try."""

    def setUp(self):
        super().setUp()
        self.wake_cmd, self.wake_log = self.fake_cmd("wake")
        probe, self.probe_log = self.probe_cmd(busy=True)
        self.set_cfg(wake_cmd=self.wake_cmd, busy_probe_cmd=probe)
        self.make_red()

    def test_a_busy_coordinator_is_not_woken_and_the_run_says_why(self):
        _green, text = self.run_sentinel()
        self.assertEqual(self.calls(self.wake_log), [])
        self.assertIn("yielded", text)
        self.assertIn("busy probe says busy", text)
        self.assertIn("yielded", self.log_lines()[-1])

    def test_yielding_does_not_count_toward_the_brake(self):
        for _ in range(5):
            self.run_sentinel()
        self.assertEqual(self.calls(self.wake_log), [])
        self.set_busy(False)
        for _ in range(3):
            self.run_sentinel()
        self.assertEqual(len(self.calls(self.wake_log)), 3, "yielded runs were counted as wakes")

    def test_an_idle_coordinator_is_woken(self):
        self.set_busy(False)
        self.run_sentinel()
        self.assertEqual(len(self.calls(self.wake_log)), 1)

    def test_a_broken_probe_counts_as_busy_and_names_itself(self):
        """A missing probe exits 127 through the shell; that must not read as a confident idle."""
        self.set_cfg(busy_probe_cmd=str(self.tmp / "no-such-probe-binary"))
        state, why = sentinel_mod.probe_busy(self.desk)
        self.assertEqual(state, sentinel_mod.BUSY)
        self.assertIn("could not be run", why)
        self.assertIn("127", why)

    def test_a_broken_probe_also_blocks_the_daily_clear(self):
        clear_cmd, clear_log = self.fake_cmd("clear")
        self.set_cfg(
            busy_probe_cmd=str(self.tmp / "no-such-probe-binary"),
            daily_clear_window="04:00-09:00", clear_cmd=clear_cmd,
        )
        _green, text = self.run_sentinel(wake=False, at=self.at(5))
        self.assertEqual(self.calls(clear_log), [])
        self.assertIn("could not be run", text)

    def test_no_probe_configured_still_wakes(self):
        self.set_cfg(busy_probe_cmd="")
        self.run_sentinel()
        self.assertEqual(len(self.calls(self.wake_log)), 1)


class StopDispatchTest(SentinelCase):
    def test_no_order_means_dispatch_is_allowed_and_the_check_is_green(self):
        allowed, reason = stop_mod.dispatch_allowed(self.desk)
        self.assertTrue(allowed)
        self.assertEqual(reason, "")
        checks = {c[0]: c for c in sentinel_mod.run_checks(self.desk)}
        self.assertTrue(checks["stop-dispatch"][1])

    def test_set_stops_dispatch_and_turns_the_sentinel_red(self):
        stop_mod.set_stop(self.desk, "release review pending")
        allowed, reason = stop_mod.dispatch_allowed(self.desk)
        self.assertFalse(allowed)
        self.assertIn("release review pending", reason)
        green, text = self.run_sentinel(wake=False)
        self.assertFalse(green)
        self.assertIn("dispatch stopped since", text)

    def test_a_stop_order_without_a_reason_is_refused(self):
        with self.assertRaises(BellhopError):
            stop_mod.set_stop(self.desk, "   ")
        self.assertFalse(stop_mod.stop_path(self.desk).exists())

    def test_stopping_twice_is_refused_rather_than_restamped(self):
        stop_mod.set_stop(self.desk, "first")
        before = read(stop_mod.stop_path(self.desk))
        with self.assertRaises(BellhopError):
            stop_mod.set_stop(self.desk, "second")
        self.assertEqual(before, read(stop_mod.stop_path(self.desk)))

    def test_one_exception_is_recorded_but_is_not_yet_a_piercing(self):
        stop_mod.set_stop(self.desk, "release review pending")
        stop_mod.add_exception(self.desk, "hotfix 019, principal asked in person")
        order = stop_mod.read_stop(self.desk)
        self.assertEqual(len(order["exceptions"]), 1)
        names = [c[0] for c in sentinel_mod.check_stop_dispatch(self.desk)]
        self.assertEqual(names, ["stop-dispatch"])

    def test_two_exceptions_make_the_order_itself_red(self):
        stop_mod.set_stop(self.desk, "release review pending")
        stop_mod.add_exception(self.desk, "hotfix 019")
        stop_mod.add_exception(self.desk, "hotfix 020")
        _green, text = self.run_sentinel(wake=False)
        self.assertIn("stop order pierced twice", text)
        self.assertIn("ask the principal to lift it", text)

    def test_a_third_exception_counts_rather_than_still_saying_twice(self):
        stop_mod.set_stop(self.desk, "release review pending")
        for i in range(3):
            stop_mod.add_exception(self.desk, f"hotfix {i}")
        _green, text = self.run_sentinel(wake=False)
        self.assertIn("stop order pierced 3 times", text)

    def test_an_exception_without_an_order_is_refused(self):
        with self.assertRaises(BellhopError):
            stop_mod.add_exception(self.desk, "just this once")

    def test_an_exception_without_a_reason_is_refused(self):
        stop_mod.set_stop(self.desk, "release review pending")
        before = read(stop_mod.stop_path(self.desk))
        with self.assertRaises(BellhopError):
            stop_mod.add_exception(self.desk, "")
        self.assertEqual(before, read(stop_mod.stop_path(self.desk)))

    def test_lifting_removes_the_file_and_writes_one_close_out(self):
        stop_mod.set_stop(self.desk, "release review pending")
        stop_mod.add_exception(self.desk, "hotfix 019")
        stop_mod.lift(self.desk)
        self.assertFalse(stop_mod.stop_path(self.desk).exists())
        closings = read(self.root / "state" / "closings.md")
        self.assertIn("[stop-dispatch]", closings)
        self.assertIn("1 exception(s) taken", closings)
        allowed, _ = stop_mod.dispatch_allowed(self.desk)
        self.assertTrue(allowed)
        green, _ = self.run_sentinel(wake=False)
        self.assertTrue(green)

    def test_lifting_nothing_is_refused_and_writes_no_close_out(self):
        before = read(self.root / "state" / "closings.md")
        with self.assertRaises(BellhopError):
            stop_mod.lift(self.desk)
        self.assertEqual(before, read(self.root / "state" / "closings.md"))

    def test_the_reason_tells_the_caller_how_to_lift_it(self):
        stop_mod.set_stop(self.desk, "release review pending")
        _allowed, reason = stop_mod.dispatch_allowed(self.desk)
        self.assertIn("bellhop stop-dispatch lift", reason)


class StopDispatchCliTest(SentinelCase):
    def cli(self, *args):
        return subprocess.run(
            [sys.executable, str(ROOT / "bellhop_main.py"), "--desk", str(self.root), "--no-commit", *args],
            capture_output=True, text=True,
        )

    def test_the_switch_round_trips_on_the_command_line(self):
        self.assertEqual(self.cli("stop-dispatch", "status").returncode, 0)
        out = self.cli("stop-dispatch", "set", "--reason", "release review pending")
        self.assertEqual(out.returncode, 0, out.stderr)
        status = self.cli("stop-dispatch", "status")
        self.assertEqual(status.returncode, 1, "status exits 0 while dispatch is stopped")
        self.assertIn("STOPPED", status.stdout)
        self.cli("stop-dispatch", "exception", "--reason", "hotfix 019")
        second = self.cli("stop-dispatch", "exception", "--reason", "hotfix 020")
        self.assertIn("pierced 2 times", second.stdout)
        lift = self.cli("stop-dispatch", "lift")
        self.assertEqual(lift.returncode, 0, lift.stderr)
        self.assertEqual(self.cli("stop-dispatch", "status").returncode, 0)

    def test_a_bad_lift_exits_non_zero_with_a_message(self):
        out = self.cli("stop-dispatch", "lift")
        self.assertEqual(out.returncode, 1)
        self.assertIn("not stopped", out.stderr)


class DailyClearTest(SentinelCase):
    def setUp(self):
        super().setUp()
        self.clear_cmd, self.clear_log = self.fake_cmd("clear")
        probe, _ = self.probe_cmd(busy=False)
        self.set_cfg(daily_clear_window="04:00-09:00", clear_cmd=self.clear_cmd, busy_probe_cmd=probe)

    def test_an_idle_run_inside_the_window_clears_once_a_day(self):
        _green, text = self.run_sentinel(wake=False, at=self.at(5))
        self.assertEqual(len(self.calls(self.clear_log)), 1)
        self.assertIn("clear: sent inside 04:00-09:00", text)
        self.assertTrue(sentinel_mod.daily_clear_marker(self.desk, self.at(5)).exists())

    def test_it_tries_on_every_run_in_the_window_not_at_one_exact_minute(self):
        self.set_busy(True)
        for hour in (4, 5, 6, 7):
            self.run_sentinel(wake=False, at=self.at(hour))
        self.assertEqual(self.calls(self.clear_log), [])
        self.set_busy(False)
        self.run_sentinel(wake=False, at=self.at(8, 37))
        self.assertEqual(len(self.calls(self.clear_log)), 1, "a late idle run inside the window did not clear")

    def test_it_does_not_clear_twice_in_one_day(self):
        self.run_sentinel(wake=False, at=self.at(5))
        _green, text = self.run_sentinel(wake=False, at=self.at(6))
        self.assertEqual(len(self.calls(self.clear_log)), 1)
        self.assertIn("already cleared today", text)

    def test_the_next_day_clears_again(self):
        self.run_sentinel(wake=False, at=self.at(5))
        self.run_sentinel(wake=False, at=self.at(5, days=1))
        self.assertEqual(len(self.calls(self.clear_log)), 2)

    def test_outside_the_window_it_says_so_and_does_not_clear(self):
        _green, text = self.run_sentinel(wake=False, at=self.at(12))
        self.assertEqual(self.calls(self.clear_log), [])
        self.assertIn("outside the window 04:00-09:00", text)
        self.assertFalse(sentinel_mod.daily_clear_marker(self.desk, self.at(12)).exists())

    def test_a_busy_coordinator_is_not_cleared(self):
        self.set_busy(True)
        _green, text = self.run_sentinel(wake=False, at=self.at(5))
        self.assertEqual(self.calls(self.clear_log), [])
        self.assertIn("not idle", text)
        self.assertFalse(sentinel_mod.daily_clear_marker(self.desk, self.at(5)).exists())

    def test_a_settled_awaiting_close_line_blocks_the_clear(self):
        write(
            self.root / "state" / "in-flight.md",
            "# In flight\n\n- [worker] 017 shipped, waiting for the close-out #settled-awaiting-close\n",
        )
        _green, text = self.run_sentinel(wake=False, at=self.at(5))
        self.assertEqual(self.calls(self.clear_log), [])
        self.assertIn("settled-awaiting-close", text)

    def test_without_a_busy_probe_it_refuses_to_clear_blind(self):
        self.set_cfg(busy_probe_cmd="")
        _green, text = self.run_sentinel(wake=False, at=self.at(5))
        self.assertEqual(self.calls(self.clear_log), [])
        self.assertIn("no busy probe configured", text)

    def test_an_empty_window_turns_the_whole_thing_off(self):
        self.set_cfg(daily_clear_window="")
        _green, text = self.run_sentinel(wake=False, at=self.at(5))
        self.assertEqual(self.calls(self.clear_log), [])
        self.assertIn("clear: window off", text)

    def test_a_backwards_window_falls_back_and_warns_on_every_run(self):
        self.set_cfg(daily_clear_window="09:00-04:00")
        _green, first = self.run_sentinel(wake=False, at=self.at(5))
        self.assertIn("starts at or after it ends", first)
        self.assertEqual(len(self.calls(self.clear_log)), 1, "the fallback window did not clear")
        _green, second = self.run_sentinel(wake=False, at=self.at(6))
        self.assertIn("starts at or after it ends", second, "the warning was printed only once")

    def test_an_unparsable_window_falls_back_too(self):
        start, end, warning = sentinel_mod.parse_window("early morning")
        self.assertIsNone(start)
        self.assertIsNone(end)
        self.assertIn("HH:MM-HH:MM", warning)

    def test_a_good_window_parses_without_a_warning(self):
        start, end, warning = sentinel_mod.parse_window("04:00-09:00")
        self.assertEqual((start.hour, end.hour), (4, 9))
        self.assertEqual(warning, "")

    def test_no_clear_cmd_means_no_clear(self):
        self.set_cfg(clear_cmd="")
        _green, text = self.run_sentinel(wake=False, at=self.at(5))
        self.assertIn("no clear_cmd configured", text)


class IdleCompactionTest(SentinelCase):
    def setUp(self):
        super().setUp()
        self.compact_cmd, self.compact_log = self.fake_cmd("compact")
        probe, _ = self.probe_cmd(busy=False)
        self.transcript = self.tmp / "coordinator.jsonl"
        self.write_transcript(300000)
        self.set_cfg(
            idle_compact=True, compact_cmd=self.compact_cmd, busy_probe_cmd=probe,
            coordinator_transcript=str(self.transcript),
        )

    def write_transcript(self, tokens, rows=2):
        lines = []
        for i in range(rows):
            usage = {"input_tokens": 100, "cache_read_input_tokens": tokens - 100 - 40, "cache_creation_input_tokens": 40}
            if i < rows - 1:
                usage = {"input_tokens": 10, "cache_read_input_tokens": 10, "cache_creation_input_tokens": 0}
            lines.append(json.dumps({"type": "assistant", "message": {"id": f"m{i}", "usage": usage}}))
        lines.append(json.dumps({"type": "user", "message": {"role": "user"}}))
        write(self.transcript, "\n".join(lines) + "\n")

    def test_it_is_off_by_default(self):
        self.set_cfg(idle_compact=False)
        _green, text = self.run_sentinel(wake=False)
        self.assertEqual(self.calls(self.compact_log), [])
        self.assertIn("compact: off", text)

    def test_the_last_assistant_usage_is_the_number_that_counts(self):
        self.write_transcript(300000)
        self.assertEqual(sentinel_mod.transcript_tokens(self.transcript), 300000)

    def test_an_idle_desk_over_the_threshold_is_compacted_once(self):
        _green, text = self.run_sentinel(wake=False, at=self.at(12))
        self.assertEqual(len(self.calls(self.compact_log)), 1)
        self.assertIn("compact: sent", text)
        self.assertTrue((self.root / "state" / ".sentinel" / "compact.json").exists())

    def test_the_cool_down_holds_the_second_run(self):
        self.run_sentinel(wake=False, at=self.at(12))
        _green, text = self.run_sentinel(wake=False, at=self.at(15))
        self.assertEqual(len(self.calls(self.compact_log)), 1)
        self.assertIn("cool-down", text)

    def test_after_the_cool_down_it_may_compact_again(self):
        self.run_sentinel(wake=False, at=self.at(12))
        self.run_sentinel(wake=False, at=self.at(19))
        self.assertEqual(len(self.calls(self.compact_log)), 2)

    def test_under_the_threshold_nothing_happens(self):
        self.write_transcript(100000)
        _green, text = self.run_sentinel(wake=False, at=self.at(12))
        self.assertEqual(self.calls(self.compact_log), [])
        self.assertIn("100,000 tokens < 250,000", text)

    def test_a_busy_desk_is_never_compacted(self):
        self.set_busy(True)
        _green, text = self.run_sentinel(wake=False, at=self.at(12))
        self.assertEqual(self.calls(self.compact_log), [])
        self.assertIn("not idle", text)

    def test_a_missing_transcript_says_so_rather_than_compacting(self):
        self.set_cfg(coordinator_transcript=str(self.tmp / "gone.jsonl"))
        _green, text = self.run_sentinel(wake=False, at=self.at(12))
        self.assertEqual(self.calls(self.compact_log), [])
        self.assertIn("transcript unreadable", text)

    def test_the_threshold_is_configurable(self):
        self.set_cfg(compact_threshold_tokens=1000000)
        _green, text = self.run_sentinel(wake=False, at=self.at(12))
        self.assertEqual(self.calls(self.compact_log), [])
        self.assertIn("< 1,000,000", text)


class AutostartTest(SentinelCase):
    def plists(self):
        import plistlib

        out = {}
        for path, content in daemon_mod.service_files(self.desk, "launchd", python="/usr/bin/python3").items():
            out[path.name] = plistlib.loads(content.encode("utf-8"))
        return out

    def units(self):
        return {p.name: c for p, c in daemon_mod.service_files(self.desk, "systemd", python="/usr/bin/python3").items()}

    def test_the_default_is_that_nothing_starts_at_boot(self):
        self.assertFalse(daemon_mod.autostart(self.desk))
        watch = [v for k, v in self.plists().items() if not k.endswith(".sentinel.plist")][0]
        self.assertFalse(watch["RunAtLoad"])
        self.assertFalse(watch["KeepAlive"])
        self.assertTrue(watch["Disabled"])
        for name, text in self.units().items():
            self.assertNotIn("WantedBy", text, name)
        self.assertNotIn("OnBootSec", "".join(self.units().values()))

    def test_autostart_true_puts_the_services_back_in_the_boot_path(self):
        self.set_cfg(autostart=True)
        watch = [v for k, v in self.plists().items() if not k.endswith(".sentinel.plist")][0]
        self.assertTrue(watch["RunAtLoad"])
        self.assertTrue(watch["KeepAlive"])
        self.assertNotIn("Disabled", watch)
        units = self.units()
        self.assertIn("WantedBy=default.target", "".join(units.values()))
        self.assertIn("WantedBy=timers.target", "".join(units.values()))

    def test_the_sentinel_job_does_not_run_at_load_either(self):
        sentinel_job = [v for k, v in self.plists().items() if k.endswith(".sentinel.plist")][0]
        self.assertFalse(sentinel_job["RunAtLoad"])
        self.assertTrue(sentinel_job["Disabled"])
        self.assertGreater(sentinel_job["StartInterval"], 0)

    def test_the_load_commands_start_them_by_hand(self):
        flat = [" ".join(c) for c in daemon_mod.load_commands(self.desk, "launchd")]
        self.assertTrue(any("kickstart" in c for c in flat), flat)
        self.assertTrue(any("launchctl enable" in c for c in flat), flat)
        flat = [" ".join(c) for c in daemon_mod.load_commands(self.desk, "systemd")]
        self.assertTrue(any(c.split()[2] == "start" for c in flat if len(c.split()) > 2), flat)
        self.assertFalse(any("enable" in c for c in flat), flat)

    def test_with_autostart_on_the_load_commands_enable_them(self):
        self.set_cfg(autostart=True)
        flat = [" ".join(c) for c in daemon_mod.load_commands(self.desk, "systemd")]
        self.assertTrue(any("enable --now" in c for c in flat), flat)
        flat = [" ".join(c) for c in daemon_mod.load_commands(self.desk, "launchd")]
        self.assertFalse(any("kickstart" in c for c in flat), flat)

    def test_status_says_which_it_is(self):
        text = daemon_mod.status_text(self.desk)
        self.assertIn("autostart: no", text)
        self.set_cfg(autostart=True)
        self.assertIn("autostart: yes", daemon_mod.status_text(self.desk))


class SentinelCliTest(SentinelCase):
    def cli(self, *args):
        return subprocess.run(
            [sys.executable, str(ROOT / "bellhop_main.py"), "--desk", str(self.root), *args],
            capture_output=True, text=True, env=dict(os.environ, BELLHOP_DESK=str(self.root)),
        )

    def test_dry_run_from_the_command_line_writes_nothing(self):
        cmd, log = self.fake_cmd("wake")
        self.set_cfg(wake_cmd=cmd)
        self.make_red()
        out = self.cli("sentinel", "--dry-run")
        self.assertEqual(out.returncode, 1, out.stderr)
        self.assertIn("[dry-run]", out.stdout)
        self.assertEqual(self.calls(log), [])
        self.assertEqual(self.log_lines(), [])

    def test_a_real_run_logs_a_line_and_exits_green(self):
        out = self.cli("sentinel", "--no-wake")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(len(self.log_lines()), 1)


if __name__ == "__main__":
    unittest.main()
