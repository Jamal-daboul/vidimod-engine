# vidimod-engine

The video render pipeline for **vidimod studio pro**. The web backend
(`vidimod-web/backend`) shells out to this package to generate voiceover (step 2)
and assemble the final video (step 4).

## Layout
- `pipeline/` — the render code (step2 voiceover, step4 assemble/long, subtitles, music)
- `music/` — background-music library
- `voice_samples/` — preview clips for the voice picker
- `fonts/` — custom subtitle fonts
- `config/settings.py` — API keys (NOT in git; create from `settings.py.example`)

## Install (Linux VPS)
```bash
sudo apt update && sudo apt install -y ffmpeg python3-venv \
     fonts-noto-core fonts-dejavu ttf-mscorefonts-installer
python3 -m venv venv
./venv/bin/pip install -r requirements-engine.txt
cp config/settings.py.example config/settings.py   # then fill in keys
```

The web backend points at this via `WILLIAM_DIR` and `WILLIAM_PYTHON` env vars.

> Note: `kokoro`/torch are intentionally not installed — English voice
> auto-falls back to `edge-tts`. Add kokoro later only if the server has spare RAM.
