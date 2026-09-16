---
name: brief
description: The principal's page - what is waiting for them, the queue, today's counts, the money, the last decisions. Use at the configured times, and whenever the principal asks what is waiting, whether the queue is empty, or what they need to do.
---

# /bellhop:brief

`BH="${CLAUDE_PLUGIN_ROOT}/bin/bellhop"`.

1. **Never answer a status question from memory.** "What is waiting for me", "is the
   queue empty", "anything I need to do" - run `$BH brief` and paste what it prints.
   An answer from a board you read six turns ago and an answer from the files look
   identical in a chat window, and only one of them is still true. On the desk this
   page comes from, the principal asked that question again and again.
2. Send it unasked at the times in the config key `brief_times`: `$BH brief --send`.
   With `alert_cmd` empty the page is printed and nothing is recorded; with one set,
   only a zero exit records `state/.brief-sent`. A send that did not send exits 5.
3. `--bytes N` overrides `brief_bytes` for one run. Over the cap whole sections are
   dropped in a fixed order - decisions, money, counts, returns, queue - and the last
   line says which went. "Waiting for you" is never dropped.
4. `n/a` in the money line means `state/readings.md` has no line for today. That is a
   reading, not a gap to fill in: run `$BH readings --transcripts <path or dir>` if the
   number is wanted, and never estimate it.
5. If "waiting for you" is missing something, do not narrate it. Put it in the file:
   `$BH state todo add --text "<what> - why only you: <reason>"`, then run the page
   again and paste that.
6. Do not paste `$BH board` to the principal. The board is the coordinator's page - it
   answers what a turn starts from, not what the principal has to act on.

The sections, and the file each number is read from: `docs/brief.md`.
