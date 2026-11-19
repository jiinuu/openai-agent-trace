"""Inspect saved Responses and trace payloads without network access."""

import argparse
import hashlib
import json
import os
import re
from datetime import datetime
from pathlib import Path

TRACE_ID = re.compile(r"trace_[0-9a-f]{32}")
SPAN_ID = re.compile(r"span_[0-9a-f]{24}")
RESPONSE_ID = re.compile(r"resp_[A-Za-z0-9_-]{1,180}")
METADATA_TOKEN = re.compile(r"[A-Za-z0-9_.:-]{1,200}")


class ArtifactError(ValueError):
    pass


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_private_json(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as output:
        json.dump(data, output, ensure_ascii=False, indent=2)
        output.write("\n")


def _object(value):
    return value if isinstance(value, dict) else {}


def _matches(pattern, value):
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _token(value):
    if _matches(METADATA_TOKEN, value) and not value.startswith(("sk-", "sess-")):
        return value
    return None


def _count(value):
    return value if type(value) is int and value >= 0 else None


def inspect_response(document):
    if not isinstance(document, dict):
        raise ArtifactError("Expected a Responses JSON object or a response wrapper.")
    response = document.get("response", document)
    if not isinstance(response, dict) or not _matches(RESPONSE_ID, response.get("id")):
        raise ArtifactError("Expected a saved Responses response ID.")

    output = response.get("output")
    if not isinstance(output, list) or any(not isinstance(item, dict) for item in output):
        raise ArtifactError("Expected the saved Responses output array.")

    fragments = []
    for item in output:
        if item.get("type") != "reasoning" or not isinstance(item.get("summary"), list):
            continue
        for part in map(_object, item["summary"]):
            value = part.get("text")
            if part.get("type") == "summary_text" and isinstance(value, str) and value.strip():
                fragments.append(value)

    http = _object(document.get("http_response"))
    headers = {key.lower(): value for key, value in _object(http.get("headers")).items()}
    usage = _object(response.get("usage"))
    reasoning = _object(response.get("reasoning"))
    stored = response.get("store")
    tools = response.get("tools")
    report = {
        "response_id": response["id"],
        "request_id": _token(headers.get("x-request-id") or http.get("request_id")),
        "http_status": _count(http.get("status_code")),
        "model": _token(response.get("model")),
        "status": _token(response.get("status")),
        "incomplete_reason": _token(_object(response.get("incomplete_details")).get("reason")),
        "service_tier": _token(response.get("service_tier")),
        "store": stored if type(stored) is bool else None,
        "reported_reasoning_effort": _token(reasoning.get("effort")),
        "reported_reasoning_summary": _token(reasoning.get("summary")),
        "returned_tool_count": len(tools) if isinstance(tools, list) else None,
        "output_items": [
            {
                key: value
                for key in ("type", "id", "call_id", "name", "status")
                if (value := _token(item.get(key))) is not None
            }
            for item in output
        ],
        "usage": {
            **{key: _count(usage.get(key)) for key in ("input_tokens", "output_tokens", "total_tokens")},
            "cached_input_tokens": _count(_object(usage.get("input_tokens_details")).get("cached_tokens")),
            "reasoning_tokens": _count(_object(usage.get("output_tokens_details")).get("reasoning_tokens")),
        },
        "summary_status": "returned" if fragments else "not_returned",
        "summary_fragments": len(fragments),
    }
    return report, {"response_id": response["id"], "fragments": fragments}


def _timestamp(value):
    if not isinstance(value, str):
        raise ArtifactError("Span timestamps must include a timezone.")
    iso_value = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        timestamp = datetime.fromisoformat(iso_value)
    except ValueError as error:
        raise ArtifactError("Invalid span timestamp.") from error
    if timestamp.tzinfo is None:
        raise ArtifactError("Span timestamps must include a timezone.")
    return timestamp


def check_payload(payload, existing_trace_id=None):
    """Check one complete trace or a rootless attachment, not arbitrary SDK batches."""
    items = _object(payload).get("data")
    if not isinstance(items, list) or not items or any(not isinstance(item, dict) for item in items):
        raise ArtifactError("Expected a nonempty data array of SDK trace/span objects.")
    roots = [item for item in items if item.get("object") == "trace"]
    if existing_trace_id is None:
        name = roots[0].get("workflow_name") if len(roots) == 1 else None
        if not isinstance(name, str) or not name.strip():
            raise ArtifactError("Expected one trace root with a workflow_name.")
        trace_id = roots[0].get("id")
    else:
        if roots:
            raise ArtifactError("Do not resend the root when attaching to an existing trace.")
        trace_id = existing_trace_id
    if not _matches(TRACE_ID, trace_id):
        raise ArtifactError("Expected trace_ followed by 32 lowercase hex digits.")

    parents = {}
    for item in items:
        if item.get("object") == "trace":
            continue
        span_id = item.get("id")
        parent_id = item.get("parent_id")
        if item.get("object") != "trace.span":
            raise ArtifactError("Expected a trace.span SDK object.")
        if not _matches(SPAN_ID, span_id):
            raise ArtifactError("Expected span_ followed by 24 lowercase hex digits.")
        if span_id in parents or item.get("trace_id") != trace_id:
            raise ArtifactError("Duplicate span ID or span attached to another trace.")
        if parent_id is not None and not _matches(SPAN_ID, parent_id):
            raise ArtifactError("A span parent must be another span ID or null.")
        span_type = _object(item.get("span_data")).get("type")
        if not isinstance(span_type, str) or not span_type.strip():
            raise ArtifactError("Expected a typed span_data object.")
        if _timestamp(item.get("started_at")) > _timestamp(item.get("ended_at")):
            raise ArtifactError("A span ends before it starts.")
        parents[span_id] = parent_id

    if not parents:
        raise ArtifactError("Expected at least one completed span.")
    resolved = set()
    for span_id in parents:
        chain = set()
        while span_id is not None and span_id not in resolved:
            if span_id in chain or span_id not in parents:
                raise ArtifactError("A span parent is missing or cyclic.")
            chain.add(span_id)
            span_id = parents[span_id]
        resolved.update(chain)
    return {"trace_id": trace_id, "root_count": len(roots), "span_count": len(parents)}


def plan_summary_span(summary, trace_id, response_id, observed_at, include_text=False):
    if not _matches(RESPONSE_ID, response_id) or _object(summary).get("response_id") != response_id:
        raise ArtifactError("The summary does not match the recorded response ID.")
    fragments = summary.get("fragments")
    if not isinstance(fragments, list) or not fragments or any(
        not isinstance(fragment, str) or not fragment.strip() for fragment in fragments
    ):
        raise ArtifactError("No saved reasoning summary fragments.")
    if not _matches(TRACE_ID, trace_id):
        raise ArtifactError("Expected an existing SDK trace ID.")

    data = {"response_id": response_id, "summary_fragments": len(fragments)}
    if include_text:
        data["summary"] = "\n\n".join(fragments)
    digest = hashlib.sha256(f"{trace_id}:reasoning_summary:{response_id}".encode()).hexdigest()
    payload = {"data": [{
        "object": "trace.span",
        "id": f"span_{digest[:24]}",
        "trace_id": trace_id,
        "parent_id": None,
        "started_at": observed_at,
        "ended_at": observed_at,
        "span_data": {"type": "custom", "name": "reasoning_summary", "data": data},
        "error": None,
    }]}
    check_payload(payload, existing_trace_id=trace_id)
    return payload


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    report_parser = commands.add_parser("response-report")
    report_parser.add_argument("response", type=Path)
    report_parser.add_argument("--summary-out", type=Path)
    check_parser = commands.add_parser("check-payload")
    check_parser.add_argument("payload", type=Path)
    check_parser.add_argument("--existing-trace-id")
    plan_parser = commands.add_parser("plan-summary-span")
    plan_parser.add_argument("summary", type=Path)
    plan_parser.add_argument("--trace-id", required=True)
    plan_parser.add_argument("--response-id", required=True)
    plan_parser.add_argument("--observed-at", required=True)
    plan_parser.add_argument("--out", type=Path, required=True)
    plan_parser.add_argument("--include-summary-text", action="store_true")
    args = parser.parse_args(argv)

    try:
        if args.command == "response-report":
            result, summary = inspect_response(read_json(args.response))
            if args.summary_out is not None:
                result["summary_written"] = bool(summary["fragments"])
                if result["summary_written"]:
                    write_private_json(args.summary_out, summary)
        elif args.command == "check-payload":
            result = check_payload(read_json(args.payload), args.existing_trace_id)
        else:
            payload = plan_summary_span(
                read_json(args.summary), args.trace_id, args.response_id, args.observed_at,
                include_text=args.include_summary_text,
            )
            write_private_json(args.out, payload)
            result = {
                "trace_id": args.trace_id,
                "span_id": payload["data"][0]["id"],
                "summary_text_included": args.include_summary_text,
                "uploaded": False,
            }
    except (OSError, ValueError) as error:
        parser.exit(2, f"{parser.prog}: {error}\n")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
