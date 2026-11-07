"""Phone push definitions; credentials stay in private addresses or extra secrets."""
from __future__ import annotations

import re
import unicodedata
from urllib.parse import parse_qs, unquote, urlencode, urlsplit


def _url(value, service):
    # Inspect before urlsplit can remove controls. Keep HTTPS and origin checks
    # while these addresses carry credentials, including on self-hosted servers.
    problem = f"{service} needs an HTTPS address without userinfo, fragments, spaces or controls"
    if (not value or any(c.isspace() or unicodedata.category(c) == "Cc" for c in value)
            or any(c in value for c in "#\\")):
        raise ValueError(problem)
    try:
        parsed = urlsplit(value)
        parsed.port
    except (ValueError, UnicodeError):
        raise ValueError(problem) from None
    if parsed.scheme != "https":
        detail = "; its default plain HTTP on port 8800 is refused" if service == "PushDeer" else ""
        raise ValueError(f"{service} needs HTTPS, including self-hosted servers{detail}")
    if not parsed.hostname or "@" in parsed.netloc:
        raise ValueError(problem)
    return parsed


def _origin(parsed):
    return "https://" + parsed.netloc.lower()


def _key(value, label):
    if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ValueError(f"paste the {label}")
    return value


def _query_key(parsed, field):
    query = parse_qs(parsed.query, keep_blank_values=True, errors="strict")
    if set(query) - {field} or len(query.get(field, [])) > 1:
        raise ValueError("paste an address with just its token or key")
    return query.get(field, [""])[0]


def _cut(text, max_bytes, render=lambda value: value):
    """Bound UTF-8 at character boundaries, marking every shortened message."""
    def size(value):
        return len(render(value).encode("utf-8"))

    if size(text) <= max_bytes:
        return text
    if size("…") > max_bytes:
        raise ValueError("too_big")
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if size(text[:middle] + "…") <= max_bytes:
            low = middle
        else:
            high = middle - 1
    return text[:low] + "…"


class Ntfy:
    id = "ntfy"
    names = {"en": "ntfy", "zh": "ntfy"}
    hints = {"en": "Paste the topic address you subscribed to in the ntfy app. Put an optional access token (tk_…) in Extra secret.",
             "zh": "粘贴 ntfy 应用中订阅的主题地址。可选的访问令牌（tk_…）填在额外密钥栏。"}
    extra_secret = True
    deadline_s = 30
    max_bytes = 4096
    levels = {"passive": 2, "active": 3, "urgent": 4}

    def recognizes(self, value):
        return urlsplit(value).hostname == "ntfy.sh"

    def normalize(self, value):
        parsed = _url(value, "ntfy")
        topic = unquote(parsed.path[1:], errors="strict")
        if parsed.query or "?" in value:
            raise ValueError("paste the ntfy topic address without a query")
        _key(topic, "ntfy topic address")
        return _origin(parsed) + "/" + topic

    def validate(self, address, secret):
        if len(urlsplit(address).path[1:]) < 8 and not secret:
            raise ValueError("the ntfy topic is too short to keep secret; use at least 8 characters or give an access token")

    def secrets(self, address):
        return [address, urlsplit(address).path[1:]]

    def hidden(self, address):
        return urlsplit(address).netloc + "/…"

    def requests(self, address, secret, title, body, level):
        self.validate(address, secret)
        parsed = urlsplit(address)
        return [{"url": _origin(parsed) + "/", "json": {
            "topic": parsed.path[1:], "title": _cut(title, 1024),
            "message": _cut(body, self.max_bytes), "priority": self.levels[level]},
            "headers": {"Authorization": "Bearer " + secret} if secret else {},
            "success": {"status": 200}, "error_field": "code"}]


class Gotify:
    id = "gotify"
    names = {"en": "Gotify", "zh": "Gotify"}
    hints = {"en": "Paste Gotify's /message?token=… address or server address. Create the application token under Apps on Gotify's web page; put it in Extra secret if absent from the address.",
             "zh": "粘贴 Gotify 的 /message?token=… 地址或服务器地址。应用令牌在 Gotify 网页的 Apps 中创建，地址中没有时填在额外密钥栏。"}
    extra_secret = True
    deadline_s = 30
    max_bytes = 4096
    levels = {"passive": 2, "active": 5, "urgent": 8}

    def recognizes(self, value):
        return False

    def normalize(self, value):
        parsed = _url(value, "Gotify")
        if parsed.path not in ("", "/", "/message"):
            raise ValueError("paste the Gotify server address or its /message?token=… address")
        token = _query_key(parsed, "token")
        if token:
            _key(token, "Gotify application token")
        return _origin(parsed) + "/message" + ("?" + urlencode({"token": token}) if token else "")

    def validate(self, address, secret):
        if not secret and not _query_key(urlsplit(address), "token"):
            raise ValueError("Gotify needs the application token in the address or Extra secret")

    def secrets(self, address):
        return [address, _query_key(urlsplit(address), "token")]

    def hidden(self, address):
        return urlsplit(address).netloc + "/message"

    def requests(self, address, secret, title, body, level):
        self.validate(address, secret)
        parsed = urlsplit(address)
        return [{"url": _origin(parsed) + "/message", "headers": {
            "X-Gotify-Key": secret or _query_key(parsed, "token")}, "json": {
            "title": title, "message": _cut(body, self.max_bytes), "priority": self.levels[level]},
            "success": {"status": 200}, "error_field": "errorCode"}]


class PushDeer:
    id = "pushdeer"
    names = {"en": "PushDeer", "zh": "PushDeer"}
    hints = {"en": "Paste the key from the PushDeer app's Key tab, or a server's /message/push?pushkey=… address.",
             "zh": "粘贴 PushDeer 应用 Key 页的密钥，或服务器的 /message/push?pushkey=… 地址。"}
    extra_secret = False
    deadline_s = 30
    max_bytes = 3500
    levels = {}

    def recognizes(self, value):
        return urlsplit(value).hostname == "api2.pushdeer.com"

    def normalize(self, value):
        if re.fullmatch(r"[A-Za-z0-9_-]+", value):
            value = "https://api2.pushdeer.com/message/push?" + urlencode({"pushkey": value})
        parsed = _url(value, "PushDeer")
        if parsed.path != "/message/push":
            raise ValueError("paste the PushDeer key or /message/push?pushkey=… address")
        key = _key(_query_key(parsed, "pushkey"), "PushDeer key")
        return _origin(parsed) + "/message/push?" + urlencode({"pushkey": key})

    def secrets(self, address):
        return [address, _query_key(urlsplit(address), "pushkey")]

    def hidden(self, address):
        return urlsplit(address).netloc + "/message/push?pushkey=…"

    def requests(self, address, secret, title, body, level):
        parsed = urlsplit(address)
        return [{"url": _origin(parsed) + "/message/push", "form": {
            "pushkey": _query_key(parsed, "pushkey"), "type": "text",
            "text": _cut(title + "\n" + body, self.max_bytes)},
            "success": {"status": 200, "field": "code", "equals": 0}, "error_field": "code"}]


class ServerChan:
    id = "serverchan"
    names = {"en": "Server酱 (WeChat)", "zh": "Server酱（微信）"}
    hints = {"en": "Paste the SendKey from Server酱's SendKey page, or its .send address. Free Turbo accounts allow 5 pushes a day.",
             "zh": "粘贴 Server酱 的 SendKey 页上的 SendKey，或其 .send 地址。Turbo 免费账户每天限 5 次推送。"}
    extra_secret = False
    deadline_s = 30
    max_bytes = 3500
    levels = {}

    def recognizes(self, value):
        return (value.startswith("SCT") or bool(re.match(r"sctp\d+t", value))
                or urlsplit(value).hostname == "sctapi.ftqq.com"
                or bool(re.fullmatch(r"\d+\.push\.ft07\.com", urlsplit(value).hostname or "")))

    def normalize(self, value):
        if value.startswith("https://"):
            parsed = _url(value, "Server酱")
            match = re.fullmatch(r"/(?:send/)?([A-Za-z0-9_-]+)\.send", parsed.path)
            if not match or parsed.query or "?" in value:
                raise ValueError("paste the Server酱 SendKey or its .send address")
            key = match[1]
        else:
            key = value
            parsed = None
        _key(key, "Server酱 SendKey")
        third = re.fullmatch(r"sctp(\d+)t[A-Za-z0-9_-]+", key)
        if third:
            address = f"https://{third[1]}.push.ft07.com/send/{key}.send"
        elif re.fullmatch(r"SCT[A-Za-z0-9_-]+", key):
            address = f"https://sctapi.ftqq.com/{key}.send"
        else:
            raise ValueError("Server酱 needs an SCT… or sctp<uid>t… SendKey")
        if parsed and value != address:
            raise ValueError("the Server酱 address must match the SendKey's server")
        return address

    def secrets(self, address):
        return [address, urlsplit(address).path.rsplit("/", 1)[1].removesuffix(".send")]

    def hidden(self, address):
        parsed = urlsplit(address)
        return parsed.netloc + ("/send/…" if parsed.path.startswith("/send/") else "/…")

    def requests(self, address, secret, title, body, level):
        # Four-space code blocks make alert lines literal Markdown. Separate
        # blocks at blank lines only through indentation, preserving their text.
        def markdown(value):
            return "\n".join("    " + line for line in value.split("\n"))

        body = _cut(body.replace("\r\n", "\n").replace("\r", "\n"), self.max_bytes, markdown)
        return [{"url": address, "json": {"title": " ".join(title.splitlines())[:32],
                                           "desp": markdown(body)},
                 "success": {"status": 200, "field": "code", "equals": 0}, "error_field": "code"}]
