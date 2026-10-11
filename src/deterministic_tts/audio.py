from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import wave
from pathlib import Path


def write_float_wav(path: Path, samples: object, sample_rate: int) -> None:
    try:
        import numpy as np
    except ImportError as exc:
        raise RuntimeError("NumPy is required by the MLX speech backend") from exc

    values = np.asarray(samples, dtype=np.float32).squeeze()
    if values.ndim != 1:
        raise RuntimeError(f"Speech backend returned unexpected audio shape {values.shape}")
    pcm = (np.clip(values, -1.0, 1.0) * 32767.0).astype("<i2")
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(int(sample_rate))
        output.writeframes(pcm.tobytes())


def combine_wavs(paths: list[Path], output: Path, pause_ms: int) -> None:
    if not paths:
        raise RuntimeError("No WAV chunks were produced")
    output.parent.mkdir(parents=True, exist_ok=True)
    expected: tuple[int, int, int, str] | None = None
    with wave.open(str(output), "wb") as destination:
        for index, path in enumerate(paths):
            with wave.open(str(path), "rb") as source:
                current = (
                    source.getnchannels(),
                    source.getsampwidth(),
                    source.getframerate(),
                    source.getcomptype(),
                )
                if current[3] != "NONE":
                    raise RuntimeError(f"Compressed WAV is not supported: {path}")
                if expected is None:
                    expected = current
                    destination.setnchannels(current[0])
                    destination.setsampwidth(current[1])
                    destination.setframerate(current[2])
                    destination.setcomptype("NONE", "not compressed")
                elif current != expected:
                    raise RuntimeError(
                        f"WAV format changed between chunks: {paths[0]} and {path}"
                    )
                if index and pause_ms:
                    frames = round(expected[2] * pause_ms / 1000)
                    destination.writeframes(
                        b"\x00" * frames * expected[0] * expected[1]
                    )
                destination.writeframes(source.readframes(source.getnframes()))


def encode_mp3(wav_path: Path, mp3_path: Path, bitrate: str) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError(
            "FFmpeg is required to create MP3 files. On macOS: brew install ffmpeg"
        )
    mp3_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{mp3_path.stem}.", suffix=".mp3", dir=mp3_path.parent
    )
    os.close(fd)
    temp_path = Path(temp_name)
    try:
        command = [
            ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-fflags",
            "+bitexact",
            "-i",
            str(wav_path),
            "-map_metadata",
            "-1",
            "-vn",
            "-af",
            "loudnorm=I=-18:TP=-1.5:LRA=11",
            "-codec:a",
            "libmp3lame",
            "-b:a",
            bitrate,
            "-id3v2_version",
            "0",
            str(temp_path),
        ]
        completed = subprocess.run(command, capture_output=True, text=True)
        if completed.returncode:
            detail = completed.stderr.strip() or "unknown FFmpeg error"
            raise RuntimeError(f"FFmpeg failed: {detail}")
        os.replace(temp_path, mp3_path)
    finally:
        temp_path.unlink(missing_ok=True)

