"""`hansa notes`: the coordinator's notes under the desk's notes/, and moving a finished section out of the file
it rereads without typing the section out again."""
from __future__ import annotations

import codecs
import contextlib
import fcntl
import hashlib
import os
import re
import shlex
import stat
import time
import uuid
from pathlib import Path

from .desk import Desk, resolved

ARCHIVE = "archive"  # where a section goes by default: notes/archive/FILE; hansa notes lists no headings there
HEADING = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*?))?[ \t]*$")
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
PAGE_LIMIT = 1024 * 1024


@contextlib.contextmanager
def _page_folder(desk: Desk, archived: bool = False):
    """Anchor page reads to opened directories; never follow a notes/ or archive/ link."""
    with contextlib.ExitStack() as stack:
        fd = os.open(desk.notes_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        stack.callback(os.close, fd)
        if archived:
            fd = os.open(ARCHIVE, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            stack.callback(os.close, fd)
        yield fd


def _page_file(desk: Desk, folder: int, name: str) -> dict:
    """Check the opened inode before reading, including after a directory-entry swap."""
    entry = os.stat(name, dir_fd=folder, follow_symlinks=False)
    if not stat.S_ISREG(entry.st_mode) or entry.st_nlink != 1:
        raise ValueError("not a regular, singly linked note")
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=folder)
    with os.fdopen(fd, "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("not a regular, singly linked note")
        raw = source.read(PAGE_LIMIT)
        text, binary, heads = "", False, []
        try:
            text = codecs.getincrementaldecoder("utf-8")().decode(raw, final=info.st_size <= PAGE_LIMIT)
            # Validate the whole file and list the same headings as the CLI, including beyond the preview.
            def lines():
                source.seek(0)
                for raw_line in source:
                    line = raw_line.decode("utf-8")
                    if "\0" in line:
                        raise UnicodeError()
                    yield line
            heads = headings(lines())
        except UnicodeError:
            binary = True
        if os.fstat(source.fileno()).st_nlink != 1:
            raise ValueError("note acquired another hard link")
    # Also hide a key prefix cut by the byte limit.
    text = desk.redact(text) if not binary else ""
    if info.st_size > PAGE_LIMIT:
        for provider, key in desk.keys().items():
            for n in range(min(len(key) - 1, len(text)), 0, -1):
                if text.endswith(key[:n]):
                    text = text[:-n] + f"[key of {provider}]"
                    break
    return {"size": info.st_size, "modified": info.st_mtime, "binary": binary,
            "truncated": info.st_size > PAGE_LIMIT, "text": text,
            "headings": [(i, level, desk.redact(title)) for i, level, title in heads]}


def _page_walk(desk: Desk, archived: bool, folder: int | None = None, prefix: str = "", depth: int = 0):
    """(opened folder, file name, path under notes/) of each note the page may read, in subfolders too: folders
    are opened without following a link, and a link, a dot name or notes/archive/ (unless ARCHIVED) is passed over
    without opening it."""
    if folder is None:
        with _page_folder(desk) as top:
            yield from _page_walk(desk, archived, top)
        return
    for name in sorted(n for n in os.listdir(folder) if not n.startswith(".")):
        path = prefix + name
        try:
            entry = os.stat(name, dir_fd=folder, follow_symlinks=False)
        except OSError:
            continue
        if stat.S_ISDIR(entry.st_mode):
            if (path == ARCHIVE and not archived) or depth >= 32:
                continue
            try:
                sub = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=folder)
            except OSError:
                continue
            try:
                yield from _page_walk(desk, archived, sub, path + "/", depth + 1)
            finally:
                os.close(sub)
        elif stat.S_ISREG(entry.st_mode):
            yield folder, name, path


def page_list(desk: Desk, archived: bool = False) -> list:
    """The page's read-only view of notes, subfolders included, using the CLI's heading parser."""
    out = []
    try:
        for folder, name, path in _page_walk(desk, archived):
            try:
                item = _page_file(desk, folder, name)
            except (OSError, ValueError):
                continue
            item.pop("text")
            out.append({"name": desk.redact(path), "id": _page_id(path), **item})
    except OSError:
        pass
    return sorted(out, key=lambda item: (-item["modified"], item["name"]))


def _page_id(name: str) -> str:
    return hashlib.sha256(name.encode("utf-8", errors="surrogateescape")).hexdigest()


def page_note(desk: Desk, name: str, identity: str = "") -> dict:
    if identity:
        try:
            for folder, filename, path in _page_walk(desk, True):
                if _page_id(path) == identity:
                    return _page_file(desk, folder, filename)
        except OSError:
            pass
        raise ValueError("no such note")
    parts = name.split("/")
    if (len(parts) not in (1, 2) or len(parts) == 2 and parts[0] != ARCHIVE
            or any(p in ("", ".", "..") or "\0" in p for p in parts)):
        raise ValueError("give a file directly in notes/ or notes/archive/")
    with _page_folder(desk, len(parts) == 2) as folder:
        return _page_file(desk, folder, parts[-1])


def headings(lines: list) -> list:
    """(index, level, text) of each Markdown heading in LINES, outside fenced code."""
    found, fence = [], ""
    for i, line in enumerate(lines):
        f = FENCE.match(line)
        if fence:
            if f and f.group(1)[0] == fence[0] and len(f.group(1)) >= len(fence) and not line.strip(" \r\n`~"):
                fence = ""
            continue
        if f:
            fence = f.group(1)
            continue
        h = HEADING.match(line.rstrip("\r\n"))
        if h:
            text = re.sub(r"[ \t]+#+$", "", h.group(2) or "").strip()
            found.append((i, len(h.group(1)), "" if set(text) <= {"#"} else text))
    return found


def _root(desk: Desk) -> Path:
    if desk.notes_dir.is_symlink() or not desk.notes_dir.is_dir():
        raise SystemExit(f"hansa: {desk.notes_dir} is not a folder of the desk"
                         + (" (it is a link)" if desk.notes_dir.is_symlink() else "") + "; nothing was changed")
    return Path(os.path.realpath(desk.notes_dir))


def inside(desk: Desk, name: str) -> Path:
    """notes/NAME, resolved; refuses an absolute path, .., and a link that leads out of notes/."""
    root = _root(desk)
    shown = name[len("notes/"):] if name.startswith("notes/") else name
    if not shown or os.path.isabs(shown) or shown.startswith("~") or ".." in Path(shown).parts:
        raise SystemExit(f"hansa: {name} is refused: give a path inside notes/, such as plan.md or archive/plan.md "
                         f"(no absolute path, no ..); nothing was changed")
    real = resolved(str(root / shown))
    if not real:
        raise SystemExit(f"hansa: notes/{shown} is refused: hansa cannot follow its links to see where it leads; "
                         "nothing was changed")
    p = Path(real)
    if p != root and root not in p.parents:
        raise SystemExit(f"hansa: notes/{shown} is a link that leads out of notes/ (to {p}); nothing was changed")
    return p


def _rel(desk: Desk, p: Path) -> str:
    return "notes/" + str(p.relative_to(_root(desk)))


def _size(n: int) -> str:
    return f"{n} bytes" if n < 1024 else f"{n / 1024:.1f} KB" if n < 1024 * 1024 else f"{n / 1024 / 1024:.1f} MB"


def _lines(p: Path, strict: bool = False) -> list:
    try:
        return p.read_bytes().decode("utf-8", errors="strict" if strict else "replace").splitlines(keepends=True)
    except UnicodeDecodeError:
        raise SystemExit(f"hansa: {p.name} is not UTF-8 text; nothing was changed")


def _file_text(desk: Desk, p: Path, with_headings: bool) -> str:
    lines = _lines(p)
    out = f"{_rel(desk, p)}  {_size(p.stat().st_size)}, {len(lines)} lines"
    if not with_headings:
        return out + f" (headings not listed; hansa notes {shlex.quote(_rel(desk, p)[6:])} lists them)"
    return "\n".join([out] + [f"  {i + 1:>5} {'#' * lvl} {text}" for i, lvl, text in headings(lines)])


def list_text(desk: Desk, name: str = "") -> str:
    """Each file under notes/ (or under notes/NAME), with its size, its number of lines and its headings."""
    root = _root(desk)
    top = inside(desk, name) if name else root
    if top.is_file():
        return _file_text(desk, top, True)
    if not top.is_dir():
        raise SystemExit(f"hansa: there is no {_rel(desk, top)}")
    out = []
    for d, dirs, files in os.walk(top):
        dirs[:] = sorted(x for x in dirs if not x.startswith("."))
        for f in sorted(x for x in files if not x.startswith(".")):
            p = Path(d) / f
            real = resolved(str(p))
            if not real:  # what it is cannot be checked: not read
                out.append(f"{_rel(desk, p)}  (hansa cannot follow its links to see where it leads; not read)")
            elif root not in Path(real).parents:
                out.append(f"{_rel(desk, p)}  (a link that leads out of notes/; not read)")
            elif Path(real).is_file():
                archived = not name and p.relative_to(root).parts[0] == ARCHIVE
                out.append(_file_text(desk, p, not archived))
    return "\n".join(out) if out else f"{_rel(desk, top) if name else 'notes/'} holds no files"


def _match(heads: list, want: str) -> tuple:
    """(the headings whose text is WANT whole, those whose text only starts with it); WANT starting with #s
    ("## Round 1") names only headings of that level. Only a whole match is ever moved: run twice, a start would
    take the next heading that starts so ("Round 10" after "Round 1")."""
    want = want.strip()
    level = len(want) - len(want.lstrip("#"))
    want = want.lstrip("#").strip()
    if level:
        heads = [h for h in heads if h[1] == level]
    whole = [h for h in heads if h[2] == want]
    return whole, [h for h in heads if want and h[2] != want and h[2].startswith(want)]


@contextlib.contextmanager
def _notes_lock(desk: Desk):
    """One hansa notes archive at a time on a desk: a second one waits until the first has written both files, so
    no section is read by both and none is lost or doubled."""
    desk.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    with open(desk.state_dir / "notes.lock", "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _write(p: Path, data: bytes, mode: int | None) -> None:
    """Replace P whole: a crash leaves the old file or the new one, never half of it."""
    tmp = p.with_name(f".{p.name}.{uuid.uuid4().hex[:8]}.hansa-tmp")
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, p)
    finally:
        tmp.unlink(missing_ok=True)


def archive(desk: Desk, name: str, heading: str, to: str = "") -> str:
    """Move the section of notes/NAME that starts at the heading matching HEADING, up to the next heading of the
    same or a higher level, onto the end of notes/TO (default notes/archive/NAME). Refuses, changing nothing, when
    HEADING matches no heading whole, or several."""
    with _notes_lock(desk):
        return _archive(desk, name, heading, to)


def _archive(desk: Desk, name: str, heading: str, to: str) -> str:
    src = inside(desk, name)
    if not src.is_file():
        raise SystemExit(f"hansa: there is no file {_rel(desk, src)}" + ("" if src.exists() else
                         f"; hansa notes lists them") + ". Nothing was changed")
    dst = inside(desk, to) if to else inside(desk, str(Path(ARCHIVE) / src.relative_to(_root(desk))))
    if dst == src:
        raise SystemExit(f"hansa: the section would go to the file it is in ({_rel(desk, src)}); nothing was changed")
    if dst.exists() and not dst.is_file():
        raise SystemExit(f"hansa: {_rel(desk, dst)} is not a file; nothing was changed")
    root = _root(desk)
    blocked = next((p for p in dst.parents if (p == root or root in p.parents) and p.exists() and not p.is_dir()), None)
    if blocked:
        raise SystemExit(f"hansa: {_rel(desk, blocked)} is a file, so {_rel(desk, dst)} cannot be made under it; "
                         f"nothing was changed")
    lines = _lines(src, strict=True)
    heads = headings(lines)
    found, starts = _match(heads, heading)
    listed = lambda hs: "\n".join(f"  {i + 1:>5} {'#' * lvl} {t}" for i, lvl, t in hs)  # noqa: E731
    if not found and starts:
        raise SystemExit(f"hansa: no heading in {_rel(desk, src)} matches {heading!r} whole (its whole text; with #s "
                         f"in front, only a heading of that level); nothing was changed. Give the whole text of the one "
                         f"to move; the headings that start so are:\n{listed(starts)}")
    if not found:
        raise SystemExit(f"hansa: no heading in {_rel(desk, src)} matches {heading!r} (its whole text; with #s in "
                         f"front, only a heading of that level); nothing was changed. Its headings:\n"
                         f"{listed(heads) or '  (none)'}")
    if len(found) > 1:
        raise SystemExit(f"hansa: {len(found)} headings in {_rel(desk, src)} match {heading!r} whole; give #s in front "
                         f"for the level of the one to move. Nothing was changed. They are:\n{listed(found)}")
    start, level, text = found[0]
    end = next((i for i, lvl, _ in heads if i > start and lvl <= level), len(lines))
    section = lines[start:end]
    nl = "\r\n" if lines[start].endswith("\r\n") else "\n"
    if not section[-1].endswith("\n"):
        section[-1] += nl
    stamp = f"_(moved here from {_rel(desk, src)} on {time.strftime('%Y-%m-%d %H:%M')} by hansa notes archive)_{nl}"
    moved = [section[0], stamp] + section[1:]
    made = not dst.exists()
    old = b"" if made else dst.read_bytes()
    gap = b"" if not old else (b"" if old.endswith(b"\n") else nl.encode()) + (b"" if old.endswith(b"\n\n") else
                                                                                   nl.encode())
    dst.parent.mkdir(parents=True, exist_ok=True)
    # the other file first: a crash between the two leaves the section in both files, never in neither
    _write(dst, old + gap + "".join(moved).encode("utf-8"), None if made else dst.stat().st_mode & 0o7777)
    rest = lines[:start] + lines[end:]
    _write(src, "".join(rest).encode("utf-8"), src.stat().st_mode & 0o7777)
    desk.event("notes_archived", file=_rel(desk, src), to=_rel(desk, dst), heading=text, lines=len(section))
    return (f"moved {'#' * level} {text} (lines {start + 1}-{end}, {len(section)} lines) from {_rel(desk, src)} to "
            f"the end of {_rel(desk, dst)}" + (", made now" if made else "")
            + f"; {_rel(desk, src)} has {len(rest)} lines now")
