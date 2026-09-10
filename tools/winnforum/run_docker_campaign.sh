#!/usr/bin/env bash
# Full non-time-gated WInnForum LIVE campaign via Docker harness.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
export DATABASE_URL="${DATABASE_URL:-postgresql+psycopg2://sas:sas_test@127.0.0.1:55434/sas}"
export SAS_EXECUTION_MODE="${SAS_EXECUTION_MODE:-certification}"
export SAS_TERRAIN_DIR="${SAS_TERRAIN_DIR:-$ROOT/data/geo/ned}"
PKI="$ROOT/.cache/winnforum-harness-pki"
exec "$ROOT/.venv/bin/python" -m tools.run_winnforum \
  --docker-harness --start-uut \
  --family REG --family SIQ --family GRA --family HBT --family RLQ --family DRG \
  --family FAD --family SSS --family BPR --family EPR --family EXZ --family FPR \
  --family GPR --family IPR --family MCP --family MES --family PAT --family PCR \
  --family PPR --family QPR --family SCS --family SDS --family WDB \
  --case FDB.1 --case FDB.2 --case FDB.3 --case FDB.4 --case FDB.5 --case FDB.6 \
  --certs-dir "$PKI" \
  --client-cert "$PKI/admin.cert" \
  --client-key "$PKI/admin.key" \
  --ca-certs "$PKI/ca.cert" \
  --artifacts-root "$ROOT/artifacts/winnforum/docker-harness"
