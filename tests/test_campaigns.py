import subprocess
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from zhiyuan_bench.campaigns import (
    campaign_id,
    campaign_summary,
    create_campaign,
    read_campaign,
    run_campaign,
    write_campaign,
)
from zhiyuan_bench.runner import SuiteUnavailableError


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


class CampaignTests(unittest.TestCase):
    def test_campaign_id_contains_time_refs_shas_and_nonce(self) -> None:
        value = campaign_id(
            [
                ("baseline", "main", "a" * 40),
                ("candidate", "feature/agent", "b" * 40),
            ],
            now=datetime(2026, 8, 6, 7, 37, 42, tzinfo=UTC),
            nonce="9c42",
        )

        self.assertEqual(
            value,
            "20260806T073742Z__main-aaaaaaaa__feature-agen-bbbbbbbb__9c42",
        )

    def test_campaign_id_bounds_branch_fragments_for_windows_paths(self) -> None:
        value = campaign_id(
            [
                ("baseline", "a" * 100, "a" * 40),
                ("candidate", "b" * 100, "b" * 40),
            ],
            now=datetime(2026, 8, 6, 7, 37, 42, tzinfo=UTC),
            nonce="9c42",
        )

        self.assertEqual(len(value), 68)
        self.assertIn("a" * 12 + "-aaaaaaaa", value)
        self.assertIn("b" * 12 + "-bbbbbbbb", value)

    def test_create_campaign_resolves_refs_and_shares_worktrees(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "repo"
            repo.mkdir()
            _git(repo, "init")
            _git(repo, "config", "user.name", "Test User")
            _git(repo, "config", "user.email", "test@example.com")
            (repo / "value.txt").write_text("one\n", encoding="utf-8")
            _git(repo, "add", "value.txt")
            _git(repo, "commit", "-m", "first")
            _git(repo, "branch", "baseline")
            (repo / "value.txt").write_text("two\n", encoding="utf-8")
            _git(repo, "commit", "-am", "second")
            _git(repo, "branch", "candidate")

            campaign_dir = create_campaign(
                repo=repo,
                branch_values=["baseline=baseline", "candidate=candidate"],
                suite_ids=["agentbench-os-dev", "bfcl-single-turn"],
                workspace=root,
                records_root=root / "records",
                reviewer_required_candidates=set(),
            )

            manifest = read_campaign(campaign_dir)
            self.assertEqual(
                [suite["id"] for suite in manifest["suites"]],
                ["agentbench-os-dev", "bfcl-single-turn"],
            )
            self.assertEqual(len(manifest["candidates"]), 2)
            self.assertTrue(
                all(
                    len(candidate["revision"]) == 40
                    for candidate in manifest["candidates"]
                )
            )
            self.assertTrue(
                all(
                    Path(candidate["root"]).parent == campaign_dir / "worktrees"
                    for candidate in manifest["candidates"]
                )
            )
            self.assertTrue((campaign_dir / "live-summary.json").is_file())
            self.assertTrue((campaign_dir / "events.jsonl").is_file())
            self.assertEqual(manifest["reviewer_required_candidates"], [])
            self.assertEqual(manifest["suites"][0]["progress"], {"completed": 0, "total": 52})
            self.assertEqual(
                manifest["suites"][1]["progress"],
                {"completed": 0, "total": 7962},
            )
            self.assertTrue((campaign_dir / "report" / "report.html").is_file())
            self.assertTrue((campaign_dir / "report" / "summary.json").is_file())

    def test_summary_translates_legacy_phase_progress_to_campaign_progress(self) -> None:
        manifest = {
            "schema_version": 1,
            "campaign_id": "legacy",
            "status": "running",
            "created_at": "2026-08-07T00:00:00+00:00",
            "limit": None,
            "candidates": [
                {"label": "baseline", "source_ref": "main", "revision": "a" * 40},
                {
                    "label": "candidate",
                    "source_ref": "feature",
                    "revision": "b" * 40,
                },
            ],
            "suites": [
                {
                    "id": "agentbench-os-dev",
                    "bridge": "headless-pi-production",
                    "status": "running",
                    "phase": "eval-baseline",
                    "progress": {"completed": 4, "total": 26},
                }
            ],
        }

        summary = campaign_summary(manifest)

        self.assertEqual(
            summary["suites"][0]["progress"], {"completed": 4, "total": 52}
        )
        self.assertEqual(
            summary["suites"][0]["phase_progress"],
            {"completed": 4, "total": 26},
        )

    def test_summary_recovers_phase_from_legacy_failure(self) -> None:
        manifest = {
            "schema_version": 1,
            "campaign_id": "legacy-failure",
            "status": "completed_with_issues",
            "created_at": "2026-08-07T00:00:00+00:00",
            "limit": None,
            "candidates": [
                {"label": "baseline", "source_ref": "main", "revision": "a" * 40},
                {
                    "label": "candidate",
                    "source_ref": "feature",
                    "revision": "b" * 40,
                },
            ],
            "suites": [
                {
                    "id": "agentdojo",
                    "bridge": "headless-pi-production",
                    "status": "failed",
                    "phase": None,
                    "progress": {"completed": 158, "total": 1014},
                    "failure": {
                        "type": "RuntimeError",
                        "message": "Phase eval-baseline failed; see logs",
                    },
                }
            ],
        }

        summary = campaign_summary(manifest)

        self.assertEqual(summary["suites"][0]["phase"], "eval-baseline")
        self.assertEqual(
            summary["suites"][0]["progress"], {"completed": 158, "total": 2028}
        )
        self.assertEqual(
            summary["suites"][0]["phase_progress"],
            {"completed": 158, "total": 1014},
        )

    def test_invalid_reviewer_label_does_not_create_worktrees(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch(
                    "zhiyuan_bench.campaigns.resolve_branch_revisions",
                    return_value=[("candidate", "main", "a" * 40)],
                ),
                patch("zhiyuan_bench.campaigns.create_resolved_candidates") as create,
                self.assertRaisesRegex(ValueError, "not configured"),
            ):
                create_campaign(
                    repo=root,
                    branch_values=["candidate=main"],
                    suite_ids=["bfcl-single-turn"],
                    workspace=root,
                    records_root=root / "records",
                    reviewer_required_candidates={"missing"},
                )

            create.assert_not_called()

    def test_run_continues_after_unavailable_and_failed_suites(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            campaign_dir = root / "records" / "campaign"
            candidates = []
            for label, revision in (("baseline", "a" * 40), ("candidate", "b" * 40)):
                candidate_root = root / label
                candidate_root.mkdir()
                candidates.append(
                    {
                        "label": label,
                        "root": str(candidate_root),
                        "revision": revision,
                        "source_ref": label,
                        "source_repo": str(root),
                    }
                )
            manifest = {
                "schema_version": 1,
                "campaign_id": "campaign",
                "status": "created",
                "created_at": datetime.now(UTC).isoformat(),
                "repo": str(root),
                "workspace": str(root),
                "records_root": str(root / "records"),
                "limit": 1,
                "concurrency": 1,
                "reviewer_required_candidates": ["baseline", "candidate"],
                "candidates": candidates,
                "suites": [
                    {
                        "id": "agentbench-os-dev",
                        "bridge": "headless-pi-production",
                        "status": "created",
                        "attempts": 0,
                    },
                    {
                        "id": "codeipi",
                        "bridge": "headless-pi-production",
                        "status": "created",
                        "attempts": 0,
                    },
                    {
                        "id": "bfcl-single-turn",
                        "bridge": "headless-pi-capture",
                        "status": "created",
                        "attempts": 0,
                    },
                ],
            }
            write_campaign(campaign_dir, manifest)
            created_runs: list[Path] = []

            def fake_create_run(**kwargs: object) -> Path:
                run_dir = Path(str(kwargs["output_root"])) / "runs" / "run"
                run_dir.mkdir(parents=True)
                created_runs.append(run_dir)
                return run_dir

            def fake_run(run_dir: Path, **kwargs: object) -> None:
                suite_id = manifest["suites"][int(run_dir.parents[1].name)]["id"]
                listener = kwargs["event_listener"]
                for phase in ("eval-baseline", "eval-candidate"):
                    listener(
                        {
                            "event_type": "phase_progress",
                            "phase": phase,
                            "status": "running",
                            "details": {"completed": 1, "total": 1},
                        }
                    )
                listener(
                    {
                        "event_type": "run_finished",
                        "status": "succeeded",
                    }
                )
                if suite_id == "agentbench-os-dev":
                    raise SuiteUnavailableError("Docker unavailable")
                if suite_id == "codeipi":
                    raise RuntimeError("evaluation failed")

            with (
                patch(
                    "zhiyuan_bench.campaigns.create_run", side_effect=fake_create_run
                ),
                patch("zhiyuan_bench.campaigns.run_manifest", side_effect=fake_run),
            ):
                run_campaign(campaign_dir)

            completed = read_campaign(campaign_dir)
            self.assertEqual(
                [suite["status"] for suite in completed["suites"]],
                ["skipped", "failed", "succeeded"],
            )
            self.assertEqual(completed["status"], "completed_with_issues")
            self.assertEqual(len(created_runs), 3)
            self.assertTrue(
                all(run.parents[2].name == "_runs" for run in created_runs)
            )
            summary = campaign_summary(completed)
            self.assertEqual(summary["counts"]["skipped"], 1)
            self.assertEqual(summary["counts"]["failed"], 1)
            self.assertEqual(summary["counts"]["succeeded"], 1)
            self.assertTrue(
                all(
                    suite["progress"] == {"completed": 2, "total": 2}
                    for suite in completed["suites"]
                )
            )
            self.assertTrue(
                all(
                    suite["phase_progress"] == {"completed": 1, "total": 1}
                    for suite in completed["suites"]
                )
            )
            self.assertTrue(
                all(suite["phase"] == "eval-candidate" for suite in completed["suites"])
            )

    def test_resume_reuses_run_and_skips_successful_suite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            campaign_dir = root / "records" / "campaign"
            run_dir = campaign_dir / "suites" / "codeipi" / "runs" / "existing"
            run_dir.mkdir(parents=True)
            manifest = {
                "schema_version": 1,
                "campaign_id": "campaign",
                "status": "completed_with_issues",
                "created_at": datetime.now(UTC).isoformat(),
                "repo": str(root),
                "workspace": str(root),
                "records_root": str(root / "records"),
                "limit": 1,
                "concurrency": 1,
                "reviewer_required_candidates": ["candidate"],
                "candidates": [
                    {
                        "label": "candidate",
                        "root": str(root),
                        "revision": "a" * 40,
                        "source_ref": "candidate",
                        "source_repo": str(root),
                    }
                ],
                "suites": [
                    {
                        "id": "agentbench-os-dev",
                        "bridge": "headless-pi-production",
                        "status": "succeeded",
                        "attempts": 1,
                    },
                    {
                        "id": "codeipi",
                        "bridge": "headless-pi-production",
                        "status": "failed",
                        "attempts": 1,
                        "run_dir": str(run_dir),
                    },
                ],
            }
            write_campaign(campaign_dir, manifest)

            with (
                patch("zhiyuan_bench.campaigns.create_run") as create_mock,
                patch("zhiyuan_bench.campaigns.run_manifest") as run_mock,
            ):
                run_campaign(campaign_dir)

            create_mock.assert_not_called()
            run_mock.assert_called_once()
            self.assertEqual(run_mock.call_args.args[0], run_dir)
            completed = read_campaign(campaign_dir)
            self.assertEqual(completed["status"], "succeeded")
            self.assertEqual(completed["suites"][1]["attempts"], 2)


if __name__ == "__main__":
    unittest.main()
