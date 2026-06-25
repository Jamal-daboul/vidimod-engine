"""Step 4B — Beat-montage assembler.

A different kind of video from the script-driven shorts/longs: NO voiceover drives
the timeline. Instead a song does. Many AI images are each shown for one beat slot
(~1–1.5 s, snapped to the song's beat by pipeline/beats.py), each with strong
continuous motion, hard-cut on the beat, over the full song. Optional per-image Veo
clips replace the ffmpeg motion when the user pays to animate a shot.

This is what trend/montage videos are: image → beat → image → beat, great music,
no narration. The script object this consumes:

    {
      "mode": "beat",
      "montage_frames": ["/abs/img1.jpg", ...],   # ordered, one per slot
      "cut_times":      [0.0, 1.16, 2.27, ...],    # slot boundaries (seconds)
      "music_path":     "/abs/song.mp3",           # the audio bed (required)
      "music_volume":   0.95,
      "motion_intensity": "high",                  # low | medium | high
      "montage_transition": "mix",                 # cut | flash | mix
      "motion_clips":   [{"number": 3, "path": "/abs/clip.mp4"}, ...],  # optional Veo
      "is_long_video":  false,                      # false → portrait 1080x1920
      "effect": "none", "effect_intensity": "medium",
      "music_start": 0.0,                           # skip N s of song intro (optional)
    }

Reuses the proven ffmpeg helpers from step4_long (binary locator, runner, duration,
music-bed loudness, retro effect) so behaviour matches the rest of the engine.
"""

import json
import logging
import concurrent.futures as _cf
from pathlib import Path

log = logging.getLogger(__name__)

FPS      = 30          # montages feel smoother at 30
MAX_SLOTS = 400        # hard safety cap on shot count


def _dims(is_long: bool):
    return (1920, 1080) if is_long else (1080, 1920)


# Eight motion styles, cycled by shot index so no two adjacent shots move the same
# way ("change the angle / change the whole shot"). Each returns a zoompan z/x/y
# expression set on a supersampled canvas (anti-jitter, like step4_long).
def _motion_vf(idx: int, intensity: str, W: int, H: int, n_frames: int) -> str:
    SS = 2                                    # supersample → sub-pixel motion (smooth)
    ow, oh = W * SS, H * SS
    uw, uh = int(ow * 1.4), int(oh * 1.4)     # headroom to crop, pan & rotate
    P = f"(on/{max(n_frames - 1, 1)})"        # 0 → 1 across the slot
    amt = {"low": 0.6, "medium": 1.0, "high": 1.5}.get((intensity or "high").lower(), 1.5)
    z0 = 0.12 * amt                            # zoom travel
    pan = 0.10 * amt                           # pan travel (fraction of frame)
    cx, cy = "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    m = idx % 8
    # Eight zoom + directional-pan combinations (incl. diagonals). No rotate filter —
    # rotate with transparent corners on a yuv420p canvas fails on some ffmpeg builds.
    right, left = f"+({P}-0.5)*{pan}*iw", f"+(0.5-{P})*{pan}*iw"
    down, up    = f"+({P}-0.5)*{pan}*ih", f"+(0.5-{P})*{pan}*ih"
    if   m == 0: z, xo, yo = f"1.0+{z0}*{P}",     "",    ""        # punch IN (center)
    elif m == 1: z, xo, yo = f"1.0+{z0}*(1-{P})", "",    ""        # pull OUT (center)
    elif m == 2: z, xo, yo = f"1.06+{z0}*{P}",    right, ""        # zoom + pan right
    elif m == 3: z, xo, yo = f"1.06+{z0}*{P}",    left,  ""        # zoom + pan left
    elif m == 4: z, xo, yo = f"1.06+{z0}*{P}",    "",    down      # zoom + push down
    elif m == 5: z, xo, yo = f"1.06+{z0}*{P}",    "",    up        # zoom + push up
    elif m == 6: z, xo, yo = f"1.08+{z0}*{P}",    right, down      # zoom + diagonal ↘
    else:        z, xo, yo = f"1.08+{z0}*{P}",    left,  up        # zoom + diagonal ↖
    chain = (f"scale={uw}:{uh}:force_original_aspect_ratio=increase,"
             f"crop={uw}:{uh},"
             f"zoompan=z='{z}':x='{cx}{xo}':y='{cy}{yo}':d=1:s={ow}x{oh}:fps={FPS}")
    return chain + f",scale={W}:{H}:flags=lanczos,setsar=1"


def _flash_prefix(transition: str, idx: int) -> str:
    """A short fade-from-white at the cut so it pops on the beat. 'mix' flashes every
    other shot (downbeat feel); 'flash' flashes every shot; 'cut' never does."""
    t = (transition or "mix").lower()
    if t == "flash" or (t == "mix" and idx % 2 == 0 and idx > 0):
        return ",fade=t=in:st=0:d=0.07:color=white"
    return ""


def run(script: dict) -> dict:
    log.info("=== STEP 4B: Beat Montage Assembly ===")
    from pipeline.step4_long import (_get_ffmpeg, _run_ffmpeg, _media_duration,
                                     _effect_filter)
    # Newer engine builds expose a loudness-matched music gain; older copies don't,
    # so import it defensively and fall back to the plain volume multiplier.
    try:
        from pipeline.step4_long import _music_bed_gain_db
    except Exception:
        _music_bed_gain_db = lambda *a, **k: None

    try:
        ff = _get_ffmpeg()
    except RuntimeError as e:
        log.error(str(e)); return script

    is_long = bool(script.get("is_long_video"))
    W, H = _dims(is_long)
    ts = str(script.get("created_at") or script.get("timestamp") or "0").replace(":", "-").replace(".", "-")[:19]

    # ── Frames (ordered, one per slot) ─────────────────────────────────────────
    frames = [p for p in (script.get("montage_frames") or []) if p and Path(p).exists()]
    if not frames:
        # Fallback: derive from the generic images list (sorted by number).
        imgs = [im for im in (script.get("images") or []) if isinstance(im, dict) and im.get("path")]
        imgs.sort(key=lambda im: im.get("number", 0))
        frames = [im["path"] for im in imgs if Path(im["path"]).exists()]
    if not frames:
        log.error("Beat montage: no frames"); return script

    music_path = script.get("music_path") or ""
    if not music_path or not Path(music_path).exists():
        log.error("Beat montage: music_path missing — a song is required"); return script

    # ── Cut grid ────────────────────────────────────────────────────────────────
    cuts = script.get("cut_times") or script.get("beat_times") or []
    if not cuts or len(cuts) < 2:
        # Compute on the fly from the song (same module the backend uses).
        try:
            from pipeline import beats
            cuts = beats.analyze(music_path, cadence=script.get("cadence", "auto"),
                                 target=float(script.get("slot_target", 1.2)))["cut_times"]
        except Exception as e:
            log.error(f"Beat montage: beat analysis failed: {e}"); return script
    cuts = [float(c) for c in cuts]

    # Align frame count and slot count: use as many shots as we have BOTH for.
    n = min(len(frames), len(cuts) - 1, MAX_SLOTS)
    if n < 1:
        log.error("Beat montage: nothing to render"); return script
    frames = frames[:n]
    cuts = cuts[:n + 1]
    total_T = cuts[-1]
    log.info(f"Beat montage: {n} shots @ {W}x{H}, {total_T:.1f}s, "
             f"intensity={script.get('motion_intensity','high')}")

    intensity  = script.get("motion_intensity", "high")
    transition = script.get("montage_transition", "mix")
    music_vol  = float(script.get("music_volume", 0.95) or 0.95)
    music_start = max(0.0, float(script.get("music_start", 0.0) or 0.0))

    # Optional Veo motion clips, keyed by 1-based shot position.
    clip_by_num = {int(c.get("number", 0)): c.get("path", "")
                   for c in (script.get("motion_clips") or []) if isinstance(c, dict)}

    Path("output/videos").mkdir(parents=True, exist_ok=True)

    # ── Build one video-only segment per shot (parallel) ───────────────────────
    def _make_seg(args):
        idx, img = args
        seg_len = max(0.2, cuts[idx + 1] - cuts[idx])
        seg_out = (Path("output/videos") / f"_bm_{ts}_{idx:04d}.mp4").resolve()
        flash   = _flash_prefix(transition, idx)
        clip_path = clip_by_num.get(idx + 1, "")

        if clip_path and Path(clip_path).exists():
            # Veo clip → fill the slot exactly: scale/crop to frame, trim to seg_len,
            # loop only if the clip is shorter than the slot (rare).
            vf = (f"scale={W}:{H}:force_original_aspect_ratio=increase,"
                  f"crop={W}:{H},setsar=1{flash}")
            cmd = [ff, "-y", "-stream_loop", "-1", "-t", f"{seg_len:.3f}", "-i", str(clip_path),
                   "-vf", vf, "-an", "-r", str(FPS),
                   "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", str(seg_out)]
        else:
            n_frames = max(2, int(round(seg_len * FPS)))
            vf = _motion_vf(idx, intensity, W, H, n_frames) + flash
            cmd = [ff, "-y", "-loop", "1", "-framerate", str(FPS),
                   "-t", f"{seg_len:.3f}", "-i", str(img),
                   "-vf", vf, "-an", "-r", str(FPS),
                   "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", str(seg_out)]
        ok = _run_ffmpeg(cmd, timeout=180)
        return str(seg_out) if ok and seg_out.exists() else None

    with _cf.ThreadPoolExecutor(max_workers=8) as pool:
        seg_paths = list(pool.map(_make_seg, list(enumerate(frames))))

    if any(p is None for p in seg_paths):
        bad = [i for i, p in enumerate(seg_paths) if p is None]
        log.error(f"Beat montage: {len(bad)} segment(s) failed to build (idx {bad[:5]})")
        return script

    # ── Concatenate (hard cuts on the beat) ────────────────────────────────────
    concat_txt = (Path("output/videos") / f"_bmcat_{ts}.txt").resolve()
    concat_txt.write_text("\n".join(
        f"file '{Path(p).resolve().as_posix()}'" for p in seg_paths), encoding="utf-8")
    vcat = (Path("output/videos") / f"_bmvcat_{ts}.mp4").resolve()
    ok = _run_ffmpeg([ff, "-y", "-f", "concat", "-safe", "0", "-i", str(concat_txt),
                      "-an", "-c:v", "copy", str(vcat)], timeout=600)
    if not ok:
        log.error("Beat montage: concat failed"); return script

    out_path = f"output/videos/final_beat_{ts}.mp4"

    # ── Audio: the song, normalised to a good level + faded out, trimmed to video ──
    gain_db = _music_bed_gain_db(ff, music_path, -14.0)
    if gain_db is None:
        a_vol = f"volume={max(0.05, music_vol):.3f}"
    else:
        # Loudness-match to -14 LUFS (YouTube ref), then the user's volume on top.
        a_vol = f"volume={gain_db:.2f}dB,volume={max(0.05, music_vol):.3f}"
    fade_out = max(0.3, min(1.2, total_T * 0.04))
    afilter = (f"{a_vol},afade=t=out:st={max(0, total_T - fade_out):.3f}:d={fade_out:.3f}")

    music_in = ["-ss", f"{music_start:.3f}"] if music_start > 0 else []
    ok = _run_ffmpeg([
        ff, "-y", "-i", str(vcat), *music_in, "-i", str(Path(music_path).resolve()),
        "-filter_complex", f"[1:a]{afilter}[a]",
        "-map", "0:v", "-map", "[a]",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
        "-t", f"{total_T:.3f}", "-shortest",
        "-movflags", "+faststart", str(Path(out_path).resolve()),
    ], timeout=600)
    try: vcat.unlink()
    except Exception: pass

    # ── Optional retro/analog effect over the whole video ──────────────────────
    effect = (script.get("effect") or "none").strip().lower()
    fx_graph = _effect_filter(effect, script.get("effect_intensity", "medium"))
    if ok and fx_graph and Path(out_path).exists():
        fx_out = (Path("output/videos") / f"_bmfx_{ts}.mp4").resolve()
        if _run_ffmpeg([ff, "-y", "-i", str(Path(out_path).resolve()),
                        "-filter_complex", fx_graph, "-map", "[v]", "-map", "0:a?",
                        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                        "-c:a", "copy", "-movflags", "+faststart", str(fx_out)],
                       timeout=1200) and fx_out.exists():
            try: Path(out_path).unlink()
            except Exception: pass
            fx_out.rename(Path(out_path).resolve())
            log.info(f"Applied '{effect}' effect")
        else:
            try: fx_out.unlink()
            except Exception: pass

    # ── Cleanup ────────────────────────────────────────────────────────────────
    for p in seg_paths:
        try: Path(p).unlink()
        except Exception: pass
    try: concat_txt.unlink()
    except Exception: pass

    if ok and Path(out_path).exists():
        size = Path(out_path).stat().st_size // (1024 * 1024)
        log.info(f"Beat montage ready: {out_path} ({size} MB, {n} shots)")
        script["final_video"] = out_path
    else:
        log.error("Beat montage: final mux failed")
    return script
