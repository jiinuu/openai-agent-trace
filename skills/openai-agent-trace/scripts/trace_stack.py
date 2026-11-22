"""Bootstrap, audit, export and view an observable agent run. Never calls a model.

Local capture and metadata projection are separate. Remote SDK export needs the
explicit configuration switch. Private sidecars are served on loopback only.
"""
from __future__ import annotations
import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import site
import subprocess
import sys
import threading
import time
from urllib.error import URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import build_opener, ProxyHandler
from uuid import uuid4
import venv

SKILL = Path(__file__).resolve().parents[1]
RUNTIME_FINGERPRINT = hashlib.sha256(b"".join((SKILL / "scripts" / name).read_bytes()
    for name in ("trace_stack.py", "trace_runtime.py", "trace_exporters.py"))).hexdigest()[:24]


def now():
    return datetime.now(timezone.utc).isoformat()


def load(path, default=None):
    return json.loads(Path(path).read_text(encoding="utf-8-sig")) if Path(path).is_file() else default


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + str(os.getpid()) + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for attempt in range(8):
        try:
            temp.replace(path)
            return
        except PermissionError:
            if attempt == 7:
                raise
            time.sleep(0.1 * (attempt + 1))


def dependencies(config):
    for path in config.get("dependency_paths", []):
        site.addsitedir(str(Path(path).resolve(strict=True)))


def available(name):
    try:
        return importlib.util.find_spec(name) is not None
    except (ModuleNotFoundError, ValueError):
        return False


def credentials(config):
    values = {}
    if config.get("env_file") and available("dotenv"):
        from dotenv import dotenv_values
        values.update(dotenv_values(config["env_file"]))
    values.update(os.environ)
    return values.get("OPENAI_API_KEY")


def audit(path):
    data = Path(path).read_text(encoding="utf-8")
    rows, duplicates, errors = {}, 0, []
    for number, line in enumerate(data.splitlines(), 1):
        try:
            row = json.loads(line)
        except ValueError:
            errors.append({"code": "invalid_or_partial_line", "line": number})
            continue
        event_id = row.get("event_id")
        if not isinstance(event_id, str) or not event_id:
            errors.append({"code": "missing_event_id", "line": number})
            continue
        if event_id in rows:
            if row != {k: v for k, v in rows[event_id].items() if k != "source_line"}:
                errors.append({"code": "conflicting_duplicate", "line": number})
            duplicates += 1
            continue
        rows[event_id] = {**row, "source_line": number}
    identity = {(r.get("run_id"), r.get("attempt_id")) for r in rows.values()}
    if len(identity) != 1 or any(None in pair for pair in identity):
        errors.append({"code": "mixed_or_missing_identity"})
    started = {r["event_id"] for r in rows.values() if r.get("kind") == "span_start"}
    ended = [r.get("span_id") for r in rows.values() if r.get("kind") == "span_end"]
    if set(ended) - started:
        errors.append({"code": "end_without_start"})
    if len(ended) != len(set(ended)):
        errors.append({"code": "multiple_span_ends"})
    if any(r.get("failed_writes", 0) for r in rows.values()):
        errors.append({"code": "collector_write_failure"})
    terminal = any(r.get("kind") == "termination" for r in rows.values())
    open_spans = sorted(started - set(ended))
    if terminal and open_spans:
        errors.append({"code": "terminated_with_open_spans"})
    requests = [r for r in rows.values() if r.get("kind") == "request_input"]
    responses = [r for r in rows.values() if r.get("kind") == "model_response"]
    for row in requests:
        if row.get("store") is not True:
            errors.append({"code": "response_storage_not_enabled", "event_id": row["event_id"]})
    body_file = Path(path).with_name("bodies.jsonl")
    body_ids, bodies = set(), {}
    if body_file.is_file():
        for line in body_file.read_text(encoding="utf-8").splitlines():
            try:
                body = json.loads(line)
                body_ids.add(body["event_id"])
                bodies[body["event_id"]] = body
            except (ValueError, KeyError):
                errors.append({"code": "invalid_body_record"})
    missing_bodies = [r["event_id"] for r in requests + responses if r["event_id"] not in body_ids]
    if missing_bodies:
        errors.append({"code": "missing_model_body", "count": len(missing_bodies)})
    execution_ids = {row.get("operation_id") for row in load_jsonl(Path(path).with_name("execution-io.jsonl"))
                     if all(key in row.get("parts", {}) for key in ("code", "stdout", "stderr"))}
    executions = [row for row in rows.values() if row.get("kind") == "span_start" and
                  row.get("operation") in {"python_execution", "tool_execution"} and row["event_id"] in ended]
    missing_executions = sum(row["event_id"] not in execution_ids for row in executions)
    if missing_executions:
        errors.append({"code": "missing_execution_io", "count": missing_executions})
    streams = {}
    for request in requests:
        if not request.get("streaming"):
            continue
        same_call = [r for r in rows.values() if r.get("call_id") == request.get("call_id")]
        receipts = [r for r in same_call if r.get("kind") == "stream_receipt"]
        sequences = [r["sequence_number"] for r in receipts if type(r.get("sequence_number")) is int]
        end = next((r for r in reversed(same_call) if r.get("kind") == "stream_end"), None)
        gaps = max(sequences) - min(sequences) + 1 - len(set(sequences)) if sequences else None
        duplicate_sequences = len(sequences) - len(set(sequences))
        failed = any(r.get("kind") == "model_error" for r in same_call)
        streams[request.get("call_id")] = {"receipts": len(receipts), "gaps": gaps,
            "duplicates": duplicate_sequences, "terminal": end.get("status") if end else None}
        if gaps or duplicate_sequences or not end or end.get("received") != len(receipts):
            errors.append({"code": "stream_capture_gap", "call_id": request.get("call_id")})
        elif end.get("status") == "terminal_unobserved" and not failed:
            errors.append({"code": "stream_terminal_missing", "call_id": request.get("call_id")})
    feedback = []
    for row in rows.values():
        if row.get("kind") != "attachment":
            continue
        origin = row.get("origin") or row.get("origin_event_id")
        following = next((r for r in requests if r.get("call_id") == row.get("call_id")), None)
        responded = any(r.get("call_id") == row.get("call_id") for r in responses)
        if origin and origin not in rows:
            errors.append({"code": "dangling_feedback_origin", "event_id": row["event_id"]})
        verified = None
        if following and row.get("field") and row.get("data_sha256"):
            try:
                value = json.loads(bodies[following["event_id"]]["parts"]["input"]["text"])
                for part in row["field"].strip("/").split("/"):
                    if isinstance(value, str):
                        value = json.loads(value)
                    value = value[int(part)] if isinstance(value, list) else value[part.replace("~1", "/").replace("~0", "~")]
                actual = hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
                verified = actual == row["data_sha256"]
            except (KeyError, ValueError, TypeError, IndexError):
                verified = False
            if not verified:
                errors.append({"code": "feedback_hash_mismatch", "event_id": row["event_id"]})
        feedback.append({"event_id": row["event_id"], "origin_present": origin in rows,
                         "request_present": following is not None, "field_verified": verified, "response_received": responded})
    return {"events": len(rows), "duplicates": duplicates, "errors": errors,
            "completed": terminal and not open_spans, "open_spans": len(open_spans),
            "requests": len(requests), "responses": len(responses), "missing_bodies": len(missing_bodies),
            "missing_execution_io": missing_executions, "streams": streams, "feedback": feedback}, rows


def http_json(url, timeout=2):
    # Loopback connections must not be sent through proxy configuration.
    with build_opener(ProxyHandler({})).open(url, timeout=timeout) as response:
        return response.status, response.read()


def ready(url):
    try:
        return http_json(url, timeout=.4)[0] == 200
    except (OSError, URLError):
        return False


def viewer_ready(config):
    try:
        _, body = http_json(config["viewer"] + "/healthz")
        expected = hashlib.sha256(config["state_dir"].encode()).hexdigest()[:24]
        health = json.loads(body)
        return health.get("profile") == expected and health.get("runtime") == RUNTIME_FINGERPRINT
    except (OSError, URLError, ValueError):
        return False


def doctor(config):
    dependencies(config)
    modules = {name: available(name) for name in ("agents", "nemo_relay", "opentelemetry.sdk.trace",
               "opentelemetry.exporter.otlp.proto.http.trace_exporter", "phoenix", "psutil", "dotenv")}
    check = {"installed": modules, "credentials_present": bool(credentials(config)),
             "openai_metadata_authorized": config["openai_metadata"],
             "phoenix_running": ready(config["phoenix"] + "/healthz"),
             "viewer_running": viewer_ready(config), "checked_at": now()}
    check["dependencies_ready"] = all(modules.values())
    check["setup_ready"] = check["dependencies_ready"] and check["phoenix_running"] and check["viewer_running"] and (
        not config["openai_metadata"] or check["credentials_present"])
    # This is readiness only. It does not certify ACK/read-back or visual rendering.
    return check


def exports(path, config):
    if __package__:
        from .trace_exporters import project_agents, project_phoenix, project_relay
    else:
        from trace_exporters import project_agents, project_phoenix, project_relay
    report, events = audit(path)
    if report["errors"] or not report["completed"]:
        return {"status": "blocked", "audit": report, "checked_at": now()}
    output = Path(config["state_dir"]) / "exports" / (str(time.time_ns()) + "-" + uuid4().hex[:6])
    output.mkdir(parents=True)
    result = {"status": "checked", "projection": "saved_observation_metadata", "audit": report,
              "checked_at": now(), "collectors": {}}
    operations = {
        "relay": lambda: asyncio.run(project_relay([events], output / "relay")),
        "phoenix": lambda: project_phoenix([events], config["phoenix"] + "/v1/traces", "agent-trace-" + output.name),
    }
    if config["openai_metadata"]:
        operations["openai_sdk"] = lambda: project_agents([events], config.get("openai_project"), credentials(config))
    else:
        result["collectors"]["openai_sdk"] = {"status": "not_authorized"}
    for name, operation in operations.items():
        try:
            result["collectors"][name] = operation()
        except Exception as error:
            result["collectors"][name] = {"status": "error", "error_type": type(error).__name__}
    collectors = result["collectors"]
    result["all_collectors_verified"] = bool(collectors.get("relay", {}).get("scopes_verified")) and (
        collectors.get("phoenix", {}).get("export_acknowledged") is True and
        collectors["phoenix"].get("spans_verified_in_phoenix") == collectors["phoenix"].get("spans") and
        collectors.get("openai_sdk", {}).get("status") == "acknowledged")
    save(output / "receipt.json", result)
    return result


def journal_paths(config):
    root = Path(config["journal_dir"]).resolve()
    return [p for p in sorted(root.glob("*/events.jsonl")) if p.resolve().is_relative_to(root)]


def inventory(config):
    entries = []
    for path in journal_paths(config):
        report, events = audit(path)
        first = next(iter(events.values()), {})
        entries.append({"id": hashlib.sha256(str(path).encode()).hexdigest()[:24],
                        "run_id": first.get("run_id"), "attempt_id": first.get("attempt_id"), **report})
    return entries


def handler(config):
    by_id = lambda: {hashlib.sha256(str(p).encode()).hexdigest()[:24]: p for p in journal_paths(config)}
    viewer_port = urlsplit(config["viewer"]).port
    class Viewer(BaseHTTPRequestHandler):
        def do_GET(self):
            host = self.headers.get("Host", "")
            origin = self.headers.get("Origin")
            if host not in {f"127.0.0.1:{viewer_port}", f"localhost:{viewer_port}"} or (
                    origin and origin not in {config["viewer"], f"http://localhost:{viewer_port}"}) or (
                    not origin and self.headers.get("Sec-Fetch-Site") == "cross-site"):
                self.send_error(403)
                return
            url = urlsplit(self.path)
            content_type = "application/json; charset=utf-8"
            if url.path == "/healthz":
                payload = json.dumps({"ok": True, "service": "openai-agent-trace",
                    "profile": hashlib.sha256(config["state_dir"].encode()).hexdigest()[:24],
                    "pid": os.getpid(), "runtime": RUNTIME_FINGERPRINT}).encode()
            elif url.path == "/":
                payload = (SKILL / "assets/trace_viewer.html").read_bytes()
                content_type = "text/html; charset=utf-8"
            elif url.path == "/runs":
                payload = json.dumps({"runs": inventory(config), "state": load(Path(config["state_dir"]) / "state.json", {})}, ensure_ascii=False).encode()
            elif url.path == "/run":
                path = by_id().get(parse_qs(url.query).get("id", [None])[0])
                if path is None:
                    self.send_error(404)
                    return
                report, events = audit(path)
                bodies = load_jsonl(path.with_name("bodies.jsonl"))
                executions = load_jsonl(path.with_name("execution-io.jsonl"))
                payload = json.dumps({"audit": report, "events": list(events.values()), "bodies": bodies,
                                      "executions": executions}, ensure_ascii=False).encode()
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "private, no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_):
            pass
    return Viewer


def load_jsonl(path):
    if not path.is_file():
        return []
    result = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            result.append(json.loads(line))
        except ValueError:
            result.append({"status": "partial_record"})
    return result


def spawn(config, argv, label, *, strip_credentials=False):
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(config.get("dependency_paths", []))
    if strip_credentials:
        env = {key: value for key, value in env.items() if not any(s in key.upper() for s in ("API_KEY", "TOKEN", "SECRET"))}
    state = Path(config["state_dir"])
    with (state / (label + ".stdout.log")).open("a", encoding="utf-8") as stdout, (state / (label + ".stderr.log")).open("a", encoding="utf-8") as stderr:
        flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        child = subprocess.Popen(argv, cwd=config["workspace"], env=env, stdout=stdout, stderr=stderr,
                                 creationflags=flags, start_new_session=os.name != "nt")
    return child.pid


def start(config, config_path):
    pids = {}
    if not ready(config["phoenix"] + "/healthz"):
        bootstrap = ("import os,runpy,site;[site.addsitedir(p) for p in os.environ.get('PYTHONPATH','').split(os.pathsep) if p];"
            f"os.environ.update(PHOENIX_HOST='127.0.0.1',PHOENIX_PORT='{urlsplit(config['phoenix']).port}',"
            f"PHOENIX_GRPC_PORT='{config.get('phoenix_grpc_port', urlsplit(config['phoenix']).port + 1)}',"
            f"PHOENIX_WORKING_DIR={str(Path(config['state_dir']) / 'phoenix')!r},PHOENIX_TELEMETRY_ENABLED='false',PHOENIX_DISABLE_AGENT_ASSISTANT='true');"
            "runpy.run_module('phoenix.server.main',run_name='__main__',alter_sys=True)")
        pids["phoenix"] = spawn(config, [config["python"], "-c", bootstrap, "serve"], "phoenix", strip_credentials=True)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if ready(config["phoenix"] + "/healthz"):
                break
            time.sleep(.25)
        if not ready(config["phoenix"] + "/healthz"):
            return {"start_blocked": "phoenix_not_ready; inspect the private phoenix stderr log", **doctor(config)}
    if not viewer_ready(config):
        if ready(config["viewer"] + "/healthz"):
            # Upgrade only a watcher whose profile and process command line both match.
            try:
                import psutil
                _, raw = http_json(config["viewer"] + "/healthz")
                health = json.loads(raw)
                expected = hashlib.sha256(config["state_dir"].encode()).hexdigest()[:24]
                previous = load(Path(config["state_dir"]) / "state.json", {})
                process = psutil.Process(health.get("pid") or previous.get("pid"))
                argv = process.cmdline()
                owned = health.get("profile") == expected and "watch" in argv and str(Path(__file__).resolve()) in argv and str(config_path) in argv
                if not owned:
                    return {"restart_blocked": "port_owned_by_other_process", **doctor(config)}
                process.terminate()
                process.wait(timeout=4)
            except Exception as error:
                return {"restart_blocked": type(error).__name__, **doctor(config)}
        pids["watcher"] = spawn(config, [config["python"], str(Path(__file__).resolve()), "watch", "--config", str(config_path)], "watcher")
        for _ in range(20):
            if viewer_ready(config):
                break
            time.sleep(.25)
    return {"started_pids": pids, **doctor(config)}


def watch(config):
    server = ThreadingHTTPServer(("127.0.0.1", urlsplit(config["viewer"]).port), handler(config))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    state_path = Path(config["state_dir"]) / "state.json"
    state = load(state_path, {"runs": {}})
    state.update(pid=os.getpid(), started_at=now())
    try:
        while True:
            for path in journal_paths(config):
                key = hashlib.sha256(str(path).encode()).hexdigest()[:24]
                signature = hashlib.sha256(path.read_bytes() + RUNTIME_FINGERPRINT.encode()).hexdigest()
                if state["runs"].get(key, {}).get("signature") == signature:
                    continue
                try:
                    report, _ = audit(path)
                    if report["completed"]:
                        receipt = exports(path, config)
                        state["runs"][key] = {"signature": signature, "receipt": receipt}
                except Exception as error:
                    state["runs"][key] = {"signature": signature, "receipt": {"status": "error", "error_type": type(error).__name__}}
            state.update(heartbeat=now())
            save(state_path, state)
            time.sleep(5)
    finally:
        server.shutdown()


def fixture(config):
    if __package__:
        from .trace_runtime import TraceLog, json_digest
    else:
        from trace_runtime import TraceLog, json_digest
    folder = Path(config["journal_dir"]) / ("fixture-" + str(time.time_ns()))
    recorder = TraceLog(folder / "events.jsonl", folder.name, folder.name + "-a1", "synthetic")
    diagnostic = ""
    for call, fail in [("call-01", True), ("call-02", False)]:
        code = "import time; time.sleep(0.1); " + ("raise RuntimeError('synthetic failure')" if fail else "print('synthetic success')")
        event = recorder.emit("request_input", call_id=call, store=True, effort="low", requested_summary="auto", fixture=True)
        recorder.body(event, input=json.dumps({"stage": "debug" if not fail else "execute", "current_goal": "Synthetic recovery check",
            "recent_diagnostic": diagnostic, "accepted_cells": []}))
        with recorder.span("model_call", call_id=call):
            event = recorder.emit("model_response", call_id=call, status="completed", fixture=True, summary_fragments=0)
            recorder.body(event, output=json.dumps({"goal": "Synthetic output", "code": code, "signal": "continue"}))
        with recorder.span("python_execution", call_id=call) as (operation, outcome):
            child = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     text=True, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            recorder.emit("worker_lifecycle", call_id=call, worker_id=str(child.pid), status="started", operation_id=operation)
            with recorder.monitor(child.pid, str(child.pid), call_id=call, operation_id=operation):
                stdout, diagnostic = child.communicate(timeout=5)
            recorder.execution_io(operation, code=code, stdout=stdout, stderr=diagnostic)
            outcome.update(status="failed" if child.returncode else "passed", process_returncode=child.returncode)
            recorder.emit("worker_lifecycle", call_id=call, worker_id=str(child.pid), status="exited", operation_id=operation)
        if fail:
            recorder.emit("attachment", call_id="call-02", origin=operation, status="included", fixture=True,
                          field="/recent_diagnostic", data_sha256=json_digest(diagnostic))
    recorder.finish("passed", fixture=True)
    report, _ = audit(folder / "events.jsonl")
    save(Path(config["state_dir"]) / "fixture.json", {"journal": str(folder / "events.jsonl"), "audit": report})
    return {"fixture": True, "model_calls_executed": 0, "python_cells_executed": 2, "journal": str(folder / "events.jsonl"), "audit": report}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["init", "install", "doctor", "start", "watch", "fixture", "audit", "export", "verify-response"])
    parser.add_argument("--config", type=Path, default=Path(".agent-trace/config.json"))
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--deps", type=Path, action="append", default=[])
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--openai-project")
    parser.add_argument("--allow-openai-metadata", action="store_true")
    parser.add_argument("--phoenix-port", type=int, default=6006)
    parser.add_argument("--phoenix-grpc-port", type=int)
    parser.add_argument("--viewer-port", type=int, default=4186)
    parser.add_argument("--journal", type=Path)
    parser.add_argument("--response-id")
    args = parser.parse_args()
    config_path = args.config.resolve()
    if args.command == "init":
        grpc_port = args.phoenix_grpc_port or args.phoenix_port + 1
        if any(not 1024 <= port <= 65535 for port in [args.phoenix_port, args.viewer_port, grpc_port]) or len({args.phoenix_port, args.viewer_port, grpc_port}) != 3:
            parser.error("Choose distinct loopback ports between 1024 and 65535")
        if config_path.exists():
            parser.error("Configuration exists; preserve it and edit deliberately")
        workspace = args.workspace.resolve(strict=True)
        state = config_path.parent
        state.relative_to(workspace)
        config = {"version": 1, "workspace": str(workspace), "state_dir": str(state),
                  "journal_dir": str(state / "runs"), "python": str(Path(args.python).resolve(strict=True)),
                  "dependency_paths": [str(p.resolve(strict=True)) for p in args.deps],
                  "env_file": str(args.env_file.resolve(strict=True)) if args.env_file else None,
                  "openai_project": args.openai_project, "openai_metadata": args.allow_openai_metadata,
                  "phoenix": f"http://127.0.0.1:{args.phoenix_port}", "viewer": f"http://127.0.0.1:{args.viewer_port}",
                  "phoenix_grpc_port": grpc_port,
                  "store": True, "reasoning_summary": "auto", "streaming": True}
        save(config_path, config)
        ignore = workspace / ".gitignore"
        line = "/" + state.relative_to(workspace).as_posix() + "/"
        old = ignore.read_text(encoding="utf-8") if ignore.exists() else ""
        if line not in old.splitlines():
            ignore.write_text(old.rstrip() + "\n" + line + "\n", encoding="utf-8")
        print(json.dumps({"config": str(config_path), "created": True}))
        return
    config = load(config_path)
    if not config:
        parser.error("Initialize a profile first")
    if args.command != "install" and Path(sys.executable).resolve() != Path(config["python"]).resolve():
        # Readiness and exporters must run in the selected observer interpreter.
        completed = subprocess.run([config["python"], str(Path(__file__).resolve()), *sys.argv[1:]])
        raise SystemExit(completed.returncode)
    if args.command == "install":
        environment = Path(config["state_dir"]) / "venv"
        venv.EnvBuilder(with_pip=True).create(environment)
        python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        subprocess.run([str(python), "-m", "pip", "install", "-r", str(SKILL / "scripts/observer-requirements.txt")], check=True)
        config.update(python=str(python), dependency_paths=[])
        save(config_path, config)
        return
    dependencies(config)
    if args.command == "watch":
        watch(config)
        return
    if args.command == "doctor":
        result = doctor(config)
    elif args.command == "start":
        result = start(config, config_path)
    elif args.command == "fixture":
        result = fixture(config)
    elif args.command == "verify-response":
        if not args.response_id or not args.response_id.startswith("resp_"):
            parser.error("--response-id must name an existing recorded response")
        from openai import OpenAI
        client = OpenAI(api_key=credentials(config), project=config.get("openai_project"))
        try:
            stored = client.responses.retrieve(args.response_id)
            result = {"response_id_matches": stored.id == args.response_id,
                      "store": getattr(stored, "store", None), "status": stored.status,
                      "summary_fragments": sum(bool(p.text.strip()) for item in stored.output if item.type == "reasoning"
                                               for p in item.summary or [] if p.type == "summary_text"), "checked_at": now()}
            result["verified"] = result["response_id_matches"] and result["store"] is True
        except Exception as error:
            result = {"verified": False, "error_type": type(error).__name__, "http_status": getattr(error, "status_code", None)}
        finally:
            client.close()
        save(Path(config["state_dir"]) / "response-verification.json", result)
    elif args.command in {"audit", "export"}:
        if not args.journal:
            parser.error("--journal is required")
        if args.command == "audit":
            result, _ = audit(args.journal)
        else:
            result = exports(args.journal, config)
    print(json.dumps(result, ensure_ascii=False))
    if result.get("errors") or result.get("status") == "blocked" or result.get("all_collectors_verified") is False or result.get("verified") is False or result.get("setup_ready") is False:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
