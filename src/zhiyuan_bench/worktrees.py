"""Resolve branch candidates into isolated Git worktrees."""

from __future__ import annotations

import re
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path

from zhiyuan_bench.models import Candidate

LABEL_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")


def parse_branch(value: str) -> tuple[str, str]:
    try:
        label, source_ref = value.split("=", 1)
    except ValueError as error:
        raise ValueError("Branch candidate must use LABEL=GIT_REF") from error
    label = label.strip()
    source_ref = source_ref.strip()
    if not label or LABEL_PATTERN.fullmatch(label) is None:
        raise ValueError(f"Invalid candidate label: {label!r}")
    if not source_ref:
        raise ValueError("Git ref must not be empty")
    return label, source_ref


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def resolve_revision(repo: Path, source_ref: str) -> str:
    result = _git(
        repo, "rev-parse", "--verify", "--end-of-options", f"{source_ref}^{{commit}}"
    )
    revision = result.stdout.strip().lower()
    if re.fullmatch(r"[0-9a-f]{40}", revision) is None:
        raise RuntimeError(
            f"Git ref {source_ref!r} did not resolve to a full commit SHA"
        )
    return revision


def create_branch_candidates(
    repo: Path,
    branch_values: list[str],
    output_root: Path,
) -> list[Candidate]:
    repo = repo.expanduser().resolve()
    if not repo.is_dir():
        raise ValueError(f"Candidate repository does not exist: {repo}")
    _git(repo, "rev-parse", "--show-toplevel")
    parsed = [parse_branch(value) for value in branch_values]
    labels = [label for label, _source_ref in parsed]
    if len(labels) != len(set(labels)):
        raise ValueError("Branch candidate labels must be unique")

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    group = (
        output_root.expanduser().resolve()
        / "worktrees"
        / f"{stamp}-{uuid.uuid4().hex[:8]}"
    )
    candidates: list[Candidate] = []
    try:
        for label, source_ref in parsed:
            revision = resolve_revision(repo, source_ref)
            root = group / label
            root.parent.mkdir(parents=True, exist_ok=True)
            _git(repo, "worktree", "add", "--detach", str(root), revision)
            candidates.append(
                Candidate(
                    label=label,
                    root=root,
                    revision=revision,
                    source_ref=source_ref,
                    source_repo=repo,
                    managed_worktree=True,
                )
            )
    except BaseException:
        cleanup_worktrees(candidates)
        raise
    return candidates


def resolve_branch_revisions(
    repo: Path, branch_values: list[str]
) -> list[tuple[str, str, str]]:
    """Resolve labelled refs without creating worktrees."""
    repo = repo.expanduser().resolve()
    if not repo.is_dir():
        raise ValueError(f"Candidate repository does not exist: {repo}")
    _git(repo, "rev-parse", "--show-toplevel")
    parsed = [parse_branch(value) for value in branch_values]
    labels = [label for label, _source_ref in parsed]
    if len(labels) != len(set(labels)):
        raise ValueError("Branch candidate labels must be unique")
    return [
        (label, source_ref, resolve_revision(repo, source_ref))
        for label, source_ref in parsed
    ]


def create_resolved_candidates(
    repo: Path,
    resolved: list[tuple[str, str, str]],
    group: Path,
) -> list[Candidate]:
    """Create one reusable worktree per already-resolved campaign candidate."""
    repo = repo.expanduser().resolve()
    group = group.expanduser().resolve()
    candidates: list[Candidate] = []
    try:
        for label, source_ref, revision in resolved:
            root = group / label
            root.parent.mkdir(parents=True, exist_ok=True)
            _git(repo, "worktree", "add", "--detach", str(root), revision)
            candidates.append(
                Candidate(
                    label=label,
                    root=root,
                    revision=revision,
                    source_ref=source_ref,
                    source_repo=repo,
                    managed_worktree=True,
                )
            )
    except BaseException:
        cleanup_worktrees(candidates)
        raise
    return candidates


def cleanup_worktrees(candidates: list[Candidate]) -> None:
    for candidate in reversed(candidates):
        if not candidate.managed_worktree or candidate.source_repo is None:
            continue
        _git(
            candidate.source_repo,
            "worktree",
            "remove",
            "--force",
            str(candidate.root),
        )
