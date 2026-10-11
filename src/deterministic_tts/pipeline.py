from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

from .audio import combine_wavs, encode_mp3
from .backends import make_backend
from .ocr import extract_image_text, resolve_ocr_backend
from .text import chunk_text, decode_text_file, normalize_text
from .util import (
    artifact,
    artifacts_valid,
    atomic_write_json,
    atomic_write_text,
    load_json,
    natural_key,
    sha256_file,
    sha256_text,
    slugify,
    stable_hash,
    utc_now,
)

SCHEMA_VERSION = 1
IMAGE_EXTENSIONS = {".bmp", ".gif", ".heic", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp"}
TEXT_EXTENSIONS = {".csv", ".md", ".rst", ".text", ".txt"}


def is_project(path: Path) -> bool:
    return path.is_dir() and (path / "project.json").is_file() and (path / "ledger.json").is_file()


def _classify_source(path: Path) -> tuple[str, list[Path]]:
    if path.is_file():
        suffix = path.suffix.casefold()
        if suffix in IMAGE_EXTENSIONS:
            return "images", [path]
        if suffix in TEXT_EXTENSIONS:
            return "text", [path]
        raise RuntimeError(
            f"Unsupported input file {path.name!r}. Text: {sorted(TEXT_EXTENSIONS)}; "
            f"images: {sorted(IMAGE_EXTENSIONS)}"
        )
    if path.is_dir():
        images = sorted(
            (item for item in path.iterdir() if item.is_file() and item.suffix.casefold() in IMAGE_EXTENSIONS),
            key=natural_key,
        )
        if not images:
            raise RuntimeError(f"No supported image files were found directly inside {path}")
        return "images", images
    raise RuntimeError(f"Input does not exist: {path}")


def _safe_copy(source: Path, destination: Path, expected_hash: str) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.copying")
    shutil.copyfile(source, temporary)
    if sha256_file(temporary) != expected_hash:
        temporary.unlink(missing_ok=True)
        raise RuntimeError(f"Source changed while it was being copied: {source}")
    os.replace(temporary, destination)


def create_project(
    source: Path,
    projects_dir: Path,
    project_name: str | None,
    settings: dict[str, Any],
    reference_audio: Path | None,
) -> Path:
    source = source.expanduser().resolve()
    source_type, source_paths = _classify_source(source)
    name = slugify(project_name or source.stem or source.name)
    project_dir = projects_dir.expanduser().resolve() / name
    if project_dir.exists():
        raise RuntimeError(
            f"Project already exists: {project_dir}. Pass that project directory as the input to resume it, "
            "or choose a different --project-name."
        )

    if source_type == "images":
        settings["ocr_backend"] = resolve_ocr_backend(settings["ocr_backend"])
    if settings["tts_backend"] == "chatterbox" and reference_audio is None:
        raise RuntimeError("--reference-audio is required when --tts-backend chatterbox is selected")

    records: list[dict[str, Any]] = []
    for index, origin in enumerate(source_paths, 1):
        destination_name = f"{index:04d}--{origin.name}"
        records.append(
            {
                "kind": "image" if source_type == "images" else "text",
                "order": index,
                "origin": str(origin),
                "path": f"source/{destination_name}",
                "sha256": sha256_file(origin),
                "bytes": origin.stat().st_size,
            }
        )

    reference_record: dict[str, Any] | None = None
    if reference_audio is not None:
        reference_audio = reference_audio.expanduser().resolve()
        if not reference_audio.is_file():
            raise RuntimeError(f"Reference audio does not exist: {reference_audio}")
        reference_record = {
            "kind": "reference_audio",
            "origin": str(reference_audio),
            "path": f"source/reference--{reference_audio.name}",
            "sha256": sha256_file(reference_audio),
            "bytes": reference_audio.stat().st_size,
        }
        settings["reference_audio_project_path"] = reference_record["path"]

    project_dir.mkdir(parents=True)
    for relative in ("source", "text/pages", "text/chunks", "audio/chunks", ".cache"):
        (project_dir / relative).mkdir(parents=True, exist_ok=True)

    config = {
        "schema_version": SCHEMA_VERSION,
        "project_name": name,
        "source_type": source_type,
        "source_origin": str(source),
        "created_at": utc_now(),
        "sources": records,
        "reference_audio": reference_record,
        "settings": settings,
    }
    ledger = {
        "schema_version": SCHEMA_VERSION,
        "project_name": name,
        "status": "in_progress",
        "created_at": utc_now(),
        "updated_at": utc_now(),
        "stages": {"ingest": {"status": "in_progress", "artifacts": []}},
        "chunks": {},
        "last_error": None,
    }
    atomic_write_json(project_dir / "project.json", config)
    atomic_write_json(project_dir / "ledger.json", ledger)

    all_records = records + ([reference_record] if reference_record else [])
    copied: list[dict[str, Any]] = []
    for record in all_records:
        assert record is not None
        _safe_copy(Path(record["origin"]), project_dir / record["path"], record["sha256"])
        copied.append(artifact(project_dir / record["path"], project_dir))
        ledger["stages"]["ingest"]["artifacts"] = copied
        ledger["updated_at"] = utc_now()
        atomic_write_json(project_dir / "ledger.json", ledger)
    ledger["stages"]["ingest"]["status"] = "complete"
    ledger["stages"]["ingest"]["completed_at"] = utc_now()
    atomic_write_json(project_dir / "ledger.json", ledger)
    return project_dir


class ProjectPipeline:
    def __init__(self, project_dir: Path, progress: bool = True):
        self.project_dir = project_dir.expanduser().resolve()
        if not is_project(self.project_dir):
            raise RuntimeError(f"Not a resumable project directory: {self.project_dir}")
        self.config = load_json(self.project_dir / "project.json")
        self.ledger = load_json(self.project_dir / "ledger.json")
        if self.config.get("schema_version") != SCHEMA_VERSION:
            raise RuntimeError("Unsupported project schema version")
        self.settings = dict(self.config["settings"])
        self.settings["project_dir"] = str(self.project_dir)
        self.progress = progress

    def _say(self, message: str) -> None:
        if self.progress:
            print(message, flush=True)

    def _save(self) -> None:
        self.ledger["updated_at"] = utc_now()
        atomic_write_json(self.project_dir / "ledger.json", self.ledger)

    def _stage(self, name: str, status: str, **values: Any) -> None:
        record = self.ledger.setdefault("stages", {}).setdefault(name, {})
        record.update(values)
        record["status"] = status
        if status == "complete":
            record["completed_at"] = utc_now()
        self._save()

    def _verify_ingest(self) -> None:
        records = list(self.config["sources"])
        if self.config.get("reference_audio"):
            records.append(self.config["reference_audio"])
        copied = []
        for record in records:
            destination = self.project_dir / record["path"]
            if not destination.exists():
                origin = Path(record["origin"])
                if not origin.is_file() or sha256_file(origin) != record["sha256"]:
                    raise RuntimeError(
                        f"Project source is missing and its original cannot be recovered: {destination}"
                    )
                _safe_copy(origin, destination, record["sha256"])
            if sha256_file(destination) != record["sha256"]:
                raise RuntimeError(
                    f"A preserved project source was modified: {destination}. "
                    "Restore it or start a new project."
                )
            copied.append(artifact(destination, self.project_dir))
        self._stage("ingest", "complete", artifacts=copied)

    def _extract(self) -> Path:
        stage = self.ledger.get("stages", {}).get("extract", {})
        if stage.get("status") == "complete" and artifacts_valid(stage.get("artifacts", []), self.project_dir):
            self._say("[skip] text extraction is already complete")
            return self.project_dir / "text/extracted.txt"

        self._say("[run ] extracting text")
        self._stage("extract", "in_progress", pages=stage.get("pages", {}))
        page_records = self.ledger["stages"]["extract"].setdefault("pages", {})
        page_paths: list[Path] = []
        for index, source_record in enumerate(self.config["sources"], 1):
            page_id = f"{index:04d}"
            page_path = self.project_dir / f"text/pages/{page_id}.txt"
            prior = page_records.get(page_id, {})
            valid = (
                prior.get("source_sha256") == source_record["sha256"]
                and artifacts_valid([prior.get("artifact", {})], self.project_dir)
            )
            if not valid:
                source_path = self.project_dir / source_record["path"]
                if source_record["kind"] == "text":
                    value = decode_text_file(source_path)
                else:
                    self._say(f"       OCR page {index}/{len(self.config['sources'])}: {source_path.name}")
                    value = extract_image_text(
                        source_path,
                        self.settings["ocr_backend"],
                        self.settings["ocr_language"],
                        self.project_dir / ".cache",
                    )
                atomic_write_text(page_path, value.strip() + "\n")
                page_records[page_id] = {
                    "source_sha256": source_record["sha256"],
                    "artifact": artifact(page_path, self.project_dir),
                }
                self._save()
            page_paths.append(page_path)

        combined = "\n\n".join(path.read_text(encoding="utf-8").strip() for path in page_paths).strip() + "\n"
        extracted = self.project_dir / "text/extracted.txt"
        atomic_write_text(extracted, combined)
        artifacts = [artifact(path, self.project_dir) for path in page_paths]
        artifacts.append(artifact(extracted, self.project_dir))
        self._stage("extract", "complete", artifacts=artifacts, pages=page_records)
        return extracted

    def _normalize(self, extracted: Path) -> Path:
        stage = self.ledger.get("stages", {}).get("normalize", {})
        input_hash = sha256_file(extracted)
        if (
            stage.get("status") == "complete"
            and stage.get("input_sha256") == input_hash
            and artifacts_valid(stage.get("artifacts", []), self.project_dir)
        ):
            self._say("[skip] text normalization is already complete")
            return self.project_dir / "text/normalized.txt"
        self._say("[run ] normalizing text")
        self._stage("normalize", "in_progress", input_sha256=input_hash)
        normalized = self.project_dir / "text/normalized.txt"
        atomic_write_text(normalized, normalize_text(extracted.read_text(encoding="utf-8")))
        self._stage(
            "normalize", "complete", input_sha256=input_hash, artifacts=[artifact(normalized, self.project_dir)]
        )
        return normalized

    def _plan_chunks(self, normalized: Path) -> list[Path]:
        stage = self.ledger.get("stages", {}).get("chunk", {})
        fingerprint = stable_hash(
            {"text": sha256_file(normalized), "chunk_chars": self.settings["chunk_chars"]}
        )
        if (
            stage.get("status") == "complete"
            and stage.get("input_fingerprint") == fingerprint
            and artifacts_valid(stage.get("artifacts", []), self.project_dir)
        ):
            self._say("[skip] chunk plan is already complete")
            return [self.project_dir / item["path"] for item in stage["artifacts"]]

        self._say("[run ] planning speech chunks")
        self._stage("chunk", "in_progress", input_fingerprint=fingerprint)
        values = chunk_text(normalized.read_text(encoding="utf-8"), self.settings["chunk_chars"])
        if not values:
            raise RuntimeError("The extracted text is empty; there is nothing to synthesize")
        paths: list[Path] = []
        for index, value in enumerate(values, 1):
            path = self.project_dir / f"text/chunks/{index:04d}.txt"
            atomic_write_text(path, value)
            paths.append(path)
        records = [artifact(path, self.project_dir) for path in paths]
        self._stage("chunk", "complete", input_fingerprint=fingerprint, artifacts=records)
        return paths

    def _render(self, chunk_paths: list[Path]) -> list[Path]:
        synthesis_settings = {
            key: self.settings.get(key)
            for key in (
                "tts_backend", "model", "voice", "language", "speed",
                "reference_audio_project_path", "exaggeration", "cfg_weight", "temperature",
            )
        }
        settings_hash = stable_hash(synthesis_settings)
        wav_paths: list[Path] = []
        pending: list[tuple[int, Path, Path, str]] = []
        chunk_ledger = self.ledger.setdefault("chunks", {})
        for index, chunk_path in enumerate(chunk_paths, 1):
            chunk_id = f"{index:04d}"
            wav_path = self.project_dir / f"audio/chunks/{chunk_id}.wav"
            text_hash = sha256_file(chunk_path)
            prior = chunk_ledger.get(chunk_id, {})
            valid = (
                prior.get("text_sha256") == text_hash
                and prior.get("settings_sha256") == settings_hash
                and prior.get("status") == "complete"
                and artifacts_valid([prior.get("artifact", {})], self.project_dir)
            )
            if not valid:
                pending.append((index, chunk_path, wav_path, text_hash))
            wav_paths.append(wav_path)

        if not pending:
            self._say("[skip] all speech chunks are already rendered")
            self._stage("render", "complete", settings_sha256=settings_hash)
            return wav_paths

        self._say(f"[run ] rendering {len(pending)} of {len(chunk_paths)} speech chunks")
        self._stage("render", "in_progress", settings_sha256=settings_hash)
        backend = make_backend(self.settings)
        for index, chunk_path, wav_path, text_hash in pending:
            self._say(f"       speech chunk {index}/{len(chunk_paths)}")
            temporary = wav_path.with_name(f".{wav_path.name}.rendering.wav")
            temporary.unlink(missing_ok=True)
            backend.render(chunk_path.read_text(encoding="utf-8"), temporary, self.settings)
            os.replace(temporary, wav_path)
            chunk_id = f"{index:04d}"
            chunk_ledger[chunk_id] = {
                "status": "complete",
                "text_sha256": text_hash,
                "settings_sha256": settings_hash,
                "artifact": artifact(wav_path, self.project_dir),
                "completed_at": utc_now(),
            }
            self._save()
        self._stage("render", "complete", settings_sha256=settings_hash)
        return wav_paths

    def _assemble(self, wav_paths: list[Path]) -> Path:
        output = self.project_dir / "audio" / f"{self.config['project_name']}.mp3"
        fingerprint = stable_hash(
            {
                "wav_sha256": [sha256_file(path) for path in wav_paths],
                "pause_ms": self.settings["pause_ms"],
                "mp3_bitrate": self.settings["mp3_bitrate"],
                "normalization": "ffmpeg-loudnorm--18LUFS-v1",
            }
        )
        stage = self.ledger.get("stages", {}).get("assemble", {})
        if (
            stage.get("status") == "complete"
            and stage.get("input_fingerprint") == fingerprint
            and artifacts_valid(stage.get("artifacts", []), self.project_dir)
        ):
            self._say("[skip] final MP3 is already assembled")
            return output

        self._say("[run ] assembling and normalizing final MP3")
        self._stage("assemble", "in_progress", input_fingerprint=fingerprint)
        combined = self.project_dir / "audio/combined.wav"
        temporary = self.project_dir / "audio/.combined.rendering.wav"
        combine_wavs(wav_paths, temporary, self.settings["pause_ms"])
        os.replace(temporary, combined)
        encode_mp3(combined, output, self.settings["mp3_bitrate"])
        self._stage(
            "assemble",
            "complete",
            input_fingerprint=fingerprint,
            artifacts=[artifact(combined, self.project_dir), artifact(output, self.project_dir)],
        )
        return output

    def _valid_assembled_output(self) -> Path | None:
        """Return the verified MP3, including a uniquely renamed final file."""
        stage = self.ledger.get("stages", {}).get("assemble", {})
        if stage.get("status") != "complete":
            return None
        mp3_records = [
            record
            for record in stage.get("artifacts", [])
            if Path(record.get("path", "")).suffix.casefold() == ".mp3"
        ]
        if len(mp3_records) != 1:
            return None
        record = mp3_records[0]
        recorded_path = self.project_dir / record["path"]
        if recorded_path.is_file() and sha256_file(recorded_path) == record.get("sha256"):
            return recorded_path

        # A user may rename the one final MP3 after generation. Adopt it only when
        # its content exactly matches the ledger, so an unrelated MP3 is never used.
        matches = [
            candidate
            for candidate in (self.project_dir / "audio").glob("*.mp3")
            if candidate.is_file() and sha256_file(candidate) == record.get("sha256")
        ]
        return matches[0] if len(matches) == 1 else None

    def _cleanup_working_audio(self, output: Path) -> None:
        """Delete generated WAV checkpoints only after the final MP3 is verified."""
        output_record = artifact(output, self.project_dir)
        wav_paths = sorted(
            path
            for path in (self.project_dir / "audio").rglob("*.wav")
            if path.is_file()
        )
        removed_paths = [path.relative_to(self.project_dir).as_posix() for path in wav_paths]
        removed_set = set(removed_paths)
        for path in wav_paths:
            path.unlink()

        remaining = [path for path in (self.project_dir / "audio").rglob("*.wav") if path.is_file()]
        if remaining:
            raise RuntimeError(
                "Could not remove all working WAV files: "
                + ", ".join(str(path) for path in remaining)
            )

        cleaned_at = utc_now()
        for record in self.ledger.get("chunks", {}).values():
            wav_artifact = record.get("artifact")
            if not isinstance(wav_artifact, dict) or wav_artifact.get("path") not in removed_set:
                continue
            record["rendered_sha256"] = wav_artifact.get("sha256")
            record["rendered_bytes"] = wav_artifact.get("bytes")
            record.pop("artifact", None)
            record["status"] = "cleaned"
            record["cleaned_at"] = cleaned_at

        # Once the working files are gone, the MP3 is the sole durable audio artifact.
        assemble = self.ledger.setdefault("stages", {}).setdefault("assemble", {})
        assemble["artifacts"] = [output_record]
        assemble["working_wavs_removed"] = True
        self._stage(
            "cleanup",
            "complete",
            removed_count=len(removed_paths),
            removed_paths=removed_paths,
            artifacts=[output_record],
        )
        if removed_paths:
            self._say(f"[run ] removed {len(removed_paths)} working WAV files")
        else:
            self._say("[skip] no working WAV files remain")

    def run(self) -> Path:
        self.ledger["status"] = "in_progress"
        self.ledger["last_error"] = None
        self._save()
        try:
            self._verify_ingest()
            completed_output = self._valid_assembled_output()
            if completed_output is not None:
                self._say("[skip] verified completed MP3")
                self._cleanup_working_audio(completed_output)
                self.ledger["status"] = "complete"
                self.ledger["last_error"] = None
                self._save()
                self._say(f"[done] {completed_output}")
                return completed_output
            extracted = self._extract()
            normalized = self._normalize(extracted)
            chunks = self._plan_chunks(normalized)
            wavs = self._render(chunks)
            output = self._assemble(wavs)
            self._cleanup_working_audio(output)
        except Exception as exc:
            self.ledger["status"] = "interrupted"
            self.ledger["last_error"] = {"at": utc_now(), "message": str(exc)}
            self._save()
            raise
        self.ledger["status"] = "complete"
        self.ledger["last_error"] = None
        self._save()
        self._say(f"[done] {output}")
        return output

