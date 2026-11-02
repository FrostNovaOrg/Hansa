# Hansa Agents

[English](README.md)

**你说想要什么，批准要发出去的东西；一支写代码的智能体团队负责规划、开发、测试，并互相检查工作，全程在你掌控的机器上。**

你只跟一个智能体对话，也就是协调员。它把工作拆成任务，每件任务交给一个工作者（另一个智能体），工作者在自己的沙箱（一块隔离出来的空间）和自己的分支（你代码的一条独立副线）上工作。Hansa 提交结果（把改动存入版本记录）并运行你的测试；另一个工作者实际运行并核验这处改动，看它是否通过，这一步叫核验；然后才合并（并入你的代码）。发布只在你点头后才会进行，可以每次点头，也可以事先点头。

| | |
|---|---|
| **状态** | Alpha，`0.5.0a1`：设置、命令和文件都可能还会变。0.4 及以前，这个项目叫 Bellhop。 |
| **运行环境** | macOS 或 Linux，需要 Python 3.9 或更新版本、git 和 Claude Code；[`hansa doctor`](#快速开始) 会检查这台机器 |
| **使用方式** | `hansa` 菜单和命令；通过 SSH 隧道（从你的电脑连过去的私密通道）打开的网页 |
| **许可证** | Apache-2.0 |

## 适合谁

**适合：**

- 负责产品、不写代码、想要一支智能体团队的人。
- 手头的任务一个智能体会话装不下的开发者。

**不适合：**

- 交互式地设计功能：先在别处设计好，再把结果作为任务的输入交给 hansa。
- 晚上要合上的笔记本电脑。Hansa 适合放在一直开着的机器上，比如 Mac mini、家用服务器或 Linux 主机，你在任何地方都能通过 SSH 和 tmux（一种远程登录，和一个在你断开后仍继续运行的会话）连上它。

## 快速开始

```sh
curl -fsSL https://raw.githubusercontent.com/FrostNovaOrg/Hansa/main/install.sh | sh
hansa doctor
hansa init ~/shop/desk --repo ~/shop/repo
hansa
```

如果 shell 找不到 `hansa`，请添加安装程序打印的 `export PATH` 那一行。

`~/shop/repo` 是你项目的 git 仓库（存放代码和改动历史的地方），至少要有一次提交；`~/shop/desk` 是新的工作台，也就是存放你团队设置和任务的文件夹。其余内容见[上手指南](docs/getting-started.md)。

## 工作原理

工作线就是你代码的一份工作副本（它的工作树），并有自己的一套规则：用哪些模型服务商、能连哪些主机、跑哪些测试（它的检查）、怎么发布。

```mermaid
flowchart TB
    P(["你<br/>在网页上"])
    C["协调员<br/>工作台里的 Claude Code 会话"]
    subgraph S["hansa serve，一直运行"]
        Q["任务队列"]
        W["工作者：claude -p<br/>在工作线的工作树里，分支 task/t0001，<br/>在 Claude Code 的沙箱内"]
        K["hansa 提交改动<br/>并运行工作线的检查"]
    end
    B[("工作线的基础分支")]
    O(["已推送，已部署"])

    C -- "hansa add" --> Q
    Q -- "空闲的工作线，就绪的服务商" --> W
    W -- "报告" --> K
    K -- "发到协调员的会话" --> C
    C -- "hansa verify, hansa retry" --> Q
    C -- "hansa merge" --> B
    B -- "hansa release：等你" --> P
    P -- "发布：hansa 运行工作线的发布命令" --> O
```

1. **协调员把任务排进队列**：每件任务都是工作者能独立完成并自行检查的一处改动。
2. **`hansa serve`（那个一直运行的程序）启动任务**，放到一条空闲的工作线上：工作者在沙箱里做改动，然后报告。
3. **hansa 提交并检查。** 它亲自运行工作线的测试，所以结果不取决于工作者怎么说。
4. **协调员审查。** 如果是代码，另一个工作者会实际运行并核验这处改动，看它是否通过；然后合并，或者退回去。
5. **由你发布。** hansa 把改动合并进工作线的基础分支（它的工作最终落到的地方），然后停下。等你点头，或者对你事先批准的发布目标，它才运行工作线的发布命令，比如部署。

## 多个模型，是有意为之

一条工作线可以用 Claude Code 登录、任何兼容 Anthropic API 的接口地址（例如 DeepSeek 或 Kimi 的），在 macOS 上还可以通过 [`contrib/codex`](contrib/codex/README.md) 用 Codex 的命令行。一处改动可以换一个不是写它的模型来核验；一家模型服务商用到额度上限时，会把任务交给下一家。

## 它做过什么

Hansa 是用 Hansa 开发的。以下数据来自一个工作台，截至 2026 年 11 月 2 日的 60 天；你的数字会不一样。

| | |
|---|---|
| 合并的改动 | 345 |
| 没被退回就合并的 | 57% |
| 结论为“未通过”的核验 | 37% |
| 每处合并改动的成本，按官方标价 | US$8.65，全部包含在内（协调员、工作者和核验） |

用订阅的话，账单就是订阅费；这个数字是这些 token（模型用量的计量单位）按官方标价值多少钱。

## 你的 Claude Code，你的登录

Hansa 运行的是你自己安装的 Claude Code，登录用的是你自己的账号或密钥。它不包含、不修改、也不转售 Claude Code。工作者的用量计入你自己的套餐，受你所用模型服务商的条款约束；用得多，就会更早碰到套餐的限额。不要用订阅登录把工作台提供给别人使用：Anthropic 的使用政策不允许通过消费者套餐转发请求的服务。Hansa 是独立项目，与 Anthropic 和 OpenAI 均无隶属关系，也未获它们认可。

## 安全靠什么保证

- **工作者运行在操作系统的沙箱里。** 它们的命令只能写入工作线允许的位置，只能连接工作线列出的主机，也读不到工作台的密钥；你在工作线上允许的网页工具在这份名单之外运行。[工作者与沙箱](docs/sandbox.md)
- **协调员在明处工作。** 它不在沙箱里：每条尚未放行的命令，Claude Code 都会先问你；规则不让它碰密钥，也不让它直接改代码。禁止推送（把代码发到远程仓库）的规则只拦得住写明的形式，其余的就靠这一问。[谁改什么](docs/who-changes-what.md)
- **发布要等你。** 只有你点头，或者是你事先批准的发布目标，发布才会运行。它在沙箱外运行，用的是这个工作台所用账号的登录信息。[发布与提醒](docs/releases.md)
- **密钥不外露。** 它们存放在 `secrets/` 里，hansa 的账号可读，只发给模型服务商。hansa 会在密钥可能显示的地方把它遮住，也拒绝合并含密钥的改动，但看不到压缩文件里面。[工作台](docs/desk.md#where-a-desks-files-are)
- **网页默认是私密的。** 它只在本机上开放，它的数据和操作都需要 `hansa web` 打印的地址里的令牌（一串访问口令）。`hansa serve --host` 可以把它开放得更广，而拿到这个地址的人就能批准发布。[令牌](docs/who-changes-what.md#a-claude-code-sessions-identity-and-the-pages-token)

更多内容：[限制](docs/concepts.md#limits)。

## 文档

使用 hansa 不需要读它的代码。

| 文档 | 内容 |
|---|---|
| [`hansa/card.md`](hansa/card.md) | 协调员每一轮都需要知道的内容：工作怎么流转、每条命令、它可以改什么；其余内容由 `hansa help <topic>` 从 `docs/` 打印。`hansa help card` 打印这份文件，每个工作台都存有一份副本，名为 `CLAUDE.md`。 |
| [概念](docs/concepts.md) | 关键术语、工作台的文件、macOS 和 Linux、限制、命令、升级 |
| [上手指南](docs/getting-started.md) | 安装、登录、创建工作台、通过 SSH 打开它的网页，并完成第一个任务 |
| [SETUP.md](SETUP.md) | 写给智能体的说明，让它陪你装好 Hansa：对你的 Claude Code 说“读 SETUP.md，和我一起装好 Hansa” |
| [工作台怎么运转](docs/principles.md) | hansa 遵循的规则、靠什么执行、各自防范什么 |
| [谁改什么](docs/who-changes-what.md) | 你在网页上设置的内容和协调员用 `hansa` 设置的内容：工作线、提交信息和身份、对协调员始终关闭的部分 |
| [工作者与沙箱](docs/sandbox.md) | 工作者和核验任务能碰什么；git 的文件夹；沙箱试跑；写入清单和环境变量；允许清单及其网页工具；hansa 拒绝的路径；一次运行结束时 hansa 会停掉什么 |
| [任务怎么运行](docs/tasks.md) | 时限和续跑、连接断开后的重试、模型服务商的用量上限 |
| [发布与提醒](docs/releases.md) | 发布目标、用一次性代码答复、发布会运行的文件、停止发布、你的提醒命令 |
| [在 Linux 服务器上](docs/linux-server.md) | 协调员以 root 身份运行，工作台用普通账号；工作线的命令在 Linux 沙箱里会遇到什么 |
| [工作线里的 Java 和 Maven](docs/java-and-maven.md) | hansa 为沙箱里的 Maven 和 Java 做了哪些设置、这对构建脚本有什么影响（macOS 上的 `mktemp` 也包括在内）、项目和工作线需要什么 |
| [工作台：文件、密钥和笔记](docs/desk.md) | 密钥、协调员的权限规则和笔记、项目自己的规则、协调员会话略去的内容、可用空间的下限、工作台的文件会在哪里变大 |
| [CHANGELOG.md](CHANGELOG.md) | 每个版本的改动，包括 Bellhop 时期的 |
| [CONTRIBUTING.md](CONTRIBUTING.md) | 怎么修改 hansa，以及贡献内容的许可方式 |

## 参与贡献与许可证

要修改 hansa，请读 [CONTRIBUTING.md](CONTRIBUTING.md)。

[Apache-2.0](LICENSE)。Copyright (c) 2026 FrostNovaOrg.
