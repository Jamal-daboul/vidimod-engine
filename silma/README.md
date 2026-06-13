# SILMA Arabic TTS — run on the VPS (CPU, no laptop, no tunnel)

This runs SILMA directly on the VPS as a small localhost service. vidimod's engine
already calls `SILMA_TTS_URL` — here it's just `http://127.0.0.1:8020`, so no tunnel
and no laptop. The model loads once and stays warm.

Run everything **on the VPS**.

---

## 0. Check the box can handle it (paste me the output)
```bash
nproc && free -h
```
SILMA on CPU wants roughly **≥4 CPU cores** and **≥3 GB free RAM** to be comfortable.
Fewer is possible but slower. (A diffusion TTS on CPU is ~5–15s of compute per
sentence — fine for autopilot batches, a little slow for instant videos.)

## 1. Install
```bash
sudo apt update && sudo apt install -y ffmpeg python3-venv
mkdir -p /app/silma && cd /app/silma
python3 -m venv venv && source venv/bin/activate
pip install torch --index-url https://download.pytorch.org/whl/cpu   # CPU-only torch
pip install silma-tts fastapi uvicorn
```

## 2. Add the server + a reference voice
- Copy `silma_server.py` (from this folder) into `/app/silma/`.
- Put a clean **5–8s Arabic voice clip** as `/app/silma/reference.wav` — this is the
  voice every Arabic video gets cloned to. (Upload one, or record it.)

## 3. Run it as a permanent service (systemd)
Create `/etc/systemd/system/silma.service`:
```ini
[Unit]
Description=SILMA Arabic TTS
After=network.target

[Service]
WorkingDirectory=/app/silma
Environment=CUDA_VISIBLE_DEVICES=
ExecStart=/app/silma/venv/bin/python /app/silma/silma_server.py
Restart=always
User=root

[Install]
WantedBy=multi-user.target
```
Then:
```bash
systemctl daemon-reload
systemctl enable --now silma
journalctl -u silma -f          # first start downloads the model (~once); wait for "Uvicorn running"
```
Test (in another shell):
```bash
curl http://127.0.0.1:8020/health
```

## 4. Point vidimod at it
```bash
echo 'SILMA_TTS_URL=http://127.0.0.1:8020' >> /app/backend/.env
cd /app/william && git pull        # engine already has the SILMA hook
systemctl restart vidimod
```

## 5. Verify
Make an **Arabic** video, then:
```bash
journalctl -u vidimod -f | grep "TTS engine"
```
Want `TTS engine=silma`. If SILMA is down/slow it auto-falls back to Google/edge and
logs `SILMA failed … → cloud fallback`, so videos never break.

---

### If it's too slow or RAM-tight
- Lower concurrency is automatic (requests are serialized).
- If `free -h` shows little RAM, this box may be too small → the laptop-GPU path is
  the fallback (see README.md).
- Speed scales with cores; a 2-core box will be sluggish.
