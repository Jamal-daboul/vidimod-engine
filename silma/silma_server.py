# SILMA Arabic TTS server. Runs on the VPS (CPU) at localhost:8020, OR on a laptop
# GPU. vidimod's step2_voiceover calls POST /tts for Arabic when SILMA_TTS_URL is set.
#
# Put a clean 5-8s Arabic voice clip as `reference.wav` beside this file — that's the
# voice every Arabic video is cloned to. See silma/README.md for full setup.
import os
import tempfile
import threading

from fastapi import FastAPI, Response
from pydantic import BaseModel
from silma_tts.api import SilmaTTS

REF_FILE = os.getenv("SILMA_REF_WAV", "reference.wav")
REF_TEXT = os.getenv("SILMA_REF_TEXT") or None   # None → SILMA auto-transcribes

app   = FastAPI()
_tts  = SilmaTTS()
_lock = threading.Lock()   # model isn't thread-safe — serialize requests


class Req(BaseModel):
    text: str
    speed: float = 1.0


@app.get("/health")
def health():
    return {"ok": True, "ref": REF_FILE}


@app.post("/tts")
def tts(r: Req):
    out = tempfile.mktemp(suffix=".wav")
    with _lock:
        _tts.infer(ref_file=REF_FILE, ref_text=REF_TEXT, gen_text=r.text,
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
