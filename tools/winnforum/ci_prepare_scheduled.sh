#!/usr/bin/env bash
# Prepare a GitHub Actions (or local) workspace for scheduled official harness LIVE runs.
#
# Does NOT claim certification PASS. Does NOT change regulatory behavior.
# Requires: docker, git, git-lfs, python3.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"

HARNESS_COMMIT="${HARNESS_COMMIT:-928c3150adf7b31e53a96b695bf1fbdd3284ecb2}"
HARNESS_REPO="${HARNESS_REPO:-https://github.com/Wireless-Innovation-Forum/Spectrum-Access-System.git}"
COMMON_DATA_ROOT="${COMMON_DATA_ROOT:-$ROOT/.cache/Common-Data}"
HARNESS_SRC="$ROOT/.cache/winnforum-harness-src"
HARNESS_PKI="$ROOT/.cache/winnforum-harness-pki"
IMAGE_TAG="${WINNFORUM_DOCKER_IMAGE:-winnforum-sas-harness:928c3150}"

export COMMON_DATA_ROOT
export PATH="${HOME}/.local/bin:${PATH}"

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "error: missing required command: $1" >&2
    exit 2
  }
}

need_cmd git
need_cmd docker
need_cmd python3
if ! command -v git-lfs >/dev/null 2>&1; then
  echo "error: git-lfs required for Common-Data" >&2
  exit 2
fi
git lfs install --skip-repo >/dev/null

echo "==> Harness source @ ${HARNESS_COMMIT}"
rm -rf "$HARNESS_SRC"
mkdir -p "$HARNESS_SRC" "$ROOT/.cache"
TMP_CLONE="$ROOT/.cache/winnforum-harness-clone"
if [[ ! -d "$TMP_CLONE/.git" ]]; then
  GIT_LFS_SKIP_SMUDGE=1 git clone "$HARNESS_REPO" "$TMP_CLONE"
else
  git -C "$TMP_CLONE" fetch --tags --force origin
fi
git -C "$TMP_CLONE" checkout --force "$HARNESS_COMMIT"
git -C "$TMP_CLONE" archive --format=tar "$HARNESS_COMMIT" | tar -x -C "$HARNESS_SRC"
printf '%s\n' "$HARNESS_COMMIT" >"$HARNESS_SRC/HARNESS_COMMIT"

echo "==> Harness PKI"
rm -rf "$HARNESS_PKI"
mkdir -p "$HARNESS_PKI"
if [[ -d "$HARNESS_SRC/src/harness/certs" ]]; then
  cp -a "$HARNESS_SRC/src/harness/certs/." "$HARNESS_PKI/"
elif [[ -d "$TMP_CLONE/src/harness/certs" ]]; then
  cp -a "$TMP_CLONE/src/harness/certs/." "$HARNESS_PKI/"
else
  echo "error: official harness certs not found under src/harness/certs" >&2
  exit 2
fi
test -f "$HARNESS_PKI/admin.cert"
test -f "$HARNESS_PKI/admin.key"
test -f "$HARNESS_PKI/ca.cert"

echo "==> Common-Data seed (git-lfs + extract_geo)"
"$ROOT/tools/winnforum/fetch_common_data.sh"

echo "==> Sync UUT geo packs from Common-Data"
"$ROOT/tools/winnforum/sync_uut_ned_tiles.sh" || true
"$ROOT/tools/winnforum/sync_uut_nlcd_tiles.sh" || true

# County GeoJSON: prefer Common-Data extract when available.
if [[ -f "$COMMON_DATA_ROOT/data/extract_county.py" ]]; then
  echo "==> Extract county geometries (Common-Data)"
  (
    cd "$COMMON_DATA_ROOT"
    python3 data/extract_county.py || python3 -c "
import runpy
runpy.run_path('data/extract_county.py', run_name='__main__')
" || true
  )
fi
"$ROOT/tools/winnforum/sync_uut_county_geometries.sh" || {
  echo "warning: county sync incomplete; PCR/WDB PPA cases may fail" >&2
}

echo "==> Build harness Docker image ${IMAGE_TAG}"
docker build -f tools/winnforum/Dockerfile.harness \
  --build-arg "HARNESS_COMMIT=${HARNESS_COMMIT}" \
  -t "$IMAGE_TAG" \
  .

echo "==> Harness self-check (Docker runner)"
export WINNFORUM_COMMON_DATA="${COMMON_DATA_ROOT}/data"
python3 - <<'PY'
from tools.winnforum.docker_runner import DockerHarnessConfig, docker_self_check
import os
from pathlib import Path

root = Path(".").resolve()
cfg = DockerHarnessConfig(
    image=os.environ.get("WINNFORUM_DOCKER_IMAGE", "winnforum-sas-harness:928c3150"),
    sas_repo_root=root,
    common_data_dir=Path(os.environ["WINNFORUM_COMMON_DATA"]).resolve(),
    harness_pki_dir=(root / ".cache" / "winnforum-harness-pki").resolve(),
)
code, out = docker_self_check(cfg)
print(out)
raise SystemExit(code)
PY

echo "prepare_ok commit=${HARNESS_COMMIT} common=${COMMON_DATA_ROOT} pki=${HARNESS_PKI}"
