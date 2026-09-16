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

    # ElevenLabs is an ADMIN choice (TTS_PROVIDER / script tts_provider), so it outranks the
    # per-video default the frontend always sends ('google' for non-English). Only an
    # explicit 'edge' opts a script out. Needs a key AND a voice for this language.
    provider = str(script.get("tts_provider") or os.getenv("TTS_PROVIDER", "auto")).strip().lower()
    if forced == "elevenlabs" or (provider == "elevenlabs" and forced != "edge"):
        from pipeline import tts_elevenlabs as el
        vid = el.resolve_voice(script)
        if el.api_key() and vid:
            log.info(f"TTS engine=elevenlabs voice={vid} model={el.model(script)}")
            return "elevenlabs", vid, ".mp3"
        missing = "ELEVENLABS_API_KEY" if not el.api_key() else f"a voice for {language}"
        log.warning(f"ElevenLabs selected but {missing} is not set → normal routing")
    # English defaults to Kokoro (natural, offline), but the user can force Google
    # Chirp3-HD ("google") or edge ("edge"). Only take the Kokoro path when it's
    # actually wanted — a "google" pick used to fall through here and ignore the
    # chosen Chirp voice, producing a wrong (often male) Kokoro voice.
    want_kokoro = (language == "English" and forced in ("auto", "kokoro"))

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

    # Non-English (or English with Kokoro down): use Google Chirp3-HD when an API
    # key is configured — much more natural for Arabic/Turkish than free edge-tts.
    if forced != "edge":
        gvoice = _google_voice(script)
        if gvoice and os.getenv("GOOGLE_TTS_API_KEY"):
            log.info(f"TTS engine=google voice={gvoice}")
            return "google", gvoice, ".wav"   # LINEAR16 (uncompressed) — see _run_jobs_google

    voice = _pick_voice(script)
    log.info(f"TTS engine=edge voice={voice}")
    return "edge", voice, ".mp3"


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
                job["tts_engine"], job["tts_model"] = "kokoro", voice
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
    like the other engines. words is [] — Chirp3-HD returns no per-word timings,
    so subtitles spread each segment's text proportionally across its duration.
    Any segment Google can't produce falls back to edge-tts so a video never ends up
    with a missing segment. An account-level error (billing disabled, bad key, no
    permission) stops calling Google for the rest of the render — it used to burn
    MAX_ATTEMPTS failed calls on EVERY segment before falling back."""
    import base64
    import requests
    key        = os.getenv("GOOGLE_TTS_API_KEY", "")
    locale     = voice.split("-Chirp3-HD-")[0]               # "ar-XA-Chirp3-HD-Kore" → "ar-XA"
    url        = f"https://texttospeech.googleapis.com/v1/text:synthesize?key={key}"
    results, fallback, dead = [], [], ""
    for job in jobs:
        audio_b64, last_err = None, ""
        for attempt in range(1, MAX_ATTEMPTS + 1):
            if dead:
                break
            try:
                r = requests.post(url, timeout=60, json={
                    "input":       {"text": job["text"]},
                    "voice":       {"languageCode": locale, "name": voice},
                    # LINEAR16 @ 24 kHz = Chirp3-HD's native, uncompressed output. The old
                    # "MP3" export was band-limited and made the HD voice sound muffled /
                    # "telephoney" next to edge-tts. This is the highest fidelity the model
                    # produces; the REST API returns it already wrapped in a WAV header.
                    "audioConfig": {"audioEncoding":   "LINEAR16",
                                    "sampleRateHertz": 24000,
                                    "speakingRate":    GOOGLE_RATE},
                })
                if r.status_code != 200:
                    last_err = f"Google TTS HTTP {r.status_code}"
                    try:
                        err = r.json().get("error", {})
                        reason = next((d.get("reason") for d in err.get("details", [])
                                       if isinstance(d, dict) and d.get("reason")), "")
                        last_err = " ".join(x for x in (last_err, err.get("status", ""), reason) if x)
                    except Exception:
                        pass
                    log.warning(f"{last_err} ({attempt}/{MAX_ATTEMPTS}): {r.text[:160]}")
                    if r.status_code in (400, 401, 403, 404):
                        dead = last_err
                        log.error(f"Google TTS unusable for this render ({dead}) → edge-tts for the rest")
                    continue
                audio_b64 = r.json().get("audioContent")
                if audio_b64:
                    break
                last_err = "Google TTS returned no audio"
                log.warning(f"{last_err} ({attempt}/{MAX_ATTEMPTS})")
            except Exception as e:
                last_err = f"Google TTS {type(e).__name__}"
                log.warning(f"Google TTS attempt {attempt}/{MAX_ATTEMPTS} for '{job['text'][:30]}…': {e}")
        if audio_b64:
            Path(job["path"]).write_bytes(base64.b64decode(audio_b64))
            if Path(job["path"]).exists() and Path(job["path"]).stat().st_size > 0:
                job["tts_engine"], job["tts_model"] = "google", voice
                results.append((job, []))           # empty words → proportional subtitles
                continue
        job["tts_fallback_reason"] = dead or last_err or "Google TTS failed"
        fallback.append(job)
    results += _edge_fallback(fallback, script, "google")
    return results


def _edge_fallback(jobs: list, script: dict, failed_engine: str) -> list:
    """Re-synthesize the jobs another engine couldn't produce with free edge-tts
    (concurrently). Edge writes MP3, so each job's path switches to .mp3."""
    if not jobs:
        return []
    edge_voice = _pick_voice(script)
    for job in jobs:
        job["path"] = str(Path(job["path"]).with_suffix(".mp3"))
    log.warning(f"{failed_engine} failed for {len(jobs)} segment(s) → edge-tts ({edge_voice})")
    out = asyncio.run(_run_jobs(jobs, edge_voice))
    for job, _words in out:
        job["tts_engine"], job["tts_model"] = "edge", edge_voice
    return out


def _run_jobs_elevenlabs(jobs: list, voice_id: str, script: dict) -> list:
    """Synthesize all jobs with ElevenLabs (bounded concurrency). Segments it can't
    produce — daily ceiling, account error, one-off failure — fall back to edge-tts."""
    import concurrent.futures as cf
    from pipeline import tts_elevenlabs as el
    model_id, circuit = el.model(script), {}
    workers = max(1, int(os.getenv("ELEVENLABS_CONCURRENCY", "2") or 2))
    results, fallback = [], []

    def work(job):
        try:
            marks, credits = el.synth(job["text"], job["path"], voice_id, script, circuit)
            job.update(tts_engine="elevenlabs", tts_model=model_id, tts_voice=voice_id, tts_credits=credits)
            return job, marks
        except Exception as e:
            job["tts_fallback_reason"] = f"{type(e).__name__}: {str(e)[:140]}"
            log.warning(f"ElevenLabs → fallback for '{job['text'][:30]}…': {job['tts_fallback_reason']}")
            try:
                Path(job["path"]).unlink()
            except Exception:
                pass
            return job, None

    with cf.ThreadPoolExecutor(max_workers=workers) as pool:
        for job, marks in pool.map(work, jobs):
            if marks is None:
                fallback.append(job)
            else:
                results.append((job, marks))
    results += _edge_fallback(fallback, script, "elevenlabs")
    return results


def _use_human_audio(src: str, dest: str) -> bool:
    """Transcode a user's RECORDED voiceover into the segment audio (24k mono wav).
    Denoise/normalize already ran at upload; here we just make the format consistent so
    it drops into the timeline exactly like a TTS clip. Lets a creator narrate in their
    own human voice while AI still does everything else."""
    import subprocess
    try:
        from .step4_long import _get_ffmpeg
    except Exception:
        from pipeline.step4_long import _get_ffmpeg
    try:
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        subprocess.run([_get_ffmpeg(), "-y", "-i", src, "-ar", "24000", "-ac", "1",
                        "-c:a", "pcm_s16le", dest], capture_output=True, timeout=180)
        return Path(dest).exists() and Path(dest).stat().st_size > 0
    except Exception as e:
        log.warning(f"human voice transcode failed: {e}")
        return False


def run(script: dict) -> dict:
    log.info("=== STEP 2: Voiceover ===")
    Path("output/audio").mkdir(parents=True, exist_ok=True)

    # Shots the creator recorded themselves → {seg_key: local audio path}. Those segments
    # use the human voice; the rest fall through to AI TTS (the feature is per-shot/optional).
    human = script.get("human_voices") or {}
    def _jkey(j):
        return j["type"] if j["type"] in ("hook", "outro") else f"fact_{j.get('number')}"

    engine, voice, ext = _resolve_engine(script)
    script["tts_requested_engine"] = engine   # what was SELECTED; tts_engine below = what RAN
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

    # Segments the creator recorded skip TTS entirely; only the rest go to the AI engine.
    human_ids = {id(j) for j in jobs if human.get(_jkey(j)) and Path(human[_jkey(j)]).exists()}
    tts_jobs  = [j for j in jobs if id(j) not in human_ids]

    # ── Synthesize the AI segments ──
    #   Kokoro runs synchronously (torch); edge-tts runs concurrent async.
    if not tts_jobs:
        results = []
    elif engine == "kokoro":
        results = _run_jobs_kokoro(tts_jobs, voice)
    elif engine == "google":
        results = _run_jobs_google(tts_jobs, voice, script)
    elif engine == "elevenlabs":
        results = _run_jobs_elevenlabs(tts_jobs, voice, script)
    else:
        results = asyncio.run(_run_jobs(tts_jobs, voice))
        for job, _w in results:
            job["tts_engine"], job["tts_model"] = "edge", voice
    done = {id(job): words for job, words in results}

    # Reassemble in the original segment order (human + AI), so the timeline lines up.
    segments = []
    for j in jobs:
        if id(j) in human_ids:
            dest = str(Path(j["path"]).with_suffix(".wav"))
            if _use_human_audio(human[_jkey(j)], dest):
                seg = {k: v for k, v in j.items()}
                seg["path"], seg["words"], seg["human"] = dest, [], True   # no word timings → subtitles split proportionally
                seg["tts_engine"], seg["tts_model"] = "human", "recorded voice"
                segments.append(seg)
                log.info(f"Voiceover: HUMAN recording for {_jkey(j)}")
            else:
                log.error(f"Human voice failed for {_jkey(j)} — dropping segment")
            continue
        words = done.get(id(j))
        if words is None:
            log.error(f"Dropping segment (TTS failed): {j.get('type')} {j.get('number', '')}")
            continue
        seg = {k: v for k, v in j.items()}     # type/number/title/text/path
        seg["words"] = words
        segments.append(seg)

    script["audio_segments"] = segments
    # Record what ACTUALLY produced the audio — per segment and overall. tts_engine used to
    # keep the SELECTED engine, so a render whose Google calls all failed over to edge still
    # said 'google' (and the backend billed it at Google's per-character rate).
    used = sorted({s.get("tts_engine") for s in segments if s.get("tts_engine") not in (None, "human")})
    script["tts_engines_used"] = used
    script["tts_engine"] = used[0] if len(used) == 1 else ("mixed" if used else "none")
    fell_back = [s for s in segments if s.get("tts_fallback_reason")]
    note = f" fallback={len(fell_back)} ({fell_back[0]['tts_fallback_reason']})" if fell_back else ""
    log.info(f"Generated {len(segments)}/{len(jobs)} audio segments — requested={engine} "
             f"actual={used or ['none']}{note}")

    if script.get("script_path"):
        with open(script["script_path"], "w", encoding="utf-8") as f:
            json.dump(script, f, indent=2, ensure_ascii=False)

    return script
