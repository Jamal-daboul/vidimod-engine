# Arabic subtitles & the tofu-box problem — READ THIS BEFORE TOUCHING FONTS

This document exists because we lost **days** chasing boxes (▯) in Arabic
subtitles across ~10 failed fixes. The root cause is subtle, environment-
dependent, and WILL come back if you add a font without following the rules
below.

## TL;DR rules

1. **Never drop a raw modern font (Google Fonts download) into `fonts/`.**
   It will render tofu boxes in burned-in Arabic subtitles, even though the
   same file works perfectly in browsers/Photoshop.
2. **Every new Arabic font must be passed through `tools/forge_arabic_font.py`
   first**, then render-tested (see "Adding a new Arabic font" below).
3. **Keep the harakat stripping in `pipeline/subtitles.py::_clean()`.**
   Removing it re-introduces inserted boxes on vocalised text.
4. A font is safe **only** if its cmap contains all three legacy groups:
   - U+FE80–U+FEFC — positional letter forms + lam-alef ligatures
   - U+FE70–U+FE7F — tashkil (harakat) presentation forms
   - U+FEFF (+ zero-width controls) — fribidi's lam-alef filler

## What actually happens (the full chain)

Subtitles are burned by ffmpeg's `subtitles` filter → **libass**. libass has
two Arabic shaping paths:

- **COMPLEX (HarfBuzz)**: shapes raw text via the font's OpenType tables.
  Any correct font works. Availability is NOT implied by `ffmpeg -buildconf`
  (distro builds link libass+HarfBuzz as shared libs), which is why
  `_renderer_has_harfbuzz()` in `pipeline/subtitles.py` *probes* a real render
  and looks for libass's `(COMPLEX)` shaper announcement.
- **SIMPLE (fribidi)**: maps each Arabic letter to its **legacy Unicode
  presentation form** (U+FE70–U+FEFC) and looks that codepoint up in the
  font's **cmap** directly. No OpenType shaping at all.

In practice the effective path on our renders behaved like SIMPLE even when
HarfBuzz was present (observed on both Ubuntu's `/usr/bin/ffmpeg` and the
static imageio build). So we must assume SIMPLE always.

**Modern fonts break under SIMPLE** because since ~2015 (and especially since
variable fonts), Google/vendors removed the legacy presentation-form cmap
entries — the positional glyphs still exist but are reachable only through
GSUB. The simple shaper's lookups then hit nothing:

| Box pattern seen in renders | Cause |
|---|---|
| Box **replacing** a letter (`؟لعقار`) | missing letter form (e.g. U+FE8D isolated alef) |
| Box **inserted** mid-word (`في▯لا` from `فيلَّا`) | missing tashkil form (U+FE70–FE7F) for a haraka |
| Box **beside lam-alef** (`ا▯لأسرار`) | fribidi replaces the ligated alef with filler **U+FEFF**; font has no cmap entry for it |

Diagnostics that were decisive:
- `/api/debug/last-ass` (backend) shows the exact text/codepoints of recent
  renders — the engine now keeps the last 30 `_sub_*.ass` files for this.
- Local repro: build an `.ass` via `pipeline/subtitles.py::build_segment_ass`
  and render one frame with `ffmpeg -f lavfi -i color=... -vf
  "subtitles=test.ass:fontsdir=fonts"`. The box patterns reproduce exactly.

## Defenses currently in the engine (don't remove)

- `pipeline/subtitles.py::_clean()` strips Arabic tashkil
  (U+0610–061A, U+064B–065F, U+0670, U+06D6–06ED) from subtitle text.
- `pipeline/subtitles.py::_renderer_has_harfbuzz()` probes the real shaper;
  when in doubt, Arabic text is forced onto a known-safe font.
- `_ARABIC_FONTS` / `_LEGACY_SAFE_ARABIC` (same file) list the families
  allowed for Arabic. Only audited fonts belong there.
- All `*VM.ttf` fonts in `fonts/` are **forged**: legacy presentation forms +
  tashkil forms + invisible fillers were wired into their cmaps
  (`tools/forge_arabic_font.py`), and families renamed with a " VM" suffix
  (OFL reserved-font-name compliance for modified builds).
- Naturally complete fonts (no forging needed): Noto Sans/Kufi/Naskh Arabic,
  Amiri, IBM Plex Sans Arabic, Droid Arabic Kufi/Naskh, Vazirmatn.

## Adding a new Arabic font (the only safe procedure)

```bash
# 1. forge it (adds legacy cmap entries; renames family to "<Family> VM")
python tools/forge_arabic_font.py NewFont.ttf fonts/NewFontVM.ttf

# 2. audit — must print 16/16 PASS (the tool does this automatically)

# 3. register the family:
#    - pipeline/subtitles.py  → add "<Family> VM" to _ARABIC_FONTS
#    - backend main.py        → FONT_LABELS + ARABIC_FAMILIES (vidimod-web repo)

# 4. render-test one frame before shipping:
#    build an .ass with Arabic text containing: alef words (الأسرار),
#    lam-alef (لا/لأ), and harakat (فيلَّا) — then:
ffmpeg -f lavfi -i color=c=black:s=1080x600:d=2 \
       -vf "subtitles=test.ass:fontsdir=fonts" -frames:v 1 out.png
# look at out.png: any ▯ anywhere → DO NOT SHIP.
```

## Quick audit one-liner (is this font safe?)

```bash
python - <<'EOF'
from fontTools.ttLib import TTFont
KEY = [0xFE8D,0xFE8E,0xFEFB,0xFEFC,0xFE83,0xFE84,0xFE91,0xFEE6,0xFEAE,0xFE9E,
       0xFE76,0xFE77,0xFE7C,0xFE7D,0xFE70,0xFEFF]
cmap = TTFont("THEFONT.ttf", fontNumber=0, lazy=True).getBestCmap()
print(sum(1 for c in KEY if c in cmap), "/ 16  (16 = safe)")
EOF
```

## History (what NOT to retry — all of these were dead ends)

- Installing fonts system-wide / fc-cache — not the issue (fontsdir works).
- Switching ffmpeg builds (imageio static ↔ apt) — changes nothing by itself;
  the simple shaper behaviour persists.
- Pre-shaping text with arabic_reshaper before the .ass — double-shaping or
  same missing-cmap failure; libass must receive RAW text.
- PIL/`step4_assemble.py` subtitle fixes — web shorts never use that path
  (they go through `step4_long._assemble_web_long` → ffmpeg/libass).
- "The font choice is wrong" — no: the font FILES were structurally
  incompatible with the simple shaper. Same file, any choice → same boxes.
