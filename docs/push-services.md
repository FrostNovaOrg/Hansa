# Push services

[简体中文](zh-CN/push-services.md)

On the desk's page, open **Settings → Alerts** and add a push address. Choose
**Recognize it** for the official addresses below, or choose a service yourself.
Self-hosted servers always need HTTPS. Save the entry and use its test button;
the result appears beside the entry and in `hansa status`.

Hansa sends the desk name and alert text. Your saved alert language controls the
wording. It never sends the one-time codes used to approve releases: answer
those on the desk's page. Addresses and extra secrets are stored privately on
this machine, with secret parts hidden on the page and in status. Do not put a
key, topic or token in an entry's name.

All of these routes expose alert text to the server you choose. Phone delivery
can also pass through Apple's or Google's push service; they can read that
text too. HTTPS protects the trip to the server, rather than hiding the alert
from that server. Self-hosting changes who runs the first server.

For WeCom, DingTalk, Feishu/Lark, Telegram, Discord and Slack, the message is
the desk name, a line break, then the alert body. Urgent batches start with
**Urgent:** or **紧急：** in your saved alert language; this marker does not
change notification permissions.
Telegram and Discord silence passive batches and otherwise use normal
notifications. The JSON webhook carries the level as a separate field.
Service rate limits still apply, including to test pushes and transport retries;
hansa does not pace requests to each service's quota.

## Bark

Install Bark and copy the address on its home screen, such as
`https://api.day.app/YOUR_DEVICE_KEY`. A ready-made curl command containing
exactly one HTTPS address also works. Hansa keeps only the server and device
key: example titles, bodies and query options such as `group` and `ttl` are
discarded. A bare device key uses `api.day.app`. Choose **Bark** explicitly for
a different server. There is no extra secret.

The device key is secret: someone with it can send to your device. Hansa sends
the key, desk name and alert text to that Bark server's `/push` address, and the
server sends the notification through Apple's push service. Neither server nor
Apple is prevented from reading it.

Passive alerts are quiet, active alerts use normal notification behavior, and
urgent alerts are time-sensitive. Hansa never asks for critical alerts.
Phone permissions and Focus settings still affect what you hear.

Hansa limits the complete message sent to Bark, including the key and other
fields, to 3,500 UTF-8 bytes. It shortens the body with `…` if necessary; if the
fixed fields alone are too large, the result is `too_big`. Bark's server may
impose further limits. A successful HTTP reply is `sent`; failures show their
HTTP category, such as `http_4xx` or `http_429`. Hansa does not retain Bark's
response text or a Bark-specific code.

Official information: [Bark](https://github.com/Finb/Bark).

## ntfy

Install ntfy for Android or iOS. Paste the address of the topic you subscribed
to in the app, such as `https://ntfy.sh/hansa-alerts-7f3k`. Official `ntfy.sh`
addresses are recognized automatically; a trailing slash after the topic is
accepted and removed. For a self-hosted server, choose
**ntfy** and paste `https://YOUR_SERVER/YOUR_TOPIC`. If the server requires an
access token, put that token (`tk_…`) in **Extra secret**; otherwise leave it
empty.

The topic is secret: anyone who knows an unprotected topic can read and send
messages. Choose a hard-to-guess topic of at least eight characters. Hansa
refuses shorter topics unless you give an access token, so a short topic needs
authenticated access. Redaction and file scrubbing deliberately ignore secret
parts shorter than eight characters to avoid changing ordinary task text; the
whole address is still protected. The token is secret too;
configure access restrictions on your server rather than relying on a short
topic alone.

Hansa sends the topic, desk name, alert text and priority as a JSON message to
the server's root address, for example `https://ntfy.sh/`. A token travels in
the Authorization header. The title always goes in the JSON body.
The ntfy server can read these fields. Delivery to a phone may continue through
Apple's or Google's push service, depending on the app and server setup.

Passive uses priority 2 (low, no sound), active uses 3 (default), and urgent
uses 4 (high). Hansa never uses priority 5, which can cause long vibration
bursts. The message is limited to 4,096 UTF-8 bytes, and the title to 1,024
bytes, shortening either with `…` when needed. This avoids ntfy treating a long
message as an attachment or refusing it. `ntfy.sh` allows 250 messages a day;
self-hosted limits depend on the administrator.

HTTP 200 means sent. A failure keeps the server's numeric `code`, when present:
**40301** means forbidden (check the topic's permissions and token), and
**42908** means the daily quota was reached. HTTP 429 may be retried within the
entry's deadline; a retry does not lift a quota.

Official information: [publishing](https://docs.ntfy.sh/publish/) and
[authentication](https://docs.ntfy.sh/publish/#authentication).

## Gotify

Gotify has an Android app and requires your own server. On the Gotify web page,
open **Apps** and create an application. Copy its application token immediately:
Gotify 3 shows it only once. Choose **Gotify** in hansa; it is never recognized
automatically. Paste the address written in Gotify's documentation,
`https://YOUR_SERVER/message?token=YOUR_APP_TOKEN`, or paste just
`https://YOUR_SERVER` and put the application token in **Extra secret**.

The application token is secret and authorizes sending to the application.
Hansa sends it in the `X-Gotify-Key` header to `https://YOUR_SERVER/message`,
removing it from the outgoing address. The message contains the desk name,
alert text and priority. Your server can read them. Gotify's Android app
normally receives messages directly from that server; a setup that forwards
through another phone push service can also expose the text to Google or
Apple. Gotify itself does not require that forwarding.

Passive uses priority 2 (status-bar icon only), active uses 5 (icon and sound),
and urgent uses 8 (sound and vibration). Hansa never uses 0, which shows
nothing. Gotify documents no message-size limit; hansa caps the body at 4,096
UTF-8 bytes and ends a shortened body with `…`. Your server may set other limits.

HTTP 200 means sent. A failure keeps Gotify's `errorCode` if present, alongside
its HTTP category. For HTTP 401 or 403, check that you copied the application's
token and that the server permits sending. Hansa does not save Gotify's error
description.

Official information: [pushing messages](https://gotify.net/docs/pushmsg).

## PushDeer

Install PushDeer for iOS, Android or Mac. Copy the key from the app's **Key**
tab. Choose **PushDeer** and paste that bare key to use the official server,
`https://api2.pushdeer.com`. A bare key cannot distinguish PushDeer from Bark,
so **Recognize it** treats ordinary bare keys as Bark keys. You can instead
paste `https://api2.pushdeer.com/message/push?pushkey=YOUR_KEY`, which is
recognized automatically. There is no extra secret.

For your own server, choose **PushDeer** and paste
`https://YOUR_SERVER/message/push?pushkey=YOUR_KEY`. Self-hosted PushDeer often
defaults to plain HTTP on port 8800. Hansa refuses that address: set up HTTPS
first, for example with an HTTPS reverse proxy.

The whole key is secret and permits sending notifications. Hansa posts a form
to the server's `/message/push` address containing the key, the type `text`,
and the desk name followed by a line break and the alert body. The server then
delivers through the device's push service, including Apple or Google where
used. Those services and the PushDeer server can read the text.

PushDeer has no alert levels. An urgent message starts with **Urgent:** in
English or **紧急：** in Chinese; this is a text marker and does not override
the phone's notification settings. Hansa caps the combined title and body at
3,500 UTF-8 bytes, with `…` when shortened, to leave room below the phone push
service's roughly 4 KB limit. PushDeer documents no further message-size limit;
server quotas may apply.

Success requires HTTP 200 and a JSON `code` of **0**. A nonzero code such as
**80501** is a refusal even when HTTP says 200; hansa shows
`refused_by_service` with that code and does not retry it. Check the key and the
server's own account information. A non-JSON response is also refused, with no
code. Other HTTP failures keep their HTTP category.

Official information: [PushDeer and its API](https://github.com/easychen/pushdeer).

## Server酱 (WeChat)

Open Server酱's **SendKey** page and copy the whole SendKey. Turbo keys begin
with `SCT`; Server酱³ keys begin with `sctp`, digits identifying your account,
then `t`. Both are recognized automatically. These ready-made addresses also
work: `https://sctapi.ftqq.com/YOUR_SENDKEY.send` and
`https://YOUR_UID.push.ft07.com/send/YOUR_SENDKEY.send`. There is no extra secret.

The whole key is secret and authorizes sending to your account. Hansa sends the
desk name and alert body to the corresponding Server酱 server, with the key in
the address. Server酱 forwards to WeChat; Tencent and any Apple or Google push
service used for phone delivery can read the notification too.

Server酱 has no alert levels. Urgent alerts start with **Urgent:** or **紧急：**
in your saved alert language. Hansa joins the title's lines with a space and
keeps at most 32 characters, matching Server酱's own integrations. The body is
sent as literal lines in Markdown code blocks, so text such as `*words*`, links
or headings is shown as text. Hansa caps that formatted body at 3,500 UTF-8
bytes and ends a shortened body with `…`.

Free Turbo accounts allow **5 pushes a day, including failed attempts**;
subscribers get 1,000 a day, with a limit of 50 a minute. Testing uses a push
too. Server酱³ may have different account limits; check its page.

Success requires HTTP 200 and a JSON `code` of **0**. A different code is
`refused_by_service`, even on HTTP 200, and is not retried. Hansa keeps the code
only, not the service's `message`; use Server酱's own page for further details
about the key, account or quota. A non-JSON reply is refused without a code.

Official information: [Turbo](https://sct.ftqq.com/) and
[Server酱³](https://doc.sc3.ft07.com/).

## 企业微信群机器人 (WeCom group bot)

Add a robot to your WeCom group and paste its whole address:
`https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=YOUR_KEY`.
Recognition uses that host and `/cgi-bin/webhook/send` path. The key is secret;
there is no extra secret. WeCom and the group's members can read the alert.

Hansa POSTs JSON to the pasted address:
`{"msgtype":"text","text":{"content":"DESK\nBODY"}}`, without mentions.
There are no levels; urgent text uses the shared marker above. Content is
limited to **2,048 UTF-8 bytes**, shortened at character boundaries with `…`.
The robot allows **20 messages a minute**.

A JSON `errcode` of **0** acknowledges delivery. On HTTP 200, another code is
`refused_by_service`: **45009** means rate limited; **93000** means the address
is invalid or the robot was removed from the group. Only the code is retained.

Official information: [WeCom group robots](https://developer.work.weixin.qq.com/document/path/91770).

## 钉钉自定义机器人 (DingTalk custom robot)

Paste `https://oapi.dingtalk.com/robot/send?access_token=YOUR_TOKEN` from your
custom robot. Recognition uses the host and `/robot/send` path; the
international help centre's `oapi.dingtalk.io` host also works and is called
as pasted. The token is secret. Put the optional **加签** signing secret
(`SEC…`) in **Extra secret**. If keyword security is enabled, use the desk's
name as the keyword: the desk name starts each message, after the urgent marker
when there is one.
DingTalk and the conversation's members can read the alert.

Hansa POSTs `{"msgtype":"text","text":{"content":"DESK\nBODY"}}` as JSON.
With a signing secret, every send appends `&timestamp=TS&sign=SIGN` to the URL:
TS is the current Unix time in milliseconds, and SIGN is Base64 of
HMAC-SHA256 over `TS + "\n" + secret`, with the UTF-8 secret as the key,
then URL-encoded with `quote_plus`. Without a secret neither field is added.
There are no levels; urgent text uses the shared marker.

DingTalk documents no text limit; hansa caps content at **3,500 UTF-8 bytes**,
shortening with `…`. A robot allows **20 messages a minute**; exceeding that
blocks it for **10 minutes** (code **410100**).

A JSON `errcode` numerically equal to **0**, including `"0"`, means sent.
Other HTTP 200 codes are `refused_by_service`: **310000** means a security
check failed (keyword, timestamp, signature or IP); **400101** means the token
does not exist.

Official information: [custom robots](https://open.dingtalk.com/document/robots/custom-robot-access)
and [security settings](https://open.dingtalk.com/document/robots/customize-robot-security-settings).

## 飞书 / Lark custom bot

Paste `https://open.feishu.cn/open-apis/bot/v2/hook/YOUR_HOOK_ID` or
`https://open.larksuite.com/open-apis/bot/v2/hook/YOUR_HOOK_ID`. Both hosts and
the hook path are recognized. The hook id is secret. Put the optional
**签名校验** secret in **Extra secret**. For keyword security, use the desk's
name as the keyword, since every message starts with it after any urgent
marker. Feishu/Lark and the conversation's members can read the alert.

Hansa POSTs JSON `{"msg_type":"text","content":{"text":"DESK\nBODY"}}`.
With a secret it adds top-level `"timestamp":"TS"` (Unix seconds, as text)
and `"sign":"SIGN"`. SIGN is Base64 of HMAC-SHA256 over an **empty message**,
with UTF-8 `TS + "\n" + secret` as the key; it is not URL-encoded.
Without a secret neither field is added. There are no levels; urgent text
uses the shared marker.

The **whole JSON body**, including signature fields and JSON escapes, stays
within **20 KB (20,480 bytes)**; text is shortened with `…` when needed.
Limits are **100 messages a minute** and **5 a second**.

A JSON `code` of **0** means sent. Other HTTP 200 codes are
`refused_by_service`: **19021** means a signature or timestamp problem,
**19022** an IP refusal, **19024** a missing keyword, and **11232** rate limiting.

Official information: [Feishu](https://open.feishu.cn/document/client-docs/bot-v3/add-custom-bot)
and [Lark](https://open.larksuite.com/document/client-docs/bot-v3/add-custom-bot).

## Telegram

Get a bot token from **@BotFather**. Send your bot one message, then open
`https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser and copy the id
inside `"chat":{"id":…}`. Paste
`https://api.telegram.org/bot<TOKEN>/sendMessage?chat_id=<CHAT_ID>` into hansa.
The `api.telegram.org` host is recognized; the address must have the
`sendMessage` path, token and chat id. The token is secret; the chat id is not.
There is no extra secret. Telegram and the chat's members can read the alert.

Hansa removes the query from the destination and POSTs JSON to
`https://api.telegram.org/bot<TOKEN>/sendMessage`:
`{"chat_id":"CHAT_ID","text":"DESK\nBODY","disable_notification":false}`.
A passive batch sets `disable_notification` to `true`; urgent text uses the
shared marker and normal notification behavior. Text is limited to
**4,096 characters**, shortening with `…`. Allow about **1 message a second**
in a chat and **20 a minute** in a group.

The JSON boolean `ok: true` means sent. On failure hansa keeps `error_code`,
such as **400** (bad request) or **403** (forbidden), without the description.

Official information: [bot setup](https://core.telegram.org/bots/tutorial#obtain-your-bot-token)
and [sendMessage](https://core.telegram.org/bots/api#sendmessage).

## Discord

In a channel's **Integrations** settings, create a webhook and paste
`https://discord.com/api/webhooks/WEBHOOK_ID/WEBHOOK_TOKEN`.
Recognition uses that host and `/api/webhooks/ID/TOKEN` path. The token and
the whole address are secret; there is no extra secret. Discord and the
channel's members can read the alert.

Hansa POSTs JSON `{"content":"DESK\nBODY"}` to that address, with
`User-Agent: DiscordBot (https://github.com/FrostNovaOrg/Hansa, VERSION)`
using hansa's version; Discord may block urllib's default agent. A passive
batch adds `"flags":4096` to suppress push notifications. Urgent text uses
the shared marker and normal notifications. Content is limited to
**2,000 characters**, shortening with `…`; Discord's webhook rate limits apply.

Any HTTP 2xx means sent, including Discord's normal **204** response.
**HTTP 404 means the webhook is gone**: create a new one and replace its
saved address. It appears as `http_4xx` in the last result.

Official information: [execute webhook](https://discord.com/developers/docs/resources/webhook#execute-webhook)
and [User-Agent](https://discord.com/developers/docs/reference#user-agent).

## Slack

Enable **Incoming Webhooks** in your Slack app, add one to the channel, and
paste `https://hooks.slack.com/services/T…/B…/…`. The host and that path shape
are recognized. The path is secret; there is no extra secret. Slack and the
channel's members can read the alert.

Hansa POSTs JSON `{"text":"DESK\nBODY"}`. There are no levels; urgent text
uses the shared marker. Hansa keeps text within **4,000 characters**,
shortening with `…`. Slack allows **1 message a second**.

Success requires **HTTP 200** and the exact whole body **`ok`**. Failures
return a plain-text word such as `invalid_payload` or `channel_not_found`;
hansa keeps that word as the code, with `http_4xx` for a 400/404 response or
`refused_by_service` for an unsuccessful acknowledgement on HTTP 200.

Official information: [Incoming Webhooks](https://docs.slack.dev/messaging/sending-messages-using-incoming-webhooks/).

## JSON webhook (通用 JSON 网址)

Choose **JSON webhook** explicitly and paste any HTTPS address, for example
a home server or automation tool's endpoint. It is never recognized
automatically: an unknown host still asks you to choose a service. The whole
address is secret, including any path or query. Put an optional token in
**Extra secret**; it is sent as `Authorization: Bearer TOKEN`.
The endpoint's operator can read the alert and token.

Hansa POSTs this JSON shape, with the desk name as both `desk` and `title`,
and the alert body alone as `text`:

```json
{"desk":"My desk","title":"My desk","text":"Alert body","level":"active"}
```

`level` is `passive`, `active` or `urgent`. This endpoint receives the level
directly, so its title has no urgent marker. There are no one-time codes.
Any HTTP 2xx means sent; no response-body check is required. The transport's
64 KiB request-body limit applies; quotas depend on your endpoint.

## Reading a failed result

`refused_by_service` means the server answered but did not acknowledge the
push in the way its service requires. Hansa retains only a numeric error code
or an ASCII word up to 40 characters, never the response's message or contents.
The code appears in the entry's last result and in `hansa status`.

`http_4xx` usually needs an address, token or permission fix; `http_429` means
rate limiting; `http_5xx` means a server error. `tls`, `network`, `timeout` and
`deadline` indicate connection or time-limit problems. A `redirect` is refused
to keep secrets from going to another address: paste the final HTTPS address
your service documents. See [alerts and delivery rules](releases.md#alerts)
for retry behavior.
