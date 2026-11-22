from pathlib import Path
import json
from types import SimpleNamespace
import tempfile
import unittest

from scripts.trace_runtime import TraceLog, call_responses
from scripts.trace_stack import audit, fixture, save
from scripts.trace_exporters import metadata


class FakeResponse:
    id = "resp_fixture"
    status = "completed"
    store = True
    output = []
    usage = None
    _request_id = "req_fixture"

    def model_dump(self, **_):
        return {"id": self.id, "status": self.status, "store": self.store, "output": []}


class Client:
    def __init__(self, error=None):
        self.calls = []
        self.error = error
        self.responses = SimpleNamespace(create=self.create)

    def create(self, **params):
        self.calls.append(params)
        if self.error:
            raise self.error
        return FakeResponse()


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "attempt/events.jsonl"
        self.trace = TraceLog(self.path, "run", "attempt", "test")

    def test_one_call_preserves_model_mode_and_headers_and_records_bodies(self):
        client = Client()
        response, _ = call_responses(client, self.trace, "call-01", model="unchanged-model", input="test",
            reasoning={"effort": "low", "mode": "pro"}, extra_headers={"X-User-Header": "preserved"})
        self.trace.finish("passed")
        self.assertIsInstance(response, FakeResponse)
        self.assertEqual(len(client.calls), 1)
        sent = client.calls[0]
        self.assertEqual(sent["model"], "unchanged-model")
        self.assertEqual(sent["reasoning"], {"effort": "low", "mode": "pro", "summary": "auto"})
        self.assertTrue(sent["store"])
        self.assertEqual(sent["extra_headers"]["X-User-Header"], "preserved")
        report, _ = audit(self.path)
        self.assertEqual(report["errors"], [])
        self.assertEqual((report["requests"], report["responses"], report["missing_bodies"]), (1, 1, 0))

    def test_none_and_explicit_local_only(self):
        client = Client()
        call_responses(client, self.trace, "call-01", model="same", input="test", reasoning={"effort": "none"})
        self.assertNotIn("summary", client.calls[0]["reasoning"])
        call_responses(client, self.trace, "call-02", model="same", input="test", store=False, platform_logs=False)
        self.assertFalse(client.calls[1]["store"])

    def test_original_failure_is_preserved_without_a_retry(self):
        original = RuntimeError("synthetic transport failure")
        client = Client(original)
        with self.assertRaises(RuntimeError) as caught:
            call_responses(client, self.trace, "call-01", model="same", input="test")
        self.assertIs(caught.exception, original)
        self.assertEqual(len(client.calls), 1)
        self.trace.finish("failed")
        report, events = audit(self.path)
        self.assertEqual(report["open_spans"], 0)
        self.assertTrue(any(row["kind"] == "model_error" for row in events.values()))

    def test_serialization_failure_is_visible_and_does_not_fail_the_agent(self):
        client = Client()
        response, _ = call_responses(client, self.trace, "call-01", model="same", input="test", unusual=object())
        self.assertIsInstance(response, FakeResponse)
        self.trace.finish("passed")
        report, _ = audit(self.path)
        self.assertTrue(any(row["code"] == "collector_write_failure" for row in report["errors"]))

    def test_credentials_not_in_sidecars_and_empty_outputs_stay_empty(self):
        credential = "sk-proj-" + "fixture" * 8
        self.trace.body("event-test", input=credential)
        self.trace.execution_io("operation", code="print(1)", stdout="", stderr="")
        body = self.path.with_name("bodies.jsonl").read_text(encoding="utf-8")
        self.assertNotIn(credential, body)
        self.assertIn("REDACTED_CREDENTIAL", body)
        io = json.loads(self.path.with_name("execution-io.jsonl").read_text(encoding="utf-8"))
        self.assertEqual(io["parts"]["stdout"]["text"], "")
        self.assertEqual(io["parts"]["stderr"]["chars"], 0)

    def test_conflicting_and_partial_records_do_not_pass(self):
        with self.path.open("a", encoding="utf-8") as file:
            original = json.loads(self.path.read_text(encoding="utf-8").splitlines()[0])
            file.write(json.dumps({**original, "status": "contradiction"}) + "\n")
            file.write('{"unfinished":')
        report, _ = audit(self.path)
        self.assertIn("conflicting_duplicate", [row["code"] for row in report["errors"]])
        self.assertIn("invalid_or_partial_line", [row["code"] for row in report["errors"]])

    def test_metadata_allowlist_excludes_text_and_paths(self):
        fields = metadata({"event_id": "run/attempt/event-1", "input": "private", "code": "private",
                           "response_id": "resp_fixture", "call_id": "C:/Users/private"})
        self.assertEqual(fields, {"agent.event_id": "run/attempt/event-1", "agent.response_id": "resp_fixture"})

    def test_sse_receipts_and_missing_sequence_are_checked(self):
        class Stream:
            response = SimpleNamespace(headers={"x-request-id": "req_fixture"})
            def __init__(self, gap):
                self.gap = gap
            def __enter__(self):
                return self
            def __exit__(self, *_):
                pass
            def __iter__(self):
                yield SimpleNamespace(type="response.created", sequence_number=0, response=None)
                yield SimpleNamespace(type="response.completed", sequence_number=2 if self.gap else 1, response=FakeResponse())
        for gap in [False, True]:
            with self.subTest(gap=gap):
                path = self.path.parent / ("gap" if gap else "complete") / "events.jsonl"
                trace = TraceLog(path, "run", "attempt", "fixture")
                client = SimpleNamespace(responses=SimpleNamespace(create=lambda **_: Stream(gap)))
                call_responses(client, trace, "call-01", model="same", input="test", stream=True)
                trace.finish("passed")
                report, _ = audit(path)
                self.assertEqual(report["streams"]["call-01"]["receipts"], 2)
                self.assertEqual(report["streams"]["call-01"]["gaps"], 1 if gap else 0)
                self.assertEqual(any(r["code"] == "stream_capture_gap" for r in report["errors"]), gap)


class FixtureTest(unittest.TestCase):
    def test_real_python_recovery_and_exact_feedback_with_no_model_call(self):
        with tempfile.TemporaryDirectory() as directory:
            result = fixture({"journal_dir": str(Path(directory) / "runs"), "state_dir": directory})
            self.assertEqual(result["model_calls_executed"], 0)
            self.assertEqual(result["python_cells_executed"], 2)
            self.assertEqual(result["audit"]["errors"], [])
            self.assertEqual(result["audit"]["missing_execution_io"], 0)
            self.assertTrue(result["audit"]["feedback"][0]["field_verified"])
            self.assertTrue(result["audit"]["feedback"][0]["response_received"])
            folder = Path(result["journal"]).parent
            generated = [json.loads(json.loads(line)["parts"]["output"]["text"])["code"]
                         for line in (folder / "bodies.jsonl").read_text(encoding="utf-8").splitlines()
                         if "output" in json.loads(line)["parts"]]
            executed = [json.loads(line)["parts"]["code"]["text"]
                        for line in (folder / "execution-io.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual(generated, executed)


if __name__ == "__main__":
    unittest.main()
