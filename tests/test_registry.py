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

    def test_auto_selects_production_bridge_for_tau2(self) -> None:
        for suite_id in (
            "tau2-airline",
            "tau2-banking",
            "tau2-retail",
            "tau2-telecom",
        ):
            with self.subTest(suite=suite_id):
                bridge = select_bridge(suite_by_id(suite_id))
                self.assertEqual(bridge.id, "headless-pi-production")
                self.assertIn("user_simulator", bridge.capabilities)

    def test_auto_selects_production_bridge_for_codeipi(self) -> None:
        suite = suite_by_id("codeipi")
        bridge = select_bridge(suite)

        self.assertEqual(bridge.id, "headless-pi-production")
        self.assertEqual(suite.model_roles, ("grader",))
        self.assertIn("model_roles", bridge.capabilities)

    def test_auto_selects_production_bridge_for_swe_bench(self) -> None:
        suite = suite_by_id("swe-bench-verified-mini")

        self.assertEqual(select_bridge(suite).id, "headless-pi-production")
        self.assertEqual(suite.expected_samples, 50)
        self.assertEqual(suite.required_host_platforms, ("linux", "darwin"))
        self.assertEqual(suite.required_python_modules, ("swebench", "jsonlines"))

    def test_auto_selects_production_bridge_for_agentdojo(self) -> None:
        suite = suite_by_id("agentdojo")

        self.assertEqual(select_bridge(suite).id, "headless-pi-production")
        self.assertEqual(suite.expected_samples, 1014)
        self.assertEqual(
            suite.preflight_task_args,
            ("with_injections=false", "with_sandbox_tasks=no"),
        )
        self.assertEqual(suite.required_python_modules, ("deepdiff", "email_validator"))

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
