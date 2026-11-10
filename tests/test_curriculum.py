from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile

from deterministic_tts.curriculum import (
    approve, extract_docx, initialize, main, read_project, references, render, review,
)
from deterministic_tts.text import chunk_text
from deterministic_tts.util import atomic_write_json, load_json


def docx(path: Path, body: str, extras: dict[str, str] | None = None) -> None:
    with ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", (
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            f"<w:body>{body}<w:sectPr/></w:body></w:document>"
        ))
        for name, value in (extras or {}).items():
            archive.writestr(name, value)


class CurriculumTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        root_patch = patch("deterministic_tts.curriculum.PROJECT_ROOT", self.root)
        root_patch.start()
        self.addCleanup(root_patch.stop)
        self.source = self.root / "lesson.docx"
        self.project = self.root / "prepared"

    def prepare(self, body: str) -> dict:
        docx(self.source, body)
        initialize(self.source, self.project)
        templates = {"opening": "Welcome to ${title}.", "closing": "Thank you.",
                     "variables": {"title": "Prayer"}}
        atomic_write_json(self.project / "templates.json", templates)
        plan = load_json(self.project / "plan.json")
        plan["inventory_checked"] = True
        atomic_write_json(self.project / "plan.json", plan)
        return plan

    def save(self, plan: dict) -> None:
        atomic_write_json(self.project / "plan.json", plan)

    def test_preserves_body_order_cells_and_formatting(self) -> None:
        docx(self.source,
             '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr>'
             '<w:r><w:rPr><w:b/></w:rPr><w:t>Prayer</w:t></w:r></w:p>'
             '<w:tbl><w:tr><w:tc><w:tcPr><w:gridSpan w:val="2"/></w:tcPr>'
             '<w:p><w:r><w:t>Category</w:t></w:r></w:p></w:tc></w:tr></w:tbl>'
             '<w:p><w:pPr><w:numPr><w:ilvl w:val="0"/></w:numPr></w:pPr>'
             '<w:r><w:t>A final point.</w:t></w:r></w:p>')
        blocks = extract_docx(self.source)
        self.assertEqual([block["kind"] for block in blocks], ["paragraph", "table", "paragraph"])
        self.assertEqual(blocks[0]["style"], "Heading1")
        self.assertEqual(blocks[0]["emphasis"][0]["text"], "Prayer")
        self.assertIn("gridSpan", blocks[1]["xml"])
        self.assertEqual(blocks[1]["cells"], [{"id": "r1c1", "text": "Category"}])
        self.assertTrue(blocks[2]["list_item"])

    def test_reproducible_render_and_pipeline_paragraphs(self) -> None:
        plan = self.prepare('<w:p><w:r><w:t>First point.</w:t></w:r></w:p>'
                            '<w:p><w:r><w:t>Second point.</w:t></w:r></w:p>')
        plan["blocks"][1]["before"] = "We now consider the second point."
        self.save(plan)
        output = render(self.project)
        first = output.read_bytes()
        audit = (self.project / "narration.audit.json").read_bytes()
        render(self.project)
        self.assertEqual(output.read_bytes(), first)
        self.assertEqual((self.project / "narration.audit.json").read_bytes(), audit)
        self.assertEqual(chunk_text(output.read_text(), 2500, "paragraph"), [
            "Welcome to Prayer.\n", "First point.\n",
            "We now consider the second point.\n", "Second point.\n", "Thank you.\n",
        ])

    def test_source_edit_requires_approval_bound_to_exact_proposal(self) -> None:
        plan = self.prepare('<w:p><w:r><w:t>A greated detail.</w:t></w:r></w:p>')
        block = plan["blocks"][0]
        block.update(narration="A greater detail.", reason="Correct a spelling error.")
        self.save(plan)
        with self.assertRaisesRegex(ValueError, "needs user approval"):
            render(self.project)
        self.assertIn("-A greated detail.", review(self.project).read_text())
        approve(self.project, "b0001", "Scott")
        render(self.project)
        block["after"] = "An additional bridge."
        self.save(plan)
        with self.assertRaisesRegex(ValueError, "needs user approval"):
            render(self.project)

    def test_table_requires_complete_coverage_and_confirmation(self) -> None:
        plan = self.prepare('<w:tbl><w:tr>'
                            '<w:tc><w:p><w:r><w:t>Trust</w:t></w:r></w:p></w:tc>'
                            '<w:tc><w:p><w:r><w:t>Pray</w:t></w:r></w:p></w:tc>'
                            '</w:tr></w:tbl>')
        block = plan["blocks"][0]
        block.update(narration="Trust: Pray.", reason="Read the category followed by its action.",
                     complex=False)
        self.save(plan)
        with self.assertRaisesRegex(ValueError, "every nonempty cell"):
            approve(self.project, "b0001", "Scott")
        block["cell_coverage"] = {"r1c1": "Category: Trust", "r1c2": "Action: Pray"}
        self.save(plan)
        with self.assertRaisesRegex(ValueError, "needs user approval"):
            render(self.project)
        approve(self.project, "b0001", "Scott")
        self.assertIn("Trust: Pray.", render(self.project).read_text())

    def test_verse_lookup_is_required_and_not_generated(self) -> None:
        plan = self.prepare('<w:p><w:r><w:t>Read John 11:35.</w:t></w:r></w:p>')
        with self.assertRaisesRegex(ValueError, "resolve John 11:35"):
            render(self.project)
        plan["blocks"][0]["citations"][0]["passages"] = ["John 11:35"]
        self.save(plan)
        passage = {"translation": "ESV", "text": "Jesus wept.",
                   "source": "https://api.esv.org/docs/passage-text/ (documented example)",
                   "intro": "John chapter eleven, verse thirty-five, reads:", "verified": False}
        atomic_write_json(self.project / "passages.json", {"John 11:35": passage})
        with self.assertRaisesRegex(ValueError, "checked against"):
            render(self.project)
        passage["verified"] = True
        atomic_write_json(self.project / "passages.json", {"John 11:35": passage})
        text = render(self.project).read_text()
        self.assertIn('reads: “Jesus wept.”', text)
        self.assertLess(text.index("Read John"), text.index("Jesus wept."))

    def test_grouped_references_and_candidate_inventory(self) -> None:
        text = "John 3:16, 18; 4:1–3 and Rom. 5:8; 1 Corinthians 13; Psalm 23."
        self.assertEqual(references(text), [
            "John 3:16, 18; 4:1–3", "Rom. 5:8", "1 Corinthians 13", "Psalm 23",
        ])
        plan = self.prepare('<w:p><w:r><w:t>Read John 11:35.</w:t></w:r></w:p>')
        plan["blocks"][0]["citations"] = []
        self.save(plan)
        with self.assertRaisesRegex(ValueError, "citation was removed"):
            render(self.project)

    def test_already_quoted_verse_requires_note(self) -> None:
        plan = self.prepare('<w:p><w:r><w:t>John 11:35 reads: Jesus wept.</w:t></w:r></w:p>')
        citation = plan["blocks"][0]["citations"][0]
        citation["already_quoted"] = True
        self.save(plan)
        with self.assertRaisesRegex(ValueError, "verification note"):
            render(self.project)
        citation["note"] = "Compared the complete quotation with the ESV API example."
        self.save(plan)
        self.assertEqual(render(self.project).read_text().count("Jesus wept."), 1)

    def test_multiple_readings_are_ordered_and_passage_changes_invalidate_approval(self) -> None:
        plan = self.prepare('<w:p><w:r><w:t>John 11:35; Luke 2:1</w:t></w:r></w:p>')
        block = plan["blocks"][0]
        block.update(complex=True, reason="Review the grouped reading.")
        for citation in block["citations"]:
            citation["passages"] = [citation["source_reference"]]
        self.save(plan)
        # Synthetic fixture strings test ordering, not a Bible translation.
        passages = {key: {
            "translation": "ESV", "text": f"Fixture reading {index}.",
            "source": "Synthetic test fixture", "verified": True,
            "intro": f"Reading {index}:",
        } for index, key in enumerate(("John 11:35", "Luke 2:1"), 1)}
        atomic_write_json(self.project / "passages.json", passages)
        approve(self.project, "b0001", "Scott")
        text = render(self.project).read_text()
        self.assertLess(text.index("Fixture reading 1"), text.index("Fixture reading 2"))
        passages["John 11:35"]["text"] = "Changed fixture."
        atomic_write_json(self.project / "passages.json", passages)
        with self.assertRaisesRegex(ValueError, "needs user approval"):
            render(self.project)

    def test_inherited_numbering_is_exposed_and_margins_are_not_silently_lost(self) -> None:
        namespace = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
        docx(self.source, '<w:p><w:pPr><w:pStyle w:val="Child"/></w:pPr>'
             '<w:r><w:t>First.</w:t></w:r></w:p>', {
                 "word/styles.xml": f'<w:styles {namespace}>'
                 '<w:style w:styleId="Child"><w:basedOn w:val="Parent"/></w:style>'
                 '<w:style w:styleId="Parent"><w:pPr><w:numPr/></w:pPr></w:style></w:styles>',
             })
        self.assertTrue(extract_docx(self.source)[0]["list_item"])
        docx(self.source, '<w:p><w:r><w:t>Body.</w:t></w:r></w:p>', {
            "word/header1.xml": f'<w:hdr {namespace}><w:p><w:r>'
            '<w:t>Meaningful header.</w:t></w:r></w:p></w:hdr>',
        })
        with self.assertRaisesRegex(ValueError, "needs manual disposition"):
            extract_docx(self.source)

    def test_reading_can_be_inserted_at_a_sentence_boundary_with_visible_preview(self) -> None:
        first = "Read John 11:35."
        plan = self.prepare(f'<w:p><w:r><w:t>{first} The lesson continues.</w:t></w:r></w:p>')
        citation = plan["blocks"][0]["citations"][0]
        citation.update(passages=["John 11:35"], insert_at=len(first))
        self.save(plan)
        atomic_write_json(self.project / "passages.json", {"John 11:35": {
            "translation": "ESV", "text": "Jesus wept.",
            "source": "https://api.esv.org/docs/passage-text/ (documented example)",
            "verified": True, "intro": "John chapter eleven, verse thirty-five, reads:",
        }})
        text = render(self.project).read_text()
        self.assertLess(text.index("Read John"), text.index("Jesus wept."))
        self.assertLess(text.index("Jesus wept."), text.index("The lesson continues."))
        preview = review(self.project).read_text()
        self.assertIn("Assembled preview", preview)
        self.assertIn('reads: “Jesus wept.”\n\nThe lesson continues.', preview)
        citation["insert_at"] = 2
        self.save(plan)
        with self.assertRaisesRegex(ValueError, "must not split a word"):
            render(self.project)
        citation["insert_at"] = 1000
        self.save(plan)
        with self.assertRaisesRegex(ValueError, "character offset"):
            render(self.project)

    def test_tampering_with_source_or_manifest_and_reordering_is_rejected(self) -> None:
        plan = self.prepare('<w:p><w:r><w:t>First.</w:t></w:r></w:p>'
                            '<w:p><w:r><w:t>Second.</w:t></w:r></w:p>')
        plan["blocks"].reverse()
        self.save(plan)
        with self.assertRaisesRegex(ValueError, "source order"):
            read_project(self.project)
        plan["blocks"].reverse()
        self.save(plan)
        manifest = load_json(self.project / "manifest.json")
        manifest["blocks"][0]["text"] = "Replaced."
        atomic_write_json(self.project / "manifest.json", manifest)
        with self.assertRaisesRegex(ValueError, "manifest does not match"):
            read_project(self.project)
        (self.project / "source.docx").write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "source.docx has changed"):
            read_project(self.project)

    def test_unsupported_content_is_rejected_without_creating_project(self) -> None:
        for content in ("drawing", "footnoteReference", "ins", "sdt", "fldChar"):
            with self.subTest(content=content):
                docx(self.source, f"<w:p><w:r><w:{content}/></w:r></w:p>")
                with self.assertRaisesRegex(ValueError, "Unsupported DOCX content"):
                    initialize(self.source, self.project)
                self.assertFalse(self.project.exists())

    def test_missing_templates_inventory_and_downstream_rewrites_are_rejected(self) -> None:
        plan = self.prepare('<w:p><w:r><w:t>A  point.</w:t></w:r></w:p>')
        templates = load_json(self.project / "templates.json")
        templates["opening"] = ""
        atomic_write_json(self.project / "templates.json", templates)
        with self.assertRaisesRegex(ValueError, "opening must be nonempty"):
            render(self.project)
        templates["opening"] = "${missing}"
        atomic_write_json(self.project / "templates.json", templates)
        with self.assertRaisesRegex(ValueError, "unresolved opening"):
            render(self.project)
        templates["opening"] = "Welcome."
        atomic_write_json(self.project / "templates.json", templates)
        with self.assertRaisesRegex(ValueError, "TTS normalization"):
            render(self.project)
        plan["inventory_checked"] = False
        self.save(plan)
        with self.assertRaisesRegex(ValueError, "inventory"):
            render(self.project)

    def test_cli_init_existing_directory_and_bad_input(self) -> None:
        docx(self.source, '<w:p><w:r><w:t>Lesson.</w:t></w:r></w:p>')
        self.assertEqual(main(["init", str(self.source), str(self.project)]), 0)
        self.assertEqual(main(["init", str(self.source), str(self.project)]), 1)
        self.assertEqual(main(["review", str(self.project)]), 0)
        self.assertEqual(main(["render", str(self.project)]), 1)


if __name__ == "__main__":
    unittest.main()
