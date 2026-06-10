#!/usr/bin/env python3
"""Forge an Arabic font so it renders correctly in burned-in subtitles.

Why this exists: ffmpeg's subtitles filter (libass) resolves Arabic through
LEGACY presentation-form codepoints (simple/fribidi shaper). Modern fonts
(current Google Fonts builds) dropped those cmap entries, so they render tofu
boxes — letters replaced, boxes inserted at harakat, and a box beside every
lam-alef (fribidi's U+FEFF filler). See ARABIC_FONTS.md at the repo root for
the full story and the mandatory ship checklist.

What it does (no outlines are modified):
  1. U+FE80-FEFC letter forms & lam-alef ligatures: discovers each form's
     glyph by shaping the base letter in context (HarfBuzz + tatweel joiners)
     and maps the legacy codepoint to that glyph.
  2. U+FE70-FE7F tashkil forms: mapped to the font's own mark glyphs.
  3. U+FEFF + zero-width/bidi controls: mapped to a zero-width empty glyph
     (created if the font has none).
  4. Renames the family to "<Family> VM" (OFL reserved-font-name rule).

Usage:
    python tools/forge_arabic_font.py IN.ttf OUT.ttf
    # variable fonts: instance first, e.g.
    #   python -m fontTools.varLib.instancer -o In.ttf 'In[wght].ttf' wght=700

Requires: pip install fonttools uharfbuzz
"""
import sys
import unicodedata

import uharfbuzz as hb
from fontTools.ttLib import TTFont
from fontTools.ttLib.tables._g_l_y_f import Glyph

TATWEEL = "ـ"
INVISIBLES = [0xFEFF, 0x200B, 0x200C, 0x200D, 0x200E, 0x200F,
              0x2060, 0x061C, 0x00AD]
# 16-point audit: letter forms, lam-alef, alef-hamza, tashkil forms, filler.
AUDIT_KEY = [0xFE8D, 0xFE8E, 0xFEFB, 0xFEFC, 0xFE83, 0xFE84, 0xFE91, 0xFEE6,
             0xFEAE, 0xFE9E, 0xFE76, 0xFE77, 0xFE7C, 0xFE7D, 0xFE70, 0xFEFF]


def _shape(font, text):
    buf = hb.Buffer()
    buf.add_str(text)
    buf.guess_segment_properties()
    hb.shape(font, buf)
    return [(i.codepoint, i.cluster) for i in buf.glyph_infos]


def _glyph_for(font, text, want_cluster):
    out = _shape(font, text)
    hits = [gid for gid, cl in out if cl == want_cluster]
    return hits[0] if len(hits) == 1 else None


def _ensure_empty_glyph(tt):
    """Return a zero-advance empty glyph name, creating 'vmnull' if needed."""
    glyf, hmtx = tt["glyf"], tt["hmtx"]
    for gname in tt.getGlyphOrder()[1:]:
        try:
            adv, _ = hmtx.metrics[gname]
            if adv == 0 and glyf[gname].numberOfContours == 0:
                return gname
        except Exception:
            continue
    gname = "vmnull"
    if gname not in tt.getGlyphOrder():
        tt.setGlyphOrder(tt.getGlyphOrder() + [gname])
        glyf.glyphs[gname] = Glyph()
        hmtx.metrics[gname] = (0, 0)
    return gname


def forge(path, out_path):
    blob = hb.Blob.from_file_path(path)
    hfont = hb.Font(hb.Face(blob))
    tt = TTFont(path, fontNumber=0)
    glyph_order = tt.getGlyphOrder()
    cmap_table = tt["cmap"]
    best = tt.getBestCmap()
    added = 0

    # 1+2) presentation forms from Unicode decompositions
    for cp in range(0xFE70, 0xFEFD):
        if cp in best:
            continue
        d = unicodedata.decomposition(chr(cp))
        if not d or not d.startswith("<"):
            continue
        form, _, rest = d.partition("> ")
        form = form[1:]
        bases = [int(h, 16) for h in rest.split()]
        gid = None
        if bases and bases[0] in (0x0020, 0x0640) and len(bases) == 2 and bases[1] >= 0x064B:
            if bases[1] in best:                      # tashkil → mark glyph
                try:
                    gid = tt.getGlyphID(best[bases[1]])
                except Exception:
                    gid = None
        elif len(bases) == 1:
            ch = chr(bases[0])
            if bases[0] not in best:
                continue
            if form == "isolated":
                out = _shape(hfont, ch)
                gid = out[0][0] if len(out) == 1 else None
            elif form == "final":
                gid = _glyph_for(hfont, TATWEEL + ch, 1)
            elif form == "initial":
                gid = _glyph_for(hfont, ch + TATWEEL, 0)
            elif form == "medial":
                gid = _glyph_for(hfont, TATWEEL + ch + TATWEEL, 1)
        elif len(bases) == 2 and bases[0] == 0x0644:  # lam-alef ligatures
            pair = chr(bases[0]) + chr(bases[1])
            if form == "isolated":
                out = _shape(hfont, pair)
                gid = out[0][0] if len(out) == 1 else None
            elif form == "final":
                out = _shape(hfont, TATWEEL + pair)
                non_t = [g for g, cl in out if cl == 1]
                gid = non_t[0] if len(non_t) == 1 and len(out) == 2 else None
        if not gid or gid == 0 or gid >= len(glyph_order):
            continue
        gname = glyph_order[gid]
        for sub in cmap_table.tables:
            if sub.isUnicode():
                sub.cmap[cp] = gname
        added += 1

    # 3) invisible fillers → zero-width empty glyph
    empty = _ensure_empty_glyph(tt)
    for cp in INVISIBLES:
        if cp in best:
            continue
        for sub in cmap_table.tables:
            if sub.isUnicode():
                sub.cmap[cp] = empty
        added += 1

    # 4) rename family (OFL reserved-font-name rule for modified builds)
    name = tt["name"]
    fam = name.getDebugName(16) or name.getDebugName(1) or "Font"
    new_fam = fam if fam.endswith(" VM") else fam + " VM"
    sub_name = name.getDebugName(17) or name.getDebugName(2) or "Regular"
    for rec in name.names:
        if rec.nameID in (1, 16):
            rec.string = new_fam
        elif rec.nameID == 4:
            rec.string = f"{new_fam} {sub_name}"
        elif rec.nameID == 6:
            rec.string = (new_fam + "-" + sub_name).replace(" ", "")
        elif rec.nameID == 3:
            rec.string = f"vidimod;{new_fam}-{sub_name}"
    tt.save(out_path)
    return new_fam, added


def main():
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(1)
    src, dst = sys.argv[1], sys.argv[2]
    fam, added = forge(src, dst)
    cmap = TTFont(dst, fontNumber=0, lazy=True).getBestCmap()
    have = sum(1 for cp in AUDIT_KEY if cp in cmap)
    verdict = "PASS — safe to ship" if have == len(AUDIT_KEY) else "FAIL — do NOT ship"
    print(f"family: {fam!r}  added {added} cmap entries  audit: {have}/{len(AUDIT_KEY)}  {verdict}")
    sys.exit(0 if have == len(AUDIT_KEY) else 2)


if __name__ == "__main__":
    main()
