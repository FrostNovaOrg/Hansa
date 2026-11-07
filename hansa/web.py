"""The principal's web page: status, settings (including keys) and cost.

It only reads and writes the desk's files and never calls a model. It listens on
127.0.0.1; open it through an SSH tunnel with the address `hansa web` prints.
The page keeps the address's token in sessionStorage for its origin and tab,
and sends it explicitly in Authorization. Changes also need X-Hansa: 1.
The HTML shell is public; every API request requires the bearer token.
"""
from __future__ import annotations

import gzip
import hashlib
import hmac
import http.server
import json
import os
import re
import threading
import time
import urllib.parse
from pathlib import Path

from . import alert, cost, disk, gaps, goals, history, notes, release, views
from .desk import Desk, KeyScrubError, SettingsError, event_record, lane_warnings, public_settings, read_json
from .run import check_provider

UI = Path(__file__).with_name("ui.html")


def _accepts_gzip(value: str) -> bool:
    encodings = {}
    for item in value.lower().split(","):
        coding, *params = item.strip().split(";")
        quality = next((p.split("=", 1)[1].strip() for p in params if p.strip().startswith("q=")), "1")
        try:
            encodings[coding.strip()] = 0 < float(quality) <= 1
        except ValueError:
            encodings[coding.strip()] = False
    return encodings.get("gzip", encodings.get("*", False))


def _last_lines(path, n: int, block: int = 65536) -> list:
    """The last n lines of a file, as read_text().splitlines()[-n:] gives them, read from its end: the log grows
    with the desk, and the page asks for its latest events every few seconds."""
    with open(path, "rb") as fh:
        at = fh.seek(0, os.SEEK_END)
        parts, following, breaks = [], b"", 0
        tail, extra = b"", False
        # All str.splitlines() boundaries in UTF-8; count CRLF once, at its LF.
        boundary = re.compile(rb"\n|\r(?!\n)|[\v\f\x1c-\x1e]|\xc2\x85|\xe2\x80[\xa8\xa9]")
        while at > 0:
            end = at
            at = max(0, at - block)
            fh.seek(at)
            data = fh.read(end - at)
            parts.append(data)
            # Two bytes from the next block complete a split Unicode boundary or CRLF.
            scan = data + following
            breaks += sum(m.start() < len(data) for m in boundary.finditer(scan))
            following = scan[:2]
            # Count an unterminated final line too, even if its last character is incomplete.
            if len(tail) < 3:
                tail = (data + tail)[-3:]
                extra = not any(m.end() == len(tail) for m in boundary.finditer(tail))
            # Keep a line before the last n so each retained line starts in the file.
            if breaks + extra > n:
                break
    data = b"".join(reversed(parts))
    if at:  # cut where a character may go on: the rest of it, in the line that is left out
        data = data[next((i for i in range(min(4, len(data))) if not 0x80 <= data[i] < 0xC0), 0):]
    return data.decode("utf-8", errors="surrogateescape").splitlines()[1 if at else 0:][-n:]


def _events(desk: Desk, n: int = 60, kind: str = "") -> list:
    """The latest n events, newest first; with kinds (comma-separated), the latest n of those kinds in
    the whole log."""
    if not desk.events_path.exists():
        return []
    kinds = {k for k in kind.split(",") if k}
    lines = desk.events_path.read_text(encoding="utf-8", errors="surrogateescape").splitlines() \
        if kinds else _last_lines(desk.events_path, n)
    out = []
    for line in reversed(lines if kinds else lines[-n:]):
        e = event_record(line)
        if e is None:
            continue
        if not kinds or (isinstance(e, dict) and e.get("event") in kinds):
            out.append(e)
            if len(out) >= n:
                break
    return out


# The coordinator sets these (hansa lane, hansa concurrency, hansa disk-floor, hansa memory-floor).
NOT_FROM_PAGE = ("lanes", "concurrency", "disk_floor", "memory_floor", "bench")


def _save_settings(desk: Desk, body: dict) -> dict:
    from . import alert_push
    new = body.get("settings")
    with desk.lock():
        on_disk = desk.settings()
        if body.get("rev") is not None and body["rev"] != desk.settings_rev():
            return {"ok": False, "problems": ["the settings changed since this page loaded them (another tab or a command); "
                                              "press Reload, then make the change again"]}
        writes, removes = [], []
        problems = []
        if isinstance(new, dict):
            new = {**new, **{k: on_disk[k] for k in NOT_FROM_PAGE if k in on_disk}}
            new.setdefault("alert_push", on_disk["alert_push"])
            try:
                changes = body.get("alert_push_changes", [])
                if not isinstance(changes, list):
                    raise ValueError("push changes must be a list")
                value, writes, removes = alert_push.prepare(desk, new, changes, by="page")
                new["alert_push"] = value
                old_ids = {e["id"] for e in alert_push.entries(on_disk)}
                removes += list(old_ids - {e["id"] for e in value})
            except ValueError as error:
                problems = [str(error)]
        if not problems:
            problems = desk.save_settings(new, by="page", rev=body.get("rev"), dry=True)
        if not problems:
            failed = alert_push.apply(desk, writes, removes)
            problems = desk.save_settings(new, by="page")
            alert_push.check_scrub(failed)
    if not problems:
        for name in desk.settings()["lanes"]:
            brief = desk.lanes_dir / name / "brief.md"
            brief.parent.mkdir(parents=True, exist_ok=True)
            brief.touch(exist_ok=True)
    warnings = [] if problems else lane_warnings(new)
    return {"ok": not problems, "problems": problems, **({"lane_warnings": warnings} if warnings else {})}


def _push_test(desk: Desk, body: dict) -> dict:
    from . import alert_push
    id = body.get("id")
    if not isinstance(id, str) or not alert_push.ID.fullmatch(id):
        return {"ok": False, "why": "no_address", "last": None}
    why = alert_push.test(desk, id)
    return {"ok": why == "sent", "why": why, "last": alert_push.last_result(desk, id)}


def _top_up_provider(desk: Desk, name: str) -> dict:
    """Only the authenticated principal's page calls this; no coordinator command clears credit rests."""
    with desk.lock():
        if name not in desk.settings()["providers"]:
            return {"ok": False, "problems": ["no such provider"]}
        if (desk.provider_state().get(name) or {}).get("credit_since"):
            from .rests import record
            record(desk, name, credit_since=0, credit_cleared=time.time())
            desk.set_provider_state(name, credit_since=0, cool_until=0, reason="", rest_how="", rest_minutes=None)
            desk.event("provider_topped_up", provider=name, by="page")
    desk.ready_news()
    return {"ok": True}


def start_web(desk: Desk, host: str = "127.0.0.1", port: int = 8765):
    token = desk.web_token()

    class Page(http.server.BaseHTTPRequestHandler):
        server_version = "hansa"
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def _authed(self) -> bool:
            return hmac.compare_digest(self.headers.get("Authorization", "").encode(),
                                       ("Bearer " + token).encode())

        def _send(self, code: int, body, ctype: str = "application/json; charset=utf-8", *, conditional=False):
            data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
            # Weak validators cover either content coding. Only the public shell may be stored;
            # poll validators and their data live in page memory, with auth checked before _send.
            conditional = conditional and self.command == "GET" and code == 200
            tag = 'W/"' + hashlib.sha256(data).hexdigest() + '"' if conditional else None
            if tag and any(t.strip().removeprefix("W/") in ("*", tag[2:])
                           for t in self.headers.get("If-None-Match", "").split(",")):
                code = 304
            compressed = code != 304 and _accepts_gzip(self.headers.get("Accept-Encoding", ""))
            if compressed:
                data = gzip.compress(data, mtime=0)
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            if code != 304:
                self.send_header("Content-Length", str(len(data)))
            if compressed:
                self.send_header("Content-Encoding", "gzip")
            self.send_header("Vary", "Accept-Encoding")
            self.send_header("Cache-Control", "private, no-cache" if tag and ctype.startswith("text/html") else "no-store")
            if tag:
                self.send_header("ETag", tag)
            if getattr(self, "close_connection", False):
                self.send_header("Connection", "close")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            if code != 304:
                self.wfile.write(data)

        def _bad_request(self, code: str, field: str, problem: str):
            return self._send(400, {"ok": False, "code": code, "field": field, "problems": [problem]})

        def do_GET(self):
            try:
                return self._get()
            except SettingsError as e:
                return self._settings_unavailable(e)

        def _settings_unavailable(self, error):
            return self._send(503, {"ok": False, "code": "settings_unavailable", "problems": [str(error)]})

        def _get(self):
            url = urllib.parse.urlparse(self.path)
            if url.path == "/":
                return self._send(200, UI.read_bytes(), "text/html; charset=utf-8", conditional=True)
            if not self._authed():
                return self._send(403, b"Open the address that `hansa web` prints.", "text/plain; charset=utf-8")
            q = urllib.parse.parse_qs(url.query, keep_blank_values=True)
            if url.path == "/api/gaps":
                return self._send(200, gaps.page_list(desk))
            if url.path == "/api/notes":
                return self._send(200, notes.page_list(desk, q.get("archived", ["0"])[0] == "1"))
            if url.path == "/api/note":
                try:
                    return self._send(200, notes.page_note(desk, q.get("name", [""])[0], q.get("id", [""])[0]))
                except (OSError, ValueError):
                    return self._send(404, {"error": "note unavailable"})
            if url.path == "/api/status":
                return self._send(200, views.status(desk), conditional=True)
            if url.path == "/api/goals":
                return self._send(200, goals.page_list(desk), conditional=True)
            if url.path == "/api/history":
                try:
                    last = int(q.get("last", ["50"])[0])
                except ValueError:
                    last = 0
                if last < 1:
                    return self._bad_request("invalid_query", "last", "last must be at least 1")
                kind, name = q.get("kind", [""])[0], q.get("name", [""])[0]
                if kind not in ("", "lane", "provider") or bool(kind) != bool(name):
                    return self._bad_request("invalid_query", "kind", "use lane or provider with a name, or neither")
                return self._send(200, history.read(desk, kind, name, last), conditional=True)
            if url.path == "/api/timeline":
                return self._send(200, views.timeline(desk))
            if url.path == "/api/todos":
                try:
                    page = int(q.get("page", ["1"])[0])
                except ValueError:
                    return self._bad_request("invalid_query", "page", "page must be a whole number")
                state = q.get("state", [""])[0]
                if state not in ("", "open", "closed"):
                    return self._bad_request("invalid_query", "state", "state must be open or closed")
                return self._send(200, views.todo_list(desk, page=page, state=state))
            if url.path == "/api/tasks":
                try:
                    page = int(q.get("page", ["1"])[0])
                except ValueError:
                    return self._bad_request("invalid_query", "page", "page must be a whole number")
                period = q.get("days", ["all"])[0]
                if period not in ("1", "7", "30", "90", "all"):
                    return self._bad_request("invalid_query", "days", "days must be 1, 7, 30, 90 or all")
                return self._send(200, views.task_list(desk, page=page, days=None if period == "all" else int(period),
                    **{k: q.get(k, [""])[0] for k in ("state", "lane", "words")}))
            if url.path == "/api/settings":
                # with the free space of the desk's volume, shown beside the floor (hansa/disk.py)
                return self._send(200, {**public_settings(desk), "disk_free": disk.desk_free(desk)[0]})
            if url.path == "/api/cost/calendar":
                return self._send(200, cost.calendar(desk))
            if url.path == "/api/cost":
                try:
                    period = q.get("days", ["7"])[0]
                    days = None if period == "all" else max(1, min(90, int(period)))
                except ValueError:
                    return self._bad_request("invalid_query", "days", "days must be a whole number or all")
                rep = cost.report(desk, days)
                if q.get("rows", ["0"])[0] != "1":
                    rep.pop("rows")
                return self._send(200, rep)
            if url.path == "/api/events":
                try:
                    n = max(1, min(200, int(q.get("n", ["60"])[0])))
                except ValueError:  # not a number: the default
                    n = 60
                return self._send(200, _events(desk, n, q.get("event", [""])[0]), conditional=True)
            if url.path == "/api/task":
                tid = q.get("id", [""])[0]
                if not re.fullmatch(r"t\d+", tid) or not (desk.tasks_dir / tid / "meta.json").is_file():
                    return self._send(404, {"error": "no such task"})
                return self._send(200, {"meta": read_json(desk.tasks_dir / tid / "meta.json"),
                                        "text": views.show_text(desk, tid, full=True)})
            return self._send(404, {"error": "not found"})

        def do_POST(self):
            if not self._authed() or self.headers.get("X-Hansa") != "1":
                self.close_connection = True  # the unread body cannot become the next HTTP/1.1 request
                return self._send(403, {"error": "forbidden"})
            length = self.headers.get("Content-Length", "0")
            if self.headers.get("Transfer-Encoding") or not re.fullmatch(r"[0-9]+", length):
                self.close_connection = True
                return self._bad_request("invalid_length", "Content-Length", "body needs a nonnegative Content-Length")
            try:
                data = self.rfile.read(int(length)) or b"{}"
                try:
                    body = json.loads(data)
                except ValueError:
                    return self._bad_request("invalid_json", "body", "body must be valid JSON")
                if not isinstance(body, dict):
                    return self._bad_request("invalid_type", "body", "body must be a JSON object")
                path = urllib.parse.urlparse(self.path).path
                if path == "/api/settings":
                    return self._send(200, _save_settings(desk, body))
                if path == "/api/key":
                    desk.write_key(body["provider"], body.get("key", ""), by="page")
                    return self._send(200, {"ok": True})
                if path == "/api/provider/top-up":
                    return self._send(200, _top_up_provider(desk, body["provider"]))
                if path == "/api/pause":
                    desk.pause(body.get("reason") or "paused on the page", by="page")
                    return self._send(200, {"ok": True})
                if path == "/api/resume":
                    desk.resume(by="page")
                    return self._send(200, {"ok": True})
                if path == "/api/check":
                    return self._send(200, check_provider(desk, body["provider"]))
                if path == "/api/release/answer":  # the principal's yes or no, to the release as the page showed it
                    return self._send(200, release.answer(desk, int(body["id"]), body.get("yes") is True,
                                                          str(body.get("reason") or ""), str(body.get("commit") or ""),
                                                          str(body.get("command") or ""), by="page"))
                if path == "/api/release/stop":  # the principal's Stop: one not started is closed, a running one ended
                    return self._send(200, release.stop_release(desk, int(body["id"]), str(body.get("reason") or ""),
                                                                by="page"))
                if path == "/api/alert/test":  # the principal's test of the alert command; no command sends one
                    return self._send(200, alert.test(desk))
                if path == "/api/alert/push-test":
                    return self._send(200, _push_test(desk, body))
                return self._send(404, {"error": "not found"})
            except SettingsError as e:
                return self._settings_unavailable(e)
            except KeyScrubError as e:
                return self._send(400, {"ok": False, "saved": True, "problems": [str(e)]})
            except (KeyError, ValueError, TypeError) as e:
                return self._send(400, {"ok": False, "problems": [str(e)]})

    httpd = http.server.ThreadingHTTPServer((host, port), Page)
    threading.Thread(target=httpd.serve_forever, daemon=True, name="hansa-web").start()
    return httpd
