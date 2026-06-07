#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# vidimod — one-shot VPS setup for the render engine.
# Safe to re-run (idempotent). Run as root on the Ubuntu VPS:
#   bash vps-setup.sh
# ─────────────────────────────────────────────────────────────────────────────
set -e

ENGINE_REPO="https://github.com/Jamal-daboul/vidimod-engine.git"
ENGINE_DIR="/app/william"
BACKEND_DIR="/app/backend"

echo "==> 1/7  System packages (ffmpeg, python venv, fonts)…"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y ffmpeg python3-venv python3-pip git \
                   fonts-noto-core fonts-noto-cjk fonts-dejavu \
                   libjpeg-dev zlib1g-dev libfreetype6-dev
# Microsoft fonts (real Arial, has Arabic glyphs) — accept EULA, non-fatal if it fails
echo "ttf-mscorefonts-installer msttcorefonts/accepted-mscorefonts-eula select true" | debconf-set-selections
apt-get install -y ttf-mscorefonts-installer || echo "   (mscorefonts skipped — Noto will be used)"
fc-cache -f >/dev/null 2>&1 || true

echo "==> 2/7  Fetch the engine code…"
mkdir -p /app
if [ -d "$ENGINE_DIR/.git" ]; then
  git -C "$ENGINE_DIR" pull --ff-only
else
  git clone "$ENGINE_REPO" "$ENGINE_DIR"
fi

echo "==> 3/7  Engine settings (no secrets — render path needs none)…"
if [ ! -f "$ENGINE_DIR/config/settings.py" ]; then
  cp "$ENGINE_DIR/config/settings.py.example" "$ENGINE_DIR/config/settings.py"
fi

echo "==> 4/7  Python venv + dependencies…"
python3 -m venv "$ENGINE_DIR/venv"
"$ENGINE_DIR/venv/bin/pip" install --upgrade pip wheel
"$ENGINE_DIR/venv/bin/pip" install -r "$ENGINE_DIR/requirements-engine.txt"

echo "==> 5/7  Swap (protects against out-of-memory during video assembly)…"
if [ ! -f /swapfile ]; then
  fallocate -l 2G /swapfile
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  grep -q '/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
  echo "   2 GB swap added."
else
  echo "   swap already present."
fi

echo "==> 6/7  Point the backend at the engine…"
touch "$BACKEND_DIR/.env"
grep -q '^WILLIAM_DIR='    "$BACKEND_DIR/.env" || echo "WILLIAM_DIR=$ENGINE_DIR"                      >> "$BACKEND_DIR/.env"
grep -q '^WILLIAM_PYTHON=' "$BACKEND_DIR/.env" || echo "WILLIAM_PYTHON=$ENGINE_DIR/venv/bin/python"   >> "$BACKEND_DIR/.env"
grep -q '^API_BASE_URL='   "$BACKEND_DIR/.env" || echo "API_BASE_URL=https://api.vidimod.com"         >> "$BACKEND_DIR/.env"

echo "==> 7/7  Restart backend service…"
systemctl restart vidimod || echo "   (could not restart 'vidimod' service — restart it manually)"

echo ""
echo "════════════════════════════════════════════════════"
echo " DONE. Server specs:"
echo "----------------------------------------------------"
free -h
echo "CPU cores: $(nproc)"
df -h / | tail -1
echo "ffmpeg:    $(command -v ffmpeg || echo MISSING)"
echo "engine py: $ENGINE_DIR/venv/bin/python"
echo "════════════════════════════════════════════════════"
echo "Now open https://vidimod.com and try creating a video."
