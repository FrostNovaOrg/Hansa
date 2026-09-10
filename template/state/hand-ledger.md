# Hand ledger (append-only; written by `bellhop hand` and the PreToolUse gate, never by hand)
(one line per event, all starting `- `:
  `- opened: <time> | <N> min | <reason>`
  `- closed: <time> | <N> min used | <M> written[ | expired]`
  `- written: <time> | <tool> | <path> | <command head>`
  `- blocked: <time> | <tool> | <path> | <command head>`
A window is the declaration; this file is the record. They are separate on purpose.)
