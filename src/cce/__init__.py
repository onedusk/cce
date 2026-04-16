from __future__ import annotations

import os
from pathlib import Path


def load_env_file(path: Path | str = ".env") -> None:
    """Minimal `.env` loader — deliberately avoids the python-dotenv dependency.

    Reads KEY=VALUE lines from the given file and sets them in ``os.environ``
    with ``setdefault`` semantics (existing env vars always win). Lines
    starting with ``#`` are ignored; blank lines are skipped; values are
    stripped of surrounding whitespace and a single layer of quoting.

    This is the shared implementation for runner scripts; each runner used
    to carry its own 7-line inline copy (audit A5).
    """
    p = Path(path)
    if not p.exists():
        return
    for raw_line in p.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)
