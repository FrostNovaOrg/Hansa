"""Tokens and what they are worth, from Claude Code's own records and the principal's prices.

Two sources, never counted twice:
- worker runs: the usage Claude Code returned for each run of each task (tasks/<id>/meta.json): its last run, and
  each earlier try, kept on its attempt record when the task went back to the queue (or in earlier_runs, for a run
  an earlier hansa serve started and lost track of); each run counted once;
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

from . import account
from .desk import Desk, read_json

TOKEN_FIELDS = ("in", "out", "cache_read", "cache_write")


def price_for(model: str, prices: dict):
    pr = prices.get(model) or prices.get(re.sub(r"-\d{8}$", "", model))
    if not pr:
        return None
    i = pr["in"]
    cw = pr.get("cache_write", i * 1.25)
    return {"in": i, "out": pr["out"], "cache_read": pr.get("cache_read", i * 0.1),
            "cache_write": cw, "cache_write_1h": pr.get("cache_write_1h", cw if "cache_write" in pr else i * 2)}


def parts(model: str, u: dict, prices: dict):
    """List-price dollars for one usage record by kind of token (in, out, cache_read, cache_write),
    or None when the model has no price."""
    pr = price_for(model, prices)
    if pr is None:
        return None
    w5, w1h = u.get("cache_write_5m"), u.get("cache_write_1h")
    if w5 is None and w1h is None:  # not split: Claude Code writes one-hour cache entries
        w5, w1h = 0, u.get("cache_write", 0)
    return {"in": u.get("in", 0) * pr["in"] / 1e6, "out": u.get("out", 0) * pr["out"] / 1e6,
            "cache_read": u.get("cache_read", 0) * pr["cache_read"] / 1e6,
            "cache_write": ((w5 or 0) * pr["cache_write"] + (w1h or 0) * pr["cache_write_1h"]) / 1e6}


def value(model: str, u: dict, prices: dict):
    """List-price dollars for one usage record, or None when the model has no price."""
    p = parts(model, u, prices)
    return None if p is None else sum(p.values())


def _day(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def task_runs(m: dict) -> list:
    """Every run of a task that has numbers, oldest first: {when, provider, usage, turns}. Earlier tries are on the
    attempt records (and in earlier_runs); the task's own usage is its last run's. Each is there once: a run's
    numbers move to its attempt record as the task is queued again, and the task's own are cleared then
    (run.finish); a lost run is kept only when its session is in neither (run.keep_lost_run)."""
    out = []
    for x in [*(m.get("attempts") or []), *(m.get("earlier_runs") or []),
              {"usage": m.get("usage"), "turns": m.get("turns"), "ended": m.get("finished"),
               "provider": m.get("provider", "")}]:
        if not isinstance(x, dict) or not isinstance(x.get("usage"), dict) or not x["usage"]:
            continue
        out.append({"when": x.get("ended") or x.get("at") or 0, "provider": x.get("provider") or m.get("provider", ""),
                    "usage": x["usage"], "turns": x.get("turns") or 0})
    return sorted(out, key=lambda r: r["when"])


def worker_rows(desk: Desk, since: float) -> list:
    """One row per model of each run of each task that ended in the window. A run's turns, and the run itself
    (runs), are on its first row, so that they add up."""
    s = desk.settings()
    rows = []
    for m in desk.tasks():
        seat = {"design": "design", "verify": "verify"}.get(m.get("kind"), "worker")
        for run in task_runs(m):
            when = run["when"]
            if when < since:
                continue
            kind = s["providers"].get(run["provider"], {}).get("kind", "")
            for i, (model, u) in enumerate(run["usage"].items()):
                usd = value(model, u, s["prices"])
                source = "price table"
                if usd is None and kind == "subscription" and u.get("claude_code_usd") is not None:
                    usd, source = u["claude_code_usd"], "Claude Code list price"
                rows.append({"day": _day(when), "seat": seat, "lane": m.get("lane", ""), "task": m["id"],
                             "provider": run["provider"], "model": model,
                             **{k: u.get(k, 0) for k in TOKEN_FIELDS}, "usd": usd,
                             "priced_by": source if usd is not None else "no price",
                             "turns": run["turns"] if i == 0 else 0, "runs": 1 if i == 0 else 0})
    return rows


def slug(path: Path) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


def coordinator_dirs(desk: Desk) -> list:
    """(provider, folder of the coordinator's session records) for every provider it may have run
    on: when the principal moves it to another provider, what it spent before still counts."""
    s = desk.settings()
    co = s["coordinator"]["provider"]
    homes = {}
    for name in sorted(sorted(s["providers"]), key=lambda n: n != co):  # a shared home goes by the coordinator's
        p = s["providers"][name]
        if p.get("kind") == "compatible":
            home = desk.state_dir / f"claude-{name}"
        else:
            home = Path(p.get("config_dir") or os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
        homes.setdefault(str(home), name)
    for home in sorted(desk.state_dir.glob("claude-*")) if desk.state_dir.is_dir() else []:
        homes.setdefault(str(home), home.name[len("claude-"):])  # a provider the principal has since removed
    return [(name, Path(home) / "projects" / slug(desk.root)) for home, name in homes.items()]


def transcript_files(desk: Desk, since: float):
    """(provider, file, seat) for every session record file of the coordinator and its helpers
    changed since then."""
    for provider, proj in coordinator_dirs(desk):
        if not proj.is_dir():
            continue
        for f in sorted(proj.rglob("*.jsonl")):
            if f.stat().st_mtime >= since:
                yield provider, f, helper_seat(f) if "subagents" in f.parts else "coordinator"


def records(f: Path, usage_only: bool = False):
    """The records of one file. The session may be writing it: a half-written last line is skipped."""
    with open(f, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if usage_only and '"usage"' not in line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(d, dict):
                yield d


def step(d: dict, seen: set, since: float):
    """(model, usage in hansa's fields, time) for the first line of one model call in the window, else None.
    A message is spread over several lines with the same id and usage: it counts once."""
    msg = d.get("message") if isinstance(d.get("message"), dict) else {}
    u, mid = msg.get("usage"), msg.get("id")
    if not u or not mid or mid in seen or str(msg.get("model", "")).startswith("<"):
        return None
    seen.add(mid)
    ts = _ts(d.get("timestamp"))
    if ts < since:
        return None
    cc = u.get("cache_creation") or {}
    out = {"in": u.get("input_tokens", 0), "out": u.get("output_tokens", 0),
           "cache_read": u.get("cache_read_input_tokens", 0), "cache_write": u.get("cache_creation_input_tokens", 0),
           "cache_write_5m": cc.get("ephemeral_5m_input_tokens", 0), "cache_write_1h": cc.get("ephemeral_1h_input_tokens", 0)}
    if not cc_split_known(out):
        out.pop("cache_write_5m"), out.pop("cache_write_1h")
    return msg.get("model", "?"), out, ts


def record_seat(d: dict, seat: str) -> str:
    """A sidechain in the coordinator's own file is a helper's."""
    return "helper" if seat == "coordinator" and d.get("isSidechain") else seat


def transcript_rows(desk: Desk, since: float) -> list:
    s = desk.settings()
    per = defaultdict(lambda: {**{k: 0 for k in TOKEN_FIELDS}, "usd": 0.0, "unpriced": False})
    seen = set()
    for provider, f, seat in transcript_files(desk, since):
        for d in records(f, usage_only=True):
            got = step(d, seen, since)
            if not got:
                continue
            model, u, ts = got
            row = per[(_day(ts), record_seat(d, seat), model, provider)]
            for k in TOKEN_FIELDS:
                row[k] += u[k]
            usd = value(model, u, s["prices"])  # each call valued alone, as hansa cost --turns does
            if usd is None:
                row["unpriced"] = True
            else:
                row["usd"] += usd
    rows = []
    for (day, seat, model, provider), u in sorted(per.items()):
        usd = None if u["unpriced"] else u["usd"]
        rows.append({"day": day, "seat": seat, "lane": "", "task": "", "provider": provider,
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


def coordinator_uncounted(desk: Desk) -> str:
    """Why the coordinator's own usage is not in the report, when it runs as another user than hansa
    (root, on a server, while the desk is an account's): Claude Code keeps that user's session records
    private to it. '' when it is counted."""
    who = read_json(desk.state_dir / "coordinator.json", {}) or {}
    if not isinstance(who, dict) or who.get("uid") is None or who["uid"] == account.uid():
        return ""
    return (f"The coordinator's own usage and its helpers' are not counted: the coordinator runs as "
            f"{who.get('user') or 'another user'}, and Claude Code keeps its session records private to that user, "
            f"where hansa, running as {account.name_of(account.uid())}, cannot read them.")


TURN_KINDS = {"human": "typed", "peer": "peer", "task-notification": "helper result"}


def _starts_turn(d: dict):
    """What a record starting a coordinator turn says started it, or None when it starts none: the principal
    typing, a hansa post or another session's message, a scheduled wakeup, a helper's result, or other.
    A record with an origin starts a turn whatever isMeta says (a hansa post is isMeta with origin peer); tool
    results, and an isMeta record without an origin (the note that follows a wakeup), carry on the turn they
    belong to."""
    if d.get("type") == "system":
        return "wakeup" if d.get("subtype") == "scheduled_task_fire" else None
    if d.get("type") != "user":
        return None
    content = (d.get("message") or {}).get("content") if isinstance(d.get("message"), dict) else None
    if isinstance(content, list) and content and all(
            isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
        return None
    origin = d.get("origin") if isinstance(d.get("origin"), dict) else {}
    if d.get("isMeta") and not origin.get("kind"):
        return None
    return TURN_KINDS.get(origin.get("kind"), "other")


def turns(desk: Desk, days: int = 7, since: float | None = None) -> dict:
    """hansa cost --turns: the coordinator's sessions turn by turn, valued as hansa cost values them, so the
    total is the coordinator's row of hansa cost --by seat over the same window. Only times, counts, tool
    names and numbers: no message text."""
    window = (f"since {time.strftime('%m-%d %H:%M', time.localtime(since))}" if since is not None
              else f"last {days} day(s)")
    since = since if since is not None else time.time() - days * 86400
    prices = desk.settings()["prices"]
    seen, tools_seen = set(), set()
    out, helpers, unpriced = [], defaultdict(lambda: {"steps": 0, "usd": 0.0}), set()
    shares = {k: 0.0 for k in TOKEN_FIELDS}
    for provider, f, seat in transcript_files(desk, since):
        cur = None
        for d in records(f):
            s_ = record_seat(d, seat)
            got = step(d, seen, since)
            if s_ != "coordinator":
                if got:
                    usd = value(got[0], got[1], prices)
                    helpers[s_]["steps"] += 1
                    helpers[s_]["usd"] += usd or 0
                    if usd is None:
                        unpriced.add(got[0])
                continue
            kind = _starts_turn(d)
            if kind or (cur is None and got):
                cur = {"session": f.stem, "start": _ts(d.get("timestamp")), "kind": kind or "other", "steps": 0,
                       "length": 0, "usd": 0.0, "tools": defaultdict(int)}
                out.append(cur)
            if cur is None or d.get("type") != "assistant":
                continue
            if got:
                model, u, _ = got
                p_ = parts(model, u, prices)
                if cur["steps"] == 0:
                    cur["length"] = u["in"] + u["cache_read"] + u["cache_write"]
                cur["steps"] += 1
                if p_ is None:
                    unpriced.add(model)
                else:
                    cur["usd"] += sum(p_.values())
                    for k in TOKEN_FIELDS:
                        shares[k] += p_[k]
            if cur["steps"]:  # a call outside the window (or seen before) brings no tools either
                content = (d.get("message") or {}).get("content")
                for b in content if isinstance(content, list) else []:
                    if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("id") not in tools_seen:
                        tools_seen.add(b.get("id"))
                        cur["tools"][str(b.get("name", "?"))] += 1
    rows = sorted((t for t in out if t["steps"]), key=lambda t: t["start"])
    for t in rows:
        t["usd"] = round(t["usd"], 6)
        # started before the window: its steps, worth and length count from its first step inside it
        t["before_window"] = 0 < t["start"] < since
        t["tools"] = dict(sorted(t["tools"].items(), key=lambda kv: (-kv[1], kv[0])))
    total = sum(t["usd"] for t in rows)
    by = {}
    for t in rows:
        b = by.setdefault(t["kind"], {"turns": 0, "steps": 0, "usd": 0.0})
        b["turns"] += 1
        b["steps"] += t["steps"]
        b["usd"] += t["usd"]
    for b in by.values():
        b["usd"] = round(b["usd"], 6)
        b["share"] = b["usd"] / total if total else 0.0
    worth = sum(shares.values())
    return {"window": window, "turns": rows, "by_kind": dict(sorted(by.items(), key=lambda kv: -kv[1]["usd"])),
            "total": {"turns": len(rows), "steps": sum(t["steps"] for t in rows), "usd": round(total, 6)},
            "token_shares": {k: (v / worth if worth else 0.0) for k, v in shares.items()},
            "helpers": {k: {**v, "usd": round(v["usd"], 6)} for k, v in sorted(helpers.items())},
            "unpriced_models": sorted(unpriced), "coordinator_uncounted": coordinator_uncounted(desk)}


def unassigned(r: dict) -> str:
    """The row, in the lane and task views, for usage that belongs to no lane or task, so that the rows add up."""
    return "(coordinator)" if r["seat"] == "coordinator" or r["seat"].startswith("helper") else "(none)"


def report(desk: Desk, days: int = 7, since: float | None = None) -> dict:
    window = (f"since {time.strftime('%m-%d %H:%M', time.localtime(since))}" if since is not None
              else f"last {days} day(s)")
    since = since if since is not None else time.time() - days * 86400
    rows = worker_rows(desk, since) + transcript_rows(desk, since)
    by = {}
    for key in ("day", "seat", "lane", "task", "model", "provider"):
        agg = defaultdict(lambda: {"tokens": 0, "usd": 0.0, "unpriced_tokens": 0, "turns": 0, "runs": 0})
        for r in rows:
            a = agg[r[key] or unassigned(r)]  # the coordinator's usage has no lane or task: a row of its own
            t = sum(r[k] for k in TOKEN_FIELDS)
            a["tokens"] += t
            a["turns"] += r.get("turns", 0)  # the workers' runs: the coordinator's turns are hansa cost --turns
            a["runs"] += r.get("runs", 0)
            if r["usd"] is None:
                a["unpriced_tokens"] += t
            else:
                a["usd"] += r["usd"]
        by[key] = {k: {**v, "usd": round(v["usd"], 6)} for k, v in sorted(agg.items())}
    total_usd = sum(r["usd"] for r in rows if r["usd"] is not None)
    return {"days": days, "window": window, "rows": rows, "by": by,
            "total": {"tokens": sum(sum(r[k] for k in TOKEN_FIELDS) for r in rows), "usd": round(total_usd, 6),
                      "turns": sum(r.get("turns", 0) for r in rows), "runs": sum(r.get("runs", 0) for r in rows)},
            "unpriced_models": sorted({r["model"] for r in rows if r["usd"] is None}),
            "coordinator_uncounted": coordinator_uncounted(desk),
            "coordinator_user": (read_json(desk.state_dir / "coordinator.json", {}) or {}).get("user", ""),
            "note": "Dollars use the principal's price table (a Claude model without an entry uses Claude Code's list price). Subscriptions are paid by the month: this is what the tokens are worth, not a bill."}
