import copy
from decimal import Decimal
import json
import logging
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import agy_save
import claude_save
import codex_save
import opencode_save
import pi_save
import rebuild_session
import saver_doctor
import check_usage_prices
import compare_claude_telemetry as telemetry

ROOT = Path(__file__).resolve().parents[1]


class SaverImprovements(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        for module in (agy_save, claude_save, codex_save, opencode_save, pi_save):
            patcher = mock.patch.multiple(module, OUTPUT_BASE=self.root / module.__name__ / "notes",
                STATE_DIR=self.root / module.__name__ / "state", logger=logging.getLogger("test." + module.__name__))
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(claude_save, "read_stop_transcript", side_effect=lambda path, *args, **kwargs: path.read_text())
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_rows(self, path, rows):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        return path

    def claude_rows(self, session="parent"):
        return [
            {"type": "user", "sessionId": session, "uuid": "q1", "timestamp": "2026-10-25T01:00:00Z",
             "message": {"content": "質問"}},
            {"type": "assistant", "sessionId": session, "uuid": "a1", "requestId": "req-parent",
             "message": {"id": "answer", "model": "claude-sonnet-5-5", "content": "親回答",
                         "usage": {"input_tokens": 100, "output_tokens": 10, "cache_read_input_tokens": 50}}}]

    def claude_hook(self, path, event="Stop", **extra):
        return {"session_id": "parent", "transcript_path": str(path), "cwd": str(self.root),
                "hook_event_name": event, **extra}

    def test_codex_stop_observes_one_snapshot_during_concurrent_append(self):
        path = self.write_rows(self.root / "codex.jsonl", [
            {"type": "turn_context", "payload": {"turn_id": "t", "model": "gpt-6-luna"}},
            {"type": "event_msg", "payload": {"type": "user_message", "message": "質問"}},
            {"type": "event_msg", "payload": {"type": "agent_message", "message": "最初の回答"}},
            {"type": "token_usage_record", "payload": {"turn_id": "t", "usage": {
                "input_tokens": 10, "output_tokens": 5, "total_tokens": 15}}}])
        original = codex_save.retrieve_quota
        appended = False
        def during_read(*args, **kwargs):
            nonlocal appended
            if not appended:
                with path.open("a") as stream:
                    stream.write(json.dumps({"type": "event_msg", "payload": {
                        "type": "agent_message", "message": "追記中の回答"}}) + "\n")
                appended = True
            return original(*args, **kwargs)
        hook = {"session_id": "one-snapshot", "transcript_path": str(path), "cwd": str(self.root)}
        with mock.patch.object(codex_save, "retrieve_quota", side_effect=during_read), \
                mock.patch.object(codex_save, "read_jsonl_snapshot", wraps=codex_save.read_jsonl_snapshot) as read:
            codex_save.handle_stop_event(hook)
            self.assertEqual(read.call_count, 1)
        state = codex_save.load_state("one-snapshot")
        output = Path(state["output_path"])
        self.assertNotIn("追記中の回答", output.read_text())
        self.assertEqual(state["last_line"], 4)
        codex_save.handle_stop_event(hook)
        self.assertIn("追記中の回答", output.read_text())

    def test_snapshot_malformed_and_partial_rows_preserve_cursor(self):
        path = self.root / "partial.jsonl"
        path.write_text('null\n{bad}\n{"type":"event_msg","payload":{"type":"agent_message","message":123}}\n{"type":')
        snapshot = codex_save.read_jsonl_snapshot(path)
        self.assertEqual(snapshot["last_line"], 3)
        self.assertEqual(snapshot["malformed_rows"], 2)
        self.assertEqual(codex_save.load_jsonl_messages(path, 0, snapshot=snapshot), ([], 3))

    def test_jsonl_preserves_unicode_separators_inside_text(self):
        text = "前半\u2028後半\u0085末尾"
        path = self.root / "unicode.jsonl"
        path.write_text(json.dumps({"type": "event_msg", "payload": {
            "type": "agent_message", "message": text}}, ensure_ascii=False) + "\n")
        messages, cursor = codex_save.load_jsonl_messages(path, 0)
        self.assertEqual(cursor, 1)
        self.assertEqual(messages[0]["message"], text)

    def test_price_versions_restore_context_and_are_independent(self):
        for module in (codex_save, claude_save, opencode_save, pi_save):
            with self.subTest(module=module.__name__):
                usage = {"input_tokens": 1_000_000, "cached_input_tokens": 1_000_000}
                self.assertEqual(module._usage_cost("claude-sonnet-5-5", usage), Decimal("0.1"))
                with module.usage_price_version("3.11.1"):
                    self.assertEqual(module._usage_cost("claude-sonnet-5-5", usage), Decimal("0.2"))
                    self.assertNotIn("claude-fable-5-1", module.active_usage_rates())
                self.assertEqual(module.active_price_version(), "4.2.0")
                with self.assertRaises(ValueError):
                    with module.usage_price_version("unregistered"):
                        pass
                self.assertEqual(module.active_price_version(), "4.2.0")

    def test_rebuild_preserves_price_version_and_explicit_legacy_selection(self):
        path = self.write_rows(self.root / "parent.jsonl", self.claude_rows())
        output_dir = self.root / "export"
        args = dict(provider="claude", session_id="parent", transcript=path,
                    output_dir=output_dir, module=claude_save)
        target, _ = rebuild_session.rebuild(**args, price_version="3.11.1")
        self.assertIn('price_version: "3.11.1"', target.read_text())
        self.assertIn('total_cost_usd: 0.00031', target.read_text())
        target, backup = rebuild_session.rebuild(**args, pricing="preserve")
        self.assertTrue(backup.exists())
        self.assertIn('total_cost_usd: 0.00031', target.read_text())
        self.assertEqual(claude_save.note_price_version(target.read_text()), "3.11.1")
        target, _ = rebuild_session.rebuild(**args)
        self.assertIn('total_cost_usd: 0.000305', target.read_text())
        self.assertIn('price_checked_at:', target.read_text())
        # A legacy note without provenance must not silently pretend its price is known.
        target.write_text(target.read_text().replace('  price_version: "4.2.0"\n', ''))
        before = target.read_bytes()
        with self.assertRaisesRegex(ValueError, "価格版がありません"):
            rebuild_session.rebuild(**args, pricing="preserve")
        self.assertEqual(target.read_bytes(), before)

    def test_claude_child_usage_deduplicates_and_attributes_only_known_parent(self):
        path = self.write_rows(self.root / "parent.jsonl", self.claude_rows())
        child = path.with_suffix("") / "subagents/agent-child.jsonl"
        record = {"type": "assistant", "sessionId": "parent", "parentUuid": "a1", "isSidechain": True,
                  "message": {"id": "answer", "model": "claude-haiku-5-5", "content": "子の本文",
                              "usage": {"input_tokens": 10, "output_tokens": 5}}}
        self.write_rows(child, [record, dict(record, message=dict(record["message"], usage={"input_tokens": 20, "output_tokens": 10}))])
        unknown = copy.deepcopy(record)
        unknown.pop("parentUuid")
        self.write_rows(child.parent / "agent-unknown.jsonl", [unknown])
        alien = dict(record, sessionId="other")
        self.write_rows(child.parent / "agent-other.jsonl", [alien])
        claude_save.handle_stop_event(self.claude_hook(path, "SubagentStop", agent_transcript_path=str(child)))
        state = claude_save.load_state("parent")
        content = Path(state["output_path"]).read_text()
        self.assertIn("total_tokens: 205", content)  # 160 parent + 30 child + 15 unlinked
        self.assertIn("parent_tokens: 160", content)
        self.assertIn("subagent_tokens: 45", content)
        self.assertIn("unattributed_subagent_tokens: 15", content)
        self.assertIn("回答に紐付かない子 15 tok", content)
        self.assertIn("Usage: 190 tok", content)  # unlinked child is not forced into a reply
        self.assertIn("claude-haiku-5-5 30 tok", content)
        self.assertNotIn("子の本文", content)
        claude_save.handle_stop_event(self.claude_hook(path))
        self.assertEqual(Path(state["output_path"]).read_text(), content)
        claude_save.state_path("parent").unlink()
        claude_save.handle_stop_event(self.claude_hook(path))
        self.assertEqual(Path(state["output_path"]).read_text(), content)
        with self.assertRaises(ValueError):
            claude_save.handle_stop_event(self.claude_hook(path, "SubagentStop", agent_transcript_path=str(path)))

    def test_claude_stop_failure_saves_partial_note_and_safe_error_category(self):
        path = self.write_rows(self.root / "parent.jsonl", self.claude_rows())
        hook = self.claude_hook(path, "StopFailure", error="rate_limit", last_assistant_message="secret API error",
                                error_details="private credentials")
        claude_save.handle_stop_event(hook)
        claude_save.read_stop_transcript.assert_not_called()
        output = Path(claude_save.load_state("parent")["output_path"])
        content = output.read_text()
        self.assertIn('type: "rate_limit"', content)
        self.assertIn("親回答", content)
        self.assertNotIn("secret API error", content)
        self.assertNotIn("private credentials", content)
        claude_save.handle_stop_event(hook)
        self.assertEqual(output.read_text(), content)

    def test_claude_empty_failure_is_recorded_and_subagent_wait_uses_child(self):
        path = self.write_rows(self.root / "parent.jsonl", [])
        claude_save.handle_stop_event(self.claude_hook(path, "StopFailure", error="billing_error"))
        self.assertEqual(claude_save.load_state("parent")["last_cli_failure"]["type"], "billing_error")
        self.write_rows(path, self.claude_rows())
        child = self.write_rows(path.with_suffix("") / "subagents/agent-last.jsonl", [])
        claude_save.handle_stop_event(self.claude_hook(path, "SubagentStop", agent_transcript_path=str(child),
                                                      last_assistant_message="子回答"))
        claude_save.read_stop_transcript.assert_called_once_with(child, "子回答", include_sidechain=True)
        self.assertEqual(claude_save.load_state("parent")["last_cli_failure"]["type"], "billing_error")

    def test_doctor_is_read_only_and_reports_cursor_and_version_drift(self):
        path = self.write_rows(self.root / "parent.jsonl", self.claude_rows())
        claude_save.handle_stop_event(self.claude_hook(path))
        state_dir = claude_save.STATE_DIR
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.rglob("*") if p.is_file()}
        result = saver_doctor.diagnose("claude", state_dir, ROOT / "claude_save.py", ROOT, "parent")
        self.assertEqual(result["issues"], [])
        self.assertEqual(result["sessions"][0]["issues"], [])
        self.assertIsNotNone(result["sessions"][0]["observation"]["last_success_at"])
        self.assertEqual({p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.rglob("*") if p.is_file()}, before)
        state = claude_save.load_state("parent")
        state["last_line"] = 999
        claude_save.save_state("parent", state)
        changed = self.root / "old_save.py"
        changed.write_text('__version__ = "0.0.0"\n')
        result = saver_doctor.diagnose("claude", state_dir, changed, ROOT, "parent")
        self.assertIn("installed_differs", result["issues"])
        self.assertIn("cursor_mismatch", result["sessions"][0]["issues"])
        self.assertIn("cursor_beyond_transcript", result["sessions"][0]["issues"])

    def test_failed_write_observation_keeps_error_after_recovery(self):
        path = self.write_rows(self.root / "parent.jsonl", self.claude_rows())
        with mock.patch.object(claude_save.os, "replace", side_effect=OSError("private message")):
            with self.assertRaises(OSError):
                claude_save.handle_stop_event(self.claude_hook(path))
        # Simulate note-only failure so the independent diagnostic writer can succeed.
        with mock.patch.object(claude_save, "atomic_write_md", side_effect=OSError("private message")):
            with self.assertRaises(OSError):
                claude_save.handle_stop_event(self.claude_hook(path))
        report = json.loads(claude_save.diagnostic_path("parent").read_text())
        self.assertEqual(report["last_failure_type"], "OSError")

        self.assertNotIn("private message", json.dumps(report))
        diagnosis = saver_doctor.diagnose("claude", claude_save.STATE_DIR, ROOT / "claude_save.py", ROOT)
        self.assertIn("last_save_failed", diagnosis["sessions"][0]["issues"])
        self.assertIn("missing_state", diagnosis["sessions"][0]["issues"])
        claude_save.handle_stop_event(self.claude_hook(path))
        report = json.loads(claude_save.diagnostic_path("parent").read_text())
        self.assertEqual(report["last_outcome"], "saved")
        self.assertEqual(report["last_failure_type"], "OSError")

    def test_doctor_checks_step_cursor_and_snapshot_hash_without_execution(self):
        output = self.root / "note.md"
        output.write_text('---\nsession_id: "agy"\n---\n<!-- last_id: 5 -->\n')
        state_path = self.root / "agy.json"
        state_path.write_text(json.dumps({"output_path": str(output), "last_id": "4"}))
        self.assertIn("cursor_mismatch", saver_doctor.inspect_session(state_path)["issues"])
        state_path.write_text(json.dumps({"output_path": str(output), "payload_hash": "invalid"}))
        self.assertIn("invalid_payload_hash", saver_doctor.inspect_session(state_path)["issues"])

    def test_concurrent_claude_hooks_preserve_children_and_single_note(self):
        path = self.write_rows(self.root / "parent.jsonl", self.claude_rows())
        installed = self.root / "hook/claude_save.py"
        installed.parent.mkdir()
        source = (ROOT / "claude_save.py").read_text()
        source = source.replace('STATE_DIR = Path.home()', 'STATE_DIR = Path(' + repr(str(self.root / "home")) + ')')
        source = source.replace('OUTPUT_BASE = OBSIDIAN_VAULT / OBSIDIAN_OUTPUT_DIR / SOURCE_NAME',
                                'OUTPUT_BASE = Path(' + repr(str(self.root / "concurrent-notes")) + ')')
        installed.write_text(source)
        jobs = []
        for index in range(3):
            child = path.with_suffix("") / ("subagents/agent-%s.jsonl" % index)
            self.write_rows(child, [{"type": "assistant", "sessionId": "parent", "isSidechain": True,
                "message": {"id": "same-id", "model": "claude-haiku-5-5", "usage": {"input_tokens": 10, "output_tokens": 5}}}])
            hook = self.claude_hook(path, "SubagentStop", agent_transcript_path=str(child))
            process = subprocess.Popen(["/usr/bin/python3", str(installed)], stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            process.stdin.write(json.dumps(hook))
            process.stdin.close()
            jobs.append(process)
        for process in jobs:
            process.wait(timeout=20)
            self.assertEqual(process.returncode, 0, process.stderr.read())
            process.stdout.close()
            process.stderr.close()
        notes = list((self.root / "concurrent-notes").glob("*.md"))
        self.assertEqual(len(notes), 1)
        self.assertIn("total_tokens: 205", notes[0].read_text())
        self.assertEqual(notes[0].read_text().count("親回答"), 1)

    def test_price_diff_does_not_modify_catalog_and_rejects_bad_rates(self):
        catalog = check_usage_prices.catalog(claude_save)
        self.assertEqual(check_usage_prices.price_diff(catalog, copy.deepcopy(catalog)), [])
        candidate = copy.deepcopy(catalog)
        candidate["rates"]["claude-sonnet-5-5"]["cached_input"] = "0.2"
        self.assertEqual(len(check_usage_prices.price_diff(catalog, candidate)), 1)
        self.assertEqual(catalog["rates"]["claude-sonnet-5-5"]["cached_input"], "0.10")
        for bad in (True, "NaN", "-1"):
            candidate["rates"]["claude-sonnet-5-5"]["cached_input"] = bad
            with self.assertRaises(ValueError):
                check_usage_prices.price_diff(catalog, candidate)

    def test_otlp_request_comparison_handles_cache_dedup_and_session_filter(self):
        attrs = {"event.name": "api_request", "session.id": "parent", "request_id": "req-parent",
                 "model": "claude-sonnet-5-5",
                 "input_tokens": "100", "output_tokens": 10, "cache_read_tokens": 50,
                 "cache_creation_tokens": 0, "cost_usd_micros": 305}
        def record(data):
            return {"attributes": [{"key": key, "value": {"stringValue": str(value)}} for key, value in data.items()]}
        document = {"resourceLogs": [{"scopeLogs": [{"logRecords": [record(attrs), record(attrs),
            record(dict(attrs, **{"session.id": "other", "request_id": "other"})),
            record(dict(attrs, **{"event.name": "api_error"}))]}]}]}
        path = self.root / "otel.json"
        path.write_text(json.dumps(document))
        observed, skipped = telemetry.telemetry_requests(path, "parent")
        transcript = self.write_rows(self.root / "parent.jsonl", self.claude_rows())
        recorded, _ = telemetry.transcript_requests(transcript, "parent", module=claude_save)
        result = telemetry.compare(observed, recorded)
        self.assertEqual(skipped, 0)
        self.assertEqual(result["matched_requests"], 1)
        self.assertEqual(result["differences"], [])
        self.assertEqual(result["only_telemetry"], [])
        recorded["req-parent"]["input_tokens"] = 101
        self.assertEqual(telemetry.compare(observed, recorded)["differences"][0]["field"], "input_tokens")
        args = ["--telemetry", str(path), "--transcript", str(transcript), "--session-id", "parent"]
        with mock.patch("sys.stdout"):
            self.assertEqual(telemetry.main(args), 0)
            self.assertEqual(telemetry.main(args[:-1] + ["unknown"]), 1)

    def test_codex_invalid_item_type_does_not_discard_valid_messages(self):
        path = self.write_rows(self.root / "codex.jsonl", [
            {"type": "event_msg", "payload": {"type": "item_completed", "item": {"type": []}}},
            {"type": "response_item", "payload": {"type": {}}},
            {"type": "event_msg", "payload": {"type": "agent_message", "message": "valid"}}])
        messages, cursor = codex_save.load_jsonl_messages(path, 0)
        self.assertEqual([message["message"] for message in messages], ["valid"])
        self.assertEqual(cursor, 3)

    def test_foreign_child_rows_cannot_attribute_unlinked_usage(self):
        path = self.write_rows(self.root / "parent.jsonl", self.claude_rows())
        child = path.with_suffix("") / "subagents/agent-child.jsonl"
        own = self.claude_rows()[1]
        foreign = dict(own, sessionId="other", parentUuid="a1")
        self.write_rows(child, [own, foreign])
        records = claude_save.claude_subagent_records(path, "parent", claude_save.read_jsonl_snapshot(path))
        self.assertEqual(len(records), 1)
        self.assertIsNone(records[0]["parent_turn_index"])

    def test_telemetry_model_mismatch_and_invalid_otlp(self):
        row = dict.fromkeys(telemetry.TOKEN_FIELDS, 0)
        row.update(cost_usd="0", model="model-a")
        result = telemetry.compare({"req": row}, {"req": dict(row, model="model-b")})
        self.assertEqual(result["differences"][0]["field"], "model")
        for invalid in ({"resourceLogs": None}, {"resourceLogs": [None]},
                        {"resourceLogs": [{"scopeLogs": [None]}]},
                        {"resourceLogs": [{"scopeLogs": [{"logRecords": [None]}]}]}):
            with self.assertRaises(ValueError):
                list(telemetry.event_attributes(invalid))

    def test_doctor_excludes_opencode_bridge_cursor(self):
        state_dir = self.root / "state"
        state_dir.mkdir()
        (state_dir / "opencode_v2_cursor.json").write_text('{"cursor": 42}')
        result = saver_doctor.diagnose("opencode", state_dir, ROOT / "opencode_save.py", ROOT)
        self.assertEqual(result["sessions"], [])
        self.assertEqual(result["issues"], ["no_saved_sessions"])

    def test_doctor_uses_documented_pi_install_path(self):
        home = self.root / 'home'
        installed = home / '.pi/agent/scripts/pi_save.py'
        installed.parent.mkdir(parents=True)
        installed.write_bytes((ROOT / 'pi_save.py').read_bytes())
        state_dir = home / '.local/state/pi-obsidian'
        state_dir.mkdir(parents=True)
        with mock.patch.object(saver_doctor.Path, 'home', return_value=home):
            result = saver_doctor.diagnose('pi')
        self.assertEqual(result['installed']['path'], str(installed))
        self.assertNotIn('installed_unreadable', result['issues'])
        self.assertNotIn('installed_differs', result['issues'])

    def test_doctor_explains_skipped_save_and_clears_reason_after_success(self):
        path = self.write_rows(self.root / 'parent.jsonl', self.claude_rows())
        hook = self.claude_hook(path)
        missing = dict(hook, transcript_path=str(self.root / 'missing.jsonl'))
        claude_save.handle_stop_event(missing)
        observation = json.loads(claude_save.diagnostic_path('parent').read_text())
        self.assertEqual(observation['skip_reason'], 'transcript_missing')
        self.assertIn('last_save_skipped', saver_doctor.inspect_session(claude_save.state_path('parent'))['issues'])
        claude_save.handle_stop_event(hook)
        observation = json.loads(claude_save.diagnostic_path('parent').read_text())
        self.assertEqual(observation['last_outcome'], 'saved')
        self.assertNotIn('skip_reason', observation)
        claude_save.handle_stop_event(hook)
        observation = json.loads(claude_save.diagnostic_path('parent').read_text())
        self.assertEqual(observation['last_outcome'], 'saved')
        self.assertNotIn('skip_reason', observation)
        self.assertNotIn('last_save_skipped', saver_doctor.inspect_session(claude_save.state_path('parent'))['issues'])

    def test_agy_read_error_keeps_original_skip_reason(self):
        transcript_directory = self.root / 'transcript.jsonl'
        transcript_directory.mkdir()
        agy_save.handle_stop_event({'conversationId': 'agy-read-error',
                                    'transcriptPath': str(transcript_directory),
                                    'workspacePaths': [str(self.root)]})
        observation = json.loads(agy_save.diagnostic_path('agy-read-error').read_text())
        self.assertEqual(observation['last_outcome'], 'skipped')
        self.assertEqual(observation['skip_reason'], 'transcript_unreadable')


if __name__ == "__main__":
    unittest.main()
