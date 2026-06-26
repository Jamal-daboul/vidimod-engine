"""Beat / tempo detection for the beat-montage video mode — pure NumPy.

The montage assembler (step4_beat.py) cuts the image slideshow ON the song's
beat. To do that we need, from any audio file: its duration, an estimated tempo
(BPM), and a list of beat times in seconds. We deliberately use ONLY numpy (which
the render engine already depends on) — NOT librosa/scipy — so this adds zero new
packages to the small VPS (see requirements-engine.txt).

Pipeline:
  1. ffmpeg decodes the song to mono f32 PCM (piped to stdout — no temp file).
  2. Short-time spectral flux → an onset-strength envelope (numpy.rfft).
  3. Autocorrelation of that envelope → tempo (BPM), searched in a musical range.
  4. Phase the steady beat grid to the onsets, then emit beats across the track.

A steady grid (rather than raw per-onset cuts) is exactly what looks good for a
montage: predictable, on-the-beat cuts instead of jittery ones. `cut_times()`
then thins that grid to the cadence the user picked (auto / every beat / every N).
"""

import json
import logging
import re
import subprocess
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

SR        = 22050      # analysis sample rate (plenty for beat tracking, fast)
HOP       = 512        # onset-envelope hop (≈ 23 ms frames)
WIN       = 1024       # STFT window
BPM_MIN   = 70.0       # plausible musical tempo range for trend/montage tracks
BPM_MAX   = 180.0


def _ffmpeg() -> str:
    """Reuse the engine's ffmpeg locator so this works on every box."""
    try:
        from pipeline.step4_long import _get_ffmpeg
        return _get_ffmpeg()
    except Exception:
        import shutil
        return shutil.which("ffmpeg") or "ffmpeg"


def _decode_mono(path: str, ff: str = "") -> np.ndarray:
    """Decode any audio/video file to a mono float32 array at SR (via ffmpeg stdout)."""
    ff = ff or _ffmpeg()
    # -nostdin + stdin=DEVNULL: ffmpeg must NEVER read the inherited stdin, or it can hang
    # forever (the montage analyze-beats subprocess inherits uvicorn's stdin).
    # -vn drops any embedded cover-image / video stream (common in ripped "mp3" files).
    cmd = [ff, "-nostdin", "-v", "error", "-i", str(path), "-vn",
           "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"]
    out = subprocess.run(cmd, capture_output=True, timeout=180, stdin=subprocess.DEVNULL).stdout
    if not out:
        raise RuntimeError("ffmpeg produced no audio (unreadable file?)")
    # frombuffer is read-only and shares the bytes; copy so it's writable downstream.
    a = np.frombuffer(out, dtype=np.float32).copy()
    # Guard against NaN/inf from odd encodes.
    return np.nan_to_num(a, copy=False)


def _decode_window(path: str, start: float, dur: float, ff: str = "") -> np.ndarray:
    """Decode ONLY [start, start+dur] of the audio to mono f32. `-ss` BEFORE `-i` is a
    fast input seek, so we never read the whole song just to time a short clip — this is
    the speed fix for montage analysis."""
    ff = ff or _ffmpeg()
    cmd = [ff, "-nostdin", "-v", "error", "-ss", f"{max(0.0, start):.3f}", "-t", f"{max(0.1, dur):.3f}",
           "-i", str(path), "-vn", "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"]
    out = subprocess.run(cmd, capture_output=True, timeout=45, stdin=subprocess.DEVNULL).stdout
    if not out:
        raise RuntimeError("ffmpeg produced no audio for the window (start past song end?)")
    a = np.frombuffer(out, dtype=np.float32).copy()
    return np.nan_to_num(a, copy=False)


def _probe_duration(path: str, ff: str = "") -> float:
    """Full song length (seconds) from the container header — no decode (fast)."""
    ff = ff or _ffmpeg()
    try:
        info = subprocess.run([ff, "-nostdin", "-i", str(path)], capture_output=True,
                              text=True, timeout=20, stdin=subprocess.DEVNULL).stderr
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", info or "")
        if m:
            return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    except Exception:
        pass
    return 0.0


def _onset_envelope(samples: np.ndarray) -> np.ndarray:
    """Spectral-flux onset strength, one value per HOP. Rising spectral energy
    (note/drum onsets) produces peaks; this is the signal we beat-track on."""
    n = len(samples)
    if n < WIN:
        return np.zeros(1, dtype=np.float32)
    window = np.hanning(WIN).astype(np.float32)
    n_frames = 1 + (n - WIN) // HOP
    # Build the framed STFT magnitude matrix (frames × freq bins).
    idx = np.arange(WIN)[None, :] + HOP * np.arange(n_frames)[:, None]
    frames = samples[idx] * window
    mag = np.abs(np.fft.rfft(frames, axis=1))
    # Positive spectral difference, summed across frequency = onset strength.
    diff = np.diff(mag, axis=0)
    flux = np.maximum(diff, 0.0).sum(axis=1)
    flux = np.concatenate([[0.0], flux]).astype(np.float32)
    # Normalise + remove slow drift so autocorrelation locks onto the pulse.
    if flux.max() > 0:
        flux = flux / flux.max()
    flux = flux - _moving_avg(flux, 16)
    return np.maximum(flux, 0.0)


def _moving_avg(x: np.ndarray, w: int) -> np.ndarray:
    if w <= 1 or len(x) < w:
        return np.zeros_like(x)
    kern = np.ones(w, dtype=np.float32) / w
    return np.convolve(x, kern, mode="same")


def _estimate_bpm(env: np.ndarray, fps: float) -> float:
    """Tempo (BPM) from the autocorrelation of the onset envelope, restricted to a
    musical lag range so half/double-time errors are bounded. Uses an FFT
    autocorrelation (O(n log n)) — the old np.correlate(full) was O(n²) and was the
    main reason analysis felt slow on longer songs."""
    n = len(env)
    if n < 4:
        return 120.0
    x = (env - env.mean()).astype(np.float32)
    nfft = 1 << int(np.ceil(np.log2(2 * n)))        # zero-pad to avoid wrap-around
    f = np.fft.rfft(x, nfft)
    ac = np.fft.irfft(f * np.conj(f), nfft)[:n]
    if ac[0] <= 0:
        return 120.0
    ac = ac / ac[0]
    lag_min = int(round(60.0 / BPM_MAX * fps))      # fast tempo → short lag
    lag_max = min(int(round(60.0 / BPM_MIN * fps)), n - 1)
    if lag_max <= lag_min:
        return 120.0
    best_lag = lag_min + int(np.argmax(ac[lag_min:lag_max + 1]))
    return float(60.0 * fps / best_lag) if best_lag > 0 else 120.0


def _beat_grid(env: np.ndarray, fps: float, bpm: float, duration: float) -> list:
    """A steady beat grid at `bpm`, phase-aligned to the onsets (the offset that
    lands the most beats on real onset peaks wins). Returns beat times in seconds."""
    period = 60.0 / max(bpm, 1.0)              # seconds per beat
    period_f = period * fps                    # in envelope frames
    if period_f < 1 or duration <= 0:
        # Fall back to a pure metronome grid.
        return list(np.arange(0.0, max(duration, period), period))
    n_beats = int(duration / period) + 1
    best_off, best_score = 0.0, -1.0
    # Try 12 phase offsets across one beat; score = onset energy summed on the grid.
    for k in range(12):
        off = (k / 12.0) * period_f
        pos = (off + period_f * np.arange(n_beats)).astype(int)
        pos = pos[pos < len(env)]
        score = float(env[pos].sum()) if len(pos) else 0.0
        if score > best_score:
            best_score, best_off = score, off
    off_sec = best_off / fps
    beats = [round(off_sec + i * period, 4) for i in range(n_beats)
             if off_sec + i * period < duration + 1e-6]
    if not beats or beats[0] > 0.05:
        beats = [0.0] + beats
    return beats


def _best_start(env: np.ndarray, fps: float, duration: float,
                window: float, beats: list) -> float:
    """Pick the start time (snapped to a beat) whose `window`-second slice carries the
    most onset energy — i.e. skip the slow intro and begin on the busiest, most
    'dropped-in' part of the song. Returns a start time in seconds (0 if the window is
    basically the whole song)."""
    if window >= duration - 0.25 or len(env) < 4:
        return 0.0
    w_frames = max(1, int(round(window * fps)))
    n = len(env)
    cs = np.concatenate([[0.0], np.cumsum(env)])      # prefix sums → O(1) window energy
    cands = [b for b in (beats or []) if 0.0 <= b <= duration - window + 1e-6]
    if not cands:
        cands = list(np.arange(0.0, max(0.0, duration - window), 0.5))
    if not cands:
        return 0.0
    best_b, best_score = 0.0, -1.0
    for b in cands:
        i0 = int(round(b * fps)); i1 = min(n, i0 + w_frames)
        if i1 <= i0:
            continue
        score = float(cs[i1] - cs[i0]) / (i1 - i0)    # mean onset energy across the window
        if score > best_score:
            best_score, best_b = score, b
    return round(float(best_b), 3)


def _core(path: str) -> dict:
    """The EXPENSIVE part — decode + onset envelope + tempo + full beat grid — depends
    ONLY on the audio file, so cache it in a sidecar JSON keyed by size+mtime. Changing
    the start / length / cadence then re-slices instantly without re-decoding (this is
    what made re-analysis / smart-start feel slow). Returns
    {duration, fps, bpm, beats_abs, env}."""
    p = Path(path)
    try:
        stt = p.stat(); sig = f"{stt.st_size}-{int(stt.st_mtime)}"
    except Exception:
        sig = "0"
    cache = p.with_suffix(p.suffix + ".beats.json")
    try:
        c = json.loads(cache.read_text(encoding="utf-8"))
        if c.get("sig") == sig and c.get("env"):
            return c
    except Exception:
        pass
    samples = _decode_mono(path)
    duration = round(len(samples) / SR, 3)
    env = _onset_envelope(samples)
    fps = SR / HOP
    bpm = round(max(BPM_MIN, min(BPM_MAX, _estimate_bpm(env, fps))), 1)
    beats_abs = _beat_grid(env, fps, bpm, duration)
    c = {"sig": sig, "duration": duration, "fps": fps, "bpm": bpm,
         "beats_abs": beats_abs, "env": [round(float(x), 5) for x in env]}
    try:
        cache.write_text(json.dumps(c), encoding="utf-8")
    except Exception:
        pass
    return c


def analyze(path: str, cadence: str = "auto", target: float = 1.2,
            max_seconds: float = 0.0, start: float = 0.0,
            smart_start: bool = False) -> dict:
    """Analyse one audio file. Returns:
        {duration, used_duration, start, bpm, beats:[...], cut_times:[...],
         image_count, slot_avg}
    `cut_times`/`beats` are 0-based on the VIDEO timeline; the render plays the song
    from `start` seconds (passed as music_start). `image_count` = len(cut_times) - 1.

    `max_seconds` caps the montage length. `start` skips into the song (songs often open
    slowly). `smart_start=True` ignores `start` and auto-picks the most energetic window
    of length `max_seconds` (snapped to a beat) — the best place to begin.

    The heavy decode/tempo work is cached per file (see _core), so repeated calls with a
    different start/length/cadence are near-instant."""
    start = max(0.0, float(start or 0.0))
    want  = float(max_seconds) if (max_seconds and float(max_seconds) > 0) else 0.0

    # ── FAST PATH ──────────────────────────────────────────────────────────────────
    # A fixed length from a known start (the normal montage case): decode + analyse ONLY
    # that [start, start+want] window instead of the whole song. Beats come out 0-based on
    # the video timeline already, and the render plays the song from `start`.
    if want > 0 and not smart_start:
        fps = SR / HOP
        samples = _decode_window(path, start, want)
        eff = round(len(samples) / SR, 3)            # actual window (song may be shorter)
        env = _onset_envelope(samples)
        bpm = round(max(BPM_MIN, min(BPM_MAX, _estimate_bpm(env, fps))), 1)
        beats = _beat_grid(env, fps, bpm, eff)
        cuts = cut_times(beats, eff, cadence=cadence, target=target, bpm=bpm)
        slots = [round(cuts[i + 1] - cuts[i], 3) for i in range(len(cuts) - 1)]
        return {
            "duration":      round(_probe_duration(path) or (start + eff), 3),
            "used_duration": eff,
            "start":         round(start, 3),
            "bpm":           bpm,
            "beats":         beats,
            "cut_times":     cuts,
            "image_count":   max(1, len(cuts) - 1),
            "slot_avg":      round(sum(slots) / len(slots), 3) if slots else 0.0,
        }

    # ── FULL-SONG PATH ─────────────────────────────────────────────────────────────
    # Whole-song length, or smart_start (needs to scan everything) — cached per file.
    core = _core(path)
    duration  = core["duration"]
    fps       = core["fps"]
    bpm       = core["bpm"]
    beats_abs = core["beats_abs"]
    env       = np.asarray(core["env"], dtype=np.float32)

    want = float(max_seconds) if (max_seconds and float(max_seconds) > 0) else duration
    want = min(want, duration)

    if smart_start:
        start = _best_start(env, fps, duration, want, beats_abs)
    start = max(0.0, min(float(start or 0.0), max(0.0, duration - 0.5)))
    eff = round(min(want, duration - start), 3)

    # Beats inside [start, start+eff], shifted to a 0-based video timeline.
    rel = [round(b - start, 4) for b in beats_abs if (start - 1e-6) <= b <= (start + eff + 1e-6)]
    rel = [r for r in rel if r >= 0.0]
    if not rel or rel[0] > 0.05:
        rel = [0.0] + [r for r in rel if r > 0.0]

    cuts = cut_times(rel, eff, cadence=cadence, target=target, bpm=bpm)
    slots = [round(cuts[i + 1] - cuts[i], 3) for i in range(len(cuts) - 1)]
    return {
        "duration":      duration,
        "used_duration": eff,
        "start":         round(start, 3),
        "bpm":           bpm,
        "beats":         rel,
        "cut_times":     cuts,
        "image_count":   max(1, len(cuts) - 1),
        "slot_avg":      round(sum(slots) / len(slots), 3) if slots else 0.0,
    }


def cut_times(beats: list, duration: float, cadence: str = "auto",
              target: float = 1.2, bpm: float = 120.0) -> list:
    """Thin a beat grid to the requested cut cadence and return cut BOUNDARIES
    (so one image fills each gap). The final boundary is the song end.

      cadence='auto'  → every k-th beat, k chosen so each shot ≈ `target` seconds
      cadence='beat'  → every beat
      cadence='2'/'3' → every 2nd / 3rd beat (any integer string)
    """
    beats = [b for b in (beats or []) if b is not None]
    if not beats:
        period = 60.0 / max(bpm, 1.0)
        beats = list(np.arange(0.0, max(duration, period), period))
    period = 60.0 / max(bpm, 1.0)
    if cadence == "beat":
        k = 1
    elif str(cadence).isdigit():
        k = max(1, int(cadence))
    else:                                      # auto → nearest beat-multiple to target
        k = max(1, round(float(target) / period)) if period > 0 else 2
    cuts = list(beats[::k])
    # Always start at 0 and close on the real song end (last image isn't cut short).
    if not cuts or cuts[0] > 0.01:
        cuts = [0.0] + cuts
    if duration - cuts[-1] > 0.25:
        cuts.append(round(duration, 4))
    elif len(cuts) >= 2:
        cuts[-1] = round(duration, 4)
    # Drop boundaries that would make a slot shorter than MIN_SLOT (e.g. a grid
    # phase-offset can leave a tiny sliver right after 0.0). Keeps cuts musical
    # without ever producing a flash-frame image.
    MIN_SLOT = 0.35
    merged = [cuts[0]]
    for b in cuts[1:-1]:
        if b - merged[-1] >= MIN_SLOT:
            merged.append(round(b, 4))
    last = cuts[-1]
    if last - merged[-1] < MIN_SLOT and len(merged) >= 2:
        merged[-1] = last          # absorb a short final slot into the previous one
    else:
        merged.append(round(last, 4))
    return merged


# CLI: `python -m pipeline.beats <file> [cadence] [target]` → prints JSON. The web
# backend calls this via the engine's python so it never needs numpy itself.
if __name__ == "__main__":
    import sys
    p = sys.argv[1]
    cad = sys.argv[2] if len(sys.argv) > 2 else "auto"
    tgt = float(sys.argv[3]) if len(sys.argv) > 3 else 1.2
    mx  = float(sys.argv[4]) if len(sys.argv) > 4 else 0.0
    st  = float(sys.argv[5]) if len(sys.argv) > 5 else 0.0
    sm  = (sys.argv[6].lower() in ("1", "true", "yes")) if len(sys.argv) > 6 else False
    print(json.dumps(analyze(p, cadence=cad, target=tgt, max_seconds=mx, start=st, smart_start=sm)))
