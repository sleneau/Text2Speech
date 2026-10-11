from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from deterministic_tts.pipeline import ProjectPipeline, create_project
from deterministic_tts.text import chunk_text, normalize_text
from deterministic_tts.util import natural_key, sha256_file


def test_settings() -> dict[str, object]:
    return {
        "tts_backend": "test-tone",
        "ocr_backend": "auto",
        "voice": "af_heart",
        "model": None,
        "language": "a",
        "ocr_language": "en-US",
        "speed": 1.0,
        "chunk_chars": 100,
        "pause_ms": 25,
        "mp3_bitrate": "64k",
        "exaggeration": 0.5,
        "cfg_weight": 0.5,
        "temperature": 0.8,
    }


class TextTests(unittest.TestCase):
    def test_natural_order(self) -> None:
        values = [Path("page10.png"), Path("page2.png"), Path("page1.png")]
        self.assertEqual([item.name for item in sorted(values, key=natural_key)], ["page1.png", "page2.png", "page10.png"])

    def test_normalize_and_chunk_are_stable(self) -> None:
        source = "First hy-\nphenated line.  Second sentence!\r\n\r\n\r\nFinal paragraph."
        normalized = normalize_text(source)
        self.assertIn("hyphenated", normalized)
        self.assertNotIn("\r", normalized)
        self.assertEqual(chunk_text(normalized, 100), chunk_text(normalized, 100))
        self.assertTrue(all(len(item.strip()) <= 100 for item in chunk_text(normalized, 100)))


class PipelineTests(unittest.TestCase):
    def test_end_to_end_resume_and_repair(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "story.txt"
            source.write_text(
                "This is a short first sentence. " * 8 + "\n\nThis is the ending.",
                encoding="utf-8",
            )
            project = create_project(source, root / "projects", "story", test_settings(), None)
            output = ProjectPipeline(project, progress=False).run()
            self.assertTrue(output.is_file())
            first_hash = sha256_file(output)
            ledger = json.loads((project / "ledger.json").read_text(encoding="utf-8"))
            self.assertEqual(ledger["status"], "complete")
            self.assertGreater(len(ledger["chunks"]), 1)
            self.assertEqual(list((project / "audio").rglob("*.wav")), [])
            self.assertEqual(ledger["stages"]["cleanup"]["status"], "complete")
            self.assertTrue(all(item["status"] == "cleaned" for item in ledger["chunks"].values()))
            self.assertEqual(
                [item["path"] for item in ledger["stages"]["assemble"]["artifacts"]],
                ["audio/story.mp3"],
            )

            # A clean resume is a no-op with byte-identical output.
            self.assertEqual(sha256_file(ProjectPipeline(project, progress=False).run()), first_hash)
            self.assertEqual(list((project / "audio").rglob("*.wav")), [])

            # If the only durable audio is lost, chunks are deterministically rerendered.
            output.unlink()
            rebuilt = ProjectPipeline(project, progress=False).run()
            self.assertEqual(sha256_file(rebuilt), first_hash)
            self.assertEqual(list((project / "audio").rglob("*.wav")), [])

    def test_wavs_survive_failure_then_are_cleaned_after_success(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "story.txt"
            source.write_text("A resumable sentence. " * 12, encoding="utf-8")
            project = create_project(source, root / "projects", "resume", test_settings(), None)

            with patch("deterministic_tts.pipeline.encode_mp3", side_effect=RuntimeError("encode stopped")):
                with self.assertRaisesRegex(RuntimeError, "encode stopped"):
                    ProjectPipeline(project, progress=False).run()
            self.assertGreater(len(list((project / "audio/chunks").glob("*.wav"))), 0)

            output = ProjectPipeline(project, progress=False).run()
            self.assertTrue(output.is_file())
            self.assertEqual(list((project / "audio").rglob("*.wav")), [])

    def test_uniquely_renamed_final_mp3_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "story.txt"
            source.write_text("A completed project with a renamed result.", encoding="utf-8")
            project = create_project(source, root / "projects", "renamed", test_settings(), None)
            output = ProjectPipeline(project, progress=False).run()
            renamed = output.with_name("preferred-name.mp3")
            output.rename(renamed)

            resumed = ProjectPipeline(project, progress=False).run()
            self.assertEqual(resumed, renamed)
            self.assertTrue(renamed.is_file())
            self.assertFalse(output.exists())
            self.assertEqual(list((project / "audio").rglob("*.wav")), [])

    def test_modified_preserved_source_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.txt"
            source.write_text("A source that must remain stable.", encoding="utf-8")
            project = create_project(source, root / "projects", "guard", test_settings(), None)
            preserved = next((project / "source").glob("*.txt"))
            preserved.write_text("changed", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "modified"):
                ProjectPipeline(project, progress=False).run()


if __name__ == "__main__":
    unittest.main()

