# CancelShield (MVP)

面向团队的 SaaS 试用防误扣费服务，当前版本已支持：

- 团队级 API Key 鉴权 + 角色权限（admin/editor/viewer）
- 订阅台账与团队数据隔离
- 续费提醒预览 + 当日提醒任务
- 取消/删卡证据留存（JSON + 文件上传）
- 争议材料导出（ZIP）
- Webhook 通知通道（feishu/slack/generic）
- Web 管理控制台

## 快速启动

```bash
cd cancelshield
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8090
```

- Swagger: `http://127.0.0.1:8090/docs`
- 控制台: `http://127.0.0.1:8090/console`

## 首次使用流程

1. 在控制台执行“初始化团队”（需团队名 + 管理员邮箱），拿到 Admin API Key。
2. 后续所有业务 API 请求都带请求头：`X-API-Key: cs_live_xxx`。
3. 配置通知通道（可选）后，执行提醒任务会自动尝试推送。

## API 清单

- `GET /health`
- `POST /api/v1/teams/bootstrap`
- `GET /api/v1/teams/me`
- `GET /api/v1/teams/members`
- `POST /api/v1/teams/members` (admin)
- `POST /api/v1/teams/api-keys` (admin)
- `POST /api/v1/subscriptions` (admin/editor)
- `GET /api/v1/subscriptions`
- `POST /api/v1/subscriptions/{id}/evidence` (admin/editor)
- `POST /api/v1/subscriptions/{id}/evidence/upload` (admin/editor, base64 JSON)
- `GET /api/v1/subscriptions/{id}/reminders/preview`
- `POST /api/v1/subscriptions/{id}/dispute-export`
- `POST /api/v1/reminders/run` (admin/editor)
- `GET /api/v1/notifications/channels`
- `POST /api/v1/notifications/channels` (admin/editor)
- `POST /api/v1/notifications/test`

## 提醒任务（命令行）

在 `cancelshield` 目录执行：

```bash
PYTHONPATH=. python3 scripts/run_reminders_once.py
```

## API 冒烟测试（全流程）

```bash
PYTHONPATH=. python3 scripts/smoke_api_flow.py
```

## 目录

```text
cancelshield/
  app/
    main.py
    db.py
    schemas.py
    security.py
    routers/
    services/
    web/
  scripts/
  data/       # sqlite 数据与证据文件
  exports/    # 争议导出 ZIP
```
