import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from zhiyuan_bench.events import EventSink
from zhiyuan_bench.models import Candidate, Phase
from zhiyuan_bench.registry import select_bridge, suite_by_id
from zhiyuan_bench.runner import (
    PROGRESS_PATTERN,
    build_phases,
    execute_phase,
    mark_manifest_running,
    pending_phases,
)


class RunnerTests(unittest.TestCase):
    def test_progress_parser_ignores_docker_build_steps(self) -> None:
        self.assertIsNone(PROGRESS_PATTERN.search("#5 [1/7] FROM python:3.12"))

    def test_execute_phase_emits_numeric_progress(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sink = EventSink(root / "events.jsonl", "run-1", stream=io.StringIO())
            phase = Phase(
                id="fake-eval",
                label="Fake evaluation",
                command=(
                    sys.executable,
                    "-c",
                    "print('Samples: 1/2', flush=True); print('Samples: 2/2', flush=True)",
                ),
                environment={},
            )
            return_code = execute_phase(phase, workspace=root, run_dir=root, sink=sink)
            self.assertEqual(return_code, 0)
            events = [
                json.loads(line)
                for line in (root / "events.jsonl").read_text().splitlines()
            ]
            progress = [
                event["details"]
                for event in events
                if event["event_type"] == "phase_progress"
            ]
            self.assertEqual(
                progress,
                [
                    {"completed": 1, "total": 2},
                    {"completed": 2, "total": 2},
                ],
            )

    def test_pending_phases_skip_only_successes(self) -> None:
        phases = [
            Phase("done", "Done", ("true",), {}),
            Phase("running", "Running", ("true",), {}),
            Phase("new", "New", ("true",), {}),
        ]
        manifest = {
            "phases": {
                "done": {"status": "succeeded"},
                "running": {"status": "running"},
            }
        }
        self.assertEqual(
            [phase.id for phase in pending_phases(manifest, phases)],
            ["running", "new"],
        )

    def test_agentrl_phase_uses_auto_selected_gateway_adapter(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            suite = suite_by_id("agentbench-dbbench-std")
            bridge = select_bridge(suite)
            candidate = Candidate("candidate", root, "a" * 40)

            phases = build_phases(
                suite,
                bridge,
                [candidate],
                root,
                root / "run",
                limit=3,
                concurrency=2,
            )

            self.assertEqual(len(phases), 1)
            self.assertEqual(
                phases[0].command[1:3], ("-m", "zhiyuan_bench.agentrl_adapter")
            )
            self.assertIn("--limit", phases[0].command)
            self.assertIn("--concurrency", phases[0].command)
            self.assertIn("zhiyuanBench\\src", phases[0].environment["PYTHONPATH"])

    def test_tau2_phase_configures_direct_user_model_role(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            suite = suite_by_id("tau2-airline")
            bridge = select_bridge(suite)
            candidate = Candidate("candidate", root, "a" * 40)
            with patch.dict(
                os.environ,
                {
                    "ZHIYUAN_MODEL_BASE_URL": "http://model.test/v1",
                    "ZHIYUAN_MODEL_ID": "gemma-test",
                },
                clear=False,
            ):
                phases = build_phases(
                    suite,
                    bridge,
                    [candidate],
                    root,
                    root / "run",
                    limit=2,
                )

            self.assertEqual(
                [phase.id for phase in phases],
                [
                    "build-candidate",
                    "preflight-candidate",
                    "validate-preflight-candidate",
                    "eval-candidate",
                ],
            )
            preflight = phases[1]
            self.assertIn("user=openai-api/zhiyuan/gemma-test", preflight.command)
            self.assertIn("message_limit=1", preflight.command)
            self.assertIn("user_max_tokens=256", preflight.command)
            self.assertIn("user_timeout_seconds=180", preflight.command)
            self.assertEqual(
                preflight.environment["ZHIYUAN_BASE_URL"],
                "http://model.test/v1",
            )
            self.assertEqual(preflight.environment["ZHIYUAN_API_KEY"], "local-eval")
            validation = phases[2]
            self.assertIn("tools.zhiyuan.validate_production_log", validation.command)
            self.assertIn(candidate.revision, validation.command)
            full = phases[3]
            self.assertNotIn("message_limit=1", full.command)
            self.assertNotIn("user_max_tokens=256", full.command)

    def test_resume_clears_stale_terminal_state(self) -> None:
        manifest = {
            "status": "failed",
            "failure": {"type": "OSError"},
            "completed_at": "old",
        }

        mark_manifest_running(manifest)

        self.assertEqual(manifest, {"status": "running"})


if __name__ == "__main__":
    unittest.main()
