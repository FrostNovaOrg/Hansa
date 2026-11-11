# 上手指南

[English](../getting-started.md)

在一台一直开着的 Mac 或 Linux 机器上运行 hansa。你是**负责人**，登录、密钥和设置由你掌管；协调员负责为工作智能体安排任务。如果你用 root 账号操作，请先按[在 Linux 服务器上（英文）](../linux-server.md#the-coordinator-as-root-the-desk-as-an-ordinary-account)的说明设置。

## 安装所需工具

把这些工具装在工作副本的文件夹以外。启动 `hansa serve` 时，工作台所在账号的 `PATH`（查找命令的位置）里要能找到它们：

| 工具 | 版本 |
|---|---|
| Python | 3.9 或更新版本；只用标准库，无需编译安装其他东西 |
| Git | 必须安装，没有强制的最低版本。Linux 上建议用 Git 2.32 以上：隐藏沙箱占位文件时能保留全局配置（[详情（英文）](../linux-server.md#what-a-lanes-commands-meet-in-linuxs-sandbox)）。 |
| [Claude Code（英文）](https://code.claude.com/docs/en/overview) | 协调员插件需要 2.1.306 以上；项目的自动测试使用 2.1.307 |
| Node | srt 需要 22.12.0 以上；项目的自动测试使用 24.22.0 |
| srt (`@anthropic-ai/sandbox-runtime`) | 0.0.79 |
| tmux | 建议安装；用 `hansa up` 启动协调员时必须有它，没有固定版本要求 |

装好 Node 和 npm 后，运行：

```sh
npm install -g @anthropic-ai/claude-code@2.1.307 @anthropic-ai/sandbox-runtime@0.0.79
```

[工具与升级（英文）](../sandbox.md#tools-and-upgrades)记录了所用版本和升级时要做的检查。

工作智能体需要 Claude Code 的操作系统沙箱，macOS 自带所需支持。Linux 上要安装 `bubblewrap` 和 `socat`（Debian/Ubuntu 上运行 `sudo apt-get install bubblewrap socat tmux`），并确保普通账号能使用用户命名空间。Ubuntu 24.04 以上版本的 AppArmor 限制可能拦住它。请按你的发行版，参照 [Claude Code 的 Linux 沙箱设置（英文）](https://code.claude.com/docs/en/sandboxing#set-up-linux-and-wsl2)处理。

## 安装 hansa

装好 Git 和 Python 3.9 以上版本后，运行：

```sh
curl -fsSL https://raw.githubusercontent.com/FrostNovaOrg/Hansa/main/install.sh | sh
```

安装程序把项目下载到 `~/.local/share/hansa`，在 `~/.local/bin/hansa` 建立命令链接，不需要管理员权限，也不会修改你的 shell 配置文件。
安装程序和 `hansa doctor` 的中文输出优先引用已安装的中文上手指南，缺少中文文件时使用英文指南。

要更新时，先暂停工作台，等正在运行的工作结束，再运行一次安装程序，然后重启并恢复工作（[升级步骤](concepts.md#升级与退回旧版)）。要卸载 hansa，运行下面的命令；已有工作台会留在原处：

```sh
rm -rf ~/.local/share/hansa ~/.local/bin/hansa
```

装好后运行 `hansa doctor`；如果有智能体帮你安装，也让它先运行这个命令。

```sh
hansa doctor
```

这个检查不需要工作台，也不改设置：它检查工具和沙箱能否运行，不读取凭据，不保留登录命令的输出，最后会删掉检查用的临时沙箱文件夹。
在负责人按下面的说明登录前，登录这一项会显示未通过；只用接口和密钥的配置可以忽略这一项。处理完发现的问题后，再运行一次检查。

每一行都列出检查结果、要求和处理办法；提示项不算失败。退出码 `0` 表示没有未通过的项，`1` 表示有未通过的项。加上 `--json` 会输出 `{"ok": ..., "rows": [...]}`，其中每行包含 `name`、`state`、`found`、`need`、`fix`；状态为 `ok`、`note`、`fails` 或 `skipped`。`--lang zh|en` 和 `hansa menu` 一样，用来选择语言。
检查中的命令超过 15 秒就会停止，之后会短暂等待，清理子进程留下的输出连接。

## 登录（由负责人操作）

如果用订阅，请以工作台所在账号运行 `claude auth login`；新工作台的 `claude` 服务商会使用这个登录。
也可以在下面介绍的网页上设置兼容 Anthropic API 的接口地址和密钥，这种方式不需要订阅登录。凭据只由负责人处理。

## 先试试示例

运行 `hansa example`，生成一件示例任务，用你自己的登录试一试。这个命令本身不会启动任何服务，也不会调用模型。

## 创建并启动工作台

对于新项目或空文件夹，`hansa init` 会创建所需的文件夹和 Git 仓库，并做一次不含文件的初始提交。
`hansa lane add --repo` 也会这样处理。如果文件夹里已经有文件，请先自行选择要纳入 Git 的文件并提交。

```sh
hansa init ~/shop/desk --repo ~/shop/repo
cd ~/shop/desk
```

文件夹名尽量简单。如果某个名字被拒绝，见 [hansa 拒绝的路径（英文）](../sandbox.md#paths-hansa-refuses)。
工作台需要一个新的或空的文件夹：里面已有文件时，`hansa init` 会拒绝；工作台若位于某个 Git 仓库的工作区内，它会把工作台的路径写进该仓库的 `info/exclude`，这样在那里运行 `git add` 不会把工作台收进去。

初始化会以项目当前检出的分支为基础创建工作线 `a`，并把尚未启动的工作台登记到这个账号下，所以打开 `hansa` 菜单就能看到它。如果已经用订阅登录，`hansa up` 会先启动服务，再在 tmux 里启动协调员。

如果只用接口和密钥，先让 `hansa serve` 保持运行，在另一个终端里按下面的说明配置网页，再运行 `hansa --desk ~/shop/desk up`。经 SSH 操作时，用 tmux 保持服务运行（先运行 `tmux new -s shop-server`，再运行 `hansa serve`）。

用协调员启动时打印的 `attach:` 命令进入会话。Claude Code 问你是否信任这个文件夹时，选“是”，它获准使用的命令才能运行。

**没有 tmux 时，只会启动服务。** 可以在另一个终端里进入 `~/shop/desk`，运行 `hansa coordinator`；也可以装好 tmux 后重新运行 `hansa up`。只运行一个协调员；`hansa up` 不会重启已经运行的进程。

## 打开负责人的网页

在你的终端里进入工作台文件夹，运行：

```sh
hansa web
```

打开它打印的完整地址，包括 `#t=...`，并保持终端开着。
其中的令牌允许访问网页，也允许批准发布。
请从普通 shell 启动服务和网页：在 Claude Code 会话里，`hansa web` 会拒绝显示令牌。

在 **Settings → Providers（设置 → 服务商）** 里添加接口地址、默认模型和思考力度；用 **Save settings（保存设置）** 保存设置，密钥要另点 **Save key（保存密钥）**。在 **Coordinator（协调员）** 里，先选好服务商和模型再启动协调员；如果它已经在运行，保存后要重启。
在 **Prices（价格）** 里，按每百万 token 的美元价格填写。服务商的新设置用于之后的新运行；修改价格还会重新计算过去的用量费用。`hansa ping` 会发一个很小的请求来检查所有服务商；网页上也有检查服务商的按钮。

## 完成第一件任务

对协调员说：

> 看看工作线 a，用已经配置好的服务商。设置一条有用的检查命令，并列出下载依赖需要访问的主机。先试运行工作线，再做一处小改进，要能测试。审查并检查结果，必要时另做核验，确认站得住后合并。不要发布。

如果是空项目，可以让它写一个 Python 函数和 unittest 测试。
协调员会设置检查命令，例如 `hansa lane set a check "python3 -m unittest discover"`，并用 `hansa lane set a network HOST ...` 设置下载所需的主机。默认的网络名单是空的，要逐个列出所需的软件包仓库或下载主机，见[工作线的网络名单（英文）](../sandbox.md#a-lanes-network-list)。

`hansa lane try a` 会在工作线的沙箱里运行准备步骤和检查。确认它通过了，并且 `hansa explain server` 显示服务的沙箱探测结果为 `hansa serve tried srt` 和 `it ran`。工作智能体自己的沙箱探测也必须通过。
如果没有 srt，准备步骤和检查会显示 **could not run**，表示机器的运行环境有问题，任务本身还没有失败。如果配置好的准备步骤无法运行，工作智能体就不会启动。把工具装到服务能通过 `PATH` 找到的位置，重启服务，合并前再检查一次（`hansa merge ID --recheck`）。

在网页的 Status（状态）里查看进展。协调员会读 `hansa show ID` 和 `hansa diff ID`；工作智能体的改动由 hansa 提交并检查。任务经过审查、合并进工作线的基础分支后，就算完成。

## 通过 SSH 使用

先打开那台运行 hansa 的机器的远程登录：macOS 上在“系统设置”→“共享”里打开“远程登录”；Linux 上要安装并启动 SSH 服务。在那台机器的终端里运行 `whoami` 可以得到账号名；机器名或地址可以看网络设置，或者问服务器提供商。下面的命令里，`HOST` 指 `账号@主机名或地址`。

机器重启后，在你自己的电脑上打开工作台菜单，同时转发所需的网页端口（这里是 8765 和 8766）：

```sh
ssh -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -o ExitOnForwardFailure=yes -t -L 8765:127.0.0.1:8765 -L 8766:127.0.0.1:8766 HOST '"$HOME/.local/bin/hansa"'
```

用空格键选择工作台，按 `s` 启动，按 Enter 打开协调员，或按 `w` 查看网页地址。先按 Ctrl+B、再按 D 可以离开协调员会话；Ctrl+C 退出网页连接。在你选择操作前，什么都不会启动。按 `q` 退出菜单，正在进行的工作会继续运行。

在服务器上安装 tmux，这样 SSH 断开后，`hansa up` 启动的协调员也能继续运行。
重新连接时，用它打印的 attach 命令，包括其中的 `-L` 或 `-S`。`hansa status` 会说明会话情况；如果会话已停止，`hansa up` 会把它重新启动，接着原来的对话继续。

网页端口最初是 8765，可以用 `hansa status`、`hansa explain server`，或查看服务器上的 `.hansa/web-port` 来确认实际端口。
如果实际端口是 **8766**，在你自己的电脑上运行：

```sh
ssh -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -o ExitOnForwardFailure=yes -L 127.0.0.1:8766:127.0.0.1:8766 my-server '"$HOME/.local/bin/hansa" --desk "$HOME/shop/desk" web'
```

把每一处 `8766` 都换成实际端口。在本机打开打印的地址，并保持隧道所在的终端开着；关掉它就不能访问网页了。保留服务器默认的 `127.0.0.1` 监听地址，除了 SSH，不要再开放其他入站网络端口。

`ServerAliveInterval=30` 表示每 30 秒检查一次连接，`ServerAliveCountMax=3` 表示连续三次没有回应就断开，`ExitOnForwardFailure=yes` 表示端口转发失败就退出。如果希望隧道断开后自动重连，可以在本机安装 `autossh`，另开一个终端运行：

```sh
autossh -M 0 -N -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -o ExitOnForwardFailure=yes -L 127.0.0.1:8766:127.0.0.1:8766 my-server
```

## 遇到问题时

| 看到的情况 | 原因 | 处理办法 |
|---|---|---|
| `No module named hansa` | 无法读取项目文件 | 确保工作台所在账号能读取 hansa 的项目文件。 |
| 初始化时拒绝仓库 | 有文件尚未提交，或指定的文件夹不存在且位于已有仓库内 | 先提交文件，或改用已有仓库的路径。 |
| 服务在运行，但没有协调员 | 没有 tmux | 安装 tmux 后重新运行 `hansa up`，或运行 `hansa coordinator`。 |
| 协调员无法运行获准的命令 | 没有信任文件夹 | Claude Code 问是否信任这个文件夹时，选“是”。 |
| 登录失败 | 账号或服务商不对 | 由负责人以工作台所在账号登录，或在网页上保存接口地址和密钥。 |
| `could not run`、`srt MISSING` / `srt CANNOT RUN` | 缺少 srt、Node，或沙箱无法运行 | 检查版本和服务的 PATH，补齐 Linux 所需条件，重启服务后重新检查。 |
| `bwrap:` / 任务被暂缓 | 沙箱无法启动，或拒绝了某项设置 | 阅读 `hansa status`，处理用户命名空间、AppArmor 或提示中指出的设置，再重启服务。 |
| 下载被拒绝 | 工作线的网络名单为空或不全 | 让协调员加上所需的主机，再试运行工作线。 |
| 网页打不开 / 密钥被拒绝 | 端口不对、隧道关闭、缺少令牌，或在智能体的终端里操作 | 确认实际端口，在你自己的终端里重新打开隧道，并使用 `hansa web` 打印的完整地址。 |
