#!/usr/bin/env bash
# Orchestrate official WInnForum Common-Data acquisition for harness LIVE geo cases.
# Does NOT transform or rename tiles — only git-lfs + official extract_geo.py.
#
# Usage:
#   tools/winnforum/fetch_common_data.sh            # seed tiles + CONUS NED gaps
#   tools/winnforum/fetch_common_data.sh --conus    # pull remaining CONUS NED LFS
#   COMMON_DATA_EXTRA_INCLUDE='data/nlcd/foo.zip,' tools/winnforum/fetch_common_data.sh
set -euo pipefail

PATH="${HOME}/.local/bin:${PATH}"
COMMON_ROOT="${COMMON_DATA_ROOT:-/home/alanveloso/Código/Common-Data}"
REPO_URL="${COMMON_DATA_URL:-https://github.com/Wireless-Innovation-Forum/Common-Data.git}"
MODE="${1:-seed}"

# Minimal seed derived from prior Docker campaign FileNotFoundErrors.
NED_SEED=(
  floatn49w101_1_std.zip
  usgs_ned_1_n31w087_gridfloat_std.zip
  usgs_ned_1_n31w089_gridfloat_std.zip
  usgs_ned_1_n34w118_gridfloat_std.zip
  usgs_ned_1_n39w098_gridfloat_std.zip
  usgs_ned_1_n40w100_gridfloat_std.zip
  usgs_ned_1_n40w101_gridfloat_std.zip
  usgs_ned_1_n44w070_gridfloat_std.zip
)
NLCD_SEED=(
  nlcd_n40w099_ref.zip
  nlcd_n40w100_ref.zip
  nlcd_n40w101_ref.zip
  nlcd_n39w098_ref.zip
  nlcd_n39w099_ref.zip
  nlcd_n31w087_ref.zip
  nlcd_n31w088_ref.zip
  nlcd_n31w089_ref.zip
  nlcd_n34w118_ref.zip
  nlcd_n44w070_ref.zip
  nlcd_n49w101_ref.zip
  nlcd_n49w123_ref.zip
  nlcd_n43w084_ref.zip
)

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "error: missing required command: $1" >&2
    exit 2
  }
}

need_cmd git
need_cmd python3
if ! command -v git-lfs >/dev/null 2>&1; then
  echo "error: git-lfs not found (install Git LFS, then re-run)" >&2
  exit 2
fi
git lfs install --skip-repo >/dev/null

if [[ ! -d "${COMMON_ROOT}/.git" ]]; then
  mkdir -p "$(dirname "${COMMON_ROOT}")"
  GIT_LFS_SKIP_SMUDGE=1 git clone "${REPO_URL}" "${COMMON_ROOT}"
fi

cd "${COMMON_ROOT}"
echo "Common-Data commit: $(git rev-parse HEAD)"
echo "Common-Data status: $(git status -sb | head -1)"

extract_valid() {
  python3 <<'PY'
import os, zipfile, sys
sys.path.insert(0, "data")
from extract_geo import UnzipNeededFiles

def extract_valid(directory: str) -> None:
    ok = skip = fail = 0
    for name in sorted(os.listdir(directory)):
        if not name.endswith(".zip"):
            continue
        path = os.path.join(directory, name)
        if not zipfile.is_zipfile(path):
            skip += 1
            continue
        try:
            UnzipNeededFiles(path, directory)
            ok += 1
            os.unlink(path)  # free space after successful extract
            print(f"extracted {directory}/{name}")
        except Exception as exc:  # noqa: BLE001
            fail += 1
            print(f"FAIL {path}: {exc}")
    print(f"summary {directory}: ok={ok} skip_pointer={skip} fail={fail}")

extract_valid("data/ned")
extract_valid("data/nlcd")
if os.path.isdir("data/nlcd/nlcd_islands"):
    extract_valid("data/nlcd/nlcd_islands")
PY
}

pull_include() {
  local include="$1"
  [[ -z "${include}" ]] && return 0
  echo "git lfs pull --include=${include}"
  git lfs pull --include="${include}"
}

if [[ "${MODE}" == "--conus" || "${MODE}" == "conus" ]]; then
  # Remaining CONUS NED LFS pointers (lat 25-50, lon w066-w125) lacking .flt
  INCLUDE="$(python3 <<'PY'
import re, zipfile
from pathlib import Path
ned = Path("data/ned")
pat_usgs = re.compile(r"^usgs_ned_1_n(\d{2})w(\d{3})_gridfloat_std\.zip$")
pat_float = re.compile(r"^floatn(\d{2})w(\d{3})_1_std\.zip$")
need = []
for z in sorted(ned.glob("*.zip")):
    if zipfile.is_zipfile(z):
        continue
    m = pat_usgs.match(z.name) or pat_float.match(z.name)
    if not m:
        continue
    lat, lon = int(m.group(1)), int(m.group(2))
    if not (25 <= lat <= 50 and 66 <= lon <= 125):
        continue
    enc = f"n{lat:02d}w{lon:03d}"
    if (ned / f"usgs_ned_1_{enc}_gridfloat_std.flt").exists() or (
        ned / f"float{enc}_1_std.flt"
    ).exists():
        continue
    need.append(f"data/ned/{z.name}")
print(",".join(need))
PY
)"
  # batch to keep include strings manageable
  IFS=',' read -r -a ARR <<< "${INCLUDE}"
  BATCH=40
  for ((i = 0; i < ${#ARR[@]}; i += BATCH)); do
    chunk=("${ARR[@]:i:BATCH}")
    pull_include "$(IFS=,; echo "${chunk[*]}")"
    extract_valid
  done
else
  INCLUDE=""
  for z in "${NED_SEED[@]}"; do INCLUDE+="data/ned/${z},"; done
  for z in "${NLCD_SEED[@]}"; do INCLUDE+="data/nlcd/${z},"; done
  if [[ -n "${COMMON_DATA_EXTRA_INCLUDE:-}" ]]; then
    INCLUDE+="${COMMON_DATA_EXTRA_INCLUDE},"
  fi
  pull_include "${INCLUDE}"
  extract_valid
fi

echo "NED flt count: $(find data/ned -maxdepth 1 -name '*.flt' | wc -l)"
echo "NLCD int count: $(find data/nlcd -maxdepth 1 -name '*.int' | wc -l)"
echo "DONE host=${COMMON_ROOT}/data commit=$(git rev-parse HEAD)"
