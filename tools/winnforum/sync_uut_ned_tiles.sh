#!/usr/bin/env bash
# Copy official Common-Data NED tiles into the UUT protection pack (data/geo/ned).
# Does not rename or fabricate tiles. Binaries stay gitignored.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
COMMON="${COMMON_DATA_ROOT:-/home/alanveloso/Código/Common-Data}"
DEST="${SAS_TERRAIN_DIR:-$ROOT/data/geo/ned}"
SRC="${COMMON}/data/ned"

if [[ ! -d "$SRC" ]]; then
  echo "error: Common-Data NED missing at $SRC (run fetch_common_data.sh first)" >&2
  exit 2
fi
mkdir -p "$DEST"

# Certification outdoor Cat-A HAAT footprints (site + 3–16 km radials).
# Encoding: NW corner nXXwYYY as used by NedTerrainProvider / harness TerrainDriver.
# PAT.2 DPA AMSL (San Diego / Oceanside corridor) needs n33–n34 / w117–w118.
TILES=(
  n31w090
  n33w117 n33w118
  n34w107 n34w117 n34w118
  n39w077 n39w078 n39w097 n39w098 n39w100 n39w101
  n40w077 n40w078 n40w097 n40w098 n40w099 n40w100 n40w101
  n41w097 n41w098 n41w099
  n43w100 n43w101 n43w105 n43w106 n43w111
  n44w101
)

copied=0
missing=0
for enc in "${TILES[@]}"; do
  usgs="usgs_ned_1_${enc}_gridfloat_std"
  float="float${enc}_1_std"
  if [[ -f "$DEST/${usgs}.flt" || -f "$DEST/${float}.flt" ]]; then
    continue
  fi
  src=""
  if [[ -f "$SRC/${usgs}.flt" ]]; then
    src="$usgs"
  elif [[ -f "$SRC/${float}.flt" ]]; then
    src="$float"
  else
    echo "MISSING $enc (not in Common-Data extract)" >&2
    missing=$((missing + 1))
    continue
  fi
  for ext in flt hdr prj; do
    if [[ -f "$SRC/${src}.${ext}" ]]; then
      cp "$SRC/${src}.${ext}" "$DEST/"
    fi
  done
  echo "copied ${src}.*"
  copied=$((copied + 1))
done

echo "DEST=$DEST flt=$(find "$DEST" -maxdepth 1 -name '*.flt' | wc -l) copied=$copied missing=$missing"
if (( missing > 0 )); then
  exit 1
fi
