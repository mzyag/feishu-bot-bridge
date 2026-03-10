#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="/Users/cn/Workspace/feishu-bot-bridge"
KEYCHAIN_SCRIPT="${PROJECT_DIR}/scripts/xhs_account_keychain.sh"
STATE_DIR_DEFAULT="${PROJECT_DIR}/.state/xhs"
cmd="${1:-status}"

usage() {
  cat <<EOF
usage:
  $0 login --account-id <xhs_account_id> [--username <login_name>] [--state-file <path>] [--url <login_url>]
  $0 status

Description:
  - Use browser-based web authorization login (Playwright interactive browser).
  - Save storage state JSON and write it into Keychain via xhs_account_keychain.sh.

Requirements:
  - Node.js + npx available
  - Playwright CLI available (npx will auto-fetch if needed)
EOF
}

ensure_tools() {
  if ! command -v npx >/dev/null 2>&1; then
    echo "npx not found. Please install Node.js first." >&2
    exit 1
  fi
  if [ ! -x "$KEYCHAIN_SCRIPT" ]; then
    echo "missing keychain script: $KEYCHAIN_SCRIPT" >&2
    exit 1
  fi
}

login_flow() {
  local account_id="$1"
  local username="$2"
  local state_file="$3"
  local login_url="$4"
  mkdir -p "$(dirname "$state_file")"
  rm -f "$state_file"

  echo "[xhs-web-auth] opening browser for Xiaohongshu login..."
  echo "[xhs-web-auth] complete login in browser, then close browser window to finish."

  npx -y playwright open --save-storage="$state_file" "$login_url"

  if [ ! -s "$state_file" ]; then
    echo "storage state not created: $state_file" >&2
    exit 1
  fi

  "$KEYCHAIN_SCRIPT" set \
    --account-id "$account_id" \
    --username "$username" \
    --storage-state "$state_file" \
    --session-token "" \
    --session-updated-at "$(/bin/date -u +"%Y-%m-%dT%H:%M:%SZ")"

  echo "[xhs-web-auth] success. storage_state=$state_file"
  "$KEYCHAIN_SCRIPT" status
}

case "$cmd" in
  login)
    ensure_tools
    shift || true
    account_id=""
    username=""
    state_file=""
    login_url="https://creator.xiaohongshu.com/new/home"
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
        --state-file)
          state_file="${2:-}"
          shift 2
          ;;
        --url)
          login_url="${2:-}"
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
    state_file="${state_file:-${STATE_DIR_DEFAULT}/${account_id}.storage-state.json}"
    login_flow "$account_id" "$username" "$state_file" "$login_url"
    ;;
  status)
    "$KEYCHAIN_SCRIPT" status
    ;;
  *)
    usage
    exit 1
    ;;
esac
