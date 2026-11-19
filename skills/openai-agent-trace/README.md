# OpenAI Agent Trace

A reusable Codex skill for adding or auditing observability in existing OpenAI API agents. It covers local events, Responses Logs, Agents SDK Traces, reasoning summaries, no-model replay, and failure-case handoffs. It does not replace the runner or change the official experiment protocol.

## Use

In Codex, ask `$skill-installer` to install `skills/openai-agent-trace` from `jiinuu/openai-agent-trace`. After installation, invoke `$openai-agent-trace` in the target project and say whether you want local-only logging or an authorized dashboard check. Other coding agents can read [SKILL.md](SKILL.md) as a runbook. Installing the skill does not automatically capture unrelated API requests; it guides integration at the target agent's execution boundaries.

Example request:

> Use $openai-agent-trace to make new Responses calls visible in OpenAI Platform Logs. Store every response and request the official reasoning summary for supported efforts.

## Offline quick start

Run from this skill directory with Python 3.10 or newer. No dependencies, credentials, or network are needed.

```sh
python3 scripts/trace_artifacts.py response-report assets/synthetic_response.json
python3 -m unittest discover -s tests -v
```

- [Add instrumentation](references/instrumentation.md)
- [Troubleshoot Logs, Traces, or replay](references/recovery.md)
- [Read a run and hand off a failure case](references/evidence.md)

Do not commit generated trace files or summary text. The included response fixture is synthetic.
