"""Immutable registry and run configuration models."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class BridgeDefinition:
    id: str
    description: str
    capabilities: frozenset[str]
    priority: int = 100


@dataclass(frozen=True)
class SuiteDefinition:
    id: str
    description: str
    adapter: str
    required_capabilities: frozenset[str]
    expected_samples: int | None
    task: str
    production_policy: bool = False
    preflight: bool = False
    preflight_task_args: tuple[str, ...] = ()
    min_candidates: int = 1
    max_candidates: int | None = None
    required_environment: tuple[str, ...] = ()
    required_host_platforms: tuple[str, ...] = ()
    required_python_modules: tuple[str, ...] = ()
    health_url_environment: tuple[str, ...] = ()
    model_roles: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class Candidate:
    label: str
    root: Path
    revision: str
    source_ref: str | None = None
    source_repo: Path | None = None
    managed_worktree: bool = False

    def as_dict(self) -> dict[str, str | bool]:
        result: dict[str, str | bool] = {
            "label": self.label,
            "root": str(self.root),
            "revision": self.revision,
        }
        if self.source_ref is not None:
            result["source_ref"] = self.source_ref
        if self.source_repo is not None:
            result["source_repo"] = str(self.source_repo)
        if self.managed_worktree:
            result["managed_worktree"] = True
        return result


@dataclass(frozen=True)
class Phase:
    id: str
    label: str
    command: tuple[str, ...]
    environment: dict[str, str]
    track_containers: bool = False
