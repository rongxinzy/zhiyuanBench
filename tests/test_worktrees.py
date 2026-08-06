import subprocess
import tempfile
import unittest
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


if __name__ == "__main__":
    unittest.main()
