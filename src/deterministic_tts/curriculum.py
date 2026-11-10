"""DOCX extraction, bounded Crossway lookups, and reviewed narration assembly.

No model is called here. Meaning, citation discovery, and table interpretation
require editorial judgment; the companion instructions describe that work.
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path
from string import Template
from typing import Any
from xml.etree import ElementTree as ET
from zipfile import BadZipFile, ZipFile

from .text import normalize_text
from .esv import fetch_passage, parse_reference, reusable
from .util import atomic_write_json, atomic_write_text, load_json, sha256_file, stable_hash

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
BOOKS = (
    r"(?:[123]\s*)?(?:Genesis|Gen\.?|Exodus|Ex\.?|Leviticus|Lev\.?|Numbers|Num\.?|"
    r"Deuteronomy|Deut\.?|Joshua|Josh\.?|Judges|Judg\.?|Ruth|Samuel|Sam\.?|Kings|"
    r"Chronicles|Chron\.?|Ezra|Nehemiah|Neh\.?|Esther|Esth\.?|Job|Psalms?|Ps\.?|"
    r"Proverbs|Prov\.?|Ecclesiastes|Eccl\.?|Song of (?:Solomon|Songs)|Isaiah|Isa\.?|"
    r"Jeremiah|Jer\.?|Lamentations|Lam\.?|Ezekiel|Ezek\.?|Daniel|Dan\.?|Hosea|Hos\.?|"
    r"Joel|Amos|Obadiah|Obad\.?|Jonah|Micah|Mic\.?|Nahum|Nah\.?|Habakkuk|Hab\.?|"
    r"Zephaniah|Zeph\.?|Haggai|Hag\.?|Zechariah|Zech\.?|Malachi|Mal\.?|Matthew|Matt\.?|"
    r"Mark|Luke|John|Jn\.?|Acts|Romans|Rom\.?|Corinthians|Cor\.?|Galatians|Gal\.?|"
    r"Ephesians|Eph\.?|Philippians|Phil\.?|Colossians|Col\.?|Thessalonians|Thess\.?|"
    r"Timothy|Tim\.?|Titus|Philemon|Phlm\.?|Hebrews|Heb\.?|James|Jas\.?|Peter|Pet\.?|"
    r"Jude|Revelation|Rev\.?)"
)
NUMBER = r"\d+(?::\d+)?(?:\s*[-–—]\s*\d+(?::\d+)?)?"
CITATION = re.compile(
    rf"(?<!\w){BOOKS}\s+{NUMBER}(?:\s*[,;]\s*(?!{BOOKS}\s+\d){NUMBER})*(?!\w)",
    re.I,
)


def references(text: str) -> list[str]:
    """Candidates only; the editor must also find unsupported citation forms."""
    return list(dict.fromkeys(match.group() for match in CITATION.finditer(text)))


def _paragraph(element: ET.Element) -> str:
    pieces = []
    for item in element.iter():
        if item.tag == W + "t":
            pieces.append(item.text or "")
        elif item.tag == W + "tab":
            pieces.append("\t")
        elif item.tag in {W + "br", W + "cr"}:
            pieces.append("\n")
        elif item.tag == W + "noBreakHyphen":
            pieces.append("‑")
    return "".join(pieces)


def extract_docx(path: Path) -> list[dict[str, Any]]:
    """Keep body order and physical cells; reject content we cannot represent."""
    with ZipFile(path) as archive:
        # Bound uncompressed XML before reading it into memory.
        if sum(item.file_size for item in archive.infolist()) > 100_000_000:
            raise ValueError("DOCX exceeds the 100 MB uncompressed preparation limit")
        root = ET.fromstring(archive.read("word/document.xml"))
        forbidden = {
            "drawing", "pict", "object", "altChunk", "ins", "del", "moveFrom", "moveTo",
            "footnoteReference", "endnoteReference", "sym", "contentPart", "subDoc",
            "sdt", "customXml", "fldSimple", "fldChar", "instrText",
        }
        for item in root.iter():
            local = item.tag.rsplit("}", 1)[-1]
            if local in forbidden or "officeDocument/2006/math" in item.tag:
                raise ValueError(
                    f"Unsupported DOCX content: {local}. Resolve tracked changes or provide "
                    "an accessible text version of drawings, notes, or equations first."
                )
        for name in archive.namelist():
            if re.fullmatch(r"word/(?:header|footer)\d+\.xml", name):
                margin = ET.fromstring(archive.read(name))
                if (any((node.text or "").strip() for node in margin.iter(W + "t"))
                        or any(node.tag.rsplit("}", 1)[-1] in forbidden for node in margin.iter())):
                    raise ValueError(
                        f"Text in {name} needs manual disposition. Move meaningful text into "
                        "the body, or remove decorative margins in a COPY before preparing."
                    )
        styles = {}
        if "word/styles.xml" in archive.namelist():
            styles = {
                item.get(W + "styleId"): item
                for item in ET.fromstring(archive.read("word/styles.xml")).findall(W + "style")
            }

        def properties(p: ET.Element) -> dict[str, Any]:
            style = p.find(f"{W}pPr/{W}pStyle")
            style_id = style.get(W + "val", "") if style is not None else ""
            inherited = []
            current = style_id
            while current in styles and current not in inherited:
                inherited.append(current)
                parent = styles[current].find(W + "basedOn")
                current = parent.get(W + "val", "") if parent is not None else ""
            nodes = [p] + [styles[key] for key in inherited]
            return {
                "style": style_id,
                "list_item": any(node.find(f"{W}pPr/{W}numPr") is not None for node in nodes),
                "outline": any(node.find(f"{W}pPr/{W}outlineLvl") is not None for node in nodes),
                "emphasis": [
                    {"text": _paragraph(run), "properties": ET.tostring(props, encoding="unicode")}
                    for run in p.iter(W + "r")
                    if (props := run.find(W + "rPr")) is not None
                ],
            }

        body = root.find(W + "body")
        if body is None:
            raise ValueError("DOCX has no document body")
        blocks = []
        for element in body:
            if element.tag == W + "sectPr":
                continue
            block_id = f"b{len(blocks) + 1:04d}"
            if element.tag == W + "p":
                text = _paragraph(element)
                if not text.strip():
                    continue
                metadata = properties(element)
                block = {"id": block_id, "kind": "paragraph", "text": text, **metadata}
            elif element.tag == W + "tbl":
                if len(list(element.iter(W + "tbl"))) > 1:
                    raise ValueError("Nested tables require an accessible text version first")
                cells = []
                rows = []
                for row_index, row in enumerate(element.findall(W + "tr"), 1):
                    row_text = []
                    for cell_index, cell in enumerate(row.findall(W + "tc"), 1):
                        text = "\n".join(_paragraph(p) for p in cell.findall(W + "p"))
                        cell_id = f"r{row_index}c{cell_index}"
                        cells.append({"id": cell_id, "text": text})
                        row_text.append(f"[{cell_id}] {text}")
                    rows.append(" | ".join(row_text))
                block = {"id": block_id, "kind": "table", "text": "\n".join(rows), "cells": cells}
            else:
                raise ValueError(f"Unsupported DOCX body element: {element.tag}")
            # Preserve formatting/merges/numbering for the editor without flattening it away.
            block["xml"] = ET.tostring(element, encoding="unicode")
            block["detected_references"] = references(block["text"])
            blocks.append(block)
    if not blocks:
        raise ValueError("DOCX has no narratable body text")
    return blocks


def initialize(source: Path, project: Path) -> None:
    if project.exists():
        raise ValueError("Preparation directory already exists; choose a new directory")
    blocks = extract_docx(source)
    project.mkdir(parents=True)
    shutil.copyfile(source, project / "source.docx")
    atomic_write_json(project / "manifest.json", {
        "version": 1, "source_name": source.name,
        "source_sha256": sha256_file(project / "source.docx"), "blocks": blocks,
    })
    atomic_write_json(project / "templates.json", {
        "opening": "", "closing": "", "variables": {},
    })
    atomic_write_json(project / "passages.json", {})
    atomic_write_json(project / "plan.json", {
        "version": 1, "inventory_checked": False,
        "blocks": [{
            "id": block["id"], "narration": "" if block["kind"] == "table" else block["text"],
            "before": "", "after": "", "reason": "", "complex": block["kind"] == "table",
            "cell_coverage": {}, "citations": [{
                "source_reference": ref, "passages": [], "already_quoted": False,
                "note": "", "insert_at": None,
            } for ref in block["detected_references"]],
        } for block in blocks],
    })
    atomic_write_json(project / "approvals.json", {})


def read_project(project: Path) -> dict[str, Any]:
    data = {name: load_json(project / f"{name}.json") for name in (
        "manifest", "plan", "templates", "passages", "approvals",
    )}
    manifest = data["manifest"]
    if manifest.get("version") != 1 or data["plan"].get("version") != 1:
        raise ValueError("Unsupported preparation version")
    if sha256_file(project / "source.docx") != manifest["source_sha256"]:
        raise ValueError("Preserved source.docx has changed")
    if extract_docx(project / "source.docx") != manifest["blocks"]:
        raise ValueError("Extracted manifest does not match preserved source.docx")
    expected = [block["id"] for block in manifest["blocks"]]
    if [block["id"] for block in data["plan"]["blocks"]] != expected:
        raise ValueError("Plan must contain every source block exactly once, in source order")
    return data


def fetch_passages(project: Path) -> dict[str, int]:
    """Resolve plan citations, fetching only explicit verse spans from Crossway.

    Validate the entire request list before any HTTP calls. Completed lookups
    are checkpointed individually; the plan is changed only after success.
    """
    data = read_project(project)
    requests = {}
    skipped = 0
    for block in data["plan"]["blocks"]:
        for citation in block["citations"]:
            if citation["already_quoted"] is True:
                continue
            keys = citation["passages"]
            if not isinstance(keys, list) or not all(isinstance(key, str) for key in keys):
                raise ValueError("Citation passages must be a list of explicit verse references")
            try:
                source_spans = parse_reference(citation["source_reference"])
            except ValueError:
                if not keys:
                    raise
                source_spans = None  # An editor supplied explicit keys for a contextual reference.
            if source_spans == []:
                if keys:
                    raise ValueError(
                        f"Chapter-only citation must not request readings: {citation['source_reference']}"
                    )
                citation.update(reference_only=True, note="Chapter-only reference retained without expansion.")
                skipped += 1
                continue
            spans = []
            for key in keys:
                resolved = parse_reference(key)
                if len(resolved) != 1:
                    raise ValueError(f"Each passage key must name one explicit verse span: {key}")
                spans.extend(resolved)
            if not keys:
                spans = source_spans
            citation["passages"] = [span.reference for span in spans]
            citation["reference_only"] = False
            for span in spans:
                requests[span.reference] = span

    # Bound retained API text in this preparation project. No global Bible cache.
    verses = set()
    for span in requests.values():
        verses.update((span.book_id, span.chapter, verse) for verse in range(span.first, span.last + 1))
    for passage in data["passages"].values():
        if isinstance(passage, dict) and passage.get("provider") == "crossway":
            for span in parse_reference(passage["reference"]):
                verses.update((span.book_id, span.chapter, verse) for verse in range(span.first, span.last + 1))
    if len(verses) > 500:
        raise ValueError("This project's retained Crossway passages would exceed 500 verses")
    pending = []
    for key, span in requests.items():
        existing = data["passages"].get(key)
        if reusable(existing, span):
            continue
        if existing is not None:
            raise ValueError(f"Existing passage {key} differs from a verified Crossway record; resolve it before fetching")
        pending.append(span)
    api_key = os.environ.get("ESV_API_KEY", "").strip()
    if pending and not api_key:
        raise ValueError("Set ESV_API_KEY to your Crossway application key before fetch-passages")
    if "\r" in api_key or "\n" in api_key:
        raise ValueError("ESV_API_KEY must be a single-line application key")
    last_request = None
    for span in pending:
        if last_request is not None:
            time.sleep(max(0, 1.05 - (time.monotonic() - last_request)))
        data["passages"][span.reference] = fetch_passage(span, api_key)
        last_request = time.monotonic()
        atomic_write_json(project / "passages.json", data["passages"])
    atomic_write_json(project / "plan.json", data["plan"])
    return {"fetched": len(pending), "reused": len(requests) - len(pending), "chapter_references": skipped}


def _text(value: Any, label: str, required: bool = False) -> str:
    if not isinstance(value, str) or (required and not value.strip()):
        raise ValueError(f"{label} must be {'nonempty ' if required else ''}text")
    return value


def _template(config: dict[str, Any], key: str) -> str:
    raw = _text(config[key], key, required=True)
    variables = config["variables"]
    if not isinstance(variables, dict) or not all(isinstance(v, str) for v in variables.values()):
        raise ValueError("Template variables must be a mapping of names to text")
    try:
        return Template(raw).substitute(variables)
    except (KeyError, ValueError) as exc:
        raise ValueError(f"Invalid or unresolved {key} template: {exc}") from exc


def _fingerprint(data: dict[str, Any], block: dict[str, Any]) -> str:
    return stable_hash({
        "source": data["manifest"]["source_sha256"], "block": block,
        "templates": data["templates"], "passages": data["passages"],
    })


def _requires_approval(source: dict[str, Any], block: dict[str, Any]) -> bool:
    return (source["kind"] == "table" or block["complex"] is True
            or source["text"] != block["narration"])


def _block_parts(data: dict[str, Any], source: dict[str, Any], block: dict[str, Any]) -> list[str]:
    block_id = block["id"]
    narration = _text(block["narration"], f"{block_id} narration", required=True)
    parts = [_text(block["before"], f"{block_id} before")]
    insertions: dict[int, list[str]] = {}
    if not isinstance(block["complex"], bool):
        raise ValueError(f"{block_id}: complex must be true or false")
    if _requires_approval(source, block):
        _text(block["reason"], f"{block_id} change/interpretation reason", required=True)
    if source["kind"] == "table":
        cells = {cell["id"] for cell in source["cells"] if cell["text"].strip()}
        coverage = block["cell_coverage"]
        if not isinstance(coverage, dict) or set(coverage) != cells:
            raise ValueError(f"{block_id}: explain the narration location for every nonempty cell")
        for cell, explanation in coverage.items():
            _text(explanation, f"{block_id} {cell} coverage", required=True)
    seen = []
    for citation in block["citations"]:
        reference = _text(citation["source_reference"], "source_reference", required=True)
        if reference not in source["text"] or reference in seen:
            raise ValueError(f"{block_id}: citation must occur in the source and be unique: {reference}")
        seen.append(reference)
        keys = citation["passages"]
        if not isinstance(keys, list) or not all(isinstance(key, str) for key in keys):
            raise ValueError(f"{block_id}: passages must be a list of passage keys")
        if citation.get("reference_only") is True:
            if (parse_reference(reference) or keys or citation["already_quoted"] is not False
                    or reference not in narration):
                raise ValueError(f"{block_id}: reference_only must retain a chapter-only citation without readings")
            _text(citation["note"], "reference-only note", required=True)
            continue
        if citation["already_quoted"] is True:
            _text(citation["note"], "already-quoted verification note", required=True)
            if keys:
                raise ValueError("Already-quoted citations must not also insert passages")
            continue
        if citation["already_quoted"] is not False or not keys or len(set(keys)) != len(keys):
            raise ValueError(f"{block_id}: resolve {reference} to verified passage keys")
        offset = citation.get("insert_at")
        if offset is None:
            offset = len(narration)
        if type(offset) is not int or not 0 <= offset <= len(narration):
            raise ValueError(f"{block_id}: insert_at must be a character offset in narration")
        if 0 < offset < len(narration) and narration[offset - 1].isalnum() and narration[offset].isalnum():
            raise ValueError(f"{block_id}: insert_at must not split a word")
        for key in keys:
            passage = data["passages"].get(key)
            if not isinstance(passage, dict) or passage.get("translation") != "ESV":
                raise ValueError(f"Missing ESV passage: {key}")
            if passage.get("verified") is not True:
                raise ValueError(f"Passage must be checked against its named source: {key}")
            if passage.get("provider") == "crossway":
                spans = parse_reference(key)
                if len(spans) != 1 or not reusable(passage, spans[0]):
                    raise ValueError(f"Crossway passage content or verse metadata has changed: {key}")
            _text(passage.get("source"), f"{key} provenance", required=True)
            quotation = _text(passage.get("text"), f"{key} quotation", required=True)
            intro = _text(passage.get("intro"), f"{key} explanatory sentence", required=True)
            insertions.setdefault(offset, []).append(f'{intro} “{quotation}”')
    if not set(source["detected_references"]).issubset(seen):
        raise ValueError(f"{block_id}: a detected citation was removed from the inventory")
    cursor = 0
    for offset, readings in sorted(insertions.items()):
        parts.append(narration[cursor:offset].strip())
        parts.extend(readings)
        cursor = offset
    parts.append(narration[cursor:].strip())
    parts.append(_text(block["after"], f"{block_id} after"))
    return [part for part in parts if part.strip()]


def approve(project: Path, block_id: str, reviewer: str) -> None:
    data = read_project(project)
    for source, block in zip(data["manifest"]["blocks"], data["plan"]["blocks"]):
        if block["id"] == block_id:
            _block_parts(data, source, block)
            data["approvals"][block_id] = {
                "reviewer": _text(reviewer, "reviewer", required=True),
                "fingerprint": _fingerprint(data, block),
            }
            atomic_write_json(project / "approvals.json", data["approvals"])
            return
    raise ValueError(f"Unknown block: {block_id}")


def review(project: Path) -> Path:
    data = read_project(project)
    lines = ["# Curriculum narration review", "", "This is a review artifact, not TTS input.", ""]
    for key in ("opening", "closing"):
        lines.extend([f"## {key.title()} template", "", data["templates"][key] or "[NOT PROVIDED]", ""])
        try:
            lines.extend(["Rendered template:", "", _template(data["templates"], key), ""])
        except ValueError as exc:
            lines.extend([f"Pending: {exc}", ""])
    for source, block in zip(data["manifest"]["blocks"], data["plan"]["blocks"]):
        lines.extend([f"## {block['id']} ({source['kind']})", "", "### Source", "", source["text"], ""])
        if _requires_approval(source, block):
            lines.extend(["APPROVAL REQUIRED for this exact proposal.", "", block["reason"], ""])
        lines.extend(["### Proposed narration", "", block["before"], block["narration"], ""])
        if source["text"] != block["narration"]:
            diff = "\n".join(difflib.unified_diff(
                source["text"].splitlines(), block["narration"].splitlines(),
                fromfile="source", tofile="narration", lineterm="",
            ))
            lines.extend(["```diff", diff, "```", ""])
        lines.extend(["### Citations and cell coverage", "", "```json", json.dumps({
            "citations": block["citations"], "cell_coverage": block["cell_coverage"],
        }, ensure_ascii=False, indent=2), "```", "", block["after"], ""])
        try:
            lines.extend(["### Assembled preview (including readings)", "",
                          "\n\n".join(_block_parts(data, source, block)), ""])
        except (ValueError, KeyError, TypeError) as exc:
            lines.extend([f"Assembly pending: {exc}", ""])
    lines.extend(["## Verified-passage input", "", "```json",
                  json.dumps(data["passages"], ensure_ascii=False, indent=2), "```", ""])
    path = project / "review.md"
    atomic_write_text(path, "\n".join(lines))
    return path


def render(project: Path, workload_dir: Path | None = None) -> Path:
    data = read_project(project)
    if data["plan"].get("inventory_checked") is not True:
        raise ValueError("An editor must check the full citation and complex-content inventory first")
    parts = [_template(data["templates"], "opening")]
    for source, block in zip(data["manifest"]["blocks"], data["plan"]["blocks"]):
        block_parts = _block_parts(data, source, block)
        if _requires_approval(source, block):
            approval = data["approvals"].get(block["id"], {})
            if not approval.get("reviewer") or approval.get("fingerprint") != _fingerprint(data, block):
                raise ValueError(f"{block['id']}: exact proposal needs user approval; run review first")
        parts.extend(block_parts)
    parts.append(_template(data["templates"], "closing"))
    text = "\n\n".join(parts).strip() + "\n"
    # The downstream normalizer must not silently rewrite curriculum or Scripture.
    if normalize_text(text) != text:
        raise ValueError(
            "Narration would change during TTS normalization. Resolve whitespace, Unicode, "
            "soft hyphens, or line-break hyphenation in the reviewed plan/passages first."
        )
    from .curriculum_queue import publish

    return publish(project, data, text, workload_dir)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog='Run without arguments to scan curriculum/, or name one file: prepare_curriculum.py "Lesson 2.docx"',
    )
    commands = parser.add_subparsers(dest="command", required=True)
    batch = commands.add_parser("batch", help="scan curriculum/ and export ready documents to workload/")
    batch.add_argument("filename", nargs="?", type=Path, help="process only this DOCX in curriculum/ (name or path)")
    batch.add_argument("--curriculum-dir", type=Path, default=PROJECT_ROOT / "curriculum")
    batch.add_argument("--workload-dir", type=Path, default=PROJECT_ROOT / "workload")
    init = commands.add_parser("init", help="extract DOCX and create an editable preparation plan")
    init.add_argument("source", type=Path)
    init.add_argument("project", type=Path)
    for name in ("review", "render", "approve", "fetch-passages"):
        sub = commands.add_parser(name)
        sub.add_argument("project", type=Path)
        if name == "approve":
            sub.add_argument("--block", required=True)
            sub.add_argument("--reviewer", required=True)
        if name == "render":
            sub.add_argument("--workload-dir", type=Path, help="output directory (default: project-root workload/)")
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and not argv[0].startswith("-") and argv[0] not in {
        "batch", "init", "review", "render", "approve", "fetch-passages",
    }:
        argv.insert(0, "batch")
    args = parser.parse_args(argv or ["batch"])
    try:
        if args.command == "batch":
            from .curriculum_queue import run_batch

            counts = run_batch(args.curriculum_dir.expanduser().resolve(), args.workload_dir.expanduser().resolve(), args.filename)
            print(f"Curriculum: {counts['exported']} exported, {counts['skipped']} already complete, "
                  f"{counts['pending']} awaiting review, {counts['failed']} failed")
            return 1 if counts["failed"] else 0
        project = args.project.expanduser().resolve()
        if args.command == "init":
            initialize(args.source.expanduser().resolve(), project)
            print(f"Prepared {project}; edit the plan using CURRICULUM_LLM_INSTRUCTIONS.md")
        elif args.command == "review":
            print(review(project))
        elif args.command == "approve":
            approve(project, args.block, args.reviewer)
            print(f"Recorded approval for {args.block}")
        elif args.command == "fetch-passages":
            counts = fetch_passages(project)
            print(f"Crossway: {counts['fetched']} fetched, {counts['reused']} reused; "
                  f"{counts['chapter_references']} chapter-only citations retained without expansion")
        else:
            print(render(project, args.workload_dir))
        return 0
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, BadZipFile, ET.ParseError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
