#!/usr/bin/env python3
"""Run the manual WordPress album metadata CLI."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from metadata_cli.cli import main  # noqa: E402  # pyright: ignore[reportMissingImports]


if __name__ == "__main__":
    raise SystemExit(main())
