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
    """Greedy word-wrap honouring explicit line breaks. Returns EVERY line — never drops
    text (a hard line cap silently swallowed the end of long captions); the caller shrinks
    the font until the whole block fits instead."""
    out = []
    for para in (text or "").replace("\r", "").split("\n"):
        if not para.strip():
            out.append("")                             # keep blank separator lines
            continue
        cur = ""
        for w in para.split():
            trial = (cur + " " + w).strip()
            if font_for.getlength(_ar_shape(trial)) <= max_w or not cur:
                cur = trial
            else:
                out.append(cur)
                cur = w
        if cur:
            out.append(cur)
    return out or [""]


def bake(image_path: str, text: str, pos: str = "center", box=None) -> bool:
    """Draw `text` onto the image in place, MATCHING the reference caption's size + place.

    `box` (when given) = {x, y, w, h} as fractions 0-1 of the frame — the caption's measured
    region in the source. The text is sized to FILL that box (wrapped to its width, largest
    font whose block still fits its height) and centred inside it, so the remix's caption
    lands exactly where the original's did at the same scale — instead of a giant centred
    block covering the subject. Without a box it falls back to a modest lower-third layout.
    """
    text = (text or "").strip()
    if not text:
        return True
    try:
        from PIL import Image, ImageDraw
        im = Image.open(image_path).convert("RGB")
        W, H = im.size
        d = ImageDraw.Draw(im)

        # ── Target region ──────────────────────────────────────────────────────────
        if isinstance(box, dict) and float(box.get("w") or 0) > 0.05 and float(box.get("h") or 0) > 0.01:
            bx = max(0.0, min(0.95, float(box.get("x", 0.08))))
            by = max(0.0, min(0.97, float(box.get("y", 0.62))))
            bw = max(0.15, min(1.0 - bx, float(box["w"])))
            bh = max(0.03, min(1.0 - by, float(box["h"])))
        else:
            # modest defaults — never the huge centred block
            bx, bw, bh = 0.08, 0.84, 0.14
            by = {"top": 0.06, "bottom": 0.80}.get(pos, 0.64)   # default = lower third

        max_w   = bw * W
        avail_h = bh * H
        cx      = (bx + bw / 2) * W

        # Largest font whose FULL wrapped block fits the box (width AND height). The whole
        # caption always survives — if it can't fit at any size we still draw every line at
        # the floor size (slight overflow beats losing words).
        floor = max(12, int(H * 0.014))
        best_size = floor
        lo, hi = floor, max(floor + 1, int(avail_h * 1.2))
        while lo <= hi:
            s = (lo + hi) // 2
            font = _load_bold_font(s, text)
            lines = _wrap(text, font, max_w)
            block_h = int(s * 1.25) * len(lines)
            widest = max((font.getlength(_ar_shape(ln)) for ln in lines), default=0)
            if block_h <= avail_h and widest <= max_w:
                best_size = s
                lo = s + 1
            else:
                hi = s - 1
        size = best_size
        font = _load_bold_font(size, text)
        lines = _wrap(text, font, max_w)          # re-wrap at the FINAL size

        line_h  = int(size * 1.25)
        block_h = line_h * len(lines)
        y0 = int(by * H + (avail_h - block_h) / 2)
        y0 = max(int(H * 0.01), min(y0, H - block_h - int(H * 0.01)))

        stroke = max(2, size // 12)
        for i, ln in enumerate(lines):
            if not ln.strip():                    # blank separator line — keep the gap only
                continue
            shaped = _ar_shape(ln)
            lw = font.getlength(shaped)
            x = cx - lw / 2
            y = y0 + i * line_h
            d.text((x, y), shaped, font=font, fill="white",
                   stroke_width=stroke, stroke_fill="black")

        im.save(image_path, "JPEG", quality=92)
        return True
    except Exception as e:
        log.warning(f"overlay_text bake failed for {Path(image_path).name}: {e}")
        return False


def bake_all(items) -> int:
    """items: [{path, text, pos, box?}] → bakes each; returns how many succeeded."""
    ok = 0
    for it in items or []:
        if bake(it.get("path", ""), it.get("text", ""), it.get("pos", "center"), it.get("box")):
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
