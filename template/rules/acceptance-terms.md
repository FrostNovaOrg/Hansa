# Acceptance terms

A fresh read-only session that did not write the change.

## Boundaries

- Read only: no edits, commits, pushes, messages. The one file you write is the
  verdict, at the path the prompt names. **Written anywhere else is not verified.**
- Stay in the lane worktree the prompt names. Run no build that writes outside it:
  a build that collides with the worker's own leaves a red that is yours, and
  "the change is red" and "you made it red" look the same in a verdict.
- Numbers in the receipt are not evidence. Recompute them.

## Commands you may run

Read-only and repeatable: `git log|show|diff|ls-files`, `grep`, `wc`, `shasum`,
`cat`, and the lane's test command with a targeted selector. Nothing that
installs, writes outside the tree, or reaches the network.

## One mutation per criterion

Break what the criterion claims, run the check, **see it go red**, restore, and
confirm the tree hash is the one you started from. One line each, under
`## Mutation`: `- broke: <what> / <the red> / restored <hash>`.
A verdict with no mutation is a reading, not a verification.

## Doubts, two levels

- `- [blocking] ...` criterion unmet, forbidden ground touched, numbers that
  disagree. You may not pass; the verdict is `doubt` and it stops for the coordinator.
- `- [recorded] ...` anything else worth saying. It does not change a pass.

Each doubt gives the fact you checked, what follows from it, and your recommendation.

## The file

Eight sections, in this order:
`## Task` `## Criteria` `## Mutation` `## Extra findings` `## Doubts`
`## Hygiene` `## Recommendation` `## Verdict`

Criteria are `- [x]` / `- [ ]` items, one per acceptance item. An empty section
says `none`. The last non-empty line is one of:

```
VERDICT: pass
VERDICT: return — <one sentence>
VERDICT: doubt — <one sentence>
```

The machine header at the top is not yours to write: `bellhop acceptance-header`
writes it from your body afterwards, and its spec lives in `bellhop/headers.py`.
