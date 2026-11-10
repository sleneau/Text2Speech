from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from zipfile import ZipFile

from deterministic_tts.curriculum import fetch_passages, initialize, render
from deterministic_tts.esv import API_URL, fetch_passage, parse_reference, reusable
from deterministic_tts.util import atomic_write_json, load_json


def response(reference: str, text: str = "Synthetic test passage.") -> dict:
    span = parse_reference(reference)[0]
    return {"canonical": span.reference, "parsed": span.parsed, "passages": [text]}


class ReferenceTests(unittest.TestCase):
    def test_explicit_verses_ranges_aliases_and_groups(self) -> None:
        cases = {
            "Jn. 3:16": ["John 3:16"],
            "Psalm 3:1–5": ["Psalm 3:1-5"],
            "1Cor. 13:4-7": ["1 Corinthians 13:4-7"],
            "Song of Songs 2:1": ["Song of Solomon 2:1"],
            "John 3:16, 18; 4:1-3; Rom. 5:8": [
                "John 3:16", "John 3:18", "John 4:1-3", "Romans 5:8",
            ],
            "Jude 1:5": ["Jude 1:5"],
            "John 3:16-3:18": ["John 3:16-18"],
        }
        for source, expected in cases.items():
            with self.subTest(source=source):
                self.assertEqual([span.reference for span in parse_reference(source)], expected)

    def test_chapters_are_never_expanded(self) -> None:
        for source in ("John 3", "John 3-5", "Psalms 1–5", "1 Corinthians 13", "Jude 5", "John 3; 5-7"):
            with self.subTest(source=source):
                self.assertEqual(parse_reference(source), [])

    def test_ambiguous_unbounded_reversed_and_cross_chapter_queries_rejected(self) -> None:
        for source in ("John", "43003016", "John 3:16ff", "John 3:16-", "John 3:18-16",
                       "John 0:1", "John 3:0", "John 3:16-4:2", "John 3-4:2", "John 3:16; 4",
                       "John 3:16,", "Unknown 3:16", "John 3:16; Genesis 1"):
            with self.subTest(source=source):
                with self.assertRaises(ValueError):
                    parse_reference(source)


class CrosswayTests(unittest.TestCase):
    @patch("deterministic_tts.esv.time.sleep")
    def test_verse_range_spelling_cannot_download_a_whole_chapter(self, sleep) -> None:
        span = parse_reference("Psalm 117:1-2")[0]
        probe = response("Psalm 117:1")
        probe["passage_meta"] = [{"chapter_start": [19117001, 19117002]}]
        with patch("deterministic_tts.esv.urlopen", return_value=io.BytesIO(json.dumps(probe).encode())) as http:
            with self.assertRaisesRegex(ValueError, "Whole-chapter verse range not fetched"):
                fetch_passage(span, "secret")
        self.assertEqual(http.call_count, 1)
        self.assertEqual(parse_qs(urlsplit(http.call_args.args[0].full_url).query)["q"], ["Psalm 117:1"])
        sleep.assert_not_called()

    @patch("deterministic_tts.esv.time.sleep")
    def test_partial_range_starting_at_verse_one_is_allowed_after_metadata_check(self, sleep) -> None:
        span = parse_reference("John 3:1-3")[0]
        probe = response("John 3:1")
        probe["passage_meta"] = [{"chapter_start": [43003001, 43003036]}]
        actual = response("John 3:1-3")
        actual["passage_meta"] = probe["passage_meta"]
        with patch("deterministic_tts.esv.urlopen", side_effect=[
            io.BytesIO(json.dumps(payload).encode()) for payload in (probe, actual)
        ]) as http:
            fetched = fetch_passage(span, "secret")
        self.assertEqual(http.call_count, 2)
        self.assertEqual(fetched["chapter_bounds"], [43003001, 43003036])
        self.assertEqual(fetched["reference"], "John 3:1-3")
        sleep.assert_called_once_with(1.05)

    def test_range_starting_at_one_requires_trustworthy_boundary_metadata(self) -> None:
        span = parse_reference("John 3:1-3")[0]
        with patch("deterministic_tts.esv.urlopen", return_value=io.BytesIO(
            json.dumps(response("John 3:1")).encode(),
        )) as http:
            with self.assertRaisesRegex(ValueError, "did not supply chapter boundaries"):
                fetch_passage(span, "secret")
            self.assertEqual(http.call_count, 1)

    def test_request_authentication_options_and_verified_record(self) -> None:
        span = parse_reference("John 11:35")[0]
        payload = response(span.reference, "\n  Jesus wept.\n")
        with patch("deterministic_tts.esv.urlopen", return_value=io.BytesIO(json.dumps(payload).encode())) as http:
            passage = fetch_passage(span, "test-secret")
        request = http.call_args.args[0]
        self.assertTrue(request.full_url.startswith(API_URL))
        self.assertEqual(request.get_header("Authorization"), "Token test-secret")
        params = parse_qs(urlsplit(request.full_url).query)
        self.assertEqual(params["q"], ["John 11:35"])
        for option in ("include-headings", "include-footnotes", "include-verse-numbers", "include-passage-references"):
            self.assertEqual(params[option], ["false"])
        self.assertEqual(http.call_args.kwargs["timeout"], 30)
        self.assertEqual(passage["text"], "Jesus wept.")
        self.assertTrue(reusable(passage, span))
        self.assertIn("English Standard Version", passage["intro"])
        self.assertNotIn("test-secret", json.dumps(passage))
        passage["text"] = "Altered text."
        self.assertFalse(reusable(passage, span))

    def test_empty_corrected_truncated_or_malformed_responses_rejected(self) -> None:
        span = parse_reference("John 3:16-18")[0]
        for payload in (None, [], {}, {"parsed": [[43003016, 43003017]], "passages": ["Truncated"]},
                        {"parsed": span.parsed, "passages": []},
                        {"parsed": span.parsed, "passages": [""]},
                        {"parsed": span.parsed, "passages": ["One", "Two"]}):
            with self.subTest(payload=payload), patch(
                "deterministic_tts.esv.urlopen", return_value=io.BytesIO(json.dumps(payload).encode()),
            ):
                with self.assertRaises(ValueError):
                    fetch_passage(span, "secret")
        with patch("deterministic_tts.esv.urlopen", return_value=io.BytesIO(b"not-json")):
            with self.assertRaisesRegex(ValueError, "invalid JSON"):
                fetch_passage(span, "secret")

    def test_network_errors_are_actionable_and_do_not_echo_credentials(self) -> None:
        span = parse_reference("John 11:35")[0]
        for error, expected in (
            (HTTPError(API_URL, 401, "test-secret", {}, None), "HTTP 401"),
            (HTTPError(API_URL, 429, "test-secret", {}, None), "HTTP 429"),
            (URLError("test-secret"), "Unable to reach"),
            (TimeoutError("test-secret"), "Unable to reach"),
        ):
            with self.subTest(error=error), patch("deterministic_tts.esv.urlopen", side_effect=error):
                with self.assertRaisesRegex(RuntimeError, expected) as raised:
                    fetch_passage(span, "test-secret")
                self.assertNotIn("test-secret", str(raised.exception))


class FetchPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        root_patch = patch("deterministic_tts.curriculum.PROJECT_ROOT", self.root)
        root_patch.start()
        self.addCleanup(root_patch.stop)
        self.project = self.root / "prepared"

    def prepare(self, text: str) -> dict:
        source = self.root / "lesson.docx"
        with ZipFile(source, "w") as archive:
            archive.writestr("word/document.xml", (
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                f"<w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>"
            ))
        initialize(source, self.project)
        return load_json(self.project / "plan.json")

    @patch.dict(os.environ, {"ESV_API_KEY": "test-secret"})
    @patch("deterministic_tts.curriculum.time.sleep")
    def test_group_fetch_reuse_and_chapter_only_retention(self, sleep) -> None:
        self.prepare("John 11:35, 36. John 11:35. Psalm 23. Psalms 1–5.")
        payloads = [response("John 11:35"), response("John 11:36")]
        with patch("deterministic_tts.esv.urlopen", side_effect=[
            io.BytesIO(json.dumps(payload).encode()) for payload in payloads
        ]) as http:
            counts = fetch_passages(self.project)
        self.assertEqual(counts, {"fetched": 2, "reused": 0, "chapter_references": 2})
        self.assertEqual(http.call_count, 2)
        self.assertEqual(sleep.call_count, 1)
        plan = load_json(self.project / "plan.json")
        citations = plan["blocks"][0]["citations"]
        self.assertEqual(citations[0]["passages"], ["John 11:35", "John 11:36"])
        self.assertTrue(citations[-1]["reference_only"])
        cache_bytes = (self.project / "passages.json").read_bytes()
        with patch.dict(os.environ, {}, clear=True), patch("deterministic_tts.esv.urlopen") as http:
            self.assertEqual(fetch_passages(self.project)["reused"], 2)
            http.assert_not_called()
        self.assertEqual(cache_bytes, (self.project / "passages.json").read_bytes())
        plan["inventory_checked"] = True
        atomic_write_json(self.project / "plan.json", plan)
        atomic_write_json(self.project / "templates.json", {
            "opening": "Welcome.", "closing": "Thank you.", "variables": {},
        })
        output = render(self.project).read_text()
        self.assertIn("Psalm 23", output)
        self.assertEqual(output.count("Synthetic test passage."), 3)
        passages = load_json(self.project / "passages.json")
        passages["John 11:35"]["text"] = "Altered."
        atomic_write_json(self.project / "passages.json", passages)
        with self.assertRaisesRegex(ValueError, "content or verse metadata has changed"):
            render(self.project)

    def test_chapters_only_need_no_key_and_make_no_http_requests(self) -> None:
        self.prepare("John 3-5; Psalm 23.")
        with patch.dict(os.environ, {}, clear=True), patch("deterministic_tts.esv.urlopen") as http:
            self.assertEqual(fetch_passages(self.project)["fetched"], 0)
            http.assert_not_called()

    def test_entire_batch_is_checked_before_network_and_requires_key(self) -> None:
        plan = self.prepare("John 11:35; John 3:16-4:2.")
        with patch.dict(os.environ, {"ESV_API_KEY": "test-secret"}), patch("deterministic_tts.esv.urlopen") as http:
            with self.assertRaisesRegex(ValueError, "cross-chapter"):
                fetch_passages(self.project)
            http.assert_not_called()
        plan["blocks"][0]["citations"].pop()
        atomic_write_json(self.project / "plan.json", plan)
        with patch.dict(os.environ, {}, clear=True), patch("deterministic_tts.esv.urlopen") as http:
            with self.assertRaisesRegex(ValueError, "Set ESV_API_KEY"):
                fetch_passages(self.project)
            http.assert_not_called()

    @patch.dict(os.environ, {"ESV_API_KEY": "test-secret"})
    @patch("deterministic_tts.curriculum.time.sleep")
    def test_failed_fetch_checkpoints_completed_passage_and_resumes(self, sleep) -> None:
        original_plan = self.prepare("John 11:35, 36.")
        with patch("deterministic_tts.esv.urlopen", side_effect=[
            io.BytesIO(json.dumps(response("John 11:35")).encode()), URLError("offline"),
        ]):
            with self.assertRaises(RuntimeError):
                fetch_passages(self.project)
        self.assertEqual(load_json(self.project / "plan.json"), original_plan)
        self.assertEqual(list(load_json(self.project / "passages.json")), ["John 11:35"])
        with patch("deterministic_tts.esv.urlopen", return_value=io.BytesIO(
            json.dumps(response("John 11:36")).encode(),
        )) as http:
            self.assertEqual(fetch_passages(self.project)["fetched"], 1)
            self.assertEqual(parse_qs(urlsplit(http.call_args.args[0].full_url).query)["q"], ["John 11:36"])


if __name__ == "__main__":
    unittest.main()
