# Feishu Bot Bridge (Long Connection SDK)

Feishu long-connection bot using `lark-oapi`:
- persistent connection mode (no callback URL needed)
- text message handling
- user whitelist (`ALLOWED_USER_IDS`)
- reply backend priority: local `codex` CLI -> OpenAI API -> echo fallback
- duplicate event/message suppression (avoid double replies)
- per-user Codex thread reuse via `codex exec resume` (reduces cold-start overhead)
- per-user short-term local memory fallback (context survives thread reset/timeout)
- long-running requests use a placeholder reply then update the same message in place
- periodic task status updates (default every 3 seconds) during long runs
- auto fallback to new status messages when Feishu edit limit is reached
- auto bypass proxy for `*.feishu.cn` to reduce websocket reconnect failures

## 1) Install

Use path-safe variables (avoid hardcoded `/Users/...`):

```bash
export WORKSPACE_ROOT="${WORKSPACE_ROOT:-$HOME/Workspace}"
export PROJECT_DIR="${PROJECT_DIR:-$WORKSPACE_ROOT/feishu-bot-bridge}"
```

```bash
cd "$PROJECT_DIR"
python3 -m pip install -r requirements.txt
```

## 2) Configure

Edit `.env`:

- `FEISHU_APP_ID`
- `FEISHU_APP_SECRET`
- `FEISHU_HTTP_TIMEOUT_SEC=20` (avoid long blocking when Feishu API/network is unstable)
- `OPENAI_API_KEY` (optional; if empty, service uses echo reply)
- `ALLOWED_USER_IDS` (comma-separated Feishu `open_id`, e.g. `ou_xxx,ou_yyy`)
- `USE_CODEX_CLI=true` (default; prefer local codex CLI)
- `CODEX_WORKDIR=${WORKSPACE_ROOT}` (codex execution root)
- `CODEX_PROJECT_ROOT=${WORKSPACE_ROOT}` (飞书里“新建项目”默认落地目录)
- `CODEX_SANDBOX=workspace-write` (allow writing inside `CODEX_WORKDIR`)
- `CODEX_ADD_DIRS=` (optional comma-separated extra writable dirs)
- `CODEX_RESUME_ENABLED=true` (recommended; reuse per-user Codex thread for context continuity)
- `CODEX_THREAD_STATE_FILE=.state/codex_threads.json` (store per-user Codex session ids)
- `CODEX_MEMORY_ENABLED=true` (recommended; local fallback memory when a fresh thread is created)
- `CODEX_MEMORY_TURNS=6` (keep recent N turns, each turn=user+assistant)
- `CODEX_MEMORY_STATE_FILE=.state/codex_memory.json` (store per-user short memory)
- `CODEX_STATUS_UPDATE_ENABLED=true` (send in-place status updates while task is running)
- `CODEX_STATUS_POLL_SEC=3` (status check + update interval in seconds)
- `CODEX_STATUS_FOLLOWUP_SEC=30` (fallback status push interval after edit-limit error)
- `DEDUPE_TTL_SEC=900` and `DEDUPE_MAX_IDS=2000` (duplicate suppression window/cache size)

Tip:
- send `/reset` (or `重置会话` / `清空记忆`) in Feishu to clear both thread + local memory for your account.

## 3) Run

```bash
cd "$PROJECT_DIR"
python3 ws_bot.py
```

Run as launchd service (recommended):

```bash
cd "$PROJECT_DIR"
./scripts/launchd_manage.sh start
```

Stop service:

```bash
cd "$PROJECT_DIR"
./scripts/launchd_manage.sh stop
```

Check service status:

```bash
cd "$PROJECT_DIR"
./scripts/launchd_manage.sh status
```

View fixed logs:

```bash
cd "$PROJECT_DIR"
./scripts/launchd_manage.sh logs
```

## 3.1 Daily Auto Report (Feishu + Memory)

Configure in `.env`:

- `DAILY_REPORT_HOUR` / `DAILY_REPORT_MINUTE` (default `22:30`)
- `DAILY_REPORT_DATE_MODE=today|yesterday`
- `DAILY_REPORT_SEND_OPEN_ID` (if empty, fallback to first `ALLOWED_USER_IDS`)
- `DAILY_REPORT_WORKSPACE_ROOT` (memory root)
- `DAILY_REPORT_SESSIONS_DIR` (session source)

Start scheduled task:

```bash
cd "$PROJECT_DIR"
./scripts/launchd_daily_report.sh start
```

Check / stop:

```bash
./scripts/launchd_daily_report.sh status
./scripts/launchd_daily_report.sh stop
```

Dry run (generate only, no Feishu send):

```bash
./scripts/launchd_daily_report.sh dry-run
```

Run once immediately:

```bash
./scripts/launchd_daily_report.sh run-now
```

Task outputs:

- report: `reports/daily-YYYY-MM-DD.md`
- memory diary: `memory/diary/YYYY/daily/YYYY-MM-DD.md`
- session-memory sync (if available): `memory/YYYY-MM-DD.md`

## 3.2 Daily Opportunity Scout (08:00 Feishu + Local Codex)

Configure in `.env`:

- `SCOUT_REPORT_HOUR` / `SCOUT_REPORT_MINUTE` (default `08:00`)
- `SCOUT_SEND_OPEN_ID` (if empty, fallback to `DAILY_REPORT_SEND_OPEN_ID`, then first `ALLOWED_USER_IDS`)
- `SCOUT_CODEX_MODEL` (optional; fallback to `CODEX_MODEL`)
- `SCOUT_CODEX_TIMEOUT_SEC` (default `900`)
- `SCOUT_TARGET_MARKET=global_en`
- `SCOUT_REPORT_LANGUAGE=zh-CN`
- `SCOUT_FALLBACK_POLICY=send_low_confidence`
- `SCOUT_OUTPUT_DIR=${PROJECT_DIR}/reports/opportunity-scout`
- `SCOUT_JOB_LOCK_FILE=${PROJECT_DIR}/.state/opportunity_scout_job.lock` (防并发重跑)
- `SCOUT_WATCHDOG_INTERVAL_SEC=360` (boot 后每 6 分钟巡检一次)
- `SCOUT_WATCHDOG_GRACE_MIN=20` (超过计划时间后多少分钟开始判定“漏跑”)
- `SCOUT_WATCHDOG_CODEX_TIMEOUT_SEC=1800` (watchdog 补跑的超时保护；仅在 `SCOUT_CODEX_TIMEOUT_SEC<=0` 时生效)
- `SCOUT_WATCHDOG_STATE_FILE=${PROJECT_DIR}/.state/opportunity_scout_watchdog.json`

Runtime behavior:

- runs local `codex exec --search` in read-only mode for both research phases
- does not require `OPENAI_API_KEY` for the scout task
- still uses Feishu HTTP API to send the final report

Start scheduled task:

```bash
cd "$PROJECT_DIR"
./scripts/launchd_opportunity_scout.sh start
```

Watchdog behavior:

- boot 后立即执行一次检查，之后每 6 分钟检查一次
- 若当天 `08:00`（加 `SCOUT_WATCHDOG_GRACE_MIN` 缓冲）后仍未产出 `YYYY-MM-DD.md/.json`，自动补跑一次
- 若补跑失败，向飞书发送失败告警消息
- 若已有 scout 任务在跑，watchdog 会跳过本次补跑，避免并发重复执行

Check / stop:

```bash
./scripts/launchd_opportunity_scout.sh status
./scripts/launchd_opportunity_scout.sh stop
```

Dry run (generate only, no Feishu send):

```bash
./scripts/launchd_opportunity_scout.sh dry-run
```

Run once immediately:

```bash
./scripts/launchd_opportunity_scout.sh run-now
```

Task outputs:

- markdown report: `reports/opportunity-scout/YYYY-MM-DD.md`
- research JSON: `reports/opportunity-scout/YYYY-MM-DD.json`

## 4) Feishu Console

Use:
- `Event configuration` -> `Receive events through persistent connection`

Enable event:
- `im.message.receive_v1`

## 5) GitHub Token Storage (Keychain)

For local GitHub automation, store PAT in macOS Keychain instead of `.env`:

```bash
cd "$PROJECT_DIR"
./scripts/github_token_keychain.sh set <github_pat_xxx>
./scripts/github_token_keychain.sh status
```

Available commands:

```bash
# Store / update token
./scripts/github_token_keychain.sh set <github_pat_xxx>

# Check whether token exists (does not print token)
./scripts/github_token_keychain.sh status

# Read token (for scripting only; avoid printing in shared terminals)
./scripts/github_token_keychain.sh get

# Delete token from keychain
./scripts/github_token_keychain.sh delete
```

Optional environment variables:

```bash
export GITHUB_TOKEN_KEYCHAIN_SERVICE="feishu-bot-bridge.github.token"
export GITHUB_TOKEN_KEYCHAIN_ACCOUNT="mzyag"
```

## 6) Cloud Server Credential Storage (Keychain)

Store cloud server login credentials in macOS Keychain:

```bash
cd "$PROJECT_DIR"
./scripts/cloud_server_keychain.sh set --host <ip-or-host> --user <username> --password '<password>'
```

Available commands:

```bash
# Example: save credentials
./scripts/cloud_server_keychain.sh set --host <server-ip> --user <ssh-user> --password '<password>'

# Check whether credentials exist (host/user shown, password masked)
./scripts/cloud_server_keychain.sh status

# Read full JSON payload (contains password; use carefully)
./scripts/cloud_server_keychain.sh get

# Delete credentials
./scripts/cloud_server_keychain.sh delete
```

Optional environment variables:

```bash
export CLOUD_SERVER_KEYCHAIN_SERVICE="feishu-bot-bridge.cloud.server"
export CLOUD_SERVER_KEYCHAIN_ACCOUNT="default"
```

## 7) Auto Sync + Security Policy (Required)

Policy:
- Every push must pass security scan.
- After code commit, auto sync to GitHub.
- If private information is detected, push is blocked until masked.

Enable hooks once per clone:

```bash
cd "$PROJECT_DIR"
git config core.hooksPath .githooks
chmod +x .githooks/pre-push .githooks/post-commit scripts/security_scan_before_push.sh scripts/safe_sync_to_github.sh
```

Manual safe sync command:

```bash
# Scan -> commit (if needed) -> push
./scripts/safe_sync_to_github.sh "chore: your commit message"
```

Security scan only:

```bash
./scripts/security_scan_before_push.sh
```

Optional controls:

```bash
# Disable auto push for current shell/session
export AUTO_SYNC_TO_GITHUB=false
```
