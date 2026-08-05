import unittest

from zhiyuan_bench.registry import select_bridge, suite_by_id


class RegistryTests(unittest.TestCase):
    def test_auto_selects_production_bridge(self) -> None:
        suite = suite_by_id("agentbench-os-dev")
        self.assertEqual(select_bridge(suite).id, "headless-pi-production")

    def test_auto_selects_capture_bridge(self) -> None:
        suite = suite_by_id("bfcl-single-turn")
        self.assertEqual(select_bridge(suite).id, "headless-pi-capture")

    def test_production_bridge_exposes_subagent(self) -> None:
        suite = suite_by_id("agentbench-os-dev")
        bridge = select_bridge(suite)
        self.assertIn("subagent", bridge.capabilities)

    def test_auto_selects_agentrl_bridge_for_non_os_suites(self) -> None:
        for suite_id in (
            "agentbench-alfworld-std",
            "agentbench-dbbench-std",
            "agentbench-kg-std",
            "agentbench-webshop-std",
        ):
            with self.subTest(suite=suite_id):
                self.assertEqual(
                    select_bridge(suite_by_id(suite_id)).id,
                    "headless-pi-agentrl-fc",
                )

    def test_explicit_incompatible_bridge_is_rejected(self) -> None:
        suite = suite_by_id("agentbench-os-dev")
        with self.assertRaisesRegex(ValueError, "missing capabilities"):
            select_bridge(suite, "headless-pi-capture")


if __name__ == "__main__":
    unittest.main()
