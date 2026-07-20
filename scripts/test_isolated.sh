#!/usr/bin/env bash
# Run an ISOLATED strategy forward-test against a LOCAL SQLite database.
#
# Isolation is enforced two ways:
#   1. DATABASE_URL="" forces the local SQLite path from config.test.yaml,
#      overriding any .env DATABASE_URL (which points at shared Supabase).
#   2. config.test.yaml uses its own db_path (data/test_isolated.db) and pins
#      tier 0 to a single strategy under test.
#
# This reads LIVE Tradier production quotes (paper simulation only — no orders
# are ever sent) so intraday/0DTE/STRAT signals get real market data. It never
# touches the production ledger and never races the Railway loop (different DB).
#
# Usage:
#   scripts/test_isolated.sh              # run the loop (60s tick)
#   scripts/test_isolated.sh scan         # one-shot scan, no loop
#   scripts/test_isolated.sh positions    # inspect the isolated test DB
#   scripts/test_isolated.sh pnl          # P/L of the isolated test DB
#   scripts/test_isolated.sh reset        # wipe the isolated test DB only
set -euo pipefail

cd "$(dirname "$0")/.."

CONFIG="config.test.yaml"
BIN=".venv/bin/killer-options-bot"
export DATABASE_URL=""          # <-- the isolation guarantee
export PYTHONWARNINGS=ignore

cmd="${1:-run}"
case "$cmd" in
  run)
    exec "$BIN" --config "$CONFIG" run --source tradier --tick 60
    ;;
  scan)
    exec "$BIN" --config "$CONFIG" scan --source tradier
    ;;
  positions)
    exec "$BIN" --config "$CONFIG" positions --source tradier
    ;;
  pnl)
    exec "$BIN" --config "$CONFIG" pnl --source tradier
    ;;
  reset)
    .venv/bin/python - "$CONFIG" <<'PY'
import sys
from killer_options_bot.config import load_config
from killer_options_bot.storage import get_storage
st = get_storage(load_config(sys.argv[1]))
st._execute("DELETE FROM positions", ())
st._execute("DELETE FROM candidates", ())
print("isolated test DB wiped: open",
      len(st.open_positions()), "closed", len(st.closed_positions()))
PY
    ;;
  *)
    echo "unknown command: $cmd" >&2
    echo "usage: scripts/test_isolated.sh [run|scan|positions|pnl|reset]" >&2
    exit 2
    ;;
esac
