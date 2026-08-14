#!/usr/bin/env python3
"""make_fonts.py — regenerate every font in fonts/ from the diffusion sources.

Traces each family in diffusion_generations/ with the current trace_grid
code, then rebuilds the derived artifacts:

  fonts/<Family>-<Style>.ttf      the traced fonts
  fonts/EdgeKnight.zip            the Edge Knight family zipped
  out/edge_knight/*_grid.png      grid re-renders of the traced fonts
  out/edge_knight/*_compare.png   source grid | re-render, stacked
  out/edge_knight/specimen.png    every style in one image

Run from the repo root (with the venv active):

  python3 make_fonts.py
"""

import os
import subprocess
import sys
import zipfile

from PIL import Image, ImageDraw

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import glyph_grid  # noqa: E402

ROOT = os.path.dirname(os.path.abspath(__file__))
SRC_DIR = os.path.join(ROOT, "diffusion_generations")
FONTS_DIR = os.path.join(ROOT, "fonts")
EK_DIR = os.path.join(ROOT, "out", "edge_knight")
PY = sys.executable

# family -> (source subfolder, [(image, weight, italic, style), ...])
# style None derives from weight/italic (Regular/Bold/Italic/...).
FAMILIES = [
    ("EdgeKnight", "EdgeKnight", [
        ("Weathered Gothic Serif Font.png", 400, False, None),
        ("Weathered Gothic Serif _ Bold.png", 700, False, None),
        ("Weathered Gothic Serif _ Italic.png", 400, True, None),
        ("Weathered Gothic Serif _ Bold Italic.png", 700, True, None),
        ("Weathered Gothic Serif _ Light.png", 300, False, None),
        ("Weathered Gothic Serif _ Light Italic.png", 300, True, None),
    ]),
    ("Edge Knight Outline", "EdgeKnight Outline", [
        ("Weathered Gothic Serif _ Outline.png", 400, False, "Outline"),
    ]),
    ("y2kDiffusion", "y2kDiffusion", [
        ("Y2K Sans Font Grid _ Regular.png", 400, False, None),
        ("Y2K Sans _ Bold.png", 700, False, None),
        ("Y2K Sans _ Regular Italic.png", 400, True, None),
        ("Y2K Sans _ Bold Italic.png", 700, True, None),
        ("Y2K Sans _ Light.png", 300, False, None),
        ("Y2K Sans _ Light Italic _v2.png", 300, True, None),
    ]),
    ("diffuseHand", "diffuseHand", [
        ("Handwritten Font Sheet.png", 400, False, None),
    ]),
]

# explicit output names (bypass the sanitized default) — family -> out name
OUT_NAMES = {
    "Edge Knight Outline": "EdgeKnightOutline-Outline.ttf",
}


def style_for(weight, italic):
    if weight == 700:
        return "Bold Italic" if italic else "Bold"
    if weight == 300:
        return "Light Italic" if italic else "Light"
    return "Italic" if italic else "Regular"


def trace(image, out, family, weight, italic, style):
    cmd = [PY, os.path.join(ROOT, "trace_grid.py"), image,
           "-o", out, "--family", family, "--style", style,
           "--weight", str(weight)]
    if italic:
        cmd.append("--italic")
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        sys.exit((p.stdout or "") + (p.stderr or ""))
    print(f"  wrote {os.path.basename(out)}")


def main():
    os.makedirs(FONTS_DIR, exist_ok=True)
    os.makedirs(EK_DIR, exist_ok=True)

    # stale fonts from earlier naming conventions are replaced
    for name in os.listdir(FONTS_DIR):
        if name.endswith(".ttf"):
            os.remove(os.path.join(FONTS_DIR, name))

    # out/edge_knight/ is fully generated — rebuild it from scratch
    # (subdirectories like tuned/ are the user's own work, left alone)
    for name in os.listdir(EK_DIR):
        p = os.path.join(EK_DIR, name)
        if os.path.isfile(p):
            os.remove(p)

    traced = []   # (family, style, font_path, source_image)
    for family, folder, entries in FAMILIES:
        print(f"== {family} ==")
        for image, weight, italic, style in entries:
            style = style or style_for(weight, italic)
            src = os.path.join(SRC_DIR, folder, image)
            out = os.path.join(FONTS_DIR, OUT_NAMES.get(family, ""))
            if not out.endswith(".ttf"):
                out = os.path.join(
                    FONTS_DIR, glyph_grid.sanitize_name(
                        f"{family}-{style.replace(' ', '')}") + ".ttf")
            trace(src, out, family, weight, italic, style)
            traced.append((family, style, out, src))

    # ---- Edge Knight zip -------------------------------------------------
    ek_fonts = [t for t in traced if t[0] == "EdgeKnight"
                or t[0] == "Edge Knight Outline"]
    zip_path = os.path.join(FONTS_DIR, "EdgeKnight.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for _, _, font, _ in ek_fonts:
            z.write(font, os.path.basename(font))
    print(f"wrote {os.path.relpath(zip_path)}")

    # ---- out/edge_knight artifacts --------------------------------------
    ek_grids = []   # (style label, grid png, source image)
    for family, style, font, src in traced:
        if family != "EdgeKnight" and family != "Edge Knight Outline":
            continue
        stem = os.path.join(EK_DIR,
                            f"{family.replace(' ', '')}-{style.replace(' ', '')}")
        grid = stem + "_grid"
        subprocess.run([PY, os.path.join(ROOT, "glyph_grid.py"), font,
                        "-o", grid, "--format", "both", "--scale", "2"],
                       check=True, capture_output=True)
        grid_png = grid + ".png"
        ek_grids.append((style, grid_png, src))

        # compare: source resized to the grid size, stacked above the render
        g = Image.open(grid_png).convert("L")
        s = Image.open(src).convert("L").resize(g.size)
        side = Image.new("L", (g.width, g.height * 2 + 30), 255)
        side.paste(s, (0, 0))
        side.paste(g, (0, g.height + 30))
        compare = stem + "_compare.png"
        side.save(compare)
        print(f"  wrote {os.path.basename(compare)}")

    # ---- specimen: every style in one labelled image --------------------
    THUMB_W = 700
    LABEL_H = 26
    thumbs = []
    for label, grid_png, _ in ek_grids:
        im = Image.open(grid_png).convert("RGB")
        h = max(1, round(im.height * THUMB_W / im.width))
        thumbs.append((label, im.resize((THUMB_W, h))))
    canvas = Image.new("RGB",
                       (THUMB_W + 40,
                        sum(im.height + LABEL_H for _, im in thumbs) + 20),
                       "white")
    d = ImageDraw.Draw(canvas)
    y = 10
    for label, im in thumbs:
        canvas.paste(im, (20, y))
        y += im.height
        d.text((22, y + 8), label, fill="#222222")
        y += LABEL_H
    spec = os.path.join(EK_DIR, "specimen.png")
    canvas.save(spec)
    print(f"wrote {os.path.relpath(spec)}")


if __name__ == "__main__":
    main()
