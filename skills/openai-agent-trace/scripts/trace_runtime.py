"""Correlated local capture for an existing Responses API loop and executor.

No model is called by importing this module. External exporters never receive
the private sidecars. Call the existing agent exactly once through call_responses.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import hashlib
import json
import os
import re
from pathlib import Path
from threading import Event, Lock, Thread
import sys
import time
from uuid import uuid4

def json_digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def trace_text(text):
    return {"text": text, "chars": len(text), "omittedAt": None}


class TraceLog:
    """One append-only file per attempt. Failed observations cannot fail a trial."""

    def __init__(self, path, run_id, attempt_id, harness, *, trial_id=None):
        self.path = Path(path) if path is not None else None
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.run_id = run_id
        self.attempt_id = attempt_id
        self.trial_id = trial_id
        self.harness = harness
        self.failed_writes = 0
        self.lock = Lock() if self.path is not None else None
        if self.path is not None and (not self.path.exists() or self.path.stat().st_size == 0):
            self.emit("run_start", status="started")

    @classmethod
    def for_trial(cls, enabled, logs, trial_id, harness):
        run_id = os.getenv("AGENT_TRACE_RUN_ID", trial_id)
        attempt_id = os.getenv("AGENT_TRACE_ATTEMPT_ID", trial_id)
        return cls(logs / "live-trace.jsonl" if enabled else None, run_id, attempt_id, harness,
                   trial_id=os.getenv("AGENT_TRACE_TRIAL_ID") or (trial_id if attempt_id == trial_id else None))

    def emit(self, kind, *, call_id=None, **details):
        if self.path is None:
            return None
        event_id = uuid4().hex
        with self.lock:
            row = {
                "schema": 1, "event_id": event_id, "kind": kind,
                "ts": datetime.now(timezone.utc).isoformat(), "monotonic_ns": time.monotonic_ns(),
                "run_id": self.run_id, "trial_id": self.trial_id or details.get("job_id"),
                "attempt_id": self.attempt_id, "harness": self.harness,
                "call_id": call_id, **details,
            }
            if not self._append(self.path, row):
                return None
        return event_id

    def _append(self, path, row):
        try:
            with path.open("a", encoding="utf-8") as file:
                serialized = json.dumps(row, ensure_ascii=False, allow_nan=False)
                serialized = re.sub(r"\b(?:sk-(?:proj-|svcacct-|admin-)?|ghp_|github_pat_|vcp_)[\w-]{16,}", "[REDACTED_CREDENTIAL]", serialized)
                file.write(serialized + "\n")
            return True
        except (OSError, TypeError, ValueError) as error:
            self.failed_writes += 1
            if self.failed_writes == 1:
                print(f"Trace collector failed: {self.attempt_id} · {type(error).__name__}",
                      file=sys.stderr, flush=True)
            return False

    def body(self, event_id, **parts):
        if self.path is None or not event_id:
            return
        with self.lock:
            self._append(self.path.with_name("bodies.jsonl"), {"event_id": event_id,
                "parts": {key: trace_text(value) for key, value in parts.items()}})

    def json_body(self, event_id, **parts):
        def serialized(value):
            def convert(obj):
                if hasattr(obj, "model_dump"):
                    return obj.model_dump(mode="json")
                raise TypeError("Unrecordable body type")
            return json.dumps(value, ensure_ascii=False, default=convert)
        try:
            self.body(event_id, **{key: serialized(value) for key, value in parts.items()})
        except Exception as error:
            self.failed_writes += 1
            self.emit("collector_health", component="body_serialization", status="degraded",
                      error_type=type(error).__name__, failed_writes=self.failed_writes)

    def execution_io(self, operation_id, **parts):
        if self.path is None or not operation_id:
            return
        # Private sidecar. Never include the contents in a span or public snapshot.
        fields = {key: trace_text(value) for key, value in parts.items()}
        with self.lock:
            self._append(self.path.with_name("execution-io.jsonl"),
                         {"operation_id": operation_id, "parts": fields})

    def request_body(self, call_id, transport_attempt_id, body):
        # Persist the JSON body alone. HTTP headers stay outside this private record.
        with self.lock:
            self._append(self.path.with_name("request-bodies.jsonl"), {
                "attempt_id": self.attempt_id, "call_id": call_id,
                "transport_attempt_id": transport_attempt_id, "request_body": body,
            })

    @contextmanager
    def span(self, operation, *, call_id=None, **details):
        span_id = self.emit("span_start", operation=operation, call_id=call_id, **details)
        outcome = {"status": "ok"}
        try:
            yield span_id, outcome
        except BaseException as error:
            outcome.update(status="error", error_type=type(error).__name__)
            raise
        finally:
            self.emit("span_end", span_id=span_id, operation=operation, call_id=call_id, **outcome)

    def finish(self, status, **details):
        self.emit("termination", status=status, **details)
        self.emit("collector_health", failed_writes=self.failed_writes,
                  status="degraded" if self.failed_writes else "ok")

    @contextmanager
    def monitor(self, pid, worker_id, *, call_id=None, operation_id=None):
        if self.path is None:
            yield
            return
        try:
            import psutil
        except ImportError:
            self.emit("collector_health", component="psutil", status="unavailable")
            yield
            return
        stop = Event()

        def sample():
            try:
                process = psutil.Process(pid)
                with process.oneshot():
                    cpu = process.cpu_times()
                    rss = process.memory_info().rss
                    try:
                        io = process.io_counters()
                    except (AttributeError, psutil.AccessDenied, NotImplementedError):
                        io = None
                self.emit("resource_sample", call_id=call_id, worker_id=worker_id,
                          operation_id=operation_id,
                          cpu_seconds=cpu.user + cpu.system, rss_bytes=rss,
                          read_bytes=io.read_bytes if io else None, write_bytes=io.write_bytes if io else None)
                return True
            except (psutil.NoSuchProcess, psutil.ZombieProcess):
                return False
            except (psutil.Error, OSError) as error:
                self.emit("collector_health", component="psutil", status="degraded",
                          error_type=type(error).__name__)
                return False

        def poll():
            while not stop.wait(1):
                if not sample():
                    break

        active = sample()
        thread = Thread(target=poll, daemon=True) if active else None
        if thread:
            thread.start()
        try:
            yield
        finally:
            stop.set()
            if thread:
                thread.join(timeout=0.2)
                sample()


def snapshot_artifact(path: Path, workspace: Path):
    """Observe the output contract at execution boundaries."""
    try:
        if not path.resolve().is_relative_to(workspace.resolve()):
            return {"status": "outside_workspace"}
        if not path.is_file():
            return {"status": "missing"}
        with path.open("rb") as file:
            return {"status": "present", "sha256": hashlib.file_digest(file, "sha256").hexdigest()}
    except (OSError, RuntimeError):
        return {"status": "unreadable"}


def record_artifact(trace, path, workspace, before, *, call_id=None, operation_id=None):
    if trace.path is None:
        return
    after = snapshot_artifact(path, workspace)
    if before != after:
        trace.emit("artifact_lineage", call_id=call_id, operation_id=operation_id,
                   artifact="output_file", before=before, after=after)


_active_call = ContextVar("agent_trace_http_call", default=None)


class HttpCall:
    def __init__(self, trace, call_id):
        self.trace = trace
        self.call_id = call_id
        self.client_id = f"agt-{uuid4().hex}"
        self.pending = None
        self.request_id = None

    def request_started(self, request):
        if self.pending is not None:
            self.end_without_headers("retried_without_headers")
        self.pending = uuid4().hex
        try:
            payload = request.content.decode("utf-8")
            input_hash = json_digest(json.loads(payload).get("input"))
        except (ValueError, KeyError, TypeError):
            payload = None
            input_hash = None
        body_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest() if payload is not None else None
        recorded = self.trace.emit("transport_attempt", call_id=self.call_id, status="started",
                                   transport_attempt_id=self.pending, client_request_id=self.client_id,
                                   input_sha256=input_hash, body_sha256=body_hash)
        if recorded and payload is not None:
            self.trace.request_body(self.call_id, self.pending, payload)

    def response_headers(self, response):
        self.request_id = response.headers.get("x-request-id")
        self.trace.emit("transport_attempt", call_id=self.call_id, status="headers_received",
                        transport_attempt_id=self.pending, client_request_id=self.client_id,
                        http_status=response.status_code, request_id=self.request_id)
        self.pending = None

    def end_without_headers(self, status, error_type=None):
        if self.pending is not None:
            self.trace.emit("transport_attempt", call_id=self.call_id, status=status,
                            transport_attempt_id=self.pending, client_request_id=self.client_id,
                            error_type=error_type)
            self.pending = None


def trace_http_hooks():
    def request_started(request):
        call = _active_call.get()
        if call is not None:
            call.request_started(request)

    def response_headers(response):
        call = _active_call.get()
        if call is not None:
            call.response_headers(response)

    return {"request": [request_started], "response": [response_headers]}


def observed_client_options(enabled):
    if enabled:
        from openai import DefaultHttpxClient

        return {"http_client": DefaultHttpxClient(event_hooks=trace_http_hooks())}
    return {}


STREAM_TERMINALS = {"response.completed", "response.incomplete", "response.failed"}


def observe_stream(client, trace, call_id, params):
    terminal = None
    terminal_type = None
    count = 0
    try:
        with client.responses.create(**params, stream=True) as stream:
            for event in stream:
                count += 1
                observed_response = getattr(event, "response", None)
                trace.emit("stream_receipt", call_id=call_id, sequence_number=getattr(event, "sequence_number", None),
                           event_type=event.type, response_id=getattr(observed_response, "id", None),
                           item_id=getattr(event, "item_id", None))
                if event.type in STREAM_TERMINALS:
                    terminal = observed_response
                    terminal_type = event.type
                    break
            http_response = stream.response
    finally:
        trace.emit("stream_end", call_id=call_id,
                   status=terminal_type or "terminal_unobserved", received=count)
    if terminal is None:
        raise RuntimeError("Responses SSE ended without a terminal Response")
    return terminal, http_response


def response_fields(response):
    output = getattr(response, "output", None) or []
    usage = getattr(response, "usage", None)
    summaries = sum(
        part.type == "summary_text" and bool(part.text.strip())
        for item in output if item.type == "reasoning" for part in item.summary or []
    )
    return {
        "response_id": response.id, "status": response.status, "store": getattr(response, "store", None),
        "output_types": [item.type for item in output], "summary_fragments": summaries,
        "returned_tool_count": sum(item.type.endswith("_call") for item in output),
        "input_tokens": getattr(usage, "input_tokens", None),
        "output_tokens": getattr(usage, "output_tokens", None),
        "cached_input_tokens": getattr(getattr(usage, "input_tokens_details", None), "cached_tokens", None),
        "reasoning_tokens": getattr(getattr(usage, "output_tokens_details", None), "reasoning_tokens", None),
    }


def call_responses(client, trace, call_id, *, raw=False, stream=False, platform_logs=True, **params):
    if platform_logs:
        params = {**params, "store": True}
        reasoning = params.get("reasoning")
        if isinstance(reasoning, dict) and reasoning.get("effort") != "none":
            params["reasoning"] = {**reasoning, "summary": reasoning.get("summary") or "auto"}
    if trace.path is None:
        if raw:
            http_response = client.responses.with_raw_response.create(**params)
            return http_response.parse(), http_response
        return client.responses.create(**params), None

    call = HttpCall(trace, call_id)
    request_event = trace.emit("request_input", call_id=call_id, client_request_id=call.client_id,
               input_sha256=json_digest(params.get("input")), store=params.get("store"),
               model=params.get("model"), effort=(params.get("reasoning") or {}).get("effort"),
               reasoning_mode=(params.get("reasoning") or {}).get("mode"),
               requested_summary=(params.get("reasoning") or {}).get("summary"), streaming=stream,
               offered_tool_count=len(params.get("tools") or []))
    recorded = {key: value for key, value in params.items() if key not in {"extra_headers", "api_key"}}
    trace.json_body(request_event, input=recorded)
    token = _active_call.set(call)
    request_params = {**params, "extra_headers": {**(params.get("extra_headers") or {}), "X-Client-Request-Id": call.client_id}}
    try:
        with trace.span("model_call", call_id=call_id) as (_, outcome):
            if stream:
                response, http_response = observe_stream(client, trace, call_id, request_params)
            elif raw:
                http_response = client.responses.with_raw_response.create(**request_params)
                response = http_response.parse()
            else:
                response = client.responses.create(**request_params)
                http_response = None
            fields = response_fields(response)
            request_id = getattr(response, "_request_id", None) or call.request_id
            response_event = trace.emit("model_response", call_id=call_id, request_id=request_id, **fields)
            trace.json_body(response_event, output=response)
            outcome.update(status=response.status, response_id=response.id)
            return response, http_response
    except Exception as error:
        call.end_without_headers("headers_unobserved", type(error).__name__)
        trace.emit("model_error", call_id=call_id, error_type=type(error).__name__,
                   http_status=getattr(error, "status_code", None),
                   request_id=getattr(error, "request_id", None) or call.request_id)
        raise
    finally:
        _active_call.reset(token)
