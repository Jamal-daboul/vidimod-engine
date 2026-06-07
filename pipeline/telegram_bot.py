"""
William - Telegram Bot
Every message goes to Claude (Haiku for normal chat, Sonnet only when making videos).
"""

import json
import re
import logging
import requests
import threading
import time
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

BOT_TOKEN = None
CHAT_ID   = None
_running  = False

CMD_RE  = re.compile(r"\[\[WILLIAM:\s*(\{.*?\})\s*\]\]", re.DOTALL)
PROF_RE = re.compile(r"\[\[PROFILE:\s*(\{.*?\})\s*\]\]", re.DOTALL)


def setup(token: str, chat_id: str):
    global BOT_TOKEN, CHAT_ID
    BOT_TOKEN = token
    CHAT_ID   = str(chat_id)


def send(text: str) -> bool:
    if not BOT_TOKEN or not CHAT_ID:
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"},
            timeout=10,
        )
        return r.status_code == 200
    except Exception as e:
        log.warning(f"Send failed: {e}")
        return False


def _get_updates(offset=None) -> list:
    try:
        params = {"timeout": 10, "allowed_updates": ["message"]}
        if offset:
            params["offset"] = offset
        r = requests.get(
            f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates",
            params=params, timeout=15,
        )
        if r.status_code == 200:
            return r.json().get("result", [])
    except Exception:
        pass
    return []


def _execute(command: dict):
    """Execute an action tag emitted by Claude."""
    action = (command.get("action") or "").lower()

    if action == "make_video":
        topic      = command.get("topic", "")
        style      = command.get("style", "")
        video_type = command.get("video_type", "short")
        Path("memory").mkdir(exist_ok=True)
        with open("memory/next_video.json", "w", encoding="utf-8") as f:
            json.dump({"topic": topic, "style": style, "video_type": video_type}, f)
        label = "long video" if video_type == "long" else "Short"
        send(f"Starting {label}: *{topic}*")
        import subprocess, sys
        subprocess.Popen(
            [sys.executable, "william.py", "make_approved"],
            cwd=str(Path(".").absolute()),
        )

    elif action == "generate_plan":
        def _gen():
            from pipeline import content_planner
            send("Generating 7-day content plan...")
            plan = content_planner.generate_plan(days=7)
            if plan:
                content_planner.save_plan(plan)
                send(content_planner.format_plan_message())
            else:
                send("Plan generation failed — try again?")
        threading.Thread(target=_gen, daemon=True).start()
        return

    elif action == "assign_human_task":
        task    = command.get("task", "")
        context = command.get("context", "")
        prio    = command.get("priority", "normal")
        tasks   = []
        tp = Path("memory/tasks.json")
        if tp.exists():
            tasks = json.loads(tp.read_text(encoding="utf-8"))
        tid = len(tasks) + 1
        tasks.append({
            "id": tid, "task": task, "context": context,
            "priority": prio, "status": "pending",
            "created": datetime.now().isoformat(),
        })
        tp.write_text(json.dumps(tasks, indent=2), encoding="utf-8")
        emoji = "!!" if prio == "high" else "--"
        send(f"{emoji} *Task #{tid}:*\n{task}\n_{context}_\n\nReply `done {tid}` when complete.")

    elif action == "stats":
        _cmd_stats()


def _handle(text: str):
    """Handle every incoming Telegram message."""
    from pipeline.brain import chat

    cmd = text.lower().strip()

    # Built-in slash commands
    if cmd in ["/start", "/help"]:
        send("I'm William. Talk to me naturally.\n\n"
             "/status - pipeline state\n"
             "/stats - channel stats\n"
             "/tasks - pending tasks\n"
             "/plan - show content plan\n"
             "/plan generate - regenerate plan")
        return
    if cmd == "/status":
        _cmd_status(); return
    if cmd == "/stats":
        _cmd_stats(); return
    if cmd == "/tasks":
        _cmd_tasks(); return
    if cmd in ["/plan", "/plan generate"]:
        if cmd == "/plan generate":
            def _regen():
                from pipeline import content_planner
                send("Regenerating content plan...")
                plan = content_planner.generate_plan(days=7)
                if plan:
                    content_planner.save_plan(plan)
                    send(content_planner.format_plan_message())
                else:
                    send("Plan generation failed — try again?")
            threading.Thread(target=_regen, daemon=True).start()
        else:
            from pipeline import content_planner
            send(content_planner.format_plan_message())
        return
    if cmd.startswith("done "):
        try:
            tid = int(cmd.split()[1])
            tp  = Path("memory/tasks.json")
            if tp.exists():
                tasks = json.loads(tp.read_text(encoding="utf-8"))
                for t in tasks:
                    if t["id"] == tid:
                        t["status"]    = "done"
                        t["completed"] = datetime.now().isoformat()
                tp.write_text(json.dumps(tasks, indent=2), encoding="utf-8")
            send(f"Task #{tid} marked done!")
        except Exception:
            send("Usage: done <task_number>")
        return

    # Everything else -> Claude Haiku (cheap & fast for normal chat)
    raw = chat(text, fast=True)
    if not raw:
        send("No response — try again?")
        return

    # Execute action tags if Claude emitted any
    for m in CMD_RE.finditer(raw):
        try:
            _execute(json.loads(m.group(1)))
        except Exception as e:
            log.warning(f"Command parse failed: {e}")

    # Update profile if Claude learned something
    for m in PROF_RE.finditer(raw):
        try:
            data = json.loads(m.group(1))
            pp   = Path("memory/profile.json")
            profile = json.loads(pp.read_text(encoding="utf-8")) if pp.exists() else {}
            profile.update(data)
            profile["last_updated"] = datetime.now().isoformat()
            pp.write_text(json.dumps(profile, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    # Strip tags and send reply
    clean = CMD_RE.sub("", raw)
    clean = PROF_RE.sub("", clean).strip()
    if clean:
        send(clean)


# ── Commands ──────────────────────────────────────────────────────────────────

def _cmd_status():
    try:
        scripts = sorted(Path("output").glob("script_*.json"), reverse=True)
        if not scripts:
            send("No videos yet."); return
        with open(scripts[0], encoding="utf-8") as f:
            s = json.load(f)
        lines = []
        for label, key in [("Script", "title"), ("Audio", "audio_segments"),
                            ("Footage", "clips"), ("Video", "final_video"), ("YouTube", "youtube_url")]:
            val = s.get(key)
            ok  = bool(val) if not isinstance(val, list) else len(val) > 0
            lines.append(f"{'OK' if ok else '..'} {label}")
        send(f"*{s.get('title', 'Unknown')}*\n" + "\n".join(lines))
    except Exception as e:
        send(f"Status error: {e}")


def _cmd_stats():
    try:
        mp = Path("memory/videos.json")
        if not mp.exists():
            send("No stats yet."); return
        m       = json.loads(mp.read_text(encoding="utf-8"))
        videos  = m.get("videos", [])
        uploads = m.get("total_uploads", 0)
        today   = datetime.now().strftime("%Y-%m-%d")
        today_n = sum(1 for v in videos if v.get("timestamp", "").startswith(today) and v.get("success"))
        msg = (f"*Stats - {datetime.now().strftime('%B %d')}*\n\n"
               f"Total uploads: {uploads}\n"
               f"Today: {today_n}/4\n"
               f"All time: {len(videos)}")
        if videos:
            last = videos[-1]
            msg += f"\n\nLast: _{last.get('title', '')}_"
            if last.get("youtube_url"):
                msg += f"\n{last['youtube_url']}"
        send(msg)
    except Exception as e:
        send(f"Stats error: {e}")


def _cmd_tasks():
    try:
        tp = Path("memory/tasks.json")
        if not tp.exists():
            send("No pending tasks."); return
        tasks = [t for t in json.loads(tp.read_text(encoding="utf-8")) if t["status"] == "pending"]
        if not tasks:
            send("No pending tasks."); return
        lines = [f"#{t['id']} ({t['priority']}): {t['task']}" for t in tasks]
        send("*Your tasks:*\n\n" + "\n".join(lines) + "\n\nReply `done N` to complete.")
    except Exception as e:
        send(f"Tasks error: {e}")


# ── Listener ──────────────────────────────────────────────────────────────────

def start_listener():
    global _running
    if _running:
        return
    _running = True
    threading.Thread(target=_loop, daemon=True).start()
    log.info("Telegram listener started")


def _loop():
    global _running
    last_id = None
    updates = _get_updates()
    if updates:
        last_id = updates[-1]["update_id"] + 1
    while _running:
        try:
            for upd in _get_updates(offset=last_id):
                last_id = upd["update_id"] + 1
                msg  = upd.get("message", {})
                if str(msg.get("chat", {}).get("id")) == CHAT_ID:
                    text = msg.get("text", "").strip()
                    if text:
                        threading.Thread(target=_handle, args=(text,), daemon=True).start()
        except Exception as e:
            log.warning(f"Listener error: {e}")
        time.sleep(2)
