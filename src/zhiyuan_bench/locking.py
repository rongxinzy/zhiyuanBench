"""Portable atomic lock that refuses concurrent runners."""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path


def _process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


class RunLock:
    def __init__(self, path: Path, run_id: str) -> None:
        self.path = path
        self.run_id = run_id
        self.token = uuid.uuid4().hex
        self.acquired = False

    def __enter__(self) -> "RunLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"pid": os.getpid(), "run_id": self.run_id, "token": self.token}
        while True:
            try:
                descriptor = os.open(
                    self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
                )
            except FileExistsError:
                existing = self._read()
                if _process_alive(int(existing.get("pid", -1))):
                    raise RuntimeError(
                        f"Another zhiyuanBench runner is active: {existing}"
                    )
                self.path.unlink(missing_ok=True)
                continue
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as target:
                json.dump(payload, target, sort_keys=True)
                target.write("\n")
            self.acquired = True
            return self

    def _read(self) -> dict[str, object]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def __exit__(self, *_args: object) -> None:
        if not self.acquired:
            return
        existing = self._read()
        if existing.get("token") == self.token:
            self.path.unlink(missing_ok=True)
        self.acquired = False
