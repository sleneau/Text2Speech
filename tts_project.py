#!/usr/bin/env python3
"""Source-tree launcher; the installed equivalent is `tts-project`."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from deterministic_tts.cli import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())

