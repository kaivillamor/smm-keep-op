#!/usr/bin/env bash
# Railway collection service entrypoint.
#
# Runs once and exits — this is a CRON service, not a long-running one. Railway's
# restart policy must be NEVER or it will restart-loop after each successful exit.
#
# Deliberately does NOT touch bets.db: --collect writes only research.db, and
# BETS_DB_PATH is intentionally left unset in the Railway environment so the
# real-money record stays on the local machine.
set -uo pipefail

cd "$(dirname "$0")/../baseball" || exit 1
echo "=== $(date -u +%FT%TZ) | collection run | RESEARCH_DB_PATH=${RESEARCH_DB_PATH:-<default>} ==="

# Grade first (yesterday's pending), then collect today's newly-confirmed lineups.
# Grading is idempotent and skips in-progress games, so running it every tick is safe.
# A grading failure must not block collection — that is the part with a deadline.
python main.py --collect-grade || echo "[warn] grading step failed; continuing to collection"
python main.py --collect
