"""The principal's web page: status, settings (including keys) and cost.

It only reads and writes the desk's files and never calls a model. It listens on
127.0.0.1; open it through an SSH tunnel with the address `hansa web` prints.
The page keeps the address's token in sessionStorage for its origin and tab,
and sends it explicitly in Authorization. Changes also need X-Hansa: 1.
The HTML shell is public; every API request requires the bearer token.
"""
from __future__ import annotations

import hmac
import http.server
import json
import os
import re
import threading
import urllib.parse
from pathlib import Path

from . import alert, cost, disk, release, views
from .desk import Desk, KeyScrubError, event_record, lane_warnings, public_settings, read_json
from .run import check_provider

UI = Path(__file__).with_name("ui.html")


def _last_lines(path, n: int, block: int = 65536) -> list:
    """The last n lines of a file, as read_text().splitlines()[-n:] gives them, read from its end: the log grows
    with the desk, and the page asks for its latest events every few seconds."""
    with open(path, "rb") as fh:
        end = at = fh.seek(0, os.SEEK_END)
        data = b""
        while at > 0:
            at = max(0, at - block)
            fh.seek(at)
            data = fh.read(end - at)
            # a line before the last n, whole or not, so that each of those n starts where it does in the file
            if at and len(data.decode("utf-8", errors="replace").splitlines()) > n:
                break
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
NOT_FROM_PAGE = ("lanes", "concurrency", "disk_floor", "memory_floor")


def _save_settings(desk: Desk, body: dict) -> dict:
    new = body.get("settings")
    with desk.lock():  # the same lock save_settings takes, so no lane change lands in between
        if isinstance(new, dict):
            on_disk = desk.settings()
            new = {**new, **{k: on_disk[k] for k in NOT_FROM_PAGE if k in on_disk}}
        # a stale page first, then the lists (desk.page_list_problems), then the rest
        problems = desk.save_settings(new, by="page", rev=body.get("rev"))
    if not problems:
        for name in desk.settings()["lanes"]:
            brief = desk.lanes_dir / name / "brief.md"
            brief.parent.mkdir(parents=True, exist_ok=True)
            brief.touch(exist_ok=True)
    warnings = [] if problems else lane_warnings(new)  # the coordinator's to fix: the page saved all the same
    return {"ok": not problems, "problems": problems, **({"lane_warnings": warnings} if warnings else {})}


def _top_up_provider(desk: Desk, name: str) -> dict:
    """Only the authenticated principal's page calls this; no coordinator command clears credit rests."""
    with desk.lock():
        if name not in desk.settings()["providers"]:
            return {"ok": False, "problems": ["no such provider"]}
        if (desk.provider_state().get(name) or {}).get("credit_since"):
            desk.set_provider_state(name, credit_since=0, cool_until=0, reason="", rest_how="", rest_minutes=None)
            desk.event("provider_topped_up", provider=name, by="page")
    desk.ready_news()
    return {"ok": True}


def start_web(desk: Desk, host: str = "127.0.0.1", port: int = 8765):
    token = desk.web_token()

    class Page(http.server.BaseHTTPRequestHandler):
        server_version = "hansa"

        def log_message(self, *args):
            pass

        def _authed(self) -> bool:
            return hmac.compare_digest(self.headers.get("Authorization", "").encode(),
                                       ("Bearer " + token).encode())

        def _send(self, code: int, body, ctype: str = "application/json; charset=utf-8"):
            data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(data)

        def _bad_request(self, code: str, field: str, problem: str):
            return self._send(400, {"ok": False, "code": code, "field": field, "problems": [problem]})

        def do_GET(self):
            url = urllib.parse.urlparse(self.path)
            if url.path == "/":
                return self._send(200, UI.read_bytes(), "text/html; charset=utf-8")
            if not self._authed():
                return self._send(403, b"Open the address that `hansa web` prints.", "text/plain; charset=utf-8")
            q = urllib.parse.parse_qs(url.query, keep_blank_values=True)
            if url.path == "/api/status":
                return self._send(200, views.status(desk))
            if url.path == "/api/timeline":
                return self._send(200, views.timeline(desk))
            if url.path == "/api/settings":
                # with the free space of the desk's volume, shown beside the floor (hansa/disk.py)
                return self._send(200, {**public_settings(desk), "disk_free": disk.desk_free(desk)[0]})
            if url.path == "/api/cost":
                try:
                    days = max(1, min(90, int(q.get("days", ["7"])[0])))
                except ValueError:
                    return self._bad_request("invalid_query", "days", "days must be a whole number")
                rep = cost.report(desk, days)
                if q.get("rows", ["0"])[0] != "1":
                    rep.pop("rows")
                return self._send(200, rep)
            if url.path == "/api/events":
                try:
                    n = max(1, min(200, int(q.get("n", ["60"])[0])))
                except ValueError:  # not a number: the default
                    n = 60
                return self._send(200, _events(desk, n, q.get("event", [""])[0]))
            if url.path == "/api/task":
                tid = q.get("id", [""])[0]
                if not re.fullmatch(r"t\d+", tid) or not (desk.tasks_dir / tid / "meta.json").is_file():
                    return self._send(404, {"error": "no such task"})
                return self._send(200, {"meta": read_json(desk.tasks_dir / tid / "meta.json"),
                                        "text": views.show_text(desk, tid, full=True)})
            return self._send(404, {"error": "not found"})

        def do_POST(self):
            if not self._authed() or self.headers.get("X-Hansa") != "1":
                return self._send(403, {"error": "forbidden"})
            try:
                data = self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}"
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
                if path == "/api/ask/close":
                    refused = desk.close_ask(int(body["id"]), body.get("answer", ""), by="page")
                    return self._send(200, {"ok": False, "problems": [refused]} if refused else {"ok": True})
                return self._send(404, {"error": "not found"})
            except KeyScrubError as e:
                return self._send(400, {"ok": False, "saved": True, "problems": [str(e)]})
            except (KeyError, ValueError, TypeError) as e:
                return self._send(400, {"ok": False, "problems": [str(e)]})

    httpd = http.server.ThreadingHTTPServer((host, port), Page)
    threading.Thread(target=httpd.serve_forever, daemon=True, name="hansa-web").start()
    return httpd
