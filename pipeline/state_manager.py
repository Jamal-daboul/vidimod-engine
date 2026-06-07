"""
William - Smart State Manager
Checks what's done, resumes from where it left off.
"""

import json
import logging
from pathlib import Path
from datetime import datetime

log = logging.getLogger(__name__)


def find_latest_script() -> dict | None:
    scripts = sorted(Path("output").glob("script_*.json"), reverse=True)
    if not scripts:
        return None
    try:
        with open(scripts[0], encoding="utf-8") as f:
            return json.load(f)
    except:
        return None


def assess_state(script: dict) -> dict:
    state = {
        "has_script":  bool(script.get("title") and script.get("facts")),
        "has_audio":   False,
        "has_footage": False,
        "has_video":   False,
        "has_youtube": bool(script.get("youtube_url")),
        "script_path": script.get("script_path", ""),
    }
    audio = script.get("audio_segments", [])
    if audio:
        state["has_audio"] = all(Path(s["path"]).exists() for s in audio if s.get("path")) and len(audio) >= 10
    clips = script.get("clips", [])
    if clips:
        state["has_footage"] = all(Path(c["path"]).exists() for c in clips if c.get("path")) and len(clips) >= 10
    final = script.get("final_video")
    if final and Path(final).exists():
        state["has_video"] = True
    return state


def get_next_action(state: dict) -> str:
    if not state["has_script"]:  return "research"
    if not state["has_audio"]:   return "voiceover"
    if not state["has_footage"]: return "footage"
    if not state["has_video"]:   return "assemble"
    if not state["has_youtube"]: return "upload"
    return "complete"


def print_state_report(script: dict, state: dict):
    print("\n" + "="*55)
    print(f"  {script.get('title', 'Unknown')[:50]}")
    print("-"*55)
    for label, key in [("Script","has_script"),("Audio","has_audio"),
                        ("Footage","has_footage"),("Video","has_video"),("YouTube","has_youtube")]:
        print(f"  {'✅' if state[key] else '⏳'} {label}")
    if state["has_youtube"]:
        print(f"\n  {script.get('youtube_url')}")
    print("="*55)


def save_to_memory(script: dict, success: bool, error: str = None):
    Path("memory").mkdir(exist_ok=True)
    path = "memory/videos.json"
    try:
        memory = {"videos": [], "total_uploads": 0, "total_failures": 0}
        if Path(path).exists():
            with open(path, encoding="utf-8") as f:
                memory = json.load(f)
        existing = [v.get("script_path") for v in memory["videos"]]
        if script.get("script_path") not in existing:
            memory["videos"].append({
                "timestamp": datetime.now().isoformat(),
                "title": script.get("title", ""),
                "topic": script.get("topic", ""),
                "success": success,
                "youtube_url": script.get("youtube_url"),
                "script_path": script.get("script_path"),
                "error": error
            })
            if success:
                memory["total_uploads"] = memory.get("total_uploads", 0) + 1
            elif error:
                memory["total_failures"] = memory.get("total_failures", 0) + 1
        with open(path, "w", encoding="utf-8") as f:
            json.dump(memory, f, indent=2, ensure_ascii=False)
    except Exception as e:
        log.warning(f"Memory save failed: {e}")


def smart_run(force_new: bool = False, approved_topic: str = None, approved_style: str = None):
    import sys
    sys.path.insert(0, ".")
    from pipeline import step1_research, step2_voiceover, step3_footage, step4_assemble, step5_upload
    from pipeline.telegram_bot import send

    # Find latest script
    script = find_latest_script()
    if script and not force_new:
        state = assess_state(script)
        if get_next_action(state) == "complete":
            print_state_report(script, state)
            script = None  # Start new
        else:
            print_state_report(script, state)
    else:
        script = None

    state = assess_state(script) if script else {
        "has_script": False, "has_audio": False, "has_footage": False,
        "has_video": False, "has_youtube": False
    }
    next_action = get_next_action(state) if script else "research"
    success = False

    send("🎬 Starting video pipeline...")

    try:
        if next_action == "research":
            log.info("Step 1: Research & Script")

            if approved_topic:
                topic = approved_topic
                style = approved_style
                log.info(f"Using approved topic: {topic}")
            else:
                topics = step1_research.get_trending_topics()
                topic = step1_research.pick_best_topic(topics)
                style = None
                send(f"📝 Making video about: *{topic}*")

            script = step1_research.write_script(topic, style_notes=style)
            script["topic"] = topic
            script["created_at"] = datetime.now().isoformat()

            from datetime import datetime as dt
            ts = dt.now().strftime("%Y%m%d_%H%M%S")
            path = f"output/script_{ts}.json"
            with open(path, "w", encoding="utf-8") as f:
                json.dump(script, f, indent=2, ensure_ascii=False)
            script["script_path"] = path
            send(f"✅ Script: *{script.get('title', '')}*")

            state = assess_state(script)
            next_action = get_next_action(state)

        if next_action == "voiceover":
            log.info("Step 2: Voiceover")
            send("🎙️ Generating voiceover...")
            script = step2_voiceover.run(script)
            state = assess_state(script)
            next_action = get_next_action(state)

        if next_action == "footage":
            log.info("Step 3: Footage")
            send("🎬 Downloading footage...")
            script = step3_footage.run(script)
            state = assess_state(script)
            next_action = get_next_action(state)

        if next_action == "assemble":
            log.info("Step 4: Assemble")
            send("⚙️ Assembling video...")
            script = step4_assemble.run(script)
            state = assess_state(script)
            next_action = get_next_action(state)

        if next_action == "upload":
            log.info("Step 5: Upload")
            send("📤 Uploading to YouTube...")
            script = step5_upload.run(script)
            state = assess_state(script)

        success = state.get("has_video", False)

        if script.get("youtube_url"):
            send(f"🎉 *Video live!*\n{script['youtube_url']}")
        else:
            send("✅ Video complete! Upload when ready: `python william.py upload`")

    except Exception as e:
        log.error(f"Pipeline failed: {e}", exc_info=True)
        send(f"⚠️ Pipeline error: {str(e)[:200]}")
        if script:
            save_to_memory(script, success=False, error=str(e))
        return False

    if script:
        save_to_memory(script, success=success)
    return success