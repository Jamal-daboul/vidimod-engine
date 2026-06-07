"""
William - Claude Desktop Controller
Uses the COPY BUTTON to get Claude's response reliably.
Detects response completion by watching for copy/like buttons to appear.
"""

import json
import time
import logging
import subprocess
import threading
import pyperclip

log = logging.getLogger(__name__)

try:
    import uiautomation as auto
    auto.SetGlobalSearchTimeout(20)
    UIA_AVAILABLE = True
except ImportError:
    UIA_AVAILABLE = False

_chat_started = False
_claude_lock = threading.Lock()
_last_response = ""  # Track to avoid repeating


def get_claude_window(launch_if_needed: bool = True):
    if not UIA_AVAILABLE:
        return None
    for _ in range(2):
        try:
            root = auto.GetRootControl()
            for win in root.GetChildren():
                name = (win.Name or "").strip()
                class_name = (win.ClassName or "")
                is_claude = (name == "Claude" or
                            (name.lower() == "claude" and
                             "chrome" not in class_name.lower()))
                is_excluded = any(x in name.lower() for x in
                    ["monogatari", "pycharm", "chrome", "edge", "firefox",
                     "terminal", "powershell", "cmd", "visual studio"])
                if is_claude and not is_excluded:
                    win.SetActive()
                    time.sleep(1)
                    return win
        except Exception as e:
            log.warning(f"Window search error: {e}")
        if launch_if_needed:
            log.info("Launching Claude...")
            subprocess.Popen(
                'explorer.exe shell:AppsFolder\\Claude_pzs8sxrjxfjjc!Claude',
                shell=True)
            time.sleep(7)
            launch_if_needed = False
    return None


def find_input_box(window):
    candidates = []
    def search(control, depth=0):
        if depth > 25:
            return
        try:
            if control.ControlTypeName in ["EditControl", "DocumentControl"]:
                candidates.append(control)
            for child in control.GetChildren():
                search(child, depth + 1)
        except:
            pass
    search(window)
    if candidates:
        try:
            candidates.sort(key=lambda c: c.BoundingRectangle.top)
            return candidates[-1]
        except:
            return candidates[-1]
    return None


def _find_copy_buttons(window) -> list:
    """Find all copy buttons in the Claude window."""
    buttons = []
    def search(control, depth=0):
        if depth > 30:
            return
        try:
            ctype = control.ControlTypeName
            name = (control.Name or "").lower().strip()
            if ctype == "ButtonControl" and name in ["copy", "copy response",
                                                       "copy message", "copy text"]:
                buttons.append(control)
            for child in control.GetChildren():
                search(child, depth + 1)
        except:
            pass
    search(window)
    return buttons


def _send_text(window, text: str):
    """Paste text into Claude's input and send."""
    # Activate Claude window
    window.SetActive()
    time.sleep(0.5)

    # Always click bottom-center of Claude window
    # The input box is ALWAYS at the bottom — don't search for elements
    rect = window.BoundingRectangle
    cx = (rect.left + rect.right) // 2
    cy = rect.bottom - 60  # 60px from bottom edge = always the input area
    auto.Click(cx, cy)
    time.sleep(0.8)
    log.info(f"Clicked input at ({cx}, {cy})")

    # Paste text from clipboard
    pyperclip.copy(text)
    time.sleep(0.4)
    auto.SendKeys("{Ctrl}v", waitTime=0.6)
    time.sleep(0.5)

    # Send with Enter
    auto.SendKeys("{Enter}", waitTime=0.3)
    log.info("Message sent")


def _wait_for_response(window, max_wait: int = 60) -> bool:
    """
    Wait for Claude to finish responding.
    Detects completion by counting copy buttons — a new one appears when
    Claude finishes each message.
    """
    initial_buttons = len(_find_copy_buttons(window))
    log.info(f"Copy buttons before: {initial_buttons}")

    for i in range(max_wait):
        time.sleep(1)
        current_buttons = len(_find_copy_buttons(window))
        if current_buttons > initial_buttons:
            log.info(f"New copy button appeared after {i+1}s (now {current_buttons})")
            time.sleep(1)  # Wait a moment for text to finalize
            return True

    log.warning("Timed out waiting for copy button")
    return False


def _click_last_copy_button(window) -> str:
    """Click the last copy button and return clipboard content."""
    buttons = _find_copy_buttons(window)
    if not buttons:
        log.warning("No copy buttons found")
        return ""

    btn = buttons[-1]
    log.info(f"Clicking copy button {len(buttons)} (last one)")

    # Make sure Claude window is focused
    window.SetActive()
    time.sleep(0.5)

    pyperclip.copy("")
    time.sleep(0.2)

    # Try multiple methods to click the copy button
    copied = False

    # Method 1: Invoke pattern (programmatic, no mouse)
    try:
        pattern = btn.GetInvokePattern()
        if pattern:
            pattern.Invoke()
            time.sleep(1.0)
            result = pyperclip.paste()
            if result and len(result) > 3:
                log.info(f"Method 1 (Invoke): {len(result)} chars")
                return result.strip()
    except Exception as e:
        log.info(f"Invoke failed: {e}")

    # Method 2: Click using BoundingRectangle center
    try:
        rect = btn.BoundingRectangle
        cx = (rect.left + rect.right) // 2
        cy = (rect.top + rect.bottom) // 2
        window.SetActive()
        time.sleep(0.3)
        auto.Click(cx, cy)
        time.sleep(1.0)
        result = pyperclip.paste()
        if result and len(result) > 3:
            log.info(f"Method 2 (coords): {len(result)} chars")
            return result.strip()
    except Exception as e:
        log.info(f"Coord click failed: {e}")

    # Method 3: SetFocus + SendKeys Enter
    try:
        btn.SetFocus()
        time.sleep(0.3)
        auto.SendKeys("{Enter}", waitTime=0.5)
        time.sleep(1.0)
        result = pyperclip.paste()
        if result and len(result) > 3:
            log.info(f"Method 3 (Enter): {len(result)} chars")
            return result.strip()
    except Exception as e:
        log.info(f"Focus+Enter failed: {e}")

    log.warning("All copy methods failed")

    return ""


def start_working_chat(intro_message: str = None):
    """Open ONE new chat at startup."""
    global _chat_started
    window = get_claude_window()
    if not window:
        return False
    auto.SendKeys("{Ctrl}n", waitTime=0.5)
    time.sleep(2.5)
    _chat_started = True
    log.info("New chat opened")
    return True


def ask_claude(prompt: str, wait_seconds: int = 60, new_chat: bool = False) -> str:
    """
    Send prompt to Claude in the SAME chat, return response.
    Uses copy button count to detect response completion.
    Clicks the last copy button to get the clean full text.
    Thread-safe with lock.
    """
    if not UIA_AVAILABLE:
        return ""

    with _claude_lock:
        window = get_claude_window()
        if not window:
            return ""

        if new_chat:
            auto.SendKeys("{Ctrl}n", waitTime=0.5)
            time.sleep(2.5)
            window = get_claude_window(launch_if_needed=False)

        _send_text(window, prompt)

        log.info(f"Waiting for response (max {wait_seconds}s)...")
        _wait_for_response(window, max_wait=wait_seconds)

        # Click the copy button on Claude's latest message
        response = _click_last_copy_button(window)

        # Click back to input area to unfocus
        try:
            rect = window.BoundingRectangle
            auto.Click((rect.left + rect.right) // 2, rect.bottom - 60)
            time.sleep(0.3)
        except:
            pass

        # Don't return the same response twice (stale copy)
        global _last_response
        if response and response == _last_response:
            log.warning("Response is same as last time — message may not have sent")
            return ""
        if response:
            _last_response = response

        return response


def ask_claude_for_script(topic: str, niche: str, tone: str) -> dict | None:
    prompt = f"""Write a YouTube Shorts script about: {topic}
Style: {tone}. Exactly 10 facts. ~55 seconds.

Reply with ONLY this JSON:
{{"title":"catchy title max 60 chars","description":"desc with #Shorts #Facts","tags":["t1","t2","t3","t4","t5"],"hook":"opener","facts":[{{"number":1,"text":"fact"}},{{"number":2,"text":"fact"}},{{"number":3,"text":"fact"}},{{"number":4,"text":"fact"}},{{"number":5,"text":"fact"}},{{"number":6,"text":"fact"}},{{"number":7,"text":"fact"}},{{"number":8,"text":"fact"}},{{"number":9,"text":"fact"}},{{"number":10,"text":"fact"}}],"outro":"follow for more"}}"""

    raw = ask_claude(prompt, wait_seconds=80)
    if not raw:
        return None
    try:
        start = raw.find('{"title"')
        if start < 0:
            start = raw.find("{")
        end = raw.rfind("}") + 1
        if start >= 0 and end > start:
            script = json.loads(raw[start:end])
            if script.get("title") and script.get("facts"):
                return script
    except Exception as e:
        log.error(f"Parse failed: {e}\nRaw: {raw[:300]}")
    return None


def ask_claude_conversation(message: str, context: str = "") -> str:
    raw = ask_claude(message, wait_seconds=40)
    return raw.strip() if raw else "I didn't catch that, try again?"


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                       format="%(asctime)s [%(levelname)s] %(message)s")
    print("Testing... Claude must be open. Don't touch anything.\n")
    time.sleep(2)

    # Test 1: send and get response
    r = ask_claude("Say exactly: WORKING GREAT BOSS", wait_seconds=30)
    print(f"\nTest 1 response: '{r}'")

    time.sleep(2)

    # Test 2: same chat, second message
    r2 = ask_claude("Now say exactly: STILL HERE", wait_seconds=30)
    print(f"Test 2 response: '{r2}'")