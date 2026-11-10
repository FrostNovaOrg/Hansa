"""Bark service definition and compatibility for the page's original Bark block."""
from __future__ import annotations

import base64
import json
import re
import secrets
import shlex
import string
import unicodedata
from urllib.parse import urlsplit, unquote

DEFAULT_SERVER = "https://api.day.app"
MAX_BYTES = 3500
LEVELS = {"passive": "passive", "active": "active", "urgent": "timeSensitive"}
ENCRYPTION_PROBLEM = "choose AES-128, AES-192 or AES-256 with CBC in the Bark app; use a 16, 24 or 32 character ASCII key"
DEVICE_PROBLEM = "this example is for another Bark device"


def _key_valid(key):
    return isinstance(key, str) and len(key) in (16, 24, 32) and all(32 <= ord(c) < 127 for c in key)


def _encryption_key(address, value):
    """Read a literal key or Bark's copied shell example, without executing it.

    Require the example's cipher and device to match so a paste cannot silently
    configure a phone which will fail to decrypt. Keep this while the app's
    example contains settings hansa does not support (ECB/GCM or a fixed IV).
    """
    if value == "" or _key_valid(value):
        return value
    if not isinstance(value, str):
        raise ValueError(ENCRYPTION_PROBLEM)
    assignments = {}
    for line in value.splitlines():
        if re.match(r"\s*(?:export\s+)?(?:key|device_key|deviceKey|server)\s*=", line):
            try:
                tokens = shlex.split(line, comments=True)
            except ValueError:
                raise ValueError(ENCRYPTION_PROBLEM) from None
            if tokens[:1] == ["export"]:
                tokens = tokens[1:]
            if len(tokens) != 1 or "=" not in tokens[0]:
                raise ValueError(ENCRYPTION_PROBLEM)
            name, literal = tokens[0].split("=", 1)
            if name in assignments:
                raise ValueError(ENCRYPTION_PROBLEM)
            assignments[name] = literal
    ciphers = re.findall(r"\bopenssl\s+enc\s+-aes-(128|192|256)-(cbc|ecb|gcm)\b", value)
    key = assignments.get("key", "")
    if len(ciphers) != 1 or ciphers[0][1] != "cbc" or not _key_valid(key) or len(key) * 8 != int(ciphers[0][0]):
        raise ValueError(ENCRYPTION_PROBLEM)
    devices = re.findall(r"https://[^\s'\"<>`]+", value)
    device_keys = []
    for url in devices:
        try:
            parts = urlsplit(url).path.split("/")
            device = unquote(parts[1]) if len(parts) > 1 else ""
        except ValueError:
            raise ValueError(ENCRYPTION_PROBLEM) from None
        if device:
            device_keys.append(device)
    device_keys.extend(assignments[name] for name in ("device_key", "deviceKey") if name in assignments)
    if not device_keys:
        raise ValueError(ENCRYPTION_PROBLEM)
    if any(device != urlsplit(address).path[1:] for device in device_keys):
        raise ValueError(DEVICE_PROBLEM)
    return key


def _pasted_address(value):
    # Inspect the whole paste before extracting a URL or letting urlsplit strip
    # controls. Keep this while pasted text can contain device credentials.
    if any(unicodedata.category(c) == "Cc" and c not in "\t\r\n" for c in value):
        raise ValueError("Bark needs pasted text without control characters")
    addresses = re.findall(r"https://[^\s'\"<>`]+", value)
    if len(addresses) > 1:
        raise ValueError("paste exactly one HTTPS address for Bark")
    return addresses[0] if addresses else value


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
    extra_secret = True
    secret_labels = {"en": "Encryption key (optional)", "zh": "加密密钥（选填）"}
    secret_hints = {"en": "In the Bark app's encryption settings choose AES-128, AES-192 or AES-256 with mode CBC. "
                          "Paste the same 16, 24 or 32 character ASCII key typed there, or its copied example, here.",
                    "zh": "在 Bark 应用的加密设置中选择 AES-128、AES-192 或 AES-256，模式选 CBC。"
                          "在这里粘贴那里输入的同一个 16、24 或 32 个 ASCII 字符的密钥，或复制的示例。"}
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
        if not value or any(c.isspace() for c in value) or "\\" in value:
            raise ValueError("Bark needs an HTTPS address without spaces, controls, userinfo or backslashes")
        if re.fullmatch(r"[A-Za-z0-9_-]+", value):
            value = DEFAULT_SERVER + "/" + value
        try:
            parsed = urlsplit(value)
            parsed.port
            key = unquote(parsed.path.split("/")[1], errors="strict")
            # Dot segments can change which path contains the key; reject
            # them while normalizing pasted example paths to credentials.
            if (parsed.scheme != "https" or not parsed.hostname or "@" in parsed.netloc
                    or any(part in (".", "..") for part in unquote(parsed.path, errors="strict").split("/"))
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

    def normalize_secret(self, address, value):
        return _encryption_key(address, value)

    def validate(self, address, secret):
        if secret and not _key_valid(secret):
            raise ValueError(ENCRYPTION_PROBLEM)

    def requests(self, address, secret, title, body, level, *, texts=None):
        parsed = urlsplit(address)
        payload = {"device_key": parsed.path[1:], "title": title, "body": body,
                   "group": title, "level": self.levels[level]}
        if secret:
            self.validate(address, secret)
            from .aes import cbc_encrypt
            device_key = payload.pop("device_key")
            # The app has no IV setting; it uses the fresh ASCII IV passed here.
            iv = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(16))
            envelope = {"device_key": device_key, "ciphertext": "", "iv": iv}

            def encrypted_size(body):
                length = len(_json({**payload, "body": body}))
                ciphertext_length = 4 * ((16 * (length // 16 + 1) + 2) // 3)
                return len(_json(envelope)) + ciphertext_length

            if not _fit(payload, encrypted_size):
                raise ValueError("too_big")
            if texts is not None:
                texts.append(payload["body"])  # final plaintext, after the byte bound, in memory only
            envelope["ciphertext"] = base64.b64encode(cbc_encrypt(
                secret.encode("ascii"), iv.encode("ascii"), _json(payload))).decode("ascii")
            payload = envelope
        elif not _fit(payload):
            raise ValueError("too_big")
        return [{"url": "https://" + parsed.netloc + "/push", "json": payload}]


def _json(payload):
    return json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")


def _fit(payload: dict, size=None) -> bool:
    """Match alert_http's actual JSON encoding, including escapes and field overhead.

    Keep the full-payload bound while APNs/Bark add their own envelope. If fixed
    fields alone cannot fit, refuse the send rather than cutting a title or key.
    """
    if size is None:
        def size(body):
            return len(_json({**payload, "body": body}))

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
