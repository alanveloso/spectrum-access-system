#!/usr/bin/env bash
# Build WInnForum reference_models C extensions (ITM / eHata) for the UUT interpreter.
# Required for PPA RF maximum contour and hybrid path-loss via spectrum-propagation.
# Does not modify harness upstream sources beyond in-tree build_ext -i artifacts.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
PY="${PYTHON:-$ROOT/.venv/bin/python}"
HARNESS="${SAS_HARNESS_DIR:-$ROOT/.cache/winnforum-harness-src/src/harness}"

if [[ ! -x "$PY" ]]; then
  echo "error: python not found at $PY" >&2
  exit 2
fi
if [[ ! -d "$HARNESS/reference_models/propagation/itm" ]]; then
  echo "error: harness reference_models missing at $HARNESS" >&2
  exit 2
fi

# Python 3.12+: distutils lives in setuptools.
"$PY" -c "import distutils" 2>/dev/null || "$PY" -m pip install 'setuptools==75.8.2'

for ext in itm ehata; do
  dir="$HARNESS/reference_models/propagation/$ext"
  echo "building $ext in $dir"
  (
    cd "$dir"
    "$PY" setup.py build_ext -i
  )
done

echo "SAS_HARNESS_DIR=$HARNESS"
"$PY" - <<'PY'
from services.propagation.engines import clear_reference_engines_cache, load_reference_engines
clear_reference_engines_cache()
load_reference_engines()
print("reference engines: OK")
PY
