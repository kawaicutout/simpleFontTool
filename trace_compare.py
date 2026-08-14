#!/usr/bin/env python3
"""trace_compare.py — trace reference grids back to fonts and build comparisons.

For each (reference grid, source font) pair:
  - trace_grid.py  -> out/traced/<Name>_traced.ttf      (with --metrics-from)
  - glyph_grid.py  -> out/traced/<Name>_traced_grid.png  (re-render, same layout)
  - out/traced/<Name>_compare.png  (source | traced side by side)
  - out/traced/<Name>_diff.png     (2x red/green pixel diff)
Prints per-font position and shape IoU.
"""

import os
import subprocess
import sys

from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import glyph_grid
import trace_grid

FONT_DIR = "/usr/local/share/fonts"

# (reference grid stem, source font file) — trace with source metrics
PAIRS = [
    ("Merriweather_Regular",       f"{FONT_DIR}/m/Merriweather_Regular.ttf"),
    ("Merriweather_Light_Italic",  f"{FONT_DIR}/m/Merriweather_LightItalic.ttf"),
    ("Merriweather_Bold_Italic",   f"{FONT_DIR}/m/Merriweather_BoldItalic.ttf"),
    ("Merriweather_Sans_Regular",  f"{FONT_DIR}/m/MerriweatherSans_Regular.ttf"),
    ("Lato_Bold",                  f"{FONT_DIR}/l/LatoWeb_Bold.ttf"),
    ("Lato_Light_Italic",          f"{FONT_DIR}/l/LatoWeb_LightItalic.ttf"),
    ("Aleo_Regular",               f"{FONT_DIR}/a/Aleo_Regular.ttf"),
    ("Aleo_Bold_Italic",           f"{FONT_DIR}/a/Aleo_BoldItalic.ttf"),
    ("IBM_Plex_Sans_Regular",      f"{FONT_DIR}/i/IBMPlexSans_Regular.ttf"),
    ("IBM_Plex_Serif_Regular",     f"{FONT_DIR}/i/IBMPlexSerif_Regular.ttf"),
    ("IBM_Plex_Serif_Bold",        f"{FONT_DIR}/i/IBMPlexSerif_Bold.ttf"),
    ("IBM_Plex_Mono_Regular",      f"{FONT_DIR}/i/IBMPlexMono_Regular.ttf"),
]


def cell_masks(img, cols, rows, cell, padding):
    masks = []
    for r in range(rows):
        for c in range(cols):
            x0, y0 = padding + c * cell, padding + r * cell
            masks.append(img.crop((x0 + 2, y0 + 2, x0 + cell - 2, y0 + cell - 2))
                         .point(lambda p: 255 if p < 128 else 0))
    return masks


def iou_cells(src_masks, tr_masks, canvas):
    """Position and shape IoU per cell; shapes are bbox-cropped and centered on
    a fixed canvas so both masks share the same coordinate space."""
    pos, shp = [], []
    for sm, tm in zip(src_masks, tr_masks):
        if sm.getbbox() is None and tm.getbbox() is None:
            pos.append(1.0)
            shp.append(1.0)
            continue

        def shape(m):
            b = m.getbbox()
            c = m.crop(b)
            cv = Image.new("L", (canvas, canvas), 0)
            cv.paste(c, ((canvas - c.width) // 2, (canvas - c.height) // 2))
            return cv

        a, b = shape(sm), shape(tm)
        inter = sum(1 for x, y in zip(a.getdata(), b.getdata()) if x and y)
        union = sum(1 for x, y in zip(a.getdata(), b.getdata()) if x or y)
        shp.append(inter / union if union else 0.0)
        inter = sum(1 for x, y in zip(sm.getdata(), tm.getdata()) if x and y)
        union = sum(1 for x, y in zip(sm.getdata(), tm.getdata()) if x or y)
        pos.append(inter / union if union else 0.0)
    return sum(pos) / len(pos), sum(shp) / len(shp)


def build_diff(src, tr, cols, rows, cell, padding, out_path):
    """Red/green diff: red = source-only ink, green = traced-only ink."""
    canvas = Image.new("RGB", (src.width, src.height), "white")
    sp = src.convert("L").point(lambda p: 255 if p < 128 else 0)
    tp = tr.convert("L").point(lambda p: 255 if p < 128 else 0)
    px = canvas.load()
    for y in range(src.height):
        for x in range(src.width):
            a, b = sp.getpixel((x, y)), tp.getpixel((x, y))
            if a and not b:
                px[x, y] = (255, 40, 40)
            elif b and not a:
                px[x, y] = (40, 220, 40)
            elif a and b:
                px[x, y] = (60, 60, 60)
    canvas.save(out_path)


def main():
    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out", "traced")
    os.makedirs(out_dir, exist_ok=True)
    py = sys.executable

    # References for tracing are rendered at 2x, unhinted: higher resolution
    # halves the rasterization noise at glyph edges, and unhinted rasters trace
    # cleanly (hinting snaps stems to the pixel grid, which the traced outlines
    # cannot reproduce). See the fidelity ladder in the README.
    scale = 2
    print(f"{'font':32s} {'pos IoU':>8s} {'shape IoU':>9s}")
    for stem, src_font in PAIRS:
        src_png = os.path.join(out_dir, f"{stem}_ref.png")
        ttf = os.path.join(out_dir, f"{stem}_traced.ttf")
        grid_png = os.path.join(out_dir, f"{stem}_traced_grid.png")

        subprocess.run([py, "glyph_grid.py", src_font, "--format", "png",
                        "--scale", str(scale), "--unhinted", "-o", src_png],
                       check=True, capture_output=True)
        subprocess.run([py, "trace_grid.py", src_png, "-o", ttf,
                        "--metrics-from", src_font],
                       check=True, capture_output=True)
        subprocess.run([py, "glyph_grid.py", ttf, "--format", "png",
                        "--scale", str(scale), "--unhinted", "-o", grid_png],
                       check=True, capture_output=True)

        src = Image.open(src_png).convert("L")
        tr = Image.open(grid_png).convert("L")
        cols, rows, cell, padding = trace_grid.detect_grid(src)
        pos, shp = iou_cells(cell_masks(src, cols, rows, cell, padding),
                             cell_masks(tr, cols, rows, cell, padding), cell)

        # side-by-side comparison (source | traced)
        side = Image.new("RGB", (src.width * 2 + 4, src.height), "white")
        side.paste(src.convert("RGB"), (0, 0))
        side.paste(tr.convert("RGB"), (src.width + 4, 0))
        side.save(os.path.join(out_dir, f"{stem}_compare.png"))

        build_diff(src, tr, cols, rows, cell, padding,
                   os.path.join(out_dir, f"{stem}_diff.png"))

        print(f"{stem:32s} {pos:8.4f} {shp:9.4f}")

    print(f"\nartifacts in {out_dir}/")


if __name__ == "__main__":
    main()
