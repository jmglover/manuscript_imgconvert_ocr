#!/usr/bin/env python3
"""
manuscript_ocr.py — Convert a folder of HEIC manuscript photos to PNG, OCR them
into a single searchable text file, and optionally produce a searchable PDF with
an invisible text layer over each page image.

What it does
------------
The work is split into two stages you can run separately or together:

  convert   Stage 1: HEIC/HEIF -> PNG, written to <output>/png/.
  ocr       Stage 2: OCR a folder of PNGs into:
              <output>/transcription_<engine>.txt   (collated, one header per page)
              <output>/searchable_<engine>.pdf      (page images + invisible text)
  all       Run convert then ocr in a single pass.

Outputs are tagged with the engine name, so you can OCR the same PNGs twice
(e.g. tesseract first, gvision later) without overwriting the earlier results.

OCR engines
-----------
  tesseract  (default)  Local, free. Excellent on typed/printed text, weak on
                        cursive handwriting.
  gvision               Google Cloud Vision DOCUMENT_TEXT_DETECTION. Strong on
                        handwriting. Needs a Google Cloud account + credentials
                        (see notes at the bottom of this file).
  easyocr               Local deep-learning OCR; a free middle ground for some
                        hands. Downloads model weights on first run.

Quick start
-----------
    pip install pillow-heif Pillow pytesseract reportlab
    # engine itself: brew install tesseract  /  apt-get install tesseract-ocr

    # One pass, convert + OCR:
    python manuscript_ocr.py all ./heic_folder

    # Or split it: convert once, then OCR at your leisure / with each engine:
    python manuscript_ocr.py convert ./heic_folder -o ./out
    python manuscript_ocr.py ocr ./out --engine tesseract
    python manuscript_ocr.py ocr ./out --engine gvision     # reuses the same PNGs

    python manuscript_ocr.py ocr ./out --no-pdf             # skip the PDF
    python manuscript_ocr.py <command> --help               # all options
"""
from __future__ import annotations

import argparse
import io
import os
import re
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageOps
import pillow_heif

pillow_heif.register_heif_opener()

HEIC_SUFFIXES = {".heic", ".heif", ".hif"}
PNG_SUFFIXES = {".png"}

# A "word" is (text, x0, y0, x1, y1) with the box in pixel coords of the
# preprocessed image that was OCR'd. These drive the PDF text layer.


# --------------------------------------------------------------------------- #
# Filename ordering / discovery
# --------------------------------------------------------------------------- #
def natural_key(path: Path):
    return [int(t) if t.isdigit() else t.lower()
            for t in re.split(r"(\d+)", path.name)]


def gather(src: Path, suffixes: set[str], recursive: bool) -> list[Path]:
    pattern = "**/*" if recursive else "*"
    files = [p for p in src.glob(pattern) if p.suffix.lower() in suffixes]
    return sorted(files, key=natural_key)


def resolve_png_dir(d: Path) -> Path:
    """Accept either a folder of PNGs or an output folder containing a png/ subdir.

    Lets `ocr ./out` work after `convert ... -o ./out` produced ./out/png, as well
    as `ocr ./out/png` directly.
    """
    if any(p.suffix.lower() == ".png" for p in d.glob("*.png")):
        return d
    sub = d / "png"
    if sub.is_dir() and any(sub.glob("*.png")):
        return sub
    return d  # leave as-is; caller reports "no PNGs found"


# --------------------------------------------------------------------------- #
# Conversion + preprocessing
# --------------------------------------------------------------------------- #
def convert_to_png(src: Path, dst: Path) -> None:
    with Image.open(src) as img:
        dst.parent.mkdir(parents=True, exist_ok=True)
        kwargs = {}
        if icc := img.info.get("icc_profile"):
            kwargs["icc_profile"] = icc
        if exif := img.info.get("exif"):
            kwargs["exif"] = exif
        img.save(dst, format="PNG", **kwargs)


def preprocess(img: Image.Image, threshold: bool, upscale_min: int) -> Image.Image:
    """Deterministic: grayscale + autocontrast, optional gentle upscale + binarise.

    Determinism matters because the PDF builder re-runs this to recreate the exact
    image the OCR boxes refer to, so the invisible text layer lines up.
    """
    img = ImageOps.exif_transpose(img)
    img = img.convert("L")
    img = ImageOps.autocontrast(img)
    if upscale_min and min(img.size) < upscale_min:
        scale = upscale_min / min(img.size)
        img = img.resize((round(img.width * scale), round(img.height * scale)), Image.LANCZOS)
    if threshold:
        img = img.point(lambda p: 255 if p > 150 else 0).convert("L")
    return img


# --------------------------------------------------------------------------- #
# OCR engines — each returns (full_text, words)
# --------------------------------------------------------------------------- #
def ocr_tesseract(img: Image.Image, lang: str, psm: int):
    import pytesseract
    from pytesseract import Output
    config = f"--oem 1 --psm {psm}"
    text = pytesseract.image_to_string(img, lang=lang, config=config)
    data = pytesseract.image_to_data(img, lang=lang, config=config, output_type=Output.DICT)
    words = []
    for i, tok in enumerate(data["text"]):
        tok = (tok or "").strip()
        if not tok:
            continue
        try:
            if float(data["conf"][i]) < 0:
                continue
        except (ValueError, TypeError):
            pass
        x, y, w, h = data["left"][i], data["top"][i], data["width"][i], data["height"][i]
        words.append((tok, x, y, x + w, y + h))
    return text, words


_GVISION_CLIENT = None


def ocr_gvision(img: Image.Image, lang: str, psm: int):
    """Google Cloud Vision. Credentials via GOOGLE_APPLICATION_CREDENTIALS
    (path to a service-account JSON) — the client picks it up automatically."""
    global _GVISION_CLIENT
    from google.cloud import vision
    if _GVISION_CLIENT is None:
        _GVISION_CLIENT = vision.ImageAnnotatorClient()
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG")
    image = vision.Image(content=buf.getvalue())
    # Language hints are optional but improve accuracy; map our codes loosely.
    ctx = vision.ImageContext(language_hints=[_gvision_lang(l) for l in lang.split("+")])
    resp = _GVISION_CLIENT.document_text_detection(image=image, image_context=ctx)
    if resp.error.message:
        raise RuntimeError(resp.error.message)
    fta = resp.full_text_annotation
    words = []
    for page in fta.pages:
        for block in page.blocks:
            for para in block.paragraphs:
                for word in para.words:
                    txt = "".join(s.text for s in word.symbols)
                    xs = [v.x for v in word.bounding_box.vertices]
                    ys = [v.y for v in word.bounding_box.vertices]
                    if xs and ys:
                        words.append((txt, min(xs), min(ys), max(xs), max(ys)))
    return fta.text, words


def _gvision_lang(tess_lang: str) -> str:
    return {"eng": "en", "fra": "fr", "deu": "de", "spa": "es", "ita": "it"}.get(tess_lang, tess_lang)


_EASYOCR_READER = None


def ocr_easyocr(img: Image.Image, lang: str, psm: int):
    """Optional. pip install easyocr (downloads weights on first run)."""
    global _EASYOCR_READER
    import numpy as np
    import easyocr
    if _EASYOCR_READER is None:
        langs = [_easyocr_lang(l) for l in lang.split("+")]
        _EASYOCR_READER = easyocr.Reader(langs, gpu=False)
    results = _EASYOCR_READER.readtext(np.array(img.convert("RGB")), detail=1, paragraph=False)
    words, lines = [], []
    for box, txt, _conf in results:
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        words.append((txt, int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys))))
        lines.append(txt)
    return "\n".join(lines), words


def _easyocr_lang(tess_lang: str) -> str:
    return {"eng": "en", "fra": "fr", "deu": "de", "spa": "es", "ita": "it"}.get(tess_lang, "en")


ENGINES = {"tesseract": ocr_tesseract, "gvision": ocr_gvision, "easyocr": ocr_easyocr}


# --------------------------------------------------------------------------- #
# Per-image workers (module-level so they are picklable for multiprocessing)
# --------------------------------------------------------------------------- #
def convert_one(args):
    """Stage 1: HEIC -> PNG. Returns (png_name, error_or_None)."""
    src, png_path = args
    try:
        convert_to_png(src, png_path)
        return (png_path.name, None)
    except Exception as e:  # noqa: BLE001
        return (png_path.name, f"{type(e).__name__}: {e}")


def ocr_one(args):
    """Stage 2: OCR one PNG. Returns (png_path_str, text, words, error_or_None)."""
    png_path, engine, lang, psm, threshold, upscale_min = args
    try:
        with Image.open(png_path) as img:
            prepared = preprocess(img, threshold, upscale_min)
            text, words = ENGINES[engine](prepared, lang, psm)
        return (str(png_path), text.strip(), words, None)
    except Exception as e:  # noqa: BLE001
        return (str(png_path), "", [], f"{type(e).__name__}: {e}")


def imap(worker, tasks, workers):
    """Yield worker(task) for each task, in parallel when workers > 1.

    Falls back to sequential if a process pool can't be started. Callers collect
    results into a dict keyed by filename, so an occasional re-run is harmless.
    """
    if workers > 1:
        try:
            with ProcessPoolExecutor(max_workers=workers) as ex:
                for fut in as_completed([ex.submit(worker, t) for t in tasks]):
                    yield fut.result()
            return
        except Exception as e:  # noqa: BLE001
            print(f"Parallel execution failed ({e}); running sequentially.", file=sys.stderr)
    for t in tasks:
        yield worker(t)


# --------------------------------------------------------------------------- #
# Searchable PDF (invisible text layer over each page image)
# --------------------------------------------------------------------------- #
def build_searchable_pdf(pages, out_path, threshold, upscale_min, dpi,
                         max_px, quality):
    """pages: list of (png_path, words). One multi-page PDF, image + hidden text.

    The embedded page image is downscaled (to `max_px` on its longer edge) and
    JPEG-compressed (`quality`) to keep the file small. This does NOT affect the
    text layer: page geometry and word boxes are computed from the OCR image's
    dimensions and expressed in PDF points, independent of how many pixels the
    embedded picture actually has. Your full-resolution PNGs in png/ are untouched.
    """
    from reportlab.pdfgen import canvas
    from reportlab.lib.utils import ImageReader

    s = 72.0 / dpi  # pixels -> points
    c = canvas.Canvas(str(out_path))
    for png_path, words in pages:
        with Image.open(png_path) as img:
            prepared = preprocess(img, threshold, upscale_min).convert("L")
            w, h = prepared.size           # geometry / box coordinate space
            pw, ph = w * s, h * s
            c.setPageSize((pw, ph))

            # Build a compact image to embed (visual only).
            embed = prepared
            if max_px and max(w, h) > max_px:
                f = max_px / max(w, h)
                embed = prepared.resize((max(1, round(w * f)), max(1, round(h * f))),
                                        Image.LANCZOS)
            buf = io.BytesIO()
            embed.save(buf, format="JPEG", quality=quality, optimize=True)
            buf.seek(0)
            # ImageReader on JPEG bytes -> reportlab embeds via DCTDecode (small).
            c.drawImage(ImageReader(buf), 0, 0, width=pw, height=ph)

            to = c.beginText()
            to.setTextRenderMode(3)  # invisible
            for (txt, x0, y0, x1, y1) in words:
                if not txt.strip():
                    continue
                box_w = max(1.0, (x1 - x0) * s)
                box_h = max(1.0, (y1 - y0) * s)
                font_size = box_h
                to.setFont("Helvetica", font_size)
                tw = c.stringWidth(txt, "Helvetica", font_size) or 1.0
                to.setHorizScale(100.0 * box_w / tw)  # stretch to box width
                to.setTextOrigin(x0 * s, ph - y1 * s)  # flip Y to PDF origin
                to.textLine(txt)
                to.setHorizScale(100.0)
            c.drawText(to)
        c.showPage()
    c.save()


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def preflight(engine: str) -> str | None:
    if engine == "gvision":
        try:
            import google.auth
        except ImportError:
            return ("engine 'gvision' needs the google-cloud-vision package:\n"
                    "    pip install google-cloud-vision")
        try:
            # Finds Application Default Credentials however they're provided:
            # a gcloud login, the GOOGLE_APPLICATION_CREDENTIALS env var, or an
            # attached service account. No key file required.
            google.auth.default()
        except Exception:
            return (
                "engine 'gvision' can't find Google Cloud credentials. On your own\n"
                "machine the simplest, keyless way is your own Google account:\n"
                "    gcloud auth application-default login\n"
                "    gcloud auth application-default set-quota-project YOUR_PROJECT_ID\n"
                "No service-account key or Workload Identity Federation needed. See the\n"
                "notes at the bottom of manuscript_ocr.py.")
    return None


# --------------------------------------------------------------------------- #
# Stage 1: convert
# --------------------------------------------------------------------------- #
def run_convert(input_dir, output_dir, recursive, workers):
    """HEIC -> PNG only. Returns (png_dir, n_errors)."""
    files = gather(input_dir, HEIC_SUFFIXES, recursive)
    if not files:
        print(f"No HEIC/HEIF files found in {input_dir}", file=sys.stderr)
        return (None, 0)

    png_dir = output_dir / "png"
    png_dir.mkdir(parents=True, exist_ok=True)
    tasks = [(src, png_dir / (src.stem + ".png")) for src in files]

    print(f"[convert] {len(files)} image(s) -> {png_dir}  (workers: {workers})")
    results = {}
    for name, err in imap(convert_one, tasks, workers):
        results[name] = err
        print(f"  [{len(results)}/{len(files)}] {'OK ' if err is None else 'ERR'} {name}"
              + (f"  ({err})" if err else ""))

    errors = sum(1 for e in results.values() if e)
    print(f"[convert] done: {len(results) - errors} converted, {errors} failed.")
    return (png_dir, errors)


# --------------------------------------------------------------------------- #
# Stage 2: ocr
# --------------------------------------------------------------------------- #
def run_ocr(png_input, output_dir, engine, lang, psm, recursive,
            threshold, upscale_min, workers, make_pdf, pdf_dpi, pdf_max_px, pdf_quality):
    """OCR a folder of PNGs. Outputs are engine-tagged so repeated runs with
    different engines don't overwrite each other."""
    if msg := preflight(engine):
        print(f"error: {msg}", file=sys.stderr)
        return 1

    png_dir = resolve_png_dir(png_input)
    if output_dir is None:
        output_dir = png_dir.parent if png_dir.name == "png" else png_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    files = gather(png_dir, PNG_SUFFIXES, recursive)
    if not files:
        print(f"No PNG files found in {png_dir}. Run the 'convert' stage first, or "
              f"point at the folder that contains them.", file=sys.stderr)
        return 1

    tasks = [(p, engine, lang, psm, threshold, upscale_min) for p in files]
    print(f"[ocr] {len(files)} image(s) from {png_dir}  (engine: {engine}, workers: {workers})")
    results = {}
    for path_str, text, words, err in imap(ocr_one, tasks, workers):
        results[path_str] = (text, words, err)
        print(f"  [{len(results)}/{len(files)}] {'OK ' if err is None else 'ERR'} "
              f"{Path(path_str).name}" + (f"  ({err})" if err else ""))

    ordered = sorted(results.items(), key=lambda kv: natural_key(Path(kv[0])))
    errors = sum(1 for _, (_, _, err) in ordered if err)

    # Engine-tagged transcript
    transcript_path = output_dir / f"transcription_{engine}.txt"
    with transcript_path.open("w", encoding="utf-8") as f:
        f.write("OCR transcription\n")
        f.write(f"Generated:        {datetime.now().isoformat(timespec='seconds')}\n")
        f.write(f"PNG directory:    {png_dir.resolve()}\n")
        f.write(f"Engine / lang:    {engine} / {lang}\n")
        f.write(f"Images processed: {len(ordered)}  (errors: {errors})\n\n")
        for path_str, (text, _words, err) in ordered:
            label = Path(path_str).name
            f.write("=" * 70 + f"\nFILE: {label}\n" + "=" * 70 + "\n\n")
            f.write((f"[OCR FAILED: {err}]" if err else (text or "[no text detected]")) + "\n\n")
    print(f"[ocr] transcript: {transcript_path}")

    # Engine-tagged searchable PDF
    if make_pdf:
        pdf_path = output_dir / f"searchable_{engine}.pdf"
        try:
            pages = [(Path(path_str), words)
                     for path_str, (_t, words, err) in ordered if err is None]
            build_searchable_pdf(pages, pdf_path, threshold, upscale_min, pdf_dpi,
                                  pdf_max_px, pdf_quality)
            mb = pdf_path.stat().st_size / 1_000_000
            print(f"[ocr] searchable PDF: {pdf_path}  ({mb:.1f} MB, {len(pages)} pages)")
        except ImportError:
            print("PDF skipped: reportlab not installed (pip install reportlab).", file=sys.stderr)
        except Exception as e:
            print(f"PDF generation failed: {e}", file=sys.stderr)

    if errors:
        print(f"[ocr] {errors} image(s) failed — see transcript.", file=sys.stderr)
    return 1 if errors == len(ordered) else 0


# --------------------------------------------------------------------------- #
# Combined: all (convert then ocr, one shot)
# --------------------------------------------------------------------------- #
def run_all(input_dir, output_dir, engine, lang, psm, recursive,
            threshold, upscale_min, workers, make_pdf, pdf_dpi, pdf_max_px, pdf_quality):
    # Check credentials before doing the conversion work, so a gvision auth
    # problem fails fast rather than after converting hundreds of images.
    if msg := preflight(engine):
        print(f"error: {msg}", file=sys.stderr)
        return 1
    png_dir, _conv_err = run_convert(input_dir, output_dir, recursive, workers)
    if png_dir is None:
        return 1
    return run_ocr(png_dir, output_dir, engine, lang, psm, False,
                   threshold, upscale_min, workers, make_pdf, pdf_dpi, pdf_max_px, pdf_quality)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _add_ocr_args(p):
    p.add_argument("--engine", choices=sorted(ENGINES), default="tesseract",
                   help="OCR engine (default tesseract)")
    p.add_argument("--lang", default="eng", help="e.g. eng, fra, 'eng+fra'")
    p.add_argument("--psm", type=int, default=3, help="Tesseract page-seg mode (default 3)")
    p.add_argument("--threshold", action="store_true",
                   help="Binarise before OCR (helps clean scans, hurts photos)")
    p.add_argument("--upscale-min", type=int, default=1000,
                   help="Upscale images whose shorter side is below this (0 disables)")


def _add_pdf_args(p):
    p.add_argument("--pdf", dest="pdf", action="store_true", default=True,
                   help="Build the searchable PDF (default on)")
    p.add_argument("--no-pdf", dest="pdf", action="store_false")
    p.add_argument("--pdf-dpi", type=int, default=150,
                   help="Assumed image DPI for PDF page sizing (default 150)")
    p.add_argument("--pdf-max-px", type=int, default=2000,
                   help="Max longer-edge px of embedded PDF page images (0 = no downscale)")
    p.add_argument("--pdf-quality", type=int, default=80,
                   help="JPEG quality for embedded PDF page images, 1-95 (default 80)")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="HEIC manuscripts -> PNG + searchable text/PDF. Run stages "
                    "separately (convert, then ocr) or together (all).")
    sub = ap.add_subparsers(dest="command", required=True)

    # convert
    c = sub.add_parser("convert", help="Stage 1: HEIC -> PNG only")
    c.add_argument("input", type=Path, help="Directory of HEIC/HEIF images")
    c.add_argument("-o", "--output", type=Path, default=Path("manuscript_output"),
                   help="Output dir; PNGs go in <output>/png/ (default ./manuscript_output)")
    c.add_argument("-r", "--recursive", action="store_true")
    c.add_argument("--workers", type=int, default=4)

    # ocr
    o = sub.add_parser("ocr", help="Stage 2: OCR a folder of PNGs (engine-tagged output)")
    o.add_argument("input", type=Path,
                   help="Folder of PNGs, or an output folder containing a png/ subdir")
    o.add_argument("-o", "--output", type=Path, default=None,
                   help="Where to write transcription_<engine>.txt / searchable_<engine>.pdf "
                        "(default: alongside the PNGs)")
    o.add_argument("-r", "--recursive", action="store_true")
    o.add_argument("--workers", type=int, default=4)
    _add_ocr_args(o)
    _add_pdf_args(o)

    # all
    a = sub.add_parser("all", help="Convert then OCR in one pass")
    a.add_argument("input", type=Path, help="Directory of HEIC/HEIF images")
    a.add_argument("-o", "--output", type=Path, default=Path("manuscript_output"),
                   help="Output dir (default ./manuscript_output)")
    a.add_argument("-r", "--recursive", action="store_true")
    a.add_argument("--workers", type=int, default=4)
    _add_ocr_args(a)
    _add_pdf_args(a)

    args = ap.parse_args(argv)
    if not args.input.is_dir():
        print(f"error: {args.input} is not a directory", file=sys.stderr)
        return 1
    workers = max(1, args.workers)

    if args.command == "convert":
        _png_dir, errors = run_convert(args.input, args.output, args.recursive, workers)
        return 1 if (_png_dir is None or errors) else 0

    if args.command == "ocr":
        return run_ocr(args.input, args.output, args.engine, args.lang, args.psm,
                       args.recursive, args.threshold, args.upscale_min, workers,
                       args.pdf, args.pdf_dpi, args.pdf_max_px, args.pdf_quality)

    # all
    return run_all(args.input, args.output, args.engine, args.lang, args.psm,
                   args.recursive, args.threshold, args.upscale_min, workers,
                   args.pdf, args.pdf_dpi, args.pdf_max_px, args.pdf_quality)


if __name__ == "__main__":
    raise SystemExit(main())

# --------------------------------------------------------------------------- #
# Google Cloud Vision setup (engine 'gvision')
# --------------------------------------------------------------------------- #
#   1. Create a Google Cloud project and enable the Cloud Vision API.
#   2. pip install google-cloud-vision
#   3. Authenticate. On your own machine the recommended, keyless way is to use
#      your own Google account (no service-account key, no Workload Identity
#      Federation):
#          gcloud auth application-default login
#          gcloud auth application-default set-quota-project YOUR_PROJECT_ID
#      The client library then finds these credentials automatically. Do NOT set
#      GOOGLE_APPLICATION_CREDENTIALS when using this method.
#   4. python manuscript_ocr.py ./folder --engine gvision
#
#   The set-quota-project step chooses which project is billed; your account
#   needs the Editor or Owner role on it (which it has if you created it).
#
#   Pricing: billed per image; the first ~1,000 images/month are typically free.
#   Vision uploads images to Google's servers — check that's acceptable for your
#   material before using it on unpublished or sensitive archives.
#
#   (Only if a key is unavoidable, e.g. a headless server with no logged-in user,
#   you can still set GOOGLE_APPLICATION_CREDENTIALS to a service-account JSON;
#   the code path supports it. Google discourages downloadable keys, so prefer
#   the login above where you can.)
#
# Non-Latin scripts: the PDF text layer uses Helvetica (Latin). For Greek,
# Cyrillic, etc., register an embedded TTF with reportlab's pdfmetrics and use
# it in build_searchable_pdf so the hidden glyphs copy/search correctly.
