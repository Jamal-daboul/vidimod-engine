# SILMA Arabic TTS server (multi-voice). Runs on the VPS (CPU) at localhost:8020.
# vidimod's step2_voiceover calls POST /tts for Arabic when SILMA_TTS_URL is set.
#
# Voices live in  /app/silma/voices/<name>.wav  (optional <name>.txt = transcript).
# Seed a starter set with seed_voices.py, or drop in your own 5-8s Arabic clips.
import glob
import os
import tempfile
import threading

from fastapi import FastAPI, HTTPException, Response
from pydantic import BaseModel
from silma_tts.api import SilmaTTS

VOICES_DIR   = os.getenv("SILMA_VOICES_DIR", "voices")
FALLBACK_REF = os.getenv("SILMA_REF_WAV", "reference.wav")   # used if voices/ is empty

app   = FastAPI()
_tts  = SilmaTTS()
_lock = threading.Lock()   # model isn't thread-safe — serialize requests


def _list_voices():
    names = [os.path.splitext(os.path.basename(w))[0]
             for w in sorted(glob.glob(os.path.join(VOICES_DIR, "*.wav")))]
    if not names and os.path.exists(FALLBACK_REF):
        names = ["default"]
    return names


def _voice_path(name: str):
    if name and name != "default":
        p = os.path.join(VOICES_DIR, f"{name}.wav")
        if os.path.exists(p):
            return p
    wavs = sorted(glob.glob(os.path.join(VOICES_DIR, "*.wav")))
    return wavs[0] if wavs else FALLBACK_REF   # first voice, else the bundled ref


def _voice_text(wav_path: str):
    txt = os.path.splitext(wav_path)[0] + ".txt"
    if os.path.exists(txt):
        try:
            return (open(txt, encoding="utf-8").read().strip() or None)
        except Exception:
            return None
    return None   # SILMA auto-transcribes


class Req(BaseModel):
    text: str
    voice: str = ""
    speed: float = 1.0


@app.get("/health")
def health():
    return {"ok": True, "voices": _list_voices()}


@app.get("/voices")
def voices():
    return {"voices": _list_voices()}


@app.post("/tts")
def tts(r: Req):
    ref = _voice_path(r.voice)
    if not os.path.exists(ref):
        raise HTTPException(status_code=503, detail="no reference voice on server")
    out = tempfile.mktemp(suffix=".wav")
    with _lock:
        _tts.infer(ref_file=ref, ref_text=_voice_text(ref), gen_text=r.text,
                   file_wave=out, seed=None, speed=r.speed)
    data = open(out, "rb").read()
    try:
        os.remove(out)
    except Exception:
        pass
    return Response(content=data, media_type="audio/wav")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8020)
