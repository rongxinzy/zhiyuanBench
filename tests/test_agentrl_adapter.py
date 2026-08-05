import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from zhiyuan_bench.agentrl_adapter import (
    build_agentrl_command,
    run_agentrl,
    selected_range,
)


class AgentRLAdapterTests(unittest.TestCase):
    def test_selected_range_is_explicit_and_bounded(self) -> None:
        self.assertEqual(selected_range([0, 1, 2], 2), ("0-1", 2))
        self.assertEqual(selected_range([0, 1, 2], None), (None, 3))
        with self.assertRaisesRegex(ValueError, "between 1 and 3"):
            selected_range([0, 1, 2], 4)
        with self.assertRaisesRegex(ValueError, "contiguous"):
            selected_range([0, 2, 4], 2)

    def test_command_forces_chat_completions_and_deterministic_sampling(self) -> None:
        command = build_agentrl_command(
            python="python",
            controller="http://controller/api",
            gateway_base_url="http://127.0.0.1:1234/v1",
            model_id="zhiyuan-headless-pi",
            task="dbbench-std",
            output_root=Path("out"),
            store=Path("out/store"),
            concurrency=2,
            indices_range="0-2",
        )
        self.assertIn("--chat-completions", command)
        self.assertIn("--no-parallel-tool-calls", command)
        self.assertEqual(command[command.index("--temperature") + 1], "0")
        self.assertEqual(command[-1], "dbbench-std")

    def test_subprocess_output_becomes_numeric_progress_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            store = output_dir / "store"
            store.mkdir()
            (store / "results.jsonl").write_text(
                json.dumps({"status": "completed"}) + "\n", encoding="utf-8"
            )
            script = (
                "print('completed task=secret index=one', flush=True);"
                "print('completed task=secret index=two', flush=True)"
            )
            visible = io.StringIO()
            with redirect_stdout(visible):
                return_code = run_agentrl(
                    (sys.executable, "-c", script),
                    environment=os.environ.copy(),
                    output_dir=output_dir,
                    expected=3,
                )
            self.assertEqual(return_code, 0)
            self.assertEqual(
                visible.getvalue().splitlines(),
                ["Samples: 1/3", "Samples: 2/3", "Samples: 3/3"],
            )
            self.assertNotIn("secret", visible.getvalue())


if __name__ == "__main__":
    unittest.main()
