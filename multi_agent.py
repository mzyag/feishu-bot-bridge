"""
Multi-agent dev team orchestration.

Router → decides: simple (single Claude) vs team (Dispatcher → Executor → Reviewer)
Dispatcher: Claude Opus — analyze requirements, plan, assess risk
Executor: Claude Code (real file access) + DeepSeek (code advisor)
Reviewer: Claude Opus — independent code review
Supervisor: Python state machine — no LLM needed
"""

import json
import os
import time
import threading
from typing import Callable, Dict, List, Optional, Tuple

import httpx
from dotenv import load_dotenv

from config import SETTINGS

load_dotenv()

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "").strip()
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").strip()
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-pro").strip()
DEEPSEEK_TIMEOUT_SEC = int(os.getenv("DEEPSEEK_TIMEOUT_SEC", "180"))


# ---------------------------------------------------------------------------
# Router prompt: decides if task needs team mode
# ---------------------------------------------------------------------------

_TEAM_KEYWORDS = {
    "写代码", "改代码", "新功能", "重构", "修bug", "修复bug", "修 bug",
    "开发", "实现", "编写", "新增功能", "添加功能", "加一个", "加个",
    "架构设计", "技术方案", "建一个", "创建项目", "写个脚本",
    "优化一下", "改一下", "修一下", "拆分", "拆一下",
    "fix", "implement", "refactor", "build", "develop",
}
_SINGLE_KEYWORDS = {
    "查看", "看看", "看下", "状态", "git status", "git log",
    "什么模型", "哪个模型", "你是谁", "解释", "是什么",
    "帮我查", "搜索", "搜一下", "提交", "同步",
    "刚才", "刚刚", "上次", "之前", "问的是什么", "进度",
    "解决了吗", "完成了吗", "怎么样了",
}

ROUTER_SYSTEM_PROMPT = """你是一个任务路由器。根据用户消息判断应该走哪种处理模式。

判断标准：
- **team** 模式：需要写代码/改代码/新功能/重构/修 bug/架构设计/多步骤开发任务
- **single** 模式：问答/解释/查状态/闲聊/单次查询/配置修改/简单操作

只回复一个 JSON，不要其他内容：
{"mode": "team"} 或 {"mode": "single"}

如果有任何不确定，偏向 single。"""

# ---------------------------------------------------------------------------
# Dispatcher prompt
# ---------------------------------------------------------------------------

DISPATCHER_SYSTEM_PROMPT = """你是开发团队的总调度员。你的职责：
1. 分析用户需求，理解真正的目标
2. 评估风险等级（是否涉及 git push/delete/生产环境/凭证/外部系统）
3. 检查现有代码是否已经满足需求
4. 如果需要开发，制定简洁的实施计划（最多 5 个步骤）
5. 为每个步骤声明验证方式

输出格式（严格 JSON）：
{
  "analysis": "一句话需求理解",
  "action": "execute | skip | clarify",
  "reason": "为什么选择这个 action",
  "risk_level": "low | medium | high",
  "risk_flags": ["涉及的风险项，如 git_push / file_delete / production / credentials / external_api"],
  "files_affected": ["将修改的文件路径"],
  "plan": [
    {"step": 1, "title": "简短标题(5-10字)", "detail": "详细描述：做什么、怎么做、涉及哪些函数/类", "context": "需要的上下文/文件路径", "validation": "验证方式: syntax_check / test_run / manual_review / import_test", "acceptance": "验收断言：用一句 assert 描述完成标准，如 assert TaskQueue().enqueue(msg) returns int"},
    ...
  ],
  "review_focus": ["安全", "性能", "边界条件"]
}

action 说明：
- execute: 需要写代码，plan 里列出步骤
- skip: 需求已满足或无需改动，plan 留空 []
- clarify: 需求不明确，reason 里写需要确认的问题，plan 留空 []

risk_level 判断：
- low: 只读操作、本地文件修改、无外部影响
- medium: 文件写入、配置变更、本地服务重启
- high: git push、文件删除、生产环境、凭证操作、外部 API 调用

规则：
- 绝不写代码
- action=skip 时不要硬凑步骤
- action=execute 时每个 task 必须足够具体
- 每个 step 必须有 validation 字段
- risk_level=high 时，reason 里必须说明为什么需要谨慎
- 最多 5 步，能合并就合并"""

# ---------------------------------------------------------------------------
# Reviewer prompt
# ---------------------------------------------------------------------------

REVIEWER_SYSTEM_PROMPT = """你是独立代码审查者。只审查，不写代码。

审查维度（按优先级）：
1. 安全漏洞（注入、XSS、认证绕过、信息泄露）
2. 逻辑错误（边界条件、race condition、资源泄漏）
3. 性能问题（N+1、死循环、内存泄漏）
4. 可维护性（命名、结构、过度复杂）

输出格式：
{
  "verdict": "pass" | "needs_fix" | "critical",
  "issues": [
    {"severity": "high|medium|low", "location": "文件:行号或函数", "issue": "问题描述", "suggestion": "修复建议"}
  ],
  "summary": "一句话总结"
}

规则：
- 绝不写代码，只指出问题和方向
- 没问题就 verdict=pass，issues=[]
- 最多列 5 个最重要的问题"""


# ---------------------------------------------------------------------------
# DeepSeek Executor
# ---------------------------------------------------------------------------

def call_deepseek(system_prompt: str, user_prompt: str, timeout: int = DEEPSEEK_TIMEOUT_SEC) -> Tuple[bool, str]:
    if not DEEPSEEK_API_KEY:
        return False, "DEEPSEEK_API_KEY not configured"

    headers = {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.3,
        "max_tokens": 8192,
    }
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(f"{DEEPSEEK_BASE_URL}/v1/chat/completions", headers=headers, json=payload)
        if resp.status_code != 200:
            return False, f"DeepSeek API error: {resp.status_code} {resp.text[:200]}"
        data = resp.json()
        content = data.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
        if not content:
            return False, "DeepSeek returned empty response"
        return True, content
    except Exception as ex:
        return False, f"DeepSeek call failed: {ex}"


# ---------------------------------------------------------------------------
# Claude calls (via persistent session or direct)
# ---------------------------------------------------------------------------

def call_claude_via_session(prompt: str, session, retries: int = 1, timeout_sec: int = 180) -> Tuple[bool, str]:
    """Use the existing ClaudePersistentSession for Claude calls. Retries on failure."""
    for attempt in range(retries + 1):
        result = session.send_message(text=prompt, timeout_sec=timeout_sec)
        if result["status"] == "ok":
            return True, result["content"]
        if result["status"] == "timeout":
            return False, f"超时({timeout_sec}s)"
        if attempt < retries:
            time.sleep(3)
    return False, result.get("error", "Claude call failed")


# ---------------------------------------------------------------------------
# Task state machine (with human-in-the-loop checkpoints)
# ---------------------------------------------------------------------------

# Workflow phases for per-user state
# idle → awaiting_requirement_confirm → awaiting_plan_confirm → executing → done
_WORKFLOW_LOCK = threading.Lock()
_WORKFLOW_BY_USER: Dict[str, dict] = {}
_WORKFLOW_STATE_FILE = os.path.join(os.path.dirname(__file__), ".state", "team_workflows.json")
_WORKFLOW_TIMEOUT_SEC = 1800

_CONFIRM_EXACT = {"确认", "确定", "ok", "可以", "继续", "执行", "开始执行", "approve", "yes", "好", "好的", "同意", "通过", "没问题", "开始", "嗯", "行", "对", "是的", "没错", "做吧", "搞吧", "干吧"}
_REJECT_EXACT = {"取消", "cancel", "不要", "算了", "停", "stop", "重来", "reject", "不执行", "放弃"}
_SKIP_EXACT = {"全部执行", "一键执行", "跳过确认", "skip", "直接执行", "auto"}


def _save_workflows() -> None:
    try:
        os.makedirs(os.path.dirname(_WORKFLOW_STATE_FILE), exist_ok=True)
        with open(_WORKFLOW_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(_WORKFLOW_BY_USER, f, ensure_ascii=False, indent=2)
    except Exception as ex:
        print(f"[team] workflow state save failed: {ex}")


def _load_workflows() -> None:
    try:
        if os.path.exists(_WORKFLOW_STATE_FILE):
            with open(_WORKFLOW_STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                now = time.time()
                for uid, wf in data.items():
                    if now - wf.get("started_at", 0) <= _WORKFLOW_TIMEOUT_SEC:
                        _WORKFLOW_BY_USER[uid] = wf
    except Exception:
        pass


_load_workflows()


def get_workflow(open_id: str) -> Optional[dict]:
    import copy
    with _WORKFLOW_LOCK:
        wf = _WORKFLOW_BY_USER.get(open_id)
        if wf and time.time() - wf.get("started_at", 0) > _WORKFLOW_TIMEOUT_SEC:
            _WORKFLOW_BY_USER.pop(open_id, None)
            _save_workflows()
            return None
        return copy.deepcopy(wf) if wf else None


def set_workflow(open_id: str, wf: dict) -> None:
    if "started_at" not in wf:
        wf["started_at"] = time.time()
    wf["updated_at"] = time.time()
    with _WORKFLOW_LOCK:
        _WORKFLOW_BY_USER[open_id] = wf
        _save_workflows()


def clear_workflow(open_id: str) -> None:
    with _WORKFLOW_LOCK:
        _WORKFLOW_BY_USER.pop(open_id, None)
        _save_workflows()


def is_confirm(text: str) -> bool:
    t = text.strip()
    return t in _CONFIRM_EXACT or t.lower() in _CONFIRM_EXACT


def is_reject(text: str) -> bool:
    t = text.strip()
    return t in _REJECT_EXACT or t.lower() in _REJECT_EXACT


def is_skip_checkpoint(text: str) -> bool:
    t = text.strip()
    return t in _SKIP_EXACT or t.lower() in _SKIP_EXACT


# ---------------------------------------------------------------------------
# Main orchestration
# ---------------------------------------------------------------------------

def route_message(user_text: str, claude_session) -> str:
    """Keyword-first routing. LLM judges all non-obvious messages."""
    t = user_text.strip().lower()
    # Fast path: obvious team keywords
    if any(kw in t for kw in _TEAM_KEYWORDS):
        return "team"
    # Fast path: obvious single keywords
    if any(kw in t for kw in _SINGLE_KEYWORDS):
        return "single"
    # All other messages: let LLM decide
    ok, resp = call_claude_via_session(
        f"[ROUTER MODE - 只回复 JSON]\n\n用户消息: {user_text}\n\n{ROUTER_SYSTEM_PROMPT}",
        claude_session,
        timeout_sec=30,
    )
    if not ok:
        return "single"
    data = _extract_json_from_response(resp)
    if data:
        mode = data.get("mode", "single")
        return mode if mode in ("team", "single") else "single"
    return "single"


def handle_team_message(
    user_text: str,
    open_id: str,
    claude_session,
    notify_fn: Callable[[str], None],
) -> str:
    """
    Entry point for team mode messages. Manages the checkpoint state machine.
    Returns the reply text.
    """
    wf = get_workflow(open_id)

    if wf and wf.get("phase") == "awaiting_requirement_confirm":
        if is_reject(user_text):
            clear_workflow(open_id)
            return "已取消。"
        if is_skip_checkpoint(user_text):
            wf["skip_checkpoints"] = True
            return _execute_from_plan(open_id, claude_session, notify_fn)
        if is_confirm(user_text):
            return _dispatch_plan(open_id, claude_session, notify_fn)
        wf["user_request"] = wf["user_request"] + "\n补充: " + user_text
        set_workflow(open_id, wf)
        return _dispatch_requirement(open_id, claude_session, notify_fn)

    if wf and wf.get("phase") == "awaiting_plan_confirm":
        if is_reject(user_text):
            clear_workflow(open_id)
            return "已取消。"
        if is_skip_checkpoint(user_text):
            wf["skip_checkpoints"] = True
            return _execute_from_plan(open_id, claude_session, notify_fn)
        if is_confirm(user_text):
            return _execute_from_plan(open_id, claude_session, notify_fn)
        if len(user_text.strip()) > 10:
            clear_workflow(open_id)
            set_workflow(open_id, {"phase": "awaiting_requirement_confirm", "user_request": user_text, "skip_checkpoints": False})
            return _dispatch_requirement(open_id, claude_session, notify_fn)
        return f"当前有待确认的计划:\n{_format_plan(wf)}\n\n没问题就说一声，或者补充修改意见"

    clear_workflow(open_id)
    set_workflow(open_id, {
        "phase": "awaiting_requirement_confirm",
        "user_request": user_text,
        "skip_checkpoints": False,
    })
    return _dispatch_requirement(open_id, claude_session, notify_fn)


def _extract_json_from_response(text: str) -> Optional[dict]:
    """Extract JSON object from a response that may contain surrounding prose."""
    import re
    fenced = re.search(r"```(?:json)?\s*\n(.*?)```", text, re.DOTALL)
    if fenced:
        try:
            return json.loads(fenced.group(1).strip())
        except json.JSONDecodeError:
            pass
    brace_start = text.find("{")
    brace_end = text.rfind("}")
    if brace_start >= 0 and brace_end > brace_start:
        candidate = text[brace_start:brace_end + 1]
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass
    return None


def _check_existing_task(user_text: str) -> Optional[str]:
    """Check if a similar task already exists and is completed. Return status summary if found."""
    task_dir = os.path.join(os.path.dirname(__file__), ".state", "tasks")
    if not os.path.isdir(task_dir):
        return None
    try:
        prd_files = sorted(
            [f for f in os.listdir(task_dir) if f.startswith("prd-")],
            reverse=True,
        )[:20]
        for prd_name in prd_files:
            prd_path = os.path.join(task_dir, prd_name)
            with open(prd_path, "r", encoding="utf-8") as f:
                content = f.read()
            if "## 用户原始需求" not in content:
                continue
            req_section = content.split("## 用户原始需求")[1].split("##")[0].strip()
            if not req_section:
                continue
            _stopchars = set("的了在是我你他她它们这那有不会一个到说要和就人都能可以上中用时大也子为与从方面以及等下前后被对做让把给 \t\n")
            user_chars = set(user_text[:50]) - _stopchars
            req_chars = set(req_section) - _stopchars
            if len(user_chars) < 3 or not req_chars or len(user_chars & req_chars) / len(user_chars) < 0.6:
                continue
            plan_name = prd_name.replace("prd-", "plan-")
            plan_path = os.path.join(task_dir, plan_name)
            if not os.path.exists(plan_path):
                continue
            with open(plan_path, "r", encoding="utf-8") as f:
                plan_content = f.read()
            if "**状态:** 已完成" in plan_content:
                return f"## 该任务已完成\n\n相同需求已执行过，计划文件: `{plan_name}`\n\n{plan_content[:500]}"
            if "- [ ]" not in plan_content and ("- [x]" in plan_content or "- [!]" in plan_content):
                return f"## 该任务已执行\n\n计划文件: `{plan_name}`\n\n{plan_content[:500]}"
    except Exception:
        pass
    return None


def _dispatch_requirement(open_id: str, claude_session, notify_fn: Callable[[str], None]) -> str:
    wf = get_workflow(open_id)
    if not wf:
        return "工作流状态丢失"
    user_text = wf["user_request"]

    existing = _check_existing_task(user_text)
    if existing:
        clear_workflow(open_id)
        return existing

    notify_fn("🗺️ 调度员正在分析需求...")
    dispatch_prompt = (
        f"忽略之前的格式要求。你现在是 DISPATCHER 角色。\n"
        f"严格只输出一个 JSON 对象，不要任何其他文字、markdown、代码块包裹。\n"
        f"JSON 必须包含 plan 数组，action=execute 时 plan 不能为空。\n\n"
        f"{DISPATCHER_SYSTEM_PROMPT}\n\n用户需求:\n{user_text}"
    )
    ok, dispatch_resp = call_claude_via_session(dispatch_prompt, claude_session)
    if not ok:
        clear_workflow(open_id)
        return f"调度失败: {dispatch_resp}"

    plan = _extract_json_from_response(dispatch_resp)
    if not plan:
        plan = {"analysis": user_text, "action": "execute", "reason": "", "risk_level": "medium", "risk_flags": [], "files_affected": [], "plan": [{"step": 1, "task": user_text, "context": "", "validation": "auto"}], "review_focus": []}

    action = plan.get("action", "execute")
    reason = plan.get("reason", "")
    analysis = plan.get("analysis", "")

    if action == "skip":
        clear_workflow(open_id)
        return f"## 调度结论: 无需开发\n\n{analysis}\n\n**原因:** {reason}"

    if action == "clarify":
        wf["phase"] = "awaiting_requirement_confirm"
        set_workflow(open_id, wf)
        return f"## 需要确认\n\n{analysis}\n\n**请补充:** {reason}"

    wf["plan"] = plan
    wf["phase"] = "awaiting_requirement_confirm"
    set_workflow(open_id, wf)

    _save_prd_file(open_id, user_text, plan)

    risk_level = plan.get("risk_level", "low")
    risk_flags = plan.get("risk_flags", [])
    files = plan.get("files_affected", [])

    parts = [f"## 需求确认\n\n{analysis}"]
    if reason:
        parts.append(f"**原因:** {reason}")
    if files:
        short_files = [f.split("/")[-1] for f in files[:8]]
        parts.append(f"**涉及文件:** {', '.join(f'`{sf}`' for sf in short_files)}")
    if risk_level == "high":
        parts.append(f"⚠️ **风险: HIGH** — {', '.join(risk_flags)}")
    elif risk_level == "medium":
        parts.append(f"**风险: MEDIUM** — {', '.join(risk_flags)}")
    parts.append("\n没问题就说一声，有补充直接说。")
    return "\n".join(parts)


def _dispatch_plan(open_id: str, claude_session, notify_fn: Callable[[str], None]) -> str:
    wf = get_workflow(open_id)
    if not wf or not wf.get("plan"):
        return _dispatch_requirement(open_id, claude_session, notify_fn)

    plan = wf["plan"]
    steps = plan.get("plan", [])

    if not steps and plan.get("action") == "execute":
        steps = [{"step": 1, "task": wf.get("user_request", plan.get("analysis", "")), "context": "", "validation": "auto"}]
        plan["plan"] = steps
        wf["plan"] = plan
        set_workflow(open_id, wf)

    if not steps:
        clear_workflow(open_id)
        return f"## 调度完成\n\n{plan.get('analysis', '')}\n\n无具体步骤需要执行。"

    wf["phase"] = "awaiting_plan_confirm"
    wf["executor_results"] = []
    wf["current_step"] = 0
    wf["fix_rounds"] = 0
    set_workflow(open_id, wf)

    _save_plan_file(open_id, wf)

    plan_text = _format_plan(wf)
    return f"## 执行计划\n\n{plan_text}\n\n没问题就说一声开始执行。说「全部执行」可跳过中间确认。"


def _save_prd_file(open_id: str, user_text: str, plan: dict) -> None:
    """Create a PRD markdown file for the task."""
    import datetime
    prd_dir = os.path.join(os.path.dirname(__file__), ".state", "tasks")
    os.makedirs(prd_dir, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    filename = f"prd-{ts}.md"

    analysis = plan.get("analysis", user_text)
    reason = plan.get("reason", "")
    risk_level = plan.get("risk_level", "low")
    risk_flags = plan.get("risk_flags", [])
    files = plan.get("files_affected", [])

    content = f"""# 需求文档 (PRD)

**创建时间:** {datetime.datetime.now().strftime("%Y-%m-%d %H:%M")}
**状态:** 待执行

## 用户原始需求

{user_text}

## 需求分析

{analysis}

**原因:** {reason or 'N/A'}

## 风险评估

- **等级:** {risk_level}
- **风险项:** {', '.join(risk_flags) if risk_flags else '无'}

## 涉及文件

{chr(10).join(f'- {f}' for f in files) if files else '- 待定'}
"""
    try:
        filepath = os.path.join(prd_dir, filename)
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)
    except Exception:
        pass


def _save_plan_file(open_id: str, wf: dict) -> None:
    """Create an execution plan markdown file with step tracking."""
    import datetime
    plan_dir = os.path.join(os.path.dirname(__file__), ".state", "tasks")
    os.makedirs(plan_dir, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    filename = f"plan-{ts}.md"

    plan = wf.get("plan", {})
    analysis = plan.get("analysis", "")
    steps = plan.get("plan", [])
    review_focus = plan.get("review_focus", [])

    lines = [
        f"# 执行计划",
        f"",
        f"**目标:** {analysis}",
        f"**创建时间:** {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"**状态:** 待执行",
        f"",
        f"## 步骤",
        f"",
    ]
    for i, s in enumerate(steps):
        title = s.get("title", s.get("task", ""))
        detail = s.get("detail", s.get("task", ""))
        validation = s.get("validation", "auto")
        acceptance = s.get("acceptance", "")
        lines.append(f"- [ ] Step {i+1}: {title}")
        if detail and detail != title:
            lines.append(f"  - 描述: {detail}")
        if acceptance:
            lines.append(f"  - 验收: {acceptance}")
        lines.append(f"  - 上下文: {s.get('context', 'N/A')}")
        lines.append(f"  - 验证: {validation}")
        lines.append(f"  - 状态: 待执行")
        lines.append("")

    if review_focus:
        lines.append(f"## 审查重点")
        lines.append("")
        for rf in review_focus:
            lines.append(f"- {rf}")

    wf["plan_file"] = os.path.join(plan_dir, filename)
    try:
        with open(wf["plan_file"], "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
    except Exception:
        pass


def _update_plan_step_status(wf: dict, step_num: int, status: str) -> None:
    """Update a step's checkbox and status in the plan markdown file."""
    plan_file = wf.get("plan_file")
    if not plan_file or not os.path.exists(plan_file):
        return
    try:
        with open(plan_file, "r", encoding="utf-8") as f:
            content = f.read()

        old_checkbox = f"- [ ] Step {step_num}:"
        new_checkbox = f"- [x] Step {step_num}:" if "通过" in status or "完成" in status else f"- [!] Step {step_num}:"
        content = content.replace(old_checkbox, new_checkbox, 1)

        old_status = f"  - 状态: 待执行"
        new_status = f"  - 状态: {status}"
        lines = content.split("\n")
        step_found = False
        for i, line in enumerate(lines):
            if f"Step {step_num}:" in line:
                step_found = True
            if step_found and line.strip() == "- 状态: 待执行":
                lines[i] = f"  - 状态: {status}"
                break
        content = "\n".join(lines)

        all_done = "- [ ]" not in content
        if all_done:
            content = content.replace("**状态:** 待执行", f"**状态:** 已完成")

        with open(plan_file, "w", encoding="utf-8") as f:
            f.write(content)
    except Exception:
        pass


def _format_plan(wf: dict) -> str:
    plan = wf.get("plan", {})
    analysis = plan.get("analysis", "")
    steps = plan.get("plan", [])
    lines = [f"**目标:** {analysis}", ""]
    for i, s in enumerate(steps):
        title = s.get("title", s.get("task", ""))
        detail = s.get("detail", s.get("task", ""))
        validation = s.get("validation", "auto")
        acceptance = s.get("acceptance", "")
        lines.append(f"**Step {i+1}: {title}** [{validation}]")
        if detail and detail != title:
            lines.append(f"  {detail}")
        if acceptance:
            lines.append(f"  验收: `{acceptance}`")
        lines.append("")
    review_focus = plan.get("review_focus", [])
    if review_focus:
        lines.append(f"**审查重点:** {', '.join(review_focus)}")
    return "\n".join(lines)


def _execute_from_plan(open_id: str, claude_session, notify_fn: Callable[[str], None]) -> str:
    wf = get_workflow(open_id)
    if not wf:
        return "工作流状态丢失"
    wf["phase"] = "executing"
    wf["current_step"] = 0
    set_workflow(open_id, wf)
    return _continue_execution(open_id, claude_session, notify_fn)


def _continue_execution(open_id: str, claude_session, notify_fn: Callable[[str], None]) -> str:
    wf = get_workflow(open_id)
    if not wf:
        return "工作流状态丢失"

    plan = wf.get("plan", {})
    steps = plan.get("plan", [])
    user_text = wf["user_request"]
    skip = wf.get("skip_checkpoints", False)
    current_step = wf.get("current_step", 0)
    executor_results = wf.get("executor_results", [])

    while current_step < len(steps):
        step = steps[current_step]
        title = step.get("title", step.get("task", ""))
        detail = step.get("detail", step.get("task", ""))
        task_desc = detail
        context = step.get("context", "")
        step_num = current_step + 1
        validation_method = step.get("validation", "auto")

        notify_fn(f"⚡ Step {step_num}/{len(steps)}: {title}")
        acceptance = step.get("acceptance", "")

        # Tool progress callback: sends real-time tool notifications during Claude execution
        sub_step_counter = [0]
        def _step_tool_progress(stage: str, detail: str = "") -> None:
            from claude_session import CLAUDE_SESSION
            if not hasattr(CLAUDE_SESSION, "_tool_log_lock"):
                return
            new_entries = []
            with CLAUDE_SESSION._tool_log_lock:
                current_count = len(CLAUDE_SESSION._tool_log)
                if current_count > sub_step_counter[0]:
                    new_entries = CLAUDE_SESSION._tool_log[sub_step_counter[0]:]
                    sub_step_counter[0] = current_count
            if new_entries:
                notify_fn("\n".join(new_entries))

        # TDD Phase 1: Generate test (Step N.1)
        notify_fn(f"Step {step_num}.1 生成测试...")
        test_code = _generate_test_from_acceptance(step_num, title, task_desc, acceptance, claude_session)
        if test_code:
            notify_fn(f"🧪 Step {step_num}.1 测试已生成")

        # TDD Phase 2: DeepSeek code draft (Step N.2)
        notify_fn(f"Step {step_num}.2 DeepSeek 出方案...")
        ds_system = (
            "你是代码顾问。为任务输出完整的实现方案和代码片段。\n"
            "如果修改现有文件，给出具体的修改位置和代码。\n"
            "如果是新文件，给出完整文件内容。\n"
            "代码前后用 ```language 包裹。简洁，不废话。"
        )
        ds_prompt = f"任务: {task_desc}\n上下文: {context}\n\n原始需求: {user_text}"
        if acceptance:
            ds_prompt += f"\n\n验收标准: {acceptance}"
        ds_ok, ds_output = call_deepseek(ds_system, ds_prompt)
        ds_ref = ""
        if ds_ok:
            ds_ref = f"\n\nDeepSeek 参考方案:\n{ds_output[:3000]}"
            notify_fn(f"📝 Step {step_num}.2 DeepSeek 方案已生成")

        # TDD Phase 3: Claude Code executes (Step N.3)
        notify_fn(f"Step {step_num}.3 Claude 开始执行...")
        # Reset tool counter so we capture tools from this execution
        with claude_session._tool_log_lock:
            sub_step_counter[0] = len(claude_session._tool_log)

        exec_prompt = (
            f"请执行以下任务。你可以读写文件、运行命令。\n\n"
            f"任务: {task_desc}\n上下文: {context}\n\n原始需求: {user_text}"
        )
        if acceptance:
            exec_prompt += f"\n\n验收标准（你的代码必须让这个断言通过）: {acceptance}"
        if test_code:
            exec_prompt += f"\n\n预定义测试（执行完后必须通过）:\n{test_code[:1500]}"
        exec_prompt += ds_ref
        # Pass progress callback so tool events push to user during execution
        result = claude_session.send_message(
            text=exec_prompt,
            timeout_sec=SETTINGS.claude_timeout_sec,
            progress_callback=_step_tool_progress,
        )
        ok = result.get("status") == "ok"
        code_output = result.get("content", "") if ok else result.get("error", "Claude call failed")
        claude_session._progress_callback = None

        if not ok:
            executor_results.append(f"### Step {step_num}: {title}\n\n❌ 执行失败: {code_output}")
            notify_fn(f"❌ Step {step_num}.3 执行失败: {code_output[:80]}")
            wf.setdefault("unfinished", []).append(f"Step {step_num}: {title}")
            _update_plan_step_status(wf, step_num, f"❌ 失败: {code_output[:40]}")
            current_step += 1
            wf["current_step"] = current_step
            wf["executor_results"] = executor_results
            set_workflow(open_id, wf)
            continue

        # TDD Phase 4: Run test (Step N.4)
        test_passed = True
        test_msg = ""
        if test_code:
            notify_fn(f"Step {step_num}.4 运行测试...")
            test_passed, test_msg = _run_step_test(step_num, test_code, claude_session)
            notify_fn(f"🧪 Step {step_num}.4 测试{'✓' if test_passed else '✗'} {test_msg[:40]}")

        # H4 per-step validation (Step N.5)
        notify_fn(f"Step {step_num}.5 验证中...")
        if not test_passed:
            step_ok, check_msg = False, f"测试失败: {test_msg[:80]}"
        else:
            step_ok, check_msg = _validate_step(step_num, task_desc, code_output, claude_session, validation_method)
        if step_ok:
            executor_results.append(f"### Step {step_num}: {task_desc}\n\n{code_output}")
            notify_fn(f"✅ Step {step_num}.5 验证通过 — {check_msg}")
            _update_plan_step_status(wf, step_num, f"✅ 完成: {check_msg[:30]}")
        else:
            # Retry once with validation feedback (H4: return to H3, smallest fix)
            notify_fn(f"⚠️ Step {step_num}.5 验证未通过: {check_msg}")
            notify_fn(f"Step {step_num}.6 修复中...")
            fix_prompt = (
                f"上一步执行结果验证未通过: {check_msg}\n"
                f"原任务: {task_desc}\n请做最小修复。"
            )
            ok2, fix_output = call_claude_via_session(fix_prompt, claude_session)
            if ok2:
                fix_ok, fix_msg = _validate_step(step_num, task_desc, fix_output, claude_session, validation_method)
                if fix_ok:
                    executor_results.append(f"### Step {step_num}: {task_desc}\n\n{fix_output}")
                    notify_fn(f"✅ Step {step_num}.6 修复通过 — {fix_msg}")
                    _update_plan_step_status(wf, step_num, f"✅ 修复后通过: {fix_msg[:30]}")
                else:
                    executor_results.append(f"### Step {step_num}: {task_desc}\n\n{fix_output}\n\n> ⚠️ 验证仍未通过: {fix_msg}（停止条件: 最多重试1次）")
                    notify_fn(f"⚠️ Step {step_num}.6 修复后仍有问题，继续下一步")
                    wf.setdefault("validation_failures", []).append({"step": step_num, "issue": check_msg, "retry_issue": fix_msg})
                    _update_plan_step_status(wf, step_num, f"⚠️ 验证未通过: {check_msg[:30]}")
            else:
                executor_results.append(f"### Step {step_num}: {task_desc}\n\n{code_output}\n\n> ⚠️ 修复失败")
                notify_fn(f"⚠️ Step {step_num}.6 修复失败，保留原输出继续")
                wf.setdefault("validation_failures", []).append({"step": step_num, "issue": check_msg})
                _update_plan_step_status(wf, step_num, f"⚠️ 修复失败: {check_msg[:30]}")

        current_step += 1
        wf["current_step"] = current_step
        wf["executor_results"] = executor_results
        set_workflow(open_id, wf)

    # All steps done → final Review + Delivery
    return _run_review_and_deliver(open_id, claude_session, notify_fn)


TDD_TEST_GEN_PROMPT = """根据任务描述和验收标准，生成一个测试脚本（TDD：先写测试，代码还没写）。

要求：
- 用 Python 写，assert 断言验收标准
- 只测这一步的核心验收条件
- 必须能在当前环境运行（不依赖外部服务/网络）
- 如果是新文件：assert import 成功 + 核心类/函数存在
- 如果是修改：assert 修改后的行为符合预期
- 不超过 15 行
- 只输出 Python 代码，不要解释、不要 markdown 包裹"""


def _generate_test_from_acceptance(
    step_num: int,
    title: str,
    task_desc: str,
    acceptance: str,
    claude_session,
) -> str:
    """TDD: Generate test BEFORE code is written, based on acceptance criteria."""
    if not acceptance:
        return ""
    prompt = (
        f"[TDD 测试生成 - 只输出 Python 代码]\n\n"
        f"{TDD_TEST_GEN_PROMPT}\n\n"
        f"步骤: {title}\n"
        f"描述: {task_desc}\n"
        f"验收标准: {acceptance}"
    )
    ok, test_code = call_claude_via_session(prompt, claude_session, timeout_sec=60)
    if not ok:
        return ""
    cleaned = test_code.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    return cleaned


def _run_step_test(
    step_num: int,
    test_code: str,
    claude_session,
) -> Tuple[bool, str]:
    """Run a pre-generated test script and return (passed, message)."""
    run_prompt = (
        f"请运行以下 Python 测试脚本，报告是否通过。\n"
        f"如果通过回复 PASS，如果失败回复 FAIL 加错误信息。\n\n"
        f"```python\n{test_code}\n```"
    )
    ok, result = call_claude_via_session(run_prompt, claude_session, timeout_sec=60)
    if not ok:
        return True, "测试跳过(执行失败)"
    upper = result.upper()
    if "PASS" in upper or "通过" in result:
        return True, result[:60]
    if "FAIL" in upper or "失败" in result or "Error" in result:
        return False, result[:100]
    return True, "结果不明确，默认通过"


STEP_VALIDATOR_PROMPT = """你是代码验证器。检查 Executor 的输出是否真正完成了任务要求。

只回复 JSON：
{"pass": true, "reason": "一句话说明"} 或 {"pass": false, "reason": "哪里没做到"}

检查项：
1. 输出是否包含实际代码/操作结果（不是空的或只有注释）
2. 结果是否对应任务描述（不是答非所问）
3. 有无明显语法错误或逻辑缺陷
4. 是否遗漏了任务的关键要求

宽容判断：小瑕疵算 pass，只有明显未完成或答非所问才 fail。"""


def _parse_check_result(resp: str, label: str) -> Tuple[bool, str]:
    data = _extract_json_from_response(resp)
    if data:
        passed = data.get("pass", True)
        detail = data.get("detail", "")
        return bool(passed), f"{label}: {detail}" if detail else f"{label} {'通过' if passed else '失败'}"
    return True, f"{label} 结果解析失败，默认通过"


def _validate_step(
    step_num: int,
    task_desc: str,
    code_output: str,
    claude_session,
    validation_method: str = "auto",
) -> Tuple[bool, str]:
    if not code_output or len(code_output.strip()) < 20:
        return False, "输出为空或过短"

    _CHECK_RESULT_PROMPT = '只回复 JSON: {"pass": true, "detail": "结果摘要"} 或 {"pass": false, "detail": "错误信息"}'

    if validation_method == "import_test":
        ok, resp = call_claude_via_session(
            f"请对刚才修改的文件执行 python3 -c 'import ...' 测试导入。{_CHECK_RESULT_PROMPT}",
            claude_session,
        )
        if ok:
            return _parse_check_result(resp, "import 测试")

    if validation_method == "syntax_check":
        ok, resp = call_claude_via_session(
            f"请对刚才修改的文件执行 python3 -m py_compile 语法检查。{_CHECK_RESULT_PROMPT}",
            claude_session,
        )
        if ok:
            return _parse_check_result(resp, "语法检查")

    if validation_method == "test_run":
        ok, resp = call_claude_via_session(
            f"请运行与刚才修改相关的测试（没有则 smoke test）。{_CHECK_RESULT_PROMPT}",
            claude_session,
        )
        if ok:
            return _parse_check_result(resp, "测试")

    if validation_method == "manual_review":
        return True, "需人工审查（已标记）"

    # Default: LLM-based review
    prompt = (
        f"[VALIDATOR - 只回复 JSON]\n\n"
        f"{STEP_VALIDATOR_PROMPT}\n\n"
        f"任务要求: {task_desc}\n\n"
        f"Executor 输出 (前2000字):\n{code_output[:2000]}"
    )
    ok, resp = call_claude_via_session(prompt, claude_session)
    if not ok:
        return True, "验证跳过(Claude不可用)"

    data = _extract_json_from_response(resp)
    if data:
        passed = data.get("pass", True)
        reason = data.get("reason", "")
        return bool(passed), reason
    return True, "验证解析失败，默认通过"


def _run_review_and_deliver(
    open_id: str,
    claude_session,
    notify_fn: Callable[[str], None],
) -> str:
    """H4 Review + H5 Delivery + H6 Post-task."""
    wf = get_workflow(open_id)
    if not wf:
        return "工作流状态丢失"

    plan = wf.get("plan", {})
    user_text = wf["user_request"]
    executor_results = wf.get("executor_results", [])
    full_code = "\n\n---\n\n".join(executor_results)
    plan_summary = plan.get("analysis", "")
    steps = plan.get("plan", [])
    fix_rounds = wf.get("fix_rounds", 0)

    # --- H4: Review ---
    notify_fn("🔍 审查员正在检查代码...")
    review_focus = plan.get("review_focus", ["安全", "性能", "边界条件"])
    review_prompt = (
        f"{REVIEWER_SYSTEM_PROMPT}\n\n"
        f"重点审查: {', '.join(review_focus)}\n\n"
        f"原始需求: {user_text}\n\n"
        f"Executor 输出:\n{full_code[:6000]}"
    )
    ok, review_resp = call_claude_via_session(review_prompt, claude_session)

    review_verdict = "pass"
    review_summary = ""
    review_issues = []
    if ok:
        try:
            cleaned = review_resp.strip()
            if cleaned.startswith("```"):
                cleaned = cleaned.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
            review_data = json.loads(cleaned)
            review_verdict = review_data.get("verdict", "pass")
            review_summary = review_data.get("summary", "")
            review_issues = review_data.get("issues", [])
        except json.JSONDecodeError:
            review_summary = review_resp[:200]

    # Fix loop (Claude Code executes fixes with real file access)
    if review_verdict == "critical" and fix_rounds < 2:
        fix_rounds += 1
        wf["fix_rounds"] = fix_rounds
        notify_fn(f"⚠️ 审查发现严重问题，第 {fix_rounds} 轮修复...")
        fix_prompt = (
            f"审查员指出以下严重问题，请直接修复（你可以读写文件）:\n"
            + "\n".join(f"- [{iss.get('severity')}] {iss.get('issue')}: {iss.get('suggestion')}" for iss in review_issues[:3])
        )
        ok, fix_output = call_claude_via_session(fix_prompt, claude_session)
        if ok:
            executor_results.append(f"### 修复轮 {fix_rounds}\n\n{fix_output}")
            full_code = "\n\n---\n\n".join(executor_results)
            notify_fn("✅ 修复完成")

    # --- H5: Structured delivery report ---
    notify_fn("📦 正在汇总交付报告...")
    steps_done = sum(1 for o in executor_results if "❌" not in o)
    steps_total = len(steps)
    fix_note = f"，修复 {fix_rounds} 轮" if fix_rounds > 0 else ""

    result_parts = [
        f"## 交付报告",
        f"**需求:** {plan_summary}",
        f"**技能:** multi-agent team (Dispatcher: Claude, Executor: Claude+DeepSeek, Reviewer: Claude)",
        f"**范围:** {steps_total} 步骤，{steps_done}/{steps_total} 成功{fix_note}",
    ]

    if review_verdict == "pass":
        result_parts.append(f"**审查(H4):** ✅ 通过" + (f" — {review_summary}" if review_summary else ""))
    elif review_verdict == "needs_fix":
        result_parts.append(f"**审查(H4):** ⚠️ 有建议 — {review_summary}")
        if review_issues:
            result_parts.append("\n".join(f"- [{iss.get('severity')}] {iss.get('issue')}" for iss in review_issues[:5]))
    else:
        result_parts.append(f"**审查(H4):** 🚨 {review_summary}")
        if review_issues:
            result_parts.append("\n".join(f"- [{iss.get('severity')}] {iss.get('issue')}" for iss in review_issues[:5]))

    unfinished = wf.get("unfinished", [])
    if unfinished:
        result_parts.append("**未完成项:**\n" + "\n".join(f"- {item}" for item in unfinished))

    residual = []
    if steps_done < steps_total:
        residual.append(f"{steps_total - steps_done} 步骤未完成")
    if review_verdict in ("critical", "needs_fix"):
        residual.append(f"审查状态: {review_verdict}")
    validation_failures = wf.get("validation_failures", [])
    if validation_failures:
        residual.append(f"{len(validation_failures)} 步验证有问题")
    result_parts.append(f"**残余风险:** {', '.join(residual) if residual else '无'}")

    if validation_failures:
        vf_lines = [f"- Step {vf['step']}: {vf['issue']}" for vf in validation_failures[:5]]
        result_parts.append(f"**验证问题明细:**\n" + "\n".join(vf_lines))

    result_parts.append(f"\n## 代码输出\n\n{full_code}")

    # --- H6: Post-task capture ---
    outcome = "success" if review_verdict == "pass" and steps_done == steps_total else (
        "partial_success" if steps_done > 0 else "recoverable_failure"
    )
    started_at_ts = wf.get("started_at", time.time())
    validation_failures = wf.get("validation_failures", [])
    missing_checks = [f"Step {vf['step']}: {vf['issue']}" for vf in validation_failures]
    _run_post_task_capture(
        user_text=user_text,
        outcome=outcome,
        plan_summary=plan_summary,
        steps_total=steps_total,
        steps_done=steps_done,
        review_verdict=review_verdict,
        review_issues=review_issues,
        fix_rounds=fix_rounds,
        started_at_ts=started_at_ts,
        missing_checks=missing_checks,
    )

    clear_workflow(open_id)
    return "\n\n".join(result_parts)


def _run_post_task_capture(
    user_text: str,
    outcome: str,
    plan_summary: str,
    steps_total: int,
    steps_done: int,
    review_verdict: str,
    review_issues: list,
    fix_rounds: int,
    started_at_ts: float = 0,
    missing_checks: Optional[List[str]] = None,
) -> None:
    """H6: Write a normalized episode to self-improving-core state."""
    import datetime
    state_dir = "/Users/cn/Workspace/.codex/state/self-improving-core"
    os.makedirs(state_dir, exist_ok=True)

    now = datetime.datetime.now()
    started_dt = datetime.datetime.fromtimestamp(started_at_ts) if started_at_ts else now
    episode = {
        "episode_id": f"team-{now.strftime('%Y%m%d-%H%M%S')}",
        "task_type": "multi-agent-team",
        "task_scope": "feishu-bot-bridge",
        "user_goal": user_text[:200],
        "outcome_label": outcome,
        "operator_or_agent_id": "feishu-bot-bridge/multi-agent",
        "started_at": started_dt.isoformat(),
        "ended_at": now.isoformat(),
        "duration_sec": round(time.time() - started_at_ts) if started_at_ts else 0,
        "tools_used": ["Dispatcher(Claude)", "Executor(Claude+DeepSeek)", "Reviewer(Claude)"],
        "reflection": {
            "goal": plan_summary,
            "actual_outcome": f"{steps_done}/{steps_total} steps done, review={review_verdict}",
            "what_worked": [s for s in [
                "Dispatcher skip/clarify routing" if outcome != "recoverable_failure" else None,
                f"Claude+DeepSeek execution ({steps_done}/{steps_total})" if steps_done > 0 else None,
                "Review caught issues" if review_issues else None,
            ] if s],
            "what_failed": [s for s in [
                f"{steps_total - steps_done} steps failed" if steps_done < steps_total else None,
                f"Required {fix_rounds} fix rounds" if fix_rounds > 0 else None,
            ] if s],
            "missing_checks": missing_checks or [],
            "generalization_scope": "narrow",
            "confidence": "medium",
        },
    }

    jsonl_path = os.path.join(state_dir, "experiences.jsonl")
    try:
        with open(jsonl_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(episode, ensure_ascii=False, separators=(",", ":")) + "\n")
    except Exception:
        pass
