# Deterministic, resumable local text-to-speech

This project turns a text file, one source image, or an ordered folder of source
images into a normalized MP3. It is designed for this Apple Silicon Mac:

- **Apple Vision** performs local OCR using the framework already in macOS.
- **MLX-Audio** runs Kokoro (default) or Chatterbox locally on the Apple GPU.
- **FFmpeg/libmp3lame** normalizes and encodes the final MP3.
- An atomic, SHA-256-verified ledger makes every expensive stage resumable.

No local LLM and no background service are required. OCR and speech generation
are deterministic transformations; Chatterbox's random generator is seeded,
and already-rendered audio is content-addressed and never regenerated unless an
input artifact fails verification.

## Install on this computer

Python 3.11 is recommended because the system's newer Python may not be supported
by every audio dependency:

```sh
/Library/Frameworks/Python.framework/Versions/3.11/bin/python3.11 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -e '.[local]'
brew install ffmpeg
```

The source-tree launcher also works before an editable install (and is useful for
checking prerequisites while offline): `python3.11 tts_project.py --doctor`.

Apple Vision does not need a Homebrew package. The first image project compiles a
small Objective-C bridge with the installed Xcode Command Line Tools. If `xcrun` is not
available, run `xcode-select --install`. Tesseract is an optional cross-platform
fallback (`brew install tesseract`).

The first Kokoro or Chatterbox run downloads model weights from Hugging Face.
After that, generation can be offline. Kokoro is the recommended default: it is
small, fast, and requires no voice sample. Chatterbox is much larger and requires
a clean 5-10 second reference recording but supports voice cloning and more expressive output.

## Use

With no arguments, the command prints full help and checks the current environment:

```sh
.venv/bin/tts-project
# or: .venv/bin/python tts_project.py
```

Create projects from supported text, a single image, or a folder of images:

```sh
.venv/bin/tts-project manuscript.txt --project-name my-book
.venv/bin/tts-project scans/ --project-name my-scanned-book
.venv/bin/tts-project page01.png
```

Image folders are non-recursive and sorted naturally (`page2.jpg` precedes
`page10.jpg`). The OCR result of every image is saved separately before the pages
are combined. Text inputs may be UTF-8, UTF-16, or UTF-32.

Resume an interrupted project by passing its project folder:

```sh
.venv/bin/tts-project projects/my-book
.venv/bin/tts-project projects/my-book --status
```

Use Chatterbox with a voice sample:

```sh
.venv/bin/tts-project manuscript.txt \
  --project-name cloned-voice \
  --tts-backend chatterbox \
  --reference-audio clean-voice-sample.wav
```

Project synthesis settings are frozen at creation. This is intentional: changing
a voice or chunking option creates a meaningfully different artifact, so create a
new project for that run.

## Project layout

```text
projects/<project_name>/
├── project.json             # immutable inputs and settings
├── ledger.json              # atomic stage/chunk checkpoints and hashes
├── source/                  # byte-for-byte copies of original inputs
├── text/
│   ├── pages/               # OCR/decoded text for each source
│   ├── extracted.txt        # ordered page combination
│   ├── normalized.txt       # deterministic cleanup
│   └── chunks/              # exact text sent to the voice model
└── audio/
    ├── chunks/              # temporary resumable WAVs while processing
    └── <project_name>.mp3   # sole audio file after successful completion
```

If execution stops, rerun the project path. Completed pages and speech chunks are
verified against the ledger and skipped. If a preserved source file was changed,
the run stops rather than mixing old and new inputs.

Chunk and combined WAV files are interruption checkpoints, not final outputs. They
are retained while a project is incomplete and automatically deleted only after
the final MP3 has been encoded and SHA-256 verified. Resuming a completed project
validates the MP3 without recreating those working files.

## Useful options

Run `.venv/bin/tts-project --help` for every option. Common controls are:

- `--voice af_heart`, `--speed 1.0`, `--language a` for Kokoro.
- `--ocr-backend vision|tesseract` and `--ocr-language en-US`.
- `--chunk-chars 350`, `--pause-ms 250`, and `--mp3-bitrate 128k`.
- `--doctor` to check dependencies without starting a project.
- `--tts-backend test-tone` to validate the full pipeline without downloading a model; it is not speech.

Upstream references: [MLX-Audio](https://github.com/Blaizzy/mlx-audio),
[Kokoro](https://huggingface.co/hexgrad/Kokoro-82M),
[Chatterbox](https://github.com/resemble-ai/chatterbox), and
[FFmpeg](https://ffmpeg.org/).

