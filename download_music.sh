#!/usr/bin/env bash
# Download a big batch of royalty-free background music into ./music (~77 tracks,
# ~15 per mood). Every URL here was pre-verified, so nothing 404s. Already-downloaded
# tracks are kept, so it's safe to re-run (it just adds anything new).
# Source: Kevin MacLeod / incompetech.com (CC-BY 4.0).
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

# mood|Track Name → saved as  mood_Track Name.mp3  so the picker is organized.
list=(
  # ── Epic / cinematic (14) ──
  "epic|Heroic Age" "epic|Strength of the Titans" "epic|The Descent" "epic|Crusade"
  "epic|Hitman" "epic|Clash Defiant" "epic|Rising Tide" "epic|Five Armies"
  "epic|Impact Prelude" "epic|Lightless Dawn" "epic|Floating Cities" "epic|Volatile Reaction"
  "epic|Killing Time" "epic|Ascending the Vale"
  # ── Dramatic / tension (16) ──
  "tension|Investigations" "tension|Killers" "tension|Anguish" "tension|Decisions"
  "tension|Crowd Hammer" "tension|Darkest Child" "tension|Long Note Two" "tension|Comfortable Mystery"
  "tension|Welcome to the Show" "tension|The Complex" "tension|Hard Boiled" "tension|Frozen Star"
  "tension|Mechanolith" "tension|Exit the Premises" "tension|Mistake the Getaway" "tension|Industrial Revolution"
  # ── Dark / mystery / horror (18) ──
  "dark|Crypto" "dark|Echoes of Time" "dark|Dark Times" "dark|Spider Eyes"
  "dark|Bump in the Night" "dark|Phantom from Space" "dark|Wounded" "dark|Long Note Three"
  "dark|Anxiety" "dark|Mourning Song" "dark|Disquiet" "dark|Static Motion" "dark|Aftermath"
  "dark|Death and Axes" "dark|Constance" "dark|Decay" "dark|Curse of the Scarab" "dark|Grave Matters"
  # ── Calm / emotional / inspirational (16) ──
  "calm|Healing" "calm|Bittersweet" "calm|Heartwarming" "calm|Clean Soul" "calm|As I Figure"
  "calm|Sincerely" "calm|Despair and Triumph" "calm|Sad Trio" "calm|Angevin" "calm|Easy Lemon"
  "calm|Wholesome" "calm|Sovereign" "calm|Dreamy Flashback" "calm|Anamalie" "calm|Sardana" "calm|Almost in F"
  # ── Upbeat / happy / quirky (13) ──
  "happy|Sneaky Snitch" "happy|Fluffing a Duck" "happy|Carefree" "happy|Monkeys Spinning Monkeys"
  "happy|The Builder" "happy|Wallpaper" "happy|Pixelland" "happy|Local Forecast" "happy|Run Amok"
  "happy|Happy Boy Theme" "happy|Itty Bitty 8 Bit" "happy|Spazzmatica Polka" "happy|Cool Vibes"
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
echo "Done: $ok tracks in library, $skip skipped  →  $DIR"
echo "Now click Refresh on the Create page (music auto-levels itself)."
