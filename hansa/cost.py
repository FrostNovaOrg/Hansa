"""Tokens and what they are worth, from Claude Code's own records and the principal's prices.

Two sources, never counted twice:
- worker runs: the usage Claude Code returned for each run of each task (tasks/<id>/meta.json): its last run, and
  each earlier try, kept on its attempt record when the task went back to the queue (or in earlier_runs, for a run
  an earlier hansa serve started and lost track of); each run counted once;
- the coordinator and its helpers: the transcripts Claude Code writes for sessions
  started in the desk (<config dir>/projects/<desk path>/**/*.jsonl), and, since the desk began, those of the
  coordinator's sessions hansa noted from its commands (note_session) and of every other session in the folder
  those ran in, its working folder: what the coordinator costs is everything spent there (working_folders).

Prices come from the principal's table. A Claude model with no entry, run on a
subscription, falls back to Claude Code's own list-price figure; any other model
with no price is listed, not guessed (Claude Code's figure for a model it does not
know is a placeholder). A subscription is paid by the month, so the dollar figure
is what the tokens are worth, for comparing and spotting waste, not a bill.
"""
from __future__ import annotations

import json
import os
import re
import time
from bisect import bisect_right
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import account, notify, release, session_value, up
from .desk import Desk, event_record, read_json, write_json

TOKEN_FIELDS = ("in", "out", "cache_read", "cache_write")
# in .hansa/: the Claude Code sessions the coordinator's process ran, each noted at a hansa command run in it
SESSIONS = "coordinator-sessions.json"
SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{7,63}")


def price_for(model: str, prices: dict):
    pr = prices.get(model) or prices.get(re.sub(r"-\d{8}$", "", model))
    if not pr:
        return None
    i = pr["in"]
    cw = pr.get("cache_write", i * 1.25)
    cw1h = i * 2 if model.startswith("claude-") or "cache_write" not in pr else cw
    return {"in": i, "out": pr["out"], "cache_read": pr.get("cache_read", i * 0.1),
            "cache_write": cw, "cache_write_1h": pr.get("cache_write_1h", cw1h)}


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


def worker_rows(desk: Desk, since: float, s: dict | None = None, tasks: list | None = None) -> list:
    """One row per model of each run of each task that ended in the window. A run's turns, and the run itself
    (runs), are on its first row, so that they add up. s, tasks: the desk's settings and tasks, when the caller
    has read them already."""
    s = desk.settings() if s is None else s
    rows = []
    for m in desk.tasks() if tasks is None else tasks:
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


def note_session(desk: Desk) -> None:
    """Note the Claude Code session this command runs in, when its process is the coordinator's one, the one hansa
    up started or hansa notify on recorded (coordinator-start.json): its id, as Claude Code gives it to the command,
    and the Claude login folder it runs with. A /clear gives the same process a new id, noted at its next hansa
    command; a compaction keeps it. Never raises."""
    try:
        sess, sid = notify.session_here(), session_value("CLAUDE_CODE_SESSION_ID")
        if not sess or not SESSION_ID.fullmatch(sid) or not notify.same(up.read_coordinator(desk).get("session"), sess):
            return
        known = known_sessions(desk)
        if any(x.get("id") == sid for x in known):
            return
        home = os.environ.get("CLAUDE_CONFIG_DIR") or str(Path.home() / ".claude")
        write_json(desk.state_dir / SESSIONS, [*known, {"id": sid, "home": home, "session": sess, "at": time.time()}])
    except Exception:  # noqa: BLE001 - the command runs all the same; the next one notes it
        pass


def known_sessions(desk: Desk) -> list:
    """The coordinator's sessions hansa noted (note_session), oldest first."""
    got = read_json(desk.state_dir / SESSIONS, [])
    return [x for x in got if isinstance(x, dict) and SESSION_ID.fullmatch(str(x.get("id", "")))] \
        if isinstance(got, list) else []


def session_files(proj: Path, sid: str) -> list:
    """A session's record files under one Claude login folder's projects/, whatever folder it started in: its own,
    and its helpers'."""
    projects = proj.parent
    return sorted([*projects.glob(f"*/{sid}.jsonl"), *projects.glob(f"*/{sid}/**/*.jsonl")])


def began(desk: Desk) -> float:
    """When the desk began: the time of its first event (hansa init records desk_created), else 0."""
    try:
        with open(desk.events_path, "rb") as fh:
            for line in fh:
                e = event_record(line)
                if e is not None and isinstance(e.get("ts"), (int, float)):
                    return float(e["ts"])
    except OSError:
        pass
    return 0.0


def working_folders(desk: Desk, dirs: list) -> list:
    """(name of the folder's records under projects/, from, until) for each folder the coordinator's noted sessions
    ran in, in turn, by where their records are: the first from when the desk began, each next one from when hansa
    noted its first session there. None noted: none, and the desk's own folder counts as it does anyway."""
    out = []
    for x in known_sessions(desk):
        f = next((g for _, proj in dirs if proj.parent.is_dir() for g in proj.parent.glob(f"*/{x['id']}.jsonl")), None)
        if f is not None and (not out or out[-1][0] != f.parent.name):
            at = x.get("at") if isinstance(x.get("at"), (int, float)) else 0
            out.append([f.parent.name, max(at, out[-1][1]) if out else began(desk), float("inf")])
            if len(out) > 1:
                out[-2][2] = out[-1][1]
    return [tuple(w) for w in out]


def transcript_files(desk: Desk, since: float):
    """(provider, file, seat, from, until) for every session record file of the coordinator and its helpers changed
    since then: every session started in the desk; each session of the coordinator's hansa noted, wherever it
    started, since the desk began; and every other session in the folders those ran in (working_folders), while
    each was the coordinator's."""
    dirs, done, start = coordinator_dirs(desk), set(), max(since, began(desk))
    ids = [x["id"] for x in known_sessions(desk)]
    folders = working_folders(desk, dirs) if ids else []
    for provider, proj in dirs:
        files = [(f, since, float("inf")) for f in sorted(proj.rglob("*.jsonl"))] if proj.is_dir() else []
        if ids and proj.parent.is_dir():
            files += [(f, start, float("inf")) for sid in ids for f in session_files(proj, sid)]
            files += [(f, max(since, lo), hi) for name, lo, hi in folders if (proj.parent / name).is_dir()
                      for f in sorted((proj.parent / name).rglob("*.jsonl"))]
        for f, lo, hi in files:
            if f not in done and lo < hi and f.stat().st_mtime >= lo:
                done.add(f)
                yield provider, f, helper_seat(f) if "subagents" in f.parts else "coordinator", lo, hi


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


def step(d: dict, seen: set, since: float, until: float = float("inf")):
    """(model, usage in hansa's fields, time) for the first line of one model call in the window, else None.
    A message is spread over several lines with the same id and usage: it counts once."""
    msg = d.get("message") if isinstance(d.get("message"), dict) else {}
    u, mid = msg.get("usage"), msg.get("id")
    if not u or not mid or mid in seen or str(msg.get("model", "")).startswith("<"):
        return None
    seen.add(mid)
    ts = _ts(d.get("timestamp"))
    if ts < since or ts >= until:
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
    return scan(desk, since)["rows"]


def scan(desk: Desk, since: float) -> dict:
    """One read of the coordinator's and its helpers' records in the window: the rows hansa cost adds up, by day,
    seat, model and provider, and the coordinator's turns as hansa cost --turns lists them (see turns)."""
    prices = desk.settings()["prices"]
    per = defaultdict(lambda: {**{k: 0 for k in TOKEN_FIELDS}, "usd": 0.0, "unpriced": False})
    seen, tools_seen = set(), set()
    out, helpers, unpriced = [], defaultdict(lambda: {"steps": 0, "usd": 0.0}), set()
    shares = {k: 0.0 for k in TOKEN_FIELDS}
    for provider, f, seat, lo, hi in transcript_files(desk, since):
        cur = None
        try:
            for d in records(f):
                s_ = record_seat(d, seat)
                got = step(d, seen, lo, hi)
                usd = value(got[0], got[1], prices) if got else None  # each call valued alone
                if got:
                    row = per[(_day(got[2]), s_, got[0], provider)]
                    for k in TOKEN_FIELDS:
                        row[k] += got[1][k]
                    row["usd"] += usd or 0
                    row["unpriced"] |= usd is None
                    if usd is None:
                        unpriced.add(got[0])
                if s_ != "coordinator":
                    if got:
                        helpers[s_]["steps"] += 1
                        helpers[s_]["usd"] += usd or 0
                    continue
                kind = _starts_turn(d)
                if kind or (cur is None and got):
                    cur = {"session": f.stem, "start": _ts(d.get("timestamp")), "kind": kind or "other", "steps": 0,
                           "length": 0, "usd": 0.0, "tools": defaultdict(int), "first": 0.0, "last": 0.0}
                    out.append(cur)
                if cur is None or d.get("type") != "assistant":
                    continue
                if got:
                    model, u, ts = got
                    p_ = parts(model, u, prices)
                    if cur["steps"] == 0:
                        cur["length"], cur["first"] = u["in"] + u["cache_read"] + u["cache_write"], ts
                    cur["steps"] += 1
                    cur["last"] = max(cur["last"], ts)
                    if p_ is not None:
                        cur["usd"] += sum(p_.values())
                        for k in TOKEN_FIELDS:
                            shares[k] += p_[k]
                if cur["steps"]:  # a call outside the window (or seen before) brings no tools either
                    content = (d.get("message") or {}).get("content")
                    for b in content if isinstance(content, list) else []:
                        if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("id") not in tools_seen:
                            tools_seen.add(b.get("id"))
                            cur["tools"][str(b.get("name", "?"))] += 1
        except OSError:  # unreadable: coordinator_uncounted says so
            continue
    rows = []
    for (day, seat, model, provider), u in sorted(per.items()):
        usd = None if u["unpriced"] else u["usd"]
        rows.append({"day": day, "seat": seat, "lane": "", "task": "", "provider": provider,
                     "model": model, **{k: u[k] for k in TOKEN_FIELDS}, "usd": usd,
                     "priced_by": "price table" if usd is not None else "no price"})
    return {"rows": rows, "turns": sorted((t for t in out if t["steps"]), key=lambda t: t["start"]),
            "helpers": helpers, "unpriced": unpriced, "shares": shares}


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
        if len(text) < 19 or text[10] != "T":
            return 0.0
        return datetime.fromisoformat(text[:19]).replace(tzinfo=timezone.utc).timestamp()  # strptime's is slow
    except (TypeError, ValueError):
        return 0.0


def coordinator_uncounted(desk: Desk) -> str:
    return uncounted(desk)["coordinator_uncounted"]


def uncounted(desk: Desk) -> dict:
    """Why the coordinator's own usage is not in the report ('' when it is counted), and what kind of why, for the
    page's translation: it runs as another user than hansa (root, on a server, while the desk is an account's), whose
    session records Claude Code keeps private to it ('user'); or its session started outside the desk, and hansa has
    not yet noted it ('untied'), it runs with another Claude login than the workers' ('login'), or its records are
    not where they should be ('missing') or cannot be read ('unreadable'). where: the folder or file it names."""
    def why(kind, text, where=""):
        return {"coordinator_uncounted": f"The coordinator's own usage and its helpers' are not counted: {text}"
                if kind else "", "coordinator_uncounted_kind": kind, "coordinator_uncounted_where": where}
    who = read_json(desk.state_dir / "coordinator.json", {}) or {}
    if isinstance(who, dict) and who.get("uid") is not None and who["uid"] != account.uid():
        return why("user", f"the coordinator runs as {who.get('user') or 'another user'}, and Claude Code keeps its "
                           f"session records private to that user, where hansa, running as "
                           f"{account.name_of(account.uid())}, cannot read them.")
    rec = up.read_coordinator(desk)
    folder, homes = rec.get("folder"), [proj.parent.parent for _, proj in coordinator_dirs(desk)]
    if not isinstance(folder, str) or not folder or slug(Path(os.path.realpath(folder))) == slug(desk.root):
        return why("", "")  # started in the desk: its records are the desk's folder's, counted as they are
    mine = [x for x in known_sessions(desk) if notify.same(x.get("session"), rec.get("session"))]
    if not mine:
        return why("untied", f"its session started in {folder}, outside the desk, and hansa counts the sessions "
                             f"there once a hansa command run in one has told hansa it is the coordinator's; none has "
                             f"yet.", folder)
    home = str(mine[-1].get("home") or "")
    if not any(os.path.realpath(home) == os.path.realpath(h) for h in homes):
        return why("login", f"its session runs with the Claude login in {home}, not the workers' "
                            f"({', '.join(map(str, homes))}), and hansa reads the workers' login only.", home)
    files = [f for _, proj in coordinator_dirs(desk) if proj.parent.is_dir() for f in session_files(proj, mine[-1]["id"])]
    if not files:
        return why("missing", f"the records of its session {mine[-1]['id']} are not in {home}/projects.", home)
    shut = next((f for f in files if not os.access(f, os.R_OK)), None)
    if shut:
        return why("unreadable", f"hansa cannot read its session's records, {shut}.", str(shut))
    return why("", "")


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
    got = scan(desk, since)
    rows, helpers, unpriced, shares = got["turns"], got["helpers"], got["unpriced"], got["shares"]
    for t in rows:
        t["usd"] = round(t["usd"], 6)
        # started before the window: its steps, worth and length count from its first step inside it
        t["before_window"] = 0 < t["start"] < since
        t["seconds"] = t.pop("last") - t.pop("first")  # the time it worked: from its first step to its last
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
            "total": {"turns": len(rows), "steps": sum(t["steps"] for t in rows), "usd": round(total, 6),
                      "seconds": sum(t["seconds"] for t in rows)},
            "token_shares": {k: (v / worth if worth else 0.0) for k, v in shares.items()},
            "helpers": {k: {**v, "usd": round(v["usd"], 6)} for k, v in sorted(helpers.items())},
            "unpriced_models": sorted(unpriced), **uncounted(desk)}


def unassigned(r: dict) -> str:
    """The row, in the lane and task views, for usage that belongs to no lane or task, so that the rows add up."""
    return "(coordinator)" if r["seat"] == "coordinator" or r["seat"].startswith("helper") else "(none)"


def work_buckets(tasks: list, since: float, now: float, days: int | None) -> dict:
    """Daily rolling windows, or calendar weeks in the desk's timezone (including partial weeks)."""
    weekly = days is None or days > 30
    first = since if since else min([now, *(m[k] for m in tasks for k in ("finished", "merged") if m.get(k))])
    starts = [first]
    if weekly:
        monday = date.fromisoformat(_day(first))
        monday += timedelta(days=7 - monday.weekday())
        while (stamp := time.mktime((*monday.timetuple()[:6], 0, 0, -1))) < now:
            starts.append(stamp)
            monday += timedelta(days=7)
    else:
        starts.extend(first + n * 86400 for n in range(1, days) if first + n * 86400 < now)
    return {"interval": "week" if weekly else "day", "buckets": [
        {"start": start, "end": end,
         "label": str(date.fromisoformat(_day(start)) - timedelta(days=time.localtime(start).tm_wday)) if weekly else _day(end),
         "range": " — ".join(time.strftime("%Y-%m-%d %H:%M", time.localtime(t)) for t in (start, end)),
         "finished": 0, "merged": 0}
        for start, end in zip(starts, [*starts[1:], now])]}


def recorded_lines(stat) -> tuple | None:
    """The saved --shortstat, never a fresh git diff. An empty/unrecognized record is unknown."""
    if not isinstance(stat, str) or not re.fullmatch(
            r"\s*\d+ files? changed(?:, \d+ insertions?\(\+\))?(?:, \d+ deletions?\(-\))?\s*", stat):
        return None
    return tuple(int(m[1]) if (m := re.search(r"(\d+) " + word, stat)) else 0
                 for word in ("insertion", "deletion"))


def work_history(tasks: list, releases: list, since: float, chart: dict | None = None) -> dict:
    """Outcomes by task, releases by request, and elapsed run time by when each ended.
    Replaced tasks retain ended_as; attempts are runs, never extra task outcomes.
    merged_first_try counts the merged tasks that no hansa retry sent back (those have a parent);
    verdicts counts the verifications that reached a verdict, held, did not hold or unsure."""
    out = dict.fromkeys(("finished", "merged", "merged_first_try", "failed", "verifications", "held",
                         "did_not_hold", "unsure", "verdicts",
                         "releases_asked", "releases_out", "lines_added", "lines_removed", "lines_omitted"), 0)
    seconds = 0
    inside = lambda stamp: stamp is not None and stamp > 0 and stamp >= since
    buckets = chart["buckets"] if chart else []
    starts = [b["start"] for b in buckets]

    def count(key, stamp):
        out[key] += 1
        if buckets:
            buckets[max(0, bisect_right(starts, stamp) - 1)][key] += 1

    for m in tasks:
        state = m.get("ended_as") if m.get("state") == "replaced" else m.get("state")
        if m.get("kind") == "verify":
            if state == "done" and inside(m.get("finished")):
                out["verifications"] += 1
                verdict = {"holds": "held", "holds, with notes": "held", "does not hold": "did_not_hold",
                           "unsure": "unsure"}.get(m.get("verdict"))
                if verdict:
                    out[verdict] += 1
                    out["verdicts"] += 1
        else:
            if inside(m.get("finished")):
                if state in ("done", "merged", "merging"):
                    count("finished", m["finished"])
                out["failed"] += state == "failed"
            if inside(m.get("merged")):
                count("merged", m["merged"])
                if not m.get("parent"):  # a follow-up hansa retry queued was sent back
                    out["merged_first_try"] += 1
                lines = recorded_lines(m.get("stat"))
                if lines is None:
                    out["lines_omitted"] += 1
                else:
                    out["lines_added"] += lines[0]
                    out["lines_removed"] += lines[1]
        runs = [r for r in [*(m.get("attempts") or []), *(m.get("earlier_runs") or [])] if isinstance(r, dict)]
        # A queued retry still has the last run's timestamps, but its run is already on its attempt record.
        if not any(r.get("ran_from") == m.get("started") and r.get("ended") for r in runs):
            runs.append({"ran_from": m.get("started"), "ended": m.get("finished")})
        for r in runs:
            start, end = r.get("ran_from"), r.get("ended")
            if start is not None and inside(end) and end >= start:
                seconds += end - start
    for r in releases:
        out["releases_asked"] += inside(r.get("asked"))
        out["releases_out"] += r.get("state") == "done" and inside(r.get("ended"))
    return {**out, "worker_hours": seconds / 3600}


def calendar(desk: Desk) -> dict:
    """Local calendar dates and the report's day totals, limited to 53 Monday-first weeks."""
    today = date.fromisoformat(_day(time.time()))
    monday = today - timedelta(days=today.weekday())
    start = max(date.fromisoformat(_day(began(desk))), monday - timedelta(weeks=52))
    first, last = start.isoformat(), today.isoformat()
    return {"start": first, "today": last,
            "days": {day: row for day, row in report(desk, None)["by"]["day"].items() if first <= day <= last}}


def report(desk: Desk, days: int | None = 7, since: float | None = None) -> dict:
    window = (f"since {time.strftime('%m-%d %H:%M', time.localtime(since))}" if since is not None
              else "since the desk began" if days is None else f"last {days} day(s)")
    now = time.time()
    since = since if since is not None else 0 if days is None else now - days * 86400
    tasks = desk.tasks()
    chart = work_buckets(tasks, since, now, days)
    # the coordinator's records too since the desk began: one read of them, about 3 s for 500 MB of a weeks-long session
    got = scan(desk, since)
    rows = worker_rows(desk, since, tasks=tasks) + got["rows"]
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
    total_usd = round(sum(r["usd"] for r in rows if r["usd"] is not None), 6)
    work = {**work_history(tasks, release.releases(desk), since, chart),
            # the coordinator's turns with a step in the window, and the time each worked, first step to last
            "coordinator_turns": len(got["turns"]),
            "coordinator_hours": sum(t["last"] - t["first"] for t in got["turns"]) / 3600}
    # what the period's value, the total shown, comes to per task merged in it; no merge: no figure
    work["usd_per_merged"] = round(total_usd / work["merged"], 6) if work["merged"] else None
    return {"days": days, "window": window, "rows": rows, "by": by, "work": work,
            "work_chart": chart,
            "total": {"tokens": sum(sum(r[k] for k in TOKEN_FIELDS) for r in rows), "usd": total_usd,
                      "turns": sum(r.get("turns", 0) for r in rows), "runs": sum(r.get("runs", 0) for r in rows)},
            "unpriced_models": sorted({r["model"] for r in rows if r["usd"] is None}),
            **uncounted(desk),
            "coordinator_user": (read_json(desk.state_dir / "coordinator.json", {}) or {}).get("user", ""),
            "note": "Dollars use the principal's price table (a Claude model without an entry uses Claude Code's list price). Subscriptions are paid by the month: this is what the tokens are worth, not a bill."}
