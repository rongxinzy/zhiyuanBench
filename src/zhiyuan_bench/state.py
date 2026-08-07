"""Atomic run manifest persistence."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from zhiyuan_bench.persistence import atomic_write_text


def read_manifest(run_dir: Path) -> dict[str, Any]:
    path = run_dir / "manifest.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise ValueError(f"Unsupported run manifest: {path}")
    return value


def write_manifest(run_dir: Path, manifest: dict[str, Any]) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        run_dir / "manifest.json",
        json.dumps(manifest, indent=2, ensure_ascii=True, sort_keys=True) + "\n",
    )
