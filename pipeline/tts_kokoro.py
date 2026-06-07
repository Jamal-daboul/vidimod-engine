"""Kokoro local TTS engine — used for ENGLISH voiceover.

Kokoro-82M runs fully offline (weights cached under ~/.cache/huggingface) and
sounds noticeably more natural than edge-tts for English. It also exposes
per-word timestamps, so karaoke / word-synced subtitles keep working.

Arabic and Turkish are NOT supported by Kokoro — those languages stay on
edge-tts (see step2_voiceover.py).

The heavy `torch`/`kokoro` import is lazy (only when first used), so importing
this module is cheap and never breaks the pipeline if Kokoro isn't installed.
"""
import logging
from pathlib import Path

log = logging.getLogger(__name__)

SAMPLE_RATE = 24000          # Kokoro outputs 24 kHz
SPEED       = 1.0            # 1.0 = natural narration pace

# ── Curated English voices shown in the UI ──────────────────────────────────
# id -> {"lang": 'a'(American)|'b'(British), "label_en", "label_ar"}
KOKORO_VOICES = {
    "af_heart":   {"lang": "a", "label_en": "Heart — natural female",   "label_ar": "هارت — أنثى طبيعية"},
    "af_bella":   {"lang": "a", "label_en": "Bella — expressive female", "label_ar": "بيلا — أنثى معبّرة"},
    "af_nicole":  {"lang": "a", "label_en": "Nicole — soft / calm female","label_ar": "نيكول — أنثى هادئة"},
    "am_onyx":    {"lang": "a", "label_en": "Onyx — deep male narrator", "label_ar": "أونيكس — راوٍ رجولي عميق"},
    "am_michael": {"lang": "a", "label_en": "Michael — warm male",       "label_ar": "مايكل — رجل دافئ"},
    "am_adam":    {"lang": "a", "label_en": "Adam — casual male",        "label_ar": "آدم — رجل عفوي"},
    "am_eric":    {"lang": "a", "label_en": "Eric — energetic male",     "label_ar": "إريك — رجل حماسي"},
    "bm_george":  {"lang": "b", "label_en": "George — British male",     "label_ar": "جورج — رجل بريطاني"},
    "bf_emma":    {"lang": "b", "label_en": "Emma — British female",     "label_ar": "إيما — أنثى بريطانية"},
}

DEFAULT_VOICE = "af_heart"

# ── Auto voice selection by topic / niche keywords ──────────────────────────
# First matching rule wins; falls back to DEFAULT_VOICE.
_AUTO_RULES = [
    (("finance", "money", "invest", "stock", "econom", "wealth", "rich",
      "business", "entrepreneur", "market", "crypto", "budget", "bank"), "am_onyx"),
    (("history", "war", "ancient", "empire", "documentary", "civiliz",
      "conspiracy", "mystery", "unsolved"), "am_onyx"),
    (("horror", "scary", "creepy", "dark", "crime", "murder", "haunt", "ghost"), "am_onyx"),
    (("tech", "ai ", "artificial", "robot", "gadget", "software", "computer",
      "future", "science", "space", "physics", "coding", "programming"), "am_michael"),
    (("motivat", "success", "discipline", "mindset", "hustle", "grind",
      "fitness", "gym", "workout", "sport", "champion"), "am_eric"),
    (("calm", "relax", "sleep", "meditat", "wellness", "mindful", "asmr",
      "soothing", "healing", "anxiety"), "af_nicole"),
    (("beauty", "fashion", "makeup", "lifestyle", "vlog", "daily", "travel",
      "food", "recipe", "cooking"), "af_heart"),
    (("kids", "fun", "comedy", "funny", "cartoon", "game", "gaming", "play"), "af_bella"),
]


def auto_select_voice(topic: str = "", niche: str = "") -> str:
    """Pick the most fitting voice from the video's topic/niche text."""
    text = f"{topic} {niche}".lower()
    for keys, voice in _AUTO_RULES:
        if any(k in text for k in keys):
            return voice
    return DEFAULT_VOICE


def resolve_voice(voice: str, topic: str = "", niche: str = "") -> str:
    """Return a valid Kokoro voice id. 'auto' / unknown -> topic-based pick."""
    if voice and voice in KOKORO_VOICES:
        return voice
    return auto_select_voice(topic, niche)


# ── Pipeline cache (one per language code) ──────────────────────────────────
_PIPELINES: dict = {}


def _get_pipeline(lang_code: str):
    if lang_code not in _PIPELINES:
        from kokoro import KPipeline          # heavy import (torch) — lazy
        _PIPELINES[lang_code] = KPipeline(lang_code=lang_code)
    return _PIPELINES[lang_code]


def is_available() -> bool:
    """True if Kokoro can be imported (installed)."""
    try:
        import kokoro  # noqa: F401
        import soundfile  # noqa: F401
        return True
    except Exception:
        return False


def warmup(voice: str = DEFAULT_VOICE) -> None:
    """Pre-load the pipeline so the first segment isn't slow. May raise."""
    lang_code = KOKORO_VOICES.get(voice, {"lang": "a"})["lang"]
    _get_pipeline(lang_code)


def _tokens_to_words(tokens) -> list:
    """Convert Kokoro token objects to edge-tts-style {text,start,dur} dicts."""
    out = []
    for tk in tokens or []:
        txt = (getattr(tk, "text", "") or "").strip()
        st  = getattr(tk, "start_ts", None)
        en  = getattr(tk, "end_ts", None)
        if not txt or st is None or en is None:
            continue
        out.append({"text": txt, "start": float(st), "dur": max(float(en) - float(st), 0.05)})
    return out


def synth(text: str, path: str, voice: str) -> list:
    """Generate a WAV at `path` and return per-word timings [{text,start,dur}].
    Raises on failure so the caller can retry/fallback."""
    import numpy as np
    import soundfile as sf

    lang_code = KOKORO_VOICES.get(voice, KOKORO_VOICES[DEFAULT_VOICE])["lang"]
    pipe = _get_pipeline(lang_code)

    chunks, words, offset = [], [], 0.0
    for r in pipe(text, voice=voice, speed=SPEED):
        audio = getattr(r, "audio", None)
        if audio is None:
            continue
        # torch.Tensor -> numpy
        audio = audio.detach().cpu().numpy() if hasattr(audio, "detach") else np.asarray(audio)
        if audio.size == 0:
            continue
        # Token timings are relative to this chunk; shift by accumulated offset.
        for w in _tokens_to_words(getattr(r, "tokens", None)):
            w["start"] += offset
            words.append(w)
        offset += len(audio) / SAMPLE_RATE
        chunks.append(audio)

    if not chunks:
        raise RuntimeError("kokoro produced no audio")

    full = np.concatenate(chunks) if len(chunks) > 1 else chunks[0]
    sf.write(path, full, SAMPLE_RATE)
    if not (Path(path).exists() and Path(path).stat().st_size > 0):
        raise RuntimeError("kokoro wrote no file")
    return words
