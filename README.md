# Feishu Bot Bridge

[English](#english) | [中文](#中文)

---

## English

A multi-channel AI bot bridge that connects **Feishu (Lark)** and **WeChat** to **Claude Code CLI**, enabling phone-based control of your development environment.

### Features

- **Dual channel**: Feishu (WebSocket) + WeChat (long-poll via ilinkai API)
- **Claude Code persistent session**: bidirectional stream-json, maintains context across messages
- **Multi-agent team mode**: Dispatcher (Claude) + Executor (Claude Code + DeepSeek) + Reviewer (Claude)
- **TDD workflow**: auto-generates tests before execution, validates after
- **Harness compliance**: follows H0-H6 workspace skill harness
- **Session warmup**: Claude process pre-starts at boot, ready when first message arrives
- **Watchdog**: kills stalled processes (90s no stdout), auto-restart with backoff
- **Unified message queue**: platform-agnostic task processing with deduplication

### Architecture

```
┌──────────┐  ┌──────────┐  ┌──────────┐
│  Feishu   │  │  WeChat   │  │  Future   │
└─────┬────┘  └─────┬────┘  └─────┬────┘
      │              │              │
      └──────┬───────┘──────────────┘
             ▼
    ┌─────────────────┐
    │  Message Queue   │   unified task processing
    └────────┬────────┘
             │
     ┌───────┴────────┐
     │   Router        │   keyword → skill / team / single
     └───────┬────────┘
             │
    ┌────────┴─────────┐
    │ Single: Claude    │   direct reply (file access, tools)
    │ Team: multi-agent │   Dispatcher → Executor → Reviewer
    └──────────────────┘
```

### Multi-Agent Team Mode

Triggered automatically for development tasks or manually via `/team <request>`.

```
User sends task
  → H0: Dispatcher analyzes requirements + risk assessment
  → User confirms requirement understanding
  → H2: Dispatcher creates execution plan (with acceptance criteria)
  → User confirms plan
  → H3: For each step (TDD):
       1. Generate test from acceptance criteria
       2. DeepSeek drafts solution (advisor)
       3. Claude Code executes (real file access)
       4. Run pre-generated test
       5. Validate step output
  → H4: Final review (Reviewer)
  → H5: Structured delivery report
  → H6: Post-task episode capture
```

### File Structure

```
ws_bot.py           → Entry point: routing, event handler, main()
config.py           → Settings, ReplyResult
feishu_api.py       → Feishu IM API (reply/update)
state.py            → State management (thread/memory/session)
text_utils.py       → Text parsing, command detection
log_viewer.py       → Log/status/trace formatting
codex_runner.py     → Codex CLI executor
claude_session.py   → Claude persistent session (stream-json)
message_queue.py    → Unified message queue
multi_agent.py      → Team mode orchestration (TDD)
wx_channel.py       → WeChat channel (ilinkai long-poll)
```

### Setup

```bash
cd feishu-bot-bridge
python3 -m pip install -r requirements.txt
cp .env.example .env
# Edit .env with your credentials
```

Required in `.env`:
- `FEISHU_APP_ID` / `FEISHU_APP_SECRET` — from Feishu console
- `CLAUDE_CODE_OAUTH_TOKEN` — from `claude setup-token`
- `ALLOWED_USER_IDS` — Feishu open_id whitelist

Optional:
- `WX_BOT_ENABLED=true` + `WX_BOT_TOKEN` — enable WeChat channel
- `DEEPSEEK_API_KEY` — enable DeepSeek advisor in team mode
- Proxy settings (`http_proxy`, `https_proxy`) — required if behind GFW

### Run

```bash
# Direct
python3 ws_bot.py

# As launchd service (recommended)
./scripts/launchd_manage.sh start
./scripts/launchd_manage.sh status
./scripts/launchd_manage.sh stop
```

### Message Prefixes

| Prefix | Effect |
|--------|--------|
| (none) | Default backend (Claude) |
| `/cc` | Force Claude Code |
| `/codex` | Force Codex |
| `/team <msg>` | Force team mode |
| `/reset` | Clear session + memory |

---

## 中文

多通道 AI Bot 桥接服务，连接**飞书**和**微信**到**Claude Code CLI**，实现手机端远程控制开发环境。

### 功能特性

- **双通道**：飞书（WebSocket 长连接）+ 微信（ilinkai API 长轮询）
- **Claude Code 持久进程**：双向 stream-json，跨消息保持上下文
- **多 Agent 团队模式**：调度员(Claude) + 执行者(Claude Code + DeepSeek) + 审查员(Claude)
- **TDD 工作流**：执行前自动生成测试，执行后验证
- **Harness 合规**：遵循 H0-H6 workspace skill harness 规范
- **Session 预热**：bot 启动时立即预热 Claude 进程，首条消息到达时已就绪
- **Watchdog 守护**：检测僵死进程（90s 无输出）→ 自动 kill + 重启（带 backoff）
- **统一消息队列**：平台无关的任务处理，内置去重

### 架构

```
┌──────────┐  ┌──────────┐  ┌──────────┐
│   飞书    │  │   微信    │  │  未来平台  │
└─────┬────┘  └─────┬────┘  └─────┬────┘
      │              │              │
      └──────┬───────┘──────────────┘
             ▼
    ┌─────────────────┐
    │   消息队列       │   统一任务处理
    └────────┬────────┘
             │
     ┌───────┴────────┐
     │   路由器        │   关键词 → skill / 团队 / 单次
     └───────┬────────┘
             │
    ┌────────┴─────────┐
    │ 单次: Claude 直答  │   直接回复（可读写文件、运行命令）
    │ 团队: 多Agent协作  │   调度 → 执行 → 审查
    └──────────────────┘
```

### 多 Agent 团队模式

开发任务自动触发，或手动 `/team <需求>` 强制进入。

```
用户发送任务
  → H0: 调度员分析需求 + 风险评估
  → 用户确认需求理解
  → H2: 调度员制定执行计划（含验收标准）
  → 用户确认计划
  → H3: 逐步执行（TDD）:
       1. 根据验收标准生成测试
       2. DeepSeek 生成方案（顾问）
       3. Claude Code 执行（真实文件操作）
       4. 运行预生成测试
       5. 验证步骤输出
  → H4: 最终审查（审查员）
  → H5: 结构化交付报告
  → H6: 事后经验记录
```

### 文件结构

```
ws_bot.py           → 入口：路由、事件处理、main()
config.py           → 配置(Settings)、回复结果(ReplyResult)
feishu_api.py       → 飞书 IM API（发送/更新消息）
state.py            → 状态管理（会话/记忆/session）
text_utils.py       → 文本解析、命令识别
log_viewer.py       → 日志/状态/trace 格式化
codex_runner.py     → Codex CLI 执行器
claude_session.py   → Claude 持久进程（stream-json 双向通信）
message_queue.py    → 统一消息队列
multi_agent.py      → 团队模式编排（TDD）
wx_channel.py       → 微信通道（ilinkai 长轮询）
```

### 安装

```bash
cd feishu-bot-bridge
python3 -m pip install -r requirements.txt
cp .env.example .env
# 编辑 .env 填入凭证
```

`.env` 必填项：
- `FEISHU_APP_ID` / `FEISHU_APP_SECRET` — 飞书开放平台
- `CLAUDE_CODE_OAUTH_TOKEN` — 通过 `claude setup-token` 获取
- `ALLOWED_USER_IDS` — 飞书 open_id 白名单

可选项：
- `WX_BOT_ENABLED=true` + `WX_BOT_TOKEN` — 启用微信通道
- `DEEPSEEK_API_KEY` — 启用 DeepSeek 顾问（团队模式）
- 代理设置（`http_proxy`, `https_proxy`）— 墙内必须配置

### 运行

```bash
# 直接运行
python3 ws_bot.py

# launchd 服务（推荐）
./scripts/launchd_manage.sh start
./scripts/launchd_manage.sh status
./scripts/launchd_manage.sh stop
```

### 消息前缀

| 前缀 | 效果 |
|------|------|
| （无） | 默认后端（Claude） |
| `/cc` | 强制 Claude Code |
| `/codex` | 强制 Codex |
| `/team <消息>` | 强制团队模式 |
| `/reset` | 清空会话 + 记忆 |

### 安全注意

- `CLAUDE_PERMISSION_MODE=bypassPermissions` 允许 CLI 执行任何命令，仅在个人可控机器 + 白名单保护下使用
- `.env` 含敏感凭证，已在 `.gitignore` 中，不会被提交
- 空白名单 = 拒绝所有消息（fail-closed）
