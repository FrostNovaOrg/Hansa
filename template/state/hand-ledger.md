# Hand ledger (append-only; written by `bellhop hand` and the PreToolUse gate, never by hand)
(one line per event, all starting `- `:
  `- opened: <time> | <N> min | <reason>`
  `- closed: <time> | <N> min used | <M> written[ | expired]`
  `- written: <time> | <tool> | <path> | <command head> | open-window <verb>`
  `- blocked: <time> | <tool> | <path> | <command head> | block-write <verb>`
  `- allow-read: <time> | <tool> | <path> | <command head>`
  `- role-refused: <time> | <tool> | <path> | <command head> | <why>`
  `- role-open: <time> | <tool> | <path> | <command head> | <why>`
The three gate decisions are `allow-read`, `block-write` (`blocked`) and
`open-window` (`written`); the `role-` rows are the coordinator role gate.
A window is the declaration; this file is the record. They are separate on purpose.)
