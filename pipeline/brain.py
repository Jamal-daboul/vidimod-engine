"""
William - Claude API Brain
Sonnet 4.6 for complex tasks. Haiku 4.5 for simple/fast tasks.
Maintains conversation history across messages.
"""

import json
import logging

log = logging.getLogger(__name__)

SONNET = "claude-sonnet-4-6"
HAIKU  = "claude-haiku-4-5-20251001"

_client       = None
_conversation = []
_system       = ""

try:
    from anthropic import Anthropic
    _OK = True
except ImportError:
    _OK = False
    log.warning("Run: pip install anthropic")


def setup(api_key: str) -> bool:
    global _client
    if not _OK:
        return False
    try:
        _client = Anthropic(api_key=api_key)
        log.info("Claude API ready")
        return True
    except Exception as e:
        log.error(f"API setup failed: {e}")
        return False


def set_system(prompt: str):
    global _system
    _system = prompt


def reset():
    global _conversation
    _conversation = []


def chat(message: str, fast: bool = False) -> str:
    """Chat with history. fast=True uses Haiku."""
    global _conversation
    if not _client:
        return "(API not configured)"
    model = HAIKU if fast else SONNET
    _conversation.append({"role": "user", "content": message})
    try:
        kw = {"model": model, "max_tokens": 2048, "messages": _conversation}
        if _system:
            kw["system"] = _system
        r = _client.messages.create(**kw)
        reply = r.content[0].text
        _conversation.append({"role": "assistant", "content": reply})
        if len(_conversation) > 30:
            _conversation = _conversation[-30:]
        log.info(f"Claude ({model}): {len(reply)} chars")
        return reply
    except Exception as e:
        log.error(f"chat failed: {e}")
        return f"(Error: {e})"


def ask(prompt: str, fast: bool = False, max_tokens: int = 2000) -> str:
    """Single question, no history."""
    if not _client:
        return ""
    model = HAIKU if fast else SONNET
    try:
        r = _client.messages.create(
            model=model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}]
        )
        return r.content[0].text.strip()
    except Exception as e:
        log.error(f"ask failed: {e}")
        return ""


def write_script(topic: str, niche: str, tone: str) -> dict | None:
    """Write a video script. Returns parsed dict or None."""
    prompt = f"""Write a YouTube Shorts facts video script.

TOPIC: {topic}
NICHE: {niche}
TONE: {tone}
FACTS: exactly 5
LENGTH: ~30 seconds when read aloud (very short and punchy)

Return ONLY valid JSON (no markdown, no explanation):
{{
  "title": "catchy title under 60 chars",
  "description": "80 word YouTube description ending with #Shorts #Facts #TopFacts",
  "tags": ["tag1","tag2","tag3","tag4","tag5","tag6"],
  "hook": "one punchy sentence opening that grabs attention",
  "facts": [
    {{"number": 1, "text": "fact one — genuinely surprising, 1 sentence"}},
    {{"number": 2, "text": "fact two — 1 sentence"}},
    {{"number": 3, "text": "fact three — 1 sentence"}},
    {{"number": 4, "text": "fact four — 1 sentence"}},
    {{"number": 5, "text": "fact five — 1 sentence"}}
  ],
  "outro": "short closing line asking viewers to follow"
}}"""

    raw = ask(prompt, fast=False, max_tokens=1500)
    if not raw:
        log.error("write_script: empty response from API")
        return None

    # Strip markdown code fences if present
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
            if obj.get("title") and obj.get("facts") and len(obj["facts"]) >= 3:
                log.info(f"Script ready: {obj['title']}")
                return obj
        log.error(f"Script JSON invalid. Raw: {raw[:300]}")
    except Exception as e:
        log.error(f"Script parse error: {e}. Raw: {raw[:300]}")
    return None