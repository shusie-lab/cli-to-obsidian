import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__) + "/.."))
import opencode_save


class TestOpenCodeSave(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        opencode_save.OUTPUT_BASE = root / "output"
        opencode_save.STATE_DIR = root / "state"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_extracts_only_user_and_assistant_text_parts(self):
        messages = opencode_save.extract_messages([
            {"info": {"role": "user", "time": {"created": 1722988800000}}, "parts": [{"type": "text", "text": "質問"}]},
            {"info": {"role": "assistant", "modelID": "test-model", "time": {"created": 1722988805000}}, "parts": [
                {"type": "tool", "tool": "bash"}, {"type": "text", "text": "回答"},
            ]},
            {"info": {"role": "assistant"}, "parts": [{"type": "tool", "tool": "read"}]},
        ])
        self.assertEqual([item["type"] for item in messages], ["user", "assistant"])
        self.assertEqual(messages[1]["model"], "test-model")
        self.assertEqual(messages[1]["text"], "回答")

    def test_extracts_sdk_session_messages_and_keeps_numeric_timestamp(self):
        messages = opencode_save.extract_messages([
            {"id": "msg_user", "type": "user", "time": {"created": 1722988800000}, "text": "SDK質問"},
            {
                "id": "msg_assistant",
                "type": "assistant",
                "model": {"providerID": "test", "modelID": "test-model"},
                "time": {"created": 1722988805000},
                "content": [
                    {"type": "reasoning", "id": "reasoning", "text": "考え中"},
                    {"type": "text", "id": "part", "text": "SDK回答"},
                ],
            },
        ])
        self.assertEqual([item["id"] for item in messages], ["msg_user", "msg_assistant"])
        self.assertEqual(messages[0]["timestamp"], 1722988800000)
        self.assertEqual(messages[1]["text"], "SDK回答")
        self.assertIn("2024-08-07 09:00:00", opencode_save.format_message_time(messages[0]["timestamp"]))
        self.assertEqual(opencode_save.format_message_time("invalid timestamp"), "")

    def test_extracts_provider_id_from_opencode_messages(self):
        messages = opencode_save.extract_messages([
            {
                "info": {"role": "assistant", "providerID": "openrouter", "modelID": "test-model"},
                "parts": [{"type": "text", "text": "回答"}],
            },
            {
                "type": "assistant",
                "model": {"providerID": "anthropic", "modelID": "claude"},
                "content": [{"type": "text", "text": "別の回答"}],
            },
        ])
        self.assertEqual([item["provider"] for item in messages], ["openrouter", "anthropic"])

    def test_render_markdown_full_snapshot_with_fixed_modified_time(self):
        created = opencode_save.datetime(2026, 9, 14, 10, 0, tzinfo=opencode_save.JST)
        modified = opencode_save.datetime(2026, 9, 14, 10, 5, tzinfo=opencode_save.JST)
        messages = [
            {
                "type": "user",
                "text": "Snapshot question",
                "timestamp": "2026-09-14T10:00:00+09:00",
            },
            {
                "type": "assistant",
                "text": "Snapshot answer",
                "timestamp": "2026-09-14T10:00:01+09:00",
                "model": "snapshot-model",
            },
        ]

        actual = opencode_save.render_markdown(
            "snapshot-session",
            "/tmp/example",
            created,
            messages,
            modified=modified,
        )

        expected = (
            "---\n"
            "source: opencode\n"
            'title: "Snapshot question"\n'
            'session_id: "snapshot-session"\n'
            'project: "/tmp/example"\n'
            'created: "2026-09-14T10:00:00+09:00"\n'
            'modified: "2026-09-14T10:05:00+09:00"\n'
            "tags:\n"
            "  - ai-conversation\n"
            "  - opencode\n"
            "message_count: 2\n"
            "---\n\n"
            "💰 **Usage (USD換算)**: N/A\n\n"
            "# Snapshot question\n"
            "> [!QUESTION] User\n"
            "> <small>⏱ 2026-09-14 10:00:00</small>\n"
            ">\n"
            "> Snapshot question\n\n"
            "> [!NOTE] OpenCode\n"
            "> <small>🤖 snapshot-model</small>\n"
            "> <small>⏱ 回答所要時間: 取得不可</small>\n\n"
            "Snapshot answer\n\n"
        )
        self.assertEqual(actual, expected)

    def test_consecutive_assistant_records_share_callout_and_usage(self):
        created = opencode_save.datetime(2026, 9, 27, 6, 0, tzinfo=opencode_save.JST)
        messages = [
            {"type": "user", "text": "質問1", "timestamp": "", "model": ""},
            {"type": "assistant", "text": "進捗1", "model": "model-a", "usage": {"total_tokens": 10, "cost_usd": 0.1}},
            {"type": "assistant", "text": "進捗2", "model": "model-a", "usage": {"total_tokens": 20, "cost_usd": 0.2}},
            {"type": "assistant", "text": "最終回答", "model": "model-a", "usage": {"total_tokens": 30, "cost_usd": 0.3}},
            {"type": "user", "text": "質問2", "timestamp": "", "model": ""},
            {"type": "assistant", "text": "別の回答", "model": "model-b"},
        ]
        content = opencode_save.render_markdown("session", "/tmp/example", created, messages, modified=created)
        self.assertIn("message_count: 4", content)
        self.assertEqual(content.count("> [!NOTE] OpenCode"), 2)
        self.assertIn("Usage: 60 tok", content)
        self.assertLess(content.index("進捗1"), content.index("進捗2"))
        self.assertLess(content.index("進捗2"), content.index("最終回答"))
        self.assertLess(content.index("最終回答"), content.index("質問2"))

    def test_hidden_user_message_keeps_answer_callouts_separate(self):
        raw = [
            {"info": {"id": "u1", "role": "user"}, "parts": [{"type": "text", "text": "質問"}]},
            {"info": {"id": "a1", "role": "assistant", "modelID": "model-a"}, "parts": [{"type": "text", "text": "回答1"}]},
            {"info": {"id": "u2", "role": "user"}, "parts": []},
            {"info": {"id": "a2", "role": "assistant", "modelID": "model-a"}, "parts": [{"type": "text", "text": "回答2"}]},
        ]
        payload = {"session_id": "ses_hidden_user", "full_sync": True, "messages": raw}
        path = opencode_save.handle_payload(payload)
        content = path.read_text()
        self.assertEqual(content.count("> [!NOTE] OpenCode"), 2)
        self.assertIn("message_count: 3", content)
        # 旧描画のhashを持つstateからも、同じpayloadで新形式へ更新する。
        import hashlib
        import json
        state = opencode_save.load_state(payload["session_id"])
        messages = opencode_save.extract_messages(raw)
        hash_messages = [dict(id=m.get("id", ""), type=m["type"], text=m["text"],
            timestamp=m.get("timestamp", ""), model=m.get("model", ""),
            provider=opencode_save._message_provider(m), usage=m.get("usage")) for m in messages]
        state["payload_hash"] = hashlib.sha256(json.dumps({"render_version": 2,
            "cwd": os.getcwd(), "messages": hash_messages,
            "usage_records": opencode_save.extract_usage_records(raw)},
            ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
        opencode_save.save_state(payload["session_id"], state)
        path.write_text("old rendering")
        self.assertEqual(opencode_save.handle_payload(payload), path)
        self.assertEqual(path.read_text().count("> [!NOTE] OpenCode"), 2)

    def test_full_sync_after_state_loss_does_not_duplicate_grouped_reply(self):
        session_id = "ses_grouped_state_loss"
        messages = [
            {"info": {"id": "u1", "role": "user"}, "parts": [{"type": "text", "text": "質問"}]},
            {"info": {"id": "a1", "role": "assistant", "modelID": "model-a"}, "parts": [{"type": "text", "text": "進捗"}]},
            {"info": {"id": "a2", "role": "assistant", "modelID": "model-a"}, "parts": [{"type": "text", "text": "回答"}]},
        ]
        payload = {"session_id": session_id, "cwd": "/tmp/example", "full_sync": True, "messages": messages}
        path = opencode_save.handle_payload(payload)
        opencode_save.state_path(session_id).unlink()
        opencode_save.handle_payload(payload)
        content = path.read_text(encoding="utf-8")
        self.assertEqual(content.count("> [!QUESTION] User"), 1)
        self.assertEqual(content.count("> [!NOTE] OpenCode"), 1)
        self.assertEqual(content.count("進捗"), 1)
        self.assertEqual(content.count("\n回答\n"), 1)

    def test_full_sync_after_state_loss_preserves_reply_missing_from_payload(self):
        session_id = "ses_grouped_partial_recovery"
        first = [
            {"info": {"id": "u1", "role": "user"}, "parts": [{"type": "text", "text": "過去の質問"}]},
            {"info": {"id": "a1", "role": "assistant", "modelID": "model-a"}, "parts": [{"type": "text", "text": "過去の進捗"}]},
            {"info": {"id": "a2", "role": "assistant", "modelID": "model-a"}, "parts": [{"type": "text", "text": "過去の回答"}]},
        ]
        path = opencode_save.handle_payload({"session_id": session_id, "cwd": "/tmp/example", "full_sync": True, "messages": first})
        opencode_save.state_path(session_id).unlink()
        second = [
            {"info": {"id": "u2", "role": "user"}, "parts": [{"type": "text", "text": "新しい質問"}]},
            {"info": {"id": "a3", "role": "assistant", "modelID": "model-a"}, "parts": [{"type": "text", "text": "新しい回答"}]},
        ]
        opencode_save.handle_payload({"session_id": session_id, "cwd": "/tmp/example", "full_sync": True, "messages": second})
        content = path.read_text(encoding="utf-8")
        for text in ("過去の進捗", "過去の回答", "新しい回答"):
            self.assertEqual(content.count(text), 1)
        self.assertEqual(content.count("> [!QUESTION] User"), 2)

    def test_contributor_free_usage_is_estimated_in_usd(self):
        payload = {
            "session_id": "ses_muse_spark_cost",
            "cwd": "/tmp/example",
            "messages": [
                {"info": {"id": "u1", "role": "user"}, "parts": [{"type": "text", "text": "質問"}]},
                {"info": {"id": "a1", "role": "assistant", "modelID": "muse-spark-1.3-contributor-free",
                          "cost": 0,
                          "tokens": {"input": 1000000, "output": 1000000, "reasoning": 0,
                                     "cache": {"read": 0, "write": 0}}},
                 "parts": [{"type": "text", "text": "回答"}]},
            ],
        }
        path = opencode_save.handle_payload(payload)
        content = path.read_text(encoding="utf-8")
        self.assertIn("total_cost_usd: 0.3", content)
        self.assertIn("cost_usd: 0.3", content)
        self.assertIn("Usage: 2,000,000 tok", content)
        self.assertIn(
            "💰 **Usage (USD換算)**: Total: $0.3 (muse-spark-1.3-contributor-free $0.3)", content
        )
        self.assertLess(content.index("💰 **Usage (USD換算)**:"), content.index("# 質問"))

    def test_current_model_rates(self):
        usage = {
            "input_tokens": 1000000,
            "cached_input_tokens": 100000,
            "cache_write_input_tokens": 100000,
            "output_tokens": 100000,
        }
        self.assertEqual(
            str(opencode_save._usage_cost("opencode/nemotron-3-ultra-free", usage)),
            "0.68",
        )
        self.assertEqual(
            str(opencode_save._usage_cost("anthropic/claude-opus-5-5", usage)),
            "5.72",
        )

    def test_usage_cost_sum_keeps_decimal_precision(self):
        payload = {
            "session_id": "ses_muse_spark_decimal_cost",
            "cwd": "/tmp/example",
            "messages": [
                {"info": {"id": "u1", "role": "user"}, "parts": [{"type": "text", "text": "質問"}]},
                {"info": {"id": "a1", "role": "assistant", "modelID": "muse-spark-1.3-contributor-free",
                          "cost": 0,
                          "tokens": {"input": 1000000, "output": 0, "reasoning": 0,
                                     "cache": {"read": 0, "write": 0}}},
                 "parts": [{"type": "text", "text": "前半"}]},
                {"info": {"id": "a2", "role": "assistant", "modelID": "muse-spark-1.3-contributor-free",
                          "cost": 0,
                          "tokens": {"input": 0, "output": 1000000, "reasoning": 0,
                                     "cache": {"read": 0, "write": 0}}},
                 "parts": [{"type": "text", "text": "後半"}]},
            ],
        }
        path = opencode_save.handle_payload(payload)
        content = path.read_text(encoding="utf-8")
        self.assertIn("total_cost_usd: 0.3", content)
        self.assertIn("cost_usd: 0.3", content)
        self.assertIn("Usage: 2,000,000 tok · $0.3", content)
        state = opencode_save.load_state(payload["session_id"])
        self.assertEqual(state["usage_records"]["a1"]["usage"]["cost_usd"], "0.1")
        self.assertEqual(state["usage_records"]["a2"]["usage"]["cost_usd"], "0.2")

    def test_body_usage_summary_shows_only_usd_and_unknown_models(self):
        usage = {
            "total_cost_usd": opencode_save.Decimal("0.25"),
            "cost_incomplete": True,
            "by_model": {
                "priced-model": {"cost_usd": opencode_save.Decimal("0.25")},
                "unknown-model": {"cost_unavailable": True},
            },
        }
        self.assertEqual(
            opencode_save.build_usage_section(usage),
            "💰 **Usage (USD換算)**: Total: $0.25（部分換算） (priced-model $0.25、unknown-model 換算不可)\n",
        )

    def test_openrouter_balance_is_not_fetched_and_native_tokens_are_saved(self):
        payload = {
            "session_id": "ses_native_usage",
            "cwd": "/tmp/example",
            "messages": [
                {"info": {"id": "u1", "role": "user"}, "parts": [{"type": "text", "text": "質問"}]},
                {"info": {"id": "a1", "role": "assistant", "providerID": "openrouter",
                          "modelID": "test-model", "cost": 0,
                          "tokens": {"input": 10, "output": 5, "reasoning": 2,
                                     "cache": {"read": 20, "write": 3}}},
                 "parts": [{"type": "text", "text": "回答"}]},
            ],
        }
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-secret"}), mock.patch(
            "urllib.request.urlopen", side_effect=AssertionError("no network call expected")
        ) as urlopen:
            path = opencode_save.handle_payload(payload)
            opencode_save.handle_payload(payload)
        urlopen.assert_not_called()
        content = path.read_text(encoding="utf-8")
        self.assertIn("input_tokens: 33", content)
        self.assertIn("cached_input_tokens: 20", content)
        self.assertIn("output_tokens: 7", content)
        self.assertIn("total_tokens: 40", content)
        self.assertIn("cost_unavailable: true", content)
        self.assertIn("Usage: 40 tok", content)
        self.assertNotIn("OpenRouter balance", content)
        self.assertNotIn("openrouter_balance", content)

    def test_native_usage_updates_when_same_answer_gets_late_tokens(self):
        answer = {"info": {"id": "a1", "role": "assistant", "modelID": "test-model"},
                  "parts": [{"type": "text", "text": "回答"}]}
        payload = {"session_id": "ses_late_usage", "cwd": "/tmp/example", "full_sync": True,
                   "messages": [
                       {"info": {"id": "u1", "role": "user"},
                        "parts": [{"type": "text", "text": "質問"}]}, answer]}
        path = opencode_save.handle_payload(payload)
        self.assertNotIn("usage:", path.read_text(encoding="utf-8"))
        answer["info"]["tokens"] = {"input": 10, "output": 2, "reasoning": 0,
                                    "cache": {"read": 0, "write": 0}}
        opencode_save.handle_payload(payload)
        content = path.read_text(encoding="utf-8")
        self.assertIn("total_tokens: 12", content)
        self.assertEqual(content.count("Usage: 12 tok"), 1)
        self.assertEqual(content.count("\n回答\n"), 1)

    def test_usage_includes_assistant_without_visible_text(self):
        payload = {"session_id": "ses_hidden_usage", "cwd": "/tmp/example", "full_sync": True,
                   "messages": [
                       {"info": {"id": "u1", "role": "user"},
                        "parts": [{"type": "text", "text": "質問"}]},
                       {"info": {"id": "a_tool", "role": "assistant", "modelID": "test-model",
                                 "tokens": {"input": 10, "output": 2, "reasoning": 3,
                                            "cache": {"read": 4, "write": 1}}},
                        "parts": [{"type": "tool", "tool": "bash"}]},
                       {"info": {"id": "a_text", "role": "assistant", "modelID": "test-model",
                                 "tokens": {"input": 20, "output": 5, "reasoning": 0,
                                            "cache": {"read": 0, "write": 0}}},
                        "parts": [{"type": "text", "text": "回答"}]},
                   ]}
        path = opencode_save.handle_payload(payload)
        content = path.read_text(encoding="utf-8")
        self.assertIn("total_tokens: 45", content)
        self.assertIn("reasoning_output_tokens: 3", content)
        self.assertEqual(content.count("> [!NOTE] OpenCode"), 1)
        self.assertIn("message_count: 2", content)

    def test_tool_only_usage_is_added_to_the_answer_in_its_user_turn(self):
        def assistant(message_id, token_count, text=None):
            message = {
                "info": {
                    "id": message_id,
                    "role": "assistant",
                    "modelID": "model-a",
                    "tokens": {
                        "input": token_count,
                        "output": 0,
                        "reasoning": 0,
                        "cache": {"read": 0, "write": 0},
                    },
                },
                "parts": [],
            }
            if text:
                message["parts"] = [{"type": "text", "text": text}]
            return message

        payload = {
            "session_id": "ses_tool_only_answer_usage",
            "cwd": "/tmp/example",
            "full_sync": True,
            "messages": [
                {"info": {"id": "u1", "role": "user"}, "parts": [{"type": "text", "text": "質問1"}]},
                assistant("tool1", 100),
                assistant("answer1", 10, "回答1"),
                {"info": {"id": "u2", "role": "user"}, "parts": [{"type": "text", "text": "質問2"}]},
                assistant("tool2", 20),
                assistant("answer2", 30, "回答2"),
                {"info": {"id": "u3", "role": "user"}, "parts": [{"type": "text", "text": "質問3"}]},
                assistant("tool3", 15),
            ],
        }

        path = opencode_save.handle_payload(payload)
        content = path.read_text(encoding="utf-8")

        self.assertIn("total_tokens: 175", content)
        self.assertEqual(content.count("Usage: 110 tok"), 1)
        self.assertEqual(content.count("Usage: 50 tok"), 1)
        self.assertLess(content.index("Usage: 110 tok"), content.index("質問2"))
        self.assertGreater(content.index("Usage: 50 tok"), content.index("質問2"))

    def test_provider_change_is_in_payload_hash(self):
        def payload(provider):
            return {
                "session_id": "ses_provider_hash",
                "cwd": "/tmp/example",
                "full_sync": True,
                "messages": [
                    {"info": {"id": "u1", "role": "user"}, "parts": [{"type": "text", "text": "質問"}]},
                    {"info": {"id": "a1", "role": "assistant", "providerID": provider}, "parts": [{"type": "text", "text": "回答"}]},
                ],
            }

        opencode_save.handle_payload(payload("anthropic"))
        first_hash = opencode_save.load_state("ses_provider_hash")["payload_hash"]
        opencode_save.handle_payload(payload("openrouter"))
        second_hash = opencode_save.load_state("ses_provider_hash")["payload_hash"]
        self.assertNotEqual(first_hash, second_hash)

    def test_save_is_repeatable_without_duplicate_messages(self):
        payload = {
            "session_id": "ses_test_123",
            "cwd": "/tmp/example",
            "created": 1722988800000,
            "messages": [
                {"info": {"role": "user", "time": {"created": 1722988800000}}, "parts": [{"type": "text", "text": "質問"}]},
                {"info": {"role": "assistant", "modelID": "test-model", "time": {"created": 1722988805000}}, "parts": [{"type": "text", "text": "回答"}]},
            ],
        }
        path = opencode_save.handle_payload(payload)
        self.assertIsNotNone(path)
        opencode_save.handle_payload(payload)
        content = path.read_text(encoding="utf-8")
        self.assertIn("source: opencode", content)
        self.assertIn("message_count: 2", content)
        self.assertEqual(content.count("質問"), 3)
        self.assertIn('title: "質問"', content)
        self.assertEqual(content.count("\n回答\n"), 1)
        self.assertIn("payload_hash", opencode_save.load_state("ses_test_123"))

    def test_partial_payloads_preserve_previous_turns(self):
        def payload(user_id, assistant_id, user_text, assistant_text):
            return {
                "session_id": "ses_multi_turn",
                "cwd": "/tmp/example",
                "created": 1722988800000,
                "messages": [
                    {"info": {"id": user_id, "role": "user", "time": {"created": 1722988800000}}, "parts": [{"type": "text", "text": user_text}]},
                    {"info": {"id": assistant_id, "role": "assistant", "time": {"created": 1722988805000}}, "parts": [{"type": "text", "text": assistant_text}]},
                ],
            }

        path = opencode_save.handle_payload(payload("u1", "a1", "質問1", "回答1"))
        opencode_save.handle_payload(payload("u2", "a2", "質問2", "回答2"))
        content = path.read_text(encoding="utf-8")
        self.assertIn("message_count: 4", content)
        for text in ("質問1", "回答1", "質問2", "回答2"):
            self.assertIn(text, content)

    def test_full_sync_restores_canonical_message_order(self):
        def message(message_id, role, text, timestamp):
            return {
                "info": {"id": message_id, "role": role, "time": {"created": timestamp}},
                "parts": [{"type": "text", "text": text}],
            }

        session_id = "ses_full_sync"
        second_turn = [
            message("u2", "user", "質問2", 1722988860000),
            message("a2", "assistant", "回答2", 1722988865000),
        ]
        first_turn = [
            message("u1", "user", "質問1", 1722988800000),
            message("a1", "assistant", "回答1", 1722988805000),
        ]
        path = opencode_save.handle_payload({"session_id": session_id, "cwd": "/tmp/example", "messages": second_turn})
        opencode_save.handle_payload({
            "session_id": session_id,
            "cwd": "/tmp/example",
            "full_sync": True,
            "messages": first_turn + second_turn,
        })
        self.assertEqual([message["id"] for message in opencode_save.load_state(session_id)["messages"]], ["u1", "a1", "u2", "a2"])
        self.assertLess(path.read_text(encoding="utf-8").index("質問1"), path.read_text(encoding="utf-8").index("質問2"))

    def test_full_sync_prefers_ids_when_assistant_text_repeats(self):
        def message(message_id, role, text, timestamp):
            return {
                "info": {"id": message_id, "role": role, "time": {"created": timestamp}},
                "parts": [{"type": "text", "text": text}],
            }

        session_id = "ses_full_sync_same_answer"
        first_turn = [
            message("u1", "user", "質問1", 1722988800000),
            message("a1", "assistant", "同じ回答", 1722988805000),
        ]
        second_turn = [
            message("u2", "user", "質問2", 1722988860000),
            message("a2", "assistant", "同じ回答", 1722988865000),
        ]
        path = opencode_save.handle_payload({"session_id": session_id, "cwd": "/tmp/example", "messages": second_turn})
        # 部分保存が逆順で到着した状態から、回答更新を含む全履歴を同期する。
        opencode_save.handle_payload({"session_id": session_id, "cwd": "/tmp/example", "messages": first_turn})
        self.assertEqual(
            [item["id"] for item in opencode_save.load_state(session_id)["messages"]],
            ["u2", "a2", "u1", "a1"],
        )
        canonical = first_turn + [
            second_turn[0],
            message("a2", "assistant", "更新された回答", 1722988865000),
        ]
        opencode_save.handle_payload({
            "session_id": session_id,
            "cwd": "/tmp/example",
            "full_sync": True,
            "messages": canonical,
        })
        opencode_save.handle_payload({
            "session_id": session_id,
            "cwd": "/tmp/example",
            "full_sync": True,
            "messages": canonical,
        })
        state = opencode_save.load_state(session_id)
        self.assertEqual([message["id"] for message in state["messages"]], ["u1", "a1", "u2", "a2"])
        self.assertEqual(state["messages"][-1]["text"], "更新された回答")
        self.assertIn("message_count: 4", path.read_text(encoding="utf-8"))

    def test_state_loss_recovers_messages_from_existing_markdown(self):
        def payload(messages):
            return {"session_id": "ses_state_loss", "cwd": "/tmp/example", "created": 1722988800000, "messages": messages}

        first = [
            {"info": {"id": "u1", "role": "user", "time": {"created": 1722988800000}}, "parts": [{"type": "text", "text": "過去の質問"}]},
            {"info": {"id": "a1", "role": "assistant", "time": {"created": 1722988805000}}, "parts": [{"type": "text", "text": "過去の回答"}]},
        ]
        second = [
            {"info": {"id": "u2", "role": "user", "time": {"created": 1722988860000}}, "parts": [{"type": "text", "text": "新しい質問"}]},
            {"info": {"id": "a2", "role": "assistant", "time": {"created": 1722988865000}}, "parts": [{"type": "text", "text": "新しい回答"}]},
        ]
        path = opencode_save.handle_payload(payload(first))
        opencode_save.state_path("ses_state_loss").unlink()
        opencode_save.handle_payload(payload(second))
        content = path.read_text(encoding="utf-8")
        self.assertIn("message_count: 4", content)
        for text in ("過去の質問", "過去の回答", "新しい質問", "新しい回答"):
            self.assertIn(text, content)

    def test_state_loss_does_not_duplicate_same_payload(self):
        payload = {
            "session_id": "ses_state_retry",
            "cwd": "/tmp/example",
            "messages": [
                {"info": {"id": "u1", "role": "user", "time": {"created": 1722988800000}}, "parts": [{"type": "text", "text": "同じ質問"}]},
                {"info": {"id": "a1", "role": "assistant", "time": {"created": 1722988805000}}, "parts": [{"type": "text", "text": "同じ回答"}]},
            ],
        }
        path = opencode_save.handle_payload(payload)
        opencode_save.state_path("ses_state_retry").unlink()
        opencode_save.handle_payload(payload)
        self.assertIn("message_count: 2", path.read_text(encoding="utf-8"))

    def test_state_loss_does_not_merge_new_turn_with_same_assistant_text(self):
        def payload(user_id, assistant_id, user_text, timestamp):
            return {
                "session_id": "ses_state_same_answer",
                "cwd": "/tmp/example",
                "messages": [
                    {"info": {"id": user_id, "role": "user", "time": {"created": timestamp}}, "parts": [{"type": "text", "text": user_text}]},
                    {"info": {"id": assistant_id, "role": "assistant", "time": {"created": timestamp + 5000}}, "parts": [{"type": "text", "text": "了解しました"}]},
                ],
            }

        path = opencode_save.handle_payload(payload("u1", "a1", "最初の質問", 1722988800000))
        opencode_save.state_path("ses_state_same_answer").unlink()
        opencode_save.handle_payload(payload("u2", "a2", "別の質問", 1722988860000))
        state = opencode_save.load_state("ses_state_same_answer")
        self.assertEqual([message["type"] for message in state["messages"]], ["user", "assistant", "user", "assistant"])
        self.assertEqual([message["text"] for message in state["messages"]], ["最初の質問", "了解しました", "別の質問", "了解しました"])
        self.assertIn("message_count: 4", path.read_text(encoding="utf-8"))

    def test_state_recovery_ignores_markers_inside_code_fence(self):
        response = (
            "コード例です。\n\n"
            "```markdown\n"
            "# User: これは発言ではありません\n"
            "> [!QUESTION] User\n"
            "> 例の質問\n\n"
            "> [!NOTE] OpenCode\n\n"
            "例の回答\n"
            "```\n\n"
            "コード例の後の本文です。"
        )
        payload = {
            "session_id": "ses_fenced_recovery",
            "cwd": "/tmp/example",
            "messages": [
                {"info": {"id": "u1", "role": "user", "time": {"created": 1722988800000}}, "parts": [{"type": "text", "text": "コードを保存"}]},
                {"info": {"id": "a1", "role": "assistant", "time": {"created": 1722988805000}}, "parts": [{"type": "text", "text": response}]},
            ],
        }
        path = opencode_save.handle_payload(payload)
        opencode_save.state_path("ses_fenced_recovery").unlink()
        recovered = opencode_save.parse_existing_markdown(path)
        self.assertEqual(len(recovered), 2)
        self.assertEqual(recovered[1]["text"], response)

    def test_timestamp_update_rewrites_existing_message(self):
        base = {
            "session_id": "ses_timestamp_update",
            "cwd": "/tmp/example",
            "messages": [{"info": {"id": "u1", "role": "user", "time": {"created": 1722988800000}}, "parts": [{"type": "text", "text": "日時確認"}]}],
        }
        path = opencode_save.handle_payload(base)
        updated = {**base, "full_sync": True, "messages": [{"info": {"id": "u1", "role": "user", "time": {"created": 1722988860000}}, "parts": [{"type": "text", "text": "日時確認"}]}]}
        opencode_save.handle_payload(updated)
        self.assertEqual(opencode_save.load_state("ses_timestamp_update")["messages"][0]["timestamp"], 1722988860000)
        self.assertIn("2024-08-07 09:01:00", path.read_text(encoding="utf-8"))

    def test_new_session_ids_do_not_share_output_path(self):
        def payload(session_id, text):
            return {
                "session_id": session_id,
                "cwd": "/tmp/example",
                "created": 1722988800000,
                "messages": [{"info": {"role": "user"}, "parts": [{"type": "text", "text": text}]}],
            }

        first = opencode_save.handle_payload(payload("ses_same_00000001", "最初"))
        second = opencode_save.handle_payload(payload("ses_same_00000002", "次"))
        self.assertNotEqual(first, second)
        self.assertIn("最初", first.read_text(encoding="utf-8"))
        self.assertIn("次", second.read_text(encoding="utf-8"))

    def test_output_dir_rejects_absolute_and_parent_paths(self):
        self.assertEqual(opencode_save.resolve_output_dir("/tmp/out"), Path("生成AI/ChatLog"))
        self.assertEqual(opencode_save.resolve_output_dir("AI/../out"), Path("生成AI/ChatLog"))

    def test_opencode_find_existing_md_supports_legacy_short_id(self):
        session_id = "ses_legacy_opencode_test"
        short_id = opencode_save.safe_filename_component(session_id[:8])
        legacy_filename = f"20260101_120000_myproj_{short_id}.md"
        legacy_path = opencode_save.OUTPUT_BASE / legacy_filename
        content = (
            "---\n"
            "source: opencode\n"
            f"session_id: {opencode_save.yaml_quote(session_id)}\n"
            "---\n\n"
            "# User: 過去の質問\n"
            "> [!QUESTION] User\n"
            "> 過去の質問\n\n"
            "> [!NOTE] OpenCode\n\n"
            "過去の回答\n\n"
        )
        opencode_save.OUTPUT_BASE.mkdir(parents=True, exist_ok=True)
        opencode_save.atomic_write(legacy_path, content)

        found = opencode_save.find_existing_md(session_id)
        self.assertEqual(found, legacy_path)

        payload = {
            "session_id": session_id,
            "cwd": "/tmp/myproj",
            "messages": [
                {"info": {"id": "u2", "role": "user", "time": {"created": 1722988860000}}, "parts": [{"type": "text", "text": "新しい質問"}]},
                {"info": {"id": "a2", "role": "assistant", "time": {"created": 1722988865000}}, "parts": [{"type": "text", "text": "新しい回答"}]},
            ],
        }
        res_path = opencode_save.handle_payload(payload)
        self.assertEqual(res_path, legacy_path)
        saved_text = legacy_path.read_text(encoding="utf-8")
        self.assertIn("過去の質問", saved_text)
        self.assertIn("新しい質問", saved_text)

    def test_opencode_cleanup_old_states_and_locked_session(self):
        import fcntl
        import json
        from datetime import datetime, timezone, timedelta
        jst = timezone(timedelta(hours=9))
        opencode_save.STATE_DIR.mkdir(parents=True, exist_ok=True)
        old_time = (datetime.now(jst) - timedelta(days=35)).isoformat()
        new_time = (datetime.now(jst) - timedelta(days=5)).isoformat()

        # 1. 削除対象の古い state
        old_ses = "old_opencode_ses"
        old_file = opencode_save.state_path(old_ses)
        old_file.write_text(json.dumps({"last_used_at": old_time}), encoding="utf-8")
        os.utime(old_file, (1000000, 1000000))

        # 2. 保持対象の新しい state
        new_ses = "new_opencode_ses"
        new_file = opencode_save.state_path(new_ses)
        new_file.write_text(json.dumps({"last_used_at": new_time}), encoding="utf-8")

        # 3. 古いがロック中の state
        locked_ses = "locked_opencode_ses"
        locked_file = opencode_save.state_path(locked_ses)
        locked_file.write_text(json.dumps({"last_used_at": old_time}), encoding="utf-8")
        os.utime(locked_file, (1000000, 1000000))

        lock_dir = opencode_save.STATE_DIR / ".locks"
        lock_dir.mkdir(parents=True, exist_ok=True)
        lock_path = lock_dir / f"{opencode_save.safe_session_key(locked_ses)}.lock"
        with open(lock_path, "a", encoding="utf-8") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            opencode_save.cleanup_old_states()
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

        self.assertFalse(old_file.exists())
        self.assertTrue(new_file.exists())
        self.assertTrue(locked_file.exists())

        new_file.unlink(missing_ok=True)
        locked_file.unlink(missing_ok=True)

    def test_opencode_identical_payload_updates_last_used_at(self):
        import json
        payload = {
            "session_id": "ses_payload_repeat",
            "cwd": "/tmp/example",
            "messages": [{"info": {"id": "u1", "role": "user"}, "parts": [{"type": "text", "text": "同じ内容"}]}],
        }
        opencode_save.handle_payload(payload)
        state1 = opencode_save.load_state("ses_payload_repeat")
        self.assertIn("last_used_at", state1)

        state1["last_used_at"] = "2026-01-01T00:00:00+09:00"
        opencode_save.atomic_write(opencode_save.state_path("ses_payload_repeat"), json.dumps(state1))

        opencode_save.handle_payload(payload)
        state2 = opencode_save.load_state("ses_payload_repeat")
        self.assertNotEqual(state2["last_used_at"], "2026-01-01T00:00:00+09:00")

    def test_opencode_find_existing_md_error_handling(self):
        from unittest import mock
        session_id = "ses_err_opencode"
        opencode_save.OUTPUT_BASE.mkdir(parents=True, exist_ok=True)

        with mock.patch.object(opencode_save.Path, "iterdir", side_effect=OSError("directory error")):
            with self.assertRaises(opencode_save.ExistingMarkdownSearchError):
                opencode_save.find_existing_md(session_id)

            payload = {"session_id": session_id, "cwd": "/tmp/example", "messages": [{"info": {"role": "user"}, "parts": [{"type": "text", "text": "テスト"}]}]}
            with self.assertRaises(opencode_save.ExistingMarkdownSearchError):
                opencode_save.handle_payload(payload)

        bad_path = opencode_save.OUTPUT_BASE / f"20260101_120000_myproj_{opencode_save.safe_session_key(session_id)}.md"
        bad_path.write_text("dummy", encoding="utf-8")
        os.chmod(bad_path, 0o000)
        try:
            with self.assertRaises(opencode_save.ExistingMarkdownSearchError):
                opencode_save.find_existing_md(session_id)
        finally:
            os.chmod(bad_path, 0o644)
            bad_path.unlink(missing_ok=True)

    def test_opencode_resume_after_state_cleaned_up(self):
        session_id = "ses_resume_after_cleanup"
        first_payload = {
            "session_id": session_id,
            "cwd": "/tmp/example",
            "messages": [
                {"info": {"id": "u1", "role": "user", "time": {"created": 1722988800000}}, "parts": [{"type": "text", "text": "1回目の質問"}]},
                {"info": {"id": "a1", "role": "assistant", "time": {"created": 1722988805000}}, "parts": [{"type": "text", "text": "1回目の回答"}]},
            ],
        }
        md_path = opencode_save.handle_payload(first_payload)
        self.assertIsNotNone(md_path)

        import json
        from datetime import datetime, timedelta
        old_state = opencode_save.load_state(session_id)
        old_state["last_used_at"] = (datetime.now(opencode_save.JST) - timedelta(days=35)).isoformat()
        opencode_save.state_path(session_id).write_text(json.dumps(old_state), encoding="utf-8")
        opencode_save.cleanup_old_states()
        self.assertFalse(opencode_save.state_path(session_id).exists())

        second_payload = {
            "session_id": session_id,
            "cwd": "/tmp/example",
            "messages": [
                {"info": {"id": "u2", "role": "user", "time": {"created": 1722988860000}}, "parts": [{"type": "text", "text": "2回目の質問"}]},
                {"info": {"id": "a2", "role": "assistant", "time": {"created": 1722988865000}}, "parts": [{"type": "text", "text": "2回目の回答"}]},
            ],
        }
        res_path = opencode_save.handle_payload(second_payload)
        self.assertEqual(res_path, md_path)
        content = md_path.read_text(encoding="utf-8")
        self.assertIn("1回目の質問", content)
        self.assertIn("1回目の回答", content)
        self.assertIn("2回目の質問", content)
        self.assertIn("2回目の回答", content)
        self.assertIn("message_count: 4", content)


class TestSharedStateRetention(unittest.TestCase):
    def test_last_use_and_mtime_fallback_across_all_scripts(self):
        import json
        from datetime import datetime, timedelta
        from unittest.mock import patch
        import codex_save
        import claude_save
        import agy_save

        for module in (codex_save, claude_save, agy_save, opencode_save):
            with self.subTest(script=module.__name__), tempfile.TemporaryDirectory() as root:
                with patch.object(module, "STATE_DIR", Path(root)):
                    now = datetime.now(module.JST)
                    old = now - timedelta(days=35)
                    expired = module.state_path("expired")
                    expired.write_text(json.dumps({"last_used_at": old.isoformat()}))
                    legacy = module.state_path("legacy")
                    legacy.write_text(json.dumps({"created": now.isoformat(), "start_time": now.isoformat()}))
                    os.utime(legacy, (old.timestamp(), old.timestamp()))
                    active = module.state_path("active")
                    active.write_text(json.dumps({"last_used_at": now.isoformat()}))
                    os.utime(active, (old.timestamp(), old.timestamp()))
                    long_id = "x" * 120
                    locked = module.state_path(long_id)
                    locked.write_text(json.dumps({"last_used_at": old.isoformat()}))
                    with module.session_lock(long_id):
                        module.cleanup_old_states()
                    self.assertFalse(expired.exists())
                    self.assertFalse(legacy.exists())
                    self.assertTrue(active.exists())
                    self.assertTrue(locked.exists())


if __name__ == "__main__":
    unittest.main()
