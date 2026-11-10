# Local Voice-Cloning Tool

The `voice-clone` tool creates resumable, fully local voice-cloning projects on
Apple Silicon. It uses a recording only when the user explicitly attests that
they have permission to clone that voice.

The recommended model is Qwen3-TTS 12Hz 0.6B Base BF16 through MLX-Audio. Model
weights are never downloaded implicitly. Once a verified model has been placed
under `models/`, synthesis forces Hugging Face, Transformers, and Datasets into
offline mode.

Until the console entry point is refreshed with an editable installation, run
the source-tree launcher shown below. All commands should be run from the
repository root.

## Safety boundaries

- `assess` and `plan` are read-only and make no network requests.
- `prepare-reference` requires `--confirm-permission`.
- `synthesize` accepts only a model carrying a verified local manifest.
- `synthesize` cannot download missing weights.
- `download-model` is the only network-capable command and requires the explicit
  `--confirm-download` flag.
- Original reference audio, its normalized copy, the transcript, model revision,
  settings, source hashes, and output hashes are preserved in manifests.

## 1. Inspect the computer

```sh
.venv/bin/python voice_clone.py assess
```

Machine-readable output is available with `--json`:

```sh
.venv/bin/python voice_clone.py assess --json
```

The report deliberately omits serial numbers and other hardware identifiers.
Metal can be unavailable to a sandboxed process even when the Mac supports it;
run the assessment in a normal Terminal session for the definitive runtime
check.

## 2. Review the model plan

```sh
.venv/bin/python voice_clone.py plan
```

This compares Qwen3-TTS, Chatterbox, and F5-TTS, then prints the repository,
license, expected download size, target directory, and exact authorized download
command. It does not contact Hugging Face.

Available Qwen configurations are:

| Key | Use | Approximate size |
|---|---|---:|
| `qwen3-0.6b-bf16` | Recommended balance of resemblance, quality, and speed | 2.6 GB |
| `qwen3-1.7b-bf16` | Larger quality challenger for later comparison | 4.6 GB |

The Qwen checkpoints are Apache-2.0 licensed. Chatterbox is the recommended
future comparison model and is MIT licensed. F5-TTS code is MIT, but its official
weights are CC-BY-NC-4.0 and are therefore not the default.

## 3. Download an approved model

Do not run this command until the plan and network download have been approved:

```sh
.venv/bin/python voice_clone.py download-model qwen3-0.6b-bf16 \
  --models-dir models \
  --confirm-download
```

The command resolves the current Hugging Face revision to an immutable commit,
downloads into a temporary directory, verifies that configuration and
SafeTensors files exist, hashes every model file, writes
`model-manifest.json`, and then atomically adopts the model directory.

Every synthesis verifies the recorded hashes before loading the model. This is
deliberately strict and may take several seconds for a multi-gigabyte model.

## 4. Prepare an authorized reference voice

Use one clean speaker, little or no background noise, and an exact transcript.
Clips from 5–15 seconds generally work well; the tool accepts 3–30 seconds.

```sh
.venv/bin/python voice_clone.py prepare-reference narrator reference.wav \
  --transcript-file reference.txt \
  --confirm-permission
```

An inline transcript is also accepted:

```sh
.venv/bin/python voice_clone.py prepare-reference narrator reference.wav \
  --transcript "These are the exact words spoken in the recording." \
  --confirm-permission
```

The resulting profile is stored under `voice_references/narrator/`. The original
recording is preserved, while `reference.wav` is a deterministic 24 kHz mono PCM
copy used by the model.

## 5. Synthesize text entirely offline

Supply a text file, PDF, image, or ordered image folder accepted by the main TTS
pipeline:

```sh
.venv/bin/python voice_clone.py synthesize manuscript.txt \
  --reference voice_references/narrator \
  --project-name cloned-narration
```

The completed output is:

```text
voice_projects/cloned-narration/audio/cloned-narration.mp3
```

Important controls include:

| Option | Default | Purpose |
|---|---:|---|
| `--model` | `qwen3-0.6b-bf16` | Select a verified local Qwen configuration |
| `--language` | `english` | Qwen generation language |
| `--chunk-mode` | `paragraph` | Process one normal paragraph per speech request |
| `--chunk-chars` | `2500` | Maximum chunk size before boundary-aware splitting |
| `--terminal-ellipsis` | `period` | Render a request-final `...` or `…` as a complete sentence ending |
| `--pause-ms` | `250` | Silence between chunks |
| `--temperature` | `0.9` | Sampling temperature |
| `--top-k` | `50` | Token candidate limit |
| `--top-p` | `1.0` | Nucleus sampling threshold |
| `--repetition-penalty` | `1.5` | Qwen ICL repetition control |
| `--mp3-bitrate` | `128k` | Final MP3 bitrate |

Extraction and synthesis settings are frozen when the project is created.

In paragraph mode, adjacent paragraphs are never combined. A paragraph longer
than `--chunk-chars` is split at sentence boundaries, with word-boundary
splitting only when one sentence is itself too long. The accepted range is
100-3,000 characters. The conservative 2,500-character default stays below the
local MLX Qwen3-TTS generation budget for ordinary English while avoiding very
long requests that are more prone to pacing drift or truncation. A character
count is not a model-level guarantee because language, speaking rate, pauses,
and tokenization all affect generated duration.

Qwen treats a terminal ellipsis as a request to trail off, which can sound like
the last word was clipped. By default, only an ellipsis at the end of an
individual synthesis request is changed to a period. Ellipses inside the text
remain unchanged, and `text/normalized.txt` preserves the source verbatim; the
exact prepared request is stored under `text/chunks/`. Use
`--terminal-ellipsis preserve` when trailing-off delivery is intentional. This
setting is frozen with a new project like the other synthesis settings.

### Runaway-generation protection

Qwen voice cloning is autoregressive: rarely, it can fail to produce its
end-of-speech token and generate garbled codec audio until its token ceiling.
The pipeline treats generated audio as untrusted until it passes these checks:

- The request receives a text-derived duration and audio-token budget, bounded
  by Qwen's 4,096-token ceiling.
- Reaching that budget is a failed generation, never a completed chunk.
- Audio whose duration is implausibly long for its word count is also rejected.
- A rejected paragraph is deterministically split at sentence boundaries and
  retried in groups of at most 600 characters. Further failures are split more
  finely, up to three fallback levels.
- Only validated audio is moved into the resumable chunk location or assembled
  into the final MP3.

The ledger records `metrics`, `validation_failures`, and `fallback` details for
each affected chunk, including duration, word count, words per minute, token
budget, and the preserved retry text under `text/fallback/`. Run status to see
a summary:

```sh
.venv/bin/python voice_clone.py status voice_projects/<project-name>
```

These frozen defaults are recorded in each new voice project:

| Safety setting | Default | Purpose |
|---|---:|---|
| Qwen maximum audio tokens | `4096` | Absolute per-request ceiling |
| Minimum plausible pace | `80 WPM` | Builds a conservative text-derived duration budget |
| Duration margin | `20 seconds` | Allows pauses and expressive delivery |
| Fallback text size | `600 characters` | Sentence-group size after a rejected request |
| Fallback depth | `3` | Maximum recursive safety splits |
| Fallback pause | `150 ms` | Pause between replacement sentence groups |

### Reference alignment

The transcript must cover every spoken word in the reference recording,
including fillers, repeated words, and false starts. A mismatch between the
reference audio codes and transcript can provoke Qwen ICL degeneration. The
tool accepts clean references from 3-30 seconds but recommends 5-15 seconds.
It records estimated transcript pace and prints warnings for unusually short,
long, or implausibly transcribed references. This is a deterministic sanity
check, not speech recognition; listen to the clip while verifying its exact
transcript.

## Resuming and inspecting projects

Resume after interruption by passing the project directory:

```sh
.venv/bin/python voice_clone.py synthesize voice_projects/cloned-narration
```

Inspect its ledger without running inference:

```sh
.venv/bin/python voice_clone.py status voice_projects/cloned-narration
```

Completed chunks are hash-verified and reused. Working WAV files remain while a
run is incomplete so it can resume, then are removed after the final MP3 is
successfully encoded and verified.

## Project and reference layout

```text
voice_references/<profile>/
├── profile.json
├── reference.wav
├── transcript.txt
└── source/<original recording>

voice_projects/<project>/
├── project.json
├── ledger.json
├── source/<text and copied reference audio>
├── text/<extracted, normalized, and chunked text>
└── audio/<project>.mp3
```

## Determinism

The tool deterministically records source ordering, normalized reference audio,
model revision, model hashes, reference transcript, sampling settings, chunk
text, random seed inputs, and output artifacts. Neural generation is stochastic;
bit-identical regeneration is not guaranteed after changing MLX-Audio, model
weights, macOS, or hardware. Within an existing project, completed artifacts are
reused rather than regenerated.

