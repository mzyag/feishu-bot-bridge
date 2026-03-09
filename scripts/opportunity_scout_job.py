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
from urllib.parse import urlparse

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
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "reports" / "opportunity-scout"
DEFAULT_JOB_LOCK_FILE = PROJECT_ROOT / ".state" / "opportunity_scout_job.lock"
LOW_CONFIDENCE_LINE = "Confidence: Low — recent signals are weak today."
DEFAULT_NOVELTY_LOOKBACK_DAYS = 3
DEFAULT_NOVELTY_MAX_PENALTY = 0.7
TOKEN_STOPWORDS = {
    "there",
    "their",
    "about",
    "after",
    "before",
    "while",
    "with",
    "from",
    "this",
    "that",
    "into",
    "been",
    "were",
    "have",
    "has",
    "had",
    "your",
    "they",
    "them",
    "will",
    "would",
    "could",
    "should",
    "what",
    "when",
    "where",
    "which",
    "than",
    "then",
    "very",
    "more",
    "most",
    "just",
    "into",
    "only",
}
TOPIC_CLUSTER_KEYWORDS = {
    "billing_pricing": [
        "billing",
        "price",
        "pricing",
        "charge",
        "charged",
        "invoice",
        "checkout",
        "refund",
        "subscription",
        "cancel",
        "cancellation",
        "plan",
    ],
    "content_seo": [
        "seo",
        "keyword",
        "blog",
        "article",
        "content",
        "newsletter",
        "ugc",
        "social",
        "video",
    ],
    "agency_ops": [
        "agency",
        "client",
        "proposal",
        "reporting",
        "manual",
        "workflow",
        "handoff",
        "campaign",
    ],
    "crm_sales": [
        "lead",
        "pipeline",
        "outreach",
        "sales",
        "crm",
        "prospect",
    ],
}


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
    target_market: str
    report_language: str
    fallback_policy: str
    output_dir: Path

    @staticmethod
    def from_env() -> "Config":
        load_dotenv(PROJECT_ROOT / ".env")
        allowed = [item.strip() for item in os.getenv("ALLOWED_USER_IDS", "").split(",") if item.strip()]
        daily_send_open_id = os.getenv("DAILY_REPORT_SEND_OPEN_ID", "").strip()
        scout_send_open_id = os.getenv("SCOUT_SEND_OPEN_ID", "").strip()
        output_dir_raw = os.getenv("SCOUT_OUTPUT_DIR", str(DEFAULT_OUTPUT_DIR)).strip()
        timeout_raw = os.getenv("SCOUT_CODEX_TIMEOUT_SEC", os.getenv("CODEX_TIMEOUT_SEC", "900")).strip()
        try:
            timeout_sec = int(timeout_raw)
        except ValueError:
            timeout_sec = 900
        return Config(
            app_id=os.getenv("FEISHU_APP_ID", "").strip(),
            app_secret=os.getenv("FEISHU_APP_SECRET", "").strip(),
            allowed_user_ids=allowed,
            send_open_id=scout_send_open_id or daily_send_open_id or (allowed[0] if allowed else ""),
            codex_cmd=os.getenv("CODEX_CLI_CMD", "codex").strip() or "codex",
            codex_workdir=os.getenv("CODEX_WORKDIR", "/Users/cn/Workspace").strip() or "/Users/cn/Workspace",
            codex_model=os.getenv("SCOUT_CODEX_MODEL", os.getenv("CODEX_MODEL", "")).strip(),
            codex_timeout_sec=timeout_sec,
            target_market=os.getenv("SCOUT_TARGET_MARKET", "global_en").strip(),
            report_language=os.getenv("SCOUT_REPORT_LANGUAGE", "zh-CN").strip(),
            fallback_policy=os.getenv("SCOUT_FALLBACK_POLICY", "send_low_confidence").strip(),
            output_dir=Path(output_dir_raw),
        )


@dataclass
class Candidate:
    idea_stub: str
    pain_point: str
    user_quote: str
    quote_url: str
    quote_timestamp_text: str
    gap: str
    trend_source: str
    trend_url: str
    solo_build_fit: int
    distribution_leverage: int
    boring_b2b_score: int
    difficulty: str
    freshness: str
    confidence_notes: str


def _http_client(timeout_seconds: int = 180) -> httpx.Client:
    if httpx is None:
        raise RuntimeError("httpx is required for Feishu API calls.")
    return httpx.Client(timeout=timeout_seconds)


def _slug_date(forced_date: Optional[str]) -> str:
    if forced_date:
        return forced_date
    return dt.datetime.now().date().isoformat()


def _job_lock_file_path() -> Path:
    raw = os.getenv("SCOUT_JOB_LOCK_FILE", str(DEFAULT_JOB_LOCK_FILE)).strip()
    return Path(raw) if raw else DEFAULT_JOB_LOCK_FILE


def _acquire_job_lock():
    lock_path = _job_lock_file_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("w", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        raise RuntimeError("Opportunity scout job already running.")
    return handle


def _normalize_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _normalize_url(value: Any) -> str:
    text = str(value or "").strip()
    return text if text.startswith("http://") or text.startswith("https://") else ""


def _url_domain(url: str) -> str:
    if not url:
        return ""
    try:
        host = (urlparse(url).netloc or "").lower().strip()
    except Exception:
        return ""
    if host.startswith("www."):
        host = host[4:]
    return host


def _int_env(name: str, default_value: int, min_value: int, max_value: int) -> int:
    raw = os.getenv(name, str(default_value)).strip()
    try:
        value = int(raw)
    except ValueError:
        return default_value
    return max(min_value, min(max_value, value))


def _float_env(name: str, default_value: float, min_value: float, max_value: float) -> float:
    raw = os.getenv(name, str(default_value)).strip()
    try:
        value = float(raw)
    except ValueError:
        return default_value
    return max(min_value, min(max_value, value))


def _text_tokens(text: str) -> set:
    raw_tokens = re.findall(r"[a-z0-9]{3,}", text.lower())
    return {token for token in raw_tokens if token not in TOKEN_STOPWORDS}


def _topic_clusters(text: str) -> set:
    lowered = text.lower()
    clusters = set()
    for cluster_name, keywords in TOPIC_CLUSTER_KEYWORDS.items():
        if any(keyword in lowered for keyword in keywords):
            clusters.add(cluster_name)
    return clusters


def _load_recent_context(cfg: Config, report_date: str) -> List[Dict[str, Any]]:
    lookback_days = _int_env(
        "SCOUT_NOVELTY_LOOKBACK_DAYS",
        default_value=DEFAULT_NOVELTY_LOOKBACK_DAYS,
        min_value=0,
        max_value=14,
    )
    if lookback_days <= 0:
        return []
    try:
        current_day = dt.date.fromisoformat(report_date)
    except ValueError:
        return []

    recent_items: List[Dict[str, Any]] = []
    if not cfg.output_dir.exists():
        return recent_items

    for path in sorted(cfg.output_dir.glob("*.json"), reverse=True):
        date_key = path.stem
        try:
            day = dt.date.fromisoformat(date_key)
        except ValueError:
            continue
        delta_days = (current_day - day).days
        if delta_days <= 0 or delta_days > lookback_days:
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        ranked = payload.get("ranked_candidates")
        if not isinstance(ranked, list) or not ranked:
            continue
        top = ranked[0]
        candidate = top.get("candidate") if isinstance(top, dict) else None
        if not isinstance(candidate, dict):
            continue
        idea_stub = _normalize_text(candidate.get("idea_stub"))
        pain_point = _normalize_text(candidate.get("pain_point"))
        gap = _normalize_text(candidate.get("gap"))
        quote_url = _normalize_url(candidate.get("quote_url"))
        trend_url = _normalize_url(candidate.get("trend_url"))
        signature_text = f"{idea_stub} {pain_point} {gap}".strip()
        if not signature_text:
            continue
        recent_items.append(
            {
                "report_date": date_key,
                "idea_stub": idea_stub,
                "quote_domain": _url_domain(quote_url),
                "trend_domain": _url_domain(trend_url),
                "theme_tokens": sorted(_text_tokens(signature_text)),
                "clusters": sorted(_topic_clusters(signature_text)),
            }
        )
    return recent_items


def _recent_context_brief(recent_items: List[Dict[str, Any]], limit: int = 3) -> str:
    lines: List[str] = []
    for item in recent_items[:limit]:
        quote_domain = item.get("quote_domain") or "-"
        trend_domain = item.get("trend_domain") or "-"
        lines.append(
            f"- {item.get('report_date')}: {item.get('idea_stub', '')} "
            f"(quote_domain={quote_domain}, trend_domain={trend_domain})"
        )
    return "\n".join(lines)


def _coerce_score(value: Any) -> int:
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, int):
        return max(1, min(5, value))
    match = re.search(r"([1-5])", str(value or ""))
    if match:
        return int(match.group(1))
    return 3


def _normalize_difficulty(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in {"low", "medium", "high"}:
        return text.title()
    return "Medium"


def _normalize_freshness(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in {"strong", "weak"}:
        return text
    if "24" in text or "48" in text or "recent" in text:
        return "strong"
    return "weak"


def _pain_severity_score(candidate: Candidate) -> float:
    text = f"{candidate.pain_point} {candidate.user_quote}".lower()
    severe_keywords = [
        "hate",
        "pain",
        "frustrat",
        "manual",
        "waste",
        "hours",
        "impossible",
        "too expensive",
        "annoying",
        "broken",
        "slow",
        "missing",
        "why is there no",
    ]
    hits = sum(1 for keyword in severe_keywords if keyword in text)
    score = 2.0 + min(3.0, hits * 0.45)
    if len(candidate.user_quote) > 120:
        score += 0.2
    return min(5.0, score)


def _freshness_score(candidate: Candidate) -> float:
    return 5.0 if candidate.freshness == "strong" else 2.0


def _difficulty_modifier(candidate: Candidate) -> float:
    mapping = {"Low": 1.0, "Medium": 0.7, "High": 0.3}
    return mapping.get(candidate.difficulty, 0.7)


def _rank_candidate(candidate: Candidate) -> float:
    score = 0.0
    score += _pain_severity_score(candidate) * 0.30
    score += candidate.solo_build_fit * 0.20
    score += candidate.distribution_leverage * 0.20
    score += candidate.boring_b2b_score * 0.15
    score += _freshness_score(candidate) * 0.15
    score += _difficulty_modifier(candidate) * 0.05
    return round(score, 4)


def _novelty_penalty(candidate: Candidate, recent_items: List[Dict[str, Any]]) -> float:
    if not recent_items:
        return 0.0

    quote_domain = _url_domain(candidate.quote_url)
    trend_domain = _url_domain(candidate.trend_url)
    signature_text = f"{candidate.idea_stub} {candidate.pain_point} {candidate.gap}".strip()
    candidate_tokens = _text_tokens(signature_text)
    candidate_clusters = _topic_clusters(signature_text)

    penalty = 0.0
    for item in recent_items:
        local_penalty = 0.0
        if quote_domain and quote_domain == item.get("quote_domain"):
            local_penalty += 0.18
        if trend_domain and trend_domain == item.get("trend_domain"):
            local_penalty += 0.10

        previous_tokens = set(item.get("theme_tokens") or [])
        if candidate_tokens and previous_tokens:
            overlap_ratio = len(candidate_tokens & previous_tokens) / len(candidate_tokens | previous_tokens)
            if overlap_ratio >= 0.45:
                local_penalty += 0.30
            elif overlap_ratio >= 0.30:
                local_penalty += 0.18
            elif overlap_ratio >= 0.20:
                local_penalty += 0.10

        previous_clusters = set(item.get("clusters") or [])
        if candidate_clusters and previous_clusters:
            shared_clusters = candidate_clusters & previous_clusters
            if shared_clusters:
                local_penalty += min(0.16, 0.08 * len(shared_clusters))

        penalty = max(penalty, local_penalty)

    max_penalty = _float_env(
        "SCOUT_NOVELTY_MAX_PENALTY",
        default_value=DEFAULT_NOVELTY_MAX_PENALTY,
        min_value=0.0,
        max_value=2.0,
    )
    return round(min(max_penalty, penalty), 4)


def _validate_candidate(candidate: Candidate) -> None:
    required_texts = [
        candidate.idea_stub,
        candidate.pain_point,
        candidate.user_quote,
        candidate.gap,
        candidate.trend_source,
        candidate.confidence_notes,
    ]
    if not all(required_texts):
        raise ValueError("Phase A candidate missing required text fields.")
    if not candidate.quote_url or not candidate.trend_url:
        raise ValueError("Phase A candidate missing quote_url or trend_url.")


def _parse_candidates(payload: Dict[str, Any]) -> List[Candidate]:
    raw_candidates = payload.get("candidates")
    if not isinstance(raw_candidates, list) or not raw_candidates:
        raise ValueError("Phase A returned no candidates.")
    parsed: List[Candidate] = []
    for item in raw_candidates:
        if not isinstance(item, dict):
            continue
        candidate = Candidate(
            idea_stub=_normalize_text(item.get("idea_stub")),
            pain_point=_normalize_text(item.get("pain_point")),
            user_quote=_normalize_text(item.get("user_quote")),
            quote_url=_normalize_url(item.get("quote_url")),
            quote_timestamp_text=_normalize_text(item.get("quote_timestamp_text")),
            gap=_normalize_text(item.get("gap")),
            trend_source=_normalize_text(item.get("trend_source")),
            trend_url=_normalize_url(item.get("trend_url")),
            solo_build_fit=_coerce_score(item.get("solo_build_fit")),
            distribution_leverage=_coerce_score(item.get("distribution_leverage")),
            boring_b2b_score=_coerce_score(item.get("boring_b2b_score")),
            difficulty=_normalize_difficulty(item.get("difficulty")),
            freshness=_normalize_freshness(item.get("freshness")),
            confidence_notes=_normalize_text(item.get("confidence_notes")),
        )
        _validate_candidate(candidate)
        parsed.append(candidate)
    if not parsed:
        raise ValueError("Phase A candidates could not be normalized.")
    return parsed


def _sources_from_candidates(payload: Dict[str, Any]) -> List[Dict[str, str]]:
    sources: List[Dict[str, str]] = []
    seen = set()
    for item in payload.get("candidates") or []:
        if not isinstance(item, dict):
            continue
        pairs = [
            (_normalize_text(item.get("idea_stub")) or "complaint", _normalize_url(item.get("quote_url"))),
            (_normalize_text(item.get("trend_source")) or "trend", _normalize_url(item.get("trend_url"))),
        ]
        for title, url in pairs:
            if not url or url in seen:
                continue
            seen.add(url)
            sources.append({"title": title, "url": url})
    return sources


def _run_codex_json(cfg: Config, prompt: str, schema: Dict[str, Any], phase_name: str) -> Dict[str, Any]:
    codex_bin = shutil.which(cfg.codex_cmd)
    if not codex_bin:
        raise RuntimeError(f"Codex command not found: {cfg.codex_cmd}")

    timeout_value = None if cfg.codex_timeout_sec <= 0 else cfg.codex_timeout_sec
    with tempfile.TemporaryDirectory(prefix=f"scout-{phase_name}-") as tmp_dir:
        schema_path = Path(tmp_dir) / f"{phase_name}-schema.json"
        output_path = Path(tmp_dir) / f"{phase_name}-output.json"
        schema_path.write_text(json.dumps(schema, ensure_ascii=False, indent=2), encoding="utf-8")

        cmd = [
            codex_bin,
            "--search",
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
            err = (result.stderr or result.stdout or "").strip()
            raise RuntimeError(f"Codex {phase_name} failed: {err[:800]}")
        if not output_path.exists():
            raise RuntimeError(f"Codex {phase_name} did not produce output schema file.")

        raw = output_path.read_text(encoding="utf-8").strip()
        if not raw:
            raise RuntimeError(f"Codex {phase_name} returned empty output.")
        parsed = json.loads(raw)
        return {
            "parsed": parsed,
            "response": {
                "runner": "codex_cli",
                "phase": phase_name,
                "returncode": result.returncode,
                "stdout_tail": (result.stdout or "")[-1200:],
                "stderr_tail": (result.stderr or "")[-1200:],
            },
        }


def _phase_a_schema() -> Dict[str, Any]:
    candidate_schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "idea_stub": {"type": "string"},
            "pain_point": {"type": "string"},
            "user_quote": {"type": "string"},
            "quote_url": {"type": "string"},
            "quote_timestamp_text": {"type": "string"},
            "gap": {"type": "string"},
            "trend_source": {"type": "string"},
            "trend_url": {"type": "string"},
            "solo_build_fit": {"type": "integer", "minimum": 1, "maximum": 5},
            "distribution_leverage": {"type": "integer", "minimum": 1, "maximum": 5},
            "boring_b2b_score": {"type": "integer", "minimum": 1, "maximum": 5},
            "difficulty": {"type": "string"},
            "freshness": {"type": "string"},
            "confidence_notes": {"type": "string"},
        },
        "required": [
            "idea_stub",
            "pain_point",
            "user_quote",
            "quote_url",
            "quote_timestamp_text",
            "gap",
            "trend_source",
            "trend_url",
            "solo_build_fit",
            "distribution_leverage",
            "boring_b2b_score",
            "difficulty",
            "freshness",
            "confidence_notes",
        ],
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "search_window": {"type": "string"},
            "candidates": {
                "type": "array",
                "items": candidate_schema,
                "minItems": 1,
                "maxItems": 10,
            },
        },
        "required": ["search_window", "candidates"],
    }


def _phase_b_schema() -> Dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "idea_name": {"type": "string"},
            "high_concept": {"type": "string"},
            "market_gap_problem": {"type": "string"},
            "market_gap_gap": {"type": "string"},
            "market_gap_founder_fit": {"type": "string"},
            "validation_signal": {"type": "string"},
            "validation_trend": {"type": "string"},
            "validation_difficulty": {"type": "string"},
            "confidence_level": {"type": "string"},
            "confidence_reason": {"type": "string"},
            "first_1000_step_1": {"type": "string"},
            "first_1000_step_2": {"type": "string"},
            "first_1000_step_3": {"type": "string"},
            "ad_strategy_zero": {"type": "string"},
            "ad_strategy_100": {"type": "string"},
        },
        "required": [
            "idea_name",
            "high_concept",
            "market_gap_problem",
            "market_gap_gap",
            "market_gap_founder_fit",
            "validation_signal",
            "validation_trend",
            "validation_difficulty",
            "confidence_level",
            "confidence_reason",
            "first_1000_step_1",
            "first_1000_step_2",
            "first_1000_step_3",
            "ad_strategy_zero",
            "ad_strategy_100",
        ],
    }


def phase_a_hunt(cfg: Config, report_date: str, recent_items: List[Dict[str, Any]]) -> Dict[str, Any]:
    recent_context = _recent_context_brief(recent_items)
    novelty_block = (
        "Recent selected opportunities (avoid near-duplicates unless today's evidence is significantly stronger):\n"
        f"{recent_context}\n\n"
        "Novelty requirement:\n"
        "- Prefer a different root pain cluster and different source domains from the recent list.\n"
        "- If you must stay in a similar cluster, explain in confidence_notes why today's signal is materially new.\n\n"
        if recent_context
        else ""
    )
    prompt = (
        f"Today is {report_date}. You are Hunter, an autonomous business opportunity scout for Morning, "
        "a serial entrepreneur and software engineer running AfterWork Startup.\n\n"
        "Mission:\n"
        "- Browse the live web and find real software pain.\n"
        "- Only surface opportunities suited to micro-SaaS, AI wrappers, programmatic SEO, or high-end digital downloads.\n"
        "- Avoid hardware, regulated-heavy categories, high-capital marketplaces, and ideas needing a big sales team.\n"
        "- Focus on the global English-speaking market.\n\n"
        "Search strategy:\n"
        "1) Reddit pain sweep:\n"
        "   - site:reddit.com inurl:r/SaaS OR inurl:r/entrepreneur \"why is there no\"\n"
        "   - site:reddit.com \"I hate using\" AND \"software\"\n"
        "   - site:reddit.com/r/marketing \"manual process\"\n"
        "2) Competitor weakness sweep:\n"
        "   - site:trustpilot.com \"too expensive\" alternative\n"
        "   - \"marketing agencies\" \"spending too much time on\"\n"
        "3) Trend check:\n"
        "   - \"trending digital products 2026\"\n"
        "   - \"fastest growing SaaS categories February 2026\"\n\n"
        f"{novelty_block}"
        "Rules:\n"
        "- Prefer evidence from the last 24-48 hours.\n"
        "- If a supporting trend source needs to be older, allow up to 7 days and mark freshness as weak.\n"
        "- Return 6-10 candidates when possible.\n"
        "- Every candidate must include a real complaint quote or a very short faithful paraphrase, a complaint URL, and a trend URL.\n"
        "- Score solo_build_fit, distribution_leverage, and boring_b2b_score from 1 to 5.\n"
        "- Use difficulty values Low, Medium, or High.\n"
        "- Use freshness values strong or weak.\n"
    )
    response_json = _run_codex_json(cfg, prompt, _phase_a_schema(), "phase-a")
    parsed = response_json["parsed"]
    return {
        "parsed": parsed,
        "response": response_json,
        "sources": _sources_from_candidates(parsed),
    }


def phase_b_report(cfg: Config, report_date: str, candidate: Candidate, low_confidence: bool) -> Dict[str, Any]:
    confidence_target = "Low" if low_confidence else "Medium or High"
    prompt = (
        "You are Hunter, writing a concise investor-style daily opportunity memo in Chinese.\n"
        "Rules:\n"
        "- Use only the supplied candidate.\n"
        "- Do not invent any fact or source.\n"
        "- Keep English product names and URLs intact.\n"
        "- Describe exactly one opportunity.\n"
        "- Maintain founder fit for a software engineer building a boring-but-profitable software business.\n"
        "- The idea should be sellable via SEO, short-form content, or simple outbound-free channels.\n"
        f"- Confidence target: {confidence_target}.\n"
        "- If confidence is low, explain why in one sentence.\n\n"
        f"Report date: {report_date}\n"
        f"Report language: {cfg.report_language}\n"
        f"Target market: {cfg.target_market}\n"
        f"Selected candidate JSON:\n{json.dumps(asdict(candidate), ensure_ascii=False, indent=2)}\n"
    )
    response_json = _run_codex_json(cfg, prompt, _phase_b_schema(), "phase-b")
    parsed = response_json["parsed"]
    return {
        "parsed": parsed,
        "response": response_json,
    }


def _bullet(text: str) -> str:
    return _normalize_text(text)


def build_report_markdown(report_date: str, phase_b: Dict[str, Any], selected: Candidate, low_confidence: bool) -> str:
    lines: List[str] = []
    lines.append("🚀 Daily Opportunity Scout Report")
    lines.append("")
    if low_confidence:
        lines.append(LOW_CONFIDENCE_LINE)
        lines.append("")
    lines.append(f"Idea Name: {phase_b['idea_name']} — {phase_b['high_concept']}")
    lines.append("")
    lines.append("1. Market Gap (The 'Why Now')")
    lines.append(f"   • The Problem: {phase_b['market_gap_problem']} Quote: “{selected.user_quote}” ({selected.quote_url})")
    lines.append(f"   • The Gap: {phase_b['market_gap_gap']}")
    lines.append(f"   • Founder Fit: {phase_b['market_gap_founder_fit']}")
    lines.append("")
    lines.append("2. Validation (Data/Trends)")
    lines.append(f"   • Signal: {phase_b['validation_signal']} Source: {selected.quote_url}")
    lines.append(f"   • The Trend: {phase_b['validation_trend']} Source: {selected.trend_source} ({selected.trend_url})")
    lines.append(f"   • Difficulty: {_bullet(phase_b['validation_difficulty'])}")
    lines.append("")
    lines.append("3. First $1,000 Plan (Vibe-Coding / $0 Setup)")
    lines.append(f"   • Step 1 (The Tech): {phase_b['first_1000_step_1']}")
    lines.append(f"   • Step 2 (The Hook): {phase_b['first_1000_step_2']}")
    lines.append(f"   • Step 3 (Pricing): {phase_b['first_1000_step_3']}")
    lines.append("   • The $100 Ad Strategy:")
    lines.append(f"     • $0: {phase_b['ad_strategy_zero']}")
    lines.append(f"     • $100: {phase_b['ad_strategy_100']}")
    if low_confidence:
        lines.append("")
        lines.append(f"Confidence Reason: {phase_b['confidence_reason']}")
    return "\n".join(lines).strip() + "\n"


def _validate_report_markdown(markdown: str) -> None:
    urls = re.findall(r"https?://\S+", markdown)
    if len(urls) < 2:
        raise ValueError("Generated report must include at least two URLs.")
    if "Quote:" not in markdown:
        raise ValueError("Generated report must include a user quote.")


def _is_low_confidence(candidates: List[Candidate], selected: Candidate) -> bool:
    if len(candidates) < 6:
        return True
    if selected.freshness != "strong":
        return True
    if selected.solo_build_fit <= 2 or selected.distribution_leverage <= 2:
        return True
    return False


def _sorted_candidates(candidates: List[Candidate], recent_items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    ranked: List[Dict[str, Any]] = []
    for candidate in candidates:
        base_score = _rank_candidate(candidate)
        novelty_penalty = _novelty_penalty(candidate, recent_items)
        final_score = round(base_score - novelty_penalty, 4)
        ranked.append(
            {
                "candidate": candidate,
                "base_score": base_score,
                "novelty_penalty": novelty_penalty,
                "score": final_score,
            }
        )
    ranked.sort(key=lambda item: (item["score"], item["base_score"]), reverse=True)
    return ranked


def _pick_best_candidate(ranked_candidates: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not ranked_candidates:
        raise ValueError("No ranked candidates to select.")
    top = ranked_candidates[0]
    top_penalty = float(top.get("novelty_penalty") or 0.0)
    if top_penalty <= 0.35:
        return top
    top_score = float(top.get("score") or 0.0)
    for item in ranked_candidates[1:]:
        candidate_penalty = float(item.get("novelty_penalty") or 0.0)
        candidate_score = float(item.get("score") or 0.0)
        if candidate_penalty + 0.12 < top_penalty and (top_score - candidate_score) <= 0.25:
            return item
    return top


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
        raise RuntimeError("Missing SCOUT_SEND_OPEN_ID / DAILY_REPORT_SEND_OPEN_ID / ALLOWED_USER_IDS.")
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


def write_outputs(
    cfg: Config,
    report_date: str,
    markdown: str,
    phase_a: Dict[str, Any],
    phase_b: Dict[str, Any],
    ranked_candidates: List[Dict[str, Any]],
    recent_items: List[Dict[str, Any]],
    low_confidence: bool,
) -> Dict[str, Path]:
    cfg.output_dir.mkdir(parents=True, exist_ok=True)
    markdown_path = cfg.output_dir / f"{report_date}.md"
    json_path = cfg.output_dir / f"{report_date}.json"
    markdown_path.write_text(markdown, encoding="utf-8")
    json_payload = {
        "report_date": report_date,
        "low_confidence": low_confidence,
        "config": {
            "target_market": cfg.target_market,
            "report_language": cfg.report_language,
            "fallback_policy": cfg.fallback_policy,
            "codex_cmd": cfg.codex_cmd,
            "codex_model": cfg.codex_model,
            "codex_timeout_sec": cfg.codex_timeout_sec,
            "novelty_lookback_days": _int_env(
                "SCOUT_NOVELTY_LOOKBACK_DAYS",
                default_value=DEFAULT_NOVELTY_LOOKBACK_DAYS,
                min_value=0,
                max_value=14,
            ),
            "novelty_max_penalty": _float_env(
                "SCOUT_NOVELTY_MAX_PENALTY",
                default_value=DEFAULT_NOVELTY_MAX_PENALTY,
                min_value=0.0,
                max_value=2.0,
            ),
        },
        "recent_context": recent_items,
        "phase_a": {
            "parsed": phase_a["parsed"],
            "sources": phase_a["sources"],
            "runner": phase_a["response"].get("runner"),
            "stdout_tail": phase_a["response"].get("stdout_tail"),
            "stderr_tail": phase_a["response"].get("stderr_tail"),
        },
        "ranked_candidates": [
            {
                "score": item["score"],
                "base_score": item.get("base_score"),
                "novelty_penalty": item.get("novelty_penalty"),
                "candidate": asdict(item["candidate"]),
            }
            for item in ranked_candidates
        ],
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


def _load_mock_hunt_file(path: Path) -> Dict[str, Any]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {"parsed": raw, "response": {"id": "mock-phase-a"}, "sources": []}


def _load_mock_report_file(path: Path) -> Dict[str, Any]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {"parsed": raw, "response": {"id": "mock-phase-b"}}


def run(
    report_date: str,
    dry_run: bool,
    mock_hunt_file: Optional[Path],
    mock_report_file: Optional[Path],
) -> Dict[str, Any]:
    lock_handle = _acquire_job_lock()
    try:
        cfg = Config.from_env()
        if not shutil.which(cfg.codex_cmd) and not (mock_hunt_file and mock_report_file):
            raise RuntimeError(f"Codex command not found: {cfg.codex_cmd}")
        recent_items = _load_recent_context(cfg, report_date)
        phase_a = (
            _load_mock_hunt_file(mock_hunt_file)
            if mock_hunt_file
            else phase_a_hunt(cfg, report_date, recent_items)
        )
        candidates = _parse_candidates(phase_a["parsed"])
        ranked_candidates = _sorted_candidates(candidates, recent_items)
        selected_item = _pick_best_candidate(ranked_candidates)
        selected = selected_item["candidate"]
        selected_novelty_penalty = float(selected_item.get("novelty_penalty") or 0.0)
        low_confidence = _is_low_confidence(candidates, selected) or selected_novelty_penalty >= 0.45
        if cfg.fallback_policy != "send_low_confidence" and low_confidence:
            raise RuntimeError(f"Unsupported fallback policy: {cfg.fallback_policy}")
        phase_b = (
            _load_mock_report_file(mock_report_file)
            if mock_report_file
            else phase_b_report(cfg, report_date, selected, low_confidence)
        )
        markdown = build_report_markdown(report_date, phase_b["parsed"], selected, low_confidence)
        _validate_report_markdown(markdown)
        outputs = write_outputs(
            cfg,
            report_date,
            markdown,
            phase_a,
            phase_b,
            ranked_candidates,
            recent_items,
            low_confidence,
        )
        title = f"【Low Confidence】{report_date}" if low_confidence else report_date
        send_to_feishu(cfg, title, markdown, dry_run=dry_run)
        return {
            "outputs": outputs,
            "selected_score": selected_item["score"],
            "low_confidence": low_confidence,
            "send_open_id": cfg.send_open_id,
        }
    finally:
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
            lock_handle.close()
        except Exception:
            pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate and send the daily Hunter opportunity scout report.")
    parser.add_argument("--date", help="Report date in YYYY-MM-DD format.")
    parser.add_argument("--dry-run", action="store_true", help="Generate outputs only, do not send Feishu.")
    parser.add_argument("--mock-hunt-file", help="Use local phase A JSON instead of calling OpenAI.")
    parser.add_argument("--mock-report-file", help="Use local phase B JSON instead of calling OpenAI.")
    args = parser.parse_args()

    report_date = _slug_date(args.date)
    mock_hunt_file = Path(args.mock_hunt_file) if args.mock_hunt_file else None
    mock_report_file = Path(args.mock_report_file) if args.mock_report_file else None

    try:
        result = run(
            report_date,
            dry_run=args.dry_run,
            mock_hunt_file=mock_hunt_file,
            mock_report_file=mock_report_file,
        )
    except Exception as exc:
        print(f"[opportunity-scout] {type(exc).__name__}: {exc}", file=sys.stderr)
        raise

    print(f"markdown_file={result['outputs']['markdown']}")
    print(f"json_file={result['outputs']['json']}")
    print(f"selected_score={result['selected_score']}")
    print(f"low_confidence={result['low_confidence']}")
    print(f"send_open_id={result['send_open_id']}")


if __name__ == "__main__":
    main()
