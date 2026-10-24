"""Task attachment snapshots: validate everything before a task is queued."""
from __future__ import annotations

import stat
from pathlib import Path

from .desk import _above

MAX_FILES = 20
MAX_BYTES = 20 * 1024 * 1024


def read_problem(desk, path, st=None, denied=None) -> str:
    """Whether a source may supply content to workers; st also checks open files' private hard links."""
    from .run import worker_read_denials

    real = str(Path(path).resolve()) if path else ""
    kept = desk.kept_folder(real, st)
    if kept:
        return f"is a file in the desk's {kept}/, which no agent reads"
    if not real:
        return "cannot determine the source file's path to check worker read permissions"
    if denied is None:
        denied = worker_read_denials(desk)
    if any(_above(d, real) for d in denied):
        return "is a file workers may not read"
    return ""


def snapshot(desk, paths=(), parent="") -> list:
    """Read the inherited copies and new sources once, bounded in size, under the desk lock.
    Return (name, bytes) pairs; no task or attachment is written on a refusal.
    """
    from .run import worker_read_denials

    sources = [(desk.attachment_dir(parent) / a["name"], a["name"], True)
               for a in desk.meta(parent).get("attachments", [])] if parent else []
    sources += [(Path(p), Path(p).name, False) for p in paths]
    if not sources:
        return []

    def refuse(path, why):
        raise SystemExit(desk.redact(f"hansa: {path} {why}; nothing was queued"))

    if len(sources) > MAX_FILES:
        refuse(sources[MAX_FILES][0], "would give the task more than 20 files")
    try:
        denied = worker_read_denials(desk)
    except RuntimeError as e:
        refuse(sources[-1][0], f"cannot be attached: {e}")
    out, names, total = [], set(), 0
    for path, name, inherited in sources:
        if name in names:
            refuse(path, f"has the same name as another attachment ({name})")
        names.add(name)
        try:
            real = path.resolve(strict=True)
            st = real.stat()
            if stat.S_ISDIR(st.st_mode):
                refuse(path, "is a folder, not a file")
            if not stat.S_ISREG(st.st_mode):
                refuse(path, "is not a regular file")
            if not inherited:
                problem = read_problem(desk, real, st, denied)
                if problem:
                    refuse(path, problem)
            with real.open("rb") as f:
                data = f.read(MAX_BYTES - total + 1)
        except FileNotFoundError:
            refuse(path, "does not exist")
        except OSError as e:
            refuse(path, f"cannot be read ({e.strerror or e})")
        total += len(data)
        if total > MAX_BYTES:
            refuse(path, "would give the task more than 20 MB in all")
        found = desk.keys_in(data.decode("utf-8", "replace"))
        if found:
            refuse(path, f"holds the key of {', '.join(found)}")
        out.append((name, data))
    return out
