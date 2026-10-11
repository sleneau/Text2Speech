from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from deterministic_tts.pdf import clean_pdf_body_pages
from deterministic_tts.pipeline import ProjectPipeline, create_project
from deterministic_tts.text import chunk_text, normalize_text
from deterministic_tts.util import natural_key, sha256_file


def test_settings() -> dict[str, object]:
    return {
        "tts_backend": "test-tone",
        "ocr_backend": "auto",
        "pdf_mode": "auto",
        "pdf_dpi": 300,
        "pdf_cleanup": "headers-footers",
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
    def test_pdf_margin_cleanup_removes_repeated_lines_and_page_numbers(self) -> None:
        pages = [
            "Example Book\nFirst unique body paragraph.\nConfidential\n1\n",
            "Example Book\nSecond unique body paragraph.\nConfidential\n2\n",
            "Example Book\nThird unique body paragraph.\nConfidential\n3\n",
        ]
        cleaned, removals = clean_pdf_body_pages(pages)
        self.assertEqual(
            cleaned,
            [
                "First unique body paragraph.\n",
                "Second unique body paragraph.\n",
                "Third unique body paragraph.\n",
            ],
        )
        self.assertEqual(len(removals), 9)
        self.assertEqual(
            {item["reason"] for item in removals},
            {"repeated_header", "repeated_footer", "page_number"},
        )

    def test_pdf_margin_cleanup_keeps_ambiguous_roman_heading(self) -> None:
        cleaned, removals = clean_pdf_body_pages(["Part I\n"])
        self.assertEqual(cleaned, ["Part I\n"])
        self.assertEqual(removals, [])

        cleaned, removals = clean_pdf_body_pages(["A short body line.\n12\n"])
        self.assertEqual(cleaned, ["A short body line.\n"])
        self.assertEqual(removals[0]["reason"], "page_number")

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
    def test_pdf_embedded_text_resumes_at_first_unfinished_page(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "book.pdf"
            source.write_bytes(b"%PDF-1.7 fake test fixture")
            settings = test_settings()
            settings["pdf_mode"] = "text"
            project = create_project(source, root / "projects", "pdf-text", settings, None)

            with (
                patch("deterministic_tts.pipeline.pdf_info", return_value={"page_count": 2}),
                patch(
                    "deterministic_tts.pipeline.pdf_page_text",
                    side_effect=["Embedded text from page one.", RuntimeError("stopped")],
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "stopped"):
                    ProjectPipeline(project, progress=False).run()

            self.assertEqual(
                (project / "text/pages/0001.txt").read_text(encoding="utf-8"),
                "Embedded text from page one.\n",
            )
            with (
                patch("deterministic_tts.pipeline.pdf_info", return_value={"page_count": 2}),
                patch(
                    "deterministic_tts.pipeline.pdf_page_text",
                    return_value="Embedded text from page two.",
                ) as extract_page,
            ):
                output = ProjectPipeline(project, progress=False).run()

            self.assertTrue(output.is_file())
            extract_page.assert_called_once()
            self.assertEqual(extract_page.call_args.args[1], 1)
            self.assertEqual(
                (project / "text/extracted.txt").read_text(encoding="utf-8"),
                "Embedded text from page one.\n\nEmbedded text from page two.\n",
            )
            ledger = json.loads((project / "ledger.json").read_text(encoding="utf-8"))
            self.assertEqual(ledger["stages"]["extract"]["pages"]["0001"]["extraction_method"], "embedded_text")
            self.assertEqual(ledger["stages"]["extract"]["pages"]["0002"]["extraction_method"], "embedded_text")

    def test_pdf_auto_mode_uses_ocr_for_a_page_without_usable_text(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "scan.pdf"
            source.write_bytes(b"%PDF-1.7 fake test fixture")
            settings = test_settings()
            settings["ocr_backend"] = "tesseract"
            project = create_project(source, root / "projects", "pdf-ocr", settings, None)

            def render_page(
                _source: Path, _page_index: int, output: Path, _dpi: int, _cache: Path
            ) -> None:
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(b"fake png")

            with (
                patch("deterministic_tts.pipeline.pdf_info", return_value={"page_count": 1}),
                patch("deterministic_tts.pipeline.pdf_page_text", return_value=""),
                patch("deterministic_tts.pipeline.pdf_render_page", side_effect=render_page) as render,
                patch(
                    "deterministic_tts.pipeline.extract_image_text",
                    return_value="Text recovered from the scanned PDF page.",
                ) as ocr,
            ):
                output = ProjectPipeline(project, progress=False).run()

            self.assertTrue(output.is_file())
            render.assert_called_once()
            ocr.assert_called_once()
            self.assertFalse((project / ".cache/pdf-pages/0001.png").exists())
            ledger = json.loads((project / "ledger.json").read_text(encoding="utf-8"))
            self.assertEqual(ledger["stages"]["extract"]["pages"]["0001"]["extraction_method"], "ocr")

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

