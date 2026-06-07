"""Step 1 - Research topics and write script using Claude API."""

import json
import logging
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)


def get_topics(exclude: list = None, count: int = 5) -> list:
    from pipeline.brain import ask
    from config.settings import CHANNEL_NICHE

    avoid = ""
    if exclude:
        recent = exclude[-30:]
        avoid = f" Do NOT use any of these already-used topics: {json.dumps(recent)}."

    raw = ask(
        f'Generate {count} YouTube Shorts topic ideas for a "{CHANNEL_NICHE}" channel. '
        f'Format: "Top 5 Facts About X" or "5 Shocking Facts About X". '
        f'Make them surprising and very shareable.{avoid} '
        f'Reply ONLY with a JSON array of {count} strings, no explanation.',
        fast=True
    )
    try:
        s = raw.find("[")
        e = raw.rfind("]") + 1
        if s >= 0 and e > s:
            topics = json.loads(raw[s:e])
            if isinstance(topics, list) and len(topics) >= 3:
                log.info(f"Topics: {topics}")
                return topics
    except Exception as ex:
        log.warning(f"Topic parse failed: {ex}")

    return [
        "10 Mind-Blowing Facts About the Human Brain",
        "Top 10 Shocking Facts About Space",
        "10 Incredible Facts About Ancient Egypt",
        "Top 10 Facts About Everyday Technology",
        "10 Fascinating Ocean Creature Facts",
    ]


def write_script(topic: str, style: str = None) -> dict:
    from pipeline.brain import write_script as brain_write
    from config.settings import CHANNEL_NICHE, CHANNEL_TONE

    tone = f"{CHANNEL_TONE}. {style}" if style else CHANNEL_TONE

    for attempt in range(3):
        script = brain_write(topic, CHANNEL_NICHE, tone)
        if script:
            return script
        log.warning(f"Script attempt {attempt+1} failed, retrying...")

    raise RuntimeError(f"Could not generate script for '{topic}' after 3 attempts")


def run(topic: str = None, style: str = None) -> dict:
    log.info("=== STEP 1: Research & Script ===")

    if not topic:
        topics = get_topics()
        topic = topics[0]

    script = write_script(topic, style)
    script["topic"]      = topic
    script["created_at"] = datetime.now().isoformat()

    Path("output").mkdir(exist_ok=True)
    ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = f"output/script_{ts}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(script, f, indent=2, ensure_ascii=False)

    script["script_path"] = path
    log.info(f"Script saved: {path}")
    return script