import os
import sys
import tempfile
import unittest
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__) + "/.."))

import codex_save
import claude_save
import agy_save

class TestIntegration(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.test_dir = Path(self.temp_dir.name)
        
        # Override paths
        codex_save.OUTPUT_BASE = self.test_dir / "codex_out"
        codex_save.STATE_DIR = self.test_dir / "codex_state"
        
        claude_save.OUTPUT_BASE = self.test_dir / "claude_out"
        claude_save.STATE_DIR = self.test_dir / "claude_state"
        
        agy_save.OUTPUT_BASE = self.test_dir / "agy_out"
        agy_save.STATE_DIR = self.test_dir / "agy_state"
        
    def tearDown(self):
        self.temp_dir.cleanup()

    def test_codex_full_lifecycle(self):
        session_id = "test_codex_session"
        jsonl_path = self.test_dir / "transcript_codex.jsonl"
        
        # Write first line
        msg1 = {
            "type": "event_msg",
            "timestamp": "2026-08-07T10:00:00Z",
            "payload": {"type": "user_message", "message": "Hi Codex"}
        }
        msg2 = {
            "type": "event_msg",
            "timestamp": "2026-08-07T10:00:05Z",
            "payload": {"type": "agent_message", "message": "Hello from Codex!"}
        }
        jsonl_path.write_text(json.dumps(msg1) + "\n" + json.dumps(msg2) + "\n")
        
        hook_input = {
            "session_id": session_id,
            "transcript_path": str(jsonl_path),
            "cwd": str(self.test_dir),
            "model": "codex-v1"
        }
        
        codex_save.handle_stop_event(hook_input)
        
        state_file = codex_save.state_path(session_id)
        self.assertTrue(state_file.exists())
        
        state = codex_save.load_state(session_id)
        out_md = Path(state["output_path"])
        self.assertTrue(out_md.exists())
        
        content = out_md.read_text()
        self.assertIn("message_count: 2", content)
        self.assertIn("Hi Codex", content)
        self.assertIn("Hello from Codex!", content)
        
        # Second turn (appending)
        msg3 = {
            "type": "event_msg",
            "timestamp": "2026-08-07T10:05:00Z",
            "payload": {"type": "user_message", "message": "Follow up"}
        }
        with open(jsonl_path, "a") as f:
            f.write(json.dumps(msg3) + "\n")
            
        codex_save.handle_stop_event(hook_input)
        
        content2 = out_md.read_text()
        self.assertIn("message_count: 3", content2)
        self.assertIn("Follow up", content2)

    def test_codex_item_completed_message_format(self):
        session_id = "test_codex_item_completed"
        jsonl_path = self.test_dir / "transcript_codex_item_completed.jsonl"
        events = [
            {
                "type": "turn_context",
                "payload": {"model": "gpt-5", "effort": "medium"},
            },
            {
                "type": "event_msg",
                "timestamp": "2026-08-10T01:00:00Z",
                "payload": {
                    "type": "item_completed",
                    "item": {
                        "type": "UserMessage",
                        "content": [{"type": "text", "text": "新形式の質問"}],
                    },
                },
            },
            {
                "type": "event_msg",
                "timestamp": "2026-08-10T01:00:05Z",
                "payload": {
                    "type": "item_completed",
                    "item": {
                        "type": "AgentMessage",
                        "content": [{"type": "Text", "text": "新形式の回答"}],
                    },
                },
            },
        ]
        jsonl_path.write_text(
            "".join(json.dumps(event) + "\n" for event in events),
            encoding="utf-8",
        )

        codex_save.handle_stop_event(
            {
                "session_id": session_id,
                "transcript_path": str(jsonl_path),
                "cwd": str(self.test_dir),
            }
        )

        output_path = Path(codex_save.load_state(session_id)["output_path"])
        content = output_path.read_text(encoding="utf-8")
        self.assertIn("新形式の質問", content)
        self.assertIn("新形式の回答", content)
        self.assertIn("message_count: 2", content)
        self.assertIn("<!-- last_line: 3 -->", content)

    def test_codex_uses_transcript_source_and_effort(self):
        session_id = "test_codex_app_metadata"
        jsonl_path = self.test_dir / "transcript_codex_app.jsonl"
        events = [
            {
                "type": "session_meta",
                "payload": {
                    "source": "vscode",
                    "originator": "Codex Desktop",
                },
            },
            {
                "type": "turn_context",
                "payload": {
                    "model": "gpt-5.6-sol",
                    "effort": "medium",
                },
            },
            {
                "type": "event_msg",
                "timestamp": "2026-08-08T10:00:00Z",
                "payload": {"type": "user_message", "message": "App question"},
            },
            {
                "type": "event_msg",
                "timestamp": "2026-08-08T10:00:05Z",
                "payload": {"type": "agent_message", "message": "App answer"},
            },
        ]
        jsonl_path.write_text(
            "".join(json.dumps(event) + "\n" for event in events),
            encoding="utf-8",
        )

        codex_save.handle_stop_event(
            {
                "session_id": session_id,
                "transcript_path": str(jsonl_path),
                "cwd": str(self.test_dir),
            }
        )

        output_path = Path(codex_save.load_state(session_id)["output_path"])
        content = output_path.read_text(encoding="utf-8")
        self.assertIn("source: codex-app", content)
        self.assertIn("  - codex-app", content)
        self.assertIn("🤖 gpt-5.6-sol-medium", content)

    def test_codex_projectless_app_uses_stable_project_name(self):
        session_id = "test_codex_projectless"
        jsonl_path = self.test_dir / "transcript_codex_projectless.jsonl"
        projectless_cwd = "/Users/test/Documents/Codex/2026-08-09/co"
        events = [
            {
                "type": "session_meta",
                "payload": {
                    "source": "vscode",
                    "originator": "Codex Desktop",
                },
            },
            {
                "type": "turn_context",
                "payload": {
                    "cwd": projectless_cwd,
                    "workspace_roots": [
                        "/Users/test/Documents/Codex",
                        projectless_cwd,
                    ],
                },
            },
            {
                "type": "event_msg",
                "payload": {"type": "agent_message", "message": "Projectless answer"},
            },
        ]
        jsonl_path.write_text(
            "".join(json.dumps(event) + "\n" for event in events),
            encoding="utf-8",
        )

        codex_save.handle_stop_event(
            {
                "session_id": session_id,
                "transcript_path": str(jsonl_path),
                "cwd": projectless_cwd,
            }
        )

        output_path = Path(codex_save.load_state(session_id)["output_path"])
        self.assertEqual(output_path.name.split("_")[2], "projectless")
        content = output_path.read_text(encoding="utf-8")
        self.assertIn('project: "projectless"', content)

    def test_codex_cli_source_is_preserved(self):
        session_id = "test_codex_cli_metadata"
        jsonl_path = self.test_dir / "transcript_codex_cli.jsonl"
        events = [
            {
                "type": "session_meta",
                "payload": {"source": "cli", "originator": "codex-tui"},
            },
            {
                "type": "turn_context",
                "payload": {"model": "gpt-5.6-sol", "effort": "high"},
            },
            {
                "type": "event_msg",
                "payload": {"type": "agent_message", "message": "CLI answer"},
            },
        ]
        jsonl_path.write_text(
            "".join(json.dumps(event) + "\n" for event in events),
            encoding="utf-8",
        )

        codex_save.handle_stop_event(
            {
                "session_id": session_id,
                "transcript_path": str(jsonl_path),
                "cwd": str(self.test_dir),
            }
        )

        output_path = Path(codex_save.load_state(session_id)["output_path"])
        content = output_path.read_text(encoding="utf-8")
        self.assertIn("source: codex-cli", content)
        self.assertIn("🤖 gpt-5.6-sol-high", content)

    def test_codex_quota_lifecycle(self):
        session_id = "test_codex_quota_session"
        jsonl_path = self.test_dir / "transcript_codex_quota.jsonl"

        def token_count(used_percent):
            return {
                "type": "event_msg",
                "timestamp": "2026-08-07T10:00:06Z",
                "payload": {
                    "type": "token_count",
                    "rate_limits": {
                        "primary": {
                            "used_percent": used_percent,
                            "window_minutes": 10080,
                            "resets_at": 1786695941,
                        },
                        "plan_type": "plus",
                    },
                },
            }

        first_turn = [
            {
                "type": "event_msg",
                "timestamp": "2026-08-07T10:00:00Z",
                "payload": {"type": "user_message", "message": "Quota question"},
            },
            {
                "type": "event_msg",
                "timestamp": "2026-08-07T10:00:05Z",
                "payload": {"type": "agent_message", "message": "First answer"},
            },
            token_count(20.0),
        ]
        jsonl_path.write_text(
            "".join(json.dumps(event) + "\n" for event in first_turn),
            encoding="utf-8",
        )
        hook_input = {
            "session_id": session_id,
            "transcript_path": str(jsonl_path),
            "cwd": str(self.test_dir),
            "model": "gpt-5",
        }
        codex_save.handle_stop_event(hook_input)

        state = codex_save.load_state(session_id)
        self.assertEqual(state["initial_quota"]["codex"]["weekly"]["remaining"], 80.0)
        self.assertEqual(state["last_quota"]["codex"]["weekly"]["remaining"], 80.0)

        second_turn = [
            {
                "type": "event_msg",
                "timestamp": "2026-08-07T10:05:00Z",
                "payload": {"type": "user_message", "message": "Quota follow-up"},
            },
            {
                "type": "event_msg",
                "timestamp": "2026-08-07T10:05:05Z",
                "payload": {"type": "agent_message", "message": "Second answer"},
            },
            token_count(25.0),
        ]
        with open(jsonl_path, "a", encoding="utf-8") as f:
            f.write("".join(json.dumps(event) + "\n" for event in second_turn))

        codex_save.handle_stop_event(hook_input)

        final_state = codex_save.load_state(session_id)
        content = Path(final_state["output_path"]).read_text(encoding="utf-8")
        self.assertEqual(final_state["final_quota"]["codex"]["weekly"]["remaining"], 75.0)
        self.assertIn("- **Codex**: W: 80.0% ➔ 75.0%", content)
        self.assertIn("Quota: W 75.0(-5.00)%", content)

    def test_codex_recovers_stale_and_missing_state_without_duplicates(self):
        session_id = "test_codex_recovery"
        jsonl_path = self.test_dir / "transcript_codex_recovery.jsonl"
        events = [
            {
                "type": "event_msg",
                "timestamp": "2026-08-07T10:00:00Z",
                "payload": {"type": "user_message", "message": "Recovery question"},
            },
            {
                "type": "event_msg",
                "timestamp": "2026-08-07T10:00:05Z",
                "payload": {"type": "agent_message", "message": "Recovery answer"},
            },
        ]
        jsonl_path.write_text(
            "".join(json.dumps(event) + "\n" for event in events),
            encoding="utf-8",
        )
        hook_input = {
            "session_id": session_id,
            "transcript_path": str(jsonl_path),
            "cwd": str(self.test_dir),
            "model": "gpt-5",
        }

        codex_save.handle_stop_event(hook_input)
        state = codex_save.load_state(session_id)
        output_path = Path(state["output_path"])

        # Markdown更新後・state更新前に終了した状態を再現する。
        state["last_line"] = 1
        codex_save.save_state(session_id, state)
        codex_save.handle_stop_event(hook_input)
        content = output_path.read_text(encoding="utf-8")
        self.assertEqual(content.count("Recovery question"), 2)  # 見出しと本文
        self.assertEqual(content.count("Recovery answer"), 1)
        self.assertEqual(codex_save.load_state(session_id)["last_line"], 2)

        # stateが消失しても既存Markdownを再利用する。
        codex_save.state_path(session_id).unlink()
        codex_save.handle_stop_event(hook_input)
        recovered_state = codex_save.load_state(session_id)
        self.assertEqual(Path(recovered_state["output_path"]), output_path)
        self.assertEqual(len(list(codex_save.OUTPUT_BASE.glob("*.md"))), 1)
        self.assertEqual(output_path.read_text(encoding="utf-8").count("Recovery answer"), 1)

    def test_claude_missing_state_reuses_existing_markdown(self):
        session_id = "test_claude_recovery"
        jsonl_path = self.test_dir / "transcript_claude_recovery.jsonl"
        events = [
            {
                "type": "user",
                "timestamp": "2026-08-07T10:00:00Z",
                "message": {"content": [{"type": "text", "text": "Claude recovery question"}]},
            },
            {
                "type": "assistant",
                "timestamp": "2026-08-07T10:00:05Z",
                "message": {
                    "model": "claude-test",
                    "content": [{"type": "text", "text": "Claude recovery answer"}],
                },
            },
        ]
        jsonl_path.write_text(
            "".join(json.dumps(event) + "\n" for event in events),
            encoding="utf-8",
        )
        hook_input = {
            "session_id": session_id,
            "transcript_path": str(jsonl_path),
            "cwd": str(self.test_dir),
        }

        claude_save.handle_stop_event(hook_input)
        output_path = Path(claude_save.load_state(session_id)["output_path"])
        claude_save.state_path(session_id).unlink()
        claude_save.handle_stop_event(hook_input)

        self.assertEqual(Path(claude_save.load_state(session_id)["output_path"]), output_path)
        self.assertEqual(output_path.read_text(encoding="utf-8").count("Claude recovery answer"), 1)
        self.assertEqual(len(list(claude_save.OUTPUT_BASE.glob("*.md"))), 1)

    def test_agy_missing_state_reuses_existing_markdown(self):
        session_id = "test_agy_recovery"
        jsonl_path = self.test_dir / "transcript_agy_recovery.jsonl"
        events = [
            {
                "step_index": 1,
                "type": "USER_INPUT",
                "created_at": "2026-08-07T10:00:00Z",
                "content": "<USER_REQUEST>AGY recovery question</USER_REQUEST>",
            },
            {
                "step_index": 2,
                "type": "PLANNER_RESPONSE",
                "created_at": "2026-08-07T10:00:05Z",
                "content": "AGY recovery answer",
                "tool_calls": [],
            },
        ]
        jsonl_path.write_text(
            "".join(json.dumps(event) + "\n" for event in events),
            encoding="utf-8",
        )
        hook_input = {
            "conversationId": session_id,
            "transcriptPath": str(jsonl_path),
            "workspacePaths": [str(self.test_dir)],
        }

        with mock.patch.object(agy_save, "retrieve_quota", return_value=None):
            agy_save.handle_stop_event(hook_input)
        output_path = Path(agy_save.load_state(session_id)["output_path"])
        agy_save.state_path(session_id).unlink()
        with mock.patch.object(agy_save, "retrieve_quota", return_value=None):
            agy_save.handle_stop_event(hook_input)

        self.assertEqual(Path(agy_save.load_state(session_id)["output_path"]), output_path)
        self.assertEqual(output_path.read_text(encoding="utf-8").count("AGY recovery answer"), 1)
        self.assertEqual(len(list(agy_save.OUTPUT_BASE.glob("*.md"))), 1)

if __name__ == "__main__":
    unittest.main()
