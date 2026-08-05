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
    min_candidates: int = 1
    max_candidates: int | None = None


@dataclass(frozen=True)
class Candidate:
    label: str
    root: Path
    revision: str

    def as_dict(self) -> dict[str, str]:
        return {
            "label": self.label,
            "root": str(self.root),
            "revision": self.revision,
        }


@dataclass(frozen=True)
class Phase:
    id: str
    label: str
    command: tuple[str, ...]
    environment: dict[str, str]
    track_containers: bool = False

