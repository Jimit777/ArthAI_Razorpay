#!/bin/bash
# Start the merchant platform.  ./run_platform.sh   then open http://localhost:8000
#
# Exists for the same reason demo.sh does: "command not found: python" is not a
# thing to discover with an audience watching. This activates the venv, checks
# for the API key, and says plainly what will happen if it is missing.
set -e
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  echo "No .venv. Run:  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
  exit 1
fi
source .venv/bin/activate

if [ -z "$LEDGERLINE_SECRET_KEY" ]; then
  echo "NOTE: LEDGERLINE_SECRET_KEY is not set."
  echo "      API secrets will not be stored at all - each sync will ask."
  echo "      Generate one with:  python -m merchant.vault"
  echo
fi

if [ -z "$ANTHROPIC_API_KEY" ]; then
  echo "NOTE: ANTHROPIC_API_KEY is not set."
  echo "      The platform still runs and the rate-card rules still work -"
  echo "      only the agent step will be skipped, and the page will say so."
  echo
fi

echo "http://localhost:8000"
echo
exec uvicorn merchant.app:app --port 8000 --reload
