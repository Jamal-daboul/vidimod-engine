"""Content Planner - weekly/daily video plan driven by channel identity."""

import json
import logging
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

PLAN_FILE = Path("memory/content_plan.json")


def generate_plan(days: int = 7) -> dict:
    from pipeline.brain import ask
    from config.settings import (
        CHANNEL_NICHE, CHANNEL_TONE, CHANNEL_CONTENT_IDEA,
        CONTENT_FOCUS, LONG_VIDEOS_PER_DAY, SHORTS_PER_DAY, LONG_VIDEO_MINUTES,
    )

    # Collect already-used topics to avoid repeats
    used = []
    existing = load_plan()
    for v in existing.get("videos", []):
        if v.get("topic"):
            used.append(v["topic"])

    avoid = f"\nAvoid these already-planned topics: {json.dumps(used[-50:])}" if used else ""

    idea = CHANNEL_CONTENT_IDEA.strip()
    idea_ctx = f"\n\nChannel identity / content focus:\n{idea}" if idea else ""

    focus_map = {
        "long":   f"PRIORITIZE long videos — {LONG_VIDEOS_PER_DAY} long + {SHORTS_PER_DAY} Shorts per day",
        "shorts": f"PRIORITIZE Shorts — {SHORTS_PER_DAY} Shorts + {LONG_VIDEOS_PER_DAY} long per day",
        "mixed":  f"Balanced — {LONG_VIDEOS_PER_DAY} long + {SHORTS_PER_DAY} Shorts per day",
    }
    focus_desc = focus_map.get(CONTENT_FOCUS, f"{LONG_VIDEOS_PER_DAY} long + {SHORTS_PER_DAY} Shorts per day")

    total_long   = days * LONG_VIDEOS_PER_DAY
    total_shorts = days * SHORTS_PER_DAY

    prompt = f"""You are a YouTube content strategist. Create a {days}-day content plan.

Channel niche: {CHANNEL_NICHE}
Tone: {CHANNEL_TONE}
Strategy: {focus_desc}{idea_ctx}{avoid}

Generate exactly {total_long} long-form video ideas ({LONG_VIDEO_MINUTES}-min deep dives)
and {total_shorts} Shorts ideas (quick punchy facts/lists).

Long videos must be unique, in-depth, and directly aligned with the channel identity above.
Shorts should complement the long videos with related quick-hit content.

Return ONLY valid JSON:
{{
  "videos": [
    {{"type": "long",  "topic": "...", "angle": "what makes it unique/compelling", "day": 1}},
    {{"type": "short", "topic": "...", "angle": "quick hook idea", "day": 1}},
    {{"type": "short", "topic": "...", "angle": "quick hook idea", "day": 1}},
    ... (repeat for all {days} days — {LONG_VIDEOS_PER_DAY} long + {SHORTS_PER_DAY} shorts each day)
  ]
}}"""

    raw = ask(prompt, fast=False, max_tokens=3500)
    if not raw:
        log.error("Content plan generation returned empty")
        return {}

    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    raw = raw.strip()

    try:
        start = raw.find("{")
        end   = raw.rfind("}") + 1
        if start >= 0 and end > start:
            plan = json.loads(raw[start:end])
            if plan.get("videos"):
                plan["generated_at"] = datetime.now().isoformat()
                plan["days"]         = days
                log.info(f"Content plan: {len(plan['videos'])} videos over {days} days")
                return plan
    except Exception as e:
        log.error(f"Content plan parse error: {e}")

    return {}


def save_plan(plan: dict):
    PLAN_FILE.parent.mkdir(exist_ok=True)
    PLAN_FILE.write_text(json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info(f"Plan saved: {len(plan.get('videos', []))} videos")


def load_plan() -> dict:
    if not PLAN_FILE.exists():
        return {}
    try:
        return json.loads(PLAN_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def get_next_topics(video_type: str, count: int = 1, used_topics: list = None) -> list:
    """Return next undone planned topics of the given type."""
    plan   = load_plan()
    used   = set(used_topics or [])
    result = []
    for v in plan.get("videos", []):
        if (v.get("type") == video_type
                and not v.get("done")
                and v.get("topic") not in used):
            result.append(v["topic"])
            if len(result) >= count:
                break
    return result


def mark_done(topic: str):
    plan = load_plan()
    for v in plan.get("videos", []):
        if v.get("topic") == topic and not v.get("done"):
            v["done"]    = True
            v["done_at"] = datetime.now().isoformat()
            break
    save_plan(plan)


def remaining_count(video_type: str = None) -> int:
    plan = load_plan()
    return sum(
        1 for v in plan.get("videos", [])
        if not v.get("done") and (video_type is None or v.get("type") == video_type)
    )


def format_plan_message(show_days: int = 7) -> str:
    plan   = load_plan()
    videos = plan.get("videos", [])
    if not videos:
        return "No content plan yet. Send /plan generate to create one."

    gen = plan.get("generated_at", "")[:10]
    lines = [f"*Content Plan* (generated {gen})\n"]
    shown = 0
    current_day = None
    for v in videos:
        if v.get("done"):
            continue
        day = v.get("day", "?")
        if day != current_day:
            if shown >= show_days * 3:
                break
            current_day = day
            lines.append(f"\n*Day {day}:*")
        label = "Long " if v.get("type") == "long" else "Short"
        lines.append(f"  [{label}] {v.get('topic', '?')}")
        shown += 1

    total  = len(videos)
    done   = sum(1 for v in videos if v.get("done"))
    lines.append(f"\n_{done}/{total} done_")
    return "\n".join(lines)
