import contextlib
import io
import json
import os
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from scripts.trace_artifacts import (
    ArtifactError,
    check_payload,
    inspect_response,
    main,
    plan_summary_span,
)


FIXTURE = Path(__file__).resolve().parents[1] / "assets" / "synthetic_response.json"
TRACE = "trace_" + "a" * 32
RESPONSE = "resp_fixture_001"
OBSERVED = "2026-01-01T12:00:00Z"


class ResponseTests(unittest.TestCase):
    def setUp(self):
        self.raw = json.loads(FIXTURE.read_text(encoding="utf-8"))

    def test_uses_allowlisted_fields_only(self):
        self.raw["http_response"]["headers"]["Authorization"] = "do-not-print"
        self.raw["response"]["output"][0]["encrypted_content"] = "do-not-print"
        report, summary = inspect_response(self.raw)
        self.assertEqual(report["request_id"], "req_fixture_001")
        self.assertEqual(report["usage"]["total_tokens"], 20)
        self.assertEqual(report["usage"]["reasoning_tokens"], 3)
        self.assertEqual(report["output_items"][1]["call_id"], "call_fixture_001")
        self.assertNotIn("arguments", report["output_items"][1])
        self.assertEqual(report["summary_fragments"], 1)
        self.assertNotIn(summary["fragments"][0], json.dumps(report))
        self.assertNotIn("do-not-print", json.dumps(report))

    def test_no_summary_or_usage_does_not_guess(self):
        response = self.raw["response"]
        response.pop("usage")
        response["store"] = True
        response["reasoning"]["summary"] = "auto"
        response["output"] = [{"type": "reasoning", "summary": [
            {"type": "reasoning_text", "text": "not a summary"}, {"type": "summary_text", "text": " "}
        ]}]
        report, summary = inspect_response(response)
        self.assertIs(report["store"], True)
        self.assertEqual(report["reported_reasoning_summary"], "auto")
        self.assertEqual(report["summary_status"], "not_returned")
        self.assertIsNone(report["usage"]["total_tokens"])
        self.assertIsNone(report["request_id"])
        self.assertEqual(summary["fragments"], [])

    def test_cli_summary_output_is_private_and_exclusive(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "summary.json"
            out = io.StringIO()
            args = ["response-report", str(FIXTURE), "--summary-out", str(path)]
            with contextlib.redirect_stdout(out):
                self.assertEqual(main(args), 0)
            contents = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(contents["response_id"], RESPONSE)
            self.assertTrue(json.loads(out.getvalue())["summary_written"])
            self.assertNotIn(contents["fragments"][0], out.getvalue())
            if os.name == "posix":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as failure:
                main(args)
            self.assertEqual(failure.exception.code, 2)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), contents)

    def test_cli_missing_summary_does_not_write_a_file(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            path = folder / "summary.json"
            raw_path = folder / "response.json"
            raw_path.write_text(json.dumps({"id": RESPONSE, "output": []}), encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(["response-report", str(raw_path), "--summary-out", str(path)]), 0)
            self.assertFalse(path.exists())


class PayloadTests(unittest.TestCase):
    def setUp(self):
        self.record = {"response_id": RESPONSE, "fragments": ["Fixture-only summary"]}
        self.patch = plan_summary_span(self.record, TRACE, RESPONSE, OBSERVED)
        self.full = {"data": [
            {"object": "trace", "id": TRACE, "workflow_name": "Synthetic"},
            *self.patch["data"],
        ]}

    def test_complete_and_rootless_shapes(self):
        self.assertEqual(check_payload(self.full), {"trace_id": TRACE, "root_count": 1, "span_count": 1})
        self.assertEqual(check_payload(self.patch, TRACE)["root_count"], 0)
        with self.assertRaisesRegex(ArtifactError, "Do not resend"):
            check_payload(self.full, TRACE)
        with self.assertRaisesRegex(ArtifactError, "one trace root"):
            check_payload(self.patch)

    def test_summary_attachment_is_stable_and_opt_in(self):
        with_text = plan_summary_span(self.record, TRACE, RESPONSE, OBSERVED, include_text=True)
        original = self.patch["data"][0]
        self.assertEqual(original["id"], with_text["data"][0]["id"])
        self.assertEqual(original["started_at"], OBSERVED)
        self.assertNotIn("summary", original["span_data"]["data"])
        self.assertEqual(with_text["data"][0]["span_data"]["data"]["summary"], self.record["fragments"][0])
        with self.assertRaisesRegex(ArtifactError, "does not match"):
            plan_summary_span(self.record, TRACE, "resp_other", OBSERVED)

    def test_rejects_bad_span_id_trace_reference_parent_and_time(self):
        cases = (
            ({"id": "span_" + "b" * 32}, "24 lowercase hex"),
            ({"trace_id": "trace_" + "b" * 32}, "another trace"),
            ({"parent_id": "span_" + "c" * 24}, "missing or cyclic"),
            ({"started_at": "2026-01-01T12:00:00"}, "timezone"),
            ({"ended_at": "2026-01-01T11:59:59Z"}, "ends before"),
        )
        for fields, message in cases:
            with self.subTest(fields=fields), self.assertRaisesRegex(ArtifactError, message):
                payload = deepcopy(self.full)
                payload["data"][1].update(fields)
                check_payload(payload)

    def test_rejects_duplicate_and_cyclic_spans(self):
        duplicate = deepcopy(self.full)
        duplicate["data"].append(duplicate["data"][1].copy())
        with self.assertRaisesRegex(ArtifactError, "Duplicate span"):
            check_payload(duplicate)
        cyclic = deepcopy(self.full)
        cyclic["data"][1]["parent_id"] = cyclic["data"][1]["id"]
        with self.assertRaisesRegex(ArtifactError, "missing or cyclic"):
            check_payload(cyclic)


if __name__ == "__main__":
    unittest.main()
