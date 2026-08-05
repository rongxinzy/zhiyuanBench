import unittest

from zhiyuan_bench.registry import select_bridge, suite_by_id


class RegistryTests(unittest.TestCase):
    def test_auto_selects_production_bridge(self) -> None:
        suite = suite_by_id("agentbench-os-dev")
        self.assertEqual(select_bridge(suite).id, "headless-pi-production")

    def test_auto_selects_capture_bridge(self) -> None:
        suite = suite_by_id("bfcl-single-turn")
        self.assertEqual(select_bridge(suite).id, "headless-pi-capture")

    def test_missing_subagent_fails_before_launch(self) -> None:
        suite = suite_by_id("agentbench-os-dev-subagent")
        with self.assertRaisesRegex(ValueError, "subagent"):
            select_bridge(suite)

    def test_explicit_incompatible_bridge_is_rejected(self) -> None:
        suite = suite_by_id("agentbench-os-dev")
        with self.assertRaisesRegex(ValueError, "missing capabilities"):
            select_bridge(suite, "headless-pi-capture")


if __name__ == "__main__":
    unittest.main()

