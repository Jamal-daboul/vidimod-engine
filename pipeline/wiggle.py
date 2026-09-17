"""Wiggle stereoscopy for still images.

A wigglegram fakes 3D by rapidly alternating between slightly different viewpoints of
one scene. We synthesize those viewpoints from a single still:

  1. Depth Anything V2 Small (ONNX, CPU) estimates relative depth.
  2. Each view shifts pixels horizontally in proportion to (depth - subject depth):
     the subject stays pinned while background and foreground swing in opposite
     directions — the classic wigglegram pivot.
  3. The views are written once as a short looping image sequence. ffmpeg reads it with
     `-loop 1` on an image2 pattern IN PLACE of the single still, so Ken Burns, captions
     and transitions downstream are untouched. Captions stay steady because they are
     burned after the motion.

Rendering never depends on this module: any failure (no onnxruntime, no model, an
unreadable image) quietly returns the plain still.

Config (env, overridable per script):
  STILL_MOTION       wiggle | none            (script: still_motion_effect)
  WIGGLE_INTENSITY   subtle | medium | strong (script: still_motion_intensity)
  WIGGLE_DEPTH_MODEL path to the ONNX model (default: <engine>/models/depth-anything-v2-small/model.onnx)
"""

import hashlib
import logging
import os
import shutil
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

VERSION = 2                       # bump to invalidate cached view sequences
ENGINE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_MODEL = ENGINE_DIR / "models" / "depth-anything-v2-small" / "model.onnx"
CACHE_DIR = ENGINE_DIR / "output" / "wiggle_cache"
CACHE_MAX_AGE_S = 12 * 3600

# Peak horizontal displacement, as a fraction of image width, for the region whose depth
# is farthest from the subject. The assemblers crop with >=12% headroom, so the stretched
# border columns this creates are never on screen.
AMPLITUDE = {"subtle": 0.008, "medium": 0.013, "strong": 0.02}
# Smooth sway: the viewpoint follows a sine over CYCLE_SECONDS with a new position on EVERY
# frame. (v1 held 4 fixed viewpoints for 3 frames each — a classic wigglegram, but on video it
# read as a low frame rate.) Positions are snapped to 1/POSITION_STEPS of the amplitude (well
# under a pixel) so identical positions share one rendered view.
CYCLE_SECONDS = 2.0
POSITION_STEPS = 12               # 25 distinct views per image: -1 … +1 in 1/12 steps
MAX_WARP_SIDE = 1920              # warp at <=1920px on the long side (output is 1080p)
DEPTH_SHORT_SIDE = 518            # Depth Anything V2 native input size
MEAN = (0.485, 0.456, 0.406)
STD = (0.229, 0.224, 0.225)

_session = None
_session_lock = threading.Lock()  # one inference at a time — 4 CPUs are shared with ffmpeg
_state_lock = threading.Lock()
_state = {"enabled": False, "intensity": "medium", "reason": "not configured"}
_stats = {"applied": set(), "fallback": set(), "seconds": 0.0}
_failed_session = False


def configure(script: dict) -> dict:
    """Decide whether wiggle runs for this render and reset the per-render counters.
    Call once at the start of assembly."""
    effect = str(script.get("still_motion_effect") or os.getenv("STILL_MOTION", "wiggle")).strip().lower()
    intensity = str(script.get("still_motion_intensity") or os.getenv("WIGGLE_INTENSITY", "medium")).strip().lower()
    if intensity not in AMPLITUDE:
        intensity = "medium"
    enabled, reason = effect in ("wiggle", "wiggle_stereoscopy"), ""
    if not enabled:
        reason = f"disabled (STILL_MOTION={effect})"
    elif not _model_path().exists():
        enabled, reason = False, f"depth model missing at {_model_path()}"
    else:
        try:
            import onnxruntime  # noqa: F401
            import numpy  # noqa: F401
        except Exception as e:
            enabled, reason = False, f"missing dependency: {type(e).__name__}"
    with _state_lock:
        _state.update(enabled=enabled, intensity=intensity, reason=reason)
        _stats.update(applied=set(), fallback=set(), seconds=0.0)
    if enabled:
        _prune_cache()
        log.info(f"[wiggle] on — intensity={intensity}")
    else:
        log.info(f"[wiggle] off — {reason}")
    return summary()


def summary() -> dict:
    """What actually happened this render — written to script['still_motion']."""
    with _state_lock:
        return {
            "effect": "wiggle_stereoscopy" if _state["enabled"] else "none",
            "intensity": _state["intensity"],
            "depth_model": "Depth Anything V2 Small" if _state["enabled"] else None,
            "images_applied": len(_stats["applied"]),
            "images_fallback": len(_stats["fallback"]),
            "prep_seconds": round(_stats["seconds"], 1),
            "note": _state["reason"],
        }


def prepare_many(paths, fps: int = 25) -> None:
    """Precompute view sequences for every still up front, before the ffmpeg segment
    builds start competing for the CPU. Safe to call with duplicates / missing files."""
    if not _state["enabled"]:
        return
    unique = []
    for p in paths:
        if p and p not in unique and Path(p).exists():
            unique.append(p)
    if not unique:
        return
    import concurrent.futures as cf
    t0 = time.time()
    # Inference is serialized by _session_lock; the extra workers keep warping/encoding the
    # (now ~25) views of earlier images while the next one is inferred.
    with cf.ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(lambda p: _pattern_for(p, fps), unique))
    with _state_lock:
        _stats["seconds"] += time.time() - t0
    log.info(f"[wiggle] prepared {len(unique)} image(s) in {time.time() - t0:.1f}s")


def still_input(img_path: str, fps: int, duration: float) -> list:
    """ffmpeg input args for a still: a looping wiggle view sequence when enabled,
    otherwise exactly the old `-loop 1 … -i image`."""
    src = str(img_path)
    if _state["enabled"]:
        pattern = _pattern_for(src, fps)
        if pattern:
            src = pattern
    return ["-loop", "1", "-framerate", str(fps), "-t", f"{duration:.3f}", "-i", src]


# ── internals ────────────────────────────────────────────────────────────────────

def _model_path() -> Path:
    return Path(os.getenv("WIGGLE_DEPTH_MODEL") or DEFAULT_MODEL)


def _cache_key(img_path: str, fps: int) -> str:
    h = hashlib.sha1()
    with open(img_path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    h.update(f"|v{VERSION}|{_state['intensity']}|{fps}".encode())
    return h.hexdigest()[:20]


def _pattern_for(img_path: str, fps: int):
    """Return the image2 pattern for this still's view loop (building it once), or None."""
    try:
        out_dir = CACHE_DIR / _cache_key(img_path, fps)
        done = out_dir / "done"
        if not done.exists():
            _build_views(img_path, out_dir, fps)
            done.write_text("ok")
        with _state_lock:
            _stats["applied"].add(img_path)
        return str((out_dir / "f_%03d.jpg").resolve())
    except Exception as e:
        log.warning(f"[wiggle] {Path(img_path).name}: {type(e).__name__}: {str(e)[:160]} → plain still")
        with _state_lock:
            _stats["fallback"].add(img_path)
        return None


def _get_session():
    global _session, _failed_session
    if _session is None and not _failed_session:
        try:
            import onnxruntime as ort
            opts = ort.SessionOptions()
            opts.intra_op_num_threads = max(1, min(4, os.cpu_count() or 1))
            _session = ort.InferenceSession(str(_model_path()), sess_options=opts,
                                            providers=["CPUExecutionProvider"])
        except Exception:
            _failed_session = True
            raise
    if _session is None:
        raise RuntimeError("depth model failed to load earlier in this render")
    return _session


def _estimate_depth(rgb):
    """Relative inverse depth (higher = nearer) at the model's resolution, float32."""
    import numpy as np
    from PIL import Image
    w, h = rgb.size
    # Depth Anything's own preprocessing: keep aspect ratio, scale the side that needs
    # the smaller change toward 518, snap both sides to multiples of 14.
    sw, sh = DEPTH_SHORT_SIDE / w, DEPTH_SHORT_SIDE / h
    s = sw if abs(1 - sw) < abs(1 - sh) else sh
    nw, nh = max(14, round(w * s / 14) * 14), max(14, round(h * s / 14) * 14)
    x = np.asarray(rgb.resize((nw, nh), Image.BICUBIC), dtype=np.float32) / 255.0
    x = (x - np.array(MEAN, dtype=np.float32)) / np.array(STD, dtype=np.float32)
    x = np.ascontiguousarray(x.transpose(2, 0, 1)[None])
    with _session_lock:
        sess = _get_session()
        depth = sess.run(None, {sess.get_inputs()[0].name: x})[0]
    return np.asarray(depth, dtype=np.float32).reshape(depth.shape[-2], depth.shape[-1])


def _build_views(img_path: str, out_dir: Path, fps: int) -> None:
    import numpy as np
    from PIL import Image

    rgb = Image.open(img_path).convert("RGB")
    if max(rgb.size) > MAX_WARP_SIDE:
        k = MAX_WARP_SIDE / max(rgb.size)
        rgb = rgb.resize((round(rgb.width * k), round(rgb.height * k)), Image.LANCZOS)
    W, H = rgb.size

    depth = _estimate_depth(rgb)
    # Pivot on the subject: the median depth of the central region stays still.
    dh, dw = depth.shape
    center = depth[int(dh * 0.25):int(dh * 0.75), int(dw * 0.3):int(dw * 0.7)]
    pivot = float(np.median(center if center.size else depth))
    rel = depth - pivot
    spread = float(np.percentile(np.abs(rel), 98))
    if spread < 1e-6:
        raise ValueError("flat depth map")
    rel = np.clip(rel / spread, -1.2, 1.2)

    # Soften (at depth resolution — cheap) so depth edges smear instead of tear, then
    # upsample to the warp resolution. Pillow can't blur float images, hence numpy.
    rel = _box_blur(_box_blur(rel.astype(np.float32), 2), 2)
    rel = np.asarray(Image.fromarray(rel, mode="F").resize((W, H), Image.BILINEAR), dtype=np.float32)

    src = np.asarray(rgb, dtype=np.float32)
    amp = AMPLITUDE[_state["intensity"]] * W
    xs = np.arange(W, dtype=np.float32)[None, :]
    rows = np.arange(H)[:, None]

    tmp = out_dir.with_name(out_dir.name + f".tmp{threading.get_ident()}")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)

    import math
    n_frames = max(8, round(fps * CYCLE_SECONDS))
    frame_pos = [round(math.sin(2 * math.pi * k / n_frames) * POSITION_STEPS) / POSITION_STEPS
                 for k in range(n_frames)]
    view_files = {}
    for p in sorted(set(frame_pos)):
        sx = np.clip(xs - amp * p * rel, 0, W - 1.001)
        x0 = sx.astype(np.int32)
        wt = (sx - x0)[..., None]
        view = src[rows, x0] * (1.0 - wt) + src[rows, x0 + 1] * wt
        vf = tmp / f"view_{len(view_files)}.jpg"
        Image.fromarray(np.clip(view + 0.5, 0, 255).astype(np.uint8)).save(vf, quality=92)
        view_files[p] = vf

    for n, p in enumerate(frame_pos):
        dst = tmp / f"f_{n:03d}.jpg"
        try:
            os.link(view_files[p], dst)
        except OSError:
            shutil.copyfile(view_files[p], dst)

    if out_dir.exists():                      # a parallel render built it first
        shutil.rmtree(tmp, ignore_errors=True)
        return
    try:
        tmp.rename(out_dir)
    except OSError:
        shutil.rmtree(tmp, ignore_errors=True)
        if not out_dir.exists():
            raise


def _box_blur(a, r: int):
    """Separable box blur with edge padding (two passes approximate a Gaussian)."""
    import numpy as np
    if r < 1:
        return a
    k = 2 * r + 1
    p = np.pad(a, ((r, r), (0, 0)), mode="edge")
    c = np.concatenate([np.zeros((1, p.shape[1])), np.cumsum(p, axis=0, dtype=np.float64)], axis=0)
    a = ((c[k:] - c[:-k]) / k).astype(np.float32)
    p = np.pad(a, ((0, 0), (r, r)), mode="edge")
    c = np.concatenate([np.zeros((p.shape[0], 1)), np.cumsum(p, axis=1, dtype=np.float64)], axis=1)
    return ((c[:, k:] - c[:, :-k]) / k).astype(np.float32)


def _prune_cache() -> None:
    try:
        if not CACHE_DIR.exists():
            return
        cutoff = time.time() - CACHE_MAX_AGE_S
        for d in CACHE_DIR.iterdir():
            if d.is_dir() and d.stat().st_mtime < cutoff:
                shutil.rmtree(d, ignore_errors=True)
    except Exception:
        pass
