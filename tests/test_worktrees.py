import subprocess
import tempfile
import unittest
from json import dumps
from pathlib import Path

from zhiyuan_bench.worktrees import (
    cleanup_worktrees,
    create_branch_candidates,
    parse_branch,
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


class WorktreeTests(unittest.TestCase):
    def test_parse_branch_rejects_invalid_values(self) -> None:
        with self.assertRaisesRegex(ValueError, "LABEL=GIT_REF"):
            parse_branch("main")
        with self.assertRaisesRegex(ValueError, "Invalid candidate label"):
            parse_branch("bad label=main")

    def test_branch_candidates_resolve_refs_and_create_isolated_worktrees(self) -> None:
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
            first = _git(repo, "rev-parse", "HEAD")
            _git(repo, "branch", "candidate1")
            (repo / "value.txt").write_text("two\n", encoding="utf-8")
            _git(repo, "commit", "-am", "second")
            second = _git(repo, "rev-parse", "HEAD")
            _git(repo, "branch", "candidate2")

            candidates = create_branch_candidates(
                repo,
                ["baseline=candidate1", "candidate=candidate2"],
                root / "output",
            )

            self.assertEqual([item.revision for item in candidates], [first, second])
            self.assertEqual(
                [item.source_ref for item in candidates],
                ["candidate1", "candidate2"],
            )
            self.assertTrue(all(item.managed_worktree for item in candidates))
            self.assertEqual(
                [(item.root / "value.txt").read_text() for item in candidates],
                ["one\n", "two\n"],
            )

            cleanup_worktrees(candidates)

            self.assertTrue(all(not item.root.exists() for item in candidates))

    def test_policy_build_dependencies_are_available_in_managed_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "repo"
            repo.mkdir()
            _git(repo, "init")
            _git(repo, "config", "user.name", "Test User")
            _git(repo, "config", "user.email", "test@example.com")
            (repo / ".gitignore").write_text("node_modules/\n", encoding="utf-8")
            (repo / "package.json").write_text(
                dumps(
                    {
                        "scripts": {
                            "build:eval-policy": "node scripts/build-policy.mjs"
                        }
                    }
                ),
                encoding="utf-8",
            )
            (repo / "package-lock.json").write_text(
                dumps(
                    {
                        "packages": {
                            "node_modules/esbuild": {"version": "1.2.3"},
                            "node_modules/@earendil-works/pi-coding-agent": {
                                "version": "4.5.6"
                            },
                            "node_modules/typebox": {"version": "7.8.9"},
                            "node_modules/@esbuild/test-platform": {
                                "version": "1.2.3"
                            },
                        }
                    }
                ),
                encoding="utf-8",
            )
            esbuild = repo / "node_modules" / "esbuild"
            pi_agent = (
                repo
                / "node_modules"
                / "@earendil-works"
                / "pi-coding-agent"
            )
            platform = repo / "node_modules" / "@esbuild" / "test-platform"
            typebox = repo / "node_modules" / "typebox"
            esbuild.mkdir(parents=True)
            pi_agent.mkdir(parents=True)
            platform.mkdir(parents=True)
            typebox.mkdir(parents=True)
            (esbuild / "package.json").write_text(
                dumps(
                    {
                        "version": "1.2.3",
                        "optionalDependencies": {
                            "@esbuild/test-platform": "1.2.3"
                        },
                    }
                ),
                encoding="utf-8",
            )
            (platform / "package.json").write_text(
                dumps({"version": "1.2.3"}), encoding="utf-8"
            )
            (pi_agent / "package.json").write_text(
                dumps({"version": "4.5.6"}), encoding="utf-8"
            )
            (typebox / "package.json").write_text(
                dumps({"version": "7.8.9"}), encoding="utf-8"
            )
            _git(repo, "add", ".gitignore", "package.json", "package-lock.json")
            _git(repo, "commit", "-m", "policy build")

            candidates = create_branch_candidates(
                repo, ["candidate=HEAD"], root / "output"
            )

            candidate_modules = candidates[0].root / "node_modules"
            self.assertTrue((candidate_modules / "esbuild" / "package.json").is_file())
            self.assertTrue(
                (
                    candidate_modules
                    / "@esbuild"
                    / "test-platform"
                    / "package.json"
                ).is_file()
            )
            self.assertTrue(
                (
                    candidate_modules
                    / "@earendil-works"
                    / "pi-coding-agent"
                    / "package.json"
                ).is_file()
            )
            self.assertTrue((candidate_modules / "typebox" / "package.json").is_file())
            cleanup_worktrees(candidates)


if __name__ == "__main__":
    unittest.main()
