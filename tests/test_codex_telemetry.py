import contextlib
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest

import compare_codex_telemetry as compare
from rebuild_session import load_saver


class CodexTelemetry(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.module = load_saver("codex")
        self.usage = dict(input_tokens=100, cached_input_tokens=20, cache_write_input_tokens=0,
                          output_tokens=10, reasoning_output_tokens=3, total_tokens=110)
        self.row = {"event.name": "codex.sse_event", "event.kind": "response.completed", "success": True,
                    "conversation.id": "session", "turn.id": "turn", "response_id": "response",
                    "model": "gpt-6-luna", "timeUnixNano": "1",
                    **{raw: self.usage[field] for field, raw in compare.FIELDS.items()}}
        self.transcript = self.root / "session.jsonl"
        self.transcript.write_text("".join(json.dumps(row) + "\n" for row in [
            {"type": "session_meta", "payload": {"id": "session"}},
            {"type": "turn_context", "payload": {"turn_id": "turn", "model": "gpt-6-luna"}},
            {"type": "token_usage_record", "payload": {"session_id": "session", "thread_id": "session",
             "turn_id": "turn", "response_id": "response", "usage": self.usage}}]))
        self.telemetry = self.root / "otel.json"

    def save_events(self, events):
        records = [{"timeUnixNano": str(index + 1), "attributes": [
            {"key": key, "value": {"stringValue": str(value)} if not isinstance(value, bool) else {"boolValue": value}}
            for key, value in row.items() if key != "timeUnixNano"]} for index, row in enumerate(events)]
        self.telemetry.write_text(json.dumps({"resourceLogs": [{"scopeLogs": [{"logRecords": records}]}]}))

    def run_cli(self, *extra):
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(io.StringIO()):
            code = compare.main(["--telemetry", str(self.telemetry), "--transcript", str(self.transcript),
                                 "--session-id", "session", *extra])
        return code, json.loads(stream.getvalue()) if stream.getvalue() else None

    def test_otlp_redelivery_session_filter_and_token_cost_parity_are_read_only(self):
        self.save_events([self.row, self.row, dict(self.row, **{"conversation.id": "foreign"}),
                          dict(self.row, **{"event.kind": "response.failed"})])
        before = {p: p.read_bytes() for p in self.root.iterdir()}
        code, result = self.run_cli()
        self.assertEqual(code, 0)
        self.assertEqual(result["matched_groups"], 1)
        self.assertEqual(result["differences"], [])
        self.assertEqual(result["cost_basis"], "reference_only")
        self.assertEqual(before, {p: p.read_bytes() for p in self.root.iterdir()})

    def test_changed_tokens_model_missing_request_and_conflicting_redelivery_are_detected(self):
        for key, value in (("input_token_count", 101), ("model", "gpt-6.1-sol"), ("response_id", "other")):
            self.save_events([dict(self.row, **{key: value})])
            code, result = self.run_cli()
            self.assertEqual(code, 1, key)
        self.save_events([self.row, dict(self.row, input_token_count=101)])
        self.assertEqual(self.run_cli()[0], 2)

    def test_missing_response_ids_require_explicit_aggregate_mode(self):
        row = dict(self.row)
        row.pop("response_id")
        self.save_events([row])
        self.assertEqual(self.run_cli()[0], 1)
        self.assertEqual(self.run_cli("--group-by", "turn")[0], 0)
        self.assertEqual(self.run_cli("--group-by", "model")[0], 0)
        self.telemetry.write_text(json.dumps(row) + "\n" + json.dumps(row) + "\n")
        self.assertEqual(self.run_cli("--group-by", "model")[0], 0)
        row.pop("timeUnixNano")
        self.telemetry.write_text(json.dumps(row))
        code, result = self.run_cli("--group-by", "model")
        self.assertEqual(code, 1)
        self.assertEqual(result["undetectable_redeliveries"], 1)

    def test_incomplete_invalid_fields_and_empty_data_do_not_match(self):
        for bad in (True, -1, "NaN", 1.5):
            self.save_events([dict(self.row, input_token_count=bad)])
            self.assertEqual(self.run_cli()[0], 2)
        row = dict(self.row)
        row.pop("cache_write_token_count")
        self.save_events([row])
        self.assertEqual(self.run_cli()[0], 2)
        self.save_events([])
        self.assertEqual(self.run_cli()[0], 1)

    def test_duplicate_transcript_response_is_reported(self):
        self.save_events([self.row])
        with self.transcript.open("a") as stream:
            stream.write(self.transcript.read_text().splitlines()[-1] + "\n")
        self.assertEqual(self.run_cli()[0], 2)

    def test_descendant_usage_is_only_included_when_requested(self):
        child = self.root / "rollout-child.jsonl"
        child.write_text("".join(json.dumps(row) + "\n" for row in [
            {"type": "session_meta", "payload": {"id": "child", "parent_thread_id": "session",
             "source": {"subagent": {"thread_spawn": {}}}}},
            {"type": "turn_context", "payload": {"turn_id": "turn-child", "model": "gpt-6-luna"}},
            {"type": "token_usage_record", "payload": {"session_id": "session", "thread_id": "child",
             "turn_id": "turn-child", "response_id": "response-child", "usage": self.usage}}]))
        self.save_events([self.row, dict(self.row, **{"conversation.id": "child", "turn.id": "turn-child",
                                                    "response_id": "response-child"})])
        code, result = self.run_cli()
        self.assertEqual((code, result["matched_groups"]), (0, 1))
        code, result = self.run_cli("--include-subagents")
        self.assertEqual((code, result["matched_groups"]), (0, 2))

    def test_unknown_price_is_marked_incomplete_even_when_tokens_match(self):
        self.transcript.write_text(self.transcript.read_text().replace("gpt-6-luna", "unknown"))
        self.save_events([dict(self.row, model="unknown")])
        code, result = self.run_cli()
        self.assertEqual(code, 1)
        self.assertTrue(result["unpriced_groups"])


if __name__ == "__main__":
    unittest.main()
