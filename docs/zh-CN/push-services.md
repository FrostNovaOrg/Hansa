# 推送服务

[English](../push-services.md)

在工作台的网页上打开 **Settings → Alerts（设置 → 提醒）**，添加一个推送地址。如果用的是下面列出的官方地址，选 **Recognize it（自动识别）**；也可以自己指定服务。自建的服务器一律要用 HTTPS。保存这条地址，再用它的测试按钮发一条测试推送；结果会显示在这条地址旁边，也会出现在 `hansa status` 里。

hansa 发送的是工作台名称和提醒正文，措辞由你保存的提醒语言决定。批准发布用的一次性代码从不通过推送发送：请在网页上回答。地址和额外密钥都私密地保存在这台机器上，其中密钥部分在网页和 `hansa status` 里都会隐藏。不要把密钥、主题或令牌写进地址的名称里。

以上这些方式都会把提醒正文暴露给你选择的服务器。发到手机时，内容还可能经过苹果或谷歌的推送服务，它们同样能读到这些文字。HTTPS 保护的是发往服务器这一段传输，而不是让那台服务器看不到提醒。自建服务器改变的只是第一台服务器由谁运行。

对企业微信、钉钉、飞书/Lark、Telegram、Discord 和 Slack，消息内容依次是工作台名称、一个换行和提醒正文。紧急提醒会按你保存的提醒语言，以 **Urgent:** 或 **紧急：** 开头；这个标记不会改变通知权限。Telegram 和 Discord 会让被动提醒不出声，其他情况使用普通通知。通用 JSON 网址则把级别放在单独的字段里。各服务自己的频率限制仍然有效，测试推送和传输重试也算在内；hansa 不会按各服务的配额来调节发送速度。

## Bark

安装 Bark，复制它首页上显示的地址，例如 `https://api.day.app/YOUR_DEVICE_KEY`。现成的 curl 命令也可以，只要其中恰好包含一个 HTTPS 地址。hansa 只保留服务器和设备密钥：命令里的示例标题、正文以及 `group`、`ttl` 之类的查询选项都会被丢弃。只给设备密钥时，使用 `api.day.app`。要换别的服务器，请手动选择 **Bark**。没有额外密钥。

设备密钥是保密的：拿到它的人可以往你的设备发推送。hansa 把密钥、工作台名称和提醒正文发送到那台 Bark 服务器的 `/push` 地址，再由服务器通过苹果的推送服务发出通知。那台服务器和苹果都能读到这些内容。

被动提醒不出声，主动提醒使用普通通知方式，紧急提醒会标记为时效性通知。hansa 从不申请发送关键提醒的权限。手机的权限和专注模式设置仍会影响你能听到什么。

hansa 发给 Bark 的整条消息（包括密钥和其他字段）不得超过 3,500 个 UTF-8 字节。需要时它会用 `…` 缩短正文；如果光是固定字段就太大，结果会是 `too_big`。Bark 的服务器还可能另有限制。HTTP 请求成功时结果是 `sent`；失败时显示 HTTP 类别，例如 `http_4xx` 或 `http_429`。hansa 不保留 Bark 的响应文字，也不保留 Bark 自己的错误码。

官方资料：[Bark](https://github.com/Finb/Bark)。

## ntfy

在 Android 或 iOS 上安装 ntfy。粘贴你在应用里订阅的主题地址，例如 `https://ntfy.sh/hansa-alerts-7f3k`。官方的 `ntfy.sh` 地址会自动识别；主题后面多一个斜杠也可以，会被去掉。自建服务器请选择 **ntfy**，粘贴 `https://YOUR_SERVER/YOUR_TOPIC`。如果服务器要求访问令牌，把这个令牌（`tk_…`）填在 **Extra secret（额外密钥）** 里；否则留空。

主题是保密的：任何知道未受保护主题的人都能读取和发送消息。请选一个至少八个字符、难以猜到的主题。少于八个字符的主题 hansa 不接受，除非你提供访问令牌，所以短主题必须配合身份验证。为了让普通任务文字不被改动，隐去密钥和清理文件时会有意忽略不足八个字符的密钥部分；整个地址仍然受保护。令牌同样保密；请在你的服务器上设置访问限制，不要只依赖短主题。

hansa 把主题、工作台名称、提醒正文和优先级作为一条 JSON 消息发送到服务器的根地址，例如 `https://ntfy.sh/`。令牌放在 Authorization 头里。标题始终放在 JSON 请求体里。ntfy 服务器能读到这些字段。发到手机时，内容还可能继续经过苹果或谷歌的推送服务，具体取决于应用和服务器的设置。

被动提醒用优先级 2（低，不发声），主动提醒用 3（默认），紧急提醒用 4（高）。hansa 从不用优先级 5，它可能引起长时间连续振动。消息最多 4,096 个 UTF-8 字节，标题最多 1,024 字节，需要时用 `…` 缩短。这样可以避免 ntfy 把过长的消息当作附件，或者直接拒收。`ntfy.sh` 每天允许 250 条消息；自建服务器的限制由管理员决定。

HTTP 200 表示已发送。失败时，如果服务器返回了数字 `code`，hansa 会保留它：**40301** 表示被拒绝（请检查主题的权限和令牌），**42908** 表示已达到每日配额。HTTP 429 可以在这条地址的时限内重试；重试并不会解除配额限制。

官方资料：[发布消息（英文）](https://docs.ntfy.sh/publish/)和[身份验证（英文）](https://docs.ntfy.sh/publish/#authentication)。

## Gotify

Gotify 有 Android 应用，而且需要你自己的服务器。在 Gotify 网页上打开 **Apps**，创建一个应用，并立刻复制它的应用令牌：Gotify 3 只显示一次。在 hansa 里选择 **Gotify**；它从不自动识别。粘贴 Gotify 文档里写的地址 `https://YOUR_SERVER/message?token=YOUR_APP_TOKEN`，或者只粘贴 `https://YOUR_SERVER`，把应用令牌填在 **Extra secret（额外密钥）** 里。

应用令牌是保密的，它授权你向这个应用发送消息。hansa 把它放在 `X-Gotify-Key` 头里发送到 `https://YOUR_SERVER/message`，并从要发送的地址中去掉它。消息包含工作台名称、提醒正文和优先级，你的服务器能读到。Gotify 的 Android 应用通常直接从这台服务器接收消息；如果额外经过别的手机推送服务转发，文字也可能暴露给谷歌或苹果。Gotify 本身并不要求这种转发。

被动提醒用优先级 2（只显示状态栏图标），主动提醒用 5（图标和声音），紧急提醒用 8（声音和振动）。hansa 从不用 0，它什么也不显示。Gotify 没有写明消息大小限制；hansa 把正文限制在 4,096 个 UTF-8 字节以内，缩短时以 `…` 结尾。你的服务器可能另有限制。

HTTP 200 表示已发送。失败时，如果 Gotify 返回了 `errorCode`，hansa 会把它和 HTTP 类别一起保留。遇到 HTTP 401 或 403，请检查应用令牌是否复制正确，以及服务器是否允许发送。hansa 不保存 Gotify 的错误描述。

官方资料：[推送消息（英文）](https://gotify.net/docs/pushmsg)。

## PushDeer

在 iOS、Android 或 Mac 上安装 PushDeer，从应用的 **Key** 页面复制密钥。选择 **PushDeer** 并粘贴这个密钥，就会使用官方服务器 `https://api2.pushdeer.com`。单独的密钥无法区分 PushDeer 和 Bark，所以 **Recognize it（自动识别）** 会把普通的单独密钥当作 Bark 密钥。你也可以改为粘贴 `https://api2.pushdeer.com/message/push?pushkey=YOUR_KEY`，这个地址会自动识别。没有额外密钥。

使用自己的服务器时，选择 **PushDeer**，粘贴 `https://YOUR_SERVER/message/push?pushkey=YOUR_KEY`。自建的 PushDeer 常常默认在 8800 端口上用明文 HTTP。hansa 拒绝这样的地址：请先配好 HTTPS，例如用 HTTPS 反向代理。

整个密钥都是保密的，它能用来发送通知。hansa 会向服务器的 `/message/push` 地址提交一个表单，其中包含密钥、类型 `text`，以及工作台名称、一个换行和提醒正文。然后服务器通过设备的推送服务投递，用到的可能包括苹果或谷歌。这些服务和 PushDeer 服务器都能读到文字内容。

PushDeer 没有提醒级别。紧急消息在英文下以 **Urgent:** 开头，中文下以 **紧急：** 开头；这只是一个文字标记，不会覆盖手机的通知设置。hansa 把标题和正文合计限制在 3,500 个 UTF-8 字节以内，缩短时用 `…`，以便低于手机推送服务大约 4 KB 的上限。PushDeer 没有写明更多消息大小限制；服务器可能另有配额。

成功要求 HTTP 200 且 JSON 里的 `code` 为 **0**。像 **80501** 这样的非零 code 表示被拒绝，即使 HTTP 状态是 200；hansa 会显示 `refused_by_service` 并带上这个 code，而且不重试。请检查密钥和服务器上的账号信息。响应不是 JSON 时同样算被拒绝，不带 code。其他 HTTP 失败保留各自的 HTTP 类别。

官方资料：[PushDeer 及其 API](https://github.com/easychen/pushdeer)。

## Server酱（微信）

打开 Server酱 的 **SendKey** 页面，复制完整的 SendKey。Turbo 的密钥以 `SCT` 开头；Server酱³ 的密钥以 `sctp` 开头，接着是代表你账号的数字，然后是 `t`。两种都会自动识别。下面这些现成的地址也可以直接用：`https://sctapi.ftqq.com/YOUR_SENDKEY.send` 和 `https://YOUR_UID.push.ft07.com/send/YOUR_SENDKEY.send`。没有额外密钥。

整个密钥都是保密的，它授权你向自己的账号发送消息。hansa 把工作台名称和提醒正文发送到对应的 Server酱 服务器，密钥就在地址里。Server酱 再转发到微信；腾讯，以及手机投递中可能用到的苹果或谷歌推送服务，也都能读到通知内容。

Server酱 没有提醒级别。紧急提醒按你保存的提醒语言，以 **Urgent:** 或 **紧急：** 开头。hansa 会用空格连接标题的各行，并且最多保留 32 个字符，与 Server酱 自家的集成保持一致。正文以 Markdown 代码块里的原样文字行发送，所以 `*words*` 这样的文字、链接或标题都会按普通文字显示。hansa 把这个排版后的正文限制在 3,500 个 UTF-8 字节以内，缩短时以 `…` 结尾。

免费的 Turbo 账号每天允许 **5 次推送，发送失败也算**；订阅用户每天 1,000 次，每分钟最多 50 次。测试推送也会占用一次推送。Server酱³ 的账号限制可能不同，请查看它的页面。

成功要求 HTTP 200 且 JSON 里的 `code` 为 **0**。其他 code 都算 `refused_by_service`，即使 HTTP 状态是 200，而且不会重试。hansa 只保留 code，不保留服务返回的 `message`；关于密钥、账号或配额的更多细节，请到 Server酱 自己的页面上查看。响应不是 JSON 时算作被拒绝，不带 code。

官方资料：[Turbo](https://sct.ftqq.com/)和 [Server酱³](https://doc.sc3.ft07.com/)。

## 企业微信群机器人

在企业微信群里添加一个机器人，粘贴它的完整地址：`https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=YOUR_KEY`。识别依据是这个主机和 `/cgi-bin/webhook/send` 路径。密钥是保密的；没有额外密钥。企业微信和群成员都能读到提醒内容。

hansa 向粘贴的地址 POST 一段 JSON：`{"msgtype":"text","text":{"content":"DESK\nBODY"}}`，不@任何人。这里没有级别；紧急文字用上面共用的标记。内容最多 **2,048 个 UTF-8 字节**，超长时在字符边界处用 `…` 缩短。这个机器人每分钟最多 **20 条消息**。

JSON 里的 `errcode` 为 **0** 表示已送达。在 HTTP 200 下，其他 code 都算 `refused_by_service`：**45009** 表示触发频率限制；**93000** 表示地址无效，或者机器人已被移出群。hansa 只保留 code。

官方资料：[企业微信群机器人](https://developer.work.weixin.qq.com/document/path/91770)。

## 钉钉自定义机器人

从你的自定义机器人里复制 `https://oapi.dingtalk.com/robot/send?access_token=YOUR_TOKEN` 并粘贴。识别依据是主机和 `/robot/send` 路径；国际版帮助中心的 `oapi.dingtalk.io` 主机也可以，会按粘贴的原样调用。令牌是保密的。可选的**加签**密钥（`SEC…`）填在 **Extra secret（额外密钥）** 里。如果启用了关键词安全设置，请把工作台名称设为关键词：工作台名称放在每条消息开头；有紧急标记时，则紧跟在标记之后。钉钉和会话成员都能读到提醒内容。

hansa 以 JSON 形式 POST `{"msgtype":"text","text":{"content":"DESK\nBODY"}}`。设置了加签密钥后，每次发送都会在 URL 后面加上 `&timestamp=TS&sign=SIGN`：TS 是当前的 Unix 时间，单位毫秒；SIGN 是对 `TS + "\n" + secret` 做 HMAC-SHA256 后再 Base64 的结果，以 UTF-8 编码的密钥作为 HMAC 密钥，最后用 `quote_plus` 做 URL 编码。没有密钥时这两个字段都不加。这里没有级别；紧急文字用上面共用的标记。

钉钉没有写明文字限制；hansa 把内容限制在 **3,500 个 UTF-8 字节**以内，超长时用 `…` 缩短。一个机器人每分钟允许 **20 条消息**；超出后会被封禁 **10 分钟**（code **410100**）。

JSON 里的 `errcode` 数值上等于 **0**（包括 `"0"`）表示已发送。HTTP 200 下的其他 code 都算 `refused_by_service`：**310000** 表示安全校验未通过（关键词、时间戳、签名或 IP）；**400101** 表示令牌不存在。

官方资料：[自定义机器人](https://open.dingtalk.com/document/robots/custom-robot-access)和[安全设置](https://open.dingtalk.com/document/robots/customize-robot-security-settings)。

## 飞书 / Lark 自定义机器人

粘贴 `https://open.feishu.cn/open-apis/bot/v2/hook/YOUR_HOOK_ID` 或 `https://open.larksuite.com/open-apis/bot/v2/hook/YOUR_HOOK_ID`。两个主机和这个 hook 路径都会自动识别。hook id 是保密的。可选的**签名校验**密钥填在 **Extra secret（额外密钥）** 里。如果启用了关键词校验，请把工作台名称设为关键词，因为每条消息都紧跟在紧急标记之后以它开头。飞书 / Lark 和会话成员都能读到提醒内容。

hansa 以 JSON 形式 POST `{"msg_type":"text","content":{"text":"DESK\nBODY"}}`。设置了密钥后，它会在顶层加上 `"timestamp":"TS"`（Unix 秒，以文本形式）和 `"sign":"SIGN"`。SIGN 是对**空消息**做 HMAC-SHA256 后再 Base64 的结果，密钥是 UTF-8 编码的 `TS + "\n" + secret`；结果不做 URL 编码。没有密钥时这两个字段都不加。这里没有级别；紧急文字用上面共用的标记。

**整个 JSON 请求体**（包括签名字段和 JSON 转义）都不得超过 **20 KB（20,480 字节）**；需要时用 `…` 缩短文字。限制是每分钟 **100 条消息**、每秒 **5 条**。

JSON 里的 `code` 为 **0** 表示已发送。HTTP 200 下的其他 code 都算 `refused_by_service`：**19021** 表示签名或时间戳有问题，**19022** 表示 IP 被拒绝，**19024** 表示缺少关键词，**11232** 表示触发频率限制。

官方资料：[飞书](https://open.feishu.cn/document/client-docs/bot-v3/add-custom-bot)和 [Lark（英文）](https://open.larksuite.com/document/client-docs/bot-v3/add-custom-bot)。

## Telegram

向 **@BotFather** 要一个机器人令牌。先给你的机器人发一条消息，然后在浏览器里打开 `https://api.telegram.org/bot<TOKEN>/getUpdates`，复制 `"chat":{"id":…}` 里的 id。把 `https://api.telegram.org/bot<TOKEN>/sendMessage?chat_id=<CHAT_ID>` 粘贴到 hansa 里。`api.telegram.org` 主机会被识别；地址里必须有 `sendMessage` 路径、令牌和 chat id。令牌是保密的，chat id 不是。没有额外密钥。Telegram 和聊天成员都能读到提醒内容。

hansa 会从目标地址里去掉查询参数，然后向 `https://api.telegram.org/bot<TOKEN>/sendMessage` POST JSON：`{"chat_id":"CHAT_ID","text":"DESK\nBODY","disable_notification":false}`。被动提醒会把 `disable_notification` 设为 `true`；紧急文字用上面共用的标记，并使用普通通知方式。文字最多 **4,096 个字符**，超长时用 `…` 缩短。私聊里大约每秒 **1 条消息**，群里每分钟 **20 条**。

JSON 里的布尔值 `ok: true` 表示已发送。失败时 hansa 会保留 `error_code`，例如 **400**（请求有误）或 **403**（被禁止），但不保留描述文字。

官方资料：[机器人设置（英文）](https://core.telegram.org/bots/tutorial#obtain-your-bot-token)和 [sendMessage（英文）](https://core.telegram.org/bots/api#sendmessage)。

## Discord

在频道设置里的**整合（Integrations）**中创建一个 webhook，然后粘贴 `https://discord.com/api/webhooks/WEBHOOK_ID/WEBHOOK_TOKEN`。识别依据是这个主机和 `/api/webhooks/ID/TOKEN` 路径。令牌和整个地址都是保密的；没有额外密钥。Discord 和频道成员都能读到提醒内容。

hansa 向该地址 POST JSON `{"content":"DESK\nBODY"}`，并带上 `User-Agent: DiscordBot (https://github.com/FrostNovaOrg/Hansa, VERSION)`，其中的版本号用 hansa 自己的版本；Discord 可能会拦下 urllib 默认的 User-Agent。被动提醒会加上 `"flags":4096`，以不发出推送通知。紧急文字用上面共用的标记，并使用普通通知。内容最多 **2,000 个字符**，超长时用 `…` 缩短；Discord 的 webhook 频率限制仍然有效。

任何 HTTP 2xx 都表示已发送，包括 Discord 正常的 **204** 响应。**HTTP 404 表示 webhook 已不存在**：请创建一个新的，并替换保存的地址。它会在上次结果里显示为 `http_4xx`。

官方资料：[执行 webhook（英文）](https://discord.com/developers/docs/resources/webhook#execute-webhook)和 [User-Agent（英文）](https://discord.com/developers/docs/reference#user-agent)。

## Slack

在你的 Slack 应用里启用 **Incoming Webhooks**，给频道添加一个，然后粘贴 `https://hooks.slack.com/services/T…/B…/…`。主机会被识别，路径也要符合这个形状。路径是保密的；没有额外密钥。Slack 和频道成员都能读到提醒内容。

hansa POST JSON `{"text":"DESK\nBODY"}`。这里没有级别；紧急文字用上面共用的标记。hansa 把文字控制在 **4,000 个字符**以内，超长时用 `…` 缩短。Slack 允许每秒 **1 条消息**。

成功要求 **HTTP 200**，而且整个响应体恰好是 **`ok`**。失败时会返回 `invalid_payload`、`channel_not_found` 这样的纯文本单词；hansa 把这个单词当作 code 保留，响应为 400/404 时用 `http_4xx`，HTTP 200 但确认不成功时用 `refused_by_service`。

官方资料：[Incoming Webhooks（英文）](https://docs.slack.dev/messaging/sending-messages-using-incoming-webhooks/)。

## 通用 JSON 网址

请明确选择 **JSON webhook（通用 JSON 网址）**，粘贴任意 HTTPS 地址，比如家里服务器或自动化工具的接口。它从不自动识别：遇到未知主机会要求你先选择推送服务。整个地址都是保密的，包括路径和查询参数。可选的令牌填在 **Extra secret（额外密钥）** 里，会以 `Authorization: Bearer TOKEN` 发送。接口的运营者能读到提醒内容和令牌。

hansa 会 POST 下面这样的 JSON，把工作台名称同时放进 `desk` 和 `title`，`text` 只放提醒正文：

```json
{"desk":"My desk","title":"My desk","text":"Alert body","level":"active"}
```

`level` 为 `passive`、`active` 或 `urgent`。这个接口直接收到级别，所以它的标题里没有紧急标记。这里没有一次性代码。任何 HTTP 2xx 都表示已发送，不需要检查响应体。传输过程有 64 KiB 的请求体上限；配额取决于你的接口。

## 查看发送失败的结果

`refused_by_service` 表示服务器回应了，但没有按该服务要求的方式确认这次推送。hansa 只保留数字错误码，或最多 40 个字符的 ASCII 单词，从不保留响应里的消息文字或内容。这个 code 会出现在这条地址的上次结果和 `hansa status` 里。

`http_4xx` 通常要改地址、令牌或权限；`http_429` 表示触发频率限制；`http_5xx` 表示服务器出错。`tls`、`network`、`timeout` 和 `deadline` 表示连接或时限方面的问题。`redirect` 会被拒绝，以免密钥被发到别的地址：请粘贴服务文档里最终的 HTTPS 地址。重试规则见[提醒与投递规则（英文）](../releases.md#alerts)。
