#!/usr/bin/env bash
# Download the newest event log from GitHub Actions and print a report.
# Usage: scripts/report.sh [DAYS]   (needs the gh CLI, logged in)
set -euo pipefail
cd "$(dirname "$0")/.."
run=$(gh run list -w monitor.yml -s completed -L 1 --json databaseId -q '.[0].databaseId')
dir=$(mktemp -d)
gh run download "$run" -n "events-$run" -D "$dir"
PYTHONPATH=src python3 -m dian_monitor --report "$dir/events.jsonl" ${1:+--days "$1"}
