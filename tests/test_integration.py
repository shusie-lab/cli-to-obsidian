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

    def test_codex_late_usage_updates_existing_answer_callout(self):
        session_id = "test_codex_late_usage"
        jsonl_path = self.test_dir / "transcript_codex_late_usage.jsonl"
        rows = [
            {"type": "turn_context", "payload": {"turn_id": "turn-1", "model": "gpt-5.6-luna"}},
            {"type": "event_msg", "payload": {"type": "user_message", "turn_id": "turn-1", "message": "Question"}},
            {"type": "event_msg", "payload": {"type": "agent_message", "turn_id": "turn-1", "message": "Answer"}},
        ]
        jsonl_path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        hook_input = {
            "session_id": session_id,
            "transcript_path": str(jsonl_path),
            "cwd": str(self.test_dir),
        }

        codex_save.handle_stop_event(hook_input)
        output_path = Path(codex_save.load_state(session_id)["output_path"])
        before = output_path.read_text(encoding="utf-8")
        self.assertNotIn("Usage:", before)

        with open(jsonl_path, "a", encoding="utf-8") as stream:
            stream.write(json.dumps({
                "type": "token_usage_record",
                "payload": {
                    "turn_id": "turn-1",
                    "usage": {
                        "input_tokens": 100,
                        "cached_input_tokens": 10,
                        "cache_write_input_tokens": 0,
                        "output_tokens": 20,
                        "reasoning_output_tokens": 5,
                        "total_tokens": 120,
                    },
                },
            }) + "\n")

        codex_save.handle_stop_event(hook_input)
        after = output_path.read_text(encoding="utf-8")
        self.assertIn("Usage: 120 tok · $0.0000422", after)
        self.assertEqual(after.count("Usage: 120 tok · $0.0000422"), 1)
        self.assertIn("total_tokens: 120", after)

    def test_codex_subagent_usage_updates_session_without_double_counting(self):
        session_id = "parent-session"
        parent = self.test_dir / "rollout-parent.jsonl"
        child = self.test_dir / "rollout-child.jsonl"
        grandchild = self.test_dir / "rollout-grandchild.jsonl"
        unrelated = self.test_dir / "rollout-unrelated.jsonl"
        guardian = self.test_dir / "rollout-guardian.jsonl"

        def write_rows(path, rows):
            path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

        def usage(tokens, model_session=session_id, root_turn_id="turn-1"):
            return {"type": "token_usage_record", "payload": {
                "session_id": model_session,
                "root_turn_id": root_turn_id,
                "usage": {"input_tokens": tokens, "cached_input_tokens": 0,
                          "cache_write_input_tokens": 0, "output_tokens": 0,
                          "reasoning_output_tokens": 0, "total_tokens": tokens},
                "thread_token_usage": {"total_tokens": 999999},
            }}

        write_rows(parent, [
            {"type": "session_meta", "payload": {"id": session_id}},
            {"type": "turn_context", "payload": {"turn_id": "turn-1", "model": "gpt-6-sol"}},
            {"type": "event_msg", "payload": {"type": "user_message", "message": "Question"}},
            {"type": "event_msg", "payload": {"type": "agent_message", "message": "Answer"}},
            usage(100),
        ])
        write_rows(child, [
            {"type": "session_meta", "payload": {"id": "child", "parent_thread_id": session_id,
                "source": {"subagent": {"thread_spawn": {"parent_thread_id": session_id}}}}},
            {"type": "turn_context", "payload": {"model": "gpt-6-luna", "effort": "xhigh"}},
            usage(120), usage(999, "another-session"),
        ])
        write_rows(grandchild, [
            {"type": "session_meta", "payload": {"id": "grandchild", "parent_thread_id": "child",
                "source": {"subagent": {"thread_spawn": {"parent_thread_id": "child"}}}}},
            {"type": "turn_context", "payload": {"model": "gpt-6-luna"}},
            usage(30),
        ])
        write_rows(unrelated, [
            {"type": "session_meta", "payload": {"id": "unrelated", "parent_thread_id": "other",
                "source": {"subagent": {}}}},
            {"type": "turn_context", "payload": {"model": "gpt-6-luna"}},
            usage(800),
        ])
        write_rows(guardian, [
            {"type": "session_meta", "payload": {"id": "guardian", "parent_thread_id": session_id,
                "source": {"subagent": {"other": "guardian"}}}},
            {"type": "turn_context", "payload": {"model": "codex-auto-review"}},
            usage(700),
        ])

        hook_input = {"session_id": session_id, "transcript_path": str(parent), "cwd": str(self.test_dir)}
        codex_save.handle_stop_event(hook_input)
        output = Path(codex_save.load_state(session_id)["output_path"])
        first = output.read_text(encoding="utf-8")
        self.assertIn("total_tokens: 250", first)
        self.assertIn('"gpt-6-luna":', first)
        self.assertIn(
            "Usage: 250 tok · $0.000215 (gpt-6-luna 150 tok・$0.000015)", first,
        )
        self.assertNotIn("total_tokens: 999999", first)

        with child.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(usage(50)) + "\n")
        codex_save.handle_stop_event(hook_input)
        second = output.read_text(encoding="utf-8")
        self.assertIn("total_tokens: 300", second)
        self.assertEqual(
            second.count("Usage: 300 tok · $0.000220 (gpt-6-luna 200 tok・$0.000020)"), 1,
        )
        codex_save.handle_stop_event(hook_input)
        self.assertEqual(output.read_text(encoding="utf-8"), second)

        codex_save.state_path(session_id).unlink()
        codex_save.handle_stop_event(hook_input)
        recovered = output.read_text(encoding="utf-8")
        self.assertEqual(recovered.count("gpt-6-luna 200 tok・$0.000020"), 1)
        self.assertEqual(recovered.count("Answer"), 1)

    def test_codex_subagent_usage_follows_parent_root_turn(self):
        session_id = "two-turn-parent"
        parent = self.test_dir / "rollout-two-turn-parent.jsonl"
        child = self.test_dir / "rollout-two-turn-child.jsonl"

        def token_record(tokens, root_turn_id):
            return {"type": "token_usage_record", "payload": {
                "session_id": session_id, "root_turn_id": root_turn_id,
                "usage": {"input_tokens": tokens, "cached_input_tokens": 0,
                          "cache_write_input_tokens": 0, "output_tokens": 0,
                          "reasoning_output_tokens": 0, "total_tokens": tokens},
            }}

        parent_rows = [
            {"type": "session_meta", "payload": {"id": session_id}},
            {"type": "turn_context", "payload": {"turn_id": "turn-1", "model": "gpt-6-sol"}},
            {"type": "event_msg", "payload": {"type": "user_message", "message": "Q1"}},
            {"type": "event_msg", "payload": {"type": "agent_message", "message": "A1 interim"}},
            {"type": "event_msg", "payload": {"type": "agent_message", "message": "A1 final",
                                          "model": "gpt-5.6-sol"}},
            {"type": "turn_context", "payload": {"turn_id": "turn-2", "model": "gpt-6-sol"}},
            {"type": "event_msg", "payload": {"type": "user_message", "message": "Q2"}},
            {"type": "event_msg", "payload": {"type": "agent_message", "message": "A2"}},
        ]
        child_rows = [
            {"type": "session_meta", "payload": {"id": "two-turn-child",
                "parent_thread_id": session_id,
                "source": {"subagent": {"thread_spawn": {"parent_thread_id": session_id}}}}},
            {"type": "turn_context", "payload": {"model": "gpt-6-luna"}},
            token_record(30, "turn-1"), token_record(40, "turn-2"),
            token_record(5, ""),
        ]
        parent.write_text("".join(json.dumps(row) + "\n" for row in parent_rows), encoding="utf-8")
        child.write_text("".join(json.dumps(row) + "\n" for row in child_rows), encoding="utf-8")
        codex_save.handle_stop_event(
            {"session_id": session_id, "transcript_path": str(parent), "cwd": str(self.test_dir)}
        )
        output = Path(codex_save.load_state(session_id)["output_path"])
        content = output.read_text(encoding="utf-8")
        self.assertIn("total_tokens: 75", content)
        self.assertEqual(content.count("gpt-6-luna 30 tok・$0.000003"), 1)
        self.assertEqual(content.count("gpt-6-luna 40 tok・$0.000004"), 1)
        self.assertNotIn("gpt-6-luna 5 tok", content)
        self.assertLess(content.index("A1 interim"), content.index("gpt-6-luna 30 tok"))
        self.assertLess(content.index("gpt-6-luna 30 tok"), content.index("A1 final"))
        self.assertIn("A1 interim", content)
        self.assertIn("A1 final", content)
        self.assertIn("A2", content)

    def test_codex_app_transcript_uses_common_source_and_effort(self):
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
        self.assertIn("source: codex-cli", content)
        self.assertIn("  - codex-cli", content)
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

    def test_codex_cli_transcript_uses_common_source(self):
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
        self.assertEqual(state["final_quota"]["codex"]["weekly"]["remaining"], 80.0)

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
        self.assertIn("quota:\n  \"codex\":\n    \"weekly\":", content)
        self.assertIn("first_observed:\n        remaining_percent: 80.0", content)
        self.assertIn("last_observed:\n        remaining_percent: 75.0", content)
        self.assertNotIn("(Quota:", content)

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
        self.assertEqual(content.count("Recovery question"), 3)  # title、見出し、本文
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

    def test_agy_app_transcript_uses_common_source(self):
        transcript = self.test_dir / ".gemini" / "antigravity" / "app.jsonl"
        transcript.parent.mkdir(parents=True)
        transcript.write_text(
            json.dumps({
                "step_index": 1,
                "type": "USER_INPUT",
                "created_at": "2026-08-07T10:00:00Z",
                "content": "<USER_REQUEST>App question</USER_REQUEST>",
            }) + "\n",
            encoding="utf-8",
        )
        session_id = "test_agy_app_source"
        with mock.patch.object(agy_save, "retrieve_quota", return_value=None):
            agy_save.handle_stop_event({
                "conversationId": session_id,
                "transcriptPath": str(transcript),
                "workspacePaths": [str(self.test_dir)],
            })
        output_path = Path(agy_save.load_state(session_id)["output_path"])
        content = output_path.read_text(encoding="utf-8")
        self.assertIn("source: antigravity-cli\n", content)
        self.assertIn("  - antigravity-cli\n", content)
        self.assertNotIn("antigravity-app", content)

if __name__ == "__main__":
    unittest.main()
