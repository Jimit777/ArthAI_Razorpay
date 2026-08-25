#!/bin/bash
# The demo, in one command. Run `./demo.sh` and nothing else.
#
# Exists because "zsh: command not found: python" is not a thing to discover
# while three judges watch. macOS ships python3, not python, and the
# dependencies live in the venv - so this activates it and checks before
# anything is on screen.
set -e
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  echo "No .venv here. Run:  python3 -m venv .venv && .venv/bin/pip install anthropic pydantic pytest"
  exit 1
fi
source .venv/bin/activate

if [ ! -f demo_run.json ]; then
  echo "demo_run.json is missing - that is the recorded run the demo replays."
  echo "Record one with:  python audit.py --save demo_run.json --db auditor.db"
  exit 1
fi

MODE="${1:-replay}"
case "$MODE" in
  replay) echo "-> replaying the recorded run (instant, no network)"; echo
          python audit.py --replay demo_run.json --db auditor.db ;;
  live)   echo "-> running live against the API (~150s; keep talking)"; echo
          python audit.py --save demo_run.json --db auditor.db ;;
  *)      echo "usage: ./demo.sh [replay|live]"; exit 1 ;;
esac

python report.py --db auditor.db --out report.html >/dev/null
echo
echo "dashboard refreshed: report.html"
