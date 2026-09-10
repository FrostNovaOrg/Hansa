#!/bin/sh
# Runs the whole Bellhop loop in dry-run mode: no model is called, a stub worker writes the receipt.
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/.." && pwd)
BH="$ROOT/bin/bellhop"
WORK=${BELLHOP_DEMO_DIR:-$(mktemp -d "${TMPDIR:-/tmp}/bellhop-demo.XXXXXX")}
mkdir -p "$WORK"
echo "demo workspace: $WORK"

# 1. A fictional product repository.
cp -R "$HERE/widget-service" "$WORK/widget-service"
git -C "$WORK/widget-service" init -q -b main
git -C "$WORK/widget-service" -c user.name=demo -c user.email=demo@example.invalid add -A
git -C "$WORK/widget-service" -c user.name=demo -c user.email=demo@example.invalid commit -q -m "widget-service: initial"
BASE=$(git -C "$WORK/widget-service" rev-parse --short HEAD)

# 2. A desk from the template, with the api lane pointed at the repository and a stub worker.
"$BH" init "$WORK/desk" >/dev/null
cd "$WORK/desk"
python3 - "$ROOT" <<'PY'
import json, sys
root = sys.argv[1]
cfg = json.load(open("bellhop.json"))
cfg["lanes"]["api"]["repo"] = "../widget-service"
cfg["lanes"]["api"]["worker_cmd"] = f"python3 {root}/bellhop/stubworker.py"
# The stub verifier answers in the shape the checker wants only when it is asked to:
# without `--good` it writes prose, `bellhop accept` retries once and then stalls on
# `verdict format`, which is what 35 of 71 shadow runs did. Try it without the switch.
cfg["lanes"]["api"]["accept_cmd"] = f"python3 {root}/bellhop/stubaccept.py --good"
cfg["sample_rate"] = 1
json.dump(cfg, open("bellhop.json", "w"), indent=2)
PY
cp "$HERE/lane-rules-api.md" rules/lanes/api.md
git -c user.name=demo -c user.email=demo@example.invalid add -A && git -c user.name=demo -c user.email=demo@example.invalid commit -q -m "[desk] configure api lane"

# 3. The principal decides; the coordinator records it verbatim.
"$BH" decide "Ship the health endpoint this week; patch releases do not need my sign-off." --scope "widget-service" --standing
"$BH" seal >/dev/null

# 4. Dispatch: write a packet, ring the bell.
PKT=$("$BH" packet new --lane api --title "Add health check note" --base "$BASE")
python3 - "$PKT" <<'PY'
import sys, re
p = sys.argv[1]
s = open(p).read()
s = s.replace("- replace me with a checkable statement", "- ./check.sh prints 'check.sh: all green'\n  - a file named bellhop-stub-001.txt exists in the working tree")
s = s.replace("Say it in plain words. One task. Name the files or areas if you know them.", "Add a short note file describing the health check. Keep check.sh green.")
open(p, "w").write(s)
PY
"$BH" card --ready yes --next "001 add health check note" --lane api --reason "" >/dev/null
"$BH" ring "$PKT"

# 5. The watchdog answers the bell (stub worker), archives the receipt.
"$BH" watch --once
echo "--- receipt ---"; sed -n '1,14p' receipts/001-add-health-check-note.md; echo "..."; tail -2 receipts/001-add-health-check-note.md

# 6. Intake: sample, accept, ledger, card, merge.
"$BH" sample 001 || true
"$BH" accept 001
"$BH" ledger add --title "received 001 add health check note" --ref 001 --body "Receipt read; acceptance PASS (stub); merged." >/dev/null
"$BH" card --ready no --next "" --lane api --reason "queue empty; waiting on the principal" >/dev/null
"$BH" merge 001 --verdict "PASS (stub)"

# 7. Sentinel, readings, close.
"$BH" sentinel --no-wake || true
"$BH" readings >/dev/null
"$BH" close
echo
echo "desk git log:"; git log --oneline | head -12
echo
echo "done. workspace kept at $WORK"
