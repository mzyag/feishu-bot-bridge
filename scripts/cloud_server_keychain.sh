#!/usr/bin/env bash
set -euo pipefail

SERVICE="${CLOUD_SERVER_KEYCHAIN_SERVICE:-feishu-bot-bridge.cloud.server}"
ACCOUNT="${CLOUD_SERVER_KEYCHAIN_ACCOUNT:-default}"
cmd="${1:-status}"

usage() {
  cat <<EOF
usage:
  $0 set --host <ip-or-host> --user <username> --password <password>
  $0 status
  $0 get
  $0 delete

Environment (optional):
  CLOUD_SERVER_KEYCHAIN_SERVICE   default: ${SERVICE}
  CLOUD_SERVER_KEYCHAIN_ACCOUNT   default: ${ACCOUNT}
EOF
}

json_get_field() {
  local payload="$1"
  local key="$2"
  /usr/bin/python3 - "$payload" "$key" <<'PY'
import json
import sys

raw = sys.argv[1]
key = sys.argv[2]
try:
    obj = json.loads(raw)
except Exception:
    print("")
    raise SystemExit(0)
print(obj.get(key, ""))
PY
}

store_payload() {
  local host="$1"
  local user="$2"
  local password="$3"
  local payload
  payload=$(/usr/bin/python3 - "$host" "$user" "$password" <<'PY'
import json
import sys
payload = {
    "host": sys.argv[1],
    "user": sys.argv[2],
    "password": sys.argv[3],
}
print(json.dumps(payload, ensure_ascii=False))
PY
)
  security add-generic-password -U -a "$ACCOUNT" -s "$SERVICE" -w "$payload" >/dev/null
}

load_payload() {
  security find-generic-password -a "$ACCOUNT" -s "$SERVICE" -w
}

case "$cmd" in
  set)
    shift || true
    host=""
    user=""
    password=""
    while [ $# -gt 0 ]; do
      case "$1" in
        --host)
          host="${2:-}"
          shift 2
          ;;
        --user)
          user="${2:-}"
          shift 2
          ;;
        --password)
          password="${2:-}"
          shift 2
          ;;
        *)
          echo "unknown option: $1" >&2
          usage
          exit 1
          ;;
      esac
    done
    if [ -z "$host" ] || [ -z "$user" ] || [ -z "$password" ]; then
      echo "missing required args for set" >&2
      usage
      exit 1
    fi
    store_payload "$host" "$user" "$password"
    echo "stored: service=${SERVICE}, account=${ACCOUNT}, host=${host}, user=${user}"
    ;;
  status)
    if ! payload="$(load_payload 2>/dev/null)"; then
      echo "missing: service=${SERVICE}, account=${ACCOUNT}"
      exit 1
    fi
    host="$(json_get_field "$payload" host)"
    user="$(json_get_field "$payload" user)"
    if [ -z "$host" ] || [ -z "$user" ]; then
      echo "exists but payload format invalid: service=${SERVICE}, account=${ACCOUNT}"
      exit 1
    fi
    echo "exists: service=${SERVICE}, account=${ACCOUNT}, host=${host}, user=${user}, password=***"
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
