#!/usr/bin/env python3
"""make_specimen_pdf.py — render font specimen PDFs.

Builds an HTML specimen (one A4 page per font: family name, pangram,
alphabet, digits, punctuation, a size ladder and a paragraph) with the fonts
embedded as base64 @font-face, then prints it to PDF with headless Chromium.

Usage:
  make_specimen_pdf.py                    # all fonts in fonts/*.ttf
  make_specimen_pdf.py FONT.ttf ...       # specific fonts
  make_specimen_pdf.py -o out/specimen.pdf fonts/*.ttf
"""

import argparse
import base64
import glob
import os
import subprocess
import sys
import tempfile

from fontTools.ttLib import TTFont

PANGRAM = "The quick brown fox jumps over the lazy dog."
ALPHABET = ("ABCDEFGHIJKLMNOPQRSTUVWXYZ\n"
            "abcdefghijklmnopqrstuvwxyz")
DIGITS = "0123456789 .,:;!? &$@%#*+-=/()[]{} <>^~|_\\"
EXTRA = "© ® ° € £ ¥ ¢ ÷ × § "  # only render what the font has
PARA = ("Edge Knight was traced from a diffused glyph grid and converted to "
        "vector outlines — the return trip from the diffuser. Weathered "
        "serifs, crisp edges and a little grit survive the journey. "
        "0123456789 — all the way down.")


def font_data_uri(path):
    with open(path, "rb") as f:
        return "data:font/ttf;base64," + base64.b64encode(f.read()).decode()


def family_name(path):
    try:
        return TTFont(path).getBestFamilyName() or os.path.splitext(
            os.path.basename(path))[0]
    except Exception:
        return os.path.splitext(os.path.basename(path))[0]


def build_html(fonts):
    faces = ""
    sections = []
    for path in fonts:
        fam = family_name(path)
        slug = "f" + str(len(sections))
        faces += (f"@font-face {{ font-family: '{slug}'; "
                  f"src: url('{font_data_uri(path)}') format('truetype'); }}\n")
        sections.append(f"""
<section class="page">
  <div class="fam" style="font-family:'{slug}'">{fam}</div>
  <div class="sub">traced from diffused glyph grid &middot; GF Latin Kernel &middot; 114 glyphs</div>
  <div class="huge" style="font-family:'{slug}'">Weathered &amp; Bold</div>
  <div class="pan" style="font-family:'{slug}'">{PANGRAM}</div>
  <div class="alpha" style="font-family:'{slug}'">{ALPHABET}</div>
  <div class="digits" style="font-family:'{slug}'">{DIGITS}</div>
  <div class="ladder">
    <span style="font-family:'{slug}'; font-size:14px">Ag 14</span>
    <span style="font-family:'{slug}'; font-size:22px">Ag 22</span>
    <span style="font-family:'{slug}'; font-size:34px">Ag 34</span>
    <span style="font-family:'{slug}'; font-size:52px">Ag 52</span>
    <span style="font-family:'{slug}'; font-size:80px">Ag 80</span>
  </div>
  <div class="para" style="font-family:'{slug}'">{PARA}</div>
</section>""")
    return f"""<!doctype html>
<html><head><meta charset="utf-8">
<style>
  @page {{ size: A4; margin: 14mm; }}
  body {{ margin: 0; font-family: sans-serif; color: #111; }}
  .page {{ page-break-after: always; }}
  .page:last-child {{ page-break-after: auto; }}
  .fam {{ font-size: 40px; font-weight: bold; margin-bottom: 2px; }}
  .sub {{ font-size: 10px; color: #888; margin-bottom: 26px;
         letter-spacing: 1px; text-transform: uppercase; }}
  .huge {{ font-size: 64px; line-height: 1.1; margin-bottom: 20px; }}
  .pan {{ font-size: 30px; line-height: 1.3; margin-bottom: 22px; }}
  .alpha {{ font-size: 30px; line-height: 1.35; margin-bottom: 22px;
           white-space: pre-line; }}
  .digits {{ font-size: 26px; line-height: 1.5; margin-bottom: 22px; }}
  .ladder {{ margin-bottom: 22px; }}
  .ladder span {{ margin-right: 18px; vertical-align: bottom; }}
  .para {{ font-size: 16px; line-height: 1.55; max-width: 66ch; }}
{faces}
</style></head><body>
{''.join(sections)}
</body></html>"""


def find_chromium():
    for name in ("chromium", "chromium-browser", "google-chrome",
                 "google-chrome-stable"):
        path = os.path.join("/usr/bin", name)
        if os.path.exists(path):
            return path
    for name in ("chromium", "chromium-browser", "google-chrome"):
        from shutil import which
        p = which(name)
        if p:
            return p
    return os.environ.get("CHROME_BIN")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("fonts", nargs="*", help="font files (default: fonts/*.ttf)")
    ap.add_argument("-o", "--out", default="out/specimen.pdf",
                    help="output PDF (default: out/specimen.pdf)")
    ap.add_argument("--keep-html", action="store_true",
                    help="keep the generated HTML next to the PDF")
    args = ap.parse_args()

    fonts = args.fonts or sorted(glob.glob("fonts/*.ttf"))
    if not fonts:
        raise SystemExit("no font files given and no fonts/*.ttf found")
    for f in fonts:
        if not os.path.isfile(f):
            raise SystemExit(f"font not found: {f}")

    chrome = find_chromium()
    if not chrome:
        raise SystemExit("could not find chromium; set CHROME_BIN")

    html = build_html(fonts)
    out_abs = os.path.abspath(args.out)
    os.makedirs(os.path.dirname(out_abs) or ".", exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        html_path = os.path.join(tmp, "specimen.html")
        with open(html_path, "w") as f:
            f.write(html)
        if args.keep_html:
            with open(os.path.splitext(out_abs)[0] + ".html", "w") as f:
                f.write(html)
        cmd = [chrome, "--headless", "--disable-gpu", "--no-sandbox",
               f"--print-to-pdf={out_abs}", "--print-to-pdf-no-header",
               f"file://{html_path}"]
        subprocess.run(cmd, check=True, capture_output=True)
    print(f"wrote {args.out} ({len(fonts)} pages: "
          + ", ".join(os.path.basename(f) for f in fonts) + ")")


if __name__ == "__main__":
    main()
