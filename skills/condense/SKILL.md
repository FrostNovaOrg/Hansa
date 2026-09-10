---
name: condense
description: The weekly condensation - recount the self-catch table, retire hot rules that fired again, settle stale debts, list rule and knowledge files nobody has touched, and write one ledger entry for the week. Use once a week, after /bellhop:readings.
---

# /bellhop:condense

`BH="${CLAUDE_PLUGIN_ROOT}/bin/bellhop"`. Everything that grows gets read less. This is
the hour that shrinks it. One pass, in this order, and nothing is deleted by hand.

1. `$BH state selfcatch table`. The counts are recomputed from the rows, never
   remembered. Note every root cause at or above `hot_rule_promote`, and every root
   cause that already has a hot rule.
2. For each hot rule whose root cause has fired again since it was promoted:
   `$BH state hotrules retire --root <root> --reason "fired again <date>; the rule did
   not hold"`. A rule that is not working and a rule nobody has tested look the same in
   a list of ten, and the ten slots are the most expensive lines in the session prompt.
3. Debts: `$BH state debts settle --match "<text>"` for each one that is done, and
   `$BH state debts set --match "<text>" --text "<what is actually owed now>"` for each
   one whose wording has drifted. A debt nobody can act on is not a debt.
4. List the rule and knowledge files nobody has touched, output only:

   ```sh
   T="${TMPDIR:-/tmp}/bellhop-touched"
   git log --since="8 weeks ago" --name-only --pretty=format: -- rules/ docs/ | sort -u > "$T"
   git ls-files rules/ docs/ | sort | comm -23 - "$T"
   ```

   Do not delete anything here. For each file, either put one line on the own list
   (`$BH state own add --text "<file>: read and decide - kept or retired"`) or say in
   the week's entry that it was read and kept.
5. `$BH readings` for the week, if `/bellhop:readings` did not already run it.
6. One entry, the whole week in it: `$BH state ledger add --file -` with what was
   retired and why, what was settled, which files are stale, and the single number in
   the readings that moved most - with one sentence on the likely cause.
