import io
import importlib.util
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
    create_run,
    build_phases,
    execute_phase,
    mark_manifest_running,
    pending_phases,
    _health_checks,
    run_manifest,
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
                    "validate-full-candidate",
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
            full_validation = phases[4]
            self.assertIn("--expected-samples", full_validation.command)
            self.assertIn("2", full_validation.command)
            self.assertIn("--require-reviewer-subagent", full_validation.command)

    def test_codeipi_phase_uses_benign_preflight_and_grader_role(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            suite = suite_by_id("codeipi")
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
                    limit=3,
                )

            self.assertEqual(
                [phase.id for phase in phases],
                [
                    "build-candidate",
                    "preflight-candidate",
                    "validate-preflight-candidate",
                    "eval-candidate",
                    "validate-full-candidate",
                ],
            )
            preflight = phases[1]
            self.assertIn("grader=openai-api/zhiyuan/gemma-test", preflight.command)
            self.assertIn("preflight_benign_only=true", preflight.command)
            self.assertEqual(
                preflight.environment["ZHIYUAN_BASE_URL"],
                "http://model.test/v1",
            )
            full = phases[3]
            self.assertIn("grader=openai-api/zhiyuan/gemma-test", full.command)
            self.assertNotIn("preflight_benign_only=true", full.command)

    def test_agentdojo_phase_uses_benign_non_sandbox_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            suite = suite_by_id("agentdojo")
            phases = build_phases(
                suite,
                select_bridge(suite),
                [Candidate("candidate", root, "a" * 40)],
                root,
                root / "run",
                limit=None,
            )

            preflight = next(
                phase for phase in phases if phase.id == "preflight-candidate"
            )
            full = next(phase for phase in phases if phase.id == "eval-candidate")
            self.assertIn("with_injections=false", preflight.command)
            self.assertIn("with_sandbox_tasks=no", preflight.command)
            self.assertNotIn("with_injections=false", full.command)
            self.assertNotIn("with_sandbox_tasks=no", full.command)

    def test_health_check_rejects_missing_suite_python_module(self) -> None:
        suite = suite_by_id("swe-bench-verified-mini")
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(sys, "platform", "linux"),
            patch.object(importlib.util, "find_spec", return_value=None),
            self.assertRaisesRegex(RuntimeError, "swebench, jsonlines"),
        ):
            root = Path(directory)
            sink = EventSink(root / "events.jsonl", "run-1", stream=io.StringIO())
            _health_checks(suite, sink)

    def test_health_check_rejects_unsupported_host_platform(self) -> None:
        suite = suite_by_id("swe-bench-verified-mini")
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(sys, "platform", "win32"),
            self.assertRaisesRegex(
                RuntimeError, "requires host platform linux or darwin"
            ),
        ):
            root = Path(directory)
            sink = EventSink(root / "events.jsonl", "run-1", stream=io.StringIO())
            _health_checks(suite, sink)

    def test_resume_clears_stale_terminal_state(self) -> None:
        manifest = {
            "status": "failed",
            "failure": {"type": "OSError"},
            "completed_at": "old",
        }

        mark_manifest_running(manifest)

        self.assertEqual(manifest, {"status": "running"})

    def test_full_validation_failure_prevents_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidates = [
                Candidate("candidate1", root / "candidate1", "a" * 40),
                Candidate("candidate2", root / "candidate2", "b" * 40),
            ]
            for candidate in candidates:
                candidate.root.mkdir()
            run_dir = create_run(
                suite_id="agentbench-os-dev",
                bridge_id="auto",
                candidates=candidates,
                workspace=root,
                output_root=root / "output",
                limit=2,
            )
            executed: list[str] = []

            def fake_execute(phase: Phase, **_kwargs: object) -> int:
                executed.append(phase.id)
                return 1 if phase.id == "validate-full-candidate2" else 0

            with (
                patch("zhiyuan_bench.runner._verify_candidate"),
                patch("zhiyuan_bench.runner.execute_phase", side_effect=fake_execute),
                self.assertRaisesRegex(RuntimeError, "validate-full-candidate2"),
            ):
                run_manifest(run_dir, health_checks=False)

            self.assertIn("validate-full-candidate2", executed)
            self.assertNotIn("report", executed)


if __name__ == "__main__":
    unittest.main()
