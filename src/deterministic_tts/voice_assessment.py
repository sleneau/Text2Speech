from __future__ import annotations

import importlib.metadata
import json
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


PACKAGES = (
    "mlx",
    "mlx-audio",
    "torch",
    "torchaudio",
    "transformers",
    "huggingface-hub",
    "soundfile",
    "numpy",
)


def _package_versions() -> dict[str, str | None]:
    values: dict[str, str | None] = {}
    for name in PACKAGES:
        try:
            values[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            values[name] = None
    return values


def _system_profiler() -> dict[str, Any]:
    executable = shutil.which("system_profiler")
    if not executable or platform.system() != "Darwin":
        return {}
    try:
        completed = subprocess.run(
            [
                executable,
                "SPHardwareDataType",
                "SPDisplaysDataType",
                "-json",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    if completed.returncode:
        return {}
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return {}

    hardware_values = payload.get("SPHardwareDataType", [])
    display_values = payload.get("SPDisplaysDataType", [])
    hardware = hardware_values[0] if hardware_values else {}
    display = display_values[0] if display_values else {}
    cpu_cores = hardware.get("number_processors")
    if isinstance(cpu_cores, str) and cpu_cores.startswith("proc "):
        cpu_cores = cpu_cores.removeprefix("proc ").split(":", 1)[0]
    metal = display.get("spdisplays_metal")
    if metal == "spdisplays_supported":
        metal = "Supported"
    return {
        "model": hardware.get("machine_name") or hardware.get("_name"),
        "model_identifier": hardware.get("machine_model"),
        "chip": hardware.get("chip_type"),
        "cpu_cores": int(cpu_cores) if str(cpu_cores).isdigit() else cpu_cores,
        "memory": hardware.get("physical_memory"),
        "gpu": display.get("sppci_model") or display.get("_name"),
        "gpu_cores": (
            int(display["sppci_cores"])
            if str(display.get("sppci_cores", "")).isdigit()
            else display.get("sppci_cores")
        ),
        "metal": metal,
    }


def _mlx_runtime() -> dict[str, Any]:
    # Run MLX probing out of process. A sandboxed Metal initialization can leave
    # the extension partially imported, making a second import unsafe.
    script = """
import json
try:
    import mlx.core as mx
    try:
        value = {
            "importable": True,
            "default_device": str(mx.default_device()),
            "metal_available": bool(mx.metal.is_available()),
            "error": None,
        }
    except Exception as exc:
        value = {
            "importable": True,
            "default_device": None,
            "metal_available": False,
            "error": str(exc),
        }
except Exception as exc:
    value = {
        "importable": False,
        "default_device": None,
        "metal_available": False,
        "error": str(exc),
    }
print(json.dumps(value))
"""
    try:
        completed = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {
            "importable": False,
            "default_device": None,
            "metal_available": False,
            "error": str(exc),
        }
    if completed.returncode:
        return {
            "importable": False,
            "default_device": None,
            "metal_available": False,
            "error": completed.stderr.strip() or "MLX probe failed",
        }
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError:
        return {
            "importable": False,
            "default_device": None,
            "metal_available": False,
            "error": "MLX probe returned invalid output",
        }


def _cached_models() -> list[str]:
    hub = Path.home() / ".cache/huggingface/hub"
    if not hub.is_dir():
        return []
    return sorted(
        path.name.removeprefix("models--").replace("--", "/")
        for path in hub.glob("models--*")
        if path.is_dir()
    )


def collect_assessment(path: Path | None = None) -> dict[str, Any]:
    target = (path or Path.cwd()).resolve()
    usage = shutil.disk_usage(target)
    return {
        "read_only": True,
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "macos_version": platform.mac_ver()[0] or None,
            "machine": platform.machine(),
        },
        "hardware": _system_profiler(),
        "python": {
            "executable": sys.executable,
            "version": platform.python_version(),
            "supported_for_tool": (3, 11) <= sys.version_info[:2] < (3, 14),
        },
        "packages": _package_versions(),
        "tools": {
            "ffmpeg": shutil.which("ffmpeg"),
            "ffprobe": shutil.which("ffprobe"),
            "xcrun": shutil.which("xcrun"),
        },
        "mlx_runtime": _mlx_runtime(),
        "storage": {
            "path": str(target),
            "free_bytes": usage.free,
            "free_gib": round(usage.free / (1024**3), 1),
        },
        "cached_huggingface_models": _cached_models(),
    }

