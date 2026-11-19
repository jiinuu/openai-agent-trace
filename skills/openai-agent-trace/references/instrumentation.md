# Instrument an Existing Agent

## Choose what to record

| Surface | What it answers | Control |
|---|---|---|
| Local `events.jsonl`, diagnostics, artifacts | What the runner observed, including failures before a response arrived | Application logging; no server retention required |
| Responses in OpenAI Platform Logs | What a model request returned, if the project stores it | Responses `store` and project data controls; not enabled by SDK tracing |
| OpenAI Agents SDK Traces | How model calls, tools, execution, and evaluation relate in an instrumented run | SDK spans and exporter; independent of Responses `store` |

Preserve the existing production or benchmark configuration. In a debug CLI, use separate opt-ins such as `--response-diagnostics`, `--store-response`, `--reasoning-summary auto`, `--trace`, and `--trace-reasoning-summary`. Do not make them imply each other. A response may contain a summary even with `store=false`. `store=false` is not a promise of zero data retention.

## Responses API loop

Instrument the existing call once. Keep the requested model, instructions, tools, streaming mode, reasoning effort, and storage as they were when debugging flags are off.

- Before the call, persist requested settings and an application `call_id`. When authorized to request a summary, merge `{"summary": "auto"}` into the existing `reasoning` object rather than replacing `effort`. Verify support for the target model. Do not silently fall back to another effort or model.
- After the call, capture the provider response ID, request ID (Python SDK `response._request_id` when available), returned model, status, latency, output item types, and usage. Keep HTTP success separate from response completion. For an exception, save the observed error stage, safe error class, HTTP status and request ID when available. Missing usage is unknown, not zero.
- If selected HTTP headers are needed, use the client's raw response wrapper for the **same** request, not a second call. Keep an explicit header allowlist, for example `x-request-id` and `openai-processing-ms`. For a stream, preserve an observed terminal event or identify an interrupted stream; partial text is not a completed response.
- When raw response capture is authorized, retain it only in a private local artifact. Summaries are `output[]` items of type `reasoning`, then `summary[]` entries of type `summary_text`. Use the offline tool to write a separate, response-linked summary; do not scrape model output prose or `encrypted_content` and call it reasoning.
- In a function-calling loop, record the returned `function_call` and its `call_id` first. Record executor invocation and outcome separately. Link the actual `function_call_output` or other feedback sent in the next request. A listed tool does not imply a call, a request does not imply execution, and a tool result does not imply the model received it.

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
- [Reasoning summaries](https://developers.openai.com/api/docs/guides/reasoning#reasoning-summaries)
- [Tool calling and results](https://developers.openai.com/api/docs/guides/function-calling)
- [Agents SDK tracing, including sensitive data](https://openai.github.io/openai-agents-python/tracing/)
- [Agents SDK for JavaScript and TypeScript](https://openai.github.io/openai-agents-js/guides/tracing/)
