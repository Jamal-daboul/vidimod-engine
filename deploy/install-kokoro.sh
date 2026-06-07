#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Enable Kokoro (local, natural English voices) on the VPS.
# Installs CPU-only PyTorch (no CUDA → smaller) + Kokoro into the engine venv,
# then pre-downloads the model so the first real render doesn't time out.
# Safe to re-run. Run as root:  bash /app/william/deploy/install-kokoro.sh
# ─────────────────────────────────────────────────────────────────────────────
set -e
ENGINE_DIR="/app/william"
PY="$ENGINE_DIR/venv/bin/python"
PIP="$ENGINE_DIR/venv/bin/pip"

echo "==> 1/4  espeak-ng (phoneme fallback Kokoro uses)…"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y >/dev/null
apt-get install -y espeak-ng

echo "==> 2/4  CPU-only PyTorch (no CUDA, much smaller)…"
"$PIP" install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu

echo "==> 3/4  Kokoro TTS + soundfile…"
"$PIP" install --no-cache-dir "kokoro>=0.9.4" soundfile

echo "==> 4/4  Pre-downloading the Kokoro model (~330MB, one time) + test synth…"
cd "$ENGINE_DIR"
"$PY" - <<'PYEOF'
import sys
sys.path.insert(0, '/app/william')
try:
    from pipeline import tts_kokoro
    print("kokoro importable:", tts_kokoro.is_available())
    words = tts_kokoro.synth("Hello, this is a Kokoro voice test for vidimod.",
                             "/tmp/kokoro_test.wav", "am_michael")
    import os
    sz = os.path.getsize("/tmp/kokoro_test.wav") if os.path.exists("/tmp/kokoro_test.wav") else 0
    print(f"KOKORO OK  — wav {sz} bytes, {len(words)} word timings")
except Exception as e:
    import traceback; traceback.print_exc()
    print("KOKORO FAILED:", e)
PYEOF

echo ""
echo "==== Free memory now ===="
free -h
echo "Done. If you saw 'KOKORO OK', English videos will use Kokoro voices."
