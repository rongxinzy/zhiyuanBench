"""Suite phase construction, execution, resume, and progress monitoring."""

from __future__ import annotations

import json
import importlib.util
import os
import queue
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid
import zipfile
from collections.abc import Callable
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
    "ZHIYUAN_ENABLE_SUBAGENT",
    "ZHIYUAN_SUBAGENT_TIMEOUT_MS",
    "ZHIYUAN_BRIDGE_TIMEOUT_SECONDS",
    "ZHIYUAN_AGENTRL_CONTROLLER",
    "ZHIYUAN_AGENTRL_ROOT",
    "ZHIYUAN_AGENTRL_PYTHON",
    "ZHIYUAN_GATEWAY_TIMEOUT_SECONDS",
    "ZHIYUAN_AGENTBENCH_KG_SPARQL_URL",
    "DOCKER_HOST",
)
PROGRESS_PATTERN = re.compile(
    r"\bSamples:\s*(\d{1,6})\s*/\s*(\d{1,6})(?!\d)", re.IGNORECASE
)


class SuiteUnavailableError(RuntimeError):
    """The selected suite cannot run with the current local infrastructure."""


def _append_no_proxy_host(value: str, host: str) -> str:
    entries = [entry.strip() for entry in value.split(",") if entry.strip()]
    if host.lower() not in {entry.lower() for entry in entries}:
        entries.append(host)
    return ",".join(entries)


def _loopback_model_proxy_environment(base_url: str) -> dict[str, str]:
    host = urllib.parse.urlsplit(base_url).hostname
    if host is None or not (
        host.lower() == "localhost" or host == "::1" or host.startswith("127.")
    ):
        return {}
    return {
        name: _append_no_proxy_host(os.environ.get(name, ""), host)
        for name in ("NO_PROXY", "no_proxy")
    }


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
    return str(
        next((path for path in candidates if path.is_file()), Path(sys.executable))
    )


def _npm() -> str:
    return "npm.cmd" if os.name == "nt" else "npm"


def _candidate_environment(
    candidate: Candidate,
    suite: SuiteDefinition,
    bridge: BridgeDefinition,
    log_dir: Path,
    *,
    reviewer_required: bool = False,
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
        if "subagent" in bridge.capabilities:
            environment["ZHIYUAN_ENABLE_SUBAGENT"] = "true"
        if reviewer_required:
            environment["ZHIYUAN_REQUIRE_REVIEWER_SUBAGENT"] = "true"
        if suite.model_roles:
            base_url = environment.get("ZHIYUAN_MODEL_BASE_URL")
            if base_url:
                environment["ZHIYUAN_BASE_URL"] = base_url
                environment.update(_loopback_model_proxy_environment(base_url))
            environment["ZHIYUAN_API_KEY"] = (
                environment.get("ZHIYUAN_MODEL_API_KEY") or "local-eval"
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
    concurrency: int = 1,
    reviewer_required_candidates: set[str] | None = None,
) -> list[Phase]:
    phases: list[Phase] = []
    reviewer_required = (
        {candidate.label for candidate in candidates}
        if reviewer_required_candidates is None
        else reviewer_required_candidates
    )
    python = _python(workspace)
    if suite.adapter == "agentrl-agentbench-fc":
        for candidate in candidates:
            log_dir = run_dir / "agentrl" / candidate.label
            command = [
                python,
                "-m",
                "zhiyuan_bench.agentrl_adapter",
                "--inspect-workspace",
                str(workspace),
                "--candidate-root",
                str(candidate.root),
                "--candidate-id",
                candidate.revision,
                "--task",
                suite.task,
                "--output",
                str(log_dir),
                "--concurrency",
                str(concurrency),
            ]
            if limit is not None:
                command.extend(("--limit", str(limit)))
            environment = _candidate_environment(candidate, suite, bridge, log_dir)
            bench_source = str(Path(__file__).resolve().parents[1])
            environment["PYTHONPATH"] = os.pathsep.join(
                [
                    bench_source,
                    *(
                        [environment["PYTHONPATH"]]
                        if environment.get("PYTHONPATH")
                        else []
                    ),
                ]
            )
            phases.append(
                Phase(
                    id=f"eval-{candidate.label}",
                    label=f"Evaluate {candidate.label}",
                    command=tuple(command),
                    environment=environment,
                )
            )
        return phases
    if suite.production_policy:
        for candidate in candidates:
            phases.append(
                Phase(
                    id=f"build-{candidate.label}",
                    label=f"Build policy for {candidate.label}",
                    command=(
                        _npm(),
                        "--prefix",
                        str(candidate.root),
                        "run",
                        "build:eval-policy",
                    ),
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
                    environment=_candidate_environment(
                        candidate,
                        suite,
                        bridge,
                        log_dir,
                        reviewer_required=candidate.label in reviewer_required,
                    ),
                    track_containers=True,
                    progress_log_dir=log_dir,
                    expected_samples=1,
                )
            )
            phases.append(
                Phase(
                    id=f"validate-preflight-{candidate.label}",
                    label=f"Validate production preflight for {candidate.label}",
                    command=(
                        python,
                        "-m",
                        "tools.zhiyuan.validate_production_log",
                        "--log-dir",
                        str(log_dir),
                        "--candidate-id",
                        candidate.revision,
                        "--expected-samples",
                        "1",
                        *(
                            ("--allow-no-inspect-tool-call",)
                            if not suite.preflight_require_inspect_tool_call
                            else ()
                        ),
                        *(
                            ("--require-reviewer-subagent",)
                            if candidate.label in reviewer_required
                            else ()
                        ),
                    ),
                    environment={},
                )
            )
        log_dir = run_dir / "evals" / candidate.label / "full"
        command = _inspect_command(python, suite, log_dir, limit, preflight=False)
        phases.append(
            Phase(
                id=f"eval-{candidate.label}",
                label=f"Evaluate {candidate.label}",
                command=command,
                environment=_candidate_environment(
                    candidate,
                    suite,
                    bridge,
                    log_dir,
                    reviewer_required=candidate.label in reviewer_required,
                ),
                track_containers="sandbox" in suite.required_capabilities,
                progress_log_dir=log_dir,
                expected_samples=limit or suite.expected_samples,
            )
        )
        if suite.production_policy:
            expected = limit or suite.expected_samples
            if expected is None:
                raise ValueError(
                    f"Suite {suite.id} must declare expected_samples for production validation"
                )
            phases.append(
                Phase(
                    id=f"validate-full-{candidate.label}",
                    label=f"Validate full production run for {candidate.label}",
                    command=(
                        python,
                        "-m",
                        "tools.zhiyuan.validate_production_log",
                        "--log-dir",
                        str(log_dir),
                        "--candidate-id",
                        candidate.revision,
                        "--expected-samples",
                        str(expected),
                        *(
                            ("--require-reviewer-subagent",)
                            if candidate.label in reviewer_required
                            else ()
                        ),
                        "--label",
                        candidate.label,
                    ),
                    environment={},
                )
            )
    if len(candidates) in {1, 2}:
        report_dir = run_dir / "report"
        expected = limit or suite.expected_samples
        command: tuple[str, ...] = (
            python,
            "tools/zhiyuan/report_comparison.py",
        )
        if len(candidates) == 1:
            candidate = candidates[0]
            command += (
                "--solo",
                str(run_dir / "evals" / candidate.label / "full"),
                "--label",
                candidate.label,
            )
        else:
            command += (
                "--baseline",
                str(run_dir / "evals" / candidates[0].label / "full"),
                "--candidate",
                str(run_dir / "evals" / candidates[1].label / "full"),
            )
        command += ("--output-dir", str(report_dir))
        if expected is not None:
            command += ("--expected-samples", str(expected))
        if suite.production_policy:
            command += ("--require-production-agent",)
        if "subagent" in bridge.capabilities:
            if len(candidates) == 1 and candidates[0].label in reviewer_required:
                command += ("--require-reviewer-subagent",)
            elif len(candidates) == 2:
                if candidates[0].label in reviewer_required:
                    command += ("--require-baseline-reviewer-subagent",)
                if candidates[1].label in reviewer_required:
                    command += ("--require-candidate-reviewer-subagent",)
        phases.append(
            Phase(
                id="report",
                label=(
                    "Validate and generate solo report"
                    if len(candidates) == 1
                    else "Validate and generate comparison report"
                ),
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
    if suite.production_policy:
        bridge_timeout = _positive_int_environment(
            "ZHIYUAN_BRIDGE_TIMEOUT_SECONDS", default=600
        )
        inspect_time_limit = _positive_int_environment(
            "ZHIYUAN_INSPECT_TIME_LIMIT_SECONDS", default=bridge_timeout + 60
        )
        if inspect_time_limit <= bridge_timeout:
            raise ValueError(
                "ZHIYUAN_INSPECT_TIME_LIMIT_SECONDS must be greater than "
                "ZHIYUAN_BRIDGE_TIMEOUT_SECONDS so the bridge can persist its "
                "terminal state"
            )
        command.extend(("--time-limit", str(inspect_time_limit)))
    if preflight:
        command.extend(
            (
                "--limit",
                "1",
                "-T",
                "require_inspect_tool_call="
                + str(suite.preflight_require_inspect_tool_call).lower(),
            )
        )
        for task_arg in suite.preflight_task_args:
            command.extend(("-T", task_arg))
    elif limit is not None:
        command.extend(("--limit", str(limit)))
    if suite.adapter == "inspect-bfcl":
        command.extend(("-T", "categories=all_single_turn"))
    if suite.model_roles:
        http_idle_timeout_ms = _positive_int_environment(
            "ZHIYUAN_HTTP_IDLE_TIMEOUT_MS", default=300_000
        )
        model_timeout = _positive_int_environment(
            "ZHIYUAN_INSPECT_MODEL_TIMEOUT_SECONDS",
            default=max(1, (http_idle_timeout_ms + 999) // 1000),
        )
        command.extend(
            (
                "--timeout",
                str(model_timeout),
                "--attempt-timeout",
                str(model_timeout),
                "--max-retries",
                "0",
            )
        )
        model_id = os.environ.get("ZHIYUAN_MODEL_ID", "missing-model")
        for role in suite.model_roles:
            command.extend(("--model-role", f"{role}=openai-api/zhiyuan/{model_id}"))
    if "user_simulator" in suite.required_capabilities:
        if preflight:
            command.extend(
                (
                    "-T",
                    "message_limit=1",
                    "-T",
                    "user_max_tokens=256",
                    "-T",
                    "user_timeout_seconds=180",
                )
            )
    return tuple(command)


def _positive_int_environment(name: str, *, default: int) -> int:
    raw = os.environ.get(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as error:
        raise ValueError(f"{name} must be a positive integer") from error
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


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
    if suite.required_host_platforms and not any(
        sys.platform.startswith(platform) for platform in suite.required_host_platforms
    ):
        raise RuntimeError(
            f"Suite {suite.id} requires host platform "
            + " or ".join(suite.required_host_platforms)
            + f"; current platform is {sys.platform}"
        )
    if suite.required_host_platforms:
        sink.emit("health_check", status="host_platform_ok")
    missing_modules = [
        name
        for name in suite.required_python_modules
        if importlib.util.find_spec(name) is None
    ]
    if missing_modules:
        raise RuntimeError(
            "Missing suite Python modules: " + ", ".join(missing_modules)
        )
    if suite.required_python_modules:
        sink.emit("health_check", status="python_modules_ok")
    missing_environment = [
        name for name in suite.required_environment if not os.environ.get(name)
    ]
    if missing_environment:
        raise RuntimeError(
            "Missing suite environment: " + ", ".join(missing_environment)
        )
    base_url = os.environ.get("ZHIYUAN_MODEL_BASE_URL", "").rstrip("/")
    if not base_url or not os.environ.get("ZHIYUAN_MODEL_ID"):
        raise RuntimeError("ZHIYUAN_MODEL_BASE_URL and ZHIYUAN_MODEL_ID are required")
    with urllib.request.urlopen(f"{base_url}/models", timeout=10) as response:
        if response.status != 200:
            raise RuntimeError(f"Model API health check returned {response.status}")
        json.load(response)
    sink.emit("health_check", status="model_api_ok")
    if suite.adapter == "agentrl-agentbench-fc":
        controller = os.environ["ZHIYUAN_AGENTRL_CONTROLLER"].rstrip("/")
        with urllib.request.urlopen(
            f"{controller}/list_workers", timeout=15
        ) as response:
            workers = json.load(response)
        if not isinstance(workers, dict) or suite.task not in workers:
            raise RuntimeError(f"AgentRL controller has no workers for {suite.task}")
        indices_url = f"{controller}/get_indices?" + urllib.parse.urlencode(
            {"name": suite.task}
        )
        with urllib.request.urlopen(indices_url, timeout=15) as response:
            indices = json.load(response)
        if not isinstance(indices, list) or not indices:
            raise RuntimeError(
                f"AgentRL controller returned no indices for {suite.task}"
            )
        sink.emit(
            "health_check",
            status="agentrl_task_workers_ok",
            details={"available_samples": len(indices)},
        )
    for name in suite.health_url_environment:
        url = os.environ.get(name)
        if not url:
            raise RuntimeError(f"{name} is required by this suite")
        with urllib.request.urlopen(url, timeout=15) as response:
            if response.status >= 400:
                raise RuntimeError(f"{name} health check returned {response.status}")
        sink.emit("health_check", status=f"{name.lower()}_ok")
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
    source: Any,
    target: Any,
    channel: str,
    output_queue: queue.Queue[tuple[str, str | None]],
) -> None:
    try:
        for line in iter(source.readline, ""):
            target.write(line)
            target.flush()
            output_queue.put((channel, line))
    finally:
        output_queue.put((channel, None))


def _inspect_journal_completed(
    log_dir: Path | None, *, exclude: frozenset[Path] = frozenset()
) -> int | None:
    if log_dir is None or not log_dir.is_dir():
        return None
    logs = [path for path in log_dir.glob("*.eval") if path not in exclude]
    if not logs:
        return None
    latest = max(logs, key=lambda path: path.stat().st_mtime_ns)
    try:
        with zipfile.ZipFile(latest) as archive:
            return sum(
                name.startswith("samples/") and name.endswith(".json")
                for name in archive.namelist()
            )
    except (OSError, zipfile.BadZipFile):
        return None


def _read_inspect_log(path: Path) -> Any:
    try:
        from inspect_ai.log import read_eval_log
    except ImportError as error:
        raise RuntimeError(
            "Inspect is required to validate evaluation log completion"
        ) from error
    return read_eval_log(str(path), header_only=True)


def _validate_inspect_phase_log(
    phase: Phase, *, exclude: frozenset[Path]
) -> None:
    log_dir = phase.progress_log_dir
    if log_dir is None or not log_dir.is_dir():
        raise RuntimeError("Inspect phase did not create its evaluation log directory")
    logs = [path for path in log_dir.glob("*.eval") if path not in exclude]
    if len(logs) != 1:
        raise RuntimeError(
            f"Inspect phase must create exactly one evaluation log; found {len(logs)}"
        )
    log = _read_inspect_log(logs[0])
    if getattr(log, "status", None) != "success":
        raise RuntimeError(
            f"Inspect evaluation log is not complete: {getattr(log, 'status', 'unknown')}"
        )
    results = getattr(log, "results", None)
    completed = getattr(results, "completed_samples", None)
    total = getattr(results, "total_samples", None)
    if results is None or not isinstance(completed, int) or not isinstance(total, int):
        raise RuntimeError("Inspect evaluation log has no aggregate completion result")
    if phase.expected_samples is not None and (
        completed != phase.expected_samples or total != phase.expected_samples
    ):
        raise RuntimeError(
            "Inspect evaluation sample count mismatch: "
            f"expected {phase.expected_samples}, completed {completed} of {total}"
        )


def _progress_advanced(
    current: tuple[int, int] | None, observed: tuple[int, int]
) -> bool:
    return observed[0] <= observed[1] and (
        current is None or observed[0] > current[0]
    )


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
    existing_progress_logs = (
        frozenset(phase.progress_log_dir.glob("*.eval"))
        if phase.progress_log_dir is not None
        and phase.progress_log_dir.is_dir()
        else frozenset()
    )
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
                            if _progress_advanced(progress, observed):
                                progress = observed
                                sink.emit(
                                    "phase_progress",
                                    phase=phase.id,
                                    status="running",
                                    details={
                                        "completed": observed[0],
                                        "total": observed[1],
                                    },
                                )
                except queue.Empty:
                    pass
                now = time.monotonic()
                if now - last_heartbeat >= 10:
                    completed = _inspect_journal_completed(
                        phase.progress_log_dir, exclude=existing_progress_logs
                    )
                    if (
                        completed is not None
                        and phase.expected_samples is not None
                        and _progress_advanced(
                            progress, (completed, phase.expected_samples)
                        )
                    ):
                        progress = (completed, phase.expected_samples)
                        sink.emit(
                            "phase_progress",
                            phase=phase.id,
                            status="running",
                            details={
                                "completed": completed,
                                "total": phase.expected_samples,
                            },
                        )
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
    if return_code == 0 and phase.progress_log_dir is not None:
        try:
            _validate_inspect_phase_log(phase, exclude=existing_progress_logs)
        except RuntimeError as error:
            with stderr_path.open("a", encoding="utf-8", newline="\n") as stderr_file:
                stderr_file.write(f"[zhiyuan-bench] {error}\n")
            return_code = 1
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
    concurrency: int = 1,
    reviewer_required_candidates: set[str] | None = None,
) -> Path:
    suite = suite_by_id(suite_id)
    bridge = select_bridge(suite, bridge_id)
    if concurrency <= 0:
        raise ValueError("concurrency must be positive")
    if len(candidates) < suite.min_candidates or (
        suite.max_candidates is not None and len(candidates) > suite.max_candidates
    ):
        raise ValueError(
            f"Suite {suite.id} accepts {suite.min_candidates}.."
            f"{suite.max_candidates or 'many'} candidates"
        )
    candidate_labels = {candidate.label for candidate in candidates}
    reviewer_required = (
        candidate_labels
        if reviewer_required_candidates is None
        else reviewer_required_candidates
    )
    unknown_reviewer_labels = reviewer_required - candidate_labels
    if unknown_reviewer_labels:
        raise ValueError(
            "Reviewer-required candidates are not configured: "
            + ", ".join(sorted(unknown_reviewer_labels))
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
        "concurrency": concurrency,
        "candidates": [candidate.as_dict() for candidate in candidates],
        "reviewer_required_candidates": sorted(reviewer_required),
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


def mark_manifest_running(manifest: dict[str, Any]) -> None:
    manifest["status"] = "running"
    manifest.pop("failure", None)
    manifest.pop("completed_at", None)


def invalidate_failed_validation_sources(manifest: dict[str, Any]) -> bool:
    states = manifest.get("phases")
    if not isinstance(states, dict):
        return False
    changed = False
    for validation_prefix, source_prefix in (
        ("validate-preflight-", "preflight-"),
        ("validate-full-", "eval-"),
    ):
        for phase_id, state in list(states.items()):
            if not phase_id.startswith(validation_prefix) or not isinstance(
                state, dict
            ):
                continue
            if state.get("status") != "failed":
                continue
            source_id = source_prefix + phase_id.removeprefix(validation_prefix)
            source_state = states.get(source_id)
            if isinstance(source_state, dict) and source_state.get(
                "status"
            ) == "succeeded":
                source_state["status"] = "failed"
                changed = True
    return changed


def run_manifest(
    run_dir: Path,
    *,
    health_checks: bool = True,
    event_listener: Callable[[dict[str, Any]], None] | None = None,
) -> None:
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
            source_ref=(
                str(item["source_ref"]) if item.get("source_ref") is not None else None
            ),
            source_repo=(
                Path(str(item["source_repo"]))
                if item.get("source_repo") is not None
                else None
            ),
            managed_worktree=bool(item.get("managed_worktree", False)),
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
        int(manifest.get("concurrency", 1)),
        set(
            str(label)
            for label in manifest.get(
                "reviewer_required_candidates",
                [candidate.label for candidate in candidates],
            )
        ),
    )
    sink = EventSink(
        run_dir / "events.jsonl",
        run_id,
        stream=sys.stdout,
        listener=event_listener,
    )
    tracker = None
    docker_host = os.environ.get("DOCKER_HOST")
    if docker_host and "sandbox" in suite.required_capabilities:
        tracker = ContainerTracker(docker_host)
    with RunLock(output_root / "runner.lock", run_id):
        try:
            mark_manifest_running(manifest)
            invalidate_failed_validation_sources(manifest)
            write_manifest(run_dir, manifest)
            sink.emit(
                "run_started",
                status="running",
                details={"suite": suite.id, "bridge": bridge.id},
            )
            for candidate in candidates:
                _verify_candidate(candidate)
            if health_checks:
                try:
                    _health_checks(suite, sink)
                except (OSError, RuntimeError, subprocess.SubprocessError) as error:
                    raise SuiteUnavailableError(str(error)) from error
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
                            details={
                                "removed_count": len(removed),
                                "skipped_count": len(skipped),
                            },
                        )
                phase_state["status"] = "succeeded" if return_code == 0 else "failed"
                invalidate_failed_validation_sources(manifest)
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
            manifest["completed_at"] = datetime.now(UTC).isoformat()
            manifest["failure"] = {
                "type": "KeyboardInterrupt",
                "message": "Run cancelled by keyboard interrupt",
            }
            write_manifest(run_dir, manifest)
            sink.emit("run_finished", status="cancelled")
            raise
        except BaseException as error:
            manifest["status"] = "failed"
            manifest["completed_at"] = datetime.now(UTC).isoformat()
            manifest["failure"] = {"type": type(error).__name__, "message": str(error)}
            write_manifest(run_dir, manifest)
            sink.emit(
                "run_finished",
                status="failed",
                details={"error_type": type(error).__name__, "message": str(error)},
            )
            raise
