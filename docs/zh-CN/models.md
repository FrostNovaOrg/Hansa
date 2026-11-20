# 模型选择

[English](../models.md)

这些是维护者截至 2026 年 11 月在自己的工作台上使用并推荐的模型和推理强度，模型会变，请留意这个日期。

## 协调员

| 模型 | 推理强度 |
|---|---|
| Claude Mythos 5.2 或 Claude Fable 5.2 | 规划时用 `xhigh`（把目标变成设计和任务），管理时用 `high`（安排任务、阅读结果、合并和发布） |
| Claude Opus 5.5 | `max` |

负责人在网页的“设置 → 协调员”中选择模型和推理强度，保存后下次启动协调员时生效。[网页设置（英文）](../desk.md#settings-on-the-web-page)

协调员的助手（侦察员、审阅员、操作员）不必用协调员的模型：负责人可以在同一处为它们选一个更便宜的模型（“助手的模型”，留空即用协调员的模型）。

## 工作智能体与核验

- Claude Opus 5.5
- GPT-6.2 Astra 和 GPT-6.2 Sol，都用 [Codex（英文）](../codex.md)
- DeepSeek V4.1 Flash 1017，使用它的[兼容 Anthropic API 的接口地址](getting-started.md#登录由负责人操作)

每条工作线都指定服务商，换一个模型核验可以发现原模型漏掉的问题。[有意用多家模型](../../README.zh-CN.md#有意用多家模型)

## 安全审计

除非你的账号已加入服务商的安全工作验证计划（Claude 对应 Anthropic 的 Cyber Verification Program，简称 CVP），否则维护者不建议在 hansa 里对你的项目做安全审计。没有这项验证，服务商的安全检查很可能会中止审计。
