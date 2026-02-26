#!/usr/bin/env bash
set -euo pipefail

# Run an Opentrons protocol on a USB-connected OT-2 over robot-server HTTP API.
#
# Usage:
#   ./run_ot2_protocol_usb.sh --protocol transfer_test.py
#   ./run_ot2_protocol_usb.sh --protocol transfer_test.py --host 169.254.195.131
#   ./run_ot2_protocol_usb.sh --protocol transfer_test.py --host f21566.local --timeout 900

PROTOCOL_PATH=""
ROBOT_HOST="${OT2_HOST:-}"
TIMEOUT_SECONDS=600
POLL_SECONDS=2
PORT=31950
API_VERSION_HEADER="opentrons-version: 2"

timestamp() {
  date +"%Y-%m-%dT%H:%M:%S%z"
}

log_info() {
  printf "%s [INFO] %s\n" "$(timestamp)" "$*" >&2
}

log_warn() {
  printf "%s [WARN] %s\n" "$(timestamp)" "$*" >&2
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
  ./run_ot2_protocol_usb.sh --protocol PATH [--host HOST] [--timeout SECONDS]

Options:
  --protocol PATH   Protocol file to upload and run (required).
  --host HOST       OT-2 host or IP. If omitted, script auto-discovers USB OT-2.
                    You can also set OT2_HOST env var.
  --timeout N       Max seconds to wait for completion (default: 600).
  -h, --help        Show this help.
USAGE
}

require_command() {
  local cmd="$1"
  command -v "$cmd" >/dev/null 2>&1 || fail "Missing required command: $cmd"
}

json_get() {
  local expr="$1"
  python3 -c '
import json
import sys

expr = sys.argv[1]
raw = sys.stdin.read()
if not raw.strip():
    print("")
    raise SystemExit(0)

data = json.loads(raw)
cur = data
for part in expr.split("."):
    if isinstance(cur, dict):
        cur = cur.get(part)
    else:
        cur = None
        break

if cur is None:
    print("")
elif isinstance(cur, (dict, list)):
    print(json.dumps(cur))
else:
    print(cur)
' "$expr"
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --protocol)
        [[ $# -ge 2 ]] || fail "--protocol requires a value"
        PROTOCOL_PATH="$2"
        shift 2
        ;;
      --host)
        [[ $# -ge 2 ]] || fail "--host requires a value"
        ROBOT_HOST="$2"
        shift 2
        ;;
      --timeout)
        [[ $# -ge 2 ]] || fail "--timeout requires a value"
        TIMEOUT_SECONDS="$2"
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

validate_inputs() {
  [[ -n "$PROTOCOL_PATH" ]] || fail "--protocol is required"
  [[ -f "$PROTOCOL_PATH" ]] || fail "Protocol file not found: $PROTOCOL_PATH"
  [[ "$TIMEOUT_SECONDS" =~ ^[0-9]+$ ]] || fail "--timeout must be an integer"
}

probe_host() {
  local host="$1"
  local url="http://${host}:${PORT}/health"
  curl -sS -m 2 -H "$API_VERSION_HEADER" "$url" >/dev/null 2>&1
}

discover_hosts() {
  local -a candidates=()
  local line host ip

  candidates+=("opentrons.local")

  while IFS= read -r line; do
    host="$(printf "%s" "$line" | awk '{print $1}')"
    ip="$(printf "%s" "$line" | awk '{print $2}' | tr -d '()')"

    if [[ "$host" == *.local ]]; then
      candidates+=("$host")
    fi
    if [[ "$ip" =~ ^169\.254\.[0-9]+\.[0-9]+$ ]]; then
      candidates+=("$ip")
    fi
  done < <(arp -a 2>/dev/null || true)

  printf "%s\n" "${candidates[@]}" | awk 'NF' | awk '!seen[$0]++'
}

resolve_robot_host() {
  if [[ -n "$ROBOT_HOST" ]]; then
    log_info "Using host from args/env: $ROBOT_HOST"
    probe_host "$ROBOT_HOST" || fail "Unable to reach robot at $ROBOT_HOST:$PORT"
    return
  fi

  local -a found=()
  local candidate
  while IFS= read -r candidate; do
    [[ -n "$candidate" ]] || continue
    if probe_host "$candidate"; then
      found+=("$candidate")
    fi
  done < <(discover_hosts)

  if [[ ${#found[@]} -eq 0 ]]; then
    fail "No reachable OT-2 robot found. Connect via USB and/or pass --host HOST."
  fi

  ROBOT_HOST="${found[0]}"
  if [[ ${#found[@]} -gt 1 ]]; then
    log_warn "Multiple reachable hosts found: ${found[*]}"
    log_warn "Using first host: $ROBOT_HOST (pass --host to choose explicitly)."
  else
    log_info "Auto-discovered OT-2 host: $ROBOT_HOST"
  fi
}

main() {
  parse_args "$@"
  require_command curl
  require_command python3
  require_command arp
  validate_inputs
  resolve_robot_host

  local api_url="http://${ROBOT_HOST}:${PORT}"
  local upload_resp protocol_id run_resp run_id play_resp
  local start_ts now elapsed status status_resp run_errors

  log_info "Uploading protocol: $PROTOCOL_PATH"
  upload_resp="$(curl -sS -H "$API_VERSION_HEADER" -F "files=@${PROTOCOL_PATH}" "${api_url}/protocols")"
  protocol_id="$(printf "%s" "$upload_resp" | json_get "data.id")"
  [[ -n "$protocol_id" ]] || fail "Protocol upload failed: $upload_resp"
  log_info "Uploaded protocol id: $protocol_id"

  log_info "Creating run"
  run_resp="$(curl -sS -H "$API_VERSION_HEADER" -H "Content-Type: application/json" \
    -d "{\"data\":{\"protocolId\":\"${protocol_id}\"}}" "${api_url}/runs")"
  run_id="$(printf "%s" "$run_resp" | json_get "data.id")"
  [[ -n "$run_id" ]] || fail "Run creation failed: $run_resp"
  log_info "Created run id: $run_id"

  log_info "Starting run"
  play_resp="$(curl -sS -H "$API_VERSION_HEADER" -H "Content-Type: application/json" \
    -d '{"data":{"actionType":"play"}}' "${api_url}/runs/${run_id}/actions")"
  [[ "$(printf "%s" "$play_resp" | json_get "data.actionType")" == "play" ]] || {
    fail "Run play failed: $play_resp"
  }

  start_ts="$(date +%s)"
  while true; do
    status_resp="$(curl -sS -H "$API_VERSION_HEADER" "${api_url}/runs/${run_id}")"
    status="$(printf "%s" "$status_resp" | json_get "data.status")"

    case "$status" in
      succeeded)
        log_info "Run succeeded: $run_id"
        exit 0
        ;;
      failed|stopped|blocked-by-open-door|paused|pause-requested)
        run_errors="$(printf "%s" "$status_resp" | json_get "data.errors")"
        fail "Run ended with status=${status}, errors=${run_errors}, run_id=${run_id}"
        ;;
      *)
        log_info "Run status: ${status:-unknown}"
        ;;
    esac

    now="$(date +%s)"
    elapsed=$((now - start_ts))
    if (( elapsed > TIMEOUT_SECONDS )); then
      fail "Timed out after ${TIMEOUT_SECONDS}s waiting for run ${run_id}."
    fi

    sleep "$POLL_SECONDS"
  done
}

main "$@"
