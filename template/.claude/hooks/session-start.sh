#!/bin/sh
# Prints the coordinator's short reading list at session start.
echo "== dispatch card =="; sed -n '1,12p' state/next.md 2>/dev/null
echo; echo "== in flight =="; grep '^- ' state/in-flight.md 2>/dev/null
echo; echo "== debts (open) =="; grep '^- ' state/debts.md 2>/dev/null | grep -v '^- ~~' | head -20
echo; echo "== sentinel =="; cat state/.sentinel/status.txt 2>/dev/null || echo "no sentinel status yet"
echo; echo "== last decisions =="; grep '^## ' state/decisions.md 2>/dev/null | tail -3
