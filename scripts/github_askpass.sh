#!/usr/bin/env bash
set -euo pipefail

PROMPT="${1:-}"

case "$PROMPT" in
  *Username*|*username*)
    echo "x-access-token"
    ;;
  *Password*|*password*)
    exec "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/github_token_keychain.sh" get
    ;;
  *)
    echo ""
    ;;
esac
