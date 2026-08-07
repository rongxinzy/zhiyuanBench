import tempfile
import unittest
from pathlib import Path

from zhiyuan_bench.reports import render_campaign_report, write_campaign_report


class ReportTests(unittest.TestCase):
    def test_report_escapes_values_and_contains_prompt_free_progress(self) -> None:
        summary = {
            "campaign_id": "campaign-<unsafe>",
            "status": "completed_with_issues",
            "created_at": "2026-08-06T00:00:00+00:00",
            "completed_at": "2026-08-06T01:00:00+00:00",
            "candidates": [
                {
                    "label": "candidate",
                    "source_ref": "feature/<script>",
                    "revision": "a" * 40,
                }
            ],
            "suites": [
                {
                    "id": "agentbench-os-dev",
                    "bridge": "headless-pi-production",
                    "status": "failed",
                    "progress": {"completed": 8, "total": 26},
                    "failure": {"message": "bridge timeout"},
                }
            ],
        }

        report = render_campaign_report(summary)

        self.assertIn("campaign-&lt;unsafe&gt;", report)
        self.assertNotIn("<script>", report)
        self.assertIn('value="8" max="26"', report)
        self.assertIn("bridge timeout", report)

    def test_writes_standalone_html_and_json(self) -> None:
        summary = {
            "campaign_id": "campaign",
            "status": "succeeded",
            "created_at": "2026-08-06T00:00:00+00:00",
            "candidates": [],
            "suites": [],
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            write_campaign_report(root, summary)

            self.assertTrue((root / "report" / "report.html").is_file())
            self.assertTrue((root / "report" / "summary.json").is_file())

    def test_running_report_refreshes_until_campaign_finishes(self) -> None:
        running = render_campaign_report(
            {
                "campaign_id": "campaign",
                "status": "running",
                "created_at": "2026-08-06T00:00:00+00:00",
                "candidates": [],
                "suites": [],
            }
        )
        finished = render_campaign_report(
            {
                "campaign_id": "campaign",
                "status": "succeeded",
                "created_at": "2026-08-06T00:00:00+00:00",
                "candidates": [],
                "suites": [],
            }
        )

        self.assertIn('<meta http-equiv="refresh" content="3">', running)
        self.assertNotIn('http-equiv="refresh"', finished)


if __name__ == "__main__":
    unittest.main()
