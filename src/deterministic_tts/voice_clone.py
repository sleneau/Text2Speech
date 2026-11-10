from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from .pipeline import ProjectPipeline, create_project, is_project
from .util import load_json, sha256_file
from .voice_assessment import collect_assessment
from .voice_models import (
    DEFAULT_VOICE_MODEL,
    MODEL_COMPARISON,
    VOICE_MODELS,
    download_voice_model,
    verify_voice_model,
)
from .voice_profiles import load_reference_profile, prepare_reference


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="voice-clone",
        description="Deterministic, resumable, local voice cloning on Apple Silicon.",
        epilog="""safety and offline behavior:
  Assessment and planning are read-only and never contact the network.
  Reference preparation requires an explicit permission attestation.
  Synthesis refuses to download weights and forces supported libraries offline.
  Only download-model can contact Hugging Face, and it requires --confirm-download.
""",
    )
    subparsers = parser.add_subparsers(dest="command")

    assess = subparsers.add_parser("assess", help="read-only local hardware and software report")
    assess.add_argument("--json", action="store_true", help="emit machine-readable JSON")

    plan = subparsers.add_parser("plan", help="read-only model comparison and installation plan")
    plan.add_argument(
        "--model", choices=tuple(VOICE_MODELS), default=DEFAULT_VOICE_MODEL
    )
    plan.add_argument("--models-dir", type=Path, default=Path("models"))
    plan.add_argument("--json", action="store_true", help="emit machine-readable JSON")

    download = subparsers.add_parser(
        "download-model", help="explicitly download and hash a pinned MLX model"
    )
    download.add_argument("model", choices=tuple(VOICE_MODELS))
    download.add_argument("--models-dir", type=Path, default=Path("models"))
    download.add_argument(
        "--confirm-download",
        action="store_true",
        help="confirm that the reviewed network download is authorized",
    )

    prepare = subparsers.add_parser(
        "prepare-reference", help="validate and preserve an authorized voice reference"
    )
    prepare.add_argument("name", help="reference profile name")
    prepare.add_argument("audio", type=Path, help="3-30 second clean recording")
    transcript = prepare.add_mutually_exclusive_group(required=True)
    transcript.add_argument("--transcript", help="exact words spoken in the recording")
    transcript.add_argument("--transcript-file", type=Path, help="UTF-8 transcript file")
    prepare.add_argument(
        "--references-dir", type=Path, default=Path("voice_references")
    )
    prepare.add_argument(
        "--confirm-permission",
        action="store_true",
        help="attest that you have permission to clone this recorded voice",
    )

    synthesize = subparsers.add_parser(
        "synthesize", help="create a new project or resume an existing one"
    )
    synthesize.add_argument("input", type=Path, help="text/PDF/image source or project directory")
    synthesize.add_argument("--reference", type=Path, help="prepared reference profile directory")
    synthesize.add_argument("--project-name", help="new voice project name")
    synthesize.add_argument(
        "--projects-dir", type=Path, default=Path("voice_projects")
    )
    synthesize.add_argument("--models-dir", type=Path, default=Path("models"))
    synthesize.add_argument("--model", choices=tuple(VOICE_MODELS), default=None)
    synthesize.add_argument("--language", default=None, help="Qwen language (default: english)")
    synthesize.add_argument(
        "--chunk-mode", choices=("paragraph", "target"), default=None,
        help="paragraph-first or legacy target-size chunking (default: paragraph)",
    )
    synthesize.add_argument(
        "--chunk-chars", type=int, default=None,
        help="maximum paragraph chunk size, 100-3000 (default: 2500)",
    )
    synthesize.add_argument(
        "--terminal-ellipsis", choices=("period", "preserve"), default=None,
        help="render request-final ellipses as periods or preserve trailing-off delivery (default: period)",
    )
    synthesize.add_argument("--pause-ms", type=int, default=None)
    synthesize.add_argument("--mp3-bitrate", default=None)
    synthesize.add_argument("--temperature", type=float, default=None)
    synthesize.add_argument("--top-k", type=int, default=None)
    synthesize.add_argument("--top-p", type=float, default=None)
    synthesize.add_argument("--repetition-penalty", type=float, default=None)
    synthesize.add_argument("--quiet", action="store_true")

    status = subparsers.add_parser("status", help="show a voice project's ledger")
    status.add_argument("project", type=Path)
    return parser


def _human_bytes(value: int) -> str:
    return f"{value / (1024**3):.1f} GiB"


def _print_assessment(report: dict[str, Any]) -> None:
    hardware = report["hardware"]
    packages = report["packages"]
    mlx = report["mlx_runtime"]
    print("Read-only local voice-cloning assessment")
    print(f"  Computer       : {hardware.get('model') or 'unknown'}")
    print(f"  Chip           : {hardware.get('chip') or 'unknown'}")
    print(f"  CPU / GPU      : {hardware.get('cpu_cores') or '?'} / {hardware.get('gpu_cores') or '?'} cores")
    print(f"  Memory         : {hardware.get('memory') or 'unknown'}")
    print(f"  Metal hardware : {hardware.get('metal') or 'unknown'}")
    print(f"  macOS          : {report['platform'].get('macos_version') or 'unknown'}")
    print(f"  Python         : {report['python']['version']} ({report['python']['executable']})")
    print(f"  MLX            : {packages.get('mlx') or 'not installed'}")
    print(f"  MLX-Audio      : {packages.get('mlx-audio') or 'not installed'}")
    print(f"  FFmpeg         : {report['tools'].get('ffmpeg') or 'not installed'}")
    print(f"  Free storage   : {_human_bytes(report['storage']['free_bytes'])}")
    runtime = "available" if mlx.get("metal_available") else "not available in this process"
    print(f"  MLX Metal      : {runtime}")
    if mlx.get("error"):
        print(f"  Runtime note   : {mlx['error']}")
    cached = report["cached_huggingface_models"]
    print(f"  Cached models  : {', '.join(cached) if cached else 'none'}")


def _plan_payload(model_key: str, models_dir: Path) -> dict[str, Any]:
    spec = VOICE_MODELS[model_key]
    target = spec.local_path(models_dir.expanduser().resolve())
    assessment = collect_assessment()
    packages = assessment["packages"]
    package_actions = []
    if not packages.get("mlx") or not packages.get("mlx-audio"):
        package_actions.append("Install the MLX and MLX-Audio runtime in Python 3.11.")
    if not assessment["tools"].get("ffmpeg"):
        package_actions.append("Install FFmpeg with MP3 support.")
    hardware = assessment["hardware"]
    return {
        "read_only": True,
        "catalog_date": "2026-10-30",
        "hardware_summary": {
            "chip": hardware.get("chip"),
            "gpu_cores": hardware.get("gpu_cores"),
            "memory": hardware.get("memory"),
            "metal": hardware.get("metal"),
            "free_gib": assessment["storage"]["free_gib"],
        },
        "package_actions": package_actions,
        "recommendation_rationale": (
            "The recommended Qwen3-TTS 0.6B BF16 checkpoint uses the installed "
            "MLX-Audio runtime and fits comfortably in this Apple Silicon system's "
            "unified memory while prioritizing published speaker similarity and speed."
        ),
        "recommended_model": VOICE_MODELS[DEFAULT_VOICE_MODEL].as_dict(),
        "selected_model": spec.as_dict(),
        "comparison": MODEL_COMPARISON,
        "local_target": str(target),
        "download_command": (
            f".venv/bin/python voice_clone.py download-model {model_key} --models-dir "
            f"{models_dir} --confirm-download"
        ),
        "steps": [
            "Review model license, expected size, and target directory.",
            "Explicitly authorize download-model; it resolves and pins the repository revision.",
            "The downloader hashes every model file and writes model-manifest.json.",
            "Prepare a permitted reference recording and exact transcript.",
            "Run synthesis; it verifies local weights and forces network libraries offline.",
        ],
    }


def _print_plan(payload: dict[str, Any]) -> None:
    selected = payload["selected_model"]
    print("Read-only installation plan")
    print(f"  Recommended     : {payload['recommended_model']['name']}")
    print(f"  Selected        : {selected['name']}")
    print(f"  Repository      : {selected['repo_id']}")
    print(f"  Approx. download: {selected['approximate_download_gb']:.1f} GB")
    print(f"  License         : {selected['license']}")
    print(f"  Local target    : {payload['local_target']}")
    hardware = payload["hardware_summary"]
    print(
        f"  Hardware fit    : {hardware.get('chip') or 'unknown chip'}, "
        f"{hardware.get('memory') or 'unknown memory'}, "
        f"{hardware.get('gpu_cores') or '?'} GPU cores"
    )
    print(f"  Why             : {payload['recommendation_rationale']}")
    if payload["package_actions"]:
        print("  Prerequisites   : " + " ".join(payload["package_actions"]))
    else:
        print("  Prerequisites   : existing MLX-Audio and FFmpeg stack is sufficient")
    print("\nCurrent model comparison")
    for item in payload["comparison"]:
        print(f"  {item['model']}: {item['recommendation']}")
        print(f"    Runtime: {item['apple_silicon_runtime']}; license: {item['license']}")
        print(f"    Strengths: {item['strengths']}")
        print(f"    Tradeoffs: {item['tradeoffs']}")
    print("\nNo action has been taken. After approval, the explicit command is:")
    print(f"  {payload['download_command']}")


def _transcript_from_args(args: argparse.Namespace) -> str:
    if args.transcript is not None:
        return args.transcript
    path = args.transcript_file.expanduser().resolve()
    if not path.is_file():
        raise RuntimeError(f"Transcript file does not exist: {path}")
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeError("Reference transcript must be UTF-8") from exc


def _voice_settings(
    model_key: str,
    model_path: Path,
    model_manifest: dict[str, Any],
    profile: dict[str, Any],
    args: argparse.Namespace,
) -> dict[str, Any]:
    chunk_mode = args.chunk_mode if args.chunk_mode is not None else "paragraph"
    chunk_chars = args.chunk_chars if args.chunk_chars is not None else 2500
    terminal_ellipsis = (
        args.terminal_ellipsis if args.terminal_ellipsis is not None else "period"
    )
    pause_ms = args.pause_ms if args.pause_ms is not None else 250
    bitrate = args.mp3_bitrate if args.mp3_bitrate is not None else "128k"
    temperature = args.temperature if args.temperature is not None else 0.9
    top_k = args.top_k if args.top_k is not None else 50
    top_p = args.top_p if args.top_p is not None else 1.0
    repetition_penalty = (
        args.repetition_penalty if args.repetition_penalty is not None else 1.5
    )
    if not 100 <= chunk_chars <= 3000:
        raise RuntimeError("--chunk-chars must be between 100 and 3000")
    if not 0 <= pause_ms <= 5000:
        raise RuntimeError("--pause-ms must be between 0 and 5000")
    if not bitrate.endswith("k") or not bitrate[:-1].isdigit():
        raise RuntimeError("--mp3-bitrate must look like 128k or 192k")
    if not 0.0 < temperature <= 2.0:
        raise RuntimeError("--temperature must be greater than 0 and at most 2")
    if not 1 <= top_k <= 1000:
        raise RuntimeError("--top-k must be between 1 and 1000")
    if not 0.0 < top_p <= 1.0:
        raise RuntimeError("--top-p must be greater than 0 and at most 1")
    if not 1.0 <= repetition_penalty <= 3.0:
        raise RuntimeError("--repetition-penalty must be between 1 and 3")

    return {
        "voice_clone_project": True,
        "tts_backend": VOICE_MODELS[model_key].backend,
        "ocr_backend": "auto",
        "pdf_mode": "auto",
        "pdf_dpi": 300,
        "pdf_cleanup": "headers-footers",
        "voice": None,
        "model": str(model_path),
        "model_key": model_key,
        "model_repo_id": model_manifest["repo_id"],
        "model_revision": model_manifest["revision"],
        "model_manifest_sha256": model_manifest["manifest_sha256"],
        "reference_profile": profile["profile_name"],
        "reference_profile_sha256": sha256_file(
            Path(profile["profile_dir"]) / "profile.json"
        ),
        "reference_validation": profile.get("validation", {}),
        "permission_confirmed": True,
        "language": args.language or "english",
        "ocr_language": "en-US",
        "speed": 1.0,
        "chunk_mode": chunk_mode,
        "chunk_chars": chunk_chars,
        "terminal_ellipsis": terminal_ellipsis,
        "pause_ms": pause_ms,
        "mp3_bitrate": bitrate,
        "exaggeration": 0.5,
        "cfg_weight": 0.5,
        "temperature": temperature,
        "top_k": top_k,
        "top_p": top_p,
        "repetition_penalty": repetition_penalty,
        "qwen_max_tokens": 4096,
        "qwen_min_wpm": 80.0,
        "qwen_duration_margin_seconds": 20.0,
        "qwen_min_duration_seconds": 30.0,
        "qwen_fallback_chars": 600,
        "qwen_max_fallback_depth": 3,
        "qwen_fallback_pause_ms": 150,
    }


def _force_offline() -> None:
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["HF_DATASETS_OFFLINE"] = "1"


def _validate_resume_args(args: argparse.Namespace, config: dict[str, Any]) -> None:
    stored = config.get("settings", {})
    if stored.get("voice_clone_project") is not True:
        raise RuntimeError("This is not a voice-clone project")
    option_names = (
        "language",
        "chunk_mode",
        "chunk_chars",
        "terminal_ellipsis",
        "pause_ms",
        "mp3_bitrate",
        "temperature",
        "top_k",
        "top_p",
        "repetition_penalty",
    )
    for name in option_names:
        requested = getattr(args, name)
        if requested is not None and requested != stored.get(name):
            raise RuntimeError(
                f"Project setting {name!r} is frozen as {stored.get(name)!r}, not {requested!r}"
            )
    if args.model is not None and args.model != stored.get("model_key"):
        raise RuntimeError(
            f"Project model is frozen as {stored.get('model_key')!r}, not {args.model!r}"
        )
    if args.reference is not None:
        raise RuntimeError("A reference profile cannot be replaced while resuming")


def _run_synthesis(args: argparse.Namespace) -> Path:
    candidate = args.input.expanduser().resolve()
    if is_project(candidate):
        project = candidate
        config = load_json(project / "project.json")
        _validate_resume_args(args, config)
        settings = config["settings"]
        model_key = settings["model_key"]
        model_path, manifest = verify_voice_model(model_key, args.models_dir)
        if str(model_path) != settings["model"]:
            raise RuntimeError(
                f"Project expects model at {settings['model']}, but --models-dir resolves to {model_path}"
            )
        if manifest["manifest_sha256"] != settings["model_manifest_sha256"]:
            raise RuntimeError("The project's pinned model manifest has changed")
    else:
        if args.reference is None:
            raise RuntimeError("--reference is required when creating a voice project")
        model_key = args.model or DEFAULT_VOICE_MODEL
        model_path, manifest = verify_voice_model(model_key, args.models_dir)
        profile = load_reference_profile(args.reference)
        if not args.quiet:
            for warning in profile.get("validation", {}).get("warnings", []):
                print(f"Reference warning: {warning}")
        settings = _voice_settings(model_key, model_path, manifest, profile, args)
        project = create_project(
            candidate,
            args.projects_dir,
            args.project_name,
            settings,
            Path(profile["reference_audio_path"]),
            profile["transcript_text"],
        )
        print(f"Created voice project: {project}")

    _force_offline()
    return ProjectPipeline(project, progress=not args.quiet).run()


def _print_status(project: Path) -> None:
    project = project.expanduser().resolve()
    if not is_project(project):
        raise RuntimeError(f"Not a resumable project: {project}")
    config = load_json(project / "project.json")
    if config.get("settings", {}).get("voice_clone_project") is not True:
        raise RuntimeError(f"Not a voice-clone project: {project}")
    ledger = load_json(project / "ledger.json")
    print(f"Project : {config['project_name']}")
    print(f"Path    : {project}")
    print(f"Model   : {config['settings']['model_key']} @ {config['settings']['model_revision']}")
    print(f"Voice   : {config['settings']['reference_profile']}")
    print(f"Status  : {ledger.get('status', 'unknown')}")
    for name, stage in ledger.get("stages", {}).items():
        print(f"  {name:10} {stage.get('status', 'unknown')}")
    chunks = ledger.get("chunks", {})
    validation_failures = sum(
        len(item.get("validation_failures", [])) for item in chunks.values()
    )
    fallbacks = sum(bool(item.get("fallback")) for item in chunks.values())
    legacy_unvalidated = sum("metrics" not in item for item in chunks.values())
    if chunks:
        print(f"  speech     {len(chunks)} chunks; {fallbacks} safety fallback(s)")
    if validation_failures:
        print(f"  safety     {validation_failures} rejected generation attempt(s)")
    if legacy_unvalidated:
        print(
            f"  safety     {legacy_unvalidated} legacy chunk(s) predate output validation"
        )
    for warning in config.get("settings", {}).get("reference_validation", {}).get(
        "warnings", []
    ):
        print(f"Reference warning: {warning}")
    if ledger.get("last_error"):
        print(f"Last error: {ledger['last_error'].get('message')}")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    if args.command is None:
        parser.print_help()
        return 0
    try:
        if args.command == "assess":
            report = collect_assessment()
            if args.json:
                print(json.dumps(report, indent=2, sort_keys=True))
            else:
                _print_assessment(report)
        elif args.command == "plan":
            payload = _plan_payload(args.model, args.models_dir)
            if args.json:
                print(json.dumps(payload, indent=2, sort_keys=True))
            else:
                _print_plan(payload)
        elif args.command == "download-model":
            destination = download_voice_model(
                args.model, args.models_dir, args.confirm_download
            )
            print(f"Downloaded and verified model: {destination}")
        elif args.command == "prepare-reference":
            profile = prepare_reference(
                args.name,
                args.audio,
                _transcript_from_args(args),
                args.references_dir,
                args.confirm_permission,
            )
            print(f"Prepared reference profile: {profile}")
            prepared = load_reference_profile(profile)
            for warning in prepared.get("validation", {}).get("warnings", []):
                print(f"Reference warning: {warning}")
        elif args.command == "synthesize":
            _run_synthesis(args)
        elif args.command == "status":
            _print_status(args.project)
        return 0
    except (RuntimeError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

