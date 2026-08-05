"""Isolation-aware cleanup for containers created by one evaluation phase."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Iterable, Sequence

CommandRunner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


def _default_run(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=True, capture_output=True, text=True)


class ContainerTracker:
    def __init__(
        self,
        docker_host: str,
        *,
        name_filter: str = "name=inspect-zhiyuan_agen",
        run_command: CommandRunner = _default_run,
    ) -> None:
        self.docker_host = docker_host
        self.name_filter = name_filter
        self.run_command = run_command

    def _docker(self, *arguments: str) -> list[str]:
        return ["docker", "-H", self.docker_host, *arguments]

    def ids(self) -> set[str]:
        result = self.run_command(
            self._docker("ps", "-a", "--filter", self.name_filter, "--format", "{{.ID}}")
        )
        return {line.strip() for line in result.stdout.splitlines() if line.strip()}

    def cleanup_created(self, before: Iterable[str]) -> tuple[list[str], list[str]]:
        created = sorted(self.ids().difference(before))
        removed: list[str] = []
        skipped: list[str] = []
        for container_id in created:
            inspect_result = self.run_command(
                self._docker("inspect", container_id, "--format", "{{json .Mounts}}")
            )
            mounts = json.loads(inspect_result.stdout.strip() or "[]")
            if mounts:
                skipped.append(container_id)
                continue
            self.run_command(self._docker("stop", "-t", "2", container_id))
            self.run_command(self._docker("rm", container_id))
            removed.append(container_id)
        return removed, skipped
