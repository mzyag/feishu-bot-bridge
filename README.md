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
- `DAILY_REPORT_CURRENT_WORKDIR`（当前工作窗口目录，用于日报附加 Git/改动快照）
- `DAILY_REPORT_SCOPE`（日报范围，逗号分隔）
  - 默认：`codex_snapshot,work_snapshot`
  - 可选：`session_summary,codex_snapshot,work_snapshot`

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

Daily report includes:

- content controlled by `DAILY_REPORT_SCOPE`
- `session_summary`: session summary from local Codex session logs
- `codex_snapshot`: local Codex runtime snapshot (`.state/codex_threads.json` / `.state/codex_memory.json`)
- `work_snapshot`: current workdir snapshot (git branch / uncommitted changes / commits of the day)

## 3.2 Daily Opportunity Scout (08:00 Feishu + Local Codex)

Configure in `.env`:

- `SCOUT_REPORT_HOUR` / `SCOUT_REPORT_MINUTE` (default `08:00`)
- `SCOUT_SEND_OPEN_ID` (if empty, fallback to `DAILY_REPORT_SEND_OPEN_ID`, then first `ALLOWED_USER_IDS`)
- `SCOUT_CODEX_MODEL` (optional; fallback to `CODEX_MODEL`)
- `SCOUT_CODEX_TIMEOUT_SEC` (default `900`)
- `SCOUT_TARGET_MARKET=global_en`
- `SCOUT_REPORT_LANGUAGE=zh-CN`
- `SCOUT_FALLBACK_POLICY=send_low_confidence`
- `SCOUT_NOVELTY_LOOKBACK_DAYS=3` (去重参考天数，读取最近报告做“题材去重”)
- `SCOUT_NOVELTY_MAX_PENALTY=0.7` (新颖性惩罚上限，越大越倾向避开重复题材)
- `SCOUT_MIN_PAY_SIGNAL=3` (最低付费信号分；低于阈值自动降级为 Low Confidence)
- `SCOUT_MIN_COMMERCIAL_SCORE=3.0` (最低商业清晰度分；综合“付费信号+任务频率”)
- `SCOUT_HUNT_ROUNDS=1` (Phase A 调研轮次；>1 时会多轮检索并合并去重后再选最高分)
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
- phase A 会读取最近 `SCOUT_NOVELTY_LOOKBACK_DAYS` 的已选机会，提示模型优先避开同题材
- 本地排序会对“与近期机会高度相似”的候选加惩罚分（同源域名/同主题词/同集群）
- phase A 额外提取 ICP/付费信号字段（persona、frequency、current spend/workaround、switch trigger）
- 本地评分增加商业可行性维度，优先“有明确付费动机 + 高频痛点 + 单人可交付”的机会
- 当 `SCOUT_HUNT_ROUNDS>1` 时：执行多轮调研、按 URL/题材去重、统一排序，仅输出最高分机会报告

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

## 3.3 Xiaohongshu AI Blogger Daily Ops (Feishu + Local Codex)

Configure in `.env`:

- `XHS_REPORT_HOUR` / `XHS_REPORT_MINUTE` (default `09:00`)
- `XHS_SEND_OPEN_ID` (if empty, fallback to `DAILY_REPORT_SEND_OPEN_ID`, then first `ALLOWED_USER_IDS`)
- `XHS_CODEX_MODEL` (optional; fallback to `CODEX_MODEL`)
- `XHS_CODEX_TIMEOUT_SEC` (default `900`)
- `XHS_NICHE` / `XHS_TARGET_PERSONA` / `XHS_MONETIZATION_GOAL`
- `XHS_BRAND_VOICE` (daily note writing tone)
- `XHS_PUBLISH_WINDOWS=12:30,18:30,21:30`
- `XHS_MAX_POSTS_PER_DAY` / `XHS_MAX_COMMENTS_PER_DAY`
- `XHS_COMMENTS_PER_TOPIC=2`
- `XHS_SIGNAL_MIN_COUNT` (below threshold => `Low Confidence`)
- `XHS_FALLBACK_POLICY=send_low_confidence`
- `XHS_OUTPUT_DIR=${PROJECT_DIR}/reports/xhs-ai-blogger`
- `XHS_COVER_ENABLED=true` (固定单图封面发布链路开关)
- `XHS_COVER_OUTPUT_DIR=${PROJECT_DIR}/reports/xhs-ai-blogger/assets`
- `XHS_COVER_TEMPLATE=minimal_v1`
- `XHS_COVER_SCRIPT=${PROJECT_DIR}/scripts/xhs_cover_generator.py`
- `XHS_COVER_PROVIDER=auto|codex_skill|local`（默认 `auto`：先尝试技能，失败回退本地生成）
- `XHS_COVER_SKILL_PRIMARY=xiaohongshu-images`
- `XHS_COVER_SKILL_SECONDARY=image-generation-mcp`
- `XHS_COVER_SKILL_REQUIRED=true|false`（为 `true` 时技能失败直接报错，不走回退）
- `XHS_COVER_SKILL_FALLBACK_LOCAL=true|false`
- `XHS_COVER_SKILL_TIMEOUT_SEC=1200`
- `XHS_JOB_LOCK_FILE=${PROJECT_DIR}/.state/xhs_ai_blogger_job.lock`
- `XHS_EXECUTOR_ENABLED=true|false`
- `XHS_EXECUTOR_MODE=queue_only|command_hooks`
- `XHS_EXECUTOR_REQUIRE_APPROVAL=true|false`
- `XHS_EXECUTOR_AUTO_APPROVE=true|false`
- `XHS_EXECUTOR_SCRIPT=${PROJECT_DIR}/scripts/xhs_auto_executor.py`
- `XHS_PUBLISH_DRIVER=auto|post_to_xhs|command_hooks`（推荐 `auto`）
- `XHS_POST_TO_XHS_SCRIPT=${HOME}/.codex/skills/post-to-xhs/scripts/publish_pipeline.py`
- `XHS_POST_TO_XHS_HEADLESS=true|false`
- `XHS_POST_TO_XHS_AUTO_PUBLISH=true|false`
- `XHS_POST_TO_XHS_MODE=image-text|long-article`
- `XHS_POST_TO_XHS_ACCOUNT=`（可选，指定 skill 内账号名）
- `XHS_AUTH_MODE=web_session` (recommended)
- `XHS_SESSION_MAX_AGE_HOURS=72`
- `XHS_SESSION_CHECK_REQUIRED=true|false`
- `XHS_HOOK_SESSION_CHECK_CMD` (optional session validation hook)
- `XHS_RELOGIN_HINT` (message shown when session expired)
- `XHS_HOOK_PUBLISH_CMD` / `XHS_HOOK_COMMENT_CMD` (only for `command_hooks`)
- `XHS_ACCOUNT_KEYCHAIN_SERVICE` / `XHS_ACCOUNT_KEYCHAIN_ACCOUNT`
- `XHS_PYTHON_BIN`（可选，指定任务执行 Python；建议指向已安装 `Pillow` 的解释器）

Runtime behavior:

- Phase A: use local Codex + web search to collect trend signals (with URL/timestamp)
- local scoring: relevance + monetization + freshness + competition
- Phase B: generate note drafts + comment interaction plan
- build execution queue (`publish` + `comment`) and write into report JSON
- 每条 publish action 自动生成 1 张封面图，并写入 `action_queue[].images`
- 封面生成支持技能链路：`xiaohongshu-images` -> `image-generation-mcp` -> 本地 `xhs_cover_generator.py`（可配置）
- 发布执行支持 `post-to-xhs`：`XHS_PUBLISH_DRIVER=auto` 时，检测到 skill 即自动走该发布器
- optional executor runs automatically after report generation (approval gate on by default)
- 发布流程开启找不到即停策略（No Retry Policy）：元素缺失时当前动作立即失败并上报，不做同动作重试
- when web session expires, task sends Feishu alert and pauses execution
- output sections: `Today Objective` / `Selected Topics` / `Publishing Plan` / `Engagement Plan` / `Risk/Compliance Checks` / `KPI Snapshot` / `Reflection + Tomorrow Optimization`

Start scheduled task:

```bash
cd "$PROJECT_DIR"
./scripts/launchd_xhs_ai_blogger.sh start
```

Check / stop:

```bash
./scripts/launchd_xhs_ai_blogger.sh status
./scripts/launchd_xhs_ai_blogger.sh stop
```

Dry run (generate only, no Feishu send):

```bash
./scripts/launchd_xhs_ai_blogger.sh dry-run
```

Run once immediately:

```bash
./scripts/launchd_xhs_ai_blogger.sh run-now
```

Task outputs:

- markdown report: `reports/xhs-ai-blogger/YYYY-MM-DD.md`
- research JSON: `reports/xhs-ai-blogger/YYYY-MM-DD.json`
- executor result: `reports/xhs-ai-blogger/YYYY-MM-DD.execution.json` (when executor enabled)
- cover assets: `reports/xhs-ai-blogger/assets/YYYY-MM-DD/cover-1.png`

Configure XHS account in Keychain:

```bash
cd "$PROJECT_DIR"
./scripts/xhs_web_session_auth.sh login --account-id <xhs_account_id> --username <login_name> --url https://creator.xiaohongshu.com/new/home
./scripts/xhs_account_keychain.sh status
```

> `xhs_web_session_auth.sh` 会打开网页让你手动登录小红书，关闭浏览器后自动保存 `storage_state` 并写入 Keychain。

Executor manual run:

```bash
cd "$PROJECT_DIR"
python3 scripts/xhs_auto_executor.py --plan-json reports/xhs-ai-blogger/$(date +%F).json --approve
```

Generate one local cover manually:

```bash
cd "$PROJECT_DIR"
python3 scripts/xhs_cover_generator.py --title "你的标题" --date "$(date +%F)" --keyword "AI提效" --output reports/xhs-ai-blogger/assets/$(date +%F)/cover-1.png
```

`command_hooks` mode example:

```bash
XHS_EXECUTOR_MODE=command_hooks
XHS_HOOK_PUBLISH_CMD='python3 scripts/xhs_web_operator.py publish --storage-state {storage_state} --entry-url https://creator.xiaohongshu.com/new/home --publish-mode image_note --image-strategy text_card --title {title} --content {content} --images {images_csv} --topics {tags_csv} --headful --keep-open-on-fail --hold-seconds-on-fail 1800'
XHS_HOOK_COMMENT_CMD='python3 scripts/xhs_web_operator.py comment --storage-state {storage_state} --browse-url https://www.xiaohongshu.com/explore --topic {topic} --comment {comment} --headful'
# 常用占位符：{storage_state} {title} {content} {topic} {comment} {images_csv} {tags_csv}
# publish 可选：--publish-mode image_note|long_article，--image-strategy text_card|upload
```

> 默认建议先用 `queue_only + require_approval=true` 跑通流程，再切换到 `command_hooks` 真执行。

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
chmod +x .githooks/pre-push .githooks/post-commit scripts/security_scan_before_push.sh scripts/safe_sync_to_github.sh scripts/github_askpass.sh scripts/release.sh
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

## 8) One-Click Release (`v0.x.y`)

Release strategy:
- Use semantic version tags in `v0.x.y` format.
- Default bump is `patch` (for example, `v0.1.0 -> v0.1.1`).
- The script enforces: clean working tree, security scan, sync `main`, tag push, then GitHub release creation.
- Requires GitHub token in Keychain (`./scripts/github_token_keychain.sh set <github_pat_xxx>`).

Create a patch release (default):

```bash
cd "$PROJECT_DIR"
./scripts/release.sh
```

Create a minor/major release:

```bash
./scripts/release.sh --minor
./scripts/release.sh --major
```

Set explicit version:

```bash
./scripts/release.sh --version v0.2.0
```

Optional release controls:

```bash
./scripts/release.sh --notes "Release highlights"
./scripts/release.sh --notes-file ./release-notes.md
./scripts/release.sh --draft
./scripts/release.sh --prerelease
./scripts/release.sh --no-generate-notes
```
