"""Step 2 - Generate voiceover audio using Edge TTS (free).

Robustness notes:
- Edge-TTS occasionally STALLS mid-stream (the websocket stops sending data without
  raising). A naive `async for chunk in c.stream()` then blocks forever, and the whole
  step gets killed by the outer process timeout. So every attempt is wrapped in a
  per-segment timeout; a stalled stream is cancelled and retried instead of hanging.
- Segments are generated with light concurrency (a few at a time) so long videos finish
  quickly, and one slow segment never blocks the rest.
- If all streamed attempts fail we fall back to a plain `save()` (audio only, no word
  timings) so the segment still exists; only if THAT fails too is the segment dropped.
"""

import asyncio
import json
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

RATE = "+15%"

# How many segments to synthesize at once. Edge-TTS tolerates a few parallel
# connections fine here; keep it modest to avoid throttling (HTTP 429).
CONCURRENCY  = 3
MAX_ATTEMPTS = 3          # streamed attempts before falling back to plain save()

# Male + female voice per language so the user's gender choice actually changes the
# narration. voice_sex comes from the account's setup ("male" / "female").
LANG_VOICES = {
    "Arabic":  {"male": "ar-SA-HamedNeural",  "female": "ar-SA-ZariyahNeural"},
    "English": {"male": "en-US-AndrewNeural",  "female": "en-US-AriaNeural"},
    "Turkish": {"male": "tr-TR-AhmetNeural",   "female": "tr-TR-EmelNeural"},
    "Spanish": {"male": "es-ES-AlvaroNeural",  "female": "es-ES-ElviraNeural"},
    "French":  {"male": "fr-FR-HenriNeural",   "female": "fr-FR-DeniseNeural"},
    "German":  {"male": "de-DE-ConradNeural",  "female": "de-DE-KatjaNeural"},
    "Hindi":   {"male": "hi-IN-MadhurNeural",  "female": "hi-IN-SwaraNeural"},
}


def _pick_voice(script: dict) -> str:
    lang = script.get("language", "English")
    sex  = (script.get("voice_sex") or "female").lower()
    pair = LANG_VOICES.get(lang, LANG_VOICES["English"])
    return pair.get(sex, pair["female"])


# ── Google Cloud TTS (Chirp3-HD) — premium Arabic/Turkish/multilingual narration ──
# Used for non-English when GOOGLE_TTS_API_KEY is set; far more natural than free
# edge-tts. Chirp3-HD voice "personalities" (Kore, Charon …) are shared across all
# locales, so we keep one female + one male and just swap the locale prefix.
LANG_LOCALES = {
    "Arabic": "ar-XA", "English": "en-US", "Turkish": "tr-TR", "Spanish": "es-ES",
    "French": "fr-FR", "German": "de-DE", "Hindi": "hi-IN", "Italian": "it-IT",
    "Portuguese": "pt-BR", "Russian": "ru-RU", "Japanese": "ja-JP", "Korean": "ko-KR",
    "Dutch": "nl-NL", "Polish": "pl-PL", "Indonesian": "id-ID",
}
GOOGLE_CHIRP_VOICE = {"male": "Charon", "female": "Kore"}   # gender defaults (multilingual)
GOOGLE_RATE = 1.15                                          # match edge's +15% pace
# All 28 Chirp3-HD voice "personalities" (shared across every locale). A guard so an
# arbitrary voice string (e.g. a leftover Kokoro id) is never sent as a voice name.
CHIRP3_VOICES = {
    "Achernar", "Aoede", "Autonoe", "Callirrhoe", "Despina", "Erinome", "Gacrux",
    "Kore", "Laomedeia", "Leda", "Pulcherrima", "Sulafat", "Vindemiatrix", "Zephyr",
    "Achird", "Algenib", "Algieba", "Alnilam", "Charon", "Enceladus", "Fenrir",
    "Iapetus", "Orus", "Puck", "Rasalgethi", "Sadachbia", "Sadaltager", "Schedar",
    "Umbriel", "Zubenelgenubi",
}


def _google_voice(script: dict):
    """Chirp3-HD voice name like 'ar-XA-Chirp3-HD-Kore', or None if the language has
    no mapped locale (→ caller falls back to edge-tts). A user-picked Chirp3-HD voice
    (script['voice']) wins; otherwise fall back to the gender default."""
    loc = LANG_LOCALES.get(script.get("language", "English"))
    if not loc:
        return None
    chosen = (script.get("voice") or "auto").strip()
    if chosen in CHIRP3_VOICES:
        return f"{loc}-Chirp3-HD-{chosen}"
    sex  = (script.get("voice_sex") or "female").lower()
    name = GOOGLE_CHIRP_VOICE.get(sex, GOOGLE_CHIRP_VOICE["female"])
    return f"{loc}-Chirp3-HD-{name}"


def _attempt_timeout(text: str) -> float:
    """Per-attempt ceiling. Real segments stream in 1-10s even when long, so this is
    generous enough never to cut a healthy stream short, yet short enough to catch a
    stalled connection quickly (instead of hanging until the outer process timeout)."""
    return max(30.0, len(text or "") * 0.08)


async def _tts_stream(text: str, path: str, voice: str) -> list:
    """Stream speech to `path` AND capture per-word/sentence timings. Raises on any
    error or if no audio was produced (so the caller can retry)."""
    import edge_tts
    c = edge_tts.Communicate(text, voice, rate=RATE)
    words, got_audio = [], False
    with open(path, "wb") as f:
        async for chunk in c.stream():
            ctype = chunk.get("type", "")
            if ctype == "audio":
                f.write(chunk["data"]); got_audio = True
            elif str(ctype).endswith("Boundary"):
                # Word- or Sentence-level boundary (edge-tts 7.x emits the latter).
                words.append({
                    "text":  chunk.get("text", ""),
                    "start": chunk["offset"] / 1e7,     # 100-ns units → seconds
                    "dur":   chunk["duration"] / 1e7,
                })
    if not got_audio or not (Path(path).exists() and Path(path).stat().st_size > 0):
        raise RuntimeError("no audio produced")
    return words


async def _tts_save(text: str, path: str, voice: str) -> None:
    """Plain save fallback — produces audio but no word-level timings."""
    import edge_tts
    await edge_tts.Communicate(text, voice, rate=RATE).save(path)
    if not (Path(path).exists() and Path(path).stat().st_size > 0):
        raise RuntimeError("save produced no audio")


async def _speak_async(text: str, path: str, voice: str, sem: asyncio.Semaphore):
    """Stream with a per-attempt timeout + retries; fall back to plain save().
    Returns a list of word timings (possibly empty if the save() fallback was used),
    or None if every attempt failed."""
    to = _attempt_timeout(text)
    async with sem:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                return await asyncio.wait_for(_tts_stream(text, path, voice), timeout=to)
            except Exception as e:
                kind = "timed out" if isinstance(e, asyncio.TimeoutError) else type(e).__name__
                log.warning(f"TTS attempt {attempt}/{MAX_ATTEMPTS} ({kind}) for '{text[:30]}…'")
                try: Path(path).unlink()
                except Exception: pass
                if attempt < MAX_ATTEMPTS:
                    await asyncio.sleep(1.0 * attempt)     # brief backoff
        # Last resort: plain save (keeps the audio, loses word-sync for this segment).
        try:
            await asyncio.wait_for(_tts_save(text, path, voice), timeout=to)
            log.warning(f"TTS fell back to plain save for '{text[:30]}…' (no word timings)")
            return []
        except Exception as e:
            log.error(f"TTS failed entirely for '{text[:30]}…': {e}")
            try: Path(path).unlink()
            except Exception: pass
            return None


def speak(text: str, path: str, voice: str):
    """Synchronous single-segment helper (kept for compatibility).
    Returns (success: bool, words: list of {text,start,dur})."""
    async def _one():
        sem = asyncio.Semaphore(1)
        return await _speak_async(text, path, voice, sem)
    try:
        words = asyncio.run(_one())
        if words is not None:
            return True, words
    except Exception as e:
        log.error(f"TTS failed for '{text[:40]}': {e}")
    return False, []


async def _run_jobs(jobs: list, voice: str) -> list:
    """Synthesize all jobs with bounded concurrency; results stay in job order."""
    sem = asyncio.Semaphore(CONCURRENCY)
    async def work(job):
        return job, await _speak_async(job["text"], job["path"], voice, sem)
    return await asyncio.gather(*(work(j) for j in jobs))


def _import_kokoro():
    """Import the sibling Kokoro engine whether step2 is run as a package
    module (pipeline.step2_voiceover) or directly. Returns the module or None."""
    try:
        from . import tts_kokoro
        return tts_kokoro
    except Exception:
        pass
    try:
        from pipeline import tts_kokoro
        return tts_kokoro
    except Exception:
        pass
    try:
        import tts_kokoro
        return tts_kokoro
    except Exception:
        return None


def _resolve_engine(script: dict):
    """Decide which TTS engine to use. Returns (engine, voice, ext).

    English  -> Kokoro local TTS (more natural, offline) when available.
    Arabic / Turkish / others -> edge-tts (Kokoro can't speak them).
    A script may force edge with tts_engine="edge".
    """
    language = script.get("language", "English")
    forced   = (script.get("tts_engine") or "auto").lower()
    want_kokoro = (language == "English" and forced != "edge")

    if want_kokoro:
        kokoro = _import_kokoro()
        if kokoro and kokoro.is_available():
            voice = kokoro.resolve_voice(
                script.get("voice", "auto"),
                script.get("topic", "") or script.get("title", ""),
                script.get("niche", ""),
            )
            try:
                kokoro.warmup(voice)
                log.info(f"TTS engine=kokoro voice={voice}")
                return "kokoro", voice, ".wav"
            except Exception as e:
                log.warning(f"Kokoro warmup failed ({e}); falling back to edge-tts")
        else:
            log.warning("Kokoro not installed/available; using edge-tts for English")

    # Arabic: prefer self-hosted SILMA (Arabic-native, runs on the user's laptop GPU)
    # when SILMA_TTS_URL is set. Far more natural/relatable for Arabic than the
    # general cloud models. Falls back to Google/edge per-segment if the laptop is
    # unreachable (handled inside _run_jobs_silma), so videos never break.
    if language == "Arabic" and forced != "edge" and os.getenv("SILMA_TTS_URL"):
        log.info("TTS engine=silma")
        return "silma", "silma", ".wav"

    # Non-English (or English with Kokoro down): use Google Chirp3-HD when an API
    # key is configured — much more natural for Arabic/Turkish than free edge-tts.
    if forced != "edge":
        gvoice = _google_voice(script)
        if gvoice and os.getenv("GOOGLE_TTS_API_KEY"):
            log.info(f"TTS engine=google voice={gvoice}")
            return "google", gvoice, ".mp3"

    voice = _pick_voice(script)
    log.info(f"TTS engine=edge voice={voice}")
    return "edge", voice, ".mp3"


def _run_jobs_silma(jobs: list, script: dict) -> list:
    """Synthesize Arabic via the self-hosted SILMA server on the user's laptop
    (SILMA_TTS_URL, e.g. a cloudflare/ngrok tunnel). Returns [(job, words)] like the
    other engines (words=[] → subtitles distribute text proportionally). If the
    laptop is unreachable for a segment, falls back to Google (if a key is set) then
    edge-tts so a video is never left with a missing segment."""
    import requests
    base = os.getenv("SILMA_TTS_URL", "").rstrip("/")
    edge_voice = _pick_voice(script)
    # The picked SILMA voice id (e.g. "Karim"); "" / "auto" → server's default voice.
    silma_voice = (script.get("voice") or "").strip()
    if silma_voice.lower() == "auto":
        silma_voice = ""
    results = []
    for job in jobs:
        ok = False
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                r = requests.post(f"{base}/tts",
                                  json={"text": job["text"], "voice": silma_voice}, timeout=180)
                if r.status_code == 200 and r.content and len(r.content) > 1000:
                    Path(job["path"]).write_bytes(r.content)
                    ok = True
                    break
                log.warning(f"SILMA HTTP {r.status_code} ({attempt}/{MAX_ATTEMPTS}) for '{job['text'][:30]}…'")
            except Exception as e:
                log.warning(f"SILMA attempt {attempt}/{MAX_ATTEMPTS} for '{job['text'][:30]}…': {e}")
        if ok:
            results.append((job, []))
            continue
        # Laptop unreachable → cloud fallback so audio is never missing.
        log.warning(f"SILMA failed for '{job['text'][:30]}…' → cloud fallback")
        gvoice = _google_voice(script) if os.getenv("GOOGLE_TTS_API_KEY") else None
        if gvoice:
            results.extend(_run_jobs_google([job], gvoice, script))
        else:
            okk, words = speak(job["text"], job["path"], edge_voice)
            results.append((job, words if okk else None))
    return results


def _run_jobs_kokoro(jobs: list, voice: str) -> list:
    """Synthesize all jobs sequentially with Kokoro. Returns [(job, words|None)],
    matching the shape produced by the edge-tts path."""
    kokoro = _import_kokoro()
    results = []
    for job in jobs:
        words = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                words = kokoro.synth(job["text"], job["path"], voice)
                break
            except Exception as e:
                log.warning(f"Kokoro attempt {attempt}/{MAX_ATTEMPTS} for '{job['text'][:30]}…': {e}")
                try: Path(job["path"]).unlink()
                except Exception: pass
        if words is None:
            log.error(f"Kokoro failed entirely for '{job['text'][:30]}…'")
        results.append((job, words))
    return results


def _run_jobs_google(jobs: list, voice: str, script: dict) -> list:
    """Synthesize all jobs with Google Cloud TTS (Chirp3-HD). Returns [(job, words)]
    like the other engines. words is [] — Chirp3-HD returns no per-word timestamps,
    so subtitles spread each segment's text proportionally across its duration
    (subtitles.py already does this when words is empty). Any segment Google can't
    produce falls back to edge-tts so a video never ends up with a missing segment."""
    import base64
    import requests
    key        = os.getenv("GOOGLE_TTS_API_KEY", "")
    locale     = voice.split("-Chirp3-HD-")[0]               # "ar-XA-Chirp3-HD-Kore" → "ar-XA"
    url        = f"https://texttospeech.googleapis.com/v1/text:synthesize?key={key}"
    edge_voice = _pick_voice(script)                         # for per-segment fallback
    results    = []
    for job in jobs:
        audio_b64 = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                r = requests.post(url, timeout=60, json={
                    "input":       {"text": job["text"]},
                    "voice":       {"languageCode": locale, "name": voice},
                    "audioConfig": {"audioEncoding": "MP3", "speakingRate": GOOGLE_RATE},
                })
                if r.status_code != 200:
                    log.warning(f"Google TTS HTTP {r.status_code} ({attempt}/{MAX_ATTEMPTS}): {r.text[:160]}")
                    continue
                audio_b64 = r.json().get("audioContent")
                if audio_b64:
                    break
                log.warning(f"Google TTS returned no audio ({attempt}/{MAX_ATTEMPTS})")
            except Exception as e:
                log.warning(f"Google TTS attempt {attempt}/{MAX_ATTEMPTS} for '{job['text'][:30]}…': {e}")
        if audio_b64:
            Path(job["path"]).write_bytes(base64.b64decode(audio_b64))
            if Path(job["path"]).exists() and Path(job["path"]).stat().st_size > 0:
                results.append((job, []))           # empty words → proportional subtitles
                continue
        # Google failed for this segment → edge-tts so the audio is never missing.
        log.warning(f"Google TTS failed for '{job['text'][:30]}…' → edge-tts fallback")
        ok, words = speak(job["text"], job["path"], edge_voice)
        results.append((job, words if ok else None))
    return results


def run(script: dict) -> dict:
    log.info("=== STEP 2: Voiceover ===")
    Path("output/audio").mkdir(parents=True, exist_ok=True)

    engine, voice, ext = _resolve_engine(script)
    script["tts_engine"] = engine
    script["tts_voice"]  = voice
    ts    = script.get("created_at", str(script.get("timestamp", "0"))).replace(":", "-").replace(".", "-")[:19]

    # ── Build the job list (carries each segment's final metadata + text/path) ──
    jobs = []
    if script.get("type") == "long":
        for section in script.get("sections", []):
            n = section["number"]
            jobs.append({
                "type":   section.get("type", "section"),
                "number": n,
                "title":  section.get("title", ""),
                "text":   section.get("text", ""),
                "path":   f"output/audio/section_{n:02d}_{ts}{ext}",
            })
    else:
        jobs.append({"type": "hook", "text": script.get("hook", ""),
                     "path": f"output/audio/hook_{ts}{ext}"})
        for fact in script.get("facts", []):
            n = fact["number"]
            jobs.append({"type": "fact", "number": n, "text": fact["text"],
                         "path": f"output/audio/fact_{n:02d}_{ts}{ext}"})
        jobs.append({"type": "outro", "text": script.get("outro", ""),
                     "path": f"output/audio/outro_{ts}{ext}"})

    jobs = [j for j in jobs if (j.get("text") or "").strip()]   # skip empty segments

    # ── Synthesize ──
    #   Kokoro runs synchronously (torch); edge-tts runs concurrent async.
    if engine == "kokoro":
        results = _run_jobs_kokoro(jobs, voice)
    elif engine == "google":
        results = _run_jobs_google(jobs, voice, script)
    elif engine == "silma":
        results = _run_jobs_silma(jobs, script)
    else:
        results = asyncio.run(_run_jobs(jobs, voice))
    segments = []
    for job, words in results:
        if words is None:
            log.error(f"Dropping segment (TTS failed): {job.get('type')} {job.get('number', '')}")
            continue
        seg = {k: v for k, v in job.items()}     # type/number/title/text/path
        seg["words"] = words
        segments.append(seg)

    script["audio_segments"] = segments
    log.info(f"Generated {len(segments)}/{len(jobs)} audio segments")

    if script.get("script_path"):
        with open(script["script_path"], "w", encoding="utf-8") as f:
            json.dump(script, f, indent=2, ensure_ascii=False)

    return script
