import io
import importlib.util
import json
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from zhiyuan_bench.events import EventSink
from zhiyuan_bench.models import Candidate, Phase
from zhiyuan_bench.registry import select_bridge, suite_by_id
from zhiyuan_bench.runner import (
    PROGRESS_PATTERN,
    _inspect_journal_completed,
    _progress_advanced,
    create_run,
    build_phases,
    execute_phase,
    invalidate_failed_validation_sources,
    mark_manifest_running,
    pending_phases,
    _health_checks,
    run_manifest,
)


class RunnerTests(unittest.TestCase):
    def test_progress_parser_ignores_docker_build_steps(self) -> None:
        self.assertIsNone(PROGRESS_PATTERN.search("#5 [1/7] FROM python:3.12"))

    def test_inspect_journal_reports_only_sample_count(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with zipfile.ZipFile(root / "run.eval", "w") as archive:
                archive.writestr("_journal/start.json", "{}")
                archive.writestr("samples/opaque-1.json", "{}")
                archive.writestr("_journal/summaries/1.json", "{}")
                archive.writestr("samples/opaque-2.json", "{}")

            self.assertEqual(_inspect_journal_completed(root), 2)
            self.assertTrue(_progress_advanced(None, (2, 45)))
            self.assertTrue(_progress_advanced((2, 45), (3, 45)))
            self.assertFalse(_progress_advanced((45, 45), (1, 45)))

    def test_inspect_journal_ignores_logs_from_previous_phase(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old_log = root / "old.eval"
            with zipfile.ZipFile(old_log, "w") as archive:
                for index in range(45):
                    archive.writestr(f"samples/old-{index}.json", "{}")
            with zipfile.ZipFile(root / "current.eval", "w") as archive:
                archive.writestr("samples/current.json", "{}")

            self.assertEqual(
                _inspect_journal_completed(root, exclude=frozenset({old_log})), 1
            )

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

    def test_execute_phase_rejects_unsuccessful_inspect_log(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log_dir = root / "evals"
            log_dir.mkdir()
            sink = EventSink(root / "events.jsonl", "run-1", stream=io.StringIO())
            phase = Phase(
                id="fake-eval",
                label="Fake evaluation",
                command=(sys.executable, "-c", "pass"),
                environment={},
                progress_log_dir=log_dir,
                expected_samples=1,
            )

            with patch(
                "zhiyuan_bench.runner._validate_inspect_phase_log",
                side_effect=RuntimeError("Inspect evaluation log is not complete: error"),
            ):
                return_code = execute_phase(
                    phase, workspace=root, run_dir=root, sink=sink
                )

            self.assertEqual(return_code, 1)
            self.assertIn(
                "Inspect evaluation log is not complete: error",
                (root / "logs" / "fake-eval.stderr.log").read_text(),
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
            self.assertIn(
                str(Path(__file__).resolve().parents[1] / "src"),
                phases[0].environment["PYTHONPATH"],
            )

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
                    "report",
                ],
            )
            preflight = phases[1]
            self.assertIn("user=openai-api/zhiyuan/gemma-test", preflight.command)
            self.assertIn("require_inspect_tool_call=false", preflight.command)
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
            self.assertIn("--allow-no-inspect-tool-call", validation.command)
            self.assertIn(candidate.revision, validation.command)
            full = phases[3]
            self.assertNotIn("message_limit=1", full.command)
            self.assertNotIn("user_max_tokens=256", full.command)
            full_validation = phases[4]
            self.assertIn("--expected-samples", full_validation.command)
            self.assertIn("2", full_validation.command)
            self.assertIn("--require-reviewer-subagent", full_validation.command)
            report = phases[5]
            self.assertIn("--solo", report.command)
            self.assertIn("--require-production-agent", report.command)
            self.assertIn("--require-reviewer-subagent", report.command)

    def test_loopback_model_role_bypasses_system_proxy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            suite = suite_by_id("tau2-airline")
            with patch.dict(
                os.environ,
                {
                    "ZHIYUAN_MODEL_BASE_URL": "http://127.0.0.1:18010/v1",
                    "ZHIYUAN_MODEL_ID": "gemma-test",
                    "NO_PROXY": "internal.test",
                },
                clear=False,
            ):
                phases = build_phases(
                    suite,
                    select_bridge(suite),
                    [Candidate("candidate", root, "a" * 40)],
                    root,
                    root / "run",
                    limit=1,
                )

            preflight = next(
                phase for phase in phases if phase.id == "preflight-candidate"
            )
            self.assertEqual(
                preflight.environment["NO_PROXY"], "internal.test,127.0.0.1"
            )
            self.assertEqual(
                preflight.environment["no_proxy"], "internal.test,127.0.0.1"
            )

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
                    "report",
                ],
            )
            preflight = phases[1]
            self.assertIn("grader=openai-api/zhiyuan/gemma-test", preflight.command)
            self.assertIn("preflight_benign_only=true", preflight.command)
            self.assertIn("--timeout", preflight.command)
            self.assertIn("300", preflight.command)
            self.assertIn("--attempt-timeout", preflight.command)
            self.assertIn("--max-retries", preflight.command)
            self.assertIn("0", preflight.command)
            self.assertEqual(
                preflight.environment["ZHIYUAN_BASE_URL"],
                "http://model.test/v1",
            )
            full = phases[3]
            self.assertIn("grader=openai-api/zhiyuan/gemma-test", full.command)
            self.assertNotIn("preflight_benign_only=true", full.command)

    def test_solo_report_preserves_explicitly_disabled_reviewer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate = Candidate("candidate", root, "a" * 40)
            phases = build_phases(
                suite_by_id("agentbench-os-dev"),
                select_bridge(suite_by_id("agentbench-os-dev")),
                [candidate],
                root,
                root / "run",
                limit=2,
                reviewer_required_candidates=set(),
            )

            evaluation = next(
                phase for phase in phases if phase.id == "eval-candidate"
            )
            report = next(phase for phase in phases if phase.id == "report")
            self.assertNotIn(
                "ZHIYUAN_REQUIRE_REVIEWER_SUBAGENT", evaluation.environment
            )
            self.assertNotIn("--require-reviewer-subagent", report.command)
            self.assertIn("--solo", report.command)
            self.assertIn("candidate", report.command)
            self.assertIn("2", report.command)

    def test_bfcl_solo_run_generates_non_production_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            suite = suite_by_id("bfcl-single-turn")
            phases = build_phases(
                suite,
                select_bridge(suite),
                [Candidate("candidate", root, "a" * 40)],
                root,
                root / "run",
                limit=3,
            )

            self.assertEqual([phase.id for phase in phases], ["eval-candidate", "report"])
            report = phases[-1]
            self.assertIn("--solo", report.command)
            self.assertIn("--expected-samples", report.command)
            self.assertIn("3", report.command)
            self.assertNotIn("--require-production-agent", report.command)
            self.assertNotIn("--require-reviewer-subagent", report.command)

    def test_model_role_timeout_can_be_configured(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            suite = suite_by_id("codeipi")
            with patch.dict(
                os.environ,
                {"ZHIYUAN_INSPECT_MODEL_TIMEOUT_SECONDS": "420"},
                clear=False,
            ):
                phases = build_phases(
                    suite,
                    select_bridge(suite),
                    [Candidate("candidate", root, "a" * 40)],
                    root,
                    root / "run",
                    limit=1,
                )

            full = next(phase for phase in phases if phase.id == "eval-candidate")
            timeout_index = full.command.index("--timeout")
            attempt_index = full.command.index("--attempt-timeout")
            self.assertEqual(full.command[timeout_index + 1], "420")
            self.assertEqual(full.command[attempt_index + 1], "420")

    def test_production_inspect_time_limit_exceeds_bridge_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            suite = suite_by_id("agentbench-os-dev")
            with patch.dict(
                os.environ,
                {"ZHIYUAN_BRIDGE_TIMEOUT_SECONDS": "1800"},
                clear=False,
            ):
                phases = build_phases(
                    suite,
                    select_bridge(suite),
                    [Candidate("candidate", root, "a" * 40)],
                    root,
                    root / "run",
                    limit=1,
                )

            preflight = next(
                phase for phase in phases if phase.id == "preflight-candidate"
            )
            full = next(phase for phase in phases if phase.id == "eval-candidate")
            self.assertIn("--time-limit", preflight.command)
            self.assertIn("1860", preflight.command)
            self.assertIn("--time-limit", full.command)
            self.assertIn("1860", full.command)

    def test_rejects_inspect_time_limit_not_above_bridge_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            suite = suite_by_id("agentbench-os-dev")
            with (
                patch.dict(
                    os.environ,
                    {
                        "ZHIYUAN_BRIDGE_TIMEOUT_SECONDS": "1800",
                        "ZHIYUAN_INSPECT_TIME_LIMIT_SECONDS": "1800",
                    },
                    clear=False,
                ),
                self.assertRaisesRegex(
                    ValueError,
                    "ZHIYUAN_INSPECT_TIME_LIMIT_SECONDS must be greater",
                ),
            ):
                build_phases(
                    suite,
                    select_bridge(suite),
                    [Candidate("candidate", root, "a" * 40)],
                    root,
                    root / "run",
                    limit=1,
                )

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

    def test_reviewer_validation_can_target_only_candidate2(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidates = [
                Candidate("candidate1", root, "a" * 40),
                Candidate("candidate2", root, "b" * 40),
            ]
            suite = suite_by_id("agentbench-os-dev")
            phases = build_phases(
                suite,
                select_bridge(suite),
                candidates,
                root,
                root / "run",
                limit=2,
                reviewer_required_candidates={"candidate2"},
            )

            baseline_preflight = next(
                phase for phase in phases if phase.id == "validate-preflight-candidate1"
            )
            candidate_preflight = next(
                phase for phase in phases if phase.id == "validate-preflight-candidate2"
            )
            baseline_full = next(
                phase for phase in phases if phase.id == "validate-full-candidate1"
            )
            candidate_full = next(
                phase for phase in phases if phase.id == "validate-full-candidate2"
            )
            report = next(phase for phase in phases if phase.id == "report")

            self.assertNotIn("--require-reviewer-subagent", baseline_preflight.command)
            self.assertNotIn("--require-reviewer-subagent", baseline_full.command)
            self.assertIn("--require-reviewer-subagent", candidate_preflight.command)
            self.assertIn("--require-reviewer-subagent", candidate_full.command)
            baseline_eval_preflight = next(
                phase for phase in phases if phase.id == "preflight-candidate1"
            )
            candidate_eval_preflight = next(
                phase for phase in phases if phase.id == "preflight-candidate2"
            )
            baseline_eval = next(
                phase for phase in phases if phase.id == "eval-candidate1"
            )
            candidate_eval = next(
                phase for phase in phases if phase.id == "eval-candidate2"
            )
            self.assertNotIn(
                "ZHIYUAN_REQUIRE_REVIEWER_SUBAGENT", baseline_eval_preflight.environment
            )
            self.assertNotIn(
                "ZHIYUAN_REQUIRE_REVIEWER_SUBAGENT", baseline_eval.environment
            )
            self.assertEqual(
                candidate_eval_preflight.environment[
                    "ZHIYUAN_REQUIRE_REVIEWER_SUBAGENT"
                ],
                "true",
            )
            self.assertEqual(
                candidate_eval.environment["ZHIYUAN_REQUIRE_REVIEWER_SUBAGENT"],
                "true",
            )
            self.assertNotIn(
                "--require-baseline-reviewer-subagent", report.command
            )
            self.assertIn("--require-candidate-reviewer-subagent", report.command)

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

    def test_failed_validation_invalidates_its_source_phase(self) -> None:
        manifest = {
            "phases": {
                "preflight-candidate": {"status": "succeeded"},
                "validate-preflight-candidate": {"status": "failed"},
                "eval-candidate": {"status": "succeeded"},
                "validate-full-candidate": {"status": "failed"},
            }
        }

        self.assertTrue(invalidate_failed_validation_sources(manifest))
        self.assertEqual(
            manifest["phases"]["preflight-candidate"]["status"], "failed"
        )
        self.assertEqual(manifest["phases"]["eval-candidate"]["status"], "failed")
        self.assertFalse(invalidate_failed_validation_sources(manifest))

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
