#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

PUSH_ONLY=0
NO_SCAN=0
QUIET=0
COMMIT_MSG=""

while [ $# -gt 0 ]; do
  case "$1" in
    --push-only)
      PUSH_ONLY=1
      shift
      ;;
    --no-scan)
      NO_SCAN=1
      shift
      ;;
    --quiet)
      QUIET=1
      shift
      ;;
    *)
      COMMIT_MSG="$1"
      shift
      ;;
  esac
done

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "[safe-sync] not a git repository" >&2
  exit 2
fi

BRANCH="$(git branch --show-current)"
if [ -z "$BRANCH" ]; then
  echo "[safe-sync] no active branch" >&2
  exit 2
fi

if [ "$NO_SCAN" -ne 1 ]; then
  ./scripts/security_scan_before_push.sh
fi

if [ "$PUSH_ONLY" -ne 1 ]; then
  if [ -n "$(git status --porcelain)" ]; then
    git add -A
    MSG="${COMMIT_MSG:-chore: auto sync after local changes}"
    SAFE_SYNC_IN_PROGRESS=1 git commit -m "$MSG"
  fi
fi

if [ -z "$(git remote 2>/dev/null | tr -d '[:space:]')" ]; then
  echo "[safe-sync] no git remote configured, skip push"
  exit 0
fi

if [ "$QUIET" -ne 1 ]; then
  echo "[safe-sync] pushing branch: ${BRANCH}"
fi

if [ -x ./scripts/github_token_keychain.sh ]; then
  if TOKEN="$(./scripts/github_token_keychain.sh get 2>/dev/null)"; then
    BASIC="$(printf 'x-access-token:%s' "$TOKEN" | base64)"
    git \
      -c http.version=HTTP/1.1 \
      -c http.https://github.com/.extraheader="AUTHORIZATION: basic ${BASIC}" \
      push origin "$BRANCH"
    exit 0
  fi
fi

git push origin "$BRANCH"
