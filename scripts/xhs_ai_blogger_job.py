#!/usr/bin/env python3
import argparse
import datetime as dt
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import httpx
except ImportError:
    httpx = None
try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv(*args, **kwargs):
        return False


PROJECT_ROOT = Path("/Users/cn/Workspace/feishu-bot-bridge")
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "reports" / "xhs-ai-blogger"
DEFAULT_JOB_LOCK_FILE = PROJECT_ROOT / ".state" / "xhs_ai_blogger_job.lock"
LOW_CONFIDENCE_LINE = "Confidence: Low — trend evidence is weak today."


@dataclass
class Config:
    app_id: str
    app_secret: str
    allowed_user_ids: List[str]
    send_open_id: str
    codex_cmd: str
    codex_workdir: str
    codex_model: str
    codex_timeout_sec: int
    output_dir: Path
    niche: str
    target_persona: str
    monetization_goal: str
    brand_voice: str
    publish_windows: List[str]
    max_posts_per_day: int
    max_comments_per_day: int
    comments_per_topic: int
    signal_min_count: int
    fallback_policy: str
    executor_enabled: bool
    executor_mode: str
    executor_require_approval: bool
    executor_auto_approve: bool
    executor_script: Path
    cover_enabled: bool
    cover_images_per_post: int
    cover_output_dir: Path
    cover_template: str
    cover_script: Path
    cover_provider: str
    cover_skill_primary: str
    cover_skill_secondary: str
    cover_skill_required: bool
    cover_skill_fallback_local: bool
    cover_skill_timeout_sec: int
    content_skill_name: str
    content_skill_path: Path
    content_skill_required: bool

    @staticmethod
    def from_env() -> "Config":
        load_dotenv(PROJECT_ROOT / ".env")
        allowed = [item.strip() for item in os.getenv("ALLOWED_USER_IDS", "").split(",") if item.strip()]
        daily_send_open_id = os.getenv("DAILY_REPORT_SEND_OPEN_ID", "").strip()
        xhs_send_open_id = os.getenv("XHS_SEND_OPEN_ID", "").strip()
        xhs_timeout_raw = os.getenv("XHS_CODEX_TIMEOUT_SEC", "").strip()
        timeout_raw = xhs_timeout_raw if xhs_timeout_raw else os.getenv("CODEX_TIMEOUT_SEC", "900").strip()
        output_dir_raw = os.getenv("XHS_OUTPUT_DIR", str(DEFAULT_OUTPUT_DIR)).strip()
        publish_windows_raw = os.getenv("XHS_PUBLISH_WINDOWS", "12:30,18:30,21:30").strip()
        publish_windows = [x.strip() for x in publish_windows_raw.split(",") if x.strip()]
        if not publish_windows:
            publish_windows = ["12:30", "18:30", "21:30"]
        try:
            timeout_sec = int(timeout_raw)
        except ValueError:
            timeout_sec = 900
        if timeout_sec <= 0 and not xhs_timeout_raw:
            timeout_sec = 900
        return Config(
            app_id=os.getenv("FEISHU_APP_ID", "").strip(),
            app_secret=os.getenv("FEISHU_APP_SECRET", "").strip(),
            allowed_user_ids=allowed,
            send_open_id=xhs_send_open_id or daily_send_open_id or (allowed[0] if allowed else ""),
            codex_cmd=os.getenv("CODEX_CLI_CMD", "codex").strip() or "codex",
            codex_workdir=os.getenv("CODEX_WORKDIR", "/Users/cn/Workspace").strip() or "/Users/cn/Workspace",
            codex_model=os.getenv("XHS_CODEX_MODEL", os.getenv("CODEX_MODEL", "")).strip(),
            codex_timeout_sec=timeout_sec,
            output_dir=Path(output_dir_raw),
            niche=os.getenv("XHS_NICHE", "AI提效").strip() or "AI提效",
            target_persona=os.getenv("XHS_TARGET_PERSONA", "一二线城市职场人").strip() or "一二线城市职场人",
            monetization_goal=os.getenv("XHS_MONETIZATION_GOAL", "咨询线索").strip() or "咨询线索",
            brand_voice=os.getenv("XHS_BRAND_VOICE", "务实、直接、少鸡汤").strip() or "务实、直接、少鸡汤",
            publish_windows=publish_windows,
            max_posts_per_day=_int_env("XHS_MAX_POSTS_PER_DAY", default_value=3, min_value=1, max_value=8),
            max_comments_per_day=_int_env("XHS_MAX_COMMENTS_PER_DAY", default_value=20, min_value=4, max_value=120),
            comments_per_topic=_int_env("XHS_COMMENTS_PER_TOPIC", default_value=2, min_value=1, max_value=6),
            signal_min_count=_int_env("XHS_SIGNAL_MIN_COUNT", default_value=12, min_value=4, max_value=120),
            fallback_policy=os.getenv("XHS_FALLBACK_POLICY", "send_low_confidence").strip() or "send_low_confidence",
            executor_enabled=_bool_env("XHS_EXECUTOR_ENABLED", default_value=False),
            executor_mode=os.getenv("XHS_EXECUTOR_MODE", "queue_only").strip() or "queue_only",
            executor_require_approval=_bool_env("XHS_EXECUTOR_REQUIRE_APPROVAL", default_value=True),
            executor_auto_approve=_bool_env("XHS_EXECUTOR_AUTO_APPROVE", default_value=False),
            executor_script=Path(
                os.getenv("XHS_EXECUTOR_SCRIPT", str(PROJECT_ROOT / "scripts" / "xhs_auto_executor.py")).strip()
                or str(PROJECT_ROOT / "scripts" / "xhs_auto_executor.py")
            ),
            cover_enabled=_bool_env("XHS_COVER_ENABLED", default_value=True),
            cover_images_per_post=max(
                3,
                _int_env("XHS_COVER_IMAGES_PER_POST", default_value=3, min_value=1, max_value=9),
            ),
            cover_output_dir=Path(
                os.getenv("XHS_COVER_OUTPUT_DIR", str(DEFAULT_OUTPUT_DIR / "assets")).strip()
                or str(DEFAULT_OUTPUT_DIR / "assets")
            ),
            cover_template=os.getenv("XHS_COVER_TEMPLATE", "minimal_v1").strip() or "minimal_v1",
            cover_script=Path(
                os.getenv("XHS_COVER_SCRIPT", str(PROJECT_ROOT / "scripts" / "xhs_cover_generator.py")).strip()
                or str(PROJECT_ROOT / "scripts" / "xhs_cover_generator.py")
            ),
            cover_provider=(os.getenv("XHS_COVER_PROVIDER", "auto").strip().lower() or "auto"),
            cover_skill_primary=os.getenv("XHS_COVER_SKILL_PRIMARY", "xiaohongshu-images").strip(),
            cover_skill_secondary=os.getenv("XHS_COVER_SKILL_SECONDARY", "image-generation-mcp").strip(),
            cover_skill_required=_bool_env("XHS_COVER_SKILL_REQUIRED", default_value=False),
            cover_skill_fallback_local=_bool_env("XHS_COVER_SKILL_FALLBACK_LOCAL", default_value=True),
            cover_skill_timeout_sec=_int_env(
                "XHS_COVER_SKILL_TIMEOUT_SEC",
                default_value=1200,
                min_value=60,
                max_value=7200,
            ),
            content_skill_name=os.getenv("XHS_CONTENT_SKILL_NAME", "xiaohongshu-content-generator").strip(),
            content_skill_path=Path(
                os.getenv(
                    "XHS_CONTENT_SKILL_PATH",
                    str(Path.home() / ".codex" / "skills" / "xiaohongshu-content-generator" / "SKILL.md"),
                ).strip()
            ),
            content_skill_required=_bool_env("XHS_CONTENT_SKILL_REQUIRED", default_value=False),
        )


@dataclass
class TrendSignal:
    topic: str
    signal_summary: str
    url: str
    timestamp_text: str
    relevance: float
    monetization_intent: float
    freshness: float
    competition_pressure: float
    raw_quote: str


def _http_client(timeout_seconds: int = 120) -> httpx.Client:
    if httpx is None:
        raise RuntimeError("httpx is required for Feishu API calls.")
    return httpx.Client(timeout=timeout_seconds)


def _slug_date(forced_date: Optional[str]) -> str:
    return forced_date or dt.date.today().isoformat()


def _int_env(name: str, default_value: int, min_value: int, max_value: int) -> int:
    raw = os.getenv(name, str(default_value)).strip()
    try:
        value = int(raw)
    except ValueError:
        return default_value
    return max(min_value, min(max_value, value))


def _bool_env(name: str, default_value: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default_value
    text = raw.strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default_value


def _normalize_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _normalize_url(value: Any) -> str:
    text = str(value or "").strip()
    return text if text.startswith("http://") or text.startswith("https://") else ""


def _score_float(value: Any, default: float = 3.0) -> float:
    if isinstance(value, (int, float)):
        return max(0.0, min(5.0, float(value)))
    match = re.search(r"([0-5](?:\.\d+)?)", str(value or ""))
    if match:
        return max(0.0, min(5.0, float(match.group(1))))
    return default


def _job_lock_file_path() -> Path:
    raw = os.getenv("XHS_JOB_LOCK_FILE", str(DEFAULT_JOB_LOCK_FILE)).strip()
    return Path(raw) if raw else DEFAULT_JOB_LOCK_FILE


def _acquire_job_lock():
    lock_path = _job_lock_file_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("w", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        raise RuntimeError("XHS AI blogger job already running.")
    return handle


def _run_codex_json(cfg: Config, prompt: str, schema: Dict[str, Any], phase_name: str, use_search: bool) -> Dict[str, Any]:
    codex_bin = shutil.which(cfg.codex_cmd)
    if not codex_bin:
        raise RuntimeError(f"Codex command not found: {cfg.codex_cmd}")

    timeout_value = None if cfg.codex_timeout_sec <= 0 else cfg.codex_timeout_sec
    with tempfile.TemporaryDirectory(prefix=f"xhs-{phase_name}-") as tmp_dir:
        schema_path = Path(tmp_dir) / f"{phase_name}-schema.json"
        output_path = Path(tmp_dir) / f"{phase_name}-output.json"
        schema_path.write_text(json.dumps(schema, ensure_ascii=False, indent=2), encoding="utf-8")

        cmd: List[str] = [codex_bin]
        if use_search:
            cmd.append("--search")
        cmd.extend(
            [
                "-a",
                "never",
                "-s",
                "read-only",
                "-C",
                cfg.codex_workdir,
                "exec",
                "--skip-git-repo-check",
                "--output-schema",
                str(schema_path),
                "-o",
                str(output_path),
            ]
        )
        if cfg.codex_model:
            cmd.extend(["-m", cfg.codex_model])
        cmd.append(prompt)

        try:
            result = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout_value,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"Codex {phase_name} timed out after {cfg.codex_timeout_sec}s.") from exc

        if result.returncode != 0:
            message = (result.stderr or result.stdout or "").strip()
            raise RuntimeError(f"Codex {phase_name} failed: {message[:800]}")
        if not output_path.exists():
            raise RuntimeError(f"Codex {phase_name} output file missing.")
        raw = output_path.read_text(encoding="utf-8").strip()
        if not raw:
            raise RuntimeError(f"Codex {phase_name} returned empty output.")
        parsed = json.loads(raw)
        return {
            "parsed": parsed,
            "response": {
                "runner": "codex_cli",
                "phase": phase_name,
                "stdout_tail": (result.stdout or "")[-1200:],
                "stderr_tail": (result.stderr or "")[-1200:],
            },
        }


def _run_codex_text(
    cfg: Config,
    prompt: str,
    phase_name: str,
    use_search: bool = False,
    sandbox: str = "workspace-write",
    timeout_sec: Optional[int] = None,
) -> Dict[str, Any]:
    codex_bin = shutil.which(cfg.codex_cmd)
    if not codex_bin:
        raise RuntimeError(f"Codex command not found: {cfg.codex_cmd}")

    resolved_timeout = cfg.codex_timeout_sec if timeout_sec is None else timeout_sec
    timeout_value = None if resolved_timeout <= 0 else resolved_timeout
    cmd: List[str] = [codex_bin]
    if use_search:
        cmd.append("--search")
    cmd.extend(
        [
            "-a",
            "never",
            "-s",
            sandbox,
            "-C",
            cfg.codex_workdir,
            "exec",
            "--skip-git-repo-check",
        ]
    )
    if cfg.codex_model:
        cmd.extend(["-m", cfg.codex_model])
    cmd.append(prompt)

    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout_value,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"Codex {phase_name} timed out after {resolved_timeout}s.") from exc

    if result.returncode != 0:
        message = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"Codex {phase_name} failed: {message[:800]}")
    return {
        "runner": "codex_cli",
        "phase": phase_name,
        "stdout_tail": (result.stdout or "")[-1200:],
        "stderr_tail": (result.stderr or "")[-1200:],
    }


def _skill_file_for_name(skill_name: str) -> Path:
    return Path.home() / ".codex" / "skills" / skill_name / "SKILL.md"


def _skill_token_from_name(skill_name: str, required: bool, env_name: str) -> str:
    name = skill_name.strip().lower()
    if not name:
        if required:
            raise RuntimeError(f"{env_name} is required but empty.")
        return ""
    if not re.fullmatch(r"[a-z0-9-]{1,64}", name):
        if required:
            raise RuntimeError(f"Invalid {env_name}: {skill_name}")
        return ""
    skill_path = _skill_file_for_name(name)
    if not skill_path.exists():
        if required:
            raise RuntimeError(f"Required skill not found for {env_name}: {skill_path}")
        return ""
    return f"${name}"


def _skill_token(cfg: Config) -> str:
    name = cfg.content_skill_name.strip().lower()
    if not name:
        return ""
    if not re.fullmatch(r"[a-z0-9-]{1,64}", name):
        if cfg.content_skill_required:
            raise RuntimeError(f"Invalid XHS_CONTENT_SKILL_NAME: {cfg.content_skill_name}")
        return ""
    if not cfg.content_skill_path.exists():
        if cfg.content_skill_required:
            raise RuntimeError(f"Required content skill not found: {cfg.content_skill_path}")
        return ""
    return f"${name}"


def _skill_instruction(cfg: Config, purpose: str) -> str:
    token = _skill_token(cfg)
    if not token:
        return ""
    return (
        f"Skill requirement:\n"
        f"- Use {token} for this {purpose} task.\n"
        f"- Follow its workflow and output constraints strictly.\n"
        f"- If evidence is weak, keep output but explicitly mark confidence.\n\n"
    )


def _phase_a_schema() -> Dict[str, Any]:
    signal_schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "topic": {"type": "string"},
            "signal_summary": {"type": "string"},
            "url": {"type": "string"},
            "timestamp_text": {"type": "string"},
            "relevance": {"type": "number"},
            "monetization_intent": {"type": "number"},
            "freshness": {"type": "number"},
            "competition_pressure": {"type": "number"},
            "raw_quote": {"type": "string"},
        },
        "required": [
            "topic",
            "signal_summary",
            "url",
            "timestamp_text",
            "relevance",
            "monetization_intent",
            "freshness",
            "competition_pressure",
            "raw_quote",
        ],
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "search_window": {"type": "string"},
            "signals": {"type": "array", "items": signal_schema, "minItems": 1, "maxItems": 80},
        },
        "required": ["search_window", "signals"],
    }


def _phase_b_schema() -> Dict[str, Any]:
    note_schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "topic": {"type": "string"},
            "title_a": {"type": "string"},
            "title_b": {"type": "string"},
            "hook": {"type": "string"},
            "key_points": {"type": "array", "items": {"type": "string"}, "minItems": 3, "maxItems": 5},
            "cta": {"type": "string"},
            "tags": {"type": "array", "items": {"type": "string"}, "minItems": 8, "maxItems": 15},
        },
        "required": ["topic", "title_a", "title_b", "hook", "key_points", "cta", "tags"],
    }
    engagement_schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "topic": {"type": "string"},
            "target_profile_hint": {"type": "string"},
            "comment_samples": {"type": "array", "items": {"type": "string"}, "minItems": 3, "maxItems": 5},
        },
        "required": ["topic", "target_profile_hint", "comment_samples"],
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "note_packages": {"type": "array", "items": note_schema, "minItems": 1, "maxItems": 8},
            "engagement_plan": {"type": "array", "items": engagement_schema, "minItems": 1, "maxItems": 8},
            "kpi_focus": {"type": "array", "items": {"type": "string"}, "minItems": 3, "maxItems": 6},
            "reflection": {"type": "string"},
        },
        "required": ["note_packages", "engagement_plan", "kpi_focus", "reflection"],
    }


def _phase_a_hunt(cfg: Config, report_date: str) -> Dict[str, Any]:
    skill_instruction = _skill_instruction(cfg, "trend research")
    prompt = (
        f"Today is {report_date}. You are an operations researcher for Xiaohongshu content growth.\n\n"
        f"{skill_instruction}"
        "Task:\n"
        "- Find real, recent trend signals for the niche and persona.\n"
        "- Prioritize evidence from the last 24-48h, allow up to 7 days when needed.\n"
        "- Keep only signals that can support content topics with monetization potential.\n\n"
        f"Niche: {cfg.niche}\n"
        f"Target persona: {cfg.target_persona}\n"
        f"Monetization goal: {cfg.monetization_goal}\n\n"
        "Search guidance:\n"
        "- Prefer Xiaohongshu, Weibo, Zhihu, Bilibili, and creator discussion pages.\n"
        "- Include direct URLs and concise timestamp text.\n"
        "- No fabricated links or quotes.\n\n"
        "Output rules:\n"
        "- Return 8-15 signals when possible.\n"
        "- relevance / monetization_intent / freshness / competition_pressure are scored 0-5.\n"
        "- competition_pressure means crowdedness (higher = more crowded).\n"
        "- raw_quote should be a short source quote or faithful paraphrase.\n"
    )
    return _run_codex_json(cfg, prompt, _phase_a_schema(), "xhs-phase-a", use_search=True)


def _phase_b_draft(cfg: Config, report_date: str, selected: List[TrendSignal], low_confidence: bool) -> Dict[str, Any]:
    selected_json = [asdict(item) for item in selected]
    skill_instruction = _skill_instruction(cfg, "content drafting")
    prompt = (
        "You are a Xiaohongshu content strategist writing concise, execution-ready Chinese output.\n"
        f"{skill_instruction}"
        "Rules:\n"
        "- Use only supplied topics and evidence.\n"
        "- Keep voice consistent with brand voice.\n"
        "- Writing style must follow 'AI漫游' persona: first-person web-travel diary, like a human exploring platforms and sharing real-time observations.\n"
        "- Use concrete action verbs (e.g., 我刷到/我点开/我对比了/我踩坑了), avoid robotic summary tone.\n"
        "- Simulate human behavior and tone, but do not fabricate unverifiable personal events; every claim must map to provided signals.\n"
        "- Calibrate tone to current Xiaohongshu explore hot-note style: compact title, early tension, first-person scene.\n"
        "- Emulate hot-note title patterns: [人物/关系+场景+数字节奏+结果反差] OR [先说结论+对象称呼].\n"
        "- Title should avoid generic summaries like 'XX指南/XX复盘'; prefer concrete scene and outcome.\n"
        "- Opening sentence must immediately place scene/time/action (e.g., 今天/刚刚/这周 + 我 + 动作).\n"
        "- Keep paragraph rhythm short, spoken, and human. Avoid long semicolon-heavy machine text.\n"
        "- Each note should naturally support a 3-part content flow: 见闻 -> 拆解 -> 可执行动作/提问.\n"
        "- Title length target: 12-20 Chinese chars.\n"
        "- Use at most 1-2 emotional markers (e.g. ？/！/emoji), avoid exaggerated clickbait.\n"
        "- End with one direct question that invites comments.\n"
        "- No unverifiable claims.\n"
        "- Generate one note package per topic and one engagement item per topic.\n"
        "- comments must be contextual and non-spam.\n"
        "- Every note's tags must include '#AI'.\n"
        f"- Confidence mode: {'Low' if low_confidence else 'Normal'}.\n\n"
        f"Date: {report_date}\n"
        f"Brand voice: {cfg.brand_voice}\n"
        f"Selected signals:\n{json.dumps(selected_json, ensure_ascii=False, indent=2)}\n"
    )
    return _run_codex_json(cfg, prompt, _phase_b_schema(), "xhs-phase-b", use_search=False)


def _parse_signals(payload: Dict[str, Any]) -> List[TrendSignal]:
    raw = payload.get("signals")
    if not isinstance(raw, list) or not raw:
        raise ValueError("Phase A returned no signals.")
    parsed: List[TrendSignal] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        signal = TrendSignal(
            topic=_normalize_text(item.get("topic")),
            signal_summary=_normalize_text(item.get("signal_summary")),
            url=_normalize_url(item.get("url")),
            timestamp_text=_normalize_text(item.get("timestamp_text")),
            relevance=_score_float(item.get("relevance")),
            monetization_intent=_score_float(item.get("monetization_intent")),
            freshness=_score_float(item.get("freshness")),
            competition_pressure=_score_float(item.get("competition_pressure")),
            raw_quote=_normalize_text(item.get("raw_quote")),
        )
        if not signal.topic or not signal.signal_summary or not signal.url:
            continue
        parsed.append(signal)
    if not parsed:
        raise ValueError("Phase A signals could not be normalized.")
    return parsed


def _signal_score(signal: TrendSignal) -> float:
    score = 0.0
    score += signal.relevance * 0.32
    score += signal.monetization_intent * 0.30
    score += signal.freshness * 0.23
    score += (5.0 - signal.competition_pressure) * 0.15
    return round(score, 4)


def _dedupe_signals(signals: List[TrendSignal]) -> List[TrendSignal]:
    seen_urls = set()
    deduped: List[TrendSignal] = []
    for item in signals:
        key = item.url.lower()
        if key in seen_urls:
            continue
        seen_urls.add(key)
        deduped.append(item)
    return deduped


def _rank_signals(signals: List[TrendSignal]) -> List[Dict[str, Any]]:
    ranked: List[Dict[str, Any]] = []
    for signal in _dedupe_signals(signals):
        ranked.append({"signal": signal, "score": _signal_score(signal)})
    ranked.sort(key=lambda item: item["score"], reverse=True)
    return ranked


def _is_low_confidence(cfg: Config, ranked_signals: List[Dict[str, Any]]) -> bool:
    if len(ranked_signals) < cfg.signal_min_count:
        return True
    top_score = float(ranked_signals[0]["score"]) if ranked_signals else 0.0
    return top_score < 3.2


def _split_text(text: str, max_len: int = 2600) -> List[str]:
    chunks: List[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        if len(current) + len(line) > max_len and current:
            chunks.append(current)
            current = line
        else:
            current += line
    if current:
        chunks.append(current)
    return chunks


def _build_markdown(
    report_date: str,
    cfg: Config,
    selected: List[TrendSignal],
    ranked_signals: List[Dict[str, Any]],
    phase_b_parsed: Dict[str, Any],
    low_confidence: bool,
    execution_result: Optional[Dict[str, Any]] = None,
) -> str:
    notes = phase_b_parsed.get("note_packages") or []
    engagement = phase_b_parsed.get("engagement_plan") or []
    kpi_focus = phase_b_parsed.get("kpi_focus") or []
    reflection = _normalize_text(phase_b_parsed.get("reflection"))

    signal_lookup = {item.topic: item for item in selected}
    note_lookup = {str(item.get("topic")).strip(): item for item in notes if isinstance(item, dict)}
    engage_lookup = {str(item.get("topic")).strip(): item for item in engagement if isinstance(item, dict)}

    lines: List[str] = []
    lines.append("🤖 小红书 AI 博主系统日报")
    lines.append("")
    if low_confidence:
        lines.append(LOW_CONFIDENCE_LINE)
        lines.append("")
    lines.append("Today Objective")
    lines.append(f"- 日期: {report_date}")
    lines.append(f"- 赛道: {cfg.niche}")
    lines.append(f"- 人群: {cfg.target_persona}")
    lines.append(f"- 目标: {cfg.monetization_goal}")
    lines.append("")

    lines.append("Selected Topics")
    for rank, item in enumerate(ranked_signals[: cfg.max_posts_per_day], start=1):
        signal: TrendSignal = item["signal"]
        lines.append(
            f"- T{rank}: {signal.topic} | score={item['score']} | fresh={signal.freshness} | pay={signal.monetization_intent}"
        )
        lines.append(f"  - signal: {signal.signal_summary}")
        lines.append(f"  - quote: {signal.raw_quote}")
        lines.append(f"  - source: {signal.url}")
    lines.append("")

    lines.append("Publishing Plan")
    for index, topic in enumerate(signal_lookup.keys(), start=1):
        slot = cfg.publish_windows[(index - 1) % len(cfg.publish_windows)]
        note = note_lookup.get(topic, {})
        title_a = _normalize_text(note.get("title_a")) or "待补充标题A"
        title_b = _normalize_text(note.get("title_b")) or "待补充标题B"
        hook = _normalize_text(note.get("hook")) or "待补充开场 Hook"
        lines.append(f"- {slot} | {topic}")
        lines.append(f"  - title A: {title_a}")
        lines.append(f"  - title B: {title_b}")
        lines.append(f"  - hook: {hook}")
    lines.append("")

    lines.append("Engagement Plan")
    per_topic_comments = max(2, cfg.max_comments_per_day // max(1, len(selected)))
    lines.append(f"- 总评论预算: {cfg.max_comments_per_day}")
    lines.append(f"- 每主题评论预算: {per_topic_comments}")
    for topic in signal_lookup.keys():
        item = engage_lookup.get(topic, {})
        hint = _normalize_text(item.get("target_profile_hint"))
        comments = item.get("comment_samples") if isinstance(item.get("comment_samples"), list) else []
        lines.append(f"- {topic}")
        if hint:
            lines.append(f"  - target: {hint}")
        for comment in comments[:3]:
            lines.append(f"  - sample: {_normalize_text(comment)}")
    lines.append("")

    lines.append("Risk/Compliance Checks")
    lines.append("- 禁止模板化刷评与短时高频重复评论")
    lines.append("- 禁止无依据数据、收益承诺、医疗金融等敏感承诺")
    lines.append("- 争议内容先人工复核再发布")
    lines.append("")

    lines.append("KPI Snapshot")
    lines.append(f"- 今日候选信号: {len(ranked_signals)}")
    lines.append(f"- 计划发布数: {len(selected)}")
    lines.append(f"- 目标评论数: {cfg.max_comments_per_day}")
    for item in kpi_focus[:5]:
        lines.append(f"- KPI focus: {_normalize_text(item)}")
    lines.append("")

    lines.append("Reflection + Tomorrow Optimization")
    lines.append(f"- {_normalize_text(reflection) or '明天优先保留高保存率选题，淘汰低互动模板。'}")
    if execution_result:
        lines.append("")
        lines.append("Execution Status")
        lines.append(f"- mode: {_normalize_text(execution_result.get('mode'))}")
        lines.append(f"- status: {_normalize_text(execution_result.get('status'))}")
        lines.append("- No Retry Policy: find-error fail-fast enabled")
        lines.append(f"- published: {execution_result.get('publish_success', 0)}/{execution_result.get('publish_total', 0)}")
        lines.append(f"- commented: {execution_result.get('comment_success', 0)}/{execution_result.get('comment_total', 0)}")
        if execution_result.get("message"):
            lines.append(f"- message: {_normalize_text(execution_result.get('message'))}")
        records = execution_result.get("records") if isinstance(execution_result.get("records"), list) else []
        for record in records:
            if not isinstance(record, dict):
                continue
            if _normalize_text(record.get("status")) != "failed_not_found":
                continue
            step = _normalize_text(record.get("action_id")) or "unknown-step"
            detail = _normalize_text(record.get("error")) or _normalize_text(record.get("stderr_tail")) or "not_found"
            lines.append(f"- failed_not_found: {step} | {detail}")
    return "\n".join(lines).strip() + "\n"


def _validate_markdown(markdown: str) -> None:
    if markdown.count("source: http") < 1:
        raise ValueError("Report must include at least one clickable source URL.")
    if "Selected Topics" not in markdown:
        raise ValueError("Report missing required section: Selected Topics.")


def _get_tenant_access_token(cfg: Config) -> str:
    payload = {"app_id": cfg.app_id, "app_secret": cfg.app_secret}
    with _http_client(timeout_seconds=30) as client:
        response = client.post(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            json=payload,
        )
    response.raise_for_status()
    data = response.json()
    if data.get("code") != 0:
        raise RuntimeError(f"Failed to get tenant token: {data}")
    token = str(data.get("tenant_access_token", "")).strip()
    if not token:
        raise RuntimeError("Feishu tenant access token is empty.")
    return token


def send_to_feishu(cfg: Config, title: str, body_markdown: str, dry_run: bool) -> None:
    if dry_run:
        print("[dry-run] skip Feishu sending.")
        return
    if not cfg.app_id or not cfg.app_secret:
        raise RuntimeError("Missing FEISHU_APP_ID/FEISHU_APP_SECRET.")
    if not cfg.send_open_id:
        raise RuntimeError("Missing XHS_SEND_OPEN_ID / DAILY_REPORT_SEND_OPEN_ID / ALLOWED_USER_IDS.")
    token = _get_tenant_access_token(cfg)
    chunks = _split_text(body_markdown, max_len=2600)
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=utf-8",
    }
    with _http_client(timeout_seconds=60) as client:
        for index, chunk in enumerate(chunks, start=1):
            prefix = f"{title} ({index}/{len(chunks)})\n" if len(chunks) > 1 else f"{title}\n"
            payload = {
                "receive_id": cfg.send_open_id,
                "msg_type": "text",
                "content": json.dumps({"text": prefix + chunk}, ensure_ascii=False),
            }
            response = client.post(
                "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id",
                headers=headers,
                json=payload,
            )
            response.raise_for_status()
            data = response.json()
            if data.get("code") != 0:
                raise RuntimeError(f"Feishu send failed: {data}")
    print(f"Feishu sent {len(chunks)} message(s) to {cfg.send_open_id}.")


def _notify_session_expired(cfg: Config, report_date: str, execution_result: Dict[str, Any], dry_run: bool) -> None:
    status = _normalize_text(execution_result.get("status"))
    if status != "session_expired":
        return
    reason = _normalize_text(execution_result.get("message")) or "小红书登录会话已过期。"
    relogin_hint = _normalize_text(execution_result.get("relogin_hint")) or (
        "./scripts/xhs_web_session_auth.sh login --account-id <xhs_account_id> --username <login_name> --url https://creator.xiaohongshu.com/new/home"
    )
    body = (
        "⚠️ 小红书会话已过期，自动执行已暂停。\n"
        f"- 日期: {report_date}\n"
        f"- 原因: {reason}\n"
        "- 请重新网页授权登录后重跑任务。\n"
        f"- 参考命令: {relogin_hint}\n"
    )
    send_to_feishu(cfg, f"【XHS Session Expired】{report_date}", body, dry_run=dry_run)


def _sources_from_signals(signals: List[TrendSignal]) -> List[Dict[str, str]]:
    seen = set()
    sources: List[Dict[str, str]] = []
    for signal in signals:
        if not signal.url or signal.url in seen:
            continue
        seen.add(signal.url)
        sources.append({"title": signal.topic, "url": signal.url})
    return sources


def _build_action_queue(
    cfg: Config,
    report_date: str,
    selected: List[TrendSignal],
    phase_b_parsed: Dict[str, Any],
) -> List[Dict[str, Any]]:
    def _build_three_content_blocks(hook_text: str, points: List[str], cta_text: str, topic_text: str) -> List[str]:
        def _clip(text: str, limit: int) -> str:
            normalized = _normalize_text(text).strip("，。；、:：!?！？ ")
            if len(normalized) <= limit:
                return normalized
            return normalized[:limit].rstrip("，。；、:：!?！？ ")

        def _compact_point(text: str, limit: int = 20) -> str:
            normalized = _normalize_text(text)
            normalized = re.sub(
                r"^(信号依据|时间备注|落地顺序|交付形态|数据复盘最小闭环|执行建议|补充说明)\s*[：:]",
                "",
                normalized,
            )
            normalized = re.sub(r"“[^”]+”", "", normalized)
            normalized = re.sub(r"\d{4}-\d{2}-\d{2}", "当日", normalized)
            pieces = [segment.strip("：: ") for segment in re.split(r"[。；;，,]", normalized) if segment.strip()]
            candidate = next((segment for segment in pieces if len(segment) >= 6), pieces[0] if pieces else normalized)
            if len(candidate) < 6 or re.search(r"(提到|日期|标注|发布|当日)$", candidate):
                candidate = "先在小范围验证效果"
            return _clip(candidate, limit)

        normalized_points = [_normalize_text(item) for item in points if _normalize_text(item)]
        p1 = _compact_point(normalized_points[0]) if normalized_points else f"{topic_text}先做小范围验证"
        p2 = _compact_point(normalized_points[1]) if len(normalized_points) > 1 else "只盯一个关键指标看反馈"
        if p1 == p2:
            p2 = "只盯一个关键指标看反馈"
        hook_clean = _clip(hook_text, 34)
        cta_clean = _clip(cta_text, 30)

        if hook_clean:
            if re.match(r"^(我|今天|刚刚|这周|昨晚)", hook_clean):
                block1 = f"{hook_clean}。"
            else:
                block1 = f"我点开一个{topic_text}的讨论，第一反应是：{hook_clean}。"
        else:
            block1 = f"今天我刷到一个{topic_text}话题，评论区的信息密度比正文更高。"

        block2 = f"我拆成3步执行：①{p1}；②{p2}；③先跑24小时小测试，再决定要不要放量。"

        if cta_clean:
            if not re.search(r"[？?]$", cta_clean):
                cta_clean = f"{cta_clean}，你会先试哪一步？"
            block3 = f"目前我最看重的是可复制和省时间。{cta_clean}"
        else:
            block3 = "目前我最看重的是可复制和省时间。你会先从哪一步开始？"
        return [block1, block2, block3]

    notes = phase_b_parsed.get("note_packages") if isinstance(phase_b_parsed.get("note_packages"), list) else []
    engagement = phase_b_parsed.get("engagement_plan") if isinstance(phase_b_parsed.get("engagement_plan"), list) else []
    note_lookup = {str(item.get("topic")).strip(): item for item in notes if isinstance(item, dict)}
    engagement_lookup = {str(item.get("topic")).strip(): item for item in engagement if isinstance(item, dict)}
    queue: List[Dict[str, Any]] = []
    if not cfg.cover_enabled:
        raise RuntimeError("XHS_COVER_ENABLED must be true for image publish flow.")

    for index, signal in enumerate(selected, start=1):
        topic = signal.topic
        note_item = note_lookup.get(topic, {})
        slot = cfg.publish_windows[(index - 1) % len(cfg.publish_windows)]
        key_points = note_item.get("key_points") if isinstance(note_item.get("key_points"), list) else []
        tags = note_item.get("tags") if isinstance(note_item.get("tags"), list) else []
        normalized_tags = [str(tag).strip().lstrip("#") for tag in tags if str(tag).strip()]
        if "ai" not in {item.lower() for item in normalized_tags}:
            normalized_tags.append("AI")
        hook = _normalize_text(note_item.get("hook"))
        cta = _normalize_text(note_item.get("cta"))

        content_blocks = _build_three_content_blocks(hook, key_points, cta, topic)
        content_lines = [
            f"① {_normalize_text(content_blocks[0])}",
            f"② {_normalize_text(content_blocks[1])}",
            f"③ {_normalize_text(content_blocks[2])}",
        ]
        if normalized_tags:
            tag_text = " ".join([f"#{_normalize_text(tag).lstrip('#')}" for tag in normalized_tags[:15] if _normalize_text(tag)])
            if tag_text:
                content_lines.append(tag_text)
        note_content = "\n".join(content_lines).strip()
        primary_title = _normalize_text(note_item.get("title_a")) or topic
        images: List[str] = []
        for image_index in range(1, max(3, cfg.cover_images_per_post) + 1):
            block_preview = _normalize_text(content_blocks[(image_index - 1) % len(content_blocks)])[:14]
            image_title = f"{primary_title}｜{block_preview}" if block_preview else primary_title
            cover_path = _generate_cover(
                cfg=cfg,
                report_date=report_date,
                post_index=index,
                image_index=image_index,
                title=image_title,
                keyword=topic,
                content=note_content,
            )
            images.append(str(cover_path))
        queue.append(
            {
                "action_id": f"publish-{index}",
                "type": "publish",
                "topic": topic,
                "scheduled_slot": slot,
                "images": images,
                "payload": {
                    "title": primary_title,
                    "title_alt": _normalize_text(note_item.get("title_b")),
                    "content": note_content,
                    "content_blocks": content_blocks,
                    "tags": [str(tag).strip() for tag in normalized_tags if str(tag).strip()],
                    "images": images,
                    "report_date": report_date,
                },
            }
        )

    comments_per_topic = max(1, cfg.comments_per_topic)
    for index, signal in enumerate(selected, start=1):
        topic = signal.topic
        engage_item = engagement_lookup.get(topic, {})
        comments = engage_item.get("comment_samples") if isinstance(engage_item.get("comment_samples"), list) else []
        for comment_index, comment in enumerate(comments[:comments_per_topic], start=1):
            queue.append(
                {
                    "action_id": f"comment-{index}-{comment_index}",
                    "type": "comment",
                    "topic": topic,
                    "scheduled_slot": cfg.publish_windows[(index - 1) % len(cfg.publish_windows)],
                    "payload": {
                        "comment": _normalize_text(comment),
                        "target_profile_hint": _normalize_text(engage_item.get("target_profile_hint")),
                        "report_date": report_date,
                    },
                }
            )
    return queue


def _resolve_cover_skill_tokens(cfg: Config) -> List[str]:
    tokens: List[str] = []
    seen = set()
    for env_name, raw_value in [
        ("XHS_COVER_SKILL_PRIMARY", cfg.cover_skill_primary),
        ("XHS_COVER_SKILL_SECONDARY", cfg.cover_skill_secondary),
    ]:
        token = _skill_token_from_name(raw_value, required=False, env_name=env_name)
        if token and token not in seen:
            tokens.append(token)
            seen.add(token)
    if not tokens and cfg.cover_skill_required:
        primary_path = _skill_file_for_name(cfg.cover_skill_primary.strip().lower())
        secondary_path = _skill_file_for_name(cfg.cover_skill_secondary.strip().lower())
        raise RuntimeError(
            "Cover skill is required but unavailable. "
            f"Checked: {primary_path} and {secondary_path}"
        )
    return tokens


def _generate_cover_with_skill(
    cfg: Config,
    report_date: str,
    post_index: int,
    image_index: int,
    title: str,
    keyword: str,
    content: str,
    output_path: Path,
) -> Optional[Path]:
    tokens = _resolve_cover_skill_tokens(cfg)
    if not tokens:
        return None
    output_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_body = (
        "Generate exactly 1 Xiaohongshu cover image.\n"
        "Constraints:\n"
        "- image format: PNG\n"
        "- image ratio: 3:4 vertical\n"
        "- style: clean, high-contrast, readable for social post\n"
        "- do not include real person faces\n"
        "- keep text on image concise and Chinese-first\n"
        "- safe and compliant visual style\n\n"
        f"Title: {title}\n"
        f"Keyword: {keyword}\n"
        f"Date: {report_date}\n"
        f"Content context:\n{content[:1000]}\n\n"
        f"Write final file to this exact absolute path:\n{output_path}\n"
        "If your tool writes elsewhere first, move/copy the final PNG to this path.\n"
        "Return a short status message only.\n"
    )
    errors: List[str] = []
    for token in tokens:
        prompt = f"{token}\n\n{prompt_body}"
        try:
            _run_codex_text(
                cfg=cfg,
                prompt=prompt,
                phase_name=f"xhs-cover-skill-{post_index}-{image_index}",
                use_search=False,
                sandbox="workspace-write",
                timeout_sec=cfg.cover_skill_timeout_sec,
            )
        except Exception as exc:
            errors.append(f"{token}: {_normalize_text(exc)}")
            continue
        if output_path.exists() and output_path.stat().st_size > 0:
            return output_path
        errors.append(f"{token}: output file not created at {output_path}")

    if cfg.cover_skill_required:
        raise RuntimeError("cover skill generation failed: " + " | ".join(errors))
    return None


def _generate_cover_local(
    cfg: Config,
    report_date: str,
    post_index: int,
    image_index: int,
    title: str,
    keyword: str,
    output_path: Path,
) -> Path:
    if not cfg.cover_script.exists():
        raise RuntimeError(f"cover generator script missing: {cfg.cover_script}")
    if cfg.cover_template != "minimal_v1":
        raise RuntimeError(f"unsupported XHS_COVER_TEMPLATE: {cfg.cover_template}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable,
        str(cfg.cover_script),
        "--title",
        title,
        "--date",
        report_date,
        "--keyword",
        keyword,
        "--output",
        str(output_path),
    ]
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if result.returncode != 0:
        err = _normalize_text((result.stderr or result.stdout)[-500:])
        raise RuntimeError(f"cover generation failed: {err}")
    if not output_path.exists():
        raise RuntimeError(f"cover image missing after generation: {output_path}")
    return output_path


def _generate_cover(
    cfg: Config,
    report_date: str,
    post_index: int,
    image_index: int,
    title: str,
    keyword: str,
    content: str,
) -> Path:
    provider = (cfg.cover_provider or "auto").strip().lower()
    if provider not in {"auto", "local", "codex_skill"}:
        raise RuntimeError(f"unsupported XHS_COVER_PROVIDER: {cfg.cover_provider}")

    output_dir = cfg.cover_output_dir / report_date
    output_path = output_dir / f"cover-{post_index}-{image_index}.png"
    if output_path.exists():
        output_path.unlink()

    if provider in {"auto", "codex_skill"}:
        generated = _generate_cover_with_skill(
            cfg=cfg,
            report_date=report_date,
            post_index=post_index,
            image_index=image_index,
            title=title,
            keyword=keyword,
            content=content,
            output_path=output_path,
        )
        if generated is not None:
            return generated
        if provider == "codex_skill" and not cfg.cover_skill_fallback_local:
            raise RuntimeError("cover skill generation failed and local fallback is disabled.")

    return _generate_cover_local(
        cfg=cfg,
        report_date=report_date,
        post_index=post_index,
        image_index=image_index,
        title=title,
        keyword=keyword,
        output_path=output_path,
    )


def write_outputs(
    cfg: Config,
    report_date: str,
    markdown: str,
    phase_a: Dict[str, Any],
    phase_b: Dict[str, Any],
    ranked_signals: List[Dict[str, Any]],
    selected: List[TrendSignal],
    low_confidence: bool,
    action_queue: List[Dict[str, Any]],
    execution_result: Optional[Dict[str, Any]] = None,
) -> Dict[str, Path]:
    cfg.output_dir.mkdir(parents=True, exist_ok=True)
    markdown_path = cfg.output_dir / f"{report_date}.md"
    json_path = cfg.output_dir / f"{report_date}.json"
    markdown_path.write_text(markdown, encoding="utf-8")

    json_payload = {
        "report_date": report_date,
        "low_confidence": low_confidence,
        "config": {
            "niche": cfg.niche,
            "target_persona": cfg.target_persona,
            "monetization_goal": cfg.monetization_goal,
            "brand_voice": cfg.brand_voice,
            "publish_windows": cfg.publish_windows,
            "max_posts_per_day": cfg.max_posts_per_day,
            "max_comments_per_day": cfg.max_comments_per_day,
            "comments_per_topic": cfg.comments_per_topic,
            "signal_min_count": cfg.signal_min_count,
            "fallback_policy": cfg.fallback_policy,
            "executor_enabled": cfg.executor_enabled,
            "executor_mode": cfg.executor_mode,
            "executor_require_approval": cfg.executor_require_approval,
            "executor_auto_approve": cfg.executor_auto_approve,
            "executor_script": str(cfg.executor_script),
            "cover_enabled": cfg.cover_enabled,
            "cover_images_per_post": cfg.cover_images_per_post,
            "cover_output_dir": str(cfg.cover_output_dir),
            "cover_template": cfg.cover_template,
            "cover_script": str(cfg.cover_script),
            "cover_provider": cfg.cover_provider,
            "cover_skill_primary": cfg.cover_skill_primary,
            "cover_skill_secondary": cfg.cover_skill_secondary,
            "cover_skill_required": cfg.cover_skill_required,
            "cover_skill_fallback_local": cfg.cover_skill_fallback_local,
            "cover_skill_timeout_sec": cfg.cover_skill_timeout_sec,
            "cover_skill_loaded": bool(_resolve_cover_skill_tokens(cfg)),
            "codex_cmd": cfg.codex_cmd,
            "codex_model": cfg.codex_model,
            "codex_timeout_sec": cfg.codex_timeout_sec,
            "content_skill_name": cfg.content_skill_name,
            "content_skill_path": str(cfg.content_skill_path),
            "content_skill_required": cfg.content_skill_required,
            "content_skill_loaded": bool(_skill_token(cfg)),
        },
        "phase_a": {
            "parsed": phase_a["parsed"],
            "runner": phase_a["response"].get("runner"),
            "stdout_tail": phase_a["response"].get("stdout_tail"),
            "stderr_tail": phase_a["response"].get("stderr_tail"),
        },
        "ranked_signals": [
            {"score": item["score"], "signal": asdict(item["signal"])} for item in ranked_signals
        ],
        "selected_signals": [asdict(item) for item in selected],
        "action_queue": action_queue,
        "execution_result": execution_result or {},
        "sources": _sources_from_signals(selected),
        "phase_b": {
            "parsed": phase_b["parsed"],
            "runner": phase_b["response"].get("runner"),
            "stdout_tail": phase_b["response"].get("stdout_tail"),
            "stderr_tail": phase_b["response"].get("stderr_tail"),
        },
        "report_markdown": markdown,
    }
    json_path.write_text(json.dumps(json_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"markdown": markdown_path, "json": json_path}


def _run_executor(cfg: Config, plan_json_path: Path, dry_run: bool) -> Dict[str, Any]:
    if not cfg.executor_enabled:
        return {
            "status": "disabled",
            "mode": cfg.executor_mode,
            "publish_total": 0,
            "publish_success": 0,
            "comment_total": 0,
            "comment_success": 0,
            "message": "executor is disabled by config",
        }
    if not cfg.executor_script.exists():
        return {
            "status": "failed",
            "mode": cfg.executor_mode,
            "publish_total": 0,
            "publish_success": 0,
            "comment_total": 0,
            "comment_success": 0,
            "message": f"executor script missing: {cfg.executor_script}",
        }
    output_path = plan_json_path.with_name(plan_json_path.stem + ".execution.json")
    cmd = ["/usr/bin/python3", "-u", str(cfg.executor_script), "--plan-json", str(plan_json_path), "--output", str(output_path)]
    if dry_run:
        cmd.append("--dry-run")
    if cfg.executor_auto_approve:
        cmd.append("--approve")
    if cfg.executor_mode:
        cmd.extend(["--mode", cfg.executor_mode])
    if cfg.executor_require_approval:
        cmd.append("--require-approval")

    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    parsed_result: Optional[Dict[str, Any]] = None
    if output_path.exists():
        try:
            parsed = json.loads(output_path.read_text(encoding="utf-8"))
            if isinstance(parsed, dict):
                parsed_result = parsed
        except Exception:
            parsed_result = None
    if parsed_result is not None:
        return parsed_result
    if result.returncode != 0:
        err = _normalize_text((result.stderr or result.stdout)[-1000:])
        return {
            "status": "failed",
            "mode": cfg.executor_mode,
            "publish_total": 0,
            "publish_success": 0,
            "comment_total": 0,
            "comment_success": 0,
            "message": f"executor failed: {err}",
        }
    return {
        "status": "unknown",
        "mode": cfg.executor_mode,
        "publish_total": 0,
        "publish_success": 0,
        "comment_total": 0,
        "comment_success": 0,
        "message": "executor finished without readable result file",
    }


def _load_mock_json(path: Path, phase_name: str) -> Dict[str, Any]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {"parsed": raw, "response": {"runner": "mock", "phase": phase_name}}


def run(
    report_date: str,
    dry_run: bool,
    mock_signals_file: Optional[Path],
    mock_draft_file: Optional[Path],
) -> Dict[str, Any]:
    lock_handle = _acquire_job_lock()
    try:
        cfg = Config.from_env()
        if not shutil.which(cfg.codex_cmd) and not (mock_signals_file and mock_draft_file):
            raise RuntimeError(f"Codex command not found: {cfg.codex_cmd}")
        phase_a = _load_mock_json(mock_signals_file, "xhs-phase-a") if mock_signals_file else _phase_a_hunt(cfg, report_date)
        signals = _parse_signals(phase_a["parsed"])
        ranked_signals = _rank_signals(signals)
        selected_items = ranked_signals[: cfg.max_posts_per_day]
        selected = [item["signal"] for item in selected_items]
        low_confidence = _is_low_confidence(cfg, ranked_signals)
        if cfg.fallback_policy != "send_low_confidence" and low_confidence:
            raise RuntimeError(f"Unsupported fallback policy: {cfg.fallback_policy}")
        phase_b = _load_mock_json(mock_draft_file, "xhs-phase-b") if mock_draft_file else _phase_b_draft(cfg, report_date, selected, low_confidence)
        action_queue = _build_action_queue(cfg, report_date, selected, phase_b["parsed"])
        markdown = _build_markdown(
            report_date,
            cfg,
            selected,
            ranked_signals,
            phase_b["parsed"],
            low_confidence,
            execution_result=None,
        )
        _validate_markdown(markdown)
        outputs = write_outputs(
            cfg=cfg,
            report_date=report_date,
            markdown=markdown,
            phase_a=phase_a,
            phase_b=phase_b,
            ranked_signals=ranked_signals,
            selected=selected,
            low_confidence=low_confidence,
            action_queue=action_queue,
            execution_result=None,
        )
        execution_result = _run_executor(cfg, outputs["json"], dry_run=dry_run)
        markdown = _build_markdown(
            report_date,
            cfg,
            selected,
            ranked_signals,
            phase_b["parsed"],
            low_confidence,
            execution_result=execution_result,
        )
        _validate_markdown(markdown)
        outputs = write_outputs(
            cfg=cfg,
            report_date=report_date,
            markdown=markdown,
            phase_a=phase_a,
            phase_b=phase_b,
            ranked_signals=ranked_signals,
            selected=selected,
            low_confidence=low_confidence,
            action_queue=action_queue,
            execution_result=execution_result,
        )
        title = f"【XHS Low Confidence】{report_date}" if low_confidence else f"【XHS】{report_date}"
        send_to_feishu(cfg, title, markdown, dry_run=dry_run)
        _notify_session_expired(cfg, report_date, execution_result, dry_run=dry_run)
        return {
            "outputs": outputs,
            "selected_score": selected_items[0]["score"] if selected_items else 0.0,
            "selected_topics": [item.topic for item in selected],
            "low_confidence": low_confidence,
            "executor_status": execution_result.get("status"),
            "send_open_id": cfg.send_open_id,
        }
    finally:
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
            lock_handle.close()
        except Exception:
            pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate and send Xiaohongshu AI blogger daily operation report.")
    parser.add_argument("--date", help="Report date in YYYY-MM-DD format.")
    parser.add_argument("--dry-run", action="store_true", help="Generate outputs only, do not send Feishu.")
    parser.add_argument("--mock-signals-file", help="Use local phase A JSON instead of online search.")
    parser.add_argument("--mock-draft-file", help="Use local phase B JSON instead of drafting with Codex.")
    args = parser.parse_args()

    report_date = _slug_date(args.date)
    mock_signals_file = Path(args.mock_signals_file) if args.mock_signals_file else None
    mock_draft_file = Path(args.mock_draft_file) if args.mock_draft_file else None

    try:
        result = run(
            report_date=report_date,
            dry_run=args.dry_run,
            mock_signals_file=mock_signals_file,
            mock_draft_file=mock_draft_file,
        )
    except Exception as exc:
        print(f"[xhs-ai-blogger] {type(exc).__name__}: {exc}", file=sys.stderr)
        raise

    print(f"markdown_file={result['outputs']['markdown']}")
    print(f"json_file={result['outputs']['json']}")
    print(f"selected_score={result['selected_score']}")
    print(f"selected_topics={','.join(result['selected_topics'])}")
    print(f"low_confidence={result['low_confidence']}")
    print(f"executor_status={result['executor_status']}")
    print(f"send_open_id={result['send_open_id']}")


if __name__ == "__main__":
    main()
