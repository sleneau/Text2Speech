"""Bounded verse lookups using Crossway's official passage-lookup example.

https://api.esv.org/docs/samples/ -- standard-library HTTP implementation.
Only explicit, same-chapter verse spans are sent to the passage endpoint.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .util import sha256_text, utc_now

API_URL = "https://api.esv.org/v3/passage/text/"
_BOOK_ROWS = (
    "Genesis|Gen;Exodus|Ex|Exod;Leviticus|Lev;Numbers|Num;Deuteronomy|Deut;"
    "Joshua|Josh;Judges|Judg;Ruth;1 Samuel|1 Sam;2 Samuel|2 Sam;1 Kings|1 Kgs;"
    "2 Kings|2 Kgs;1 Chronicles|1 Chron|1 Chr;2 Chronicles|2 Chron|2 Chr;Ezra;"
    "Nehemiah|Neh;Esther|Esth;Job;Psalm|Psalms|Ps;Proverbs|Prov;Ecclesiastes|Eccl;"
    "Song of Solomon|Song of Songs|Song;Isaiah|Isa;Jeremiah|Jer;Lamentations|Lam;"
    "Ezekiel|Ezek;Daniel|Dan;Hosea|Hos;Joel;Amos;Obadiah|Obad;Jonah;Micah|Mic;"
    "Nahum|Nah;Habakkuk|Hab;Zephaniah|Zeph;Haggai|Hag;Zechariah|Zech;Malachi|Mal;"
    "Matthew|Matt;Mark|Mk;Luke|Lk;John|Jn;Acts;Romans|Rom;1 Corinthians|1 Cor;"
    "2 Corinthians|2 Cor;Galatians|Gal;Ephesians|Eph;Philippians|Phil;Colossians|Col;"
    "1 Thessalonians|1 Thess;2 Thessalonians|2 Thess;1 Timothy|1 Tim;2 Timothy|2 Tim;"
    "Titus;Philemon|Phlm;Hebrews|Heb;James|Jas;1 Peter|1 Pet;2 Peter|2 Pet;"
    "1 John|1 Jn;2 John|2 Jn;3 John|3 Jn;Jude;Revelation|Rev"
)


def _alias(value: str) -> str:
    return re.sub(r"[ .]", "", value).casefold()


BOOK_ALIASES = {
    _alias(alias): (index, row.split("|")[0])
    for index, row in enumerate(_BOOK_ROWS.split(";"), 1)
    for alias in row.split("|")
}
BOOK_PREFIX = re.compile(r"((?:[123]\s*)?[A-Za-z][A-Za-z. ]*?)\s*(\d.*)")
CHAPTERS_ONLY = re.compile(r"[1-9]\d*(?:\s*-\s*[1-9]\d*)?(?:\s*[,;]\s*[1-9]\d*(?:\s*-\s*[1-9]\d*)?)*")
VERSE_SPAN = re.compile(r"(?:(\d+):)?(\d+)(?:\s*-\s*(?:(\d+):)?(\d+))?")


@dataclass(frozen=True)
class VerseSpan:
    book_id: int
    book: str
    chapter: int
    first: int
    last: int

    @property
    def reference(self) -> str:
        suffix = f"-{self.last}" if self.last != self.first else ""
        return f"{self.book} {self.chapter}:{self.first}{suffix}"

    @property
    def parsed(self) -> list[list[int]]:
        base = self.book_id * 1_000_000 + self.chapter * 1000
        return [[base + self.first, base + self.last]]


def parse_reference(reference: str) -> list[VerseSpan]:
    """Expand explicit groups; return [] for chapter-only references (never fetch).

    Commas inherit a chapter (John 3:16, 18). Semicolons require a new explicit
    chapter:verse or book chapter:verse. Cross-chapter spans require editorial
    resolution into separate explicit verse spans, never chapter expansion.
    """
    reference = reference.strip().replace("–", "-").replace("—", "-")
    initial = BOOK_PREFIX.fullmatch(reference)
    if initial and _alias(initial[1]) in BOOK_ALIASES and CHAPTERS_ONLY.fullmatch(initial[2]):
        return []
    book = None
    chapter = None
    output = []
    separator = ""
    for token in re.split(r"([,;])", reference):
        if token in {",", ";"}:
            separator = token
            continue
        token = token.strip()
        explicit_book = BOOK_PREFIX.fullmatch(token)
        if explicit_book:
            book = BOOK_ALIASES.get(_alias(explicit_book[1]))
            chapter = None
            token = explicit_book[2]
        match = VERSE_SPAN.fullmatch(token)
        if book is None or match is None:
            raise ValueError(f"Use explicit book chapter:verse references, not chapter queries: {reference}")
        start_chapter, first, end_chapter, last = match.groups()
        if start_chapter is None and (chapter is None or separator == ";"):
            raise ValueError(f"Missing explicit chapter:verse after semicolon or book: {reference}")
        chapter = int(start_chapter) if start_chapter else chapter
        first = int(first)
        last = int(last) if last else first
        if end_chapter is not None and int(end_chapter) != chapter:
            raise ValueError(f"Split cross-chapter readings into explicit verse spans: {reference}")
        if not (1 <= chapter <= 150 and 1 <= first <= last <= 176):
            raise ValueError(f"Invalid or reversed verse span: {reference}")
        output.append(VerseSpan(book[0], book[1], chapter, first, last))
    return output


def fetch_passage(span: VerseSpan, api_key: str) -> dict:
    """Fetch one exact span; reject empty, corrected, or truncated responses."""
    known_bounds = None
    if span.first == 1 and span.last > 1:
        # A verse-bounded spelling can still request a whole chapter. Ask for
        # verse 1 alone to learn the boundary before issuing the range request.
        probe = fetch_passage(VerseSpan(span.book_id, span.book, span.chapter, 1, 1), api_key)
        bounds = probe.get("chapter_bounds")
        if bounds is None:
            raise ValueError(f"Crossway did not supply chapter boundaries; range not fetched: {span.reference}")
        if span.parsed[0][1] >= bounds[1]:
            raise ValueError(f"Whole-chapter verse range not fetched: {span.reference}; choose a narrower reading")
        known_bounds = bounds
        time.sleep(1.05)
    params = {"q": span.reference, **{key: "false" for key in (
        "include-headings", "include-footnotes", "include-footnote-body",
        "include-verse-numbers", "include-first-verse-numbers",
        "include-short-copyright", "include-copyright", "include-passage-references",
        "include-passage-horizontal-lines", "include-heading-horizontal-lines",
    )}, "include-selahs": "true", "line-length": "0"}
    request = Request(API_URL + "?" + urlencode(params), headers={
        "Authorization": f"Token {api_key}", "Accept": "application/json",
    })
    try:
        with urlopen(request, timeout=30) as response:
            raw = response.read(1_000_001)
        if len(raw) > 1_000_000:
            raise ValueError("Crossway response exceeds the expected passage size")
        result = json.loads(raw)
    except HTTPError as exc:
        raise RuntimeError(f"Crossway returned HTTP {exc.code} for {span.reference}") from None
    except (URLError, TimeoutError, OSError):
        raise RuntimeError(f"Unable to reach Crossway for {span.reference}; retry fetch-passages") from None
    except (UnicodeError, json.JSONDecodeError):
        raise ValueError(f"Crossway returned invalid JSON for {span.reference}") from None
    if not isinstance(result, dict) or result.get("parsed") != span.parsed:
        raise ValueError(f"Crossway did not return the exact requested verses: {span.reference}")
    passages = result.get("passages")
    if not isinstance(passages, list) or len(passages) != 1 or not isinstance(passages[0], str) or not passages[0].strip():
        raise ValueError(f"Crossway returned no single complete passage for {span.reference}")
    # Match the sample's poetry whitespace flattening; preserve words/punctuation.
    text = re.sub(r"\s+", " ", passages[0]).strip()
    bounds = known_bounds
    metadata = result.get("passage_meta")
    if isinstance(metadata, list) and len(metadata) == 1 and isinstance(metadata[0], dict):
        candidate = metadata[0].get("chapter_start")
        base = span.book_id * 1_000_000 + span.chapter * 1000
        if (isinstance(candidate, list) and len(candidate) == 2
                and all(type(value) is int for value in candidate)
                and candidate[0] == base + 1 and base + span.last <= candidate[1] <= base + 176):
            bounds = candidate
    return {
        "translation": "ESV", "text": text, "verified": True,
        "source": f"{API_URL} q={span.reference}; whitespace flattened only",
        "intro": f"{span.reference}, in the English Standard Version, reads:",
        "provider": "crossway", "reference": span.reference, "parsed": result["parsed"],
        "canonical": result.get("canonical", span.reference), "fetched_at": utc_now(),
        "text_sha256": sha256_text(text), "raw_passage": passages[0],
        "request_parameters": params, "chapter_bounds": bounds,
    }


def reusable(passage: object, span: VerseSpan) -> bool:
    if span.first == 1 and span.last > 1:
        bounds = passage.get("chapter_bounds") if isinstance(passage, dict) else None
        if (not isinstance(bounds, list) or len(bounds) != 2
                or not all(type(value) is int for value in bounds)
                or bounds[0] != span.parsed[0][0] or bounds[1] <= span.parsed[0][1]
                or bounds[1] // 1000 != bounds[0] // 1000):
            return False
    return (isinstance(passage, dict) and passage.get("provider") == "crossway"
            and passage.get("translation") == "ESV" and passage.get("verified") is True
            and passage.get("parsed") == span.parsed and isinstance(passage.get("text"), str)
            and bool(passage["text"].strip())
            and passage.get("text_sha256") == sha256_text(passage["text"]))
