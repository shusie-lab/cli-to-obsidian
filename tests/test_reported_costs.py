import json
import logging
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import claude_save
import opencode_save
import pi_save
import rebuild_session


class ReportedCosts(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for module in (claude_save, opencode_save, pi_save):
            patcher = mock.patch.multiple(module, OUTPUT_BASE=self.root / module.__name__ / "notes",
                STATE_DIR=self.root / module.__name__ / "state", logger=logging.getLogger("costs." + module.__name__))
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_reference_and_reported_amounts_are_independent_even_for_unknown_models(self):
        for module, normalize, raw in (
            (opencode_save, lambda model, raw: opencode_save._message_usage(dict(raw, modelID=model)),
             {"tokens": {"input": 100, "output": 10}, "cost": 7.5}),
            (pi_save, lambda model, raw: pi_save.normalize_pi_usage(raw, model),
             {"input": 100, "output": 10, "cost": {"total": 7.5}})):
            with self.subTest(module=module.__name__):
                usage = normalize("gpt-6-luna", raw)
                self.assertEqual(Decimal(usage["cost_usd"]), Decimal("0.000015"))
                self.assertEqual(Decimal(usage["reported_cost_usd"]), Decimal("7.5"))
                unknown = normalize("unregistered", raw)
                summary = module.summarize_usage([{"type": "assistant", "model": "unregistered", "usage": unknown}])
                self.assertNotIn("total_cost_usd", summary)
                self.assertEqual(summary["reported_cost"]["usd"], Decimal("7.5"))
                self.assertIn("取得コスト: $7.5", module.format_message_usage(summary))

    def test_zero_missing_invalid_and_partial_reported_costs_are_distinct(self):
        for module in (opencode_save, pi_save):
            with self.subTest(module=module.__name__):
                def normalize(cost):
                    if module is opencode_save:
                        return module._message_usage({"modelID": "gpt-6-luna", "cost": cost,
                                                      "tokens": {"input": 10, "output": 5}})
                    return module.normalize_pi_usage({"input": 10, "output": 5, "cost": {"total": cost}}, "gpt-6-luna")
                zero = normalize(0)
                self.assertEqual(zero["reported_cost_usd"], "0")
                for bad in (None, True, -1, "NaN", "Infinity", {}):
                    missing = normalize(bad)
                    self.assertNotIn("reported_cost_usd", missing)
                    summary = module.summarize_usage([{"type": "assistant", "usage": zero},
                                                      {"type": "assistant", "usage": missing}])
                    self.assertEqual(summary["reported_cost"]["usd"], 0)
                    self.assertTrue(summary["reported_cost"]["incomplete"])
                    self.assertIn("$0（部分取得）", module.build_usage_section(summary))

    def test_saved_opencode_has_both_costs_and_preserves_other_notes(self):
        opencode_save.OUTPUT_BASE.mkdir(parents=True)
        old_note = opencode_save.OUTPUT_BASE / "old-unrelated-note.md"
        old_note.write_text("過去ノート\n", encoding="utf-8")
        payload = {"session_id": "cost-session", "cwd": str(self.root), "full_sync": True,
                   "messages": [{"info": {"id": "q", "role": "user"}, "parts": [{"type": "text", "text": "質問"}]},
                                {"info": {"id": "a", "role": "assistant", "modelID": "gpt-6-luna",
                                          "tokens": {"input": 100, "output": 10}, "cost": 7.5},
                                 "parts": [{"type": "text", "text": "回答"}]}]}
        path = opencode_save.handle_payload(payload)
        content = path.read_text()
        self.assertIn('cost_basis: "reference"', content)
        self.assertIn("total_cost_usd: 0.000015", content)
        self.assertIn('reported_cost:\n  source: "opencode.cost"', content)
        self.assertIn("  usd: 7.5", content)
        self.assertIn("取得コスト: $7.5", content)
        opencode_save.handle_payload(payload)
        self.assertEqual(content, path.read_text())
        self.assertEqual(old_note.read_text(), "過去ノート\n")

    def test_claude_cost_without_quota_is_saved_before_and_after_stop_and_recovered(self):
        data = {"session_id": "claude-cost", "cost": {"total_cost_usd": 0.12}}
        self.assertEqual(claude_save.capture_statusline_quota(data), "CLI概算: $0.12")
        self.assertEqual(claude_save.load_state("claude-cost")["reported_cost"]["usd"], "0.12")
        rows = [{"type": "user", "sessionId": "claude-cost", "message": {"content": "質問"}},
                {"type": "assistant", "sessionId": "claude-cost", "message": {"id": "a", "model": "gpt-6-luna",
                 "content": "回答", "usage": {"input_tokens": 100, "output_tokens": 10}}}]
        transcript = self.root / "claude-cost.jsonl"
        transcript.write_text("".join(json.dumps(row) + "\n" for row in rows))
        hook = {"session_id": "claude-cost", "transcript_path": str(transcript), "cwd": str(self.root)}
        with mock.patch.object(claude_save, "read_stop_transcript", return_value=transcript.read_text()):
            claude_save.handle_stop_event(hook)
            state = claude_save.load_state("claude-cost")
            path = Path(state["output_path"])
            self.assertIn("total_cost_usd: 0.000015", path.read_text())
            self.assertIn("CLI報告値)**: $0.12（セッション累計）", path.read_text())
            self.assertNotIn("取得コスト:", path.read_text())  # 累計を回答calloutへ配分しない。
            data["cost"]["total_cost_usd"] = 0
            claude_save.capture_statusline_quota(data)
            content = path.read_text()
            claude_save.capture_statusline_quota(data)
            self.assertEqual(content, path.read_text())
            self.assertEqual(content.count("reported_cost:\n"), 1)
            self.assertEqual(content.count("💵 **取得コスト"), 1)
            self.assertIn("CLI報告値)**: $0（セッション累計）", content)
            claude_save.state_path("claude-cost").unlink()
            claude_save.handle_stop_event(hook)
            self.assertEqual(content, path.read_text())
            target, backup = rebuild_session.rebuild("claude", "claude-cost", transcript=transcript,
                                                    module=claude_save)
            self.assertIsNotNone(backup)
            self.assertEqual(claude_save.recover_reported_cost(target.read_text())["usd"], "0")
            self.assertIn("total_cost_usd: 0.000015", target.read_text())

    def test_reported_cost_precision_survives_note_recovery_and_summary_merge(self):
        amount = "0.1234567890123456789012345"
        report = {"source": "test", "scope": "session", "usd": amount, "observed_at": "2026-10-25T00:00:00+09:00"}
        content = "---\n" + "\n".join(claude_save.reported_cost_fields(report)) + "\n---\n"
        self.assertEqual(claude_save.recover_reported_cost(content)["usd"], amount)
        rows = [{"type": "assistant", "model": "gpt-6-luna", "usage": {
                    "input_tokens": 100, "reported_cost_source": "test", "reported_cost_usd": amount}}]
        first = pi_save.summarize_usage(rows)
        merged = pi_save._merge_usage_summaries(first, first)
        self.assertEqual(merged["reported_cost"]["usd"], Decimal(amount) * 2)
        self.assertEqual(merged["reported_cost"]["available_records"], 2)

    def test_legacy_opencode_cached_amount_is_repriced_without_inventing_reported_cost(self):
        old = {"type": "assistant", "model": "gpt-6-luna", "usage": {
            "input_tokens": 100, "output_tokens": 10, "total_tokens": 110, "cost_usd": "7.5"}}
        normalized = opencode_save.normalize_cached_costs(old)
        self.assertEqual(old["usage"]["cost_usd"], "7.5")
        self.assertEqual(normalized["usage"]["cost_usd"], "0.000015")
        self.assertNotIn("reported_cost_usd", normalized["usage"])
        self.assertIn("未取得", opencode_save.build_usage_section(opencode_save.summarize_usage([normalized])))

    def test_claude_quota_updates_preserve_cost_and_do_not_duplicate_metadata(self):
        path = claude_save.create_md_file("quota-cost", str(self.root), claude_save.now_jst(),
            usage=claude_save.summarize_usage([{"type": "assistant", "model": "gpt-6-luna", "usage": {
                "input_tokens": 100, "output_tokens": 10, "total_tokens": 110}}]))
        claude_save.save_state("quota-cost", {"output_path": str(path)})
        data = {"session_id": "quota-cost", "cost": {"total_cost_usd": 0.12},
                "rate_limits": {"five_hour": {"used_percentage": 20}}}
        label = claude_save.capture_statusline_quota(data)
        self.assertIn("80.0%", label)
        self.assertIn("$0.12", label)
        content = path.read_text()
        claude_save.capture_statusline_quota(data)
        self.assertEqual(content, path.read_text())
        self.assertIn("total_cost_usd: 0.000015", content)
        self.assertEqual(content.count("reported_cost:\n"), 1)
        self.assertEqual(content.count("💵 **取得コスト"), 1)

    def test_claude_summary_updates_preserve_cost_examples_inside_conversation(self):
        path = claude_save.create_md_file("examples", str(self.root), claude_save.now_jst())
        example = "💵 **取得コスト (CLI報告値)**: $999（セッション累計）\n"
        content = path.read_text() + "# 質問\n```text\n" + example + "```\n"
        report = {"source": "claude.statusLine.cost.total_cost_usd", "scope": "session", "usd": "0.12"}
        content = claude_save.refresh_reported_cost_metadata(content, report)
        usage = claude_save.summarize_usage([{"type": "assistant", "model": "gpt-6-luna", "usage": {
            "input_tokens": 100, "output_tokens": 10, "total_tokens": 110}}])
        usage["reported_cost"] = report
        updated = claude_save.update_markdown_metadata(content, claude_save.now_jst(), 0, 0, usage)
        self.assertIn("```text\n" + example + "```", updated)
        self.assertEqual(updated.count("💵 **取得コスト"), 2)


if __name__ == "__main__":
    unittest.main()
