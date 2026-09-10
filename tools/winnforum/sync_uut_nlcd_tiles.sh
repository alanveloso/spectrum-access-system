#!/usr/bin/env bash
# Copy official Common-Data NLCD tiles into the UUT protection pack (data/geo/nlcd).
# Required for PPA RF region typing (NlcdDriver.RegionNlcdVote).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
COMMON="${COMMON_DATA_ROOT:-$ROOT/../Common-Data}"
DEST="${SAS_NLCD_DIR:-$ROOT/data/geo/nlcd}"
SRC="${COMMON}/data/nlcd"

if [[ ! -d "$SRC" ]]; then
  echo "error: Common-Data NLCD missing at $SRC" >&2
  exit 2
fi
mkdir -p "$DEST"

# PCR.1 / WDB.1 Kansas cluster + PAT.1 PPA centroid (~39.30/-98.65 → n40w099).
# Encoding: NW corner nXXwYYY as used by NlcdDriver.RegionNlcdVote.
TILES=(
  n39w098
  n39w099
  n39w100
  n39w101
  n40w099
  n40w100
  n40w101
)

copied=0
missing=0
for enc in "${TILES[@]}"; do
  base="nlcd_${enc}_ref"
  if [[ -f "$DEST/${base}.int" ]]; then
    continue
  fi
  if [[ -f "$SRC/${base}.int" ]]; then
    for ext in int hdr prj; do
      [[ -f "$SRC/${base}.${ext}" ]] && cp "$SRC/${base}.${ext}" "$DEST/"
    done
    echo "copied ${base}.*"
    copied=$((copied + 1))
    continue
  fi
  echo "MISSING ${base}.int (extract Common-Data NLCD zip first)" >&2
  missing=$((missing + 1))
done

echo "DEST=$DEST int=$(find "$DEST" -maxdepth 1 -name '*.int' | wc -l) copied=$copied missing=$missing"
if (( missing > 0 )); then
  exit 1
fi
