import json
import io
import os
import subprocess
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

    def test_incremental_stdin_requires_an_object(self):
        for module in (codex_save, claude_save, agy_save):
            for raw in ('null', '[]', '1', '"text"', '{broken'):
                with self.subTest(provider=module.__name__, raw=raw), mock.patch.object(
                        module.sys, 'stdin', io.StringIO(raw)):
                    self.assertEqual(module.read_stdin(), {})

    def test_claude_statusline_cost_without_subscription_quota(self):
        data = {'session_id': 'api-only', 'cost': {'total_cost_usd': 0.01234}}
        self.assertEqual(claude_save.capture_statusline_quota(data), 'CLI概算: $0.01234')
        self.assertEqual(claude_save.load_state('api-only')['reported_cost']['usd'], '0.01234')
        data['rate_limits'] = {'five_hour': {'used_percentage': 20}}
        label = claude_save.capture_statusline_quota(data)
        self.assertIn('80.0%', label)
        self.assertIn('CLI概算: $0.01234', label)
        for bad in (True, -1, 'NaN', 'Infinity', {}, None):
            with self.subTest(cost=bad):
                self.assertEqual(claude_save.capture_statusline_quota(
                    {'cost': {'total_cost_usd': bad}}), '')
        self.assertEqual(claude_save.capture_statusline_quota(
            {'cost': {'total_cost_usd': 0}}), 'CLI概算: $0')

    def test_claude_fable_ttl_cost_and_unknown_conditions(self):
        raw = {'input_tokens': 100, 'output_tokens': 10,
               'cache_read_input_tokens': 50, 'cache_creation_input_tokens': 30,
               'cache_creation': {'ephemeral_5m_input_tokens': 20, 'ephemeral_1h_input_tokens': 10}}
        for model in ('claude-fable-5-1', 'anthropic/claude-fable-5.1', 'claude-fable-5-1-20261022'):
            with self.subTest(model=model):
                record = claude_save._claude_usage_record({'model': model, 'usage': raw})
                self.assertEqual(record['cost_usd'], claude_save.Decimal('0.0019625'))
                self.assertEqual(record['total_tokens'], 190)
        for override in ({'speed': 'fast'}, {'inference_geo': 'us'}, {'cache_creation': {}}):
            record = claude_save._claude_usage_record(
                {'model': 'claude-fable-5-1', 'usage': dict(raw, **override)})
            self.assertNotIn('cost_usd', record)

    def test_claude_stop_wait_ignores_sidechain_answers(self):
        transcript = self.test_dir / 'sidechain.jsonl'
        message = {'model': 'claude-fable-5-1', 'content': '親回答',
                   'usage': {'input_tokens': 10, 'output_tokens': 5}}
        rows = [{'type': 'assistant', 'message': message},
                {'type': 'assistant', 'isSidechain': True,
                 'message': dict(message, content='子回答')}]
        text = ''.join(json.dumps(row) + '\n' for row in rows)
        transcript.write_text(text)
        with mock.patch.object(claude_save.logger, 'warning') as warning:
            self.assertEqual(claude_save.read_stop_transcript(transcript, '親回答'), text)
        warning.assert_not_called()

    def test_malformed_json_rows_do_not_lose_valid_codex_and_claude_messages(self):
        noise = [None, [], 42, 'text', {'type': 'event_msg', 'payload': None},
                 {'type': 'assistant', 'message': None}]
        cases = (
            (codex_save, [
                {'type': 'turn_context', 'payload': {'model': 'gpt-6-luna'}},
                {'type': 'event_msg', 'payload': {'type': 'user_message', 'message': '質問'}},
                {'type': 'event_msg', 'payload': {'type': 'agent_message', 'message': '正常回答'}},
                {'type': 'token_usage_record', 'payload': {'usage': {'input_tokens': 10,
                    'output_tokens': 5, 'total_tokens': 15}}}]),
            (claude_save, [
                {'type': 'user', 'message': {'content': [{'type': 'text', 'text': None},
                    {'type': 'text', 'text': '質問'}]}},
                {'type': 'assistant', 'message': {'id': 'answer', 'model': 'claude-fable-5-1',
                    'content': [{'type': 'text', 'text': {}}, {'type': 'text', 'text': '正常回答'}],
                    'usage': {'input_tokens': 10, 'output_tokens': 5}}}]),
        )
        for module, rows in cases:
            with self.subTest(provider=module.__name__):
                transcript = self.test_dir / (module.__name__ + '.jsonl')
                transcript.write_text(''.join(json.dumps(row) + '\n' for row in noise + rows))
                hook = {'session_id': 'malformed', 'transcript_path': str(transcript), 'cwd': str(self.test_dir)}
                module.handle_stop_event(hook)
                path = Path(module.load_state('malformed')['output_path'])
                content = path.read_text()
                self.assertIn('正常回答', content)
                self.assertIn('total_tokens: 15', content)
                self.assertIn('total_cost_usd:', content)
                module.handle_stop_event(hook)
                self.assertEqual(path.read_text(), content)

    def test_codex_first_hook_preserves_first_and_last_quota_in_batch(self):
        transcript = self.test_dir / "quota-batch.jsonl"
        rows = [{"type": "event_msg", "payload": {"type": "user_message", "message": "質問"}}]
        for stamp, used in (("2026-09-26T01:00:00Z", 10), ("2026-09-26T01:05:00Z", 20)):
            rows.append({"type": "event_msg", "timestamp": stamp, "payload": {
                "type": "token_count", "rate_limits": {"primary": {
                    "window_minutes": 300, "used_percent": used}}}})
        transcript.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        codex_save.handle_stop_event({"session_id": "quota-batch", "transcript_path": str(transcript),
                                      "cwd": str(self.test_dir)})
        state = codex_save.load_state("quota-batch")
        content = Path(state["output_path"]).read_text(encoding="utf-8")
        first, last = codex_save.recover_quota_frontmatter(content)
        self.assertEqual(first["codex"]["5h"]["remaining"], 90)
        self.assertEqual(last["codex"]["5h"]["remaining"], 80)
        self.assertEqual(first["codex"]["5h"]["observed_at"], "2026-09-26T01:00:00Z")

    def test_claude_unchanged_quota_refreshes_last_observation_time(self):
        data = {"session_id": "quota-time", "rate_limits": {
            "five_hour": {"used_percentage": 20}}}
        early = datetime(2026, 9, 26, 10, tzinfo=JST)
        late = early + timedelta(minutes=5)
        with mock.patch.object(claude_save, "now_jst", return_value=early):
            claude_save.capture_statusline_quota(data)
        with mock.patch.object(claude_save, "now_jst", return_value=late):
            claude_save.capture_statusline_quota(data)
        state = claude_save.load_state("quota-time")
        self.assertEqual(state["initial_quota"]["claude"]["5h"]["observed_at"], early.isoformat())
        self.assertEqual(state["final_quota"]["claude"]["5h"]["observed_at"], late.isoformat())

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

    def test_source_name_default_override_and_invalid_values(self):
        for module, default in (
            (codex_save, "codex-cli"),
            (claude_save, "claude-code"),
            (agy_save, "antigravity-cli"),
            (opencode_save, "opencode"),
        ):
            with self.subTest(module=module.__name__):
                with mock.patch.dict(os.environ, {}, clear=False):
                    os.environ.pop("OBSIDIAN_SOURCE", None)
                    self.assertEqual(module.resolve_source_name(default), default)
                with mock.patch.dict(os.environ, {"OBSIDIAN_SOURCE": "device-pi4"}):
                    self.assertEqual(module.resolve_source_name(default), "device-pi4")
                for value in ("", ".", "..", "../outside", "codex/pi4", "codex pi4", "x" * 65):
                    self.assertEqual(module.resolve_source_name(default, value), default)

    def test_source_override_controls_output_directory_and_frontmatter(self):
        script = """
import agy_save, claude_save, codex_save, opencode_save
from datetime import datetime, timezone
from pathlib import Path
now = datetime.now(timezone.utc)
for module in (agy_save, claude_save, codex_save, opencode_save):
    assert module.OUTPUT_BASE == Path(__import__('os').environ['OBSIDIAN_VAULT']) / 'ChatLog' / 'device-pi4'
    if module is opencode_save:
        frontmatter = module.build_frontmatter('session', '/project', now, now, 0)
    else:
        frontmatter = module.build_frontmatter('session', '/project', now)
    assert 'source: device-pi4\\n' in frontmatter
    assert '  - device-pi4\\n' in frontmatter
"""
        env = os.environ.copy()
        env.update({
            "OBSIDIAN_VAULT": str(self.test_dir),
            "OBSIDIAN_OUTPUT_DIR": "ChatLog",
            "OBSIDIAN_SOURCE": "device-pi4",
            "HOME": str(self.test_dir),
        })
        result = subprocess.run(
            ["/usr/bin/python3", "-c", script],
            cwd=Path(__file__).parent.parent,
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

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

    def test_codex_question_reply_is_rendered_as_assistant_continuation(self):
        reply = (
            codex_save.QUESTION_REPLY_START
            + '\n[{"question":"配布方法は？","answer":"Pages（推奨）"}]\n'
            + codex_save.QUESTION_REPLY_END
        )
        self.assertEqual(
            codex_save.parse_question_reply(reply),
            "**ユーザーの選択**\n\n- 配布方法は？ → Pages（推奨）",
        )

        for malformed in (
            "質問だけ",
            codex_save.QUESTION_REPLY_START + "\nnot-json\n" + codex_save.QUESTION_REPLY_END,
            codex_save.QUESTION_REPLY_START + "\n[]\n" + codex_save.QUESTION_REPLY_END,
            codex_save.QUESTION_REPLY_START
            + '\n[{"question":"質問","answer":""}]\n'
            + codex_save.QUESTION_REPLY_END,
        ):
            self.assertIsNone(codex_save.parse_question_reply(malformed))

    def test_codex_question_reply_item_completed_is_not_user_callout(self):
        transcript = self.test_dir / "codex-question-reply.jsonl"
        reply = (
            codex_save.QUESTION_REPLY_START
            + '\n[{"question":"配布方法は？","answer":"Pages（推奨）"}]\n'
            + codex_save.QUESTION_REPLY_END
        )
        transcript.write_text(
            "".join(
                json.dumps(row, ensure_ascii=False) + "\n"
                for row in (
                    {
                        "type": "event_msg",
                        "timestamp": "2026-09-16T01:00:00Z",
                        "payload": {
                            "type": "item_completed",
                            "item": {
                                "type": "AgentMessage",
                                "content": [{"type": "Text", "text": "配布方法を選んでください。"}],
                            },
                        },
                    },
                    {
                        "type": "event_msg",
                        "timestamp": "2026-09-16T01:00:01Z",
                        "payload": {
                            "type": "item_completed",
                            "item": {
                                "type": "UserMessage",
                                "content": [{"type": "text", "text": reply}],
                            },
                        },
                    },
                )
            ),
            encoding="utf-8",
        )

        messages, last_line = codex_save.load_jsonl_messages(transcript, 0)

        self.assertEqual(last_line, 2)
        self.assertEqual([message["type"] for message in messages], ["agent_message", "agent_message"])
        self.assertIn("ユーザーの選択", messages[1]["message"])

        output_path = codex_save.create_md_file("codex-question-reply", "myproj", datetime.now(JST))
        codex_save.append_messages(output_path, messages, datetime.now(JST), "gpt-5", last_line)
        content = output_path.read_text(encoding="utf-8")
        self.assertNotIn("> [!QUESTION] User", content)
        self.assertEqual(content.count("> [!NOTE] Codex"), 1)
        self.assertIn("**ユーザーの選択**", content)

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
        self.assertIn("# 表示する依頼", content)
        self.assertIn('title: "表示する依頼"', content)
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
        batch_body = batch_content[batch_content.index("# "):]
        split_body = split_content[split_content.index("# "):]
        self.assertEqual(batch_body, split_body)
        self.assertEqual(batch_body.count("> [!QUESTION] User"), 2)
        self.assertIn("# 次の依頼", batch_body)

    def test_incremental_savers_ignore_fenced_fake_callout_headers(self):
        content = """> [!NOTE] Codex
> <small>🤖 gpt-5</small>

```markdown
> [!QUESTION] fake question
> [!NOTE] fake answer
```
"""
        for save_module in (codex_save, claude_save, agy_save):
            self.assertEqual(
                save_module.get_last_callout_header(content),
                "> [!NOTE] Codex\n> <small>🤖 gpt-5</small>",
            )

    def test_user_heading_escapes_markdown_special_characters(self):
        message = "*_save.py [確認] (a) #tag"
        expected = r"\*\_save\.py \[確認\] \(a\) \#tag"

        for save_module in (codex_save, claude_save, agy_save, opencode_save):
            escape_heading = (
                save_module.escape_markdown_heading
                if hasattr(save_module, "escape_markdown_heading")
                else save_module.escape_heading
            )
            self.assertEqual(escape_heading(message), expected)

    def test_common_message_metadata_formatters(self):
        for save_module in (codex_save, claude_save, agy_save, opencode_save):
            self.assertEqual(
                save_module.format_message_time("2026-08-07T10:00:00"),
                "> <small>⏱ 2026-08-07 19:00:00</small>\n>\n",
            )
            self.assertEqual(
                save_module.user_heading("\n*_save.py [確認]"),
                "# \\*\\_save\\.py \\[確認\\]\n",
            )
            self.assertEqual(save_module.user_heading(r"\[確認\]"), "# \\[確認\\]\n")
            self.assertEqual(
                save_module.callout_lines("<tag>\n  > quoted\n"),
                "> &lt;tag>\n>   &gt; quoted\n>",
            )
            self.assertEqual(save_module.user_heading("\n"), "# User\n")

    def test_callout_preserves_code_regions_while_escaping_normal_markdown(self):
        text = (
            "<normal>\n"
            "`<inline>` and `> quoted`\n"
            "```text\n"
            "<fenced>\n"
            "> raw\n"
            "```\n"
            "> > ```text\n"
            "> > [nested](/tmp/nested.md)\n"
            "> > <nested>\n"
            "> > ```\n"
            "  > normal\n"
        )
        expected = (
            "> &lt;normal>\n"
            "> `<inline>` and `> quoted`\n"
            "> ```text\n"
            "> <fenced>\n"
            "> > raw\n"
            "> ```\n"
            "> > > ```text\n"
            "> > > [nested](/tmp/nested.md)\n"
            "> > > <nested>\n"
            "> > > ```\n"
            ">   &gt; normal\n"
            ">"
        )
        for save_module in (codex_save, claude_save, agy_save, opencode_save):
            self.assertEqual(save_module.callout_lines(text), expected)

    def test_common_message_blocks_have_the_same_contract(self):
        expected_user = (
            "# \*確認\*\n"
            "> [!QUESTION] User\n"
            "> <small>⏱ 2026-08-07 19:00:00</small>\n"
            ">\n"
            "> *確認*\n\n"
        )
        expected_assistant = (
            "> [!NOTE] Agent\n"
            "> <small>🤖 model</small>\n\n"
            "回答\n\n"
        )
        for save_module in (codex_save, claude_save, agy_save, opencode_save):
            self.assertEqual(
                save_module.render_user_message_block("*確認*", "2026-08-07T10:00:00"),
                expected_user,
            )
            self.assertEqual(
                save_module.render_assistant_message_block("回答", "Agent", ["🤖 model"]),
                expected_assistant,
            )
            self.assertEqual(
                save_module.render_assistant_message_block(
                    "回答",
                    "Agent",
                    ["🤖 model"],
                    extra_blank_line=True,
                ),
                expected_assistant.replace("</small>\n\n", "</small>\n\n\n"),
            )

    def test_assistant_local_file_links_are_normalized_for_all_savers(self):
        text = (
            "[README](/tmp/My Project/README.md)\n"
            "[既存URI](file:///tmp/existing.md)\n"
            "`[コード](/tmp/code.py)`\n"
            "```text\n"
            "[例](/tmp/example.txt)\n"
            "```\n"
            "> ```text\n"
            "> [入れ子](/tmp/nested.txt)\n"
            "> ```\n"
            "[行](</tmp/space.md>)"
        )
        expected = (
            "[README](file:///tmp/My%20Project/README.md)\n"
            "[既存URI](file:///tmp/existing.md)\n"
            "`[コード](/tmp/code.py)`\n"
            "```text\n"
            "[例](/tmp/example.txt)\n"
            "```\n"
            "> ```text\n"
            "> [入れ子](/tmp/nested.txt)\n"
            "> ```\n"
            "[行](<file:///tmp/space.md>)"
        )
        for save_module in (codex_save, claude_save, agy_save, opencode_save):
            self.assertEqual(save_module.normalize_local_file_links(text), expected)

    def test_title_comes_from_first_user_heading(self):
        now = datetime(2026, 8, 7, 10, 0, tzinfo=JST)
        content = (
            "---\n"
            "source: claude-code\n"
            "message_count: 0\n"
            "---\n\n"
            "<!-- last_line: 0 -->\n\n"
            "# 最初の発話です\n"
            "> [!QUESTION] User\n"
            "> 本文\n\n"
        )
        updated = claude_save.update_markdown_metadata(content, now, 1, 1)
        self.assertIn('title: "最初の発話です"', updated)
        self.assertNotIn('title: "User:', updated)

    def test_title_does_not_use_markdown_heading_escaping(self):
        message = "修正対象: foo-bar *確認* [重要]"
        for save_module in (codex_save, claude_save, agy_save, opencode_save):
            self.assertEqual(save_module.heading_title(message), message)
            self.assertEqual(save_module.heading_title("\n"), "User")
            self.assertEqual(
                save_module.user_heading(message),
                r"# 修正対象: foo\-bar \*確認\* \[重要\]" + "\n",
            )

        content = (
            "---\n"
            "source: claude-code\n"
            "message_count: 0\n"
            "---\n\n"
            "# 修正対象: foo\\-bar \\*確認\\* \\[重要\\]\n"
        )
        updated = claude_save.update_markdown_metadata(content, datetime.now(JST), 1, 1)
        self.assertIn('title: "修正対象: foo-bar *確認* [重要]"', updated)

    def test_incremental_metadata_updates_are_pure(self):
        content = (
            "---\n"
            'modified: "old"\n'
            "message_count: 1\n"
            "---\n"
            "<!-- last_line: 1 -->\n"
            "<!-- last_id: 1 -->\n\n"
            "body\n"
        )
        modified = datetime(2026, 8, 7, 10, 0, tzinfo=JST)
        codex_result = codex_save.update_markdown_metadata(content, modified, 2, 3, None, None)
        claude_result = claude_save.update_markdown_metadata(content, modified, 2, 3)
        agy_result = agy_save.update_markdown_metadata(content, modified, 2, "3")
        for result in (codex_result, claude_result, agy_result):
            self.assertIn("message_count: 3", result)
            self.assertIn('modified: "2026-08-07T10:00:00+09:00"', result)
        self.assertIn("<!-- last_line: 3 -->", codex_result)
        self.assertIn("<!-- last_line: 3 -->", claude_result)
        self.assertIn("<!-- last_id: 3 -->", agy_result)

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

    def test_codex_streaming_usage_stays_in_one_callout(self):
        now = datetime.now(JST)
        md_path = codex_save.create_md_file("streaming-usage", "myproj", now)
        messages = [
            {"line_number": 1, "type": "agent_message", "message": "part 1", "model": "gpt-5.6-luna",
             "usage": {"input_tokens": 10, "cached_input_tokens": 0, "cache_write_input_tokens": 0,
                       "output_tokens": 2, "reasoning_output_tokens": 0, "total_tokens": 12,
                       "by_model": {"gpt-5.6-luna": {"input_tokens": 10, "cached_input_tokens": 0,
                       "cache_write_input_tokens": 0, "output_tokens": 2, "reasoning_output_tokens": 0,
                       "total_tokens": 12, "cost_usd": codex_save.Decimal("0.0000044")}},
                       "total_cost_usd": codex_save.Decimal("0.0000044")}},
            {"line_number": 2, "type": "agent_message", "message": "part 2", "model": "gpt-5.6-luna",
             "usage": {"input_tokens": 20, "cached_input_tokens": 0, "cache_write_input_tokens": 0,
                       "output_tokens": 3, "reasoning_output_tokens": 0, "total_tokens": 23,
                       "by_model": {"gpt-5.6-luna": {"input_tokens": 20, "cached_input_tokens": 0,
                       "cache_write_input_tokens": 0, "output_tokens": 3, "reasoning_output_tokens": 0,
                       "total_tokens": 23, "cost_usd": codex_save.Decimal("0.0000076")}},
                       "total_cost_usd": codex_save.Decimal("0.0000076")}},
        ]
        appended, updated = codex_save.append_messages(md_path, messages, now, "gpt-5.6-luna", 2)
        self.assertTrue(updated)
        self.assertEqual(appended, 1)
        content = md_path.read_text(encoding="utf-8")
        self.assertEqual(content.count("> [!NOTE] Codex"), 1)
        self.assertIn("part 1\npart 2", content)
        self.assertIn("Usage: 35 tok · $0.000012", content)

    def test_codex_late_usage_preserves_one_callout_and_repairs_split_headers(self):
        now = datetime.now(JST)
        path = codex_save.create_md_file("late-split", "myproj", now)

        def summary(tokens, cost):
            return {"total_tokens": tokens, "total_cost_usd": codex_save.Decimal(cost), "by_model": {}}

        user = {"line_number": 1, "type": "user_message", "message": "Question"}
        first = {"line_number": 2, "type": "agent_message", "message": "part 1",
                 "model": "gpt-6-sol", "usage": summary(10, "0.00002")}
        second = {"line_number": 3, "type": "agent_message", "message": "part 2",
                  "model": "gpt-6-sol", "usage": summary(20, "0.00004")}
        codex_save.append_messages(path, [user, first], now, "gpt-6-sol", 2)
        codex_save.append_messages(path, [second], now, "gpt-6-sol", 3)
        content = path.read_text(encoding="utf-8")
        self.assertEqual(content.count("> [!NOTE] Codex"), 1)

        legacy_split = content.replace(
            "part 2\n\n",
            "> [!NOTE] Codex\n> <small>🤖 gpt-6-sol</small>\n"
            "> <small>Usage: 20 tok · $0.00004</small>\n\npart 2\n\n",
        )
        self.assertEqual(legacy_split.count("> [!NOTE] Codex"), 2)
        second["usage"] = summary(35, "0.000055")
        second["subagent_usage"] = {
            **summary(15, "0.000015"),
            "by_model": {"gpt-6-luna": {
                "total_tokens": 15, "cost_usd": codex_save.Decimal("0.000015"),
            }},
        }
        refreshed = codex_save.refresh_answer_usage_metadata(legacy_split, [user, first, second])
        self.assertEqual(refreshed.count("Usage:"), 1)
        self.assertIn(
            "Usage: 45 tok · $0.000075 (gpt-6-luna 15 tok・$0.000015)", refreshed,
        )
        self.assertLess(refreshed.index("part 1"), refreshed.index("Usage: 45 tok"))
        self.assertLess(refreshed.index("Usage: 45 tok"), refreshed.index("part 2"))

        mixed = codex_save.format_message_usage(
            {"total_tokens": 50, "total_cost_usd": codex_save.Decimal("0.000015")},
            {"by_model": {
                "gpt-6-luna": {"total_tokens": 30, "cost_usd": codex_save.Decimal("0.000015")},
                "unknown-model": {"total_tokens": 20, "cost_unavailable": True},
            }},
        )
        self.assertEqual(
            mixed,
            "Usage: 50 tok · $0.000015 (gpt-6-luna 30 tok・$0.000015、unknown-model 20 tok・換算不可) · Cache: N/A",
        )

    def test_codex_tool_use_inserts_one_blank_line_before_next_answer(self):
        transcript = self.test_dir / "codex_tool_use.jsonl"
        rows = [
            {"type": "event_msg", "payload": {"type": "agent_message", "message": "ツール前の回答"}},
            {
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "item": {"type": "CommandExecution"},
                },
            },
            {"type": "event_msg", "payload": {"type": "agent_message", "message": "ツール後の回答"}},
        ]
        transcript.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

        messages, last_line = codex_save.load_jsonl_messages(transcript, 0)

        self.assertEqual(last_line, 3)
        self.assertEqual([message["after_tool_use"] for message in messages], [False, True])
        path = codex_save.create_md_file("tool-use", "myproj", datetime.now(JST))
        codex_save.append_messages(path, messages, datetime.now(JST), "gpt-5.6-luna", last_line)
        content = path.read_text(encoding="utf-8")
        self.assertIn("ツール前の回答\n\nツール後の回答", content)
        self.assertNotIn("ツール前の回答\n\n\nツール後の回答", content)

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
        for start_line in (0, 1):
            observed = codex_save.retrieve_quota(jsonl_path, start_line=start_line)
            self.assertEqual(observed["codex"]["weekly"]["remaining"], 75.0)
            self.assertEqual(observed["codex"]["5h"]["remaining"], 60.0)

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
            initial_quota=initial_quota,
        )
        self.assertTrue(updated)
        self.assertEqual(appended, 1)

        content = md_path.read_text(encoding="utf-8")
        self.assertIn("- **Codex**: W: 80.0% ➔ 75.0%", content)
        self.assertIn("5h: 65.0% ➔ 60.0%", content)
        self.assertIn("first_observed:\n        remaining_percent: 80.0", content)
        self.assertIn("last_observed:\n        remaining_percent: 75.0", content)
        self.assertNotIn("(Quota:", content)
        self.assertNotIn("quota: 5.00", content)

    def test_codex_usage_is_summed_per_record_and_model(self):
        transcript = self.test_dir / "usage.jsonl"
        records = [
            {"type": "turn_context", "payload": {"model": "gpt-6-luna"}},
            {
                "type": "token_usage_record",
                "payload": {
                    "usage": {
                        "input_tokens": 100,
                        "cached_input_tokens": 10,
                        "cache_write_input_tokens": 2,
                        "output_tokens": 20,
                        "reasoning_output_tokens": 5,
                        "total_tokens": 120,
                    },
                    "thread_token_usage": {"total_tokens": 999999},
                },
            },
            {"type": "turn_context", "payload": {"model": "gpt-6-astra"}},
            {
                "type": "token_usage_record",
                "payload": {
                    "usage": {
                        "input_tokens": 200,
                        "cached_input_tokens": 20,
                        "cache_write_input_tokens": 0,
                        "output_tokens": 30,
                        "reasoning_output_tokens": 7,
                        "total_tokens": 230,
                    },
                    "thread_token_usage": {"total_tokens": 1000000},
                },
            },
        ]
        transcript.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")

        usage = codex_save.retrieve_usage(transcript)

        self.assertEqual(usage["total_tokens"], 350)
        self.assertEqual(usage["input_tokens"], 300)
        self.assertEqual(usage["by_model"]["gpt-6-luna"]["total_tokens"], 120)
        self.assertEqual(usage["by_model"]["gpt-6-astra"]["total_tokens"], 230)
        # 通常入力（input - cached - cache write）、キャッシュ入力、cache write、
        # 出力を別単価で加算する。reasoning_output_tokens は output_tokens の内数。
        self.assertEqual(str(usage["total_cost_usd"]), "0.00333915")
        self.assertEqual(str(usage["by_model"]["gpt-6-luna"]["cost_usd"]), "0.00001915")
        self.assertEqual(str(usage["by_model"]["gpt-6-astra"]["cost_usd"]), "0.00332")

        # 既定では272K超も通常単価。設定を有効にすると各recordへ長文倍率を適用する。
        long_context = {
            "input_tokens": 272001,
            "cached_input_tokens": 0,
            "cache_write_input_tokens": 0,
            "output_tokens": 10,
        }
        self.assertEqual(str(codex_save._usage_cost("gpt-6-astra", long_context)), "2.72051")
        with mock.patch.object(codex_save, "APPLY_LONG_CONTEXT_PRICING", True):
            self.assertEqual(str(codex_save._usage_cost("gpt-6-astra", long_context)), "5.44077")

        gpt6_usage = {
            "input_tokens": 1000000,
            "cached_input_tokens": 200000,
            "cache_write_input_tokens": 100000,
            "output_tokens": 100000,
        }
        self.assertEqual(codex_save._usage_cost("gpt-6-astra", gpt6_usage), codex_save.Decimal("13.45"))
        self.assertEqual(
            codex_save._usage_cost("gpt-6.1-sol", gpt6_usage), codex_save.Decimal("2.67")
        )
        self.assertEqual(str(codex_save._usage_cost("gpt-6-luna", gpt6_usage)), "0.1345")

        path = codex_save.create_md_file("usage-session", "myproj", datetime.now(JST), usage=usage)
        content = path.read_text(encoding="utf-8")
        self.assertIn("usage:", content)
        self.assertIn("total_cost_usd: 0.00333915", content)
        self.assertIn('"gpt-6-luna":', content)
        self.assertIn("💰 **Usage (USD換算)**: Total: $0.00333915 (gpt-6-luna $0.00001915 · Cache: 10.0%、gpt-6-astra $0.00332 · Cache: 10.0%)", content)

        refreshed = codex_save.update_markdown_metadata(
            content,
            datetime.now(JST),
            0,
            0,
            None,
            None,
            usage,
        )
        self.assertEqual(refreshed.count("💰 **Usage (USD換算)**:"), 1)

        unknown = dict(usage)
        unknown["by_model"] = dict(usage["by_model"])
        unknown["by_model"]["codex-auto-review"] = {
            "total_tokens": 10,
            "cost_unavailable": True,
        }
        # 未知モデルが混在しても既知分の総計は保存し、完全な請求額と誤認しない。
        unknown["cost_incomplete"] = True
        self.assertIn("codex-auto-review 換算不可", codex_save.build_usage_section(unknown))
        self.assertIn("cost_incomplete: true", "\n".join(codex_save.build_usage_frontmatter_fields(unknown)))
        merged = codex_save._merge_usage_summaries(
            usage, {"total_tokens": 10, "by_model": {"codex-auto-review": {
                "total_tokens": 10, "cost_unavailable": True,
            }}, "cost_unavailable": True},
        )
        self.assertTrue(merged["cost_incomplete"])

    def test_claude_usage_deduplicates_message_id_and_marks_unpriced_models(self):
        transcript = self.test_dir / "claude-usage.jsonl"
        priced = {
            "type": "assistant", "message": {"id": "msg-1", "model": "claude-sonnet-5-5",
                "content": [{"type": "text", "text": "回答"}],
                "usage": {"input_tokens": 100, "cache_creation_input_tokens": 20,
                          "cache_read_input_tokens": 50, "output_tokens": 10,
                          "cache_creation": {"ephemeral_5m_input_tokens": 20,
                                             "ephemeral_1h_input_tokens": 0}}},
        }
        unknown = {
            "type": "assistant", "message": {"id": "msg-2", "model": "other/model",
                "content": [{"type": "text", "text": "別回答"}],
                "usage": {"input_tokens": 5, "cache_creation_input_tokens": 0,
                          "cache_read_input_tokens": 0, "output_tokens": 2}},
        }
        transcript.write_text("".join(json.dumps(row) + "\n" for row in [priced, priced, unknown]), encoding="utf-8")
        usage = claude_save.retrieve_usage(transcript)
        self.assertEqual(usage["total_tokens"], 187)
        self.assertEqual(usage["input_tokens"], 175)
        self.assertEqual(usage["by_model"]["claude-sonnet-5-5"]["cost_usd"], claude_save.Decimal("0.000355"))
        self.assertTrue(usage["cost_incomplete"])
        self.assertIn("cost_unavailable: true", "\n".join(claude_save.build_usage_frontmatter_fields(usage)))

    def test_claude_uses_shared_price_registry_and_ttl_rates(self):
        self.assertEqual(claude_save.STANDARD_USAGE_RATES, codex_save.STANDARD_USAGE_RATES)
        self.assertIs(claude_save.USAGE_COST_USD_PER_1M_TOKEN_TYPE, claude_save.STANDARD_USAGE_RATES)
        for rates in claude_save.USAGE_COST_USD_PER_1M_TOKEN_TYPE.values():
            self.assertTrue({"input", "cached_input", "cache_write_input", "output"} <= set(rates))
            self.assertTrue(all(isinstance(rate, claude_save.Decimal) for rate in rates.values()))
        raw = {"input_tokens": 100, "output_tokens": 10}
        self.assertNotIn("cost_usd", claude_save._claude_usage_record(
            {"model": "claude-sonnet-4-6", "usage": raw}))
        # 各token種別は倍率の再計算でなく表の値を直接参照する。
        rates = {"input": claude_save.Decimal("1"), "cached_input": claude_save.Decimal("2"),
                 "cache_write_input": claude_save.Decimal("3"),
                 "cache_write_input_1h": claude_save.Decimal("4"),
                 "output": claude_save.Decimal("5")}
        with mock.patch.dict(claude_save.USAGE_COST_USD_PER_1M_TOKEN_TYPE,
                             {"claude-opus-5-5": rates}):
            usage = claude_save._claude_usage_record({"model": "anthropic/claude-opus-5.5",
                "usage": {"input_tokens": 10, "cache_read_input_tokens": 20,
                          "cache_creation_input_tokens": 70, "output_tokens": 50,
                          "cache_creation": {"ephemeral_5m_input_tokens": 30,
                                             "ephemeral_1h_input_tokens": 40}}})
        self.assertEqual(usage["cost_usd"], claude_save.Decimal("0.00055"))

    def test_claude_openrouter_reference_prices(self):
        raw = {"input_tokens": 100, "cache_read_input_tokens": 50,
               "cache_creation_input_tokens": 30, "output_tokens": 10,
               "cache_creation": {"ephemeral_5m_input_tokens": 20,
                                  "ephemeral_1h_input_tokens": 10}}
        for model in ("anthropic/claude-sonnet-5.5", "claude-sonnet-5-5",
                      "claude-sonnet-5-5-20260928"):
            with self.subTest(model=model):
                usage = claude_save._claude_usage_record({"model": model, "usage": raw})
                # 100*2 + 50*0.1 + 20*2.5 + 10*4 + 10*10
                self.assertEqual(usage["cost_usd"], claude_save.Decimal("0.000395"))
                self.assertEqual(usage["total_tokens"], 190)
        model = "nvidia/nemotron-3-ultra-550b-a55b:free"
        usage = claude_save._claude_usage_record({"model": model, "usage": raw})
        # 100*0.5 + 50*0.1 + 30*0.5 + 10*2.2
        self.assertEqual(usage["cost_usd"], claude_save.Decimal("0.000092"))
        for alias in ("nemotron-3-ultra-550b-a55b:free", "nemotron-3-ultra-free",
                      "nvidia/nemotron-3-ultra-free"):
            with self.subTest(alias=alias):
                self.assertEqual(claude_save._claude_usage_record(
                    {"model": alias, "usage": raw})["cost_usd"], usage["cost_usd"])
                self.assertEqual(claude_save._normalize_usage_model(alias),
                                 opencode_save._normalize_usage_model(alias))
                self.assertEqual(opencode_save._usage_cost(alias, usage), usage["cost_usd"])
        # Anthropic用のTTL情報はNemotronの書き込み単価に影響しない。
        without_ttl = dict(raw)
        without_ttl.pop("cache_creation")
        self.assertEqual(claude_save._claude_usage_record(
            {"model": model, "usage": without_ttl})["cost_usd"], usage["cost_usd"])
        transcript = self.test_dir / "openrouter-claude.jsonl"
        rows = [{"type": "assistant", "message": {"id": str(i), "model": m, "usage": raw}}
                for i, m in enumerate(("anthropic/claude-sonnet-5.5", model))]
        transcript.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        summary = claude_save.retrieve_usage(transcript)
        self.assertEqual(summary["total_cost_usd"], claude_save.Decimal("0.000487"))
        self.assertIn("total_cost_usd: 0.000487", "\n".join(
            claude_save.build_usage_frontmatter_fields(summary)))
        self.assertIn("$0.000487", claude_save.build_usage_section(summary))
        self.assertNotIn("cost_incomplete", summary)

    def test_claude_stop_waits_for_delayed_answer_and_usage(self):
        transcript = self.test_dir / "delayed.jsonl"
        question = json.dumps({"type": "user", "message": {"content": "質問"}}) + "\n"
        answer = {"type": "assistant", "message": {
            "id": "delayed-answer", "model": "claude-sonnet-5-5",
            "content": [{"type": "text", "text": "最終回答"}]}}
        transcript.write_text(question, encoding="utf-8")
        ticks = [0]

        def delayed_write(_):
            ticks[0] += 1
            if ticks[0] == 2:
                transcript.write_text(question + json.dumps(answer) + "\n", encoding="utf-8")
            if ticks[0] == 4:
                answer["message"]["usage"] = {"input_tokens": 100, "output_tokens": 10}
                transcript.write_text(question + json.dumps(answer) + "\n", encoding="utf-8")

        hook = {"session_id": "delayed", "transcript_path": str(transcript),
                "last_assistant_message": "最終回答"}
        with mock.patch.object(claude_save.time, "sleep", side_effect=delayed_write), \
                mock.patch.object(claude_save.time, "monotonic", side_effect=lambda: ticks[0] * 0.05):
            claude_save.handle_stop_event(hook)
        state = claude_save.load_state("delayed")
        content = Path(state["output_path"]).read_text(encoding="utf-8")
        self.assertIn("最終回答", content)
        self.assertIn("total_tokens: 110", content)
        self.assertEqual(state["last_line"], 2)
        claude_save.handle_stop_event(hook)
        self.assertEqual(content, Path(state["output_path"]).read_text(encoding="utf-8"))

    def test_claude_stop_wait_has_a_deadline(self):
        transcript = self.test_dir / "missing-answer.jsonl"
        transcript.write_text('{}\n', encoding="utf-8")
        ticks = [0]

        def advance(_):
            ticks[0] += 1

        with mock.patch.object(claude_save.time, "sleep", side_effect=advance), \
                mock.patch.object(claude_save.time, "monotonic", side_effect=lambda: ticks[0] * 0.05):
            text = claude_save.read_stop_transcript(transcript, "未到着の回答")
        self.assertEqual(text, '{}\n')
        self.assertLessEqual(ticks[0], 40)

    def test_claude_hook_saves_usage_with_existing_conversation(self):
        transcript = self.test_dir / "claude-hook-usage.jsonl"
        rows = [
            {"type": "user", "message": {"content": "質問"}},
            {"type": "assistant", "message": {"id": "msg-1", "model": "claude-sonnet-5-5",
                "content": [{"type": "text", "text": "回答"}],
                "usage": {"input_tokens": 100, "cache_creation_input_tokens": 0,
                          "cache_read_input_tokens": 0, "output_tokens": 10}}},
            {"type": "assistant", "message": {"id": "msg-1", "model": "claude-sonnet-5-5",
                "content": [], "usage": {"input_tokens": 100, "cache_creation_input_tokens": 0,
                                       "cache_read_input_tokens": 0, "output_tokens": 10}}},
        ]
        transcript.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
        hook = {"session_id": "claude-usage-hook", "transcript_path": str(transcript), "cwd": str(self.test_dir)}
        claude_save.handle_stop_event(hook)
        state = claude_save.load_state(hook["session_id"])
        content = Path(state["output_path"]).read_text(encoding="utf-8")
        self.assertIn("total_tokens: 110", content)
        self.assertIn("total_cost_usd:", content)
        cost_line = next(line for line in content.splitlines() if line.startswith("  total_cost_usd:"))
        self.assertEqual(claude_save.Decimal(cost_line.split(":", 1)[1].strip()), claude_save.Decimal("0.00030"))
        self.assertEqual(content.count("\n回答\n"), 1)
        claude_save.handle_stop_event(hook)
        self.assertEqual(content, Path(state["output_path"]).read_text(encoding="utf-8"))

    def test_claude_statusline_quota_updates_note_and_recovers_first_observation(self):
        transcript = self.test_dir / "claude-quota.jsonl"
        transcript.write_text(json.dumps({"type": "user", "message": {"content": "質問"}}) + "\n", encoding="utf-8")
        session_id = "claude-statusline-quota"
        first = {"session_id": session_id, "rate_limits": {
            "five_hour": {"used_percentage": 20, "resets_at": 1800000000},
            "seven_day": {"used_percentage": 10, "resets_at": 1800100000},
        }}
        self.assertIn("W: 90.0%", claude_save.capture_statusline_quota(first))
        hook = {"session_id": session_id, "transcript_path": str(transcript), "cwd": str(self.test_dir)}
        claude_save.handle_stop_event(hook)
        path = Path(claude_save.load_state(session_id)["output_path"])
        self.assertIn("remaining_percent: 90.0", path.read_text(encoding="utf-8"))
        second = {"session_id": session_id, "rate_limits": {
            "five_hour": {"used_percentage": 30, "resets_at": 1800000000},
            "seven_day": {"used_percentage": 15, "resets_at": 1800100000},
        }}
        claude_save.capture_statusline_quota(second)
        content = path.read_text(encoding="utf-8")
        self.assertIn("first_observed:\n        remaining_percent: 90.0", content)
        self.assertIn("last_observed:\n        remaining_percent: 85.0", content)
        self.assertIn("W: 90.0% ➔ 85.0%", content)
        self.assertNotIn("quota: 5", content)
        claude_save.state_path(session_id).unlink()
        claude_save.capture_statusline_quota(second)
        claude_save.handle_stop_event(hook)
        recovered = claude_save.load_state(session_id)
        self.assertEqual(Path(recovered["output_path"]), path)
        self.assertEqual(recovered["initial_quota"]["claude"]["weekly"]["remaining"], 90.0)
        self.assertEqual(recovered["final_quota"]["claude"]["weekly"]["remaining"], 85.0)

    @mock.patch.object(codex_save, "APPLY_LONG_CONTEXT_PRICING", True)
    def test_codex_answer_usage_is_assigned_once_and_marks_long_context(self):
        transcript = self.test_dir / "answer-usage.jsonl"
        rows = [
            {"type": "turn_context", "payload": {"turn_id": "turn-1", "model": "gpt-6-luna"}},
            {"type": "event_msg", "payload": {"type": "user_message", "turn_id": "turn-1", "message": "Q1"}},
            {"type": "event_msg", "payload": {"type": "agent_message", "turn_id": "turn-1", "message": "A1"}},
            {"type": "token_usage_record", "payload": {"turn_id": "turn-1", "usage": {
                "input_tokens": 100, "cached_input_tokens": 10, "cache_write_input_tokens": 0,
                "output_tokens": 20, "reasoning_output_tokens": 4, "total_tokens": 120,
            }}},
            {"type": "turn_context", "payload": {"turn_id": "turn-2", "model": "gpt-6.1-sol"}},
            {"type": "event_msg", "payload": {"type": "user_message", "turn_id": "turn-2", "message": "Q2"}},
            {"type": "event_msg", "payload": {"type": "agent_message", "turn_id": "turn-2", "message": "A2"}},
            {"type": "token_usage_record", "payload": {"turn_id": "turn-2", "usage": {
                "input_tokens": 272001, "cached_input_tokens": 0, "cache_write_input_tokens": 0,
                "output_tokens": 10, "reasoning_output_tokens": 0, "total_tokens": 272011,
            }}},
        ]
        transcript.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

        messages, last_line = codex_save.load_jsonl_messages(transcript, 0)
        answer_messages = [message for message in messages if message["type"] == "agent_message"]
        self.assertEqual(last_line, len(rows))
        self.assertEqual(len(answer_messages), 2)
        session_usage = codex_save.retrieve_usage(transcript)
        self.assertEqual(
            sum(message["usage"]["total_tokens"] for message in answer_messages),
            session_usage["total_tokens"],
        )
        self.assertEqual(str(answer_messages[0]["usage"]["total_cost_usd"]), "0.0000191")
        self.assertTrue(answer_messages[1]["usage"]["long_context"])

        path = codex_save.create_md_file("answer-usage", "myproj", datetime.now(JST))
        codex_save.append_messages(path, messages, datetime.now(JST), "gpt-6-luna", last_line)
        content = path.read_text(encoding="utf-8")
        self.assertIn("Usage: 120 tok · $0.0000191", content)
        self.assertIn("Usage: 272,011 tok · $1.088154 ⚠︎ 272K+", content)

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

    def test_agy_replaces_legacy_quota_when_existing_markdown_is_updated(self):
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
        self.assertNotIn("quota: 5.00", updated)
        self.assertIn("📊 **Quota**: N/A", updated)

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
        self.assertEqual(state["final_quota"]["gemini"]["weekly"]["remaining"], 80.0)
        self.assertEqual(state["initial_quota"]["gemini"]["5h"]["remaining"], 60.0)
        self.assertEqual(state["final_quota"]["gemini"]["5h"]["remaining"], 50.0)
        self.assertIn("quota:\n  \"gemini\":", content)
        self.assertIn("first_observed:\n        remaining_percent: 90.0", content)
        self.assertIn("last_observed:\n        remaining_percent: 80.0", content)
        self.assertIn("W: 90.0% ➔ 80.0% / 5h: 60.0% ➔ 50.0%", content)
        self.assertNotIn("(Quota:", content)

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
        self.assertEqual(state_after.get("final_quota"), state.get("final_quota"))
        self.assertEqual(Path(state_after["output_path"]).read_text(encoding="utf-8"), content_before)

    def test_quota_frontmatter_roundtrip(self):
        first = {"gemini": {"weekly": {"remaining": 90.0, "reset": "2026-08-14 09:00",
                                      "observed_at": "2026-08-07T10:00:00+09:00"}}}
        last = {"gemini": {"weekly": {"remaining": 80.0, "reset": "2026-08-14 09:00",
                                     "observed_at": "2026-08-07T11:00:00+09:00"}}}
        content = "---\n" + "\n".join(agy_save.build_quota_frontmatter_fields(first, last)) + "\n---\n"
        recovered_first, recovered_last = agy_save.recover_quota_frontmatter(content)
        self.assertEqual(recovered_first, first)
        self.assertEqual(recovered_last, last)

    def test_quota_windows_keep_first_observation_through_reset(self):
        initial = {"codex": {"weekly": {"remaining": 10.0, "reset": "2026-08-14 09:00",
                                         "observed_at": "2026-08-13T10:00:00+09:00"}}}
        later = {"codex": {"weekly": {"remaining": 100.0, "reset": "2026-08-21 09:00",
                                       "observed_at": "2026-08-14T10:00:00+09:00"},
                           "5h": {"remaining": 65.0, "observed_at": "2026-08-14T10:00:00+09:00"}}}
        first = codex_save.merge_first_quota(initial, later)
        fields = "\n".join(codex_save.build_quota_frontmatter_fields(first, later))
        self.assertIn("remaining_percent: 10.0", fields)
        self.assertIn("remaining_percent: 100.0", fields)
        self.assertIn('reset_at_jst: "2026-08-14 09:00"', fields)
        self.assertIn('reset_at_jst: "2026-08-21 09:00"', fields)
        self.assertIn('"5h":\n      first_observed:\n        remaining_percent: 65.0', fields)
        self.assertNotIn("quota: -90", fields)

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
        self.assertIn("quota:\n  \"gemini\":", content)
        self.assertIn("first_observed:\n        remaining_percent: 90.0", content)
        self.assertIn("last_observed:\n        remaining_percent: 80.0", content)
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

    def test_agy_legacy_quota_is_removed_on_update(self):
        content = "---\nsource: antigravity-cli\nquota: not-a-number\n---\n\n📊 **Quota**:\n- **Gemini**: W: unknown\n\n本文\n"
        updated = agy_save.add_quota_metadata(
            content, None, {"gemini": {"weekly": {"remaining": 70.0, "reset": ""}}},
        )
        self.assertNotIn("not-a-number", updated)
        self.assertIn("quota:\n  \"gemini\":", updated)
        self.assertIn("last_observed:\n        remaining_percent: 70.0", updated)

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
        self.assertNotIn("preserve_quota_history", state)
        self.assertNotIn("quota: 0.00", updated)
        self.assertIn("W: 60.0%", updated)
        self.assertIn("新回答", updated)

    def test_incremental_savers_append_with_single_atomic_markdown_write(self):
        now = datetime.now(JST)
        base_message = {
            "type": "agent_message",
            "message": "Atomic response",
            "model": "test-model",
            "timestamp": "2026-08-07T10:00:05Z",
        }
        cases = [
            {
                "module": codex_save,
                "path": codex_save.create_md_file("atomic-codex", "project", now),
                "messages": [{**base_message, "line_number": 1}],
                "append": lambda path, messages: codex_save.append_messages(
                    path, messages, now, "test-model", 1
                ),
                "cursor": "<!-- last_line: 1 -->",
            },
            {
                "module": claude_save,
                "path": claude_save.create_md_file("atomic-claude", "project", now),
                "messages": [{**base_message, "line_number": 1}],
                "append": lambda path, messages: claude_save.append_messages(path, messages, now, 1),
                "cursor": "<!-- last_line: 1 -->",
            },
            {
                "module": agy_save,
                "path": agy_save.create_md_file("atomic-agy", "project", now),
                "messages": [{**base_message, "step_index": 1}],
                "append": lambda path, messages: agy_save.append_messages(path, messages, now),
                "cursor": "<!-- last_id: 1 -->",
            },
        ]
        for case in cases:
            with self.subTest(module=case["module"].__name__):
                with mock.patch.object(
                    case["module"],
                    "atomic_write_md",
                    wraps=case["module"].atomic_write_md,
                ) as atomic_write:
                    case["append"](case["path"], case["messages"])
                self.assertEqual(atomic_write.call_count, 1)
                content = case["path"].read_text(encoding="utf-8")
                self.assertIn("Atomic response", content)
                self.assertIn(case["cursor"], content)
                self.assertIn("> <small>🤖 test-model</small>\n\n\nAtomic response", content)

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
