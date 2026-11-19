# Recover a Missing Trace

## Troubleshoot in order

1. **Local proof:** confirm the run, the response and request IDs, the trace ID, and which stages actually happened. No response ID can be normal for a failure before the model responds.
2. **Exporter check:** if authorized, run a synthetic trace containing one custom span and no model call. Use the same SDK version, credentials and project as the exporter you are debugging. Close the trace and flush it before checking the dashboard. Exporting a synthetic trace is still a network write.
3. **Check the right surface:** OpenAI Traces shows exported spans. Platform Logs depends on Responses storage and project data controls. A trace ID generated locally proves neither ingestion nor dashboard visibility. A successful exporter acknowledgement proves ingestion, not that the current UI account can see it. Do not rerun an entire batch to diagnose either surface.
4. **Compare formats for HTTP 400:** compare a known-good synthetic SDK export and the failing replay, without printing request bodies. Check top-level `data`, SDK object types `trace` and `trace.span`, root and parent references, and timezone-aware timestamps. The SDK generates `trace_` plus 32 hex digits and `span_` plus 24, not 32. Fix the mismatch before attempting one authorized retry. Stop on project, permission, or retention-policy blockers instead of bypassing them.

Example synthetic export, for use only in an authorized environment:

```python
from agents.tracing import custom_span, flush_traces, trace

with trace("Synthetic export check") as root:
    with custom_span("fixture", data={"model_called": False}):
        pass
flush_traces()
print(root.trace_id)
```

The SDK exporter is the default sending path. Use the current SDK's exporter contract rather than hardcoding an internal ingestion URL or relying on historical HTTP status codes. For a diagnostic URL prefer the one your platform or integration already provides; open [Platform Traces](https://platform.openai.com/traces) as a fallback and select the matching project.

## Replay only what is missing

- **Entire trace absent:** build one root and its observed spans from saved records, keeping the original trace ID, stable span IDs, and recorded times. Mark it as reconstructed. Omit stages that did not run. Do not use a dictionary keyed only by stage name when stages can recur. Preserve distinct attempt and call IDs.
- **Root already visible:** send only the approved missing span or spans. Do not emit a second root or rerun the workload. Verify trace-to-run and response-to-run linkage in the saved manifest, then check the payload offline.
- **No summary returned:** report it as absent. A stored response can be inspected if it already contains a summary, but a summary cannot be reconstructed from token counts or the API text answer. Requesting one on a fresh run is a distinct, model-calling debug action.

If replaying a historical run requires a lower-level export path, verify what your installed SDK can submit and how the project authenticates it. Use SDK-built payloads or the observed SDK shape, not a guessed REST contract. Stable IDs make it possible to recognize a replay; do not assume every exporter provides idempotency or automatically retry an ambiguous write. Redact fields before sending, keep the planned output local by default, and only export to the authorized destination.

To build SDK objects without sending them, use a separate process with `set_trace_processors([])` before entering the trace and span contexts. Inspect their `.export()` output offline. An SDK exporter that accepts trace/span objects does not necessarily accept raw dictionaries. For a summary-only attachment, make sure the sending path receives only the planned span, even if you used a temporary root context while building it.

## Offline commands

Run from the skill folder. Use a private directory outside git for outputs.

```sh
python3 scripts/trace_artifacts.py response-report RESPONSE.json --summary-out LOCAL_SUMMARY.json
python3 scripts/trace_artifacts.py check-payload RECONSTRUCTED_TRACE.json
python3 scripts/trace_artifacts.py plan-summary-span LOCAL_SUMMARY.json \
  --trace-id "$RECORDED_TRACE_ID" --response-id "$RECORDED_RESPONSE_ID" \
  --observed-at "$RECORDED_MODEL_EVENT_TIME" --out LOCAL_SPAN.json
python3 scripts/trace_artifacts.py check-payload LOCAL_SPAN.json --existing-trace-id "$RECORDED_TRACE_ID"
```

The planner does not discover an existing trace or upload anything. The operator supplies IDs and event time from the same run. Add `--include-summary-text` only when the summary has been reviewed and putting it in the trace is explicitly allowed. Without that flag, the planned span contains only the response ID and fragment count. Files are created without overwriting previous outputs.

The checker targets one complete trace or an attachment to an existing trace, with parents included in the payload when referenced. It detects structural mistakes; it is not a privacy scan, a complete provider schema, or proof of acceptance.
