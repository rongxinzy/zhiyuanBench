"""Suite phase construction, execution, resume, and progress monitoring."""

from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from zhiyuan_bench.containers import ContainerTracker
from zhiyuan_bench.events import EventSink
from zhiyuan_bench.locking import RunLock
from zhiyuan_bench.models import BridgeDefinition, Candidate, Phase, SuiteDefinition
from zhiyuan_bench.registry import select_bridge, suite_by_id
from zhiyuan_bench.state import read_manifest, write_manifest

MODEL_ENVIRONMENT = (
    "ZHIYUAN_MODEL_BASE_URL",
    "ZHIYUAN_MODEL_API_KEY",
    "ZHIYUAN_MODEL_ID",
    "ZHIYUAN_MODEL_CONTEXT_WINDOW",
    "ZHIYUAN_MODEL_MAX_TOKENS",
    "ZHIYUAN_MODEL_TEMPERATURE",
    "ZHIYUAN_MODEL_SEED",
    "ZHIYUAN_HTTP_IDLE_TIMEOUT_MS",
    "ZHIYUAN_PI_THINKING_LEVEL",
    "ZHIYUAN_MODEL_PROFILE",
    "ZHIYUAN_CANDIDATE_POLICY_MAX_ITERATIONS",
    "DOCKER_HOST",
)
PROGRESS_PATTERN = re.compile(
    r"\bSamples:\s*(\d{1,6})\s*/\s*(\d{1,6})(?!\d)", re.IGNORECASE
)


def parse_candidate(value: str) -> Candidate:
    try:
        label, checkout = value.split("=", 1)
        root_value, revision = checkout.rsplit("@", 1)
    except ValueError as error:
        raise ValueError("Candidate must use LABEL=PATH@40_HEX_SHA") from error
    label = label.strip()
    revision = revision.strip().lower()
    if not label or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", label):
        raise ValueError(f"Invalid candidate label: {label!r}")
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError(f"Candidate revision must be a full SHA: {revision!r}")
    root = Path(root_value).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"Candidate root does not exist: {root}")
    return Candidate(label=label, root=root, revision=revision)


def _run_id() -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{uuid.uuid4().hex[:8]}"


def _python(workspace: Path) -> str:
    candidates = (
        workspace / ".venv" / "Scripts" / "python.exe",
        workspace / ".venv" / "bin" / "python",
    )
    return str(next((path for path in candidates if path.is_file()), Path(sys.executable)))


def _npm() -> str:
    return "npm.cmd" if os.name == "nt" else "npm"


def _candidate_environment(
    candidate: Candidate, suite: SuiteDefinition, log_dir: Path
) -> dict[str, str]:
    environment = {
        name: os.environ[name] for name in MODEL_ENVIRONMENT if name in os.environ
    }
    environment.update(
        {
            "ZHIYUAN_CANDIDATE_ROOT": str(candidate.root),
            "ZHIYUAN_CANDIDATE_ID": candidate.revision,
            "INSPECT_LOG_DIR": str(log_dir),
            "PYTHONUTF8": "1",
        }
    )
    if suite.production_policy:
        environment["ZHIYUAN_CANDIDATE_POLICY_MODULE"] = (
            "dist-eval/zhiyuan-evaluation-policy.mjs"
        )
    else:
        environment.pop("ZHIYUAN_CANDIDATE_POLICY_MODULE", None)
    return environment


def build_phases(
    suite: SuiteDefinition,
    bridge: BridgeDefinition,
    candidates: list[Candidate],
    workspace: Path,
    run_dir: Path,
    limit: int | None,
) -> list[Phase]:
    del bridge
    phases: list[Phase] = []
    python = _python(workspace)
    if suite.production_policy:
        for candidate in candidates:
            phases.append(
                Phase(
                    id=f"build-{candidate.label}",
                    label=f"Build policy for {candidate.label}",
                    command=(_npm(), "--prefix", str(candidate.root), "run", "build:eval-policy"),
                    environment={},
                )
            )
    for candidate in candidates:
        if suite.preflight:
            log_dir = run_dir / "evals" / candidate.label / "preflight"
            command = _inspect_command(python, suite, log_dir, 1, preflight=True)
            phases.append(
                Phase(
                    id=f"preflight-{candidate.label}",
                    label=f"Production preflight for {candidate.label}",
                    command=command,
                    environment=_candidate_environment(candidate, suite, log_dir),
                    track_containers=True,
                )
            )
        log_dir = run_dir / "evals" / candidate.label / "full"
        command = _inspect_command(python, suite, log_dir, limit, preflight=False)
        phases.append(
            Phase(
                id=f"eval-{candidate.label}",
                label=f"Evaluate {candidate.label}",
                command=command,
                environment=_candidate_environment(candidate, suite, log_dir),
                track_containers="sandbox" in suite.required_capabilities,
            )
        )
    if suite.adapter == "inspect-agentbench" and len(candidates) == 2:
        report_dir = run_dir / "report"
        expected = limit or suite.expected_samples
        command = (
            python,
            "tools/zhiyuan/report_comparison.py",
            "--baseline",
            str(run_dir / "evals" / candidates[0].label / "full"),
            "--candidate",
            str(run_dir / "evals" / candidates[1].label / "full"),
            "--output-dir",
            str(report_dir),
            "--expected-samples",
            str(expected),
            "--require-production-agent",
        )
        phases.append(
            Phase(
                id="report",
                label="Validate and generate comparison report",
                command=command,
                environment={},
            )
        )
    return phases


def _inspect_command(
    python: str,
    suite: SuiteDefinition,
    log_dir: Path,
    limit: int | None,
    *,
    preflight: bool,
) -> tuple[str, ...]:
    command = [
        python,
        "-m",
        "inspect_ai._cli.main",
        "eval",
        suite.task,
        "--model",
        "mockllm/model",
        "--max-samples",
        "1",
        "--max-connections",
        "1",
        "--display",
        "plain",
        "--log-buffer",
        "1",
        "--log-dir",
        str(log_dir),
    ]
    if preflight:
        command.extend(("--limit", "1", "-T", "require_inspect_tool_call=true"))
    elif limit is not None:
        command.extend(("--limit", str(limit)))
    if suite.adapter == "inspect-bfcl":
        command.extend(("-T", "categories=all_single_turn"))
    return tuple(command)


def _verify_candidate(candidate: Candidate) -> None:
    result = subprocess.run(
        ["git", "-C", str(candidate.root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    actual = result.stdout.strip().lower()
    if actual != candidate.revision:
        raise RuntimeError(
            f"Candidate {candidate.label} expected {candidate.revision}, got {actual}"
        )


def _health_checks(suite: SuiteDefinition, sink: EventSink) -> None:
    base_url = os.environ.get("ZHIYUAN_MODEL_BASE_URL", "").rstrip("/")
    if not base_url or not os.environ.get("ZHIYUAN_MODEL_ID"):
        raise RuntimeError("ZHIYUAN_MODEL_BASE_URL and ZHIYUAN_MODEL_ID are required")
    with urllib.request.urlopen(f"{base_url}/models", timeout=10) as response:
        if response.status != 200:
            raise RuntimeError(f"Model API health check returned {response.status}")
        json.load(response)
    sink.emit("health_check", status="model_api_ok")
    ssh_target = os.environ.get("ZHIYUAN_SSH_TARGET")
    if ssh_target:
        subprocess.run(["ssh", ssh_target, "true"], check=True, timeout=15)
        sink.emit("health_check", status="ssh_ok")
    if "sandbox" in suite.required_capabilities:
        docker_host = os.environ.get("DOCKER_HOST")
        if not docker_host:
            raise RuntimeError("DOCKER_HOST is required by this suite")
        subprocess.run(
            ["docker", "-H", docker_host, "info", "--format", "{{.ServerVersion}}"],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        sink.emit("health_check", status="docker_ok")


def _phase_output_worker(
    source: Any, target: Any, channel: str, output_queue: queue.Queue[tuple[str, str | None]]
) -> None:
    try:
        for line in iter(source.readline, ""):
            target.write(line)
            target.flush()
            output_queue.put((channel, line))
    finally:
        output_queue.put((channel, None))


def execute_phase(
    phase: Phase,
    *,
    workspace: Path,
    run_dir: Path,
    sink: EventSink,
) -> int:
    logs_dir = run_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = logs_dir / f"{phase.id}.stdout.log"
    stderr_path = logs_dir / f"{phase.id}.stderr.log"
    environment = os.environ.copy()
    environment.update(phase.environment)
    sink.emit(
        "phase_started",
        phase=phase.id,
        status="running",
        details={"label": phase.label},
    )
    started = time.monotonic()
    with (
        stdout_path.open("w", encoding="utf-8", newline="\n") as stdout_file,
        stderr_path.open("w", encoding="utf-8", newline="\n") as stderr_file,
    ):
        process = subprocess.Popen(
            phase.command,
            cwd=workspace,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        assert process.stdout is not None and process.stderr is not None
        output_queue: queue.Queue[tuple[str, str | None]] = queue.Queue()
        threads = [
            threading.Thread(
                target=_phase_output_worker,
                args=(process.stdout, stdout_file, "stdout", output_queue),
                daemon=True,
            ),
            threading.Thread(
                target=_phase_output_worker,
                args=(process.stderr, stderr_file, "stderr", output_queue),
                daemon=True,
            ),
        ]
        for thread in threads:
            thread.start()
        closed = 0
        progress: tuple[int, int] | None = None
        last_heartbeat = started
        try:
            while closed < 2 or process.poll() is None:
                try:
                    _channel, line = output_queue.get(timeout=0.5)
                    if line is None:
                        closed += 1
                    else:
                        match = PROGRESS_PATTERN.search(line)
                        if match:
                            observed = (int(match.group(1)), int(match.group(2)))
                            if observed != progress and observed[0] <= observed[1]:
                                progress = observed
                                sink.emit(
                                    "phase_progress",
                                    phase=phase.id,
                                    status="running",
                                    details={"completed": observed[0], "total": observed[1]},
                                )
                except queue.Empty:
                    pass
                now = time.monotonic()
                if now - last_heartbeat >= 10:
                    sink.emit(
                        "phase_heartbeat",
                        phase=phase.id,
                        status="running",
                        details={"elapsed_seconds": round(now - started)},
                    )
                    last_heartbeat = now
        except KeyboardInterrupt:
            process.terminate()
            process.wait(timeout=10)
            raise
        finally:
            for thread in threads:
                thread.join(timeout=2)
            process.stdout.close()
            process.stderr.close()
    return_code = process.wait()
    sink.emit(
        "phase_finished",
        phase=phase.id,
        status="succeeded" if return_code == 0 else "failed",
        details={
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "exit_code": return_code,
            "stdout": str(stdout_path),
            "stderr": str(stderr_path),
        },
    )
    return return_code


def create_run(
    *,
    suite_id: str,
    bridge_id: str,
    candidates: list[Candidate],
    workspace: Path,
    output_root: Path,
    limit: int | None = None,
) -> Path:
    suite = suite_by_id(suite_id)
    bridge = select_bridge(suite, bridge_id)
    if len(candidates) < suite.min_candidates or (
        suite.max_candidates is not None and len(candidates) > suite.max_candidates
    ):
        raise ValueError(
            f"Suite {suite.id} accepts {suite.min_candidates}.."
            f"{suite.max_candidates or 'many'} candidates"
        )
    run_id = _run_id()
    run_dir = output_root.resolve() / "runs" / run_id
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "run_id": run_id,
        "status": "created",
        "suite": suite.id,
        "bridge": bridge.id,
        "workspace": str(workspace.resolve()),
        "output_root": str(output_root.resolve()),
        "limit": limit,
        "candidates": [candidate.as_dict() for candidate in candidates],
        "created_at": datetime.now(UTC).isoformat(),
        "phases": {},
    }
    write_manifest(run_dir, manifest)
    return run_dir


def pending_phases(manifest: dict[str, Any], phases: list[Phase]) -> list[Phase]:
    states = manifest.get("phases", {})
    return [
        phase
        for phase in phases
        if not isinstance(states.get(phase.id), dict)
        or states[phase.id].get("status") != "succeeded"
    ]


def run_manifest(run_dir: Path, *, health_checks: bool = True) -> None:
    manifest = read_manifest(run_dir)
    run_id = str(manifest["run_id"])
    suite = suite_by_id(str(manifest["suite"]))
    bridge = select_bridge(suite, str(manifest["bridge"]))
    workspace = Path(str(manifest["workspace"]))
    output_root = Path(str(manifest["output_root"]))
    candidates = [
        Candidate(
            label=str(item["label"]),
            root=Path(str(item["root"])),
            revision=str(item["revision"]),
        )
        for item in manifest["candidates"]
    ]
    phases = build_phases(
        suite,
        bridge,
        candidates,
        workspace,
        run_dir,
        manifest.get("limit"),
    )
    sink = EventSink(run_dir / "events.jsonl", run_id, stream=sys.stdout)
    tracker = None
    docker_host = os.environ.get("DOCKER_HOST")
    if docker_host and "sandbox" in suite.required_capabilities:
        tracker = ContainerTracker(docker_host)
    with RunLock(output_root / "runner.lock", run_id):
        try:
            manifest["status"] = "running"
            write_manifest(run_dir, manifest)
            sink.emit(
                "run_started",
                status="running",
                details={"suite": suite.id, "bridge": bridge.id},
            )
            for candidate in candidates:
                _verify_candidate(candidate)
            if health_checks:
                _health_checks(suite, sink)
            for phase in phases:
                phase_state = manifest["phases"].setdefault(phase.id, {})
                if phase_state.get("status") == "succeeded":
                    sink.emit("phase_skipped", phase=phase.id, status="succeeded")
                    continue
                before: set[str] = set()
                if phase.track_containers and tracker is not None:
                    stale_before = phase_state.get("containers_before")
                    if phase_state.get("status") == "running" and isinstance(
                        stale_before, list
                    ):
                        removed, skipped = tracker.cleanup_created(stale_before)
                        sink.emit(
                            "container_recovery",
                            phase=phase.id,
                            status="completed",
                            details={
                                "removed_count": len(removed),
                                "skipped_count": len(skipped),
                            },
                        )
                    before = tracker.ids()
                    phase_state["containers_before"] = sorted(before)
                phase_state["status"] = "running"
                write_manifest(run_dir, manifest)
                return_code = 1
                try:
                    return_code = execute_phase(
                        phase, workspace=workspace, run_dir=run_dir, sink=sink
                    )
                finally:
                    if phase.track_containers and tracker is not None:
                        removed, skipped = tracker.cleanup_created(before)
                        sink.emit(
                            "container_cleanup",
                            phase=phase.id,
                            status="completed",
                            details={"removed_count": len(removed), "skipped_count": len(skipped)},
                        )
                phase_state["status"] = "succeeded" if return_code == 0 else "failed"
                write_manifest(run_dir, manifest)
                if return_code != 0:
                    raise RuntimeError(
                        f"Phase {phase.id} failed; see {run_dir / 'logs'}"
                    )
            manifest["status"] = "succeeded"
            manifest["completed_at"] = datetime.now(UTC).isoformat()
            write_manifest(run_dir, manifest)
            sink.emit("run_finished", status="succeeded")
        except KeyboardInterrupt:
            manifest["status"] = "cancelled"
            write_manifest(run_dir, manifest)
            sink.emit("run_finished", status="cancelled")
            raise
        except BaseException as error:
            manifest["status"] = "failed"
            manifest["failure"] = {"type": type(error).__name__, "message": str(error)}
            write_manifest(run_dir, manifest)
            sink.emit(
                "run_finished",
                status="failed",
                details={"error_type": type(error).__name__, "message": str(error)},
            )
            raise
