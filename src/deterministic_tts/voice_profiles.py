from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from .util import (
    artifact,
    atomic_write_json,
    atomic_write_text,
    load_json,
    sha256_file,
    slugify,
    utc_now,
)

PROFILE_SCHEMA_VERSION = 1
PERMISSION_STATEMENT = (
    "The user attests that they have permission to clone the voice in this recording."
)


def _reference_validation(duration: float, transcript: str) -> dict[str, Any]:
    words = len(re.findall(r"\b[\w’'-]+\b", transcript, flags=re.UNICODE))
    estimated_wpm = words * 60.0 / duration if duration > 0 else 0.0
    warnings: list[str] = []
    if duration > 15.0:
        warnings.append(
            f"Reference audio is {duration:.1f} seconds; a clean 5-15 second clip "
            "is less likely to destabilize voice-clone conditioning."
        )
    if duration < 5.0:
        warnings.append(
            f"Reference audio is {duration:.1f} seconds; 5-15 seconds usually "
            "captures the voice more reliably."
        )
    if words < 3 or estimated_wpm < 60.0 or estimated_wpm > 260.0:
        warnings.append(
            f"The transcript/audio ratio is unusual ({estimated_wpm:.0f} estimated WPM). "
            "Verify that the transcript covers every spoken word in the recording."
        )
    return {
        "duration_seconds": round(duration, 3),
        "transcript_word_count": words,
        "estimated_words_per_minute": round(estimated_wpm, 1),
        "recommended_duration_seconds": {"minimum": 5, "maximum": 15},
        "exact_transcript_required": True,
        "warnings": warnings,
    }


def _ffprobe(audio: Path) -> dict[str, Any]:
    executable = shutil.which("ffprobe")
    if not executable:
        raise RuntimeError("FFprobe is required and normally ships with FFmpeg")
    command = [
        executable,
        "-v",
        "error",
        "-select_streams",
        "a:0",
        "-show_entries",
        "stream=codec_name,sample_rate,channels:format=duration",
        "-of",
        "json",
        str(audio),
    ]
    completed = subprocess.run(command, capture_output=True, text=True)
    if completed.returncode:
        raise RuntimeError(
            f"Could not inspect reference audio: {completed.stderr.strip() or 'FFprobe failed'}"
        )
    try:
        payload = json.loads(completed.stdout)
        stream = payload["streams"][0]
        duration = float(payload["format"]["duration"])
    except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("Reference file does not contain readable audio") from exc
    return {
        "duration_seconds": round(duration, 3),
        "sample_rate": int(stream["sample_rate"]),
        "channels": int(stream["channels"]),
        "codec": stream.get("codec_name"),
    }


def _normalize_reference(source: Path, destination: Path) -> None:
    executable = shutil.which("ffmpeg")
    if not executable:
        raise RuntimeError("FFmpeg is required. On macOS: brew install ffmpeg")
    command = [
        executable,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source),
        "-map_metadata",
        "-1",
        "-vn",
        "-ac",
        "1",
        "-ar",
        "24000",
        "-codec:a",
        "pcm_s16le",
        str(destination),
    ]
    completed = subprocess.run(command, capture_output=True, text=True)
    if completed.returncode:
        raise RuntimeError(
            f"Could not normalize reference audio: {completed.stderr.strip() or 'FFmpeg failed'}"
        )


def prepare_reference(
    name: str,
    audio: Path,
    transcript: str,
    references_dir: Path,
    permission_confirmed: bool,
) -> Path:
    if not permission_confirmed:
        raise RuntimeError(
            "Voice cloning requires --confirm-permission to attest that you may clone this voice"
        )
    audio = audio.expanduser().resolve()
    if not audio.is_file():
        raise RuntimeError(f"Reference audio does not exist: {audio}")
    transcript = " ".join(transcript.split())
    if not transcript:
        raise RuntimeError("The exact transcript of the reference recording is required")
    if len(transcript) > 5000:
        raise RuntimeError("The reference transcript is unexpectedly long")

    probe = _ffprobe(audio)
    duration = float(probe["duration_seconds"])
    if not 3.0 <= duration <= 30.0:
        raise RuntimeError(
            f"Reference audio is {duration:.1f} seconds; provide a clean 3-30 second clip"
        )

    profile_name = slugify(name)
    root = references_dir.expanduser().resolve()
    profile_dir = root / profile_name
    if profile_dir.exists():
        raise RuntimeError(f"Reference profile already exists: {profile_dir}")

    root.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{profile_name}.", dir=root))
    try:
        source_dir = temporary / "source"
        source_dir.mkdir()
        preserved = source_dir / audio.name
        shutil.copyfile(audio, preserved)
        if sha256_file(preserved) != sha256_file(audio):
            raise RuntimeError("Reference audio changed while it was copied")

        normalized = temporary / "reference.wav"
        _normalize_reference(preserved, normalized)
        normalized_probe = _ffprobe(normalized)
        transcript_path = temporary / "transcript.txt"
        atomic_write_text(transcript_path, transcript + "\n")

        manifest = {
            "schema_version": PROFILE_SCHEMA_VERSION,
            "profile_name": profile_name,
            "created_at": utc_now(),
            "permission": {
                "confirmed": True,
                "statement": PERMISSION_STATEMENT,
            },
            "source_origin": str(audio),
            "source_probe": probe,
            "normalized_probe": normalized_probe,
            "normalization": {
                "sample_rate": 24000,
                "channels": 1,
                "codec": "pcm_s16le",
                "metadata_removed": True,
            },
            "validation": _reference_validation(
                float(normalized_probe["duration_seconds"]), transcript
            ),
            "artifacts": {
                "source": artifact(preserved, temporary),
                "reference_audio": artifact(normalized, temporary),
                "transcript": artifact(transcript_path, temporary),
            },
        }
        atomic_write_json(temporary / "profile.json", manifest)
        os.replace(temporary, profile_dir)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return profile_dir


def load_reference_profile(profile_dir: Path) -> dict[str, Any]:
    profile_dir = profile_dir.expanduser().resolve()
    manifest_path = profile_dir / "profile.json"
    manifest = load_json(manifest_path)
    if manifest.get("schema_version") != PROFILE_SCHEMA_VERSION:
        raise RuntimeError(f"Unsupported reference profile schema: {profile_dir}")
    if manifest.get("permission", {}).get("confirmed") is not True:
        raise RuntimeError(f"Reference profile has no permission attestation: {profile_dir}")
    records = manifest.get("artifacts")
    required = {"source", "reference_audio", "transcript"}
    if not isinstance(records, dict) or not required.issubset(records):
        raise RuntimeError(f"Reference profile has an invalid artifact list: {profile_dir}")
    resolved: dict[str, Path] = {}
    for name, record in records.items():
        if not isinstance(record, dict) or not isinstance(record.get("path"), str):
            raise RuntimeError(f"Reference profile has an invalid artifact record: {name}")
        path = (profile_dir / record["path"]).resolve()
        if not path.is_relative_to(profile_dir):
            raise RuntimeError(f"Reference profile contains an unsafe path: {record['path']}")
        if not path.is_file() or sha256_file(path) != record.get("sha256"):
            raise RuntimeError(f"Reference profile artifact is missing or modified: {name}")
        resolved[name] = path
    manifest["profile_dir"] = str(profile_dir)
    manifest["reference_audio_path"] = str(resolved["reference_audio"])
    manifest["transcript_text"] = resolved["transcript"].read_text(
        encoding="utf-8"
    ).strip()
    if not isinstance(manifest.get("validation"), dict):
        manifest["validation"] = _reference_validation(
            float(manifest["normalized_probe"]["duration_seconds"]),
            manifest["transcript_text"],
        )
    return manifest

