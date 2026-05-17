# 从飞书 Bot 到多 Agent 开发团队：手机控制 Claude Code 的完整实践

> 一个人 + 一台 Mac + 飞书/微信 = 随时随地远程操控 AI 写代码

## 起因

坐地铁时想改个 bug，打开手机发现只能干瞪着。SSH + tmux 方案能用，但在手机上敲命令行太痛苦。能不能直接在飞书/微信里发一句话，让 Claude Code 帮我改？

## 最终效果

手机飞书/微信发消息 → Claude Code 在 Mac 上执行（读写文件、跑命令）→ 结构化回复到手机。

复杂任务自动走多 Agent 团队模式：调度员拆任务 → DeepSeek 出方案 → Claude Code 执行 → 自动测试 → 审查员 review → 交付报告。

## 技术选型踩坑

### 1. 非交互模式的选择

Claude Code CLI 有几种用法：

| 方式 | 适用场景 | 问题 |
|------|----------|------|
| `claude -p "prompt"` | 单次调用 | 每次冷启动 5-10s，MCP 加载慢 |
| `claude -p --resume <id>` | 续聊 | 仍是每次 spawn 新进程 |
| `claude -p --input-format stream-json` | **持久进程** | 一次启动，stdin/stdout 双向通信 |

最终选了 **stream-json 持久进程**：进程只启动一次，通过 stdin 发消息、stdout 读回复。上下文天然保持，无冷启动开销。

### 2. launchd 环境的坑

Mac launchd 启动的进程环境极其精简，遇到的问题：

- **没有代理** → API 403（Claude API 被墙）→ 在 plist 里加 `http_proxy`/`https_proxy`
- **Keychain 认证不稳定** → OAuth token 刷新间歇失败 → 改用 `CLAUDE_CODE_OAUTH_TOKEN` 环境变量
- **PATH 不完整** → `claude` 命令找不到 → plist 里显式加 `/Users/cn/.npm-global/bin`
- **HOME 未设置** → Claude 找不到配置文件 → plist 里加 `HOME=/Users/cn`

教训：**任何依赖用户环境的 CLI 工具放到 launchd 里，都要把环境变量手动配全。**

### 3. exit code ≠ 失败

Claude CLI 即使成功回答了问题，也可能 exit code = 1（MCP server 连接失败等非致命错误）。不能简单地 `if returncode != 0: return error`。

解法：**先解析 stdout 的 stream-json 事件流，有 result 就算成功，exit code 只在真的没有输出时才视为错误。**

### 4. 进程僵死检测

持久进程最怕的是"活着但不干活"——进程存在、poll() 返回 None，但 stdout 不再有输出。

解法：**Watchdog 线程**每 10 秒检查 `last_stdout_timestamp`，超过 90 秒无输出 → kill + 自动重启（带 backoff）。

## 多 Agent 架构设计

### 角色分工

| 角色 | 模型 | 职责 | 为什么选它 |
|------|------|------|-----------|
| Router | 关键词 + Claude | 判断 single/team | 关键词零延迟，LLM 只处理模糊情况 |
| Dispatcher | Claude Opus | 分析需求、制定计划 | 需要深度推理和规划 |
| Executor | Claude Code + DeepSeek | 写代码 | Claude 操作文件，DeepSeek 出方案 |
| Reviewer | Claude Opus | 审查代码 | 需要多维度安全/性能分析 |
| Supervisor | Python | 状态机、超时、重启 | 确定性逻辑不需要 LLM |

### 为什么 Executor 用两个模型

两个模型擅长的事不一样：

- **DeepSeek V4 Pro** — 代码生成能力极强，尤其擅长算法实现、设计模式套用、从零写完整模块。输出代码的质量和效率很高，且成本极低。
- **Claude Opus** — 擅长理解复杂意图、多步推理、工程决策（"这里该用什么设计模式"、"这个改动会不会影响其他模块"）。对需求到实现的 gap 理解更深。

**协作方式**：DeepSeek 负责"怎么写"（生成高质量代码），Claude 负责"写什么、写哪里、写完验证"（工程判断和执行决策）。

类比：DeepSeek 是写代码飞快的高级工程师，Claude 是理解需求、做架构决策的 Tech Lead。让他们各做最擅长的事。

**方案对比：**

| 维度 | 纯 Claude | 纯 DeepSeek | 双模型协作 |
| --- | --- | --- | --- |
| 代码生成质量 | 良好 | **优秀**(算法/模式) | 优秀 |
| 需求理解/决策 | **优秀** | 一般 | 优秀 |
| 速度 | 慢(10-30s/步) | **快**(3-5s/步) | 中等(并行可优化) |
| 成本 | 高 | **极低** | 中等 |
| 上下文感知 | **强**(读项目文件) | 弱(只看prompt) | 强 |
| 自主纠错 | **强** | 弱 | 强 |
| 链路复杂度 | 简单 | 简单 | **复杂**(双模型调度) |
| 单点故障 | 低 | 低 | **较高**(任一挂都影响) |

**双模型的核心收益：**
1. 用 DeepSeek 的速度和代码能力弥补 Claude 生成代码慢的短板
2. 用 Claude 的推理和上下文能力弥补 DeepSeek 工程决策弱的短板
3. 两个模型交叉验证——DeepSeek 出的方案如果有问题，Claude 执行时会发现并修正

**双模型的代价：**
1. 链路变长——每步多一次 API 调用（DeepSeek 3-5s + Claude 10-30s vs 单 Claude 10-30s）
2. 调度复杂度上升——要处理两个模型的超时、失败、结果合并
3. DeepSeek 方案可能误导 Claude——如果参考方案有 bug，Claude 可能被带偏（概率低但存在）

**结论：** 对于简单任务（改个配置、查个状态），单 Claude 更快更直接。对于复杂任务（新建模块、架构重构），双模型协作质量更高。所以我们用 Router 自动判断——简单走 single，复杂走 team。

### TDD 工作流

传统方式：写代码 → 写测试 → 发现 bug → 改代码（循环）

团队模式的 TDD：
```
Dispatcher 定义验收标准（acceptance criteria）
  → 先生成测试（此时代码还不存在，测试必定 fail）
  → Executor 写代码（目标：让测试通过）
  → 跑测试验证
  → 通过则继续，失败则修复（最多 1 轮）
```

好处：Executor 写代码时**已知道验收标准是什么**，不会偏离目标。

## Harness 规范映射

参考 workspace 的 H0-H6 skill harness，团队模式完整映射：

| Gate | 实现 |
|------|------|
| H0 Intake | Router + Dispatcher 分析 + 风险评估 + 用户确认 |
| H1 Routing | 关键词优先 + skill 覆盖 + LLM 兜底 |
| H2 Plan | 计划（含验证方式 + 验收标准）+ 用户确认 |
| H3 Execute | TDD：测试 → DeepSeek → Claude Code → 跑测试 |
| H4 Validate | per-step 验证 + 最终审查 + 修复循环 |
| H5 Delivery | 结构化报告（技能/范围/审查/残余风险） |
| H6 Post-task | episode 写入 experiences.jsonl |

## 关键工程决策

### 1. 持久进程 vs 每次 spawn

每次 spawn 简单但慢（5-10s 冷启动）。持久进程复杂但快（消息到达即可处理）。为手机操控场景，**响应速度是关键体验指标**，选了持久进程。

### 2. 预热启动

Bot 进程启动时立即后台预热 Claude session（不等第一条消息）。等飞书 WebSocket 连上时，Claude 已经 ready。

### 3. 确认关键词用精确匹配

最初用 `"好" in text` → "不好意思，继续吧"被误判为确认。改为精确匹配 `text.strip() == "确认"`。

### 4. workflow 状态持久化

用户发"确认"时 bot 可能刚好重启了 → 状态丢失 → 流程断掉。把 workflow state 写到 `.state/team_workflows.json`，重启后自动恢复。30 分钟超时自动清理。

### 5. 旧任务去重

用户重复发同一个需求 → 检查 `.state/tasks/` 下的 PRD 文件，如果已有完成的相同任务直接返回结果，不重复执行。

## 微信通道接入

微信 IM Bot（ilinkai.weixin.qq.com）的协议很简单：

```
notifyStart → 注册
getUpdates  → 长轮询收消息（服务端 hold 住直到有新消息）
sendMessage → 发消息
notifyStop  → 注销
```

跟飞书共享同一个 Claude 持久进程和消息队列。新增通道只需实现"收消息 → 入队"和"发回复"两个接口。

需要注意 `NO_PROXY` 设置——微信 API 是国内服务器（不走代理），但 Claude API 需要代理。在 `_ensure_no_proxy()` 里把 `*.weixin.qq.com` 加入免代理列表。

## 经验总结

### 什么有效

1. **持久 stream-json 进程** — 一次启动，上下文保持，响应快
2. **关键词路由优先** — 90% 的消息不需要调 LLM 做路由判断，省 token
3. **Watchdog + 自动重启** — 杜绝"进程死了没人知道"的情况
4. **TDD 验收驱动** — Executor 写代码时有明确的通过标准
5. **人工卡点在前不在中** — 需求确认 + 计划确认在前面，执行过程自动跑

### 什么坑多

1. **LLM 返回格式不稳定** — 说好只输出 JSON，实际会加 markdown 包裹。必须用正则从 prose 中提取 JSON
2. **launchd 环境** — 跟终端差异巨大，每个环境变量都要手动配
3. **Opus 模型太慢** — 团队模式 5 步 × 4 次调用 = 20 次 LLM 调用，每次 10-30s，总计 3-10 分钟
4. **多 Agent 链路脆弱** — 任何一环 timeout 整条链断。需要 per-call 超时 + 跳过机制

### 如果重来会怎么做

1. **一开始就用 stream-json 持久进程**，不走 per-message spawn 弯路
2. **先做 single mode 跑通**，再加 team mode。团队模式复杂度指数级上升
3. **Sonnet 做日常，Opus 只在审查/规划时用** — 平衡速度和质量
4. **测试从第一天写**，不要等功能堆到 2000 行再拆

## 数据

- 开发耗时：约 6 小时（从零到可用）
- 代码量：11 个文件，约 2000 行 Python
- 依赖：`lark-oapi`, `httpx`, `python-dotenv`
- 成本：Claude Max 订阅（不额外计费）+ DeepSeek API（极低）
- 支持通道：飞书 + 微信（可扩展）

## 后续方向

- 语音消息支持（Whisper 转文字 → Claude）
- 图片/文件处理（截图 → Claude 分析 UI）
- 多用户隔离（每人独立 Claude session）
- Web dashboard 查看任务历史和 plan 状态
