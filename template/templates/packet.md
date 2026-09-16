---
id: {{id}}
lane: {{lane}}
title: {{title}}
base: {{base}}
budget_minutes: {{budget_minutes}}
acceptance:
{{acceptance}}
---
## What to do

Say it in plain words. One task. Name the files or areas if you know them.

## Do not touch

List anything out of bounds (other modules, config, CI). This heading and `## Forbids`
are the two spellings that are carried verbatim into the acceptance pack and into a
`--return` packet; any other wording is ordinary prose. Same for `## Required context`,
if the task needs source excerpts quoted into it.

## Acceptance

- one line per check a command can run; `bellhop dispatch` lifts these into the front matter
- every line says how it goes red: `- <criterion> — breaks when: <what makes it red>`
- a criterion with no `breaks when:` clause is refused at dispatch — it has passed before
  the worker starts, and acceptance cannot tell it from work nobody did

## How to report

Write the receipt from `templates/receipt.md`. For each acceptance item: the command you ran and what it printed.

<!--
The three body sections above (What to do / Do not touch / Acceptance) are all `bellhop
dispatch` needs: write them to a file with no front matter and run

    bellhop dispatch --lane <lane> --title "<short>" --body <file>

which computes the id, reads the base from the lane, and adds this head and a fixed tail.
`bellhop packet new` writes this whole file instead, front matter included, for the times a
packet has to be assembled by hand -- then the acceptance items go in the front matter above
and the section here is the human-readable copy of them.
-->
