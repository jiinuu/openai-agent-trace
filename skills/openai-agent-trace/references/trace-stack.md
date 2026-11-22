# Complete, verified trace stack

Use this reference when setting up a working stack or recovering missing collectors. The portable helpers ship with the skill. The existing agent keeps control of model selection, tool execution, retries, budgets and evaluation.

## Scope and completion gates

Before editing, identify the user-requested sources and the current run directory. Defaults for an OpenAI agent that requests the complete stack are Responses storage/official summaries, HTTP attempts and optional SSE, local Python/tool I/O and resources, Relay ATOF, Agents SDK metadata, OTLP/Phoenix, and the existing harness's feedback/artifact/evaluation records. Other services are optional destinations, not silently enabled substitutes.

Discover and reuse an existing collector configuration, startup script and dashboard before initializing another stack. Run the same evidence gates against those services. Do not start a duplicate collector over an active experiment; the bundled profile is for a missing stack or isolated validation.

Record each source as **not configured**, **blocked**, **running**, **captured**, **acknowledged**, **read back**, or **rendered**. An empty summary and a never-requested native tool are not collector failures. Label fixed model responses and archive projections. Missing old observations stay missing.

Completion requires the applicable gates below. Save the evidence locally without credential values.

| Gate | Required evidence |
| --- | --- |
| Environment | Correct interpreter, module availability, writable private output, loopback ports; no changes to the benchmark dependencies. |
| Lifecycle | A distinct run/attempt/call/event identity, paired observed spans, preserved original timestamps and IDs, no conflicting duplicates or failed writes. |
| API | The real outbound request has `store=true`, supported summary setting and intended mode; existing response retrieval confirms matching ID/storage. SSE requires real streaming. |
| Tool and feedback | Actual execution start/end, exact supplied code/arguments and stdout/stderr/result, error origin and hash/pointer of the feedback in the actual next request, followed by that response. An SDK tool request alone proves no execution. |
| Collectors | Relay start/end/parent verification; OTLP ACK and Phoenix read-back; Agents SDK HTTP ACK. Inspect the same OpenAI project in the hosted UI separately. |
| Viewer | Select one run; follow one failure through the next request and repair; compare displayed text to saved sidecars; test raw fallback, empty output, long logs, and keyboard closing. |
| Delivery | Preserve prior run artifacts, save the coverage report/receipts/heartbeat, verify the actual hosted build if deployment was requested, and provide the restart command. |

Do not collapse these gates into “all on.” If an external account or credential is absent, finish the available work and name exactly what is blocked. Do not obtain or disclose credentials through logs or chat. Use the existing credential editor/secret flow. An exporter ACK does not prove hosted UI visibility.

## Bootstrap and operate

Use absolute paths for the skill script, workspace, configuration and interpreters in generated commands. The examples below use readable placeholders; resolve them before running.

```text
<python> <skill>/scripts/trace_stack.py init --workspace <project> --config <project>/.agent-trace/config.json --allow-openai-metadata
<python> <skill>/scripts/trace_stack.py install --config <project>/.agent-trace/config.json
<python> <skill>/scripts/trace_stack.py start --config <project>/.agent-trace/config.json
<python> <skill>/scripts/trace_stack.py doctor --config <project>/.agent-trace/config.json
<python> <skill>/scripts/trace_stack.py fixture --config <project>/.agent-trace/config.json
```

Only set `--allow-openai-metadata` when the user requested/authorized that destination. `--env-file <existing .env>` reads the existing key without printing it; `--openai-project <confirmed project ID>` scopes export and retrieval. Never put the key itself in arguments or configuration. If the proven observer environment already exists, use `init --python <observer-python> --deps <observer-dependency-directory>` and skip installation. The observer package set was tested with Python 3.12 on Windows; the offline artifact helper still supports Python 3.10+.

`install` creates a private isolated venv and does not upgrade the benchmark environment. Commands then execute in the configured observer interpreter. `start` reuses healthy matching local services, starts Phoenix and the collector/viewer in hidden processes on Windows, and writes PID/heartbeat. A server on an unrelated port is not evidence that this profile is running. Pick distinct available ports with `--phoenix-port` and `--viewer-port`; do not terminate an unrelated process. Re-run the same `start` command after shutdown. Do not create an OS startup task unless requested.

The profile defaults to loopback Phoenix HTTP on 6006, a distinct Phoenix gRPC port on 6007, and viewer on 4186. Set `--phoenix-grpc-port` when that port is occupied. Phoenix's otherwise-default gRPC 4317 can collide with an already-running server even when the HTTP ports differ. Startup waits are bounded and a Phoenix startup failure blocks completion. An updated watcher is restarted only after both its profile and process command line match; unrelated processes are preserved.

The private `.agent-trace/` directory is added to the project's ignore file. Output remains outside the skill's installation directory. Watch only the configured journal directory. Every experiment attempt has its own directory and journal.

The fixture records fixed model outputs and executes two harmless Python processes, one failing and one succeeding. It creates no model request. The watcher exports only ID/status/timing metadata for completed journals, retaining private bodies locally. Use `export --journal <events.jsonl>` for an explicit offline projection; preserve all original files. Do not present a projection as historical live instrumentation.

## Instrument the current loop

Import the runtime from the installed skill's `scripts` directory using the project's existing dependency/module convention. Keep the existing API parameters, model, effort/mode, dispatch, limits and evaluation. Attach HTTP hooks at the existing `OpenAI(...)` client, rather than creating a second client call.

```python
from pathlib import Path
from trace_runtime import TraceLog, call_responses, observed_client_options

trace = TraceLog(Path(private_run_dir) / "events.jsonl", run_id, attempt_id, harness_name)
client = OpenAI(**existing_client_options, **observed_client_options(True))
response, raw_http = call_responses(
    client, trace, call_id,
    stream=trace_profile["streaming"],
    **existing_response_parameters,
)
```

`call_responses` makes the original call once. By default it sets `store=true` and requests `summary=auto` when a reasoning configuration exists and the effort is not `none`. It preserves effort/mode and existing headers. For an explicitly local-only workflow pass `platform_logs=False`. Check model support first; do not switch models, downgrade effort, retry an invalid prompt, or remove storage merely to make the test pass. Preserve the existing streaming choice for a fixed benchmark; enable SSE when preparing a new tracing protocol. There is no internal-CoT access.

The adapter records request settings, the complete header-free JSON input, response body, IDs, usage, official summary count, and stream receipts when streaming. HTTP hooks record actual transport attempts and a private header-free wire body. Full input/output are private sidecars; credential-looking strings are redacted. A body serialization/write failure appears in collector health instead of being hidden as a successful capture. The original model exception remains the original exception.

Wrap the executor where it actually runs, not where a model mentions code:

```python
with trace.span("python_execution", call_id=call_id) as (operation_id, outcome):
    # Run the existing executor exactly once here.
    trace.execution_io(operation_id, code=actual_code, stdout=actual_stdout, stderr=actual_stderr)
    outcome.update(status="passed" if succeeded else "failed", process_returncode=actual_returncode)
```

Use `trace.monitor(actual_pid, worker_id, call_id=call_id, operation_id=operation_id)` around the existing process lifetime for psutil samples. In-process notebooks need their own real cell-execution boundary; do not label the editor's cell contents as executed. Record native function call IDs separately from application Python execution. Add artifact hashes and evaluator spans at existing boundaries; never feed gold labels or final evaluation back into the model-visible error loop.

For feedback, record the failed event/operation ID and the actual next call ID, JSON pointer and hash of the included diagnostic. Verify the pointer against the saved wire input and correlate the next response ID. “Appended to history,” “HTTP sent,” “response received,” and “referenced by the model” are distinct observations. Call `trace.finish(actual_status)` after the attempt, including failures, so open spans and failed writes remain visible.

## Verify and diagnose

```text
<python> <skill>/scripts/trace_stack.py audit --config <config> --journal <saved events.jsonl>
<python> <skill>/scripts/trace_stack.py verify-response --config <config> --response-id <existing resp_ID>
```

The read-only verification retrieves one existing response; it generates no model response. It saves matching ID, storage and summary count separately from SDK exporter receipts. Some accepted summary requests return zero fragments. Do not call models repeatedly to force a summary to appear.

If a collector fails, inspect the allowlisted error type and local stderr, correct the specific dependency/endpoint/authentication issue, and replay the saved metadata. Do not rerun the experiment or overwrite an earlier failure. On Windows, restricted process views can hide a live process; verify the recorded PID in the real host context before restarting. Atomic state replacement retries transient Windows/OneDrive locks. The capture module and observer process are separate, so telemetry transport cannot become a hidden model retry.

Use the local viewer to check structured fields and original bodies. The bundled viewer binds to loopback, rejects foreign origins/hosts, exposes only registered run records and never serves arbitrary files. It uses text nodes for model content and 60,000-character pages for long text. UI references alone do not implement Langfuse/LangSmith; add those destinations only after authenticating and proving receipt.

For the existing React dashboard, reuse its current palette/layout and the proven request/output presentation pattern: scalar facts; goal and description as readable text; dedicated code/stdout/stderr; previous cells/task cards behind details; exact private raw fallback. Keep cost attribution by actual provider response ID, separate cache writes/reads, and mark missing usage unknown. Verify pricing and a dated FX source before showing a KRW estimate.

## Sharing

Installing this skill is not permission to publish private bodies. Keep the public build metadata-only. If private sharing is requested, preserve the approved recipients and existing authentication. Use the same protected/encrypted per-run path when present, exclude credentials/test answers/local paths, verify anonymous access is denied, and confirm the new deployment is READY. Do not create an unrestricted authentication-bypass link. Never turn an old user's address, project ID, deployment host, or local Windows path into a skill default.

## Primary references

- [Responses storage and retrieve](https://developers.openai.com/api/reference/resources/responses/methods/retrieve)
- [Agents SDK tracing](https://openai.github.io/openai-agents-python/tracing/)
- [NVIDIA NeMo Relay](https://docs.nvidia.com/nemo/relay/latest/about-nemo-relay/overview)
- [OpenTelemetry Python exporters](https://opentelemetry.io/docs/languages/python/exporters/)
- [Phoenix tracing](https://arize.com/docs/phoenix/tracing)
