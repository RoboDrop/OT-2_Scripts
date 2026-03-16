#!/usr/bin/env bash
set -euo pipefail

# Usage:
#   ./run_ot2_pipette_smoke_test.sh [--mount left|right|both] [--host HOSTNAME]
#
# Examples:
#   ./run_ot2_pipette_smoke_test.sh
#   ./run_ot2_pipette_smoke_test.sh --mount left
#   ./run_ot2_pipette_smoke_test.sh --mount right --host f21566.local

MOUNT_SELECTION="both"
ROBOT_HOST="${OT2_HOST:-}"
PYTHON_BIN="${PYTHON_BIN:-}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOCAL_SCRIPT_PATH="${SCRIPT_DIR}/ot2_pipette_smoke_test.py"
HOST_RESOLVER="${SCRIPT_DIR}/ot2_resolve_host.py"

timestamp() {
  date +"%Y-%m-%dT%H:%M:%S%z"
}

log_info() {
  printf "%s [INFO] %s\n" "$(timestamp)" "$*" >&2
}

log_error() {
  printf "%s [ERROR] %s\n" "$(timestamp)" "$*" >&2
}

fail() {
  log_error "$*"
  exit 1
}

usage() {
  cat >&2 <<'USAGE'
Usage:
  ./run_ot2_pipette_smoke_test.sh [--mount left|right|both] [--host HOSTNAME]

Options:
  --mount VALUE   Start mount: left, right, or both (default: both).
                  If only one is selected, the other mount is tested next if attached.
  --host HOST     OT-2 robot-server hostname or IP (default: OT2_HOST env var, else opentrons.local).
  -h, --help      Show this help.
USAGE
}

require_command() {
  local cmd="$1"
  command -v "${cmd}" >/dev/null 2>&1 || fail "Missing required command: ${cmd}"
}

python_has_packages() {
  local python_cmd="$1"
  "${python_cmd}" -c "import opentrons" >/dev/null 2>&1
}

resolve_python_bin() {
  local candidates=()

  if [[ -n "${PYTHON_BIN}" ]]; then
    require_command "${PYTHON_BIN}"
    if python_has_packages "${PYTHON_BIN}"; then
      return
    fi
    fail "PYTHON_BIN='${PYTHON_BIN}' does not have required Opentrons packages."
  fi

  if [[ -n "${CONDA_PREFIX:-}" && -x "${CONDA_PREFIX}/bin/python" ]]; then
    candidates+=("${CONDA_PREFIX}/bin/python")
  fi
  if command -v python >/dev/null 2>&1; then
    candidates+=("python")
  fi
  if command -v python3 >/dev/null 2>&1; then
    candidates+=("python3")
  fi

  for candidate in "${candidates[@]}"; do
    if python_has_packages "${candidate}"; then
      PYTHON_BIN="${candidate}"
      return
    fi
  done

  fail "No Python interpreter with required Opentrons packages was found. Activate your Opentrons environment first."
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --mount)
        [[ $# -ge 2 ]] || fail "--mount requires a value"
        MOUNT_SELECTION="$(printf "%s" "$2" | tr '[:upper:]' '[:lower:]')"
        shift 2
        ;;
      --host)
        [[ $# -ge 2 ]] || fail "--host requires a value"
        ROBOT_HOST="$2"
        shift 2
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      *)
        fail "Unknown argument: $1"
        ;;
    esac
  done
}

validate_mount_selection() {
  case "${MOUNT_SELECTION}" in
    left|right|both) ;;
    *)
      fail "Invalid --mount value: ${MOUNT_SELECTION}. Use left, right, or both."
      ;;
  esac
}

resolve_robot_host() {
  [[ -f "${HOST_RESOLVER}" ]] || fail "Host resolver not found: ${HOST_RESOLVER}"
  if [[ -n "${ROBOT_HOST}" ]]; then
    ROBOT_HOST="$(python3 "${HOST_RESOLVER}" --host "${ROBOT_HOST}")"
    return
  fi
  ROBOT_HOST="$(python3 "${HOST_RESOLVER}")"
}

main() {
  parse_args "$@"
  validate_mount_selection

  [[ -f "${LOCAL_SCRIPT_PATH}" ]] || fail "Smoke test script not found: ${LOCAL_SCRIPT_PATH}"
  require_command python3
  resolve_python_bin
  resolve_robot_host

  log_info "Starting OT-2 smoke test over robot-server API."
  log_info "Selected start mount: ${MOUNT_SELECTION}"
  log_info "Robot host: ${ROBOT_HOST}"
  log_info "Using Python interpreter: ${PYTHON_BIN}"

  OT2_SMOKE_TEST_MOUNT="${MOUNT_SELECTION}" OT2_HOST="${ROBOT_HOST}" \
    "${PYTHON_BIN}" "${LOCAL_SCRIPT_PATH}"
}

main "$@"
