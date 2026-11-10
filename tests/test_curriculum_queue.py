from __future__ import annotations

import os
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile

from deterministic_tts.curriculum import main, render
from deterministic_tts.curriculum_queue import run_batch
from deterministic_tts.util import atomic_write_json, load_json, sha256_file


def write_docx(path: Path, text: str) -> None:
    with ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", (
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            f'<w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>'
        ))


class CurriculumQueueTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        output_capture = redirect_stdout(io.StringIO())
        output_capture.__enter__()
        self.addCleanup(output_capture.__exit__, None, None, None)
        self.root = Path(temporary.name).resolve()
        root_patch = patch("deterministic_tts.curriculum.PROJECT_ROOT", self.root)
        root_patch.start()
        self.addCleanup(root_patch.stop)
        self.inbox = self.root / "curriculum"
        self.workload = self.root / "workload"
        self.inbox.mkdir()
        self.source = self.inbox / "Lesson 2.docx"

    def scan(self) -> dict:
        return run_batch(self.inbox, self.workload)

    def record(self) -> dict:
        return load_json(self.inbox / "ledger.json")["documents"][self.source.name]

    def ready(self) -> Path:
        project = Path(self.record()["preparation"])
        plan = load_json(project / "plan.json")
        plan["inventory_checked"] = True
        atomic_write_json(project / "plan.json", plan)
        atomic_write_json(project / "templates.json", {
            "opening": "Welcome.", "closing": "Thank you.", "variables": {},
        })
        return project

    def test_no_arguments_use_project_root_from_another_working_directory(self) -> None:
        write_docx(self.source, "A lesson.")
        old_directory = Path.cwd()
        try:
            os.chdir(self.root.parent)
            self.assertEqual(main([]), 0)
        finally:
            os.chdir(old_directory)
        self.assertEqual(self.record()["status"], "awaiting_review")
        self.assertFalse((self.workload / "Lesson 2.txt").exists())
        self.ready()
        self.assertEqual(main([]), 0)
        self.assertEqual(self.record()["status"], "complete")
        self.assertEqual((self.workload / "Lesson 2.txt").read_text(), "Welcome.\n\nA lesson.\n\nThank you.\n")

    def test_filename_shorthand_processes_only_the_named_document_and_can_resume(self) -> None:
        write_docx(self.source, "Selected lesson.")
        write_docx(self.inbox / "Another lesson.docx", "Other lesson.")
        self.assertEqual(main([self.source.name]), 0)
        records = load_json(self.inbox / "ledger.json")["documents"]
        self.assertEqual(list(records), [self.source.name])
        self.ready()
        self.assertEqual(main([self.source.name]), 0)
        self.assertIn("Selected lesson.", (self.workload / "Lesson 2.txt").read_text())
        self.assertFalse((self.workload / "Another lesson.txt").exists())
        with patch("deterministic_tts.curriculum.render") as renderer:
            self.assertEqual(main([self.source.name]), 0)
            renderer.assert_not_called()

    def test_named_document_accepts_absolute_and_curriculum_relative_paths(self) -> None:
        write_docx(self.source, "A lesson.")
        self.assertEqual(main([str(self.source)]), 0)
        self.ready()
        old_directory = Path.cwd()
        try:
            os.chdir(self.root)
            self.assertEqual(main(["curriculum/Lesson 2.docx"]), 0)
        finally:
            os.chdir(old_directory)
        self.assertEqual(self.record()["status"], "complete")
        self.assertEqual(main(["batch", self.source.name, "--curriculum-dir", str(self.inbox)]), 0)

    def test_invalid_named_documents_fail_without_processing_other_documents(self) -> None:
        write_docx(self.source, "Do not process this other lesson.")
        outside = self.root / "Outside.docx"
        write_docx(outside, "Outside the inbox.")
        for name in ("Missing.docx", "notes.txt", "~$Lesson.docx", str(outside)):
            with self.subTest(name=name):
                self.assertEqual(main([name]), 1)
                self.assertFalse((self.inbox / "ledger.json").exists())
                self.assertFalse((self.workload / "Lesson 2.txt").exists())

    def test_success_is_recorded_only_after_export_and_completed_sources_are_skipped(self) -> None:
        write_docx(self.source, "A lesson.")
        original = self.source.read_bytes()
        self.assertEqual(self.scan()["pending"], 1)
        project = self.ready()
        self.assertEqual(self.scan()["exported"], 1)
        self.assertEqual(self.source.read_bytes(), original)
        record = self.record()
        output = self.workload / "Lesson 2.txt"
        self.assertEqual(record["narration_sha256"], sha256_file(output))
        self.assertEqual(load_json(project / "narration.audit.json")["output_path"], str(output))
        with patch("deterministic_tts.curriculum.render") as renderer:
            self.assertEqual(self.scan()["skipped"], 1)
            renderer.assert_not_called()

    def test_direct_render_of_a_queued_preparation_also_updates_the_ledger(self) -> None:
        write_docx(self.source, "A lesson.")
        self.scan()
        project = self.ready()
        self.assertEqual(render(project), self.workload / "Lesson 2.txt")
        self.assertEqual(self.record()["status"], "complete")
        self.assertEqual(self.scan()["skipped"], 1)

    def test_modified_original_gets_new_preparation_and_can_replace_its_managed_output(self) -> None:
        write_docx(self.source, "Original lesson.")
        self.scan()
        first_project = self.ready()
        self.scan()
        write_docx(self.source, "Updated lesson.")
        self.assertEqual(self.scan()["pending"], 1)
        self.assertIn("Original lesson.", (self.workload / "Lesson 2.txt").read_text())
        second_project = self.ready()
        self.assertNotEqual(first_project, second_project)
        self.assertTrue(first_project.is_dir())
        self.assertEqual(self.scan()["exported"], 1)
        self.assertIn("Updated lesson.", (self.workload / "Lesson 2.txt").read_text())
        with self.assertRaisesRegex(ValueError, "Inbox source has changed"):
            render(first_project)

    def test_deleted_output_is_recreated_and_modified_output_is_preserved(self) -> None:
        write_docx(self.source, "A lesson.")
        self.scan()
        self.ready()
        self.scan()
        output = self.workload / "Lesson 2.txt"
        output.unlink()
        self.assertEqual(self.scan()["exported"], 1)
        output.write_text("Manual changes.\n")
        self.assertEqual(self.scan()["failed"], 1)
        self.assertEqual(output.read_text(), "Manual changes.\n")
        self.assertEqual(self.record()["status"], "failed")

    def test_existing_unrelated_file_is_not_overwritten(self) -> None:
        write_docx(self.source, "A lesson.")
        self.scan()
        self.ready()
        self.workload.mkdir()
        output = self.workload / "Lesson 2.txt"
        output.write_text("Existing unrelated material.\n")
        self.assertEqual(self.scan()["failed"], 1)
        self.assertEqual(output.read_text(), "Existing unrelated material.\n")

    def test_failed_write_does_not_mark_complete_and_retry_can_resume(self) -> None:
        write_docx(self.source, "A lesson.")
        self.scan()
        self.ready()
        with patch("deterministic_tts.curriculum_queue.atomic_write_text", side_effect=OSError("Disk full")):
            self.assertEqual(self.scan()["failed"], 1)
        self.assertEqual(self.record()["status"], "failed")
        self.assertFalse((self.workload / "Lesson 2.txt").exists())
        self.assertEqual(self.scan()["exported"], 1)

    def test_editorial_changes_are_not_skipped_even_when_source_is_unchanged(self) -> None:
        write_docx(self.source, "A lesson.")
        self.scan()
        project = self.ready()
        self.scan()
        plan = load_json(project / "plan.json")
        plan["blocks"][0]["before"] = "Let us begin the lesson."
        atomic_write_json(project / "plan.json", plan)
        self.assertEqual(self.scan()["exported"], 1)
        self.assertIn("Let us begin", (self.workload / "Lesson 2.txt").read_text())

    def test_word_lock_files_subfolders_and_other_file_types_are_ignored(self) -> None:
        write_docx(self.inbox / "~$Lesson.docx", "A lock file.")
        (self.inbox / "notes.txt").write_text("Notes")
        nested = self.inbox / "processed"
        nested.mkdir()
        write_docx(nested / "Old.docx", "Already processed.")
        self.assertEqual(self.scan(), {"exported": 0, "skipped": 0, "pending": 0, "failed": 0})

    def test_custom_paths_and_uppercase_extension_keep_the_filename(self) -> None:
        self.source = self.inbox / "Lesson 2.DOCX"
        write_docx(self.source, "A lesson.")
        other_workload = self.root / "custom-workload"
        args = ["batch", "--curriculum-dir", str(self.inbox), "--workload-dir", str(other_workload)]
        self.assertEqual(main(args), 0)
        project = self.ready()
        self.assertEqual(render(project), other_workload / "Lesson 2.txt")
        self.assertEqual(main(args), 0)

    def test_bad_docx_does_not_prevent_preparation_of_another_file(self) -> None:
        self.source.write_bytes(b"Invalid DOCX")
        write_docx(self.inbox / "Good.docx", "A good lesson.")
        self.assertEqual(self.scan(), {"exported": 0, "skipped": 0, "pending": 1, "failed": 1})
        self.assertEqual(self.record()["status"], "failed")


if __name__ == "__main__":
    unittest.main()
