#!/usr/bin/env python3
"""tune_metrics.py — adjust a traced font's metrics and spacing.

The traced fonts estimate advances, bearings and vertical metrics from the
raster. This tool lets you tune them: global ascent/descent/linegap, a
baseline shift, advance scaling, side bearings, per-glyph overrides, and
kerning pairs. Adjustments can come from CLI flags, a JSON file, or both.

Usage:
  tune_metrics.py FONT.ttf -o OUT.ttf [options]
  tune_metrics.py FONT.ttf --list                # print current metrics
  tune_metrics.py FONT.ttf -o OUT.ttf --adjustments tune.json

JSON schema (all keys optional; CLI flags override the file):
{
  "ascent": 800, "descent": -250, "linegap": 0,
  "baseline_shift": 0,          # move ALL glyphs vertically, + = up
  "advance_scale": 1.02,        # scale every advance width
  "bearing": 10,                # add to every side bearing (and advance)
  "glyphs": {
    "a":   {"shift": 3, "advance": 500, "lsb": 30},
    "y":   {"shift": -2}
  },
  "kerning": {"AV": -40, "To": -30}
}
"""

import argparse
import json
import os
import sys

from fontTools.ttLib import TTFont, newTable

GLYPH_KEYS = ("shift", "advance", "lsb")


def list_metrics(font, only=None):
    glyf = font["glyf"]
    hmtx = font["hmtx"]
    print(f"{'glyph':16s} {'adv':>6s} {'lsb':>6s} {'xMin':>6s} {'yMin':>6s} "
          f"{'xMax':>6s} {'yMax':>6s}")
    for name in font.getGlyphOrder():
        if only and name not in only:
            continue
        g = glyf[name]
        adv, lsb = hmtx[name]
        if g.numberOfContours == 0:
            print(f"{name:16s} {adv:6d} {lsb:6d}    —     —     —     —")
            continue
        print(f"{name:16s} {adv:6d} {lsb:6d} {g.xMin:6d} {g.yMin:6d} "
              f"{g.xMax:6d} {g.yMax:6d}")


def shift_glyph(glyf, name, dy):
    """Move a glyph's outlines vertically by dy units (+ = up)."""
    g = glyf[name]
    if g.numberOfContours == 0:
        return
    if g.numberOfContours > 0:
        # simple glyph: translate coordinates directly
        g.coordinates.translate((0, dy))
        g.recalcBounds(None)
    else:
        # composite glyph: apply a transform (rare in traced fonts)
        g.transform((1, 0, 0, 1, 0, dy))


def set_kerning(font, pairs):
    """Write a kern table (format 0, horizontal) from {leftright: value}."""
    if not pairs:
        return
    from fontTools.ttLib.tables._k_e_r_n import KernTable_format_0
    st = KernTable_format_0(0)
    st.version = 0
    st.coverage = 1  # horizontal kerning
    st.kernTable = {(p[0], p[1]): v for p, v in pairs.items()}
    kern = newTable("kern")
    kern.version = 0
    kern.kernTables = [st]
    font["kern"] = kern


def apply_adjustments(font, adj):
    glyf = font["glyf"]
    hmtx = font["hmtx"]
    hhea = font["hhea"]
    os2 = font["OS/2"]

    baseline_shift = adj.get("baseline_shift", 0)
    advance_scale = adj.get("advance_scale", 1.0)
    bearing = adj.get("bearing", 0)
    glyphs_adj = adj.get("glyphs", {})

    # vertical metrics
    if "ascent" in adj:
        a = adj["ascent"]
        hhea.ascent = a
        os2.sTypoAscender = a
        os2.usWinAscent = a
    if "descent" in adj:
        d = adj["descent"]
        hhea.descent = d
        os2.sTypoDescender = d
        os2.usWinDescent = abs(d)
    if "linegap" in adj:
        hhea.lineGap = adj["linegap"]
        os2.sTypoLineGap = adj["linegap"]

    # per-glyph: baseline shift, advance, lsb
    for name in font.getGlyphOrder():
        g = glyf[name]
        if baseline_shift:
            shift_glyph(glyf, name, baseline_shift)
        if name in glyphs_adj:
            pg = glyphs_adj[name]
            if "shift" in pg:
                shift_glyph(glyf, name, pg["shift"])
        adv, lsb = hmtx[name]
        new_adv, new_lsb = adv, lsb
        if advance_scale != 1.0:
            new_adv = round(new_adv * advance_scale)
        if bearing:
            new_lsb = new_lsb + bearing
            new_adv = new_adv + 2 * bearing
        if name in glyphs_adj:
            pg = glyphs_adj[name]
            if "advance" in pg:
                new_adv = pg["advance"]
            if "lsb" in pg:
                new_lsb = pg["lsb"]
        new_lsb = max(0, new_lsb)
        hmtx[name] = (max(1, new_adv), new_lsb)

    # kerning
    if "kerning" in adj:
        set_kerning(font, adj["kerning"])

    # refresh head bbox
    xmin = ymin = 10 ** 9
    xmax = ymax = -10 ** 9
    for name in font.getGlyphOrder():
        g = glyf[name]
        if g.numberOfContours == 0:
            continue
        for x, y in g.coordinates:
            xmin, ymin = min(xmin, x), min(ymin, y)
            xmax, ymax = max(xmax, x), max(ymax, y)
    head = font["head"]
    head.xMin, head.yMin = xmin, ymin
    head.xMax, head.yMax = xmax, ymax


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("font", help="input .ttf")
    ap.add_argument("-o", "--out", help="output .ttf (omit with --list)")
    ap.add_argument("--adjustments", metavar="FILE.json",
                    help="JSON adjustments file (see docstring)")
    ap.add_argument("--ascent", type=int)
    ap.add_argument("--descent", type=int)
    ap.add_argument("--linegap", type=int)
    ap.add_argument("--baseline-shift", type=int, default=0)
    ap.add_argument("--advance-scale", type=float)
    ap.add_argument("--bearing", type=int)
    ap.add_argument("--list", action="store_true", help="print metrics and exit")
    ap.add_argument("--glyphs", help="comma-separated glyph list for --list")
    args = ap.parse_args()

    font = TTFont(args.font)
    if args.list:
        only = set(args.glyphs.split(",")) if args.glyphs else None
        list_metrics(font, only)
        return

    if not args.out:
        raise SystemExit("pass -o OUT.ttf (or --list to inspect)")

    adj = {}
    if args.adjustments:
        with open(args.adjustments) as f:
            adj = json.load(f)
    for key, val in (("ascent", args.ascent), ("descent", args.descent),
                     ("linegap", args.linegap)):
        if val is not None:
            adj[key] = val
    if args.baseline_shift:
        adj["baseline_shift"] = args.baseline_shift
    if args.advance_scale is not None:
        adj["advance_scale"] = args.advance_scale
    if args.bearing is not None:
        adj["bearing"] = args.bearing

    apply_adjustments(font, adj)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
    font.save(args.out)
    print(f"wrote {args.out}")
    if "kerning" in adj:
        print(f"  kerning pairs: {len(adj['kerning'])}")


if __name__ == "__main__":
    main()
