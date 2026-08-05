# zhiyuanBench

`zhiyuanBench` is a lightweight, prompt-free orchestration layer for repeatable Zhiyuan agent evaluations. It explicitly selects a suite, resolves a compatible bridge by declared capabilities, prevents concurrent runners, resumes completed phases, writes JSONL progress, and cleans only evaluation containers created after the current phase started.

It has no runtime dependencies beyond Python 3.11. Inspect and candidate runtimes remain external tools owned by their respective repositories.

## Commands

```text
python -m zhiyuan_bench list-suites
python -m zhiyuan_bench list-bridges
python -m zhiyuan_bench run --suite agentbench-os-dev --workspace D:\rxzy\inspect_evals --candidate baseline=C:\tmp\RongxinAI-eval-candidate-1@FULL_SHA --candidate candidate=C:\tmp\RongxinAI-eval-candidate-2@FULL_SHA
python -m zhiyuan_bench monitor .zhiyuan-bench\runs\RUN_ID --follow
python -m zhiyuan_bench resume .zhiyuan-bench\runs\RUN_ID
```

Use `PYTHONPATH=src` when running directly from a checkout, or install the project in editable mode.

## Runtime configuration

Model and infrastructure configuration is read from environment variables so API keys never enter command history or the run manifest.

Required for model-backed suites:

```text
ZHIYUAN_MODEL_BASE_URL=http://host:8000/v1
ZHIYUAN_MODEL_ID=gemma-4-31B-it
```

Production sandbox suites also require `DOCKER_HOST`. Set `ZHIYUAN_SSH_TARGET` to make a separate SSH health check mandatory. Existing `ZHIYUAN_MODEL_*` sampling variables and `ZHIYUAN_CANDIDATE_POLICY_MAX_ITERATIONS` pass through to the bridge.

## Safety and resume behavior

The output root contains one atomic `runner.lock`, preventing a second run. A lock is recovered only when its recorded process is no longer alive. Each phase records its pre-existing container IDs before launch; cleanup considers only IDs created afterward and skips every container with mounts. A resumed run skips successful phases and repeats incomplete ones.

`events.jsonl` contains suite, bridge, phase state, elapsed time, numeric progress, and cleanup counts. It intentionally does not contain benchmark prompts, targets, answers, or sample IDs. Raw command output remains in the run-local `logs` directory for diagnosis.

## Capability boundaries

The current production bridge supports Inspect tools, sandbox execution, production policy activation, progress events, and in-prompt run limits. It does not claim `subagent`, `approval_ui`, `ask_user`, or `mcp`. Selecting `agentbench-os-dev-subagent` therefore fails before launch instead of silently degrading to a same-model critic.
