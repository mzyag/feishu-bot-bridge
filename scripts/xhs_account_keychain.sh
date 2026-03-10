#!/usr/bin/env bash
set -euo pipefail

SERVICE="${XHS_ACCOUNT_KEYCHAIN_SERVICE:-feishu-bot-bridge.xhs.account}"
ACCOUNT="${XHS_ACCOUNT_KEYCHAIN_ACCOUNT:-default}"
cmd="${1:-status}"

usage() {
  cat <<EOF
usage:
  $0 set --account-id <id> [--username <username>] [--password <password>] [--session-token <token>] [--storage-state <path>] [--session-updated-at <iso8601>]
  $0 status
  $0 get
  $0 delete

Environment (optional):
  XHS_ACCOUNT_KEYCHAIN_SERVICE   default: ${SERVICE}
  XHS_ACCOUNT_KEYCHAIN_ACCOUNT   default: ${ACCOUNT}
EOF
}

store_payload() {
  local account_id="$1"
  local username="$2"
  local password="$3"
  local session_token="$4"
  local storage_state="$5"
  local session_updated_at="$6"
  local payload
  payload=$(/usr/bin/python3 - "$account_id" "$username" "$password" "$session_token" "$storage_state" "$session_updated_at" <<'PY'
import json
import sys
print(json.dumps(
    {
        "account_id": sys.argv[1],
        "username": sys.argv[2],
        "password": sys.argv[3],
        "session_token": sys.argv[4],
        "storage_state": sys.argv[5],
        "session_updated_at": sys.argv[6],
    },
    ensure_ascii=False,
))
PY
)
  security add-generic-password -U -a "$ACCOUNT" -s "$SERVICE" -w "$payload" >/dev/null
}

load_payload() {
  security find-generic-password -a "$ACCOUNT" -s "$SERVICE" -w
}

json_field() {
  local payload="$1"
  local key="$2"
  /usr/bin/python3 - "$payload" "$key" <<'PY'
import json
import sys
payload = sys.argv[1]
key = sys.argv[2]
try:
    obj = json.loads(payload)
except Exception:
    print("")
    raise SystemExit(0)
print(obj.get(key, ""))
PY
}

case "$cmd" in
  set)
    shift || true
    account_id=""
    username=""
    password=""
    session_token=""
    storage_state=""
    session_updated_at=""
    while [ $# -gt 0 ]; do
      case "$1" in
        --account-id)
          account_id="${2:-}"
          shift 2
          ;;
        --username)
          username="${2:-}"
          shift 2
          ;;
        --password)
          password="${2:-}"
          shift 2
          ;;
        --session-token)
          session_token="${2:-}"
          shift 2
          ;;
        --storage-state)
          storage_state="${2:-}"
          shift 2
          ;;
        --session-updated-at)
          session_updated_at="${2:-}"
          shift 2
          ;;
        *)
          echo "unknown option: $1" >&2
          usage
          exit 1
          ;;
      esac
    done
    if [ -z "$account_id" ]; then
      echo "missing required --account-id" >&2
      usage
      exit 1
    fi
    store_payload "$account_id" "$username" "$password" "$session_token" "$storage_state" "$session_updated_at"
    echo "stored: service=${SERVICE}, account=${ACCOUNT}, account_id=${account_id}"
    ;;
  status)
    if ! payload="$(load_payload 2>/dev/null)"; then
      echo "missing: service=${SERVICE}, account=${ACCOUNT}"
      exit 1
    fi
    account_id="$(json_field "$payload" account_id)"
    username="$(json_field "$payload" username)"
    state="$(json_field "$payload" storage_state)"
    updated_at="$(json_field "$payload" session_updated_at)"
    if [ -z "$account_id" ]; then
      echo "exists but payload format invalid: service=${SERVICE}, account=${ACCOUNT}"
      exit 1
    fi
    echo "exists: service=${SERVICE}, account=${ACCOUNT}, account_id=${account_id}, username=${username:-<empty>}, storage_state=${state:-<empty>}, session_updated_at=${updated_at:-<empty>}, password=***, session_token=***"
    ;;
  get)
    load_payload
    ;;
  delete)
    security delete-generic-password -a "$ACCOUNT" -s "$SERVICE" >/dev/null
    echo "deleted: service=${SERVICE}, account=${ACCOUNT}"
    ;;
  *)
    usage
    exit 1
    ;;
esac
