# Dispatch card

ready: no
next: 
lane: 
reason: desk just initialised; no packets yet
queue: 0
refreshed: 1970-01-01T00:00:00+0000
by: template

---
Fixed keys above; the sentinel reads them with `^key: `. Refresh with `bellhop card`.
- `ready: yes` means a packet is written and can be rung now.
- `ready: no` must say in `reason` what we are waiting on and who wakes us.
- The card must be newer than the last receipt or the sentinel goes red.
