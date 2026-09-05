from __future__ import annotations

import collections
import json
import statistics
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from . import ledger as ledger_mod
from .config import Desk
from .util import git, iso_week, read_json, run, write


def _week_bounds(week: str):
    year, w = week.split("-W")
    monday = datetime.strptime(f"{year}-W{int(w):02d}-1", "%G-W%V-%u")
    return monday, monday + timedelta(days=7)


def collect(desk: Desk, week: str):
    start, end = _week_bounds(week)
    rows = []
    for p in desk.receipts.glob("*.meta.json"):
        d = read_json(p, {})
        fin = d.get("finished_at", "")
        try:
            t = datetime.strptime(fin, "%Y-%m-%dT%H:%M:%S%z").replace(tzinfo=None)
        except Exception:
            continue
        if start <= t < end:
            rows.append(d)
    return rows


def _commits_per_day(desk: Desk, week: str) -> dict:
    start, end = _week_bounds(week)
    out = collections.Counter()
    log = run(["git", "-C", str(desk.root), "log", f"--since={start:%Y-%m-%d}", f"--until={end:%Y-%m-%d}", "--format=%ad", "--date=short"], check=False)
    for line in (log.stdout or "").splitlines():
        out[line.strip()] += 1
    return out


def transcripts_summary(paths: list) -> dict:
    """Sum output tokens per project directory, deduplicating by message id."""
    out = {}
    for root in paths:
        root = Path(root).expanduser()
        if not root.exists():
            continue
        per_msg = {}
        for f in root.rglob("*.jsonl"):
            try:
                with f.open(encoding="utf-8", errors="ignore") as fh:
                    for line in fh:
                        try:
                            j = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if j.get("type") != "assistant":
                            continue
                        m = j.get("message") or {}
                        mid = m.get("id") or f"{f.name}:{len(per_msg)}"
                        tok = (m.get("usage") or {}).get("output_tokens", 0) or 0
                        per_msg[mid] = max(per_msg.get(mid, 0), tok)
            except OSError:
                continue
        out[str(root)] = {"output_tokens": sum(per_msg.values()), "messages": len(per_msg)}
    return out


def report(desk: Desk, week: Optional[str] = None, transcripts: Optional[list] = None) -> Path:
    week = week or iso_week()
    rows = collect(desk, week)
    by_lane = collections.Counter(r.get("lane") for r in rows)
    by_model = collections.Counter(f"{r.get('provider')}/{r.get('model')}" for r in rows)
    dead = [r for r in rows if r.get("dead")]
    costs = [float(r["cost_usd"]) for r in rows if r.get("cost_usd") is not None]
    durs = [float(r["duration_s"]) for r in rows if r.get("duration_s") is not None]
    titles = collections.Counter()
    for r in rows:
        titles[r.get("id")] += 1
    commits = _commits_per_day(desk, week)
    count, size = ledger_mod.stats(desk)
    lines = [f"# Weekly readings {week}", "", "## Outcome numbers (fill by hand)", "",
             "| Number | Value |", "|---|---|",
             "| Report-to-release (hours, median) | _fill_ |",
             "| Post-release defects found by the principal | _fill_ |",
             f"| Rework rate | {'n/a' if not rows else f'{sum(1 for c in titles.values() if c > 1)}/{len(titles)}'} |",
             "", "## Process numbers (diagnostics)", "",
             "| Number | Value |", "|---|---|",
             f"| Tasks | {len(rows)} |",
             f"| By lane | {', '.join(f'{k}: {v}' for k, v in sorted(by_lane.items())) or '-'} |",
             f"| By provider/model | {', '.join(f'{k}: {v}' for k, v in sorted(by_model.items())) or '-'} |",
             f"| Dead tasks | {len(dead)} |",
             f"| Median duration (s) | {statistics.median(durs):.0f} |" if durs else "| Median duration (s) | - |",
             f"| Cost known for | {len(costs)}/{len(rows)} tasks, total ${sum(costs):.2f} |",
             f"| Desk commits per day | {', '.join(f'{d}: {n}' for d, n in sorted(commits.items())) or '-'} |",
             f"| Commits per task | {(sum(commits.values()) / len(rows)):.1f} |" if rows else "| Commits per task | - |",
             f"| Ledger | {count} entries, {size} bytes |"]
    if transcripts:
        lines += ["", "## Output tokens (message ids deduplicated)", "", "| Directory | Output tokens | Messages |", "|---|---|---|"]
        for k, v in transcripts_summary(transcripts).items():
            lines.append(f"| {k} | {v['output_tokens']} | {v['messages']} |")
    out = desk.path("reports", "weekly", f"{week}.md")
    write(out, "\n".join(lines) + "\n")
    return out
