#!/usr/bin/env python3
import argparse
import os
import re
import sys
import time
from pathlib import Path
from typing import Iterable, List, Optional
from urllib.parse import quote_plus, urlparse

try:
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright
except ImportError:
    print("playwright not installed. Run: python3 -m pip install playwright", file=sys.stderr)
    raise


DEFAULT_TIMEOUT_MS = 15000
DEFAULT_CREATOR_URL = "https://creator.xiaohongshu.com/new/home"
DEFAULT_EXPLORE_URL = "https://www.xiaohongshu.com/explore"


class NotFoundStepError(RuntimeError):
    pass


def _first_visible(page, selectors: Iterable[str]):
    for selector in selectors:
        locator = page.locator(selector)
        count = locator.count()
        for index in range(min(count, 5)):
            item = locator.nth(index)
            try:
                if item.is_visible():
                    return item
            except Exception:
                continue
    return None


def _click_text_any(page, texts: Iterable[str], timeout_ms: int = 3000) -> bool:
    for text in texts:
        try:
            locator = page.get_by_text(text, exact=False)
            count = locator.count()
            for index in range(min(count, 10)):
                item = locator.nth(index)
                try:
                    item.wait_for(state="visible", timeout=timeout_ms)
                    item.click(timeout=timeout_ms)
                    return True
                except Exception:
                    continue
        except Exception:
            continue
    return False


def _has_visible_text(page, texts: Iterable[str]) -> bool:
    for text in texts:
        try:
            locator = page.get_by_text(text, exact=False)
            count = locator.count()
            for index in range(min(count, 10)):
                item = locator.nth(index)
                try:
                    if item.is_visible():
                        return True
                except Exception:
                    continue
        except Exception:
            continue
    return False


def _wait_stable(page, sleep_sec: float = 1.0):
    try:
        page.wait_for_load_state("domcontentloaded", timeout=DEFAULT_TIMEOUT_MS)
    except Exception:
        pass
    try:
        page.wait_for_load_state("networkidle", timeout=DEFAULT_TIMEOUT_MS)
    except Exception:
        pass
    time.sleep(sleep_sec)


def _debug_artifact(page, name: str) -> None:
    try:
        debug_dir = Path("/Users/cn/Workspace/feishu-bot-bridge/logs/xhs-debug")
        debug_dir.mkdir(parents=True, exist_ok=True)
        screenshot = debug_dir / f"{name}.png"
        html_file = debug_dir / f"{name}.html"
        page.screenshot(path=str(screenshot), full_page=True)
        html_file.write_text(page.content(), encoding="utf-8")
    except Exception:
        pass


def _hold_browser_on_failure(page, hold_seconds: int) -> None:
    if hold_seconds < 0:
        hold_seconds = 0
    if hold_seconds == 0:
        print("[xhs-web-operator] keep-open-on-fail enabled, waiting until browser window is closed manually", file=sys.stderr)
    else:
        print(
            f"[xhs-web-operator] keep-open-on-fail enabled, waiting up to {hold_seconds}s or until browser window is closed",
            file=sys.stderr,
        )

    start = time.time()
    while True:
        try:
            if page.is_closed():
                break
        except Exception:
            break
        if hold_seconds > 0 and (time.time() - start) >= hold_seconds:
            break
        time.sleep(1.0)


def _fill_any_input(locator, value: str) -> None:
    try:
        locator.fill(value, timeout=DEFAULT_TIMEOUT_MS)
        return
    except Exception:
        pass
    locator.click(timeout=DEFAULT_TIMEOUT_MS)
    try:
        locator.press("Meta+A")
    except Exception:
        pass
    locator.type(value, delay=10)


def _read_locator_text(locator) -> str:
    try:
        text = locator.input_value(timeout=1200)
        if text:
            return str(text)
    except Exception:
        pass
    try:
        text = locator.inner_text(timeout=1200)
        if text:
            return str(text)
    except Exception:
        pass
    try:
        text = locator.text_content(timeout=1200)
        if text:
            return str(text)
    except Exception:
        pass
    return ""


def _force_fill_contenteditable(locator, value: str) -> None:
    locator.evaluate(
        """(el, rawText) => {
            const text = String(rawText || "").replace(/\\r\\n/g, "\\n");
            const escapeHtml = (s) => s
              .replace(/&/g, "&amp;")
              .replace(/</g, "&lt;")
              .replace(/>/g, "&gt;");
            el.focus();
            const isPm = (el.className || "").includes("ProseMirror");
            if (isPm) {
              const lines = text.split("\\n");
              const html = lines.map(line => {
                const v = escapeHtml(line || "");
                return v ? `<p>${v}</p>` : "<p><br></p>";
              }).join("");
              el.innerHTML = html || "<p><br></p>";
            } else if ("value" in el) {
              el.value = text;
            } else {
              el.textContent = text;
            }
            const inputEvt = new InputEvent("input", { bubbles: true, data: text, inputType: "insertText" });
            el.dispatchEvent(inputEvt);
            el.dispatchEvent(new Event("change", { bubbles: true }));
        }""",
        value,
    )


def _click_first_visible_locator(page, selectors: Iterable[str], timeout_ms: int = DEFAULT_TIMEOUT_MS) -> bool:
    locator = _first_visible(page, selectors)
    if locator is None:
        return False
    try:
        locator.scroll_into_view_if_needed(timeout=min(timeout_ms, 3000))
    except Exception:
        pass
    for force in (False, True):
        try:
            locator.click(timeout=timeout_ms, force=force)
            return True
        except Exception:
            continue
    return False


def _parse_images_csv(images_csv: str) -> List[Path]:
    paths: List[Path] = []
    for item in (images_csv or "").split(","):
        text = item.strip()
        if not text:
            continue
        path = Path(text).expanduser()
        if not path.exists():
            raise FileNotFoundError(f"image file not found: {path}")
        paths.append(path)
    return paths


def _parse_topics_csv(topics_csv: str) -> List[str]:
    topics: List[str] = []
    for item in (topics_csv or "").split(","):
        text = item.strip().lstrip("#").strip()
        if text:
            topics.append(text)
    return topics


def _normalize_note_content_text(raw_text: str) -> str:
    text = str(raw_text or "")
    text = text.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\t", "\t")
    text = text.replace("\r\n", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = _collapse_broken_ascii_hashtags(text)
    text = re.sub(r"(?<=\S)#(?=[。！？；：，,.!?;:])", "", text)
    text = re.sub(r"(?<=\S)#(?=\s*$)", "", text)
    text = text.strip()
    text = re.sub(r"#\s*$", "", text).rstrip()
    return text


def _collapse_broken_ascii_hashtags(text: str) -> str:
    normalized = str(text or "").replace("\u200b", "").replace("\ufeff", "")
    pattern = re.compile(r"#\s*[A-Za-z0-9](?:\s*#\s*[A-Za-z0-9])+")
    while True:
        collapsed = pattern.sub(lambda m: "#" + "".join(re.findall(r"[A-Za-z0-9]", m.group(0))), normalized)
        collapsed = re.sub(r"##+", "#", collapsed)
        if collapsed == normalized:
            return collapsed
        normalized = collapsed


def _resolve_note_content_input(page):
    return _first_visible(
        page,
        [
            "div.tiptap.ProseMirror[role='textbox']",
            "div.ProseMirror[role='textbox']",
            "[contenteditable='true'].tiptap.ProseMirror",
            "div.tiptap.ProseMirror:has(p[data-placeholder*='输入正文描述'])",
            "div[contenteditable='true'][role='textbox']",
            "textarea[placeholder*='正文']",
            "textarea[placeholder*='内容']",
            "textarea[placeholder*='描述']",
            "[contenteditable='true'][placeholder*='正文']",
            "[contenteditable='true'][placeholder*='内容']",
            "[contenteditable='true'][data-placeholder*='正文']",
            "[contenteditable='true'][data-placeholder*='内容']",
            ".ql-editor",
            "textarea",
        ],
    )


def _cleanup_content_trailing_hash(page) -> None:
    content_input = _resolve_note_content_input(page)
    if content_input is None:
        return
    existing = ""
    try:
        existing = content_input.input_value(timeout=1200)
    except Exception:
        try:
            existing = content_input.inner_text(timeout=1200)
        except Exception:
            existing = ""
    if not existing:
        return
    cleaned = _collapse_broken_ascii_hashtags(existing)
    cleaned = re.sub(r"#\s*$", "", cleaned).rstrip()
    if cleaned != existing:
        _fill_any_input(content_input, cleaned[:1000])


def _ensure_topics_in_content(page, topics: List[str]) -> bool:
    if not topics:
        return False
    content_input = _resolve_note_content_input(page)
    if content_input is None:
        return False
    existing = _read_locator_text(content_input)
    normalized_existing = _collapse_broken_ascii_hashtags(existing)
    merged = normalized_existing.rstrip()
    changed = False
    for topic in topics:
        value = (topic or "").strip().lstrip("#").strip()
        if not value:
            continue
        pattern = re.compile(rf"(?:^|\s)#{re.escape(value)}(?:\s|$)", flags=re.IGNORECASE)
        if pattern.search(merged):
            continue
        merged = f"{merged} #{value}".strip()
        changed = True
    if not changed:
        return False
    merged = _normalize_note_content_text(merged)
    _fill_any_input(content_input, merged[:1000])
    return True


def _upload_images(page, image_paths: List[Path]) -> None:
    if not image_paths:
        raise NotFoundStepError("cannot find image source: publish requires at least one image")
    file_input = None
    for selector in ["input[type='file'][accept*='image']", "input[type='file']"]:
        locator = page.locator(selector)
        if locator.count() > 0:
            file_input = locator.first
            break
    if file_input is None:
        _debug_artifact(page, "publish-upload-input-missing")
        raise NotFoundStepError("cannot find image upload input[type=file]")
    try:
        file_input.set_input_files([str(path) for path in image_paths], timeout=DEFAULT_TIMEOUT_MS)
    except Exception as exc:
        _debug_artifact(page, "publish-upload-set-files-failed")
        raise NotFoundStepError(f"cannot find usable image upload channel: {exc}") from exc
    _wait_stable(page, sleep_sec=2.0)


def _fill_title(page, title: str) -> None:
    title_input = _first_visible(
        page,
        [
            "input[placeholder*='标题']",
            "textarea[placeholder*='标题']",
            "input[placeholder*='添加标题']",
            "[contenteditable='true'][placeholder*='标题']",
            "[contenteditable='true'][data-placeholder*='标题']",
        ],
    )
    if title_input is None:
        snippet = (title or "").strip()[:16]
        if snippet and _has_visible_text(page, [snippet]):
            return
        raise NotFoundStepError("cannot find publish title input")
    _fill_any_input(title_input, title[:30])


def _fill_note_content(page, content: str) -> None:
    normalized_content = _normalize_note_content_text(content)
    content_input = _resolve_note_content_input(page)
    if content_input is None:
        snippet = (normalized_content or "").strip()[:24]
        if snippet and _has_visible_text(page, [snippet]):
            return
        raise NotFoundStepError("cannot find publish content input")
    _fill_any_input(content_input, normalized_content[:1000])
    expected = normalized_content.strip()[:12]
    actual = _read_locator_text(content_input).strip()
    if expected and expected not in actual:
        try:
            _force_fill_contenteditable(content_input, normalized_content[:1000])
        except Exception:
            pass
        actual = _read_locator_text(content_input).strip()
        if expected and expected not in actual and not _has_visible_text(page, [expected]):
            raise NotFoundStepError("cannot fill publish content input")


def _fill_text_card_input(page, content: str) -> None:
    card_input = _first_visible(
        page,
        [
            "textarea[placeholder*='输入']",
            "textarea[placeholder*='文案']",
            "textarea[placeholder*='内容']",
            "textarea[placeholder*='写点什么']",
            "[contenteditable='true'][placeholder*='输入']",
            "[contenteditable='true'][data-placeholder*='输入']",
            "[contenteditable='true'][data-placeholder*='真诚分享经验或资讯']",
            "[contenteditable='true'][placeholder*='真诚分享经验或资讯']",
            "[class*='editor'] [contenteditable='true']",
            "[role='textbox']",
            ".ql-editor",
        ],
    )
    if card_input is not None:
        _fill_any_input(card_input, content[:600])
        return

    # Fallback: click the visible hint text area shown in creator UI, then type into focused editor.
    if _click_text_any(page, ["真诚分享经验或资讯，提个问题也不错", "真诚分享经验或资讯", "提个问题也不错"], timeout_ms=5000):
        time.sleep(0.2)
        focused = page.locator(":focus")
        if focused.count() > 0:
            try:
                _fill_any_input(focused.first, content[:600])
                return
            except Exception:
                pass
        try:
            page.keyboard.type(content[:600], delay=8)
            return
        except Exception:
            pass

    _debug_artifact(page, "publish-text-card-input-missing")
    raise NotFoundStepError("cannot find text-card input")


def _click_generate_image_button(page) -> None:
    exact = _first_visible(
        page,
        [
            "div.edit-text-button:has(span.edit-text-button-text:has-text('生成图片'))",
            "div.edit-text-button:has-text('生成图片')",
        ],
    )
    button = exact or _first_visible(
        page,
        [
            "button:has-text('生成图片')",
            "[role='button']:has-text('生成图片')",
            "[class*='button']:has-text('生成图片')",
            "[class*='edit-text-button']:has-text('生成图片')",
        ],
    )
    if button is None:
        _debug_artifact(page, "publish-text-card-generate-missing")
        raise NotFoundStepError("cannot find text-card generate button")

    try:
        button.scroll_into_view_if_needed(timeout=3000)
    except Exception:
        pass

    # Blur editor focus first, otherwise the click can be swallowed by editor state.
    try:
        page.keyboard.press("Escape")
    except Exception:
        pass
    try:
        page.mouse.click(20, 20)
    except Exception:
        pass

    try:
        if button.get_attribute("disabled") is not None or button.get_attribute("aria-disabled") == "true":
            _debug_artifact(page, "publish-text-card-generate-disabled")
            raise NotFoundStepError("cannot click text-card generate button: button disabled")
    except NotFoundStepError:
        raise
    except Exception:
        pass

    def _click_once() -> bool:
        for force in (False, True):
            try:
                button.click(timeout=6000, force=force)
                return True
            except Exception:
                continue
        try:
            box = button.bounding_box()
            if box:
                page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                return True
        except Exception:
            pass
        try:
            button.evaluate(
                """el => {
                    const fire = (type) => el.dispatchEvent(new MouseEvent(type, {bubbles: true, cancelable: true, view: window}));
                    fire('pointerdown'); fire('mousedown'); fire('pointerup'); fire('mouseup'); fire('click');
                }"""
            )
            return True
        except Exception:
            return False

    def _generation_started() -> bool:
        signal_selectors = [
            "text=图片生成中",
            "[class*='loading']:has-text('图片生成中')",
            "[aria-busy='true']",
            ".d-loading-mask",
        ]
        for selector in signal_selectors:
            try:
                locator = page.locator(selector)
                if locator.count() > 0 and locator.first.is_visible():
                    return True
            except Exception:
                continue
        return False

    clicked = False
    started = False
    for _ in range(3):
        clicked = _click_once()
        if not clicked:
            continue
        # generation indicator can be brief, poll shortly.
        for _ in range(8):
            if _generation_started():
                started = True
                break
            time.sleep(0.25)
        if started:
            break
        time.sleep(0.4)

    if not clicked:
        _debug_artifact(page, "publish-text-card-generate-click-failed")
        raise NotFoundStepError("cannot click text-card generate button")
    if not started:
        _debug_artifact(page, "publish-text-card-generate-not-triggered")
        raise NotFoundStepError("cannot trigger text-card generation after click")

    _wait_stable(page, sleep_sec=2.0)


def _select_generated_card(page) -> None:
    clickable_selectors = [
        ".template-card",
        ".card-item",
        "[class*='card-item']",
        "[class*='template-item']",
        "[class*='style-card']",
        ".swiper-slide.text-editor-slide",
        ".text-editor-slide",
        ".text-editor-container.cover",
        ".edit-text-item-container .swiper-slide-active",
    ]
    if _click_first_visible_locator(page, clickable_selectors, timeout_ms=7000):
        _wait_stable(page, sleep_sec=0.8)
        return

    # Fallback: if generated card area is already visible/active, treat as selected.
    active_card = _first_visible(
        page,
        [
            ".swiper-slide.text-editor-slide.swiper-slide-active",
            ".text-editor-slide.swiper-slide-active",
            ".text-editor-container.cover",
            ".short-container.background-for-short .text-content",
        ],
    )
    if active_card is not None:
        return

    _debug_artifact(page, "publish-text-card-select-missing")
    raise NotFoundStepError("cannot find generated card selection")


def _preferred_card_styles(title: str, content: str, topics: List[str]) -> List[str]:
    text = f"{title}\n{content}\n{' '.join(topics)}".lower()
    style_order = ["简约", "边框", "基础", "便签", "备忘", "清新", "手写", "涂鸦", "光影"]
    scores = {style: 0.0 for style in style_order}

    def _count(words: Iterable[str]) -> int:
        return sum(1 for word in words if word and word.lower() in text)

    # 默认偏稳健样式，减少随机漂移。
    scores["简约"] += 1.2
    scores["边框"] += 1.0
    scores["基础"] += 0.8

    # 任务/方法论/商业类内容：优先结构化样式。
    task_hits = _count(
        [
            "流程",
            "步骤",
            "清单",
            "方法",
            "教程",
            "指南",
            "sop",
            "模板",
            "执行",
            "复盘",
            "策略",
            "增长",
            "运营",
            "效率",
            "数据",
            "分析",
            "ai",
            "职场",
        ]
    )
    scores["简约"] += task_hits * 0.9
    scores["边框"] += task_hits * 0.85
    scores["基础"] += task_hits * 0.55
    scores["便签"] += task_hits * 0.35

    # 备忘/打卡/待办：优先备忘、便签。
    memo_hits = _count(["备忘", "提醒", "待办", "todo", "checklist", "deadline", "打卡", "记录"])
    scores["备忘"] += memo_hits * 1.2
    scores["便签"] += memo_hits * 1.0
    scores["简约"] += memo_hits * 0.4

    # 情绪/日记/游记：偏手写、清新、光影。
    diary_hits = _count(["日记", "游记", "随笔", "感悟", "心情", "故事", "今天", "刚刚", "我"])
    scores["手写"] += diary_hits * 0.85
    scores["清新"] += diary_hits * 0.65
    scores["光影"] += diary_hits * 0.45

    # 设计/摄影/审美：偏清新、光影、涂鸦。
    visual_hits = _count(["设计", "灵感", "创意", "摄影", "美学", "穿搭", "配色", "氛围"])
    scores["清新"] += visual_hits * 1.0
    scores["光影"] += visual_hits * 0.95
    scores["涂鸦"] += visual_hits * 0.75

    # 列表/编号内容更适合结构化卡片。
    if any(marker in content for marker in ["①", "②", "③", "1.", "2.", "3.", "第1", "第2", "第3"]):
        scores["简约"] += 1.2
        scores["边框"] += 1.0
        scores["基础"] += 0.6

    # 文本偏长时，提升可读性样式优先级。
    content_len = len(re.sub(r"\s+", "", content or ""))
    if content_len >= 180:
        scores["边框"] += 0.9
        scores["基础"] += 0.8
        scores["简约"] += 0.6
    elif content_len <= 80:
        scores["便签"] += 0.8
        scores["手写"] += 0.5

    ranked = sorted(style_order, key=lambda style: (scores[style], -style_order.index(style)), reverse=True)
    return ranked


def _find_visible_style_label(page, style: str):
    selectors = [
        f".template-item:has-text('{style}')",
        f".style-item:has-text('{style}')",
        f".card-item:has-text('{style}')",
        f"[class*='style']:has-text('{style}')",
        f"[class*='template']:has-text('{style}')",
        f"span:has-text('{style}')",
        f"div:has-text('{style}')",
    ]
    return _first_visible(page, selectors)


def _compact_text(value: str) -> str:
    return re.sub(r"\s+", "", str(value or ""))


def _find_cover_container_by_style(page, style: str):
    target = _compact_text(style)
    if not target:
        return None
    containers = page.locator(".cover-item-container")
    try:
        count = min(containers.count(), 24)
    except Exception:
        count = 0
    for index in range(count):
        container = containers.nth(index)
        try:
            if not container.is_visible():
                continue
        except Exception:
            continue
        name_locator = container.locator(".cover-name").first
        name_text = _compact_text(_read_locator_text(name_locator))
        if name_text and (name_text == target or target in name_text):
            return container
    return None


def _available_style_labels(page, styles: List[str]) -> List[str]:
    available: List[str] = []
    for style in styles:
        if _find_cover_container_by_style(page, style) is not None or _find_visible_style_label(page, style) is not None:
            available.append(style)
    return available


def _is_style_selected(page, style: str) -> bool:
    container = _find_cover_container_by_style(page, style)
    if container is not None:
        active_cover = container.locator(".cover-item.active").first
        try:
            if active_cover.count() > 0 and active_cover.is_visible():
                return True
        except Exception:
            pass

    selected_selectors = [
        f".template-item.selected:has-text('{style}')",
        f".style-item.selected:has-text('{style}')",
        f".card-item.selected:has-text('{style}')",
        f"[class*='active']:has-text('{style}')",
        f"[class*='selected']:has-text('{style}')",
        f"[class*='current']:has-text('{style}')",
        f"[class*='checked']:has-text('{style}')",
    ]
    if _first_visible(page, selected_selectors) is not None:
        return True
    label = _find_visible_style_label(page, style)
    if label is None:
        return False
    try:
        return bool(
            label.evaluate(
                """(el) => {
                    let node = el;
                    for (let i = 0; i < 6 && node; i += 1) {
                        const cls = String(node.className || '').toLowerCase();
                        if (/(active|selected|current|checked|choose|focus|on)/.test(cls)) {
                            return true;
                        }
                        node = node.parentElement;
                    }
                    return false;
                }"""
            )
        )
    except Exception:
        return False


def _click_style_and_verify(page, style: str) -> bool:
    cover_container = _find_cover_container_by_style(page, style)
    if cover_container is not None:
        if _is_style_selected(page, style):
            return True
        cover_click_targets = [
            cover_container.locator(".cover-item").first,
            cover_container.locator(".cover-name").first,
            cover_container,
        ]
        for target in cover_click_targets:
            for force in (False, True):
                try:
                    target.scroll_into_view_if_needed(timeout=1200)
                except Exception:
                    pass
                try:
                    target.click(timeout=2500, force=force)
                except Exception:
                    continue
                _wait_stable(page, sleep_sec=0.35)
                if _is_style_selected(page, style):
                    return True

    label = _find_visible_style_label(page, style)
    if label is None:
        return False

    click_targets = [label]
    try:
        click_targets.append(label.locator("xpath=ancestor::*[contains(@class,'item') or contains(@class,'card') or contains(@class,'style')][1]"))
    except Exception:
        pass
    try:
        click_targets.append(label.locator("xpath=.."))
    except Exception:
        pass

    for target in click_targets:
        for force in (False, True):
            try:
                target.scroll_into_view_if_needed(timeout=1200)
            except Exception:
                pass
            try:
                target.click(timeout=2200, force=force)
            except Exception:
                continue
            _wait_stable(page, sleep_sec=0.35)
            if _is_style_selected(page, style):
                return True
    return False


def _choose_card_style(page, title: str, content: str, topics: List[str]) -> str:
    preferred = _preferred_card_styles(title, content, topics)
    visible_styles = _available_style_labels(page, preferred)
    candidate_styles = visible_styles + [style for style in preferred if style not in visible_styles]

    for style in candidate_styles:
        if _click_style_and_verify(page, style):
            return style

    # Fallback: click the first visible style card if labels cannot be matched.
    if _click_first_visible_locator(
        page,
        [
            ".template-item",
            ".style-item",
            ".card-item",
            "[class*='style']",
            "[class*='template']",
            "[class*='card']",
        ],
        timeout_ms=4000,
    ):
        _wait_stable(page, sleep_sec=0.6)
        if _has_visible_text(page, ["下一步", "去发布"]):
            return "fallback_first_visible"

    # If card generation view is already active and we can proceed, keep current card.
    if _has_visible_text(page, ["下一步", "去发布"]):
        return "fallback_current_selected"
    raise NotFoundStepError("cannot select a card style")


def _append_topics(page, topics: List[str]) -> None:
    if not topics:
        return
    clean_topics = []
    for topic in topics:
        value = (topic or "").strip().lstrip("#").strip()
        if value and value not in clean_topics:
            clean_topics.append(value)
    if not clean_topics:
        return

    clicked_any = False
    # Preferred path: click dedicated topic button only, avoid generic "话题" mis-click.
    opened_topic_panel = _click_first_visible_locator(
        page,
        [
            "button#topicBtn",
            "#topicBtn",
            "button.topic-btn",
            "button.contentBtn.topic-btn",
        ],
        timeout_ms=2500,
    ) or _click_text_any(page, ["# 话题", "#话题"], timeout_ms=1500)
    if not opened_topic_panel:
        return

    _wait_stable(page, sleep_sec=0.6)
    topic_input = _first_visible(
        page,
        [
            "input#topicInput",
            "input[id*='topic']",
            "[class*='topic'] input[placeholder*='话题']",
            "[class*='topic'] input[placeholder*='搜索']",
            "[role='dialog'] input[placeholder*='话题']",
            "[role='dialog'] input[placeholder*='搜索']",
            "[class*='popover'] input[placeholder*='话题']",
            "[class*='popover'] input[placeholder*='搜索']",
        ],
    )
    for topic in clean_topics[:5]:
        selected = _click_first_visible_locator(
            page,
            [
                f"button:has-text('#{topic}')",
                f"[class*='topic']:has-text('#{topic}')",
                f"[role='dialog'] :text('#{topic}')",
            ],
            timeout_ms=1200,
        )
        if selected:
            clicked_any = True
            continue
        if topic_input is not None:
            try:
                _fill_any_input(topic_input, topic)
                page.keyboard.press("Enter")
                time.sleep(0.2)
                clicked_any = True
                continue
            except Exception:
                pass
    try:
        page.keyboard.press("Escape")
    except Exception:
        pass
    _wait_stable(page, sleep_sec=0.3)
    _ensure_topics_in_content(page, clean_topics)
    _cleanup_content_trailing_hash(page)


def _set_original_ai_declaration(page) -> None:
    def _handle_original_declaration_modal() -> None:
        modal = _first_visible(
            page,
            [
                "[role='dialog']:has-text('声明原创')",
                ".d-modal:has-text('声明原创')",
                "[class*='modal']:has-text('声明原创')",
                "div:has-text('我已阅读并同意'):has-text('声明原创')",
            ],
        )
        if modal is None:
            return

        agree_checkbox = _first_visible(
            modal,
            [
                ".d-checkbox",
                "input[type='checkbox']",
                "[class*='checkbox']",
            ],
        )
        if agree_checkbox is None:
            _debug_artifact(page, "publish-original-modal-checkbox-missing")
            raise NotFoundStepError("cannot find 原创声明弹窗同意勾选框")
        try:
            agree_checkbox.click(timeout=3000)
        except Exception as exc:
            _debug_artifact(page, "publish-original-modal-checkbox-click-failed")
            raise NotFoundStepError(f"cannot click 原创声明弹窗同意勾选框: {exc}") from exc

        confirmed = _click_text_any(page, ["声明原创"], timeout_ms=4000) or _click_first_visible_locator(
            page,
            [
                "button:has-text('声明原创')",
                "[role='button']:has-text('声明原创')",
            ],
            timeout_ms=3000,
        )
        if not confirmed:
            _debug_artifact(page, "publish-original-modal-confirm-missing")
            raise NotFoundStepError("cannot click 原创声明弹窗按钮: 声明原创")
        _wait_stable(page, sleep_sec=0.5)

    original_enabled = False
    rows = page.locator("div:has-text('原创声明')")
    try:
        row_count = rows.count()
    except Exception:
        row_count = 0
    for index in range(min(row_count, 12)):
        row = rows.nth(index)
        try:
            if not row.is_visible():
                continue
        except Exception:
            continue
        switch = row.locator(".d-switch-simulator").first
        try:
            if switch.count() == 0 or not switch.is_visible():
                continue
        except Exception:
            continue

        unchecked = False
        try:
            cls = (switch.get_attribute("class") or "").lower()
            if "unchecked" in cls:
                unchecked = True
            elif "checked" in cls:
                unchecked = False
            else:
                checkbox = switch.locator("input[type='checkbox']").first
                if checkbox.count() > 0:
                    unchecked = not checkbox.is_checked()
        except Exception:
            unchecked = False

        if unchecked:
            clicked = False
            for force in (False, True):
                try:
                    switch.click(timeout=2500, force=force)
                    clicked = True
                    break
                except Exception:
                    continue
            if not clicked:
                _debug_artifact(page, "publish-original-declare-toggle-failed")
                raise NotFoundStepError("cannot enable 原创声明 switch")
            _wait_stable(page, sleep_sec=0.25)
            _handle_original_declaration_modal()
            try:
                cls_after = (switch.get_attribute("class") or "").lower()
                if "unchecked" in cls_after:
                    _debug_artifact(page, "publish-original-declare-still-unchecked")
                    raise NotFoundStepError("原创声明 switch still unchecked after click")
            except NotFoundStepError:
                raise
            except Exception:
                pass
        original_enabled = True
        break

    if not original_enabled:
        _debug_artifact(page, "publish-original-declare-missing")
        raise NotFoundStepError("cannot find 原创声明 switch")

    _wait_stable(page, sleep_sec=0.5)

    type_opened = _click_text_any(page, ["内容类型声明", "内容类型"], timeout_ms=3000) or _click_first_visible_locator(
        page,
        [
            "button:has-text('内容类型')",
            "[class*='declare']:has-text('内容类型')",
            "[class*='option']:has-text('内容类型')",
        ],
        timeout_ms=2500,
    )
    if not type_opened:
        _debug_artifact(page, "publish-content-type-declare-missing")
        raise NotFoundStepError("cannot find 内容类型声明入口")

    _wait_stable(page, sleep_sec=0.5)

    selected = _click_text_any(
        page,
        [
            "笔记含AI合成内容",
            "含AI合成内容",
            "AI合成内容",
            "已在正文中自主标注",
        ],
        timeout_ms=3500,
    ) or _click_first_visible_locator(
        page,
        [
            "label:has-text('笔记含AI合成内容')",
            "button:has-text('笔记含AI合成内容')",
            "[class*='option']:has-text('笔记含AI合成内容')",
            "[class*='option']:has-text('含AI合成内容')",
        ],
        timeout_ms=3000,
    )
    if not selected:
        _debug_artifact(page, "publish-ai-declare-option-missing")
        raise NotFoundStepError("cannot find 声明内容: 笔记含AI合成内容")

    _wait_stable(page, sleep_sec=0.4)
    _click_text_any(page, ["确定", "完成", "保存"], timeout_ms=1200)
    try:
        page.keyboard.press("Escape")
    except Exception:
        pass
    _wait_stable(page, sleep_sec=0.3)


def _submit_publish(page) -> None:
    try:
        page.mouse.wheel(0, 4000)
        page.keyboard.press("End")
        time.sleep(0.2)
    except Exception:
        pass

    if _click_first_visible_locator(
        page,
        [
            "button:has-text('发布笔记')",
            "button:has-text('发布')",
            "[role='button']:has-text('发布笔记')",
            "[role='button']:has-text('发布')",
            "[class*='publish']:has-text('发布')",
        ],
        timeout_ms=8000,
    ):
        _wait_stable(page, sleep_sec=2.0)
        return

    if not _click_text_any(page, ["发布笔记", "发布", "立即发布", "确认发布"], timeout_ms=8000):
        _debug_artifact(page, "publish-submit-button-missing")
        raise NotFoundStepError("cannot find publish submit button")
    _wait_stable(page, sleep_sec=2.0)


def _verify_publish_in_note_management(page, title: str, timeout_sec: int = 60) -> None:
    title_snippet = (title or "").strip()[:20]
    if not title_snippet:
        return

    date_marker = time.strftime("%Y年%m月%d日")
    deadline = time.time() + max(10, timeout_sec)

    while time.time() < deadline:
        try:
            if "creator.xiaohongshu.com" not in (page.url or ""):
                page.goto(DEFAULT_CREATOR_URL, wait_until="domcontentloaded", timeout=60000)
                _wait_stable(page, sleep_sec=1.0)
        except Exception:
            pass

        if not _click_text_any(page, ["笔记管理"], timeout_ms=4000):
            try:
                page.goto(DEFAULT_CREATOR_URL, wait_until="domcontentloaded", timeout=60000)
                _wait_stable(page, sleep_sec=1.0)
                _click_text_any(page, ["笔记管理"], timeout_ms=4000)
            except Exception:
                pass

        _wait_stable(page, sleep_sec=1.0)
        _click_text_any(page, ["全部笔记"], timeout_ms=2500)
        _wait_stable(page, sleep_sec=0.6)
        # Acceptance rule: success when latest notes under “全部笔记” include the just-published title.
        note_rows = page.locator(
            ", ".join(
                [
                    ".note-item",
                    ".note-card",
                    ".content-item",
                    "[class*='note-item']",
                    "[class*='noteItem']",
                    "[class*='note-card']",
                    "[class*='NoteItem']",
                ]
            )
        )
        try:
            row_count = note_rows.count()
        except Exception:
            row_count = 0
        for idx in range(min(row_count, 3)):
            row = note_rows.nth(idx)
            try:
                if not row.is_visible():
                    continue
                row_text = row.inner_text(timeout=1200)
            except Exception:
                continue
            if title_snippet in row_text and any(token in row_text for token in [date_marker, "发布于", "刚刚", "分钟前", "小时前", "今天"]):
                return

        # Fallback: some views only expose merged text nodes.
        if _has_visible_text(page, [title_snippet]) and _has_visible_text(page, ["全部笔记", date_marker, "发布于"]):
            return

        try:
            page.reload(wait_until="domcontentloaded", timeout=30000)
            _wait_stable(page, sleep_sec=1.2)
        except Exception:
            time.sleep(1.2)

    _debug_artifact(page, "publish-verify-note-management-failed")
    raise NotFoundStepError("cannot verify published note in 笔记管理")


def _switch_to_tab(page, names: List[str]) -> bool:
    for name in names:
        if _click_first_visible_locator(
            page,
            [
                f".creator-tab:has-text('{name}')",
                f".header-tabs .creator-tab:has-text('{name}')",
                f".d-menu-item:has-text('{name}')",
                f"[role='tab']:has-text('{name}')",
            ],
            timeout_ms=5000,
        ):
            _wait_stable(page, sleep_sec=1.0)
            return True
    if _click_text_any(page, names, timeout_ms=5000):
        _wait_stable(page, sleep_sec=1.0)
        return True
    return False


def _select_publish_mode(page, mode: str) -> None:
    if not _click_text_any(page, ["发布笔记", "发布", "去发布"], timeout_ms=4000):
        _debug_artifact(page, "publish-open-entry-missing")
        raise NotFoundStepError("cannot open publish entry")
    _wait_stable(page, sleep_sec=0.8)

    selectors = [
        f".publish-video-popover .container:has-text('{mode}')",
        f".d-popover .container:has-text('{mode}')",
        f".dropdownItem .container:has-text('{mode}')",
    ]
    if _click_first_visible_locator(page, selectors, timeout_ms=6000):
        _wait_stable(page, sleep_sec=1.0)
        return

    tab_selectors = [
        f".header-tabs .creator-tab:has-text('{mode}')",
        f".creator-tab:has-text('{mode}')",
    ]
    if _click_first_visible_locator(page, tab_selectors, timeout_ms=6000):
        _wait_stable(page, sleep_sec=1.0)
        return

    _debug_artifact(page, f"publish-mode-missing-{mode}")
    raise NotFoundStepError(f"cannot find publish mode button: {mode}")


def _run_image_note_flow(
    page,
    title: str,
    content: str,
    image_paths: List[Path],
    image_strategy: str,
    topics: List[str],
    submit_publish: bool = True,
) -> None:
    _select_publish_mode(page, "上传图文")

    if image_strategy == "upload":
        _upload_images(page, image_paths)
    else:
        if not _click_text_any(page, ["文字配图", "配图", "图文生成"], timeout_ms=6000):
            _debug_artifact(page, "publish-text-card-entry-missing")
            raise NotFoundStepError("cannot find text-card entry")
        _wait_stable(page, sleep_sec=1.0)
        _fill_text_card_input(page, content)
        print("[xhs] image_note step6: click 生成图片", file=sys.stderr)
        _click_generate_image_button(page)
        print("[xhs] image_note step6 done: clicked 生成图片", file=sys.stderr)
        card_ready = False
        for _ in range(60):
            if _has_visible_text(page, ["选择一个喜欢的卡片", "换配色", "下一步", "去发布"]):
                card_ready = True
                break
            time.sleep(0.5)
        if card_ready and _has_visible_text(page, ["选择一个喜欢的卡片", "换配色"]):
            chosen_style = _choose_card_style(page, title, content, topics)
            print(f"[xhs] image_note step7 done: selected style={chosen_style}", file=sys.stderr)
        elif card_ready and _has_visible_text(page, ["下一步", "去发布"]):
            print("[xhs] image_note step7 done: card already selected by default", file=sys.stderr)
        else:
            _select_generated_card(page)

    if _click_text_any(page, ["下一步", "去发布"], timeout_ms=6000):
        _wait_stable(page, sleep_sec=1.0)
    else:
        if image_strategy != "upload":
            _debug_artifact(page, "publish-next-step-missing")
            raise NotFoundStepError("cannot find next step button")

    _fill_title(page, title)
    if image_strategy == "upload":
        _fill_note_content(page, content)
    _append_topics(page, topics)
    _set_original_ai_declaration(page)
    if submit_publish:
        _submit_publish(page)


def _run_long_article_flow(page, title: str, content: str, topics: List[str], submit_publish: bool = True) -> None:
    _select_publish_mode(page, "写长文")

    if not _click_text_any(page, ["新的创作", "新建创作", "开始创作"], timeout_ms=5000):
        _debug_artifact(page, "publish-article-new-creation-missing")
        raise NotFoundStepError("cannot find new creation entry")
    _wait_stable(page, sleep_sec=1.0)

    _fill_title(page, title)
    _fill_note_content(page, content)

    if not _click_text_any(page, ["一键排版"], timeout_ms=4000):
        _debug_artifact(page, "publish-article-format-missing")
        raise NotFoundStepError("cannot find one-click format button")
    _wait_stable(page, sleep_sec=1.0)

    if not _click_first_visible_locator(
        page,
        [
            ".template-card",
            ".card-item",
            "[class*='template-item']",
        ],
        timeout_ms=3000,
    ):
        _debug_artifact(page, "publish-article-template-missing")
        raise NotFoundStepError("cannot find article template selection")
    if not _click_text_any(page, ["下一步", "去发布"], timeout_ms=6000):
        _debug_artifact(page, "publish-article-next-step-missing")
        raise NotFoundStepError("cannot find next step button")
    _wait_stable(page, sleep_sec=1.0)

    _append_topics(page, topics)
    _set_original_ai_declaration(page)
    if submit_publish:
        _submit_publish(page)


def _find_bio_editor(page):
    preferred = _first_visible(
        page,
        [
            "textarea[placeholder*='简介']",
            "textarea[placeholder*='介绍']",
            "input[placeholder*='简介']",
            "input[placeholder*='介绍']",
            "textarea[name*='bio']",
            "textarea",
            "[contenteditable='true'][placeholder*='简介']",
            "[contenteditable='true'][placeholder*='介绍']",
            "[contenteditable='true']",
        ],
    )
    if preferred:
        return preferred

    try:
        locator = page.locator("textarea,input,[contenteditable='true']")
        count = locator.count()
        best = None
        for index in range(min(count, 30)):
            item = locator.nth(index)
            try:
                if not item.is_visible():
                    continue
            except Exception:
                continue
            meta = item.evaluate(
                """(el) => ({
                    placeholder: el.getAttribute('placeholder') || '',
                    aria: el.getAttribute('aria-label') || '',
                    name: el.getAttribute('name') || '',
                    id: el.getAttribute('id') || '',
                    cls: el.getAttribute('class') || ''
                })"""
            )
            hint = " ".join([str(meta.get("placeholder", "")), str(meta.get("aria", "")), str(meta.get("name", "")), str(meta.get("id", "")), str(meta.get("cls", ""))]).lower()
            if any(token in hint for token in ["简介", "介绍", "bio", "desc"]):
                return item
            if best is None:
                best = item
        return best
    except Exception:
        return None


def _assert_session(page) -> None:
    url = (page.url or "").lower()
    if any(token in url for token in ["login", "passport"]):
        raise RuntimeError("session appears invalid: redirected to login")
    if _click_text_any(page, ["登录", "立即登录", "扫码登录"], timeout_ms=1000):
        raise RuntimeError("session appears invalid: login prompt found")


def check_session(storage_state: Path, headless: bool = True) -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context(storage_state=str(storage_state))
        page = context.new_page()
        page.goto(DEFAULT_CREATOR_URL, wait_until="domcontentloaded", timeout=60000)
        _wait_stable(page, sleep_sec=1.0)
        _assert_session(page)
        context.close()
        browser.close()


def update_bio(storage_state: Path, bio_text: str, headless: bool = False) -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context(storage_state=str(storage_state), viewport={"width": 1440, "height": 960})
        page = context.new_page()
        page.goto("https://www.xiaohongshu.com", wait_until="domcontentloaded", timeout=60000)
        _wait_stable(page, sleep_sec=1.2)
        _assert_session(page)

        _click_text_any(page, ["我", "我的", "个人主页"], timeout_ms=4000)
        _wait_stable(page, sleep_sec=0.8)
        _click_text_any(page, ["编辑资料", "编辑信息", "编辑个人资料"], timeout_ms=5000)
        _wait_stable(page, sleep_sec=0.8)

        for url in [
            "https://www.xiaohongshu.com/user/profile/edit",
            "https://www.xiaohongshu.com/user/setting",
            "https://creator.xiaohongshu.com/personal/home",
        ]:
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=15000)
                _wait_stable(page, sleep_sec=0.8)
            except Exception:
                continue

        input_locator = _find_bio_editor(page)
        if input_locator is None:
            _debug_artifact(page, "update-bio-no-input")
            raise RuntimeError("cannot find bio input field")
        _fill_any_input(input_locator, bio_text)

        if not _click_text_any(page, ["保存", "完成", "确定"], timeout_ms=5000):
            _debug_artifact(page, "update-bio-no-save")
            raise RuntimeError("cannot find save button for bio")
        _wait_stable(page, sleep_sec=1.2)

        context.close()
        browser.close()


def publish(
    storage_state: Path,
    title: str,
    content: str,
    headless: bool = True,
    entry_url: str = "",
    image_paths: Optional[List[Path]] = None,
    publish_mode: str = "image_note",
    image_strategy: str = "text_card",
    topics: Optional[List[str]] = None,
    keep_open_on_fail: bool = False,
    hold_seconds_on_fail: int = 1800,
    preview_only: bool = False,
    hold_seconds_preview: int = 0,
) -> None:
    target_url = entry_url.strip() or DEFAULT_CREATOR_URL
    images = image_paths or []
    topic_list = topics or []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context(storage_state=str(storage_state), viewport={"width": 1440, "height": 960})
        page = context.new_page()
        page.set_default_timeout(DEFAULT_TIMEOUT_MS)
        failed = False
        try:
            page.goto(target_url, wait_until="domcontentloaded", timeout=60000)
            _wait_stable(page, sleep_sec=1.5)
            _assert_session(page)
            if publish_mode == "long_article":
                _run_long_article_flow(page, title, content, topic_list, submit_publish=not preview_only)
            else:
                _run_image_note_flow(
                    page,
                    title,
                    content,
                    images,
                    image_strategy,
                    topic_list,
                    submit_publish=not preview_only,
                )
            if preview_only:
                print("[xhs] preview-only: note created and ready for manual review", file=sys.stderr)
                if not headless:
                    _hold_browser_on_failure(page, hold_seconds_preview)
                return
            print("[xhs] step8: verify published note in 笔记管理", file=sys.stderr)
            _verify_publish_in_note_management(page, title, timeout_sec=75)
            print("[xhs] step8 done: publish verified", file=sys.stderr)
        except NotFoundStepError as exc:
            failed = True
            _debug_artifact(page, "publish-failed-not-found")
            if keep_open_on_fail and not headless:
                _hold_browser_on_failure(page, hold_seconds_on_fail)
            raise RuntimeError(f"not_found: {exc}") from exc
        except Exception as exc:
            failed = True
            _debug_artifact(page, "publish-failed")
            if keep_open_on_fail and not headless:
                _hold_browser_on_failure(page, hold_seconds_on_fail)
            raise RuntimeError(f"publish failed: {exc}") from exc
        finally:
            if not (failed and keep_open_on_fail and not headless):
                context.close()
                browser.close()


def comment(
    storage_state: Path,
    topic: str,
    comment_text: str,
    headless: bool = True,
    browse_url: str = DEFAULT_EXPLORE_URL,
) -> None:
    target_browse_url = (browse_url or "").strip() or DEFAULT_EXPLORE_URL
    parsed = urlparse(target_browse_url)
    origin = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else "https://www.xiaohongshu.com"
    search_url = f"{origin}/search_result?keyword={quote_plus(topic)}"
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context(storage_state=str(storage_state), viewport={"width": 1440, "height": 960})
        page = context.new_page()
        page.set_default_timeout(DEFAULT_TIMEOUT_MS)
        page.goto(target_browse_url, wait_until="domcontentloaded", timeout=60000)
        _wait_stable(page, sleep_sec=1.0)
        _assert_session(page)
        page.goto(search_url, wait_until="domcontentloaded", timeout=60000)
        _wait_stable(page, sleep_sec=1.2)
        _assert_session(page)

        note_link = _first_visible(
            page,
            [
                "a[href*='/explore/']",
                "section a[href*='xiaohongshu.com/explore']",
                "a[href*='note']",
            ],
        )
        if note_link is None:
            raise RuntimeError("cannot find target note link from search result")

        with context.expect_page(timeout=DEFAULT_TIMEOUT_MS) as new_page_info:
            note_link.click()
        note_page = new_page_info.value
        note_page.wait_for_load_state("domcontentloaded", timeout=60000)
        _wait_stable(note_page, sleep_sec=1.0)

        input_locator = _first_visible(
            note_page,
            [
                "textarea[placeholder*='说点什么']",
                "textarea[placeholder*='评论']",
                "[contenteditable='true'][placeholder*='说点什么']",
                "textarea",
            ],
        )
        if input_locator is None:
            raise RuntimeError("cannot find comment input")
        input_locator.click()
        input_locator.fill(comment_text[:200])

        if not _click_text_any(note_page, ["发送", "发布", "评论"], timeout_ms=5000):
            raise RuntimeError("cannot find comment submit button")
        _wait_stable(note_page, sleep_sec=1.5)

        context.close()
        browser.close()


def _storage_path(path_value: str) -> Path:
    path = Path(path_value).expanduser()
    if not path.exists():
        raise FileNotFoundError(f"storage state file not found: {path}")
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description="Xiaohongshu web operator via Playwright.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    check_parser = subparsers.add_parser("check-session")
    check_parser.add_argument("--storage-state", required=True)
    check_parser.add_argument("--headful", action="store_true")

    bio_parser = subparsers.add_parser("update-bio")
    bio_parser.add_argument("--storage-state", required=True)
    bio_parser.add_argument("--bio-text", required=True)
    bio_parser.add_argument("--headless", action="store_true")

    publish_parser = subparsers.add_parser("publish")
    publish_parser.add_argument("--storage-state", required=True)
    publish_parser.add_argument("--title", required=True)
    publish_parser.add_argument("--content", required=True)
    publish_parser.add_argument("--entry-url", default="")
    publish_parser.add_argument("--images", default="", help="Comma-separated local image paths")
    publish_parser.add_argument("--topics", default="", help="Comma-separated optional topics")
    publish_parser.add_argument(
        "--publish-mode",
        default="image_note",
        choices=["image_note", "long_article"],
        help="Publish mode: image note or long article",
    )
    publish_parser.add_argument(
        "--image-strategy",
        default="text_card",
        choices=["upload", "text_card"],
        help="For image_note mode: upload local images or generate text cards",
    )
    publish_parser.add_argument(
        "--keep-open-on-fail",
        action="store_true",
        help="When publish fails in headful mode, keep browser window open for debugging",
    )
    publish_parser.add_argument(
        "--hold-seconds-on-fail",
        type=int,
        default=int(os.getenv("XHS_HOLD_SECONDS_ON_FAIL", "1800")),
        help="Debug hold seconds after failure when --keep-open-on-fail is enabled; 0 means wait until window is closed manually",
    )
    publish_parser.add_argument(
        "--preview-only",
        action="store_true",
        help="Create note and stop before clicking publish",
    )
    publish_parser.add_argument(
        "--hold-seconds-preview",
        type=int,
        default=0,
        help="When --preview-only and --headful, keep browser open for preview; 0 means wait until closed manually",
    )
    publish_parser.add_argument("--headful", action="store_true")

    comment_parser = subparsers.add_parser("comment")
    comment_parser.add_argument("--storage-state", required=True)
    comment_parser.add_argument("--topic", required=True)
    comment_parser.add_argument("--comment", required=True)
    comment_parser.add_argument("--browse-url", default=DEFAULT_EXPLORE_URL)
    comment_parser.add_argument("--headful", action="store_true")

    args = parser.parse_args()
    try:
        if args.command == "check-session":
            check_session(_storage_path(args.storage_state), headless=not args.headful)
        elif args.command == "update-bio":
            update_bio(_storage_path(args.storage_state), args.bio_text, headless=args.headless)
        elif args.command == "publish":
            publish(
                _storage_path(args.storage_state),
                args.title,
                args.content,
                headless=not args.headful,
                entry_url=args.entry_url,
                image_paths=_parse_images_csv(args.images),
                publish_mode=args.publish_mode,
                image_strategy=args.image_strategy,
                topics=_parse_topics_csv(args.topics),
                keep_open_on_fail=args.keep_open_on_fail,
                hold_seconds_on_fail=args.hold_seconds_on_fail,
                preview_only=args.preview_only,
                hold_seconds_preview=args.hold_seconds_preview,
            )
        elif args.command == "comment":
            comment(
                _storage_path(args.storage_state),
                args.topic,
                args.comment,
                headless=not args.headful,
                browse_url=args.browse_url,
            )
        else:
            raise RuntimeError(f"unknown command: {args.command}")
    except PlaywrightTimeoutError as exc:
        print(f"[xhs-web-operator] timeout: {exc}", file=sys.stderr)
        raise
    except Exception as exc:
        print(f"[xhs-web-operator] {type(exc).__name__}: {exc}", file=sys.stderr)
        raise


if __name__ == "__main__":
    main()
