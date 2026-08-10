"""Optional local Web API and SSE transport for evaluation campaigns."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import subprocess
import sys
import threading
import urllib.request
import webbrowser
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from zhiyuan_bench.campaigns import (
    CAMPAIGN_SUITES,
    TERMINAL_CAMPAIGN_STATUSES,
    campaign_summary,
    create_campaign,
    finalize_interrupted_campaign,
    read_campaign,
)
from zhiyuan_bench.locking import RunLock
from zhiyuan_bench.persistence import atomic_write_text
from zhiyuan_bench.registry import select_bridge, suite_by_id
from zhiyuan_bench.reports import render_campaign_report, write_campaign_report
from zhiyuan_bench.worktrees import list_local_branches

STATIC_ROOT = Path(__file__).with_name("static")
MODEL_REQUIRED_ENVIRONMENT = (
    "ZHIYUAN_MODEL_BASE_URL",
    "ZHIYUAN_MODEL_ID",
)
@dataclass(frozen=True)
class WebConfig:
    repo: Path
    workspace: Path
    records_root: Path

    def resolved(self) -> "WebConfig":
        return WebConfig(
            repo=self.repo.expanduser().resolve(),
            workspace=self.workspace.expanduser().resolve(),
            records_root=self.records_root.expanduser().resolve(),
        )


class CampaignConflictError(RuntimeError):
    """A campaign runner is already active for this records root."""


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


def _write_json(path: Path, value: dict[str, Any]) -> None:
    atomic_write_text(
        path,
        json.dumps(value, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
    )


class CampaignLauncher:
    def __init__(self, config: WebConfig) -> None:
        self.config = config.resolved()

    def _active_lock_pid(self) -> int | None:
        lock_path = self.config.records_root / "campaign.lock"
        try:
            value = json.loads(lock_path.read_text(encoding="utf-8"))
            pid = int(value.get("pid", -1))
        except (OSError, TypeError, ValueError):
            return None
        return pid if _process_alive(pid) else None

    def launch(self, campaign_dir: Path) -> dict[str, Any]:
        campaign_dir = campaign_dir.resolve()
        try:
            with RunLock(
                self.config.records_root / "web-launch.lock", campaign_dir.name
            ):
                return self._launch_locked(campaign_dir)
        except RuntimeError as error:
            raise CampaignConflictError(str(error)) from error

    def _launch_locked(self, campaign_dir: Path) -> dict[str, Any]:
        active_pid = self._active_lock_pid()
        if active_pid is not None:
            raise CampaignConflictError(
                f"Another campaign runner is active with PID {active_pid}"
            )
        launch_path = campaign_dir / "web-runner.json"
        if launch_path.is_file():
            try:
                previous = json.loads(launch_path.read_text(encoding="utf-8"))
                previous_pid = int(previous.get("pid", -1))
                campaign_status = str(read_campaign(campaign_dir)["status"])
            except (OSError, TypeError, ValueError):
                previous_pid = -1
                campaign_status = "unknown"
            if (
                _process_alive(previous_pid)
                and campaign_status not in TERMINAL_CAMPAIGN_STATUSES
            ):
                raise CampaignConflictError(
                    f"Campaign runner is already active with PID {previous_pid}"
                )

        logs_dir = campaign_dir / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = logs_dir / "web-runner.stdout.log"
        stderr_path = logs_dir / "web-runner.stderr.log"
        command = [
            sys.executable,
            "-m",
            "zhiyuan_bench",
            "campaign",
            "run",
            str(campaign_dir),
        ]
        with (
            stdout_path.open("a", encoding="utf-8", newline="\n") as stdout,
            stderr_path.open("a", encoding="utf-8", newline="\n") as stderr,
        ):
            process = subprocess.Popen(
                command,
                cwd=self.config.workspace,
                env=os.environ.copy(),
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                close_fds=True,
            )
        launch = {
            "schema_version": 1,
            "pid": process.pid,
            "started_at": datetime.now(UTC).isoformat(),
            "stdout": str(stdout_path),
            "stderr": str(stderr_path),
        }
        _write_json(launch_path, launch)
        return launch


def _campaign_dir(config: WebConfig, campaign_id: str) -> Path:
    if Path(campaign_id).name != campaign_id:
        raise FileNotFoundError(campaign_id)
    root = config.records_root.resolve()
    path = (root / campaign_id).resolve()
    if path.parent != root or not (path / "campaign.json").is_file():
        raise FileNotFoundError(campaign_id)
    return path


def _runner_pid_from(path: Path) -> int | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        pid = int(value.get("pid", -1))
    except (OSError, TypeError, ValueError):
        return None
    return pid if pid > 0 else None


def _campaign_runner_alive(campaign_dir: Path, manifest: dict[str, Any]) -> bool:
    lock_path = Path(str(manifest["records_root"])) / "campaign.lock"
    try:
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        lock = {}
    if lock.get("run_id") == manifest.get("campaign_id"):
        try:
            if _process_alive(int(lock.get("pid", -1))):
                return True
        except (TypeError, ValueError):
            pass
    runner = manifest.get("runner")
    if isinstance(runner, dict):
        try:
            if _process_alive(int(runner.get("pid", -1))):
                return True
        except (TypeError, ValueError):
            pass
    launch_pid = _runner_pid_from(campaign_dir / "web-runner.json")
    return launch_pid is not None and _process_alive(launch_pid)


def _load_campaign(campaign_dir: Path) -> dict[str, Any]:
    manifest = read_campaign(campaign_dir)
    if manifest.get("status") == "running" and not _campaign_runner_alive(
        campaign_dir, manifest
    ):
        manifest = finalize_interrupted_campaign(campaign_dir, best_effort=True)
    return manifest


def _campaigns(config: WebConfig) -> list[dict[str, Any]]:
    if not config.records_root.is_dir():
        return []
    records: list[dict[str, Any]] = []
    for path in sorted(config.records_root.iterdir(), reverse=True):
        if not path.is_dir() or not (path / "campaign.json").is_file():
            continue
        try:
            records.append(campaign_summary(_load_campaign(path)))
        except (OSError, ValueError):
            continue
    return records


def _suite_payload() -> list[dict[str, Any]]:
    result = []
    for suite_id in CAMPAIGN_SUITES:
        suite = suite_by_id(suite_id)
        bridge = select_bridge(suite)
        result.append(
            {
                "id": suite.id,
                "description": suite.description,
                "expected_samples": suite.expected_samples,
                "bridge": bridge.id,
                "required_capabilities": sorted(suite.required_capabilities),
                "notes": list(suite.notes),
            }
        )
    return result


def _service_readiness(suite_ids: tuple[str, ...] | list[str]) -> dict[str, Any]:
    model_ready = False
    model_reason = "not_configured"
    base_url = os.environ.get("ZHIYUAN_MODEL_BASE_URL", "").rstrip("/")
    if base_url and os.environ.get("ZHIYUAN_MODEL_ID"):
        try:
            with urllib.request.urlopen(f"{base_url}/models", timeout=5) as response:
                model_ready = response.status == 200
                model_reason = "ready" if model_ready else "unhealthy"
        except (OSError, ValueError):
            model_reason = "unreachable"

    needs_docker = any(
        "sandbox" in suite_by_id(suite_id).required_capabilities
        for suite_id in suite_ids
    )
    docker_ready = not needs_docker
    docker_reason = "not_required" if not needs_docker else "not_configured"
    docker_host = os.environ.get("DOCKER_HOST")
    if needs_docker and docker_host:
        try:
            result = subprocess.run(
                [
                    "docker",
                    "-H",
                    docker_host,
                    "info",
                    "--format",
                    "{{.ServerVersion}}",
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=15,
            )
            docker_ready = result.returncode == 0
            docker_reason = "ready" if docker_ready else "unreachable"
        except (OSError, subprocess.SubprocessError):
            docker_reason = "unreachable"

    return {
        "model_api": {"ready": model_ready, "reason": model_reason},
        "docker": {"ready": docker_ready, "reason": docker_reason},
    }


def _suite_readiness(
    suite_id: str, services: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    suite = suite_by_id(suite_id)
    required_environment = list(MODEL_REQUIRED_ENVIRONMENT)
    required_environment.extend(suite.required_environment)
    if "sandbox" in suite.required_capabilities:
        required_environment.append("DOCKER_HOST")
    missing_environment = sorted(
        {name for name in required_environment if not os.environ.get(name)}
    )
    missing_modules = sorted(
        name
        for name in suite.required_python_modules
        if importlib.util.find_spec(name) is None
    )
    platform_supported = not suite.required_host_platforms or any(
        sys.platform.startswith(platform)
        for platform in suite.required_host_platforms
    )
    unavailable_services = []
    if not services["model_api"]["ready"]:
        unavailable_services.append("model_api")
    if (
        "sandbox" in suite.required_capabilities
        and not services["docker"]["ready"]
    ):
        unavailable_services.append("docker")
    return {
        "id": suite.id,
        "ready": not missing_environment
        and not missing_modules
        and platform_supported
        and not unavailable_services,
        "missing_environment": missing_environment,
        "missing_python_modules": missing_modules,
        "platform_supported": platform_supported,
        "required_host_platforms": list(suite.required_host_platforms),
        "unavailable_services": unavailable_services,
    }


def _readiness(
    suite_ids: tuple[str, ...] | list[str] = CAMPAIGN_SUITES,
) -> dict[str, Any]:
    services = _service_readiness(suite_ids)
    suites = [_suite_readiness(suite_id, services) for suite_id in suite_ids]
    missing_model_environment = [
        name for name in MODEL_REQUIRED_ENVIRONMENT if not os.environ.get(name)
    ]
    return {
        "schema_version": 1,
        "ready": all(suite["ready"] for suite in suites),
        "missing_environment": missing_model_environment,
        "services": services,
        "suites": suites,
    }


def _unavailable_message(readiness: dict[str, Any]) -> str:
    unavailable = [suite for suite in readiness["suites"] if not suite["ready"]]
    details = []
    for suite in unavailable:
        reasons = []
        if suite["missing_environment"]:
            reasons.append(
                "missing environment " + ", ".join(suite["missing_environment"])
            )
        if suite["missing_python_modules"]:
            reasons.append(
                "missing Python modules "
                + ", ".join(suite["missing_python_modules"])
            )
        if not suite["platform_supported"]:
            reasons.append(
                "requires host platform "
                + " or ".join(suite["required_host_platforms"])
            )
        if suite["unavailable_services"]:
            reasons.append(
                "unavailable services " + ", ".join(suite["unavailable_services"])
            )
        details.append(f"{suite['id']}: {'; '.join(reasons)}")
    return "Selected suites are not ready: " + " | ".join(details)


def _error(message: str, status_code: int) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status_code)


def _parse_create_payload(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("Request body must be a JSON object")
    suites = value.get("suites")
    branches = value.get("branches")
    if not isinstance(suites, list) or not all(
        isinstance(item, str) for item in suites
    ):
        raise ValueError("suites must be a list of suite IDs")
    if not isinstance(branches, list) or not branches:
        raise ValueError("branches must be a non-empty list")
    branch_values: list[str] = []
    for branch in branches:
        if not isinstance(branch, dict):
            raise ValueError("Each branch must be an object")
        label = branch.get("label")
        source_ref = branch.get("ref")
        if not isinstance(label, str) or not isinstance(source_ref, str):
            raise ValueError("Each branch requires string label and ref fields")
        branch_values.append(f"{label}={source_ref}")
    limit = value.get("limit")
    concurrency = value.get("concurrency", 1)
    if limit is not None and (not isinstance(limit, int) or isinstance(limit, bool)):
        raise ValueError("limit must be an integer or null")
    if not isinstance(concurrency, int) or isinstance(concurrency, bool):
        raise ValueError("concurrency must be an integer")
    reviewer = value.get("reviewer_required_candidates")
    if reviewer is not None and (
        not isinstance(reviewer, list)
        or not all(isinstance(item, str) for item in reviewer)
    ):
        raise ValueError("reviewer_required_candidates must be a list of labels")
    return {
        "suite_ids": suites,
        "branch_values": branch_values,
        "limit": limit,
        "concurrency": concurrency,
        "reviewer_required_candidates": (
            set(reviewer) if reviewer is not None else None
        ),
        "run": bool(value.get("run", False)),
    }


def _read_events(path: Path, after_sequence: int) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    events = []
    with path.open(encoding="utf-8") as source:
        for line in source:
            try:
                event = json.loads(line)
                sequence = int(event["sequence"])
            except (KeyError, TypeError, ValueError):
                continue
            if sequence > after_sequence:
                events.append(event)
    return events


def _sse(event: dict[str, Any]) -> str:
    payload = json.dumps(event, ensure_ascii=True, sort_keys=True)
    return (
        f"id: {event['sequence']}\n"
        f"event: {event.get('event_type', 'message')}\n"
        f"data: {payload}\n\n"
    )


def create_app(
    config: WebConfig, *, launcher: CampaignLauncher | None = None
) -> Starlette:
    config = config.resolved()
    launcher = launcher or CampaignLauncher(config)

    async def index(_request: Request) -> Response:
        return FileResponse(
            STATIC_ROOT / "index.html", headers={"Cache-Control": "no-cache"}
        )

    async def health(_request: Request) -> Response:
        return JSONResponse({"status": "ok", "schema_version": 1})

    async def readiness(_request: Request) -> Response:
        return JSONResponse(await run_in_threadpool(_readiness))

    async def configuration(_request: Request) -> Response:
        return JSONResponse(
            {
                "repo": str(config.repo),
                "workspace": str(config.workspace),
                "records_root": str(config.records_root),
            }
        )

    async def suites(_request: Request) -> Response:
        return JSONResponse(_suite_payload())

    async def branches(_request: Request) -> Response:
        try:
            values = await run_in_threadpool(list_local_branches, config.repo)
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
            return _error(str(error), 400)
        return JSONResponse(values)

    async def campaigns(_request: Request) -> Response:
        return JSONResponse(await run_in_threadpool(_campaigns, config))

    async def campaign_detail(request: Request) -> Response:
        try:
            path = _campaign_dir(config, request.path_params["campaign_id"])
            summary = await run_in_threadpool(campaign_summary, _load_campaign(path))
        except (FileNotFoundError, OSError, ValueError):
            return _error("Campaign not found", 404)
        return JSONResponse(summary)

    async def campaign_create(request: Request) -> Response:
        try:
            payload = _parse_create_payload(await request.json())
            should_run = payload.pop("run")
            if should_run:
                run_readiness = await run_in_threadpool(
                    _readiness, payload["suite_ids"]
                )
                if any(not suite["ready"] for suite in run_readiness["suites"]):
                    return JSONResponse(
                        {
                            "error": _unavailable_message(run_readiness),
                            "readiness": run_readiness,
                        },
                        status_code=503,
                    )
            path = await run_in_threadpool(
                create_campaign,
                repo=config.repo,
                workspace=config.workspace,
                records_root=config.records_root,
                **payload,
            )
            launch = None
            if should_run:
                try:
                    launch = await run_in_threadpool(launcher.launch, path)
                except CampaignConflictError as error:
                    return JSONResponse(
                        {
                            "error": str(error),
                            "campaign": campaign_summary(read_campaign(path)),
                        },
                        status_code=409,
                    )
        except json.JSONDecodeError:
            return _error("Request body must be valid JSON", 400)
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
            return _error(str(error), 400)
        response = campaign_summary(read_campaign(path))
        if launch is not None:
            response["runner"] = launch
        return JSONResponse(response, status_code=201)

    async def campaign_run(request: Request) -> Response:
        try:
            path = _campaign_dir(config, request.path_params["campaign_id"])
            campaign = _load_campaign(path)
            suite_ids = [str(item["id"]) for item in campaign["suites"]]
            run_readiness = await run_in_threadpool(_readiness, suite_ids)
            if any(not suite["ready"] for suite in run_readiness["suites"]):
                return JSONResponse(
                    {
                        "error": _unavailable_message(run_readiness),
                        "readiness": run_readiness,
                    },
                    status_code=503,
                )
            launch = await run_in_threadpool(launcher.launch, path)
        except FileNotFoundError:
            return _error("Campaign not found", 404)
        except CampaignConflictError as error:
            return _error(str(error), 409)
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            return _error(str(error), 400)
        return JSONResponse({"status": "started", "runner": launch}, status_code=202)

    async def campaign_report(request: Request) -> Response:
        try:
            path = _campaign_dir(config, request.path_params["campaign_id"])
            manifest = await run_in_threadpool(_load_campaign, path)
            summary = campaign_summary(manifest)
            try:
                await run_in_threadpool(write_campaign_report, path, summary)
            except OSError:
                return Response(
                    render_campaign_report(summary),
                    media_type="text/html",
                    headers={"Cache-Control": "no-cache"},
                )
            report = path / "report" / "report.html"
        except FileNotFoundError:
            return _error("Campaign report not found", 404)
        return FileResponse(
            report,
            media_type="text/html",
            headers={"Cache-Control": "no-cache"},
        )

    async def campaign_events(request: Request) -> Response:
        try:
            path = _campaign_dir(config, request.path_params["campaign_id"])
        except FileNotFoundError:
            return _error("Campaign not found", 404)
        raw_after = request.headers.get("last-event-id") or request.query_params.get(
            "after", "0"
        )
        try:
            after = max(0, int(raw_after))
        except ValueError:
            return _error("after must be an integer", 400)
        follow = request.query_params.get("follow", "1") != "0"

        async def stream() -> Any:
            sequence = after
            idle_ticks = 0
            try:
                while True:
                    events = await run_in_threadpool(
                        _read_events, path / "events.jsonl", sequence
                    )
                    for event in events:
                        sequence = max(sequence, int(event["sequence"]))
                        yield _sse(event)
                    if not follow:
                        return
                    try:
                        campaign = await run_in_threadpool(read_campaign, path)
                    except (OSError, ValueError):
                        campaign = {}
                    if campaign.get("status") in TERMINAL_CAMPAIGN_STATUSES:
                        return
                    if await request.is_disconnected():
                        return
                    idle_ticks += 1
                    if idle_ticks >= 30:
                        yield ": keep-alive\n\n"
                        idle_ticks = 0
                    await asyncio.sleep(0.5)
            except asyncio.CancelledError:
                return

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return Starlette(
        debug=False,
        routes=[
            Route("/", index),
            Route("/api/health", health),
            Route("/api/readiness", readiness),
            Route("/api/config", configuration),
            Route("/api/suites", suites),
            Route("/api/branches", branches),
            Route("/api/campaigns", campaigns, methods=["GET"]),
            Route("/api/campaigns", campaign_create, methods=["POST"]),
            Route("/api/campaigns/{campaign_id}", campaign_detail),
            Route("/api/campaigns/{campaign_id}/run", campaign_run, methods=["POST"]),
            Route("/api/campaigns/{campaign_id}/report", campaign_report),
            Route("/api/campaigns/{campaign_id}/events", campaign_events),
            Mount("/static", app=StaticFiles(directory=STATIC_ROOT), name="static"),
        ],
    )


def run_web_server(
    config: WebConfig,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = False,
) -> None:
    try:
        import uvicorn
    except ImportError as error:
        raise RuntimeError(
            "Web dependencies are missing; install zhiyuan-bench[web]"
        ) from error
    if open_browser:
        browser_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
        browser_timer = threading.Timer(
            0.75, webbrowser.open, args=(f"http://{browser_host}:{port}/",)
        )
        browser_timer.daemon = True
        browser_timer.start()
    uvicorn.run(
        create_app(config),
        host=host,
        port=port,
        log_level="info",
        timeout_graceful_shutdown=3,
    )
