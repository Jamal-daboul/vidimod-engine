# Seed a starter set of SILMA reference voices. Uses Google Chirp3-HD to make clean,
# single-speaker Arabic reference clips (24 kHz wav + transcript), which SILMA then
# clones — so each gives a DISTINCT, natural SILMA voice. Run once on the VPS:
#   cd /app/silma && source venv/bin/activate
#   GOOGLE_TTS_API_KEY=$(grep GOOGLE_TTS_API_KEY /app/backend/.env | cut -d= -f2) python /app/william/silma/seed_voices.py
# Then: systemctl restart silma
#
# Replace any voice later by dropping your own clean 5-8s clip as voices/<name>.wav.
import base64
import os

import requests

KEY = os.getenv("GOOGLE_TTS_API_KEY", "").strip()
if not KEY:                                  # fall back to the backend .env
    try:
        for line in open("/app/backend/.env", encoding="utf-8"):
            if line.startswith("GOOGLE_TTS_API_KEY="):
                KEY = line.split("=", 1)[1].strip().strip('"').strip("'")
                break
    except Exception:
        pass
assert KEY, "GOOGLE_TTS_API_KEY not found (env or /app/backend/.env)"

OUT = os.getenv("SILMA_VOICES_DIR", "voices")
os.makedirs(OUT, exist_ok=True)

# A neutral, expressive reference sentence (defines timbre/style, not the content).
SAMPLE = ("مرحباً بكم. سنروي لكم اليوم قصة مشوّقة مليئة بالحقائق المدهشة "
          "التي ستغيّر طريقة تفكيركم تماماً.")

# name -> Chirp3-HD voice used only as the reference timbre. 4 female + 4 male.
SEEDS = {
    "Hana":  "ar-XA-Chirp3-HD-Kore",      # ♀ warm
    "Lina":  "ar-XA-Chirp3-HD-Aoede",     # ♀ bright
    "Salma": "ar-XA-Chirp3-HD-Leda",      # ♀ youthful
    "Nour":  "ar-XA-Chirp3-HD-Zephyr",    # ♀ soft
    "Karim": "ar-XA-Chirp3-HD-Charon",    # ♂ deep
    "Omar":  "ar-XA-Chirp3-HD-Fenrir",    # ♂ strong
    "Ziad":  "ar-XA-Chirp3-HD-Puck",      # ♂ upbeat
    "Tarek": "ar-XA-Chirp3-HD-Orus",      # ♂ clear
}

url = f"https://texttospeech.googleapis.com/v1/text:synthesize?key={KEY}"
for name, voice in SEEDS.items():
    try:
        r = requests.post(url, timeout=60, json={
            "input": {"text": SAMPLE},
            "voice": {"languageCode": "ar-XA", "name": voice},
            # LINEAR16 @ 24 kHz → a ready-to-use wav reference, no ffmpeg needed.
            "audioConfig": {"audioEncoding": "LINEAR16", "sampleRateHertz": 24000},
        })
        if r.status_code != 200:
            print(f"[{name}] FAILED {r.status_code}: {r.text[:120]}")
            continue
        wav = base64.b64decode(r.json()["audioContent"])
        open(os.path.join(OUT, f"{name}.wav"), "wb").write(wav)
        open(os.path.join(OUT, f"{name}.txt"), "w", encoding="utf-8").write(SAMPLE)
        print(f"[{name}] OK -> {OUT}/{name}.wav")
    except Exception as e:
        print(f"[{name}] ERROR {e}")

print("Done. Now: systemctl restart silma")
