from __future__ import annotations

import json
import re
import shutil
import subprocess
import unicodedata
from collections import defaultdict
from importlib.resources import files
from pathlib import Path
from typing import Any

from .util import sha256_file

PDF_EXTRACTOR_VERSION = "pdfkit-v1+margin-cleanup-v1"
_BOUNDARY_LINE_COUNT = 3
_ARABIC_PAGE_NUMBER = re.compile(
    r"^\s*[-–—|•·\[(]*\s*(?:page\s+)?\d+"
    r"(?:\s*(?:of|/)\s*\d+)?\s*[-–—|•·\])]*\s*$",
    flags=re.IGNORECASE,
)
_ROMAN_PAGE_NUMBER = re.compile(
    r"^\s*[-–—|•·\[(]*\s*(?:page\s+)?[ivxlcdm]+"
    r"(?:\s*(?:of|/)\s*[ivxlcdm]+)?\s*[-–—|•·\])]*\s*$",
    flags=re.IGNORECASE,
)


def _margin_signature(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    normalized = re.sub(r"\d+", "#", normalized)
    return " ".join(normalized.split())


def clean_pdf_body_pages(
    page_texts: list[str],
) -> tuple[list[str], list[dict[str, Any]]]:
    """Remove high-confidence page numbers and repeated boundary lines.

    Repeated lines must appear in the same top/bottom region on at least three
    pages. This intentionally leaves ambiguous one-off material intact.
    """
    page_lines = [value.splitlines() for value in page_texts]
    candidates: dict[tuple[str, str], dict[int, list[int]]] = defaultdict(
        lambda: defaultdict(list)
    )
    regions: list[dict[int, set[str]]] = []

    for page_index, lines in enumerate(page_lines):
        nonempty = [index for index, line in enumerate(lines) if line.strip()]
        per_line: dict[int, set[str]] = defaultdict(set)
        if nonempty:
            count = min(_BOUNDARY_LINE_COUNT, max(1, len(nonempty) // 2))
            for region, indices in (
                ("header", nonempty[:count]),
                ("footer", nonempty[-count:]),
            ):
                for line_index in indices:
                    per_line[line_index].add(region)
                    if len(nonempty) < 3:
                        continue
                    signature = _margin_signature(lines[line_index])
                    if not signature:
                        continue
                    candidates[(region, signature)][page_index].append(line_index)
        regions.append(per_line)

    repeated = {
        key for key, occurrences in candidates.items() if len(occurrences) >= 3
    }
    removals_by_page: list[dict[int, dict[str, Any]]] = [dict() for _ in page_lines]

    for (region, signature), occurrences in candidates.items():
        if (region, signature) not in repeated:
            continue
        for page_index, line_indices in occurrences.items():
            for line_index in line_indices:
                removals_by_page[page_index][line_index] = {
                    "location": region,
                    "reason": f"repeated_{region}",
                }

    for page_index, lines in enumerate(page_lines):
        for line_index, line_regions in regions[page_index].items():
            is_arabic = _ARABIC_PAGE_NUMBER.fullmatch(lines[line_index])
            is_footer_roman = (
                _ROMAN_PAGE_NUMBER.fullmatch(lines[line_index])
                and "footer" in line_regions
                and "header" not in line_regions
            )
            if is_arabic or is_footer_roman:
                location = "footer" if "footer" in line_regions else "header"
                removals_by_page[page_index][line_index] = {
                    "location": location,
                    "reason": "page_number",
                }

    cleaned_pages: list[str] = []
    removals: list[dict[str, Any]] = []
    for page_index, lines in enumerate(page_lines):
        removed = removals_by_page[page_index]
        kept = [line for line_index, line in enumerate(lines) if line_index not in removed]
        cleaned = re.sub(r"\n[ \t]*\n(?:[ \t]*\n)+", "\n\n", "\n".join(kept)).strip()
        cleaned_pages.append(cleaned + ("\n" if cleaned else ""))
        for line_index in sorted(removed):
            removals.append(
                {
                    "page_number": page_index + 1,
                    "line": lines[line_index].strip(),
                    **removed[line_index],
                }
            )
    return cleaned_pages, removals


def _pdf_executable(cache_dir: Path) -> Path:
    source = Path(str(files("deterministic_tts").joinpath("pdf_helper.m")))
    key = sha256_file(source)[:12]
    executable = cache_dir / f"pdf-helper-{key}"
    if executable.is_file():
        return executable
    xcrun = shutil.which("xcrun")
    if not xcrun:
        raise RuntimeError(
            "PDF processing needs Xcode Command Line Tools: xcode-select --install"
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
        "AppKit",
        "-framework",
        "PDFKit",
        str(source),
        "-o",
        str(executable),
    ]
    completed = subprocess.run(command, capture_output=True, text=True)
    if completed.returncode:
        raise RuntimeError(
            "Could not compile the PDFKit helper: "
            + (completed.stderr.strip() or "unknown compiler error")
        )
    return executable


def _run_pdf_helper(cache_dir: Path, *arguments: str) -> dict[str, Any]:
    executable = _pdf_executable(cache_dir)
    completed = subprocess.run(
        [str(executable), *arguments], capture_output=True, text=True
    )
    if completed.returncode:
        raise RuntimeError(completed.stderr.strip() or "PDFKit helper failed")
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("PDFKit helper returned invalid JSON") from exc
    if not isinstance(result, dict):
        raise RuntimeError("PDFKit helper returned an unexpected result")
    return result


def pdf_info(path: Path, cache_dir: Path) -> dict[str, Any]:
    result = _run_pdf_helper(cache_dir, "info", str(path))
    page_count = result.get("page_count")
    if not isinstance(page_count, int) or page_count < 1:
        raise RuntimeError(f"PDF contains no readable pages: {path}")
    return result


def pdf_page_text(path: Path, page_index: int, cache_dir: Path) -> str:
    result = _run_pdf_helper(cache_dir, "text", str(path), str(page_index))
    value = result.get("text")
    if not isinstance(value, str):
        raise RuntimeError(f"PDFKit returned invalid text for page {page_index + 1}")
    return value


def pdf_render_page(
    path: Path, page_index: int, output: Path, dpi: int, cache_dir: Path
) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    _run_pdf_helper(
        cache_dir, "render", str(path), str(page_index), str(output), str(dpi)
    )
    if not output.is_file() or output.stat().st_size == 0:
        raise RuntimeError(f"PDFKit did not render page {page_index + 1}")

