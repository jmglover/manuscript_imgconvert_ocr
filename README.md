# Manuscript OCR

Convert a folder of HEIC manuscript photos into ordinary image files, OCR them
into a collated searchable text file, and produce a compact searchable PDF (page
images with an invisible, selectable text layer).

The work is split into two stages so you can run them independently:

1. **convert** — turn the HEIC photos into JPEGs (fast, free, done once).
2. **ocr** — run OCR over those images (the slow / costly / experiment-y part).

Because the converted images are written to disk, you can convert once and then
OCR as many times as you like — Tesseract first, Google Vision later — without
redoing the conversion. OCR outputs are **named after the engine**, so repeated
runs sit side by side instead of overwriting each other.

## What you get

After `convert` you have:

- `<output>/images/` — your individual page images (JPEG by default), in a
  widely supported, easily browsable format

After each `ocr` run (here with the default `tesseract` engine) you also have:

- `<output>/transcription_tesseract.txt` — all recognised text, collated, one header per page
- `<output>/searchable_tesseract.pdf` — every page image with a hidden, searchable text layer

Run `ocr` again with `--engine gvision` and you get `transcription_gvision.txt`
and `searchable_gvision.pdf` alongside the Tesseract ones, ready to compare.

## Image format and storage

By default `convert` writes **colour JPEGs at quality 92**. This is the sensible
choice for keeping a usable image archive: JPEG opens everywhere, the files are a
fraction of the size of a lossless PNG, and at quality 92 the loss is invisible
for viewing and reading. Camera orientation, EXIF and colour profile are
preserved.

Options if you want something different:

```bash
--format jpeg --quality 92   # default
--format jpeg --quality 85   # smaller files, still very legible
--format png                 # lossless, but several times larger
--format webp --quality 90   # smaller than JPEG, slightly less universal
```

A note on "lossless": a photograph of paper is full of sensor noise and texture,
which lossless formats must store exactly, so PNG/lossless-WebP files stay large
(often ~10 MB/page) and re-optimising them barely helps. If you want a truly
lossless original, your **HEIC files already are one** (and are smaller than the
PNGs) — keep that folder. The JPEGs here are the convenient working/viewing copy.

## Setup

1. Install the **Tesseract engine** (a system program, not a pip package):
   - macOS: `brew install tesseract`
   - Ubuntu/Debian: `sudo apt-get install tesseract-ocr`
   - Windows: https://github.com/UB-Mannheim/tesseract/wiki

2. Install the Python dependencies (a virtual environment is recommended):

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate        # Windows: .venv\Scripts\activate
   pip install -r requirements.txt
   ```

## Usage

The tool has three subcommands: `convert`, `ocr`, and `all`. Run
`python manuscript_ocr.py <command> --help` for the full option list of each.

### The staged workflow (recommended)

```bash
# Stage 1 — convert once. JPEGs land in ./out/images/
python manuscript_ocr.py convert ./my_heic_folder -o ./out

# Stage 2 — OCR at your leisure. Point at ./out (it finds the images/ subfolder),
# or directly at ./out/images. Outputs are written into ./out.
python manuscript_ocr.py ocr ./out --engine tesseract

# Later, try a different engine on the SAME images — no reconversion:
python manuscript_ocr.py ocr ./out --engine gvision
```

You now have `transcription_tesseract.txt` / `searchable_tesseract.pdf` and
`transcription_gvision.txt` / `searchable_gvision.pdf` side by side in `./out`.

### One pass

```bash
python manuscript_ocr.py all ./my_heic_folder -o ./out --engine tesseract
```

This converts and then OCRs, producing the same engine-tagged outputs.

### Common options

```bash
# More parallel workers (good for a large batch on a multi-core machine)
python manuscript_ocr.py convert ./my_heic_folder -o ./out --workers 8

# Skip the PDF (just the text transcript)
python manuscript_ocr.py ocr ./out --no-pdf
```

`--workers` is available on every subcommand. `--format` / `--quality` apply to
`convert` and `all`; the OCR and PDF options below apply to `ocr` and `all`.

### Controlling PDF size

The PDF embeds each page as a downscaled JPEG. For ~250 photos this is roughly
100–150 MB rather than the ~1 GB a lossless PDF would be. Tune it on the `ocr`
(or `all`) command:

```bash
# Smaller file (more aggressive downscale + compression)
python manuscript_ocr.py ocr ./out --pdf-max-px 1500 --pdf-quality 70

# Crisper page images (for close inspection)
python manuscript_ocr.py ocr ./out --pdf-max-px 2500 --pdf-quality 90
```

`--pdf-max-px` caps the longer edge in pixels (`0` disables downscaling);
`--pdf-quality` is JPEG quality 1–95. Neither affects the text layer or the
images in `images/`. (This is separate from the `--quality` that controls your
stored images.)

### Handwriting

Tesseract (the default) is excellent on typescripts but weak on cursive hand.
For handwritten material use Google Cloud Vision. The staged workflow shines
here: convert once, then run `gvision` only on the pages that need it.

```bash
pip install google-cloud-vision

# Authenticate with your own Google account (keyless — recommended for a
# tool on your own machine; no service-account key needed):
gcloud auth application-default login
gcloud auth application-default set-quota-project YOUR_PROJECT_ID

python manuscript_ocr.py ocr ./out --engine gvision
```

`gcloud` is the **Google Cloud CLI**, a separate program you install once (macOS:
`brew install --cask google-cloud-sdk`); it is unrelated to the
`google-cloud-vision` Python package. You'll also need a Google Cloud project
with the Cloud Vision API enabled. If you've been told to create a service-account
key or set up Workload Identity Federation: you need neither for local use — the
`application-default login` above is the simpler, Google-recommended path.

See the comment block at the bottom of `manuscript_ocr.py` for the full Google
Cloud setup, per-image billing, and the privacy implication (images are uploaded
to Google). Tip: run a handful of representative pages through `gvision` and
check the transcript before committing the whole archive.

### Other useful flags (ocr / all)

```
--lang eng+fra      OCR language(s); e.g. eng, fra, deu, or combined
--recursive, -r     also process images in subfolders
--threshold         binarise before OCR (can help clean scans, hurts photos)
--upscale-min N     upscale images whose shorter side is below N px (0 disables)
--psm 6             Tesseract page-segmentation mode (default 3 = auto)
```

Run `python manuscript_ocr.py <command> --help` for the complete list.
