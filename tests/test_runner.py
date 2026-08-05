import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

from zhiyuan_bench.events import EventSink
from zhiyuan_bench.models import Phase
from zhiyuan_bench.runner import PROGRESS_PATTERN, execute_phase, pending_phases


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
            return_code = execute_phase(
                phase, workspace=root, run_dir=root, sink=sink
            )
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


if __name__ == "__main__":
    unittest.main()
