#!/usr/bin/env bash
# Thin wrapper around official harness execution. Does not modify testcase logic.
set -euo pipefail

HARNESS_HOME="${HARNESS_HOME:-/opt/winnforum-harness}"
HARNESS_WORKDIR="${HARNESS_WORKDIR:-/opt/winnforum-harness/src/harness}"
SAS_REPO_MOUNT="${SAS_REPO_MOUNT:-/opt/sas}"
ARTIFACTS_DIR="${ARTIFACTS_DIR:-/artifacts}"
COMMON_DATA_DIR="${COMMON_DATA_DIR:-/common-data}"
COMMON_DATA_COUNTY_DIR="${COMMON_DATA_COUNTY_DIR:-/common-data-county}"

wire_common_data() {
  # Infrastructure wiring only — do not patch official reference_models source.
  mkdir -p "${HARNESS_HOME}/data/geo"
  if [[ -d "${COMMON_DATA_DIR}/ned" ]]; then
    ln -sfn "${COMMON_DATA_DIR}/ned" "${HARNESS_HOME}/data/geo/ned"
  fi
  if [[ -d "${COMMON_DATA_DIR}/nlcd" ]]; then
    ln -sfn "${COMMON_DATA_DIR}/nlcd" "${HARNESS_HOME}/data/geo/nlcd"
  fi
  if [[ -d "${COMMON_DATA_COUNTY_DIR}" ]] && ls "${COMMON_DATA_COUNTY_DIR}"/*.json >/dev/null 2>&1; then
    ln -sfn "${COMMON_DATA_COUNTY_DIR}" "${HARNESS_HOME}/data/county"
  fi
}

wire_common_data

cd "${HARNESS_WORKDIR}"

# SAS tools (exec_unittest, openssl_compat) live outside official harness source.
if [[ -d "${SAS_REPO_MOUNT}" ]]; then
  export PYTHONPATH="${SAS_REPO_MOUNT}${PYTHONPATH:+:${PYTHONPATH}}"
fi

export ARTIFACTS_DIR COMMON_DATA_DIR COMMON_DATA_COUNTY_DIR

case "${1:-self-check}" in
  self-check)
    exec python /usr/local/bin/harness-self-check
    ;;
  exec)
    shift
    if [[ -f /artifacts/sas.cfg ]]; then
      cp /artifacts/sas.cfg ./sas.cfg
    fi
    exec python -c \
      'import runpy, sys; sys.path.insert(0, "."); from tools.winnforum.exec_unittest import main as _main; raise SystemExit(_main(sys.argv[1:]))' \
      "$@"
    ;;
  shell)
    exec /bin/bash
    ;;
  resolve)
    shift
    exec python -c \
      'import json, sys; sys.path.insert(0, "/opt/sas"); from tools.winnforum.discover_tests import resolve_case_methods; payload=json.loads(sys.argv[1]); print(json.dumps(resolve_case_methods(payload["module"], payload["case"], workdir="/opt/winnforum-harness/src/harness")))' \
      "$1"
    ;;
  *)
    exec "$@"
    ;;
esac
