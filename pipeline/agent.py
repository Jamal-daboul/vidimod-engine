"""
William - Autonomous Agent Core
William creates his own pipelines, manages tasks, discovers the user,
and assigns human tasks when needed. This is the heart of William.
"""

import json
import logging
import threading
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

# Pending tasks assigned to the human
_human_tasks = []
_task_lock = threading.Lock()


def load_profile() -> dict:
    try:
        with open("memory/profile.json", encoding="utf-8") as f:
            return json.load(f)
    except:
        return {}


def save_profile(profile: dict):
    Path("memory").mkdir(exist_ok=True)
    with open("memory/profile.json", "w", encoding="utf-8") as f:
        json.dump(profile, f, indent=2, ensure_ascii=False)


def load_tasks() -> list:
    try:
        with open("memory/tasks.json", encoding="utf-8") as f:
            return json.load(f)
    except:
        return []


def save_tasks(tasks: list):
    Path("memory").mkdir(exist_ok=True)
    with open("memory/tasks.json", "w", encoding="utf-8") as f:
        json.dump(tasks, f, indent=2, ensure_ascii=False)


def add_human_task(task: str, context: str = "", priority: str = "normal"):
    """William assigns a task to the human."""
    tasks = load_tasks()
    tasks.append({
        "id": len(tasks) + 1,
        "task": task,
        "context": context,
        "priority": priority,
        "status": "pending",
        "created": datetime.now().isoformat()
    })
    save_tasks(tasks)
    log.info(f"Human task added: {task}")
    return tasks[-1]


def complete_human_task(task_id: int):
    """Mark a human task as done."""
    tasks = load_tasks()
    for t in tasks:
        if t["id"] == task_id:
            t["status"] = "done"
            t["completed"] = datetime.now().isoformat()
    save_tasks(tasks)


def get_pending_human_tasks() -> list:
    return [t for t in load_tasks() if t["status"] == "pending"]


def build_william_system_prompt() -> str:
    """Build William's full system prompt with profile and context."""
    profile = load_profile()
    pending_tasks = get_pending_human_tasks()

    # Load video history
    memory = {"videos": [], "total_uploads": 0}
    try:
        with open("memory/videos.json", encoding="utf-8") as f:
            memory = json.load(f)
    except:
        pass

    videos = memory.get("videos", [])
    video_titles = [v.get("title", "") for v in videos[-10:]]
    uploads = memory.get("total_uploads", 0)

    # Pending tasks summary
    tasks_text = "None pending."
    if pending_tasks:
        tasks_text = "\n".join([
            f"  #{t['id']}: {t['task']}"
            for t in pending_tasks[:5]
        ])

    prompt = f"""You are William, an autonomous AI life manager working for {profile.get('name', 'your employer')}.

=== WHO YOU WORK FOR ===
Name: {profile.get('name', 'Unknown')}
Age: {profile.get('age', 'Unknown')}
Country: {profile.get('country', 'Unknown')}
Skills: {', '.join(profile.get('skills', []))}
Languages: {', '.join(profile.get('languages', []))}
Current situation: {profile.get('situation', 'Unknown')}
Urgent goal: {profile.get('urgent_goal', 'passive income')}
Laptop availability: {profile.get('laptop_availability', '24/7')}

=== WHAT YOUR EMPLOYER CAN DO (human tasks) ===
{', '.join(profile.get('human_tasks_willing', ['phone calls', 'identity verification']))}

=== YOUTUBE CHANNEL STATUS ===
Videos uploaded: {uploads}
Recent titles: {', '.join(video_titles) if video_titles else 'None yet'}
Schedule: 2 videos/day at 02:00 and 14:00

=== PENDING HUMAN TASKS ===
{tasks_text}

=== YOUR ROLE ===
You are not a fixed-function bot. You are an intelligent agent who:
1. Understands your employer's situation deeply
2. Proactively identifies the best income opportunities for their specific skills
3. Creates plans and executes them autonomously
4. When you need human action (a phone call, physical presence, identity verification), you assign it clearly
5. You update your understanding of your employer as you learn more
6. You manage multiple workstreams simultaneously

=== HOW YOU COMMUNICATE ===
- Keep Telegram messages SHORT (3-5 sentences max)
- Be direct and action-oriented
- When you've decided to DO something, say what you're doing and do it
- When you need the human, be specific about exactly what they need to do
- Use the hidden command tag when ready to act

=== COMMAND TAGS ===
When you decide to take action, end your message with:
[[WILLIAM: {{"action": "make_video", "topic": "...", "style": "..."}}]]
[[WILLIAM: {{"action": "assign_human_task", "task": "...", "context": "...", "priority": "high/normal"}}]]
[[WILLIAM: {{"action": "status"}}]]
[[WILLIAM: {{"action": "research", "query": "...", "purpose": "..."}}]]
[[WILLIAM: {{"action": "none"}}]]

Only add the tag when you've decided to act. During discussion, no tag needed.

=== PROFILE UPDATES ===
When you learn something new about your employer, end with:
[[PROFILE: {{"field": "value"}}]]
Example: [[PROFILE: {{"new_skill": "photography"}}]]

You work 24/7. Your employer sleeps. Keep working while they sleep."""

    return prompt


def process_command(command: dict, send_func) -> bool:
    """Execute a command William decided on."""
    action = command.get("action", "none")

    if action == "make_video":
        topic = command.get("topic", "")
        style = command.get("style", "")
        try:
            Path("memory").mkdir(exist_ok=True)
            with open("memory/next_video.json", "w") as f:
                json.dump({"topic": topic, "style": style}, f)
        except Exception as e:
            log.warning(f"Could not save video task: {e}")
        send_func("▶️ Starting video pipeline now...")
        import subprocess, sys
        subprocess.Popen(
            [sys.executable, "william.py", "make_approved"],
            cwd=str(Path(".").absolute())
        )
        return True

    elif action == "assign_human_task":
        task = command.get("task", "")
        context = command.get("context", "")
        priority = command.get("priority", "normal")
        t = add_human_task(task, context, priority)
        emoji = "🚨" if priority == "high" else "📋"
        send_func(
            f"{emoji} *Task for you #{t['id']}*\n\n"
            f"{task}\n\n"
            f"_{context}_\n\n"
            f"Reply 'done {t['id']}' when complete.",
        )
        return True

    elif action == "status":
        from pipeline.state_manager import find_latest_script, assess_state
        script = find_latest_script()
        if script:
            state = assess_state(script)
            steps = ["Script ✅" if state["has_script"] else "Script ⏳",
                     "Audio ✅" if state["has_audio"] else "Audio ⏳",
                     "Video ✅" if state["has_video"] else "Video ⏳",
                     "YouTube ✅" if state["has_youtube"] else "YouTube ⏳"]
            send_func(f"📊 {script.get('title', '')}\n" + " | ".join(steps))
        else:
            send_func("No videos yet.")
        return True

    elif action == "research":
        query = command.get("query", "")
        purpose = command.get("purpose", "")
        log.info(f"Research task: {query} for {purpose}")
        # Research runs in background
        threading.Thread(
            target=_background_research,
            args=(query, purpose, send_func),
            daemon=True
        ).start()
        return True

    return False


def _background_research(query: str, purpose: str, send_func):
    """Run web research in background and report back."""
    try:
        from pipeline.claude_api import ask_once
        prompt = f"""Search and research: {query}
Purpose: {purpose}

Provide a concise, actionable summary with the most important findings.
Focus on specific, actionable information relevant to the purpose.
Keep it under 200 words."""

        result = ask_once(prompt, fast=False)
        if result:
            send_func(f"🔍 *Research: {query[:40]}*\n\n{result[:500]}")
    except Exception as e:
        log.error(f"Research failed: {e}")


def update_profile_from_tag(tag_data: dict):
    """Update profile when William discovers something new."""
    profile = load_profile()
    profile.update(tag_data)
    profile["last_updated"] = datetime.now().isoformat()
    save_profile(profile)
    log.info(f"Profile updated: {tag_data}")
