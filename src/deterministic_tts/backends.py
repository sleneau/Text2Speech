from __future__ import annotations

import hashlib
import math
import random
import re
import wave
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .audio import write_float_wav


DEFAULT_MODELS = {
    "kokoro": "mlx-community/Kokoro-82M-bf16",
    "chatterbox": "mlx-community/chatterbox-multilingual-v3",
    "qwen3": "mlx-community/Qwen3-TTS-12Hz-0.6B-Base-bf16",
}

QWEN_AUDIO_TOKENS_PER_SECOND = 12.5
QWEN_DEFAULT_MAX_TOKENS = 4096
QWEN_VALIDATION_POLICY = "qwen-duration-and-token-limit-v1"


def spoken_word_count(text: str) -> int:
    return len(re.findall(r"\b[\w’'-]+\b", text, flags=re.UNICODE))


@dataclass(frozen=True)
class RenderMetrics:
    sample_rate: int
    samples: int
    duration_seconds: float
    word_count: int
    words_per_minute: float | None
    token_count: int | None = None
    max_tokens: int | None = None
    allowed_duration_seconds: float | None = None
    validation_policy: str | None = None

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        for key in ("duration_seconds", "words_per_minute", "allowed_duration_seconds"):
            value = payload.get(key)
            if isinstance(value, float):
                payload[key] = round(value, 3)
        return payload


class SpeechValidationError(RuntimeError):
    def __init__(self, code: str, message: str, metrics: RenderMetrics):
        super().__init__(message)
        self.code = code
        self.metrics = metrics

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": str(self),
            "metrics": self.metrics.as_dict(),
        }


def _metrics(
    text: str,
    sample_rate: int,
    samples: int,
    *,
    token_count: int | None = None,
    max_tokens: int | None = None,
    allowed_duration_seconds: float | None = None,
    validation_policy: str | None = None,
) -> RenderMetrics:
    duration = samples / sample_rate if sample_rate > 0 else 0.0
    words = spoken_word_count(text)
    wpm = words * 60.0 / duration if words and duration > 0 else None
    return RenderMetrics(
        sample_rate=sample_rate,
        samples=samples,
        duration_seconds=duration,
        word_count=words,
        words_per_minute=wpm,
        token_count=token_count,
        max_tokens=max_tokens,
        allowed_duration_seconds=allowed_duration_seconds,
        validation_policy=validation_policy,
    )


def qwen_generation_limits(text: str, settings: dict[str, Any]) -> tuple[int, float]:
    """Return a conservative token ceiling and plausible duration for this text."""
    hard_max = int(settings.get("qwen_max_tokens", QWEN_DEFAULT_MAX_TOKENS))
    minimum_wpm = float(settings.get("qwen_min_wpm", 80.0))
    margin = float(settings.get("qwen_duration_margin_seconds", 20.0))
    floor = float(settings.get("qwen_min_duration_seconds", 30.0))
    words = spoken_word_count(text)
    allowed_seconds = max(floor, words * 60.0 / minimum_wpm + margin)
    estimated_tokens = max(75, math.ceil(allowed_seconds * QWEN_AUDIO_TOKENS_PER_SECOND))
    return min(hard_max, estimated_tokens), allowed_seconds


class TestToneBackend:
    """Dependency-free deterministic renderer used only for tests and diagnostics."""

    def render(self, text: str, output: Path, _: dict[str, Any]) -> RenderMetrics:
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
        return _metrics(text, sample_rate, frame_count)


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
            if settings.get("voice_clone_project"):
                raise RuntimeError(
                    f"Could not load verified local MLX model {model_name!r} while offline. "
                    f"Original error: {exc}"
                ) from exc
            raise RuntimeError(
                f"Could not load MLX model {model_name!r}. The first run needs internet "
                f"access to download it. Original error: {exc}"
            ) from exc

    def render(self, text: str, output: Path, settings: dict[str, Any]) -> RenderMetrics:
        seed_value = text
        if settings.get("tts_backend") == "qwen3":
            seed_value += "\n" + str(settings.get("model_manifest_sha256", ""))
            seed_value += "\n" + str(settings.get("reference_profile_sha256", ""))
        seed = int(hashlib.sha256(seed_value.encode("utf-8")).hexdigest()[:8], 16)
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
        elif backend == "chatterbox":
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
        elif backend == "qwen3":
            reference = settings.get("reference_audio_project_path")
            reference_text = settings.get("reference_text")
            if not reference or not reference_text:
                raise RuntimeError(
                    "Qwen3-TTS needs reference audio and its exact transcript"
                )
            max_tokens, allowed_duration = qwen_generation_limits(text, settings)
            kwargs = {
                "text": text.strip(),
                "ref_audio": str(Path(settings["project_dir"]) / reference),
                "ref_text": reference_text,
                "lang_code": settings["language"],
                "temperature": settings["temperature"],
                "top_k": settings.get("top_k", 50),
                "top_p": settings.get("top_p", 1.0),
                "repetition_penalty": settings.get("repetition_penalty", 1.5),
                "max_tokens": max_tokens,
            }
        else:
            raise RuntimeError(f"Unsupported MLX speech backend: {backend}")

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
        sample_rate = sample_rates.pop()
        token_values = [getattr(result, "token_count", None) for result in results]
        token_count = None
        if token_values and all(isinstance(value, int) for value in token_values):
            token_count = sum(token_values)
        metrics = _metrics(
            text,
            sample_rate,
            int(audio.size),
            token_count=token_count,
            max_tokens=max_tokens if backend == "qwen3" else None,
            allowed_duration_seconds=allowed_duration if backend == "qwen3" else None,
            validation_policy=QWEN_VALIDATION_POLICY if backend == "qwen3" else None,
        )
        if backend == "qwen3":
            token_limit_hit = token_count is not None and token_count >= max_tokens
            duration_limit_hit = metrics.duration_seconds >= (
                max_tokens / QWEN_AUDIO_TOKENS_PER_SECOND
                - 1.0 / QWEN_AUDIO_TOKENS_PER_SECOND
            )
            if token_limit_hit or duration_limit_hit:
                raise SpeechValidationError(
                    "token_limit",
                    "Qwen reached its generated-audio limit without a normal end-of-speech token",
                    metrics,
                )
            if (
                metrics.word_count >= 20
                and metrics.allowed_duration_seconds is not None
                and metrics.duration_seconds > metrics.allowed_duration_seconds
            ):
                raise SpeechValidationError(
                    "implausible_duration",
                    "Qwen produced implausibly long audio for the supplied text",
                    metrics,
                )
        write_float_wav(output, audio, sample_rate)
        return metrics


def make_backend(settings: dict[str, Any]) -> TestToneBackend | MlxBackend:
    if settings["tts_backend"] == "test-tone":
        return TestToneBackend()
    return MlxBackend(settings)

