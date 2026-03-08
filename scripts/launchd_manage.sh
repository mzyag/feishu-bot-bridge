#!/usr/bin/env bash
set -euo pipefail

LABEL="com.cn.feishu-bot-bridge"
SRC_PLIST="/Users/cn/Workspace/feishu-bot-bridge/launchd/${LABEL}.plist"
DST_PLIST="/Users/cn/Library/LaunchAgents/${LABEL}.plist"
cmd="${1:-status}"

install_plist() {
  mkdir -p "/Users/cn/Library/LaunchAgents" "/Users/cn/Workspace/feishu-bot-bridge/logs"
  cp "$SRC_PLIST" "$DST_PLIST"
}

case "$cmd" in
  start)
    install_plist
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
    tail -n 200 "/Users/cn/Workspace/feishu-bot-bridge/logs/launchd.out.log" 2>/dev/null || true
    echo "---"
    tail -n 200 "/Users/cn/Workspace/feishu-bot-bridge/logs/launchd.err.log" 2>/dev/null || true
    ;;
  *)
    echo "usage: $0 {start|stop|restart|status|logs}" >&2
    exit 1
    ;;
esac
