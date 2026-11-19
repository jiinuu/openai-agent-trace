---
name: openai-agent-trace
description: Instrument or audit an existing OpenAI API agent's observable execution. Use for Responses Logs, Agents SDK Traces, model and tool timelines, reasoning summaries, and replaying saved traces without rerunning a model. Do not use to recover private chain-of-thought.
---

# OpenAI Agent Trace

Make a trace that another person can verify from the original artifacts. Preserve the application's model, tools, retry behavior, and evaluator unless the user asks to change them.

## Choose the path

| Starting point | Action |
|---|---|
| Existing Responses API loop | Add checkpoints around the current call and executor. Use optional SDK spans as an observability layer; do not migrate the loop. |
| New runs in OpenAI Platform Logs | Explicitly set `store=true` on every Responses call and request `reasoning.summary=auto` for supported reasoning efforts. Verify that the stored response can be retrieved. |
| Existing Agents SDK runner | Check built-in traces and sensitive-data settings first. Add spans only for application work not already traced. |
| Saved run or missing dashboard trace | Audit local artifacts first. Reconstruct the missing trace or summary span without calling the model, tools, or evaluator again. |

Read [instrumentation](references/instrumentation.md) for implementation, [recovery](references/recovery.md) for missing traces or summary-only replay, and [evidence](references/evidence.md) for reviewing behavior or handing off a failure case.

## Instrument and verify

1. Locate the model request, tool dispatcher or subprocess, result-to-model handoff, artifact write, and evaluator. Document which are actually present. In a single-turn runner there may be no result-to-model handoff at all.
2. Start with local, correlated events. Use a stable `run_id`, a distinct `attempt_id` and `call_id`, timestamps, source, status, and available provider IDs. Capture offered tools, model requests, real execution results, and feedback sent back as separate observations. Keep final evaluation outside model-visible feedback.
3. Apply the Platform Logs settings at the actual API call site for every harness in scope. Preserve existing storage settings for local-only or offline workflows. OpenAI exposes official reasoning summaries and token counts. Internal chain-of-thought is unavailable. Record whether the response actually contained a summary.
4. Validate settings with a fixture. For Platform Logs, retrieve one stored response by its provider ID in the same project and check its returned summary. For remote span export, start with a synthetic trace that makes no model call. Treat API retrieval, exporter acknowledgement, and dashboard rendering as separate evidence.
5. Keep credentials, prompts, tool arguments/results, code, stdout/stderr, gold answers, and summary text out of remote spans by default. Never send raw artifacts merely to diagnose an upload. Do not add a production retry or guardrail to make a trace look successful.

## Offline tools

Use `scripts/trace_artifacts.py` with saved JSON. It has no network client and never runs a model, tool, or evaluator.

- `response-report RESPONSE.json`: allowlisted metadata, usage, output item types, and summary availability. `--summary-out FILE` explicitly writes a separate local summary when returned.
- `check-payload PAYLOAD.json`: shape and ID checks for a single recorded trace. Use `--existing-trace-id` for a rootless attachment to an existing trace.
- `plan-summary-span SUMMARY.json ...`: bind one saved response summary to an existing trace and create a local payload. Text is excluded unless explicitly requested.

## Report the outcome

Separate **observed**, **inferred**, **not recorded**, and **not applicable**. A model requesting a tool, a runner launching Python, and a library appearing in code are different evidence. Separate execution completion, valid submission, final task result, and total cost. Count usage by actual call ID, not by the number of rows or spans containing the same usage.
