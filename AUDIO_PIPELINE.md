# Audio assembly & the "tiny cuts between segments" — READ BEFORE TOUCHING step4_long.py

This document exists because we spent many rounds chasing tiny audible cuts /
clicks / blips at the shot boundaries of rendered videos. The final fix was
ARCHITECTURAL, not a parameter tweak. If you change the assembly code without
understanding this, the artifacts WILL come back.

## TL;DR — the one rule

**The voiceover must live on ONE continuous audio track, encoded exactly once.**
Never put per-shot audio back inside the per-shot video segments. The moment
each video join is also an audio join, boundary artifacts are guaranteed —
no combination of pads/fades/gates fully fixes them (we tried; see below).

## The symptom history (all real, all measured)

- Tiny cut / click between every pair of shots.
- Last word of a shot clipped or ending abruptly.
- First letter of the next shot's sentence clipped.
- A faint "millisecond voice" whisper between shots.
- Background music seeming to "cut out" in the pauses.

## Dead ends — tried, verified insufficient, do NOT retry

1. **Silent pads + crossfade curves** (`apad` tails, `acrossfade c1/c2=nofade`):
   fixed one boundary artifact, exposed the next.
2. **Hard trailing trims** (silenceremove at -40dB): chopped the natural decay
   of final words → audible cut at every shot end.
3. **Aggressive head trims** (-45dB): clipped fricative sentence-openers
   ("s", "f", "h" ramp up from below -50dB). Use -50dB + 0.15s keeps.
4. **Loudness gain exposing TTS noise**: two-pass loudnorm (+10dB or more)
   amplifies the TTS breath/noise tail to ~-25dB → audible wisp between shots.
   Fix that survived: gentle `agate` (threshold 0.013, ratio 2, release 250ms)
   AFTER loudnorm — never a hard trim.
5. **Sidechain ducking** (`sidechaincompress`): BROKEN on our ffmpeg builds —
   verified 4.0s of input produced 2.69s of output (stream dies mid-file), and
   its gain pumping punched ~25ms near-silent holes right after each sentence
   onset. Do not use sidechaincompress in this pipeline. The music is a steady
   bed at ~1.7x the user's volume instead.
6. **Per-segment audio generally**: separate AAC encodes meeting at a seam,
   `-shortest` frame quantization, acrossfade alignment, gate/pad interactions
   — each join had multiple independent ways to click.

## The architecture that finally fixed it (current code)

In `pipeline/step4_long.py :: _assemble_web_long`:

1. **Voice phase** — each shot's TTS file → clean mono WAV:
   `trim lead/tail (-50dB, keep 0.15s) → two-pass loudnorm (-14 LUFS, measured
   through the speech compressor) → gentle agate`. Exact lengths measured.
2. **One timeline** — voices scheduled at `starts[i]`:
   `LEAD (0.3s) + voice + GAP + voice + ... + TAIL (0.45s)`,
   GAP = 0.55s (shot_gap on) / 0.40s (off). Uniform, predictable pacing.
3. **Video-only segments** — each shot's picture is rendered `-an` with a
   PRESCRIBED length so that each 0.3s dissolve ENDS exactly when the next
   shot's voice begins. `LEAD == XF == 0.3` is deliberate: every shot's voice
   starts 0.3s into its own segment, so the subtitle shift is a constant 0.3s
   (`build_segment_ass(..., shift=XF)`).
4. **Single mux** — all voice WAVs are `adelay`-placed and `amix`-summed
   (normalize=0) in one filtergraph, the music bed added in the same graph,
   and the ENTIRE audio is AAC-encoded once (192k). Video is stream-copied.

   Result: there is **no audio join at any shot boundary** — the artifact
   class doesn't exist anymore. Bonus: 1 voice encode generation instead of 3.

## Loudness / voice-quality decisions that also stand

- Target **-14 LUFS / -1.0 dBTP** (YouTube's playback reference; YouTube never
  boosts quieter videos, so anything below -14 simply plays quieter than other
  shorts). Two-pass measured loudnorm — single-pass lands several dB low on
  short clips.
- Speech compressor (`acompressor` 3:1 @ -20dB) before loudnorm: density is
  what makes a voice FEEL loud on a phone speaker.
- AAC at 192k everywhere a voice passes through.

## How to verify any future change (the lab method)

```bash
# 1. download a rendered video from the server, extract mono wav
ffmpeg -i final.mp4 -ac 1 -ar 16000 a.wav
# 2. python: 25ms RMS timeline -> find voice spans/gaps;
#    4ms windows inside each gap -> the bed must be flat (no >10dB steps,
#    no holes below the music bed), voice onsets must RAMP (-35 -27 -22 ...)
#    and decays must descend smoothly (-23 -28 -33 -41 ...).
# 3. for pipeline changes: build synthetic speech-shaped voices
#    (fade-in 0.2s, fade-out 0.4s, plus a -45dB noise tail to simulate TTS
#    breath) and run _assemble_web_long directly, then inspect as above.
```

Render-verify BEFORE pushing. Every fix in this saga that was pushed on theory
alone bounced; every fix verified with waveform measurements stuck.
