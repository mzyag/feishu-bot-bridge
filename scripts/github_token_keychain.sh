#!/usr/bin/env bash
set -euo pipefail

SERVICE="${GITHUB_TOKEN_KEYCHAIN_SERVICE:-feishu-bot-bridge.github.token}"
ACCOUNT="${GITHUB_TOKEN_KEYCHAIN_ACCOUNT:-mzyag}"
cmd="${1:-status}"

usage() {
  cat <<EOF
usage: $0 {set|get|status|delete}

Environment (optional):
  GITHUB_TOKEN_KEYCHAIN_SERVICE   default: ${SERVICE}
  GITHUB_TOKEN_KEYCHAIN_ACCOUNT   default: ${ACCOUNT}
EOF
}

case "$cmd" in
  set)
    token="${2:-}"
    if [ -z "$token" ]; then
      echo "missing token argument" >&2
      echo "example: $0 set <github_pat_xxx>" >&2
      exit 1
    fi
    security add-generic-password -U -a "$ACCOUNT" -s "$SERVICE" -w "$token" >/dev/null
    echo "stored: service=${SERVICE}, account=${ACCOUNT}"
    ;;
  get)
    security find-generic-password -a "$ACCOUNT" -s "$SERVICE" -w
    ;;
  status)
    if security find-generic-password -a "$ACCOUNT" -s "$SERVICE" >/dev/null 2>&1; then
      echo "exists: service=${SERVICE}, account=${ACCOUNT}"
    else
      echo "missing: service=${SERVICE}, account=${ACCOUNT}"
      exit 1
    fi
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
