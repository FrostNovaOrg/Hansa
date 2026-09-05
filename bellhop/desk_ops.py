from __future__ import annotations

import shutil
from pathlib import Path

from . import ledger as ledger_mod
from . import seal as seal_mod
from . import sentinel as sentinel_mod
from .card import read_card
from .config import CONFIG_NAME, FRAMEWORK_ROOT, Desk
from .util import BellhopError, git, read, read_json, run, today, ts


def init_desk(target: Path, force: bool = False) -> Desk:
    target = Path(target).resolve()
    src = FRAMEWORK_ROOT / "template"
    if target.exists() and any(target.iterdir()) and not force:
        raise BellhopError(f"{target} is not empty (use --force to copy into it anyway)")
    target.mkdir(parents=True, exist_ok=True)
    for item in src.rglob("*"):
        rel = item.relative_to(src)
        dst = target / rel
        if item.is_dir():
            dst.mkdir(parents=True, exist_ok=True)
        elif not dst.exists() or force:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, dst)
    for keep in ("inbox", "bells/pending", "bells/rung", "bells/logs", "receipts", "ledger", "reports/weekly", "seals", "frozen", "rules/lanes", "state/.sentinel"):
        d = target / keep
        d.mkdir(parents=True, exist_ok=True)
        gk = d / ".gitkeep"
        if not any(d.iterdir()):
            gk.write_text("", encoding="utf-8")
    example = target / "bellhop.example.json"
    cfg = target / CONFIG_NAME
    if example.exists() and not cfg.exists():
        example.rename(cfg)
    if not (target / ".git").exists():
        git(target, "init", "-q", "-b", "main")
    desk = Desk(target, read_json(cfg, {}))
    git(target, "add", "-A")
    if git(target, "diff", "--cached", "--name-only"):
        git(target, "commit", "-q", "-m", f"{desk.cfg['commit_prefix']} desk initialised from Bellhop template")
    return desk


def close(desk: Desk) -> str:
    try:
        changed = seal_mod.verify(desk)
    except BellhopError:
        changed = []
        seal_note = "no seals manifest yet (run `bellhop seal`)"
    else:
        seal_note = "seals hold" if not changed else f"{len(changed)} sealed file(s) changed"
    green, text = sentinel_mod.sentinel(desk, wake=False)
    count, size = ledger_mod.stats(desk)
    receipts_today = [p for p in desk.receipts.glob("*.meta.json") if (read_json(p, {}).get("finished_at", "").startswith(today()))]
    dead_today = [p for p in receipts_today if read_json(p, {}).get("dead")]
    summary = (
        f"Receipts today: {len(receipts_today)} ({len(dead_today)} dead). Ledger: {count} entries, {size} bytes. "
        f"Sentinel: {'green' if green else 'RED'}. Seals: {seal_note}."
    )
    ledger_mod.add(desk, f"day close {today()}", summary + "\n\n" + text.strip())
    return summary


def coldstart(desk: Desk) -> str:
    lines = ["# Cold-start reading list", ""]
    order = [
        ("state/next.md", "what happens next and why"),
        ("state/in-flight.md", "who is doing what"),
        ("state/debts.md", "what is owed"),
        ("state/human-todo.md", "what only the principal can do"),
        ("state/decisions.md", "what the principal decided, verbatim"),
        ("rules/core.md", "the rules every session follows"),
        ("state/.sentinel/status.txt", "last sentinel status"),
    ]
    problems = []
    for rel, why in order:
        p = desk.path(rel)
        if not p.exists():
            problems.append(f"missing: {rel}")
            lines.append(f"- {rel} — {why} — MISSING")
            continue
        size = p.stat().st_size
        lines.append(f"- {rel} — {why} — {size} bytes")
    try:
        card = read_card(desk)
        lines.append("")
        lines.append(f"Card: ready={card.get('ready')} next={card.get('next') or '-'} reason={card.get('reason') or '-'} refreshed={card.get('refreshed')}")
    except BellhopError as exc:
        problems.append(str(exc))
    recent = sorted(desk.ledger.rglob("*.md"))[-5:] if desk.ledger.exists() else []
    lines.append("")
    lines.append("Last ledger entries:")
    for p in recent:
        lines.append(f"- {p.relative_to(desk.root)}: {read(p).splitlines()[0].lstrip('# ')}")
    if problems:
        lines.append("")
        lines.append("Problems: " + "; ".join(problems))
    return "\n".join(lines) + "\n"
