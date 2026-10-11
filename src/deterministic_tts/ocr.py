from __future__ import annotations

import json
import platform
import shutil
import subprocess
from importlib.resources import files
from pathlib import Path

from .util import sha256_file


def resolve_ocr_backend(requested: str) -> str:
    if requested != "auto":
        return requested
    if platform.system() == "Darwin" and shutil.which("xcrun"):
        return "vision"
    if shutil.which("tesseract"):
        return "tesseract"
    raise RuntimeError(
        "No OCR backend is available. On macOS install Xcode Command Line Tools "
        "with: xcode-select --install. Else install Tesseract."
    )


def _vision_executable(cache_dir: Path) -> Path:
    source = Path(str(files("deterministic_tts").joinpath("vision_ocr.m")))
    key = sha256_file(source)[:12]
    executable = cache_dir / f"vision-ocr-{key}"
    if executable.is_file():
        return executable
    xcrun = shutil.which("xcrun")
    if not xcrun:
        raise RuntimeError(
            "Apple Vision OCR needs Xcode Command Line Tools: xcode-select --install"
        )
    cache_dir.mkdir(parents=True, exist_ok=True)
    modules = cache_dir / "clang-modules"
    modules.mkdir(parents=True, exist_ok=True)
    command = [
        xcrun,
        "clang",
        "-fobjc-arc",
        "-O2",
        f"-fmodules-cache-path={modules}",
        "-framework",
        "Foundation",
        "-framework",
        "CoreGraphics",
        "-framework",
        "Vision",
        "-framework",
        "ImageIO",
        str(source),
        "-o",
        str(executable),
    ]
    completed = subprocess.run(command, capture_output=True, text=True)
    if completed.returncode:
        raise RuntimeError(
            "Could not compile the Apple Vision OCR helper: "
            + (completed.stderr.strip() or "unknown compiler error")
        )
    return executable


def vision_ocr(image: Path, language: str, cache_dir: Path) -> str:
    executable = _vision_executable(cache_dir)
    completed = subprocess.run(
        [str(executable), str(image), language], capture_output=True, text=True
    )
    if completed.returncode:
        raise RuntimeError(
            f"Vision OCR failed for {image.name}: "
            + (completed.stderr.strip() or "unknown error")
        )
    try:
        records = json.loads(completed.stdout)
        return "\n".join(record["text"] for record in records).strip() + "\n"
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise RuntimeError(f"Vision OCR returned invalid output for {image.name}") from exc


def tesseract_ocr(image: Path, language: str) -> str:
    executable = shutil.which("tesseract")
    if not executable:
        raise RuntimeError("Tesseract is not installed. On macOS: brew install tesseract")
    mapped_language = {"en-US": "eng", "en": "eng"}.get(language, language)
    completed = subprocess.run(
        [executable, str(image), "stdout", "-l", mapped_language, "--oem", "1", "--psm", "3"],
        capture_output=True,
        text=True,
    )
    if completed.returncode:
        raise RuntimeError(
            f"Tesseract failed for {image.name}: "
            + (completed.stderr.strip() or "unknown error")
        )
    return completed.stdout.strip() + "\n"


def extract_image_text(image: Path, backend: str, language: str, cache_dir: Path) -> str:
    selected = resolve_ocr_backend(backend)
    if selected == "vision":
        return vision_ocr(image, language, cache_dir)
    return tesseract_ocr(image, language)

