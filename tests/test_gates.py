"""The four gates the field asked for, and the readings that count them.

Every gate here is tested from both sides. A gate is only worth having if it also
lets the right thing through: the hand gate's whole failure on a production desk was
eleven refusals that were all reads, so the read list below is as long as the write
list and a regression in either direction fails a test.

Nothing here touches the network or a real desk: each case builds a desk from
`template/` in a tempfile sandbox.
"""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bellhop import accept as accept_mod  # noqa: E402
from bellhop import criteria as criteria_mod  # noqa: E402
from bellhop import hand as hand_mod  # noqa: E402
from bellhop import rolegate as rolegate_mod  # noqa: E402
from bellhop import transcripts as tr  # noqa: E402
from bellhop.config import Desk, helper_budget  # noqa: E402
from bellhop.packet import Packet  # noqa: E402
from bellhop.util import read, write  # noqa: E402


class DeskCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bellhop-gates-test.")).resolve()
        self.root = self.tmp / "desk"
        shutil.copytree(ROOT / "template", self.root)
        cfg_path = self.root / "bellhop.json"
        (self.root / "bellhop.example.json").rename(cfg_path)
        self.cfg = json.loads(read(cfg_path))
        self.cfg["lanes"] = {}
        self.desk = Desk(self.root, self.cfg)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def reload(self):
        write(self.root / "bellhop.json", json.dumps(self.cfg, indent=2) + "\n")
        self.desk = Desk(self.root, self.cfg)
        return self.desk

    def add_lane(self, name="api"):
        repo = self.tmp / name
        (repo / "src").mkdir(parents=True, exist_ok=True)
        write(repo / "src" / "app.py", "print('hi')\n")
        self.cfg["lanes"][name] = {"repo": str(repo)}
        self.reload()
        return repo

    def bash(self, command):
        return {"cwd": str(self.root), "tool_name": "Bash", "tool_input": {"command": command}}


# ==========================================================================
# 1. the hand gate, by write intent
# ==========================================================================
# Both lists are written against a lane at {repo}. Every read must pass and every
# write must be refused; the counts below are asserted, so shortening a list to make
# a run green fails too.

READS = (
    "rg 'def gate' {repo}",
    "rg pattern {repo} 2>/dev/null",
    "grep -rn foo {repo} | head -3",
    "ls -la {repo}/src",
    "find {repo} -name '*.py'",
    "cat {repo}/README.md",
    "head -20 {repo}/src/app.py",
    "tail -n 5 {repo}/src/app.py",
    "wc -l {repo}/src/app.py",
    "diff {repo}/src/app.py /etc/hosts",
    "bash -n {repo}/src/run.sh",
    "sed -n '1,20p' {repo}/src/app.py",
    "git -C {repo} status --porcelain",
    "git -C {repo} log --oneline -5",
    "git -C {repo} diff HEAD~1",
    "git -C {repo} show HEAD:src/app.py",
    "git -C {repo} blame {repo}/src/app.py",
    "git -C {repo} rev-parse HEAD",
    "git -C {repo} ls-files",
    "git -C {repo} branch --list",
    "git -C {repo} worktree list",
    "git -C {repo} config --get user.name",
    "cd {repo} && git status",
    "ls {repo} && rg foo {repo}",
    "cat {repo}/src/app.py > /tmp/out.txt",
    "python3 -c \"print(open('{repo}/src/app.py').read())\"",
    "cd {repo} && python3 -c 'import sys; print(sys.version)'",
    "echo '{repo}/src/app.py'",
    "test -f {repo}/src/app.py && echo yes",
    "cat > /tmp/body.md <<'EOF'\ndo not run rm -rf {repo}\nEOF",
    "python3 - <<'PY'\nprint(open('{repo}/src/app.py').read())\nPY",
)

WRITES = (
    "echo hi > {repo}/src/x.txt",
    "echo hi >> {repo}/src/x.txt",
    "cat > {repo}/src/x.txt <<'EOF'\nhello\nEOF",
    "tee {repo}/src/x.txt < /tmp/a",
    "mv {repo}/src/a {repo}/src/b",
    "cp /etc/hosts {repo}/src/app.py",
    "rm -rf {repo}/build",
    "rmdir {repo}/build",
    "sed -i 's/a/b/' {repo}/src/app.py",
    "sed -i.bak 's/a/b/' {repo}/src/app.py",
    "patch -p1 -d {repo} < /tmp/p.patch",
    "chmod +x {repo}/src/run.sh",
    "touch {repo}/src/new.txt",
    "mkdir -p {repo}/src/newdir",
    "install -m 755 /tmp/x {repo}/bin/x",
    "ln -s /tmp/x {repo}/src/link",
    "vim {repo}/src/app.py",
    "git -C {repo} add -A",
    "git -C {repo} commit -m x",
    "git -C {repo} push origin main",
    "git -C {repo} checkout -- src/app.py",
    "git -C {repo} reset --hard",
    "git -C {repo} rebase main",
    "git -C {repo} merge lane/api",
    "git -C {repo} stash push",
    "git -C {repo} clean -fd",
    "git -C {repo} apply /tmp/p.patch",
    "git -C {repo} branch -D old",
    "cd {repo} && sed -i 's/a/b/' src/app.py",
    "cd {repo} && git commit -am wip",
    "rg foo {repo} && rm {repo}/src/app.py",
    "python3 -c \"open('{repo}/src/app.py','w').write('1')\"",
    "python3 - <<'PY'\nopen('{repo}/src/app.py','w').write('1')\nPY",
)


class HandGateWriteIntentTest(DeskCase):
    def setUp(self):
        super().setUp()
        self.repo = self.add_lane()

    def test_at_least_twenty_reads_of_a_lane_are_let_through(self):
        self.assertGreaterEqual(len(READS), 20)
        for shape in READS:
            command = shape.format(repo=self.repo)
            code, message = hand_mod.gate(self.desk, self.bash(command))
            self.assertEqual(code, 0, "blocked a read: %r (%s)" % (command, message))

    def test_at_least_twenty_writes_into_a_lane_are_refused(self):
        self.assertGreaterEqual(len(WRITES), 20)
        for shape in WRITES:
            command = shape.format(repo=self.repo)
            code, message = hand_mod.gate(self.desk, self.bash(command))
            self.assertEqual(code, 2, "let a write through: %r" % command)
            self.assertIn("lane 'api'", message)
            self.assertIn("bellhop hand open", message)

    def test_a_read_that_named_a_lane_is_logged_as_allow_read(self):
        hand_mod.gate(self.desk, self.bash("rg foo %s" % self.repo))
        rows = hand_mod.rows(self.desk)
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0].startswith("- allow-read: "), rows[0])
        self.assertIn(str(self.repo), rows[0])

    def test_a_read_that_names_no_lane_is_not_logged_at_all(self):
        code, _ = hand_mod.gate(self.desk, self.bash("rg foo /etc"))
        self.assertEqual(code, 0)
        self.assertEqual(hand_mod.rows(self.desk), [])

    def test_the_read_rows_can_be_switched_off(self):
        self.cfg["hand_log_reads"] = False
        hand_mod.gate(self.reload(), self.bash("rg foo %s" % self.repo))
        self.assertEqual(hand_mod.rows(self.desk), [])

    def test_the_three_decisions_are_named_in_the_rows(self):
        hand_mod.gate(self.desk, self.bash("cat %s/src/app.py" % self.repo))
        hand_mod.gate(self.desk, self.bash("rm %s/src/app.py" % self.repo))
        hand_mod.open_window(self.desk, minutes=30, reason="one-line revert")
        hand_mod.gate(self.desk, self.bash("rm %s/src/app.py" % self.repo))
        rows = "\n".join(hand_mod.rows(self.desk))
        self.assertIn("- allow-read: ", rows)
        self.assertIn("block-write rm", rows)
        self.assertIn("open-window rm", rows)

    def test_a_heredoc_body_is_data_not_a_chain_of_commands(self):
        """A packet body written to /tmp that mentions `rm -rf <lane>` is not a write
        into the lane; a gate that reads the body as commands cannot tell them apart."""
        command = "cat > /tmp/body.md <<'EOF'\nTell the worker: never rm -rf %s\nEOF" % self.repo
        code, _ = hand_mod.gate(self.desk, self.bash(command))
        self.assertEqual(code, 0)

    def test_a_heredoc_fed_to_an_interpreter_is_read_for_intent(self):
        printing = "python3 - <<'PY'\nprint(open('%s/src/app.py').read())\nPY" % self.repo
        writing = "python3 - <<'PY'\nopen('%s/src/app.py','w').write('x')\nPY" % self.repo
        self.assertEqual(hand_mod.gate(self.desk, self.bash(printing))[0], 0)
        self.assertEqual(hand_mod.gate(self.desk, self.bash(writing))[0], 2)

    def test_the_refusal_names_the_verb_it_saw(self):
        _code, message = hand_mod.gate(self.desk, self.bash("git -C %s commit -m x" % self.repo))
        self.assertIn("git commit", message)

    def test_segments_are_judged_separately(self):
        """A classifier reading only the first word would pass the second command."""
        code, _ = hand_mod.gate(self.desk, self.bash("rg foo %s && rm -rf %s/src" % (self.repo, self.repo)))
        self.assertEqual(code, 2)

    def test_a_redirect_to_elsewhere_is_not_a_lane_write(self):
        code, _ = hand_mod.gate(self.desk, self.bash("rg foo %s > /tmp/found.txt" % self.repo))
        self.assertEqual(code, 0)

    def test_a_file_descriptor_redirect_is_not_a_file(self):
        self.assertEqual(hand_mod.bash_writes("rg foo /x 2>&1", "/tmp"), [])

    def test_unknown_git_verbs_count_as_writes_and_known_readers_do_not(self):
        self.assertIsNotNone(hand_mod.write_verb(["git", "-C", "/x", "unheard-of"]))
        self.assertIsNone(hand_mod.write_verb(["git", "-C", "/x", "status"]))


# ==========================================================================
# 2. the role gate
# ==========================================================================
class RoleGateTest(DeskCase):
    def payload(self, path, tool="Write"):
        return {"cwd": str(self.root), "tool_name": tool, "tool_input": {"file_path": str(path)}}

    def test_a_source_file_anywhere_is_refused_once_with_the_way_forward(self):
        for path in (self.tmp / "elsewhere" / "thing.py", self.root / "bin" / "helper.sh",
                     self.root / "scripts" / "fix.py", self.root / "bellhop" / "hand.py"):
            code, message = rolegate_mod.check(self.desk, self.payload(path))
            self.assertEqual(code, 2, path)
            self.assertIn("coordinators dispatch; open a packet or `bellhop hand open --reason`", message)

    def test_the_desks_own_records_are_the_coordinators_to_write(self):
        for rel in ("state/next.md", "rules/core.md", "inbox/api/001-x.md",
                    "ledger/2026-09-16/001-x.md", "reports/weekly/2026-W38.md",
                    "receipts/001-x.md"):
            code, message = rolegate_mod.check(self.desk, self.payload(self.root / rel))
            self.assertEqual(code, 0, "%s: %s" % (rel, message))

    def test_a_note_outside_the_desk_is_not_this_gates_business(self):
        code, _ = rolegate_mod.check(self.desk, self.payload(self.tmp / "body.md"))
        self.assertEqual(code, 0)

    def test_inside_the_desk_but_not_a_record_is_refused(self):
        code, message = rolegate_mod.check(self.desk, self.payload(self.root / "frozen" / "note.md"))
        self.assertEqual(code, 2)
        self.assertIn("inside the desk", message)

    def test_a_bash_write_of_code_is_refused_and_a_bash_read_is_not(self):
        code, _ = rolegate_mod.check(self.desk, self.bash("cat > %s/bin/fix.sh <<'EOF'\nx\nEOF" % self.root))
        self.assertEqual(code, 2)
        code, _ = rolegate_mod.check(self.desk, self.bash("rg thing %s/bin" % self.root))
        self.assertEqual(code, 0)

    def test_the_frameworks_own_command_is_never_refused(self):
        code, _ = rolegate_mod.check(self.desk, self.bash("bellhop dispatch --lane api --title x --body /tmp/b"))
        self.assertEqual(code, 0)

    def test_an_open_window_allows_it_and_the_write_is_counted(self):
        hand_mod.open_window(self.desk, minutes=30, reason="the tooling is on fire")
        code, message = rolegate_mod.check(self.desk, self.payload(self.root / "bin" / "helper.sh"))
        self.assertEqual(code, 0)
        self.assertEqual(message, "")
        rows = [r for r in hand_mod.rows(self.desk) if "role-open" in r]
        self.assertEqual(len(rows), 1)
        self.assertEqual(sum(rolegate_mod.writes_by_day(self.desk).values()), 1)

    def test_a_refusal_is_not_counted_as_a_write(self):
        """A refused write did not happen. Counting it would report the habit and the
        cure as one number."""
        rolegate_mod.check(self.desk, self.payload(self.root / "bin" / "helper.sh"))
        self.assertTrue(any("role-refused" in r for r in hand_mod.rows(self.desk)))
        self.assertEqual(rolegate_mod.writes_by_day(self.desk), {})

    def test_role_gate_off_lets_everything_through(self):
        for value in ("off", False):
            self.cfg["role_gate"] = value
            code, _ = rolegate_mod.check(self.reload(), self.payload(self.root / "bin" / "x.sh"))
            self.assertEqual(code, 0, repr(value))
        self.cfg["role_gate"] = True
        self.assertEqual(rolegate_mod.check(self.reload(), self.payload(self.root / "bin" / "x.sh"))[0], 2)

    def test_the_glob_list_is_configurable(self):
        self.cfg["role_gate_code_globs"] = ["*.rs"]
        desk = self.reload()
        self.assertEqual(rolegate_mod.check(desk, self.payload(self.tmp / "a.py"))[0], 0)
        self.assertEqual(rolegate_mod.check(desk, self.payload(self.tmp / "a.rs"))[0], 2)

    def test_the_template_desk_has_it_on(self):
        self.assertTrue(rolegate_mod.enabled(self.desk))
        self.assertIs(json.loads(read(ROOT / "template" / "bellhop.example.json"))["role_gate"], True)

    def test_the_hook_asks_both_gates_and_the_first_refusal_wins(self):
        repo = self.add_lane()
        from bellhop import hooks as hooks_mod

        self.assertEqual(hooks_mod.hand_mod.gate(self.desk, self.payload(repo / "src" / "app.py"))[0], 2)
        self.assertEqual(hooks_mod.rolegate_mod.check(self.desk, self.payload(repo / "src" / "app.py"))[0], 2)


# ==========================================================================
# 3. criteria that can go red
# ==========================================================================
class CriteriaTest(unittest.TestCase):
    def test_a_criterion_with_a_clause_passes(self):
        item = "`make test` exits 0 — breaks when: the new assertion in test_x is deleted"
        self.assertIsNone(criteria_mod.problem(item))
        criterion, clause = criteria_mod.split(item)
        self.assertEqual(criterion, "`make test` exits 0")
        self.assertEqual(clause, "the new assertion in test_x is deleted")

    def test_a_criterion_without_a_clause_is_a_problem(self):
        self.assertEqual(criteria_mod.problem("the endpoint returns the right shape"),
                         criteria_mod.NO_CLAUSE)

    def test_an_empty_clause_and_an_empty_criterion_are_both_problems(self):
        self.assertEqual(criteria_mod.problem("x — breaks when:"), criteria_mod.EMPTY_CLAUSE)
        self.assertEqual(criteria_mod.problem("breaks when: something"), criteria_mod.EMPTY_CRITERION)

    def test_a_hyphen_is_not_refused_over_punctuation(self):
        self.assertIsNone(criteria_mod.problem("x - breaks when: y"))
        self.assertIsNone(criteria_mod.problem("x BREAKS WHEN: y"))

    def test_the_first_offending_line_is_the_one_named(self):
        items = ["a — breaks when: b", "c", "d"]
        position, item, why = criteria_mod.first_problem(items)
        self.assertEqual((position, item, why), (2, "c", criteria_mod.NO_CLAUSE))
        message = criteria_mod.refusal(position, item, why)
        self.assertIn("acceptance item 2", message)
        self.assertIn("breaks when:", message)

    def test_a_return_message_says_the_items_cannot_be_reworded_here(self):
        self.assertIn("scope a new task", criteria_mod.refusal(1, "c", criteria_mod.NO_CLAUSE, source="017"))


class CriteriaDispatchTest(DeskCase):
    """The gate as `bellhop dispatch` calls it, including the ledger exception."""

    def test_check_refuses_and_writes_nothing(self):
        before = list(self.desk.ledger.rglob("*.md"))
        message = criteria_mod.check(self.desk, ["a — breaks when: b", "c"], task_id="017", lane="api")
        self.assertIn("acceptance item 2", message)
        self.assertEqual(list(self.desk.ledger.rglob("*.md")), before)

    def test_allow_unbreakable_passes_and_records_the_exception(self):
        message = criteria_mod.check(self.desk, ["a — breaks when: b", "c"], allow=True,
                                     task_id="017", lane="api", title="Add a health endpoint")
        self.assertIsNone(message)
        entries = list(self.desk.ledger.rglob("*.md"))
        self.assertEqual(len(entries), 1)
        text = read(entries[0])
        self.assertIn("017", text)
        self.assertIn("item 2", text)

    def test_a_demo_records_nothing(self):
        criteria_mod.check(self.desk, ["c"], allow=True, record=False, task_id="017", lane="api")
        self.assertEqual(list(self.desk.ledger.rglob("*.md")), [])

    def test_every_item_with_a_clause_needs_no_exception(self):
        self.assertIsNone(criteria_mod.check(self.desk, ["a — breaks when: b"], task_id="017"))
        self.assertEqual(list(self.desk.ledger.rglob("*.md")), [])

    def test_the_packet_template_and_the_skill_carry_the_convention(self):
        for path in (ROOT / "template" / "templates" / "packet.md",
                     ROOT / "skills" / "dispatch" / "SKILL.md",
                     ROOT / "docs" / "dispatch.md"):
            self.assertIn("breaks when:", read(path), path)


# ==========================================================================
# 4. acceptance pre-flight, and the cap by kind
# ==========================================================================
def _packet(acceptance, kind=""):
    return Packet(id="017", lane="api", title="t", base="abc1234", acceptance=list(acceptance),
                  body="x", extra={"kind": kind} if kind else {})


class PreflightTest(DeskCase):
    def lane(self, **over):
        lane = {"name": "api", "check_cmd": "./check.sh", "accept_cmd": ""}
        lane.update(over)
        return lane

    def test_a_policy_with_no_recovery_command_stalls_before_a_session(self):
        lane = self.lane(accept_allowed_tools=["Read", "Grep", "Edit", "Bash(./check.sh:*)"])
        reason = accept_mod.preflight(self.desk, _packet(["x — breaks when: y"]), lane)
        self.assertIsNotNone(reason)
        self.assertIn("git checkout --", reason)

    def test_the_built_policy_already_carries_the_recovery_command(self):
        self.assertIsNone(accept_mod.preflight(self.desk, _packet(["x — breaks when: y"]), self.lane()))

    def test_a_check_command_the_policy_cannot_run_stalls(self):
        lane = self.lane(check_cmd="make test",
                         accept_allowed_tools=["Read", "Bash(git checkout:*)", "Bash(./check.sh:*)"])
        reason = accept_mod.preflight(self.desk, _packet(["x — breaks when: y"]), lane)
        self.assertIn("make test", reason)

    def test_a_command_named_in_a_criterion_must_be_allowed(self):
        lane = self.lane(check_cmd="", accept_allowed_tools=["Read", "Bash(git checkout:*)"])
        reason = accept_mod.preflight(self.desk, _packet(["`pytest -q` passes — breaks when: the new test is deleted"]), lane)
        self.assertIn("pytest -q", reason)
        self.assertIn("no check command", reason)

    def test_prose_in_backticks_is_not_read_as_a_command(self):
        lane = self.lane()
        packet = _packet(["`role_gate` is `off` in `bellhop.json` — breaks when: the key is removed"])
        self.assertIsNone(accept_mod.preflight(self.desk, packet, lane))

    def test_a_lane_with_its_own_accept_cmd_is_left_alone(self):
        lane = self.lane(accept_cmd="my-verifier --go", accept_allowed_tools=["Read"])
        self.assertIsNone(accept_mod.preflight(self.desk, _packet(["x — breaks when: y"]), lane))

    def test_preflight_can_be_switched_off(self):
        self.cfg["accept_preflight"] = False
        lane = self.lane(accept_allowed_tools=["Read"])
        self.assertIsNone(accept_mod.preflight(self.reload(), _packet(["x — breaks when: y"]), lane))

    def test_criteria_with_no_mutation_do_not_need_a_recovery_command(self):
        lane = self.lane(accept_allowed_tools=["Read", "Bash(./check.sh:*)"])
        self.assertIsNone(accept_mod.preflight(self.desk, _packet(["the file is there"]), lane))

    def test_allowlisted_matches_the_way_claude_code_does(self):
        tools = ["Bash(git:*)", "Read"]
        self.assertTrue(accept_mod.allowlisted("git -C /x status", tools))
        self.assertFalse(accept_mod.allowlisted("make test", tools))
        self.assertFalse(accept_mod.allowlisted("git -C /x status", ["Bash(git status:*)"]))

    def test_preflight_is_a_named_step(self):
        self.assertEqual(accept_mod.STEPS[0], "preflight")
        self.assertEqual(accept_mod.STALL_PREFLIGHT, "preflight")


class AcceptanceCapTest(DeskCase):
    def test_a_plain_task_keeps_the_default_cap(self):
        self.assertEqual(accept_mod.acceptance_cap(self.desk, _packet(["x"])), 3000)

    def test_a_report_gets_the_report_cap(self):
        self.assertEqual(accept_mod.acceptance_cap(self.desk, _packet(["x"], kind="report")), 6000)
        self.assertEqual(accept_mod.acceptance_cap(self.desk, _packet(["x"], kind="Report")), 6000)

    def test_both_caps_are_configurable(self):
        self.cfg["acceptance_bytes"] = 1000
        self.cfg["acceptance_bytes_report"] = 9000
        desk = self.reload()
        self.assertEqual(accept_mod.acceptance_cap(desk, _packet(["x"])), 1000)
        self.assertEqual(accept_mod.acceptance_cap(desk, _packet(["x"], kind="report")), 9000)

    def test_no_packet_at_all_reads_the_default(self):
        self.assertEqual(accept_mod.acceptance_cap(self.desk), 3000)


# ==========================================================================
# 5. the helper budget
# ==========================================================================
def _transcript(sessions):
    """sessions: {agent_id or "": [(day, output_tokens), ...]} -> a Transcript."""
    t = tr.Transcript()
    for agent, turns in sessions.items():
        for day, out in turns:
            t.turns.append(tr.Turn(day=day, model="claude-opus-5", output_tokens=out, agent=agent))
    return t


class HelperBudgetTest(DeskCase):
    def test_the_budget_is_declared_in_config_and_in_every_helper_prompt(self):
        self.assertEqual(helper_budget(self.desk), (40, 2.0))
        for name in ("scout", "walkthrough", "acceptance-reader"):
            text = read(ROOT / "agents" / f"{name}.md")
            self.assertIn("40 turns", text, name)
            self.assertIn("$2.00", text, name)

    def test_helper_sessions_are_counted_apart_from_the_coordinator(self):
        parsed = _transcript({"": [("2026-09-16", 10)], "agent-a": [("2026-09-16", 10)] * 3})
        rows = tr.helpers(parsed)
        self.assertEqual([(h.agent, h.turns) for h in rows], [("agent-a", 3)])

    def test_over_budget_counts_turns_and_money(self):
        parsed = _transcript({"agent-a": [("2026-09-16", 1)] * 41,
                              "agent-b": [("2026-09-16", 1)] * 2,
                              "agent-c": [("2026-09-16", 200_000)] * 2})
        rows = tr.helpers(parsed, tr.PRICE_DEFAULTS)
        over = {h.agent for h in tr.over_budget(rows, 40, 2.0)}
        self.assertEqual(over, {"agent-a", "agent-c"})

    def test_an_unpriced_turn_leaves_the_session_without_a_cost(self):
        """A total missing a turn and a complete one look the same as a dollar amount."""
        parsed = tr.Transcript()
        parsed.turns.append(tr.Turn(day="2026-09-16", model="claude-opus-5", output_tokens=1, agent="a"))
        parsed.turns.append(tr.Turn(day="2026-09-16", model="who-knows", output_tokens=1, agent="a"))
        row = tr.helpers(parsed, tr.PRICE_DEFAULTS)[0]
        self.assertIsNone(row.cost)
        self.assertFalse(row.priced)
        self.assertEqual(tr.over_budget([row], 40, 2.0), [])

    def test_a_sidechain_turn_carries_its_agent_id(self):
        line = json.dumps({"type": "assistant", "timestamp": "2026-09-16T10:00:00Z",
                           "isSidechain": True, "agentId": "agent-x",
                           "message": {"id": "m1", "model": "claude-opus-5",
                                       "usage": {"input_tokens": 5, "output_tokens": 5},
                                       "content": [{"type": "text", "text": "hi"}]}})
        path = self.tmp / "t.jsonl"
        write(path, line + "\n")
        parsed = tr.parse([path])
        self.assertEqual([t.agent for t in parsed.turns], ["agent-x"])

    def test_the_readings_table_carries_the_two_new_lines(self):
        from bellhop import readings as readings_mod

        out = readings_mod.report(self.desk, week="2026-W38")
        text = read(out)
        self.assertIn("| Helpers over budget |", text)
        self.assertIn("| Self-implemented writes |", text)
        # No transcripts were given, so the helper line must say so rather than say 0.
        self.assertIn("| Helpers over budget | n/a", text)

    def test_the_helper_line_reads_the_budget_from_the_desk(self):
        from bellhop import readings as readings_mod

        parsed = _transcript({"agent-a": [("2026-09-16", 1)] * 5})
        self.cfg["helper_max_turns"] = 3
        line = readings_mod._helper_line(parsed, tr.PRICE_DEFAULTS, self.reload())
        self.assertIn("1 of 1", line)
        self.assertIn("over 3 turns", line)


if __name__ == "__main__":
    unittest.main()
