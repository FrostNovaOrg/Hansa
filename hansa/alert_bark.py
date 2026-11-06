"""Bark channel groundwork. The page saves its key and tests it; automatic alert delivery does not call it yet.

send takes a gathered, code-free queue batch, before alert._with_codes. There is no
public key reader: consumers get only has_key; sending and Desk's redaction read it.
"""
from __future__ import annotations

import json
import os
import unicodedata
from pathlib import Path
from urllib.parse import urlsplit

from . import alert, alert_http
from .desk import Desk, key_problem, now, read_json, write_json, write_text

DEFAULT_SERVER = "https://api.day.app"
MAX_BYTES = 3500
LEVELS = {"passive": "passive", "active": "active", "urgent": "timeSensitive"}


def problems(config) -> list[str]:
    """Refuse destinations that could disclose a key or change Bark's /push path.

    Keep these checks while the principal can supply a server address. Check the
    original spelling too: urlsplit silently removes some control characters.
    """
    if (not isinstance(config, dict) or set(config) - {"on", "server"}
            or type(config.get("on", False)) is not bool):
        return ["alert_bark must hold on (a boolean) and server (an HTTPS server address), with no key"]
    server = config.get("server", DEFAULT_SERVER)
    try:
        if (not isinstance(server, str) or any(c.isspace() or unicodedata.category(c) == "Cc" for c in server)
                or "?" in server or "#" in server or "\\" in server):
            raise ValueError
        parsed = urlsplit(server)
        if (parsed.scheme != "https" or not parsed.hostname or "@" in parsed.netloc
                or parsed.path not in ("", "/")):
            raise ValueError
        parsed.port
    except (ValueError, TypeError):
        return ["alert_bark.server must be HTTPS, without userinfo, a query, a fragment or a path except /"]
    return []


def _key_file(desk: Desk) -> Path:
    # Not <provider>.key: a provider with a similar name cannot replace this key.
    return desk.secrets_dir / "alert-bark.device-key"


def has_key(desk: Desk) -> bool:
    """Whether a key is set, without reading or returning it."""
    path = _key_file(desk)
    return path.is_file() and path.stat().st_size > 0


def save_key(desk: Desk, value: str, *, by: str = "") -> None:
    """Create or replace the principal's key atomically; an empty value removes it.

    Like settings, only by='page' is the principal's save. History records just
    that it changed, including replacements, without reading the previous key.
    """
    if by != "page":
        raise ValueError("the Bark key is the principal's, set only on the web page (Settings); ask with hansa ask")
    if not isinstance(value, str):
        raise ValueError("paste only the Bark device key")
    value = value.strip()
    if value and (problem := key_problem(value)):
        raise ValueError(problem)
    from .history import record
    with desk.lock():
        path = _key_file(desk)
        if value:
            desk.secrets_dir.mkdir(mode=0o700, exist_ok=True)
            os.chmod(desk.secrets_dir, 0o700)
            write_text(path, value + "\n", mode=0o600, follow_symlinks=False)
        else:
            if not path.exists():
                return
            path.unlink()
        record(desk, {}, {"alert_bark": {"key": "changed"}}, by)
        desk.event("key_set" if value else "key_removed", channel="bark", by=by)


def remove_key(desk: Desk, *, by: str = "") -> None:
    save_key(desk, "", by=by)


def last_result(desk: Desk) -> dict | None:
    """The last attempt's time, ok and why; no destination or request data."""
    return read_json(desk.state_dir / "alert-bark-last.json", None)


def _record(desk: Desk, why: str) -> str:
    with desk.lock():
        write_json(desk.state_dir / "alert-bark-last.json", {"at": now(), "ok": why == "sent", "why": why})
        desk.event("alert_channel", channel="bark", ok=why == "sent", why=why)
    return why


def _text(desk: Desk, items: list[dict], held: int) -> tuple[str, str]:
    lang = "zh" if alert.chinese(desk) else "en"
    safe = []
    codes = [x["code"] for x in items if x.get("code")]
    for item in items:
        translations = item.get("translations") or {}
        text = translations.get(lang, item.get("text", ""))
        if item["kind"] == "release_waiting" and item.get("code") and lang not in translations:
            # A caller passed the command's in-memory batch. Never reuse its
            # code-bearing wording, even for an older queue without translations.
            text = "发布等你在网页上点头" if lang == "zh" else "the release waits for your yes on the page"
        for code in codes:
            text = text.replace(code, "")
        safe.append({**item, "text": text, "code": ""})
    # Reuse the command's language, grouping and character cap, without its codes.
    title = desk.redact(alert.desk_name(desk))
    header = f"[hansa] {'工作台' if lang == 'zh' else 'desk'} {title}"
    body = alert.text_of(desk, safe, held).removeprefix(header).removeprefix(": ").lstrip()
    return title, body


def _fit(payload: dict) -> bool:
    """Match alert_http's actual JSON encoding, including escapes and field overhead.

    Keep the full-payload bound while APNs/Bark add their own envelope. If fixed
    fields alone cannot fit, refuse the send rather than cutting a title or key.
    """
    def size(body):
        return len(json.dumps({**payload, "body": body}, ensure_ascii=False, allow_nan=False).encode("utf-8"))

    original = payload["body"]
    if size(original) <= MAX_BYTES:
        return True
    if size("…") > MAX_BYTES:
        return False
    low, high = 0, len(original)
    while low < high:
        middle = (low + high + 1) // 2
        if size(original[:middle] + "…") <= MAX_BYTES:
            low = middle
        else:
            high = middle - 1
    payload["body"] = original[:low] + "…"
    return True


def _send(desk: Desk, items: list[dict], held: int, *, testing: bool = False) -> str:
    config = desk.settings().get("alert_bark", {})
    if problems(config):
        return _record(desk, "refused_url")
    if not testing and not config.get("on", False):
        return _record(desk, "off")
    if not has_key(desk):
        return _record(desk, "no_key")
    if not items:
        return _record(desk, "empty")
    title, body = _text(desk, items, held)
    payload = {"device_key": _key_file(desk).read_text(encoding="utf-8").strip(), "title": title, "body": body,
               "group": title, "level": LEVELS[max((x.get("level", alert.level_of(x["kind"])) for x in items),
                                                  key=alert.LEVELS.index)]}
    if not _fit(payload):
        return _record(desk, "too_big")
    [result] = alert_http.send([{"url": config.get("server", DEFAULT_SERVER).rstrip("/") + "/push",
                                 "json": payload}], deadline_s=30)
    return _record(desk, result["why"])


def send(desk: Desk, items: list[dict], held: int = 0) -> str:
    """Send one gathered code-free batch when on; return only the result category."""
    return _send(desk, items, held)


def test(desk: Desk) -> str:
    """Test the saved key/server now, even while off; leave the alert queue alone."""
    return _send(desk, [{"kind": "test", "level": "active", "text": "a test alert from hansa",
                         "translations": {"en": "a test alert from hansa", "zh": "来自 hansa 的测试提醒"}}], 0,
                 testing=True)
