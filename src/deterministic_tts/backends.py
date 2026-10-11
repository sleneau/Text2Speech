from __future__ import annotations

import hashlib
import math
import random
import wave
from pathlib import Path
from typing import Any

from .audio import write_float_wav


DEFAULT_MODELS = {
    "kokoro": "mlx-community/Kokoro-82M-bf16",
    "chatterbox": "mlx-community/chatterbox-multilingual-v3",
}


class TestToneBackend:
    """Dependency-free deterministic renderer used only for tests and diagnostics."""

    def render(self, text: str, output: Path, _: dict[str, Any]) -> None:
        sample_rate = 24_000
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        frequency = 180 + int.from_bytes(digest[:2], "big") % 260
        seconds = max(0.4, min(12.0, len(text.strip()) * 0.008))
        frame_count = round(sample_rate * seconds)
        output.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(output), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(sample_rate)
            frames = bytearray()
            for index in range(frame_count):
                # Short ramps prevent clicks at chunk boundaries.
                ramp = min(1.0, index / 480, (frame_count - index) / 480)
                value = int(4500 * ramp * math.sin(2 * math.pi * frequency * index / sample_rate))
                frames.extend(value.to_bytes(2, "little", signed=True))
            wav.writeframes(frames)


class MlxBackend:
    def __init__(self, settings: dict[str, Any]):
        try:
            import mlx.core as mx
            import numpy as np
            from mlx_audio.tts.utils import load_model
        except ImportError as exc:
            raise RuntimeError(
                "MLX-Audio is not installed in this Python environment. "
                "Run: python3.11 -m venv .venv && "
                ".venv/bin/pip install -e '.[local]'"
            ) from exc
        self.mx = mx
        self.np = np
        self.settings = settings
        model_name = settings["model"]
        try:
            self.model = load_model(model_name)
        except Exception as exc:
            raise RuntimeError(
                f"Could not load MLX model {model_name!r}. The first run needs internet "
                f"access to download it. Original error: {exc}"
            ) from exc

    def render(self, text: str, output: Path, settings: dict[str, Any]) -> None:
        seed = int(hashlib.sha256(text.encode("utf-8")).hexdigest()[:8], 16)
        random.seed(seed)
        self.np.random.seed(seed)
        self.mx.random.seed(seed)

        backend = settings["tts_backend"]
        if backend == "kokoro":
            kwargs = {
                "text": text.strip(),
                "voice": settings["voice"],
                "speed": settings["speed"],
                "lang_code": settings["language"],
            }
        else:
            reference = settings.get("reference_audio_project_path")
            if not reference:
                raise RuntimeError(
                    "Chatterbox needs a reference voice recording; use --reference-audio"
                )
            kwargs = {
                "text": text.strip(),
                "ref_audio": str(Path(settings["project_dir"]) / reference),
                "lang_code": settings["language"],
                "exaggeration": settings["exaggeration"],
                "cfg_weight": settings["cfg_weight"],
                "temperature": settings["temperature"],
            }

        try:
            generated = self.model.generate(**kwargs)
            results = [generated] if hasattr(generated, "audio") else list(generated)
        except Exception as exc:
            raise RuntimeError(f"{backend} failed to synthesize a chunk: {exc}") from exc
        if not results:
            raise RuntimeError(f"{backend} returned no audio")
        sample_rates = {int(result.sample_rate) for result in results}
        if len(sample_rates) != 1:
            raise RuntimeError(f"{backend} returned mixed sample rates: {sample_rates}")
        audio = self.np.concatenate(
            [self.np.asarray(result.audio, dtype=self.np.float32).reshape(-1) for result in results]
        )
        write_float_wav(output, audio, sample_rates.pop())


def make_backend(settings: dict[str, Any]) -> TestToneBackend | MlxBackend:
    if settings["tts_backend"] == "test-tone":
        return TestToneBackend()
    return MlxBackend(settings)

