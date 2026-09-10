# Principal intake (append-only; written by the UserPromptSubmit hook, never by hand)
(one entry per message: a `## Intake N (YYYY-MM-DD HH:MM)` heading, then the words verbatim.
N is assigned by the script as the largest existing number plus one, never as the entry count,
so a removed entry never causes a number to be issued twice. `bellhop intake list` and
`bellhop intake show N` read it; nothing else writes it.)
