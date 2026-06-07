"""Step 3 - Fetch images: FLUX.1 Schnell (primary) → Pixabay → gradient."""

import base64
import json
import logging
import re
import requests
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

log = logging.getLogger(__name__)

from config.settings import PIXABAY_API_KEY
TOGETHER_API_KEY = "tgp_v1_A65HYGTcNd4nTSuVVEDjPPWpQ2a_qNbNmDQ_u9OlzNs"

_STOPWORDS = {
    "the","a","an","is","was","were","are","that","this","and","or","but",
    "in","on","at","to","for","of","with","it","its","by","from","you",
    "your","they","their","which","who","when","what","how","why","also",
    "just","more","than","has","have","had","been","one","two","three",
    "four","five","six","seven","eight","nine","ten","top","facts","about",
    "will","did","your","know","these","some","very","even","so","do",
    "uses","have","body","each","all","not","can","its","our","any","both",
}


# ── Prompt builder ─────────────────────────────────────────────────────────────

def build_flux_prompt(fact_text: str, topic: str) -> str:
    words = re.sub(r"[^\w\s]", "", fact_text.lower()).split()
    keywords = [w for w in words if w not in _STOPWORDS and len(w) > 3][:4]
    kw_str = ", ".join(keywords) if keywords else topic
    return (
        f"cinematic photorealistic vertical shot, {kw_str}, "
        f"dramatic professional lighting, 4K ultra detailed, "
        f"no text no words, award winning photography"
    )


# ── FLUX.1 Schnell via Together.ai ─────────────────────────────────────────────

def generate_flux_image(prompt: str, out_path: str, landscape: bool = False) -> bool:
    # Portrait (9:16) for Shorts, landscape (16:9) for long videos
    w, h = (1344, 768) if landscape else (768, 1344)
    try:
        r = requests.post(
            "https://api.together.xyz/v1/images/generations",
            headers={
                "Authorization": f"Bearer {TOGETHER_API_KEY}",
                "Content-Type":  "application/json",
            },
            json={
                "model":           "black-forest-labs/FLUX.1-schnell",
                "prompt":          prompt,
                "width":           w,
                "height":          h,
                "steps":           4,
                "n":               1,
                "response_format": "b64_json",
            },
            timeout=60,
        )
        if r.status_code != 200:
            log.warning(f"FLUX error {r.status_code}: {r.text[:200]}")
            return False

        data   = r.json()
        b64    = data["data"][0].get("b64_json", "")
        if not b64:
            log.warning("FLUX returned empty b64_json")
            return False

        img_bytes = base64.b64decode(b64)
        if len(img_bytes) < 10_000:
            log.warning(f"FLUX image too small: {len(img_bytes)} bytes")
            return False

        Path(out_path).write_bytes(img_bytes)
        log.info(f"FLUX.1 Schnell: {Path(out_path).name} ({len(img_bytes)//1024}KB)")
        return True

    except Exception as e:
        log.warning(f"FLUX generation failed: {e}")
        return False


# ── Pixabay ────────────────────────────────────────────────────────────────────

def fetch_pixabay_image(keywords: str, topic: str, out_path: str, landscape: bool = False) -> bool:
    preferred_orient = "horizontal" if landscape else "vertical"

    def _search(query: str, orientation: str | None) -> list:
        params = {
            "key":        PIXABAY_API_KEY,
            "q":          query,
            "image_type": "photo",
            "per_page":   10,
            "safesearch": "true",
            "order":      "popular",
        }
        if orientation:
            params["orientation"] = orientation
        try:
            r = requests.get("https://pixabay.com/api/", params=params, timeout=20)
            if r.status_code == 200:
                return r.json().get("hits", [])
        except Exception as e:
            log.warning(f"Pixabay request error: {e}")
        return []

    hits = (_search(keywords, preferred_orient) or _search(keywords, None)
            or _search(topic, preferred_orient) or _search(topic, None))
    if not hits:
        return False

    url = hits[0].get("largeImageURL") or hits[0].get("webformatURL")
    if not url:
        return False

    try:
        r = requests.get(url, timeout=30, stream=True)
        if r.status_code != 200:
            return False
        with open(out_path, "wb") as fp:
            for chunk in r.iter_content(8192):
                fp.write(chunk)
        size = Path(out_path).stat().st_size
        if size > 10_000:
            log.info(f"Pixabay: {Path(out_path).name} ({size//1024}KB) q='{keywords}'")
            return True
    except Exception as e:
        log.warning(f"Pixabay download failed: {e}")
    return False


# ── Gradient fallback ──────────────────────────────────────────────────────────

def make_gradient_fallback(out_path: str, landscape: bool = False) -> bool:
    try:
        from PIL import Image as PILImage
        import numpy as np
        W_g, H_g = (1920, 1080) if landscape else (1080, 1920)
        arr = np.zeros((H_g, W_g, 3), dtype=np.uint8)
        for y in range(H_g):
            t = y / H_g
            arr[y, :, 0] = int(10 * (1 - t))
            arr[y, :, 1] = int(20 * (1 - t))
            arr[y, :, 2] = int(80 * (1 - t))
        PILImage.fromarray(arr, "RGB").save(out_path, "JPEG", quality=85)
        log.info(f"Gradient fallback: {Path(out_path).name}")
        return True
    except Exception as e:
        log.error(f"Gradient fallback failed: {e}")
        return False


# ── Per-shot worker ────────────────────────────────────────────────────────────

def generate_one(shot: dict, topic: str, images_dir: Path, landscape: bool = False) -> dict:
    sid      = shot.get("id", "shot")
    text     = shot.get("text", "")
    keywords = shot.get("search_query", "") or " ".join(
        [w for w in re.sub(r"[^\w\s]", "", text.lower()).split()
         if w not in _STOPWORDS and len(w) > 3][:3]
    )
    out_path = str(images_dir / f"{sid}.jpg")

    # Tier 0: FLUX.1 Schnell
    prompt = build_flux_prompt(text, topic)
    if generate_flux_image(prompt, out_path, landscape=landscape):
        shot["image_path"]   = out_path
        shot["image_source"] = "flux"
        return shot

    # Tier 1: Pixabay with shot keywords
    if fetch_pixabay_image(keywords, topic, out_path, landscape=landscape):
        shot["image_path"]   = out_path
        shot["image_source"] = "pixabay"
        return shot

    # Tier 2: Pixabay with just the topic word
    topic_kw = re.sub(r"[^\w\s]", "", topic.lower()).split()[0] if topic else "nature"
    if fetch_pixabay_image(topic_kw, topic, out_path, landscape=landscape):
        shot["image_path"]   = out_path
        shot["image_source"] = "pixabay_topic"
        return shot

    # Tier 3: gradient
    make_gradient_fallback(out_path, landscape=landscape)
    shot["image_path"]   = out_path
    shot["image_source"] = "gradient"
    return shot


# ── Main ───────────────────────────────────────────────────────────────────────

def run(script: dict) -> dict:
    log.info("=== STEP 3: Image Fetch (FLUX.1 Schnell > Pixabay > Gradient) ===")

    video_type = script.get("type", "short")
    landscape  = (video_type == "long")
    topic      = script.get("topic", "interesting facts")
    ts         = script.get("timestamp", int(time.time()))

    # Auto-build shots from the script if not already present
    shots = script.get("shots", [])
    if not shots:
        if landscape:
            # Long video: one shot per section
            for section in script.get("sections", []):
                shots.append({
                    "id":           f"section_{section['number']}",
                    "type":         section.get("type", "section"),
                    "number":       section["number"],
                    "text":         section.get("text", ""),
                    "search_query": section.get("search_query", ""),
                })
        else:
            # Short video: hook + facts + outro
            if script.get("hook"):
                shots.append({"id": "hook_0", "type": "hook", "number": 0,
                              "text": script["hook"], "search_query": ""})
            for fact in script.get("facts", []):
                shots.append({"id": f"fact_{fact['number']}", "type": "fact",
                              "number": fact["number"], "text": fact["text"], "search_query": ""})
            if script.get("outro"):
                shots.append({"id": "outro_0", "type": "outro", "number": 0,
                              "text": script["outro"], "search_query": ""})

    if not shots:
        log.warning("No shots found in script")
        return script

    out_dir = Path(f"output/images/imgs_{ts}")
    out_dir.mkdir(parents=True, exist_ok=True)
    log.info(f"Output dir: {out_dir}  |  {len(shots)} shots  |  topic: '{topic[:60]}'")

    updated_shots = []
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {pool.submit(generate_one, shot.copy(), topic, out_dir, landscape): shot
                   for shot in shots}
        for fut in as_completed(futures):
            updated_shots.append(fut.result())

    # Preserve original order
    order = {s["id"]: i for i, s in enumerate(shots)}
    updated_shots.sort(key=lambda s: order.get(s["id"], 999))

    # Build images list
    images = []
    for s in updated_shots:
        if s.get("image_path"):
            stype = s.get("type", "fact")
            num   = s.get("number")
            if num is None:
                try:
                    num = int(s["id"].split("_")[1])
                except Exception:
                    num = 0
            images.append({"segment_type": stype, "number": num, "path": s["image_path"]})

    # Summary
    sources = [s.get("image_source", "?") for s in updated_shots]
    log.info(
        f"Done: {sources.count('flux')} flux, "
        f"{sources.count('pixabay') + sources.count('pixabay_topic')} pixabay, "
        f"{sources.count('gradient')} gradient"
    )

    print("\n=== IMAGE SOURCES ===")
    for s in updated_shots:
        print(f"  {s['id']:12s} -> {s.get('image_source','?'):15s} -> {s.get('image_path','none')}")

    script["shots"]  = updated_shots
    script["images"] = images
    script["clips"]  = [i["path"] for i in images]

    if script.get("script_path"):
        with open(script["script_path"], "w", encoding="utf-8") as f:
            json.dump(script, f, indent=2, ensure_ascii=False)

    return script
