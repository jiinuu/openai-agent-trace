---
name: openai-agent-trace
description: Instrument and verify an existing OpenAI agent's observable execution, including Responses Logs, local model/tool I/O, NeMo Relay, Agents SDK, and OpenTelemetry/Phoenix. Use for a working trace stack, missing logs, readable trace viewers, or replaying saved records. Does not recover private chain-of-thought.
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

For a complete tracing setup or a request to make all selected tools work together, read [trace-stack](references/trace-stack.md). It contains the bundled capture adapter, isolated dependency setup, hidden background services, metadata fan-out, readable viewer, and completion gates. Preserve an existing dashboard and adapt its data binding; the bundled local viewer is a fallback.

## Instrument and verify

1. Locate the model request, tool dispatcher or subprocess, result-to-model handoff, artifact write, and evaluator. Document which are actually present. In a single-turn runner there may be no result-to-model handoff at all.
2. Start with local, correlated events. Use a stable `run_id`, a distinct `attempt_id` and `call_id`, timestamps, source, status, and available provider IDs. Capture offered tools, model requests, real execution results, and feedback sent back as separate observations. Keep final evaluation outside model-visible feedback.
3. Apply the Platform Logs settings at the actual API call site for every harness in scope. Preserve existing storage settings for local-only or offline workflows. OpenAI exposes official reasoning summaries and token counts. Internal chain-of-thought is unavailable. Record whether the response actually contained a summary.
4. Validate settings with a fixture. For Platform Logs, retrieve one stored response by its provider ID in the same project and check its returned summary. For remote span export, start with a synthetic trace that makes no model call. Treat API retrieval, exporter acknowledgement, and dashboard rendering as separate evidence.
5. Keep credentials, prompts, tool arguments/results, code, stdout/stderr, gold answers, and summary text out of remote spans by default. Never send raw artifacts merely to diagnose an upload. Do not add a production retry or guardrail to make a trace look successful.
6. Distinguish installed, configured, running, captured, acknowledged, stored/read back, and rendered. A tool is not enabled merely because it appears on a diagram. Record the required sources and show any blocked, missing, partial, or not-applicable source. Do not declare the full setup complete while a required gate remains unchecked.

## Complete stack

Use the actual agent's interpreter and entry points for instrumentation, and an isolated observer interpreter for Relay/Phoenix/SDK exporters. `scripts/trace_runtime.py` supplies local capture and a single-call Responses adapter. `scripts/trace_stack.py` supplies `init`, `install`, `doctor`, `start`, `fixture`, `audit`, `export`, and `verify-response`. Run `--help` and follow the reference before using them. Installation and export make network requests; the existing `trace_artifacts.py` commands remain entirely offline.

- Start with a synthetic model response and real harmless Python failure/recovery; this does not call a model. Remote metadata export needs the user's authorization and `--allow-openai-metadata` in the profile.
- Verify Relay boundaries/parents, OTLP acknowledgement and Phoenix read-back, and SDK HTTP acknowledgement separately. OpenAI hosted UI rendering is a separate browser check. Check one existing stored response by ID instead of creating a new model request when possible.
- Watchers project completed observations. They do not intercept arbitrary uninstrumented agents. Instrument both the API and executor and prove that request bodies, responses, code, stdout/stderr, and result-to-next-request links are actually present.
- Preserve the run/attempt/call/event hierarchy. Keep HTTP attempts and SSE receipts separate from model calls. Empty official summaries, absent native tool calls, and unavailable historical SSE are explicit states; never invent them.
- Use semantic fields, role labels, code and stdout/stderr panels, and details on demand. Keep exact raw records available privately and paginate long logs without truncating them. Preserve original-language source text.
- Save exporter receipts and collector heartbeat, provide an idempotent restart command, and check liveness in the real host context before restarting a process. Do not launch a second experiment to repair tracing.
- Treat LangSmith/Langfuse and other optional destinations as configured only after valid destination-specific credentials and a test receipt exist. Their use as a UI reference does not activate them.

## Offline tools

Use `scripts/trace_artifacts.py` with saved JSON. It has no network client and never runs a model, tool, or evaluator.

- `response-report RESPONSE.json`: allowlisted metadata, usage, output item types, and summary availability. `--summary-out FILE` explicitly writes a separate local summary when returned.
- `check-payload PAYLOAD.json`: shape and ID checks for a single recorded trace. Use `--existing-trace-id` for a rootless attachment to an existing trace.
- `plan-summary-span SUMMARY.json ...`: bind one saved response summary to an existing trace and create a local payload. Text is excluded unless explicitly requested.

## Report the outcome

Separate **observed**, **inferred**, **not recorded**, and **not applicable**. A model requesting a tool, a runner launching Python, and a library appearing in code are different evidence. Separate execution completion, valid submission, final task result, and total cost. Count usage by actual call ID, not by the number of rows or spans containing the same usage.
