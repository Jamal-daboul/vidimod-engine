"""ElevenLabs narration (text-to-speech with character timestamps).

Configuration — a per-render value on the script wins over the environment:
  ELEVENLABS_API_KEY                  required; a restricted key with text_to_speech is enough
  ELEVENLABS_MODEL                    default eleven_multilingual_v2      (script: elevenlabs_model)
  ELEVENLABS_VOICE_<LANGUAGE>_<SEX>   e.g. ELEVENLABS_VOICE_ARABIC_MALE    (script: elevenlabs_voices)
  ELEVENLABS_VOICE_<LANGUAGE>         e.g. ELEVENLABS_VOICE_ARABIC
  ELEVENLABS_VOICE_ID                 fallback for every language
  ELEVENLABS_DAILY_CHARACTER_LIMIT    credit ceiling per UTC day (default 10000)
  ELEVENLABS_CONCURRENCY              parallel requests (default 2 — the lowest plan limit)
  ELEVENLABS_SPEED                    voice_settings.speed, 0.7–1.2 (unset = the voice's own)

Spending safety, mirroring the backend text gateway:
  * Credits are reserved in a SQLite ledger BEFORE each call and the daily ceiling is
    enforced atomically, so parallel segments can't overshoot it.
  * No retry after a timeout (the request may have been billed); only a "too many
    concurrent requests" 429 — which is never billed — is retried.
  * An account/billing/permission error opens a circuit for the rest of the render
    instead of failing once per segment.
The caller falls back to free edge-tts for any segment this module can't produce.
"""

import base64
import datetime as dt
import logging
import os
import re
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

import requests

log = logging.getLogger(__name__)

ENGINE_DIR = Path(__file__).resolve().parent.parent
LEDGER = ENGINE_DIR / "output" / "elevenlabs_usage.sqlite3"
API = "https://api.elevenlabs.io/v1/text-to-speech/{voice}/with-timestamps"
OUTPUT_FORMAT = "mp3_44100_128"

LANG_CODES = {"Arabic": "ar", "English": "en", "Turkish": "tr", "Spanish": "es", "French": "fr",
              "German": "de", "Hindi": "hi", "Italian": "it", "Portuguese": "pt", "Russian": "ru",
              "Japanese": "ja", "Korean": "ko", "Dutch": "nl", "Polish": "pl", "Indonesian": "id"}
# Sentence ends. Subtitles only anchor to marks that span multiple words, so we emit
# sentence-level marks exactly like edge-tts does.
_SENTENCE_END = re.compile(r"[.!?؟…\n]")


class AccountError(Exception):
    """Key, permission, plan or quota problem — retrying won't help this render."""


class BudgetError(Exception):
    """The local daily credit ceiling would be exceeded."""


def api_key() -> str:
    return os.getenv("ELEVENLABS_API_KEY", "").strip()


def model(script: dict = None) -> str:
    return ((script or {}).get("elevenlabs_model") or os.getenv("ELEVENLABS_MODEL") or "eleven_multilingual_v2").strip()


def resolve_voice(script: dict) -> str:
    """Voice ID for this script's language and narrator gender, or '' if none is set."""
    lang = str(script.get("language") or "English").strip()
    sex = str(script.get("voice_sex") or "female").strip().lower()
    chosen = script.get("elevenlabs_voices") or {}
    if isinstance(chosen, dict):
        for k in (f"{lang}_{sex}", lang, "default"):
            v = str(chosen.get(k) or chosen.get(k.lower()) or "").strip()
            if v:
                return v
    L = re.sub(r"[^A-Z]", "_", lang.upper())
    for name in (f"ELEVENLABS_VOICE_{L}_{sex.upper()}", f"ELEVENLABS_VOICE_{L}", "ELEVENLABS_VOICE_ID"):
        v = os.getenv(name, "").strip()
        if v:
            return v
    return ""


def daily_limit() -> float:
    try:
        return max(0.0, float(os.getenv("ELEVENLABS_DAILY_CHARACTER_LIMIT", "10000")))
    except ValueError:
        return 10000.0


def credits_per_char(model_id: str) -> float:
    # Flash / Turbo models bill half a credit per character; the others one.
    return 0.5 if ("flash" in model_id or "turbo" in model_id) else 1.0


# ── ledger ───────────────────────────────────────────────────────────────────────

@contextmanager
def _db():
    """Commit on success, roll back on error, and ALWAYS close (a bare `with connect()`
    only commits — it leaks the connection)."""
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(LEDGER, timeout=15)
    try:
        con.row_factory = sqlite3.Row
        con.execute("CREATE TABLE IF NOT EXISTS calls (id TEXT PRIMARY KEY, ts REAL, day TEXT, voice_id TEXT, "
                    "model TEXT, chars INTEGER, credits REAL, reserved REAL, status TEXT, detail TEXT)")
        yield con
        con.commit()
    except BaseException:
        con.rollback()
        raise
    finally:
        con.close()


def _today() -> str:
    return dt.datetime.now(dt.timezone.utc).date().isoformat()


def _reserve(voice_id: str, model_id: str, text: str) -> str:
    est = len(text) * credits_per_char(model_id)
    rid, day = uuid.uuid4().hex, _today()
    with _db() as con:
        con.execute("BEGIN IMMEDIATE")
        used = con.execute("SELECT COALESCE(SUM(MAX(credits, reserved)), 0) FROM calls WHERE day=?", (day,)).fetchone()[0]
        if used + est > daily_limit():
            raise BudgetError(f"daily ElevenLabs ceiling reached ({used:.0f}+{est:.0f} > {daily_limit():.0f} credits)")
        con.execute("INSERT INTO calls VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (rid, time.time(), day, voice_id, model_id, len(text), 0.0, est, "pending", ""))
    return rid


def _finish(rid: str, status: str, credits: float = 0.0, detail: str = "", keep_reservation: bool = False):
    with _db() as con:
        con.execute("UPDATE calls SET status=?, credits=?, detail=?, reserved=CASE WHEN ? THEN reserved ELSE 0 END WHERE id=?",
                    (status, credits, detail[:180], keep_reservation, rid))


def usage_today() -> dict:
    with _db() as con:
        row = con.execute("SELECT COUNT(*) n, COALESCE(SUM(credits),0) c, COALESCE(SUM(reserved),0) r FROM calls WHERE day=?",
                          (_today(),)).fetchone()
    return {"calls": row["n"], "credits": row["c"], "reserved": row["r"], "limit": daily_limit()}


# ── synthesis ────────────────────────────────────────────────────────────────────

def _sentence_marks(alignment: dict) -> list:
    """Turn ElevenLabs character timings into sentence marks [{text,start,dur}]."""
    chars = alignment.get("characters") or []
    starts = alignment.get("character_start_times_seconds") or []
    ends = alignment.get("character_end_times_seconds") or []
    n = min(len(chars), len(starts), len(ends))
    marks, buf, t0, t1 = [], [], None, None

    def flush():
        text = "".join(buf).strip()
        if text and t0 is not None:
            marks.append({"text": text, "start": round(t0, 3), "dur": round(max(t1 - t0, 0.05), 3)})

    for i in range(n):
        c = chars[i]
        if t0 is None and c.strip():
            t0 = float(starts[i])
        buf.append(c)
        if c.strip():
            t1 = float(ends[i])
        if _SENTENCE_END.match(c):
            flush()
            buf, t0, t1 = [], None, None
    flush()
    return marks


_circuit_lock = threading.Lock()


def synth(text: str, path: str, voice_id: str, script: dict, circuit: dict) -> tuple:
    """Speak `text` into `path` (mp3). Returns (marks, credits). Raises AccountError,
    BudgetError, or another exception for a one-off failure."""
    with _circuit_lock:
        if circuit.get("open"):
            raise AccountError(circuit["reason"])
    key, model_id = api_key(), model(script)
    if not key:
        raise AccountError("ELEVENLABS_API_KEY is not set")
    rid = _reserve(voice_id, model_id, text)

    body = {"text": text, "model_id": model_id}
    if ("flash" in model_id or "turbo" in model_id or "v3" in model_id) and LANG_CODES.get(script.get("language")):
        body["language_code"] = LANG_CODES[script["language"]]
    speed = os.getenv("ELEVENLABS_SPEED", "").strip()
    if speed:
        body["voice_settings"] = {"speed": max(0.7, min(1.2, float(speed)))}

    for attempt in range(4):
        try:
            r = requests.post(API.format(voice=voice_id), params={"output_format": OUTPUT_FORMAT},
                              headers={"xi-api-key": key, "content-type": "application/json"},
                              json=body, timeout=(10, 120))
        except Exception as e:
            _finish(rid, "uncertain", detail=f"{type(e).__name__}: request may have been billed", keep_reservation=True)
            raise
        detail = ""
        if r.status_code != 200:
            try:
                d = r.json().get("detail") or {}
                detail = (d.get("code") or d.get("status") or "") if isinstance(d, dict) else str(d)
            except Exception:
                detail = ""
        if r.status_code == 429 and "concurrent" in detail and attempt < 3:
            time.sleep(1.5 * (attempt + 1))                     # not billed — safe to wait and retry
            continue
        break

    if r.status_code != 200:
        msg = f"ElevenLabs HTTP {r.status_code} {detail}".strip()
        _finish(rid, "failed", detail=msg)
        if r.status_code in (401, 402, 403, 404, 422) or (r.status_code == 429 and "concurrent" not in detail):
            with _circuit_lock:
                circuit.update(open=True, reason=msg)
            raise AccountError(msg)
        raise RuntimeError(msg)

    try:
        d = r.json()
        audio = base64.b64decode(d.get("audio_base64") or "")
        if len(audio) < 200:
            raise RuntimeError("empty audio")
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(audio)
        credits = float(r.headers.get("character-cost") or len(text) * credits_per_char(model_id))
        marks = _sentence_marks(d.get("alignment") or d.get("normalized_alignment") or {})
    except Exception as e:
        _finish(rid, "uncertain", detail=f"unreadable response: {type(e).__name__}", keep_reservation=True)
        raise
    _finish(rid, "ok", credits=credits)
    return marks, credits
