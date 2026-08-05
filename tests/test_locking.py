import json
import os
import tempfile
import unittest
from pathlib import Path

from zhiyuan_bench.locking import RunLock


class RunLockTests(unittest.TestCase):
    def test_refuses_live_runner(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runner.lock"
            path.write_text(
                json.dumps({"pid": os.getpid(), "run_id": "other", "token": "x"}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RuntimeError, "runner is active"):
                with RunLock(path, "new"):
                    pass

    def test_recovers_stale_lock_and_releases_own_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runner.lock"
            path.write_text(
                json.dumps({"pid": 2_147_483_647, "run_id": "old", "token": "x"}),
                encoding="utf-8",
            )
            with RunLock(path, "new"):
                self.assertTrue(path.is_file())
            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
