"""
William - Claude API Brain
Uses Claude Sonnet 4.6 for complex reasoning and planning.
Uses Claude Haiku 4.5 for simple/fast tasks to save quota.
Maintains full conversation history.
"""

import json
import logging

log = logging.getLogger(__name__)

_conversation = []
_system_prompt = ""
_client = None

SONNET = "claude-sonnet-4-6"   # Smart — planning, decisions, writing
HAIKU  = "claude-haiku-4-5-20251001"    # Fast/cheap — simple tasks, formatting

try:
    from anthropic import Anthropic
    AVAILABLE = True
except ImportError:
    AVAILABLE = False
    log.warning("Run: pip install anthropic")


def setup(api_key: str):
    global _client
    if not AVAILABLE:
        return False
    try:
        _client = Anthropic(api_key=api_key)
        log.info("Claude API ready (Sonnet 4.6 + Haiku 4.5)")
        return True
    except Exception as e:
        log.error(f"Claude API setup failed: {e}")
        return False


def set_system_prompt(prompt: str):
    global _system_prompt
    _system_prompt = prompt


def reset_conversation():
    global _conversation
    _conversation = []


def chat(message: str, fast: bool = False) -> str:
    """
    Send message, maintain history.
    fast=True uses Haiku (cheap). Default uses Sonnet (smart).
    """
    global _conversation
    if not _client:
        return "(Claude API not configured — add ANTHROPIC_API_KEY to settings.py)"

    model = HAIKU if fast else SONNET
    _conversation.append({"role": "user", "content": message})

    try:
        kwargs = {
            "model": model,
            "max_tokens": 2048,
            "messages": _conversation,
        }
        if _system_prompt:
            kwargs["system"] = _system_prompt

        response = _client.messages.create(**kwargs)
        reply = response.content[0].text
        _conversation.append({"role": "assistant", "content": reply})

        # Keep last 30 messages
        if len(_conversation) > 30:
            _conversation = _conversation[-30:]

        log.info(f"Claude ({model}): {len(reply)} chars")
        return reply

    except Exception as e:
        log.error(f"Claude chat failed: {e}")
        return f"(Claude error: {e})"


def ask_once(prompt: str, fast: bool = False, max_tokens: int = 2000) -> str:
    """One-off question, no history."""
    if not _client:
        return ""
    model = HAIKU if fast else SONNET
    try:
        response = _client.messages.create(
            model=model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}]
        )
        return response.content[0].text.strip()
    except Exception as e:
        log.error(f"Claude ask_once failed: {e}")
        return ""


def ask_claude_for_script(topic: str, niche: str, tone: str) -> dict | None:
    """Write video script — uses Sonnet for quality."""
    prompt = f"""Write a YouTube Shorts script about: {topic}
Style: {tone}. Exactly 10 facts. ~55 seconds when read aloud.

Reply with ONLY valid JSON:
{{"title":"catchy title max 60 chars","description":"YouTube description 100 words with #Shorts #Facts #TopFacts","tags":["tag1","tag2","tag3","tag4","tag5","tag6","tag7","tag8"],"hook":"1 sentence opener","facts":[{{"number":1,"text":"fact"}},{{"number":2,"text":"fact"}},{{"number":3,"text":"fact"}},{{"number":4,"text":"fact"}},{{"number":5,"text":"fact"}},{{"number":6,"text":"fact"}},{{"number":7,"text":"fact"}},{{"number":8,"text":"fact"}},{{"number":9,"text":"fact"}},{{"number":10,"text":"fact"}}],"outro":"closing line"}}"""

    raw = ask_once(prompt, fast=False, max_tokens=2000)
    if not raw:
        return None
    try:
        start = raw.find("{")
        end = raw.rfind("}") + 1
        if start >= 0 and end > start:
            script = json.loads(raw[start:end])
            if script.get("title") and script.get("facts"):
                return script
    except Exception as e:
        log.error(f"Script parse: {e}")
    return None


def ask_claude_conversation(message: str, context: str = "") -> str:
    return chat(message)
