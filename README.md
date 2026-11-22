# OpenAI Agent Trace

A Codex skill for adding and auditing observable traces in existing OpenAI API agents. It keeps the original model, runner, tool policy, and evaluation behavior intact unless you choose to change them.

- Correlate model calls, actual tool execution, delivered feedback, artifacts, and evaluation.
- Tell OpenAI Responses Logs, Agents SDK Traces, and local event records apart.
- Extract returned reasoning summaries locally, validate replay payloads, and plan a summary-only span without calling a model.
- Capture private model/Python I/O and resources, connect NeMo Relay, OpenTelemetry/Phoenix and Agents SDK metadata, and read structured records in a local viewer.
- Check dependencies, collector heartbeat, export acknowledgements, stored spans, missing bodies and exact feedback links separately.

## Install In Codex

Ask `$skill-installer` to install `skills/openai-agent-trace` from `jiinuu/openai-agent-trace`. In the project you want to instrument, start with:

> Use $openai-agent-trace to make new Responses calls visible in OpenAI Platform Logs. Verify response storage and returned reasoning summaries with one non-sensitive input.

For other coding agents, start at [SKILL.md](skills/openai-agent-trace/SKILL.md).

## Verify Offline

```sh
cd skills/openai-agent-trace
python3 scripts/trace_artifacts.py response-report assets/synthetic_response.json
python3 -m unittest discover -s tests -v
```

`trace_artifacts.py` requires Python 3.10+ and makes no network requests. The complete stack uses an isolated Python 3.12 observer environment and can install packages or export allowlisted metadata when authorized. Keep real run logs, credentials, prompts, and summaries outside git.

For a complete setup, use the installed skill and its [trace-stack reference](skills/openai-agent-trace/references/trace-stack.md). The included helpers provide `init`, `install`, `doctor`, `start`, `fixture`, `audit`, `export`, and read-only `verify-response`. No model or benchmark is rerun by those commands. The fixture executes two harmless Python processes with fixed model responses.

Ask Codex:

> Use $openai-agent-trace to connect the selected collectors, capture model and tool I/O, and verify the complete trace path in the existing dashboard. Preserve the experiment settings and show every unverified or unavailable source.
