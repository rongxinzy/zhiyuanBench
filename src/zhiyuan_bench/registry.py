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
        id="headless-pi-agentrl-fc",
        description="OpenAI-compatible Pi gateway for AgentRL function-calling tasks",
        capabilities=frozenset(
            {
                "agentrl_controller",
                "model_api",
                "multi_turn",
                "native_function_calls",
                "progress_events",
                "resume",
                "run_limits",
                "task_workers",
            }
        ),
        priority=10,
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
                "subagent",
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
                "subagent",
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
    *(
        SuiteDefinition(
            id=f"agentbench-{suite_id}",
            description=description,
            adapter="agentrl-agentbench-fc",
            required_capabilities=frozenset(
                {
                    "agentrl_controller",
                    "model_api",
                    "multi_turn",
                    "native_function_calls",
                    "progress_events",
                    "resume",
                    "run_limits",
                    "task_workers",
                }
            ),
            expected_samples=expected_samples,
            task=task,
            required_environment=(
                "ZHIYUAN_AGENTRL_CONTROLLER",
                "ZHIYUAN_AGENTRL_ROOT",
            ),
            health_url_environment=health_urls,
            notes=notes,
        )
        for suite_id, description, task, expected_samples, health_urls, notes in (
            (
                "alfworld-std",
                "AgentBench ALFWorld standard function-calling task",
                "alfworld-std",
                None,
                (),
                ("Requires controller workers with ALFWorld assets.",),
            ),
            (
                "dbbench-std",
                "AgentBench DBBench standard function-calling task",
                "dbbench-std",
                None,
                (),
                (
                    "Requires controller workers with MySQL/SQLite and Redis-backed isolation.",
                ),
            ),
            (
                "kg-std",
                "AgentBench knowledge-graph standard function-calling task",
                "kg-std",
                None,
                ("ZHIYUAN_AGENTBENCH_KG_SPARQL_URL",),
                ("Requires a reachable Freebase-compatible SPARQL service.",),
            ),
            (
                "webshop-std",
                "AgentBench WebShop standard function-calling task",
                "webshop-std",
                200,
                (),
                (
                    "Requires WebShop task workers; upstream recommends about 16 GB RAM.",
                ),
            ),
        )
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


def select_bridge(suite: SuiteDefinition, requested: str = "auto") -> BridgeDefinition:
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
