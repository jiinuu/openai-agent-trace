# OpenAI Agent Trace

A Codex skill for adding and auditing observable traces in existing OpenAI API agents. It keeps the original model, runner, tool policy, and evaluation behavior intact unless you choose to change them.

- Correlate model calls, actual tool execution, delivered feedback, artifacts, and evaluation.
- Tell OpenAI Responses Logs, Agents SDK Traces, and local event records apart.
- Extract returned reasoning summaries locally, validate replay payloads, and plan a summary-only span without calling a model.

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

The bundled tools require Python 3.10+ and do not make network requests. Remote export is a separate integration decision. Keep real run logs, credentials, prompts, and summaries outside git.
