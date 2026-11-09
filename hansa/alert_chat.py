"""Chat bots and a chosen JSON webhook; credentials stay in private requests."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time
from urllib.parse import quote_plus, urlsplit

from . import __version__
from .alert_phone import _cut, _key, _query_key, _url


def _characters(text, limit):
    return text if len(text) <= limit else text[:limit - 1] + "…"


class _Chat:
    extra_secret = False
    deadline_s = 30
    levels = {}
    query_field = None

    def recognizes(self, value):
        parsed = urlsplit(value)
        return parsed.hostname in self.hosts and bool(re.fullmatch(self.path, parsed.path))

    def normalize(self, value):
        parsed = _url(value, self.names["en"])
        # Keep exact official origins and endpoint shapes while these URLs
        # carry credentials; do not guess another endpoint or follow redirects.
        if parsed.hostname not in self.hosts or not re.fullmatch(self.path, parsed.path):
            raise ValueError("paste the service's complete bot address")
        if self.query_field:
            _key(_query_key(parsed, self.query_field), self.query_field)
        elif parsed.query or "?" in value:
            raise ValueError("paste the bot address without a query")
        return value


class WeCom(_Chat):
    id = "wecom"
    names = {"en": "WeCom group bot", "zh": "企业微信群机器人"}
    hints = {"en": "Paste the group robot's whole webhook address from WeCom.",
             "zh": "粘贴企业微信群机器人的完整 Webhook 地址。"}
    hosts = ("qyapi.weixin.qq.com",)
    path = r"/cgi-bin/webhook/send"
    query_field = "key"
    max_bytes = 2048

    def secrets(self, address):
        return [address, _query_key(urlsplit(address), self.query_field)]

    def hidden(self, address):
        parsed = urlsplit(address)
        return parsed.netloc + parsed.path + "?" + self.query_field + "=…"

    def requests(self, address, secret, title, body, level):
        return [{"url": address, "json": {"msgtype": "text", "text": {
            "content": _cut(title + "\n" + body, self.max_bytes)}},
            "success": {"field": "errcode", "equals": 0}}]


class DingTalk(WeCom):
    id = "dingtalk"
    names = {"en": "DingTalk custom robot", "zh": "钉钉自定义机器人"}
    hints = {"en": "Paste the robot's webhook address and use the desk's name as its security keyword. Put the optional signing secret (SEC…) in Extra secret.",
             "zh": "粘贴机器人 Webhook 地址，将工作台名称设为安全关键词。可选的加签密钥（SEC…）填在额外密钥栏。"}
    hosts = ("oapi.dingtalk.com", "oapi.dingtalk.io")
    path = r"/robot/send"
    query_field = "access_token"
    extra_secret = True
    max_bytes = 3500

    def requests(self, address, secret, title, body, level):
        [request] = super().requests(address, secret, title, body, level)
        if secret:
            ts = str(int(time.time() * 1000))
            digest = hmac.new(secret.encode("utf-8"), (ts + "\n" + secret).encode("utf-8"), hashlib.sha256).digest()
            sign = quote_plus(base64.b64encode(digest).decode("ascii"))
            request["url"] += "&timestamp=" + ts + "&sign=" + sign
        return [request]


class Feishu(_Chat):
    id = "feishu"
    names = {"en": "Feishu / Lark custom bot", "zh": "飞书 / Lark 自定义机器人"}
    hints = {"en": "Paste the bot's Feishu or Lark webhook address and use the desk's name as its security keyword. Put the optional signature check secret in Extra secret.",
             "zh": "粘贴飞书或 Lark 机器人的 Webhook 地址，将工作台名称设为安全关键词。可选的签名校验密钥填在额外密钥栏。"}
    hosts = ("open.feishu.cn", "open.larksuite.com")
    path = r"/open-apis/bot/v2/hook/[A-Za-z0-9_-]+"
    extra_secret = True
    max_bytes = 20 * 1024

    def secrets(self, address):
        return [address, urlsplit(address).path.rsplit("/", 1)[1]]

    def hidden(self, address):
        return urlsplit(address).netloc + "/open-apis/bot/v2/hook/…"

    def requests(self, address, secret, title, body, level):
        payload = {"msg_type": "text", "content": {"text": ""}}
        if secret:
            ts = str(int(time.time()))
            key = (ts + "\n" + secret).encode("utf-8")
            payload.update(timestamp=ts, sign=base64.b64encode(hmac.new(key, b"", hashlib.sha256).digest()).decode("ascii"))

        def render(text):
            return json.dumps({**payload, "content": {"text": text}}, ensure_ascii=False, allow_nan=False)

        payload["content"]["text"] = _cut(title + "\n" + body, self.max_bytes, render)
        return [{"url": address, "json": payload, "success": {"field": "code", "equals": 0}}]


class Telegram(_Chat):
    id = "telegram"
    names = {"en": "Telegram", "zh": "Telegram"}
    hints = {"en": "Get a token from @BotFather, send the bot one message, then open https://api.telegram.org/bot<TOKEN>/getUpdates in a browser and copy chat.id. Paste https://api.telegram.org/bot<TOKEN>/sendMessage?chat_id=<CHAT_ID>.",
             "zh": "从 @BotFather 获取令牌，给机器人发一条消息，再用浏览器打开 https://api.telegram.org/bot<TOKEN>/getUpdates，复制 chat.id。粘贴 https://api.telegram.org/bot<TOKEN>/sendMessage?chat_id=<CHAT_ID>。"}
    hosts = ("api.telegram.org",)
    path = r"/bot\d+:[A-Za-z0-9_-]+/sendMessage"
    max_bytes = 4096 * 4

    def recognizes(self, value):
        return urlsplit(value).hostname in self.hosts

    def normalize(self, value):
        parsed = _url(value, "Telegram")
        chat = _query_key(parsed, "chat_id")
        if (parsed.hostname not in self.hosts or not re.fullmatch(self.path, parsed.path)
                or not re.fullmatch(r"-?\d+|@[A-Za-z0-9_]+", chat)):
            raise ValueError("paste the sendMessage address with its token and chat_id")
        return value

    def secrets(self, address):
        return [address, urlsplit(address).path[4:].split("/", 1)[0]]

    def hidden(self, address):
        parsed = urlsplit(address)
        return parsed.netloc + "/bot…/sendMessage?chat_id=" + _query_key(parsed, "chat_id")

    def requests(self, address, secret, title, body, level):
        return [{"url": address.split("?", 1)[0], "json": {
            "chat_id": _query_key(urlsplit(address), "chat_id"),
            "text": _characters(title + "\n" + body, 4096), "disable_notification": level == "passive"},
            "success": {"field": "ok", "equals": True}, "error_field": "error_code"}]


class Discord(_Chat):
    id = "discord"
    names = {"en": "Discord", "zh": "Discord"}
    hints = {"en": "Create a webhook in the channel's Integrations settings and paste its whole address.",
             "zh": "在频道设置的整合（Integrations）中创建 Webhook，粘贴完整地址。"}
    hosts = ("discord.com",)
    path = r"/api/webhooks/\d+/[A-Za-z0-9_-][A-Za-z0-9_.-]*"
    max_bytes = 2000 * 4

    def secrets(self, address):
        return [address, urlsplit(address).path.rsplit("/", 1)[1]]

    def hidden(self, address):
        parsed = urlsplit(address)
        return parsed.netloc + parsed.path.rsplit("/", 1)[0] + "/…"

    def requests(self, address, secret, title, body, level):
        payload = {"content": _characters(title + "\n" + body, 2000)}
        if level == "passive":
            payload["flags"] = 4096
        return [{"url": address, "json": payload, "error_field": "code", "headers": {
            "User-Agent": f"DiscordBot (https://github.com/FrostNovaOrg/Hansa, {__version__})"}}]


class Slack(_Chat):
    id = "slack"
    names = {"en": "Slack", "zh": "Slack"}
    hints = {"en": "Enable Incoming Webhooks in your Slack app, add one to the channel, and paste its whole address.",
             "zh": "在 Slack 应用中启用 Incoming Webhooks，为频道添加一个 Webhook，粘贴完整地址。"}
    hosts = ("hooks.slack.com",)
    path = r"/services/T[A-Za-z0-9_-]+/B[A-Za-z0-9_-]+/[A-Za-z0-9_-]+"
    max_bytes = 4000 * 4

    def secrets(self, address):
        return [address, urlsplit(address).path]

    def hidden(self, address):
        return urlsplit(address).netloc + "/services/…"

    def requests(self, address, secret, title, body, level):
        return [{"url": address, "json": {"text": _characters(title + "\n" + body, 4000)},
                 "success": {"status": 200, "text": "ok"}}]


class JsonWebhook:
    id = "json"
    names = {"en": "JSON webhook", "zh": "通用 JSON 网址"}
    hints = {"en": "Choose this service and paste any HTTPS address that accepts JSON POSTs. Put an optional Bearer token in Extra secret.",
             "zh": "选择此服务，粘贴接收 JSON POST 的 HTTPS 地址。可选的 Bearer 令牌填在额外密钥栏。"}
    extra_secret = True
    deadline_s = 30
    max_bytes = 64 * 1024
    levels = {level: level for level in ("passive", "active", "urgent")}

    def recognizes(self, value):
        return False

    def normalize(self, value):
        _url(value, "JSON webhook")
        return value

    def secrets(self, address):
        return [address]

    def hidden(self, address):
        return urlsplit(address).netloc + "/…"

    def requests(self, address, secret, title, body, level):
        return [{"url": address, "json": {"desk": title, "title": title, "text": body, "level": level},
                 "headers": {"Authorization": "Bearer " + secret} if secret else {}}]
