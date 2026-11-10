# DOCX curriculum to narration

This is a separate preparation step for the existing TTS pipeline.
It uses only Python's standard library. An LLM or human editor proposes bridges
and light edits using [these instructions](CURRICULUM_LLM_INSTRUCTIONS.md).
The program preserves the DOCX, checks block order/coverage and verse inputs,
records review decisions, and assembles plain text reproducibly.

Extraction and assembly are deterministic. Interpreting meaning, deciding how
to narrate a table, discovering every possible Scripture reference, and checking
the intended citation spans are editorial tasks. The program does not claim that a hash,
coverage mapping, or LLM can prove semantic equivalence. It makes those choices
visible and reviewable. It calls no LLM. Only the explicit `fetch-passages`
command uses the network; extraction, review, and rendering remain offline.

Put DOCX files directly into `text_to_speech/curriculum/`, then run:

```sh
cd ~/git/text_to_speech
.venv/bin/python prepare_curriculum.py
```

With no arguments, the script scans the project-root `curriculum/` directory.
These defaults work regardless of the shell's current directory. For example,
`curriculum/Lesson 2.docx` exports to `workload/Lesson 2.txt`, keeping the original
filename, spaces, and capitalization, with only the extension changed.

To process only one document, name it explicitly (quote filenames with spaces):

```sh
.venv/bin/python prepare_curriculum.py "Lesson 2.docx"
```

This selects `curriculum/Lesson 2.docx` and uses the same preparation, review,
completion ledger, and `workload/Lesson 2.txt` export. Other documents are left
alone. A path such as `curriculum/Lesson 2.docx` or an absolute path inside the
inbox is also accepted. A missing or invalid filename reports an error.
For a different inbox, use the explicit batch form:

```sh
.venv/bin/python prepare_curriculum.py batch "Lesson 2.docx" \
  --curriculum-dir /path/to/curriculum
```

The first scan creates a preparation directory for each source and reports its
location. Give that directory and the instruction file to the editor. Complete
the templates, citation inventory, transitions, and required approvals there.
Run the script again to export ready documents. The batch command does not call
an LLM, fetch passages, or bypass review; unfinished documents remain pending.
You can also use `render PREPARATION_DIRECTORY` to export one ready document.

The layout is:

```text
text_to_speech/
├── curriculum/
│   ├── Lesson 2.docx
│   ├── ledger.json
│   └── .preparation/Lesson 2.docx/<source-sha256>/
│       ├── source.docx
│       ├── plan.json
│       ├── review.md
│       └── narration.audit.json
└── workload/
    └── Lesson 2.txt
```

`curriculum/ledger.json` marks a document complete only after successful text and
audit export. It records the source hash, preparation hash, output path, and
output hash. Unchanged completed documents are skipped on later runs. Pending
reviews and failures are retained for retry; originals stay in `curriculum/`.
No source renaming or moving is needed.

A changed DOCX receives a new preparation directory. Changes to an existing
preparation also cause validation and export to run again. Missing exports can
be recreated from completed preparation files. Changed or unrelated output text
is reported as a conflict and preserved. A previously generated, unchanged
export can be replaced when a revised source or plan is ready.

Only immediate `.docx` files are scanned (case-insensitive extension). Word's
`~$` lock files and subdirectories are ignored. One bad document does not prevent
other documents from being prepared. Any failed document makes the batch command
exit with status 1; documents awaiting editorial work are reported as pending.

Optional path overrides are available:

```sh
.venv/bin/python prepare_curriculum.py batch \
  --curriculum-dir /path/to/curriculum --workload-dir /path/to/workload
```

Explicit one-document initialization is still supported:

```sh
.venv/bin/python prepare_curriculum.py init /path/to/lesson.docx /path/to/lesson-prep
```

The preparation directory contains:

| File | Purpose |
|---|---|
| `source.docx` | Unchanged source copy |
| `manifest.json` | Ordered source blocks, style/emphasis metadata, raw XML, citation candidates, and table cells |
| `templates.json` | User-provided opening/closing and approved variable substitutions |
| `plan.json` | Proposed narration, bridges, citation inventory, reasons, and table coverage |
| `passages.json` | Saved Crossway ESV responses, verified verse spans, and provenance |
| `approvals.json` | Tool-recorded approvals of particular proposals |
| `queue.json` | Default-scan source/output locations, when managed by the batch workflow |

In the commands below, replace `/path/to/lesson-prep` with the preparation path
reported by the scan or recorded in the ledger.

Both templates start empty deliberately. Supply the static text before rendering:

```json
{
  "opening": "USER-PROVIDED OPENING WITH ${title} IF APPROVED",
  "closing": "USER-PROVIDED CLOSING",
  "variables": {"title": "The lesson title"}
}
```

Fetch the plan's verse readings using Crossway's
[official passage-lookup example](https://api.esv.org/docs/samples/).
Create an application key through [Crossway](https://api.esv.org/docs/) and set
`ESV_API_KEY` in the environment. The tool never writes the key into project files.
For example, in zsh, read it without echoing it:

```sh
read -s 'ESV_API_KEY?Crossway API key: '
export ESV_API_KEY
.venv/bin/python prepare_curriculum.py fetch-passages /path/to/lesson-prep
```

The command resolves unmapped citation candidates automatically, while explicit
`passages` keys supplied by the editor control contextual/ambiguous references.
Review those mappings before narration. The retrieval rules are:

- Fetch single verses and explicit verse ranges within one chapter, such as
  `John 3:16`, `John 3:16-18`, or `Psalm 3:1-5`.
- Expand grouped verse references in order: `John 3:16, 18; 4:1-3` becomes three
  lookups, never a chapter lookup.
- Keep chapter-only citations such as `Psalm 23` and `John 3-5` in the narration
  without fetching their text. The command marks these `reference_only: true`.
- Reject open-ended, ambiguous, or cross-chapter verse spans. Resolve them to
  separate explicit, partial-chapter readings in the plan; do not expand chapters.
- A range beginning with verse 1 gets a preliminary **single-verse** lookup to
  check Crossway's chapter-boundary metadata. If the range covers the whole
  chapter, it is rejected before that range is requested. For example,
  `Psalm 117:1-2` is blocked even though its spelling includes verse numbers.
- Verify the returned verse IDs exactly against each request. Empty, corrected,
  truncated, or malformed results stop the command instead of creating a reading.
- Save successful lookups as they complete, and reuse unchanged saved results
  on subsequent runs. A failed run can resume without repeating completed spans.
  API text is marked verified by its official origin and exact verse-ID match;
  the editor still checks reference interpretation and spoken placement.

New results record the request parameters, retrieval time, original response
text, whitespace-only flattening, and text hash. They include an introduction
identifying ESV. Keep appropriate ESV attribution in the approved static text.
Existing conflicting or edited passage records are not silently overwritten.
No key is required when all lookups are already saved or only chapters occur.
Requests are paced; retained Crossway text is capped at 500 distinct verses per
preparation project. Crossway's [usage and storage terms](https://api.esv.org/)
also apply across your projects; the command does not inventory other copies.

Generate a concrete review after the editor has populated the plan:

```sh
.venv/bin/python prepare_curriculum.py review /path/to/lesson-prep
```

Read `review.md` for the original text, proposals, differences, verse inputs,
and table coverage. Every changed source paragraph, table, and editor-flagged
complex block needs explicit user confirmation. Simple added bridges and
verified readings leave the source paragraph intact and do not require a
separate block approval. The editor must still check their fidelity.

After the user confirms a specific proposal, record it:

```sh
.venv/bin/python prepare_curriculum.py approve /path/to/lesson-prep \
  --block b0003 --reviewer 'Scott'
.venv/bin/python prepare_curriculum.py render /path/to/lesson-prep
```

Approvals cover the source hash, exact block proposal, templates, and supplied
passages. Changing those inputs requires renewed approval of affected reviewed
blocks. These are local workflow records, not cryptographic signatures or
authentication of a reviewer. The editor must not approve its own proposals.

Successful rendering writes `text_to_speech/workload/<source-stem>.txt` and keeps
`narration.audit.json` in the preparation directory. The audit records the output
path, source/output hashes, and all editorial inputs. Rendering a batch-managed
preparation also updates its completion ledger. Identical inputs produce
identical narration. Rendering does not launch speech synthesis. Use the export
only after a successful render of the current plan. A custom output directory
can be selected with `render PREPARATION_DIRECTORY --workload-dir DIRECTORY`.

Feed that text to the existing ordinary narration pipeline:

```sh
.venv/bin/python tts_project.py workload/lesson.txt \
  --project-name lesson-narration --chunk-mode paragraph
```

For a cloned voice, use the text input with the existing commands in
[VOICE_CLONING.md](VOICE_CLONING.md). The preparation workflow does not alter
voice configuration or synthesis behavior. Existing audio projects freeze their
source; use a new audio project when the approved manuscript changes.

## Limits of the first version

- Supports DOCX body paragraphs and ordinary tables, including hyperlinks,
  paragraph styles, list metadata, emphasis, and raw table merge XML. Automatic
  list labels are metadata rather than spoken text: the editor must narrate
  numbering or hierarchy when it carries meaning. Empty paragraphs are ignored.
- Rejects nested tables, tracked changes, notes, drawings/text boxes, equations,
  content controls, dynamic body fields, unsupported body elements, and textual
  headers/footers. This avoids silently losing material. Prepare an accessible
  text equivalent or a reviewed copy when needed; preserve the original.
- Citation detection is a convenience, not a complete Bible parser. An editor
  must check omitted or unusual forms and explicitly complete the inventory.
- ESV passages can be fetched with `fetch-passages` or supplied locally after
  checking an actual source. No built-in Bible or LLM-generated verse text is
  included. Manually supplied provenance and already-quoted passages require
  an editorial comparison.
- Readings default to the end of their source block. A citation's optional
  `insert_at` character offset places grouped readings at a sentence boundary
  within the proposed narration. The editor reviews placement and introductions.
- Rendering refuses text the existing TTS normalizer would change, including
  repeated spaces, tabs, soft hyphens, non-NFC text, or line-break hyphenation.
  Resolve those formatting details explicitly in the proposal before approval.
  Blank lines keep paragraphs separate for the pipeline's paragraph chunk mode.
- No prepared DOCX is written; the deliverable is the narration text plus review
  and audit artifacts. The source DOCX remains available for reading along.
