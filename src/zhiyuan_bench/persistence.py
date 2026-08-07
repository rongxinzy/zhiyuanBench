"""Small, retrying helpers for local state persistence."""

from __future__ import annotations

import os
import time
import uuid
from pathlib import Path


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}")
    try:
        temporary.write_text(content, encoding="utf-8", newline="\n")
        for attempt in range(7):
            try:
                os.replace(temporary, path)
                return
            except PermissionError:
                if attempt == 6:
                    raise
                time.sleep(0.01 * (2**attempt))
    finally:
        temporary.unlink(missing_ok=True)
