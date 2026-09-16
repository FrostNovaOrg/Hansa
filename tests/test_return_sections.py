"""`return_inherit_sections`: a named section carried across a `--return`, verbatim.

A return body is the sections the return skill asks for. A desk whose packet adapter
requires another one — "Execution scope" here — used to dispatch a return with a hole
in it where that section should be, and nobody had typed the hole. These tests pin the
fixed chain: inherit the original's section when the return brought none, refuse an
ambiguous original rather than guess, and change nothing for a desk that configured no
titles, an original with no such section, a body that brought its own, or a dispatch
that is not a return.
"""
import contextlib
import io
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

from bellhop import cli, desk_ops, packet as packet_mod  # noqa: E402
from bellhop.config import Desk  # noqa: E402
from bellhop.util import read, read_json, write, write_json  # noqa: E402

GIT_ID = ["-c", "user.name=test", "-c", "user.email=test@example.invalid"]
# Counted the way the gate counts: a standalone level-2 heading, exact title.
SCOPE_RE = re.compile(r"(?m)^##[ \t]+Execution scope[ \t]*$")
SCOPE_JSON = '{"read": ["./check.sh"], "write": ["note.md"], "commands": ["./check.sh"]}'

WHAT = """## What to do

Add a short note file describing the health check. Keep the checks green.

## Do not touch

`check.sh`, and the lane rules.

## Required context

Retry limit: 3.
"""

SCOPE_SECTION = f"""## Execution scope

```json
{SCOPE_JSON}
```
"""

CRITERIA = """## Acceptance

- C1: ./check.sh prints 'check.sh: all green'
- C2: a file named note.md exists in the working tree
"""

FORMAL_BODY = WHAT + "\n" + SCOPE_SECTION + "\n" + CRITERIA

# A `### Execution scope` written inside `## Required context`: content of that
# section, not a scope of the packet.
NESTED = """### Execution scope

A nested look-alike: this heading belongs to Required context, not to the packet.
"""

FORMAL_BODY_NESTED = WHAT + "\n" + NESTED + "\n" + SCOPE_SECTION + "\n" + CRITERIA
PARENT_BODY_NESTED = WHAT + "\n" + NESTED + "\n" + CRITERIA

RETURN_BODY = """## Criteria (unchanged)

See the original packet; the criteria stand as written.

## Why returned

The note is missing; the receipt shows no note.md.

## Change

Add note.md as scoped.

## Do not change

Anything outside the execution scope.
"""


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *GIT_ID, *args],
                          check=True, capture_output=True, text=True).stdout.strip()


class SectionCollectorTest(unittest.TestCase):
    """`standalone_sections` on its own, where the boundaries live."""

    def test_only_a_level_two_heading_with_that_exact_title_counts(self):
        body = ("## Execution scope\n\nfirst\n\n"
                "### Execution scope\n\nnested, part of the first\n\n"
                "## Execution scope extra\n\nnot this one\n")
        found = packet_mod.standalone_sections(body, "Execution scope")
        self.assertEqual(len(found), 1)
        self.assertIn("nested, part of the first", found[0])
        self.assertNotIn("not this one", found[0])

    def test_a_section_ends_at_the_next_heading_of_the_same_level_or_above(self):
        body = "## Scope\n\nkept\n\n### deeper\n\nalso kept\n\n## Next\n\ngone\n\n# Top\n"
        found = packet_mod.standalone_sections(body, "Scope")
        self.assertEqual(found, ["## Scope\n\nkept\n\n### deeper\n\nalso kept"])

    def test_a_heading_inside_a_fence_is_a_line_of_code(self):
        body = "## Scope\n\n```md\n## Scope\nnot a second one\n```\n\ntail\n"
        self.assertEqual(len(packet_mod.standalone_sections(body, "Scope")), 1)

    def test_the_title_is_matched_without_case_or_trailing_whitespace(self):
        body = "##\texecution scope   \n\nbody\n"
        self.assertEqual(len(packet_mod.standalone_sections(body, "Execution Scope")), 1)

    def test_two_sections_with_the_same_title_are_both_returned(self):
        body = "## Scope\n\none\n\n## Scope\n\ntwo\n"
        self.assertEqual(len(packet_mod.standalone_sections(body, "Scope")), 2)

    def test_a_body_without_the_section_returns_nothing(self):
        self.assertEqual(packet_mod.standalone_sections(WHAT, "Execution scope"), [])


class ReturnSectionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bellhop-return-sections."))
        self.repo = self.tmp / "widget-service"
        shutil.copytree(ROOT / "examples" / "widget-service", self.repo)
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "initial")
        self.desk_dir = self.tmp / "desk"
        os.environ["GIT_AUTHOR_NAME"] = os.environ["GIT_COMMITTER_NAME"] = "test"
        os.environ["GIT_AUTHOR_EMAIL"] = os.environ["GIT_COMMITTER_EMAIL"] = "test@example.invalid"
        desk_ops.init_desk(self.desk_dir)
        cfg = read_json(self.desk_dir / "bellhop.json")
        cfg["lanes"]["api"]["repo"] = "../widget-service"
        cfg["return_inherit_sections"] = ["Execution scope"]
        write(self.desk_dir / "bellhop.json", json.dumps(cfg, indent=2))
        git(self.desk_dir, "add", "-A")
        git(self.desk_dir, "commit", "-q", "-m", "configure")
        self.desk = Desk(self.desk_dir, cfg)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def configure(self, titles):
        cfg = read_json(self.desk_dir / "bellhop.json")
        cfg["return_inherit_sections"] = titles
        write(self.desk_dir / "bellhop.json", json.dumps(cfg, indent=2))

    def run_cli(self, *argv):
        buf, err = io.StringIO(), io.StringIO()
        code = 0
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
            try:
                code = cli.main(["--desk", str(self.desk_dir), *argv]) or 0
            except SystemExit as exc:
                code = exc.code or 0
        return code, buf.getvalue() + err.getvalue()

    def dispatch(self, title, body_text, name, *extra):
        p = self.tmp / name
        write(p, body_text)
        return self.run_cli("dispatch", "--lane", "api", "--title", title, "--body", str(p), *extra)

    def original(self, body_text=FORMAL_BODY):
        """Dispatch the original, then give it the receipt a return needs."""
        code, out = self.dispatch("Original task", body_text, "original.md")
        self.assertEqual(code, 0, out)
        path = self.desk.inbox / "api" / "001-original-task.md"
        prior = packet_mod.parse_packet(path)
        write(self.desk.receipts / "001-original-task.md", "# receipt\n")
        write_json(self.desk.receipts / "001.meta.json", {"packet_sha256": prior.sha256})
        return path, prior

    def returned(self):
        return packet_mod.parse_packet(self.desk.inbox / "api" / "002-return-original.md")

    # -- the gap the inheritance closes ------------------------------------

    def test_a_return_inherits_the_configured_section_verbatim(self):
        self.original()
        code, out = self.dispatch("Return original", RETURN_BODY, "return.md", "--return", "001")
        self.assertEqual(code, 0, out)
        self.assertIn("'Execution scope' inherited verbatim", out)
        new = self.returned()
        self.assertEqual(len(SCOPE_RE.findall(new.body)), 1)
        self.assertIn("```json\n" + SCOPE_JSON + "\n```", new.body)  # the bytes, not a rewrite
        self.assertGreater(new.body.index("## Execution scope"), new.body.index("## Do not change"))

    def test_the_rest_of_the_return_still_carries_what_it_always_did(self):
        path, prior = self.original()
        before = read(path)
        code, out = self.dispatch("Return original", RETURN_BODY, "return.md", "--return", "001")
        self.assertEqual(code, 0, out)
        new = self.returned()
        self.assertEqual(new.acceptance, prior.acceptance)
        self.assertEqual(new.base, prior.base)
        self.assertEqual(new.extra.get("returns"), "001")
        self.assertIn("Retry limit: 3.", new.body)                  # required context
        self.assertIn("`check.sh`, and the lane rules.", new.body)  # forbids
        self.assertEqual(read(path), before, "the original packet is never edited")

    def test_a_desk_that_configured_no_titles_is_untouched(self):
        self.configure([])
        self.original()
        code, out = self.dispatch("Return original", RETURN_BODY, "return.md", "--return", "001")
        self.assertEqual(code, 0, out)
        self.assertNotIn("inherited verbatim", out)
        self.assertEqual(SCOPE_RE.findall(self.returned().body), [])

    def test_an_original_without_the_section_changes_nothing(self):
        _path, prior = self.original(WHAT + "\n" + CRITERIA)
        code, out = self.dispatch("Return original", RETURN_BODY, "return.md", "--return", "001")
        self.assertEqual(code, 0, out)
        self.assertIn("inherited from the original packet: acceptance", out)
        self.assertNotIn("inherited verbatim", out)
        new = self.returned()
        self.assertEqual(SCOPE_RE.findall(new.body), [])
        self.assertEqual(new.acceptance, prior.acceptance)

    def test_a_return_body_that_brought_its_own_gets_no_copy(self):
        self.original()
        own = RETURN_BODY + "\n## Execution scope\n\nScoped to note.md only.\n"
        code, out = self.dispatch("Return original", own, "return.md", "--return", "001")
        self.assertEqual(code, 0, out)
        self.assertNotIn("inherited verbatim", out)
        new = self.returned()
        self.assertEqual(len(SCOPE_RE.findall(new.body)), 1)
        self.assertIn("Scoped to note.md only.", new.body)
        self.assertNotIn(SCOPE_JSON, new.body)

    def test_an_original_with_two_such_sections_is_refused_rather_than_guessed(self):
        path, _prior = self.original(WHAT + "\n" + SCOPE_SECTION + "\n" + SCOPE_SECTION + "\n" + CRITERIA)
        before = read(path)
        code, out = self.dispatch("Return original", RETURN_BODY, "return.md", "--return", "001")
        self.assertEqual(code, 2, out)
        self.assertIn("carries 2 '## Execution scope' sections", out)
        self.assertFalse((self.desk.inbox / "api" / "002-return-original.md").exists())
        self.assertEqual(read(path), before)

    def test_a_nested_look_alike_is_context_and_never_the_section(self):
        self.original(FORMAL_BODY_NESTED)
        code, out = self.dispatch("Return original", RETURN_BODY, "return.md", "--return", "001")
        self.assertEqual(code, 0, out)
        new = self.returned()
        self.assertEqual(len(SCOPE_RE.findall(new.body)), 1, "the level-2 heading only")
        self.assertEqual(new.body.count("### Execution scope"), 1, "carried once, with its context")

    def test_an_original_whose_only_look_alike_is_nested_inherits_nothing(self):
        self.original(PARENT_BODY_NESTED)
        code, out = self.dispatch("Return original", RETURN_BODY, "return.md", "--return", "001")
        self.assertEqual(code, 0, out)
        self.assertNotIn("inherited verbatim", out)
        new = self.returned()
        self.assertEqual(SCOPE_RE.findall(new.body), [], "no level-2 heading was invented")
        self.assertEqual(new.body.count("### Execution scope"), 1)

    def test_a_plain_dispatch_never_goes_near_the_inheritance(self):
        code, out = self.dispatch("Formal task", FORMAL_BODY, "body.md")
        self.assertEqual(code, 0, out)
        self.assertNotIn("↩", out)
        pkt = packet_mod.parse_packet(self.desk.inbox / "api" / "001-formal-task.md")
        self.assertEqual(len(SCOPE_RE.findall(pkt.body)), 1)
        self.assertIn(SCOPE_JSON, pkt.body)

    def test_a_setting_that_is_not_a_list_of_titles_stops_the_dispatch(self):
        self.configure("Execution scope")
        self.original()
        code, out = self.dispatch("Return original", RETURN_BODY, "return.md", "--return", "001")
        self.assertEqual(code, 2, out)
        self.assertIn("return_inherit_sections", out)


if __name__ == "__main__":
    unittest.main()
