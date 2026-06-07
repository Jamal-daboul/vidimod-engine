"""Step 4b - Background music: random pick from local library."""

import logging
import random
from pathlib import Path

log = logging.getLogger(__name__)

MUSIC_DIR = Path("output/audio/music")


def get_background_music(duration_seconds: float, topic: str = "") -> str | None:
    """Pick a random MP3 from the music library. Returns None if folder is empty."""
    if not MUSIC_DIR.exists():
        log.warning(f"Music folder not found: {MUSIC_DIR}")
        return None

    tracks = list(MUSIC_DIR.glob("*.mp3"))
    if not tracks:
        log.warning("Music folder is empty — skipping background music")
        return None

    pick = random.choice(tracks)
    log.info(f"Music selected: '{pick.name}'")
    return str(pick)


def run(duration: float, ts: str, topic: str = "") -> str | None:
    log.info("=== STEP 4b: Background Music ===")
    return get_background_music(duration, topic)
