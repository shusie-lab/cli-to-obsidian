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
import opencode_save

JST = timezone(timedelta(hours=9))


class TestSaveScripts(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.test_dir = Path(self.temp_dir.name)
        codex_save.OUTPUT_BASE = self.test_dir / "codex"
        claude_save.OUTPUT_BASE = self.test_dir / "claude"
        agy_save.OUTPUT_BASE = self.test_dir / "agy"
        codex_save.STATE_DIR = self.test_dir / "codex_state"
        claude_save.STATE_DIR = self.test_dir / "claude_state"
        agy_save.STATE_DIR = self.test_dir / "agy_state"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_public_versions_match_version_file(self):
        version = (Path(__file__).parent.parent / "VERSION").read_text(
            encoding="utf-8"
        ).strip()
        for save_module in (codex_save, claude_save, agy_save, opencode_save):
            self.assertEqual(save_module.__version__, version)

    def test_output_dir_defaults_to_vault_relative_path(self):
        for save_module in (codex_save, claude_save, agy_save, opencode_save):
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

    def test_codex_structured_user_message_detection_is_strict(self):
        request = "実際に表示する依頼\n2行目"
        referenced = "## Referenced ChatGPT conversation:\n\n過去の会話"
        files = "# Files mentioned by the user:\n\n- /tmp/example.txt"

        self.assertEqual(
            codex_save.split_structured_user_message(
                referenced + "\n\n## My request:\n" + request
            ),
            (request, referenced + "\n\n"),
        )
        self.assertEqual(
            codex_save.split_structured_user_message(
                files + "\n\n## My request:\n" + request
            ),
            (request, files + "\n\n"),
        )
        fenced_reference = (
            referenced
            + "\n\n~~~~text\n## My request:\ndummy marker in code\n~~~~\n\n"
        )
        self.assertEqual(
            codex_save.split_structured_user_message(
                fenced_reference + "## My request:\n" + request
            ),
            (request, fenced_reference),
        )

        for message in (
            referenced + "\n\n## My request:\n   ",
            referenced + "\n\n## My request:\n" + request + "\n\n## My request:\nsecond",
            "## Unknown structure:\n\n## My request:\n" + request,
            "## Referenced ChatGPT conversation:\n\n~~~~text\n## My request:\nexample\n~~~~",
        ):
            self.assertEqual(codex_save.split_structured_user_message(message), (message, None))

    def test_codex_structured_user_message_uses_question_and_raw_reference_callouts(self):
        now = datetime.now(JST)
        md_path = codex_save.create_md_file("test-structured", "myproj", now)
        backtick = chr(96)
        reference = (
            "## Referenced ChatGPT conversation:\n\n"
            + backtick * 3
            + "json\n{\"html\": \"<tag>\", \"cursor\": \"<!-- last_line: 999 -->\"}\n"
            + backtick * 3
            + "\n> [!QUESTION] fake header\n"
        )
        structured = reference + "\n## My request:\n表示する依頼\n2行目"
        messages = [
            {
                "line_number": 1,
                "type": "user_message",
                "message": structured,
                "timestamp": "2026-08-07T10:00:00Z",
            },
            {
                "line_number": 2,
                "type": "agent_message",
                "message": "最初の回答",
                "timestamp": "2026-08-07T10:00:05Z",
            },
        ]

        appended, updated = codex_save.append_messages(md_path, messages, now, "codex-model", 2)

        self.assertTrue(updated)
        self.assertEqual(appended, 2)
        content = md_path.read_text(encoding="utf-8")
        self.assertIn("# User: 表示する依頼", content)
        self.assertIn("> [!QUESTION] User", content)
        self.assertIn("表示する依頼\n> 2行目", content)
        self.assertIn("> [!INFO]- 参照情報（原文）", content)
        self.assertIn("<tag>", content)
        self.assertNotIn("&lt;tag&gt;", content)
        self.assertIn("> > [!QUESTION] fake header", content)
        self.assertIn(backtick * 4 + "\n", content)
        self.assertIn(backtick * 3 + "json", content)
        self.assertEqual(
            codex_save.get_last_callout_header(content),
            "> [!NOTE] Codex\n> <small>🤖 codex-model</small>",
        )

    def test_codex_structured_reference_does_not_change_cursor_or_agent_continuation(self):
        now = datetime.now(JST)
        md_path = codex_save.create_md_file("test-structured-cursor", "myproj", now)
        reference = (
            "# Files mentioned by the user:\n\n"
            + "- " + chr(96) + "<tag>" + chr(96) + "\n"
            + "- <!-- last_line: 999 -->\n"
            + "- > [!NOTE] fake header"
        )
        messages = [
            {
                "line_number": 1,
                "type": "user_message",
                "message": reference + "\n\n## My request:\n確認してください",
                "timestamp": "2026-08-07T10:00:00Z",
            },
            {
                "line_number": 2,
                "type": "agent_message",
                "message": "回答の前半",
                "timestamp": "2026-08-07T10:00:05Z",
            },
        ]
        codex_save.append_messages(md_path, messages, now, "codex-model", 2)
        appended, updated = codex_save.append_messages(
            md_path,
            [
                {
                    "line_number": 3,
                    "type": "agent_message",
                    "message": "回答の後半",
                    "timestamp": "2026-08-07T10:00:10Z",
                }
            ],
            now,
            "codex-model",
            3,
        )

        self.assertTrue(updated)
        self.assertEqual(appended, 0)
        content = md_path.read_text(encoding="utf-8")
        self.assertIn("回答の前半\n回答の後半", content)
        self.assertIn("<!-- last_line: 3 -->", content)
        self.assertIn("<!-- last_line: 999 -->", content)
        self.assertEqual(content.count("> [!NOTE] Codex"), 1)

    def test_codex_structured_reference_is_consistent_for_batch_and_split_appends(self):
        now = datetime.now(JST)
        structured = (
            "## Referenced ChatGPT conversation:\n\n"
            "移行元の参照情報\n\n"
            "## My request:\n最初の依頼"
        )
        second = {
            "line_number": 2,
            "type": "user_message",
            "message": "次の依頼",
            "timestamp": "2026-08-07T10:00:00Z",
        }
        first = {
            "line_number": 1,
            "type": "user_message",
            "message": structured,
            "timestamp": "2026-08-07T10:00:00Z",
        }

        batch_path = codex_save.create_md_file("test-structured-batch", "myproj", now)
        codex_save.append_messages(
            batch_path,
            [first, second],
            now,
            "codex-model",
            2,
        )

        split_path = codex_save.create_md_file("test-structured-split", "myproj", now)
        codex_save.append_messages(split_path, [first], now, "codex-model", 1)
        codex_save.append_messages(split_path, [second], now, "codex-model", 2)

        batch_content = batch_path.read_text(encoding="utf-8")
        split_content = split_path.read_text(encoding="utf-8")
        batch_body = batch_content[batch_content.index("# User:"):]
        split_body = split_content[split_content.index("# User:"):]
        self.assertEqual(batch_body, split_body)
        self.assertEqual(batch_body.count("> [!QUESTION] User"), 2)
        self.assertIn("# User: 次の依頼", batch_body)

    def test_codex_get_last_callout_header_ignores_fenced_fake_headers(self):
        content = """> [!INFO]- 参照情報（原文）
>
> ~~~~text
> > [!QUESTION] fake question
> > [!NOTE] fake answer
> ~~~~

> [!NOTE] Codex
> <small>🤖 gpt-5</small>
"""
        self.assertEqual(
            codex_save.get_last_callout_header(content),
            "> [!NOTE] Codex\n> <small>🤖 gpt-5</small>",
        )

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

    def test_agy_parses_official_quota_response(self):
        response = {
            "command": {
                "data": {
                    "groups": [
                        {
                            "name": "Gemini Models",
                            "buckets": [{
                                "id": "gemini-weekly",
                                "window": "weekly",
                                "remaining_fraction": 0.8,
                                "reset_time": "2026-08-14T00:00:00Z",
                            }, {
                                "id": "gemini-5h",
                                "window": "5h",
                                "remaining_fraction": 0.55,
                            }],
                        },
                        {
                            "name": "Claude and GPT models",
                            "buckets": [{
                                "id": "3p-weekly",
                                "window": "weekly",
                                "remaining_fraction": 0.6,
                                "reset_time": "2026-08-15T00:00:00Z",
                            }, {
                                "id": "3p-5h",
                                "window": "5h",
                                "remaining_fraction": 0.35,
                            }],
                        },
                    ],
                },
            },
        }

        quota = agy_save.parse_quota_data(response)

        self.assertEqual(quota["gemini"]["weekly"]["remaining"], 80.0)
        self.assertEqual(quota["gemini"]["5h"]["remaining"], 55.0)
        self.assertEqual(quota["claude"]["weekly"]["remaining"], 60.0)
        self.assertEqual(quota["claude"]["5h"]["remaining"], 35.0)
        self.assertEqual(quota["gemini"]["weekly"]["reset"], "2026-08-14 09:00")

    def test_agy_retrieve_quota_uses_read_only_official_command(self):
        response = {
            "status": "SUCCESS",
            "num_turns": 0,
            "command": {"data": {"groups": [{
                "name": "Gemini Models",
                "buckets": [{
                    "id": "gemini-weekly",
                    "window": "weekly",
                    "remaining_fraction": 0.8,
                }],
            }]}},
        }
        completed = mock.Mock(returncode=0, stdout=json.dumps(response))

        with mock.patch.object(agy_save.subprocess, "run", return_value=completed) as run:
            quota = agy_save.retrieve_quota()

        self.assertEqual(quota["gemini"]["weekly"]["remaining"], 80.0)
        self.assertEqual(
            run.call_args.args[0],
            ["agy", "--output-format", "json", "--print=/quota"],
        )
        self.assertEqual(run.call_args.kwargs["timeout"], agy_save.AGY_QUOTA_TIMEOUT_SECONDS)

    def test_agy_rejects_malformed_quota_responses_without_raising(self):
        malformed_values = [
            [],
            None,
            {"command": {"data": {"groups": None}}},
            {"command": {"data": {"groups": [{"name": "Gemini", "buckets": None}]}}},
            {"command": {"data": {"groups": [{
                "name": "Gemini",
                "buckets": [{"id": "gemini-weekly", "remaining_fraction": "NaN"}],
            }]}}},
            {"command": {"data": {"groups": [{
                "name": "Gemini",
                "buckets": [{"id": "gemini-weekly", "remaining_fraction": "Infinity"}],
            }]}}},
        ]
        for value in malformed_values:
            self.assertEqual(agy_save.parse_quota_data(value), {})

        for stdout in (b"\xff", "[]"):
            completed = mock.Mock(returncode=0, stdout=stdout)
            with mock.patch.object(agy_save.subprocess, "run", return_value=completed):
                self.assertIsNone(agy_save.retrieve_quota())

    def test_agy_malformed_quota_does_not_block_conversation_save(self):
        transcript = self.test_dir / "agy-malformed-quota.jsonl"
        transcript.write_text("".join(json.dumps(row) + "\n" for row in [
            {"step_index": 1, "type": "USER_INPUT", "content": "<USER_REQUEST>質問</USER_REQUEST>"},
            {"step_index": 2, "type": "PLANNER_RESPONSE", "content": "回答", "tool_calls": []},
        ]), encoding="utf-8")
        completed = mock.Mock(returncode=0, stdout=json.dumps([]))
        hook = {
            "conversationId": "agy-malformed-quota",
            "transcriptPath": str(transcript),
            "workspacePaths": [str(self.test_dir)],
        }
        with mock.patch.object(agy_save.subprocess, "run", return_value=completed):
            agy_save.handle_stop_event(hook)

        state = agy_save.load_state(hook["conversationId"])
        content = Path(state["output_path"]).read_text(encoding="utf-8")
        self.assertIn("質問", content)
        self.assertIn("回答", content)
        self.assertNotIn("📊 **Quota**:", content)

    def test_agy_quota_is_saved_in_public_saver(self):
        def quota_response(weekly_remaining, five_hour_remaining):
            return mock.Mock(returncode=0, stdout=json.dumps({
                "status": "SUCCESS",
                "num_turns": 0,
                "command": {"data": {"groups": [{
                    "name": "Gemini Models",
                    "buckets": [
                        {
                            "id": "gemini-weekly",
                            "window": "weekly",
                            "remaining_fraction": weekly_remaining / 100,
                        },
                        {
                            "id": "gemini-5h",
                            "window": "5h",
                            "remaining_fraction": five_hour_remaining / 100,
                        },
                    ],
                }]}},
            }))

        transcript = self.test_dir / "agy_quota.jsonl"
        rows = [
            {"step_index": 1, "type": "USER_INPUT", "content": "<USER_REQUEST>質問</USER_REQUEST>"},
            {"step_index": 2, "type": "PLANNER_RESPONSE", "content": "回答", "tool_calls": []},
        ]
        transcript.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        hook = {"conversationId": "public-agy-quota", "transcriptPath": str(transcript)}

        with mock.patch.object(
            agy_save.subprocess,
            "run",
            side_effect=[quota_response(90, 60), quota_response(80, 50)],
        ):
            agy_save.handle_stop_event(hook)
            rows.extend([
                {"step_index": 3, "type": "USER_INPUT", "content": "<USER_REQUEST>次の質問</USER_REQUEST>"},
                {"step_index": 4, "type": "PLANNER_RESPONSE", "content": "次の回答", "tool_calls": []},
            ])
            transcript.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
            agy_save.handle_stop_event(hook)

        state = agy_save.load_state(hook["conversationId"])
        content = Path(state["output_path"]).read_text(encoding="utf-8")
        self.assertEqual(state["initial_quota"]["gemini"]["weekly"]["remaining"], 90.0)
        self.assertEqual(state["last_quota"]["gemini"]["weekly"]["remaining"], 80.0)
        self.assertEqual(state["initial_quota"]["gemini"]["5h"]["remaining"], 60.0)
        self.assertEqual(state["last_quota"]["gemini"]["5h"]["remaining"], 50.0)
        self.assertIn("quota: 10.00", content)
        self.assertIn("W: 90.0% ➔ 80.0% / 5h: 60.0% ➔ 50.0%", content)
        self.assertEqual(
            agy_save.quota_suffix("gemini", state["last_quota"], state["initial_quota"]),
            " (Quota: W 80.0(-10.00)% / 5h 50.0(-10.00)%)",
        )

    def test_agy_without_new_messages_does_not_refresh_quota(self):
        now = datetime.now(JST)
        md_path = agy_save.create_md_file("agy-no-new", "myproj", now)
        transcript = self.test_dir / "agy-no-new.jsonl"
        transcript.write_text(
            json.dumps({
                "step_index": 1,
                "type": "PLANNER_RESPONSE",
                "content": "既存回答",
            }) + "\n",
            encoding="utf-8",
        )
        hook = {
            "conversationId": "agy-no-new",
            "transcriptPath": str(transcript),
            "workspacePaths": [str(self.test_dir)],
        }
        with mock.patch.object(agy_save, "retrieve_quota", return_value={"gemini": {
            "weekly": {"remaining": 90.0, "reset": ""},
        }}) as retrieve:
            agy_save.handle_stop_event(hook)
        self.assertEqual(retrieve.call_count, 1)
        state = agy_save.load_state(hook["conversationId"])
        content_before = Path(state["output_path"]).read_text(encoding="utf-8")

        with mock.patch.object(agy_save, "retrieve_quota", side_effect=AssertionError("quota refresh")):
            agy_save.handle_stop_event(hook)

        state_after = agy_save.load_state(hook["conversationId"])
        self.assertEqual(state_after.get("last_quota"), state.get("last_quota"))
        self.assertEqual(Path(state_after["output_path"]).read_text(encoding="utf-8"), content_before)

    def test_agy_recovers_current_and_legacy_claude_quota_history(self):
        content = """---
source: antigravity-cli
session_id: \"recover-quota\"
quota: 10.00
quota_claude: 5.00
---

📊 **Quota**:
- **Gemini**: W: 90.0% ➔ 80.0% / 5h: 60.0% ➔ 50.0%
- <small>Claude: W: 70.0% / 5h: 40.0%</small>

<!-- last_id: 4 -->
"""
        initial, final = agy_save.recover_quota_from_markdown(content)
        self.assertEqual(initial["gemini"]["weekly"]["remaining"], 90.0)
        self.assertEqual(final["gemini"]["5h"]["remaining"], 50.0)
        self.assertEqual(initial["claude"]["weekly"]["remaining"], 75.0)
        self.assertEqual(final["claude"]["weekly"]["remaining"], 70.0)
        self.assertEqual(initial["claude"]["5h"]["remaining"], 40.0)
        self.assertEqual(
            agy_save.recover_quota_from_markdown(content.replace("quota: 10.00", "quota: 10.04")),
            (None, None),
        )

    def test_agy_state_recovery_preserves_quota_initial_and_consumption(self):
        session_id = "agy-quota-recovery"
        transcript = self.test_dir / "agy-quota-recovery.jsonl"
        rows = [
            {"step_index": 1, "type": "USER_INPUT", "content": "<USER_REQUEST>最初の質問</USER_REQUEST>"},
            {"step_index": 2, "type": "PLANNER_RESPONSE", "content": "最初の回答", "tool_calls": []},
        ]
        transcript.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        hook = {
            "conversationId": session_id,
            "transcriptPath": str(transcript),
            "workspacePaths": [str(self.test_dir)],
        }

        def quota_response(remaining):
            return mock.Mock(returncode=0, stdout=json.dumps({
                "status": "SUCCESS",
                "num_turns": 0,
                "command": {"data": {"groups": [{
                    "name": "Gemini Models",
                    "buckets": [{
                        "id": "gemini-weekly",
                        "window": "weekly",
                        "remaining_fraction": remaining / 100,
                    }],
                }]}},
            }))

        with mock.patch.object(agy_save.subprocess, "run", return_value=quota_response(90)):
            agy_save.handle_stop_event(hook)
        output_path = Path(agy_save.load_state(session_id)["output_path"])
        agy_save.state_path(session_id).unlink()

        rows.extend([
            {"step_index": 3, "type": "USER_INPUT", "content": "<USER_REQUEST>次の質問</USER_REQUEST>"},
            {"step_index": 4, "type": "PLANNER_RESPONSE", "content": "次の回答", "tool_calls": []},
        ])
        transcript.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        with mock.patch.object(agy_save.subprocess, "run", return_value=quota_response(80)):
            agy_save.handle_stop_event(hook)

        state = agy_save.load_state(session_id)
        content = output_path.read_text(encoding="utf-8")
        self.assertEqual(state["initial_quota"]["gemini"]["weekly"]["remaining"], 90.0)
        self.assertEqual(state["final_quota"]["gemini"]["weekly"]["remaining"], 80.0)
        self.assertIn("quota: 10.00", content)
        self.assertIn("W: 90.0% ➔ 80.0%", content)
        self.assertEqual(content.count("W: 90.0% ➔ 80.0%"), 1)

    def test_agy_quota_section_replacement_removes_old_rows(self):
        content = """---
source: antigravity-cli
quota: 5.00
---

📊 **Quota**:
- **Gemini**: W: 80.0% ➔ 75.0%

本文
"""
        updated = agy_save.add_quota_metadata(
            content,
            {"gemini": {"weekly": {"remaining": 80.0, "reset": ""}}},
            {"gemini": {"weekly": {"remaining": 70.0, "reset": ""}}},
        )
        self.assertNotIn("80.0% ➔ 75.0%", updated)
        self.assertEqual(updated.count("- **Gemini**: W:"), 1)
        self.assertIn("80.0% ➔ 70.0%", updated)

    def test_agy_unreadable_quota_history_is_preserved(self):
        fake_content = """---
source: antigravity-cli
quota: not-a-number
---

本文中の例:
📊 **Quota**:
- **Gemini**: W: 1.0%

本文
"""
        self.assertEqual(agy_save.recover_quota_from_markdown(fake_content), (None, None))
        body_only_fake = fake_content.replace("quota: not-a-number\n", "")
        self.assertFalse(agy_save.has_quota_history(body_only_fake))

        content = """---
source: antigravity-cli
quota: not-a-number
---

📊 **Quota**:
- **Gemini**: W: unknown / 5h: unknown

本文
"""
        self.assertEqual(agy_save.recover_quota_from_markdown(content), (None, None))
        updated = agy_save.add_quota_metadata(
            content,
            None,
            {"gemini": {"weekly": {"remaining": 70.0, "reset": ""}}},
        )
        self.assertEqual(updated, content)

    def test_agy_partial_quota_history_is_preserved_after_state_loss(self):
        session_id = "agy-partial-quota-recovery"
        transcript = self.test_dir / "agy-partial-quota-recovery.jsonl"
        transcript.write_text("".join(json.dumps(row) + "\n" for row in [
            {"step_index": 1, "type": "USER_INPUT", "content": "<USER_REQUEST>旧質問</USER_REQUEST>"},
            {"step_index": 2, "type": "PLANNER_RESPONSE", "content": "旧回答", "tool_calls": []},
            {"step_index": 3, "type": "USER_INPUT", "content": "<USER_REQUEST>新質問</USER_REQUEST>"},
            {"step_index": 4, "type": "PLANNER_RESPONSE", "content": "新回答", "tool_calls": []},
        ]), encoding="utf-8")
        existing = agy_save.create_md_file(session_id, str(self.test_dir), datetime.now(JST))
        existing_content = existing.read_text(encoding="utf-8").replace(
            "<!-- last_id: -->",
            "📊 **Quota**:\n"
            "- **Gemini**: W: 80.0% ➔ 70.0% / 5h: unavailable\n"
            "\n<!-- last_id: 2 -->",
        )
        agy_save.atomic_write_md(existing, existing_content)
        hook = {
            "conversationId": session_id,
            "transcriptPath": str(transcript),
            "workspacePaths": [str(self.test_dir)],
        }
        current = {"gemini": {"weekly": {"remaining": 60.0, "reset": ""}}}
        with mock.patch.object(agy_save, "retrieve_quota", return_value=current):
            agy_save.handle_stop_event(hook)

        state = agy_save.load_state(session_id)
        updated = existing.read_text(encoding="utf-8")
        self.assertTrue(state["preserve_quota_history"])
        self.assertNotIn("quota: 0.00", updated)
        self.assertIn("W: 80.0% ➔ 70.0% / 5h: unavailable", updated)
        self.assertIn("新回答", updated)

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

    def test_session_id_collision_avoidance_across_scripts(self):
        now = datetime.now(JST)
        for module, name in ((codex_save, "codex"), (claude_save, "claude"), (agy_save, "agy")):
            ses_a = "test_col_00000001"
            ses_b = "test_col_00000002"
            path_a = module.create_md_file(ses_a, "myproj", now)
            path_b = module.create_md_file(ses_b, "myproj", now)
            self.assertNotEqual(path_a, path_b, f"{name} should not collide for sessions with same 8-char prefix")
            self.assertTrue(path_a.name.endswith(f"{module.safe_session_key(ses_a)}.md"))
            self.assertTrue(path_b.name.endswith(f"{module.safe_session_key(ses_b)}.md"))

    def test_find_existing_md_supports_legacy_short_id(self):
        for module, source in ((codex_save, "codex-cli"), (claude_save, "claude-code"), (agy_save, "antigravity-cli")):
            session_id = f"legacy_session_{module.__name__}"
            short_id = module.safe_filename_component(session_id[:8])
            legacy_filename = f"20260101_120000_myproj_{short_id}.md"
            legacy_path = module.OUTPUT_BASE / legacy_filename
            content = (
                "---\n"
                f"source: {source}\n"
                f"session_id: {module.yaml_quote(session_id)}\n"
                "---\n"
            )
            module.OUTPUT_BASE.mkdir(parents=True, exist_ok=True)
            module.atomic_write_text(legacy_path, content)

            found = module.find_existing_md(session_id)
            self.assertEqual(found, legacy_path, f"{module.__name__} should find legacy short-id file")

    def test_find_existing_md_error_handling(self):
        for module in (codex_save, claude_save, agy_save):
            session_id = f"err_session_{module.__name__}"
            module.OUTPUT_BASE.mkdir(parents=True, exist_ok=True)

            # 1. glob が OSError を起こした場合は ExistingMarkdownSearchError
            with mock.patch.object(module.Path, "iterdir", side_effect=OSError("directory failure")):
                with self.assertRaises(module.ExistingMarkdownSearchError):
                    module.find_existing_md(session_id)

            # 2. 読めない候補が存在し、他に一致候補がない場合は ExistingMarkdownSearchError
            bad_path = module.OUTPUT_BASE / f"20260101_120000_myproj_{module.safe_session_key(session_id)}.md"
            bad_path.write_text("dummy", encoding="utf-8")
            with mock.patch.object(module.Path, "read_text", side_effect=OSError("permission denied")):
                with self.assertRaises(module.ExistingMarkdownSearchError):
                    module.find_existing_md(session_id)
            bad_path.unlink()

            # 3. 読めない候補があっても別の候補で一致が確認できれば正常復帰
            matching_path = module.OUTPUT_BASE / f"20260102_120000_myproj_{module.safe_session_key(session_id)}.md"
            matching_path.write_text(f"---\nsession_id: {module.yaml_quote(session_id)}\n---\n", encoding="utf-8")
            other_bad_path = module.OUTPUT_BASE / f"20260101_120000_myproj_{module.safe_filename_component(session_id[:8])}.md"
            other_bad_path.write_text("dummy", encoding="utf-8")
            os.chmod(other_bad_path, 0o000)

            try:
                found = module.find_existing_md(session_id)
                self.assertEqual(found, matching_path)
            finally:
                os.chmod(other_bad_path, 0o644)
                matching_path.unlink(missing_ok=True)
                other_bad_path.unlink(missing_ok=True)

    def test_cleanup_old_states_and_locks(self):
        import fcntl
        for module in (codex_save, claude_save, agy_save):
            module.STATE_DIR.mkdir(parents=True, exist_ok=True)
            old_time = (datetime.now(JST) - timedelta(days=35)).isoformat()
            new_time = (datetime.now(JST) - timedelta(days=5)).isoformat()

            # 1. 削除対象の古い state
            old_ses = f"old_ses_{module.__name__}"
            old_state_file = module.state_path(old_ses)
            old_state_file.write_text(json.dumps({"last_used_at": old_time}), encoding="utf-8")
            os.utime(old_state_file, (1000000, 1000000))

            # 2. 保持対象の新しい state
            new_ses = f"new_ses_{module.__name__}"
            new_state_file = module.state_path(new_ses)
            new_state_file.write_text(json.dumps({"last_used_at": new_time}), encoding="utf-8")

            # 3. 古いがロック中の state
            locked_ses = f"locked_ses_{module.__name__}"
            locked_state_file = module.state_path(locked_ses)
            locked_state_file.write_text(json.dumps({"last_used_at": old_time}), encoding="utf-8")
            os.utime(locked_state_file, (1000000, 1000000))

            lock_dir = module.STATE_DIR / ".locks"
            lock_dir.mkdir(parents=True, exist_ok=True)
            lock_path = lock_dir / f"{module.safe_session_key(locked_ses)}.lock"
            with open(lock_path, "a", encoding="utf-8") as stream:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
                module.cleanup_old_states()
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

            self.assertFalse(old_state_file.exists(), f"Old state should be deleted for {module.__name__}")
            self.assertTrue(new_state_file.exists(), f"New state should be kept for {module.__name__}")
            self.assertTrue(locked_state_file.exists(), f"Locked state should be protected for {module.__name__}")

            new_state_file.unlink(missing_ok=True)
            locked_state_file.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
