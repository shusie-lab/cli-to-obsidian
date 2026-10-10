import copy
from decimal import Decimal
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


class PiSaveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        with patch.dict(os.environ, {'PI_OBSIDIAN_STATE_DIR': cls.temp.name}):
            spec = importlib.util.spec_from_file_location('pi_save_test', Path(__file__).resolve().parents[1] / 'pi_save.py')
            cls.pi = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cls.pi)

    @classmethod
    def tearDownClass(cls):
        for handler in cls.pi.logger.handlers:
            handler.close()
        cls.temp.cleanup()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.stack = patch.multiple(self.pi, OUTPUT_BASE=root/'vault', STATE_DIR=root/'state')
        self.stack.start()
        self.addCleanup(self.stack.stop)

    def entry(self, eid, role, text, model='gpt-6-luna', usage=None):
        message = {'role': role, 'model': model, 'content': [{'type': 'text', 'text': text}]}
        if usage is not None:
            message['usage'] = usage
        return {'type': 'message', 'id': eid, 'parentId': None,
                'timestamp': '2026-10-05T01:00:00Z', 'message': message}

    def usage(self):
        return {'input': 100, 'cacheRead': 200, 'cacheWrite': 30, 'output': 20, 'reasoning': 10, 'totalTokens': 350,
                'cost': {'total': 999}}

    def payload(self):
        u=self.entry('u', 'user', '質問')
        a=self.entry('a', 'assistant', 'Lunaの回答', usage=self.usage())
        return {'session_id': 'pi-test-id', 'cwd': '/tmp/project', 'created': u['timestamp'],
                'entries': [u,a], 'branch': [u,a]}

    def test_usage_cache_and_reasoning_are_not_double_counted(self):
        result=self.pi.normalize_pi_usage(self.usage(), 'gpt-6-luna')
        self.assertEqual(result['input_tokens'], 330)
        self.assertEqual(result['output_tokens'], 20)
        self.assertEqual(result['total_tokens'], 350)
        self.assertEqual(Decimal(result['cost_usd']), Decimal('0.00002575'))

    def test_all_four_price_tables_match(self):
        import codex_save
        import opencode_save
        import claude_save
        for module in [codex_save, opencode_save, claude_save]:
            for model, rate in module.USAGE_COST_USD_PER_1M_TOKEN_TYPE.items():
                self.assertEqual(self.pi.USAGE_COST_USD_PER_1M_TOKEN_TYPE[model], rate)

    def test_aliases_unknown_and_long_context(self):
        usage=self.pi.normalize_pi_usage(self.usage(), 'anthropic/claude-sonnet-5.5')
        self.assertIn('cost_usd',usage)
        self.assertIn('cost_usd',self.pi.normalize_pi_usage(self.usage(), 'meta/muse-spark-1.3-contributor:free'))
        self.assertIn('cost_usd',self.pi.normalize_pi_usage(self.usage(), 'nvidia/nemotron-3-ultra-550b-a55b:free'))
        self.assertNotIn('cost_usd',self.pi.normalize_pi_usage(self.usage(), 'unknown'))
        large={'input':272001,'output':10}
        self.assertEqual(Decimal(self.pi.normalize_pi_usage(large,'openai/gpt-6-luna')['cost_usd']), Decimal('0.0544077'))

    def test_invalid_usage_and_one_hour_cache(self):
        for bad in [True, -1, '4']:
            self.assertIsNone(self.pi.normalize_pi_usage({'input':bad}, 'gpt-6-luna'))
        raw=self.usage(); raw['cacheWrite1h']=30
        self.assertNotIn('cost_usd',self.pi.normalize_pi_usage(raw, 'claude-opus-5.5'))

    def test_branch_body_and_session_usage_with_hidden_tools(self):
        p=self.payload()
        other=self.entry('other', 'assistant', '別分岐', usage=self.usage())
        tool=self.entry('tool', 'assistant', '', usage=self.usage())
        tool['message']['content']=[{'type':'toolCall','name':'read'},{'type':'thinking','thinking':'非表示'}]
        p['entries'].extend([other,tool]); p['branch'].append(tool)
        path=self.pi.handle_payload(p); content=path.read_text()
        self.assertNotIn('別分岐',content)
        self.assertNotIn('非表示',content)
        self.assertIn('total_tokens: 1050',content)
        self.assertIn('Usage: 700 tok',content)
        p['branch']=[p['entries'][0],other]
        self.pi.handle_payload(p)
        content=path.read_text()
        self.assertIn('別分岐',content)
        self.assertNotIn('Lunaの回答',content)
        self.assertEqual(len(list(self.pi.OUTPUT_BASE.glob('*.md'))),1)

    def test_consecutive_answers_merge_with_tool_usage(self):
        p = self.payload()
        tool = self.entry('tool', 'assistant', '', usage=self.usage())
        final = self.entry('final', 'assistant', '最終回答', usage=self.usage())
        p['entries'].extend([tool, final])
        p['branch'] = copy.deepcopy(p['entries'])
        path = self.pi.handle_payload(p)
        content = path.read_text()
        self.assertEqual(content.count('> [!NOTE] Pi'), 1)
        self.assertIn('Lunaの回答\n\n最終回答', content)
        self.assertIn('Usage: 1,050 tok · $0.00007725', content)
        self.assertIn('total_tokens: 1050', content)
        self.assertIn('message_count: 2', content)
        # 同じノートの再構成でも回答の集約を保つ。
        self.pi.state_path(p['session_id']).unlink()
        self.assertEqual(self.pi.handle_payload(p), path)
        self.assertEqual(path.read_text().count('> [!NOTE] Pi'), 1)

    def test_answer_groups_preserve_model_and_user_boundaries(self):
        for user_text in ['次の質問', '']:
            with self.subTest(user_text=user_text):
                entries = [self.entry('u', 'user', '質問'),
                           self.entry('a', 'assistant', '途中', usage=self.usage()),
                           self.entry('b', 'assistant', '別モデル', model='unknown', usage=self.usage()),
                           self.entry('u2', 'user', user_text),
                           self.entry('c', 'assistant', '次の回答', model='unknown', usage=self.usage())]
                messages, records = self.pi.entry_records(entries)
                content = self.pi.render_pi_markdown('test', '/tmp',
                    self.pi.parse_datetime(entries[0]['timestamp']), messages, records, records)
                self.assertEqual(content.count('> [!NOTE] Pi'), 3)
                self.assertEqual(content.count('Usage: 350 tok'), 3)

    def test_tool_usage_prefers_same_model_and_nearest_answer(self):
        p = self.payload()
        entries = [p['entries'][0],
                   self.entry('before', 'assistant', '', usage={'input': 1}),
                   self.entry('a1', 'assistant', '最初の回答', usage={'input': 10}),
                   self.entry('tool_a', 'assistant', '', usage={'input': 2}),
                   self.entry('b', 'assistant', '別モデルの回答', model='gpt-6.1-sol', usage={'input': 20}),
                   self.entry('tool_b', 'assistant', '', model='gpt-6.1-sol', usage={'input': 3}),
                   self.entry('a2', 'assistant', '元モデルの回答', usage={'input': 30}),
                   self.entry('after', 'assistant', '', usage={'input': 4})]
        p['entries'] = entries
        p['branch'] = copy.deepcopy(entries)
        content = self.pi.handle_payload(p).read_text()
        blocks = content.split('> [!NOTE] Pi')[1:]
        self.assertEqual(len(blocks), 3)
        for block, text, count in zip(blocks,
                ['最初の回答', '別モデルの回答', '元モデルの回答'], [13, 23, 34]):
            self.assertIn(text, block)
            self.assertIn('Usage: {} tok'.format(count), block)
        self.assertIn('total_tokens: 70', content)

    def test_unknown_tool_usage_uses_nearest_answer_without_crossing_turns(self):
        p = self.payload()
        entries = [p['entries'][0],
                   self.entry('a', 'assistant', '回答A', usage={'input': 10}),
                   self.entry('tool', 'toolResult', '', model='unknown', usage={'input': 5}),
                   self.entry('b', 'assistant', '回答B', model='gpt-6.1-sol', usage={'input': 20}),
                   self.entry('u2', 'user', '回答なしの質問'),
                   self.entry('orphan', 'assistant', '', usage={'input': 7}),
                   self.entry('u3', 'user', '次の質問'),
                   self.entry('c', 'assistant', '回答C', usage={'input': 30})]
        p['entries'] = entries
        p['branch'] = copy.deepcopy(entries)
        content = self.pi.handle_payload(p).read_text()
        blocks = content.split('> [!NOTE] Pi')[1:]
        # 等距離なら後続の回答へ加算し、回答のないターンのusageは総計だけに含める。
        for block, count in zip(blocks, [10, 25, 30]):
            self.assertIn('Usage: {} tok'.format(count), block)
        self.assertIn('部分換算', blocks[1])
        self.assertIn('total_tokens: 72', content)

    def test_hidden_branch_usage_change_rewrites_same_note(self):
        p = self.payload()
        tool1 = self.entry('t1', 'assistant', '', usage={'input': 20})
        tool2 = self.entry('t2', 'assistant', '', usage={'input': 40})
        p['entries'].extend([tool1, tool2])
        p['branch'].append(tool1)
        path = self.pi.handle_payload(p)
        self.assertIn('Usage: 370 tok', path.read_text())
        p['branch'] = p['entries'][:2] + [tool2]
        self.assertEqual(self.pi.handle_payload(p), path)
        self.assertIn('Usage: 390 tok', path.read_text())
        self.assertIn('total_tokens: 410', path.read_text())
        self.assertEqual(len(list(self.pi.OUTPUT_BASE.glob('*.md'))), 1)

    def test_empty_branch_clears_old_body_and_retains_session_usage(self):
        for recover_state in [False, True]:
            with self.subTest(recover_state=recover_state):
                p = self.payload()
                p['session_id'] += '-empty-' + str(recover_state)
                path = self.pi.handle_payload(p)
                if recover_state:
                    self.pi.state_path(p['session_id']).unlink()
                p['branch'] = []
                self.assertEqual(self.pi.handle_payload(p), path)
                content = path.read_text()
                self.assertNotIn('Lunaの回答', content)
                self.assertNotIn('title:', content)
                self.assertNotIn('[!QUESTION]', content)
                self.assertNotIn('[!NOTE]', content)
                self.assertIn('message_count: 0', content)
                self.assertIn('total_tokens: 350', content)
                self.assertIn('created: "2026-10-05T10:00:00+09:00"', content)
                original = path.read_bytes()
                modified = path.stat().st_mtime_ns
                self.assertEqual(self.pi.handle_payload(p), path)
                self.assertEqual(path.stat().st_mtime_ns, modified)
                self.assertEqual(path.read_bytes(), original)
                p['branch'] = p['entries']
                self.assertEqual(self.pi.handle_payload(p), path)
                self.assertIn('Lunaの回答', path.read_text())

    def test_branch_with_only_hidden_usage_clears_old_body(self):
        p = self.payload()
        tool = self.entry('t', 'assistant', '', usage=self.usage())
        p['entries'].append(tool)
        path = self.pi.handle_payload(p)
        p['branch'] = [tool]
        self.assertEqual(self.pi.handle_payload(p), path)
        self.assertNotIn('Lunaの回答', path.read_text())
        self.assertIn('message_count: 0', path.read_text())
        self.assertIn('total_tokens: 700', path.read_text())

    def test_new_empty_session_creates_no_note_or_state(self):
        p = self.payload()
        p['branch'] = []
        self.assertIsNone(self.pi.handle_payload(p))
        self.assertFalse(self.pi.state_path(p['session_id']).exists())
        self.assertEqual(list(self.pi.OUTPUT_BASE.glob('*.md')), [])

    def test_projectless_filename_matches_common_format(self):
        p = self.payload()
        p['cwd'] = ''
        path = self.pi.handle_payload(p)
        self.assertEqual(path.name, '20261005_100000_pi-test-id.md')

    def test_repeated_payload_and_state_recovery(self):
        p=self.payload(); path=self.pi.handle_payload(p)
        original=path.read_bytes(); modified=path.stat().st_mtime_ns
        self.assertEqual(self.pi.handle_payload(p),path)
        self.assertEqual(path.stat().st_mtime_ns,modified)
        self.assertEqual(path.read_bytes(),original)
        self.pi.state_path(p['session_id']).unlink()
        self.assertEqual(self.pi.handle_payload(p),path)
        self.assertIn('created: "2026-10-05T10:00:00+09:00"',path.read_text())

    def test_dedup_and_auxiliary_usage(self):
        p=self.payload(); p['entries'].append(copy.deepcopy(p['entries'][1]))
        p['entries'].extend([{'type':'model_change','id':'m','modelId':'gpt-6-luna'},
                             {'type':'compaction','id':'c','usage':self.usage()},
                             {'type':'usage','id':'w','model':'gpt-6-luna','usage':self.usage()}])
        path=self.pi.handle_payload(p)
        self.assertIn('total_tokens: 1050',path.read_text())

    def test_quota_entries_are_ignored(self):
        p=self.payload()
        p['entries'].append({'type':'custom','customType':'obsidian-quota','id':'quota',
                             'data':{'observed_at':'2026-10-05T01:00:00Z',
                             'headers':{'x-codex-primary-used-percent':'20',
                                        'x-codex-primary-window-minutes':'300'}}})
        path=self.pi.handle_payload(p)
        self.assertNotIn('quota:',path.read_text())
        self.assertNotIn('📊 **Quota**',path.read_text())

    def test_unknown_model_partial_cost_and_safe_filename(self):
        p=self.payload(); p['session_id']='../../pi-test'
        p['entries'].append(self.entry('unknown','assistant','別モデル',model='unknown',usage=self.usage()))
        path=self.pi.handle_payload(p)
        self.assertEqual(path.parent,self.pi.OUTPUT_BASE)
        self.assertIn('cost_incomplete: true',path.read_text())


if __name__ == '__main__':
    unittest.main()
