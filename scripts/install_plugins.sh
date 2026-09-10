#!/usr/bin/env bash
# Install Git-submodule plugin packages (source composition → pip packages).
# Runtime discovers plugins via entry points, not submodule paths.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROP="${ROOT}/plugins/spectrum-propagation"
if [[ ! -f "${PROP}/pyproject.toml" ]]; then
  echo "plugins/spectrum-propagation missing; run: git submodule update --init --recursive" >&2
  exit 1
fi
python -m pip install "${1:--e}" "${PROP}"
