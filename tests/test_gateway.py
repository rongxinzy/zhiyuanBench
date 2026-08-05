import json
import tempfile
import unittest
import urllib.request
from pathlib import Path

from zhiyuan_bench.gateway import (
    GatewayConfig,
    GatewayServer,
    PromptFreeAudit,
    completion_from_bridge,
)


def _bridge_result() -> dict:
    return {
        "status": "succeeded",
        "assistant": {
            "content": "",
            "tool_calls": [
                {
                    "id": "call-1",
                    "function": "take_action",
                    "arguments": {"action": "look"},
                }
            ],
        },
        "usage": {"input_tokens": 4, "output_tokens": 3, "total_tokens": 7},
    }


class GatewayTests(unittest.TestCase):
    def test_maps_openai_tools_to_structured_bridge_completion(self) -> None:
        observed = {}

        def run_bridge(messages, tools):
            observed["messages"] = messages
            observed["tools"] = tools
            return _bridge_result()

        response = completion_from_bridge(
            {
                "model": "zhiyuan-headless-pi",
                "messages": [{"role": "user", "content": "task"}],
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "take_action",
                            "description": "Act",
                            "parameters": {"type": "object"},
                        },
                    }
                ],
            },
            model_id="zhiyuan-headless-pi",
            run_bridge=run_bridge,
        )

        self.assertEqual(observed["tools"][0]["name"], "take_action")
        call = response["choices"][0]["message"]["tool_calls"][0]
        self.assertEqual(call["function"]["name"], "take_action")
        self.assertEqual(json.loads(call["function"]["arguments"]), {"action": "look"})
        self.assertEqual(response["choices"][0]["finish_reason"], "tool_calls")

    def test_rejects_streaming_and_non_function_tools(self) -> None:
        with self.assertRaisesRegex(ValueError, "streaming"):
            completion_from_bridge(
                {"stream": True, "messages": [{"role": "user", "content": "x"}]},
                model_id="model",
                run_bridge=lambda _messages, _tools: _bridge_result(),
            )
        with self.assertRaisesRegex(ValueError, "function tools"):
            completion_from_bridge(
                {
                    "messages": [{"role": "user", "content": "x"}],
                    "tools": [{"type": "computer"}],
                },
                model_id="model",
                run_bridge=lambda _messages, _tools: _bridge_result(),
            )
        with self.assertRaisesRegex(ValueError, "function tools"):
            completion_from_bridge(
                {
                    "messages": [{"role": "user", "content": "x"}],
                    "tools": ["invalid"],
                },
                model_id="model",
                run_bridge=lambda _messages, _tools: _bridge_result(),
            )

    def test_http_gateway_exposes_models_and_chat_completions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = GatewayConfig(root, root, "candidate")
            with GatewayServer(
                config, bridge_runner=lambda _m, _t: _bridge_result()
            ) as server:
                with urllib.request.urlopen(f"{server.base_url}/models") as response:
                    models = json.load(response)
                request = urllib.request.Request(
                    f"{server.base_url}/chat/completions",
                    data=json.dumps(
                        {"messages": [{"role": "user", "content": "task"}]}
                    ).encode(),
                    headers={"content-type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(request) as response:
                    completion = json.load(response)
            self.assertEqual(models["data"][0]["id"], config.model_id)
            self.assertEqual(completion["object"], "chat.completion")

    def test_audit_never_records_prompt_or_answer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audit.jsonl"
            PromptFreeAudit(path).emit("done", run_id="run", tool_count=2)
            content = path.read_text(encoding="utf-8")
            self.assertNotIn("prompt", content)
            self.assertNotIn("answer", content)


if __name__ == "__main__":
    unittest.main()
