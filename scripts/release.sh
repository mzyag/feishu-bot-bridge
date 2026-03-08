#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

DEFAULT_START_VERSION="v0.1.0"
BUMP_TYPE="patch"
VERSION=""
DRAFT=0
PRERELEASE=0
GENERATE_NOTES=1
NOTES=""
NOTES_FILE=""
REPO_OVERRIDE=""

usage() {
  cat <<'EOF'
usage: ./scripts/release.sh [options]

Options:
  --patch                 bump patch version (default)
  --minor                 bump minor version
  --major                 bump major version
  --version v0.x.y        set version explicitly
  --notes "text"          release notes text
  --notes-file <file>     release notes from file
  --no-generate-notes     disable GitHub auto-generated notes
  --draft                 create as draft release
  --prerelease            create as prerelease
  --repo owner/repo       override repository parsed from origin
  -h, --help              show this help
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --patch)
      BUMP_TYPE="patch"
      shift
      ;;
    --minor)
      BUMP_TYPE="minor"
      shift
      ;;
    --major)
      BUMP_TYPE="major"
      shift
      ;;
    --version)
      VERSION="${2:-}"
      shift 2
      ;;
    --notes)
      NOTES="${2:-}"
      shift 2
      ;;
    --notes-file)
      NOTES_FILE="${2:-}"
      shift 2
      ;;
    --no-generate-notes)
      GENERATE_NOTES=0
      shift
      ;;
    --draft)
      DRAFT=1
      shift
      ;;
    --prerelease)
      PRERELEASE=1
      shift
      ;;
    --repo)
      REPO_OVERRIDE="${2:-}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "[release] unknown argument: $1" >&2
      usage
      exit 2
      ;;
  esac
done

if [ -n "$NOTES" ] && [ -n "$NOTES_FILE" ]; then
  echo "[release] only one of --notes and --notes-file can be used" >&2
  exit 2
fi

if [ -n "$NOTES_FILE" ] && [ ! -f "$NOTES_FILE" ]; then
  echo "[release] notes file not found: $NOTES_FILE" >&2
  exit 2
fi

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "[release] not a git repository" >&2
  exit 2
fi

if [ -n "$(git status --porcelain)" ]; then
  echo "[release] working tree is not clean, commit/stash changes first" >&2
  exit 2
fi

if [ ! -x ./scripts/github_token_keychain.sh ]; then
  echo "[release] missing token helper: ./scripts/github_token_keychain.sh" >&2
  exit 2
fi

if ! TOKEN="$(./scripts/github_token_keychain.sh get 2>/dev/null)"; then
  echo "[release] cannot read GitHub token from keychain" >&2
  exit 2
fi

if [ -z "$VERSION" ]; then
  LATEST_TAG="$(git tag --list 'v0.*.*' --sort=-v:refname | head -n 1)"
  VERSION="$(
    /usr/bin/python3 - "$LATEST_TAG" "$BUMP_TYPE" "$DEFAULT_START_VERSION" <<'PY'
import re
import sys

latest, bump, default_start = sys.argv[1], sys.argv[2], sys.argv[3]

def parse(tag: str):
    m = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)", tag)
    if not m:
        return None
    return [int(m.group(1)), int(m.group(2)), int(m.group(3))]

if not latest:
    print(default_start)
    sys.exit(0)

parts = parse(latest)
if parts is None:
    print(default_start)
    sys.exit(0)

major, minor, patch = parts
if bump == "major":
    major += 1
    minor = 0
    patch = 0
elif bump == "minor":
    minor += 1
    patch = 0
else:
    patch += 1

print(f"v{major}.{minor}.{patch}")
PY
  )"
fi

if ! [[ "$VERSION" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "[release] invalid version format: $VERSION (expected v0.x.y)" >&2
  exit 2
fi

if git rev-parse -q --verify "refs/tags/$VERSION" >/dev/null; then
  echo "[release] tag already exists locally: $VERSION" >&2
  exit 2
fi

OWNER_REPO="$REPO_OVERRIDE"
if [ -z "$OWNER_REPO" ]; then
  REMOTE_URL="$(git remote get-url origin 2>/dev/null || true)"
  OWNER_REPO="$(
    echo "$REMOTE_URL" \
      | sed -E 's#(git@github.com:|https://github.com/)([^/]+/[^/.]+)(\.git)?#\2#'
  )"
fi

if ! [[ "$OWNER_REPO" =~ ^[^/]+/[^/]+$ ]]; then
  echo "[release] cannot parse owner/repo from origin, use --repo owner/repo" >&2
  exit 2
fi

OWNER="${OWNER_REPO%/*}"
REPO="${OWNER_REPO#*/}"

./scripts/safe_sync_to_github.sh --push-only --quiet

if GIT_TERMINAL_PROMPT=0 git -c credential.helper= ls-remote --tags origin "refs/tags/$VERSION" | rg -q "$VERSION$"; then
  echo "[release] tag already exists on remote: $VERSION" >&2
  exit 2
fi

./scripts/security_scan_before_push.sh

git tag -a "$VERSION" -m "feishu-bot-bridge $VERSION"

BASIC="$(printf 'x-access-token:%s' "$TOKEN" | base64)"
git \
  -c credential.helper= \
  -c http.version=HTTP/1.1 \
  -c http.https://github.com/.extraheader="AUTHORIZATION: basic ${BASIC}" \
  push origin "$VERSION"

if [ -n "$NOTES_FILE" ]; then
  BODY="$(cat "$NOTES_FILE")"
else
  BODY="$NOTES"
fi

/usr/bin/python3 - "$TOKEN" "$OWNER" "$REPO" "$VERSION" "$DRAFT" "$PRERELEASE" "$GENERATE_NOTES" "$BODY" <<'PY'
import json
import sys
from urllib import error, request

token, owner, repo, version = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
draft = bool(int(sys.argv[5]))
prerelease = bool(int(sys.argv[6]))
generate_notes = bool(int(sys.argv[7]))
body = sys.argv[8]

payload = {
    "tag_name": version,
    "name": version,
    "draft": draft,
    "prerelease": prerelease,
    "generate_release_notes": generate_notes,
}
if body:
    payload["body"] = body

req = request.Request(
    f"https://api.github.com/repos/{owner}/{repo}/releases",
    data=json.dumps(payload).encode("utf-8"),
    method="POST",
    headers={
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
        "User-Agent": "feishu-bot-bridge-release-script",
    },
)

try:
    with request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode("utf-8"))
except error.HTTPError as exc:
    raw = exc.read().decode("utf-8", errors="replace")
    try:
        msg = json.loads(raw).get("message", raw)
    except Exception:
        msg = raw
    print(f"[release] GitHub API error {exc.code}: {msg}", file=sys.stderr)
    sys.exit(1)

print(f"[release] created: {data.get('html_url')}")
PY

echo "[release] done: ${VERSION}"
