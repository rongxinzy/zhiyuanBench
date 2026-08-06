"""Prompt-free JSONL progress event recording and monitoring."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO


class EventSink:
    def __init__(
        self,
        path: Path,
        run_id: str,
        stream: TextIO | None = None,
        listener: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.path = path
        self.run_id = run_id
        self.stream = stream
        self.listener = listener
        self.sequence = self._last_sequence()
        path.parent.mkdir(parents=True, exist_ok=True)

    def _last_sequence(self) -> int:
        if not self.path.is_file():
            return 0
        sequence = 0
        with self.path.open(encoding="utf-8") as source:
            for line in source:
                try:
                    event = json.loads(line)
                    sequence = max(sequence, int(event["sequence"]))
                except (KeyError, TypeError, ValueError):
                    continue
        return sequence

    def emit(
        self,
        event_type: str,
        *,
        phase: str | None = None,
        status: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.sequence += 1
        event = {
            "schema_version": 1,
            "sequence": self.sequence,
            "timestamp": datetime.now(UTC).isoformat(),
            "run_id": self.run_id,
            "event_type": event_type,
            "phase": phase,
            "status": status,
            "details": details or {},
        }
        needs_boundary = False
        if self.path.is_file() and self.path.stat().st_size:
            with self.path.open("rb") as source:
                source.seek(-1, 2)
                needs_boundary = source.read(1) != b"\n"
        with self.path.open("a", encoding="utf-8", newline="\n") as target:
            if needs_boundary:
                target.write("\n")
            target.write(json.dumps(event, ensure_ascii=True, sort_keys=True) + "\n")
            target.flush()
        if self.stream is not None:
            print(format_event(event), file=self.stream, flush=True)
        if self.listener is not None:
            self.listener(event)
        return event


def format_event(event: dict[str, Any]) -> str:
    stamp = str(event.get("timestamp", ""))[11:19]
    phase = event.get("phase") or "run"
    status = event.get("status") or event.get("event_type")
    details = event.get("details") or {}
    progress = ""
    if "completed" in details and "total" in details:
        progress = f" {details['completed']}/{details['total']}"
    elif "elapsed_seconds" in details:
        progress = f" elapsed={details['elapsed_seconds']}s"
    return f"[{stamp}] {phase}: {status}{progress}"


def monitor(path: Path, *, follow: bool = False, poll_seconds: float = 0.5) -> None:
    events_path = path / "events.jsonl" if path.is_dir() else path
    position = 0
    while True:
        if events_path.is_file():
            with events_path.open(encoding="utf-8") as source:
                source.seek(position)
                for line in source:
                    if line.strip():
                        print(format_event(json.loads(line)), flush=True)
                position = source.tell()
        if not follow:
            return
        manifest = events_path.parent / "manifest.json"
        if manifest.is_file():
            status = json.loads(manifest.read_text(encoding="utf-8")).get("status")
            if status in {"succeeded", "failed", "cancelled"}:
                return
        time.sleep(poll_seconds)
