#!/usr/bin/env python3
"""trace_grid.py — separate + trace a glyph grid image into a TTF font.

The return trip from the diffuser: takes a grid image like the ones
glyph_grid.py produces (GF Latin Kernel order, no blanks), detects the grid,
splits it into cells, traces each glyph to vector outlines with Potrace, and
builds a working .ttf with cmap, advance widths and vertical metrics.

Usage:
  trace_grid.py GRID.png -o out.ttf [options]

The grid is auto-detected from the cell border lines; pass --cols/--rows/
--cell/--padding to override. Cell (r, c) maps to glyph r*cols+c in GF Latin
Kernel order (space/nbspace excluded, same layout as the references).
"""

import argparse
import math
import os
import re
import statistics
import subprocess
import sys
import tempfile
from collections import Counter

from PIL import Image

from fontTools.misc.timeTools import timestampNow
from fontTools.ttLib import TTFont, newTable
from fontTools.pens.recordingPen import RecordingPen
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.pens.cu2quPen import Cu2QuPen
from fontTools.svgLib.path import parse_path
from fontTools.ttLib.tables.O_S_2f_2 import Panose
from fontTools.ttLib.tables._c_m_a_p import CmapSubtable
from fontTools.ttLib.tables._n_a_m_e import NameRecord

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import glyph_grid

BORDER_GRAY = 226  # grid border color used by glyph_grid.py


# ---------------------------------------------------------------- grid layout

def detect_grid(img):
    """Detect (cols, rows, cell, padding) from the border lines in the image."""
    g = img.convert("L")
    w, h = g.size
    px = g.load()

    def border_like(v):
        # Border lines render antialiased (~240 gray) rather than the exact
        # #e2e2e2; accept anything that is neither white background nor ink.
        return 180 < v < 250

    hlines, vlines = [], []
    for y in range(h):
        hits = sum(1 for x in range(0, w, 2) if border_like(px[x, y]))
        if hits > 0.6 * (w // 2):
            hlines.append(y)
    for x in range(w):
        hits = sum(1 for y in range(0, h, 2) if border_like(px[x, y]))
        if hits > 0.6 * (h // 2):
            vlines.append(x)

    def cluster(vals, tol=3):
        out = []
        for v in vals:
            if out and v - out[-1][-1] <= tol:
                out[-1].append(v)
            else:
                out.append([v])
        return [sum(c) // len(c) for c in out]

    hlines, vlines = cluster(hlines), cluster(vlines)
    if len(hlines) < 3 or len(vlines) < 3:
        return None

    def spacing(lines):
        gaps = [b - a for a, b in zip(lines, lines[1:])]
        return statistics.median(gaps), gaps

    hgap, hgaps = spacing(hlines)
    vgap, vgaps = spacing(vlines)
    tol = max(2, 0.05 * hgap)
    if any(abs(gap - hgap) > tol for gap in hgaps) or \
       any(abs(gap - vgap) > tol for gap in vgaps):
        return None

    rows = len(hlines) - 1
    cols = len(vlines) - 1
    cell = int(round((hgap + vgap) / 2))
    # The first horizontal/vertical line is the top/left edge of the grid,
    # i.e. the padding.
    padding = int(round((hlines[0] + vlines[0]) / 2))
    return cols, rows, cell, padding


def detect_halftone(img, sample_step=8):
    """Detect a 1px checkerboard halftone (diffuser artifacts).

    Such images carry ink on a single diagonal parity (e.g. only (even,
    even) and (odd, odd) pixels, or only one of those). Compare ink on the
    two diagonals: returns True when >95% of it sits on one diagonal.
    Note: sampling must cover both parities — stepping by `sample_step`
    from 0 samples only (even, even), so the opposite diagonal is probed
    at (x+1, y+1).
    """
    g = img.convert("L")
    w, h = g.size
    px = g.load()
    p0 = p1 = 0
    for y in range(0, h, sample_step):
        for x in range(0, w, sample_step):
            if px[x, y] < 150:
                p0 += 1
            if x + 1 < w and y + 1 < h and px[x + 1, y + 1] < 150:
                p1 += 1
    tot = p0 + p1
    return tot > 200 and max(p0, p1) > 0.95 * tot


def _band_runs(vals, min_width, thr):
    """Return (start, end) runs of vals[i] > thr, each at least min_width."""
    out, start = [], None
    for i, v in enumerate(vals):
        if v > thr and start is None:
            start = i
        elif v <= thr and start is not None:
            if i - start >= min_width:
                out.append((start, i))
            start = None
    if start is not None and len(vals) - start >= min_width:
        out.append((start, len(vals)))
    return out


def detect_grid_ink(img):
    """Borderless grid detection for diffused images.

    The diffuser dropped the grid borders, so the grid is recovered from the
    ink itself: row bands come from the y-projection, column bands from the
    x-projection of the first row (which avoids descenders/overhangs merging
    columns). Returns (cols, rows, cell, padding, boxes) where boxes is a
    row-major list of exact (x0, y0, x1, y1) cell boxes (may be irregular),
    or None if no plausible grid is found.
    """
    g = img.convert("L")
    w, h = g.size
    px = g.load()
    ink = lambda v: v < 170

    # rows: y-projection over the full image
    yproj = [0] * h
    for y in range(h):
        for x in range(0, w, 2):
            if ink(px[x, y]):
                yproj[y] += 1
    if not yproj:
        return None
    row_bands = _band_runs(yproj, max(30, h // 40), max(yproj) * 0.03)
    if len(row_bands) < 3:
        return None
    # No merging for rows: the projection cleanly separates rows, and any
    # gap-based merge also swallows legitimately tight row spacing (bold
    # weights nearly touch).
    rows = len(row_bands)

    # columns: x-projection of the first row band only (low threshold: any ink)
    a0, b0 = row_bands[0]
    xproj = [0] * w
    for y in range(a0, b0):
        for x in range(w):
            if ink(px[x, y]):
                xproj[x] += 1
    col_bands = _band_runs(xproj, 4, 1)
    if len(col_bands) < 3:
        return None
    # Columns: merge only sub-6px splits (noise); glyphs within a row are
    # well separated (gaps of tens of px), so this never touches real gaps.
    col_bands = _drop_merged(col_bands, 6)
    cols = len(col_bands)

    # regularity sanity check: spacings within ~35% of the median
    def median_gap(bands):
        gaps = [b[0] - a[0] for a, b in zip(bands, bands[1:])]
        return statistics.median(gaps) if gaps else 0
    rgap, cgap = median_gap(row_bands), median_gap(col_bands)
    if not rgap or not cgap:
        return None
    for a, b in zip(row_bands, row_bands[1:]):
        if abs((b[0] - a[0]) - rgap) > 0.35 * rgap:
            return None
    for a, b in zip(col_bands, col_bands[1:]):
        if abs((b[0] - a[0]) - cgap) > 0.35 * cgap:
            return None

    # ---- refine the row boundaries from true ink extents
    # The projection bands shrink thin tails ('y') and strokes poking above
    # the next band ('U' in tight bold rows) below the threshold, so the
    # midpoint boundaries can land inside a neighbor's ink. Re-measure each
    # row's actual ink extent within its search region and split the gaps
    # between those.
    extents = _row_ink_extents(g, row_bands, col_bands)
    ys = [extents[0][0]] + [(a[1] + b[0]) // 2
                            for a, b in zip(extents, extents[1:])] \
         + [extents[-1][1]]

    # ---- per-row column boundaries
    # The diffuser jitters glyph x-positions per row (especially italics),
    # so a single set of column midpoints from row 0 falls inside a
    # neighbor's ink in other rows (e.g. 'l' picks up 'm's left stem, 'C'
    # picks up 'D's). Measure each row's own columns instead.
    xs_by_row = _row_col_boundaries(g, row_bands, extents, col_bands, cols)
    boxes = []
    for r in range(rows):
        xs = xs_by_row[r]
        for c in range(cols):
            boxes.append((xs[c], ys[r], xs[c + 1], ys[r + 1]))
    cell = int(round((rgap + cgap) / 2))
    padding = int(round((row_bands[0][0] + col_bands[0][0]) / 2))
    return cols, rows, cell, padding, boxes


def _row_ink_extents(g, row_bands, col_bands):
    """True per-row ink extents, split at the coarse band midpoints.

    Returns [(top, bottom)] with top/bottom being the first/last inked row
    within each row's search region (bounded by the coarse band midpoints,
    so neighboring rows can't contaminate each other).
    """
    w, h = g.size
    px = g.load()
    ink = lambda v: v < 170
    xs0, xs1 = col_bands[0][0], col_bands[-1][1]
    # Search regions span from the image edges to the coarse midpoints, so
    # row-0 caps (above the first band start) and last-row descender tails
    # (below the last band end) are included.
    mids = [0] + [(a[1] + b[0]) // 2
                  for a, b in zip(row_bands, row_bands[1:])] + [h]
    extents = []
    for r in range(len(row_bands)):
        top, bot = mids[r], mids[r + 1]
        ys = [y for y in range(top, bot)
              if any(ink(px[x, y]) for x in range(xs0, xs1, 2))]
        extents.append((ys[0], ys[-1]) if ys else (top, bot))
    return extents


def _row_col_boundaries(g, row_bands, extents, ref_col_bands, cols):
    """Per-row column boundaries, from each row's own x-projection.

    Sub-10px band splits (spurious) are merged; real glyph gaps are much
    larger. Rows that don't yield the expected column count fall back to the
    reference (row-0) boundaries.
    """
    w, h = g.size
    px = g.load()
    ink = lambda v: v < 170
    ref = [ref_col_bands[0][0]] + [(a[1] + b[0]) // 2
                                   for a, b in zip(ref_col_bands,
                                                   ref_col_bands[1:])] \
          + [ref_col_bands[-1][1]]
    out = []
    for r, (a, b) in enumerate(row_bands):
        top, bot = extents[r]
        xp = [0] * w
        for y in range(top, bot):
            for x in range(w):
                if ink(px[x, y]):
                    xp[x] += 1
        bands = _drop_merged(_band_runs(xp, 4, 1), 10)
        if len(bands) == cols:
            xs = [bands[0][0]] + [(a2[1] + b2[0]) // 2
                                  for a2, b2 in zip(bands, bands[1:])] \
                 + [bands[-1][1]]
        else:
            xs = ref
        out.append(xs)
    return out


def _drop_merged(bands, gap):
    """Merge bands separated by a gap narrower than `gap` (same glyph piece)."""
    merged = []
    for s, e in bands:
        if merged and s - merged[-1][1] < gap:
            merged[-1] = (merged[-1][0], e)
        else:
            merged.append((s, e))
    return merged


def expand_cells(img, cells, max_expand=80):
    """Grow each cell box left/right while ink is present in the edge strip.

    Vertical boundaries come from the refined per-row ink extents and are
    already exact, so only side overhangs (italic lean, wide serifs) need
    recovery here. The scan stops at the first empty strip, which keeps it
    from bleeding into the neighboring cell (unless the glyphs actually
    touch).
    """
    g = img.convert("L")
    w, h = g.size
    px = g.load()
    ink = lambda v: v < 170
    out = []
    for x0, y0, x1, y1 in cells:
        lef = x0
        while lef > 0 and x0 - lef < max_expand:
            if any(ink(px[lef - 1, y]) for y in range(y0, y1)):
                lef -= 1
            else:
                break
        rig = x1
        while rig < w and rig - x1 < max_expand:
            if any(ink(px[rig, y]) for y in range(y0, y1)):
                rig += 1
            else:
                break
        out.append((lef, y0, rig, y1))
    return out


XHEIGHT_GLYPHS = {"a", "c", "e", "m", "n", "o", "r", "s", "u", "v",
                  "w", "x", "z"}

# Glyphs whose ink bottoms reliably sit on the baseline: the big marks
# (letters, digits, caps). Small punctuation (dashes, bullets, ellipsis)
# often floats above the baseline in diffused images and would skew the
# estimate; rows without any big glyphs fall back to all bottoms.
BASELINE_RESTING = (
    set("abcdefhiklmnorstuvwxz")
    | set("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
    | set("zero one two three four five six seven eight nine".split())
)
DESCENDER_GLYPHS = set("gjpqy")


def _row_baseline(bottoms):
    """The row baseline from per-cell ink bottoms.

    Most cells rest on the baseline, so the exact mode is the baseline. When
    the raster is too noisy for exact duplicates, fall back to the densest
    6px band: the resting cluster is the largest group of bottoms, while
    glyphs hanging above the baseline (quotes, brackets) or descending
    below it form sparser bands.
    """
    c = Counter(bottoms)
    val, count = c.most_common(1)[0]
    if count > 1:
        return val
    bins = {}
    for b in bottoms:
        bins.setdefault(b // 6, []).append(b)
    best = max(bins.values(), key=len)
    return round(sum(best) / len(best))


def estimate_font_size(glyphs, boxes, row_baselines, cols, cell):
    """Estimate the font size (em, in px) the grid was rendered at.

    The x-height is measured directly off the raster: the mode of
    (baseline - glyph top) over known x-height-only glyphs, and the em is
    taken as x-height / 0.5. Falls back to the cell-relative estimate
    (56 px font in a 96 px cell) when no x-height glyph is found.
    """
    tops = []
    for i, (name, cp) in enumerate(glyphs):
        if name not in XHEIGHT_GLYPHS:
            continue
        x0, y0, x1, y1, mask, bbox = boxes[i]
        if bbox is None:
            continue
        tops.append(row_baselines[i // cols] - (y0 + 2 + bbox[1]))
    if tops:
        return max(1, round(Counter(tops).most_common(1)[0][0] / 0.5))
    return max(1, round(cell * 56 / 96))


# --------------------------------------------------------------------- tracing

def write_pbm(path, bitmap):
    """Write a 1-bit PIL image as a binary PBM (P4) for potrace."""
    # PIL mode "1" is packed MSB-first per row, padded to a byte — same as P4.
    with open(path, "wb") as f:
        f.write(f"P4\n{bitmap.size[0]} {bitmap.size[1]}\n".encode())
        f.write(bitmap.tobytes())


def trace_bitmap(bitmap, turdsize, alphamax, opttolerance):
    """Trace a 1-bit image with potrace.

    Returns (path_d_strings, transform) where transform is the SVG <g>
    transform (tx, ty, sx, sy) that maps path coordinates to bitmap pixels:
    x_px = (x_svg - tx) / sx, y_px = (y_svg - ty) / sy.
    """
    with tempfile.TemporaryDirectory() as tmp:
        pbm = os.path.join(tmp, "glyph.pbm")
        svg = os.path.join(tmp, "glyph.svg")
        write_pbm(pbm, bitmap)
        cmd = ["potrace", "-b", "svg", "-t", str(turdsize), "-a", str(alphamax),
               "-O", str(opttolerance), "-o", svg, pbm]
        subprocess.run(cmd, check=True, capture_output=True)
        with open(svg) as f:
            text = f.read()
    m = re.search(r'transform="translate\(([^,]+),([^)]+)\) scale\(([^,]+),([^)]+)\)"', text)
    if m:
        transform = tuple(float(v) for v in m.groups())
    else:
        transform = (0.0, bitmap.size[1], 0.1, -0.1)  # potrace default
    return re.findall(r'd="([^"]*)"', text), transform


# ------------------------------------------------------------------ font build

def add_outlines(ops, transform, scale, baseline_px, origin_y, bbox_top, cu2qu_err):
    """Replay potrace outline ops (cubics) into a glyf glyph in font units.

    Path coordinates arrive in potrace's scaled SVG space; transform maps them
    back to bitmap pixels first. Cubics are converted to quadratics with the
    given error tolerance (font units).
    """
    tx, ty, sx, sy = transform

    def to_px(pt):
        return (pt[0] * sx + tx, pt[1] * sy + ty)

    def to_font(pt):
        x, y = to_px(pt)
        return (x * scale, (baseline_px - (origin_y + bbox_top + y)) * scale)

    ttpen = TTGlyphPen(None)
    pen = Cu2QuPen(ttpen, max_err=cu2qu_err, reverse_direction=False)
    for op, args in ops:
        if op == "moveTo":
            pen.moveTo(to_font(args[0]))
        elif op == "lineTo":
            pen.lineTo(to_font(args[0]))
        elif op == "curveTo":
            pen.curveTo(to_font(args[0]), to_font(args[1]), to_font(args[2]))
        elif op == "closePath":
            pen.closePath()
    glyph = ttpen.glyph()
    glyph.recalcBounds(None)
    return glyph


# Punctuation vertical normalization.
#
# The diffuser places punctuation inconsistently relative to the letters
# (e.g. period and comma bottoms 40+ px apart in the same row), and rows
# dominated by raised glyphs skew the ink-bottom baseline estimate. So the
# traced punctuation is re-anchored to standard typographic positions,
# expressed as fractions of the cap height (measured from the traced font
# itself, so it scales with the design). Values follow Merriweather Regular.
#
# ('bottom', f): shift so the glyph's ink bottom sits at f * capHeight.
# ('center', f): shift so the glyph's ink center sits at f * capHeight.
PUNCT_ANCHOR = {
    # baseline sitters (bottom on the baseline)
    "period": ("bottom", 0.00), "colon": ("bottom", 0.00),
    "exclam": ("bottom", 0.00), "question": ("bottom", 0.00),
    "numbersign": ("bottom", 0.00), "dollar": ("bottom", 0.00),
    "percent": ("bottom", 0.00), "ampersand": ("bottom", 0.00),
    "at": ("bottom", 0.00), "sterling": ("bottom", 0.00),
    "yen": ("bottom", 0.00), "euro": ("bottom", 0.00),
    "cent": ("bottom", 0.00), "copyright": ("bottom", 0.00),
    "registered": ("bottom", 0.00),
    # ellipsis dots rest on the baseline (not floating mid-height)
    "ellipsis": ("bottom", 0.00),
    # comma / semicolon: dot on baseline, tail below
    "comma": ("bottom", -0.25), "semicolon": ("bottom", -0.25),
    # quotes sit above cap height
    "quotesingle": ("bottom", 0.61), "quotedbl": ("bottom", 0.61),
    "grave": ("bottom", 0.88), "acute": ("bottom", 0.88),
    "trademark": ("bottom", 0.50), "section": ("bottom", -0.13),
    # vertically centered on the cap-height axis
    "hyphen": ("center", 0.43), "minus": ("center", 0.43),
    "asterisk": ("center", 0.45), "plus": ("center", 0.41),
    "equal": ("center", 0.40), "less": ("center", 0.41),
    "greater": ("center", 0.41), "multiply": ("center", 0.41),
    "divide": ("center", 0.43), "asciitilde": ("center", 0.40),
    "asciicircum": ("center", 0.59), "degree": ("center", 0.85),
    "parenleft": ("center", 0.43), "parenright": ("center", 0.43),
    "bracketleft": ("center", 0.45), "bracketright": ("center", 0.45),
    "braceleft": ("center", 0.45), "braceright": ("center", 0.45),
    "slash": ("center", 0.41), "backslash": ("center", 0.41),
    "underscore": ("center", -0.21),
}


def normalize_punctuation(glyphs_out, cap, xheight=None):
    """Re-anchor punctuation glyphs to standard vertical positions.

    Returns the number of glyphs shifted. cap is the traced font's cap
    height in units; each glyph is moved so its bottom (or center) lands at
    the fraction of cap height given by PUNCT_ANCHOR. xheight is the traced
    x-height: the asterisk is centered on it (convention: asterisk center
    sits on the x-height line, ~95% of x-height across real fonts).
    """
    n = 0
    for name, (glyph, adv, lsb) in glyphs_out.items():
        anchor = PUNCT_ANCHOR.get(name)
        if anchor is None:
            continue
        if name == "asterisk" and xheight:
            anchor = ("center", xheight / cap)
        g = glyph
        if g.numberOfContours == 0:
            continue
        if anchor[0] == "bottom":
            target = anchor[1] * cap
            dy = round(target - g.yMin)
        else:
            center = (g.yMin + g.yMax) / 2
            target = anchor[1] * cap
            dy = round(target - center)
        if dy == 0:
            continue
        g.coordinates.translate((0, dy))
        g.recalcBounds(None)
        n += 1
    return n


def build_font(glyphs_out, cmap_entries, upem, ascent, descent, family,
               weight, italic, src_metrics=None, style=None):
    """Assemble a TTFont from traced glyph outlines.

    glyphs_out: {name: (Glyph, advance)}; cmap_entries: {codepoint: name}.
    src_metrics: (hhea_asc, hhea_desc, typo_asc, typo_desc, win_asc, win_desc,
    fs_selection) in target upem units — copied verbatim from the source font
    so layout (and the central baseline used by renderers) matches.
    style: subfamily name (name ID 2/17), e.g. "Light", "Bold Italic".
    Defaults to "Italic"/"Regular" from the italic flag. When given, the
    typographic family names (IDs 16/17) are also written so styles group
    under one family in the OS font picker.
    """
    bold = weight >= 600
    names = list(glyphs_out)
    order = [".notdef"] + names
    font = TTFont()
    font.setGlyphOrder(order)
    font["glyf"] = newTable("glyf")
    font["glyf"].glyphs = {}
    font["glyf"].glyphOrder = order
    font["loca"] = newTable("loca")

    # .notdef: simple rectangle
    nd_pen = TTGlyphPen(None)
    nd_pen.moveTo((0, 0))
    nd_pen.lineTo((0, 0.7 * upem))
    nd_pen.lineTo((0.5 * upem, 0.7 * upem))
    nd_pen.lineTo((0.5 * upem, 0))
    nd_pen.closePath()
    nd = nd_pen.glyph()
    nd.width = max(1, round(0.5 * upem))
    nd.recalcBounds(None)
    font["glyf"][".notdef"] = nd

    for name, (glyph, adv, lsb) in glyphs_out.items():
        glyph.width = adv
        font["glyf"][name] = glyph

    # cmap (format 4 + format 12)
    cmap = newTable("cmap")
    cmap.tableVersion = 0
    f4 = CmapSubtable.getSubtableClass(4)(4)
    f4.platformID, f4.platEncID, f4.language = 3, 1, 0
    f4.cmap = dict(cmap_entries)
    f12 = CmapSubtable.getSubtableClass(12)(12)
    f12.platformID, f12.platEncID, f12.language = 3, 10, 0
    f12.cmap = dict(cmap_entries)
    cmap.tables = [f4, f12]
    font["cmap"] = cmap

    # head
    head = newTable("head")
    head.tableVersion = 1.0
    head.fontRevision = 1.0
    head.checkSumAdjustment = 0
    head.magicNumber = 0x5F0F3CF5
    head.flags = 0x000B
    head.unitsPerEm = upem
    head.created = head.modified = timestampNow()
    head.xMin = head.yMin = head.xMax = head.yMax = 0
    head.macStyle = (1 if bold else 0) | (2 if italic else 0)
    head.lowestRecPPEM = 8
    head.fontDirectionHint = 2
    head.indexToLocFormat = 0
    head.glyphDataFormat = 0
    font["head"] = head

    # hhea
    hhea = newTable("hhea")
    hhea.tableVersion = 1.0
    hhea.ascent = ascent
    hhea.descent = descent
    hhea.lineGap = 0
    hhea.advanceWidthMax = max((a for _, (_, a, _) in glyphs_out.items()), default=upem)
    hhea.minLeftSideBearing = 0
    hhea.minRightSideBearing = 0
    hhea.xMaxExtent = 0
    hhea.caretSlopeRise = 1
    hhea.caretSlopeRun = 1 if italic else 0
    hhea.caretOffset = 0
    hhea.reserved0 = hhea.reserved1 = hhea.reserved2 = hhea.reserved3 = 0
    hhea.metricDataFormat = 0
    hhea.numberOfHMetrics = len(order)
    font["hhea"] = hhea

    # hmtx
    hmtx = newTable("hmtx")
    hmtx.metrics = {name: (adv, lsb) for name, (_, adv, lsb) in glyphs_out.items()}
    hmtx.metrics[".notdef"] = (nd.width, 0)
    font["hmtx"] = hmtx

    # maxp (version 1.0, computed from glyf)
    maxp = newTable("maxp")
    maxp.tableVersion = 0x00010000
    maxp.numGlyphs = len(order)
    maxp.maxPoints = maxp.maxContours = 0
    for name in order:
        g = font["glyf"][name]
        maxp.maxPoints = max(maxp.maxPoints, len(g.coordinates))
        maxp.maxContours = max(maxp.maxContours, len(g.endPtsOfContours))
    maxp.maxCompositePoints = maxp.maxCompositeContours = 0
    maxp.maxZones = 1
    maxp.maxTwilightPoints = 0
    maxp.maxStorage = maxp.maxFunctionDefs = maxp.maxInstructionDefs = 0
    maxp.maxStackElements = maxp.maxSizeOfInstructions = 0
    maxp.maxComponentElements = maxp.maxComponentDepth = 0
    font["maxp"] = maxp

    # OS/2
    os2 = newTable("OS/2")
    os2.version = 3
    os2.xAvgCharWidth = int(statistics.mean(
        [a for _, (_, a, _) in glyphs_out.items()] or [upem // 2]))
    os2.usWeightClass = weight
    os2.usWidthClass = 5
    os2.fsType = 0
    os2.ySubscriptXSize = os2.ySubscriptYSize = int(0.65 * upem)
    os2.ySubscriptXOffset = 0
    os2.ySubscriptYOffset = int(0.14 * upem)
    os2.ySuperscriptXSize = os2.ySuperscriptYSize = int(0.65 * upem)
    os2.ySuperscriptXOffset = 0
    os2.ySuperscriptYOffset = int(0.47 * upem)
    os2.yStrikeoutSize = int(0.05 * upem)
    os2.yStrikeoutPosition = int(0.26 * upem)
    os2.sFamilyClass = 0
    os2.panose = Panose()
    os2.ulUnicodeRange1 = os2.ulUnicodeRange2 = 0
    os2.ulUnicodeRange3 = os2.ulUnicodeRange4 = 0
    os2.ulCodePageRange1 = os2.ulCodePageRange2 = 0
    os2.sxHeight = int(0.5 * upem)
    os2.sCapHeight = int(0.7 * upem)
    os2.usDefaultChar = 0x20
    os2.usBreakChar = 0x20
    os2.usMaxContext = 1
    os2.achVendID = "GFTR"
    if src_metrics is not None:
        _, _, typo_asc, typo_desc, win_asc, win_desc, fs_sel = src_metrics
        os2.sTypoAscender, os2.sTypoDescender = typo_asc, typo_desc
        os2.usWinAscent, os2.usWinDescent = win_asc, win_desc
        os2.fsSelection = fs_sel | (0x01 if italic else 0)
    else:
        # fsSelection: italic 0x01, bold 0x20, regular 0x40, use-typo-metrics
        # 0x80 — same values Merriweather (a properly grouped family) uses
        os2.fsSelection = 0x80 | (0x01 if italic else 0) \
            | (0x20 if bold else 0) | (0x40 if not (italic or bold) else 0)
        os2.sTypoAscender, os2.sTypoDescender = ascent, descent
        os2.usWinAscent, os2.usWinDescent = ascent, -descent
    os2.sTypoLineGap = 0
    os2.usFirstCharIndex = min(cmap_entries, default=0)
    os2.usLastCharIndex = max(cmap_entries, default=0)
    font["OS/2"] = os2

    # post
    post = newTable("post")
    post.formatType = 3.0
    post.italicAngle = -12.0 if italic else 0.0
    post.underlinePosition = -int(0.12 * upem)
    post.underlineThickness = int(0.05 * upem)
    post.isFixedPitch = 0
    post.minMemType42 = post.maxMemType42 = 0
    post.minMemType1 = post.maxMemType1 = 0
    font["post"] = post

    # name: follow the Google Fonts convention (grounded in Merriweather's
    # name table, which groups properly) — RIBBI styles (Regular/Bold/Italic/
    # Bold Italic) use plain family + style with no typographic IDs; other
    # styles (Light, etc.) use the legacy family name "Family Style" with
    # subfamily Regular/Italic and group via typographic family IDs 16/17.
    sub = style or ("Italic" if italic else "Regular")
    if sub in ("Regular", "Bold", "Italic", "Bold Italic"):
        legacy_family, legacy_sub = family, sub
        typo = []   # no typographic IDs for the 4 core styles
    else:
        base = re.sub(r"\s*italic\s*$", "", sub,
                      flags=re.IGNORECASE).strip()
        legacy_family = f"{family} {base}"
        legacy_sub = "Italic" if italic else "Regular"
        typo = [(16, family), (17, sub)]
    full = f"{family} {sub}"
    ps = f"{family.replace(' ', '')}-{sub.replace(' ', '')}"
    name_tbl = newTable("name")
    name_tbl.names = []
    base_records = [(1, legacy_family), (2, legacy_sub), (3, full),
                    (4, full), (5, "1.0"), (6, ps)]
    for pid, eid, lid in ((1, 0, 0), (3, 1, 0x409)):
        for nid, s in base_records:
            nr = NameRecord()
            nr.platformID, nr.platEncID, nr.langID, nr.nameID = pid, eid, lid, nid
            nr.string = s.encode("utf-16-be") if pid == 3 else s
            name_tbl.names.append(nr)
        if pid == 3:   # typographic IDs exist only on the Windows platform
            for nid, s in typo:
                nr = NameRecord()
                nr.platformID, nr.platEncID, nr.langID, nr.nameID = pid, eid, lid, nid
                nr.string = s.encode("utf-16-be")
                name_tbl.names.append(nr)
    font["name"] = name_tbl

    return font


# ----------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description="Trace a glyph grid image into a TTF font.")
    ap.add_argument("image", help="input grid image (PNG)")
    ap.add_argument("-o", "--out", required=True, help="output .ttf path")
    ap.add_argument("--cols", type=int, help="grid columns (default: auto-detect)")
    ap.add_argument("--rows", type=int, help="grid rows (default: auto-detect)")
    ap.add_argument("--cell", type=int, help="cell size in px (default: auto-detect)")
    ap.add_argument("--padding", type=int, help="grid padding in px (default: auto-detect)")
    ap.add_argument("--font-size", type=int, default=None,
                    help="font size the grid was rendered at (default: derived "
                         "from the cell size, e.g. 56px in a 96px cell)")
    ap.add_argument("--upem", type=int, default=1000, help="units per em (default: 1000)")
    ap.add_argument("--baseline-frac", type=float, default=0.3,
                    help="fallback baseline as fraction of font size below the "
                         "cell center (default: 0.3)")
    ap.add_argument("--baseline-px", type=float,
                    help="baseline offset from image top, in px (overrides estimation)")
    ap.add_argument("--bearing-frac", type=float, default=0.15,
                    help="side bearing as fraction of glyph width (default: 0.15)")
    ap.add_argument("--bearing-px", type=float, default=2.0,
                    help="minimum side bearing in px (default: 2)")
    ap.add_argument("--family", help="font family name, e.g. 'Edge Knight' "
                         "(default: output stem)")
    ap.add_argument("--style", help="subfamily name, e.g. 'Light Italic'. "
                         "Writes name IDs 2/17 so styles group under one "
                         "family; --weight/--italic are derived from it "
                         "unless given explicitly")
    ap.add_argument("--weight", type=int, default=None,
                    help="usWeightClass (default: from --style, else 400)")
    ap.add_argument("--italic", action="store_true",
                    help="mark the font as italic (default: from --style)")
    ap.add_argument("--invert", action="store_true",
                    help="input has light glyphs on dark background")
    ap.add_argument("--ink-grid", action="store_true",
                    help="force borderless ink-based grid detection (diffused "
                         "images usually need this; also tried automatically "
                         "when no border lines are found)")
    ap.add_argument("--mask-threshold", type=int, default=None,
                    help="gray cutoff separating ink from background per cell "
                         "(default: 128, or 170 for halftoned images)")
    ap.add_argument("--turdsize", type=int, default=1,
                    help="potrace speckle size threshold, 0=keep everything (default: 1)")
    ap.add_argument("--alphamax", type=float, default=1.0,
                    help="potrace corner smoothing, 1.0=sharp (default: 1.0)")
    ap.add_argument("--opttolerance", type=float, default=0.1,
                    help="potrace path simplification, lower=tighter (default: 0.1)")
    ap.add_argument("--cu2qu-err", type=float, default=0.5,
                    help="cubic->quadratic conversion error in font units, "
                         "lower=tighter (default: 0.5)")
    ap.add_argument("--no-space", action="store_true",
                    help="include space/nbspace cells in the grid and add no "
                         "space glyphs to the font")
    ap.add_argument("--no-punct-normalize", action="store_true",
                    help="don't re-anchor punctuation to standard vertical "
                         "positions (diffused grids place it inconsistently)")
    ap.add_argument("--metrics-from", metavar="FONT.ttf",
                    help="copy vertical metrics and per-glyph advance widths "
                         "from a source font (the one the grid was rendered "
                         "with), scaled to --upem")
    args = ap.parse_args()

    img = Image.open(args.image).convert("L")
    if args.invert:
        img = img.point(lambda p: 255 - p)

    halftone = detect_halftone(img)
    if halftone:
        img = img.resize((img.size[0] // 2, img.size[1] // 2), Image.BOX)
        print("halftone detected; downsampled 2x to reconstruct the glyphs")

    # Grid geometry: (cols, rows, cell, padding) plus an optional explicit
    # row-major list of (x0, y0, x1, y1) cell boxes for irregular grids.
    cells = None
    ink_detected = False
    if args.cols and args.rows and args.cell and args.padding:
        cols, rows, cell, padding = args.cols, args.rows, args.cell, args.padding
    elif args.cols and args.rows:
        det_b = detect_grid(img)
        cell = args.cell or (det_b[2] if det_b else 96)
        padding = args.padding or (det_b[3] if det_b else 40)
        cols, rows = args.cols, args.rows
    else:
        det_b = detect_grid(img) if not args.ink_grid else None
        if det_b is None:
            det2 = detect_grid_ink(img)
            if det2 is None:
                raise SystemExit(
                    "could not auto-detect the grid (no border lines and no "
                    "usable ink layout); pass --cols/--rows/--cell/--padding")
            cols, rows, cell, padding, cells = det2
            ink_detected = True
            print("borderless grid detected from ink layout")
        else:
            cols, rows, cell, padding = det_b

    if cells is None:
        cells = [(padding + c * cell, padding + r * cell,
                  padding + (c + 1) * cell, padding + (r + 1) * cell)
                 for r in range(rows) for c in range(cols)]
    else:
        # Diffused grids: the band boxes are tight and clip tall glyphs /
        # descender tails; grow each cell to the actual ink extent.
        cells = expand_cells(img, cells)

    glyphs = glyph_grid.load_glyphs(glyph_grid.DEFAULT_GLYPHS_DIR)
    if not args.no_space:
        glyphs = [(n, cp) for n, cp in glyphs if cp not in glyph_grid.BLANK_CODEPOINTS]
    n = len(glyphs)

    if rows * cols < n:
        raise SystemExit(
            f"grid {cols}x{rows} has {cols*rows} cells but {n} glyphs needed")

    # ---- optional source metrics (from the font the grid was rendered with)
    src_advances = {}   # codepoint -> advance in target upem units
    src_lsb = {}        # codepoint -> left side bearing in target upem units
    src_metrics = None  # (hhea_asc, hhea_desc, typo_asc, typo_desc, win_asc,
                        #  win_desc, fs_selection) in target upem units
    if args.metrics_from:
        if not os.path.isfile(args.metrics_from):
            raise SystemExit(f"metrics font not found: {args.metrics_from}")
        sf = TTFont(args.metrics_from, lazy=True)
        ratio = args.upem / sf["head"].unitsPerEm
        cm = sf.getBestCmap()
        for name, cp in glyphs:
            if cp in cm:
                adv, lsb = sf["hmtx"][cm[cp]]
                src_advances[cp] = round(adv * ratio)
                src_lsb[cp] = round(lsb * ratio)
        for cp in (0x20, 0xA0):
            if cp in cm:
                adv, lsb = sf["hmtx"][cm[cp]]
                src_advances[cp] = round(adv * ratio)
                src_lsb[cp] = round(lsb * ratio)
        os2 = sf["OS/2"]
        src_metrics = (round(sf["hhea"].ascent * ratio),
                       round(sf["hhea"].descent * ratio),
                       round(os2.sTypoAscender * ratio),
                       round(os2.sTypoDescender * ratio),
                       round(os2.usWinAscent * ratio),
                       round(os2.usWinDescent * ratio),
                       os2.fsSelection)

    def advance_for(cp, bw):
        if cp in src_advances:
            return max(1, src_advances[cp])
        # Real fonts carry most of the spacing on the left (LSB 1-9% em)
        # with a tight right side (RSB ~0). The old bw + 2*bearing formula
        # put ALL the bearing on the right (lsb was forced to 0), which is
        # the "extra whitespace to the right of wide letters" people saw.
        lsb = max(args.bearing_px, args.bearing_frac * bw)
        rsb = args.bearing_px
        return max(1, round((bw + lsb + rsb) * scale))

    def lsb_for(cp, bw):
        if cp in src_lsb:
            return src_lsb[cp]
        return max(1, round(max(args.bearing_px,
                                args.bearing_frac * bw) * scale))

    # ---- separate: ink mask + bbox per cell (row-major, first n cells)
    mask_thr = args.mask_threshold if args.mask_threshold is not None \
        else (170 if ink_detected else 128)
    cells = cells[:n]
    boxes = []
    for i, (x0, y0, x1, y1) in enumerate(cells):
        crop = img.crop((x0 + 2, y0 + 2, x1 - 2, y1 - 2))
        mask = crop.point(lambda p: 255 if p < mask_thr else 0)
        boxes.append((x0, y0, x1, y1, mask, mask.getbbox()))

    # ---- baseline: the most common ink bottom per row, measured within each
    # cell (every glyph sits on its row's baseline, so the mode over the row's
    # cells gives the row baseline offset). This reads the baseline straight
    # off the raster, which is more accurate than deriving it from metrics.
    row_top = {}
    for i, (x0, y0, x1, y1, _, bbox) in enumerate(boxes):
        row_top[i // cols] = min(row_top.get(i // cols, 10 ** 9), y0)
    if args.baseline_px is not None:
        base0 = args.baseline_px
        row_baselines = {r: base0 + (row_top[r] - row_top[0]) for r in row_top}
    else:
        # Mode of the absolute ink bottoms of the resting glyphs per row:
        # after cell expansion the per-cell box tops differ, so the baseline
        # must be measured in absolute image coordinates (all cells in a row
        # share one baseline). Only glyphs that rest on the baseline vote —
        # quotes/brackets hang above it and descenders below it, and either
        # would skew the estimate on ragged diffusions.
        abs_bottoms = {}
        for i, (x0, y0, x1, y1, _, bbox) in enumerate(boxes):
            if bbox is None:
                continue
            if glyphs[i][0] not in BASELINE_RESTING:
                continue
            abs_bottoms.setdefault(i // cols, []).append(y0 + 2 + bbox[3])
        row_baselines = {r: _row_baseline(bs) for r, bs in abs_bottoms.items()}
        # rows with no resting glyphs (all-symbol rows): use every bottom
        for r in row_top:
            if r in row_baselines:
                continue
            bs = [y0 + 2 + bbox[3] for i, (x0, y0, x1, y1, _, bbox)
                  in enumerate(boxes)
                  if bbox is not None and i // cols == r]
            if bs:
                row_baselines[r] = _row_baseline(bs)
        if not row_baselines:
            row_baselines = {r: row_top[r] + cell / 2
                             + args.baseline_frac * args.font_size
                             for r in row_top}
    print(f"grid: {cols}x{rows}, cell {cell}px, padding {padding}px; "
          f"row-0 baseline at y={row_baselines[0]:.0f}px")

    if args.font_size is None:
        if ink_detected:
            # Diffused grids: the cell includes inter-row gaps, so the
            # cell-relative estimate is wrong; measure the x-height instead.
            args.font_size = estimate_font_size(glyphs, boxes, row_baselines,
                                                cols, cell)
            print(f"estimated font size {args.font_size}px from x-height")
        else:
            # References render glyphs at 56px in 96px cells.
            args.font_size = max(1, round(cell * 56 / 96))
    scale = args.upem / args.font_size

    # ---- trace each glyph
    glyphs_out = {}
    cmap_entries = {}
    traced = 0
    for i, (name, cp) in enumerate(glyphs):
        x0, y0, x1, y1, mask, bbox = boxes[i]
        if bbox is None:
            print(f"  {name}: no ink, skipped")
            continue
        bleft, btop, bright, bbottom = bbox
        bw, bh = bright - bleft, bbottom - btop
        if bw < 2 or bh < 2:
            continue
        bit = mask.crop(bbox).point(lambda p: 255 if p else 0).convert("1")
        d_strings, transform = trace_bitmap(bit, args.turdsize, args.alphamax,
                                            args.opttolerance)
        if not d_strings:
            print(f"  {name}: trace produced no paths, skipped")
            continue
        rec = RecordingPen()
        for d in d_strings:
            parse_path(d, rec)
        ops = rec.value
        baseline_px = row_baselines[i // cols]
        glyph = add_outlines(ops, transform, scale,
                             baseline_px, y0 + 2, btop,
                             args.cu2qu_err)
        # The diffuser renders baseline-resting glyphs (digits, letters,
        # caps) at slightly ragged heights — some numbers sit a few px above
        # the baseline while others land on it (or a touch below). Snap the
        # ones within a small tolerance onto the row baseline so they all
        # align; glyphs with real descenders sit further below and keep
        # their position.
        float_px = baseline_px - (y0 + 2 + bbottom)
        if (name not in DESCENDER_GLYPHS and float_px
                and abs(float_px) <= 0.10 * args.font_size):
            glyph.coordinates.translate((0, -round(float_px * scale)))
            glyph.recalcBounds(None)
        adv = advance_for(cp, bw)
        glyphs_out[name] = (glyph, adv, lsb_for(cp, bw))
        cmap_entries[cp] = name
        traced += 1

    print(f"traced {traced}/{n} glyphs")

    # ---- space glyphs (not in the grid, but the font needs them)
    if not args.no_space:
        space_adv = max(1, src_advances.get(0x20, round(0.25 * args.upem)))
        for name, cp in (("space", 0x20), ("nbspace", 0xA0)):
            g = TTGlyphPen(None).glyph()
            g.width = space_adv
            glyphs_out[name] = (g, space_adv, lsb_for(cp, 0))
            cmap_entries[cp] = name

    # ---- vertical metrics (source font's, or estimated from the traced ink)
    if src_metrics is not None:
        ascent = max(1, src_metrics[0])
        descent = -max(1, -src_metrics[1])
    else:
        ascent_px = descent_px = 0
        for i, (x0, y0, x1, y1, _, bbox) in enumerate(boxes):
            if bbox is None:
                continue
            row_baseline = row_baselines[i // cols]
            top_px = y0 + 2 + bbox[1]
            bottom_px = y0 + 2 + bbox[3]
            ascent_px = max(ascent_px, row_baseline - top_px)
            descent_px = max(descent_px, bottom_px - row_baseline)
        ascent = max(1, round(max(ascent_px, 0.72 * args.font_size) * scale))
        descent = -max(1, round(max(descent_px, 0.18 * args.font_size) * scale))

    family = args.family or os.path.splitext(os.path.basename(args.out))[0]

    # style -> weight/italic defaults
    style = args.style
    weight = args.weight
    italic = args.italic
    if style:
        base = re.sub(r"\s*italic\s*$", "", style, flags=re.IGNORECASE).strip()
        if weight is None:
            weight = {
                "Thin": 100, "ExtraLight": 200, "UltraLight": 200,
                "Light": 300, "Regular": 400, "Normal": 400,
                "Medium": 500, "SemiBold": 600, "DemiBold": 600,
                "Bold": 700, "ExtraBold": 800, "UltraBold": 800,
                "Black": 900, "Heavy": 900,
            }.get(base, 400)
        if not args.italic and "italic" in style.lower():
            italic = True
    if weight is None:
        weight = 400

    if not args.no_punct_normalize:
        cap = None
        for cap_name in ("H", "A", "B", "D", "E", "F", "K", "L", "N", "R"):
            if cap_name in glyphs_out and glyphs_out[cap_name][0].numberOfContours > 0:
                cap = glyphs_out[cap_name][0].yMax
                break
        if cap is None:
            cap = max((g[0].yMax for g in glyphs_out.values()
                       if g[0].numberOfContours > 0), default=700)
        xheight = None
        for xh_name in ("x", "v", "w", "z", "n", "o"):
            if xh_name in glyphs_out \
                    and glyphs_out[xh_name][0].numberOfContours > 0:
                xheight = glyphs_out[xh_name][0].yMax
                break
        shifted = normalize_punctuation(glyphs_out, cap, xheight)
        if shifted:
            print(f"normalized {shifted} punctuation glyphs "
                  f"(cap {cap}, x-height {xheight or '?'})")

    font = build_font(glyphs_out, cmap_entries, args.upem, ascent, descent,
                      family, weight, italic, src_metrics, style)

    # head bbox from all glyphs
    xmin = ymin = 10**9
    xmax = ymax = -10**9
    glyf = font["glyf"]
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

    os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
    font.save(args.out)
    print(f"wrote {args.out} ({len(font.getGlyphOrder())} glyphs)")


if __name__ == "__main__":
    main()
