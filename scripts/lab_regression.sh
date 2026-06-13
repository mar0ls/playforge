#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${BASE_URL:-http://127.0.0.1:8765}"
PROJECT_ID="${PROJECT_ID:-}"
INVENTORY_PATH="${INVENTORY_PATH:-test/hosts_pf_docker.ini}"
HOST_PATTERN="${HOST_PATTERN:-all}"
PLAYBOOKS_CSV="${PLAYBOOKS_CSV:-playbooks/lab_ping.yml,playbooks/lab_file.yml,playbooks/lab_apt.yml}"
CHECK_PREFLIGHT="${CHECK_PREFLIGHT:-true}"
INCLUDE_TARGETS_PREFLIGHT="${INCLUDE_TARGETS_PREFLIGHT:-true}"
REQUEST_TIMEOUT_SEC="${REQUEST_TIMEOUT_SEC:-600}"
EXTRA_VARS_JSON="${EXTRA_VARS_JSON:-{}}"

usage() {
  cat <<'EOF'
Usage:
  PROJECT_ID=<id> [BASE_URL=http://127.0.0.1:8765] make lab-regression

Optional env vars:
  INVENTORY_PATH             Inventory path used for preflight and runs
  HOST_PATTERN               Host pattern used in preflight ping
  PLAYBOOKS_CSV              Comma-separated playbook list
  CHECK_PREFLIGHT            true|false (include --check-mode-specific checks, e.g. python3-apt probe)
  INCLUDE_TARGETS_PREFLIGHT  true|false (probe target reachability via ad-hoc ping)
  EXTRA_VARS_JSON            JSON object injected into each run (default: {})
  REQUEST_TIMEOUT_SEC        curl max time in seconds (default: 600)

Output:
  Prints one compact JSON report to stdout and exits non-zero on regression failure.
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

if [[ -z "$PROJECT_ID" ]]; then
  echo "ERROR: PROJECT_ID is required" >&2
  usage >&2
  exit 2
fi

if ! command -v curl >/dev/null 2>&1; then
  echo "ERROR: curl is required" >&2
  exit 2
fi
if ! command -v jq >/dev/null 2>&1; then
  echo "ERROR: jq is required" >&2
  exit 2
fi

if ! jq -e . >/dev/null 2>&1 <<<"$EXTRA_VARS_JSON"; then
  echo "ERROR: EXTRA_VARS_JSON must be valid JSON" >&2
  exit 2
fi

api_post() {
  local path="$1"
  local body="$2"
  curl -sS -m "$REQUEST_TIMEOUT_SEC" \
    -H 'Content-Type: application/json' \
    -X POST "${BASE_URL}${path}" \
    -d "$body"
}

api_get() {
  local path="$1"
  curl -sS -m "$REQUEST_TIMEOUT_SEC" "${BASE_URL}${path}"
}

as_bool() {
  case "${1,,}" in
    1|true|yes|on) echo true ;;
    0|false|no|off) echo false ;;
    *)
      echo "ERROR: expected boolean, got '$1'" >&2
      exit 2
      ;;
  esac
}

check_preflight_bool="$(as_bool "$CHECK_PREFLIGHT")"
include_targets_bool="$(as_bool "$INCLUDE_TARGETS_PREFLIGHT")"

runs_file="$(mktemp)"
trap 'rm -f "$runs_file"' EXIT

preflight_req="$(jq -nc \
  --arg project_id "$PROJECT_ID" \
  --arg inventory "$INVENTORY_PATH" \
  --arg host_pattern "$HOST_PATTERN" \
  --argjson check "$check_preflight_bool" \
  --argjson include_targets "$include_targets_bool" \
  '{project_id:$project_id, inventory:$inventory, host_pattern:$host_pattern, check:$check, include_targets:$include_targets}')"

preflight_resp_raw="$(api_post '/api/runs/preflight' "$preflight_req")"
if jq -e . >/dev/null 2>&1 <<<"$preflight_resp_raw"; then
  preflight_resp="$preflight_resp_raw"
else
  preflight_resp="$(jq -nc --arg error 'non-json response' --arg raw "$preflight_resp_raw" '{ok:false, error:$error, raw:$raw}')"
fi

preflight_ok="$(jq -r '(.ok // false) | tostring' <<<"$preflight_resp")"

IFS=',' read -r -a playbooks <<<"$PLAYBOOKS_CSV"
for raw_pb in "${playbooks[@]}"; do
  pb="$(xargs <<<"$raw_pb")"
  if [[ -z "$pb" ]]; then
    continue
  fi

  run_req="$(jq -nc \
    --arg project_id "$PROJECT_ID" \
    --arg playbook "$pb" \
    --arg inventory "$INVENTORY_PATH" \
    --argjson extra_vars "$EXTRA_VARS_JSON" \
    '{project_id:$project_id, playbook:$playbook, inventory:$inventory, extra_vars:$extra_vars}')"

  run_resp="$(api_post '/api/runs' "$run_req")"

  if ! jq -e . >/dev/null 2>&1 <<<"$run_resp"; then
    jq -nc --arg playbook "$pb" --arg error "non-json response" \
      '{playbook:$playbook, status:"failed", error:$error}' >>"$runs_file"
    continue
  fi

  run_id="$(jq -r '.run_id // empty' <<<"$run_resp")"
  status="$(jq -r '.overall // .status // "failed"' <<<"$run_resp")"
  rc="$(jq -r '.rc // -1' <<<"$run_resp")"
  failures_count="$(jq -r '(.failures // []) | length' <<<"$run_resp")"
  diagnostics_count="$(jq -r '(.diagnostics // []) | length' <<<"$run_resp")"

  jq -nc \
    --arg playbook "$pb" \
    --arg status "$status" \
    --argjson run_id "${run_id:-null}" \
    --argjson rc "$rc" \
    --argjson failures "$failures_count" \
    --argjson diagnostics "$diagnostics_count" \
    --argjson response "$run_resp" \
    '{playbook:$playbook, run_id:$run_id, status:$status, rc:$rc, failures:$failures, diagnostics:$diagnostics, response:$response}' \
    >>"$runs_file"
done

runs_json="$(jq -s '.' "$runs_file")"

failed_count="$(jq -r '[.[] | select((.status != "ok") and (.status != "successful"))] | length' <<<"$runs_json")"
all_ok="false"
if [[ "$preflight_ok" == "true" && "$failed_count" == "0" ]]; then
  all_ok="true"
fi

report="$(jq -nc \
  --arg timestamp "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --arg base_url "$BASE_URL" \
  --arg project_id "$PROJECT_ID" \
  --arg inventory "$INVENTORY_PATH" \
  --argjson preflight "$preflight_resp" \
  --argjson runs "$runs_json" \
  --argjson ok "$all_ok" \
  '{ok:$ok, timestamp:$timestamp, base_url:$base_url, project_id:$project_id, inventory:$inventory, preflight:$preflight, runs:$runs}')"

echo "$report"

if [[ "$all_ok" != "true" ]]; then
  exit 1
fi
