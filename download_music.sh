#!/usr/bin/env bash
# Download a big batch of royalty-free background music into ./music.
# Source: Kevin MacLeod / incompetech.com (CC-BY 4.0). Tracks that 404 are skipped,
# already-downloaded tracks are kept, so it's safe to re-run.
#
# ATTRIBUTION (required by CC-BY): add this line to your video descriptions —
#   Music: Kevin MacLeod (incompetech.com), licensed under Creative Commons BY 4.0
#
# Usage on the VPS:
#   cd /app/william && git pull && bash download_music.sh
# then click Refresh on the Create page.
set -u
DIR="$(cd "$(dirname "$0")" && pwd)/music"
mkdir -p "$DIR"
B="https://incompetech.com/music/royalty-free/mp3-royaltyfree"

# mood|Track Name  (filename becomes mood_Track Name.mp3 so the picker is organized)
list=(
  # ── Epic / cinematic ──
  "epic|Heroic Age" "epic|Strength of the Titans" "epic|The Descent" "epic|Crusade"
  "epic|Hitman" "epic|Clash Defiant" "epic|Rising Tide" "epic|Five Armies"
  "epic|Impact Prelude" "epic|Lightless Dawn" "epic|Floating Cities" "epic|Volatile Reaction"
  "epic|Killing Time" "epic|Anachronism" "epic|Ascending the Vale"
  # ── Dramatic / tension ──
  "tension|Investigations" "tension|Killers" "tension|Anguish" "tension|Decisions"
  "tension|Crowd Hammer" "tension|Darkest Child" "tension|Long Note Two"
  "tension|Comfortable Mystery" "tension|Welcome to the Show" "tension|Tenebrous Brothers Carnival"
  # ── Dark / mystery / horror ──
  "dark|Ghostpocalypse" "dark|Crypto" "dark|Echoes of Time" "dark|Dark Times"
  "dark|Unseen Horror" "dark|Spider Eyes" "dark|Bump in the Night" "dark|Lurking Threat"
  "dark|Phantom from Space" "dark|Wounded"
  # ── Calm / emotional / inspirational ──
  "calm|Healing" "calm|Reflections" "calm|Pearls" "calm|Bittersweet" "calm|Tenderness"
  "calm|Heartwarming" "calm|Touching Moments" "calm|Clean Soul" "calm|Calm" "calm|As I Figure"
  # ── Upbeat / happy / quirky ──
  "happy|Sneaky Snitch" "happy|Fluffing a Duck" "happy|Carefree" "happy|Monkeys Spinning Monkeys"
  "happy|The Builder" "happy|Wallpaper" "happy|Pixelland" "happy|Local Forecast"
  "happy|Run Amok" "happy|Scheming Weasel" "happy|Happy Boy Theme"
)

ok=0; skip=0
for item in "${list[@]}"; do
  mood="${item%%|*}"; name="${item#*|}"
  out="$DIR/${mood}_${name}.mp3"
  if [ -f "$out" ] && [ -s "$out" ]; then echo "have  ${mood}_${name}"; ok=$((ok+1)); continue; fi
  url="$B/$(echo "$name" | sed 's/ /%20/g').mp3"
  if curl -fsSL --max-time 120 -o "$out" "$url"; then
    echo "OK    ${mood}_${name}"; ok=$((ok+1))
  else
    rm -f "$out"; echo "miss  ${mood}_${name}"; skip=$((skip+1))
  fi
done
echo "==================================================="
echo "Done: $ok in library, $skip skipped  →  $DIR"
echo "Now click Refresh on the Create page (music auto-levels itself)."
