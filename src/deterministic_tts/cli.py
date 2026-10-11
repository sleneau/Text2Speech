from __future__ import annotations

import argparse
import importlib.metadata
import platform
import shutil
import sys
from pathlib import Path
from typing import Any

from .backends import DEFAULT_MODELS
from .pipeline import ProjectPipeline, create_project, is_project
from .util import load_json

DEFAULTS: dict[str, Any] = {
    "tts_backend": "kokoro",
    "ocr_backend": "auto",
    "pdf_mode": "auto",
    "pdf_dpi": 300,
    "pdf_cleanup": "headers-footers",
    "voice": "af_heart",
    "model": None,
    "language": None,
    "ocr_language": "en-US",
    "speed": 1.0,
    "chunk_chars": 350,
    "pause_ms": 250,
    "mp3_bitrate": "128k",
    "exaggeration": 0.5,
    "cfg_weight": 0.5,
    "temperature": 0.8,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tts-project",
        description="Create or resume a deterministic OCR/text-to-speech project.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  tts-project chapter.txt --project-name chapter
  tts-project scans/ --project-name scanned-book
  tts-project scanned-book.pdf --project-name scanned-pdf
  tts-project page01.png
  tts-project projects/scanned-book
  tts-project chapter.txt --tts-backend chatterbox --reference-audio voice.wav

Input may be a UTF-8/16/32 text file, one image, a folder whose direct children
are images, a PDF, or an existing project folder. Image names are naturally
ordered (page2 before page10). Project settings are frozen when the project is
created.
""",
    )
    parser.add_argument("input", nargs="?", type=Path, help="source path or existing project path")
    parser.add_argument("--project-name", help="new project folder name (defaults to input name)")
    parser.add_argument(
        "--projects-dir", type=Path, default=Path("projects"), help="project root (default: ./projects)"
    )
    parser.add_argument(
        "--tts-backend", choices=("kokoro", "chatterbox", "test-tone"), default=None,
        help="local speech engine (default: kokoro; test-tone is for validation only)",
    )
    parser.add_argument(
        "--ocr-backend", choices=("auto", "vision", "tesseract"), default=None,
        help="image OCR engine (default: Apple Vision on macOS)",
    )
    parser.add_argument(
        "--pdf-mode", choices=("auto", "text", "ocr"), default=None,
        help="PDF extraction mode (default: auto; embedded text with per-page OCR fallback)",
    )
    parser.add_argument(
        "--pdf-dpi", type=int, default=None,
        help="PDF page render resolution for OCR, 72-600 (default: 300)",
    )
    parser.add_argument(
        "--pdf-cleanup", choices=("headers-footers", "none"), default=None,
        help="remove repeated PDF headers, footers, and page numbers (default: headers-footers)",
    )
    parser.add_argument("--voice", default=None, help="Kokoro voice ID (default: af_heart)")
    parser.add_argument("--model", default=None, help="override the Hugging Face MLX model ID")
    parser.add_argument(
        "--language", default=None,
        help="speech language code (Kokoro default: a/US English; Chatterbox default: en)",
    )
    parser.add_argument("--ocr-language", default=None, help="OCR language (default: en-US)")
    parser.add_argument("--speed", type=float, default=None, help="Kokoro speaking speed (default: 1.0)")
    parser.add_argument("--reference-audio", type=Path, help="voice reference required by Chatterbox")
    parser.add_argument("--chunk-chars", type=int, default=None, help="target chunk size (default: 350)")
    parser.add_argument("--pause-ms", type=int, default=None, help="silence between chunks (default: 250)")
    parser.add_argument("--mp3-bitrate", default=None, help="FFmpeg MP3 bitrate (default: 128k)")
    parser.add_argument("--exaggeration", type=float, default=None, help="Chatterbox expressiveness (default: 0.5)")
    parser.add_argument("--cfg-weight", type=float, default=None, help="Chatterbox guidance (default: 0.5)")
    parser.add_argument("--temperature", type=float, default=None, help="Chatterbox sampling (default: 0.8)")
    parser.add_argument("--status", action="store_true", help="show an existing project's ledger and exit")
    parser.add_argument("--doctor", action="store_true", help="check local prerequisites and exit")
    parser.add_argument("--quiet", action="store_true", help="suppress stage progress")
    return parser


def _package_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def doctor_text() -> str:
    system = platform.system()
    machine = platform.machine()
    mlx_version = _package_version("mlx-audio")
    ffmpeg = shutil.which("ffmpeg")
    xcrun = shutil.which("xcrun")
    tesseract = shutil.which("tesseract")
    statuses = [
        f"  Python 3.11-3.13 : {'OK' if (3, 11) <= sys.version_info[:2] < (3, 14) else 'NEEDS 3.11-3.13'} ({platform.python_version()})",
        f"  Apple Silicon    : {'OK' if system == 'Darwin' and machine == 'arm64' else 'MLX unavailable'} ({system} {machine})",
        f"  MLX-Audio        : {'OK ' + mlx_version if mlx_version else 'MISSING (needed for Kokoro/Chatterbox)'}",
        f"  FFmpeg           : {'OK ' + ffmpeg if ffmpeg else 'MISSING (needed for MP3)'}",
        f"  Vision + PDFKit  : {'OK via ' + xcrun if system == 'Darwin' and xcrun else 'UNAVAILABLE'}",
        f"  Tesseract OCR    : {'OK ' + tesseract if tesseract else 'not installed (optional fallback)'}",
    ]
    return "\n".join(statuses)


def print_onboarding(parser: argparse.ArgumentParser) -> None:
    parser.print_help()
    print(
        """
Local stack
  Nothing runs as a server or daemon. The command invokes OCR, the MLX model,
  and FFmpeg directly. Model weights download from Hugging Face on first use;
  subsequent runs can be fully offline.

macOS setup (from this directory)
  /Library/Frameworks/Python.framework/Versions/3.11/bin/python3.11 -m venv .venv
  .venv/bin/pip install -e '.[local]'
  .venv/bin/pip install 'misaki[en]==0.9.4'
  .venv/bin/python -m spacy download en_core_web_sm
  brew install ffmpeg

Apple Vision OCR and PDFKit ship with macOS; their local helpers are compiled
through Xcode Command Line Tools. If `xcrun` is missing, run:
  xcode-select --install
Optional Tesseract fallback: brew install tesseract

Technology references
  MLX-Audio:  https://github.com/Blaizzy/mlx-audio
  Kokoro:     https://huggingface.co/hexgrad/Kokoro-82M
  Chatterbox: https://github.com/resemble-ai/chatterbox
  FFmpeg:     https://ffmpeg.org/

Current environment
""".rstrip()
    )
    print(doctor_text())


def _new_settings(args: argparse.Namespace) -> dict[str, Any]:
    settings = {
        key: getattr(args, key) if getattr(args, key) is not None else default
        for key, default in DEFAULTS.items()
    }
    backend = settings["tts_backend"]
    if settings["model"] is None:
        settings["model"] = DEFAULT_MODELS.get(backend)
    if settings["language"] is None:
        settings["language"] = "en" if backend == "chatterbox" else "a"
    if not 0.25 <= settings["speed"] <= 4.0:
        raise RuntimeError("--speed must be between 0.25 and 4.0")
    if not 100 <= settings["chunk_chars"] <= 1000:
        raise RuntimeError("--chunk-chars must be between 100 and 1000")
    if not 0 <= settings["pause_ms"] <= 5000:
        raise RuntimeError("--pause-ms must be between 0 and 5000")
    if not 72 <= settings["pdf_dpi"] <= 600:
        raise RuntimeError("--pdf-dpi must be between 72 and 600")
    if not settings["mp3_bitrate"].endswith("k") or not settings["mp3_bitrate"][:-1].isdigit():
        raise RuntimeError("--mp3-bitrate must look like 128k or 192k")
    return settings


def _explicit_resume_settings(args: argparse.Namespace) -> dict[str, Any]:
    keys = tuple(DEFAULTS)
    return {key: getattr(args, key) for key in keys if getattr(args, key) is not None}


def _validate_resume_options(args: argparse.Namespace, project_dir: Path) -> None:
    config = load_json(project_dir / "project.json")
    stored = config["settings"]
    for key, requested in _explicit_resume_settings(args).items():
        if requested != stored.get(key):
            raise RuntimeError(
                f"Project setting {key!r} is frozen as {stored.get(key)!r}, not {requested!r}. "
                "Start a new project to change synthesis settings."
            )
    if args.reference_audio is not None:
        raise RuntimeError("A reference voice cannot be replaced while resuming a project")


def _print_status(project_dir: Path) -> None:
    config = load_json(project_dir / "project.json")
    ledger = load_json(project_dir / "ledger.json")
    print(f"Project: {config['project_name']}")
    print(f"Path:    {project_dir}")
    print(f"Status:  {ledger.get('status', 'unknown')}")
    for name, stage in ledger.get("stages", {}).items():
        print(f"  {name:10} {stage.get('status', 'unknown')}")
    chunks = ledger.get("chunks", {})
    if chunks:
        rendered = sum(item.get("status") in {"complete", "cleaned"} for item in chunks.values())
        cleaned = sum(item.get("status") == "cleaned" for item in chunks.values())
        suffix = "; working WAVs removed" if cleaned == len(chunks) else ""
        print(f"  speech     {rendered}/{len(chunks)} chunks rendered{suffix}")
    if ledger.get("last_error"):
        print(f"Last error: {ledger['last_error'].get('message')}")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    if not argv:
        print_onboarding(parser)
        return 0
    args = parser.parse_args(argv)
    if args.doctor and args.input is None:
        print(doctor_text())
        return 0
    if args.input is None:
        parser.error("input is required unless --doctor is used")

    try:
        candidate = args.input.expanduser().resolve()
        if is_project(candidate):
            project_dir = candidate
            _validate_resume_options(args, project_dir)
        else:
            settings = _new_settings(args)
            project_dir = create_project(
                candidate, args.projects_dir, args.project_name, settings, args.reference_audio
            )
            print(f"Created project: {project_dir}")
        if args.status:
            _print_status(project_dir)
            return 0
        ProjectPipeline(project_dir, progress=not args.quiet).run()
        return 0
    except (RuntimeError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

