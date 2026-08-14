#!/usr/bin/env python3
"""glyph_grid.py — render a glyph-grid reference image for a font file.

Given a TTF/OTF file, renders every glyph of a GF glyph set (default: GF Latin
Kernel) in a plain grid — no labels, no headers — and writes an SVG (and by
default also a PNG raster). The output filename carries the font identity.

Blank glyphs (space, nbspace) are excluded by default so the grid contains no
empty cells (pass --include-space to keep them); columns are auto-packed into
a full rectangle. Glyphs missing from the font's cmap get a hatched cell.

Usage:
  glyph_grid.py FONT.ttf [-o OUT] [options]

Examples:
  glyph_grid.py Merriweather_Light.ttf
  glyph_grid.py Aleo_BoldItalic.ttf --format png --scale 2
  glyph_grid.py path/to/MyFont.otf --cols 20 --cell 120 --font-size 72

The glyph list and codepoint mapping are read from <script_dir>/glyphs/
(GF_Latin_Kernel.txt + GF_Latin_Kernel.nam, both from the googlefonts/glyphsets
repo). Override with --glyphs-dir.
"""

import argparse
import base64
import math
import os
import pathlib
import re
import subprocess
from xml.sax.saxutils import escape

from fontTools.ttLib import TTFont

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_GLYPHS_DIR = os.path.join(SCRIPT_DIR, "glyphs")

# Subfamily name (name ID 2/17) -> CSS font-weight number
WEIGHT_MAP = {
    "thin": 100,
    "extralight": 200,
    "light": 300,
    "regular": 400,
    "normal": 400,
    "book": 400,
    "medium": 500,
    "semibold": 600,
    "demibold": 600,
    "bold": 700,
    "extrabold": 800,
    "black": 900,
}


def load_glyphs(glyphs_dir):
    """Return [(nice_name, codepoint), ...] zipped from the .txt and .nam files."""
    names = []
    with open(os.path.join(glyphs_dir, "GF_Latin_Kernel.txt"), encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                names.append(line)
    cps = []
    re_cp = re.compile(r"0x([0-9A-Fa-f]+)")
    with open(os.path.join(glyphs_dir, "GF_Latin_Kernel.nam"), encoding="utf-8") as f:
        for line in f:
            m = re_cp.match(line)
            if m:
                cps.append(int(m.group(1), 16))
    if len(names) != len(cps):
        raise SystemExit(
            f"glyph count mismatch: {len(names)} names vs {len(cps)} codepoints"
        )
    return list(zip(names, cps))


def font_info(path):
    """Extract font identity from the name table.

    Google-Fonts-style fonts split identity across two name pairs:
      ID16/ID17  typographic family + style  ("Merriweather", "Light")
      ID1/ID2    compatible family + style   ("Merriweather Light", "Regular")
    Returns (render_family, name_family, name_sub, weight, italic) where
    render_family is a family string unique to this file (safe to reference in
    SVG), and name_family + name_sub give the clean identity for filenames.
    """
    font = TTFont(path, lazy=True)
    name = font["name"]
    stem = os.path.splitext(os.path.basename(path))[0]

    id16 = name.getDebugName(16)
    id17 = name.getDebugName(17)
    id1 = name.getDebugName(1)
    id2 = name.getDebugName(2)

    render_family = id1 or id16 or stem
    name_family = id16 or id1 or stem
    name_sub = id17 or id2 or "Regular"

    italic = "italic" in (id2 or "").lower() or "oblique" in (id2 or "").lower()
    # Fall back to the OS/2 fsSelection italic bit when the name is ambiguous.
    if "OS/2" in font:
        try:
            italic = italic or bool(font["OS/2"].fsSelection & 0x01)
        except Exception:
            pass
    sub_lower = re.sub(r"\s*(italic|oblique)\s*$", "", (name_sub or "").lower())
    weight = WEIGHT_MAP.get(sub_lower, 400)
    # Fall back to OS/2 usWeightClass when the subfamily name is unusual.
    if "OS/2" in font and sub_lower not in WEIGHT_MAP:
        try:
            weight = font["OS/2"].usWeightClass
        except Exception:
            pass
    return render_family, name_family, name_sub, weight, italic


def sanitize_name(s):
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", s).strip("_")


EMBED_FAMILY = "gfref"

# Codepoints that render nothing and are useless to a diffusion model.
BLANK_CODEPOINTS = {0x20, 0xA0}  # space, nbspace


def best_cols(n, lo=10, hi=40, prefer=19):
    """Pick column count that packs n glyphs into a full rectangle (fewest empty cells)."""
    best = None
    for cols in range(lo, hi + 1):
        rows = math.ceil(n / cols)
        key = (cols * rows - n, abs(cols - prefer))
        if best is None or key < best[0]:
            best = (key, cols, rows)
    return best[1], best[2]


def embed_style(font_path, family):
    """Return a CSS @font-face rule embedding the font file as a data URI."""
    with open(font_path, "rb") as f:
        data = base64.b64encode(f.read()).decode("ascii")
    ext = os.path.splitext(font_path)[1].lower()
    mime = "font/otf" if ext == ".otf" else "font/ttf"
    return (
        f"<style>@font-face {{ font-family: '{family}'; src: "
        f"url(data:{mime};base64,{data}) format('truetype'); }}</style>"
    )


def build_svg(glyphs, cmap, family, weight, italic, cols, cell, font_size, padding,
              embed=False, font_path=None, unhinted=False):
    rows = math.ceil(len(glyphs) / cols)
    width = 2 * padding + cols * cell
    height = 2 * padding + rows * cell
    fam = escape(EMBED_FAMILY if embed else family)
    style = ("italic" if italic else "normal") if not embed else "normal"

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">',
    ]
    if embed:
        parts.append(embed_style(font_path, EMBED_FAMILY))
    parts += [
        "<defs>",
        '<pattern id="missing" width="8" height="8" patternUnits="userSpaceOnUse" '
        'patternTransform="rotate(45)">',
        '<rect width="8" height="8" fill="#fafafa"/>',
        '<line x1="0" y1="0" x2="0" y2="8" stroke="#e3e3e3" stroke-width="2"/>',
        "</pattern>",
        "</defs>",
        f'<rect width="{width}" height="{height}" fill="#ffffff"/>',
    ]

    for i, (name, cp) in enumerate(glyphs):
        r, c = divmod(i, cols)
        x = padding + c * cell
        y = padding + r * cell
        cx = x + cell / 2
        cy = y + cell / 2
        parts.append(
            f'<rect x="{x}" y="{y}" width="{cell}" height="{cell}" '
            f'fill="#ffffff" stroke="#e2e2e2" stroke-width="1"/>'
        )
        if cp in cmap:
            tr = ' text-rendering="geometricPrecision"' if unhinted else ""
            parts.append(
                f'<text x="{cx}" y="{cy}" font-family="{fam}" font-size="{font_size}" '
                f'font-weight="{weight}" font-style="{style}"{tr} '
                f'text-anchor="middle" dominant-baseline="central" fill="#222222">{escape(chr(cp))}</text>'
            )
        else:
            parts.append(
                f'<rect x="{x + 1}" y="{y + 1}" width="{cell - 2}" height="{cell - 2}" '
                f'fill="url(#missing)"/>'
            )

    parts.append("</svg>")
    return "\n".join(parts)


def rasterize(svg_path, png_path, width, height, scale):
    """Rasterize an SVG with headless Chromium; fall back to rsvg-convert."""
    url = pathlib.Path(svg_path).as_uri()
    candidates = [
        ["chromium", "--headless=new", "--no-sandbox", "--disable-gpu",
         "--hide-scrollbars", f"--force-device-scale-factor={scale}",
         f"--window-size={width},{height}", "--virtual-time-budget=2000",
         f"--screenshot={png_path}", url],
        ["chromium", "--headless", "--no-sandbox", "--disable-gpu",
         "--hide-scrollbars", f"--force-device-scale-factor={scale}",
         f"--window-size={width},{height}", "--virtual-time-budget=2000",
         f"--screenshot={png_path}", url],
    ]
    for cmd in candidates:
        try:
            subprocess.run(cmd, check=False, capture_output=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if os.path.exists(png_path) and os.path.getsize(png_path) > 0:
            return
    # Fallback: rsvg-convert (scale 1 only)
    subprocess.run(
        ["rsvg-convert", "-w", str(width), "-h", str(height), "-o", png_path, svg_path],
        check=True,
        capture_output=True,
        timeout=120,
    )


def main():
    ap = argparse.ArgumentParser(
        description="Render a GF Latin Kernel glyph grid for a font file."
    )
    ap.add_argument("font", help="path to a TTF/OTF font file")
    ap.add_argument("-o", "--out", help="output path (extension picks format; "
                    "default: out/<Family>_<Subfamily>.<ext>)")
    ap.add_argument("--format", choices=["svg", "png", "both"], default="both",
                    help="what to write (default: both)")
    ap.add_argument("--glyphs-dir", default=DEFAULT_GLYPHS_DIR,
                    help="directory with GF_Latin_Kernel.txt/.nam (default: "
                         "glyphs/ next to this script)")
    ap.add_argument("--cols", type=int, default=None,
                    help="grid columns (default: auto-packed into a full rectangle)")
    ap.add_argument("--include-space", action="store_true",
                    help="keep space/nbspace in the grid (they render blank; excluded by default)")
    ap.add_argument("--cell", type=int, default=96, help="cell size in px (default: 96)")
    ap.add_argument("--font-size", type=int, default=56,
                    help="glyph font size in px (default: 56)")
    ap.add_argument("--padding", type=int, default=40, help="page padding in px (default: 40)")
    ap.add_argument("--scale", type=float, default=1.0,
                    help="PNG raster scale factor (default: 1)")
    ap.add_argument("--unhinted", action="store_true",
                    help="render text with text-rendering=geometricPrecision "
                         "(disables gridfitting; smoother rasters, better for tracing)")
    ap.add_argument("--embed", dest="embed", action="store_true", default=True,
                    help="embed the font file in the SVG via @font-face data URI "
                         "(default; guarantees the exact file renders)")
    ap.add_argument("--no-embed", dest="embed", action="store_false",
                    help="reference the installed font by family name instead "
                         "(smaller SVGs, relies on system font matching)")
    args = ap.parse_args()

    if not os.path.isfile(args.font):
        raise SystemExit(f"font not found: {args.font}")

    glyphs = load_glyphs(args.glyphs_dir)
    if not args.include_space:
        glyphs = [(n, cp) for n, cp in glyphs if cp not in BLANK_CODEPOINTS]
    cols, _ = best_cols(len(glyphs)) if args.cols is None else (args.cols, None)
    render_family, name_family, name_sub, weight, italic = font_info(args.font)
    cmap = set(TTFont(args.font, lazy=True).getBestCmap())

    missing = [cp for _, cp in glyphs if cp not in cmap]
    covered = len(glyphs) - len(missing)
    print(f"{name_family} {name_sub}: {covered}/{len(glyphs)} glyphs covered"
          + (f" (missing: {', '.join(f'U+{cp:04X}' for cp in missing)})" if missing else ""))

    svg = build_svg(glyphs, cmap, render_family, weight, italic,
                    cols, args.cell, args.font_size, args.padding,
                    embed=args.embed, font_path=args.font,
                    unhinted=args.unhinted)
    width = 2 * args.padding + cols * args.cell
    height = 2 * args.padding + math.ceil(len(glyphs) / cols) * args.cell

    if args.out:
        if args.format == "both":
            # -o is a base path; write both <base>.svg and <base>.png
            out = os.path.splitext(args.out)[0]
            fmt = "both"
        else:
            out = args.out
            fmt = args.format
    else:
        stem = sanitize_name(f"{name_family} {name_sub}")
        out = os.path.join(os.path.join(SCRIPT_DIR, "out"), stem)
        fmt = args.format

    os.makedirs(os.path.dirname(os.path.abspath(out)) or ".", exist_ok=True)

    svg_path = out if fmt == "svg" else out + ".svg"
    with open(svg_path, "w", encoding="utf-8") as f:
        f.write(svg)
    print(f"  wrote {svg_path}")

    if fmt in ("png", "both"):
        png_path = out if fmt == "png" else out + ".png"
        rasterize(svg_path, png_path, width, height, args.scale)
        print(f"  wrote {png_path}")


if __name__ == "__main__":
    main()
