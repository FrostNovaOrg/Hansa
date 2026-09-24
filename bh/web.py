"""The principal's web page: status, settings (including keys) and cost.

It only reads and writes the desk's files and never calls a model. It listens on
127.0.0.1; open it through an SSH tunnel with the address `bh web` prints. The
token in that address becomes a cookie; changes also need a header that only
this page sends, so another site cannot submit them.
"""
from __future__ import annotations

import http.cookies
import http.server
import json
import threading
import urllib.parse
from pathlib import Path

from . import cost, views
from .desk import Desk, public_settings, read_json
from .run import check_provider

UI = Path(__file__).with_name("ui.html")
IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}


def _shots(desk, tid: str) -> list:
    """Pictures in a task's browser folder, newest first (names only; never a path)."""
    if not tid.startswith("t") or not tid[1:].isdigit():
        return []
    d = desk.tasks_dir / tid / "browser"
    files = [f for f in d.iterdir() if f.is_file() and f.suffix.lower() in IMAGE_TYPES] if d.is_dir() else []
    return [f.name for f in sorted(files, key=lambda f: f.stat().st_mtime, reverse=True)]


def _events(desk: Desk, n: int = 60) -> list:
    if not desk.events_path.exists():
        return []
    lines = desk.events_path.read_text(encoding="utf-8").splitlines()[-n:]
    out = []
    for line in reversed(lines):
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return out


def _save_settings(desk: Desk, body: dict) -> dict:
    problems = desk.save_settings(body.get("settings"), by="page")
    if not problems:
        for name in desk.settings()["lanes"]:
            brief = desk.lanes_dir / name / "brief.md"
            brief.parent.mkdir(parents=True, exist_ok=True)
            brief.touch(exist_ok=True)
    return {"ok": not problems, "problems": problems}


def start_web(desk: Desk, host: str = "127.0.0.1", port: int = 8765):
    token = desk.web_token()

    class Page(http.server.BaseHTTPRequestHandler):
        server_version = "bh"

        def log_message(self, *args):
            pass

        def _authed(self) -> bool:
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if q.get("t", [""])[0] == token:
                return True
            c = http.cookies.SimpleCookie(self.headers.get("Cookie", ""))
            return bool(c.get("bh_t")) and c["bh_t"].value == token

        def _send(self, code: int, body, ctype: str = "application/json; charset=utf-8", cookie: bool = False):
            data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            if cookie:
                self.send_header("Set-Cookie", f"bh_t={token}; HttpOnly; SameSite=Strict; Path=/")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if not self._authed():
                return self._send(403, b"Open the address that `bh web` prints.", "text/plain; charset=utf-8")
            url = urllib.parse.urlparse(self.path)
            q = urllib.parse.parse_qs(url.query)
            if url.path == "/":
                return self._send(200, UI.read_bytes(), "text/html; charset=utf-8", cookie=True)
            if url.path == "/api/status":
                return self._send(200, views.status(desk))
            if url.path == "/api/settings":
                return self._send(200, public_settings(desk))
            if url.path == "/api/cost":
                rep = cost.report(desk, max(1, min(90, int(q.get("days", ["7"])[0]))))
                if q.get("rows", ["0"])[0] != "1":
                    rep.pop("rows")
                return self._send(200, rep)
            if url.path == "/api/events":
                return self._send(200, _events(desk))
            if url.path == "/api/task":
                tid = q.get("id", [""])[0]
                if not (desk.tasks_dir / tid / "meta.json").is_file():
                    return self._send(404, {"error": "no such task"})
                return self._send(200, {"meta": read_json(desk.tasks_dir / tid / "meta.json"),
                                        "text": views.show_text(desk, tid), "shots": _shots(desk, tid)})
            if url.path == "/api/file":  # a picture a task's browser saved, such as a screenshot
                tid, name = q.get("task", [""])[0], q.get("name", [""])[0]
                if name not in _shots(desk, tid):
                    return self._send(404, {"error": "no such file"})
                f = desk.tasks_dir / tid / "browser" / name
                return self._send(200, f.read_bytes(), IMAGE_TYPES[f.suffix.lower()])
            return self._send(404, {"error": "not found"})

        def do_POST(self):
            if not self._authed() or self.headers.get("X-BH") != "1":
                return self._send(403, {"error": "forbidden"})
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                path = urllib.parse.urlparse(self.path).path
                if path == "/api/settings":
                    return self._send(200, _save_settings(desk, body))
                if path == "/api/key":
                    desk.write_key(body["provider"], body.get("key", ""), by="page")
                    return self._send(200, {"ok": True})
                if path == "/api/pause":
                    desk.pause(body.get("reason") or "paused on the page", by="page")
                    return self._send(200, {"ok": True})
                if path == "/api/resume":
                    desk.resume(by="page")
                    return self._send(200, {"ok": True})
                if path == "/api/check":
                    return self._send(200, check_provider(desk, body["provider"]))
                if path == "/api/ask/close":
                    desk.close_ask(int(body["id"]), body.get("answer", ""), by="page")
                    return self._send(200, {"ok": True})
                return self._send(404, {"error": "not found"})
            except (KeyError, ValueError, TypeError) as e:
                return self._send(400, {"ok": False, "problems": [str(e)]})

    httpd = http.server.ThreadingHTTPServer((host, port), Page)
    threading.Thread(target=httpd.serve_forever, daemon=True, name="bh-web").start()
    return httpd
