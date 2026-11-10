from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .util import artifact, atomic_write_json, load_json, sha256_file, utc_now


@dataclass(frozen=True)
class VoiceModel:
    key: str
    name: str
    repo_id: str
    backend: str
    approximate_download_gb: float
    license: str
    languages: str
    notes: str

    def local_path(self, models_dir: Path) -> Path:
        return models_dir / self.key

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


VOICE_MODELS = {
    "qwen3-0.6b-bf16": VoiceModel(
        key="qwen3-0.6b-bf16",
        name="Qwen3-TTS 12Hz 0.6B Base BF16",
        repo_id="mlx-community/Qwen3-TTS-12Hz-0.6B-Base-bf16",
        backend="qwen3",
        approximate_download_gb=2.6,
        license="Apache-2.0",
        languages="10 languages",
        notes="Recommended balance of resemblance, naturalness, and MLX speed.",
    ),
    "qwen3-1.7b-bf16": VoiceModel(
        key="qwen3-1.7b-bf16",
        name="Qwen3-TTS 12Hz 1.7B Base BF16",
        repo_id="mlx-community/Qwen3-TTS-12Hz-1.7B-Base-bf16",
        backend="qwen3",
        approximate_download_gb=4.6,
        license="Apache-2.0",
        languages="10 languages",
        notes="Larger quality challenger; expected to infer more slowly.",
    ),
}

DEFAULT_VOICE_MODEL = "qwen3-0.6b-bf16"
MODEL_MANIFEST_NAME = "model-manifest.json"


MODEL_COMPARISON = [
    {
        "model": "Qwen3-TTS 12Hz Base",
        "apple_silicon_runtime": "Native MLX-Audio",
        "voice_clone_input": "Reference audio plus exact transcript",
        "strengths": "Strong resemblance, natural speech, multilingual, streaming",
        "tradeoffs": "Best cloning mode requires an accurate transcript",
        "license": "Apache-2.0",
        "recommendation": "Recommended",
    },
    {
        "model": "Chatterbox Multilingual V3",
        "apple_silicon_runtime": "Native MLX-Audio",
        "voice_clone_input": "Reference audio",
        "strengths": "Expressive controls, 23 languages, simple cloning workflow",
        "tradeoffs": "Extra S3Tokenizer weights; generated audio is watermarked",
        "license": "MIT",
        "recommendation": "Runner-up / later A-B test",
    },
    {
        "model": "F5-TTS v1 Base",
        "apple_silicon_runtime": "PyTorch MPS",
        "voice_clone_input": "Reference audio plus transcript (or local ASR)",
        "strengths": "Established flow-matching model, multi-style synthesis",
        "tradeoffs": "No native path in the installed MLX stack; weights are noncommercial",
        "license": "Code MIT; official weights CC-BY-NC-4.0",
        "recommendation": "Not the default for this Mac",
    },
]


def download_voice_model(
    model_key: str,
    models_dir: Path,
    download_confirmed: bool,
) -> Path:
    if not download_confirmed:
        raise RuntimeError(
            "Model download requires --confirm-download after reviewing `voice-clone plan`"
        )
    try:
        from huggingface_hub import HfApi, snapshot_download
    except ImportError as exc:
        raise RuntimeError("huggingface-hub is not installed") from exc

    spec = VOICE_MODELS[model_key]
    root = models_dir.expanduser().resolve()
    destination = spec.local_path(root)
    if destination.exists():
        raise RuntimeError(f"Model directory already exists: {destination}")
    root.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{model_key}.", dir=root))
    try:
        revision = HfApi().model_info(spec.repo_id).sha
        if not revision:
            raise RuntimeError(f"Could not resolve a pinned revision for {spec.repo_id}")
        snapshot_download(
            repo_id=spec.repo_id,
            revision=revision,
            local_dir=temporary,
        )
        for path in temporary.rglob("*"):
            if not path.is_symlink():
                continue
            target = path.resolve(strict=True)
            materialized = path.with_name(f".{path.name}.materializing")
            shutil.copyfile(target, materialized)
            os.replace(materialized, path)
        files = sorted(
            path
            for path in temporary.rglob("*")
            if path.is_file()
            and MODEL_MANIFEST_NAME not in path.parts
            and ".cache" not in path.parts
        )
        if not any(path.name == "config.json" for path in files):
            raise RuntimeError("Downloaded model does not contain config.json")
        if not any(path.suffix == ".safetensors" for path in files):
            raise RuntimeError("Downloaded model contains no SafeTensors weights")
        manifest = {
            "schema_version": 1,
            "created_at": utc_now(),
            "model_key": model_key,
            "name": spec.name,
            "repo_id": spec.repo_id,
            "revision": revision,
            "license": spec.license,
            "files": [artifact(path, temporary) for path in files],
        }
        atomic_write_json(temporary / MODEL_MANIFEST_NAME, manifest)
        cache_dir = temporary / ".cache"
        if cache_dir.exists():
            shutil.rmtree(cache_dir)
        os.replace(temporary, destination)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return destination


def verify_voice_model(model_key: str, models_dir: Path) -> tuple[Path, dict[str, Any]]:
    spec = VOICE_MODELS[model_key]
    model_dir = spec.local_path(models_dir.expanduser().resolve())
    manifest_path = model_dir / MODEL_MANIFEST_NAME
    if not manifest_path.is_file():
        raise RuntimeError(
            f"Verified local model not found: {model_dir}. Review `voice-clone plan`, "
            "then explicitly run `.venv/bin/python voice_clone.py download-model "
            f"{model_key} --confirm-download`."
        )
    manifest = load_json(manifest_path)
    if manifest.get("model_key") != model_key or manifest.get("repo_id") != spec.repo_id:
        raise RuntimeError(f"Model manifest does not match {model_key}: {manifest_path}")
    records = manifest.get("files")
    if not isinstance(records, list) or not records:
        raise RuntimeError(f"Model manifest has no files: {manifest_path}")
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get("path"), str):
            raise RuntimeError(f"Model manifest contains an invalid file record: {manifest_path}")
        path = (model_dir / record["path"]).resolve()
        if not path.is_relative_to(model_dir):
            raise RuntimeError(f"Model manifest contains an unsafe path: {record['path']}")
        if not path.is_file():
            raise RuntimeError(f"Model file is missing: {path}")
        if path.stat().st_size != record.get("bytes") or sha256_file(path) != record.get("sha256"):
            raise RuntimeError(f"Model file failed verification: {path}")
    manifest["manifest_sha256"] = sha256_file(manifest_path)
    return model_dir, manifest

