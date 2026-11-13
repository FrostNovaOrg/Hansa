"""Check local Markdown links and Chinese translations without network requests."""
import html
from itertools import zip_longest
from pathlib import Path
import re
import subprocess
import tempfile
import unicodedata
import unittest
from urllib.parse import unquote, urlsplit


ROOT = Path(__file__).resolve().parent.parent
INLINE_CODE = re.compile(r"(?<![\\`])(`+)(?!`)([\s\S]*?)(?<!`)\1(?!`)")
LINK_START = re.compile(r"(?<!!)\[(?:\\.|[^\]\\])*\]\(\s*")


def blank(text):
    """Hide syntax while preserving offsets and line numbers."""
    return re.sub(r"[^\n]", " ", text)


def fenced_blocks(text):
    """Yield (opening line, language, exact source) for backtick and tilde fences."""
    fence = None
    for number, line in enumerate(text.splitlines(keepends=True), 1):
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if fence is not None:
            block.append(line)
            if (marker and marker[1][0] == fence[0] and
                    len(marker[1]) >= len(fence) and not marker[2].strip()):
                yield opening, language, "".join(block)
                fence = None
        elif marker:
            fence = marker[1]
            opening, block = number, [line]
            language = marker[2].strip().split()[0] if marker[2].strip() else ""
    if fence is not None:  # Markdown permits an unclosed fence extending to EOF.
        yield opening, language, "".join(block)


def without_fences(text):
    lines = text.splitlines(keepends=True)
    for opening, _, block in fenced_blocks(text):
        start = opening - 1
        end = start + len(block.splitlines(keepends=True))
        lines[start:end] = [blank(line) for line in lines[start:end]]
    return "".join(lines)


def links(text):
    """Yield (line, destination) for inline links, including balanced path parentheses."""
    visible = INLINE_CODE.sub(lambda match: blank(match[0]), without_fences(text))
    for match in LINK_START.finditer(visible):
        start = end = match.end()
        if visible[start:start + 1] == "<":
            end = visible.find(">", start + 1)
            if end == -1:
                continue
            destination = visible[start + 1:end]
            end += 1
        else:
            depth = 0
            while end < len(visible):
                char = visible[end]
                if char == "\\" and end + 1 < len(visible):
                    end += 2
                    continue
                if char == "(":
                    depth += 1
                elif char == ")":
                    if depth == 0:
                        break
                    depth -= 1
                elif char.isspace():
                    break
                end += 1
            destination = visible[start:end]
        # A destination may be followed by an optional Markdown link title.
        if re.match(r'''\s*(?:"[^"\n]*"|'[^'\n]*'|\([^\n]*?\))?\s*\)''', visible[end:]):
            destination = html.unescape(re.sub(r"\\([!\"#$%&'()*+,\-./:;<=>?@\[\]\\^_`{|}~])",
                                               r"\1", destination))
            yield visible.count("\n", 0, match.start()) + 1, destination


def headings(text):
    """Yield (line, level, title), ignoring fenced and inline code examples."""
    text = without_fences(text)
    visible = INLINE_CODE.sub(lambda match: blank(match[0]), text)
    lines = text.splitlines()
    visible_lines = visible.splitlines()
    for index, line in enumerate(lines):
        heading = (re.match(r"^ {0,3}#{1,6}(?:[ \t]+(.*?)|[ \t]*)$", line)
                   if re.match(r"^ {0,3}#{1,6}(?:[ \t]|$)", visible_lines[index]) else None)
        if heading:
            title = re.sub(r"[ \t]+#+[ \t]*$", "", heading[1] or "")
            level = len(line.lstrip().split()[0])
        elif (index + 1 < len(lines) and visible_lines[index].strip() and
              re.fullmatch(r" {0,3}(?:=+|-+)[ \t]*", visible_lines[index + 1])):
            title = line.strip()
            level = 1 if visible_lines[index + 1].lstrip().startswith("=") else 2
        else:
            continue
        yield index + 1, level, title


def anchors(text):
    visible = INLINE_CODE.sub(lambda match: blank(match[0]), without_fences(text))
    found = set()
    for match in re.finditer(r'''<a\s+[^>]*\bname\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))[^>]*>''',
                             visible, re.IGNORECASE):
        found.add(html.unescape(next(value for value in match.groups() if value is not None)))
    used = set()
    for _, _, title in headings(text):
        # Render text relevant to a heading's slug: keep code/link labels, drop HTML.
        title = INLINE_CODE.sub(lambda match: match[2], title)
        title = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", title)
        title = html.unescape(re.sub(r"<[^>]*>", "", title)).strip().lower()
        base = "".join("-" if char.isspace() else char for char in title
                       if char in "_-" or char.isspace() or
                       unicodedata.category(char)[0] in "LMN")
        slug, suffix = base, 0
        while slug in used:
            suffix += 1
            slug = f"{base}-{suffix}"
        used.add(slug)
        found.add(slug)
    return found


def translation_error(english, chinese):
    """Return the first structural drift; translated wording is reviewed by people."""
    for index, (expected, actual) in enumerate(zip_longest(headings(english), headings(chinese)), 1):
        if expected is None or actual is None or expected[1] != actual[1]:
            def describe(heading):
                if heading is None:
                    return "missing"
                line, level, title = heading
                return f"H{level} {title!r} (line {line})"
            return f"heading {index} differs: English {describe(expected)}; Chinese {describe(actual)}"

    english_blocks, chinese_blocks = list(fenced_blocks(english)), list(fenced_blocks(chinese))
    expected_blocks = [block for block in english_blocks if block[1] != "mermaid"]
    actual_blocks = [block for block in chinese_blocks if block[1] != "mermaid"]
    for index, (expected, actual) in enumerate(zip_longest(expected_blocks, actual_blocks), 1):
        if expected is None:
            return f"fenced code block {index} differs: absent in English; Chinese line {actual[0]}"
        if actual is None:
            return f"fenced code block {index} differs: English line {expected[0]}; missing in Chinese"
        if expected[2] != actual[2]:
            for line, (left, right) in enumerate(zip_longest(expected[2].splitlines(keepends=True),
                                                           actual[2].splitlines(keepends=True)), 1):
                if left != right:
                    return (f"fenced code block {index}, line {line} differs: "
                            f"English {left!r}; Chinese {right!r}")

    expected_mermaid = sum(block[1] == "mermaid" for block in english_blocks)
    actual_mermaid = sum(block[1] == "mermaid" for block in chinese_blocks)
    if expected_mermaid != actual_mermaid:
        return (f"mermaid block {min(expected_mermaid, actual_mermaid) + 1} differs: "
                f"English has {expected_mermaid}; Chinese has {actual_mermaid}")
    return None


def check_translations(root):
    errors = []
    chinese_docs = root / "docs/zh-CN"
    for chinese in sorted(chinese_docs.rglob("*")):
        if not chinese.is_file():
            continue
        english = root / "docs" / chinese.relative_to(chinese_docs)
        if not english.is_file():
            error = f"missing English document {english.relative_to(root).as_posix()}"
        else:
            error = translation_error(english.read_text(encoding="utf-8"),
                                      chinese.read_text(encoding="utf-8"))
        if error:
            errors.append(f"{chinese.relative_to(root).as_posix()}: {error}")
    return errors


def check_links(root):
    """Return the count and all diagnostics for Git-tracked Markdown sources."""
    tracked = subprocess.check_output(["git", "ls-files", "-z"], cwd=root).decode("utf-8")
    paths = [Path(name) for name in tracked.split("\0") if name and
             Path(name).suffix.lower() in (".md", ".markdown") and
             not name.startswith(("tests/", "contrib/codex/tests/"))]
    errors, checked, cache = [], 0, {}
    for path in paths:
        source = root / path
        for line, destination in links(source.read_text(encoding="utf-8")):
            url = urlsplit(destination)
            if url.scheme or url.netloc or url.path.startswith("/"):
                continue
            checked += 1
            target = source.parent / unquote(url.path) if url.path else source
            reason = None
            if not target.exists():
                reason = "missing file or folder"
            elif (url.fragment and target.is_file() and
                  target.suffix.lower() in (".md", ".markdown")):
                if target not in cache:
                    cache[target] = anchors(target.read_text(encoding="utf-8"))
                if unquote(url.fragment) not in cache[target]:
                    reason = "missing anchor"
            if reason:
                errors.append(f"{path.as_posix()}:{line}: {destination!r}: {reason}")
    return checked, errors


class DocsTest(unittest.TestCase):
    def test_alerts_help_describes_command_visibility(self):
        from hansa.cli import topic_text
        text = " ".join(topic_text("alerts").split())
        summary = text.split("The Alerts summary", 1)[1].split("The `coordinator` alert", 1)[0]
        for words in ("command's value is withheld", "this summary", "settings history",
                      "`hansa explain history`", "page's History view", "changed (values withheld)",
                      "principal can view and edit", "Settings, Alerts"):
            with self.subTest(words=words):
                self.assertIn(words, summary)

    def test_settings_changes_docs_describe_alert_values(self):
        from hansa.cli import topic_text
        texts = ((topic_text("settings-changes"),
                  ("without storing either value", "older records")),
                 ((ROOT / "docs/zh-CN/concepts.md").read_text(encoding="utf-8"),
                  ("不保存旧值或新值", "旧记录")))
        for text, words in texts:
            paragraph = next((p for p in text.split("\n\n") if "`alert.command`" in p), "")
            for word in ("`alert.command`", "`alert`", "changed (values withheld)", *words):
                with self.subTest(language=words, word=word):
                    self.assertIn(word, paragraph)

    def test_release_range_docs_qualify_single_remote_rule_and_order_example(self):
        text = " ".join((ROOT / "docs/releases.md").read_text(encoding="utf-8").split())
        with self.subTest(rule="single remote"):
            rule = text.split("commits that go out (", 1)[1].split(") and the files", 1)[0]
            self.assertIn("one remote or none", rule)
            self.assertLess(rule.index("one remote or none"), rule.index("on no remote-tracking branch"))
        with self.subTest(rule="remote order"):
            example = re.search(r"for example, `([^`]+)`", text)[1]
            self.assertRegex(example, r"0 not on remote mirror,\s*3 not on remote public")

    def test_concepts_close_includes_finished_and_failed_tasks_and_verifications(self):
        for name, words in (("concepts.md", ("finished or failed", "task or verification", "without merging")),
                            ("zh-CN/concepts.md", ("已完成或失败", "任务或核验", "不合并"))):
            with self.subTest(name=name):
                text = (ROOT / "docs" / name).read_text(encoding="utf-8")
                row = next(line for line in text.splitlines() if "`hansa close ID`" in line)
                for word in words:
                    self.assertIn(word, row)

    def test_chinese_docs_match_english(self):
        errors = check_translations(ROOT)
        self.assertFalse(errors, "Chinese documentation drift:\n" + "\n".join(errors))

    def test_translation_checker_detects_drift(self):
        english = ("# Guide\n## Install\n### Run\n"
                   "````sh\n# A code comment\n```\nhansa doctor\n````\n"
                   "~~~text\noutput /some/path\n~~~\n"
                   "```mermaid\ngraph LR\nA[Start] --> B[End]\n```\n")
        chinese = english.replace("# Guide", "# 指南").replace("## Install", "## 安装")
        chinese = chinese.replace("### Run", "### 运行").replace("[Start]", "[开始]")
        self.assertIsNone(translation_error(english, chinese))
        # Setext headings count at the same levels as ATX headings.
        self.assertIsNone(translation_error("# Guide\n## Install\n", "指南\n====\n安装\n----\n"))
        # Multiline inline-code examples cannot introduce a heading.
        self.assertIsNone(translation_error("# Guide\n", "# 指南\n`example\n## hidden\n`\n"))
        cases = [
            (chinese.replace("## 安装\n", ""), ("heading 2", "H2", "H3")),
            (chinese.replace("## 安装\n### 运行", "### 运行\n## 安装"), ("heading 2", "H2", "H3")),
            (chinese + "## 多余\n", ("heading 4", "missing", "多余")),
            (chinese.replace("hansa doctor", "hansa status"), ("fenced code block 1", "line 4", "hansa status")),
            (chinese.replace("output /some/path", "输出 /some/path"), ("fenced code block 2", "输出")),
            (chinese.replace("~~~text", "~~~sh"), ("fenced code block 2", "line 1")),
            (chinese.replace("~~~text\noutput /some/path\n~~~\n", ""), ("fenced code block 2", "missing")),
            (chinese + "```sh\nextra\n```\n", ("fenced code block 3", "absent in English")),
            (chinese.replace("```mermaid\ngraph LR\nA[开始] --> B[End]\n```\n", ""), ("mermaid block 1", "English has 1", "Chinese has 0")),
            (chinese + "~~~mermaid\ngraph LR\n~~~\n", ("mermaid block 2", "English has 1", "Chinese has 2")),
        ]
        for changed, words in cases:
            with self.subTest(words=words):
                error = translation_error(english, changed)
                self.assertIsNotNone(error)
                for word in words:
                    self.assertIn(word, error)
        # Fences with no language, an indented fence, and a fence ending at EOF.
        self.assertIsNone(translation_error("  ~~~\nvalue\n", "  ~~~\nvalue\n"))
        self.assertIn("fenced code block 1", translation_error("```\na\n```\n", "```\nb\n```\n"))
        # Neither a language change nor an unmatched delimiter may hide drift.
        self.assertIn("fenced code block 1", translation_error("```sh\na\n```\n", "```mermaid\na\n```\n"))
        self.assertIn("fenced code block 1", translation_error("~~~\na\n~~~\n", "~~~\na\n~~\n"))

    def test_translation_checker_detects_drift_in_unclosed_fence(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "docs/zh-CN").mkdir(parents=True)
            (root / "docs/guide.md").write_text(
                "# Guide\n```sh\nhansa doctor", encoding="utf-8")
            (root / "docs/zh-CN/guide.md").write_text(
                "# 指南\n```sh\nhansa status", encoding="utf-8")
            errors = check_translations(root)
            self.assertEqual(len(errors), 1, errors)
            for word in ("docs/zh-CN/guide.md", "fenced code block 1", "line 2",
                         "hansa doctor", "hansa status"):
                self.assertIn(word, errors[0])

    def test_translation_checker_names_files_and_requires_english(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "docs/zh-CN/nested").mkdir(parents=True)
            (root / "docs/nested").mkdir()
            (root / "docs/zh-CN/missing.md").write_text("# 缺失\n", encoding="utf-8")
            (root / "docs/nested/guide.md").write_text("# Guide\n## Install\n", encoding="utf-8")
            (root / "docs/zh-CN/nested/guide.md").write_text("# 指南\n", encoding="utf-8")
            errors = check_translations(root)
            self.assertEqual(len(errors), 2, errors)
            self.assertIn("docs/zh-CN/missing.md: missing English document docs/missing.md", errors[0])
            self.assertIn("docs/zh-CN/nested/guide.md: heading 2", errors[1])
            self.assertIn("missing", errors[1])

    def test_repository_links(self):
        checked, errors = check_links(ROOT)
        self.assertFalse(errors, "Broken Markdown links:\n" + "\n".join(errors))
        print(f"Checked {checked} relative Markdown links.", flush=True)

    def test_link_checker_ignores_broken_link_in_unclosed_fence(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "README.md").write_text(
                "# Guide\n```md\n[example](missing.md)", encoding="utf-8")
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            checked, errors = check_links(root)
            self.assertFalse(errors, errors)
            self.assertEqual(checked, 0)

    def test_checker_detects_broken_links(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "guide").mkdir()
            (root / "tests").mkdir()
            (root / "contrib/codex/tests").mkdir(parents=True)
            (root / "guide/page.md").write_text(
                "# Repeat!   \n## Repeat!\n## Repeat!\n## Repeat-1\n"
                "## 快速开始\n## `Code` & *words*?\n"
                '<a name="custom"></a>\n'
                "Underlined heading\n==================\n"
                "```md\n## Hidden\n```\n"
                '`<a name="fake"></a>`\n'
                '`multi-line code\n## Also hidden\n`\n', encoding="utf-8")
            (root / "guide/data (v1).txt").write_text("data", encoding="utf-8")
            (root / "README.md").write_text(
                "# Home\n"
                "[missing](guide/missing.md)\n"
                "[moved](guide/page.md#gone)\n"
                "[file](guide/page.md) [folder](guide/) [self](#home)\n"
                "[one](guide/page.md#repeat) [two](guide/page.md#repeat-1)\n"
                "[three](guide/page.md#repeat-2) [collision](guide/page.md#repeat-1-1)\n"
                "[Chinese](guide/page.md#快速开始)\n"
                "[encoded](guide/page.md#%E5%BF%AB%E9%80%9F%E5%BC%80%E5%A7%8B)\n"
                "[code heading](guide/page.md#code--words)\n"
                "[explicit](guide/page.md#custom) [setext](guide/page.md#underlined-heading)\n"
                '[title](guide/page.md "A title") [angle](<guide/data (v1).txt>)\n'
                "[parentheses](guide/data%20(v1).txt)\n"
                "[web](https://example.invalid) [http](http://example.invalid)\n"
                "[email](mailto:nobody@example.invalid)\n"
                "`[inline](missing)` ``[inline ` tick](missing)``\n"
                "````md\n[example](missing)\n```\n[still code](missing)\n````\n"
                "~~~md\n[example](missing)\n~~~\n", encoding="utf-8")
            for name in ("tests/fixture.md", "contrib/codex/tests/fixture.md"):
                (root / name).write_text("[excluded](missing)\n", encoding="utf-8")
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            (root / "untracked.md").write_text("[untracked](missing)\n", encoding="utf-8")
            checked, errors = check_links(root)
            self.assertEqual(checked, 17)
            self.assertEqual(len(errors), 2, errors)
            self.assertIn("README.md:2:", errors[0])
            self.assertIn("guide/missing.md", errors[0])
            self.assertIn("missing file", errors[0])
            self.assertIn("README.md:3:", errors[1])
            self.assertIn("guide/page.md#gone", errors[1])
            self.assertIn("missing anchor", errors[1])
            page_anchors = anchors((root / "guide/page.md").read_text(encoding="utf-8"))
            for hidden in ("hidden", "also-hidden", "fake"):
                self.assertNotIn(hidden, page_anchors)


if __name__ == "__main__":
    unittest.main()
