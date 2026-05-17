# 我在飞书里养了一支 AI 开发团队

> 一个程序员用 6 小时搭了个系统：在地铁上用飞书发条消息，AI 就帮他改代码、跑测试、做重构。这是完整的技术故事。

---

## 章节目录

### [第一章：地铁上的一个念头](blog-chapter-01.md)

通勤痛点 → 飞书 + Claude Code 的灵感 → 第一版 subprocess.Popen 的 5 秒等待 → 发现 stream-json 模式 → 响应降到 2 秒的质变。

---

### [第二章：七个环境变量的故事](blog-chapter-02.md)

做成 launchd 后台服务 → 花式报错 parade → 403 排查一小时（代理是幕后黑手）→ 7 个必备环境变量 → exit code 不可信 → 微信 200 行接入。

---

### [第三章：两个 AI，一个当军师一个当打手](blog-chapter-03.md)

复杂任务需要分工 → Claude Opus 做决策 + DeepSeek 写代码 → 关键词路由 → workflow 状态机 → TDD 验收驱动 → 为什么不全用一个模型。

---

### [第四章：AI 执行力太强是一种灾难](blog-chapter-04.md)

JWT 翻车现场（git checkout 恢复）→ 两个人工卡点 → "好"字误判事故 → Watchdog 进程保活 → exponential backoff → 状态持久化 → LLM 输出格式不可信。

---

### [第五章：如果重来一次](blog-chapter-05.md)

四个"我不会再这么做" → 当前运行数据（2-5 秒响应 / 一天不到一块钱）→ 记忆模块升级（三层架构：对话窗口 + 偏好提取 + 摘要压缩）→ 地铁发消息、到站任务完成的那个瞬间。

---

## 项目信息

- **技术栈：** Python + Claude Code CLI (stream-json) + DeepSeek API + 飞书 WebSocket + 微信长轮询
- **代码量：** 11 个文件，约 2000 行
- **开发时间：** 从零到可用约 6 小时
- **日常成本：** Claude Max 订阅 + DeepSeek API（一天不到一块钱）
