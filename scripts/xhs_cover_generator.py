#!/usr/bin/env python3
import argparse
import datetime as dt
from pathlib import Path
from typing import List

from PIL import Image, ImageDraw, ImageFont


CANVAS_WIDTH = 1080
CANVAS_HEIGHT = 1440
PADDING_X = 88
TITLE_MAX_WIDTH = CANVAS_WIDTH - PADDING_X * 2


def _pick_font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    candidates: List[str] = []
    if bold:
        candidates.extend(
            [
                "/System/Library/Fonts/PingFang.ttc",
                "/System/Library/Fonts/Hiragino Sans GB.ttc",
                "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
            ]
        )
    else:
        candidates.extend(
            [
                "/System/Library/Fonts/PingFang.ttc",
                "/System/Library/Fonts/Hiragino Sans GB.ttc",
                "/System/Library/Fonts/Supplemental/Arial.ttf",
            ]
        )
    for font_path in candidates:
        path = Path(font_path)
        if not path.exists():
            continue
        try:
            return ImageFont.truetype(str(path), size=size)
        except Exception:
            continue
    return ImageFont.load_default()


def _wrap_lines(text: str, font: ImageFont.ImageFont, max_width: int) -> List[str]:
    chars = list((text or "").strip())
    if not chars:
        return ["Untitled"]
    lines: List[str] = []
    current = ""
    for char in chars:
        test = f"{current}{char}"
        bbox = font.getbbox(test)
        width = bbox[2] - bbox[0]
        if width <= max_width or not current:
            current = test
        else:
            lines.append(current)
            current = char
    if current:
        lines.append(current)
    return lines[:4]


def generate_cover(title: str, date_text: str, keyword: str, output_path: Path) -> Path:
    image = Image.new("RGB", (CANVAS_WIDTH, CANVAS_HEIGHT), "#FFF8EE")
    draw = ImageDraw.Draw(image)

    draw.rectangle((0, 0, CANVAS_WIDTH, 180), fill="#111111")
    draw.rectangle((0, CANVAS_HEIGHT - 240, CANVAS_WIDTH, CANVAS_HEIGHT), fill="#F2E7D5")

    title_font = _pick_font(size=72, bold=True)
    subtitle_font = _pick_font(size=40, bold=False)
    badge_font = _pick_font(size=34, bold=True)

    lines = _wrap_lines(title, title_font, TITLE_MAX_WIDTH)
    y = 280
    for line in lines:
        draw.text((PADDING_X, y), line, font=title_font, fill="#1F1F1F")
        bbox = title_font.getbbox(line)
        line_height = bbox[3] - bbox[1]
        y += line_height + 18

    subtitle = f"{date_text} · AI 博主自动化"
    draw.text((PADDING_X, y + 26), subtitle, font=subtitle_font, fill="#4A4A4A")

    badge_text = f"#{(keyword or '效率增长').strip()}"
    badge_width = max(240, min(640, badge_font.getbbox(badge_text)[2] - badge_font.getbbox(badge_text)[0] + 64))
    badge_x = PADDING_X
    badge_y = CANVAS_HEIGHT - 170
    draw.rounded_rectangle((badge_x, badge_y, badge_x + badge_width, badge_y + 78), radius=24, fill="#111111")
    draw.text((badge_x + 24, badge_y + 18), badge_text, font=badge_font, fill="#FFFFFF")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(output_path, format="PNG")
    return output_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate local Xiaohongshu cover image.")
    parser.add_argument("--title", required=True, help="Cover title text")
    parser.add_argument("--date", default=dt.date.today().isoformat(), help="Date text")
    parser.add_argument("--keyword", default="效率增长", help="Theme keyword")
    parser.add_argument("--output", required=True, help="Output png path")
    args = parser.parse_args()

    output = Path(args.output).expanduser()
    result = generate_cover(args.title.strip(), args.date.strip(), args.keyword.strip(), output)
    print(str(result))


if __name__ == "__main__":
    main()
