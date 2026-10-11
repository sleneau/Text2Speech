from __future__ import annotations

import codecs
import re
import unicodedata
from pathlib import Path


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


def chunk_text(value: str, target: int) -> list[str]:
    if target < 100:
        raise ValueError("chunk size must be at least 100 characters")
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", value) if part.strip()]
    units: list[str] = []
    for paragraph in paragraphs:
        sentences = re.split(r"(?<=[.!?])\s+", re.sub(r"\s*\n\s*", " ", paragraph))
        for sentence in sentences:
            sentence = sentence.strip()
            if len(sentence) <= target:
                units.append(sentence)
            else:
                units.extend(_split_oversized(sentence, target))

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
    return [chunk + "\n" for chunk in chunks]

