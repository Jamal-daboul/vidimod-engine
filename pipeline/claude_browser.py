"""
William - Claude Browser Interface
Opens claude.ai, sends a message, waits for response, returns it.
Uses playwright for reliable browser control instead of pyautogui.
"""

import json
import logging
import time
from pathlib import Path

log = logging.getLogger(__name__)


def ask_claude(prompt: str, wait_seconds: int = 60) -> str:
    """
    Opens claude.ai in a browser, sends prompt, waits for response.
    Uses playwright for reliable automation.
    Returns Claude's response text.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log.error("Playwright not installed. Run: pip install playwright && playwright install chromium")
        return ""

    result = ""

    with sync_playwright() as p:
        # Launch browser - use persistent context to stay logged in
        user_data_dir = str(Path.home() / "william_browser_profile")
        
        browser = p.chromium.launch_persistent_context(
            user_data_dir=user_data_dir,
            headless=False,  # Show browser so user can see it
            args=["--no-sandbox"]
        )

        page = browser.new_page()

        try:
            log.info("Opening Claude...")
            page.goto("https://claude.ai/new", timeout=30000)
            page.wait_for_load_state("networkidle", timeout=15000)
            time.sleep(2)

            # Check if logged in - look for the input area
            # If not logged in, the user needs to log in manually
            page_text = page.inner_text("body")
            if "sign in" in page_text.lower() or "log in" in page_text.lower():
                log.warning("Not logged in to Claude. Waiting 60s for manual login...")
                time.sleep(60)

            # Find the input area - try multiple selectors
            input_selectors = [
                '[contenteditable="true"]',
                'div[contenteditable]',
                'textarea',
                '.ProseMirror',
                '[data-placeholder]'
            ]

            input_el = None
            for selector in input_selectors:
                try:
                    el = page.wait_for_selector(selector, timeout=5000)
                    if el:
                        input_el = el
                        break
                except:
                    continue

            if not input_el:
                log.error("Could not find Claude input field")
                browser.close()
                return ""

            # Click and type the prompt
            input_el.click()
            time.sleep(0.5)

            # Type the prompt
            page.keyboard.type(prompt, delay=10)
            time.sleep(0.5)

            # Submit
            page.keyboard.press("Enter")
            log.info(f"Prompt sent. Waiting {wait_seconds}s for Claude...")

            # Wait for response to appear and complete
            time.sleep(5)  # Initial wait

            # Wait for streaming to finish - look for the stop button to disappear
            for i in range(wait_seconds):
                time.sleep(1)
                # Check if Claude is still typing (stop button present)
                stop_button = page.query_selector('[aria-label="Stop"]')
                if not stop_button:
                    # Also check for streaming indicator
                    streaming = page.query_selector('.streaming')
                    if not streaming:
                        log.info(f"Claude finished responding after {i+5}s")
                        time.sleep(1)
                        break

            # Extract the response - get the last assistant message
            response_selectors = [
                '.font-claude-message',
                '[data-is-streaming="false"]',
                '.prose',
                'article',
            ]

            for selector in response_selectors:
                try:
                    elements = page.query_selector_all(selector)
                    if elements:
                        # Get the last one (Claude's latest response)
                        last_el = elements[-1]
                        text = last_el.inner_text()
                        if text and len(text) > 20:
                            result = text
                            log.info(f"Got response: {len(result)} chars")
                            break
                except:
                    continue

            # Fallback: get all text and find JSON
            if not result:
                full_text = page.inner_text("body")
                result = full_text

        except Exception as e:
            log.error(f"Browser error: {e}")

        finally:
            time.sleep(2)
            browser.close()

    return result


def ask_claude_for_script(topic: str, niche: str, tone: str) -> dict | None:
    """Ask Claude to write a complete video script."""
    prompt = f"""Write a YouTube Shorts video script for a faceless facts channel.

TOPIC: {topic}
STYLE: {tone}
FACTS: Exactly 10 facts
LENGTH: ~55 seconds when read aloud

Return ONLY valid JSON, starting with {{ :
{{"title": "eye-catching title max 60 chars", "description": "YouTube description with #Shorts #Facts #TopFacts at end", "tags": ["tag1", "tag2", "tag3", "tag4", "tag5", "tag6", "tag7", "tag8"], "hook": "Opening line that grabs attention - 1 sentence", "facts": [{{"number": 1, "text": "Surprising fact."}}, {{"number": 2, "text": "Fact."}}, {{"number": 3, "text": "Fact."}}, {{"number": 4, "text": "Fact."}}, {{"number": 5, "text": "Fact."}}, {{"number": 6, "text": "Fact."}}, {{"number": 7, "text": "Fact."}}, {{"number": 8, "text": "Fact."}}, {{"number": 9, "text": "Fact."}}, {{"number": 10, "text": "Fact."}}], "outro": "Closing line asking viewers to follow"}}

Rules: Facts must be TRUE. Each fact genuinely surprising. Simple language.
JSON only:"""

    raw = ask_claude(prompt, wait_seconds=60)

    if not raw:
        return None

    # Find JSON in response
    try:
        # Look for {"title" pattern
        start = raw.find('{"title"')
        if start < 0:
            start = raw.find("{")
        end = raw.rfind("}") + 1
        if start >= 0 and end > start:
            candidate = raw[start:end]
            script = json.loads(candidate)
            if script.get("title") and script.get("facts"):
                log.info(f"Script: {script['title']}")
                return script
    except Exception as e:
        log.error(f"JSON parse failed: {e}")
        log.error(f"Raw response: {raw[:500]}")

    return None


def ask_claude_conversation(message: str, context: str = "") -> str:
    """
    Have a conversation with Claude about William's work.
    Returns Claude's natural language response.
    """
    prompt = f"""You are helping an AI employee called William who makes YouTube Shorts videos.

Context: {context}

The channel owner sent this message: "{message}"

Reply naturally and helpfully as William would, understanding their intent.
Be concise (2-3 sentences max).
If they're approving something, confirm it.
If they want changes, acknowledge specifically what you'll change.
If they suggest a topic, confirm it enthusiastically."""

    return ask_claude(prompt, wait_seconds=30)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print("Testing Claude browser connection...")
    response = ask_claude("Say hello and confirm you can help write YouTube video scripts.", wait_seconds=20)
    print(f"Response: {response[:200]}")
