import json
import os
import subprocess
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

try:
    from starlette.testclient import TestClient

    from zhiyuan_bench.campaigns import write_campaign
    from zhiyuan_bench.web import (
        CampaignConflictError,
        CampaignLauncher,
        WebConfig,
        create_app,
    )
except ImportError:
    TestClient = None


class FakeLauncher:
    def __init__(self) -> None:
        self.launched: list[Path] = []

    def launch(self, campaign_dir: Path) -> dict[str, object]:
        self.launched.append(campaign_dir)
        return {"schema_version": 1, "pid": 1234}


class ConflictLauncher(FakeLauncher):
    def launch(self, campaign_dir: Path) -> dict[str, object]:
        raise CampaignConflictError("runner already active")


@unittest.skipIf(TestClient is None, "web optional dependencies are not installed")
class WebTests(unittest.TestCase):
    def setUp(self) -> None:
        self.environment = patch.dict(
            os.environ,
            {
                "ZHIYUAN_MODEL_BASE_URL": "http://model.test/v1",
                "ZHIYUAN_MODEL_ID": "gemma-test",
            },
        )
        self.environment.start()
        self.services = patch(
            "zhiyuan_bench.web._service_readiness",
            return_value={
                "model_api": {"ready": True, "reason": "ready"},
                "docker": {"ready": True, "reason": "ready"},
            },
        )
        self.services.start()
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(
            ["git", "-C", str(self.repo), "init"], check=True, capture_output=True
        )
        subprocess.run(
            ["git", "-C", str(self.repo), "config", "user.name", "Test User"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.repo), "config", "user.email", "test@example.com"],
            check=True,
        )
        (self.repo / "value.txt").write_text("value\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "value.txt"], check=True)
        subprocess.run(
            ["git", "-C", str(self.repo), "commit", "-m", "initial"],
            check=True,
            capture_output=True,
        )
        self.records = self.root / "records"
        self.launcher = FakeLauncher()
        self.config = WebConfig(self.repo, self.root, self.records)
        self.client = TestClient(create_app(self.config, launcher=self.launcher))

    def tearDown(self) -> None:
        self.services.stop()
        self.environment.stop()
        self.temporary.cleanup()

    def _campaign(
        self, campaign_id: str = "20260806T000000Z__main-aaaaaaaa__1234"
    ) -> Path:
        path = self.records / campaign_id
        write_campaign(
            path,
            {
                "schema_version": 1,
                "campaign_id": campaign_id,
                "status": "created",
                "created_at": datetime.now(UTC).isoformat(),
                "repo": str(self.repo),
                "workspace": str(self.root),
                "records_root": str(self.records),
                "limit": None,
                "concurrency": 1,
                "reviewer_required_candidates": ["candidate"],
                "candidates": [
                    {"label": "candidate", "source_ref": "main", "revision": "a" * 40}
                ],
                "suites": [
                    {
                        "id": "bfcl-single-turn",
                        "bridge": "headless-pi-capture",
                        "status": "created",
                        "attempts": 0,
                    }
                ],
            },
        )
        return path

    def test_health_suites_and_local_branches(self) -> None:
        index = self.client.get("/")
        self.assertEqual(index.status_code, 200)
        self.assertIn("知远评测", index.text)
        self.assertEqual(self.client.get("/api/health").json()["status"], "ok")
        suites = self.client.get("/api/suites").json()
        self.assertEqual(len(suites), 8)
        self.assertEqual(suites[0]["id"], "agentbench-os-dev")
        branches = self.client.get("/api/branches").json()
        self.assertEqual(len(branches), 1)
        self.assertEqual(len(branches[0]["revision"]), 40)

    def test_readiness_reports_missing_configuration_without_values(self) -> None:
        with patch.dict(
            os.environ,
            {"ZHIYUAN_MODEL_BASE_URL": "", "ZHIYUAN_MODEL_ID": ""},
        ):
            readiness = self.client.get("/api/readiness").json()

        self.assertFalse(readiness["ready"])
        self.assertEqual(
            readiness["missing_environment"],
            ["ZHIYUAN_MODEL_BASE_URL", "ZHIYUAN_MODEL_ID"],
        )
        self.assertNotIn("http://model.test/v1", json.dumps(readiness))
        self.assertEqual(len(readiness["suites"]), 8)

    def test_readiness_reports_unreachable_services_without_details(self) -> None:
        self.services.stop()
        try:
            with (
                patch("zhiyuan_bench.web.urllib.request.urlopen", side_effect=OSError),
                patch("zhiyuan_bench.web.subprocess.run") as run,
                patch.dict(os.environ, {"DOCKER_HOST": "ssh://docker.test"}),
            ):
                run.return_value.returncode = 1
                readiness = self.client.get("/api/readiness").json()
        finally:
            self.services.start()

        self.assertFalse(readiness["ready"])
        self.assertFalse(readiness["suites"][0]["ready"])
        self.assertEqual(
            readiness["services"],
            {
                "model_api": {"ready": False, "reason": "unreachable"},
                "docker": {"ready": False, "reason": "unreachable"},
            },
        )
        self.assertNotIn("172.18", json.dumps(readiness))

    def test_lists_and_reads_campaign_records(self) -> None:
        path = self._campaign()
        listing = self.client.get("/api/campaigns")
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(listing.json()[0]["campaign_id"], path.name)
        detail = self.client.get(f"/api/campaigns/{path.name}")
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()["counts"]["created"], 1)

    def test_listing_preserves_multiple_campaign_records(self) -> None:
        older = self._campaign("20260806T000000Z__main-aaaaaaaa__older")
        newer = self._campaign("20260806T010000Z__main-aaaaaaaa__newer")

        listing = self.client.get("/api/campaigns").json()

        self.assertEqual(
            [item["campaign_id"] for item in listing],
            [newer.name, older.name],
        )

    def test_create_campaign_and_launch(self) -> None:
        path = self._campaign("created-campaign")
        with patch("zhiyuan_bench.web.create_campaign", return_value=path) as create:
            response = self.client.post(
                "/api/campaigns",
                json={
                    "branches": [{"label": "candidate", "ref": "main"}],
                    "suites": ["bfcl-single-turn"],
                    "reviewer_required_candidates": [],
                    "run": True,
                },
            )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["runner"]["pid"], 1234)
        create.assert_called_once()
        self.assertEqual(create.call_args.kwargs["reviewer_required_candidates"], set())
        self.assertEqual(
            [item.resolve() for item in self.launcher.launched], [path.resolve()]
        )

    def test_create_run_rejects_unready_runtime_before_saving_campaign(self) -> None:
        with (
            patch.dict(
                os.environ,
                {"ZHIYUAN_MODEL_BASE_URL": "", "ZHIYUAN_MODEL_ID": ""},
            ),
            patch("zhiyuan_bench.web.create_campaign") as create,
        ):
            response = self.client.post(
                "/api/campaigns",
                json={
                    "branches": [{"label": "candidate", "ref": "main"}],
                    "suites": ["bfcl-single-turn"],
                    "run": True,
                },
            )

        self.assertEqual(response.status_code, 503)
        self.assertIn("ZHIYUAN_MODEL_BASE_URL", response.json()["error"])
        create.assert_not_called()
        self.assertEqual(self.client.get("/api/campaigns").json(), [])

    def test_create_without_run_allows_unready_runtime(self) -> None:
        path = self._campaign("saved-campaign")
        with (
            patch.dict(
                os.environ,
                {"ZHIYUAN_MODEL_BASE_URL": "", "ZHIYUAN_MODEL_ID": ""},
            ),
            patch("zhiyuan_bench.web.create_campaign", return_value=path) as create,
        ):
            response = self.client.post(
                "/api/campaigns",
                json={
                    "branches": [{"label": "candidate", "ref": "main"}],
                    "suites": ["bfcl-single-turn"],
                    "run": False,
                },
            )

        self.assertEqual(response.status_code, 201)
        create.assert_called_once()

    def test_create_returns_saved_campaign_when_launch_conflicts(self) -> None:
        path = self._campaign("created-campaign")
        client = TestClient(create_app(self.config, launcher=ConflictLauncher()))
        with patch("zhiyuan_bench.web.create_campaign", return_value=path):
            response = client.post(
                "/api/campaigns",
                json={
                    "branches": [{"label": "candidate", "ref": "main"}],
                    "suites": ["bfcl-single-turn"],
                    "run": True,
                },
            )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["campaign"]["campaign_id"], path.name)

    def test_run_existing_campaign(self) -> None:
        path = self._campaign()
        response = self.client.post(f"/api/campaigns/{path.name}/run")
        self.assertEqual(response.status_code, 202)
        self.assertEqual(
            [item.resolve() for item in self.launcher.launched], [path.resolve()]
        )

    def test_run_existing_campaign_rejects_unready_runtime(self) -> None:
        path = self._campaign()
        with patch.dict(
            os.environ,
            {"ZHIYUAN_MODEL_BASE_URL": "", "ZHIYUAN_MODEL_ID": ""},
        ):
            response = self.client.post(f"/api/campaigns/{path.name}/run")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.launcher.launched, [])

    def test_launcher_refuses_existing_campaign_lock(self) -> None:
        path = self._campaign()
        (self.records / "campaign.lock").write_text(
            json.dumps({"pid": os.getpid(), "run_id": path.name, "token": "test"}),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(CampaignConflictError, "active with PID"):
            CampaignLauncher(self.config).launch(path)

        self.assertFalse((path / "web-runner.json").exists())

    def test_launcher_refuses_live_web_runner_record(self) -> None:
        path = self._campaign()
        (path / "web-runner.json").write_text(
            json.dumps({"schema_version": 1, "pid": os.getpid()}),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(CampaignConflictError, "already active"):
            CampaignLauncher(self.config).launch(path)

    def test_reads_existing_events_as_finite_sse(self) -> None:
        path = self._campaign()
        event = {
            "schema_version": 1,
            "sequence": 1,
            "timestamp": datetime.now(UTC).isoformat(),
            "run_id": path.name,
            "event_type": "campaign_created",
            "phase": None,
            "status": "created",
            "details": {},
        }
        (path / "events.jsonl").write_text(json.dumps(event) + "\n", encoding="utf-8")

        response = self.client.get(f"/api/campaigns/{path.name}/events?follow=0")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.headers["content-type"].split(";")[0], "text/event-stream"
        )
        self.assertIn("event: campaign_created", response.text)
        self.assertIn('"sequence": 1', response.text)

    def test_serves_standalone_campaign_report(self) -> None:
        path = self._campaign()

        response = self.client.get(f"/api/campaigns/{path.name}/report")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"].split(";")[0], "text/html")
        self.assertIn(path.name, response.text)


if __name__ == "__main__":
    unittest.main()
