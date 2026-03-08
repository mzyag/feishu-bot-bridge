#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="/Users/cn/Workspace/feishu-bot-bridge"
LABEL="com.cn.feishu-opportunity-scout-watchdog"
SRC_PLIST="${PROJECT_DIR}/launchd/${LABEL}.plist"
DST_PLIST="/Users/cn/Library/LaunchAgents/${LABEL}.plist"
OUT_LOG="${PROJECT_DIR}/logs/opportunity-scout-watchdog.out.log"
ERR_LOG="${PROJECT_DIR}/logs/opportunity-scout-watchdog.err.log"
ENV_FILE="${PROJECT_DIR}/.env"

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

generate_plist() {
  local interval_sec
  interval_sec="$(load_env_value SCOUT_WATCHDOG_INTERVAL_SEC)"
  interval_sec="${interval_sec:-360}"

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
    <string>/usr/bin/python3</string>
    <string>-u</string>
    <string>${PROJECT_DIR}/scripts/opportunity_scout_watchdog.py</string>
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

  <key>RunAtLoad</key>
  <true/>

  <key>StartInterval</key>
  <integer>${interval_sec}</integer>

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
  echo "generated plist at ${DST_PLIST} (interval=${interval_sec}s)"
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
    /usr/bin/python3 -u "$PROJECT_DIR/scripts/opportunity_scout_watchdog.py"
    ;;
  dry-run)
    cd "$PROJECT_DIR"
    /usr/bin/python3 -u "$PROJECT_DIR/scripts/opportunity_scout_watchdog.py" --dry-run
    ;;
  *)
    echo "usage: $0 {start|stop|restart|status|logs|run-now|dry-run}" >&2
    exit 1
    ;;
esac
