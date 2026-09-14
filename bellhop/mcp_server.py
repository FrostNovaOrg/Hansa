"""A minimal MCP server (stdio, JSON-RPC 2.0, newline-delimited) exposing desk operations.

Started by Claude Code from the plugin's `.mcp.json`. While the coordinator session is open it
also runs the session-mode watchdog in a background thread, unless a resident daemon is alive.
Standard library only.
"""
from __future__ import annotations

import json
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Optional

from . import archive as archive_mod
from . import card as card_mod
from . import daemon as daemon_mod
from . import decisions as decisions_mod
from . import ledger as ledger_mod
from . import readings as readings_mod
from . import ring as ring_mod
from . import sampling
from . import sentinel as sentinel_mod
from . import watch as watch_mod
from .config import Desk, find_desk
from .util import BellhopError, append, read, ts

PROTOCOL_VERSION = "2025-06-18"

TOOLS = [
    {'name': 'support_submit', 'description': 'Delegate coordinator-owned research or implementation preparation to an isolated GLM worker with Astra fallback. Not a formal product task. Commands must come from support doctor configuration.',
     'inputSchema': {'type': 'object', 'properties': {
         'title': {'type': 'string'}, 'prompt': {'type': 'string'}, 'mode': {'enum': ['report', 'code']},
         'provider': {'enum': ['glm', 'astra']}, 'source_root': {'type': 'string'},
         'input_set': {'type': 'string'},
         'files': {'type': 'array', 'items': {'type': 'string'}},
         'writable_files': {'type': 'array', 'items': {'type': 'string'}},
         'commands': {'type': 'array', 'items': {'type': 'string'}}}, 'required': ['title', 'prompt']}},
    {'name': 'support_status', 'description': 'Read a support job or compact queue; completed results remain until reviewed.',
     'inputSchema': {'type': 'object', 'properties': {'id': {'type': 'string'}}}},
    {'name': 'support_result', 'description': 'Read the complete support worker report body and actual status. Does not acknowledge it.',
     'inputSchema': {'type': 'object', 'properties': {'id': {'type': 'string'}}, 'required': ['id']}},
    {'name': 'support_review', 'description': 'Record coordinator review/disposition of a completed support job. Does not accept formal product work.',
     'inputSchema': {'type': 'object', 'properties': {'id': {'type': 'string'}, 'note': {'type': 'string'}}, 'required': ['id', 'note']}},
    {"name": "desk_status", "description": "Sentinel status, dispatch card, pending bells, watchdog heartbeat.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "ring", "description": "Ring a bell for a task packet (path relative to the desk).",
     "inputSchema": {"type": "object", "properties": {"packet": {"type": "string"}}, "required": ["packet"]}},
    {"name": "watch_once", "description": "Answer one pending bell now (session mode).",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "sample", "description": "Is this task id sampled for deep acceptance?",
     "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}},
    {"name": "refresh_card", "description": "Refresh the dispatch card. ready is 'yes' or 'no'; a 'no' needs a reason.",
     "inputSchema": {"type": "object", "properties": {"ready": {"type": "string"}, "next": {"type": "string"}, "lane": {"type": "string"},
                                                       "reason": {"type": "string"}, "queue": {"type": "string"}}, "required": ["ready"]}},
    {"name": "ledger_add", "description": "Append one ledger entry (and commit it with the state files).",
     "inputSchema": {"type": "object", "properties": {"title": {"type": "string"}, "body": {"type": "string"},
                                                       "refs": {"type": "array", "items": {"type": "string"}}}, "required": ["title", "body"]}},
    {"name": "decide", "description": "Record the principal's decision verbatim (append-only) and commit it.",
     "inputSchema": {"type": "object", "properties": {"words": {"type": "string"}, "who": {"type": "string"}, "scope": {"type": "string"},
                                                       "expires": {"type": "string"}, "standing": {"type": "boolean"}}, "required": ["words"]}},
    {"name": "readings", "description": "Write the weekly readings report and return its path.",
     "inputSchema": {"type": "object", "properties": {"week": {"type": "string"}}}},
]

STATE_FILES = ("state/next.md", "state/in-flight.md", "state/debts.md", "state/human-todo.md")


class Server:
    def __init__(self, desk_root: Optional[Path] = None):
        self.desk: Optional[Desk] = None
        self.desk_error = ""
        try:
            self.desk = find_desk(desk_root)
        except BellhopError as exc:
            self.desk_error = str(exc)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # -- session watchdog --------------------------------------------------
    def start_session_watchdog(self) -> None:
        """Always start the thread; each tick yields to a resident watchdog whose heartbeat is fresh."""
        if not self.desk or not self.desk.cfg.get("session_watchdog", True):
            return
        self._thread = threading.Thread(target=self._watch_loop, name="bellhop-session-watchdog", daemon=True)
        self._thread.start()
        self._log("session watchdog started (yields while a resident watchdog is alive)")

    def _watch_loop(self) -> None:
        interval = max(0.05, float(self.desk.cfg.get("session_watchdog_interval", 5)))
        pool = watch_mod.WatchPool(self.desk)
        try:
            while not self._stop.is_set() or pool.active:
                try:
                    resident = daemon_mod.resident_alive(self.desk)
                    if not resident:
                        daemon_mod.touch_heartbeat(self.desk, "session")
                    pool.tick(dispatch=not self._stop.is_set() and not resident)
                except Exception as exc:  # keep the thread alive
                    append(self.desk.bells_logs / "watchdog.log", f"{ts()}\tRED\tsession watchdog: {exc}\n")
                if self._stop.is_set():
                    time.sleep(interval)  # drain without spinning on an already-set Event
                else:
                    self._stop.wait(interval)
        finally:
            pool.close()

    # -- tools -------------------------------------------------------------
    def _need_desk(self) -> Desk:
        if not self.desk:
            raise BellhopError(self.desk_error or "no desk")
        return self.desk

    def _commit(self, paths, message) -> str:
        try:
            return archive_mod.archive(self.desk, [self.desk.path(p) if isinstance(p, str) else p for p in paths], message)
        except BellhopError as exc:
            return f"(not archived: {exc})"

    def tool_desk_status(self, args) -> str:
        desk = self._need_desk()
        green, text = sentinel_mod.sentinel(desk, wake=False)
        card = card_mod.read_card(desk)
        pending = len(list(desk.bells_pending.glob("*.bell")))
        hb = daemon_mod.heartbeat_age(desk)
        hb_text = "none" if hb is None else f"{hb:.0f}s ago"
        return (f"{text}card: ready={card.get('ready')} next={card.get('next') or '-'} reason={card.get('reason') or '-'}\n"
                f"pending bells: {pending}\nwatchdog heartbeat: {hb_text}\nsession watchdog thread: {'running' if self._thread and self._thread.is_alive() else 'off'}\n")

    def tool_ring(self, args) -> str:
        desk = self._need_desk()
        bell = ring_mod.ring(desk, desk.root / args["packet"])
        return f"rung: {bell.relative_to(desk.root)}"

    def tool_watch_once(self, args) -> str:
        desk = self._need_desk()
        result = watch_mod.watch_once(desk)
        if not result:
            return "no bell to answer"
        return f"{'DEAD' if result['dead'] else 'done'}: {result['id']} on {result['lane']} in {result['duration_s']}s cost={result.get('cost_usd')}"

    def tool_sample(self, args) -> str:
        desk = self._need_desk()
        hit, seed, digest = sampling.sampled(desk, str(args["id"]))
        return f"{'sampled' if hit else 'not sampled'} id={args['id']} seed={seed} digest={digest} rate=1/{desk.cfg['sample_rate']}"

    def tool_refresh_card(self, args) -> str:
        desk = self._need_desk()
        fields = {k: args.get(k) for k in ("ready", "next", "lane", "reason", "queue")}
        card = card_mod.refresh_card(desk, **fields)
        return f"card refreshed {card['refreshed']}: ready={card.get('ready')} next={card.get('next') or '-'}"

    def tool_ledger_add(self, args) -> str:
        desk = self._need_desk()
        path = ledger_mod.add(desk, args["title"], args["body"], refs=args.get("refs") or [])
        sha = self._commit([path, *STATE_FILES], f"ledger: {args['title']}")
        return f"{path.relative_to(desk.root)} archived at {sha}"

    def tool_decide(self, args) -> str:
        desk = self._need_desk()
        decisions_mod.decide(desk, args["words"], who=args.get("who") or "principal", scope=args.get("scope") or "",
                             expires=args.get("expires") or "until revoked", standing=bool(args.get("standing")))
        sha = self._commit(["state/decisions.md"], f"decision by {args.get('who') or 'principal'}")
        return f"recorded; archived at {sha}"

    def tool_readings(self, args) -> str:
        desk = self._need_desk()
        out = readings_mod.report(desk, week=args.get("week"))
        sha = self._commit([out], f"readings {out.stem}")
        return f"{out.relative_to(desk.root)} archived at {sha}\n\n{read(out)}"

    # -- protocol ----------------------------------------------------------
    def tool_support_submit(self, args):
        from .support import submit
        job = submit(self._need_desk(), args)
        return json.dumps({k: job.get(k) for k in ('id', 'title', 'status', 'unit')}, ensure_ascii=False) + '\nUse Monitor with bellhop support listen JOB_ID for completion; the board also retains unread results.'

    def tool_support_status(self, args):
        from .support import get, summary
        return json.dumps(get(self._need_desk(), args['id']) if args.get('id') else summary(self._need_desk()), ensure_ascii=False)

    def tool_support_result(self, args):
        from .support import get, result_text
        desk = self._need_desk(); job = get(desk, args['id'])
        return json.dumps({k: job.get(k) for k in ('id', 'status', 'model', 'effort', 'error')}, ensure_ascii=False) + '\n' + result_text(desk, args['id'])

    def tool_support_review(self, args):
        from .support import review
        return json.dumps(review(self._need_desk(), args['id'], args['note']), ensure_ascii=False)

    def handle(self, msg: dict):
        method = msg.get("method")
        mid = msg.get("id")
        params = msg.get("params") or {}
        if method == "initialize":
            version = params.get("protocolVersion") or PROTOCOL_VERSION
            return self._result(mid, {"protocolVersion": version, "capabilities": {"tools": {}},
                                      "serverInfo": {"name": "bellhop", "version": _version()}})
        if method == "notifications/initialized":
            self.start_session_watchdog()
            return None
        if method and method.startswith("notifications/"):
            return None
        if method == "ping":
            return self._result(mid, {})
        if method == "tools/list":
            return self._result(mid, {"tools": TOOLS})
        if method == "tools/call":
            name = params.get("name")
            fn = getattr(self, f"tool_{name}", None)
            if not fn:
                return self._error(mid, -32602, f"unknown tool: {name}")
            try:
                text = fn(params.get("arguments") or {})
                return self._result(mid, {"content": [{"type": "text", "text": text}], "isError": False})
            except BellhopError as exc:
                return self._result(mid, {"content": [{"type": "text", "text": f"bellhop: {exc}"}], "isError": True})
            except Exception as exc:
                self._log(traceback.format_exc())
                return self._result(mid, {"content": [{"type": "text", "text": f"internal error: {exc}"}], "isError": True})
        if mid is None:
            return None
        return self._error(mid, -32601, f"method not found: {method}")

    @staticmethod
    def _result(mid, result):
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    @staticmethod
    def _error(mid, code, message):
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}

    @staticmethod
    def _log(text: str) -> None:
        sys.stderr.write(f"[bellhop mcp] {text.rstrip()}\n")
        sys.stderr.flush()

    def serve(self) -> int:
        out = sys.stdout
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            reply = self.handle(msg)
            if reply is not None:
                out.write(json.dumps(reply) + "\n")
                out.flush()
        self._stop.set()
        return 0


def _version() -> str:
    from . import __version__
    return __version__


def main(desk_root: Optional[Path] = None) -> int:
    return Server(desk_root).serve()
