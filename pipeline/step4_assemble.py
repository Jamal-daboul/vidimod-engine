"""Step 4 - Assemble final video from AI images using MoviePy + Pillow."""

import json
import logging
import re
import numpy as np
from pathlib import Path

log = logging.getLogger(__name__)

# Patch Pillow ANTIALIAS removal (Pillow >= 10)
try:
    from PIL import Image
    if not hasattr(Image, "ANTIALIAS"):
        Image.ANTIALIAS = Image.LANCZOS
except Exception:
    pass

W, H     = 1080, 1920
FADE     = 0.3
MAX_ZOOM = 1.15

SUBTITLE_FONT_SIZE = 90    # bigger
SUBTITLE_STROKE    = 8     # thicker stroke
SUBTITLE_Y_RATIO   = 0.50  # center of screen
SUBTITLE_MAX_CHARS = 30    # max chars per chunk before forcing a break

# Words that mark natural clause breaks in speech
_CLAUSE_BREAKS = {
    'and', 'but', 'or', 'so', 'yet', 'nor',
    'because', 'although', 'though', 'however',
    'since', 'while', 'when', 'that', 'which', 'who',
}


# ── Subtitle helpers ──────────────────────────────────────────────────────────

def _pil_arabic_mode() -> str:
    """How Arabic must be drawn with this Pillow install.
    'raqm'  → Pillow has the Raqm layout engine (bundled in Linux wheels since 8.2):
              draw the RAW logical text with direction='rtl' and let HarfBuzz shape
              it. REQUIRED for modern fonts like Noto Sans Arabic, which contain no
              legacy presentation-form codepoints — pre-shaped text renders as tofu
              boxes (final-alef, lam-alef…) on exactly those fonts.
    'shape' → no Raqm: pre-shape into presentation forms ourselves (arabic_reshaper
              + bidi) and draw with a font that still carries those legacy glyphs
              (Amiri does; Noto/Cairo/Tajawal don't)."""
    try:
        from PIL import features
        return 'raqm' if features.check('raqm') else 'shape'
    except Exception:
        return 'shape'


def _load_bold_font(size: int, text: str = ""):
    from PIL import ImageFont
    import os
    fonts_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fonts")
    is_ar = any('؀' <= c <= 'ۿ' for c in (text or ""))
    candidates = []
    if is_ar:
        # Arabic needs a font that actually contains Arabic glyphs — DejaVu / Arial-bold
        # on a headless Linux box don't, so they render tofu boxes. Use the bundled
        # Arabic fonts (these ship in the engine's fonts/ folder). Which one comes
        # first depends on HOW we draw (see _pil_arabic_mode): Raqm shapes modern
        # fonts (Noto) from raw text; the manual pre-shape path needs the legacy
        # presentation-form glyphs that only Amiri still includes.
        if _pil_arabic_mode() == 'raqm':
            candidates += [
                os.path.join(fonts_dir, "NotoSansArabic-Bold.ttf"),
                os.path.join(fonts_dir, "Cairo.ttf"),
                os.path.join(fonts_dir, "Tajawal.ttf"),
                os.path.join(fonts_dir, "Almarai.ttf"),
            ]
        else:
            candidates += [
                os.path.join(fonts_dir, "Amiri.ttf"),
                os.path.join(fonts_dir, "NotoSansArabic-Bold.ttf"),
                os.path.join(fonts_dir, "Cairo.ttf"),
                os.path.join(fonts_dir, "Tajawal.ttf"),
            ]
    candidates += [
        "arialbd.ttf",
        "C:/Windows/Fonts/arialbd.ttf",
        os.path.join(fonts_dir, "Montserrat.ttf"),
        "arial.ttf",
        "C:/Windows/Fonts/arial.ttf",
        "DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            pass
    return ImageFont.load_default()


def _ar_shape(s: str) -> str:
    """Reshape + bidi Arabic so PIL can draw it correctly. PIL has no Arabic shaper
    (unless libraqm is present), so we join the letters into their presentation forms
    and apply visual RTL order ourselves. Latin text is returned unchanged."""
    if not any('؀' <= c <= 'ۿ' for c in (s or "")):
        return s
    try:
        import arabic_reshaper
        from bidi.algorithm import get_display
        return get_display(arabic_reshaper.reshape(s))
    except Exception:
        return s


def _smart_chunks(text: str, audio_duration: float) -> list:
    """
    Split `text` into subtitle chunks tuned to speaking pace.

    Strategy (mirrors CapCut / viral Shorts style):
      1. Sentence-ending punctuation (.!?) always forces a break.
      2. Commas and clause-starting conjunctions break if chunk has ≥2 words.
      3. Chunk size cap adapts to words-per-second:
           >5 wps  (very fast) → max 4 words
           3-5 wps (fast)      → max 3 words
           2-3 wps (medium)    → max 2 words
           <2 wps  (slow)      → 1 word
      4. Hard character cap (SUBTITLE_MAX_CHARS) prevents overlong lines.

    Returns list of (label_str, word_count) pairs.
    """
    words = text.split()
    if not words:
        return []

    word_dur = audio_duration / len(words)

    # Pace-based word cap
    if word_dur < 0.20:
        max_wds = 4
    elif word_dur < 0.30:
        max_wds = 3
    elif word_dur < 0.50:
        max_wds = 2
    else:
        max_wds = 1

    chunks   = []
    current  = []
    cur_chars = 0

    for word in words:
        clean       = word.rstrip('.,!?;:')
        ends_sent   = bool(word) and word[-1] in '.!?'
        ends_clause = word.endswith(',') or clean.lower() in _CLAUSE_BREAKS

        current.append(word)
        cur_chars += len(word) + 1

        should_break = (
            ends_sent
            or (ends_clause and len(current) >= 2)
            or len(current) >= max_wds
            or cur_chars > SUBTITLE_MAX_CHARS
        )

        if should_break:
            chunks.append(current[:])
            current   = []
            cur_chars = 0

    if current:
        chunks.append(current)

    return [(' '.join(c), len(c)) for c in chunks]


def apply_subtitles_to_clip(clip, text: str, audio_duration: float):
    """
    Burn subtitles directly into every frame via clip.fl() — zero compositing overhead.
    Chunks are pre-built and pre-measured; each frame only does one dict lookup + one draw.
    """
    from PIL import Image as PILImage, ImageDraw

    chunk_pairs = _smart_chunks(text, audio_duration)
    if not chunk_pairs:
        return clip

    font     = _load_bold_font(SUBTITLE_FONT_SIZE, text)
    sw       = SUBTITLE_STROKE
    sub_y    = int(H * SUBTITLE_Y_RATIO)
    word_dur = audio_duration / max(len(text.split()), 1)

    # Arabic strategy (see _pil_arabic_mode): with Raqm we pass the RAW logical text
    # plus direction='rtl' and HarfBuzz shapes it against the font's OpenType tables.
    # Pre-shaping in that mode produces presentation-form codepoints that modern
    # Arabic fonts simply don't contain → tofu boxes. Only the no-Raqm fallback
    # pre-shapes (with Amiri, which still has those legacy glyphs).
    is_ar   = any('؀' <= c <= 'ۿ' for c in (text or ""))
    mode    = _pil_arabic_mode()
    draw_kw = {'direction': 'rtl', 'language': 'ar'} if (is_ar and mode == 'raqm') else {}

    # Pre-compute timing and x-position for every chunk
    timed_chunks = []   # (t_start, t_end, label, x)
    cursor = 0.0
    for label, wcount in chunk_pairs:
        dur = word_dur * wcount
        if is_ar and mode == 'shape':
            label = _ar_shape(label)   # manual join + RTL (no Raqm available)

        tmp  = PILImage.new("RGB", (1, 1))
        try:
            bbox = ImageDraw.Draw(tmp).textbbox((0, 0), label, font=font, **draw_kw)
        except Exception:
            draw_kw = {}
            label   = _ar_shape(label) if is_ar else label
            bbox = ImageDraw.Draw(tmp).textbbox((0, 0), label, font=font)
        tw   = bbox[2] - bbox[0]
        x    = max(0, (W - tw) // 2 - sw)

        timed_chunks.append((cursor, cursor + dur, label, x))
        cursor += dur

    def burn(get_frame, t):
        frame = get_frame(t)

        label = x = None
        for t0, t1, lbl, lx in timed_chunks:
            if t0 <= t < t1:
                label, x = lbl, lx
                break
        if label is None:
            return frame

        img  = PILImage.fromarray(frame.astype(np.uint8))
        draw = ImageDraw.Draw(img)

        # Measure height to vertically center the text block on sub_y
        tmp  = PILImage.new("RGB", (1, 1))
        bbox = ImageDraw.Draw(tmp).textbbox((0, 0), label, font=font, **draw_kw)
        th   = bbox[3] - bbox[1]
        y    = sub_y - th // 2

        draw.text(
            (x, y), label, font=font,
            fill=(255, 255, 255),
            stroke_width=sw,
            stroke_fill=(0, 0, 0),
            **draw_kw,
        )
        return np.array(img)

    return clip.fl(burn)


# ── Ken Burns effect ──────────────────────────────────────────────────────────

def make_ken_burns(img_path: str, duration: float):
    """Return an ImageClip with a slow zoom-in Ken Burns effect."""
    from moviepy.editor import ImageClip
    from PIL import Image as PILImage

    pil_src  = PILImage.open(img_path).convert("RGB")
    large    = pil_src.resize((int(W * MAX_ZOOM), int(H * MAX_ZOOM)), PILImage.LANCZOS)
    large_np = np.array(large)
    lw, lh   = large.size

    def zoom(gf, t):
        scale = 1.0 + (MAX_ZOOM - 1.0) * min(t / max(duration, 1e-6), 1.0)
        cw    = int(lw / scale)
        ch    = int(lh / scale)
        left  = (lw - cw) // 2
        top   = (lh - ch) // 2
        patch = large_np[top:top + ch, left:left + cw]
        return np.array(PILImage.fromarray(patch).resize((W, H), PILImage.BILINEAR))

    base = ImageClip(np.array(pil_src.resize((W, H), PILImage.LANCZOS)), duration=duration)
    return base.fl(zoom)


# ── Main assembly ─────────────────────────────────────────────────────────────

def run(script: dict) -> dict:
    log.info("=== STEP 4: Assemble ===")
    from moviepy.editor import AudioFileClip, concatenate_videoclips, ColorClip

    segments = script.get("audio_segments", [])
    images   = script.get("images") or script.get("clips", [])

    if not segments or not images:
        log.error("Missing audio segments or images")
        return script

    ts    = script.get("created_at", "").replace(":", "-").replace(".", "-")[:19]
    topic = script.get("topic", "")

    # ── Web-generated shorts: use the fast ffmpeg assembler (portrait 1080×1920) ──
    # Mirrors the long path → fast, and gives shorts the same subtitle styles.
    # Avoids the slow MoviePy Ken-Burns path that was timing out on multi-shot shorts.
    if script.get("web_generated"):
        from pipeline.step4_long import _assemble_web_long
        Path("output/videos").mkdir(parents=True, exist_ok=True)
        out_path = f"output/videos/final_{ts}.mp4"
        return _assemble_web_long(script, segments, out_path, ts, vid_w=W, vid_h=H)

    # ── Build lookup: shot_id → path ─────────────────────────────────────────
    shot_map = script.get("shot_map", {})

    img_map = {}
    for im in images:
        if isinstance(im, dict):
            img_map[(im.get("segment_type", "fact"), im.get("number", 0))] = im.get("path", "")

    def _resolve(stype, n):
        if stype == "hook":
            p = shot_map.get("hook")
        elif stype == "outro":
            p = shot_map.get("outro")
        else:
            p = shot_map.get(f"fact_{n}")
        if p and Path(p).exists():
            return p
        p = img_map.get((stype, n)) or img_map.get(("fact", n))
        if p and Path(p).exists():
            return p
        for path in shot_map.values():
            if path and Path(path).exists():
                return path
        return None

    log.info(f"shot_map keys: {list(shot_map.keys())}")
    log.info(f"audio segments: {[(s.get('type'), s.get('number')) for s in segments]}")

    final_clips = []

    for seg in segments:
        audio_path = seg.get("path")
        if not audio_path or not Path(audio_path).exists():
            continue

        stype    = seg.get("type", "fact")
        n        = seg.get("number", 0)
        img_path = _resolve(stype, n)

        log.info(f"  {stype}#{n} -> {Path(img_path).name if img_path else 'ColorClip'}")

        try:
            audio = AudioFileClip(audio_path)
            dur   = audio.duration + FADE

            if img_path and Path(img_path).exists():
                vid = make_ken_burns(img_path, dur)
            else:
                vid = ColorClip(size=(W, H), color=(10, 10, 30), duration=dur)

            clip = vid.fadein(FADE).fadeout(FADE)

            seg_text = seg.get("text", "")
            if seg_text and script.get("enable_subtitles", True):
                clip = apply_subtitles_to_clip(clip, seg_text, audio.duration)

            clip = clip.set_audio(audio)
            final_clips.append(clip)

        except Exception as e:
            log.warning(f"Segment {stype}{n} failed: {e}")
            continue

    if not final_clips:
        log.error("No clips assembled")
        return script

    out_path = f"output/videos/final_{ts}.mp4"
    Path("output/videos").mkdir(parents=True, exist_ok=True)

    try:
        full = concatenate_videoclips(final_clips, method="compose")

        # Mix background music
        try:
            from pipeline import step4b_music
            music_path = step4b_music.run(full.duration, ts, topic=topic)
            if music_path and Path(music_path).exists():
                from moviepy.editor import CompositeAudioClip
                music = AudioFileClip(music_path)
                music = music.subclip(0, min(music.duration, full.duration))
                music = music.volumex(0.12)
                orig_audio = full.audio
                if orig_audio is not None:
                    full = full.set_audio(CompositeAudioClip([orig_audio, music]))
                else:
                    full = full.set_audio(music)
                log.info(f"Music mixed at 12%: {Path(music_path).name}")
        except Exception as e:
            log.warning(f"Background music skipped: {e}")

        full.write_videofile(
            out_path,
            fps=30,
            codec="libx264",
            audio_codec="aac",
            verbose=False,
            logger=None,
        )
        for c in final_clips:
            try:
                c.close()
            except Exception:
                pass
        full.close()

        size = Path(out_path).stat().st_size // (1024 * 1024)
        log.info(f"Video ready: {out_path} ({size}MB)")
        script["final_video"] = out_path

    except Exception as e:
        log.error(f"Assembly failed: {e}")
        return script

    if script.get("script_path"):
        with open(script["script_path"], "w", encoding="utf-8") as f:
            json.dump(script, f, indent=2, ensure_ascii=False)

    return script
