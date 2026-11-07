"""Bark service definition and compatibility for the page's original Bark block."""
from __future__ import annotations

import json
import re
import unicodedata
from urllib.parse import urlsplit, unquote

DEFAULT_SERVER = "https://api.day.app"
MAX_BYTES = 3500
LEVELS = {"passive": "passive", "active": "active", "urgent": "timeSensitive"}


def _pasted_address(value):
    if value.startswith("https://"):
        return value
    addresses = re.findall(r"https://[^\s'\"<>`]+", value)
    return addresses[0] if len(addresses) == 1 else value


def problems(config) -> list[str]:
    if (not isinstance(config, dict) or set(config) - {"on", "server"}
            or type(config.get("on", False)) is not bool):
        return ["alert_bark must hold on and server, with no key"]
    try:
        Bark().normalize(config.get("server", DEFAULT_SERVER).rstrip("/") + "/fixture-key")
        if urlsplit(config.get("server", DEFAULT_SERVER)).path not in ("", "/"):
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        return ["alert_bark.server must be HTTPS, without userinfo, query, fragment or path except /"]
    return []


class Bark:
    id = "bark"
    names = {"en": "Bark", "zh": "Bark"}
    hints = {"en": "Paste the whole address on the Bark app's home screen.",
             "zh": "粘贴 Bark 应用首页显示的完整地址。"}
    extra_secret = False
    deadline_s = 30
    max_bytes = MAX_BYTES
    levels = LEVELS

    def recognizes(self, value):
        value = _pasted_address(value)
        serverchan = value.startswith("SCT") or re.match(r"sctp\d+t", value)
        return bool(not serverchan and re.fullmatch(r"[A-Za-z0-9_-]+", value)
                    or urlsplit(value).hostname == "api.day.app")

    def normalize(self, value):
        value = _pasted_address(value)
        # Check original spelling before urlsplit strips controls. Keep this rule
        # while addresses can contain credentials: normalization must not reroute them.
        if (not value or any(c.isspace() or unicodedata.category(c) == "Cc" for c in value)
                or any(c in value for c in "#\\")):
            raise ValueError("Bark needs an HTTPS address without spaces, controls, userinfo or fragment")
        if re.fullmatch(r"[A-Za-z0-9_-]+", value):
            value = DEFAULT_SERVER + "/" + value
        try:
            parsed = urlsplit(value)
            parsed.port
            key = unquote(parsed.path.split("/")[1], errors="strict")
            if (parsed.scheme != "https" or not parsed.hostname or "@" in parsed.netloc
                    or not re.fullmatch(r"[A-Za-z0-9_-]+", key)):
                raise ValueError
        except (ValueError, IndexError, UnicodeError):
            raise ValueError("Bark needs an HTTPS server address followed by its device key") from None
        return "https://" + parsed.netloc.lower() + "/" + key

    def secrets(self, address):
        return [address, urlsplit(address).path[1:]]

    def hidden(self, address):
        parsed = urlsplit(address)
        key = parsed.path[1:]
        return parsed.netloc + "/…" + (key[-2:] if len(key) > 2 else "")

    def requests(self, address, secret, title, body, level):
        parsed = urlsplit(address)
        payload = {"device_key": parsed.path[1:], "title": title, "body": body,
                   "group": title, "level": self.levels[level]}
        if not _fit(payload):
            raise ValueError("too_big")
        return [{"url": "https://" + parsed.netloc + "/push", "json": payload}]


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



def first(desk):
    from . import alert_push
    return next((e for e in alert_push.entries(desk.settings()) if e["service"] == "bark"), None)


def projection(desk):
    from . import alert_push
    entry = first(desk)
    address = alert_push._read(desk, entry["id"]) if entry else ""
    parsed = urlsplit(address)
    return {"on": bool(entry and entry["on"]),
            "server": "https://" + parsed.netloc if address else DEFAULT_SERVER}


def has_key(desk):
    from . import alert_push
    entry = first(desk)
    return bool(entry and alert_push.has_address(desk, entry))


def save_key(desk, value, *, by=""):
    from . import alert_push
    if by != "page":
        raise ValueError("the Bark key is the principal's, set only on the web page (Settings)")
    if not isinstance(value, str):
        raise ValueError("paste the Bark device key")
    if value and (len(value) < 8 or not re.fullmatch(r"[A-Za-z0-9_-]+", value)):
        raise ValueError("paste only the Bark device key (at least 8 characters)")
    entry = first(desk)
    change = {"id": entry["id"]} if entry else {"service": "bark", "on": False}
    change.update({"address": projection(desk)["server"] + "/" + value} if value else {"remove": True})
    if not entry and not value:
        return
    alert_push.change(desk, [change], by=by)


def remove_key(desk, *, by=""):
    save_key(desk, "", by=by)


def last_result(desk):
    from . import alert_push
    entry = first(desk)
    return alert_push.last_result(desk, entry["id"]) if entry else None


def send(desk, items, held=0):
    from . import alert_push
    entry = first(desk)
    return alert_push.send(desk, entry, items, held) if entry else "no_key"


def test(desk):
    from . import alert_push
    entry = first(desk)
    return alert_push.test(desk, entry["id"]) if entry else "no_key"
