# Local OCR and Text-to-Speech Pipeline

This repository provides a deterministic, resumable command-line workflow that
turns plain text or ordered page images into one high-quality MP3.

It is designed for Apple Silicon Macs and uses:

- [Apple Vision](https://developer.apple.com/documentation/vision) for local OCR.
- [MLX-Audio](https://github.com/Blaizzy/mlx-audio) for local speech synthesis.
- [Kokoro](https://huggingface.co/hexgrad/Kokoro-82M) as the default voice model.
- [Chatterbox](https://github.com/resemble-ai/chatterbox) for optional voice cloning.
- [FFmpeg](https://ffmpeg.org/) and `libmp3lame` for normalization and MP3 encoding.

No LLM, local web server, or background daemon is required. After model weights
have been downloaded, the OCR and speech pipeline can run offline.

## Features

- Accepts a text file, one image, or a folder of ordered images.
- Preserves byte-for-byte copies of the original source material.
- Saves per-page OCR results and the exact text sent to the speech model.
- Sorts image folders naturally: `page2.jpg` precedes `page10.jpg`.
- Checkpoints OCR page-by-page and speech chunk-by-chunk.
- Uses an atomic JSON ledger with SHA-256 artifact verification.
- Resumes interrupted projects without repeating completed work.
- Produces one normalized MP3 as the final audio output.
- Deletes generated working WAV files after the MP3 is successfully verified.
- Refuses to continue if a preserved project source has been modified.

## Requirements

- macOS on Apple Silicon.
- Python 3.11 through 3.13; Python 3.11 is recommended.
- Xcode Command Line Tools for the Apple Vision OCR bridge.
- FFmpeg compiled with `libmp3lame`.
- Internet access during package installation and the first model download.
- Sufficient free disk space for model weights and temporary PCM audio.

This project was developed and tested on an M1 Max with 64 GB of unified memory.
Kokoro is small enough for considerably less capable Apple Silicon systems.

## Installation

Run these commands from the repository root:

```sh
cd /path/to/text_to_speech

/Library/Frameworks/Python.framework/Versions/3.11/bin/python3.11 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e '.[local]'
```

Kokoro requires Misaki for grapheme-to-phoneme processing. Install the complete
English dependency set and the small English spaCy model:

```sh
.venv/bin/python -m pip install "misaki[en]==0.9.4"
.venv/bin/python -m spacy download en_core_web_sm
```

Install FFmpeg if it is not already available:

```sh
brew install ffmpeg
```

Apple Vision is built into macOS. If `xcrun` is unavailable, install Apple's
command-line tools:

```sh
xcode-select --install
```

Verify the installation:

```sh
.venv/bin/tts-project --doctor
.venv/bin/python -c "import misaki.en, misaki.espeak; print('Kokoro text processing: OK')"
```

The source-tree launcher can also be used without installing the CLI entry point:

```sh
.venv/bin/python tts_project.py --doctor
```

## Quick start

Create a project from a text file:

```sh
.venv/bin/tts-project manuscript.txt --project-name my-book
```

Create a project from an ordered folder of scanned pages:

```sh
.venv/bin/tts-project scans/ --project-name scanned-book
```

Create a project from one image:

```sh
.venv/bin/tts-project page01.png --project-name single-page
```

The first Kokoro run downloads `mlx-community/Kokoro-82M-bf16` from Hugging
Face. Later projects reuse the cached model.

The completed MP3 will be written to:

```text
projects/<project_name>/audio/<project_name>.mp3
```

Running the command without arguments prints the complete CLI help, local setup
instructions, and dependency status:

```sh
.venv/bin/tts-project
```

## Supported inputs

| Input | Supported forms | Behavior |
|---|---|---|
| Text | `.txt`, `.text`, `.md`, `.rst`, `.csv` | Decoded as UTF-8, UTF-16, or UTF-32 |
| Image | `.bmp`, `.gif`, `.heic`, `.jpeg`, `.jpg`, `.png`, `.tif`, `.tiff`, `.webp` | OCR produces one page text file |
| Image folder | Direct child images using the extensions above | Images are naturally sorted and processed in order |
| Existing project | Directory containing `project.json` and `ledger.json` | Work resumes from verified checkpoints |

Image-folder discovery is intentionally non-recursive. PDFs and word-processing
documents are not currently accepted directly; convert them to plain text or page
images first.

## Pipeline

| Stage | Result | Resume behavior |
|---|---|---|
| Ingest | Original inputs copied into `source/` | Copied files are hash-verified |
| Extract | Decoded text or one OCR text file per image | Completed OCR pages are skipped |
| Normalize | Unicode and whitespace cleanup | Reused while its input hash matches |
| Chunk | Sentence-aware TTS chunks | Exact chunk text is preserved |
| Render | One temporary PCM WAV per chunk | Completed chunks survive interruptions |
| Assemble | Combined, normalized MP3 | Rebuilt only when required |
| Cleanup | Generated WAV files removed | Runs only after MP3 verification |

FFmpeg normalizes the assembled result toward `-18 LUFS` and encodes mono MP3
audio at `128k` by default.

## Project structure

A completed project has this structure:

```text
projects/<project_name>/
├── project.json             # immutable source list and synthesis settings
├── ledger.json              # stage state, chunk state, timestamps, and hashes
├── source/                  # byte-for-byte copies of original inputs
├── text/
│   ├── pages/               # decoded or OCR text for each source page
│   ├── extracted.txt        # pages combined in deterministic order
│   ├── normalized.txt       # normalized full text
│   └── chunks/              # exact text submitted to the TTS model
└── audio/
    ├── chunks/              # empty after successful completion
    └── <project_name>.mp3   # sole generated audio artifact
```

While processing is incomplete, `audio/chunks/` contains resumable WAV files and
`audio/combined.wav` may temporarily exist. These generated WAVs are deleted only
after the final MP3 has been encoded and SHA-256 verified.

A Chatterbox reference recording is preserved under `source/` because it is an
original input, not a generated working file.

## Resuming work

Always resume by passing the existing project directory—not the original source
and `--project-name` again:

```sh
.venv/bin/tts-project projects/my-book
```

Inspect a project's ledger without starting work:

```sh
.venv/bin/tts-project projects/my-book --status
```

If a run is interrupted:

- Completed OCR pages are retained.
- Completed speech WAV chunks are retained.
- The next run verifies hashes before skipping anything.
- Working WAV files are deleted once the final MP3 is complete.
- A completed project does not recreate WAV files when resumed.

Project synthesis settings are frozen at creation. Create a new project if you
want to change the voice, model, language, speed, chunk size, or audio settings.

## Model downloads and caching

MLX-Audio downloads model weights through Hugging Face. The default cache is:

```text
~/.cache/huggingface/hub/
```

If the first model download must be stopped, press `Ctrl-C` before shutting down.
Completed files remain cached; the file actively downloading may restart from its
beginning. Resume the project normally:

```sh
.venv/bin/tts-project projects/<project_name>
```

Do not delete the Hugging Face cache or use a force-download option if you want to
reuse downloaded files.

## Speech backends

### Kokoro

Kokoro is the default and recommended backend for ordinary narration. It is an
82-million-parameter model with built-in voices and does not require a reference
recording.

```sh
.venv/bin/tts-project manuscript.txt \
  --project-name narration \
  --tts-backend kokoro \
  --voice af_heart \
  --language a \
  --speed 1.0
```

Common English voices include `af_heart`, `af_bella`, `af_nova`, `af_sky`,
`am_adam`, and `am_echo`. Language code `a` is American English and `b` is
British English. Other languages may require additional Misaki language extras.

### Chatterbox

Chatterbox is larger and slower but supports voice cloning and expressive speech.
It requires a clean reference recording; a 5–10 second WAV with one speaker and
minimal background noise is recommended.

```sh
.venv/bin/tts-project manuscript.txt \
  --project-name cloned-voice \
  --tts-backend chatterbox \
  --reference-audio clean-voice-sample.wav \
  --language en
```

Relevant controls are `--exaggeration`, `--cfg-weight`, and `--temperature`.

### Test tone

`test-tone` exercises project creation, chunking, checkpointing, cleanup, and MP3
encoding without loading a speech model. Its output is a tone, not human speech.

```sh
.venv/bin/tts-project manuscript.txt \
  --project-name pipeline-test \
  --tts-backend test-tone
```

## OCR backends

`--ocr-backend auto` selects Apple Vision on macOS. Vision runs locally and the
first image project compiles the small Objective-C bridge included in this repo.

Tesseract is available as an explicit fallback:

```sh
brew install tesseract
.venv/bin/tts-project scans/ \
  --project-name tesseract-book \
  --ocr-backend tesseract \
  --ocr-language eng
```

For Apple Vision, use language identifiers such as `en-US`. For Tesseract, use
installed language names such as `eng`.

## Common options

| Option | Default | Purpose |
|---|---:|---|
| `--project-name NAME` | Input filename | Name of a new project directory |
| `--projects-dir PATH` | `./projects` | Alternate project root |
| `--tts-backend` | `kokoro` | `kokoro`, `chatterbox`, or `test-tone` |
| `--ocr-backend` | `auto` | `auto`, `vision`, or `tesseract` |
| `--voice` | `af_heart` | Kokoro voice preset |
| `--language` | `a` or `en` | TTS language code |
| `--ocr-language` | `en-US` | OCR language identifier |
| `--speed` | `1.0` | Kokoro speech speed, from `0.25` to `4.0` |
| `--chunk-chars` | `350` | Target text size for a speech checkpoint |
| `--pause-ms` | `250` | Silence inserted between speech chunks |
| `--mp3-bitrate` | `128k` | Final MP3 bitrate |
| `--reference-audio PATH` | None | Required Chatterbox voice reference |
| `--status` | Off | Print an existing project's ledger summary |
| `--doctor` | Off | Check the local environment |
| `--quiet` | Off | Suppress stage progress messages |

Run `.venv/bin/tts-project --help` for every available Chatterbox and model
override option.

## Troubleshooting

### “Project already exists”

The project was already created. Resume using its directory:

```sh
.venv/bin/tts-project projects/<project_name>
```

### “Kokoro requires the optional 'misaki' package”

The base `misaki` package may be installed without its English dependencies.
Install the full extra and spaCy model:

```sh
.venv/bin/python -m pip install "misaki[en]==0.9.4"
.venv/bin/python -m spacy download en_core_web_sm
.venv/bin/python -c "import misaki.en, misaki.espeak; print('OK')"
```

Then resume the existing project; downloaded Kokoro weights and rendered chunks
will be reused.

### “No Metal device available”

Run the pipeline from a normal interactive macOS Terminal session. Sandboxed,
headless, virtualized, or some remote sessions may not expose the Metal device to
MLX even though the Mac has a supported GPU.

### Apple Vision OCR fails

Confirm that `xcrun clang --version` works. If Vision remains unavailable, install
Tesseract and create a new project with `--ocr-backend tesseract`. OCR settings are
frozen once a project is created.

### Preserved source was modified

The copy under the project's `source/` directory no longer matches its ledger
hash. Restore the original bytes or create a new project. The pipeline will not
silently combine artifacts produced from different source versions.

## Determinism

The orchestration is deterministic and idempotent: source ordering, text cleanup,
chunk planning, configuration hashing, checkpoint validation, and final assembly
are explicit and recorded. Chatterbox sampling is seeded from the chunk text.

Bit-identical regeneration across different model weights, MLX-Audio releases,
FFmpeg versions, or hardware is not guaranteed. Within a project, completed
artifacts are hash-verified and reused, preventing a resumed run from silently
changing already completed work.

## Development

Run the standard-library test suite:

```sh
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
```

The tests cover text normalization, natural filename ordering, end-to-end MP3
creation, interruption recovery, final WAV cleanup, source-integrity protection,
and preservation of a uniquely renamed final MP3.

## License

The package metadata currently declares the project under the MIT License. Add a
repository-level `LICENSE` file before redistributing the project publicly.
