"""Built-in suites and capability-based bridge selection."""

from __future__ import annotations

from zhiyuan_bench.models import BridgeDefinition, SuiteDefinition


BRIDGES = (
    BridgeDefinition(
        id="headless-pi-capture",
        description="Pi model bridge with structured Inspect tool-call capture",
        capabilities=frozenset(
            {"inspect", "model_api", "tool_capture", "progress_events", "run_limits"}
        ),
        priority=20,
    ),
    BridgeDefinition(
        id="headless-pi-production",
        description="Pi production-policy bridge with Inspect sandbox tool execution",
        capabilities=frozenset(
            {
                "inspect",
                "model_api",
                "inspect_tools",
                "sandbox",
                "production_policy",
                "progress_events",
                "run_limits",
            }
        ),
        priority=10,
    ),
)


SUITES = (
    SuiteDefinition(
        id="agentbench-os-dev",
        description="AgentBench OS public development split (26 samples)",
        adapter="inspect-agentbench",
        required_capabilities=frozenset(
            {
                "inspect",
                "model_api",
                "inspect_tools",
                "sandbox",
                "production_policy",
                "progress_events",
                "run_limits",
            }
        ),
        expected_samples=26,
        task="tools/zhiyuan/baseline.py@zhiyuan_agent_bench_os_dev",
        production_policy=True,
        preflight=True,
        max_candidates=2,
    ),
    SuiteDefinition(
        id="bfcl-single-turn",
        description="BFCL supported single-turn categories",
        adapter="inspect-bfcl",
        required_capabilities=frozenset(
            {"inspect", "model_api", "tool_capture", "progress_events", "run_limits"}
        ),
        expected_samples=None,
        task="tools/zhiyuan/baseline.py@zhiyuan_bfcl",
    ),
    SuiteDefinition(
        id="agentbench-os-dev-subagent",
        description="AgentBench OS dev requiring an independent reviewer subagent",
        adapter="inspect-agentbench",
        required_capabilities=frozenset(
            {
                "inspect",
                "model_api",
                "inspect_tools",
                "sandbox",
                "production_policy",
                "subagent",
                "progress_events",
                "run_limits",
            }
        ),
        expected_samples=26,
        task="tools/zhiyuan/baseline.py@zhiyuan_agent_bench_os_dev",
        production_policy=True,
        preflight=True,
        max_candidates=2,
    ),
)


def suite_by_id(suite_id: str) -> SuiteDefinition:
    for suite in SUITES:
        if suite.id == suite_id:
            return suite
    raise ValueError(f"Unknown suite: {suite_id}")


def bridge_by_id(bridge_id: str) -> BridgeDefinition:
    for bridge in BRIDGES:
        if bridge.id == bridge_id:
            return bridge
    raise ValueError(f"Unknown bridge: {bridge_id}")


def select_bridge(
    suite: SuiteDefinition, requested: str = "auto"
) -> BridgeDefinition:
    candidates = list(BRIDGES) if requested == "auto" else [bridge_by_id(requested)]
    compatible = [
        bridge
        for bridge in candidates
        if suite.required_capabilities.issubset(bridge.capabilities)
    ]
    if not compatible:
        available = sorted(set().union(*(bridge.capabilities for bridge in candidates)))
        missing = sorted(suite.required_capabilities.difference(available))
        raise ValueError(
            f"No bridge satisfies suite {suite.id!r}; missing capabilities: "
            + ", ".join(missing)
        )
    return min(
        compatible,
        key=lambda bridge: (
            len(bridge.capabilities - suite.required_capabilities),
            bridge.priority,
            bridge.id,
        ),
    )

