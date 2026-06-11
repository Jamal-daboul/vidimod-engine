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


def _xfade_concat(ff: str, seg_paths: list, out_path, t: float = 0.3) -> bool:
    """Crossfade (dissolve) the segments together — no black gaps. Used only for
    short videos (re-encodes the whole thing, so it's gated by caller). True on ok."""
    import re
    durs = [_media_duration(ff, p) for p in seg_paths]
    if any(d <= t + 0.1 for d in durs):
        return False
    inputs = []
    for p in seg_paths:
        inputs += ["-i", str(p)]
    filters, vlab, alab, acc = [], "[0:v]", "[0:a]", durs[0]
    for i in range(1, len(seg_paths)):
        off = acc - t
        vout, aout = f"[v{i}]", f"[a{i}]"
        filters.append(f"{vlab}[{i}:v]xfade=transition=fade:duration={t}:offset={off:.3f}{vout}")
        # nofade curves: segments carry a silent tail pad (see _make_seg), so the
        # overlap mixes silence with the next voice at FULL level — a normal
        # acrossfade was fading out the last spoken word of every segment.
        filters.append(f"{alab}[{i}:a]acrossfade=d={t}:c1=nofade:c2=nofade{aout}")
        vlab, alab, acc = vout, aout, acc + durs[i] - t
    cmd = ([ff, "-y"] + inputs +
           ["-filter_complex", ";".join(filters), "-map", vlab, "-map", alab,
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "25", "-r", "25",
            "-c:a", "aac", "-b:a", "192k", str(out_path)])
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

    def _make_seg(args):
        idx, seg = args
        audio_path = seg.get("path", "")
        if not audio_path or not Path(audio_path).exists():
            return None
        img_path  = img_by_key.get(_seg_key(seg.get("type", "fact"), seg.get("number", 0)), "")
        img2_path = img2_by_key.get(_seg_key(seg.get("type", "fact"), seg.get("number", 0)), "")
        seg_out   = (Path("output/videos") / f"_seg_{ts}_{idx:04d}.mp4").resolve()
        tmp_audio = None

        # Gap OFF → trim only LEADING + TRAILING silence (preserve in-clip pauses),
        # then add a uniform SHORT pad so every shot gets the same small gap — tight
        # but not jarring, and consistent across all shots.
        if not shot_gap:
            ta = (Path("output/videos") / f"_aud_{ts}_{idx:04d}.m4a").resolve()
            # start_silence keeps 0.12s of the trimmed silence on each side — a hard
            # trim at -45dB was clipping soft-spoken final syllables (breathy TTS
            # endings decay below the threshold while still audible).
            af = ("silenceremove=start_periods=1:start_duration=0:start_threshold=-45dB:start_silence=0.12,"
                  "areverse,"
                  "silenceremove=start_periods=1:start_duration=0:start_threshold=-45dB:start_silence=0.12,"
                  "areverse,"
                  "apad=pad_dur=0.13")          # ~0.13s uniform gap between shots
            if _run_ffmpeg([ff, "-y", "-i", str(audio_path), "-af", af,
                            "-c:a", "aac", "-b:a", "192k", str(ta)], timeout=120) and ta.exists():
                audio_path, tmp_audio = str(ta), ta

        dur = _audio_duration(ff, audio_path)

        # Per-segment audio chain: two-pass loudness (accurate -15 LUFS) + a 0.35s
        # silent tail. The tail is what the 0.3s crossfade overlaps, so a transition
        # can never eat the last spoken word again.
        seg_af = _loudnorm_af(ff, audio_path) + ",apad=pad_dur=0.35"

        # Subtitles — built per segment, but NEVER on the outro shot.
        sub_filter, ass_rel = "", None
        if subs_enabled and seg.get("type") != "outro" and (seg.get("text") or "").strip():
            ass = build_segment_ass(seg["text"], dur, style=sub_style,
                                    position=sub_position, W=vid_w, H=vid_h,
                                    img_path=img_path, words=seg.get("words"),
                                    font=sub_font, animation=sub_anim,
                                    font_scale=sub_size, v_pct=sub_v, smart=sub_smart,
                                    words_per_cue=sub_words)
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
            d1 = dur / 2.0
            # Pad the second half so the VIDEO is always slightly longer than the
            # measured audio — with -shortest the output then ends exactly on the
            # audio, and a probe under-report can never clip the voiceover.
            d2 = (dur - d1) + 0.6
            n1 = max(2, int(round(d1 * FPS)))
            n2 = max(2, int(round(d2 * FPS)))
            fc = (f"[0:v]{_kb(idx, n1)}[v0];"
                  f"[1:v]{_kb(idx + 1, n2)}[v1];"
                  f"[v0][v1]concat=n=2:v=1:a=0[vc];"
                  f"[vc]{down}{sub_filter}{fade_f}{btn}[vout]")
            cmd = [ff, "-y",
                   "-loop", "1", "-framerate", "25", "-t", f"{d1:.3f}", "-i", str(img_path),
                   "-loop", "1", "-framerate", "25", "-t", f"{d2:.3f}", "-i", str(img2_path),
                   "-i", str(audio_path),
                   "-filter_complex", fc, "-map", "[vout]", "-map", "2:a",
                   "-c:v", "libx264", "-preset", "veryfast", "-crf", "24",
                   "-r", "25",
                   "-af", seg_af, "-ar", "44100",
                   "-c:a", "aac", "-b:a", "192k",
                   "-shortest", str(seg_out)]
        elif img_path and Path(img_path).exists():
            N = max(2, int(round(dur * FPS)))               # total output frames
            vf = _kb(idx, N) + "," + down + sub_filter + fade_f + btn
            cmd = [ff, "-y",
                   "-loop", "1", "-framerate", "25", "-i", str(img_path),
                   "-i", str(audio_path),
                   "-vf", vf,
                   "-c:v", "libx264", "-preset", "veryfast", "-crf", "24",
                   "-r", "25",
                   "-af", seg_af, "-ar", "44100",
                   "-c:a", "aac", "-b:a", "192k",
                   "-shortest", str(seg_out)]
        else:
            vf = f"format=yuv420p{sub_filter}{fade_f}{btn}"
            cmd = [ff, "-y",
                   "-f", "lavfi", "-i", f"color=c=black:size={vid_w}x{vid_h}:rate=25",
                   "-i", str(audio_path), "-vf", vf,
                   "-c:v", "libx264", "-preset", "ultrafast", "-crf", "26",
                   "-r", "25",
                   "-af", seg_af, "-ar", "44100",
                   "-c:a", "aac", "-b:a", "192k",
                   "-shortest", str(seg_out)]

        ok = _run_ffmpeg(cmd, timeout=300)   # 5 min max per segment
        # Keep the .ass files (pruned below) — they're the only ground truth for
        # diagnosing subtitle rendering issues via /api/debug/last-ass.
        if tmp_audio:
            try: tmp_audio.unlink()
            except Exception: pass
        return str(seg_out) if ok and seg_out.exists() else None

    # Build segments in parallel (8 workers)
    with _cf.ThreadPoolExecutor(max_workers=8) as pool:
        seg_paths = list(pool.map(_make_seg, enumerate(segments)))

    seg_paths = [p for p in seg_paths if p]
    if not seg_paths:
        log.error("Web long: no segments produced")
        return script

    # Write concat list with ABSOLUTE paths so ffmpeg never doubles the directory
    concat_txt = Path("output/videos") / f"_concat_{ts}.txt"
    abs_lines = "\n".join(
        f"file '{Path(p).resolve().as_posix()}'" for p in seg_paths
    )
    concat_txt.write_text(abs_lines, encoding="utf-8")

    # Join segments. With transitions on → CROSSFADE (dissolve, no black gaps);
    # otherwise (or if crossfade fails) → instant stream-copy concat (hard cuts).
    # If music is selected, join to a temp first, then mix music under the voice.
    needs_music = bool(music_path) and Path(music_path).exists()
    concat_out  = ((Path("output/videos") / f"_cc_{ts}.mp4").resolve()
                   if needs_music else Path(out_path).resolve())

    ok = False
    if transitions and 2 <= len(seg_paths) <= 16:
        ok = _xfade_concat(ff, seg_paths, concat_out, t=0.3)     # dissolve (short videos)
    if not ok:
        # Hard-cut concat. Re-encode AUDIO (copy video) so joins are seamless —
        # stream-copying AAC leaves priming gaps that click/cut at every boundary.
        ok = _run_ffmpeg([
            ff, "-y", "-f", "concat", "-safe", "0", "-i", str(concat_txt.resolve()),
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", str(concat_out),
        ], timeout=400)

    if ok and needs_music:
        # Loop the music a finite number of times to cover the full video (a plain
        # `-stream_loop -1` was stopping after one play). normalize=0 keeps the voice
        # at full volume with the music sitting under it.
        import math
        vdur = _media_duration(ff, concat_out)
        mdur = _media_duration(ff, music_path) or 1.0
        loops = max(1, math.ceil(vdur / mdur) + 1)
        mixed = _run_ffmpeg([
            ff, "-y", "-i", str(concat_out),
            "-stream_loop", str(loops), "-i", str(Path(music_path).resolve()),
            "-filter_complex",
            f"[1:a]volume={music_vol}[m];[0:a][m]amix=inputs=2:duration=first:"
            f"dropout_transition=0:normalize=0[aout]",
            "-map", "0:v", "-map", "[aout]",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
            str(Path(out_path).resolve()),
        ], timeout=400)
        if mixed and Path(out_path).exists():
            try: concat_out.unlink()
            except Exception: pass
        else:
            log.warning("Music mix failed — keeping video without music")
            try:
                if Path(out_path).exists(): Path(out_path).unlink()
                concat_out.rename(Path(out_path).resolve())
            except Exception: pass
        log.info(f"Background music mixed at volume {music_vol}: {Path(music_path).name}")

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
