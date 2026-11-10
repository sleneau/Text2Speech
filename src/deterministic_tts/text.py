from __future__ import annotations

import codecs
import re
import unicodedata
from pathlib import Path


TERMINAL_ELLIPSIS_POLICY = "terminal-ellipsis-to-period-v1"


def decode_text_file(path: Path) -> str:
    data = path.read_bytes()
    if data.startswith(codecs.BOM_UTF8):
        return data.decode("utf-8-sig")
    if data.startswith(codecs.BOM_UTF16_LE) or data.startswith(codecs.BOM_UTF16_BE):
        return data.decode("utf-16")
    if data.startswith(codecs.BOM_UTF32_LE) or data.startswith(codecs.BOM_UTF32_BE):
        return data.decode("utf-32")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeError(
            f"{path} is not UTF-8/UTF-16/UTF-32 text. Convert it to UTF-8 first."
        ) from exc


def normalize_text(value: str) -> str:
    value = unicodedata.normalize("NFC", value)
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = value.replace("\u00ad", "")
    # Conservatively repair OCR line-break hyphenation (lower-case continuation only).
    value = re.sub(r"(?<=[A-Za-z])-\n(?=[a-z])", "", value)
    value = "\n".join(line.rstrip() for line in value.splitlines())
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip() + "\n"


def normalize_terminal_ellipsis(value: str, mode: str = "preserve") -> str:
    """Prevent a request-final ellipsis from instructing TTS to trail off."""
    if mode not in {"period", "preserve"}:
        raise ValueError(f"unsupported terminal ellipsis mode: {mode}")
    if mode == "preserve":
        return value
    has_newline = value.endswith("\n")
    core = value.rstrip()
    core = re.sub(r"(?:\.{3,}|…+)([\"'”’\)\]]*)$", r".\1", core)
    return core + ("\n" if has_newline else "")


def _split_oversized(unit: str, target: int) -> list[str]:
    words = unit.split()
    if not words:
        return []
    output: list[str] = []
    current = ""
    for word in words:
        if len(word) > target:
            if current:
                output.append(current)
                current = ""
            output.extend(word[i : i + target] for i in range(0, len(word), target))
            continue
        candidate = f"{current} {word}".strip()
        if current and len(candidate) > target:
            output.append(current)
            current = word
        else:
            current = candidate
    if current:
        output.append(current)
    return output


def _sentence_units(paragraph: str, target: int) -> list[str]:
    units: list[str] = []
    sentences = re.split(r"(?<=[.!?])\s+", re.sub(r"\s*\n\s*", " ", paragraph))
    for sentence in sentences:
        sentence = sentence.strip()
        if len(sentence) <= target:
            units.append(sentence)
        else:
            units.extend(_split_oversized(sentence, target))
    return units


def _pack_units(units: list[str], target: int) -> list[str]:
    chunks: list[str] = []
    current = ""
    for unit in units:
        candidate = f"{current} {unit}".strip()
        if current and len(candidate) > target:
            chunks.append(current)
            current = unit
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def chunk_text(value: str, target: int, mode: str = "target") -> list[str]:
    if target < 100:
        raise ValueError("chunk size must be at least 100 characters")
    if mode not in {"paragraph", "target"}:
        raise ValueError(f"unsupported chunk mode: {mode}")
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", value) if part.strip()]

    if mode == "paragraph":
        chunks: list[str] = []
        for paragraph in paragraphs:
            paragraph = re.sub(r"\s*\n\s*", " ", paragraph).strip()
            if len(paragraph) <= target:
                chunks.append(paragraph)
            else:
                chunks.extend(_pack_units(_sentence_units(paragraph, target), target))
        return [chunk + "\n" for chunk in chunks]

    units: list[str] = []
    for paragraph in paragraphs:
        units.extend(_sentence_units(paragraph, target))
    chunks = _pack_units(units, target)
    return [chunk + "\n" for chunk in chunks]


def split_text_for_retry(value: str, target: int) -> list[str]:
    """Split a failed speech request deterministically into at least two pieces."""
    text = re.sub(r"\s+", " ", value).strip()
    if not text:
        return []
    pieces = [item.strip() for item in chunk_text(text, target, mode="target") if item.strip()]
    if len(pieces) >= 2:
        return [piece + "\n" for piece in pieces]

    words = text.split()
    if len(words) >= 2:
        midpoint = len(words) // 2
        return [" ".join(words[:midpoint]) + "\n", " ".join(words[midpoint:]) + "\n"]
    if len(text) >= 2:
        midpoint = len(text) // 2
        return [text[:midpoint] + "\n", text[midpoint:] + "\n"]
    return [text + "\n"]

