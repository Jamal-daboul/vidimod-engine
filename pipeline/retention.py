"""Retention Engine — M2 (Emotional Waveform Planner) + M3 (Interrupt Scheduler).

Pure logic, no AI / no I/O, so it's fast and unit-testable. M1 (the loop-chain script
generator) lives in the web backend because it needs the LLM; M4 (visual cuts) lives in
the step4 assemblers. Everything reads and extends the single VideoScript dict described
in the build spec — see _empty_script() for the shape.

Design rules baked in here:
  • Contrast, not constant intensity  → plan_waveform() makes a tension/release curve.
  • Unpredictability                  → schedule_interrupts() jitters timing (slot-machine)
                                          and rotates type (deck-shuffle, no repeats in 3).
  • Never a dead frame                → gaps are capped at MAX_STILL_SEC.
Everything scales off one `intensity` float (0..1).
"""

import random
from collections import deque

MAX_STILL_SEC = 2.0          # never let one static frame hold longer than this

# Interrupt types (see spec). Split so M4 (visual) and M5 (audio) each take their own.
VISUAL_TYPES = {"hard_cut", "fake_cut", "zoom_punch", "speed_ramp", "shake", "text_pop"}
AUDIO_TYPES  = {"sound_sting", "riser", "silence"}
_LOW_TYPES   = ["fake_cut", "shake", "text_pop"]                 # calm sections
_HIGH_TYPES  = ["hard_cut", "zoom_punch", "speed_ramp", "sound_sting", "riser"]  # peaks

INTENSITY_PRESETS = {"subtle": 0.3, "balanced": 0.6, "aggressive": 0.9}


def resolve_intensity(value) -> float:
    """Accept a preset name or a float; return a 0..1 float."""
    if isinstance(value, str):
        return INTENSITY_PRESETS.get(value.strip().lower(), 0.6)
    try:
        return max(0.0, min(1.0, float(value)))
    except Exception:
        return 0.6


def _lerp(a, b, t):
    return a + (b - a) * max(0.0, min(1.0, t))


def _sawtooth(i, period=5):
    """0..1 that rises across `period` beats then drops sharply (tension→release→tension)."""
    if period <= 1:
        return 1.0
    return (i % period) / (period - 1)


def _beat_starts(beats, duration):
    """Cumulative start time (sec) of each beat from its duration_estimate_sec; if those
    aren't filled yet, spread the beats evenly across `duration`."""
    durs = [float(b.get("duration_estimate_sec") or 0) for b in beats]
    if sum(durs) <= 0 and beats:
        durs = [duration / len(beats)] * len(beats)
    starts, t = [], 0.0
    for d in durs:
        starts.append(t)
        t += d
    return starts


# ── M2 — Emotional Waveform Planner ─────────────────────────────────────────────

def plan_waveform(script, drop_points=None):
    """Fill every beat.intensity_target so the video is a waveform, not a flat wall of
    intensity. Scales by the global slider; forces the peak high and a release right
    before the payoff; if M7 supplied drop timestamps, dips before each drop (recovery)
    and spikes at it (re-grab)."""
    beats = script.get("beats") or []
    g = resolve_intensity(script.get("intensity", 0.6))
    for i, b in enumerate(beats):
        b["intensity_target"] = round(_sawtooth(i, period=5) * g, 3)

    # Release immediately BEFORE the payoff so the payoff lands harder by contrast.
    payoff_idx = next((i for i, b in enumerate(beats) if b.get("is_payoff")), None)
    if payoff_idx is not None and payoff_idx - 1 >= 0:
        beats[payoff_idx - 1]["intensity_target"] = round(0.15 * g, 3)

    if drop_points:
        _apply_recovery_then_regrab(script, drop_points, g)

    # The single peak is the highest point of the whole video — applied LAST so it always
    # wins, even if it lands on the beat we'd otherwise use as the pre-payoff release.
    pid = script.get("peak_beat_id")
    for b in beats:
        if b.get("id") == pid:
            b["intensity_target"] = round(g, 3)
    return script


def _apply_recovery_then_regrab(script, drop_points, g):
    beats = script.get("beats") or []
    if not beats:
        return
    starts = _beat_starts(beats, sum(float(b.get("duration_estimate_sec") or 2) for b in beats))
    def beat_at(ts):
        idx = 0
        for i, s in enumerate(starts):
            if s <= ts:
                idx = i
        return idx
    for ts in drop_points:
        di = beat_at(float(ts))
        beats[di]["intensity_target"] = round(g, 3)                 # re-grab AT the drop
        if di - 1 >= 0:
            beats[di - 1]["intensity_target"] = round(0.2 * g, 3)   # recovery just before


# ── M3 — Interrupt Scheduler ─────────────────────────────────────────────────────

def _pick_type(local_intensity, recent, rng):
    """Pick an interrupt type weighted by local intensity, never one of the last 3 used."""
    pool = _HIGH_TYPES if local_intensity >= 0.5 else _LOW_TYPES
    choices = [t for t in pool if t not in recent] or [t for t in pool] or list(VISUAL_TYPES)
    # Blend in a little of the other pool for variety (but keep the bias).
    other = _LOW_TYPES if pool is _HIGH_TYPES else _HIGH_TYPES
    choices += [t for t in other if t not in recent][:1]
    return rng.choice(choices)


def _make_event(t, etype, li, rng):
    p = {}
    if etype == "fake_cut":
        p = {"zoom": round(rng.uniform(1.08, 1.25), 3),
             "pan": [round(rng.uniform(-0.06, 0.06), 3), round(rng.uniform(-0.06, 0.06), 3)]}
    elif etype == "zoom_punch":
        p = {"factor": round(_lerp(1.12, 1.45, li), 3), "frames": rng.randint(6, 10)}
    elif etype == "speed_ramp":
        p = {"slow": 0.65, "fast": round(_lerp(1.5, 2.2, li), 2)}
    elif etype == "shake":
        p = {"amp_px": round(_lerp(2, 9, li), 1)}
    elif etype == "sound_sting":
        p = {"asset": rng.choice(["whoosh", "pop", "tick"])}
    elif etype == "riser":
        p = {"asset": "riser", "lead_sec": round(_lerp(0.6, 1.2, li), 2)}
    elif etype == "silence":
        p = {"dur_sec": round(rng.uniform(0.3, 0.6), 2)}
    elif etype == "text_pop":
        p = {"word": ""}     # M6 fills the keyword from the caption timeline
    return {"timestamp_sec": round(t, 3), "type": etype, "params": p, "intensity": round(li, 3)}


def schedule_interrupts(script, duration, seed=None):
    """Plan every attention-reset across the whole timeline. Variable gaps (slot-machine)
    + rotating types (no repeat within 3) + a hard cap so no frame ever holds > MAX_STILL_SEC.
    `seed` makes a render reproducible; vary it per video so the channel has no fixed rhythm."""
    rng = random.Random(seed)
    beats = script.get("beats") or []
    g = resolve_intensity(script.get("intensity", 0.6))
    starts = _beat_starts(beats, duration)

    def intensity_at(t):
        for i in range(len(beats)):
            s = starts[i]
            e = starts[i + 1] if i + 1 < len(starts) else duration
            if s <= t < e:
                return resolve_intensity(beats[i].get("intensity_target", g))
        return g

    events, t, recent = [], 0.0, deque(maxlen=3)
    base_gap = _lerp(2.5, 0.8, g)            # aggressive → shorter gaps
    while t < duration:
        li = intensity_at(t)
        gap = rng.uniform(base_gap * 0.6, base_gap * 1.4) * (1.2 - li)
        gap = max(0.4, min(gap, MAX_STILL_SEC))      # never exceed the dead-frame cap
        t += gap
        if t >= duration:
            break
        etype = _pick_type(li, recent, rng)
        recent.append(etype)
        events.append(_make_event(t, etype, li, rng))

    _inject_silence_before_key_lines(events, script, starts)
    events.sort(key=lambda e: e["timestamp_sec"])
    return events


def _inject_silence_before_key_lines(events, script, starts):
    """A sudden short silence right before the payoff + peak beats makes those lines land
    louder by contrast (silence is its own pattern interrupt)."""
    beats = script.get("beats") or []
    key_ids = {script.get("peak_beat_id")} | {b.get("id") for b in beats if b.get("is_payoff")}
    for i, b in enumerate(beats):
        if b.get("id") in key_ids and i < len(starts):
            ts = max(0.0, starts[i] - 0.35)
            events.append({"timestamp_sec": round(ts, 3), "type": "silence",
                           "params": {"dur_sec": 0.45}, "intensity": resolve_intensity(b.get("intensity_target", 0.6))})


# ── Convenience: enrich a VideoScript end-to-end (M2 then M3) ────────────────────

def enrich(script, duration=None, drop_points=None, seed=None):
    """Run M2 + M3 on a VideoScript. Returns it with intensity_targets filled and an
    `interrupts` list added. `duration` defaults to the sum of beat estimates."""
    beats = script.get("beats") or []
    if duration is None:
        duration = sum(float(b.get("duration_estimate_sec") or 2.5) for b in beats) or 30.0
    plan_waveform(script, drop_points=drop_points)
    script["interrupts"] = schedule_interrupts(script, duration, seed=seed)
    script["_duration_sec"] = duration
    return script


# ── Self-test ────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    demo = {
        "topic": "test", "intensity": 0.9, "peak_beat_id": "b4",
        "beats": [
            {"id": f"b{i}", "narration": f"line {i}", "is_payoff": (i == 5),
             "duration_estimate_sec": 3.0} for i in range(8)
        ],
    }
    enrich(demo, seed=7)
    print("intensity curve:", [b["intensity_target"] for b in demo["beats"]])
    print("interrupt count:", len(demo["interrupts"]), "over", demo["_duration_sec"], "s")
    # checks
    gaps = [demo["interrupts"][i+1]["timestamp_sec"] - demo["interrupts"][i]["timestamp_sec"]
            for i in range(len(demo["interrupts"]) - 1)]
    print("max gap:", round(max(gaps), 2), "(must be <= %.1f)" % MAX_STILL_SEC)
    # no type repeated 3x in a row among non-silence events
    seq = [e["type"] for e in demo["interrupts"] if e["type"] != "silence"]
    bad = any(len(set(seq[i:i+3])) == 1 for i in range(len(seq) - 2))
    print("3-in-a-row repeat:", bad, "(must be False)")
