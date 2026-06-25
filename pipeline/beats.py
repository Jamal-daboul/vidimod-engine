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
    cmd = [ff, "-v", "error", "-i", str(path),
           "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"]
    out = subprocess.run(cmd, capture_output=True, timeout=180).stdout
    if not out:
        raise RuntimeError("ffmpeg produced no audio (unreadable file?)")
    # frombuffer is read-only and shares the bytes; copy so it's writable downstream.
    a = np.frombuffer(out, dtype=np.float32).copy()
    # Guard against NaN/inf from odd encodes.
    return np.nan_to_num(a, copy=False)


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
    musical lag range so half/double-time errors are bounded."""
    if len(env) < 4:
        return 120.0
    env = env - env.mean()
    ac = np.correlate(env, env, mode="full")[len(env) - 1:]
    if ac[0] <= 0:
        return 120.0
    ac = ac / ac[0]
    lag_min = int(round(60.0 / BPM_MAX * fps))      # fast tempo → short lag
    lag_max = int(round(60.0 / BPM_MIN * fps))      # slow tempo → long lag
    lag_max = min(lag_max, len(ac) - 1)
    if lag_max <= lag_min:
        return 120.0
    best_lag = lag_min + int(np.argmax(ac[lag_min:lag_max + 1]))
    if best_lag <= 0:
        return 120.0
    return float(60.0 * fps / best_lag)


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


def analyze(path: str, cadence: str = "auto", target: float = 1.2) -> dict:
    """Analyse one audio file. Returns:
        {duration, bpm, beats:[...], cut_times:[...], image_count, slot_avg}
    `cut_times` are the chosen cut boundaries (one image per gap between them);
    `image_count` = len(cut_times) - 1. See cut_times() for the cadence rules."""
    ff = _ffmpeg()
    samples = _decode_mono(path, ff)
    duration = round(len(samples) / SR, 3)
    env = _onset_envelope(samples)
    fps = SR / HOP
    bpm = _estimate_bpm(env, fps)
    bpm = round(max(BPM_MIN, min(BPM_MAX, bpm)), 1)
    beats = _beat_grid(env, fps, bpm, duration)
    cuts = cut_times(beats, duration, cadence=cadence, target=target, bpm=bpm)
    slots = [round(cuts[i + 1] - cuts[i], 3) for i in range(len(cuts) - 1)]
    return {
        "duration":    duration,
        "bpm":         bpm,
        "beats":       beats,
        "cut_times":   cuts,
        "image_count": max(1, len(cuts) - 1),
        "slot_avg":    round(sum(slots) / len(slots), 3) if slots else 0.0,
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
    print(json.dumps(analyze(p, cadence=cad, target=tgt)))
