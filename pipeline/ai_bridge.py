"""
William - AI Bridge
Routes all AI tasks to Claude API. No Ollama.
"""

import json
import logging

log = logging.getLogger(__name__)


def get_topics_smart(niche: str) -> list:
    from pipeline.claude_api import ask_once
    prompt = f"""Generate 5 YouTube Shorts topics for "{niche}" channel.
Reply ONLY with JSON array: ["Topic 1", "Topic 2", "Topic 3", "Topic 4", "Topic 5"]"""
    raw = ask_once(prompt, fast=True)
    try:
        start = raw.find("[")
        end = raw.rfind("]") + 1
        if start >= 0 and end > start:
            topics = json.loads(raw[start:end])
            if isinstance(topics, list) and len(topics) >= 3:
                return topics
    except:
        pass
    return ["Top 10 Facts About Space", "10 Mind-Blowing Human Brain Facts",
            "Top 10 Ancient Egypt Facts", "10 Ocean Creature Facts", "Top 10 Technology Facts"]


def pick_topic_smart(topics: list) -> str:
    return topics[0]


def write_script_smart(topic: str, niche: str, tone: str, use_claude: bool = True) -> dict | None:
    from pipeline.claude_api import ask_claude_for_script
    return ask_claude_for_script(topic, niche, tone)


def get_footage_query_smart(fact_text: str, topic: str = "") -> str:
    stopwords = {"the","a","an","is","was","are","that","this","and","or","in",
                 "on","to","for","of","with","it","by","from","you","they"}
    words = fact_text.lower().replace(".", "").replace(",", "").split()
    keywords = [w for w in words if w not in stopwords and len(w) > 3]
    return " ".join(keywords[:3]) if keywords else topic[:20] or "nature background"