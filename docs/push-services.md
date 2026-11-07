# Phone push services

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
addresses are recognized automatically. For a self-hosted server, choose
**ntfy** and paste `https://YOUR_SERVER/YOUR_TOPIC`. If the server requires an
access token, put that token (`tk_…`) in **Extra secret**; otherwise leave it
empty.

The topic is secret: anyone who knows an unprotected topic can read and send
messages. Choose a hard-to-guess topic of at least eight characters. Hansa
refuses shorter topics unless you give an access token, because its redaction
cannot protect strings shorter than eight characters. The token is secret too;
configure access restrictions on your server rather than relying on a short
topic alone.

Hansa sends the topic, desk name, alert text and priority as a JSON message to
the server's root address, for example `https://ntfy.sh/`. A token travels in
the Authorization header. Chinese titles travel in the message, not a header.
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

PushDeer has no alert levels. An urgent message starts with **Urgent: ** in
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

Server酱 has no alert levels. Urgent alerts start with **Urgent: ** or **紧急：**
in your saved alert language. Hansa removes title line breaks and keeps at most
32 characters, matching Server酱's own integrations. The body is sent as literal
lines in Markdown code blocks, so text such as `*words*`, links or headings is
shown as text. Hansa caps that formatted body at 3,500 UTF-8 bytes and ends a
shortened body with `…`.

Free Turbo accounts allow **5 pushes a day, including failed attempts**;
subscribers get 1,000 a day, with a limit of 50 a minute. Testing uses a push
too. Server酱³ may have different account limits; check its page.

Success requires HTTP 200 and a JSON `code` of **0**. A different code is
`refused_by_service`, even on HTTP 200, and is not retried. Hansa keeps the code
only, not the service's `message`; use Server酱's own page for further details
about the key, account or quota. A non-JSON reply is refused without a code.

Official information: [Turbo](https://sct.ftqq.com/) and
[Server酱³](https://doc.sc3.ft07.com/).

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
