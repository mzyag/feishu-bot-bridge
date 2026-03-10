#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="/Users/cn/Workspace/feishu-bot-bridge"
LABEL="com.cn.feishu-xhs-ai-blogger"
SRC_PLIST="${PROJECT_DIR}/launchd/${LABEL}.plist"
DST_PLIST="/Users/cn/Library/LaunchAgents/${LABEL}.plist"
OUT_LOG="${PROJECT_DIR}/logs/xhs-ai-blogger.out.log"
ERR_LOG="${PROJECT_DIR}/logs/xhs-ai-blogger.err.log"
ENV_FILE="${PROJECT_DIR}/.env"
PYTHON_BIN=""

cmd="${1:-status}"

load_env_value() {
  local key="$1"
  if [ ! -f "$ENV_FILE" ]; then
    return 0
  fi
  local line
  line="$(grep -E "^${key}=" "$ENV_FILE" | tail -n 1 || true)"
  line="${line#*=}"
  printf '%s' "$line"
}

resolve_python_bin() {
  local from_env detected=""
  from_env="$(load_env_value XHS_PYTHON_BIN)"
  if [ -n "$from_env" ] && [ -x "$from_env" ]; then
    if "$from_env" -c "import PIL" >/dev/null 2>&1; then
      PYTHON_BIN="$from_env"
      return 0
    fi
    detected="$from_env"
  fi

  local candidates=()
  local shell_python
  shell_python="$(command -v python3 2>/dev/null || true)"
  if [ -n "$shell_python" ]; then
    candidates+=("$shell_python")
  fi
  candidates+=(
    "/opt/anaconda3/bin/python3"
    "/opt/homebrew/bin/python3"
    "/usr/local/bin/python3"
    "/usr/bin/python3"
  )

  local candidate
  for candidate in "${candidates[@]}"; do
    [ -x "$candidate" ] || continue
    if "$candidate" -c "import PIL" >/dev/null 2>&1; then
      PYTHON_BIN="$candidate"
      return 0
    fi
    if [ -z "$detected" ]; then
      detected="$candidate"
    fi
  done

  if [ -n "$detected" ]; then
    PYTHON_BIN="$detected"
    return 0
  fi
  echo "error: no usable python3 interpreter found" >&2
  exit 1
}

generate_plist() {
  local hour minute
  hour="$(load_env_value XHS_REPORT_HOUR)"
  minute="$(load_env_value XHS_REPORT_MINUTE)"
  hour="${hour:-9}"
  minute="${minute:-0}"
  resolve_python_bin

  mkdir -p "${PROJECT_DIR}/launchd" "/Users/cn/Library/LaunchAgents" "${PROJECT_DIR}/logs"
  cat > "$SRC_PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${LABEL}</string>

  <key>ProgramArguments</key>
  <array>
    <string>${PYTHON_BIN}</string>
    <string>-u</string>
    <string>${PROJECT_DIR}/scripts/xhs_ai_blogger_job.py</string>
  </array>

  <key>WorkingDirectory</key>
  <string>${PROJECT_DIR}</string>

  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
    <key>PYTHONUNBUFFERED</key>
    <string>1</string>
  </dict>

  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key>
    <integer>${hour}</integer>
    <key>Minute</key>
    <integer>${minute}</integer>
  </dict>

  <key>RunAtLoad</key>
  <false/>
  <key>KeepAlive</key>
  <false/>

  <key>StandardOutPath</key>
  <string>${OUT_LOG}</string>
  <key>StandardErrorPath</key>
  <string>${ERR_LOG}</string>
</dict>
</plist>
EOF
  cp "$SRC_PLIST" "$DST_PLIST"
  plutil -lint "$DST_PLIST" >/dev/null
  echo "generated plist at ${DST_PLIST} (time ${hour}:${minute}, python ${PYTHON_BIN})"
}

case "$cmd" in
  start)
    generate_plist
    launchctl unload "$DST_PLIST" >/dev/null 2>&1 || true
    launchctl load -w "$DST_PLIST"
    echo "started ${LABEL}"
    ;;
  stop)
    launchctl unload "$DST_PLIST" >/dev/null 2>&1 || true
    echo "stopped ${LABEL}"
    ;;
  restart)
    "$0" stop
    "$0" start
    ;;
  status)
    launchctl list | grep "$LABEL" || true
    ;;
  logs)
    tail -n 200 "$OUT_LOG" 2>/dev/null || true
    echo "---"
    tail -n 200 "$ERR_LOG" 2>/dev/null || true
    ;;
  run-now)
    cd "$PROJECT_DIR"
    resolve_python_bin
    "$PYTHON_BIN" -u "$PROJECT_DIR/scripts/xhs_ai_blogger_job.py"
    ;;
  dry-run)
    cd "$PROJECT_DIR"
    resolve_python_bin
    "$PYTHON_BIN" -u "$PROJECT_DIR/scripts/xhs_ai_blogger_job.py" --dry-run
    ;;
  *)
    echo "usage: $0 {start|stop|restart|status|logs|run-now|dry-run}" >&2
    exit 1
    ;;
esac
