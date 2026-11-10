"""Default curriculum inbox, reviewed text exports, and completion ledger."""
from __future__ import annotations

import fcntl
import unicodedata
from contextlib import contextmanager
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import BadZipFile

from . import curriculum
from .util import (
    atomic_write_json, atomic_write_text, load_json, natural_key, sha256_file,
    sha256_text, stable_hash,
)


class OutputConflictError(ValueError):
    pass


@contextmanager
def _ledger(directory: Path):
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".ledger.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another curriculum command is updating the ledger; retry when it finishes") from None
        try:
            path = directory / "ledger.json"
            value = load_json(path) if path.exists() else {"version": 1, "documents": {}}
            if value.get("version") != 1 or not isinstance(value.get("documents"), dict):
                raise ValueError("Unsupported curriculum ledger format")
            yield path, value
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _preparation_hash(project: Path) -> str:
    return stable_hash({name: sha256_file(project / name) for name in (
        "source.docx", "manifest.json", "plan.json", "templates.json",
        "passages.json", "approvals.json", "queue.json",
    )})


def _write_output(project: Path, data: dict, text: str, output: Path, previous: dict) -> Path:
    digest = sha256_text(text)
    audit_path = project / "narration.audit.json"
    old_audit = load_json(audit_path) if audit_path.exists() else {}
    if output.is_symlink():
        raise OutputConflictError(f"Refusing to replace a linked output file: {output}")
    if output.exists():
        current_hash = sha256_file(output)
        owned_hashes = {
            record.get("narration_sha256") for record in (previous, old_audit)
            if record.get("output_path") == str(output)
        }
        if current_hash != digest and current_hash not in owned_hashes:
            raise OutputConflictError(f"Output exists and is not an unchanged managed export: {output}")
    atomic_write_text(output, text)
    atomic_write_json(audit_path, {
        "version": 1, "source_sha256": data["manifest"]["source_sha256"],
        "inputs_sha256": stable_hash(data), "narration_sha256": digest,
        "output_path": str(output), "plan": data["plan"], "templates": data["templates"],
        "passages": data["passages"], "approvals": data["approvals"],
    })
    return output


def publish(project: Path, data: dict, text: str, workload_dir: Path | None) -> Path:
    context_path = project / "queue.json"
    context = load_json(context_path) if context_path.exists() else None
    default = Path(context["workload_dir"]) if context else curriculum.PROJECT_ROOT / "workload"
    directory = (workload_dir or default).expanduser().resolve()
    name = data["manifest"]["source_name"]
    if Path(name).name != name:
        raise ValueError("Source filename must not contain a directory")
    output = directory / Path(name).with_suffix(".txt")
    if context is None:
        return _write_output(project, data, text, output, {})
    source = Path(context["source"])
    inbox = Path(context["curriculum_dir"])
    if source.parent != inbox or source.name != name:
        raise ValueError("Preparation queue context does not match its source")
    with _ledger(inbox) as (ledger_path, ledger):
        if sha256_file(source) != data["manifest"]["source_sha256"]:
            raise ValueError("Inbox source has changed since this preparation; rerun the default scan")
        previous = ledger["documents"].get(name, {})
        _write_output(project, data, text, output, previous)
        ledger["documents"][name] = {
            "status": "complete", "source_sha256": data["manifest"]["source_sha256"],
            "preparation": str(project), "preparation_sha256": _preparation_hash(project),
            "output_path": str(output), "narration_sha256": sha256_file(output),
        }
        atomic_write_json(ledger_path, ledger)
    return output


def _record_pending(inbox: Path, source: Path, project: Path, digest: str, status: str, reason: str) -> None:
    with _ledger(inbox) as (path, ledger):
        previous = ledger["documents"].get(source.name, {})
        ledger["documents"][source.name] = {
            **previous, "status": status, "source_sha256": digest,
            "preparation": str(project), "reason": reason,
        }
        atomic_write_json(path, ledger)


def run_batch(inbox: Path, workload: Path, filename: Path | None = None) -> dict[str, int]:
    inbox = inbox.resolve()
    workload = workload.resolve()
    counts = {"exported": 0, "skipped": 0, "pending": 0, "failed": 0}
    selected = None
    if filename is not None:
        if filename.suffix.lower() != ".docx" or filename.name.startswith("~$"):
            raise ValueError("Name a DOCX document; Word lock files cannot be processed")
        filename = filename.expanduser()
        selected = (inbox / filename if filename.parent == Path(".") else filename).resolve()
        if selected.parent != inbox:
            raise ValueError(f"Named document must be directly in {inbox}; use --curriculum-dir for another folder")
        if not selected.is_file():
            raise ValueError(f"Curriculum document not found: {selected}")
    with _ledger(inbox) as (path, ledger):
        if not path.exists():
            atomic_write_json(path, ledger)
    all_sources = sorted((path for path in inbox.iterdir()
                      if path.is_file() and path.suffix.lower() == ".docx"
                      and not path.name.startswith("~$")), key=natural_key)
    names = {}
    for source in all_sources:
        key = unicodedata.normalize("NFC", source.stem).casefold()
        names.setdefault(key, []).append(source.name)
    sources = [path for path in all_sources if path.samefile(selected)] if selected else all_sources
    for source in sources:
        digest = sha256_file(source)
        project = inbox / ".preparation" / source.name / digest
        output = workload / source.with_suffix(".txt").name
        try:
            key = unicodedata.normalize("NFC", source.stem).casefold()
            if len(names[key]) > 1:
                raise OutputConflictError(f"Sources would share the same output filename: {names[key]}")
            with _ledger(inbox) as (_, ledger):
                previous = ledger["documents"].get(source.name, {})
            if (previous.get("status") == "complete" and previous.get("source_sha256") == digest
                    and previous.get("output_path") == str(output) and output.is_file()
                    and not output.is_symlink() and sha256_file(output) == previous.get("narration_sha256")
                    and project.is_dir() and _preparation_hash(project) == previous.get("preparation_sha256")):
                counts["skipped"] += 1
                print(f"Already complete: {source.name}")
                continue
            if not project.exists():
                curriculum.initialize(source, project)
            context = {"source": str(source), "curriculum_dir": str(inbox), "workload_dir": str(workload)}
            context_path = project / "queue.json"
            if not context_path.exists() or load_json(context_path) != context:
                atomic_write_json(context_path, context)
            curriculum.review(project)
            try:
                curriculum.render(project, workload)
            except OutputConflictError:
                raise
            except ValueError as exc:
                _record_pending(inbox, source, project, digest, "awaiting_review", str(exc))
                counts["pending"] += 1
                print(f"Awaiting review: {source.name}: {exc}\n  {project / 'review.md'}")
                continue
            counts["exported"] += 1
            print(f"Exported: {output}")
        except (OSError, ValueError, RuntimeError, KeyError, TypeError, BadZipFile, ET.ParseError) as exc:
            _record_pending(inbox, source, project, digest, "failed", str(exc))
            counts["failed"] += 1
            print(f"Failed: {source.name}: {exc}")
    if not sources:
        print(f"No DOCX files found in {inbox}")
    return counts
