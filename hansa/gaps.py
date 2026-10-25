"""Verification notes, kept and marked in the verification's task record."""
from __future__ import annotations

import re
import time

from .desk import Desk, now


def split_notes(report: str) -> list[str]:
    """Top-level Markdown list items in the last Notes section, with their continuations."""
    lines = report.splitlines()
    headings, visible = [], []
    fence = ""
    for i, line in enumerate(lines):
        marker = re.match(r"^\s*(`{3,}|~{3,})(.*)$", line)
        if fence:
            visible.append(False)
            if marker and marker[1][0] == fence[0] and len(marker[1]) >= len(fence) and not marker[2].strip():
                fence = ""
            continue
        if marker:
            fence = marker[1]
            visible.append(False)
            continue
        visible.append(True)
        atx = re.match(r"^ {0,3}(#{1,6})(?:\s+(.*?)\s*|\s*)$", line)
        if atx:
            title = re.sub(r"\s+#+\s*$", "", atx[2] or "").strip()
            headings.append((i, i + 1, len(atx[1]), title))
        elif i and visible[i - 1] and lines[i - 1].strip() and re.fullmatch(r" {0,3}(=+|-+)\s*", line):
            headings.append((i - 1, i + 1, 1 if line.lstrip().startswith("=") else 2, lines[i - 1].strip()))
    section = next((h for h in reversed(headings) if h[3].casefold() == "notes"), None)
    if section is None:
        return []
    _, start, level, _ = section
    end = next((h[0] for h in headings if h[0] >= start and h[2] <= level), len(lines))
    items = []
    for i in range(start, end):
        match = re.match(r"^( *)(?:[-+*]|\d+[.)])\s+\S", lines[i]) if visible[i] else None
        if match:
            items.append((i, len(match[1])))
    indent = min((n for _, n in items), default=4)
    if indent > 3:  # indented code alone is not a list
        return []
    roots = [i for i, n in items if n == indent]
    notes = []
    heading_lines = {h[0] for h in headings}
    for pos, first in enumerate(roots):
        last = roots[pos + 1] if pos + 1 < len(roots) else end
        for i in range(first + 1, last):
            if visible[i] and (i in heading_lines or re.match(r"^\W*VERDICT:", lines[i], re.I)
                               or (not lines[i - 1].strip() and lines[i].strip()
                                   and len(lines[i]) - len(lines[i].lstrip()) <= indent)):
                last = i
                break
        notes.append("\n".join(lines[first:last]).rstrip())
    return notes


def collect(desk: Desk, verification: dict, report: str, kept: float) -> list[dict]:
    vid, tid = verification["id"], verification.get("parent") or ""
    title = desk.meta(tid).get("title", "") if tid else ""
    texts = split_notes(report) or [f"The notes could not be split; read hansa show {vid} --report."]
    return [{"id": f"{vid}.{i}", "task": tid, "title": title, "kept": kept, "text": text}
            for i, text in enumerate(texts, 1)]


def all_notes(desk: Desk, tasks=None) -> list[dict]:
    return sorted((n for m in (desk.tasks() if tasks is None else tasks) for n in m.get("verification_notes", [])),
                  key=lambda n: (n["kept"], int(n["id"].split(".")[0][1:]), int(n["id"].split(".")[1])))


def page_list(desk: Desk) -> list[dict]:
    """The command's saved notes, with only the page's fields and current keys removed."""
    rows = []
    for n in all_notes(desk):
        row = {k: desk.redact(n[k]) for k in ("id", "task", "title", "text")}
        row["mark"] = {k: desk.redact(v) if isinstance(v, str) else v
                       for k, v in (n.get("mark") or {}).items() if k in ("kind", "why", "by", "at")}
        rows.append(row)
    return rows


def _verification(desk: Desk, nid: str) -> dict:
    match = re.fullmatch(r"(t\d+)\.[1-9]\d*", nid)
    if match and (desk.tasks_dir / match[1] / "meta.json").is_file():
        m = desk.meta(match[1])
        if any(n["id"] == nid for n in m.get("verification_notes", [])):
            return m
    raise SystemExit(f"hansa: no verification note {nid}; nothing was changed")


def show(desk: Desk, nid: str) -> str:
    m = _verification(desk, nid)
    n = next(n for n in m["verification_notes"] if n["id"] == nid)
    mark = n.get("mark") or {}
    stamp = lambda at: time.strftime("%Y-%m-%d %H:%M:%S %z", time.localtime(at))
    return (f"{nid} · task {n['task']}: {n['title']}\nkept: {stamp(n['kept'])}\n"
            + f"mark: {mark.get('kind', 'not marked')}\nwhy: {mark.get('why', '—')}\n"
            + (f"by: {mark['by']}\nmarked: {stamp(mark['at'])}\n" if mark else "")
            + f"\n{n['text']}")


def list_text(desk: Desk, include_marked=False) -> str:
    lines = []
    for n in all_notes(desk):
        mark = n.get("mark") or {}
        if mark and not include_marked:
            continue
        line = f"{n['id']}  {n['task']}  {n['text'].splitlines()[0][:160]}"
        if include_marked:
            line += f"  [{mark.get('kind', 'not marked')}]" + (f" {mark['why']}" if mark else "")
        lines.append(line)
    return "\n".join(lines) if lines else "no verification notes" + ("" if include_marked else " not marked")


def mark(desk: Desk, nid: str, kind: str, why: str, by: str, session=None) -> None:
    if kind not in ("fixed", "later", "wontfix"):
        raise SystemExit("hansa: mark must be fixed, later or wontfix; nothing was changed")
    if not why.strip():
        raise SystemExit("hansa: why is required; nothing was changed")
    with desk.lock():
        m = _verification(desk, nid)
        record = {"kind": kind, "why": desk.redact(why.strip()), "by": by, "at": now()}
        if session:
            record["session"] = session
        desk.change(m["id"], lambda current: {"verification_notes": [
            {**n, "mark": record} if n["id"] == nid else n for n in current["verification_notes"]]})
