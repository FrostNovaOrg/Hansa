"""The provider adapter layer: who answers a bell, with what command, under what gate.

Every gate here gets a positive case and a negative control, because each one fails
silently by nature: a schedule override that did not fire and one that fired correctly
leave the same receipt; a worker that ran unsandboxed and one that ran sandboxed leave
the same receipt; a cost the parser could not find and a cost of zero read the same as
`0`. The control is what tells them apart.

Stub commands are small scripts this file writes into a tempfile sandbox. Nothing here
reaches the network, a real CLI, or a file outside its own temporary directory.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bellhop import daemon as daemon_mod  # noqa: E402
from bellhop import providers  # noqa: E402
from bellhop import desk_ops, ring, watch  # noqa: E402
from bellhop.config import LANE_DEFAULTS, Desk  # noqa: E402
from bellhop.packet import parse_packet  # noqa: E402
from bellhop.util import BellhopError, read, read_json, write, write_json  # noqa: E402

GIT_ID = ["-c", "user.name=test", "-c", "user.email=test@example.invalid"]


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *GIT_ID, *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def at(text):
    """A fixed local time for the schedule tests: `2026-09-09T14:00` is a Wednesday."""
    return providers.parse_when(text, today=datetime(2026, 9, 9, 12, 0))


class ProviderUnitTest(unittest.TestCase):
    """Everything that does not need a git repository, on a desk of two files."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bellhop-prov.")).resolve()
        self.cfg = {
            "sandbox_wrapper": "",
            "lanes": {
                "api": {"repo": "../widget-service", "model": "opus"},
            },
        }
        self.desk = Desk(self.tmp, self.cfg)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        os.environ.pop("BELLHOP_NOW", None)

    def lane(self, **overrides):
        self.cfg["lanes"]["api"].update(overrides)
        return self.desk.lane("api")

    # -- the parser: three shapes, and it never raises ---------------------
    def test_parser_reads_the_claude_result_json(self):
        text = ('{"type":"system","subtype":"init"}\n'
                + json.dumps({"type": "result", "subtype": "success", "is_error": False,
                              "num_turns": 7, "duration_ms": 12400, "total_cost_usd": 0.42,
                              "model": "opus", "result": "done", "session_id": "abc"}))
        out = providers.parse_result(text)
        self.assertEqual(out["model"], "opus")
        self.assertEqual(out["turns"], 7)
        self.assertEqual(out["cost_usd"], 0.42)
        self.assertEqual(out["duration_s"], 12.4)
        self.assertEqual(out["stop_reason"], "success")
        self.assertEqual(out["result_text"], "done")
        self.assertEqual(out["raw"]["session_id"], "abc")

    def test_parser_marks_the_error_subtype(self):
        out = providers.parse_result(json.dumps({"subtype": "error_max_turns", "is_error": True}))
        self.assertEqual(out["stop_reason"], "error_max_turns (error)")

    def test_parser_reads_a_generic_result_json(self):
        out = providers.parse_result(json.dumps(
            {"result": "finished", "cost_usd": 1.25, "num_turns": 3, "model": "other-model"}))
        self.assertEqual((out["model"], out["turns"], out["cost_usd"]), ("other-model", 3, 1.25))
        self.assertEqual(out["result_text"], "finished")
        # No duration in this shape: `n/a`, which is not the reading `0` gives.
        self.assertEqual(out["duration_s"], providers.NA)
        self.assertNotEqual(out["duration_s"], 0)

    def test_parser_reads_the_text_stopreason_shape(self):
        out = providers.parse_result(json.dumps(
            {"text": "all green", "stopReason": "end_turn", "num_turns": 2,
             "modelUsage": {"other-cli-model": {"inputTokens": 10}}}))
        self.assertEqual(out["stop_reason"], "end_turn")
        self.assertEqual(out["result_text"], "all green")
        self.assertEqual(out["model"], "other-cli-model")

    def test_parser_falls_back_to_text_with_na_numbers(self):
        out = providers.parse_result("the worker printed prose and exited\n")
        self.assertEqual(out["result_text"], "the worker printed prose and exited")
        for key in ("model", "turns", "cost_usd", "duration_s", "stop_reason"):
            self.assertEqual(out[key], providers.NA, key)

    def test_parser_never_raises(self):
        """It runs on the closing path: a traceback here loses a finished task's footer."""
        for text in ("", "{", '{"a":', "[1,2,3]", '{"num_turns": "many", "duration_ms": "soon"}',
                     "\x00\x01binary", None, 17):
            out = providers.parse_result(text)
            self.assertIsInstance(out, dict)
            self.assertIn("result_text", out)
        # negative control: a well-formed line still parses, so the catch-all is not
        # swallowing everything
        self.assertEqual(providers.parse_result('{"num_turns": 4}')["turns"], 4)

    def test_na_none_keeps_zero_apart_from_missing(self):
        self.assertIsNone(providers.na_none(providers.NA))
        self.assertEqual(providers.na_none(0), 0)

    # -- the guard ---------------------------------------------------------
    def write_allowlist(self, body, name="providers/allow.txt"):
        path = self.tmp / name
        write(path, body)
        return name

    def test_guard_accepts_a_command_on_the_allowlist(self):
        rel = self.write_allowlist("allow git status:*\nallow ./check.sh:*\n")
        lane = self.lane(allowlist_file=rel)
        ok, reason = providers.guard(self.desk, lane, "git status --short")
        self.assertTrue(ok, reason)
        ok, reason = providers.guard(self.desk, lane, "./check.sh")
        self.assertTrue(ok, reason)

    def test_guard_refuses_a_command_that_is_not_on_it(self):
        lane = self.lane(allowlist_file=self.write_allowlist("allow git status:*\n"))
        ok, reason = providers.guard(self.desk, lane, "curl https://example.invalid")
        self.assertFalse(ok)
        self.assertIn("not on the allowlist", reason)
        self.assertIn("curl", reason)

    def test_guard_refuses_on_a_prefix_that_only_looks_like_a_match(self):
        """`git status:*` is a word-boundary prefix, not a string prefix."""
        lane = self.lane(allowlist_file=self.write_allowlist("allow git status:*\n"))
        self.assertTrue(providers.guard(self.desk, lane, "git status")[0])
        self.assertFalse(providers.guard(self.desk, lane, "git statuscheck --evil")[0])

    def test_guard_reads_every_segment_of_a_chained_command(self):
        """A chained command and a single one look the same from the front."""
        lane = self.lane(allowlist_file=self.write_allowlist("allow git status:*\nallow ls:*\n"))
        self.assertTrue(providers.guard(self.desk, lane, "git status && ls")[0])
        ok, reason = providers.guard(self.desk, lane, "git status && rm -rf /")
        self.assertFalse(ok)
        self.assertIn("rm -rf", reason)

    def test_guard_strips_leading_environment_assignments(self):
        lane = self.lane(allowlist_file=self.write_allowlist("allow git status:*\n"))
        self.assertTrue(providers.guard(self.desk, lane, "GIT_PAGER=cat git status")[0])

    def test_guard_lets_deny_beat_allow(self):
        lane = self.lane(allowlist_file=self.write_allowlist("allow git:*\ndeny git push:*\n"))
        self.assertTrue(providers.guard(self.desk, lane, "git log -1")[0])
        ok, reason = providers.guard(self.desk, lane, "git push origin main")
        self.assertFalse(ok)
        self.assertIn("denied by rule", reason)

    def test_guard_accepts_all_three_rule_spellings(self):
        rel = self.write_allowlist("git status:*\nallow ls:*\nallow Bash(wc:*)\n# comment\n\n")
        lane = self.lane(allowlist_file=rel)
        for command in ("git status", "ls -la", "wc -l file"):
            self.assertTrue(providers.guard(self.desk, lane, command)[0], command)

    def test_guard_refuses_when_the_lane_declares_no_allowlist(self):
        ok, reason = providers.guard(self.desk, self.lane(), "ls")
        self.assertFalse(ok)
        self.assertIn("no allowlist_file", reason)

    def test_guard_refuses_when_the_allowlist_file_is_missing(self):
        lane = self.lane(allowlist_file="providers/not-there.txt")
        ok, reason = providers.guard(self.desk, lane, "ls")
        self.assertFalse(ok)
        self.assertIn("not found", reason)

    def test_guard_refuses_an_empty_allowlist(self):
        lane = self.lane(allowlist_file=self.write_allowlist("# only comments\n"))
        ok, reason = providers.guard(self.desk, lane, "ls")
        self.assertFalse(ok)
        self.assertIn("deny by default", reason)

    def test_the_shipped_allowlist_example_parses_and_gates(self):
        allow, deny = providers.load_allowlist(ROOT / "template" / "providers" / "allowlist.example.txt")
        self.assertTrue(allow and deny)
        lane = self.lane(allowlist_file=str(ROOT / "template" / "providers" / "allowlist.example.txt"))
        self.assertTrue(providers.guard(self.desk, lane, "git status --short")[0])
        self.assertFalse(providers.guard(self.desk, lane, "git push origin main")[0])

    # -- the schedule window ------------------------------------------------
    def window(self):
        return {"name": "peak", "days": "mon-fri", "from": "14:00", "to": "18:00",
                "provider": "generic", "model": "other-model"}

    def test_schedule_boundary_minute_by_minute(self):
        w = self.window()
        self.assertFalse(providers.window_active(w, at("2026-09-09T13:59")))  # one minute before
        self.assertTrue(providers.window_active(w, at("2026-09-09T14:00")))   # `from` is inside
        self.assertTrue(providers.window_active(w, at("2026-09-09T17:59")))
        self.assertFalse(providers.window_active(w, at("2026-09-09T18:00")))  # `to` is outside
        self.assertFalse(providers.window_active(w, at("2026-09-12T15:00")))  # Saturday

    def test_schedule_window_may_wrap_midnight(self):
        w = {"days": "mon-fri", "from": "22:00", "to": "02:00"}
        self.assertTrue(providers.window_active(w, at("2026-09-09T23:30")))
        self.assertTrue(providers.window_active(w, at("2026-09-10T01:00")))   # Thursday morning
        self.assertFalse(providers.window_active(w, at("2026-09-09T12:00")))

    def test_day_spellings(self):
        self.assertEqual(providers.parse_days("mon-fri"), {0, 1, 2, 3, 4})
        self.assertEqual(providers.parse_days("sat,sun"), {5, 6})
        self.assertEqual(providers.parse_days("*"), set(range(7)))
        self.assertEqual(providers.parse_days("fri-mon"), {4, 5, 6, 0})
        with self.assertRaises(BellhopError):
            providers.parse_days("funday")

    def test_hhmm_with_a_leading_zero_is_not_octal(self):
        """The shell version had to dodge `[ 0900 -ge 1400 ]`; this one counts minutes."""
        self.assertEqual(providers.parse_hhmm("09:00", "from"), 540)
        with self.assertRaises(BellhopError):
            providers.parse_hhmm("25:00", "from")

    def test_schedule_override_applies_inside_the_window_only(self):
        lane = self.lane(schedule=[self.window()])
        inside = providers.resolve(self.desk, lane, when=at("2026-09-09T15:00"))
        self.assertEqual((inside.provider, inside.model), ("generic", "other-model"))
        self.assertEqual(inside.schedule_label, "peak")
        self.assertIn("⏰ 001.bell: schedule override → generic/other-model",
                      inside.override_line("001.bell"))
        outside = providers.resolve(self.desk, lane, when=at("2026-09-09T13:00"))
        self.assertEqual((outside.provider, outside.model), ("claude", "opus"))
        self.assertFalse(outside.overridden)

    def test_an_override_that_changes_nothing_is_said_in_red_not_logged_as_a_swap(self):
        """`it moved` and `it did not move` must not read the same in the log."""
        w = dict(self.window(), provider="claude", model="opus")
        resolved = providers.resolve(self.desk, self.lane(schedule=[w]), when=at("2026-09-09T15:00"))
        self.assertFalse(resolved.overridden)
        self.assertTrue(any("does nothing" in n for n in resolved.notes), resolved.notes)

    def test_override_may_read_a_lane_model_file(self):
        write(self.tmp / "state" / "lane-model-peak.txt", "generic\nfile-model\nhigh\n")
        w = {"days": "mon-fri", "from": "14:00", "to": "18:00",
             "model_file": "state/lane-model-peak.txt"}
        resolved = providers.resolve(self.desk, self.lane(schedule=[w]), when=at("2026-09-09T15:00"))
        self.assertEqual((resolved.provider, resolved.model, resolved.effort),
                         ("generic", "file-model", "high"))

    def test_a_missing_lane_model_file_refuses_instead_of_falling_back(self):
        w = {"days": "mon-fri", "from": "14:00", "to": "18:00",
             "model_file": "state/lane-model-peak.txt"}
        lane = self.lane(schedule=[w])
        with self.assertRaises(providers.ProviderRefusal) as caught:
            providers.resolve(self.desk, lane, when=at("2026-09-09T15:00"))
        self.assertIn("lane-model-peak.txt", str(caught.exception))
        # negative control: outside the window the same lane resolves fine, so the
        # refusal is about the window and not about the lane
        ok = providers.resolve(self.desk, lane, when=at("2026-09-09T09:00"))
        self.assertEqual(ok.provider, "claude")

    def test_unknown_provider_is_refused(self):
        with self.assertRaises(providers.ProviderRefusal):
            providers.resolve(self.desk, self.lane(provider="no-such-vendor"))

    # -- heavy --------------------------------------------------------------
    def packet_file(self, heavy):
        text = ("---\nid: 001\nlane: api\ntitle: t\nbase: abc123\nbudget_minutes: 10\n"
                + ("effort: heavy\n" if heavy else "")
                + "acceptance:\n  - it works\n---\nbody\n")
        path = self.tmp / "inbox" / "api" / "001-t.md"
        write(path, text)
        return parse_packet(path)

    def test_heavy_marker_raises_the_model(self):
        lane = self.lane(model="opus", heavy_model="opus-heavy")
        self.assertEqual(providers.resolve(self.desk, lane, packet=self.packet_file(True)).model, "opus-heavy")
        # negative control: the same lane, a packet without the marker
        self.assertEqual(providers.resolve(self.desk, lane, packet=self.packet_file(False)).model, "opus")

    def test_heavy_marker_raises_the_effort_one_notch(self):
        lane = self.lane(model="opus", effort="high", heavy_model="", heavy_effort="")
        self.assertEqual(providers.resolve(self.desk, lane, heavy=True).effort, "xhigh")
        self.assertEqual(providers.resolve(self.desk, lane, heavy=False).effort, "high")
        self.assertEqual(providers.notch_up("xhigh"), "xhigh")  # the top rung stays

    def test_heavy_effort_wins_over_the_notch(self):
        lane = self.lane(effort="low", heavy_effort="xhigh")
        self.assertEqual(providers.resolve(self.desk, lane, heavy=True).effort, "xhigh")

    def test_heavy_says_so_when_the_lane_gave_it_nothing_to_raise(self):
        lane = self.lane(model="opus", heavy_model="", heavy_effort="", effort="")
        resolved = providers.resolve(self.desk, lane, heavy=True)
        self.assertEqual(resolved.model, "opus")
        self.assertTrue(any("raised nothing" in n for n in resolved.notes), resolved.notes)

    def test_lane_heavy_model_is_not_carried_across_a_provider_change(self):
        lane = self.lane(model="opus", heavy_model="opus-heavy", schedule=[self.window()])
        resolved = providers.resolve(self.desk, lane, heavy=True, when=at("2026-09-09T15:00"))
        self.assertEqual((resolved.provider, resolved.model), ("generic", "other-model"))
        self.assertTrue(any("was not applied" in n for n in resolved.notes), resolved.notes)
        # positive control: the window may name its own
        lane = self.lane(schedule=[dict(self.window(), heavy_model="other-model-heavy")])
        resolved = providers.resolve(self.desk, lane, heavy=True, when=at("2026-09-09T15:00"))
        self.assertEqual(resolved.model, "other-model-heavy")

    def test_the_clock_can_be_frozen_but_not_silently(self):
        os.environ["BELLHOP_NOW"] = "2026-09-09T15:00"
        try:
            self.assertEqual(providers.clock().hour, 15)
            self.assertIn("BELLHOP_NOW", providers.clock_note())
            resolved = providers.resolve(self.desk, self.lane(schedule=[self.window()]))
            self.assertTrue(resolved.overridden)
            self.assertTrue(any("clock override" in n for n in resolved.notes))
        finally:
            os.environ.pop("BELLHOP_NOW")
        self.assertEqual(providers.clock_note(), "")

    # -- command building ---------------------------------------------------
    def build(self, lane, prompt="prompt", packet=None, **kw):
        resolved = kw.pop("resolved", None) or providers.resolve(self.desk, lane, packet=packet)
        return providers.build_command(self.desk, lane, packet or self.packet_file(False), prompt,
                                       resolved, worktree=self.tmp / "wt", **kw)

    def test_claude_command_carries_model_allowlist_and_permission_mode(self):
        lane = self.lane(allowed_tools=["Read", "Bash(git:*)"])
        cmd = self.build(lane)
        self.assertEqual(cmd[:3], ["claude", "-p", "prompt"])
        self.assertEqual(cmd[cmd.index("--model") + 1], "opus")
        self.assertEqual(cmd[cmd.index("--allowedTools") + 1], "Read,Bash(git:*)")
        self.assertEqual(cmd[cmd.index("--permission-mode") + 1], "acceptEdits")
        self.assertIn("--output-format", cmd)
        self.assertNotIn("--effort", cmd)  # not declared, so not passed

    def test_claude_command_passes_effort_when_the_lane_declares_one(self):
        cmd = self.build(self.lane(effort="high"))
        self.assertEqual(cmd[cmd.index("--effort") + 1], "high")

    def test_generic_command_renders_every_placeholder(self):
        lane = self.lane(provider="generic", model="other-model",
                         allowlist_file=self.write_allowlist("allow ls:*\n"),
                         worker_cmd="other-cli --file {prompt_file} --cd {cwd} -m {model} "
                                    "--log {log} --rules {allowlist} --keep ${HOME}")
        cmd = self.build(lane, prompt_file=self.tmp / "p.md", log_path=self.tmp / "b.log")
        self.assertEqual(cmd[:2], ["/bin/sh", "-c"])
        line = cmd[2]
        self.assertIn(f"--file {self.tmp / 'p.md'}", line)
        self.assertIn(f"--cd {self.tmp / 'wt'}", line)
        self.assertIn("-m other-model", line)
        self.assertIn(f"--log {self.tmp / 'b.log'}", line)
        self.assertIn("allow.txt", line)
        self.assertIn("${HOME}", line)  # a brace that is not a placeholder is left alone

    def test_generic_without_a_worker_cmd_refuses(self):
        lane = self.lane(provider="generic", allowlist_file=self.write_allowlist("allow ls:*\n"))
        with self.assertRaises(providers.ProviderRefusal) as caught:
            self.build(lane)
        self.assertIn("worker_cmd", str(caught.exception))

    def test_generic_without_an_allowlist_refuses(self):
        lane = self.lane(provider="generic", worker_cmd="other-cli -p {prompt_file}")
        with self.assertRaises(providers.ProviderRefusal) as caught:
            self.build(lane)
        self.assertIn("allowlist_file", str(caught.exception))

    def test_sandbox_profile_wraps_the_command(self):
        write(self.tmp / "providers" / "lane.sb", "(version 1)\n(deny default)\n")
        self.desk.cfg["sandbox_wrapper"] = "sandbox-exec -f {profile}"
        cmd = self.build(self.lane(sandbox_profile="providers/lane.sb"))
        self.assertEqual(cmd[0], "sandbox-exec")
        self.assertEqual(cmd[1], "-f")
        self.assertEqual(cmd[2], str(self.tmp / "providers" / "lane.sb"))
        self.assertEqual(cmd[3], "claude")
        # negative control: no profile, no wrapper
        self.cfg["lanes"]["api"].pop("sandbox_profile")
        self.assertEqual(self.build(self.lane())[0], "claude")

    def test_a_missing_sandbox_profile_refuses_rather_than_running_unsandboxed(self):
        self.desk.cfg["sandbox_wrapper"] = "sandbox-exec -f {profile}"
        with self.assertRaises(providers.ProviderRefusal) as caught:
            self.build(self.lane(sandbox_profile="providers/gone.sb"))
        self.assertIn("unsandboxed", str(caught.exception))

    def test_a_profile_with_no_wrapper_refuses(self):
        write(self.tmp / "providers" / "lane.sb", "(version 1)\n")
        self.desk.cfg["sandbox_wrapper"] = ""
        with self.assertRaises(providers.ProviderRefusal) as caught:
            self.build(self.lane(sandbox_profile="providers/lane.sb"))
        self.assertIn("sandbox_wrapper", str(caught.exception))

    def test_worker_cmd_still_wins_on_a_claude_lane(self):
        """The escape hatch, and the test stub's entry. It must not need an allowlist."""
        lane = self.lane(worker_cmd="python3 stub.py")
        self.assertEqual(self.build(lane), ["/bin/sh", "-c", "python3 stub.py"])

    def test_worker_env_is_scrubbed(self):
        os.environ["BELLHOP_TEST_SECRET"] = "swordfish"
        try:
            env = providers.worker_env(self.desk, self.lane(), providers.Resolved("claude", "opus"))
            self.assertNotIn("BELLHOP_TEST_SECRET", env)
            self.assertIn("PATH", env)
            env = providers.worker_env(self.desk, self.lane(env_allow=["BELLHOP_TEST_SECRET"]),
                                       providers.Resolved("claude", "opus"))
            self.assertEqual(env["BELLHOP_TEST_SECRET"], "swordfish")
        finally:
            os.environ.pop("BELLHOP_TEST_SECRET")

    # -- provider check -----------------------------------------------------
    def test_check_reports_clean_and_flags_what_is_missing(self):
        write(self.tmp / "providers" / "lane.sb", "(version 1)\n")
        self.desk.cfg["sandbox_wrapper"] = "sandbox-exec -f {profile}"
        lane = self.lane(allowlist_file=self.write_allowlist("allow ls:*\n"),
                         sandbox_profile="providers/lane.sb", heavy_model="opus-heavy",
                         schedule=[self.window()])
        del lane
        report = providers.check(self.desk, "api", when=at("2026-09-09T15:00"))
        self.assertEqual(report["problems"], [])
        text = providers.check_text(report)
        self.assertIn("generic/other-model", text)
        self.assertIn("override 'peak' is active", text)
        # the heavy pass's own note reaches the page: a heavy packet that raised nothing
        # and one that raised something read the same without it
        self.assertIn("was not applied", text)
        # and outside the window it says so
        text = providers.check_text(providers.check(self.desk, "api", when=at("2026-09-09T09:00")))
        self.assertIn("no override active", text)
        self.assertIn("claude/opus", text)
        # negative control: take the allowlist away and the report turns red
        self.cfg["lanes"]["api"]["allowlist_file"] = "providers/gone.txt"
        report = providers.check(self.desk, "api", when=at("2026-09-09T09:00"))
        self.assertTrue(report["problems"])
        self.assertIn("MISSING", providers.check_text(report))


class ProviderCliTest(unittest.TestCase):
    """The two command lines, run as a person would run them."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bellhop-provcli."))
        write(self.tmp / "providers" / "allow.txt", "allow git status:*\ndeny git push:*\n")
        write_json(self.tmp / "bellhop.json", {
            "sandbox_wrapper": "",
            "lanes": {"api": {"repo": "../x", "model": "opus", "provider": "claude",
                              "allowlist_file": "providers/allow.txt"}},
        })

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def bellhop(self, *args):
        env = dict(os.environ, BELLHOP_DESK=str(self.tmp))
        env.pop("BELLHOP_NOW", None)
        return subprocess.run([str(ROOT / "bin" / "bellhop"), *args], env=env,
                              capture_output=True, text=True)

    def test_provider_guard_exit_codes(self):
        ok = self.bellhop("provider-guard", "--lane", "api", "--", "git", "status", "--short")
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertIn("allowed:", ok.stdout)
        refused = self.bellhop("provider-guard", "--lane", "api", "--", "git", "push", "origin", "main")
        self.assertEqual(refused.returncode, 2)
        self.assertIn("refused: denied by rule", refused.stdout)
        unknown = self.bellhop("provider-guard", "--lane", "api", "--", "curl", "http://example.invalid")
        self.assertEqual(unknown.returncode, 2)
        self.assertIn("not on the allowlist", unknown.stdout)
        empty = self.bellhop("provider-guard", "--lane", "api", "--")
        self.assertEqual(empty.returncode, 2)
        bad_lane = self.bellhop("provider-guard", "--lane", "nope", "--", "git", "status")
        self.assertEqual(bad_lane.returncode, 2)

    def test_provider_check_exit_codes(self):
        green = self.bellhop("provider", "check", "api", "--at", "09:00")
        self.assertEqual(green.returncode, 0, green.stdout + green.stderr)
        self.assertIn("claude/opus", green.stdout)
        cfg = read_json(self.tmp / "bellhop.json")
        cfg["lanes"]["api"]["sandbox_profile"] = "providers/gone.sb"
        write_json(self.tmp / "bellhop.json", cfg)
        red = self.bellhop("provider", "check", "api")
        self.assertEqual(red.returncode, 2)
        self.assertIn("🛑", red.stdout)

    def test_provider_check_heavy_line(self):
        cfg = read_json(self.tmp / "bellhop.json")
        cfg["lanes"]["api"]["heavy_model"] = "opus-heavy"
        write_json(self.tmp / "bellhop.json", cfg)
        out = self.bellhop("provider", "check", "api", "--heavy", "--at", "09:00")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("heavy: claude/opus-heavy", out.stdout)


class DaemonVerbTest(unittest.TestCase):
    """`daemon load` and `daemon unload`: assert the commands, load nothing."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bellhop-daemon."))
        self.desk = Desk(self.tmp, {"lanes": {}, "autostart": False})

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_launchd_load_enables_then_bootstraps(self):
        cmds = [" ".join(c) for c in daemon_mod.load_commands(self.desk, "launchd")]
        self.assertTrue(any(c.startswith("launchctl enable") for c in cmds))
        self.assertTrue(any(c.startswith("launchctl bootstrap") for c in cmds))
        unload = [" ".join(c) for c in daemon_mod.unload_commands(self.desk, "launchd")]
        self.assertTrue(any(c.startswith("launchctl bootout") for c in unload))
        self.assertTrue(any(c.startswith("launchctl disable") for c in unload))

    def test_launchd_with_autostart_does_not_touch_enable(self):
        """`launchctl enable` outlives a reboot, so an autostart desk must not set it here."""
        desk = Desk(self.tmp, {"lanes": {}, "autostart": True})
        cmds = [" ".join(c) for c in daemon_mod.load_commands(desk, "launchd")]
        self.assertFalse(any("enable" in c for c in cmds))
        unload = [" ".join(c) for c in daemon_mod.unload_commands(desk, "launchd")]
        self.assertFalse(any(c.startswith("launchctl disable") for c in unload))

    def test_systemd_load_and_unload_start_and_stop(self):
        cmds = [" ".join(c) for c in daemon_mod.load_commands(self.desk, "systemd")]
        self.assertIn("systemctl --user daemon-reload", cmds)
        self.assertTrue(any(c.startswith("systemctl --user start") for c in cmds))
        unload = [" ".join(c) for c in daemon_mod.unload_commands(self.desk, "systemd")]
        self.assertTrue(any(c.startswith("systemctl --user stop") for c in unload))

    def test_load_refuses_without_an_install(self):
        with self.assertRaises(BellhopError) as caught:
            daemon_mod.load(self.desk)
        self.assertIn("daemon install", str(caught.exception))
        with self.assertRaises(BellhopError):
            daemon_mod.unload(self.desk)

    def test_status_shows_loaded_or_not(self):
        write_json(daemon_mod.marker_path(self.desk),
                   {"platform": "launchd", "label": "x", "service_files": [], "loaded": False})
        self.assertIn("loaded: no", daemon_mod.status_text(self.desk))
        write_json(daemon_mod.marker_path(self.desk),
                   {"platform": "launchd", "label": "x", "service_files": [], "loaded": True,
                    "loaded_at": "2026-09-11T00:00:00+0800"})
        text = daemon_mod.status_text(self.desk)
        self.assertIn("loaded: yes", text)
        self.assertIn("2026-09-11", text)


class ProviderWatchTest(unittest.TestCase):
    """The whole path: a bell, a schedule window, a generic stub worker, a receipt."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bellhop-provwatch."))
        self.repo = self.tmp / "widget-service"
        shutil.copytree(ROOT / "examples" / "widget-service", self.repo)
        os.environ["GIT_AUTHOR_NAME"] = os.environ["GIT_COMMITTER_NAME"] = "test"
        os.environ["GIT_AUTHOR_EMAIL"] = os.environ["GIT_COMMITTER_EMAIL"] = "test@example.invalid"
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "initial")
        self.base = git(self.repo, "rev-parse", "--short", "HEAD")
        self.desk_dir = self.tmp / "desk"
        desk_ops.init_desk(self.desk_dir)
        write(self.desk_dir / "providers" / "allow.txt", "allow git:*\nallow python3:*\n")
        cfg = read_json(self.desk_dir / "bellhop.json")
        cfg["lanes"]["api"]["repo"] = "../widget-service"
        cfg["lanes"]["api"]["worker_cmd"] = f"python3 {ROOT}/bellhop/stubworker.py"
        cfg["lanes"]["api"]["allowlist_file"] = "providers/allow.txt"
        write(self.desk_dir / "bellhop.json", json.dumps(cfg, indent=2))
        git(self.desk_dir, "add", "-A")
        git(self.desk_dir, "commit", "-q", "-m", "configure")
        self.cfg = cfg
        self.desk = Desk(self.desk_dir, cfg)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        os.environ.pop("BELLHOP_NOW", None)

    def packet(self, pid="001", title="Note", heavy=False):
        text = (f"---\nid: {pid}\nlane: api\ntitle: {title}\nbase: {self.base}\nbudget_minutes: 10\n"
                + ("effort: heavy\n" if heavy else "")
                + "acceptance:\n  - a note file exists\n---\nAdd a note file.\n")
        path = self.desk.inbox / "api" / f"{pid}-note.md"
        write(path, text)
        return path

    def watchdog_log(self):
        p = self.desk.bells_logs / "watchdog.log"
        return read(p) if p.exists() else ""

    def test_the_override_is_judged_by_the_run_time_not_the_dispatch_time(self):
        """A bell can sit in `pending/` for hours; only the launch clock counts."""
        self.cfg["lanes"]["api"]["schedule"] = [
            {"name": "peak", "days": "mon-fri", "from": "14:00", "to": "18:00",
             "provider": "generic", "model": "other-model"}]
        ring.ring(self.desk, self.packet("001"))
        os.environ["BELLHOP_NOW"] = "2026-09-09T15:00"  # dispatched now, answered inside the window
        result = watch.watch_once(self.desk)
        self.assertIsNotNone(result)
        self.assertFalse(result["dead"], result)
        self.assertEqual(result["provider"], "generic")
        self.assertEqual(result["schedule_override"], "peak")
        self.assertIn("⏰", self.watchdog_log())
        self.assertIn("schedule override → generic/other-model", self.watchdog_log())
        meta = read_json(self.desk.receipts / "001.meta.json")
        self.assertEqual(meta["provider"], "generic")
        self.assertEqual(meta["model_asked"], "other-model")
        self.assertIn('"provider": "generic"', read(self.desk.receipts / "001-note.md"))

    def test_outside_the_window_nothing_moves(self):
        self.cfg["lanes"]["api"]["schedule"] = [
            {"name": "peak", "days": "mon-fri", "from": "14:00", "to": "18:00",
             "provider": "generic", "model": "other-model"}]
        ring.ring(self.desk, self.packet("002"))
        os.environ["BELLHOP_NOW"] = "2026-09-09T09:00"
        result = watch.watch_once(self.desk)
        self.assertEqual(result["provider"], "claude")
        self.assertIsNone(result["schedule_override"])
        self.assertNotIn("⏰", self.watchdog_log())

    def test_a_missing_lane_model_file_stalls_the_bell_with_a_reason(self):
        self.cfg["lanes"]["api"]["schedule"] = [
            {"name": "peak", "days": "mon-fri", "from": "14:00", "to": "18:00",
             "model_file": "state/lane-model-peak.txt"}]
        ring.ring(self.desk, self.packet("003"))
        os.environ["BELLHOP_NOW"] = "2026-09-09T15:00"
        with self.assertRaises(providers.ProviderRefusal) as caught:
            watch.watch_once(self.desk)
        self.assertIn("lane-model-peak.txt", str(caught.exception))
        # the bell is still pending and nothing was started: no receipt, no in-flight row
        self.assertEqual(len(list(self.desk.bells_pending.glob("*.bell"))), 1)
        self.assertFalse((self.desk.receipts / "003-note.md").exists())
        self.assertNotIn("[worker] 003", read(self.desk.state / "in-flight.md"))
        # positive control: put the file there and the same bell runs
        write(self.desk_dir / "state" / "lane-model-peak.txt", "claude\nother-model\n")
        result = watch.watch_once(self.desk)
        self.assertFalse(result["dead"], result)
        self.assertEqual(result["provider"], "claude")

    def test_heavy_packet_reaches_the_footer(self):
        self.cfg["lanes"]["api"]["heavy_model"] = "opus-heavy"
        ring.ring(self.desk, self.packet("004", heavy=True))
        result = watch.watch_once(self.desk)
        self.assertTrue(result["heavy"])
        self.assertEqual(result["model_asked"], "opus-heavy")
        # negative control: the next packet is not heavy
        ring.ring(self.desk, self.packet("005"))
        result = watch.watch_once(self.desk)
        self.assertFalse(result["heavy"])
        self.assertEqual(result["model_asked"], "opus")

    def test_the_footer_keeps_null_apart_from_zero(self):
        """`readings` parses these as numbers; a `n/a` string there would break it."""
        ring.ring(self.desk, self.packet("006"))
        watch.watch_once(self.desk)
        meta = read_json(self.desk.receipts / "006.meta.json")
        self.assertEqual(meta["cost_usd"], 0.0)  # the stub reports zero, and zero it stays
        self.assertEqual(meta["turns"], 3)
        self.assertEqual(meta["stop_reason"], "success")
        json.dumps(meta)  # nothing unserialisable slipped in


class TemplateShapeTest(unittest.TestCase):
    def test_example_config_carries_the_provider_keys(self):
        cfg = read_json(ROOT / "template" / "bellhop.example.json")
        self.assertIn("sandbox_wrapper", cfg)
        lane = cfg["lanes"]["api"]
        for key in ("provider", "heavy_model", "effort", "heavy_effort", "allowlist_file",
                    "sandbox_profile", "schedule"):
            self.assertIn(key, lane, key)
        for key in ("heavy_model", "effort", "heavy_effort", "allowlist_file", "sandbox_profile",
                    "schedule"):
            self.assertIn(key, LANE_DEFAULTS, key)

    def test_the_shipped_sandbox_profile_denies_by_default_and_has_no_network(self):
        text = read(ROOT / "template" / "providers" / "sandbox.example.sb")
        self.assertIn("(deny default)", text)
        self.assertIn("(deny network*)", text)
        self.assertIn("other platforms", text.lower())


if __name__ == "__main__":
    unittest.main()
