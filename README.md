# zhiyuanBench

`zhiyuanBench` is a lightweight, prompt-free orchestration layer for repeatable Zhiyuan agent evaluations. It explicitly selects a suite, resolves a compatible bridge by declared capabilities, prevents concurrent runners, resumes completed work, writes JSONL progress, and cleans only attributable evaluation containers.

It has no installed runtime dependencies beyond Python 3.11. Inspect, RongxinAI, AgentRL, AgentBench task workers, datasets, and container images remain in their owning repositories.

## Supported suites

| Suite | Runtime | Coverage | Required external capability |
| --- | --- | --- | --- |
| `agentbench-os-dev` | Inspect Evals | RongxinAI production policy, Inspect sandbox tools, isolated reviewer subagent | Docker sandbox |
| `tau2-airline` | Inspect Evals | Stateful customer-service tools, policy adherence, simulated user | Direct Gemma user model role |
| `tau2-banking` | Inspect Evals | Stateful banking tools, verification and offline KB retrieval | Direct Gemma user model role |
| `tau2-retail` | Inspect Evals | Stateful retail tools, policy adherence, simulated user | Direct Gemma user model role |
| `tau2-telecom` | Inspect Evals | Stateful telecom and user tools, troubleshooting workflow | Direct Gemma user model role |
| `codeipi` | Inspect Evals | Indirect prompt-injection resistance and coding task completion | Remote Docker and direct Gemma grader role |
| `agentdojo` | Inspect Evals | Stateful tool use, utility, and prompt-injection robustness across five application domains | Optional Python dependencies; 70 samples use remote Docker |
| `swe-bench-verified-mini` | Inspect Evals | Repository exploration, editing, debugging, and test-driven issue resolution | POSIX controller, remote Docker, official Python dependency, and large public images |
| `agentbench-alfworld-std` | AgentRL | Pi/model multi-turn native function calling | ALFWorld task worker and assets |
| `agentbench-dbbench-std` | AgentRL | Pi/model multi-turn native function calling | DBBench workers, MySQL/SQLite, Redis isolation |
| `agentbench-kg-std` | AgentRL | Pi/model multi-turn native function calling | KG worker and Freebase-compatible SPARQL service |
| `agentbench-webshop-std` | AgentRL | Pi/model multi-turn native function calling | WebShop workers; upstream recommends about 16 GB RAM |
| `bfcl-single-turn` | Inspect Evals | Pi/model structured tool-call capture | BFCL assets managed by Inspect Evals |

The non-OS AgentBench suites use the current AgentBench FC task workers through the AgentRL evaluation client. Digital Card Game, Lateral Thinking Puzzle, and Mind2Web are legacy AgentBench v0.2 tasks and are intentionally not registered until an explicit v0.2 runtime adapter is available.

## Architecture

```text
AgentRL controller and task workers
              |
         agentrl-eval
              |
local OpenAI Chat Completions gateway
              |
fresh headless Pi worker per model request
              |
        configured Gemma API
```

The gateway supports `/v1/models` and non-streaming `/v1/chat/completions`. It converts OpenAI function schemas to the bridge protocol, preserves prior tool calls and tool results in multi-turn replay, and converts Pi calls back to native OpenAI `tool_calls`. It never executes task tools itself; AgentRL sends calls to the official task worker.

## Commands

```text
python -m zhiyuan_bench list-suites
python -m zhiyuan_bench list-bridges
python -m zhiyuan_bench run --suite agentbench-os-dev --workspace D:\rxzy\inspect_evals --candidate baseline=C:\tmp\RongxinAI-eval-candidate-1@FULL_SHA --candidate candidate=C:\tmp\RongxinAI-eval-candidate-2@FULL_SHA
python -m zhiyuan_bench run --suite agentbench-dbbench-std --workspace D:\rxzy\inspect_evals --candidate candidate=C:\path\to\RongxinAI@FULL_SHA --limit 10 --concurrency 2
python -m zhiyuan_bench run --suite tau2-airline --workspace D:\rxzy\inspect_evals --candidate candidate=C:\path\to\RongxinAI@FULL_SHA --limit 5
python -m zhiyuan_bench compare --suite agentbench-os-dev --workspace D:\rxzy\inspect_evals --repo D:\rxzy\RongxinAI --branch baseline=branch-a --branch candidate=branch-b --reviewer-required candidate --output-root D:\eval-results\agentbench-os
python -m zhiyuan_bench monitor .zhiyuan-bench\runs\RUN_ID --follow
python -m zhiyuan_bench resume .zhiyuan-bench\runs\RUN_ID
```

Use `PYTHONPATH=src` when running directly from a checkout, or install the project in editable mode.

`compare` resolves each Git ref to a full commit SHA and creates a detached, isolated worktree under the output root. The manifest records the source ref, repository, resolved SHA, and worktree path. Worktrees are retained by default for diagnosis and resume; pass `--cleanup-worktrees` to remove only worktrees created by that successful command.

## Runtime configuration

Model and infrastructure configuration is read from environment variables so API keys never enter command history or the run manifest.

```text
ZHIYUAN_MODEL_BASE_URL=http://host:8000/v1
ZHIYUAN_MODEL_ID=gemma-4-31B-it
ZHIYUAN_MODEL_TEMPERATURE=0
ZHIYUAN_MODEL_SEED=237
ZHIYUAN_BRIDGE_TIMEOUT_SECONDS=1800
ZHIYUAN_INSPECT_TIME_LIMIT_SECONDS=1860
```

Production OS suites also require `DOCKER_HOST`. Set `ZHIYUAN_SSH_TARGET` to make a separate SSH health check mandatory. The runner automatically sets `ZHIYUAN_ENABLE_SUBAGENT=true` when the selected production bridge advertises the isolated reviewer capability.

`ZHIYUAN_BRIDGE_TIMEOUT_SECONDS` controls the complete main-agent session and is independent from the model HTTP idle timeout and `ZHIYUAN_SUBAGENT_TIMEOUT_MS`. Long production workflows should size all three explicitly; a main session timeout before `request_critique` is a bridge failure and cannot satisfy a reviewer-required gate.

`ZHIYUAN_INSPECT_TIME_LIMIT_SECONDS` controls Inspect's outer per-sample limit. It defaults to the bridge session timeout plus 60 seconds and must remain greater than the bridge timeout, ensuring the bridge can persist a terminal success or failure before Inspect closes the sample.

Tau2 suites use the configured Gemma endpoint directly for the independent user simulator role while the evaluated assistant runs through the RongxinAI production policy. They do not require Docker or AgentRL, but consume substantially more tokens than single-agent suites.

AgentRL suites additionally require:

```text
ZHIYUAN_AGENTRL_CONTROLLER=http://controller:5020/api
ZHIYUAN_AGENTRL_ROOT=C:\path\to\AgentRL
ZHIYUAN_AGENTRL_PYTHON=C:\path\to\python.exe
```

`ZHIYUAN_AGENTRL_PYTHON` must have the `agentrl-eval` dependencies installed. If omitted, the current Python executable is used. KG also requires `ZHIYUAN_AGENTBENCH_KG_SPARQL_URL` so the framework can check the SPARQL endpoint before launch.

## Preflight and monitoring

Before any run, the framework verifies the candidate SHA and model API. Inspect OS runs verify SSH and Docker when configured. AgentRL runs verify `/list_workers`, available capacity, `/get_indices`, and suite-specific health URLs.

`events.jsonl` contains only suite, bridge, phase state, elapsed time, numeric progress, and cleanup counts. It intentionally excludes benchmark prompts, targets, answers, and sample IDs. AgentRL subprocess output is retained in run-local diagnostic logs; the live console emits only `Samples: completed/total`, which the outer runner converts to prompt-free `phase_progress` events.

## Safety and resume behavior

The output root contains one atomic `runner.lock`, preventing a second run. A lock is recovered only when its recorded process is no longer alive. Each Inspect phase records pre-existing matching container IDs before launch; cleanup considers only IDs created afterward and skips every container with mounts. AgentRL task-worker containers are controller-owned and are never deleted by this client.

Inspect resume skips successful phases and repeats incomplete phases. AgentRL phases use a stable result store and pass it through `--resume`, so already completed samples are not rerun.

Every production-policy candidate is validated twice: once after its single-sample preflight and again immediately after its full evaluation. Missing reviewer capability, start, or completion evidence, any reviewer failure, any bridge failure, sample errors, identity mismatch, policy bypass, or missing Inspect tool evidence fails that candidate before the comparison report can be generated.

Reviewer lifecycle validation applies to every candidate by default. For an A/B test where the baseline intentionally predates reviewer support, repeat `--reviewer-required LABEL` for only the candidates expected to exercise the reviewer. The reviewer bridge capability remains active for every production candidate; this option changes only which candidate results require observed start and completion evidence.

## Coverage boundaries

The OS bridge tests the RongxinAI production policy and an isolated, in-memory, read-only reviewer session with no tools, skills, extensions, or context files. The four non-OS FC suites test Pi/model function-calling behavior through official task workers; capture mode deliberately does not claim RongxinAI production-policy coverage.

Approval UI, AskUserQuestion, MCP, multimodal transfer, live tool updates, and arbitrary non-OpenAI tool history remain unsupported. The reviewer subagent is currently a single `reviewer` role, not a general recursive subagent runtime.
