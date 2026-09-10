---
name: release
description: Run the desk's release steps in order, stopping at the first failure and resuming from it. Use when a version is ready to ship, and when a stopped release has to be picked up where it stopped.
---

# /bellhop:release

`BH="${CLAUDE_PLUGIN_ROOT}/bin/bellhop"`. **The steps live in `bellhop.json` under
`release_steps`, never in this skill.** A step written into a skill is a step a model
paraphrases under load; a step in config is the same command every time.

1. `$BH release <version> --dry-run`. It prints the commands, runs nothing, writes
   nothing. Read them before any of them runs.
2. `$BH release <version>`. Each step runs from the desk root with `{version}`
   substituted. The run stops at the first non-zero exit and prints
   `🛑 stopped at: <step>` on its own line.
3. If it stopped, fix the cause and do **not** re-run from the beginning:
   `$BH release <version> --from <step>` picks up there and counts the earlier steps as
   done. Re-running from the top repeats the steps that are not safe to repeat - a tag,
   a publish. Progress is in `state/.release-<version>.json`.
4. A stop-dispatch order refuses the release. Lift it (`$BH stop-dispatch lift`) or
   leave the release for later; `release_ignores_stop` is a standing desk setting, not
   a way past one order.
5. One ledger entry when it is done: `$BH state ledger add --file -` with the version,
   the steps that ran, and anything that stopped on the way.

## The six steps a desk fills in

`template/bellhop.example.json` ships them as placeholders that refuse, so an
unconfigured release stops at step 1 instead of tagging something.

| Step | What it has to prove |
|---|---|
| `preflight` | Tree clean, version bumped, changelog section present, tag not already taken |
| `check` | The lane's full check, from a clean tree, not the last cached run |
| `tag` | An annotated tag on the commit that was checked; never a force, never an amend |
| `package` | The artefact builds from the tag, and its build info names that commit |
| `publish` | The artefact reaches where it is served from and reads back byte-identical |
| `verify` | The deployed thing answers, at the version shipped, with no errors since start |

Why this shape, and the numbers behind it: `docs/release.md`.
