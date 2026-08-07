"""Persistent multi-suite campaigns with shared Git worktrees."""

from __future__ import annotations

import json
import re
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from zhiyuan_bench.events import EventSink
from zhiyuan_bench.locking import RunLock
from zhiyuan_bench.models import Candidate
from zhiyuan_bench.persistence import atomic_write_text
from zhiyuan_bench.registry import select_bridge, suite_by_id
from zhiyuan_bench.reports import write_campaign_report
from zhiyuan_bench.runner import SuiteUnavailableError, create_run, run_manifest
from zhiyuan_bench.worktrees import (
    cleanup_worktrees,
    create_resolved_candidates,
    resolve_branch_revisions,
)

CAMPAIGN_SCHEMA_VERSION = 1
CAMPAIGN_SUITES = (
    "agentbench-os-dev",
    "codeipi",
    "agentdojo",
    "tau2-airline",
    "tau2-banking",
    "tau2-retail",
    "tau2-telecom",
    "bfcl-single-turn",
)


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    atomic_write_text(
        path,
        json.dumps(value, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
    )


def read_campaign(campaign_dir: Path) -> dict[str, Any]:
    path = campaign_dir / "campaign.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != CAMPAIGN_SCHEMA_VERSION
    ):
        raise ValueError(f"Unsupported campaign manifest: {path}")
    return value


def write_campaign(campaign_dir: Path, manifest: dict[str, Any]) -> None:
    _atomic_json(campaign_dir / "campaign.json", manifest)
    summary = campaign_summary(manifest)
    _atomic_json(campaign_dir / "live-summary.json", summary)
    write_campaign_report(campaign_dir, summary)


def _slug(value: str, *, limit: int = 12) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", value).strip("-").lower()
    return (slug or "ref")[:limit].rstrip("-")


def campaign_id(
    resolved: list[tuple[str, str, str]],
    *,
    now: datetime | None = None,
    nonce: str | None = None,
) -> str:
    stamp = (now or datetime.now(UTC)).astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    refs = "__".join(
        f"{_slug(source_ref)}-{revision[:8]}"
        for _label, source_ref, revision in resolved
    )
    return f"{stamp}__{refs}__{(nonce or uuid.uuid4().hex[:4]).lower()}"


def _candidate_from_dict(value: dict[str, Any]) -> Candidate:
    return Candidate(
        label=str(value["label"]),
        root=Path(str(value["root"])),
        revision=str(value["revision"]),
        source_ref=str(value["source_ref"]),
        source_repo=Path(str(value["source_repo"])),
        managed_worktree=bool(value.get("managed_worktree", False)),
    )


def _legacy_suite_progress(
    suite: dict[str, Any], manifest: dict[str, Any]
) -> tuple[dict[str, int], dict[str, int] | None]:
    """Translate phase-only progress written by older campaign manifests."""
    candidates = manifest["candidates"]
    expected = _suite_progress(
        str(suite["id"]),
        limit=manifest.get("limit"),
        candidate_count=len(candidates),
    )
    progress = suite.get("progress")
    if not isinstance(progress, dict) or progress.get("total") == expected["total"]:
        return progress or expected, suite.get("phase_progress")

    phase = str(suite.get("phase") or "")
    phase_progress = progress
    if suite.get("status") == "succeeded" or phase == "report":
        expected["completed"] = expected["total"]
        return expected, phase_progress

    samples_per_candidate = expected["total"] // len(candidates) if candidates else 0
    for index, candidate in enumerate(candidates):
        label = str(candidate["label"])
        prior_samples = index * samples_per_candidate
        if phase == f"eval-{label}":
            expected["completed"] = prior_samples + int(progress.get("completed", 0))
            break
        if phase in {
            f"preflight-{label}",
            f"validate-preflight-{label}",
        }:
            expected["completed"] = prior_samples
            break
        if phase == f"validate-full-{label}":
            expected["completed"] = prior_samples + samples_per_candidate
            break
    return expected, phase_progress


def campaign_summary(manifest: dict[str, Any]) -> dict[str, Any]:
    suites = []
    for suite in manifest["suites"]:
        summary_suite = {
            key: suite.get(key)
            for key in (
                "id",
                "bridge",
                "status",
                "phase",
                "progress",
                "phase_progress",
                "run_dir",
                "failure",
                "attempts",
                "started_at",
                "completed_at",
            )
            if suite.get(key) is not None
        }
        progress, phase_progress = _legacy_suite_progress(suite, manifest)
        summary_suite["progress"] = progress
        if phase_progress is not None:
            summary_suite["phase_progress"] = phase_progress
        suites.append(summary_suite)
    counts = {
        status: sum(suite["status"] == status for suite in manifest["suites"])
        for status in ("created", "running", "succeeded", "skipped", "failed")
    }
    return {
        "schema_version": CAMPAIGN_SCHEMA_VERSION,
        "campaign_id": manifest["campaign_id"],
        "status": manifest["status"],
        "created_at": manifest["created_at"],
        "started_at": manifest.get("started_at"),
        "completed_at": manifest.get("completed_at"),
        "current_suite": manifest.get("current_suite"),
        "candidates": [
            {
                "label": item["label"],
                "source_ref": item["source_ref"],
                "revision": item["revision"],
            }
            for item in manifest["candidates"]
        ],
        "counts": counts,
        "suites": suites,
    }


def _suite_progress(
    suite_id: str, *, limit: int | None, candidate_count: int
) -> dict[str, int]:
    expected = limit or suite_by_id(suite_id).expected_samples
    return {"completed": 0, "total": (expected or 0) * candidate_count}


def create_campaign(
    *,
    repo: Path,
    branch_values: list[str],
    suite_ids: list[str],
    workspace: Path,
    records_root: Path,
    limit: int | None = None,
    concurrency: int = 1,
    reviewer_required_candidates: set[str] | None = None,
) -> Path:
    if not suite_ids:
        raise ValueError("At least one campaign suite is required")
    if len(suite_ids) != len(set(suite_ids)):
        raise ValueError("Campaign suites must be unique")
    unsupported = sorted(set(suite_ids) - set(CAMPAIGN_SUITES))
    if unsupported:
        raise ValueError("Unsupported campaign suites: " + ", ".join(unsupported))
    if limit is not None and limit <= 0:
        raise ValueError("limit must be positive")
    if concurrency <= 0:
        raise ValueError("concurrency must be positive")
    workspace = workspace.expanduser().resolve()
    if not workspace.is_dir():
        raise ValueError(f"Evaluation workspace does not exist: {workspace}")

    resolved = resolve_branch_revisions(repo, branch_values)
    labels = {label for label, _source_ref, _revision in resolved}
    reviewer_required = (
        labels
        if reviewer_required_candidates is None
        else reviewer_required_candidates
    )
    unknown = reviewer_required - labels
    if unknown:
        raise ValueError(
            "Reviewer-required candidates are not configured: "
            + ", ".join(sorted(unknown))
        )
    for suite_id in suite_ids:
        suite = suite_by_id(suite_id)
        if len(resolved) < suite.min_candidates or (
            suite.max_candidates is not None and len(resolved) > suite.max_candidates
        ):
            raise ValueError(
                f"Suite {suite.id} accepts {suite.min_candidates}.."
                f"{suite.max_candidates or 'many'} candidates"
            )

    campaign_dir = records_root.expanduser().resolve() / campaign_id(resolved)
    candidates = create_resolved_candidates(repo, resolved, campaign_dir / "worktrees")
    created_at = datetime.now(UTC).isoformat()
    manifest: dict[str, Any] = {
        "schema_version": CAMPAIGN_SCHEMA_VERSION,
        "campaign_id": campaign_dir.name,
        "status": "created",
        "created_at": created_at,
        "repo": str(repo.expanduser().resolve()),
        "workspace": str(workspace),
        "records_root": str(records_root.expanduser().resolve()),
        "limit": limit,
        "concurrency": concurrency,
        "reviewer_required_candidates": sorted(reviewer_required),
        "candidates": [candidate.as_dict() for candidate in candidates],
        "suites": [
            {
                "id": suite_id,
                "bridge": select_bridge(suite_by_id(suite_id)).id,
                "status": "created",
                "attempts": 0,
                "progress": _suite_progress(
                    suite_id, limit=limit, candidate_count=len(candidates)
                ),
                "full_progress_by_phase": {},
            }
            for suite_id in suite_ids
        ],
    }
    try:
        write_campaign(campaign_dir, manifest)
        EventSink(campaign_dir / "events.jsonl", campaign_dir.name).emit(
            "campaign_created",
            status="created",
            details={
                "suite_count": len(suite_ids),
                "candidate_count": len(candidates),
            },
        )
    except BaseException:
        cleanup_worktrees(candidates)
        raise
    return campaign_dir


def _record_run_event(
    campaign_dir: Path,
    manifest: dict[str, Any],
    suite_state: dict[str, Any],
    sink: EventSink,
    event: dict[str, Any],
) -> None:
    event_type = str(event.get("event_type", ""))
    phase = str(event.get("phase") or "")
    suite_state["phase"] = phase or None
    details = event.get("details")
    if event_type == "phase_started":
        suite_state.pop("phase_progress", None)
    if event_type == "phase_progress" and isinstance(details, dict):
        suite_state["phase_progress"] = {
            "completed": details.get("completed"),
            "total": details.get("total"),
        }
        if phase.startswith("eval-"):
            by_phase = suite_state.setdefault("full_progress_by_phase", {})
            by_phase[phase] = suite_state["phase_progress"]
            suite_state["progress"]["completed"] = sum(
                int(progress.get("completed", 0))
                for progress in by_phase.values()
                if isinstance(progress, dict)
            )
    if event_type in {"phase_started", "phase_progress", "run_finished"}:
        event_details: dict[str, Any] = {"run_phase": event.get("phase")}
        if event_type == "phase_progress":
            event_details.update(suite_state.get("progress", {}))
            phase_progress = suite_state.get("phase_progress", {})
            event_details.update(
                {
                    "phase_completed": phase_progress.get("completed"),
                    "phase_total": phase_progress.get("total"),
                }
            )
        sink.emit(
            f"suite_{event_type}",
            phase=str(suite_state["id"]),
            status=str(event.get("status") or "running"),
            details=event_details,
        )
    write_campaign(campaign_dir, manifest)


def run_campaign(campaign_dir: Path, *, health_checks: bool = True) -> None:
    campaign_dir = campaign_dir.expanduser().resolve()
    manifest = read_campaign(campaign_dir)
    sink = EventSink(
        campaign_dir / "events.jsonl",
        str(manifest["campaign_id"]),
        stream=sys.stdout,
    )
    candidates = [_candidate_from_dict(item) for item in manifest["candidates"]]
    for suite_state in manifest["suites"]:
        expected_progress = _suite_progress(
            str(suite_state["id"]),
            limit=manifest.get("limit"),
            candidate_count=len(candidates),
        )
        if suite_state.get("progress", {}).get("total") != expected_progress["total"]:
            suite_state["progress"] = expected_progress
            suite_state["full_progress_by_phase"] = {}
    records_root = Path(str(manifest["records_root"]))
    with RunLock(records_root / "campaign.lock", str(manifest["campaign_id"])):
        manifest["status"] = "running"
        manifest["started_at"] = (
            manifest.get("started_at") or datetime.now(UTC).isoformat()
        )
        manifest.pop("completed_at", None)
        write_campaign(campaign_dir, manifest)
        sink.emit("campaign_started", status="running")
        try:
            for suite_index, suite_state in enumerate(manifest["suites"]):
                if suite_state["status"] == "succeeded":
                    continue
                suite_id = str(suite_state["id"])
                manifest["current_suite"] = suite_id
                suite_state["status"] = "running"
                suite_state["attempts"] = int(suite_state.get("attempts", 0)) + 1
                suite_state["started_at"] = datetime.now(UTC).isoformat()
                suite_state.pop("failure", None)
                write_campaign(campaign_dir, manifest)
                sink.emit("suite_started", phase=suite_id, status="running")
                run_dir_value = suite_state.get("run_dir")
                if run_dir_value:
                    run_dir = Path(str(run_dir_value))
                else:
                    run_dir = create_run(
                        suite_id=suite_id,
                        bridge_id=str(suite_state["bridge"]),
                        candidates=candidates,
                        workspace=Path(str(manifest["workspace"])),
                        output_root=campaign_dir / "_runs" / f"{suite_index:02d}",
                        limit=manifest.get("limit"),
                        concurrency=int(manifest["concurrency"]),
                        reviewer_required_candidates=set(
                            manifest["reviewer_required_candidates"]
                        ),
                    )
                    suite_state["run_dir"] = str(run_dir)
                    write_campaign(campaign_dir, manifest)

                def record_event(
                    event: dict[str, Any], state: dict[str, Any] = suite_state
                ) -> None:
                    _record_run_event(campaign_dir, manifest, state, sink, event)

                try:
                    run_manifest(
                        run_dir,
                        health_checks=health_checks,
                        event_listener=record_event,
                    )
                except SuiteUnavailableError as error:
                    suite_state["status"] = "skipped"
                    suite_state["failure"] = {
                        "type": type(error).__name__,
                        "message": str(error),
                    }
                except Exception as error:
                    suite_state["status"] = "failed"
                    suite_state["failure"] = {
                        "type": type(error).__name__,
                        "message": str(error),
                    }
                else:
                    suite_state["status"] = "succeeded"
                suite_state["completed_at"] = datetime.now(UTC).isoformat()
                write_campaign(campaign_dir, manifest)
                sink.emit(
                    "suite_finished", phase=suite_id, status=suite_state["status"]
                )
            manifest.pop("current_suite", None)
            has_issues = any(
                suite["status"] != "succeeded" for suite in manifest["suites"]
            )
            manifest["status"] = "completed_with_issues" if has_issues else "succeeded"
            manifest["completed_at"] = datetime.now(UTC).isoformat()
            write_campaign(campaign_dir, manifest)
            sink.emit("campaign_finished", status=manifest["status"])
        except KeyboardInterrupt:
            manifest["status"] = "cancelled"
            manifest["completed_at"] = datetime.now(UTC).isoformat()
            write_campaign(campaign_dir, manifest)
            sink.emit("campaign_finished", status="cancelled")
            raise
