import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from zhiyuan_bench.persistence import atomic_write_text


class PersistenceTests(unittest.TestCase):
    def test_atomic_write_retries_transient_permission_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            real_replace = os.replace
            attempts = 0

            def replace_with_transient_lock(source: Path, target: Path) -> None:
                nonlocal attempts
                attempts += 1
                if attempts < 3:
                    raise PermissionError("temporarily locked")
                real_replace(source, target)

            with (
                patch(
                    "zhiyuan_bench.persistence.os.replace",
                    side_effect=replace_with_transient_lock,
                ),
                patch("zhiyuan_bench.persistence.time.sleep"),
            ):
                atomic_write_text(path, "value\n")

            self.assertEqual(attempts, 3)
            self.assertEqual(path.read_text(encoding="utf-8"), "value\n")
            self.assertEqual(list(path.parent.glob("*.tmp-*")), [])


if __name__ == "__main__":
    unittest.main()
