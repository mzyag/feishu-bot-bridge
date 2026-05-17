"""
health_monitor.py - Proactive health monitoring for feishu-bot-bridge.

Checks every 5 minutes:
- Claude persistent session alive
- Message queue not backed up
- Sends Feishu alert to admin on failure
"""

import threading
import time
from typing import Optional


def start_health_monitor(
    claude_session,
    message_queue_instance,
    alert_fn,
    interval_sec: int = 300,
) -> threading.Thread:
    """Start background health check thread.

    alert_fn: callable(text) that sends a message to the admin user.
    """

    def _check_loop():
        while True:
            time.sleep(interval_sec)
            issues = []

            if not claude_session.is_alive():
                issues.append("Claude session 已断开")

            pending = message_queue_instance.pending_count()
            if pending > 5:
                issues.append(f"消息队列堆积: {pending} 条待处理")

            if issues:
                alert_text = f"⚠️ 健康检查告警:\n" + "\n".join(f"- {i}" for i in issues)
                try:
                    alert_fn(alert_text)
                except Exception:
                    pass

    t = threading.Thread(target=_check_loop, name="health-monitor", daemon=True)
    t.start()
    return t
