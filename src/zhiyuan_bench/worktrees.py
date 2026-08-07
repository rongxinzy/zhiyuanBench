"""Resolve branch candidates into isolated Git worktrees."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path

from zhiyuan_bench.models import Candidate

LABEL_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
POLICY_WORKTREE_DEPENDENCIES = (
    "esbuild",
    "@earendil-works/pi-coding-agent",
)


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


def _package_version(root: Path, package_name: str) -> str | None:
    package_path = root.joinpath(*package_name.split("/"), "package.json")
    if not package_path.is_file():
        return None
    value = json.loads(package_path.read_text(encoding="utf-8"))
    version = value.get("version")
    return version if isinstance(version, str) and version else None


def _locked_package_version(root: Path, package_name: str) -> str | None:
    lock_path = root / "package-lock.json"
    if not lock_path.is_file():
        return None
    value = json.loads(lock_path.read_text(encoding="utf-8"))
    package = value.get("packages", {}).get(f"node_modules/{package_name}", {})
    version = package.get("version")
    return version if isinstance(version, str) and version else None


def _link_or_copy_directory(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.symlink(source, target, target_is_directory=True)
    except OSError:
        if os.name == "nt":
            junction = subprocess.run(
                ["cmd.exe", "/d", "/c", "mklink", "/J", str(target), str(source)],
                capture_output=True,
                text=True,
            )
            if junction.returncode == 0:
                return
        shutil.copytree(source, target)


def _prepare_policy_build_dependencies(repo: Path, root: Path) -> None:
    package_path = root / "package.json"
    if not package_path.is_file():
        return
    package = json.loads(package_path.read_text(encoding="utf-8"))
    scripts = package.get("scripts", {})
    if not isinstance(scripts, dict) or "build:eval-policy" not in scripts:
        return

    source_modules = repo / "node_modules"
    dependencies = list(POLICY_WORKTREE_DEPENDENCIES)
    esbuild_package = source_modules / "esbuild" / "package.json"
    if esbuild_package.is_file():
        esbuild = json.loads(esbuild_package.read_text(encoding="utf-8"))
        optional = esbuild.get("optionalDependencies", {})
        if isinstance(optional, dict):
            dependencies.extend(
                name
                for name in optional
                if isinstance(name, str)
                and source_modules.joinpath(*name.split("/")).is_dir()
            )

    for dependency in dependencies:
        expected = _locked_package_version(root, dependency)
        installed = _package_version(source_modules, dependency)
        if expected is None:
            raise RuntimeError(
                f"Candidate lock file does not declare policy build dependency "
                f"{dependency!r}: {root / 'package-lock.json'}"
            )
        if installed != expected:
            raise RuntimeError(
                f"Source checkout must provide {dependency}@{expected} for candidate "
                f"policy builds; found {installed or 'nothing'} in {source_modules}"
            )

    target_modules = root / "node_modules"
    target_modules.mkdir(exist_ok=True)
    for dependency in dependencies:
        source = source_modules.joinpath(*dependency.split("/"))
        target = target_modules.joinpath(*dependency.split("/"))
        _link_or_copy_directory(source, target)


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
            candidate = Candidate(
                label=label,
                root=root,
                revision=revision,
                source_ref=source_ref,
                source_repo=repo,
                managed_worktree=True,
            )
            candidates.append(candidate)
            _prepare_policy_build_dependencies(repo, root)
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


def list_local_branches(repo: Path) -> list[dict[str, str]]:
    """List local branch names and immutable revisions for UI selection."""
    repo = repo.expanduser().resolve()
    if not repo.is_dir():
        raise ValueError(f"Candidate repository does not exist: {repo}")
    result = _git(
        repo,
        "for-each-ref",
        "--sort=refname",
        "--format=%(refname:short)%09%(objectname)",
        "refs/heads",
    )
    branches: list[dict[str, str]] = []
    for line in result.stdout.splitlines():
        name, separator, revision = line.partition("\t")
        if separator and re.fullmatch(r"[0-9a-fA-F]{40}", revision):
            branches.append({"name": name, "revision": revision.lower()})
    return branches


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
            candidate = Candidate(
                label=label,
                root=root,
                revision=revision,
                source_ref=source_ref,
                source_repo=repo,
                managed_worktree=True,
            )
            candidates.append(candidate)
            _prepare_policy_build_dependencies(repo, root)
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
