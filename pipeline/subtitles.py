"""Subtitle rendering — builds ASS subtitle files for ffmpeg burn-in.

Supports several styles and positions, selectable from the web UI:
  style    = "classic"  → short chunks appear/disappear (clean, readable)
             "karaoke"  → full phrase shown, each word highlights as it's spoken
             "word"     → one big word at a time (punchy TikTok style)
  position = "bottom"   → lower third (default)
             "top"      → upper third
             "smart"    → auto-placed in the image's negative space (calmer third)

ASS is burned by ffmpeg's `subtitles` filter (libass), so timing/animation is
handled by the renderer — no per-frame Python work.
"""

import logging

log = logging.getLogger(__name__)

_CLAUSE_BREAKS = {"and", "but", "or", "so", "because", "which",
                  "that", "when", "while", "if", "then"}

# ASS colours are &HAABBGGRR& (alpha, blue, green, red). 00 alpha = opaque.
_WHITE  = "&H00FFFFFF&"
_YELLOW = "&H0000FFFF&"   # highlight colour for karaoke
_BLACK  = "&H00000000&"


def _fmt_time(seconds: float) -> str:
    """Seconds → ASS timestamp H:MM:SS.cs (centiseconds)."""
    cs = max(0, int(round(seconds * 100)))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h:d}:{m:02d}:{s:02d}.{cs:02d}"


def _clean(text: str) -> str:
    """Strip invisible/bidi-control chars + punctuation/symbols, collapse all
    whitespace (tabs/newlines → single space). Punctuation & a stray tab were the
    two culprits behind boxes and the dropped line."""
    import unicodedata, re
    kept = []
    for c in (text or ""):
        o = ord(c)
        if (0x200B <= o <= 0x200F or 0x202A <= o <= 0x202E
                or 0x2060 <= o <= 0x2069 or o in (0xFEFF, 0x00AD, 0x061C)):
            continue
        if unicodedata.category(c)[0] in ("P", "S"):
            continue
        kept.append(c)
    return re.sub(r"\s+", " ", "".join(kept)).strip()


def _brace_esc(text: str) -> str:
    """Escape the few chars that have meaning in an ASS dialogue field."""
    return text.replace("\\", "\\\\").replace("{", "(").replace("}", ")")


def _esc(text: str) -> str:
    """Clean + ASS-escape (for LTR / per-word use)."""
    return _brace_esc(_clean(text))


def _shape(text: str) -> str:
    """Apply Arabic letter-joining (presentation forms). libass shapes per text-run,
    so a single word is shaped correctly; we pre-shape to control it explicitly."""
    try:
        import arabic_reshaper
        return arabic_reshaper.reshape(text)
    except Exception:
        return text


def _shape_visual(text: str) -> str:
    """Logical → VISUAL order Arabic: reshape (join) then apply the Unicode bidi
    algorithm. Handles direction AND embedded numbers ('200' stays '200', placed
    correctly). libass renders the result as-given (it isn't reordering here)."""
    try:
        import arabic_reshaper
        from bidi.algorithm import get_display
        return get_display(arabic_reshaper.reshape(text))
    except Exception:
        return text


# Fonts with no Arabic glyphs — used for Arabic text they'd render boxes (tofu).
_LATIN_ONLY_FONTS = {"Impact", "Arial Black", "Bahnschrift"}


def _is_rtl(text: str) -> bool:
    """True if the text contains Arabic/Hebrew (right-to-left) characters."""
    return any("֐" <= c <= "޿" or "יִ" <= c <= "ﻼ" for c in (text or ""))


def analyze_negative_space(img_path: str) -> str:
    """Return 'top' or 'bottom' — whichever third of the image is calmer
    (lower detail/variance), i.e. the best negative space for text."""
    try:
        from PIL import Image
        import numpy as np
        im  = Image.open(img_path).convert("L").resize((64, 64))
        arr = np.asarray(im, dtype="float32")
        top_busy = float(arr[:21].std())
        bot_busy = float(arr[43:].std())
        return "top" if top_busy < bot_busy else "bottom"
    except Exception:
        return "bottom"


def _alignment_and_margin(position: str, img_path, H: int):
    pos = position
    if position == "smart":
        pos = analyze_negative_space(img_path) if img_path else "bottom"
    if pos == "top":
        return 8, max(40, int(H * 0.07))     # top-centre
    if pos in ("center", "middle"):
        return 5, 0                          # dead-centre
    return 2, max(40, int(H * 0.08))         # bottom-centre (default)


def _placement(v_pct, smart, position, img_path, H: int):
    """Resolve vertical placement → (alignment, marginV).
    v_pct = exact % from the top (0=top, 100=bottom). smart = auto negative space.
    Falls back to the legacy position string when v_pct is None."""
    if smart:
        side = analyze_negative_space(img_path) if img_path else "bottom"
        v_pct = 10 if side == "top" else 82
    if v_pct is not None:
        v = max(4.0, min(92.0, float(v_pct)))
        return 8, int(v / 100.0 * H)         # top-anchored at the chosen height
    return _alignment_and_margin(position, img_path, H)


def build_segment_ass(text: str, duration: float, style: str = "classic",
                      position: str = "bottom", W: int = 1920, H: int = 1080,
                      img_path: str = None, font: str = "Arial", words=None,
                      animation: str = "pop", font_scale: float = 1.0,
                      v_pct=None, smart: bool = False, words_per_cue: int = 3) -> str:
    """Build a complete ASS file body for one segment spanning [0, duration].
    If `words` (list of {text,start,dur} from the TTS) is given, timing is taken
    from the real spoken word boundaries (100% sync); otherwise it's distributed
    proportionally across `duration`.
    Returns None if there is nothing to render."""
    text = (text or "").strip()
    if (not text and not words) or duration <= 0:
        return None

    font  = font or "Arial"
    # Arabic/RTL: the assembler runs on Linux (VPS), where Arial/Tahoma don't exist,
    # so libass falls back to a wide font (DejaVu) that renders Arabic horizontally
    # stretched. We ship Tajawal (fonts/Tajawal-Bold.ttf) and libass loads it via
    # `fontsdir=fonts`, so Arabic always renders with correct metrics on any OS.
    if _is_rtl(text):
        font = "Tajawal"
    style = style if style in ("classic", "karaoke", "word", "active") else "classic"
    align, margin_v = _placement(v_pct, smart, position, img_path, H)

    # Explicit \pos for every cue → libass does NOT run collision avoidance, so a new
    # cue is never shoved off an outgoing one. Without this, whenever two cues are on
    # screen together for even a frame (real TTS timings overlap slightly; fades extend
    # visibility), libass stacks the newer one upward — making each sentence appear to
    # "start low and climb up", then reset on the next sentence. \pos pins it. The pos
    # is the alignment anchor point matching the Style's Alignment + MarginV.
    _ml = _mr = 60
    if align in (1, 4, 7):      pos_x = _ml
    elif align in (3, 6, 9):    pos_x = W - _mr
    else:                       pos_x = W // 2
    if align in (7, 8, 9):      pos_y = margin_v
    elif align in (4, 5, 6):    pos_y = H // 2
    else:                       pos_y = H - margin_v
    pos_tag = "{\\pos(%d,%d)}" % (pos_x, pos_y)

    scale   = max(0.3, min(2.2, float(font_scale or 1.0)))
    base_fs = max(20, int(H * (0.075 if style == "word" else 0.052) * scale))
    outline = max(2, int(base_fs * 0.10))

    # karaoke: unsung words white → highlight to yellow as spoken.
    # classic/word: plain white text (PrimaryColour is what non-karaoke text uses).
    primary, secondary = (_YELLOW, _WHITE) if style == "karaoke" else (_WHITE, _WHITE)

    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {W}\nPlayResY: {H}\n"
        "WrapStyle: 2\nScaledBorderAndShadow: yes\n\n"   # 2 = no auto-wrap → single line
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,{font},{base_fs},{primary},{secondary},{_BLACK},{_BLACK},"
        f"-1,0,0,0,100,100,0,0,1,{outline},1,{align},60,60,{margin_v},1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )

    # Width-based char cap → guarantees the cue fits on ONE line at this font size.
    max_chars = max(6, int((W * 0.90) / (base_fs * 0.50)))
    # Short, speech-synced, animated chunks (CapCut/Submagic style).
    lines = _events_capcut(text, duration, style, words, _anim_prefix(animation),
                           max_chars, words_per_cue, pos_tag)
    if not lines:
        return None
    return header + "\n".join(lines) + "\n"


def _anim_prefix(animation: str) -> str:
    """libass override tags for a cue's entrance/emphasis animation."""
    _POP = r"{\fad(70,40)\fscx82\fscy82\t(0,130,\fscx100\fscy100)}"
    return {
        "none":   r"{\fad(40,40)}",
        "fade":   r"{\fad(220,140)}",
        "pop":    _POP,
        "bounce": r"{\fad(60,40)\fscx55\fscy55\t(0,120,\fscx112\fscy112)\t(120,230,\fscx100\fscy100)}",
        "grow":   r"{\fad(80,50)\fscx92\fscy92\t(0,180,\fscx100\fscy100)}",
    }.get((animation or "pop").lower(), _POP)

# Soft punctuation: a natural place to end a chunk early.
_BREAK_AFTER = (",", "؛", "،", ":", "—", "–", ";")


def _sentences_with_words(text: str, duration: float, marks):
    """Return a list of sentences, each a list of {text,start,dur} words anchored
    to real spoken time. Uses TTS sentence boundaries when present; otherwise
    splits the text and spreads `duration` across sentences proportionally."""
    usable = [m for m in (marks or []) if str(m.get("text", "")).strip()]
    if usable and any(len(str(m.get("text", "")).split()) > 1 for m in usable):
        out = []
        for m in usable:
            s = float(m.get("start", 0.0))
            d = max(float(m.get("dur", 0.0)), 0.1)
            out.append(_distribute_words(str(m["text"]).strip(), s, d))
        return out
    parts = _split_sentences(text) or ([text] if text else [])
    if not parts:
        return []
    weights = [max(len(p), 1) for p in parts]
    total   = sum(weights)
    out, t  = [], 0.0
    for p, w in zip(parts, weights):
        d = duration * (w / total)
        out.append(_distribute_words(p, t, d))
        t += d
    return out


def _chunk_words(words, max_words: int, max_chars: int):
    """Group one sentence's words into small phrase chunks (≤max_words, ≤max_chars),
    breaking early after soft punctuation. Returns lists of word indices."""
    chunks, cur, cur_chars = [], [], 0
    for i, w in enumerate(words):
        cur.append(i)
        cur_chars += len(w["text"]) + 1
        if (len(cur) >= max_words or cur_chars >= max_chars
                or w["text"].endswith(_BREAK_AFTER)):
            chunks.append(cur); cur, cur_chars = [], 0
    if cur:
        chunks.append(cur)
    return chunks


# "Active word" emphasis: spoken word grows + turns yellow, then settles; others
# stay small & white. (\c uses &HBBGGRR& → white=FFFFFF, yellow=00FFFF.)
_ACTIVE = r"{\c&H00FFFF&\fscx112\fscy112\t(0,90,\fscx132\fscy132)\t(90,210,\fscx120\fscy120)}"
_NORMAL = r"{\c&HFFFFFF&\fscx100\fscy100}"


def _events_capcut(text: str, duration: float, style: str, marks, anim: str,
                   max_chars: int = 24, words_per_cue: int = 3, pos_tag: str = ""):
    """Short, animated, speech-synced caption cues (CapCut / Submagic style).
    `words_per_cue` controls how many words show at once; `max_chars` keeps each
    cue on a single line."""
    rtl = _is_rtl(text)   # Arabic/Hebrew → lay words right-to-left (libass isn't reordering here)
    if rtl and style == "karaoke":
        # karaoke's \k always fills in render (left-to-right) order, which can't match
        # right-to-left speech — use the active word-highlight, which IS RTL-correct.
        style = "active"
    max_words = 1 if style == "word" else max(1, min(6, int(words_per_cue or 3)))

    sentences = _sentences_with_words(text, duration, marks)
    if not sentences:
        return []

    lines = []
    for ws in sentences:
        if not ws:
            continue
        n = len(ws)
        chunks = _chunk_words(ws, max_words, max_chars)
        for ci, idxs in enumerate(chunks):
            start = ws[idxs[0]]["start"]
            # End at next chunk's start within a sentence (continuous); the last
            # chunk ends at sentence end → pauses between sentences show nothing.
            if ci + 1 < len(chunks):
                phrase_end = ws[chunks[ci + 1][0]]["start"]
            else:
                phrase_end = ws[idxs[-1]]["start"] + max(ws[idxs[-1]]["dur"], 0.12)
            if phrase_end <= start:
                phrase_end = start + 0.3

            if style == "active":
                # The whole phrase stays on screen; ONE word at a time grows +
                # highlights exactly when it's spoken, then shrinks for the next.
                # RTL: lay the words right-to-left so the highlight moves the correct
                # way. libass shapes each word's letters correctly (harfbuzz) inside its
                # own run, but does NOT reorder across the per-word override runs, so we
                # reverse the word ORDER ourselves. We must NOT reshape/bidi the text —
                # libass already does that; pre-shaping double-processes it (reversed,
                # unreadable letters).
                disp = list(reversed(idxs)) if rtl else idxs
                def _wt(j):
                    return _esc(ws[j]["text"])
                for k, wi in enumerate(idxs):
                    a_start = ws[wi]["start"]
                    a_end   = ws[wi + 1]["start"] if (k + 1 < len(idxs)) else phrase_end
                    if a_end <= a_start:
                        a_end = a_start + 0.18
                    parts = [(_ACTIVE if j == wi else _NORMAL) + _wt(j) for j in disp]
                    # No whole-cue pop here — it shifted the first cue of each phrase
                    # one line down (top-anchored scale grows downward). The per-word
                    # grow IS the animation.
                    body  = pos_tag + " ".join(parts)
                    lines.append(f"Dialogue: 0,{_fmt_time(a_start)},{_fmt_time(a_end)},Default,,0,0,0,,{body}")

            elif style == "karaoke" and len(idxs) > 1:
                parts = []
                for wi in idxs:
                    nxt  = ws[wi + 1]["start"] if wi + 1 < n else (ws[wi]["start"] + ws[wi]["dur"])
                    kdur = max(0.05, nxt - ws[wi]["start"])
                    parts.append(f"{{\\k{max(1, int(round(kdur * 100)))}}}{_esc(ws[wi]['text'])}")
                body = pos_tag + anim + " ".join(parts)
                lines.append(f"Dialogue: 0,{_fmt_time(start)},{_fmt_time(phrase_end)},Default,,0,0,0,,{body}")

            else:
                # Classic/word: feed RAW (cleaned) text. The cue is one continuous run,
                # so libass applies its own Unicode bidi (fribidi) + shaping (harfbuzz):
                # correct RTL word order, correct letter joining, and numbers placed
                # properly. We must NOT pre-reshape/bidi — that double-reverses it.
                joined = " ".join(ws[wi]["text"] for wi in idxs)
                body = pos_tag + anim + _esc(joined)
                lines.append(f"Dialogue: 0,{_fmt_time(start)},{_fmt_time(phrase_end)},Default,,0,0,0,,{body}")
    return lines


def _split_sentences(text: str):
    """Split text into sentences, keeping the terminal punctuation."""
    import re
    text = (text or "").strip()
    if not text:
        return []
    parts = re.split(r"(?<=[.!?؟۔])\s+", text)
    return [p.strip() for p in parts if p.strip()]


def _distribute_words(text: str, start: float, dur: float):
    """Spread a sentence's words across [start, start+dur], proportional to length."""
    parts = text.split()
    if not parts:
        return []
    weights = [max(len(p), 1) for p in parts]
    total   = sum(weights)
    out, t  = [], start
    for p, w in zip(parts, weights):
        wd = dur * (w / total)
        out.append({"text": p, "start": t, "dur": max(wd, 0.05)})
        t += wd
    return out
