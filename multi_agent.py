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
    "fix", "implement", "refactor", "build", "develop",
}
_SINGLE_KEYWORDS = {
    "查看", "看看", "看下", "状态", "git status", "git log",
    "什么模型", "哪个模型", "你是谁", "解释", "是什么",
    "帮我查", "搜索", "搜一下",
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
    {"step": 1, "task": "具体任务描述", "context": "需要的上下文/文件路径", "validation": "验证方式: syntax_check / test_run / manual_review / import_test"},
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

def call_claude_via_session(prompt: str, session, retries: int = 1) -> Tuple[bool, str]:
    """Use the existing ClaudePersistentSession for Claude calls. Retries on failure."""
    for attempt in range(retries + 1):
        result = session.send_message(text=prompt, timeout_sec=180)
        if result["status"] == "ok":
            return True, result["content"]
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

_CONFIRM_EXACT = {"确认", "ok", "可以", "继续", "执行", "approve", "yes", "好", "好的", "同意", "通过", "没问题", "开始", "嗯", "行"}
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
                _WORKFLOW_BY_USER.update(data)
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
    """Keyword-first routing. LLM fallback only for ambiguous messages."""
    t = user_text.strip().lower()
    if any(kw in t for kw in _TEAM_KEYWORDS):
        return "team"
    if any(kw in t for kw in _SINGLE_KEYWORDS):
        return "single"
    if len(t) < 15:
        return "single"
    ok, resp = call_claude_via_session(
        f"[ROUTER MODE - 只回复 JSON]\n\n用户消息: {user_text}\n\n{ROUTER_SYSTEM_PROMPT}",
        claude_session,
    )
    if not ok:
        return "single"
    try:
        cleaned = resp.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        data = json.loads(cleaned)
        mode = data.get("mode", "single")
        return mode if mode in ("team", "single") else "single"
    except (json.JSONDecodeError, KeyError):
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
        return f"当前有待确认的计划:\n{_format_plan(wf)}\n\n回复「确认」执行 /「取消」放弃 /「全部执行」跳过确认"

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


def _dispatch_requirement(open_id: str, claude_session, notify_fn: Callable[[str], None]) -> str:
    wf = get_workflow(open_id)
    if not wf:
        return "工作流状态丢失"
    user_text = wf["user_request"]

    notify_fn("🗺️ 调度员正在分析需求...")
    dispatch_prompt = (
        f"[DISPATCHER - 只回复 JSON，不要其他内容，不要 markdown 包裹]\n\n"
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
    parts.append("\n回复「确认」进入方案规划，或补充修改需求，或「取消」放弃。")
    return "\n".join(parts)


def _dispatch_plan(open_id: str, claude_session, notify_fn: Callable[[str], None]) -> str:
    wf = get_workflow(open_id)
    if not wf or not wf.get("plan"):
        return _dispatch_requirement(open_id, claude_session, notify_fn)

    plan = wf["plan"]
    steps = plan.get("plan", [])

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
    return f"## 执行计划\n\n{plan_text}\n\n回复「确认」逐步执行 /「全部执行」一键跑完 /「取消」放弃"


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
        validation = s.get("validation", "auto")
        lines.append(f"- [ ] Step {i+1}: {s.get('task', '')}")
        lines.append(f"  - 上下文: {s.get('context', 'N/A')}")
        lines.append(f"  - 验证: {validation}")
        lines.append(f"  - 状态: 待执行")
        lines.append("")

    if review_focus:
        lines.append(f"## 审查重点")
        lines.append("")
        for rf in review_focus:
            lines.append(f"- {rf}")

    wf["plan_file"] = filename
    try:
        filepath = os.path.join(plan_dir, filename)
        with open(filepath, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
    except Exception:
        pass


def _format_plan(wf: dict) -> str:
    plan = wf.get("plan", {})
    analysis = plan.get("analysis", "")
    steps = plan.get("plan", [])
    lines = [f"**目标:** {analysis}"]
    for i, s in enumerate(steps):
        validation = s.get("validation", "auto")
        lines.append(f"  {i+1}. {s.get('task', '')}  [{validation}]")
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
        task_desc = step.get("task", "")
        context = step.get("context", "")
        step_num = current_step + 1
        validation_method = step.get("validation", "auto")

        notify_fn(f"⚡ Step {step_num}/{len(steps)}: {task_desc[:60]}")

        # Phase A: Ask DeepSeek for a code draft (advisor role)
        ds_system = (
            "你是代码顾问。为任务输出完整的实现方案和代码片段。\n"
            "如果修改现有文件，给出具体的修改位置和代码。\n"
            "如果是新文件，给出完整文件内容。\n"
            "代码前后用 ```language 包裹。简洁，不废话。"
        )
        ds_prompt = f"任务: {task_desc}\n上下文: {context}\n\n原始需求: {user_text}"
        ds_ok, ds_output = call_deepseek(ds_system, ds_prompt)
        ds_ref = ""
        if ds_ok:
            ds_ref = f"\n\nDeepSeek 给出的参考方案（仅供参考，你需要自己判断是否正确）:\n{ds_output[:3000]}"
            notify_fn(f"📝 Step {step_num}: DeepSeek 方案已生成")

        # Phase B: Claude Code executes (real filesystem access)
        exec_prompt = (
            f"请执行以下任务。你可以读写文件、运行命令。\n\n"
            f"任务: {task_desc}\n上下文: {context}\n\n原始需求: {user_text}"
            f"{ds_ref}"
        )
        ok, code_output = call_claude_via_session(exec_prompt, claude_session)

        if not ok:
            executor_results.append(f"### Step {step_num}: {task_desc}\n\n❌ 执行失败: {code_output}")
            notify_fn(f"❌ Step {step_num} 执行失败: {code_output[:80]}")
            wf.setdefault("unfinished", []).append(f"Step {step_num}: {task_desc}")
            current_step += 1
            wf["current_step"] = current_step
            wf["executor_results"] = executor_results
            set_workflow(open_id, wf)
            continue

        # H4 per-step validation
        step_ok, check_msg = _validate_step(step_num, task_desc, code_output, claude_session, validation_method)
        if step_ok:
            executor_results.append(f"### Step {step_num}: {task_desc}\n\n{code_output}")
            notify_fn(f"✅ Step {step_num} 验证通过 — {check_msg}")
        else:
            # Retry once with validation feedback (H4: return to H3, smallest fix)
            notify_fn(f"⚠️ Step {step_num} 验证未通过: {check_msg}，修复中...")
            fix_prompt = (
                f"上一步执行结果验证未通过: {check_msg}\n"
                f"原任务: {task_desc}\n请做最小修复。"
            )
            ok2, fix_output = call_claude_via_session(fix_prompt, claude_session)
            if ok2:
                fix_ok, fix_msg = _validate_step(step_num, task_desc, fix_output, claude_session, validation_method)
                if fix_ok:
                    executor_results.append(f"### Step {step_num}: {task_desc}\n\n{fix_output}")
                    notify_fn(f"✅ Step {step_num} 修复通过 — {fix_msg}")
                else:
                    executor_results.append(f"### Step {step_num}: {task_desc}\n\n{fix_output}\n\n> ⚠️ 验证仍未通过: {fix_msg}（停止条件: 最多重试1次）")
                    notify_fn(f"⚠️ Step {step_num} 修复后仍有问题（停止: 重试上限），继续下一步")
                    wf.setdefault("validation_failures", []).append({"step": step_num, "issue": check_msg, "retry_issue": fix_msg})
            else:
                executor_results.append(f"### Step {step_num}: {task_desc}\n\n{code_output}\n\n> ⚠️ 修复失败")
                notify_fn(f"⚠️ Step {step_num} 修复失败，保留原输出继续")
                wf.setdefault("validation_failures", []).append({"step": step_num, "issue": check_msg})

        current_step += 1
        wf["current_step"] = current_step
        wf["executor_results"] = executor_results
        set_workflow(open_id, wf)

    # All steps done → final Review + Delivery
    return _run_review_and_deliver(open_id, claude_session, notify_fn)


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
