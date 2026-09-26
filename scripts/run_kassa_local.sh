#!/usr/bin/env bash
# Local gastroweek kassa (browser UI + CSV). No Telegram / GCP needed.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
export PYTHONPATH="${ROOT}${PYTHONPATH:+:$PYTHONPATH}"
HOST="${KASSA_HOST:-127.0.0.1}"
PORT="${KASSA_PORT:-8765}"
CSV="${KASSA_CSV:-${ROOT}/kassa_local/data/sales.csv}"
mkdir -p "$(dirname "$CSV")"
export KASSA_CSV="$CSV"
echo "Gastroweek kassa → http://${HOST}:${PORT}"
echo "CSV file: ${CSV}"
exec python3 -m uvicorn kassa_local.app:app --host "$HOST" --port "$PORT"
