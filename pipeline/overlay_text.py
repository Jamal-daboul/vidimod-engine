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


def make_card(inner_path: str, out_path: str, cfg: dict) -> bool:
    """Reproduce a MEME-CARD layout: a coloured vertical canvas with the generated image
    as a horizontal strip in the middle and a caption in the margin — the exact "video
    inside white space with a caption" format of viral reaction memes.

    cfg: {canvas_w, canvas_h, inner_frac_h, header, header_pos ('top'|'bottom'), bg '#RRGGBB'}
    Writes the composited card to out_path. Returns True on success.
    """
    try:
        from PIL import Image, ImageDraw
        CW = int(cfg.get("canvas_w") or 1080)
        CH = int(cfg.get("canvas_h") or 1920)
        # normalize to a sane vertical size (keep aspect)
        if CH > 1920:
            CW = int(CW * 1920 / CH); CH = 1920
        bg = cfg.get("bg") or "#FFFFFF"
        try:
            bgc = tuple(int(bg.lstrip("#")[k:k + 2], 16) for k in (0, 2, 4))
        except Exception:
            bgc = (255, 255, 255)
        canvas = Image.new("RGB", (CW, CH), bgc)

        inner = Image.open(inner_path).convert("RGB")
        # inner image spans the full canvas width; its height follows its own aspect
        iw = CW
        ih = max(1, int(inner.height * CW / inner.width))
        inner = inner.resize((iw, ih), Image.LANCZOS)
        # vertical position: centred, but leave room for the header in its margin
        header = (cfg.get("header") or "").strip()
        hpos = cfg.get("header_pos", "top")
        if ih >= CH:                                    # inner taller than canvas → just center-crop
            y = (CH - ih) // 2
        elif header:
            y = int(CH * 0.20) if hpos == "top" else int(CH * 0.80) - ih
            y = max(int(CH * 0.14), min(y, CH - ih - int(CH * 0.14)))
        else:
            y = (CH - ih) // 2
        canvas.paste(inner, (0, y))

        if header:
            d = ImageDraw.Draw(canvas)
            size = max(30, int(CW * 0.052))
            max_w = int(CW * 0.9)
            while size > 22:
                font = _load_bold_font(size, header)
                if font.getlength(_ar_shape(header)) <= max_w:
                    break
                size -= 3
            font = _load_bold_font(size, header)
            shaped = _ar_shape(header)
            tw = font.getlength(shaped)
            tx = (CW - tw) / 2
            # place text in the empty margin above / below the image
            if hpos == "top":
                ty = max(int(CH * 0.03), (y - size) // 2)
            else:
                ty = min(CH - int(size * 1.6), y + ih + (CH - (y + ih) - size) // 2)
            ink = (17, 17, 17) if sum(bgc) > 380 else (255, 255, 255)   # dark text on light bg
            d.text((tx, ty), shaped, font=font, fill=ink)

        canvas.save(out_path, "JPEG", quality=92)
        return True
    except Exception as e:
        log.warning(f"make_card failed for {Path(inner_path).name}: {e}")
        return False


def make_cards(items) -> int:
    """items: [{inner, out, cfg}] → composite each card; returns how many succeeded."""
    ok = 0
    for it in items or []:
        if make_card(it.get("inner", ""), it.get("out", ""), it.get("cfg", {})):
            ok += 1
    return ok
