"""The project files a run's Claude Code or Codex loads and obeys, made exactly those of the commit the run starts from.

A file or folder under .claude, .codex or .agents (at any depth, as the Codex adapter's NESTED_CONFIG has them), or a
.mcp.json, that the start commit does not hold is removed; one it holds with other content is put back to the commit's;
a link, a named pipe or anything else not a plain file or folder at such a path is removed, never followed. hansa does
this itself: lstat, no link followed, no git (git keeps the files it ignores, and an account's global ignore may list
.claude/settings.local.json).

The worktree's walk here (walk) is the one hansa looks for repositories inside a worktree with (run.repos_inside).
This file imports nothing of hansa's: after a lane's setup it runs on its own, outside the sandbox, as
`python -I project_files.py MANIFEST REPORT WORKTREE -- worker command...`, and execs the worker when it is done.
"""
import base64
import json
import os
import shutil
import stat
import sys
import unicodedata

# The names a run's Claude Code or Codex reads project settings, hooks, skills, rules and servers from, matched as
# git matches .git: in any case, and without the characters HFS+ ignores in a name
NAMES = (".claude", ".codex", ".agents", ".mcp.json")
_HFS_IGNORED = dict.fromkeys(map(ord, "‌‍‎‏‪‫‬‭‮"
                                      "⁪⁫⁬⁭⁮⁯﻿"))
FAILED_EXIT = 95  # the wrapper's exit when it could not: the worker never started


def nfc(p: str) -> str:
    return p if p.isascii() else unicodedata.normalize("NFC", p)


def folded(name: str) -> str:
    return name.translate(_HFS_IGNORED).lower()


def is_project_path(path: str) -> bool:
    """Whether a path (relative, with /) is at or below one of NAMES."""
    return any(folded(c) in NAMES for c in path.split("/"))


def walk(top, places=()) -> tuple:
    """(repositories, project): the folders below `top` that hold a .git entry (a folder, a file or a link), and the
    topmost entries named one of NAMES, by hansa's own walk (lstat, no link followed, no git), never into a .git entry
    nor into `places` (relative paths, the submodules'). Relative, with /, sorted."""
    places = {nfc(p) for p in places}
    repos, project, todo = [], [], [("", False)]
    while todo:
        rel, inside = todo.pop()
        try:
            with os.scandir(os.path.join(top, rel)) as it:
                entries = list(it)
        except OSError:  # one git cannot read either
            continue
        for d in entries:
            p = f"{rel}/{d.name}" if rel else d.name
            name = folded(d.name)
            if name == ".git":
                if rel and nfc(rel) not in places:
                    repos.append(rel)
                continue  # a git folder (the top's own too) is not walked into
            mine = name in NAMES
            if mine and not inside:
                project.append(p)
            try:
                if d.is_dir(follow_symlinks=False) and nfc(p) not in places:
                    todo.append((p, inside or mine))
            except OSError:
                continue
    return sorted(set(repos)), sorted(project)


def _remove(path: str) -> None:
    if stat.S_ISDIR(os.lstat(path).st_mode):
        shutil.rmtree(path)  # never follows a link inside it
    else:
        os.unlink(path)


def _write(path: str, mode: str, data: bytes) -> None:
    if mode == "120000":
        os.symlink(os.fsdecode(data), path)
        return
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                 0o755 if mode == "100755" else 0o644)
    with os.fdopen(fd, "wb") as f:
        f.write(data)


def _same(path: str, st, mode: str, data: bytes) -> bool:
    """Whether the entry at path (lstat st) is the commit's (mode, data)."""
    if mode == "120000":
        return stat.S_ISLNK(st.st_mode) and os.fsencode(os.readlink(path)) == data
    if not stat.S_ISREG(st.st_mode) or bool(st.st_mode & 0o100) != (mode == "100755") or st.st_size != len(data):
        return False
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(fd, "rb") as f:
        return stat.S_ISREG(os.fstat(f.fileno()).st_mode) and f.read(len(data) + 1) == data


def settle(top, held: dict, places=()) -> dict:
    """Make what is at the project paths below `top` exactly `held` ({path: (git mode, bytes: content, or a link's
    target)}, the start commit's entries at those paths). {"removed": [...], "restored": [...]}, relative paths: the
    topmost entries removed (a folder counts once), and those put back to the commit's. Raises OSError (its
    `filename` the relative path) when one cannot be removed or put back."""
    held = {nfc(p): v for p, v in held.items()}
    folders = {"/".join(p.split("/")[:i]) for p in held for i in range(1, p.count("/") + 1)}
    removed, restored = [], []

    def put(rel, path):
        try:
            _write(path, *held[nfc(rel)])
        except OSError as e:
            e.filename = rel
            raise
        restored.append(rel)

    def fix(rel):
        path = os.path.join(top, rel)
        try:
            st = os.lstat(path)
            key = nfc(rel)
            if stat.S_ISDIR(st.st_mode) and key in folders:
                with os.scandir(path) as it:
                    names = sorted(e.name for e in it)
                for n in names:
                    fix(f"{rel}/{n}")
                return
            if key in held and _same(path, st, *held[key]):
                return
            _remove(path)
        except FileNotFoundError:
            return
        except OSError as e:
            e.filename = rel
            raise
        if key in held:
            put(rel, path)
        else:
            removed.append(rel)

    _, project = walk(top, places)
    for rel in project:
        fix(rel)
    for rel in sorted(held):  # what the commit holds and is not there (a setup removed it, say): put back
        path = os.path.join(top, rel)
        if os.path.lexists(path):
            continue
        parts, ok = rel.split("/"), True
        for i in range(1, len(parts)):
            up = os.path.join(top, *parts[:i])
            try:
                if not stat.S_ISDIR(os.lstat(up).st_mode):
                    ok = False  # a link or a file above it: not followed, nor put right here
                    break
            except FileNotFoundError:
                if not is_project_path("/".join(parts[:i + 1])):
                    ok = False  # above the project folder: not hansa's to make
                    break
                os.mkdir(up)
        if ok:
            put(rel, path)
    return {"removed": removed, "restored": restored}


def failure(e: OSError) -> str:
    """One sentence (after "could not start: ") for a removal or restore that failed."""
    return (f"hansa could not remove {e.filename} from the worktree, project settings its start commit does not hold "
            f"as they are ({e.strerror or e})")


def manifest(held: dict, places=()) -> dict:
    """`held` and `places` (settle's) as JSON, kept for the wrapper in the run's folder, which no run may write."""
    return {"held": {p: [m, base64.b64encode(d).decode("ascii")] for p, (m, d) in held.items()},
            "places": sorted(places)}


def load_manifest(path: str) -> tuple:
    with open(path, encoding="utf-8") as f:
        got = json.load(f)
    return {p: (m, base64.b64decode(d)) for p, (m, d) in got["held"].items()}, got["places"]


def main(argv) -> None:
    """After a lane's setup: MANIFEST REPORT WORKTREE -- command...: settle the worktree, write what it did to REPORT
    (JSON: removed, restored, error), and exec the command; on a failure write the error there and exit FAILED_EXIT."""
    manifest, report, top, dash, *command = argv
    assert dash == "--" and command
    try:
        done = {**settle(top, *load_manifest(manifest)), "error": ""}
    except OSError as e:
        done = {"removed": [], "restored": [], "error": failure(e)}
    part = f"{report}.{os.getpid()}"
    with open(os.open(part, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as f:
        json.dump(done, f)
    os.replace(part, report)
    if done["error"]:
        sys.stderr.write(f"hansa: could not start: {done['error']}\n")
        sys.exit(FAILED_EXIT)
    os.execvp(command[0], command)


if __name__ == "__main__":
    main(sys.argv[1:])
