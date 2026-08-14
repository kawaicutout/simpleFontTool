#!/usr/bin/env python3
"""font_ui.py — local web UI for tuning metrics, kerning and glyph outlines.

Loads a TrueType font, lets you adjust global metrics (ascent/descent/
linegap, baseline shift, advance scale, bearings), edit kerning pairs,
rename the font, and edit glyph outlines point by point (TrueType quadratic
outlines with on/off-curve points). All edits apply to an in-memory copy;
Export writes a new .ttf. Uses only the Python stdlib + fontTools.

Usage:
  python3 font_ui.py [--port 8000] [--no-open]

Serves the UI at http://127.0.0.1:PORT/ and opens it in a browser by
default. Relies on tune_metrics.apply_adjustments for metric changes, so
the adjustments schema matches tune.example.json.
"""

import argparse
import glob
import io
import json
import os
import re
import subprocess
import sys
import threading
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from fontTools.pens.recordingPen import DecomposingRecordingPen
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib import TTFont
from fontTools.ttLib.tables._n_a_m_e import NameRecord
from PIL import Image

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
import glyph_grid  # noqa: E402
import tune_metrics  # noqa: E402

UI_FILE = os.path.join(SCRIPT_DIR, "font_ui.html")
OUT_DIR = os.path.join(SCRIPT_DIR, "out", "ui")

# (subdir, pattern) pairs scanned for the font picker.
FONT_GLOBS = [("fonts", "*.ttf"), ("fonts", "*.otf"),
              ("out/traced", "*.ttf"), ("out/traced", "*.otf"),
              ("out", "*.ttf"), ("out", "*.otf"),
              ("", "*.ttf"), ("", "*.otf")]

ADJ_INT_KEYS = ("ascent", "descent", "linegap", "baseline_shift", "bearing")
ADJ_FLOAT_KEYS = ("advance_scale",)
GLYPH_KEYS = ("shift", "advance", "lsb")


# --------------------------------------------------------------- outline I/O

def glyph_to_outline(font, name):
    """Serialize a glyph's TrueType outline to [{x, y, on}, ...] contours.

    Composite glyphs are decomposed first (they get flattened to simple
    outlines when saved). Returns {'contours': [...]}.
    """
    glyf = font["glyf"]
    g = glyf[name]
    if g.numberOfContours == 0:
        return {"contours": []}
    if g.numberOfContours < 0:
        rec = DecomposingRecordingPen(font.getGlyphSet())
        g.draw(rec, glyf)
        pen = TTGlyphPen(None)
        for op, args in rec.value:
            getattr(pen, op)(*args)
        g = pen.glyph()
    coords = [(round(x), round(y)) for x, y in g.coordinates]
    flags = list(g.flags)
    contours = []
    start = 0
    for end in g.endPtsOfContours:
        contours.append([{"x": coords[i][0], "y": coords[i][1],
                          "on": bool(flags[i])}
                         for i in range(start, end + 1)])
        start = end + 1
    return {"contours": contours}


def replay_contour(pen, pts):
    """Replay a cyclic TrueType contour (on/off-curve points) into a pen.

    Handles consecutive off-curve points and contours that start/end on an
    off-curve point (the TrueType implicit-midpoint / wrap rules), so any
    simple glyph round-trips exactly.
    """
    n = len(pts)
    if n == 0:
        return
    ons = [p["on"] for p in pts]
    if True not in ons:
        return
    start = ons.index(True)
    pen.moveTo((pts[start]["x"], pts[start]["y"]))
    i = (start + 1) % n
    run = []
    while i != start:
        if ons[i]:
            if run:
                pen.qCurveTo(*[(p["x"], p["y"]) for p in run],
                             (pts[i]["x"], pts[i]["y"]))
                run = []
            else:
                pen.lineTo((pts[i]["x"], pts[i]["y"]))
        else:
            run.append(pts[i])
        i = (i + 1) % n
    if run:
        pen.qCurveTo(*[(p["x"], p["y"]) for p in run], None)
    pen.closePath()


def outline_to_glyph(outline):
    """Build a simple glyf Glyph from a {'contours': [...]} outline dict."""
    pen = TTGlyphPen(None)
    for contour in outline["contours"]:
        replay_contour(pen, contour)
    g = pen.glyph()
    g.recalcBounds(None)
    return g


def check_contours(cs):
    """Validate + coerce a contours payload from the client."""
    if not isinstance(cs, list):
        raise ValueError("contours must be a list of contours")
    out = []
    for c in cs:
        if not isinstance(c, list):
            raise ValueError("each contour must be a list of points")
        pts = []
        for p in c:
            if (not isinstance(p, dict) or "x" not in p or "y" not in p
                    or "on" not in p):
                raise ValueError("point must be {x, y, on}")
            pts.append({"x": int(round(p["x"])), "y": int(round(p["y"])),
                        "on": bool(p["on"])})
        out.append(pts)
    return out


# ------------------------------------------------------------------- session

class Session:
    """In-memory working font: base file + adjustments + outline edits."""

    def __init__(self):
        self.path = None
        self.font = None
        self.adj = {}        # flat adjustments dict (tune.example.json schema)
        self.edits = {}      # glyph name -> {'contours': [...]} outline edits
        self.identity = None  # {family, subfamily, weight, italic} rename
        self._base_bytes = None  # decomposed base font, ready to reload
        self._lock = threading.Lock()  # serializes mutations + rebuilds

    def mutate(self, fn):
        """Run a state mutation + rebuild atomically.

        Threaded requests can otherwise interleave: a rebuild reads self.adj
        while another request overwrites it, leaving the working font built
        from stale adjustments (silently lost updates).
        """
        with self._lock:
            return fn()

    def load(self, path):
        font = TTFont(path)
        if "glyf" not in font:
            raise ValueError(
                "font has no 'glyf' table (CFF/OTF outlines); "
                "outline editing needs TrueType outlines")
        decompose_composites(font)
        buf = io.BytesIO()
        font.save(buf)
        self._base_bytes = buf.getvalue()
        self.path = path
        self.font = font
        kern = extract_kerning(font)
        self.adj = {"kerning": kern} if kern else {}
        self.edits = {}
        self.identity = None

    def rebuild(self):
        """Recreate the working font: decomposed base + outline edits + adjustments.

        Idempotent: every call starts from the untouched decomposed base, so
        adjustments can never accumulate.
        """
        font = TTFont(io.BytesIO(self._base_bytes))
        for name, outl in self.edits.items():
            font["glyf"][name] = outline_to_glyph(outl)
        adj = dict(self.adj)
        if "kerning" in adj:
            if adj["kerning"]:
                pass  # apply_adjustments replaces the whole kern table
            else:
                # empty kerning -> drop the table (set_kerning skips {})
                adj.pop("kerning")
                if "kern" in font:
                    del font["kern"]
        tune_metrics.apply_adjustments(font, adj)
        if self.identity:
            set_identity(font, self.identity["family"],
                         self.identity["subfamily"],
                         self.identity["weight"],
                         self.identity["italic"])
        self.font = font
        return font


def decompose_composites(font):
    """Flatten every composite glyph to a simple one.

    Makes outline editing uniform (the editor works on one point list per
    glyph) and keeps tune_metrics.apply_adjustments from crashing on
    composite glyphs (it reads glyf[name].coordinates directly).
    """
    glyf = font["glyf"]
    for name in font.getGlyphOrder():
        if glyf[name].numberOfContours < 0:
            glyf[name] = outline_to_glyph(glyph_to_outline(font, name))


def extract_kerning(font):
    """Read the existing kern table as {'LR': value} (glyph-name keys)."""
    pairs = {}
    if "kern" in font:
        for st in font["kern"].kernTables:
            for (l, r), v in st.kernTable.items():
                pairs[f"{l}{r}"] = v
    return pairs


# ------------------------------------------------------------------ identity

def font_info(font, path):
    """Summarize the working font for the UI."""
    name = font["name"]
    family = name.getDebugName(16) or name.getDebugName(1) \
        or os.path.basename(path)
    sub = name.getDebugName(17) or name.getDebugName(2) or "Regular"
    os2 = font["OS/2"]
    head = font["head"]
    hhea = font["hhea"]
    italic = bool(os2.fsSelection & 0x01) \
        or "italic" in sub.lower() or "oblique" in sub.lower()
    return {
        "path": path,
        "family": family,
        "subfamily": sub,
        "weight": getattr(os2, "usWeightClass", 400),
        "italic": italic,
        "upem": head.unitsPerEm,
        "ascent": hhea.ascent,
        "descent": hhea.descent,
        "linegap": hhea.lineGap,
        "sxHeight": os2.sxHeight or round(0.5 * head.unitsPerEm),
        "sCapHeight": os2.sCapHeight or round(0.7 * head.unitsPerEm),
        "numGlyphs": len(font.getGlyphOrder()),
    }


def set_identity(font, family, sub, weight, italic):
    """Rewrite the name table + style bits (same records as trace_grid)."""
    name = font["name"]
    name.names = []
    full = f"{family} {sub}"
    for pid, eid, lid, nid, s in [
        (1, 0, 0, 1, family), (3, 1, 0x409, 1, family),
        (1, 0, 0, 2, sub), (3, 1, 0x409, 2, sub),
        (1, 0, 0, 3, full), (3, 1, 0x409, 3, full),
        (1, 0, 0, 4, full), (3, 1, 0x409, 4, full),
        (1, 0, 0, 5, "1.0"), (3, 1, 0x409, 5, "1.0"),
        (1, 0, 0, 6, full), (3, 1, 0x409, 6, full),
        (1, 0, 0, 16, family), (3, 1, 0x409, 16, family),
        (1, 0, 0, 17, sub), (3, 1, 0x409, 17, sub),
    ]:
        nr = NameRecord()
        nr.platformID, nr.platEncID, nr.langID, nr.nameID = pid, eid, lid, nid
        nr.string = s.encode("utf-16-be") if pid == 3 else s
        name.names.append(nr)
    os2 = font["OS/2"]
    os2.usWeightClass = weight
    # fsSelection/macStyle: style bits only; REGULAR (0x40) must be clear
    # when bold/italic are set (fontTools warns otherwise).
    bold = weight >= 700
    os2.fsSelection = (0x40 if not bold and not italic else 0) \
        | (0x20 if bold else 0) | (0x01 if italic else 0)
    font["head"].macStyle = (0x01 if bold else 0) | (0x02 if italic else 0)
    font["post"].italicAngle = -12.0 if italic else 0.0


# ------------------------------------------------------------ diffusion trace

TRACE_DIR = os.path.join(SCRIPT_DIR, "diffusion_generations")
TRACE_OUT_DIRS = {"fonts": os.path.join(SCRIPT_DIR, "fonts"),
                  "out/traced": os.path.join(SCRIPT_DIR, "out", "traced")}
WEIGHT_STYLES = {100: "Thin", 200: "ExtraLight", 300: "Light", 400: "Regular",
                 500: "Medium", 600: "SemiBold", 700: "Bold", 800: "ExtraBold",
                 900: "Black"}
# longest first so "extra light" beats "light" etc.
WEIGHT_WORDS = [("thin", 100), ("extralight", 200), ("extra light", 200),
                ("light", 300), ("regular", 400), ("book", 400),
                ("normal", 400), ("medium", 500), ("semibold", 600),
                ("semi bold", 600), ("demibold", 600), ("bold", 700),
                ("extrabold", 800), ("extra bold", 800), ("black", 900)]
ITALIC_RE = re.compile(r"\b(italic|oblique)\b")

_trace_lock = threading.Lock()
_trace = {"running": False, "jobs": []}   # batch trace state
_thumb_cache = {}


def is_within(base, path):
    try:
        rb, rp = os.path.realpath(base), os.path.realpath(path)
        return os.path.commonpath([rb, rp]) == rb
    except ValueError:
        return False


def parse_style_hints(name):
    """Guess weight/italic from a filename ("Light Italic" -> 300, italic).

    Returns (weight, italic, weight_auto, italic_auto); weight/italic are
    None when the name gives no confident answer (missing or ambiguous).
    """
    base = os.path.splitext(name)[0].lower().replace("_", " ").replace("-", " ")
    weights = set()
    remaining = base
    for word, w in sorted(WEIGHT_WORDS, key=lambda wl: len(wl[0]),
                          reverse=True):
        m = re.search(rf"\b{re.escape(word)}\b", remaining)
        if m:
            weights.add(w)
            # consume the span so a contained shorter word ("light" inside
            # "extra light") is not counted a second time
            remaining = (remaining[:m.start()]
                         + " " * (m.end() - m.start())
                         + remaining[m.end():])
    italic = ITALIC_RE.search(base) is not None
    if len(weights) == 1:
        weight, weight_auto = weights.pop(), True
        italic_auto = True          # named weight + no italic word = not italic
    elif len(weights) > 1:
        weight, weight_auto = None, False   # ambiguous -> ask
        italic_auto = italic
    else:
        weight, weight_auto = None, False   # no hints at all -> ask
        italic_auto = italic
    return weight, italic, weight_auto, italic_auto


def list_diffusions():
    """Families = subfolders of diffusion_generations/, each with its images."""
    families = []
    for name in sorted(os.listdir(TRACE_DIR)):
        d = os.path.join(TRACE_DIR, name)
        if not os.path.isdir(d):
            continue
        imgs = sorted(glob.glob(os.path.join(d, "*.png")))
        if not imgs:
            continue
        families.append({"name": name, "dir": d,
                         "images": [diffusion_image_info(p) for p in imgs]})
    loose = sorted(glob.glob(os.path.join(TRACE_DIR, "*.png")))
    if loose:
        families.append({"name": "(unassigned)", "dir": TRACE_DIR,
                         "images": [diffusion_image_info(p) for p in loose]})
    return families


def diffusion_image_info(path):
    name = os.path.basename(path)
    weight, italic, w_auto, i_auto = parse_style_hints(name)
    return {"path": path, "name": name, "weight": weight, "italic": italic,
            "weight_auto": w_auto, "italic_auto": i_auto}


def thumbnail(path, size):
    key = (path, size)
    if key not in _thumb_cache:
        img = Image.open(path).convert("RGB")
        img.thumbnail((size, size))
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=80)
        _thumb_cache[key] = buf.getvalue()
        if len(_thumb_cache) > 200:
            _thumb_cache.clear()
    return _thumb_cache[key]


def style_for(weight, italic):
    """Subfamily name matching trace_grid's Google-Fonts-style naming."""
    base = WEIGHT_STYLES.get(weight, "Regular")
    if base == "Regular":
        return "Italic" if italic else "Regular"
    if base == "Bold":
        return "Bold Italic" if italic else "Bold"
    return f"{base} Italic" if italic else base


def build_trace_jobs(body):
    """Validate a batch trace request, derive style + output path per image."""
    family = str(body.get("family", "")).strip()
    if not family:
        raise ValueError("family name is required")
    out_dir = str(body.get("out_dir", "fonts"))
    if out_dir not in TRACE_OUT_DIRS:
        raise ValueError(f"out_dir must be one of {sorted(TRACE_OUT_DIRS)}")
    base_dir = TRACE_OUT_DIRS[out_dir]
    os.makedirs(base_dir, exist_ok=True)
    jobs_in = body.get("jobs")
    if not isinstance(jobs_in, list) or not jobs_in:
        raise ValueError("jobs must be a non-empty list of images")

    turdsize = max(0, int(body.get("turdsize", 1) or 1))
    alphamax = float(body.get("alphamax", 1.0) or 1.0)
    opttolerance = float(body.get("opttolerance", 0.1) or 0.1)
    cu2qu_err = float(body.get("cu2qu_err", 0.5) or 0.5)
    bearing_px = float(body.get("bearing_px", 2.0) or 2.0)
    bearing_frac = float(body.get("bearing_frac", 0.15) or 0.15)
    invert = bool(body.get("invert"))
    bp = body.get("baseline_px")
    baseline_px = float(bp) if bp not in (None, "") else None
    mt = body.get("mask_threshold")
    mask_threshold = int(mt) if mt not in (None, "") else None
    metrics_from = str(body.get("metrics_from") or "").strip() or None
    if metrics_from and not os.path.isfile(metrics_from):
        raise ValueError(f"metrics font not found: {metrics_from!r}")

    jobs = []
    seen = set()
    for it in jobs_in:
        img = str(it.get("image", "")).strip()
        if not os.path.isfile(img):
            raise ValueError(f"image not found: {img!r}")
        weight = it.get("weight")
        if weight is None:
            raise ValueError(f"no weight for {os.path.basename(img)!r} — "
                             "the filename carries no weight hint; assign "
                             "one first")
        weight = max(100, min(900, int(weight)))
        italic = bool(it.get("italic"))
        style = str(it.get("style") or "").strip() or style_for(weight, italic)
        out = os.path.join(
            base_dir,
            glyph_grid.sanitize_name(f"{family}-{style}".replace(" ", ""))
            + ".ttf")
        if out in seen:
            raise ValueError(f"two images would write the same font "
                             f"({os.path.basename(out)}) — assign distinct "
                             "weights/italic")
        seen.add(out)
        jobs.append({"image": img, "name": os.path.basename(img),
                     "family": family, "style": style, "weight": weight,
                     "italic": italic, "out": out, "status": "pending",
                     "message": "", "turdsize": turdsize, "alphamax": alphamax,
                     "opttolerance": opttolerance, "cu2qu_err": cu2qu_err,
                     "bearing_px": bearing_px, "bearing_frac": bearing_frac,
                     "invert": invert, "baseline_px": baseline_px,
                     "mask_threshold": mask_threshold,
                     "metrics_from": metrics_from})
    return jobs


def _run_trace_job(job):
    cmd = [sys.executable, os.path.join(SCRIPT_DIR, "trace_grid.py"),
           job["image"], "-o", job["out"], "--family", job["family"],
           "--style", job["style"], "--weight", str(job["weight"]),
           "--turdsize", str(job["turdsize"]),
           "--alphamax", str(job["alphamax"]),
           "--opttolerance", str(job["opttolerance"]),
           "--cu2qu-err", str(job["cu2qu_err"])]
    if job["italic"]:
        cmd.append("--italic")
    if job["invert"]:
        cmd.append("--invert")
    if job["bearing_px"] != 2.0:
        cmd += ["--bearing-px", str(job["bearing_px"])]
    if job["bearing_frac"] != 0.15:
        cmd += ["--bearing-frac", str(job["bearing_frac"])]
    if job["baseline_px"] is not None:
        cmd += ["--baseline-px", str(job["baseline_px"])]
    if job["mask_threshold"] is not None:
        cmd += ["--mask-threshold", str(job["mask_threshold"])]
    if job["metrics_from"]:
        cmd += ["--metrics-from", job["metrics_from"]]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    if p.returncode != 0:
        out = (p.stdout or "") + (p.stderr or "")
        lines = [l for l in out.splitlines() if l.strip()]
        raise RuntimeError(lines[-1] if lines else f"trace exited {p.returncode}")
    return (p.stdout or "").strip()


def _trace_worker(jobs):
    try:
        for j in jobs:
            j["status"] = "running"
            j["message"] = ""
            try:
                msg = _run_trace_job(j)
                lines = [l for l in msg.splitlines() if l.strip()]
                j["status"] = "done"
                j["message"] = lines[-1] if lines else "ok"
            except Exception as e:  # noqa: BLE001 - report per-job
                j["status"] = "error"
                j["message"] = str(e)
    finally:
        with _trace_lock:
            _trace["running"] = False


def trace_status():
    with _trace_lock:
        return {"running": _trace["running"],
                "jobs": [dict(j) for j in _trace["jobs"]]}


# ------------------------------------------------------------------ endpoints

def list_fonts():
    seen, out = set(), []
    for sub, pat in FONT_GLOBS:
        for p in glob.glob(os.path.join(SCRIPT_DIR, sub, pat)):
            rp = os.path.realpath(p)
            if rp in seen:
                continue
            seen.add(rp)
            out.append({"path": rp, "name": os.path.basename(p)})
    out.sort(key=lambda f: f["name"].lower())
    return out


def glyph_list(font):
    cmap = font.getBestCmap()
    rev = {}
    for cp, n in cmap.items():
        rev.setdefault(n, []).append(cp)
    glyf = font["glyf"]
    hmtx = font["hmtx"]
    glyphs = []
    for name in font.getGlyphOrder():
        if name == ".notdef":
            continue
        g = glyf[name]
        bounds = None
        if g.numberOfContours > 0:
            xs = [x for x, _ in g.coordinates]
            ys = [y for _, y in g.coordinates]
            bounds = [min(xs), min(ys), max(xs), max(ys)]
        adv, lsb = hmtx[name]
        cps = rev.get(name) or []
        glyphs.append({"name": name, "cp": cps[0] if cps else None,
                       "adv": adv, "lsb": lsb, "bounds": bounds})
    glyphs.sort(key=lambda g: (g["cp"] is None, g["cp"] or 0, g["name"]))
    return glyphs


def kernel_coverage(font):
    try:
        glyphs = glyph_grid.load_glyphs(glyph_grid.DEFAULT_GLYPHS_DIR)
    except Exception:
        return None, None
    cmap = font.getBestCmap()
    covered = sum(1 for _, cp in glyphs if cp in cmap)
    return covered, len(glyphs)


def read_json_body(handler):
    length = int(handler.headers.get("Content-Length") or 0)
    if length <= 0:
        return {}
    raw = handler.rfile.read(length)
    return json.loads(raw.decode("utf-8"))


def send_json(handler, code, payload):
    body = json.dumps(payload).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def send_bytes(handler, code, data, ctype, disposition=None):
    handler.send_response(code)
    handler.send_header("Content-Type", ctype)
    handler.send_header("Content-Length", str(len(data)))
    if disposition:
        handler.send_header("Content-Disposition", disposition)
    handler.end_headers()
    handler.wfile.write(data)


class Handler(BaseHTTPRequestHandler):
    session = Session()  # shared across threads (single-user tool)

    # ---- plumbing ------------------------------------------------------

    def log_message(self, fmt, *args):
        sys.stderr.write("[font_ui] %s\n" % (fmt % args))

    def _json_error(self, code, msg):
        send_json(self, code, {"ok": False, "error": str(msg)})

    # ---- GET -----------------------------------------------------------

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        try:
            if path == "/":
                self._serve_ui()
            elif path == "/api/fonts":
                send_json(self, 200, {"ok": True, "fonts": list_fonts()})
            elif path == "/api/font":
                self._require_font()
                covered, total = kernel_coverage(self.session.font)
                send_json(self, 200, {"ok": True,
                                      "font": font_info(self.session.font,
                                                        self.session.path),
                                      "adj": self.session.adj,
                                      "kernel": {"covered": covered,
                                                 "total": total}})
            elif path == "/api/glyphs":
                self._require_font()
                send_json(self, 200, {"ok": True,
                                      "glyphs": glyph_list(self.session.font)})
            elif path.startswith("/api/glyph/"):
                self._require_font()
                name = urllib.parse.unquote(path[len("/api/glyph/"):])
                if name not in self.session.font["glyf"].glyphs:
                    self._json_error(404, f"no glyph named {name!r}")
                    return
                outl = glyph_to_outline(self.session.font, name)
                adv, lsb = self.session.font["hmtx"][name]
                outl.update({"name": name, "adv": adv, "lsb": lsb})
                send_json(self, 200, {"ok": True, "glyph": outl})
            elif path == "/api/session":
                self._require_font()
                session = self.session
                send_json(self, 200, {"ok": True, "session": {
                    "path": session.path,
                    "adj": session.adj,
                    "identity": session.identity,
                    "edits": session.edits,
                }})
            elif path == "/api/diffusions":
                send_json(self, 200, {"ok": True,
                                      "families": list_diffusions()})
            elif path == "/api/thumb":
                qp = (query.get("path") or [None])[0]
                try:
                    size = min(1024, max(64, int((query.get("size") or [256])[0])))
                except (TypeError, ValueError):
                    size = 256
                if not qp or not is_within(TRACE_DIR, qp) \
                        or not os.path.isfile(qp):
                    self._json_error(400, "invalid image path")
                    return
                try:
                    data = thumbnail(qp, size)
                except Exception as e:  # noqa: BLE001
                    self._json_error(400, f"{type(e).__name__}: {e}")
                    return
                send_bytes(self, 200, data, "image/jpeg")
            elif path == "/api/trace/status":
                send_json(self, 200, {"ok": True, "status": trace_status()})
            elif path == "/api/fontfile":
                self._require_font()
                font = self.session.mutate(self.session.rebuild)
                buf = io.BytesIO()
                font.save(buf)
                data = buf.getvalue()
                info = font_info(font, self.session.path)
                disp = (f'attachment; filename="{info["family"]}-'
                        f'{info["subfamily"]}.ttf"'
                        if "download" in query else None)
                send_bytes(self, 200, data, "font/ttf", disp)
            else:
                self._json_error(404, f"no route {path!r}")
        except Exception as e:  # noqa: BLE001 - surface to the UI
            self._json_error(500, f"{type(e).__name__}: {e}")

    def _serve_ui(self):
        try:
            with open(UI_FILE, encoding="utf-8") as f:
                data = f.read().encode("utf-8")
        except OSError as e:
            self._json_error(500, f"missing {UI_FILE}: {e}")
            return
        send_bytes(self, 200, data, "text/html; charset=utf-8")

    def _require_font(self):
        if self.session.font is None:
            raise ValueError("no font loaded — pick one first")

    # ---- POST ----------------------------------------------------------

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        try:
            body = read_json_body(self)
            if path == "/api/font":
                p = body.get("path")
                if not p or not os.path.isfile(p):
                    self._json_error(400, f"font not found: {p!r}")
                    return
                self.session.mutate(lambda: self.session.load(p))
                covered, total = kernel_coverage(self.session.font)
                send_json(self, 200, {"ok": True,
                                      "font": font_info(self.session.font, p),
                                      "adj": self.session.adj,
                                      "kernel": {"covered": covered,
                                                 "total": total}})
            elif path == "/api/metrics":
                self._require_font()
                adj = sanitize_adj(body)
                self.session.mutate(
                    lambda: (setattr(self.session, "adj", adj),
                             self.session.rebuild()))
                send_json(self, 200, {"ok": True, "adj": self.session.adj})
            elif path == "/api/name":
                self._require_font()
                family = str(body.get("family", "")).strip() or "Font"
                sub = str(body.get("subfamily", "")).strip() or "Regular"
                weight = max(1, min(1000, int(body.get("weight", 400))))
                italic = bool(body.get("italic"))
                ident = {"family": family, "subfamily": sub,
                         "weight": weight, "italic": italic}
                self.session.mutate(
                    lambda: (setattr(self.session, "identity", ident),
                             self.session.rebuild()))
                send_json(self, 200, {"ok": True,
                                      "font": font_info(
                                          self.session.font,
                                          self.session.path)})
            elif path == "/api/trace/preflight":
                jobs = build_trace_jobs(body)
                existing = [os.path.basename(j["out"]) for j in jobs
                            if os.path.exists(j["out"])]
                send_json(self, 200, {"ok": True, "existing": existing})
            elif path == "/api/trace":
                with _trace_lock:
                    if _trace["running"]:
                        self._json_error(409, "a trace batch is already "
                                             "running")
                        return
                jobs = build_trace_jobs(body)
                with _trace_lock:
                    _trace["running"] = True
                    _trace["jobs"] = jobs
                threading.Thread(target=_trace_worker, args=(jobs,),
                                 daemon=True).start()
                send_json(self, 200, {"ok": True, "status": trace_status()})
            elif path == "/api/export":
                self._require_font()
                base = re.sub(r"[^A-Za-z0-9_.-]+", "_",
                              str(body.get("filename", "font.ttf"))).strip("_")
                if not base.lower().endswith(".ttf"):
                    base += ".ttf"
                os.makedirs(OUT_DIR, exist_ok=True)
                out_path = os.path.join(OUT_DIR, base)
                font = self.session.mutate(self.session.rebuild)
                font.save(out_path)
                send_json(self, 200, {"ok": True, "path": out_path})
            elif path.startswith("/api/glyph/"):
                self._require_font()
                name = urllib.parse.unquote(path[len("/api/glyph/"):])
                if name not in self.session.font["glyf"].glyphs:
                    self._json_error(404, f"no glyph named {name!r}")
                    return
                contours = check_contours(body.get("contours"))
                self.session.mutate(
                    lambda: (self.session.edits.__setitem__(
                        name, {"contours": contours}),
                        self.session.rebuild()))
                send_json(self, 200, {"ok": True, "adj": self.session.adj})
            else:
                self._json_error(404, f"no route {path!r}")
        except Exception as e:  # noqa: BLE001
            self._json_error(400 if isinstance(e, ValueError) else 500,
                             f"{type(e).__name__}: {e}")

    # needed so the shared session works in threaded mode
    def handle(self):
        self.close_connection = False
        BaseHTTPRequestHandler.handle(self)


def sanitize_adj(body):
    """Coerce a client adjustments dict to the tune_metrics schema."""
    if not isinstance(body, dict):
        raise ValueError("adjustments must be a JSON object")
    adj = {}
    for k in ADJ_INT_KEYS:
        if k in body and body[k] is not None:
            adj[k] = int(body[k])
    for k in ADJ_FLOAT_KEYS:
        if k in body and body[k] is not None:
            adj[k] = float(body[k])
    glyphs = body.get("glyphs")
    if glyphs:
        if not isinstance(glyphs, dict):
            raise ValueError("glyphs must be an object of name -> overrides")
        g = {}
        for name, pg in glyphs.items():
            if not isinstance(pg, dict):
                continue
            ov = {k: int(pg[k]) for k in GLYPH_KEYS
                  if k in pg and pg[k] is not None}
            if ov:
                g[str(name)] = ov
        if g:
            adj["glyphs"] = g
    kern = body.get("kerning")
    if isinstance(kern, dict):
        adj["kerning"] = {str(k): int(v) for k, v in kern.items()}
    return adj


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--no-open", action="store_true",
                    help="don't open the browser automatically")
    args = ap.parse_args()

    if not os.path.isfile(UI_FILE):
        raise SystemExit(f"missing UI file: {UI_FILE}")

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}/"
    print(f"font_ui serving {url}  (Ctrl-C to stop)")
    if not args.no_open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
