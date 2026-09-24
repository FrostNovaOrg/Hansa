"""Tokens and what they are worth, from Claude Code's own records and the principal's prices.

Two sources, never counted twice:
- worker runs: the usage Claude Code returned for each task (tasks/<id>/meta.json);
- the coordinator and its helpers: the transcripts Claude Code writes for sessions
  started in the desk (<config dir>/projects/<desk path>/**/*.jsonl).

Prices come from the principal's table. A Claude model with no entry, run on a
subscription, falls back to Claude Code's own list-price figure; any other model
with no price is listed, not guessed (Claude Code's figure for a model it does not
know is a placeholder). A subscription is paid by the month, so the dollar figure
is what the tokens are worth, for comparing and spotting waste, not a bill.
"""
from __future__ import annotations

import calendar
import json
import os
import re
import time
from collections import defaultdict
from pathlib import Path

from .desk import Desk

TOKEN_FIELDS = ("in", "out", "cache_read", "cache_write")


def price_for(model: str, prices: dict):
    pr = prices.get(model) or prices.get(re.sub(r"-\d{8}$", "", model))
    if not pr:
        return None
    i = pr["in"]
    cw = pr.get("cache_write", i * 1.25)
    return {"in": i, "out": pr["out"], "cache_read": pr.get("cache_read", i * 0.1),
            "cache_write": cw, "cache_write_1h": pr.get("cache_write_1h", cw if "cache_write" in pr else i * 2)}


def value(model: str, u: dict, prices: dict):
    """List-price dollars for one usage record, or None when the model has no price."""
    pr = price_for(model, prices)
    if pr is None:
        return None
    w5, w1h = u.get("cache_write_5m"), u.get("cache_write_1h")
    if w5 is None and w1h is None:  # not split: Claude Code writes one-hour cache entries
        w5, w1h = 0, u.get("cache_write", 0)
    return (u.get("in", 0) * pr["in"] + u.get("out", 0) * pr["out"] + u.get("cache_read", 0) * pr["cache_read"]
            + (w5 or 0) * pr["cache_write"] + (w1h or 0) * pr["cache_write_1h"]) / 1e6


def _day(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def worker_rows(desk: Desk, since: float) -> list:
    s = desk.settings()
    rows = []
    for m in desk.tasks():
        when = m.get("finished") or 0
        if when < since or not m.get("usage"):
            continue
        kind = s["providers"].get(m.get("provider"), {}).get("kind", "")
        for model, u in m["usage"].items():
            usd = value(model, u, s["prices"])
            source = "price table"
            if usd is None and kind == "subscription" and u.get("claude_code_usd") is not None:
                usd, source = u["claude_code_usd"], "Claude Code list price"
            rows.append({"day": _day(when), "seat": "worker", "lane": m.get("lane", ""), "task": m["id"],
                         "provider": m.get("provider", ""), "model": model,
                         **{k: u.get(k, 0) for k in TOKEN_FIELDS}, "usd": usd,
                         "priced_by": source if usd is not None else "no price"})
    return rows


def slug(path: Path) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


def coordinator_dirs(desk: Desk) -> list:
    s = desk.settings()
    p = s["providers"].get(s["coordinator"]["provider"], {})
    if p.get("kind") == "compatible":
        base = desk.state_dir / f"claude-{s['coordinator']['provider']}"
    else:
        base = Path(p.get("config_dir") or os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    return [base / "projects" / slug(desk.root)]


def transcript_rows(desk: Desk, since: float) -> list:
    s = desk.settings()
    per = defaultdict(lambda: defaultdict(int))
    seen = set()
    for proj in coordinator_dirs(desk):
        if not proj.is_dir():
            continue
        for f in proj.rglob("*.jsonl"):
            if f.stat().st_mtime < since:
                continue
            seat = helper_seat(f) if "subagents" in f.parts else "coordinator"
            with open(f, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if '"usage"' not in line:
                        continue
                    try:
                        d = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    msg = d.get("message") or {}
                    u, mid = msg.get("usage"), msg.get("id")
                    if not u or not mid or mid in seen or str(msg.get("model", "")).startswith("<"):
                        continue
                    seen.add(mid)
                    ts = _ts(d.get("timestamp"))
                    if ts < since:
                        continue
                    cc = u.get("cache_creation") or {}
                    row = per[(_day(ts), seat, msg.get("model", "?"))]
                    row["in"] += u.get("input_tokens", 0)
                    row["out"] += u.get("output_tokens", 0)
                    row["cache_read"] += u.get("cache_read_input_tokens", 0)
                    row["cache_write"] += u.get("cache_creation_input_tokens", 0)
                    row["cache_write_5m"] += cc.get("ephemeral_5m_input_tokens", 0)
                    row["cache_write_1h"] += cc.get("ephemeral_1h_input_tokens", 0)
    rows = []
    for (day, seat, model), u in sorted(per.items()):
        if not cc_split_known(u):
            u.pop("cache_write_5m"), u.pop("cache_write_1h")
        usd = value(model, u, s["prices"])
        rows.append({"day": day, "seat": seat, "lane": "", "task": "", "provider": s["coordinator"]["provider"],
                     "model": model, **{k: u[k] for k in TOKEN_FIELDS}, "usd": usd,
                     "priced_by": "price table" if usd is not None else "no price"})
    return rows


def helper_seat(f: Path) -> str:
    """'helper:scout' and the like, from the note Claude Code keeps beside a helper's transcript."""
    try:
        kind = json.loads(f.with_suffix(".meta.json").read_text(encoding="utf-8")).get("agentType")
    except (OSError, ValueError, AttributeError):
        kind = None
    return f"helper:{kind}" if kind else "helper"


def cc_split_known(u) -> bool:
    return (u.get("cache_write_5m", 0) + u.get("cache_write_1h", 0)) == u.get("cache_write", 0)


def _ts(text) -> float:
    """Transcript timestamps are UTC ISO strings."""
    if not text:
        return 0.0
    try:
        return float(calendar.timegm(time.strptime(text[:19], "%Y-%m-%dT%H:%M:%S")))
    except ValueError:
        return 0.0


def report(desk: Desk, days: int = 7) -> dict:
    since = time.time() - days * 86400
    rows = worker_rows(desk, since) + transcript_rows(desk, since)
    by = {}
    for key in ("day", "seat", "lane", "task", "model", "provider"):
        agg = defaultdict(lambda: {"tokens": 0, "usd": 0.0, "unpriced_tokens": 0})
        for r in rows:
            if key in ("lane", "task") and not r[key]:
                continue
            a = agg[r[key]]
            t = sum(r[k] for k in TOKEN_FIELDS)
            a["tokens"] += t
            if r["usd"] is None:
                a["unpriced_tokens"] += t
            else:
                a["usd"] += r["usd"]
        by[key] = {k: {**v, "usd": round(v["usd"], 6)} for k, v in sorted(agg.items())}
    total_usd = sum(r["usd"] for r in rows if r["usd"] is not None)
    return {"days": days, "rows": rows, "by": by,
            "total": {"tokens": sum(sum(r[k] for k in TOKEN_FIELDS) for r in rows), "usd": round(total_usd, 6)},
            "unpriced_models": sorted({r["model"] for r in rows if r["usd"] is None}),
            "note": "Dollars use the principal's price table (a Claude model without an entry uses Claude Code's list price). Subscriptions are paid by the month: this is what the tokens are worth, not a bill."}
