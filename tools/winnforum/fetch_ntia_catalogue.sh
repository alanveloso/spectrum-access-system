#!/usr/bin/env bash
# Fetch official WInnForum NTIA KML catalogue into data/ntia/ (gitignored).
#
# Source: Wireless-Innovation-Forum/Spectrum-Access-System @ HARNESS_COMMIT
# Does not regenerate geometry. Byte-identical to the pinned harness tree.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
DEST="${NTIA_DATA_DIR:-$ROOT/data/ntia}"
HARNESS_COMMIT="${HARNESS_COMMIT:-928c3150adf7b31e53a96b695bf1fbdd3284ecb2}"
BASE_URL="${NTIA_BASE_URL:-https://raw.githubusercontent.com/Wireless-Innovation-Forum/Spectrum-Access-System/${HARNESS_COMMIT}/data/ntia}"

# Expected SHA-256 of the pinned harness files (local acceptance copies).
declare -A EXPECTED_SHA256=(
  [protection_zones.kml]=4d5e15877cb198b4bd48bbbab2f842c666a8968bbb172a4b3c6992418c6cce45
  [E-DPAs.kml]=770b4221de5d6fc2d6c63a52a49af5c556570f19167215256464db8798563f82
  [P-DPAs.kml]=15cec69d89a0b07f68e969aecb90735c6d41b13d68d4d624cbaed904e5feee78
)

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "error: missing required command: $1" >&2
    exit 2
  }
}

need_cmd curl
need_cmd sha256sum

mkdir -p "$DEST"

FILES=(protection_zones.kml E-DPAs.kml P-DPAs.kml)
# Optional override: FETCH_NTIA_FILES="protection_zones.kml"
if [[ -n "${FETCH_NTIA_FILES:-}" ]]; then
  # shellcheck disable=SC2206
  FILES=(${FETCH_NTIA_FILES})
fi

for name in "${FILES[@]}"; do
  expected="${EXPECTED_SHA256[$name]:-}"
  if [[ -z "$expected" ]]; then
    echo "error: no expected SHA-256 for $name" >&2
    exit 2
  fi
  target="$DEST/$name"
  if [[ -f "$target" ]]; then
    got="$(sha256sum "$target" | awk '{print $1}')"
    if [[ "$got" == "$expected" ]]; then
      echo "ok cached $name"
      continue
    fi
    echo "refreshing $name (hash mismatch)"
  fi
  url="$BASE_URL/$name"
  echo "fetching $url"
  tmp="$(mktemp)"
  curl -fsSL --retry 3 --retry-delay 2 -o "$tmp" "$url"
  got="$(sha256sum "$tmp" | awk '{print $1}')"
  if [[ "$got" != "$expected" ]]; then
    echo "error: SHA-256 mismatch for $name" >&2
    echo "  expected $expected" >&2
    echo "  got      $got" >&2
    rm -f "$tmp"
    exit 1
  fi
  mv "$tmp" "$target"
  echo "ok $name"
done

echo "ntia_catalogue_ok dest=$DEST commit=$HARNESS_COMMIT"
