#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "reports" / "boss-screening"
DEFAULT_RESUME_PATH = Path("/Users/cn/Downloads/简历 - Your Name - AI Agent.pdf")
DEFAULT_CITIES = ["上海", "杭州"]
DEFAULT_KEYWORDS = ["增长产品经理", "产品经理", "AI产品经理", "平台产品经理"]
DEFAULT_SALARY_RANGE = "25-50K"
DEFAULT_EXPERIENCE_RANGE = "3-10年"
DEFAULT_MAX_PAGES = 3
DEFAULT_TOP_K = 30

FIELD_NAMES = [
    "title",
    "company",
    "city",
    "salary",
    "experience",
    "education",
    "tags",
    "detail_url",
]


@dataclass
class CollectionFailure:
    city: str
    keyword: str
    step: str
    reason: str
    screenshot_path: str = ""


def _normalize_csv_list(raw: str) -> list[str]:
    return [part.strip() for part in (raw or "").split(",") if part.strip()]


def _candidate_recipe_paths() -> list[Path]:
    env_recipe = os.getenv("BOSS_OPERATOR_RECIPE_PATH", "").strip()
    candidates: list[Path] = []
    if env_recipe:
        candidates.append(Path(env_recipe).expanduser())
    candidates.extend(
        [
            Path("/Users/cn/Workspace/.codex/skills/boss-operator/assets/recipes/boss-web-job-hunt.json"),
            Path.home() / ".codex" / "skills" / "boss-operator" / "assets" / "recipes" / "boss-web-job-hunt.json",
        ]
    )
    return candidates


def _candidate_resume_keyword_extractors() -> list[Path]:
    return [
        Path("/Users/cn/Workspace/.codex/skills/boss-android-resume-job-list/scripts/extract_resume_keywords.py"),
        Path.home() / ".codex" / "skills" / "boss-android-resume-job-list" / "scripts" / "extract_resume_keywords.py",
    ]


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected JSON object, got {type(payload)} from {path}")
    return payload


def _resolve_recipe() -> dict[str, Any]:
    for recipe_path in _candidate_recipe_paths():
        if recipe_path.is_file():
            recipe = _read_json(recipe_path)
            recipe["__path__"] = str(recipe_path)
            return recipe
    raise FileNotFoundError("cannot find boss-operator recipe file")


def _extract_keywords_from_resume(resume_path: Path) -> list[str]:
    if not resume_path.is_file():
        return DEFAULT_KEYWORDS

    for extractor in _candidate_resume_keyword_extractors():
        if not extractor.is_file():
            continue
        with tempfile.TemporaryDirectory(prefix="boss_resume_kw_") as tmpdir:
            out_json = Path(tmpdir) / "keywords.json"
            proc = subprocess.run(
                [
                    sys.executable,
                    str(extractor),
                    "--resume",
                    str(resume_path),
                    "--output",
                    str(out_json),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            if proc.returncode != 0 or not out_json.is_file():
                continue
            parsed = _read_json(out_json)
            queries = parsed.get("queries")
            if isinstance(queries, list):
                cleaned = [str(item).strip() for item in queries if str(item).strip()]
                if cleaned:
                    return cleaned
    return DEFAULT_KEYWORDS


def _parse_salary_range(raw: str) -> tuple[float | None, float | None]:
    value = (raw or "").upper()
    match = re.search(r"(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)\s*K", value)
    if match:
        return float(match.group(1)), float(match.group(2))
    single = re.search(r"(\d+(?:\.\d+)?)\s*K", value)
    if single:
        point = float(single.group(1))
        return point, point
    return None, None


def _parse_experience_range(raw: str) -> tuple[float | None, float | None]:
    value = (raw or "").replace(" ", "")
    match = re.search(r"(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)\s*年", value)
    if match:
        return float(match.group(1)), float(match.group(2))
    single = re.search(r"(\d+(?:\.\d+)?)\s*年", value)
    if single:
        point = float(single.group(1))
        return point, point
    return None, None


def _overlap_score(
    target_min: float | None, target_max: float | None, actual_min: float | None, actual_max: float | None
) -> float:
    if actual_min is None or actual_max is None:
        return 0.4
    if target_min is None or target_max is None:
        return 0.5
    if actual_max < target_min:
        gap = target_min - actual_max
        return max(0.0, 1.0 - gap / max(target_min, 1.0))
    if actual_min > target_max:
        gap = actual_min - target_max
        return max(0.0, 1.0 - gap / max(target_max, 1.0))
    overlap = min(actual_max, target_max) - max(actual_min, target_min)
    span = max(target_max - target_min, 1.0)
    return max(0.0, min(1.0, 0.7 + overlap / span))


def _job_text(job: dict[str, Any]) -> str:
    tags = job.get("tags", [])
    tags_text = " ".join(tags) if isinstance(tags, list) else str(tags)
    return " ".join(
        [
            str(job.get("title", "")),
            str(job.get("company", "")),
            str(job.get("city", "")),
            str(job.get("salary", "")),
            str(job.get("experience", "")),
            str(job.get("education", "")),
            tags_text,
        ]
    ).lower()


def _keyword_score(job: dict[str, Any], keywords: list[str]) -> float:
    text = _job_text(job)
    if not keywords:
        return 0.5
    hit = sum(1 for keyword in keywords if keyword.lower() in text)
    return min(1.0, hit / max(len(keywords), 1))


def _ai_growth_bonus_score(job: dict[str, Any]) -> float:
    text = _job_text(job)
    marker_words = [
        "ai",
        "aigc",
        "llm",
        "增长",
        "growth",
        "智能",
        "大模型",
        "推荐",
        "平台产品",
    ]
    hit = sum(1 for marker in marker_words if marker in text)
    return min(1.0, hit / 4.0)


def _clean_tags(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        raw = value.replace("，", ",").replace("、", ",")
        return [part.strip() for part in raw.split(",") if part.strip()]
    return []


def _normalize_job(item: dict[str, Any], city: str, keyword: str) -> dict[str, Any]:
    job: dict[str, Any] = {}
    for field in FIELD_NAMES:
        if field == "tags":
            job[field] = _clean_tags(item.get(field))
        else:
            job[field] = str(item.get(field, "")).strip()
    if not job["city"]:
        job["city"] = city
    job["source_city"] = city
    job["source_keyword"] = keyword
    job["collected_at"] = datetime.now().isoformat(timespec="seconds")
    return job


def _parse_manual_line(line: str, city: str, keyword: str) -> dict[str, Any]:
    raw = line.strip()
    if not raw:
        raise ValueError("empty line")
    if raw.startswith("{") and raw.endswith("}"):
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ValueError("JSON line must be object")
        return _normalize_job(parsed, city=city, keyword=keyword)

    parts = [part.strip() for part in raw.split("|")]
    if len(parts) != len(FIELD_NAMES):
        raise ValueError(
            "manual line format error. expected 8 fields: "
            "title|company|city|salary|experience|education|tags(csv)|detail_url"
        )
    payload = dict(zip(FIELD_NAMES, parts))
    return _normalize_job(payload, city=city, keyword=keyword)


def _collect_jobs_interactive(
    cities: list[str], keywords: list[str], max_pages: int, non_interactive: bool
) -> tuple[list[dict[str, Any]], list[CollectionFailure]]:
    jobs: list[dict[str, Any]] = []
    failures: list[CollectionFailure] = []

    for city in cities:
        for keyword in keywords:
            print("\n" + "=" * 80)
            print(f"[checkpoint] 城市={city} 关键词={keyword}")
            print("请在 BOSS Mac App 手动执行：")
            print(f"1) 搜索关键词并切换城市；2) 浏览前 {max_pages} 页；3) 仅筛岗位卡片，不投递不打招呼。")
            if non_interactive:
                failures.append(
                    CollectionFailure(
                        city=city,
                        keyword=keyword,
                        step="manual_collect",
                        reason="non_interactive_skip",
                    )
                )
                continue

            action = input("输入状态 [ok/not_found/skip]: ").strip().lower()
            if action in {"not_found", "nf"}:
                step = input("失败步骤（如: 搜索框/筛选面板/岗位列表）: ").strip() or "unknown_step"
                reason = input("失败原因: ").strip() or "not_found"
                screenshot_path = input("截图路径(可空): ").strip()
                failures.append(
                    CollectionFailure(
                        city=city,
                        keyword=keyword,
                        step=step,
                        reason=reason,
                        screenshot_path=screenshot_path,
                    )
                )
                print("[fail-fast] 当前动作失败，跳到下一个关键词，不重试。")
                continue
            if action in {"skip", "s"}:
                failures.append(
                    CollectionFailure(
                        city=city,
                        keyword=keyword,
                        step="manual_collect",
                        reason="user_skip",
                    )
                )
                continue

            print(
                "请粘贴岗位数据（每行一条，支持 JSON 行 或 "
                "title|company|city|salary|experience|education|tags|detail_url），"
                "输入 END 结束："
            )
            while True:
                line = input()
                if line.strip().upper() == "END":
                    break
                if not line.strip():
                    continue
                try:
                    jobs.append(_parse_manual_line(line, city=city, keyword=keyword))
                except Exception as exc:
                    print(f"[warn] 跳过坏数据: {exc}")

    return jobs, failures


def _parse_jobs_from_file(path: Path) -> list[dict[str, Any]]:
    suffix = path.suffix.lower()
    jobs: list[dict[str, Any]] = []

    if suffix == ".json":
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            candidate = payload.get("jobs", [])
        else:
            candidate = payload
        if not isinstance(candidate, list):
            raise ValueError("json root must be list or {jobs:[...]}")
        for item in candidate:
            if isinstance(item, dict):
                jobs.append(_normalize_job(item, city=str(item.get("city", "")), keyword=str(item.get("source_keyword", ""))))
        return jobs

    if suffix == ".jsonl":
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            item = json.loads(line)
            if isinstance(item, dict):
                jobs.append(_normalize_job(item, city=str(item.get("city", "")), keyword=str(item.get("source_keyword", ""))))
        return jobs

    if suffix in {".csv", ".tsv"}:
        delimiter = "\t" if suffix == ".tsv" else ","
        with path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle, delimiter=delimiter)
            for row in reader:
                jobs.append(_normalize_job(row, city=str(row.get("city", "")), keyword=str(row.get("source_keyword", ""))))
        return jobs

    raise ValueError(f"unsupported raw input format: {path}")


def _dedupe_jobs(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    for item in jobs:
        key = "|".join(
            [
                str(item.get("title", "")).strip().lower(),
                str(item.get("company", "")).strip().lower(),
                str(item.get("city", "")).strip().lower(),
                str(item.get("salary", "")).strip().lower(),
            ]
        )
        if not key:
            continue
        if key not in seen:
            seen[key] = item
    return list(seen.values())


def _score_jobs(
    jobs: list[dict[str, Any]], keywords: list[str], salary_target: str, exp_target: str
) -> list[dict[str, Any]]:
    salary_min, salary_max = _parse_salary_range(salary_target)
    exp_min, exp_max = _parse_experience_range(exp_target)

    scored: list[dict[str, Any]] = []
    for job in jobs:
        job_salary_min, job_salary_max = _parse_salary_range(str(job.get("salary", "")))
        job_exp_min, job_exp_max = _parse_experience_range(str(job.get("experience", "")))
        keyword = _keyword_score(job, keywords)
        salary = _overlap_score(salary_min, salary_max, job_salary_min, job_salary_max)
        experience = _overlap_score(exp_min, exp_max, job_exp_min, job_exp_max)
        bonus = _ai_growth_bonus_score(job)
        total = keyword * 0.40 + salary * 0.25 + experience * 0.25 + bonus * 0.10
        record = dict(job)
        record["score_detail"] = {
            "keyword_40": round(keyword * 40, 2),
            "salary_25": round(salary * 25, 2),
            "experience_25": round(experience * 25, 2),
            "ai_growth_10": round(bonus * 10, 2),
        }
        record["selected_score"] = round(total * 100, 2)
        scored.append(record)

    scored.sort(key=lambda x: float(x.get("selected_score", 0.0)), reverse=True)
    return scored


def _render_plan_markdown(
    recipe: dict[str, Any],
    report_date: str,
    cities: list[str],
    keywords: list[str],
    salary_range: str,
    exp_range: str,
    max_pages: int,
) -> str:
    prechecks = recipe.get("prechecks", [])
    limits = recipe.get("limits", {})
    steps = recipe.get("steps", [])
    lines: list[str] = []
    lines.append(f"# BOSS Semi-Auto Screening Plan ({report_date})")
    lines.append("")
    lines.append("## Goal")
    lines.append("- 仅筛岗位清单，不投递、不打招呼。")
    lines.append("- 模式：Mac App 半自动（人工登录/验证码 + 代理给出步骤与检查点）。")
    lines.append("")
    lines.append("## Runtime Params")
    lines.append(f"- cities: {', '.join(cities)}")
    lines.append(f"- keywords: {', '.join(keywords)}")
    lines.append(f"- salary_range: {salary_range}")
    lines.append(f"- experience_range: {exp_range}")
    lines.append(f"- max_pages_per_keyword: {max_pages}")
    lines.append("")
    lines.append("## Prechecks")
    if isinstance(prechecks, list):
        for item in prechecks:
            lines.append(f"- [ ] {item}")
    lines.extend(
        [
            "- [ ] 仅执行筛选，不进入投递/沟通页面。",
            "- [ ] 登录、验证码、风控弹窗由人工处理。",
        ]
    )
    lines.append("")
    lines.append("## No Retry Policy")
    lines.append("- 任一步骤找不到关键元素 => 当前关键词动作立即失败并记录，不重试。")
    lines.append("- 继续执行下一关键词/城市任务。")
    lines.append("")
    lines.append("## Recipe Source")
    lines.append(f"- path: {recipe.get('__path__', 'unknown')}")
    lines.append(f"- name: {recipe.get('name', '')}")
    lines.append(f"- goal: {recipe.get('goal', '')}")
    lines.append("")
    lines.append("## Recipe Limits")
    if isinstance(limits, dict):
        for key, value in limits.items():
            lines.append(f"- {key}: {value}")
    lines.append("")
    lines.append("## Recipe Steps (reference)")
    if isinstance(steps, list):
        for idx, step in enumerate(steps, start=1):
            action = str(step.get("action", "")).strip()
            target = str(step.get("target", "")).strip()
            lines.append(f"{idx}. [{action}] {target}")
    lines.append("")
    lines.append("## Manual Execution Order")
    lines.append("1. 先跑上海（4个关键词），再跑杭州（4个关键词）。")
    lines.append("2. 每个关键词只看前 3 页岗位卡片。")
    lines.append("3. 采集字段：title/company/city/salary/experience/education/tags/detail_url。")
    lines.append("4. 失败时记录步骤 + 原因 + 截图路径。")
    lines.append("")
    return "\n".join(lines)


def _render_ranked_markdown(
    scored_jobs: list[dict[str, Any]],
    failures: list[CollectionFailure],
    top_k: int,
    salary_range: str,
    experience_range: str,
) -> str:
    lines: list[str] = []
    lines.append("# BOSS Matched Jobs (Ranked)")
    lines.append("")
    lines.append("No Retry Policy: find-error fail-fast enabled")
    lines.append("")
    lines.append("## Filters")
    lines.append(f"- salary_range: {salary_range}")
    lines.append(f"- experience_range: {experience_range}")
    lines.append(f"- top_k: {top_k}")
    lines.append("")

    if failures:
        lines.append("## Failures")
        for item in failures:
            screenshot = f", screenshot={item.screenshot_path}" if item.screenshot_path else ""
            lines.append(
                f"- city={item.city}, keyword={item.keyword}, step={item.step}, reason={item.reason}{screenshot}"
            )
        lines.append("")

    lines.append("## Top Jobs")
    headers = ["Rank", "Score", "Title", "Company", "City", "Salary", "Experience", "URL"]
    lines.append("| " + " | ".join(headers) + " |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for idx, job in enumerate(scored_jobs[:top_k], start=1):
        url = str(job.get("detail_url", "")).strip()
        url_md = f"[link]({url})" if url else ""
        lines.append(
            "| "
            + " | ".join(
                [
                    str(idx),
                    str(job.get("selected_score", "")),
                    str(job.get("title", "")).replace("|", "/"),
                    str(job.get("company", "")).replace("|", "/"),
                    str(job.get("city", "")).replace("|", "/"),
                    str(job.get("salary", "")).replace("|", "/"),
                    str(job.get("experience", "")).replace("|", "/"),
                    url_md,
                ]
            )
            + " |"
        )
    lines.append("")
    return "\n".join(lines)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="BOSS Mac App semi-auto screening pipeline.")
    parser.add_argument("--report-date", default=datetime.now().strftime("%Y-%m-%d"))
    parser.add_argument("--output-dir", default=os.getenv("BOSS_OUTPUT_DIR", str(DEFAULT_OUTPUT_ROOT)))
    parser.add_argument("--cities", default=os.getenv("BOSS_CITIES", ",".join(DEFAULT_CITIES)))
    parser.add_argument("--salary-range", default=os.getenv("BOSS_SALARY_RANGE", DEFAULT_SALARY_RANGE))
    parser.add_argument("--experience-range", default=os.getenv("BOSS_EXPERIENCE_RANGE", DEFAULT_EXPERIENCE_RANGE))
    parser.add_argument("--keywords", default=os.getenv("BOSS_KEYWORDS", ""))
    parser.add_argument("--resume-path", default=os.getenv("BOSS_RESUME_PATH", str(DEFAULT_RESUME_PATH)))
    parser.add_argument("--max-pages", type=int, default=int(os.getenv("BOSS_MAX_PAGES_PER_QUERY", str(DEFAULT_MAX_PAGES))))
    parser.add_argument("--top-k", type=int, default=int(os.getenv("BOSS_TOP_K", str(DEFAULT_TOP_K))))
    parser.add_argument("--raw-input", default="", help="optional json/jsonl/csv/tsv file for collected jobs")
    parser.add_argument("--dry-run", action="store_true", help="generate plan only")
    parser.add_argument("--non-interactive", action="store_true", help="skip manual inputs")
    return parser


def main() -> int:
    load_dotenv(PROJECT_ROOT / ".env")
    args = _build_parser().parse_args()

    cities = _normalize_csv_list(args.cities)
    if not cities:
        cities = list(DEFAULT_CITIES)

    resume_path = Path(args.resume_path).expanduser()
    keywords = _normalize_csv_list(args.keywords)
    if not keywords:
        keywords = _extract_keywords_from_resume(resume_path)
    if not keywords:
        keywords = list(DEFAULT_KEYWORDS)

    recipe = _resolve_recipe()
    out_root = Path(args.output_dir).expanduser()
    out_dir = out_root / args.report_date
    out_dir.mkdir(parents=True, exist_ok=True)
    plan_path = out_dir / "boss_plan.md"
    raw_path = out_dir / "boss_jobs_raw.json"
    ranked_path = out_dir / "boss_jobs_ranked.md"

    plan_md = _render_plan_markdown(
        recipe=recipe,
        report_date=args.report_date,
        cities=cities,
        keywords=keywords,
        salary_range=args.salary_range,
        exp_range=args.experience_range,
        max_pages=args.max_pages,
    )
    plan_path.write_text(plan_md, encoding="utf-8")

    if args.dry_run:
        payload = {
            "report_date": args.report_date,
            "mode": "dry_run",
            "cities": cities,
            "keywords": keywords,
            "salary_range": args.salary_range,
            "experience_range": args.experience_range,
            "max_pages": args.max_pages,
            "jobs": [],
            "failures": [],
        }
        raw_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        ranked_path.write_text(
            _render_ranked_markdown(
                scored_jobs=[],
                failures=[],
                top_k=args.top_k,
                salary_range=args.salary_range,
                experience_range=args.experience_range,
            ),
            encoding="utf-8",
        )
        print(f"plan={plan_path}")
        print(f"raw={raw_path}")
        print(f"ranked={ranked_path}")
        return 0

    failures: list[CollectionFailure] = []
    if args.raw_input:
        jobs = _parse_jobs_from_file(Path(args.raw_input).expanduser())
    else:
        jobs, failures = _collect_jobs_interactive(
            cities=cities,
            keywords=keywords,
            max_pages=args.max_pages,
            non_interactive=args.non_interactive,
        )

    deduped = _dedupe_jobs(jobs)
    scored = _score_jobs(
        jobs=deduped,
        keywords=keywords,
        salary_target=args.salary_range,
        exp_target=args.experience_range,
    )
    payload = {
        "report_date": args.report_date,
        "cities": cities,
        "keywords": keywords,
        "salary_range": args.salary_range,
        "experience_range": args.experience_range,
        "max_pages_per_keyword": args.max_pages,
        "raw_jobs_count": len(jobs),
        "deduped_jobs_count": len(deduped),
        "top_k": args.top_k,
        "jobs": jobs,
        "deduped_jobs": deduped,
        "scored_jobs": scored,
        "failures": [vars(item) for item in failures],
    }
    raw_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    ranked_path.write_text(
        _render_ranked_markdown(
            scored_jobs=scored,
            failures=failures,
            top_k=args.top_k,
            salary_range=args.salary_range,
            experience_range=args.experience_range,
        ),
        encoding="utf-8",
    )

    print(f"plan={plan_path}")
    print(f"raw={raw_path}")
    print(f"ranked={ranked_path}")
    print(f"raw_jobs={len(jobs)} deduped_jobs={len(deduped)} top={min(args.top_k, len(scored))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
