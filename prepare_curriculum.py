#!/usr/bin/env python3
"""Source-tree launcher for the separately reviewed DOCX narration workflow."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from deterministic_tts.curriculum import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
