import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest import mock

import rebuild_session as rebuild


class TestRebuildSession(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.session = 'session-example'

    def tearDown(self):
        self.temporary.cleanup()

    def module(self, provider):
        module = rebuild.load_saver(provider)
        module.OUTPUT_BASE = self.root / provider / 'notes'
        module.STATE_DIR = self.root / provider / 'state'
        return module

    def transcript(self, provider, empty=False):
        stamp = '2026-09-01T01:02:03Z'
        if provider == 'codex':
            rows = [{'type': 'session_meta', 'payload': {
                'id': self.session, 'timestamp': stamp, 'cwd': '/projects/example'}},
                {'type': 'turn_context', 'payload': {'model': 'gpt-6.1-sol'}}]
            if not empty:
                rows += [
                    {'type': 'event_msg', 'timestamp': stamp, 'payload': {'type': 'user_message', 'message': '質問本文'}},
                    {'type': 'event_msg', 'timestamp': stamp, 'payload': {'type': 'agent_message', 'message': '回答本文'}},
                    {'type': 'token_usage_record', 'payload': {'usage': {
                        'input_tokens': 100, 'output_tokens': 10, 'total_tokens': 110}}}]
        elif provider == 'claude':
            rows = [
                {'type': 'user', 'sessionId': self.session, 'cwd': '/projects/example', 'timestamp': stamp,
                 'message': {'content': '質問本文'}},
                {'type': 'assistant', 'sessionId': self.session, 'timestamp': stamp,
                 'message': {'id': 'answer', 'model': 'claude-sonnet-4-6', 'content': [{'type': 'text', 'text': '回答本文'}],
                             'usage': {'input_tokens': 100, 'output_tokens': 10}}}]
        else:
            rows = [{'step_index': 1, 'type': 'USER_INPUT', 'content': '<USER_REQUEST>質問本文</USER_REQUEST>'},
                    {'step_index': 2, 'type': 'PLANNER_RESPONSE', 'content': '回答本文', 'tool_calls': []}]
        path = self.root / (provider + '.jsonl')
        path.write_text(''.join(json.dumps(row) + '\n' for row in rows), encoding='utf-8')
        return path

    def database(self):
        path = self.root / 'opencode.db'
        if path.exists():
            return path
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE session_v2 (id TEXT, directory TEXT, time_created INTEGER)')
            db.execute('CREATE TABLE session_message (id TEXT, session_id TEXT, type TEXT, seq INTEGER, data TEXT)')
            db.execute('INSERT INTO session_v2 VALUES (?, ?, ?)', (self.session, '/projects/example', 1788224523000))
            messages = [
                {'text': '質問本文'},
                {'content': [{'type': 'text', 'text': '回答本文'}], 'model': {'modelID': 'gpt-6.1-sol'},
                 'tokens': {'input': 100, 'output': 10}, 'cost': 0}]
            for seq, message in enumerate(messages):
                db.execute('INSERT INTO session_message VALUES (?, ?, ?, ?, ?)',
                           ('msg' + str(seq), self.session, 'user' if seq == 0 else 'assistant', seq, json.dumps(message)))
        return path

    def run_rebuild(self, provider, module, output=None, empty=False):
        kwargs = {'database': self.database()} if provider == 'opencode' else {'transcript': self.transcript(provider, empty)}
        return rebuild.rebuild(provider, self.session, output_dir=output, module=module, **kwargs)

    def test_all_providers_export_without_normal_state_or_output(self):
        for provider in rebuild.PROVIDERS:
            with self.subTest(provider=provider):
                module = self.module(provider)
                output = self.root / ('export-' + provider)
                normal_state = module.STATE_DIR
                with mock.patch.object(module, 'retrieve_quota', side_effect=AssertionError('quota API called')) if provider == 'agy' else mock.patch.object(module, 'logger'):
                    target, backup = self.run_rebuild(provider, module, output)
                content = target.read_text(encoding='utf-8')
                self.assertEqual(target.parent, output)
                self.assertIn('質問本文', content)
                self.assertIn('回答本文', content)
                self.assertIsNone(backup)
                self.assertFalse(normal_state.exists())
                self.assertFalse(module.OUTPUT_BASE.exists())

    def test_normal_replaces_same_file_backs_up_and_resume_does_not_duplicate(self):
        for provider in rebuild.PROVIDERS:
            with self.subTest(provider=provider):
                module = self.module(provider)
                target, _ = self.run_rebuild(provider, module)
                content = target.read_text(encoding='utf-8')
                target.write_text(content + '\n古い追記\n', encoding='utf-8')
                # 古いcursor/hashがあっても全件を再生成する。
                rebuilt, backup = self.run_rebuild(provider, module)
                self.assertEqual(target, rebuilt)
                self.assertIn('古い追記', backup.read_text(encoding='utf-8'))
                self.assertNotIn('古い追記', target.read_text(encoding='utf-8'))
                self.assertEqual(rebuild.frontmatter_value(content, 'created'),
                                 rebuild.frontmatter_value(target.read_text(encoding='utf-8'), 'created'))
                self.assertEqual(module.load_state(self.session)['output_path'], str(target))
                if provider == 'opencode':
                    module.handle_payload(rebuild.read_opencode(self.session, self.root / 'opencode.db'))
                elif provider == 'agy':
                    module.handle_stop_event({'conversationId': self.session, 'transcriptPath': str(self.root / 'agy.jsonl')})
                else:
                    module.handle_stop_event({'session_id': self.session, 'transcript_path': str(self.root / (provider + '.jsonl'))})
                self.assertEqual(target.read_text(encoding='utf-8').count('回答本文'), 1)

    def test_changed_price_recomputes_frontmatter_and_answer(self):
        module = self.module('codex')
        target, _ = self.run_rebuild('codex', module)
        old = target.read_text(encoding='utf-8')
        module.USAGE_COST_USD_PER_1M_TOKEN_TYPE['gpt-6.1-sol']['input'] *= 2
        self.run_rebuild('codex', module)
        new = target.read_text(encoding='utf-8')
        self.assertNotEqual(old, new)
        self.assertIn('total_cost_usd: 0.0005', new)
        self.assertIn('$0.0005', new)
        self.assertEqual(new.count('回答本文'), 1)

    def test_empty_transcript_keeps_old_note_and_state(self):
        module = self.module('codex')
        target, _ = self.run_rebuild('codex', module)
        content = target.read_bytes()
        state = module.state_path(self.session).read_bytes()
        with self.assertRaises(ValueError):
            self.run_rebuild('codex', module, empty=True)
        self.assertEqual(target.read_bytes(), content)
        self.assertEqual(module.state_path(self.session).read_bytes(), state)

    def test_atomic_write_failure_keeps_old_note_and_state(self):
        module = self.module('codex')
        target, _ = self.run_rebuild('codex', module)
        content = target.read_bytes()
        state = module.state_path(self.session).read_bytes()
        original = module.atomic_write_md

        def fail_destination(path, text):
            if path == target:
                raise OSError('destination failure')
            return original(path, text)

        with mock.patch.object(module, 'atomic_write_md', side_effect=fail_destination):
            with self.assertRaises(OSError):
                self.run_rebuild('codex', module)
        self.assertEqual(target.read_bytes(), content)
        self.assertEqual(module.state_path(self.session).read_bytes(), state)

    def test_claude_quota_and_custom_source_survive_rebuild(self):
        module = self.module('claude')
        module.SOURCE_NAME = 'claude-personal'
        module.capture_statusline_quota({'session_id': self.session,
                                        'rate_limits': {'five_hour': {'used_percentage': 20}}})
        target, _ = self.run_rebuild('claude', module)
        old = target.read_text(encoding='utf-8')
        quota = module.recover_quota_frontmatter(old)
        module.SOURCE_NAME = module.DEFAULT_SOURCE
        self.run_rebuild('claude', module)
        new = target.read_text(encoding='utf-8')
        self.assertEqual(quota, module.recover_quota_frontmatter(new))
        self.assertEqual(rebuild.frontmatter_value(new, 'source'), 'claude-personal')

    def test_missing_database_does_not_create_file(self):
        missing = self.root / 'missing.db'
        with self.assertRaises(FileNotFoundError):
            rebuild.read_opencode(self.session, missing)
        self.assertFalse(missing.exists())

    def test_wrong_session_is_rejected(self):
        path = self.transcript('codex')
        with self.assertRaises(ValueError):
            rebuild.find_transcript('codex', 'wrong', path)

    def test_archive_discovery_and_duplicate_detection(self):
        path = self.transcript('codex')
        home = self.root / 'home'
        archive = home / 'archived_sessions'
        archive.mkdir(parents=True)
        archived = archive / ('rollout-' + self.session + '.jsonl')
        archived.write_bytes(path.read_bytes())
        with mock.patch.dict('os.environ', {'CODEX_HOME': str(home)}):
            self.assertEqual(rebuild.find_transcript('codex', self.session)[0], archived)
            (archive / ('duplicate-' + self.session + '.jsonl')).write_bytes(path.read_bytes())
            with self.assertRaises(ValueError):
                rebuild.find_transcript('codex', self.session)

    def test_state_loss_uses_existing_filename(self):
        module = self.module('codex')
        target, _ = self.run_rebuild('codex', module)
        module.state_path(self.session).unlink()
        replacement, backup = self.run_rebuild('codex', module)
        self.assertEqual(target, replacement)
        self.assertIsNotNone(backup)

    def test_normal_directory_requires_obsidian_mode(self):
        module = self.module('codex')
        with self.assertRaisesRegex(ValueError, '--obsidian'):
            self.run_rebuild('codex', module, module.OUTPUT_BASE)

    def test_projectless_codex_uses_existing_metadata_rule(self):
        module = self.module('codex')
        with mock.patch.object(module, 'project_value', return_value='projectless'):
            target, _ = self.run_rebuild('codex', module)
        self.assertEqual(rebuild.frontmatter_value(target.read_text(encoding='utf-8'), 'project'), 'projectless')

    def test_cli_exports_and_reports_failure_with_exit_status(self):
        path = self.transcript('codex')
        script = str(Path(rebuild.__file__).resolve())
        output = self.root / 'cli-output'
        command = ['/usr/bin/python3', script, 'codex', self.session, '--transcript', str(path),
                   '--output-dir', str(output)]
        completed = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn('再構成完了:', completed.stdout)
        self.assertEqual(len(list(output.glob('*.md'))), 1)
        command[3] = 'wrong-session'
        failed = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(failed.returncode, 1)
        self.assertIn('再構成失敗:', failed.stderr)

    def test_explicit_destination_required(self):
        with self.assertRaises(SystemExit) as error:
            rebuild.main(['codex', self.session])
        self.assertEqual(error.exception.code, 2)


if __name__ == '__main__':
    unittest.main()
