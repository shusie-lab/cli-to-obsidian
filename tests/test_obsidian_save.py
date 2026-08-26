import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__) + "/.."))

import agy_save
import claude_save
import codex_save

JST = timezone(timedelta(hours=9))


class TestSaveScripts(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.test_dir = Path(self.temp_dir.name)
        codex_save.OUTPUT_BASE = self.test_dir / "codex"
        claude_save.OUTPUT_BASE = self.test_dir / "claude"
        agy_save.OUTPUT_BASE = self.test_dir / "agy"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_public_versions_match_version_file(self):
        version = (Path(__file__).parent.parent / "VERSION").read_text(
            encoding="utf-8"
        ).strip()
        for save_module in (codex_save, claude_save, agy_save):
            self.assertEqual(save_module.__version__, version)

    def test_output_dir_defaults_to_vault_relative_path(self):
        for save_module in (codex_save, claude_save, agy_save):
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("OBSIDIAN_OUTPUT_DIR", None)
                self.assertEqual(save_module.resolve_output_dir(), Path("生成AI/ChatLog"))
            with mock.patch.dict(os.environ, {"OBSIDIAN_OUTPUT_DIR": "AI/ChatLog"}):
                self.assertEqual(save_module.resolve_output_dir(), Path("AI/ChatLog"))
            self.assertEqual(save_module.resolve_output_dir("/tmp/out"), Path("生成AI/ChatLog"))
            self.assertEqual(save_module.resolve_output_dir("../outside"), Path("生成AI/ChatLog"))
            self.assertEqual(save_module.resolve_output_dir("AI/../outside"), Path("生成AI/ChatLog"))

    # ------------------------------------------------------------
    # 1. ヘッダー抽出テスト
    # ------------------------------------------------------------
    def test_callout_header_extraction(self):
        content = """---
source: codex-cli
---
<!-- last_line: 5 -->

# User: Hello
> [!QUESTION] User
> <small>⏱ 2026-08-07 10:00:00</small>
>
> Hello world

> [!NOTE] Codex
> <small>🤖 gpt-4</small>

Response text
"""
        header = codex_save.get_last_callout_header(content)
        self.assertEqual(header, "> [!NOTE] Codex\n> <small>🤖 gpt-4</small>")

    def test_user_heading_escapes_markdown_special_characters(self):
        message = "*_save.py [確認] (a) #tag"
        expected = r"\*\_save\.py \[確認\] \(a\) \#tag"

        for save_module in (codex_save, claude_save, agy_save):
            self.assertEqual(save_module.escape_markdown_heading(message), expected)

    def test_common_message_metadata_formatters(self):
        for save_module in (codex_save, claude_save, agy_save):
            self.assertEqual(
                save_module.format_message_time("2026-08-07T10:00:00"),
                "> <small>⏱ 2026-08-07 19:00:00</small>\n>\n",
            )
            self.assertEqual(
                save_module.user_heading("\n*_save.py [確認]"),
                "# User: \\*\\_save\\.py \\[確認\\]\n",
            )

    def test_atomic_write_preserves_existing_permissions(self):
        for index, save_module in enumerate((codex_save, claude_save, agy_save)):
            path = self.test_dir / f"mode-{index}.md"
            path.write_text("old", encoding="utf-8")
            path.chmod(0o640)
            save_module.atomic_write_text(path, "new")
            self.assertEqual(path.stat().st_mode & 0o777, 0o640)

    # ------------------------------------------------------------
    # 2. codex_save テスト
    # ------------------------------------------------------------
    def test_codex_save_append_and_aggregation(self):
        now = datetime.now(JST)
        md_path = codex_save.create_md_file("test-session", "myproj", now)
        
        messages1 = [
            {
                "line_number": 1,
                "type": "user_message",
                "message": "First user message",
                "timestamp": "2026-08-07T10:00:00Z",
            },
            {
                "line_number": 2,
                "type": "agent_message",
                "message": "First agent response",
                "timestamp": "2026-08-07T10:00:05Z",
            },
        ]
        appended, updated = codex_save.append_messages(md_path, messages1, now, "codex-model", 2)
        self.assertTrue(updated)
        self.assertEqual(appended, 2)

        messages2 = [
            {
                "line_number": 3,
                "type": "agent_message",
                "message": "Second agent response",
                "timestamp": "2026-08-07T10:00:10Z",
            }
        ]
        appended2, updated2 = codex_save.append_messages(md_path, messages2, now, "codex-model", 3)
        self.assertTrue(updated2)
        self.assertEqual(appended2, 0)

        content2 = md_path.read_text(encoding="utf-8")
        self.assertIn("message_count: 2", content2)
        self.assertIn("<!-- last_line: 3 -->", content2)
        self.assertEqual(content2.count("> [!NOTE] Codex"), 1)
        self.assertIn("First agent response\nSecond agent response", content2)
        self.assertNotIn("---\n\nSecond agent response", content2)

    def test_codex_quota_parsing_and_markdown(self):
        rate_limits = {
            "primary": {
                "used_percent": 25.0,
                "window_minutes": 10080,
                "resets_at": 1786695941,
            },
            "secondary": {
                "used_percent": 40.0,
                "window_minutes": 300,
                "resets_at": 1786091141,
            },
            "plan_type": "plus",
        }
        quota = codex_save.parse_quota_data(rate_limits)
        self.assertEqual(quota["codex"]["weekly"]["remaining"], 75.0)
        self.assertEqual(quota["codex"]["5h"]["remaining"], 60.0)
        self.assertEqual(codex_save.quota_window_key(43200), "30d")

        jsonl_path = self.test_dir / "quota.jsonl"
        older = {
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "rate_limits": {
                    "primary": {
                        "used_percent": 20.0,
                        "window_minutes": 43200,
                        "resets_at": 1788041187,
                    }
                },
            },
        }
        latest = {
            "type": "event_msg",
            "payload": {"type": "token_count", "rate_limits": rate_limits},
        }
        jsonl_path.write_text(
            json.dumps(older) + "\n" + json.dumps(latest) + "\n",
            encoding="utf-8",
        )
        self.assertEqual(codex_save.retrieve_quota(jsonl_path), quota)
        self.assertEqual(codex_save.retrieve_quota(jsonl_path, start_line=1), quota)

        initial_quota = {
            "codex": {
                "weekly": {"remaining": 80.0, "reset": "", "window_minutes": 10080},
                "5h": {"remaining": 65.0, "reset": "", "window_minutes": 300},
            }
        }
        now = datetime.now(JST)
        md_path = codex_save.create_md_file(
            "test-codex-quota",
            "myproj",
            now,
            initial_quota=initial_quota,
        )
        messages = [
            {
                "line_number": 1,
                "type": "agent_message",
                "message": "Quota-aware response",
                "timestamp": "2026-08-07T10:00:05Z",
            }
        ]
        appended, updated = codex_save.append_messages(
            md_path,
            messages,
            now,
            "gpt-5",
            1,
            current_quota=quota,
            last_quota=initial_quota,
            initial_quota=initial_quota,
        )
        self.assertTrue(updated)
        self.assertEqual(appended, 1)

        content = md_path.read_text(encoding="utf-8")
        self.assertIn("- **Codex**: W: 80.0% ➔ 75.0%", content)
        self.assertIn("5h: 65.0% ➔ 60.0%", content)
        self.assertIn("Quota: W 75.0(-5.00)% / 5h 60.0(-5.00)%", content)
        self.assertIn("quota: 5.00", content)

    # ------------------------------------------------------------
    # 3. claude_save テスト
    # ------------------------------------------------------------
    def test_claude_save_append_and_aggregation(self):
        now = datetime.now(JST)
        md_path = claude_save.create_md_file("test-session-claude", "myproj", now)
        
        messages1 = [
            {
                "line_number": 1,
                "type": "user_message",
                "message": "Claude question 1",
                "timestamp": "2026-08-07T10:00:00Z",
            },
            {
                "line_number": 2,
                "type": "agent_message",
                "message": "Claude answer 1",
                "timestamp": "2026-08-07T10:00:05Z",
                "model": "claude-3-5-sonnet",
            },
        ]
        appended, updated = claude_save.append_messages(md_path, messages1, now, 2)
        self.assertTrue(updated)
        self.assertEqual(appended, 2)

        messages2 = [
            {
                "line_number": 3,
                "type": "agent_message",
                "message": "Claude answer 2 (continuous)",
                "timestamp": "2026-08-07T10:00:10Z",
                "model": "claude-3-5-sonnet",
            }
        ]
        appended2, updated2 = claude_save.append_messages(md_path, messages2, now, 3)
        self.assertTrue(updated2)
        self.assertEqual(appended2, 0)

        content = md_path.read_text(encoding="utf-8")
        self.assertIn("message_count: 2", content)
        self.assertIn("<!-- last_line: 3 -->", content)
        self.assertEqual(content.count("> [!NOTE] Claude"), 1)
        self.assertIn("Claude answer 1\nClaude answer 2 (continuous)", content)
        self.assertNotIn("---\n\nClaude answer 2 (continuous)", content)

    # ------------------------------------------------------------
    # 4. agy_save テスト
    # ------------------------------------------------------------
    def test_agy_save_append_and_aggregation(self):
        now = datetime.now(JST)
        md_path = agy_save.create_md_file("test-session-agy", "myproj", now)
        
        messages1 = [
            {
                "step_index": 1,
                "type": "user_message",
                "message": "AGY question 1",
                "timestamp": "2026-08-07T10:00:00Z",
                "model": "",
            },
            {
                "step_index": 2,
                "type": "agent_message",
                "message": "AGY answer 1",
                "model": "gemini-1.5-pro",
                "timestamp": "2026-08-07T10:00:05Z",
            },
        ]
        last_id, appended, updated = agy_save.append_messages(
            md_path, messages1, now
        )
        self.assertEqual(last_id, "2")
        self.assertEqual(appended, 2)

        messages2 = [
            {
                "step_index": 3,
                "type": "agent_message",
                "message": "AGY answer 2 (continuous)",
                "model": "gemini-1.5-pro",
                "timestamp": "2026-08-07T10:00:10Z",
            }
        ]
        last_id2, appended2, updated2 = agy_save.append_messages(
            md_path, messages2, now
        )
        self.assertEqual(last_id2, "3")
        self.assertEqual(appended2, 0)

        content = md_path.read_text(encoding="utf-8")
        self.assertIn("message_count: 2", content)
        self.assertIn("<!-- last_id: 3 -->", content)
        self.assertEqual(content.count("> [!NOTE] Antigravity"), 1)
        self.assertIn("AGY answer 1\nAGY answer 2 (continuous)", content)
        self.assertNotIn("---\n\nAGY answer 2 (continuous)", content)

    # ------------------------------------------------------------
    # 5. エッジケース・追記サイクルの検証
    # ------------------------------------------------------------
    def test_empty_messages_and_skipping(self):
        now = datetime.now(JST)
        md_path = claude_save.create_md_file("test-empty", "myproj", now)

        # 空メッセージのみの追記
        empty_messages = [
            {"line_number": 1, "type": "user_message", "message": "   ", "timestamp": ""},
            {"line_number": 2, "type": "agent_message", "message": "", "timestamp": ""},
        ]
        appended, updated = claude_save.append_messages(md_path, empty_messages, now, 2)
        self.assertFalse(updated)
        self.assertEqual(appended, 0)

        content = md_path.read_text(encoding="utf-8")
        self.assertIn("message_count: 0", content)

    def test_multi_turn_alternating(self):
        now = datetime.now(JST)
        md_path = codex_save.create_md_file("test-alternating", "myproj", now)

        # ターン1
        t1 = [
            {"line_number": 1, "type": "user_message", "message": "Q1", "timestamp": "2026-08-07T10:00:00Z"},
            {"line_number": 2, "type": "agent_message", "message": "A1", "timestamp": "2026-08-07T10:00:05Z"},
        ]
        codex_save.append_messages(md_path, t1, now, "model1", 2)

        # ターン2 (User -> Agent)
        t2 = [
            {"line_number": 3, "type": "user_message", "message": "Q2", "timestamp": "2026-08-07T10:01:00Z"},
            {"line_number": 4, "type": "agent_message", "message": "A2", "timestamp": "2026-08-07T10:01:05Z"},
        ]
        codex_save.append_messages(md_path, t2, now, "model1", 4)

        content = md_path.read_text(encoding="utf-8")
        self.assertIn("message_count: 4", content)
        self.assertEqual(content.count("> [!QUESTION] User"), 2)
        self.assertEqual(content.count("> [!NOTE] Codex"), 2)
        self.assertIn("<!-- last_line: 4 -->", content)

    def test_incomplete_jsonl_last_line_is_retried(self):
        codex_path = self.test_dir / "codex_partial.jsonl"
        first = {
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "complete"},
        }
        second = {
            "type": "event_msg",
            "payload": {"type": "agent_message", "message": "completed later"},
        }
        second_json = json.dumps(second)
        split_at = len(second_json) // 2
        codex_path.write_text(
            json.dumps(first) + "\n" + second_json[:split_at],
            encoding="utf-8",
        )

        messages, last_line = codex_save.load_jsonl_messages(codex_path, 0)
        self.assertEqual([message["message"] for message in messages], ["complete"])
        self.assertEqual(last_line, 1)

        with open(codex_path, "a", encoding="utf-8") as transcript:
            transcript.write(second_json[split_at:] + "\n")
        messages, last_line = codex_save.load_jsonl_messages(codex_path, last_line)
        self.assertEqual([message["message"] for message in messages], ["completed later"])
        self.assertEqual(last_line, 2)

        claude_path = self.test_dir / "claude_partial.jsonl"
        claude_path.write_text('{"type":"user"', encoding="utf-8")
        messages, last_line = claude_save.load_jsonl_messages(claude_path, 0)
        self.assertEqual(messages, [])
        self.assertEqual(last_line, 0)

    def test_agy_accepts_mixed_numeric_step_index_types(self):
        transcript = self.test_dir / "mixed-step-index.jsonl"
        rows = [
            {"step_index": 2, "type": "PLANNER_RESPONSE", "content": "answer"},
            {"step_index": "1", "type": "USER_INPUT", "content": "question"},
        ]
        transcript.write_text(
            "".join(json.dumps(row) + "\n" for row in rows),
            encoding="utf-8",
        )

        messages = agy_save.load_jsonl_messages(transcript)
        self.assertEqual([message["message"] for message in messages], ["question", "answer"])

    def test_frontmatter_and_state_paths_are_safe(self):
        now = datetime.now(JST)
        cwd = '/tmp/project"name\nnext'
        frontmatter = codex_save.build_frontmatter('session"id', cwd, now)
        self.assertIn('session_id: "session\\\"id"', frontmatter)
        self.assertIn('project: "/tmp/project\\\"name\\nnext"', frontmatter)

        malicious_id = "../../outside/session"
        for module in (codex_save, claude_save, agy_save):
            module.STATE_DIR = self.test_dir / f"{module.AGENT_NAME}-state"
            path = module.state_path(malicious_id)
            self.assertEqual(path.parent, module.STATE_DIR)
            self.assertNotIn("..", path.name)

    def test_agy_keeps_quota_history_in_existing_markdown(self):
        now = datetime.now(JST)
        md_path = agy_save.create_md_file("test-agy-history", "myproj", now)
        content = md_path.read_text(encoding="utf-8")
        content = content.replace("message_count: 0", "message_count: 0\nquota: 5.00")
        content = content.replace(
            "<!-- last_id: -->",
            "📊 **Quota**:\n- **Gemini**: W: 80.0% ➔ 75.0%\n\n<!-- last_id: -->",
        )
        agy_save.atomic_write_md(md_path, content)
        agy_save.append_messages(
            md_path,
            [{
                "step_index": 1,
                "type": "agent_message",
                "message": "A later response",
                "model": "gemini-2.5-pro",
                "timestamp": "2026-08-07T10:00:05Z",
            }],
            now,
        )
        updated = md_path.read_text(encoding="utf-8")
        self.assertIn("quota: 5.00", updated)
        self.assertIn("- **Gemini**: W: 80.0% ➔ 75.0%", updated)

    def test_agy_append_uses_single_atomic_markdown_write(self):
        now = datetime.now(JST)
        md_path = agy_save.create_md_file("atomic-agy", "project", now)
        messages = [
            {
                "step_index": 1,
                "type": "agent_message",
                "message": "Atomic response",
                "model": "gemini-test",
                "timestamp": "2026-08-07T10:00:05Z",
            }
        ]

        with mock.patch.object(
            agy_save,
            "atomic_write_md",
            wraps=agy_save.atomic_write_md,
        ) as atomic_write:
            agy_save.append_messages(md_path, messages, now)

        self.assertEqual(atomic_write.call_count, 1)
        content = md_path.read_text(encoding="utf-8")
        self.assertIn("Atomic response", content)
        self.assertIn("<!-- last_id: 1 -->", content)


if __name__ == "__main__":
    unittest.main()
