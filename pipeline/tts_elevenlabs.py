"""ElevenLabs narration (text-to-speech with character timestamps).

Configuration — a per-render value on the script wins over the environment:
  ELEVENLABS_API_KEY                  required; a restricted key with text_to_speech is enough
  ELEVENLABS_MODEL                    default eleven_multilingual_v2      (script: elevenlabs_model)
  ELEVENLABS_VOICE_<LANGUAGE>_<SEX>   e.g. ELEVENLABS_VOICE_ARABIC_MALE    (script: elevenlabs_voices)
  ELEVENLABS_VOICE_<LANGUAGE>         e.g. ELEVENLABS_VOICE_ARABIC
  ELEVENLABS_VOICE_ID                 fallback for every language
  ELEVENLABS_DAILY_CHARACTER_LIMIT    credit ceiling per UTC day (default 10000)
  ELEVENLABS_CONCURRENCY              parallel requests (default 2 — the lowest plan limit)
  ELEVENLABS_SPEED                    voice_settings.speed, 0.7–1.2 (script: elevenlabs_speed)
  script elevenlabs_limits            {daily, per_video, reserve} credits, set by the owner in Video Lab

A user picks a voice by its ID (script voice + tts_engine='elevenlabs'); the owner's per-language
choice (elevenlabs_voices / env) is the fallback.

Spending safety, mirroring the backend text gateway:
  * Credits are reserved in a SQLite ledger BEFORE each call and the daily ceiling is
    enforced atomically, so parallel segments can't overshoot it.
  * No retry after a timeout (the request may have been billed). Unbilled conflicts are
    retried with backoff: 429 "too many concurrent requests" and 409 "already_running"
    (ElevenLabs rejects a parallel request while it is still loading a cold library voice).
  * An account/billing/permission error opens a circuit for the rest of the render
    instead of failing once per segment.
The caller stops the render if narration fails; alternate providers are disabled.
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


VOICE_ID = re.compile(r"^[A-Za-z0-9]{20}$")


def resolve_voice(script: dict) -> str:
    """Voice ID for this script's language and narrator gender, or '' if none is set.
    A voice the user picked (script voice, tts_engine='elevenlabs') wins."""
    picked = str(script.get("voice") or "").strip()
    if str(script.get("tts_engine") or "").lower() == "elevenlabs" and VOICE_ID.match(picked):
        return picked
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


DEFAULT_LIMITS = {"daily": 3000.0, "per_video": 1200.0, "reserve": 1000.0}


def limits(script: dict = None) -> dict:
    """Owner's credit limits: Video Lab values on the script win, then env, then defaults."""
    out = dict(DEFAULT_LIMITS)
    env = os.getenv("ELEVENLABS_DAILY_CHARACTER_LIMIT", "").strip()
    if env:
        try:
            out["daily"] = float(env)
        except ValueError:
            pass
    for k, v in ((script or {}).get("elevenlabs_limits") or {}).items():
        try:
            if k in out and v is not None and float(v) >= 0:
                out[k] = float(v)
        except (TypeError, ValueError):
            pass
    return out


def daily_limit(script: dict = None) -> float:
    return limits(script)["daily"]


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


def credit_ratio(model_id: str) -> float:
    """Credits actually billed per character, learned from the last 40 successful calls
    (plans differ: 1.0 on Free, ~0.25 measured on the paid plan). Falls back to the
    model's list rate until there are 3 data points."""
    try:
        with _db() as con:
            rows = con.execute("SELECT credits, chars FROM calls WHERE status='ok' AND chars>0 AND credits>0 "
                               "AND model=? ORDER BY ts DESC LIMIT 40", (model_id,)).fetchall()
    except sqlite3.Error:
        rows = []
    if len(rows) < 3:
        return credits_per_char(model_id)
    ratio = sum(r["credits"] for r in rows) / sum(r["chars"] for r in rows)
    return max(0.05, min(credits_per_char(model_id), ratio))


def estimate(chars: int, model_id: str) -> float:
    """Conservative credit estimate: learned rate + 25%, never above the list rate."""
    return chars * min(credits_per_char(model_id), credit_ratio(model_id) * 1.25)


_ACCOUNT_CACHE = LEDGER.with_name("elevenlabs_account.json")


def account_remaining():
    """Credits left on the ElevenLabs account, or None if the key can't read it
    (needs the User → Read permission). Cached 10 minutes."""
    import json
    try:
        c = json.loads(_ACCOUNT_CACHE.read_text(encoding="utf-8"))
        if time.time() - c.get("ts", 0) < 600:
            return c.get("remaining")
    except Exception:
        pass
    remaining = None
    try:
        r = requests.get("https://api.elevenlabs.io/v1/user/subscription", headers={"xi-api-key": api_key()}, timeout=15)
        if r.ok:
            d = r.json()
            remaining = max(0.0, float(d.get("character_limit") or 0) - float(d.get("character_count") or 0))
    except Exception:
        remaining = None
    try:
        _ACCOUNT_CACHE.parent.mkdir(parents=True, exist_ok=True)
        _ACCOUNT_CACHE.write_text(json.dumps({"ts": time.time(), "remaining": remaining}), encoding="utf-8")
    except Exception:
        pass
    return remaining


def used_today() -> float:
    with _db() as con:
        return con.execute("SELECT COALESCE(SUM(MAX(credits, reserved)), 0) FROM calls WHERE day=?", (_today(),)).fetchone()[0]


def preflight(chars: int, script: dict) -> tuple:
    """(ok, reason, estimate) for a WHOLE render before any audio is made, so a video
    never switches voice halfway because a limit was hit mid-render."""
    model_id, lim = model(script), limits(script)
    est = estimate(chars, model_id)
    if est > lim["per_video"]:
        return False, f"video needs ~{est:.0f} credits, over the per-video limit of {lim['per_video']:.0f}", est
    used = used_today()
    if used + est > lim["daily"]:
        return False, f"daily ElevenLabs limit: {used:.0f} used + ~{est:.0f} > {lim['daily']:.0f} credits", est
    remaining = account_remaining()
    if remaining is not None and remaining - est < lim["reserve"]:
        return False, f"account balance protected: {remaining:.0f} left, keeping {lim['reserve']:.0f} in reserve", est
    return True, "", est


def _reserve(voice_id: str, model_id: str, text: str, script: dict = None) -> str:
    est = estimate(len(text), model_id)
    rid, day = uuid.uuid4().hex, _today()
    ceiling = daily_limit(script)
    with _db() as con:
        con.execute("BEGIN IMMEDIATE")
        used = con.execute("SELECT COALESCE(SUM(MAX(credits, reserved)), 0) FROM calls WHERE day=?", (day,)).fetchone()[0]
        if used + est > ceiling:
            raise BudgetError(f"daily ElevenLabs ceiling reached ({used:.0f}+{est:.0f} > {ceiling:.0f} credits)")
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
    rid = _reserve(voice_id, model_id, text, script)

    body = {"text": text, "model_id": model_id}
    if ("flash" in model_id or "turbo" in model_id or "v3" in model_id) and LANG_CODES.get(script.get("language")):
        body["language_code"] = LANG_CODES[script["language"]]
    speed = script.get("elevenlabs_speed") or os.getenv("ELEVENLABS_SPEED", "").strip()
    try:
        if speed and abs(float(speed) - 1.0) > 1e-3:
            body["voice_settings"] = {"speed": max(0.7, min(1.2, float(speed)))}
    except (TypeError, ValueError):
        pass

    for attempt in range(6):
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
        retryable = (r.status_code == 429 and "concurrent" in detail) or r.status_code == 409 \
            or r.status_code in (500, 502, 503, 504)
        if retryable and attempt < 5:
            time.sleep(2.0 * (attempt + 1))                     # not billed — safe to wait and retry
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
