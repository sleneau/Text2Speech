from __future__ import annotations

import json
import os
import tempfile
import unittest
import wave
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from deterministic_tts.backends import (
    MlxBackend,
    SpeechValidationError,
    TestToneBackend,
)
from deterministic_tts.util import artifact, atomic_write_json
from deterministic_tts.voice_clone import _plan_payload, _run_synthesis, _voice_settings, main
from deterministic_tts.voice_models import (
    VOICE_MODELS,
    download_voice_model,
    verify_voice_model,
)
from deterministic_tts.voice_profiles import load_reference_profile, prepare_reference


def make_wav(path: Path, seconds: int = 4) -> None:
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16_000)
        output.writeframes(b"\x00\x00" * 16_000 * seconds)


class VoiceReferenceTests(unittest.TestCase):
    def test_reference_requires_permission_and_is_hash_verified(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            audio = root / "speaker.wav"
            make_wav(audio)
            with self.assertRaisesRegex(RuntimeError, "confirm-permission"):
                prepare_reference(
                    "speaker", audio, "The exact spoken words.", root / "references", False
                )

            profile = prepare_reference(
                "speaker", audio, "The exact spoken words.", root / "references", True
            )
            loaded = load_reference_profile(profile)
            self.assertTrue(loaded["permission"]["confirmed"])
            self.assertEqual(loaded["transcript_text"], "The exact spoken words.")
            self.assertEqual(loaded["normalized_probe"]["sample_rate"], 24_000)
            self.assertEqual(loaded["normalized_probe"]["channels"], 1)
            self.assertTrue(loaded["validation"]["warnings"])

            (profile / "transcript.txt").write_text("modified", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "modified"):
                load_reference_profile(profile)


class VoiceModelTests(unittest.TestCase):
    def test_download_fails_closed_without_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(RuntimeError, "confirm-download"):
                download_voice_model(
                    "qwen3-0.6b-bf16", Path(temporary), download_confirmed=False
                )

    def test_confirmed_download_pins_revision_and_writes_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            def fake_snapshot_download(**kwargs) -> str:
                destination = Path(kwargs["local_dir"])
                (destination / "config.json").write_text("{}\n", encoding="utf-8")
                (destination / "model.safetensors").write_bytes(b"weights")
                return str(destination)

            with (
                patch("huggingface_hub.HfApi") as api,
                patch(
                    "huggingface_hub.snapshot_download",
                    side_effect=fake_snapshot_download,
                ) as snapshot,
            ):
                api.return_value.model_info.return_value.sha = "immutable-revision"
                destination = download_voice_model(
                    "qwen3-0.6b-bf16", root, download_confirmed=True
                )

            manifest = json.loads(
                (destination / "model-manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["revision"], "immutable-revision")
            self.assertEqual(
                snapshot.call_args.kwargs["revision"], "immutable-revision"
            )
            self.assertEqual(len(manifest["files"]), 2)
            verify_voice_model("qwen3-0.6b-bf16", root)

    def test_local_model_manifest_is_verified(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            model_key = "qwen3-0.6b-bf16"
            model_dir = root / model_key
            model_dir.mkdir()
            config = model_dir / "config.json"
            weights = model_dir / "model.safetensors"
            config.write_text("{}\n", encoding="utf-8")
            weights.write_bytes(b"test weights")
            manifest = {
                "schema_version": 1,
                "model_key": model_key,
                "repo_id": VOICE_MODELS[model_key].repo_id,
                "revision": "pinned-test-revision",
                "files": [
                    artifact(config, model_dir),
                    artifact(weights, model_dir),
                ],
            }
            atomic_write_json(model_dir / "model-manifest.json", manifest)
            path, loaded = verify_voice_model(model_key, root)
            self.assertEqual(path, model_dir.resolve())
            self.assertEqual(loaded["revision"], "pinned-test-revision")
            self.assertEqual(len(loaded["manifest_sha256"]), 64)

            weights.write_bytes(b"changed")
            with self.assertRaisesRegex(RuntimeError, "failed verification"):
                verify_voice_model(model_key, root)

    def test_plan_is_read_only_and_explicit(self) -> None:
        payload = _plan_payload("qwen3-0.6b-bf16", Path("models"))
        self.assertTrue(payload["read_only"])
        self.assertIn("--confirm-download", payload["download_command"])
        self.assertEqual(payload["recommended_model"]["key"], "qwen3-0.6b-bf16")
        with redirect_stdout(StringIO()):
            self.assertEqual(main(["plan", "--json"]), 0)


class QwenBackendTests(unittest.TestCase):
    def test_qwen_receives_reference_transcript_and_sampling_settings(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            reference = root / "reference.wav"
            make_wav(reference)
            output = root / "result.wav"
            captured: dict[str, object] = {}

            class Model:
                def generate(self, **kwargs):
                    captured.update(kwargs)
                    return SimpleNamespace(
                        audio=np.zeros(2400, dtype=np.float32), sample_rate=24_000
                    )

            backend = MlxBackend.__new__(MlxBackend)
            backend.np = np
            backend.mx = SimpleNamespace(
                random=SimpleNamespace(seed=lambda _value: None)
            )
            backend.model = Model()
            settings = {
                "tts_backend": "qwen3",
                "project_dir": str(root),
                "reference_audio_project_path": "reference.wav",
                "reference_text": "Reference transcript.",
                "language": "english",
                "temperature": 0.9,
                "top_k": 40,
                "top_p": 0.95,
                "repetition_penalty": 1.5,
            }
            metrics = backend.render("Generated sentence.", output, settings)
            self.assertTrue(output.is_file())
            self.assertEqual(captured["ref_text"], "Reference transcript.")
            self.assertEqual(captured["lang_code"], "english")
            self.assertEqual(captured["top_k"], 40)
            self.assertIn("max_tokens", captured)
            self.assertGreater(metrics.duration_seconds, 0)

    def test_qwen_rejects_a_generation_that_reaches_its_token_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            reference = root / "reference.wav"
            make_wav(reference)
            output = root / "result.wav"

            class Model:
                def generate(self, **kwargs):
                    return SimpleNamespace(
                        audio=np.zeros(800, dtype=np.float32),
                        sample_rate=100,
                        token_count=kwargs["max_tokens"],
                    )

            backend = MlxBackend.__new__(MlxBackend)
            backend.np = np
            backend.mx = SimpleNamespace(random=SimpleNamespace(seed=lambda _value: None))
            backend.model = Model()
            settings = {
                "tts_backend": "qwen3",
                "project_dir": str(root),
                "reference_audio_project_path": "reference.wav",
                "reference_text": "Reference transcript.",
                "language": "english",
                "temperature": 0.9,
                "top_k": 50,
                "top_p": 1.0,
                "repetition_penalty": 1.5,
                "qwen_max_tokens": 100,
            }
            with self.assertRaisesRegex(SpeechValidationError, "end-of-speech") as raised:
                backend.render("A request that should terminate.", output, settings)
            self.assertEqual(raised.exception.code, "token_limit")
            self.assertEqual(raised.exception.metrics.token_count, 100)
            self.assertFalse(output.exists())

    def test_voice_settings_record_permission_and_pins(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            profile_manifest = root / "profile.json"
            profile_manifest.write_text("{}\n", encoding="utf-8")
            profile = {
                "profile_name": "speaker",
                "profile_dir": str(root),
            }
            manifest = {
                "repo_id": VOICE_MODELS["qwen3-0.6b-bf16"].repo_id,
                "revision": "revision",
                "manifest_sha256": "a" * 64,
            }
            args = SimpleNamespace(
                chunk_mode=None,
                chunk_chars=None,
                terminal_ellipsis=None,
                pause_ms=None,
                mp3_bitrate=None,
                temperature=None,
                top_k=None,
                top_p=None,
                repetition_penalty=None,
                language=None,
            )
            settings = _voice_settings(
                "qwen3-0.6b-bf16", root / "model", manifest, profile, args
            )
            self.assertTrue(settings["permission_confirmed"])
            self.assertEqual(settings["model_revision"], "revision")
            self.assertEqual(settings["language"], "english")
            self.assertEqual(settings["chunk_mode"], "paragraph")
            self.assertEqual(settings["chunk_chars"], 2500)
            self.assertEqual(settings["terminal_ellipsis"], "period")
            self.assertEqual(settings["qwen_max_tokens"], 4096)
            self.assertEqual(settings["qwen_fallback_chars"], 600)


class VoiceProjectTests(unittest.TestCase):
    def test_voice_project_is_resumable_and_forces_offline_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            models = root / "models"
            model_key = "qwen3-0.6b-bf16"
            model_dir = models / model_key
            model_dir.mkdir(parents=True)
            config_path = model_dir / "config.json"
            weights_path = model_dir / "model.safetensors"
            config_path.write_text("{}\n", encoding="utf-8")
            weights_path.write_bytes(b"test weights")
            atomic_write_json(
                model_dir / "model-manifest.json",
                {
                    "schema_version": 1,
                    "model_key": model_key,
                    "repo_id": VOICE_MODELS[model_key].repo_id,
                    "revision": "test-revision",
                    "files": [
                        artifact(config_path, model_dir),
                        artifact(weights_path, model_dir),
                    ],
                },
            )

            audio = root / "speaker.wav"
            make_wav(audio)
            profile = prepare_reference(
                "speaker",
                audio,
                "These are the spoken reference words.",
                root / "references",
                True,
            )
            text = root / "story.txt"
            text.write_text("A short generated voice-cloning test...", encoding="utf-8")
            args = SimpleNamespace(
                input=text,
                reference=profile,
                project_name="voice-test",
                projects_dir=root / "projects",
                models_dir=models,
                model=None,
                language=None,
                chunk_mode=None,
                chunk_chars=None,
                terminal_ellipsis=None,
                pause_ms=None,
                mp3_bitrate="64k",
                temperature=None,
                top_k=None,
                top_p=None,
                repetition_penalty=None,
                quiet=True,
            )
            with (
                patch("deterministic_tts.pipeline.make_backend", return_value=TestToneBackend()),
                patch.dict(os.environ, {}, clear=False),
                redirect_stdout(StringIO()),
            ):
                output = _run_synthesis(args)
                self.assertEqual(os.environ["HF_HUB_OFFLINE"], "1")

            self.assertTrue(output.is_file())
            project = root / "projects/voice-test"
            project_config = json.loads(
                (project / "project.json").read_text(encoding="utf-8")
            )
            self.assertTrue(project_config["settings"]["voice_clone_project"])
            self.assertEqual(project_config["settings"]["reference_text"], "These are the spoken reference words.")
            self.assertEqual(
                (project / "text/normalized.txt").read_text(encoding="utf-8"),
                "A short generated voice-cloning test...\n",
            )
            self.assertEqual(
                (project / "text/chunks/0001.txt").read_text(encoding="utf-8"),
                "A short generated voice-cloning test.\n",
            )
            self.assertEqual(list((project / "audio").rglob("*.wav")), [])

            resume_args = SimpleNamespace(**vars(args))
            resume_args.input = project
            resume_args.reference = None
            with patch.dict(os.environ, {}, clear=False):
                resumed = _run_synthesis(resume_args)
            self.assertEqual(resumed, output)


if __name__ == "__main__":
    unittest.main()

