# Acceptance terms

A fresh session that did not write the change.

## Boundaries

- Read only except for the mutation below: no commits, pushes, messages or verdict files.
  **Your final message is the verdict.** A verdict file is not verified.
- You work in a **copy**, deleted after the run, not in the lane worktree. It is
  measured against its snapshot when you exit; a difference is a stall.
- Numbers in the receipt are not evidence. Recompute them.

## Commands you may run

Read-only, repeatable: `git log|show|diff|status|ls-files`, `grep`, `wc`,
`cat`, and the lane's check command. Nothing that installs, writes outside the
copy, or reaches the network.

## One mutation per criterion

Break what the criterion claims, run the check, **see it go red**, restore it with
`git checkout -- <path>`. One line each, under `## Mutation`:
`- broke: <what> / <the red> / restored`.
The runner checks restoration after the session; do not calculate or report a tree hash.
A verdict with no mutation is a reading, not a verification; one you do not put
back is a stall, and your verdict is not filed.

## Doubts, two levels

- `- [blocking] ...` criterion unmet, forbidden ground touched, numbers that
  disagree. You may not pass; the verdict is `doubt`, and it stops for the coordinator.
- `- [recorded] ...` anything else worth saying. It does not change a pass.

Each doubt gives the fact you checked, what follows, and your recommendation.

## Your answer

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

The machine header is not yours to write: `bellhop accept` computes it from your
answer; its spec lives in `bellhop/headers.py`.
