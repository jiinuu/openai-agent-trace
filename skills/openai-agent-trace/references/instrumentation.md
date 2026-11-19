# Instrument an Existing Agent

## Choose what to record

| Surface | What it answers | Control |
|---|---|---|
| Local `events.jsonl`, diagnostics, artifacts | What the runner observed, including failures before a response arrived | Application logging; no server retention required |
| Responses in OpenAI Platform Logs | What a model request returned, if the project stores it | Responses `store` and project data controls; not enabled by SDK tracing |
| OpenAI Agents SDK Traces | How model calls, tools, execution, and evaluation relate in an instrumented run | SDK spans and exporter; independent of Responses `store` |

When the user requests Platform Logs, use one explicit setting for the workflow, such as `--platform-logs`. It enables `store=true` on every new Responses request and `reasoning.summary=auto` for supported reasoning efforts. Keep local-only diagnostics and SDK span export independently configurable. Record the setting on the new run so a stored debug run can be distinguished from a fixed benchmark protocol.

## Responses API loop

Instrument the existing call once. Keep the requested model, instructions, tools, streaming mode, and reasoning effort. Preserve the storage setting for local-only runs.

- Before the call, persist requested settings and an application `call_id`. Set `store=True` explicitly in the actual API request for Platform Logs. Merge `{"summary": "auto"}` into the existing `reasoning` object for supported reasoning efforts, preserving both `effort` and `mode`. Omit the summary setting for `effort="none"`. Verify model support and preserve the selected model and effort when a request fails.
- After the call, capture the provider response ID, request ID (Python SDK `response._request_id` when available), returned model, status, latency, output item types, and usage. Keep HTTP success separate from response completion. For an exception, save the observed error stage, safe error class, HTTP status and request ID when available. Missing usage is unknown, not zero.
- If selected HTTP headers are needed, use the client's raw response wrapper for the **same** request, not a second call. Keep an explicit header allowlist, for example `x-request-id` and `openai-processing-ms`. For a stream, preserve an observed terminal event or identify an interrupted stream; partial text is not a completed response.
- When raw response capture is authorized, retain it only in a private local artifact. Summaries are `output[]` items of type `reasoning`, then `summary[]` entries of type `summary_text`. Use the offline tool to write a separate, response-linked summary; do not scrape model output prose or `encrypted_content` and call it reasoning.
- In a function-calling loop, record the returned `function_call` and its `call_id` first. Record executor invocation and outcome separately. Link the actual `function_call_output` or other feedback sent in the next request. A listed tool does not imply a call, a request does not imply execution, and a tool result does not imply the model received it.

## Verify Platform Logs

1. Inspect the outgoing request for `store=true` and the intended reasoning fields. Verify every model call path, including repair or follow-up calls. Keep conversation and response chaining as configured by the application.
2. Use a small non-sensitive input in the intended project. Confirm that `client.responses.retrieve(response.id)` returns the same ID with `store=true`. Count only nonempty `summary_text` fragments on returned reasoning items. Some responses can have reasoning tokens and an empty summary even when the API accepted `summary=auto`. Record the observed result for each tested mode.
3. Open [Responses Logs](https://platform.openai.com/logs?api=responses) in the same project and locate the response by its ID or input. Confirm the logged output when dashboard access is available. Record API retrieval and dashboard visibility separately. Respect any project retention restrictions. A stored request cannot be created retroactively from an earlier `store=false` run.

If remote spans are wanted, the Agents SDK tracing API can wrap an existing Responses loop without using `Runner.run`. Example instrumentation for an authorized debug run:

```python
from agents.tracing import custom_span, flush_traces, trace

try:
    with trace("Agent debug run", metadata={"run_id": run_id}) as root:
        record_trace_id(root.trace_id)
        with custom_span("agent_workflow", data={"source": "runner"}):
            run_existing_workflow()
finally:
    flush_traces()
```

In the actual workflow, add separate spans around model calls, tool dispatch, the subprocess and the evaluator where those operations exist. Keep span data allowlisted; a short safe status is enough for failures. Keep logging failures visible and separate from task failures. Do not wrap a second execution around the first just to populate Traces.

## Existing Agents SDK runner

Check the SDK's generated agent, model, tool and handoff spans before adding custom spans. On Python use `RunConfig(trace_include_sensitive_data=False)` for approved traces, or `RunConfig(tracing_disabled=True)` when not approved. Preserve the rest of the caller's configuration and account for any process-level exporters. Disabling sensitive data in built-in spans does not sanitize custom span data or your own log files.

Close the trace and flush buffered exports on short-lived workers. Use the same organization/project as the trace exporter when inspecting its result. Do not replace process-global tracing processors inside shared request handlers.

The local event contract is language-independent. For JavaScript/TypeScript, apply the same mapping at the existing client, tool dispatcher and result handoff rather than porting Python-specific tracing methods.

## Official references

- [Responses create and storage](https://developers.openai.com/api/reference/resources/responses/methods/create)
- [Retrieve a stored model response](https://developers.openai.com/api/reference/resources/responses/methods/retrieve)
- [Reasoning summaries](https://developers.openai.com/api/docs/guides/reasoning#reasoning-summaries)
- [Tool calling and results](https://developers.openai.com/api/docs/guides/function-calling)
- [Agents SDK tracing, including sensitive data](https://openai.github.io/openai-agents-python/tracing/)
- [Agents SDK for JavaScript and TypeScript](https://openai.github.io/openai-agents-js/guides/tracing/)
