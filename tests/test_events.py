import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from zhiyuan_bench.events import EventSink, monitor


class EventTests(unittest.TestCase):
    def test_jsonl_events_are_sequenced_and_prompt_free(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            output = io.StringIO()
            sink = EventSink(path, "run-1", stream=output)
            sink.emit("phase_started", phase="eval", status="running")
            sink.emit(
                "phase_progress",
                phase="eval",
                status="running",
                details={"completed": 2, "total": 26},
            )
            events = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual([event["sequence"] for event in events], [1, 2])
            self.assertNotIn("prompt", path.read_text(encoding="utf-8"))
            self.assertIn("2/26", output.getvalue())

    def test_monitor_reads_existing_events(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            EventSink(run_dir / "events.jsonl", "run-1").emit(
                "run_finished", status="succeeded"
            )
            output = io.StringIO()
            with redirect_stdout(output):
                monitor(run_dir)
            self.assertIn("succeeded", output.getvalue())

    def test_sink_recovers_after_truncated_event(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            EventSink(path, "run-1").emit("first")
            with path.open("a", encoding="utf-8") as target:
                target.write('{"sequence":')

            EventSink(path, "run-1").emit("second")

            valid_events = []
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    valid_events.append(json.loads(line))
                except ValueError:
                    continue
            self.assertEqual(valid_events[-1]["sequence"], 2)


if __name__ == "__main__":
    unittest.main()
