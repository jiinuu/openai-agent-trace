"""Metadata-only projections. No model, tool or evaluator is executed here."""
from __future__ import annotations
from collections import defaultdict
from datetime import datetime
import re
import hashlib
import json
import time
from urllib.parse import quote, urlsplit

SPAN_KINDS = {"model_call": "LLM", "python_execution": "TOOL", "evaluation": "CHAIN",
              "public_validation": "CHAIN", "validation": "CHAIN", "tool_execution": "TOOL"}
SAFE_FIELDS = {"event_id", "source_line", "run_id", "attempt_id", "call_id", "harness", "worker_id",
               "status", "request_id", "response_id", "transport_attempt_id", "result_id",
               "operation_id", "reasoning_mode", "effort", "http_status", "schema_passed", "stage", "next_state",
               "execution_mode", "elapsed_ms", "timed_out", "code_chars", "stdout_chars", "stderr_chars",
               "process_returncode", "failure_kind"}
MARK_KINDS = {"attachment", "checkpoint", "transport_attempt", "stream_end", "worker_lifecycle",
              "artifact_lineage", "submission", "state_transition", "collector_health"}


def metadata(row):
    def identifier(value):
        # Archived event IDs are relative, slash-separated identifiers, never file paths.
        return isinstance(value, str) and len(value) <= 256 and not re.match(r"^[A-Za-z]:", value) and bool(
            re.fullmatch(r"[A-Za-z0-9_.:-]+(?:/[A-Za-z0-9_.:-]+)*", value))
    return {
        f"agent.{key}": value for key, value in row.items() if key in SAFE_FIELDS and
        (type(value) in (bool, int) or identifier(value))
    }


def trace_parts(events):
    rows = list(events.values())
    ends = {row["span_id"]: row for row in rows if row["kind"] == "span_end"}
    stages = [(row, ends[row["event_id"]]) for row in rows if row["kind"] == "span_start"
              and row["event_id"] in ends and row.get("operation") in SPAN_KINDS]
    termination = next((row for row in reversed(rows) if row["kind"] == "termination"), None)
    end = {**rows[-1], "status": termination["status"] if termination else "unobserved"}
    return rows[0], end, stages


def timestamp(row):
    return datetime.fromisoformat(row["ts"])


def nanos(row):
    return int(timestamp(row).timestamp() * 1_000_000) * 1000


def scope_attrs(begin, end):
    fields = ("response_id", "schema_passed", "status", "execution_mode", "elapsed_ms", "timed_out",
              "code_chars", "stdout_chars", "stderr_chars", "process_returncode", "failure_kind")
    measured = metadata({key: end[key] for key in fields if key in end})
    attributes = {**metadata(begin), **measured, "agent.end_event_id": end["event_id"],
            "agent.end_source_line": end["source_line"],
            "agent.status": measured.get("agent.status", "unobserved")}
    if begin.get("operation") in {"python_execution", "tool_execution"}:
        attributes.update({"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": begin["operation"]})
    return attributes


async def project_relay(traces, folder):
    import nemo_relay as relay

    folder.mkdir(exist_ok=False)
    configuration = {
        "version": 1,
        "components": [{"kind": "observability", "enabled": True, "config": {
            "version": 4, "enable_full_payloads": False, "opentelemetry": {"enabled": False},
            "atof": {"enabled": True, "sinks": [{
                "type": "file", "output_directory": str(folder), "filename": "atof.jsonl", "mode": "overwrite",
            }]},
        }}],
    }
    isolated_config = folder / "plugins.toml"
    isolated_config.write_text("version = 1\n", encoding="utf-8")
    stage_types = {"model_call": relay.ScopeType.Llm, "python_execution": relay.ScopeType.Tool,
                   "evaluation": relay.ScopeType.Evaluator, "public_validation": relay.ScopeType.Guardrail, "validation": relay.ScopeType.Guardrail,
                   "tool_execution": relay.ScopeType.Tool}
    async with relay.plugin.activate(configuration, isolated_config):
        for events in traces:
            first, last, stages = trace_parts(events)
            ends = {begin["event_id"]: end for begin, end in stages}
            handles = {}
            with relay.use_scope_stack(relay.create_scope_stack()):
                root = relay.scope.push("Agent execution", relay.ScopeType.Agent,
                                        timestamp=timestamp(first), metadata=scope_attrs(first, last))
                try:
                    for row in events.values():
                        if row["event_id"] in ends:
                            operation = row["operation"]
                            handles[row["event_id"]] = relay.scope.push(
                                operation, stage_types[operation],
                                metadata=scope_attrs(row, ends[row["event_id"]]), timestamp=timestamp(row),
                            )
                        elif row["kind"] == "span_end" and row["span_id"] in handles:
                            relay.scope.pop(handles.pop(row["span_id"]), timestamp=timestamp(row))
                        elif row["kind"] in MARK_KINDS:
                            relay.scope.event(row["kind"], metadata=metadata(row), timestamp=timestamp(row))
                finally:
                    relay.scope.pop(root, timestamp=timestamp(last))
        await relay.subscribers.flush_async()
    rows = [json.loads(line) for line in (folder / "atof.jsonl").read_text(encoding="utf-8").splitlines()]
    return verify_relay(traces, rows)


def verify_relay(traces, rows):
    scopes = defaultdict(set)
    roots = {}
    for row in rows:
        if row["kind"] != "scope":
            continue
        app_id = row["metadata"]["agent.event_id"]
        scopes[app_id].add((row["uuid"], row["parent_uuid"], row["scope_category"]))
        if row["name"] == "Agent execution" and row["scope_category"] == "start":
            roots[app_id] = row["uuid"]

    def pair(app_id, *, parent=None):
        observed = scopes[app_id]
        return (len(observed) == 2 and {kind for _, _, kind in observed} == {"start", "end"}
                and len({uid for uid, _, _ in observed}) == 1
                and (parent is None or all(parent == actual for _, actual, _ in observed)))

    for trace in traces:
        first, _, stages = trace_parts(trace)
        root_id = first["event_id"]
        if not pair(root_id) or not all(pair(begin["event_id"], parent=roots[root_id]) for begin, _ in stages):
            raise ValueError("ATOF scope boundaries or parent do not match the original trace")
    return {"atof_records": len(rows), "scopes_verified": len(scopes), "path": "relay/atof.jsonl"}


def read_back_phoenix(session, base_url, project, spans):
    project_url = f"{base_url}/v1/projects/{quote(project, safe='')}"
    expected = {
        (
            f"{span.context.trace_id:032x}", f"{span.context.span_id:016x}",
            f"{span.parent.span_id:016x}" if span.parent else None,
        ) for span in spans
    }
    observed = set()
    for _ in range(8):
        page, observed = {}, set()
        while True:
            params = {"limit": 100}
            if page.get("next_cursor"):
                params["cursor"] = page["next_cursor"]
            response = session.get(f"{project_url}/spans", params=params, timeout=3)
            if response.status_code == 404:
                break
            response.raise_for_status()
            page = response.json()
            observed.update((row["context"]["trace_id"], row["context"]["span_id"], row.get("parent_id"))
                            for row in page["data"])
            if not page.get("next_cursor"):
                break
        if expected <= observed:
            project_id = session.get(project_url, timeout=3).json()["data"]["id"]
            return len(expected), project_id
        time.sleep(0.35)
    return len(expected & observed), None


def project_phoenix(traces, endpoint, project):
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExportResult
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from opentelemetry.trace import Status, StatusCode, set_span_in_context
    import requests

    local = urlsplit(endpoint)
    if (local.scheme != "http" or local.hostname not in {"127.0.0.1", "::1"}
            or local.username or local.password or local.path != "/v1/traces" or local.query or local.fragment):
        raise ValueError("Expected a loopback Phoenix OTLP HTTP endpoint")
    base_url = f"{local.scheme}://{local.netloc}"
    resource = Resource({"service.name": "agent", "openinference.project.name": project})
    provider = TracerProvider(resource=resource, shutdown_on_exit=False)
    buffer = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(buffer))
    tracer = provider.get_tracer("agent.trace")
    trace_ids = []
    for events in traces:
        first, last, stages = trace_parts(events)
        root = tracer.start_span("Agent execution", start_time=nanos(first),
                                 attributes={**scope_attrs(first, last), "openinference.span.kind": "CHAIN"})
        trace_ids.append(f"{root.get_span_context().trace_id:032x}")
        context = set_span_in_context(root)
        marks = {row["source_line"]: row for row in events.values() if row["kind"] in MARK_KINDS}
        for begin, end in stages:
            step = tracer.start_span(begin["operation"], context=context, start_time=nanos(begin),
                                     attributes={**scope_attrs(begin, end),
                                                 "openinference.span.kind": SPAN_KINDS[begin["operation"]]})
            if end.get("status") in {"error", "failed", "timeout"}:
                step.set_status(Status(StatusCode.ERROR))
            for line in range(begin["source_line"] + 1, end["source_line"]):
                row = marks.pop(line, None)
                if row is not None:
                    step.add_event(row["kind"], attributes=metadata(row), timestamp=nanos(row))
            step.end(end_time=nanos(end))
        for row in marks.values():
            root.add_event(row["kind"], attributes=metadata(row), timestamp=nanos(row))
        root.end(end_time=nanos(last))

    spans = buffer.get_finished_spans()
    with requests.Session() as session:
        session.trust_env = False
        # Keep the exporter and acknowledgement independent of any global OTLP configuration.
        exporter = OTLPSpanExporter(endpoint=endpoint, timeout=4, session=session,
                                    headers={"content-type": "application/x-protobuf"})
        try:
            acknowledged = all(exporter.export(spans[start:start + 256]) == SpanExportResult.SUCCESS
                               for start in range(0, len(spans), 256))
        finally:
            exporter.shutdown()
    with requests.Session() as session:
        session.trust_env = False
        stored, project_id = read_back_phoenix(session, base_url, project, spans) if acknowledged else (0, None)
    provider.shutdown()
    return {"url": base_url, "project": project, "spans": len(spans),
            "export_acknowledged": acknowledged, "trace_ids": trace_ids,
            "spans_verified_in_phoenix": stored, "project_id": project_id}



def project_agents(traces, project, api_key):
    """Send allowlisted ID/status/timing only, with an independently checked HTTP ACK."""
    from agents.tracing import custom_span, set_trace_processors, trace
    from agents.tracing.processors import BackendSpanExporter

    set_trace_processors([])
    key = api_key
    if not key:
        return {"status": "credentials_required", "accepted": 0}
    planned, links = [], []
    for events in traces:
        first, last, stages = trace_parts(events)
        trace_id = "trace_" + hashlib.sha256((str(first["run_id"]) + "/" + str(first["attempt_id"]) + "/trace-stack-v1").encode()).hexdigest()[:32]
        synthetic = first.get("harness") == "synthetic"
        root = trace("Synthetic trace validation" if synthetic else "Agent observed execution", trace_id=trace_id, group_id=str(first["run_id"]),
                     metadata={"source": "synthetic_fixture" if synthetic else "observed_event_projection", "attempt": str(first["attempt_id"])})
        items = [root]
        for begin, end in stages:
            span = custom_span(begin["operation"], parent=root,
                               span_id="span_" + hashlib.sha256((trace_id + begin["event_id"]).encode()).hexdigest()[:24],
                               data=scope_attrs(begin, end))
            span._started_at, span._ended_at = begin["ts"], end["ts"]
            if end.get("status") in {"error", "failed", "timeout"}:
                span.set_error({"message": "Recorded execution failure", "data": {}})
            items.append(span)
        # Include a root-span boundary even for executions without completed child spans.
        boundary = custom_span("Execution boundary", parent=root, data=scope_attrs(first, last),
                               span_id="span_" + hashlib.sha256((trace_id + "/boundary").encode()).hexdigest()[:24])
        boundary._started_at, boundary._ended_at = first["ts"], last["ts"]
        items.append(boundary)
        planned.append(items)
        links.append(trace_id)
    exporter = BackendSpanExporter(api_key=key, project=project, max_retries=0)
    responses = []
    exporter._client.event_hooks["response"].append(responses.append)
    accepted, chunks, queue = 0, [], []
    for items in planned:
        if queue and sum(map(len, queue)) + len(items) > 150:
            chunks.append(queue)
            queue = []
        queue.append(items)
    if queue:
        chunks.append(queue)
    try:
        for chunk in chunks:
            responses.clear()
            exporter.export([item for items in chunk for item in items])
            if len(responses) != 1 or not 200 <= responses[0].status_code < 300:
                return {"status": "export_failed", "accepted": accepted,
                        "http_status": responses[0].status_code if responses else None}
            accepted += len(chunk)
    finally:
        exporter._client.close()
    return {"status": "acknowledged", "accepted": accepted, "trace_ids": links}
