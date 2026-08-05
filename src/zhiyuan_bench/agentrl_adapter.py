"""Run one AgentRL task through the local headless Pi FC gateway."""

from __future__ import annotations

import argparse
import json
import os
import queue
import re
import subprocess
import sys
import threading
import urllib.parse
import urllib.request
from collections.abc import Sequence
from pathlib import Path
from typing import Any, TextIO

from zhiyuan_bench.gateway import GatewayConfig, GatewayServer


COMPLETED_PATTERN = re.compile(r"\bcompleted task=")
HTTP_OK = 200
OUTPUT_STREAM_COUNT = 2
COMPLETED_STATUSES = {
    "unknown",
    "completed",
    "agent validation failed",
    "agent invalid action",
    "task limit reached",
}


def _get_json(url: str, *, params: dict[str, str] | None = None) -> Any:
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=15) as response:
        if response.status != HTTP_OK:
            raise RuntimeError(f"health check returned HTTP {response.status}")
        return json.load(response)


def controller_preflight(controller: str, task: str) -> list[int | str]:
    base = controller.rstrip("/")
    workers = _get_json(f"{base}/list_workers")
    if not isinstance(workers, dict) or task not in workers:
        raise RuntimeError(f"controller has no worker entry for task {task!r}")
    task_workers = (
        workers[task].get("workers", {}) if isinstance(workers[task], dict) else {}
    )
    if not isinstance(task_workers, dict) or not any(
        isinstance(worker, dict)
        and str(worker.get("status", "")).lower() == "alive"
        and int(worker.get("capacity", 0)) > int(worker.get("current", 0))
        for worker in task_workers.values()
    ):
        raise RuntimeError(
            f"controller has no available worker capacity for task {task!r}"
        )
    indices = _get_json(f"{base}/get_indices", params={"name": task})
    if not isinstance(indices, list) or not indices:
        raise RuntimeError(f"controller returned no indices for task {task!r}")
    return indices


def selected_range(
    indices: list[int | str], limit: int | None
) -> tuple[str | None, int]:
    usable = [index for index in indices if index != -1]
    if limit is None:
        return None, len(usable)
    if limit <= 0 or limit > len(usable):
        raise ValueError(f"limit must be between 1 and {len(usable)}")
    selected = usable[:limit]
    if not all(isinstance(index, int) for index in selected):
        raise ValueError("--limit currently requires integer AgentRL task indices")
    ordered = sorted(selected)
    if ordered != list(range(ordered[0], ordered[-1] + 1)):
        raise ValueError("--limit requires a contiguous block of AgentRL task indices")
    return f"{ordered[0]}-{ordered[-1]}", len(selected)


def build_agentrl_command(
    *,
    python: str,
    controller: str,
    gateway_base_url: str,
    model_id: str,
    task: str,
    output_root: Path,
    store: Path,
    concurrency: int,
    indices_range: str | None,
) -> tuple[str, ...]:
    command = [
        python,
        "-m",
        "agentrl.eval",
        "--controller",
        controller,
        "--base-url",
        gateway_base_url,
        "--model",
        model_id,
        "--chat-completions",
        "--no-thinking",
        "--no-parallel-tool-calls",
        "--temperature",
        "0",
        "--max-retries",
        "0",
        "--concurrency",
        str(concurrency),
        "--output",
        str(output_root),
        "--resume",
        str(store),
    ]
    if indices_range is not None:
        command.extend(("--indices-range", indices_range))
    command.append(task)
    return tuple(command)


def _completed_results(path: Path) -> int:
    if not path.is_file():
        return 0
    completed = 0
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if str(item.get("status", "")).lower() in COMPLETED_STATUSES:
            completed += 1
    return completed


def _copy_stream(
    source: TextIO,
    target: TextIO,
    channel: str,
    output: queue.Queue[tuple[str, str | None]],
) -> None:
    try:
        for line in iter(source.readline, ""):
            target.write(line)
            target.flush()
            output.put((channel, line))
    finally:
        output.put((channel, None))


def run_agentrl(
    command: Sequence[str],
    *,
    environment: dict[str, str],
    output_dir: Path,
    expected: int,
) -> int:
    output_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = output_dir / "agentrl.stdout.log"
    stderr_path = output_dir / "agentrl.stderr.log"
    completed = _completed_results(output_dir / "store" / "results.jsonl")
    print(f"Samples: {completed}/{expected}", flush=True)
    with (
        stdout_path.open("a", encoding="utf-8", newline="\n") as stdout,
        stderr_path.open("a", encoding="utf-8", newline="\n") as stderr,
    ):
        process = subprocess.Popen(
            command,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        assert process.stdout is not None and process.stderr is not None
        output: queue.Queue[tuple[str, str | None]] = queue.Queue()
        threads = [
            threading.Thread(
                target=_copy_stream,
                args=(process.stdout, stdout, "stdout", output),
                daemon=True,
            ),
            threading.Thread(
                target=_copy_stream,
                args=(process.stderr, stderr, "stderr", output),
                daemon=True,
            ),
        ]
        for thread in threads:
            thread.start()
        closed = 0
        while closed < OUTPUT_STREAM_COUNT or process.poll() is None:
            try:
                _channel, line = output.get(timeout=0.5)
            except queue.Empty:
                continue
            if line is None:
                closed += 1
            elif COMPLETED_PATTERN.search(line):
                completed = min(completed + 1, expected)
                print(f"Samples: {completed}/{expected}", flush=True)
        for thread in threads:
            thread.join(timeout=2)
        process.stdout.close()
        process.stderr.close()
    return process.wait()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--inspect-workspace", type=Path, required=True)
    parser.add_argument("--candidate-root", type=Path, required=True)
    parser.add_argument("--candidate-id", required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--concurrency", type=int, default=1)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    controller = os.environ.get("ZHIYUAN_AGENTRL_CONTROLLER", "").rstrip("/")
    agentrl_root = Path(os.environ.get("ZHIYUAN_AGENTRL_ROOT", ""))
    if not controller or not agentrl_root.is_dir():
        raise RuntimeError(
            "ZHIYUAN_AGENTRL_CONTROLLER and an existing ZHIYUAN_AGENTRL_ROOT are required"
        )
    indices = controller_preflight(controller, args.task)
    indices_range, expected = selected_range(indices, args.limit)
    args.output.mkdir(parents=True, exist_ok=True)
    store = args.output / "store"
    store.mkdir(exist_ok=True)
    os.environ["ZHIYUAN_CANDIDATE_ROOT"] = str(args.candidate_root.resolve())
    os.environ["ZHIYUAN_CANDIDATE_ID"] = args.candidate_id
    os.environ.pop("ZHIYUAN_CANDIDATE_POLICY_MODULE", None)
    os.environ.pop("ZHIYUAN_ENABLE_SUBAGENT", None)
    config = GatewayConfig(
        inspect_workspace=args.inspect_workspace.resolve(),
        candidate_root=args.candidate_root.resolve(),
        candidate_id=args.candidate_id,
        timeout_seconds=float(os.environ.get("ZHIYUAN_GATEWAY_TIMEOUT_SECONDS", "600")),
        audit_path=args.output / "gateway-events.jsonl",
    )
    with GatewayServer(config) as gateway:
        agentrl_python = os.environ.get("ZHIYUAN_AGENTRL_PYTHON", sys.executable)
        command = build_agentrl_command(
            python=agentrl_python,
            controller=controller,
            gateway_base_url=gateway.base_url,
            model_id=config.model_id,
            task=args.task,
            output_root=args.output,
            store=store,
            concurrency=args.concurrency,
            indices_range=indices_range,
        )
        environment = os.environ.copy()
        source_root = agentrl_root / "eval" / "src"
        existing_pythonpath = environment.get("PYTHONPATH")
        environment["PYTHONPATH"] = os.pathsep.join(
            [str(source_root), *([existing_pythonpath] if existing_pythonpath else [])]
        )
        return_code = run_agentrl(
            command,
            environment=environment,
            output_dir=args.output,
            expected=expected,
        )
    completed = _completed_results(store / "results.jsonl")
    if return_code != 0:
        raise RuntimeError(f"agentrl-eval exited with code {return_code}")
    if completed != expected:
        raise RuntimeError(f"agentrl-eval completed {completed} of {expected} samples")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
