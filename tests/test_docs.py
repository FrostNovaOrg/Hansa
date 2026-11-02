"""Check local inline Markdown links without fetching anything from the network."""
import html
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


def without_fences(text):
    lines = []
    fence = None
    for line in text.splitlines(keepends=True):
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if fence is not None:
            lines.append(blank(line))
            if (marker and marker[1][0] == fence[0] and
                    len(marker[1]) >= len(fence) and not marker[2].strip()):
                fence = None
        elif marker:
            fence = marker[1]
            lines.append(blank(line))
        else:
            lines.append(line)
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


def anchors(text):
    text = without_fences(text)
    visible = INLINE_CODE.sub(lambda match: blank(match[0]), text)
    found = set()
    for match in re.finditer(r'''<a\s+[^>]*\bname\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))[^>]*>''',
                             visible, re.IGNORECASE):
        found.add(html.unescape(next(value for value in match.groups() if value is not None)))
    used = set()
    lines = text.splitlines()
    visible_lines = visible.splitlines()
    for index, line in enumerate(lines):
        heading = (re.match(r"^ {0,3}#{1,6}(?:[ \t]+(.*?)|[ \t]*)$", line)
                   if re.match(r"^ {0,3}#{1,6}(?:[ \t]|$)", visible_lines[index]) else None)
        if heading:
            title = re.sub(r"[ \t]+#+[ \t]*$", "", heading[1] or "")
        elif (index + 1 < len(lines) and visible_lines[index].strip() and
              re.fullmatch(r" {0,3}(?:=+|-+)[ \t]*", visible_lines[index + 1])):
            title = line.strip()
        else:
            continue
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
    def test_repository_links(self):
        checked, errors = check_links(ROOT)
        self.assertFalse(errors, "Broken Markdown links:\n" + "\n".join(errors))
        print(f"Checked {checked} relative Markdown links.", flush=True)

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
