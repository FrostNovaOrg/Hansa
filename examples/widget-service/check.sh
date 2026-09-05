#!/bin/sh
# Required checks for widget-service. Exit non-zero on any failure.
set -e
cd "$(dirname "$0")"
python3 -m unittest discover -s tests -q
python3 -c "import widget; assert widget.health()['status'] == 'ok'"
echo "check.sh: all green"
