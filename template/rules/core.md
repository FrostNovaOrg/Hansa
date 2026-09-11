# Core rules

These are the rules every session on this desk reads. Keep this file under 40 lines; a rule a script can enforce belongs in a script, not here.

1. **Files, not chat.** If it is not in a file in this repository, it did not happen. Decisions go in `state/decisions.md`, verbatim.
2. **One task, one session, one receipt.** A worker does the packet it was given, writes its receipt, and exits. It does not pick up other work.
3. **Read the short list, in order:** `state/next.md`, `state/in-flight.md`, `state/debts.md`, this file, the lane's file, the packet. Nothing else unless the packet says so.
4. **Workers do not push, tag, deploy, or touch the desk's git.** Workers commit on their lane branch; the coordinator merges through `bellhop close-task`.
5. **Stop and ask when the packet contradicts the code or itself.** Write the contradiction in the receipt under "Doubts" and stop; do not invent a resolution.
6. **Report numbers, not adjectives.** Every acceptance item in the receipt gets the command that checked it and what it printed.
7. **Say what you did not do.** A receipt lists what was left out and why.
8. **Helpers do the coordinator's own work only** (research, drafts, walkthroughs, acceptance reads). A result that must be archived is a worker's task.
9. **Append, never rewrite.** Ledger entries and decisions are added, not edited. Corrections are new entries that cite the old.
10. **Do not wait in a loop.** After ringing a bell, end the turn; the sentinel wakes the coordinator.
11. **Sampling is by script.** `bellhop sample <id>` selects additional review. Every merge still needs a passing `bellhop accept` verdict.
12. **State files have one writer.** Use `bellhop state` or built-in dispatch/close chains for `state/`; never an editor, never a script written for the occasion. It keeps the previous version, replaces one anchored line at a time, and echoes what it wrote.
13. **Reseal in the same commit.** A deliberate change to scripts or frozen documents comes with `bellhop seal`.
