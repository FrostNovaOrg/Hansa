"""The principal's page, the resumable release run, and the skill frontmatter.

Every gate here gets a positive case and a negative control, and the negative control
asserts the refusal actually refused: the exit code, and the file it would have written
either absent or byte-for-byte unchanged. A gate that refused and a gate that refused
*and also wrote half of something* look the same in the word "refused".

Nothing here touches the network or the user's real desk: each case builds a desk from
`template/` in a tempfile sandbox, and every release step is a shell command that only
writes into that sandbox.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bellhop import brief as brief_mod  # noqa: E402
from bellhop import decisions as decisions_mod  # noqa: E402
from bellhop import intake as intake_mod  # noqa: E402
from bellhop import release as release_mod  # noqa: E402
from bellhop import state as state_mod  # noqa: E402
from bellhop import stopdispatch as stop_mod  # noqa: E402
from bellhop.config import Desk  # noqa: E402
from bellhop.util import BellhopError, read, ts, write, write_json  # noqa: E402

MAIN = str(ROOT / "bellhop_main.py")


class DeskCase(unittest.TestCase):
    """A desk copied from the template into a tempfile sandbox. No git, no network."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bellhop-skills2-test.")).resolve()
        self.root = self.tmp / "desk"
        shutil.copytree(ROOT / "template", self.root)
        (self.root / "bellhop.example.json").rename(self.root / "bellhop.json")
        self.cfg = json.loads(read(self.root / "bellhop.json"))
        self.cfg.setdefault("lanes", {})
        self.save_cfg()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def save_cfg(self):
        write(self.root / "bellhop.json", json.dumps(self.cfg, indent=2) + "\n")
        self.desk = Desk(self.root, self.cfg)
        return self.desk

    def cli(self, *args):
        return subprocess.run(
            [sys.executable, MAIN, "--desk", str(self.root), *args],
            text=True, capture_output=True, cwd=str(self.tmp),
        )

    def today(self):
        from bellhop.util import today
        return today()


# --------------------------------------------------------------------------
# bellhop brief
# --------------------------------------------------------------------------
class BriefPage(DeskCase):
    def test_waiting_missing_empty_and_full_all_read_differently(self):
        """A missing file, an empty list and a real list must not look alike. On a page
        whose whole point is to be believed without checking, that is the worst bug."""
        text = brief_mod.brief_text(self.desk)
        self.assertIn("== waiting for you (0) ==", text)
        self.assertIn("nothing", text)

        state_mod.todo_add(self.desk, "sign the contract - why only you: only you can sign")
        text = brief_mod.brief_text(self.desk)
        self.assertIn("== waiting for you (1) ==", text)
        self.assertIn("sign the contract", text)

        # negative control: the file removed reads as missing, never as nothing waiting
        (self.root / "state" / "human-todo.md").unlink()
        text = brief_mod.brief_text(self.desk)
        self.assertIn("(state/human-todo.md is not on disk)", text)
        self.assertNotIn("== waiting for you (0) ==", text)

    def test_queue_shows_card_bells_and_the_stop_order(self):
        state_mod.card_set(self.desk, dispatchable="yes", next="ship the health endpoint", lane="api")
        (self.root / "bells" / "pending").mkdir(parents=True)
        write_json(self.root / "bells" / "pending" / "20260911-120000-017.bell",
                   {"id": "017", "lane": "api", "created_at": ts()})
        text = brief_mod.brief_text(self.desk, cap=100000)
        self.assertIn("next: ship the health endpoint", text)
        self.assertIn("dispatchable: yes", text)
        self.assertIn("bells: 1 pending", text)
        self.assertIn("dispatch: allowed", text)

        stop_mod.set_stop(self.desk, "quota ran out")
        text = brief_mod.brief_text(self.desk, cap=100000)
        self.assertIn("dispatch: STOPPED since", text)
        self.assertIn("quota ran out", text)

    def test_counts_read_the_files_and_say_n_a_when_there_is_no_file(self):
        day = self.today()
        # no bells directory at all: that is not "no bells today"
        self.assertIn("dispatched n/a", brief_mod.brief_text(self.desk, cap=100000))

        (self.root / "bells" / "rung").mkdir(parents=True)
        write_json(self.root / "bells" / "rung" / "20260101-090000-016.bell",
                   {"id": "016", "created_at": "2026-01-01T09:00:00+0000"})
        write_json(self.root / "bells" / "rung" / "20260101-100000-017.bell",
                   {"id": "017", "created_at": day + "T10:00:00+0000"})
        state_mod.closings_add(self.desk, "017", "merged")
        intake_mod.log(self.desk, "how is it going")
        text = brief_mod.brief_text(self.desk, cap=100000)
        self.assertIn("dispatched 1, closed 1, 1 message(s) from you", text)

    def test_money_comes_from_readings_or_reads_n_a(self):
        text = brief_mod.brief_text(self.desk, cap=100000)
        self.assertIn("cost today n/a, full chain per task n/a", text)
        self.assertIn("no line for today in state/readings.md", text)

        # a real day line, columns taken from the file's own header row
        day = self.today()
        cells = [day, "40", "120000", "10%", "20%", "$3.40", "1", "2", "$9.10",
                 "5%", "1/2", "0", "8%"]
        with (self.root / "state" / "readings.md").open("a", encoding="utf-8") as fh:
            fh.write("| " + " | ".join(cells) + " |\n")
        text = brief_mod.brief_text(self.desk, cap=100000)
        self.assertIn("cost today $3.40, full chain per task $9.10", text)
        self.assertNotIn("no line for today", text)

    def test_returns_and_decisions(self):
        state_mod.closings_add(self.desk, "017", "merged, nothing outstanding")
        state_mod.closings_add(self.desk, "018", "returned: acceptance item 2 was never checked")
        decisions_mod.decide(self.desk, "ship patch versions without asking", standing=True)
        text = brief_mod.brief_text(self.desk, cap=100000)
        self.assertIn("== returns and doubts (last 3 of 1) ==", text)
        self.assertIn("acceptance item 2", text)
        self.assertNotIn("nothing outstanding", text)
        self.assertIn('standing: "ship patch versions without asking"', text)

    def test_trim_drops_decisions_first_and_never_the_waiting_list(self):
        state_mod.todo_add(self.desk, "sign the contract - why only you: only you can sign")
        decisions_mod.decide(self.desk, "ship patch versions without asking", standing=True)
        full = brief_mod.brief_text(self.desk, cap=100000)
        self.assertIn("== last decisions", full)

        # one byte under the full page: exactly one section goes, and it is decisions
        one_short = brief_mod.brief_text(self.desk, cap=len(full.encode("utf-8")) - 1)
        self.assertIn("dropped: decisions", one_short)
        self.assertNotIn("== last decisions", one_short)
        self.assertIn("sign the contract", one_short)

        # everything trimmable gone, in the declared order, and still the waiting list
        squeezed = brief_mod.brief_text(self.desk, cap=1)
        self.assertIn("dropped: " + ", ".join(brief_mod.TRIM_ORDER), squeezed)
        self.assertIn("sign the contract", squeezed)
        self.assertIn("== waiting for you (1) ==", squeezed)
        # over the cap honestly rather than losing the one row that matters
        self.assertGreater(len(squeezed.encode("utf-8")), 1)

    def test_negative_control_a_page_under_the_cap_drops_nothing(self):
        state_mod.todo_add(self.desk, "sign the contract - why only you: only you can sign")
        decisions_mod.decide(self.desk, "ship patch versions without asking", standing=True)
        text = brief_mod.brief_text(self.desk, cap=100000)
        self.assertNotIn("dropped:", text)
        self.assertIn("== last decisions", text)

    def test_default_cap_comes_from_config(self):
        decisions_mod.decide(self.desk, "ship patch versions without asking", standing=True)
        self.assertIn("== last decisions", brief_mod.brief_text(self.desk))  # 1500 fits
        self.cfg["brief_bytes"] = 200
        self.save_cfg()
        small = brief_mod.brief_text(self.desk)
        self.assertNotIn("== last decisions", small)
        self.assertIn("dropped:", small)
        self.assertIn("== waiting for you", small)


class BriefSend(DeskCase):
    def test_empty_alert_cmd_prints_only_and_records_nothing(self):
        """`--send` with no alert_cmd is a configured state, not a failure: exit 0, and
        no marker, or the desk looks like it has been sending briefs all week."""
        proc = self.cli("brief", "--send")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("no alert_cmd configured", proc.stdout)
        self.assertFalse((self.root / brief_mod.SENT_MARKER).exists())

    def test_a_send_that_worked_records_the_marker(self):
        out = self.tmp / "sent.txt"
        self.cfg["alert_cmd"] = "cat > %s" % out
        self.save_cfg()
        proc = self.cli("brief", "--send")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("== waiting for you", read(out))
        marker = self.root / brief_mod.SENT_MARKER
        self.assertTrue(marker.exists())
        self.assertRegex(read(marker), r"^\d{4}-\d{2}-\d{2}T\S+ \d+ bytes\n$")

    def test_negative_control_a_failed_send_records_nothing_and_exits_1(self):
        self.cfg["alert_cmd"] = "exit 7"
        self.save_cfg()
        proc = self.cli("brief", "--send")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("alert_cmd exited 7", proc.stdout)
        self.assertFalse((self.root / brief_mod.SENT_MARKER).exists())


# --------------------------------------------------------------------------
# bellhop release
# --------------------------------------------------------------------------
class ReleaseRun(DeskCase):
    def steps(self, *specs):
        """specs are (name, shell command) pairs; they only touch the sandbox."""
        self.cfg["release_steps"] = [{"name": n, "cmd": c} for n, c in specs]
        self.save_cfg()

    def marker(self, name):
        return "touch %s/%s.done" % (self.tmp, name)

    def done(self, name):
        return (self.tmp / ("%s.done" % name)).exists()

    def progress(self, version="1.2.3"):
        p = release_mod.progress_path(self.desk, version)
        return json.loads(read(p)) if p.exists() else None

    def run_release(self, *args, **kw):
        """Always captured: a step's own output belongs in the assertion, not on the
        test runner's terminal."""
        return _capture(lambda: release_mod.release(self.desk, *args, **kw))

    # -- the happy path ---------------------------------------------------
    def test_all_steps_run_in_order_and_are_recorded(self):
        self.steps(("preflight", self.marker("preflight")), ("tag", self.marker("tag")))
        self.assertEqual(self.run_release("1.2.3").code, 0)
        self.assertTrue(self.done("preflight") and self.done("tag"))
        rows = self.progress()["steps"]
        self.assertEqual([r["name"] for r in rows], ["preflight", "tag"])
        self.assertEqual([r["status"] for r in rows], ["ok", "ok"])

    def test_version_is_substituted_with_replace_not_format(self):
        """A step containing `{print $1}` is an ordinary step; str.format would raise."""
        self.steps(("tag", "echo v{version} | awk '{print $1}' > %s/tag.txt" % self.tmp))
        self.assertEqual(self.run_release("1.2.3").code, 0)
        self.assertEqual(read(self.tmp / "tag.txt").strip(), "v1.2.3")

    # -- gate: stop at the first non-zero exit ----------------------------
    def test_stops_at_the_first_failure_and_says_where(self):
        self.steps(("preflight", self.marker("preflight")), ("check", "exit 3"),
                   ("tag", self.marker("tag")))
        out = self.run_release("1.2.3")
        self.assertEqual(out.code, 1)
        self.assertIn("\N{OCTAGONAL SIGN} stopped at: check", out.text)
        self.assertIn("--from check", out.text)
        self.assertTrue(self.done("preflight"))
        self.assertFalse(self.done("tag"))  # nothing after the stop ran
        rows = {r["name"]: r for r in self.progress()["steps"]}
        self.assertEqual(rows["preflight"]["status"], "ok")
        self.assertEqual(rows["check"]["status"], "failed")
        self.assertEqual(rows["check"]["exit"], 3)
        self.assertEqual(rows["tag"]["status"], "pending")

    def test_negative_control_a_clean_run_prints_no_stop_line(self):
        self.steps(("preflight", "true"), ("check", "true"))
        out = self.run_release("1.2.3")
        self.assertEqual(out.code, 0)
        self.assertNotIn("stopped at", out.text)
        self.assertIn("released 1.2.3", out.text)

    # -- gate: --from ------------------------------------------------------
    def test_from_resumes_and_counts_earlier_steps_as_skipped(self):
        self.steps(("preflight", self.marker("preflight")), ("check", self.marker("check")))
        self.assertEqual(self.run_release("1.2.3", from_step="check").code, 0)
        self.assertFalse(self.done("preflight"))
        self.assertTrue(self.done("check"))
        rows = {r["name"]: r["status"] for r in self.progress()["steps"]}
        self.assertEqual(rows, {"preflight": "skipped", "check": "ok"})

    def test_a_resumed_run_keeps_what_the_earlier_one_proved(self):
        """A step that passed and a step --from jumped over must not both read
        `skipped`: only one of them has evidence behind it."""
        self.steps(("preflight", self.marker("preflight")), ("check", "exit 4"),
                   ("tag", self.marker("tag")))
        self.assertEqual(self.run_release("1.2.3").code, 1)
        self.steps(("preflight", self.marker("preflight")), ("check", "true"),
                   ("tag", self.marker("tag")))
        self.assertEqual(self.run_release("1.2.3", from_step="check").code, 0)
        rows = {r["name"]: r["status"] for r in self.progress()["steps"]}
        self.assertEqual(rows, {"preflight": "ok", "check": "ok", "tag": "ok"})

    def test_negative_control_an_unknown_from_step_refuses_and_writes_nothing(self):
        self.steps(("preflight", self.marker("preflight")))
        with self.assertRaises(BellhopError) as caught:
            release_mod.release(self.desk, "1.2.3", from_step="tag")
        self.assertIn("no release step named 'tag'", str(caught.exception))
        self.assertIn("the steps are: preflight", str(caught.exception))
        self.assertFalse(self.done("preflight"))
        self.assertIsNone(self.progress())

    def test_a_later_run_says_where_the_previous_one_stopped(self):
        self.steps(("preflight", "exit 1"))
        self.run_release("1.2.3")
        out = self.run_release("1.2.3")
        self.assertIn("stopped at 'preflight'", out.text)

    # -- gate: the stop-dispatch order -------------------------------------
    def test_a_stop_order_refuses_the_release(self):
        self.steps(("preflight", self.marker("preflight")))
        stop_mod.set_stop(self.desk, "quota ran out")
        with self.assertRaises(BellhopError) as caught:
            release_mod.release(self.desk, "1.2.3")
        self.assertIn("dispatch stopped since", str(caught.exception))
        self.assertIn("release_ignores_stop", str(caught.exception))
        self.assertFalse(self.done("preflight"))
        self.assertIsNone(self.progress())

    def test_negative_control_release_ignores_stop_lets_it_through(self):
        self.steps(("preflight", self.marker("preflight")))
        stop_mod.set_stop(self.desk, "quota ran out")
        self.cfg["release_ignores_stop"] = True
        self.save_cfg()
        self.assertEqual(self.run_release("1.2.3").code, 0)
        self.assertTrue(self.done("preflight"))

    # -- gate: the version -------------------------------------------------
    def test_a_version_that_is_also_a_shell_fragment_is_refused(self):
        self.steps(("preflight", "echo {version} > %s/v.txt" % self.tmp))
        for bad in ("1.2.3; touch /tmp/pwned", "../../etc/passwd", "", "$(id)"):
            with self.assertRaises(BellhopError, msg=bad):
                release_mod.release(self.desk, bad)
        self.assertFalse((self.tmp / "v.txt").exists())

    def test_negative_control_an_ordinary_version_is_accepted(self):
        self.steps(("preflight", "echo {version} > %s/v.txt" % self.tmp))
        for good in ("1.2.3", "v1.2.3-rc.1", "2026.09.11+build7"):
            self.assertEqual(self.run_release(good).code, 0)
        self.assertTrue((self.tmp / "v.txt").exists())

    # -- gate: --dry-run ---------------------------------------------------
    def test_dry_run_prints_the_commands_and_writes_nothing(self):
        self.steps(("preflight", self.marker("preflight")), ("tag", self.marker("tag")))
        out = self.run_release("1.2.3", dry_run=True)
        self.assertEqual(out.code, 0)
        self.assertIn("1. preflight:", out.text)
        self.assertIn("2. tag:", out.text)
        self.assertFalse(self.done("preflight"))
        self.assertIsNone(self.progress())

    def test_dry_run_under_a_stop_order_says_a_real_run_would_be_refused(self):
        self.steps(("preflight", "true"))
        stop_mod.set_stop(self.desk, "quota ran out")
        out = self.run_release("1.2.3", dry_run=True)
        self.assertEqual(out.code, 0)
        self.assertIn("a real run would be refused", out.text)

    # -- gate: the steps themselves ----------------------------------------
    def test_no_steps_is_an_error_not_a_release_that_shipped_nothing(self):
        self.cfg["release_steps"] = []
        self.save_cfg()
        with self.assertRaises(BellhopError) as caught:
            release_mod.release(self.desk, "1.2.3")
        self.assertIn("no release_steps", str(caught.exception))

    def test_malformed_steps_are_refused_by_shape(self):
        for bad, needle in (
            ([{"name": "a"}], "needs both"),
            ([{"cmd": "true"}], "needs both"),
            (["true"], "not an object"),
            ([{"name": "a", "cmd": "true"}, {"name": "a", "cmd": "false"}], "two steps named"),
        ):
            self.cfg["release_steps"] = bad
            self.save_cfg()
            with self.assertRaises(BellhopError, msg=str(bad)) as caught:
                release_mod.release(self.desk, "1.2.3")
            self.assertIn(needle, str(caught.exception))

    def test_the_shipped_example_steps_refuse_until_they_are_filled_in(self):
        """An unconfigured desk must stop at step 1, not tag something."""
        example = json.loads(read(ROOT / "template" / "bellhop.example.json"))
        self.assertTrue(example["release_steps"])
        out = self.run_release("1.2.3")
        self.assertEqual(out.code, 1)
        self.assertIn("\N{OCTAGONAL SIGN} stopped at: preflight", out.text)

    # -- the CLI carries the exit code -------------------------------------
    def test_cli_exit_codes(self):
        self.steps(("preflight", "exit 5"))
        proc = self.cli("release", "1.2.3")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("stopped at: preflight", proc.stdout)
        self.steps(("preflight", "true"))
        self.assertEqual(self.cli("release", "1.2.3").returncode, 0)


# --------------------------------------------------------------------------
# every skill is a skill Claude Code can load
# --------------------------------------------------------------------------
NEW_SKILLS = ("shutdown", "resume", "return", "condense", "brief", "release")
SKILL_MAX_LINES = 45
FRONTMATTER_FIELDS = ("name", "description")


class SkillFiles(unittest.TestCase):
    def skills(self):
        return sorted(p for p in (ROOT / "skills").iterdir() if p.is_dir())

    def frontmatter(self, path):
        text = read(path)
        self.assertTrue(text.startswith("---\n"), "%s does not open with frontmatter" % path)
        end = text.index("\n---\n", 3)
        out = {}
        for line in text[4:end].splitlines():
            m = re.match(r"^([a-z-]+):\s*(.*)$", line)
            self.assertIsNotNone(m, "%s: frontmatter line is not `key: value`: %r" % (path, line))
            out[m.group(1)] = m.group(2).strip()
        return out

    def test_every_skill_directory_has_a_skill_file_with_the_expected_frontmatter(self):
        found = self.skills()
        self.assertTrue(found, "no skills found")
        for d in found:
            path = d / "SKILL.md"
            self.assertTrue(path.exists(), "%s has no SKILL.md" % d)
            fm = self.frontmatter(path)
            for field in FRONTMATTER_FIELDS:
                self.assertIn(field, fm, "%s has no `%s` in its frontmatter" % (path, field))
                self.assertTrue(fm[field], "%s has an empty `%s`" % (path, field))
            self.assertEqual(fm["name"], d.name,
                             "%s: frontmatter name %r is not the directory name" % (path, fm["name"]))

    def test_the_new_skills_are_all_present_and_within_the_line_cap(self):
        names = {d.name for d in self.skills()}
        for name in NEW_SKILLS:
            self.assertIn(name, names)
            lines = read(ROOT / "skills" / name / "SKILL.md").splitlines()
            self.assertLessEqual(len(lines), SKILL_MAX_LINES,
                                 "skills/%s/SKILL.md is %d lines, over the %d-line cap"
                                 % (name, len(lines), SKILL_MAX_LINES))

    def test_the_plugin_page_lists_every_skill(self):
        page = read(ROOT / "docs" / "plugin.md")
        for d in self.skills():
            self.assertIn("`/bellhop:%s`" % d.name, page,
                          "docs/plugin.md does not list /bellhop:%s" % d.name)


class ConfigKeys(unittest.TestCase):
    def test_the_new_keys_have_defaults_and_are_in_the_example(self):
        from bellhop.config import DESK_DEFAULTS
        example = json.loads(read(ROOT / "template" / "bellhop.example.json"))
        for key in ("brief_bytes", "brief_times", "alert_cmd", "release_steps",
                    "release_ignores_stop"):
            self.assertIn(key, DESK_DEFAULTS, "%s has no default" % key)
            self.assertIn(key, example, "%s is not in bellhop.example.json" % key)
        self.assertEqual(DESK_DEFAULTS["brief_bytes"], brief_mod.DEFAULT_BRIEF_BYTES)


# --------------------------------------------------------------------------
class _Captured:
    def __init__(self, code, text):
        self.code, self.text = code, text


def _capture(fn):
    """Run fn with stdout redirected. The release steps write to fd 1 directly, so the
    file descriptor is redirected too, not only sys.stdout."""
    import io
    buf = io.StringIO()
    saved_fd = os.dup(1)
    tmp = tempfile.TemporaryFile(mode="w+")
    try:
        sys.stdout.flush()
        os.dup2(tmp.fileno(), 1)
        old, sys.stdout = sys.stdout, buf
        try:
            code = fn()
        finally:
            sys.stdout = old
            sys.stdout.flush()
            os.dup2(saved_fd, 1)
        tmp.seek(0)
        return _Captured(code, buf.getvalue() + tmp.read())
    finally:
        os.close(saved_fd)
        tmp.close()


if __name__ == "__main__":
    unittest.main()
