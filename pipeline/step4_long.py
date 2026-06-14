"""Step 4L - Assemble long-form YouTube video (landscape 1920x1080)."""

import json
import logging
import numpy as np
from pathlib import Path

log = logging.getLogger(__name__)

W, H     = 1920, 1080
FADE     = 0.5
MAX_ZOOM = 1.08   # gentle zoom — sections are long

SUBTITLE_FONT_SIZE = 64
SUBTITLE_STROKE    = 6
SUBTITLE_Y_RATIO   = 0.88   # bottom area
WORDS_PER_CHUNK    = 6      # subtitle chunk size


def _load_font(size: int):
    from PIL import ImageFont
    candidates = [
        "arialbd.ttf", "C:/Windows/Fonts/arialbd.ttf",
        "arial.ttf",   "C:/Windows/Fonts/arial.ttf",
        "DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            pass
    return ImageFont.load_default()


def _make_ken_burns(img_path: str, duration: float):
    """Full Ken Burns — per-frame zoom. Slow but cinematic."""
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


def _make_static_clip(img_path: str, duration: float):
    """Fast static image — no per-frame processing. Used for web-generated long videos."""
    from moviepy.editor import ImageClip
    from PIL import Image as PILImage

    img = PILImage.open(img_path).convert("RGB")
    iw, ih = img.size
    # Centre-crop to 16:9
    target_ratio = W / H
    if (iw / ih) > target_ratio:
        new_w = int(ih * target_ratio)
        img = img.crop(((iw - new_w) // 2, 0, (iw - new_w) // 2 + new_w, ih))
    else:
        new_h = int(iw / target_ratio)
        img = img.crop((0, (ih - new_h) // 2, iw, (ih - new_h) // 2 + new_h))
    img = img.resize((W, H), PILImage.LANCZOS)
    return ImageClip(np.array(img), duration=duration)


def _apply_subtitles(clip, text: str, audio_duration: float):
    """Bottom-third subtitles burned into every frame."""
    from PIL import Image as PILImage, ImageDraw

    words = text.split()
    if not words:
        return clip

    font  = _load_font(SUBTITLE_FONT_SIZE)
    sw    = SUBTITLE_STROKE
    sub_y = int(H * SUBTITLE_Y_RATIO)

    # Group words into fixed-size chunks, breaking at sentence ends
    chunks  = []
    current = []
    for word in words:
        current.append(word)
        if len(current) >= WORDS_PER_CHUNK or word[-1] in ".!?,":
            chunks.append(" ".join(current))
            current = []
    if current:
        chunks.append(" ".join(current))

    if not chunks:
        return clip

    chunk_dur = audio_duration / len(chunks)
    timed     = [(i * chunk_dur, (i + 1) * chunk_dur, c) for i, c in enumerate(chunks)]

    def burn(get_frame, t):
        frame = get_frame(t)
        label = None
        for t0, t1, lbl in timed:
            if t0 <= t < t1:
                label = lbl
                break
        if not label:
            return frame

        img  = PILImage.fromarray(frame.astype(np.uint8))
        draw = ImageDraw.Draw(img)

        tmp  = PILImage.new("RGB", (1, 1))
        bbox = ImageDraw.Draw(tmp).textbbox((0, 0), label, font=font)
        tw   = bbox[2] - bbox[0]
        th   = bbox[3] - bbox[1]
        x    = max(0, (W - tw) // 2 - sw)
        y    = sub_y - th // 2

        draw.text(
            (x, y), label, font=font,
            fill=(255, 255, 255),
            stroke_width=sw,
            stroke_fill=(0, 0, 0),
        )
        return np.array(img)

    return clip.fl(burn)


def _fmt_ts(seconds: float) -> str:
    m = int(seconds // 60)
    s = int(seconds % 60)
    return f"{m:02d}:{s:02d}"


# ── Fast ffmpeg assembler (web-generated long videos) ────────────────────────

def _get_ffmpeg() -> str:
    """Return the ffmpeg binary path, reusing whatever MoviePy already found."""
    import shutil
    # 1. Ask MoviePy — it already located ffmpeg during install
    try:
        from moviepy.config import get_setting
        ff = get_setting("FFMPEG_BINARY")
        if ff and Path(ff).exists():
            return ff
    except Exception:
        pass
    # 2. Fallback: check common Windows locations used by imageio-ffmpeg
    try:
        import imageio_ffmpeg
        ff = imageio_ffmpeg.get_ffmpeg_exe()
        if ff and Path(ff).exists():
            return ff
    except Exception:
        pass
    # 3. Last resort: PATH
    ff = shutil.which("ffmpeg")
    if ff:
        return ff
    raise RuntimeError("ffmpeg not found — install it or add to PATH")


def _run_ffmpeg(cmd: list, timeout: int = 120) -> bool:
    import subprocess
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout)
        if r.returncode != 0:
            log.warning(f"ffmpeg exit {r.returncode}: {r.stderr[-300:].decode('utf-8','replace')}")
        return r.returncode == 0
    except Exception as e:
        log.warning(f"ffmpeg error: {e}")
        return False


def _media_duration(ff: str, path) -> float:
    """Duration (seconds) of any media file, from the container header."""
    import subprocess, re
    try:
        info = subprocess.run([ff, "-i", str(path)], capture_output=True, text=True).stderr
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", info)
        if m:
            return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    except Exception:
        pass
    return 0.0


def _music_bed_gain_db(ff: str, music_path, target_lufs: float):
    """Measure a music track's integrated loudness (EBU R128) and return the
    constant dB gain that lands it at `target_lufs`. This is what makes every
    background track sit at the SAME perceived level under the voice, no matter how
    loud or quiet the source file was mastered. Returns None if it can't measure
    (caller falls back to the old fixed multiplier)."""
    import subprocess, json as _json, re as _re
    try:
        r = subprocess.run(
            [ff, "-hide_banner", "-i", str(music_path),
             "-af", "loudnorm=print_format=json", "-f", "null", "-"],
            capture_output=True, text=True, timeout=90)
        m = _re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", r.stderr, _re.S)
        if not m:
            return None
        measured = float(_json.loads(m.group(0))["input_i"])
        if measured <= -70:                 # silence / unmeasurable
            return 0.0
        return max(-30.0, min(12.0, target_lufs - measured))   # gain, sanely clamped
    except Exception:
        return None


def _loudnorm_af(ff: str, audio_path: str) -> str:
    """Voice chain for one segment: speech compressor + two-pass EBU R128.
    - Compressor first: TTS speech is dynamic (quiet syllables); compression
      raises density, which is what makes voices FEEL loud on phone speakers.
    - Then loudnorm locked to measured values (linear mode) targeting -14 LUFS —
      YouTube's playback reference. YouTube never boosts quieter content, so
      anything below -14 simply plays quieter than every other short.
    Single-pass loudnorm on short clips lands well below target (3s lookahead),
    hence the measure pass. Measurement runs THROUGH the compressor so the
    second pass applies exact gain."""
    import subprocess, json as _json, re as _re
    comp = "acompressor=threshold=-20dB:ratio=3:attack=8:release=160"
    base = "loudnorm=I=-14:TP=-1.0:LRA=11"
    try:
        r = subprocess.run([ff, "-hide_banner", "-i", str(audio_path),
                            "-af", comp + "," + base + ":print_format=json", "-f", "null", "-"],
                           capture_output=True, text=True, timeout=60)
        m = _re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", r.stderr, _re.S)
        d = _json.loads(m.group(0))
        return (f"{comp},{base}:measured_I={d['input_i']}:measured_TP={d['input_tp']}:"
                f"measured_LRA={d['input_lra']}:measured_thresh={d['input_thresh']}:"
                f"offset={d['target_offset']}:linear=true")
    except Exception:
        return comp + "," + base


def _xfade_concat(ff: str, seg_paths: list, offsets: list, out_path, t: float = 0.3) -> bool:
    """VIDEO-ONLY dissolve chain at PRESCRIBED global offsets. The voiceover lives
    on its own single continuous track (see _assemble_web_long) — joining audio
    per segment was the root of every boundary artifact, so video and audio are
    now assembled independently. offsets[i] = global start time of the dissolve
    joining segment i and i+1."""
    inputs = []
    for p in seg_paths:
        inputs += ["-i", str(p)]
    if len(seg_paths) == 1:
        return _run_ffmpeg([ff, "-y"] + inputs + ["-an", "-c:v", "copy",
                            str(out_path)], timeout=300)
    filters, vlab = [], "[0:v]"
    for i in range(1, len(seg_paths)):
        vout = f"[v{i}]"
        filters.append(f"{vlab}[{i}:v]xfade=transition=fade:duration={t}:"
                       f"offset={offsets[i - 1]:.3f}{vout}")
        vlab = vout
    cmd = ([ff, "-y"] + inputs +
           ["-filter_complex", ";".join(filters), "-map", vlab, "-an",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "25", "-r", "25",
            str(out_path)])
    return _run_ffmpeg(cmd, timeout=max(600, len(seg_paths) * 30))


def _audio_duration(ff: str, path: str) -> float:
    """Approx duration from the container header. Only used as a fallback for
    subtitle pacing when no word timings exist — transitions no longer depend on it."""
    import subprocess, re
    try:
        r = subprocess.run([ff, "-i", str(path)], capture_output=True, text=True)
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", r.stderr)
        if m:
            h, mn, s = m.groups()
            return int(h) * 3600 + int(mn) * 60 + float(s)
    except Exception:
        pass
    return 0.0


def _seg_key(seg_type: str, number) -> str:
    """Unique image key per segment. hook/outro are unique by type; facts by number.
    Without this, hook and outro both carry number=0 and collide — the outro image
    overwrites the hook, so the hook image is never used."""
    if seg_type in ("hook", "outro"):
        return seg_type
    return f"fact_{number}"


def _effect_filter(effect: str, level: str) -> str:
    """A tasteful retro/analog look applied over the WHOLE finished video.
    Returns a -filter_complex graph from input [0:v] to output [v], or '' for none.
    `level` (subtle/medium/strong) scales the moving-noise and chroma-shift amounts;
    the goal is a real broadcast/film feel, not a cartoon. Recipes are the standard
    ffmpeg ones (rgbashift chroma bleed, temporal noise grain, blend-based scanlines,
    vignette) verified to render on this build."""
    effect = (effect or "none").lower()
    if effect in ("", "none"):
        return ""
    k = {"subtle": 0.55, "medium": 1.0, "strong": 1.6}.get((level or "medium").lower(), 1.0)
    def amt(x, lo=1):
        return max(lo, int(round(x * k)))

    if effect == "grain":           # film grain: moving luma grain + soft vignette
        return (f"[0:v]noise=c0s={amt(16)}:c0f=t+u,vignette=PI/5[v]")

    if effect == "vhs":             # chroma bleed + grain + soft + faded + vignette
        sh = amt(3)
        return (f"[0:v]rgbashift=rh=-{sh}:bh={sh},gblur=sigma=0.5,"
                f"noise=c0s={amt(12)}:c0f=t,eq=saturation=0.85:contrast=1.06,"
                f"vignette=PI/4.5[v]")

    if effect == "crt":             # old TV: scanlines + chroma + grain + vignette
        sh = amt(2)
        return (f"[0:v]rgbashift=rh=-{sh}:bh={sh},noise=c0s={amt(8)}:c0f=t,"
                f"format=gbrp,split[a][b];[b]lutrgb=r=0:g=0:b=0[blk];"
                f"[a][blk]blend=all_expr='if(mod(Y,3),A,A*0.70)',"
                f"eq=contrast=1.08,vignette=PI/5[v]")

    if effect == "glitch":          # analog glitch: strong chroma tear + moving noise
        return (f"[0:v]rgbashift=rh=-{amt(6)}:bh={amt(6)},noise=c0s={amt(14)}:c0f=t,"
                f"eq=contrast=1.05,vignette=PI/5[v]")

    return ""


def _assemble_web_long(script: dict, segments: list, out_path: str, ts: str,
                       vid_w: int = W, vid_h: int = H) -> dict:
    """
    Fast assembly for web-generated videos — long landscape (1920×1080) or short
    portrait (1080×1920) depending on vid_w/vid_h.
    Uses ffmpeg directly: static image + audio → per-segment mp4, then concat.
    Avoids all Python-level frame processing → 10-30× faster than MoviePy.
    """
    import subprocess, concurrent.futures as _cf

    try:
        ff = _get_ffmpeg()
    except RuntimeError as e:
        log.error(str(e))
        return script
    log.info(f"Web fast assembly via ffmpeg ({ff}), {len(segments)} segments @ {vid_w}x{vid_h}")

    # Match images to audio segments by (type, number). hook & outro both have
    # number=0, so keying by number alone makes the outro overwrite the hook.
    img_by_key = {
        _seg_key(im.get("segment_type", "fact"), im.get("number", 0)): im.get("path", "")
        for im in script.get("images", []) if isinstance(im, dict)
    }
    # Optional SECOND image per segment (long videos): the segment switches images
    # halfway through so multi-sentence shots feel like video, not a slideshow.
    img2_by_key = {
        _seg_key(im.get("segment_type", "fact"), im.get("number", 0)): im.get("path", "")
        for im in script.get("images2", []) if isinstance(im, dict)
    }

    # Safety net: an ordered list of every real image, used when a segment's
    # (type, number) key has no match. The web layer numbers images and audio the
    # SAME way now, so misses should not happen — but if one ever does, showing a
    # neighbouring image (cycled by segment index) beats a jarring black frame.
    _avail_imgs = [p for p in img_by_key.values() if p and Path(p).exists()]

    # Subtitle settings (chosen in the web review screen)
    subs_enabled = bool(script.get("enable_subtitles", True))
    sub_style    = script.get("subtitle_style", "classic")
    sub_position = script.get("subtitle_position", "bottom")
    sub_font     = script.get("subtitle_font", "Arial")
    sub_anim     = script.get("subtitle_animation", "pop")
    sub_size     = script.get("subtitle_size", 1.0)
    sub_v        = script.get("subtitle_v", None)
    sub_smart    = bool(script.get("subtitle_smart", False))
    sub_words    = script.get("subtitle_words", 3)
    if subs_enabled:
        from pipeline.subtitles import build_segment_ass

    # Assembly options
    transitions = bool(script.get("transitions", True))      # fade between shots
    shot_gap    = bool(script.get("shot_gap", True))          # keep natural pauses
    music_path  = script.get("music_path") or ""              # bg-music file (abs path)
    music_vol   = float(script.get("music_volume", 0.12) or 0.12)
    effect       = (script.get("effect") or "none").strip().lower()           # retro/analog look
    effect_level = (script.get("effect_intensity") or "medium").strip().lower()

    Path("output/videos").mkdir(parents=True, exist_ok=True)

    # ── ONE continuous audio timeline ─────────────────────────────────────────
    # The voice used to live inside each segment file, so every video join was
    # also an AUDIO join — and every audio join was an opportunity for an audible
    # artifact (the "tiny cuts between segments"). Now each shot's voice becomes a
    # clean WAV, is scheduled on a single timeline with uniform gaps, and the
    # whole track is encoded exactly ONCE. Audio boundaries no longer exist.
    XF   = 0.3                            # video dissolve length
    LEAD = 0.3                            # silence before the first word (== XF)
    GAP  = 0.55 if shot_gap else 0.40     # uniform pause between shots' voices
    TAIL = 0.45                           # silence after the last word

    voice_wavs, vlens, kept = [], [], []
    for idx, seg in enumerate(segments):
        _sk = f"{seg.get('type')}{seg.get('number', '') or ''}"
        src = seg.get("path", "")
        if not src or not Path(src).exists():
            log.warning(f"[keep] DROP {_sk}: audio file missing -> {src!r}")
            continue
        wav = (Path("output/videos") / f"_vo_{ts}_{idx:04d}.wav").resolve()
        # trim lead/tail silence (soft -50dB threshold, generous keeps so fricative
        # openers and breathy endings survive) → two-pass loudnorm to -14 LUFS →
        # gentle gate so the gain-amplified noise floor can't whisper between shots
        af = ("silenceremove=start_periods=1:start_duration=0:start_threshold=-50dB:start_silence=0.15,"
              "areverse,"
              "silenceremove=start_periods=1:start_duration=0:start_threshold=-50dB:start_silence=0.15,"
              "areverse,"
              + _loudnorm_af(ff, src) +
              ",agate=threshold=0.013:ratio=2:attack=10:release=250:range=0.04")
        if not _run_ffmpeg([ff, "-y", "-i", str(src), "-af", af,
                            "-ar", "44100", "-ac", "1", str(wav)], timeout=180):
            _run_ffmpeg([ff, "-y", "-i", str(src), "-ar", "44100", "-ac", "1",
                         str(wav)], timeout=120)
        if not wav.exists():
            log.warning(f"[keep] DROP {_sk}: ffmpeg produced no wav from {src!r}")
            continue
        vlen = _media_duration(ff, wav)
        if vlen <= 0.05:
            log.warning(f"[keep] DROP {_sk}: silent after processing (vlen={vlen:.3f}s) <- {src!r}")
            continue
        kept.append(seg); voice_wavs.append(wav); vlens.append(vlen)

    if not voice_wavs:
        log.error("Web long: no voice audio")
        return script
    segments = kept
    n = len(segments)
    log.info("[keep] kept %d segments: %s", n,
             [f"{s.get('type')}{s.get('number', '') or ''}" for s in kept])

    starts, tcur = [], LEAD
    for v in vlens:
        starts.append(tcur)
        tcur += v + GAP
    total_T = starts[-1] + vlens[-1] + TAIL
    # Video segment lengths chosen so each dissolve ENDS exactly when the next
    # shot's voice begins (dissolve i starts at starts[i+1] - XF). The voice thus
    # always starts 0.3s into its own segment — hence subtitle shift = XF.
    seg_lens = []
    for i in range(n):
        if n == 1:                      seg_lens.append(total_T)
        elif i == 0:                    seg_lens.append(starts[1])
        elif i < n - 1:                 seg_lens.append(starts[i + 1] - starts[i])
        else:                           seg_lens.append(total_T - starts[i] + XF)
    log.info("[timeline] vlens(audio s)=%s total_T=%.2f seg_lens=%s",
             [round(v, 2) for v in vlens], round(total_T, 2), [round(x, 2) for x in seg_lens])

    def _make_seg(args):
        idx, seg, seg_len = args
        img_path  = img_by_key.get(_seg_key(seg.get("type", "fact"), seg.get("number", 0)), "")
        img2_path = img2_by_key.get(_seg_key(seg.get("type", "fact"), seg.get("number", 0)), "")
        # Never render a fact/hook as black: fall back to a real image if this
        # segment's key didn't resolve (the outro is allowed to be imageless).
        if (not img_path or not Path(img_path).exists()) and _avail_imgs and seg.get("type") != "outro":
            img_path = _avail_imgs[idx % len(_avail_imgs)]
        seg_out   = (Path("output/videos") / f"_seg_{ts}_{idx:04d}.mp4").resolve()
        dur  = vlens[idx]                 # speech length (subtitle pacing)
        vdur = seg_len                    # video length (scheduled)
        log.info("[seg] %s%s img=%s dur=%.2f vdur=%.2f",
                 seg.get("type"), seg.get("number", "") or "",
                 (Path(img_path).name if img_path and Path(img_path).exists() else "NONE"),
                 dur, vdur)

        # Subtitles — built per segment, but NEVER on the outro shot.
        sub_filter, ass_rel = "", None
        if subs_enabled and seg.get("type") != "outro" and (seg.get("text") or "").strip():
            ass = build_segment_ass(seg["text"], dur, style=sub_style,
                                    position=sub_position, W=vid_w, H=vid_h,
                                    img_path=img_path, words=seg.get("words"),
                                    font=sub_font, animation=sub_anim,
                                    font_scale=sub_size, v_pct=sub_v, smart=sub_smart,
                                    words_per_cue=sub_words, shift=XF)
            if ass:
                ass_rel = f"output/videos/_sub_{ts}_{idx:04d}.ass"
                Path(ass_rel).write_text(ass, encoding="utf-8")
                # fontsdir=fonts → libass also loads custom fonts from william/fonts
                # (relative path avoids the Windows "C:" colon-escaping issue).
                sub_filter = (f",subtitles={ass_rel}:fontsdir=fonts"
                              if Path("fonts").is_dir() else f",subtitles={ass_rel}")

        # Outro → burn a simple centered "SUBSCRIBE" call-to-action (red pill, white
        # bold text). English wording is the universal YouTube convention and keeps
        # drawtext away from Arabic shaping. Drawn last so it sits on top.
        btn = ""
        if seg.get("type") == "outro":
            _fs  = max(44, min(vid_w, vid_h) // 13)
            _pad = _fs // 2
            _fp  = "fonts/Anton.ttf" if Path("fonts/Anton.ttf").exists() else "fonts/Montserrat.ttf"
            btn = (f",drawtext=fontfile={_fp}:text='SUBSCRIBE':fontcolor=white:"
                   f"fontsize={_fs}:box=1:boxcolor=0xCC0000@0.95:boxborderw={_pad}:"
                   f"x=(w-text_w)/2:y=(h-text_h)/2")

        # No per-shot fade — a fade-IN from black flashed dark at every image switch.
        # Transitions are handled at concat time (crossfade dissolve, no black).
        fade_f = ""
        FPS = 25
        # Anti-jitter: zoompan rounds its crop origin to whole input pixels each
        # frame, and on a near-output-size canvas that 1px stepping is visible as
        # vibration. Rendering the move on a 2× supersampled canvas and then
        # downscaling (lanczos) makes the steps sub-pixel → smooth motion.
        SS = 2
        ow, oh = vid_w * SS, vid_h * SS
        uw, uh = int(ow * 1.25), int(oh * 1.25)             # headroom to crop & pan

        def _kb(m, n_frames):
            """One continuous Ken Burns chain (scale→crop→zoompan) on the SS canvas.
            The move spans exactly n_frames so the image is never still; direction
            varies with m for variety."""
            P = f"(on/{max(n_frames - 1, 1)})"              # 0 → 1 across the move
            cx, cy = "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)" # centered crop origin
            m = m % 4
            if   m == 0: z, xo = f"1.02+0.13*{P}", ""                     # slow zoom IN
            elif m == 1: z, xo = f"1.15-0.13*{P}", ""                     # slow zoom OUT
            elif m == 2: z, xo = f"1.10+0.07*{P}", f"+({P}-0.5)*0.06*iw"  # zoom + pan →
            else:        z, xo = f"1.10+0.07*{P}", f"+(0.5-{P})*0.06*iw"  # zoom + pan ←
            return (f"scale={uw}:{uh}:force_original_aspect_ratio=increase,"
                    f"crop={uw}:{uh},"
                    f"zoompan=z='{z}':x='{cx}{xo}':y='{cy}':"
                    f"d=1:s={ow}x{oh}:fps={FPS}")

        down = f"scale={vid_w}:{vid_h}:flags=lanczos"       # SS canvas → final res

        use_two = (img2_path and Path(img2_path).exists()
                   and img_path and Path(img_path).exists()
                   and seg.get("type") != "outro" and dur >= 6.0)

        if use_two:
            # Two images per segment: switch halfway with opposite motion, so long
            # multi-sentence shots feel like video instead of a slideshow. Subtitles
            # and the outro button draw AFTER the downscale (final resolution).
            d1 = vdur / 2.0
            d2 = vdur - d1
            n1 = max(2, int(round(d1 * FPS)))
            n2 = max(2, int(round(d2 * FPS)))
            fc = (f"[0:v]{_kb(idx, n1)}[v0];"
                  f"[1:v]{_kb(idx + 1, n2)}[v1];"
                  f"[v0][v1]concat=n=2:v=1:a=0[vc];"
                  f"[vc]{down}{sub_filter}{fade_f}{btn}[vout]")
            cmd = [ff, "-y",
                   "-loop", "1", "-framerate", "25", "-t", f"{d1:.3f}", "-i", str(img_path),
                   "-loop", "1", "-framerate", "25", "-t", f"{d2:.3f}", "-i", str(img2_path),
                   "-filter_complex", fc, "-map", "[vout]", "-an",
                   "-c:v", "libx264", "-preset", "veryfast", "-crf", "24",
                   "-r", "25", str(seg_out)]
        elif img_path and Path(img_path).exists():
            N = max(2, int(round(vdur * FPS)))              # total output frames
            vf = _kb(idx, N) + "," + down + sub_filter + fade_f + btn
            cmd = [ff, "-y",
                   "-loop", "1", "-framerate", "25", "-t", f"{vdur:.3f}", "-i", str(img_path),
                   "-vf", vf, "-an",
                   "-c:v", "libx264", "-preset", "veryfast", "-crf", "24",
                   "-r", "25", str(seg_out)]
        else:
            vf = f"format=yuv420p{sub_filter}{fade_f}{btn}"
            cmd = [ff, "-y",
                   "-f", "lavfi", "-i", f"color=c=black:size={vid_w}x{vid_h}:rate=25:d={vdur:.3f}",
                   "-vf", vf, "-an",
                   "-c:v", "libx264", "-preset", "ultrafast", "-crf", "26",
                   "-r", "25", str(seg_out)]

        ok = _run_ffmpeg(cmd, timeout=300)   # 5 min max per segment
        # Keep the .ass files (pruned below) — they're the only ground truth for
        # diagnosing subtitle rendering issues via /api/debug/last-ass.
        return str(seg_out) if ok and seg_out.exists() else None

    # Build segments in parallel (8 workers)
    with _cf.ThreadPoolExecutor(max_workers=8) as pool:
        seg_paths = list(pool.map(_make_seg,
                                  [(i, s, seg_lens[i]) for i, s in enumerate(segments)]))

    if any(p is None for p in seg_paths):
        # The audio timeline is already scheduled — a missing video segment would
        # desync everything after it, so a failed build is fatal for this render.
        log.error("Web long: a video segment failed to build")
        return script

    # ── Join the VIDEO-ONLY segments ───────────────────────────────────────────
    # Dissolves at the scheduled offsets (each ends exactly when the next voice
    # begins); hard-cut concat as fallback. Audio is attached afterwards.
    concat_txt = Path("output/videos") / f"_concat_{ts}.txt"
    concat_txt.write_text("\n".join(
        f"file '{Path(p).resolve().as_posix()}'" for p in seg_paths), encoding="utf-8")
    vcat = (Path("output/videos") / f"_vcat_{ts}.mp4").resolve()

    ok = False
    if transitions and 1 <= len(seg_paths) <= 16:
        offsets = [starts[i] - XF for i in range(1, n)]
        ok = _xfade_concat(ff, seg_paths, offsets, vcat, t=XF)
    if not ok:
        ok = _run_ffmpeg([
            ff, "-y", "-f", "concat", "-safe", "0", "-i", str(concat_txt.resolve()),
            "-an", "-c:v", "copy", str(vcat),
        ], timeout=400)

    # ── Attach the single continuous audio track (+ optional music bed) ────────
    # Voices are placed at their scheduled start times on one timeline and the
    # whole track is encoded once — no per-boundary audio joins exist anymore.
    if ok:
        ins = [ff, "-y", "-i", str(vcat)]
        for wpath in voice_wavs:
            ins += ["-i", str(wpath)]
        fc = []
        for k in range(n):
            fc.append(f"[{k + 1}:a]adelay={int(round(starts[k] * 1000))}:all=1[a{k}]")
        if n == 1:
            fc.append(f"[a0]apad=pad_dur={TAIL}[vox]")
        else:
            join = "".join(f"[a{k}]" for k in range(n))
            fc.append(f"{join}amix=inputs={n}:duration=longest:normalize=0,"
                      f"apad=pad_dur={TAIL}[vox]")
        amap = "[vox]"
        needs_music = bool(music_path) and Path(music_path).exists()
        if needs_music:
            import math
            mdur = _media_duration(ff, music_path) or 1.0
            loops = max(1, math.ceil(total_T / mdur) + 1)
            ins += ["-stream_loop", str(loops), "-i", str(Path(music_path).resolve())]
            # Loudness-matched bed: measure THIS track's integrated loudness and
            # apply the exact constant gain to land it at a target LUFS that sits a
            # fixed distance below the voice (already -14 LUFS). So a track mastered
            # loud and a quiet one both end up at the same perceived bed level — no
            # more "this song is way louder than that one". The slider now nudges the
            # target loudness instead of a raw multiplier. Constant gain → no pumping,
            # safe with stream_loop (unlike the dynamic ducking that truncated audio).
            # Gap below the -14 LUFS voice. Default music_vol=0.12 → 8 LU gap →
            # music bed at -22 LUFS: clearly present under the narration (not faint),
            # while the voice still sits on top. Slider raises/lowers it: down to a
            # 5 LU gap (-19, music-forward) or up to 20 LU (-34, very subtle).
            bed_target = -14.0 - max(5.0, min(20.0, 8.0 + (0.12 - music_vol) * 70.0))
            gain_db = _music_bed_gain_db(ff, music_path, bed_target)
            if gain_db is not None:
                fc.append(f"[{n + 1}:a]volume={gain_db:.2f}dB[bgm]")
                log.info(f"Music bed → {bed_target:.1f} LUFS (gain {gain_db:+.1f} dB): {Path(music_path).name}")
            else:
                bed_vol = min(music_vol * 2.6, 0.5)   # measurement failed → louder raw fallback
                fc.append(f"[{n + 1}:a]volume={bed_vol:.3f}[bgm]")
            fc.append(f"[vox][bgm]amix=inputs=2:duration=first:"
                      f"dropout_transition=3:normalize=0[aout]")
            amap = "[aout]"
        ok = _run_ffmpeg(ins + [
            "-filter_complex", ";".join(fc),
            "-map", "0:v", "-map", amap,
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
            "-shortest", str(Path(out_path).resolve()),
        ], timeout=600)
        if needs_music:
            log.info(f"Background music bed at volume {music_vol}: {Path(music_path).name}")
        try: vcat.unlink()
        except Exception: pass

    # ── Optional retro/analog effect over the WHOLE video (final encode pass) ──
    # Applied once on the finished file so moving grain/scanlines are continuous and
    # consistent across every shot. Video-only: audio is stream-copied untouched.
    fx_graph = _effect_filter(effect, effect_level)
    if ok and fx_graph and Path(out_path).exists():
        fx_out = (Path("output/videos") / f"_fx_{ts}.mp4").resolve()
        okfx = _run_ffmpeg([
            ff, "-y", "-i", str(Path(out_path).resolve()),
            "-filter_complex", fx_graph,
            "-map", "[v]", "-map", "0:a?",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-c:a", "copy", "-movflags", "+faststart",
            str(fx_out),
        ], timeout=1200)
        if okfx and fx_out.exists():
            try: Path(out_path).unlink()
            except Exception: pass
            fx_out.rename(Path(out_path).resolve())
            log.info(f"Applied '{effect}' effect (intensity={effect_level})")
        else:
            log.warning(f"Effect '{effect}' pass failed — keeping clean video")
            try: fx_out.unlink()
            except Exception: pass

    # Clean up temp files
    for p in seg_paths:
        try: Path(p).unlink()
        except: pass
    for wpath in voice_wavs:
        try: Path(wpath).unlink()
        except Exception: pass
    try: concat_txt.unlink()
    except: pass
    # Prune kept subtitle files to the newest 30 (diagnostics, not a junk drawer).
    try:
        kept_ass = sorted(Path("output/videos").glob("_sub_*.ass"),
                          key=lambda f: f.stat().st_mtime, reverse=True)
        for old in kept_ass[30:]:
            try: old.unlink()
            except Exception: pass
    except Exception:
        pass

    if ok and Path(out_path).exists():
        size = Path(out_path).stat().st_size // (1024 * 1024)
        log.info(f"Web long video ready: {out_path} ({size} MB)")
        script["final_video"] = out_path
    else:
        log.error("Web long: ffmpeg concat failed")
    return script


def run(script: dict) -> dict:
    log.info("=== STEP 4L: Long Video Assembly (1920x1080) ===")

    segments  = script.get("audio_segments", [])
    images    = script.get("images", [])
    ts        = script.get("created_at", "").replace(":", "-").replace(".", "-")[:19]

    if not segments:
        log.error("No audio segments for long video")
        return script

    img_by_num = {
        im.get("number", 0): im.get("path", "")
        for im in images if isinstance(im, dict)
    }
    out_path = f"output/videos/final_long_{ts}.mp4"

    # ── Web-generated: use fast ffmpeg path ──────────────────────────────────
    if script.get("web_generated"):
        return _assemble_web_long(script, segments, out_path, ts)

    # ── william.py pipeline: full MoviePy path (Ken Burns, music, chapters) ──
    from moviepy.editor import AudioFileClip, concatenate_videoclips, ColorClip
    topic = script.get("topic", "")

    final_clips   = []
    chapter_marks = []
    elapsed       = 0.0

    for seg in segments:
        audio_path = seg.get("path")
        if not audio_path or not Path(audio_path).exists():
            continue

        num      = seg.get("number", 0)
        title    = seg.get("title", f"Section {num}")
        seg_text = seg.get("text", "")
        img_path = img_by_num.get(num)

        chapter_marks.append((_fmt_ts(elapsed), title))

        try:
            audio = AudioFileClip(audio_path)
            dur   = audio.duration + FADE

            if img_path and Path(img_path).exists():
                vid = _make_ken_burns(img_path, dur)
            else:
                vid = ColorClip(size=(W, H), color=(10, 15, 30), duration=dur)

            clip = vid.fadein(FADE).fadeout(FADE)

            if seg_text and script.get("enable_subtitles", True):
                clip = _apply_subtitles(clip, seg_text, audio.duration)

            clip = clip.set_audio(audio)
            final_clips.append(clip)
            elapsed += audio.duration

        except Exception as e:
            log.warning(f"Long segment {num} failed: {e}")
            continue

    if not final_clips:
        log.error("No clips assembled for long video")
        return script

    out_path = f"output/videos/final_long_{ts}.mp4"
    Path("output/videos").mkdir(parents=True, exist_ok=True)

    try:
        full = concatenate_videoclips(final_clips, method="compose")

        # Background music (quieter than Shorts — voice is primary)
        try:
            from pipeline import step4b_music
            music_path = step4b_music.run(full.duration, ts, topic=topic)
            if music_path and Path(music_path).exists():
                from moviepy.editor import CompositeAudioClip
                music_clip = AudioFileClip(music_path)
                music_clip = music_clip.subclip(0, min(music_clip.duration, full.duration))
                music_clip = music_clip.volumex(0.07)
                orig_audio = full.audio
                if orig_audio is not None:
                    full = full.set_audio(CompositeAudioClip([orig_audio, music_clip]))
                else:
                    full = full.set_audio(music_clip)
                log.info(f"Music mixed at 7%: {Path(music_path).name}")
        except Exception as e:
            log.warning(f"Background music skipped: {e}")

        full.write_videofile(
            out_path, fps=30, codec="libx264", audio_codec="aac",
            verbose=False, logger=None,
        )
        for c in final_clips:
            try:
                c.close()
            except Exception:
                pass
        full.close()

        size = Path(out_path).stat().st_size // (1024 * 1024)
        log.info(f"Long video ready: {out_path} ({size}MB)")
        script["final_video"]   = out_path
        script["chapter_marks"] = chapter_marks

        # Inject chapters into description
        chapters_text = "\n".join(f"{ts_str} {t}" for ts_str, t in chapter_marks)
        desc = script.get("description", "")
        if "CHAPTERS" in desc:
            script["description"] = desc.replace("CHAPTERS", f"CHAPTERS\n{chapters_text}")
        else:
            script["description"] = f"{desc}\n\n📌 CHAPTERS\n{chapters_text}"

    except Exception as e:
        log.error(f"Long video assembly failed: {e}")
        return script

    if script.get("script_path"):
        with open(script["script_path"], "w", encoding="utf-8") as f:
            json.dump(script, f, indent=2, ensure_ascii=False)

    return script
