# ABOUT

simpleFontTool is a tool that aims to do three things:

1. Make it easy to create a font from a bitmap grid (though it is opinionated on how its grids are laid out, using the Google Fonts latin Kernel with the space characters removed).
2. Create specimens for easy use as references in a diffusion system (the sample fonts were generated using Reve)
3. Allow making fixes/alterations to fonts as necessary.

Reference grids are not included; make_references uses Merriweather, Merriweather Sans, IBM Plex (Serif/Sans/Mono), and Aleo to make grids that can be used as references. When using Reve, I give a set of 20 grids (various weights and italicization) to give a fairly diverse background.

Note that diffusion models are nowhere near ready to make production-ready type, so you will need to do fixing by hand. Some of this is automatically done by the system, but I primarily made this so that I could whip up a novelty/title font in a couple minutes.

# Glyph Grid — GF Latin Kernel reference images

Renders the [GF Latin Kernel](https://github.com/googlefonts/glyphsets) glyph
set (116 glyphs) as a plain grid for any font file — no labels, no headers;
the output filename carries the font identity. Reference images for the
font-generation pipeline are generated into [`out/`](out/) (regenerable,
gitignored — see [Regenerating everything](#regenerating-everything)).

## Setup

Python dependencies live in a virtualenv so the host system stays clean:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
# then run the tools with the venv's interpreter:
.venv/bin/python3 glyph_grid.py MyFont.ttf
```

or activate it once per shell: `source .venv/bin/activate`.

Two system binaries are also needed (not pip-installable):

- **potrace** — traces grid images into outlines (`trace_grid.py`)
- **chromium** — rasterizes SVGs to PNGs (`glyph_grid.py`,
  `make_specimen_pdf.py`; `rsvg-convert` works as a fallback)

## Usage

```bash
# One font → out/<Font>_<Style>.svg + .png (font embedded in the SVG)
python3 glyph_grid.py /path/to/Merriweather_Bold.ttf

# Raster only, at 2x, custom grid, keep space/nbspace
python3 glyph_grid.py MyFont.otf --format png --scale 2 --cols 20 --cell 120 --font-size 72 --include-space

# All 42 variants (7 families × light/regular/bold × roman/italic)
FONT_DIR=/usr/local/share/fonts ./make_references.sh
```

## What you get

- **SVG** — vector grid with the font embedded via `@font-face` data URI, so it
  renders anywhere. Use `--no-embed` to reference the installed font by family
  name instead (smaller files, relies on system font matching).
- **PNG** — rasterized with headless Chromium from the SVG.
- Glyphs missing from the font's cmap get a hatched cell.
- `space`/`nbspace` are excluded by default (they render blank), and columns
  are auto-packed into a full rectangle — the grid contains no empty cells,
  so it's safe to feed straight into a diffusion renderer. Pass
  `--include-space` to keep them.

## Tracing a grid back into a font (the return trip)

`trace_grid.py` does the reverse: takes a grid image (e.g. a diffuser's output),
splits it into cells, traces each glyph to vector outlines with Potrace, and
builds a working `.ttf`.

```bash
python3 trace_grid.py diffuser_output.png -o out/traced.ttf

# The grid is auto-detected from the cell borders; override if needed:
python3 trace_grid.py img.png -o out.ttf --cols 19 --rows 6 --cell 96 --padding 40

# Name / style the resulting font, tune tracing:
python3 trace_grid.py img.png -o out.ttf --family "My Font" --weight 300 --italic \
    --turdsize 1 --alphamax 1.0 --bearing-frac 0.2
```

Cells map row-major to the GF Latin Kernel order (same layout as the
references). Missing/empty cells are skipped; `space`/`nbspace` are added to
the font automatically. The baseline is estimated from the ink bottoms; pass
`--baseline-px` if a diffuser output sits off the grid baseline.

### Baseline alignment

Diffused images place baseline-resting glyphs (digits, letters, caps) at
ragged heights — some numbers land on the baseline, others a few px above or
below. Two mechanisms keep them aligned:

- The row baseline is derived from the bottoms of the **big resting glyphs**
  (letters/digits/caps) only — small punctuation often floats above the
  baseline in diffused output and would skew the estimate (punctuation-heavy
  rows fall back to all bottoms).
- After tracing, any resting glyph whose ink bottom is within 10% of the em
  of the row baseline is **snapped onto it**. Descenders (`g j p q y`) are
  exempt so their tails are never clipped, and punctuation is re-anchored
  afterwards anyway.

### Spacing

Advance widths put the proportional side bearing on the **left** and a small
fixed bearing on the right (real fonts carry ~1-9% em on the left and almost
none on the right; the old `bw + 2*bearing` with a zero lsb piled everything
on the right — the "extra whitespace after wide letters"). Tune with
`--bearing-px` (right side + left floor, default 2.0) and `--bearing-frac`
(left side as a fraction of the glyph width, default 0.15).

### Diffused images (no grid borders)

Diffuser outputs usually drop the grid borders, so the grid is recovered from
the ink itself: row bands come from the y-projection, column bands from the
x-projection of the first row (descenders in lower rows would merge columns).
The cells are taken from the exact band boundaries, so slightly irregular
diffused grids are fine. Two things are handled automatically:

- **Grid detection** — when no border lines are found, ink-based detection
  kicks in automatically (`--ink-grid` forces it).
- **Font size** — diffused cells include large inter-row gaps, so the
  cell-relative size estimate is wrong; instead the em is derived from the
  x-height measured off the raster (x-height glyphs / 0.5).
- **Mask threshold** — diffused antialiasing keeps ink at mid-gray, so the
  ink cutoff is raised to 170 for ink-detected grids (128 for clean ones);
  override with `--mask-threshold`.

```bash
python3 trace_grid.py diffusion_output.png -o out.ttf --family "My Font"
```

The band-derived cells are tight, so each cell is grown outward to the
actual ink extent before tracing — otherwise tall glyphs (row-0 caps, `|`)
and thin descender tails (`y`, `g`) get clipped. Two more diffused-grid
pitfalls are handled automatically:

- **Per-row column boundaries** — the diffuser jitters glyph x-positions per
  row (especially italics), so a single set of column midpoints makes a
  glyph pick up its neighbor's ink (`l` gains `m`'s left stem, `C` gains
  `D`'s). Each row's columns are measured from its own projection instead.
- **Punctuation vertical normalization** — the diffuser places punctuation
  inconsistently relative to the letters (period and comma bottoms 40+ px
  apart in the same row), and punctuation-heavy rows skew the ink-bottom
  baseline estimate. Punctuation is re-anchored to standard typographic
  positions measured from the traced font's own cap height (baseline,
  comma tail, quotes at cap height, etc.); the asterisk is centered on the
  measured x-height and the ellipsis rests on the baseline.
  `--no-punct-normalize` disables.

Caveats: without `--metrics-from`, advance widths and vertical metrics are
estimated from the raster; light/thin or tiny glyphs (®, ©) lose the most
fidelity to potrace smoothing.

### Fidelity tuning (measured on the reference grids)

Tracing fidelity is dominated by three things, in order:

1. **Reference resolution** — the grid must be rendered at higher resolution
   for a tight round trip. Measured shape IoU on Merriweather Regular: 0.85 at
   1x, 0.93 at 2x, 0.97 at 4x. Render references with `--scale 2` or `--scale 4`.
2. **Unhinted rasters** — render references with `--unhinted`
   (`text-rendering=geometricPrecision`). Hinting snaps stems to the pixel
   grid, which traced outlines can't reproduce; unhinted rasters trace cleanly.
3. **Exact source metrics** — pass `--metrics-from FONT.ttf` (the font the grid
   was rendered with) so the traced font copies the source's hhea, OS/2
   sTypo/win metrics, fsSelection, advances and side bearings verbatim. This
   is what aligns re-renders to the original (e.g. Lato's unusual 2000-upem
   metrics would otherwise shift everything ~9px).

Potrace parameters (`--opttolerance`, `--turdsize`, `--cu2qu-err`) move
fidelity by only ~1% — the knobs above are what matter.

`trace_compare.py` runs this pipeline for a set of fonts and writes
`out/traced/`: `<name>_ref.png` (source grid), `<name>_traced.ttf`,
`<name>_traced_grid.png` (re-render), `<name>_compare.png` (side by side) and
`<name>_diff.png` (red = source-only ink, green = traced-only ink).

## Tuning metrics and spacing

Traced fonts estimate advances, bearings and vertical metrics from the
raster. `tune_metrics.py` adjusts them afterwards:

```bash
# inspect current metrics per glyph
python3 tune_metrics.py fonts/EdgeKnight-Regular.ttf --list --glyphs a,y

# apply adjustments (CLI flags, JSON file, or both)
python3 tune_metrics.py fonts/EdgeKnight-Regular.ttf -o out.ttf \
    --ascent 760 --descent -240 --advance-scale 1.03 --bearing 6
python3 tune_metrics.py fonts/EdgeKnight-Regular.ttf -o out.ttf --adjustments tune.example.json
```

`tune.example.json` shows the full schema: vertical metrics, a global
`baseline_shift` (+ = up), `advance_scale`, side `bearing`, per-glyph
`{shift, advance, lsb}` overrides (handy for nudging individual letter
baselines), and `kerning` pairs written to a `kern` table.

## Automatic specimen PDFs

`make_specimen_pdf.py` renders one A4 page per font (family name, pangram,
alphabet, digits, size ladder, paragraph) with the fonts embedded, then
prints to PDF via headless Chromium:

```bash
python3 make_specimen_pdf.py                     # all of fonts/*.ttf
python3 make_specimen_pdf.py -o out/specimen.pdf fonts/*.ttf
```

## Interactive UI: metrics, kerning and glyph editing

`font_ui.py` serves a local web UI for the whole tuning loop — no
dependencies beyond the stdlib + fontTools:

```bash
python3 font_ui.py              # http://127.0.0.1:8000/ (opens the browser)
python3 font_ui.py --port 8123 --no-open
```

The UI loads any TrueType font (the picker scans `fonts/`, `out/traced/` and
the repo root), then:

- **Theme** — light/dark toggle in the header (persisted; defaults to the
  system preference).
- **Layout** — drag the divider between the glyph grid and the editing panel
  to resize either side (double-click resets); the glyph editor canvas
  re-fits itself to the panel width.
- **Preview** — a live text preview strip with quick test strings: pangrams
  in mixed case, ALL CAPS and all lower, digits, and the full GF Latin
  Kernel (all 116 glyphs including every piece of punctuation) so the whole
  coverage can be eyeballed at once.
- **Trace** — batch-convert diffusion output by family: each subfolder of
  `diffusion_generations/` is a family (the picker lists them). Weight and
  italic are read from file names when they carry hints ("Light Italic" →
  300 + italic); images without a hint — or any selected set, via
  "Review assignments…" — open a thumbnail gallery to assign weight/italic
  and an optional subfamily name (e.g. "Outline"). The whole family traces
  into `fonts/` with `trace_grid.py`, showing per-job progress; existing
  fonts with the same name are only overwritten after confirmation. Advanced
  knobs (turdsize/alphamax/opttolerance/cu2qu error, invert, baseline, mask
  threshold, `--metrics-from`, side-bearing px/frac) live under "Trace
  options".
- **Metrics** — ascent/descent/linegap, baseline shift, advance scale and
  side bearings as live overrides (same schema as `tune.example.json`),
  applied idempotently on top of the base file.
- **Kerning** — add/remove/edit pairs by typing two characters; the `kern`
  table is rebuilt on every change.
- **Glyph grid** — every cmap glyph rendered live in the embedded font; click
  a cell to edit.
- **Glyph editor** — TrueType point editing on a canvas with baseline/
  x-height/cap/ascender/descender/advance guides and full viewport control:
  wheel zoom around the cursor, space/middle-drag pan, Fit (F). Multi-handle
  editing: shift-click to add points, drag on empty space for a marquee,
  click a curve to select its whole contour; selected points drag together,
  nudge with the arrow keys (Shift = 10) and delete with Del/right-click.
  Scale (S) and Rotate (R) tools transform the selection around its center
  (Shift snaps), Flip H/V mirror it, and every operation is undoable. Plus
  double-click a curve to insert a point, per-glyph advance/LSB overrides,
  and undo/redo. Outline changes save back into the working font.
- **Identity** — family/style/weight/italic rewrite the name table (IDs 1–6,
  16/17) and the style bits.
- **Export** — `Save font` writes `out/ui/<name>.ttf`; `Download` streams the
  current working font.

**Session persistence** — the whole editing state (loaded font, metrics,
kerning, identity, every outline edit) is mirrored into the browser's
`localStorage` after each change and on tab close, and replayed
automatically on the next load — closing the tab, or even a server restart,
loses nothing. Unsaved outline edits are captured on tab close too (and
beaconed to the server), and the restored session re-selects the glyph you
were editing. If the saved font file no longer exists the backup is
discarded with a notice.

All edits apply to an in-memory copy of the font; nothing is written until
you save or download. Composite glyphs are decomposed on load (this also
lets `tune_metrics.apply_adjustments` handle fonts with composite glyphs).
The server is a single-user localhost tool; the working state lives in the
server process and resets on restart.

## Regenerating everything

`make_fonts.py` re-traces every family in `diffusion_generations/` with the
current `trace_grid.py` and rebuilds the derived artifacts (fonts, the
Edge Knight zip, and the `out/edge_knight/` grids/compares/specimen):

```bash
# with the venv active (see Setup)
python3 make_fonts.py
```

The per-style weights/italics are defined explicitly at the top of the
script; new diffusion images just need a row added there. The reference
grids (`out/`) come from the installed system fonts:

```bash
FONT_DIR=/usr/local/share/fonts ./make_references.sh
```

## License

MIT (see [LICENSE](LICENSE)). The vendored glyph data in `glyphs/` comes
from googlefonts/glyphsets and keeps its Apache 2.0 license; the reference
fonts used to render grids are SIL OFL — see
[THIRD_PARTY_NOTICES](THIRD_PARTY_NOTICES) for details. Fonts traced from
`diffusion_generations/` are original to this project and MIT-licensed;
fonts traced from a reference grid inherit the source font's license.

## Files

- `glyph_grid.py` — render a glyph grid reference image (input: any TTF/OTF).
- `trace_grid.py` — trace a glyph grid image back into a TTF (needs `potrace`).
- `make_fonts.py` — regenerate every traced font in `fonts/` and the
  `out/edge_knight/` artifacts from `diffusion_generations/`.
- `tune_metrics.py` — adjust metrics, spacing, baselines and kerning of a traced font.
- `make_specimen_pdf.py` — build a specimen PDF from any set of fonts (needs `chromium`).
- `font_ui.py` — local web UI for metrics, kerning, identity and glyph editing.
- `font_ui.html` — the UI page (served by `font_ui.py`).
- `requirements.txt` — Python dependencies for the virtualenv.
- `LICENSE` / `THIRD_PARTY_NOTICES` — MIT license and third-party attributions.
- `tune.example.json` — example adjustments for the traced fonts.
- `trace_compare.py` — trace a set of references and build comparison/diff images.
- `make_references.sh` — batch renderer for the standard font set.
- `glyphs/` — vendored glyph list + codepoints from googlefonts/glyphsets
  (used to keep the tool working offline).
- `diffusion_generations/` — the diffused grid images, one subfolder per family.
- `fonts/` — traced output fonts (original to this project).
- `out/` — generated output (reference grids, `edge_knight/` artifacts,
  UI exports, specimen PDF). Fully recreated by `make_fonts.py` /
  `make_references.sh` / the UI, and gitignored — nothing in `out/` is
  committed to the repo.

### Example: Edge Knight (traced from diffused grids)

7 diffused images in `diffusion_generations/` (halftoned, no borders) were
traced into a full family in `fonts/`:

| Image | Font | Weight | Style |
|---|---|---|---|
| `Weathered Gothic Serif Font.png` | Edge Knight | 400 | regular |
| `Weathered Gothic Serif _ Light.png` | Edge Knight Light | 300 | regular |
| `Weathered Gothic Serif _ Bold.png` | Edge Knight Bold | 700 | regular |
| `Weathered Gothic Serif _ Italic.png` | Edge Knight Italic | 400 | italic |
| `Weathered Gothic Serif _ Light Italic.png` | Edge Knight Light Italic | 300 | italic |
| `Weathered Gothic Serif _ Bold Italic.png` | Edge Knight Bold Italic | 700 | italic |
| `Weathered Gothic Serif _ Outline.png` | Edge Knight Outline | 400 | regular |

Each font has all 114 kernel glyphs (117 with `.notdef`/space/nbspace) and
replicates its source image at **0.92–0.95 mean glyph IoU** (glyph bbox
normalized to a common canvas; the lowest-scoring cells are tiny glyphs like
`,` where the diffuser's rendering is ambiguous). Row boundaries are refined
from the actual ink extents before tracing (descender tails and tall row-0
glyphs aren't clipped), columns are measured per row (no neighbor bleed), and
punctuation is normalized to standard vertical positions. Metrics are estimated from the raster — pass `--metrics-from` if you
later get your hands on the original Weathered Gothic Serif font to copy its
exact advances and vertical metrics, or tune them with `tune_metrics.py`
(`tune.example.json` has a starter kerning set).
