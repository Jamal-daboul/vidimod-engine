"""Bake on-screen text onto still images (viral-remix mode).

The remix flow reproduces a reference video's on-screen text on the NEWLY generated
images — same words, roughly the same zone (top / center / bottom). Reuses the
Arabic-safe drawing stack from step4_assemble (_load_bold_font + _ar_shape), so
Arabic renders with correct joined letters, not tofu.

Style: big bold white text with a heavy dark stroke — the standard viral-shorts
look that stays readable on any footage.
"""

import logging
from pathlib import Path

log = logging.getLogger(__name__)

from pipeline.step4_assemble import _load_bold_font, _ar_shape


def _wrap(text: str, font_for, max_w: int):
    """Greedy word-wrap: returns a list of lines that each fit max_w px."""
    words = (text or "").split()
    lines, cur = [], ""
    for w in words:
        trial = (cur + " " + w).strip()
        if font_for.getlength(_ar_shape(trial)) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines[:4]                                   # never more than 4 lines


def bake(image_path: str, text: str, pos: str = "center") -> bool:
    """Draw `text` onto the image in place. pos: top | center | bottom."""
    text = (text or "").strip()
    if not text:
        return True
    try:
        from PIL import Image, ImageDraw
        im = Image.open(image_path).convert("RGB")
        W, H = im.size
        d = ImageDraw.Draw(im)

        # Font size scales with frame width; shrink until the longest line fits.
        size = max(28, int(W * 0.075))
        max_w = int(W * 0.86)
        while size > 20:
            font = _load_bold_font(size, text)
            lines = _wrap(text, font, max_w)
            if all(font.getlength(_ar_shape(ln)) <= max_w for ln in lines):
                break
            size -= 4
        font = _load_bold_font(size, text)
        lines = _wrap(text, font, max_w)

        line_h = int(size * 1.25)
        block_h = line_h * len(lines)
        y0 = {"top":    int(H * 0.08),
              "bottom": int(H * 0.88) - block_h,
              }.get(pos, (H - block_h) // 2)           # default: center
        y0 = max(int(H * 0.04), min(y0, H - block_h - int(H * 0.04)))

        stroke = max(3, size // 11)
        for i, ln in enumerate(lines):
            shaped = _ar_shape(ln)
            lw = font.getlength(shaped)
            x = (W - lw) / 2
            y = y0 + i * line_h
            d.text((x, y), shaped, font=font, fill="white",
                   stroke_width=stroke, stroke_fill="black")

        im.save(image_path, "JPEG", quality=92)
        return True
    except Exception as e:
        log.warning(f"overlay_text bake failed for {Path(image_path).name}: {e}")
        return False


def bake_all(items) -> int:
    """items: [{path, text, pos}] → bakes each; returns how many succeeded."""
    ok = 0
    for it in items or []:
        if bake(it.get("path", ""), it.get("text", ""), it.get("pos", "center")):
            ok += 1
    return ok
