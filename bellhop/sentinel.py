from __future__ import annotations

import subprocess
from datetime import datetime
from typing import List, Tuple

from . import ledger as ledger_mod
from . import seal as seal_mod
from .archive import unpushed_count
from .card import read_card
from .config import Desk
from .decisions import decisions_path
from .util import BellhopError, now, read, read_json, sha256_bytes, ts, write, write_json

Check = Tuple[str, bool, str]


def _parse_ts(s: str):
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S%z")
    except Exception:
        return None


def _last_receipt_time(desk: Desk):
    latest = None
    for p in desk.receipts.glob("*.meta.json"):
        data = read_json(p, {})
        t = _parse_ts(data.get("finished_at", ""))
        if t and (latest is None or t > latest):
            latest = t
    return latest


def check_card(desk: Desk) -> Check:
    try:
        card = read_card(desk)
    except BellhopError as exc:
        return ("card", False, str(exc))
    refreshed = _parse_ts(card.get("refreshed", ""))
    if not refreshed:
        return ("card", False, "dispatch card has no valid `refreshed` time")
    last = _last_receipt_time(desk)
    if last and refreshed < last:
        return ("card", False, f"dispatch card ({card['refreshed']}) is older than the last receipt ({ts(last)}); refresh it")
    if card.get("ready", "no").lower() == "no" and not card.get("reason"):
        return ("card", False, "card says ready: no without a reason")
    return ("card", True, f"ready={card.get('ready')} next={card.get('next') or '-'}")


def check_inflight(desk: Desk) -> Check:
    p = desk.state / "in-flight.md"
    if not p.exists():
        return ("in-flight", False, "state/in-flight.md is missing")
    bad = []
    n = 0
    for line in read(p).splitlines():
        if not line.startswith("- "):
            continue
        if line.strip() == "- none":
            continue
        n += 1
        if not (line.startswith("- [worker] ") or line.startswith("- [helper] ") or line.startswith("- [coordinator] ")):
            bad.append(line)
    if bad:
        return ("in-flight", False, f"{len(bad)} line(s) without a [worker]/[helper]/[coordinator] label")
    return ("in-flight", True, f"{n} in flight")


def check_dead_tasks(desk: Desk) -> Check:
    dead = []
    for p in desk.receipts.glob("*.meta.json"):
        data = read_json(p, {})
        if data.get("dead") and not ledger_mod.entries_mentioning(desk, data.get("id", "\0")):
            dead.append(data.get("id"))
    if dead:
        return ("dead-tasks", False, f"unacknowledged dead task(s): {', '.join(sorted(dead))}")
    return ("dead-tasks", True, "none unacknowledged")


def check_ledger(desk: Desk, snapshot: dict) -> Check:
    count, size = ledger_mod.stats(desk)
    prev = snapshot.get("ledger", {})
    if prev and (count < prev.get("count", 0) or size < prev.get("size", 0)):
        return ("ledger", False, f"ledger shrank: {prev.get('count')}→{count} entries, {prev.get('size')}→{size} bytes")
    snapshot["ledger"] = {"count": count, "size": size}
    return ("ledger", True, f"{count} entries, {size} bytes")


def check_decisions(desk: Desk, snapshot: dict) -> Check:
    p = decisions_path(desk)
    if not p.exists():
        return ("decisions", True, "no decisions file yet")
    data = p.read_bytes()
    prev = snapshot.get("decisions", {})
    if prev:
        n = prev.get("size", 0)
        if len(data) < n or sha256_bytes(data[:n]) != prev.get("prefix_sha256"):
            return ("decisions", False, "decisions.md was shortened or edited above its previous end")
    snapshot["decisions"] = {"size": len(data), "prefix_sha256": sha256_bytes(data)}
    return ("decisions", True, f"{len(data)} bytes, append-only holds")


def check_push(desk: Desk) -> Check:
    if not desk.cfg.get("push"):
        return ("push", True, "push policy off")
    n = unpushed_count(desk)
    if n is None:
        return ("push", False, "push policy on but no upstream configured")
    return ("push", n == 0, f"{n} unpushed commit(s)")


def check_seals(desk: Desk) -> Check:
    try:
        changed = seal_mod.verify(desk)
    except BellhopError as exc:
        return ("seals", True, f"no seals yet ({exc})")
    if changed:
        return ("seals", False, f"{len(changed)} sealed file(s) changed: {', '.join(changed[:3])}{'…' if len(changed) > 3 else ''}")
    return ("seals", True, "all seals hold")


def check_bells(desk: Desk) -> Check:
    pending = list(desk.bells_pending.glob("*.bell"))
    oldest = None
    for b in pending:
        data = read_json(b, {})
        t = _parse_ts(data.get("created_at", ""))
        if t and (oldest is None or t < oldest):
            oldest = t
    if oldest:
        age = (now() - oldest).total_seconds() / 60
        limit = int(desk.cfg["sentinel_interval_minutes"]) * 3
        if age > limit:
            return ("bells", False, f"{len(pending)} pending bell(s); oldest waited {age:.0f} min (is the watchdog running?)")
    return ("bells", True, f"{len(pending)} pending")


def run_checks(desk: Desk) -> List[Check]:
    snap_path = desk.state / ".sentinel" / "snapshot.json"
    snapshot = read_json(snap_path, {}) or {}
    checks = [
        check_card(desk),
        check_inflight(desk),
        check_dead_tasks(desk),
        check_bells(desk),
        check_ledger(desk, snapshot),
        check_decisions(desk, snapshot),
        check_push(desk),
        check_seals(desk),
    ]
    write_json(snap_path, snapshot)
    return checks


def status_line(checks: List[Check]) -> str:
    reds = [c for c in checks if not c[1]]
    head = "GREEN" if not reds else f"RED ({len(reds)})"
    parts = [f"{name}={'ok' if ok else 'RED'}" for name, ok, _ in checks]
    return f"{ts()} {head} | " + " ".join(parts)


def sentinel(desk: Desk, wake: bool = True) -> Tuple[bool, str]:
    checks = run_checks(desk)
    lines = [status_line(checks)]
    for name, ok, detail in checks:
        lines.append(f"  {'ok ' if ok else 'RED'} {name}: {detail}")
    text = "\n".join(lines) + "\n"
    write(desk.state / ".sentinel" / "status.txt", text)
    with (desk.state / ".sentinel" / "history.log").open("a", encoding="utf-8") as fh:
        fh.write(lines[0] + "\n")
    green = all(ok for _, ok, _ in checks)
    if not green and wake and desk.cfg.get("wake_cmd"):
        subprocess.run(desk.cfg["wake_cmd"], shell=True, input=text, text=True)
    return green, text
