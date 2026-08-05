"""Local OpenAI Chat Completions gateway backed by the headless Pi worker."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


WORKER_ENVIRONMENT = (
    "ZHIYUAN_CANDIDATE_ROOT",
    "ZHIYUAN_CANDIDATE_ID",
    "ZHIYUAN_MODEL_BASE_URL",
    "ZHIYUAN_MODEL_API_KEY",
    "ZHIYUAN_MODEL_ID",
    "ZHIYUAN_MODEL_CONTEXT_WINDOW",
    "ZHIYUAN_MODEL_MAX_TOKENS",
    "ZHIYUAN_MODEL_TEMPERATURE",
    "ZHIYUAN_MODEL_SEED",
    "ZHIYUAN_HTTP_IDLE_TIMEOUT_MS",
    "ZHIYUAN_PI_THINKING_LEVEL",
)


@dataclass(frozen=True)
class GatewayConfig:
    inspect_workspace: Path
    candidate_root: Path
    candidate_id: str
    model_id: str = "zhiyuan-headless-pi"
    model_profile: str = "zhiyuan-agentrl-fc"
    timeout_seconds: float = 600
    message_limit: int | None = 64
    token_limit: int | None = None
    tool_call_limit: int | None = 64
    audit_path: Path | None = None


BridgeRunner = Callable[[list[dict[str, Any]], list[dict[str, Any]]], dict[str, Any]]


class PromptFreeAudit:
    def __init__(self, path: Path | None) -> None:
        self.path = path
        self._lock = threading.Lock()

    def emit(self, event_type: str, **details: Any) -> None:
        if self.path is None:
            return
        event = {
            "timestamp": time.time(),
            "event_type": event_type,
            "details": details,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self.path.open("a", encoding="utf-8", newline="\n") as target:
            target.write(json.dumps(event, ensure_ascii=True, sort_keys=True) + "\n")


def _tool_specs(tools: Any) -> list[dict[str, Any]]:
    if tools is None:
        return []
    if not isinstance(tools, list):
        raise ValueError("tools must be an array")
    specs: list[dict[str, Any]] = []
    names: set[str] = set()
    for item in tools:
        if not isinstance(item, dict):
            raise ValueError("only OpenAI function tools are supported")
        function = item.get("function")
        if item.get("type") != "function" or not isinstance(function, dict):
            raise ValueError("only OpenAI function tools are supported")
        name = function.get("name")
        if not isinstance(name, str) or not name or name in names:
            raise ValueError("tool names must be non-empty and unique")
        parameters = function.get("parameters", {"type": "object", "properties": {}})
        if not isinstance(parameters, dict):
            raise ValueError(f"tool {name!r} parameters must be an object")
        names.add(name)
        specs.append(
            {
                "name": name,
                "description": str(function.get("description", "")),
                "parameters": parameters,
            }
        )
    return specs


def _messages(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise ValueError("messages must be a non-empty array")
    if not all(isinstance(message, dict) for message in value):
        raise ValueError("every message must be an object")
    return value


def completion_from_bridge(
    payload: dict[str, Any],
    *,
    model_id: str,
    run_bridge: BridgeRunner,
) -> dict[str, Any]:
    if payload.get("stream") is True:
        raise ValueError("streaming responses are not supported")
    messages = _messages(payload.get("messages"))
    tools = _tool_specs(payload.get("tools"))
    result = run_bridge(messages, tools)
    if result.get("status") != "succeeded":
        failure = result.get("failure") or {}
        code = failure.get("code", "bridge_failed")
        raise RuntimeError(
            f"{code}: {failure.get('message', 'headless Pi run failed')}"
        )
    assistant = result.get("assistant") or {}
    tool_calls = [
        {
            "id": call["id"],
            "type": "function",
            "function": {
                "name": call["function"],
                "arguments": json.dumps(
                    call.get("arguments", {}), ensure_ascii=True, separators=(",", ":")
                ),
            },
        }
        for call in assistant.get("tool_calls", [])
    ]
    message: dict[str, Any] = {
        "role": "assistant",
        "content": assistant.get("content") or None,
    }
    if tool_calls:
        message["tool_calls"] = tool_calls
    usage = result.get("usage") or {}
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model_id,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": "tool_calls" if tool_calls else "stop",
            }
        ],
        "usage": {
            "prompt_tokens": int(usage.get("input_tokens", 0)),
            "completion_tokens": int(usage.get("output_tokens", 0)),
            "total_tokens": int(usage.get("total_tokens", 0)),
        },
    }


class HeadlessPiBridgeRunner:
    def __init__(self, config: GatewayConfig, audit: PromptFreeAudit) -> None:
        self.config = config
        self.audit = audit

    def __call__(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> dict[str, Any]:
        return asyncio.run(self._run(messages, tools))

    async def _run(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> dict[str, Any]:
        source_root = self.config.inspect_workspace / "src"
        if str(source_root) not in sys.path:
            sys.path.insert(0, str(source_root))
        from inspect_evals.zhiyuan_bridge import (  # noqa: PLC0415
            HeadlessPiClient,
            HeadlessPiConfig,
            RunLimits,
            RunStart,
            ToolSpec,
            headless_pi_worker_command,
        )

        run_id = uuid.uuid4().hex
        request = RunStart(
            run_id=run_id,
            sample_id=f"gateway-{run_id}",
            epoch=0,
            candidate_id=self.config.candidate_id,
            model_profile=self.config.model_profile,
            messages=messages,
            limits=RunLimits(
                timeout_seconds=self.config.timeout_seconds,
                message_limit=self.config.message_limit,
                token_limit=self.config.token_limit,
                tool_call_limit=self.config.tool_call_limit,
            ),
            tools=[ToolSpec.model_validate(tool) for tool in tools],
            tool_mode="capture" if tools else "none",
        )
        client = HeadlessPiClient(
            HeadlessPiConfig(
                command=headless_pi_worker_command(),
                timeout_seconds=self.config.timeout_seconds + 5,
                working_directory=str(self.config.inspect_workspace),
                environment_names=WORKER_ENVIRONMENT,
            )
        )
        self.audit.emit("bridge_request_started", run_id=run_id, tool_count=len(tools))
        result = await client.run(request)
        self.audit.emit(
            "bridge_request_finished",
            run_id=run_id,
            status=result.status,
            tool_call_count=len(result.assistant.tool_calls) if result.assistant else 0,
        )
        return result.model_dump(mode="json")


class GatewayServer:
    def __init__(
        self,
        config: GatewayConfig,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        bridge_runner: BridgeRunner | None = None,
    ) -> None:
        self.config = config
        self.audit = PromptFreeAudit(config.audit_path)
        self.bridge_runner = bridge_runner or HeadlessPiBridgeRunner(config, self.audit)
        self._thread: threading.Thread | None = None
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if self.path.rstrip("/") == "/v1/models":
                    self._json(
                        200,
                        {
                            "object": "list",
                            "data": [
                                {
                                    "id": owner.config.model_id,
                                    "object": "model",
                                    "owned_by": "zhiyuan-bench",
                                }
                            ],
                        },
                    )
                else:
                    self._json(404, {"error": {"message": "not found"}})

            def do_POST(self) -> None:
                if self.path.rstrip("/") != "/v1/chat/completions":
                    self._json(404, {"error": {"message": "not found"}})
                    return
                try:
                    length = int(self.headers.get("content-length", "0"))
                    if length <= 0 or length > 4 * 1024 * 1024:
                        raise ValueError("request body size is invalid")
                    payload = json.loads(self.rfile.read(length))
                    if not isinstance(payload, dict):
                        raise ValueError("request body must be an object")
                    response = completion_from_bridge(
                        payload,
                        model_id=owner.config.model_id,
                        run_bridge=owner.bridge_runner,
                    )
                    self._json(200, response)
                except ValueError as error:
                    self._json(
                        400,
                        {"error": {"type": "invalid_request", "message": str(error)}},
                    )
                except Exception as error:
                    owner.audit.emit(
                        "bridge_request_error", error_type=type(error).__name__
                    )
                    self._json(
                        502, {"error": {"type": "bridge_error", "message": str(error)}}
                    )

            def _json(self, status: int, value: dict[str, Any]) -> None:
                body = json.dumps(value, ensure_ascii=True).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format: str, *_args: object) -> None:
                return

        self.server = ThreadingHTTPServer((host, port), Handler)
        self.server.daemon_threads = True

    @property
    def base_url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}/v1"

    def start(self) -> None:
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def __enter__(self) -> "GatewayServer":
        self.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Serve the local Zhiyuan FC gateway")
    parser.add_argument("--inspect-workspace", type=Path, required=True)
    parser.add_argument("--candidate-root", type=Path, required=True)
    parser.add_argument("--candidate-id", required=True)
    parser.add_argument("--port", type=int, default=8001)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    os.environ["ZHIYUAN_CANDIDATE_ROOT"] = str(args.candidate_root.resolve())
    os.environ["ZHIYUAN_CANDIDATE_ID"] = args.candidate_id
    server = GatewayServer(
        GatewayConfig(
            inspect_workspace=args.inspect_workspace.resolve(),
            candidate_root=args.candidate_root.resolve(),
            candidate_id=args.candidate_id,
        ),
        port=args.port,
    )
    try:
        server.server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
