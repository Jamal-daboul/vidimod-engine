"""Step 1L - Research topics and write scripts for long-form YouTube videos."""

import json
import logging
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

SECTIONS = 5   # content sections (not counting intro/outro)


def get_long_topics(exclude: list = None, count: int = 3) -> list:
    from pipeline.brain import ask
    from config.settings import CHANNEL_NICHE, CHANNEL_CONTENT_IDEA, LONG_VIDEO_MINUTES

    avoid = ""
    if exclude:
        avoid = f" Do NOT reuse any of these topics: {json.dumps(exclude[-40:])}."

    idea = CHANNEL_CONTENT_IDEA.strip()
    idea_ctx = f"\n\nChannel identity / content focus:\n{idea}" if idea else ""

    raw = ask(
        f'Generate {count} long-form YouTube video topic ideas for a "{CHANNEL_NICHE}" channel. '
        f'These are {LONG_VIDEO_MINUTES}-minute in-depth educational videos (NOT Shorts).{idea_ctx}\n'
        f'Title formats: "The Complete Guide to X", "Why X Changed Everything", '
        f'"The Truth About X", "Everything You Need to Know About X", "X Explained".{avoid} '
        f'Reply ONLY with a JSON array of {count} strings, no explanation.',
        fast=True,
    )
    try:
        s = raw.find("[")
        e = raw.rfind("]") + 1
        if s >= 0 and e > s:
            topics = json.loads(raw[s:e])
            if isinstance(topics, list) and topics:
                log.info(f"Long topics: {topics}")
                return topics
    except Exception as ex:
        log.warning(f"Long topic parse failed: {ex}")

    return [
        "The Complete Guide to the Human Brain",
        "Why Ancient Civilizations Were More Advanced Than We Think",
        "Everything You Need to Know About Black Holes",
    ]


def write_long_script(topic: str, style: str = None) -> dict:
    from pipeline.brain import ask
    from config.settings import CHANNEL_NICHE, CHANNEL_TONE, LONG_VIDEO_MINUTES, CHANNEL_CONTENT_IDEA

    tone = f"{CHANNEL_TONE}. {style}" if style else CHANNEL_TONE
    words_per_section = int(LONG_VIDEO_MINUTES * 130 / (SECTIONS + 2))  # +2 for intro/outro

    idea = CHANNEL_CONTENT_IDEA.strip()
    idea_ctx = f"\n\nChannel identity:\n{idea}" if idea else ""

    prompt = f"""Write a detailed YouTube video script for a long-form video.

TOPIC: {topic}
NICHE: {CHANNEL_NICHE}
TONE: {tone}
TARGET: {LONG_VIDEO_MINUTES} minutes (~{LONG_VIDEO_MINUTES * 130} words total){idea_ctx}

Structure: intro + {SECTIONS} main sections + outro = {SECTIONS + 2} total sections.
Each section should be ~{words_per_section} words of natural spoken narration.
Intro hooks viewers immediately. Outro asks to like, subscribe, and comment.

Return ONLY valid JSON (no markdown fences, no explanation):
{{
  "type": "long",
  "title": "engaging title under 70 chars",
  "description": "150-200 word YouTube description — compelling, includes a CHAPTERS placeholder line, ends with relevant hashtags (NOT #Shorts)",
  "tags": ["tag1","tag2","tag3","tag4","tag5","tag6","tag7","tag8"],
  "sections": [
    {{
      "number": 0,
      "type": "intro",
      "title": "Introduction",
      "text": "hook + overview of what viewers will learn, ~{words_per_section} words",
      "search_query": "visual keyword for background image"
    }},
    {{
      "number": 1,
      "type": "section",
      "title": "Section 1 Title",
      "text": "detailed educational content, ~{words_per_section} words, conversational",
      "search_query": "visual keyword"
    }},
    {{
      "number": 2,
      "type": "section",
      "title": "Section 2 Title",
      "text": "detailed content, ~{words_per_section} words",
      "search_query": "visual keyword"
    }},
    {{
      "number": 3,
      "type": "section",
      "title": "Section 3 Title",
      "text": "detailed content, ~{words_per_section} words",
      "search_query": "visual keyword"
    }},
    {{
      "number": 4,
      "type": "section",
      "title": "Section 4 Title",
      "text": "detailed content, ~{words_per_section} words",
      "search_query": "visual keyword"
    }},
    {{
      "number": 5,
      "type": "section",
      "title": "Section 5 Title",
      "text": "detailed content, ~{words_per_section} words",
      "search_query": "visual keyword"
    }},
    {{
      "number": 6,
      "type": "outro",
      "title": "Conclusion",
      "text": "summary + CTA to like/subscribe/comment, ~{words_per_section // 2} words",
      "search_query": "visual keyword"
    }}
  ]
}}"""

    for attempt in range(3):
        raw = ask(prompt, fast=False, max_tokens=5000)
        if not raw:
            log.warning(f"Long script attempt {attempt + 1}: empty response")
            continue

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
                obj = json.loads(raw[start:end])
                if obj.get("title") and obj.get("sections") and len(obj["sections"]) >= 5:
                    log.info(f"Long script ready: {obj['title']}")
                    return obj
            log.warning(f"Long script JSON invalid on attempt {attempt + 1}")
        except Exception as e:
            log.warning(f"Long script parse error attempt {attempt + 1}: {e}")

    raise RuntimeError(f"Could not generate long script for '{topic}' after 3 attempts")


def run(topic: str = None, style: str = None) -> dict:
    log.info("=== STEP 1L: Long Video Research & Script ===")

    if not topic:
        topics = get_long_topics()
        topic  = topics[0]

    script = write_long_script(topic, style)
    script["topic"]      = topic
    script["created_at"] = datetime.now().isoformat()

    Path("output").mkdir(exist_ok=True)
    ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = f"output/script_long_{ts}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(script, f, indent=2, ensure_ascii=False)

    script["script_path"] = path
    log.info(f"Long script saved: {path}")
    return script
