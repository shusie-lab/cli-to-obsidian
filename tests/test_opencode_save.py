import os
import sys
import tempfile
import unittest
from io import BytesIO
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
            "# Snapshot question\n"
            "> [!QUESTION] User\n"
            "> <small>⏱ 2026-09-14 10:00:00</small>\n"
            ">\n"
            "> Snapshot question\n\n"
            "> [!NOTE] OpenCode\n"
            "> <small>🤖 snapshot-model</small>\n\n"
            "Snapshot answer\n\n"
        )
        self.assertEqual(actual, expected)

    def test_openrouter_balance_is_recorded_once_per_answer(self):
        payload = {
            "session_id": "ses_openrouter_balance",
            "cwd": "/tmp/example",
            "messages": [
                {"info": {"id": "u1", "role": "user"}, "parts": [{"type": "text", "text": "質問"}]},
                {
                    "info": {
                        "id": "a1",
                        "role": "assistant",
                        "providerID": "openrouter",
                        "modelID": "test-model",
                    },
                    "parts": [{"type": "text", "text": "回答"}],
                },
            ],
        }
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-secret"}), mock.patch.object(
            opencode_save.urllib_request,
            "urlopen",
            return_value=BytesIO(b'{"data":{"total_credits":10,"total_usage":1.25}}'),
        ) as urlopen:
            path = opencode_save.handle_payload(payload)
            opencode_save.handle_payload(payload)

        self.assertEqual(urlopen.call_count, 1)
        state = opencode_save.load_state("ses_openrouter_balance")
        answer = state["messages"][1]
        self.assertEqual(answer["provider"], "openrouter")
        self.assertEqual(answer["openrouter_balance_usd"], 8.75)
        self.assertTrue(answer["openrouter_balance_retrieved_at"])
        content = path.read_text(encoding="utf-8")
        self.assertIn("OpenRouter balance: $8.75 USD", content)
        self.assertIn("openrouter_balance_usd: 8.75", content)
        self.assertIn("openrouter_balance_retrieved_at:", content)
        recovered = opencode_save.parse_existing_markdown(path)
        self.assertEqual(recovered[1]["text"], "回答")
        self.assertEqual(recovered[1]["openrouter_balance_usd"], 8.75)

    def test_other_provider_and_latest_non_openrouter_do_not_fetch_balance(self):
        payload = {
            "session_id": "ses_other_provider",
            "cwd": "/tmp/example",
            "messages": [
                {"info": {"id": "u1", "role": "user"}, "parts": [{"type": "text", "text": "質問"}]},
                {
                    "info": {"id": "a1", "role": "assistant", "providerID": "openrouter"},
                    "parts": [{"type": "text", "text": "過去の回答"}],
                },
                {"info": {"id": "u2", "role": "user"}, "parts": [{"type": "text", "text": "質問2"}]},
                {
                    "info": {"id": "a2", "role": "assistant", "providerID": "anthropic"},
                    "parts": [{"type": "text", "text": "最新の回答"}],
                },
            ],
            "full_sync": True,
        }
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-secret"}), mock.patch.object(
            opencode_save.urllib_request, "urlopen"
        ) as urlopen:
            opencode_save.handle_payload(payload)
        urlopen.assert_not_called()
        state = opencode_save.load_state("ses_other_provider")
        self.assertNotIn("openrouter_balance_usd", state["messages"][1])
        self.assertEqual(state["messages"][3]["provider"], "anthropic")

    def test_full_sync_fetches_only_newest_openrouter_answer(self):
        first = {
            "session_id": "ses_full_openrouter_balance",
            "cwd": "/tmp/example",
            "full_sync": True,
            "messages": [
                {"info": {"id": "u1", "role": "user"}, "parts": [{"type": "text", "text": "質問1"}]},
                {"info": {"id": "a1", "role": "assistant", "providerID": "openrouter"}, "parts": [{"type": "text", "text": "回答1"}]},
                {"info": {"id": "u_old", "role": "user"}, "parts": [{"type": "text", "text": "質問別provider"}]},
                {"info": {"id": "a_old", "role": "assistant", "providerID": "anthropic"}, "parts": [{"type": "text", "text": "回答別provider"}]},
            ],
        }
        second = {
            **first,
            "messages": first["messages"] + [
                {"info": {"id": "u2", "role": "user"}, "parts": [{"type": "text", "text": "質問2"}]},
                {"info": {"id": "a2", "role": "assistant", "providerID": "openrouter"}, "parts": [{"type": "text", "text": "回答2"}]},
            ],
        }
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-secret"}), mock.patch.object(
            opencode_save.urllib_request,
            "urlopen",
            return_value=BytesIO(b'{"data":{"total_credits":10,"total_usage":2}}'),
        ) as urlopen:
            opencode_save.handle_payload(first)
            opencode_save.handle_payload(second)
        self.assertEqual(urlopen.call_count, 1)
        messages = opencode_save.load_state("ses_full_openrouter_balance")["messages"]
        self.assertNotIn("openrouter_balance_usd", messages[1])
        self.assertEqual(messages[5]["openrouter_balance_usd"], 8.0)

    def test_state_loss_does_not_attach_new_balance_to_old_answer(self):
        payload = {
            "session_id": "ses_openrouter_state_loss",
            "cwd": "/tmp/example",
            "messages": [
                {"info": {"id": "u1", "role": "user"}, "parts": [{"type": "text", "text": "質問"}]},
                {"info": {"id": "a1", "role": "assistant", "providerID": "openrouter"}, "parts": [{"type": "text", "text": "回答"}]},
            ],
        }
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-secret"}), mock.patch.object(
            opencode_save.urllib_request,
            "urlopen",
            side_effect=[
                BytesIO(b'{"data":{"total_credits":10,"total_usage":1}}'),
                BytesIO(b'{"data":{"total_credits":10,"total_usage":2}}'),
            ],
        ) as urlopen:
            path = opencode_save.handle_payload(payload)
            opencode_save.state_path("ses_openrouter_state_loss").unlink()
            opencode_save.handle_payload(payload)
        self.assertEqual(urlopen.call_count, 1)
        self.assertIn("OpenRouter balance: $9 USD", path.read_text(encoding="utf-8"))
        self.assertNotIn("$8 USD", path.read_text(encoding="utf-8"))

    def test_state_loss_same_text_new_timestamp_gets_balance_only_on_new_answer(self):
        def payload(user_id, assistant_id, timestamp):
            return {
                "session_id": "ses_openrouter_same_text_new_turn",
                "cwd": "/tmp/example",
                "messages": [
                    {
                        "info": {"id": user_id, "role": "user", "time": {"created": timestamp}},
                        "parts": [{"type": "text", "text": "同じ質問"}],
                    },
                    {
                        "info": {
                            "id": assistant_id,
                            "role": "assistant",
                            "providerID": "openrouter",
                            "time": {"created": timestamp + 5000},
                        },
                        "parts": [{"type": "text", "text": "同じ回答"}],
                    },
                ],
            }

        with mock.patch.dict(os.environ, {}, clear=True):
            path = opencode_save.handle_payload(payload("u1", "a1", 1722988800000))
        opencode_save.state_path("ses_openrouter_same_text_new_turn").unlink()
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-secret"}), mock.patch.object(
            opencode_save.urllib_request,
            "urlopen",
            return_value=BytesIO(b'{"data":{"total_credits":10,"total_usage":2}}'),
        ) as urlopen:
            opencode_save.handle_payload(payload("u2", "a2", 1722988860000))

        self.assertEqual(urlopen.call_count, 1)
        state_messages = opencode_save.load_state("ses_openrouter_same_text_new_turn")["messages"]
        self.assertEqual(len(state_messages), 4)
        self.assertNotIn("openrouter_balance_usd", state_messages[1])
        self.assertEqual(state_messages[3]["openrouter_balance_usd"], 8.0)
        self.assertIn("message_count: 4", path.read_text(encoding="utf-8"))

    def test_old_markdown_without_balance_is_not_refreshed_on_duplicate_recovery(self):
        payload = {
            "session_id": "ses_openrouter_old_markdown",
            "cwd": "/tmp/example",
            "messages": [
                {"info": {"id": "u1", "role": "user"}, "parts": [{"type": "text", "text": "過去の質問"}]},
                {"info": {"id": "a1", "role": "assistant", "providerID": "openrouter"}, "parts": [{"type": "text", "text": "過去の回答"}]},
            ],
        }
        with mock.patch.dict(os.environ, {}, clear=True):
            path = opencode_save.handle_payload(payload)
        opencode_save.state_path("ses_openrouter_old_markdown").unlink()
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-secret"}), mock.patch.object(
            opencode_save.urllib_request, "urlopen"
        ) as urlopen:
            opencode_save.handle_payload(payload)
        urlopen.assert_not_called()
        self.assertNotIn("OpenRouter balance", path.read_text(encoding="utf-8"))

    def test_invalid_openrouter_credits_do_not_break_save(self):
        invalid_responses = (
            b'{"data":{"total_credits":true,"total_usage":0}}',
            b'{"data":{"total_credits":"10","total_usage":0}}',
            b'{"data":{"total_credits":NaN,"total_usage":0}}',
            b'{"data":{"total_credits":Infinity,"total_usage":0}}',
            b'{"data":{"total_credits":10,"total_usage":Infinity}}',
            b'{"data":{"total_credits":10,"total_usage":0}',
            b"not-json",
        )
        for response in invalid_responses:
            with self.subTest(response=response), mock.patch.dict(
                os.environ, {"OPENROUTER_API_KEY": "test-secret"}
            ), mock.patch.object(opencode_save.urllib_request, "urlopen", return_value=BytesIO(response)):
                self.assertIsNone(opencode_save.fetch_openrouter_balance())

        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-secret"}), mock.patch.object(
            opencode_save.urllib_request, "urlopen", side_effect=TimeoutError("timed out")
        ):
            self.assertIsNone(opencode_save.fetch_openrouter_balance())

        huge_int = b'{"data":{"total_credits":' + (b"9" * 5000) + b',"total_usage":0}}'
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-secret"}), mock.patch.object(
            opencode_save.urllib_request, "urlopen", return_value=BytesIO(huge_int)
        ):
            self.assertIsNone(opencode_save.fetch_openrouter_balance())

    def test_missing_key_and_http_403_do_not_call_or_record_balance(self):
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
            opencode_save.urllib_request, "urlopen"
        ) as urlopen:
            self.assertIsNone(opencode_save.fetch_openrouter_balance())
        urlopen.assert_not_called()

        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-secret"}), mock.patch.object(
            opencode_save.urllib_request,
            "urlopen",
            side_effect=opencode_save.urllib_error.HTTPError(
                opencode_save.OPENROUTER_CREDITS_URL, 403, "forbidden", {}, None
            ),
        ):
            self.assertIsNone(opencode_save.fetch_openrouter_balance())

    def test_api_failure_still_saves_answer_without_secret(self):
        payload = {
            "session_id": "ses_openrouter_failure",
            "cwd": "/tmp/example",
            "messages": [
                {"info": {"id": "u1", "role": "user"}, "parts": [{"type": "text", "text": "質問"}]},
                {"info": {"id": "a1", "role": "assistant", "providerID": "openrouter"}, "parts": [{"type": "text", "text": "回答"}]},
            ],
        }
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-secret"}), mock.patch.object(
            opencode_save.urllib_request, "urlopen", side_effect=TimeoutError("timed out")
        ):
            path = opencode_save.handle_payload(payload)
        content = path.read_text(encoding="utf-8")
        self.assertIn("回答", content)
        self.assertNotIn("test-secret", content)
        self.assertNotIn("openrouter_balance_usd", content)

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
        self.assertEqual(content.count("回答"), 1)
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
            res = opencode_save.handle_payload(payload)
            self.assertIsNone(res)

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
