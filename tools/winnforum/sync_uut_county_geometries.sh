#!/usr/bin/env bash
# Copy official county GeoJSON (FIPS) into the UUT protection pack (data/geo/county).
# Source preference: harness-extracted Common-Data county cache, then Common-Data extract.
# Does not fabricate geometries. JSON stays gitignored.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
COMMON="${COMMON_DATA_ROOT:-$ROOT/../Common-Data}"
DEST="${SAS_COUNTY_DIR:-$ROOT/data/geo/county}"
CACHE="${WINNFORUM_COUNTY_CACHE:-$ROOT/.cache/winnforum-harness-county}"
COMMON_COUNTY="${COMMON}/data/county"

# FIPS used by official PCR.1 / WDB.1 (and adjacent PCR family) default configs.
FIPS=(
  20063
  20195
)

mkdir -p "$DEST"

src_root=""
if [[ -d "$CACHE" ]] && ls "$CACHE"/*.json >/dev/null 2>&1; then
  src_root="$CACHE"
elif [[ -d "$COMMON_COUNTY" ]] && ls "$COMMON_COUNTY"/*.json >/dev/null 2>&1; then
  src_root="$COMMON_COUNTY"
else
  echo "error: county GeoJSON source missing (tried $CACHE and $COMMON_COUNTY)" >&2
  echo "hint: harness zip extract → .cache/winnforum-harness-county or Common-Data extract_county.py" >&2
  exit 2
fi

copied=0
missing=0
for fips in "${FIPS[@]}"; do
  if [[ -f "$DEST/${fips}.json" ]]; then
    continue
  fi
  if [[ ! -f "$src_root/${fips}.json" ]]; then
    echo "MISSING ${fips}.json (not in $src_root)" >&2
    missing=$((missing + 1))
    continue
  fi
  cp "$src_root/${fips}.json" "$DEST/"
  echo "copied ${fips}.json"
  copied=$((copied + 1))
done

echo "DEST=$DEST src=$src_root json=$(find "$DEST" -maxdepth 1 -name '*.json' | wc -l) copied=$copied missing=$missing"
if (( missing > 0 )); then
  exit 1
fi
