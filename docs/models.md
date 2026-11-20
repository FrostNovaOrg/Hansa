# Choosing models

[简体中文](zh-CN/models.md)

These are the models and effort levels the maintainers use on their own desks and suggest as of November 2026, so keep the date in mind as models change.

## Coordinator

| Model | Effort |
|---|---|
| Claude Mythos 5.2 or Claude Fable 5.2 | `xhigh` for planning (turning a goal into a design and tasks), `high` for managing (queuing tasks, reading results, merging and releasing) |
| Claude Opus 5.5 | `max` |

The principal sets the model and effort under Settings → Coordinator on the web page, and saved changes take effect the next time the coordinator starts. [Settings on the web page](desk.md#settings-on-the-web-page)

The coordinator's helpers (scout, reviewer, operator) need not run on the coordinator's model: the principal can give them a cheaper one under the same settings (Helpers' model; empty means the coordinator's model).

## Workers and verifiers

- Claude Opus 5.5
- GPT-6.2 Astra and GPT-6.2 Sol, both through [Codex](codex.md)
- DeepSeek V4.1 Flash 1017, through its [Anthropic-compatible endpoint](getting-started.md#log-in-the-principals-step)

Each lane names its providers, and verifying with a different model can catch things the first model missed. [Several models, on purpose](../README.md#several-models-on-purpose)

## Security audits

The maintainers do not suggest running a security audit of your project in hansa unless your account is enrolled in the provider's verification programme for security work (Anthropic's Cyber Verification Program, or CVP, for Claude). Without that verification, the provider's safety checks are likely to stop the run.
