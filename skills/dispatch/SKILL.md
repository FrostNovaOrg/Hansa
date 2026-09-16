---
name: dispatch
description: Write a packet body and dispatch it to a lane in one command. Use when there is a ready item and an idle lane.
---

# /bellhop:dispatch

`BH="${CLAUDE_PLUGIN_ROOT}/bin/bellhop"`. Three steps, not five.

1. **Write the body** to a scratch file: `## What to do`, `## Do not touch`, `## Acceptance`
   (one `- ` bullet per check a command can run). No front matter — the command writes it.
   Use an existing relevant reading; if the change touches code you have not read this
   session, ask the `scout` subagent for a brief. Do not read the repository yourself —
   the packet carries what the worker needs.
   Keep criteria self-contained. An optional `## Required context` section carries necessary
   original source text and its provenance; it survives acceptance trimming. A link alone
   does not copy an external document. Do not create a context section when criteria suffice.
2. `$BH dispatch --lane <lane> --title "<six words>" --body <file> --inflight "<one sentence>"`
   — add `--next "<what follows>"` to refresh the card, `--demo` to look without writing.
3. **Read the four lines it prints**: packet path, bell, the in-flight row as the file has it,
   the card's `next`. Then **end your turn.** The watchdog answers the bell; the sentinel or the
   receipt wakes you. Never poll a log in a loop.

The last line is `✅ dispatched <id> <bell>` or `🛑 stopped at: <step> — <reason>`. If it
stopped, fix that step; nothing after it ran. Exit 2 refused (nothing written), 3 the bell was
taken, 4 part-way (the echo says what to finish by hand). See [dispatch.md](../../docs/dispatch.md).

After dispatch, keep that packet unchanged. The scripts check its identity automatically;
follow a refusal's recovery reason. Use `$BH accept status <id>` only when more diagnosis is needed.

## Writing the body: seven rules

1. **Verify every identifier yourself** before it goes in — grep the tree under test for each
   method, key and path. A scout's brief can name a symbol that is not there, and a worker
   handed a wrong name does not stop: it rewrites the task around the name it can find.
2. **Line numbers are signposts, never the target.** Give the identifier beside every line
   number; lines move between the brief and the worker's turn.
3. **Criteria count items, not lines.** "three entries" survives reformatting; "the grep prints
   1" goes red the day one declaration wraps onto two lines.
4. **Quote the source, never a paraphrase.** Open the original lines before citing them: a
   summary of a summary is where a packet starts contradicting what it describes.
5. **One packet, one thing.** Two tasks in one body come back as one receipt, where the half
   that failed and the half nobody reached read exactly the same.
6. **Under `packet_bytes`** (3,000 by default) for a lane packet. Detail goes in the body;
   acceptance stays short enough to check.
7. **Never type a base, an id, or a receipt path.** `bellhop dispatch` reads the base from the
   lane at dispatch time and takes the id from the desk counter. A base two commits old
   produces a receipt whose diff describes work nobody did.

Say in the body that a name which does not match the tree means stop and ask, not work around
it. `--allow <pattern>` waives one forbidden command for one packet and stores nothing.
