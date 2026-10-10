"""全saverの出力・再実行・復旧契約を、provider固有のfixtureで検証する。"""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
PROVIDERS = ("codex", "agy", "claude", "opencode", "pi")


class SaverContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module_temp = tempfile.TemporaryDirectory()
        cls.modules = {}
        with patch.dict(os.environ, {"HOME": cls.module_temp.name,
                                    "PI_OBSIDIAN_STATE_DIR": cls.module_temp.name + "/pi"}):
            for provider in PROVIDERS:
                spec = importlib.util.spec_from_file_location("contract_" + provider, ROOT / (provider + "_save.py"))
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                cls.modules[provider] = module

    @classmethod
    def tearDownClass(cls):
        for module in cls.modules.values():
            for handler in module.logger.handlers:
                handler.close()
        cls.module_temp.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.stamp = "2026-01-01T01:00:00Z"
        self.created = datetime(2026, 1, 1, 10, tzinfo=timezone(timedelta(hours=9)))
        for name, module in self.modules.items():
            context = patch.multiple(module, OUTPUT_BASE=self.root/name/"notes", STATE_DIR=self.root/name/"state", logger=Mock())
            context.start()
            self.addCleanup(context.stop)

    def rows(self, provider, turn=1, user="質問", answer="回答", usage=True):
        if provider == "codex":
            rows = [
                {"type": "turn_context", "payload": {"turn_id": "turn"+str(turn), "model": "gpt-6-luna"}},
                {"type": "event_msg", "timestamp": self.stamp, "payload": {"type": "user_message", "message": user}},
                {"type": "event_msg", "timestamp": self.stamp, "payload": {"type": "agent_message", "message": answer}},
            ]
            if usage:
                rows.append({"type": "token_usage_record", "payload": {"turn_id": "turn"+str(turn),
                    "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}}})
            return rows
        if provider == "agy":
            return [
                {"step_index": turn*2-1, "type": "USER_INPUT", "created_at": self.stamp, "content": "<USER_REQUEST>"+user+"</USER_REQUEST>"},
                {"step_index": turn*2, "type": "PLANNER_RESPONSE", "created_at": self.stamp, "content": answer, "tool_calls": []},
            ]
        if provider == "claude":
            message = {"id": "a"+str(turn), "model": "claude-sonnet-5.5", "content": [{"type": "text", "text": answer}]}
            if usage:
                message["usage"] = {"input_tokens": 10, "output_tokens": 5}
            return [{"type": "user", "timestamp": self.stamp, "message": {"content": user}},
                    {"type": "assistant", "timestamp": self.stamp, "message": message}]
        if provider == "opencode":
            info = {"id": "a"+str(turn), "role": "assistant", "modelID": "muse-spark-1.3-contributor-free", "time": {"created": self.stamp}}
            if usage:
                info["tokens"] = {"input": 10, "output": 5}
            return [{"info": {"id": "u"+str(turn), "role": "user", "time": {"created": self.stamp}}, "parts": [{"type": "text", "text": user}]},
                    {"info": info, "parts": [{"type": "text", "text": answer}]}]
        message = {"role": "assistant", "model": "gpt-6-luna", "content": [{"type": "text", "text": answer}]}
        if usage:
            message["usage"] = {"input": 10, "output": 5}
        return [{"type": "message", "id": "u"+str(turn), "timestamp": self.stamp, "message": {"role": "user", "content": user}},
                {"type": "message", "id": "a"+str(turn), "timestamp": self.stamp, "message": message}]

    def save(self, provider, rows, session="contract"):
        module = self.modules[provider]
        if provider == "opencode":
            return module.handle_payload({"session_id": session, "cwd": "/tmp/example", "created": self.stamp,
                                          "full_sync": True, "messages": rows})
        if provider == "pi":
            return module.handle_payload({"session_id": session, "cwd": "/tmp/example", "created": self.stamp,
                                          "entries": rows, "branch": rows})
        transcript = self.root / (provider + ".jsonl")
        transcript.write_text("".join(json.dumps(row, ensure_ascii=False)+"\n" for row in rows), encoding="utf-8")
        if provider == "agy":
            with patch.object(module, "retrieve_quota", return_value=None):
                module.handle_stop_event({"conversationId": session, "transcriptPath": str(transcript), "workspacePaths": ["/tmp/example"]})
        else:
            with patch.object(module, "read_stop_transcript", return_value=transcript.read_text()) if provider == "claude" else patch.object(module, "logger"):
                module.handle_stop_event({"session_id": session, "transcript_path": str(transcript), "cwd": "/tmp/example"})
        state = module.load_state(session)
        return Path(state["output_path"]) if state.get("output_path") else None

    def test_all_five_skip_new_empty_session(self):
        for provider in PROVIDERS:
            with self.subTest(provider=provider):
                self.assertIsNone(self.save(provider, []))
                module = self.modules[provider]
                self.assertFalse(list(module.OUTPUT_BASE.glob("*.md")))
                self.assertFalse(module.state_path("contract").exists())

    def test_identical_save_preserves_note_but_refreshes_state_last_use(self):
        for provider, module in self.modules.items():
            with self.subTest(provider=provider):
                rows = self.rows(provider)
                with patch.object(module, "now_jst", return_value=self.created):
                    path = self.save(provider, rows)
                before = path.read_bytes()
                late = self.created + timedelta(hours=2)
                with patch.object(module, "now_jst", return_value=late):
                    self.assertEqual(self.save(provider, rows), path)
                self.assertEqual(path.read_bytes(), before)
                self.assertEqual(module.load_state("contract")["last_used_at"], module.format_iso(late))

    def test_existing_source_and_custom_tags_survive_settings_change(self):
        for provider, module in self.modules.items():
            with self.subTest(provider=provider):
                rows = self.rows(provider)
                path = self.save(provider, rows)
                before_source = module.read_markdown_metadata(path)["source"]
                path.write_text(path.read_text().replace("  - ai-conversation", "  - ai-conversation\n  - custom-tag"))
                rows += self.rows(provider, 2, user="次の質問", answer="次の回答")
                with patch.object(module, "SOURCE_NAME", "changed-source"), patch.object(module, "OUTPUT_BASE", self.root/provider/"changed-notes"):
                    self.assertEqual(self.save(provider, rows), path)
                metadata = module.read_markdown_metadata(path)
                self.assertEqual(metadata["source"], before_source)
                self.assertIn("custom-tag", metadata["tags"])
                self.assertIn("次の回答", path.read_text())

    def test_state_loss_preserves_original_created_and_prevents_duplicate_body(self):
        for provider, module in self.modules.items():
            with self.subTest(provider=provider):
                rows = self.rows(provider)
                path = self.save(provider, rows)
                created = module.read_markdown_metadata(path)["created"]
                module.state_path("contract").unlink()
                stamp = (self.created + timedelta(days=200)).timestamp()
                os.utime(path, (stamp, stamp))
                self.assertEqual(self.save(provider, rows), path)
                self.assertEqual(module.read_markdown_metadata(path)["created"], created)
                self.assertEqual(path.read_text().count("回答"), 1)

    def test_hidden_user_boundary_separates_answers_in_batch_and_across_hooks(self):
        for provider in PROVIDERS:
            for split in [False, True]:
                with self.subTest(provider=provider, split=split):
                    rows = self.rows(provider, answer="最初の回答")
                    session = "boundary-" + str(split)
                    if split:
                        self.save(provider, rows, session)
                    rows += self.rows(provider, 2, user="", answer="次の回答")
                    path = self.save(provider, rows, session)
                    content = path.read_text()
                    self.assertEqual(content.count("> [!NOTE]"), 2)
                    self.assertIn("message_count: 3", content)
                    if provider != "agy":
                        self.assertEqual(content.count("Usage: 15 tok"), 2)

    def test_common_money_format_aliases_and_partial_accounting(self):
        for provider in ["codex", "claude", "opencode", "pi"]:
            module = self.modules[provider]
            with self.subTest(provider=provider):
                usage = {"input_tokens": 1, "total_tokens": 1}
                records = [{"type": "assistant", "model": "custom", "usage": dict(usage, cost_usd="0.000000002")},
                           {"type": "assistant", "model": "custom", "usage": usage}]
                summary = module.summarize_usage(records, estimate_missing=False)
                self.assertEqual(summary["total_tokens"], 2)
                self.assertEqual(summary["total_cost_usd"], Decimal("0.000000002"))
                self.assertTrue(summary["cost_incomplete"])
                self.assertTrue(summary["by_model"]["custom"]["cost_incomplete"])
                fields = "\n".join(module.build_usage_frontmatter_fields(summary))
                self.assertIn("total_cost_usd: 0.000000002", fields)
                self.assertNotIn("E-", fields)
                self.assertIn("$0.000000002（部分換算）", module.format_message_usage(summary))
                self.assertEqual(module._normalize_usage_model("anthropic/claude-opus-5.5"), "claude-opus-5-5")

    def test_haiku_prompt_price_boundary_and_cache_ttls(self):
        for provider in ("codex", "claude", "opencode", "pi"):
            module = self.modules[provider]
            for model in ("claude-haiku-5-5", "anthropic/claude-haiku-5.5"):
                for tokens, expected in ((100_000, "0.01545"), (100_001, "0.0772505")):
                    with self.subTest(provider=provider, model=model, tokens=tokens):
                        usage = {"input_tokens": tokens, "cached_input_tokens": 20_000,
                                 "cache_write_input_tokens": 10_000, "output_tokens": 14_000}
                        self.assertEqual(module._usage_cost(model, usage), Decimal(expected))
        claude = self.modules["claude"]
        for fresh, expected in ((70_000, "0.015825"), (70_001, "0.0791255")):
            for model in ("claude-haiku-5-5", "anthropic/claude-haiku-5.5", "claude-haiku-5-5-20261007"):
                raw = {"input_tokens": fresh, "cache_read_input_tokens": 20_000,
                       "cache_creation_input_tokens": 10_000, "output_tokens": 14_000,
                       "cache_creation": {"ephemeral_5m_input_tokens": 5_000,
                                          "ephemeral_1h_input_tokens": 5_000}}
                with self.subTest(model=model, fresh=fresh):
                    self.assertEqual(claude._claude_usage_record({"model": model, "usage": raw})["cost_usd"], Decimal(expected))
        raw.pop("cache_creation")
        self.assertNotIn("cost_usd", claude._claude_usage_record({"model": "claude-haiku-5-5", "usage": raw}))

    def test_opus_cache_read_reference_price_matches_across_providers(self):
        # cached入力だけ100万token: 通常入力や書込の金額は含めない。
        expected = Decimal("0.20")
        claude = self.modules["claude"]
        record = claude._claude_usage_record({"model": "anthropic/claude-opus-5.5",
            "usage": {"input_tokens": 0, "cache_read_input_tokens": 1_000_000, "output_tokens": 0}})
        self.assertEqual(record["cost_usd"], expected)
        for provider in ("codex", "claude", "opencode", "pi"):
            module = self.modules[provider]
            with self.subTest(provider=provider):
                self.assertEqual(module._usage_cost("anthropic/claude-opus-5.5", record), expected)
                self.assertFalse(module.is_long_context_usage("claude-opus-5.5", record))

    def test_gpt_long_context_threshold_and_cache_multipliers_match(self):
        for provider in ("codex", "claude", "opencode", "pi"):
            module = self.modules[provider]
            for tokens, expected in ((272_000, "0.3791"), (272_001, "0.758154")):
                with self.subTest(provider=provider, tokens=tokens):
                    # 総入力にはread10万/write5万を含む。倍率は通常入力とcacheの双方へ。
                    usage = {"input_tokens": tokens, "cached_input_tokens": 100_000,
                             "cache_write_input_tokens": 50_000, "output_tokens": 10}
                    self.assertEqual(module._usage_cost("openai/gpt-6.1-sol", usage), Decimal(expected))
                    self.assertEqual(module.is_long_context_usage("openai/gpt-6.1-sol", usage), tokens > 272_000)
            self.assertIsNone(module._usage_cost("unregistered-model", {"input_tokens": 300_000}))
            for invalid in (None, True, -1, "300000"):
                self.assertIsNone(module._usage_cost("gpt-6.1-sol", {"input_tokens": invalid}))

    def test_reference_pricing_is_per_record_and_native_cost_is_preserved(self):
        codex, opencode, pi = (self.modules[name] for name in ("codex", "opencode", "pi"))
        def normalized(provider, fresh, cost=0):
            if provider == "codex":
                return {"input_tokens": fresh, "output_tokens": 10_000, "total_tokens": fresh + 10_000}
            if provider == "opencode":
                return opencode._message_usage({"modelID": "gpt-6.1-sol", "cost": cost,
                    "tokens": {"input": fresh, "output": 10_000}})
            return pi.normalize_pi_usage({"input": fresh, "output": 10_000}, "gpt-6.1-sol")
        for provider in ("codex", "opencode", "pi"):
            module = self.modules[provider]
            with self.subTest(provider=provider):
                records = [{"type": "assistant", "model": "gpt-6.1-sol", "usage": normalized(provider, 150_000)}
                           for _ in range(2)]
                summary = module.summarize_usage(records)
                self.assertEqual(summary["total_cost_usd"], Decimal("0.8"))
                self.assertNotIn("long_context", summary)
                large = normalized(provider, 300_000)
                self.assertEqual(module._usage_cost("gpt-6.1-sol", large), Decimal("1.35"))
                if provider != "codex":
                    self.assertTrue(large["long_context"])
        native = normalized("opencode", 300_000, cost=7.5)
        self.assertEqual(Decimal(native["cost_usd"]), Decimal("7.5"))
        self.assertNotIn("long_context", native)
        self.assertEqual(opencode.summarize_usage([
            {"type": "assistant", "model": "gpt-6.1-sol", "usage": native}])["total_cost_usd"], Decimal("7.5"))
        for provider in ("opencode", "pi"):
            with self.subTest(saved_provider=provider):
                rows = self.rows(provider)
                if provider == "opencode":
                    rows[1]["info"].update(modelID="gpt-6.1-sol", cost=0,
                        tokens={"input": 300_000, "output": 10_000})
                else:
                    rows[1]["message"].update(model="gpt-6.1-sol", usage={"input": 300_000, "output": 10_000})
                path = self.save(provider, rows)
                self.assertIn("total_cost_usd: 1.35", path.read_text())
                self.assertIn("Usage: 310,000 tok · $1.35 ⚠︎ 272K+", path.read_text())
                if provider == "opencode":
                    rows[1]["info"]["cost"] = 7.5
                    self.save(provider, rows)
                    self.assertIn("total_cost_usd: 7.5", path.read_text())
                    self.assertIn("Usage: 310,000 tok · $7.5</small>", path.read_text())

    def test_claude_long_context_keeps_ttl_and_pricing_condition_guards(self):
        module = self.modules["claude"]
        for model, expected in (("claude-opus-5.5", "3.6002"), ("claude-sonnet-5.5", "1.8001")):
            with self.subTest(model=model):
                raw = {"input_tokens": 900_000, "output_tokens": 10}
                self.assertEqual(module._claude_usage_record({"model": model, "usage": raw})["cost_usd"], Decimal(expected))
        raw = {"input_tokens": 100, "cache_read_input_tokens": 50, "cache_creation_input_tokens": 30,
               "output_tokens": 10, "cache_creation": {"ephemeral_5m_input_tokens": 20,
                                                        "ephemeral_1h_input_tokens": 10}}
        self.assertEqual(module._claude_usage_record({"model": "claude-opus-5.5", "usage": raw})["cost_usd"], Decimal("0.00079"))
        for override in ({"cache_creation": {}}, {"speed": "fast"}, {"service_tier": "fast"},
                         {"inference_geo": "us"}):
            with self.subTest(condition=override):
                usage = module._claude_usage_record({"model": "claude-opus-5.5", "usage": dict(raw, **override)})
                self.assertNotIn("cost_usd", usage)
                self.assertEqual(usage["total_tokens"], 190)
                summary = module.summarize_usage([
                    {"type": "assistant", "model": "claude-opus-5.5", "usage": usage}])
                self.assertNotIn("total_cost_usd", summary)
                self.assertTrue(summary["by_model"]["claude-opus-5.5"]["cost_unavailable"])

    def price_rows(self, provider, model):
        rows = self.rows(provider)
        if provider == "codex":
            rows[0]["payload"]["model"] = model
        elif provider == "opencode":
            rows[1]["info"]["modelID"] = model
        else:
            rows[1]["message"]["model"] = model
        return rows

    def test_all_registered_prices_are_used_by_each_usage_saver(self):
        # 入力10/出力5の期待額を別途計算し、raw入力から保存まで検証する。
        prices = {"gpt-6-astra": "0.00035", "gpt-6.1-sol": "0.00007",
                  "gpt-6-luna": "0.0000035", "muse-spark-1.3-contributor-free": "0.000002",
                  "nemotron-3-ultra-free": "0.000016", "claude-opus-5-5": "0.00014",
                  "claude-sonnet-5-5": "0.00007", "claude-haiku-5-5": "0.0000035", "gemini-3.8-flash": "0.00002625"}
        for provider in ("codex", "claude", "opencode", "pi"):
            self.assertEqual(set(self.modules[provider].STANDARD_USAGE_RATES), set(prices))
            for model, price in prices.items():
                with self.subTest(provider=provider, model=model):
                    path = self.save(provider, self.price_rows(provider, model), model)
                    content = path.read_text()
                    self.assertIn("total_cost_usd: " + price, content)
                    self.assertIn("Usage: 15 tok · $" + price + "</small>", content)

        claude = self.modules["claude"]
        usage = claude._claude_usage_record({"model": "gpt-6.1-sol", "usage": {
            "input_tokens": 150_001, "cache_read_input_tokens": 100_000,
            "cache_creation_input_tokens": 50_000, "output_tokens": 10}})
        self.assertTrue(usage["long_context"])
        self.assertEqual(usage["cost_usd"], Decimal("0.870154"))

    def test_gemini_openrouter_alias_cache_reasoning_and_large_input(self):
        aliases = ("google/gemini-3.8-flash", "gemini-3.8-flash", "gemini3.8-flash")
        for provider in ("codex", "claude", "opencode", "pi"):
            module = self.modules[provider]
            for model in aliases:
                with self.subTest(provider=provider, model=model):
                    rows = self.price_rows(provider, model)
                    if provider == "codex":
                        rows[-1]["payload"]["usage"] = {
                            "input_tokens": 180, "cached_input_tokens": 50,
                            "cache_write_input_tokens": 30, "output_tokens": 12,
                            "reasoning_output_tokens": 2, "total_tokens": 192}
                    elif provider == "claude":
                        rows[1]["message"]["usage"] = {
                            "input_tokens": 100, "cache_read_input_tokens": 50,
                            "cache_creation_input_tokens": 30, "output_tokens": 12}
                    elif provider == "opencode":
                        rows[1]["info"].update(providerID="openrouter", cost=0,
                            tokens={"input": 100, "output": 10, "reasoning": 2,
                                    "cache": {"read": 50, "write": 30}})
                    else:
                        rows[1]["message"]["usage"] = {
                            "input": 100, "cacheRead": 50, "cacheWrite": 30,
                            "output": 12, "reasoning": 2}
                    content = self.save(provider, rows, model).read_text()
                    # fresh100/read50/write30/output12（reasoningは出力に含める）。
                    self.assertIn("total_tokens: 192", content)
                    self.assertIn("total_cost_usd: 0.000125000000000000001", content)
                    self.assertIn("Usage: 192 tok · $0.000125000000000000001", content)
                    self.assertEqual(module.reference_price_model(model), "gemini-3.8-flash")
                    large = {"input_tokens": 300_000, "output_tokens": 120}
                    self.assertEqual(module._usage_cost(model, large), Decimal("0.22545"))
                    self.assertFalse(module.is_long_context_usage(model, large))

    def test_removed_price_models_keep_tokens_without_reference_estimate(self):
        for provider in ("codex", "claude", "opencode", "pi"):
            for model in ("gpt-6-sol", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"):
                with self.subTest(provider=provider, model=model):
                    rows = self.price_rows(provider, model)
                    path = self.save(provider, rows, model)
                    content = path.read_text()
                    self.assertIn("total_tokens: 15", content)
                    self.assertIn("cost_unavailable: true", content)
                    self.assertNotIn("total_cost_usd:", content)
                    self.assertIn("Usage: 15 tok · 換算不可", content)
                    self.assertIn("回答", content)
                    if provider == "opencode":
                        rows[1]["info"]["cost"] = 7.5
                        self.save(provider, rows, model)
                        self.assertIn("total_cost_usd: 7.5", path.read_text())

    def test_claude_late_usage_updates_existing_answer_once(self):
        module = self.modules["claude"]
        rows = self.rows("claude")
        path = self.save("claude", rows)
        late = {"type": "assistant", "message": {"id": "a1", "model": "claude-sonnet-5.5", "content": [],
                "usage": {"input_tokens": 30, "output_tokens": 7}}}
        rows.append(late)
        self.save("claude", rows)
        content = path.read_text()
        self.assertEqual(content.count("> [!NOTE] Claude"), 1)
        self.assertEqual(content.count("Usage: 37 tok"), 1)
        self.assertNotIn("Usage: 15 tok", content)
        self.assertIn("total_tokens: 37", content)
        self.save("claude", rows)
        self.assertEqual(path.read_text(), content)

    def test_initial_hidden_user_keeps_late_usage_aligned(self):
        for provider in ("codex", "claude"):
            with self.subTest(provider=provider):
                rows = self.rows(provider, user="")
                path = self.save(provider, rows)
                if provider == "codex":
                    rows.append({"type": "token_usage_record", "payload": {"turn_id": "turn1",
                        "usage": {"input_tokens": 20, "output_tokens": 5, "total_tokens": 25}}})
                    expected = 40
                else:
                    rows.append({"type": "assistant", "message": {"id": "a1", "model": "claude-sonnet-5.5",
                        "content": [], "usage": {"input_tokens": 20, "output_tokens": 5}}})
                    expected = 25
                self.save(provider, rows)
                content = path.read_text()
                self.assertIn("Usage: " + str(expected) + " tok", content)
                self.assertNotIn("Usage: 15 tok", content)
                self.assertEqual(content.count("> [!NOTE]"), 1)
                self.assertIn("message_count: 1", content)

    def test_codex_explicit_turn_change_without_user_separates_usage(self):
        for split, context in [(False, True), (True, True), (False, False), (True, False)]:
            with self.subTest(split=split, context=context):
                session = "explicit-turn-" + str(split) + "-" + str(context)
                rows = self.rows("codex", answer="最初の回答")
                if not context:
                    rows[1]["payload"]["turn_id"] = "turn1"
                    rows[2]["payload"]["turn_id"] = "turn1"
                    del rows[0]
                if split:
                    self.save("codex", rows, session)
                next_rows = self.rows("codex", 2, answer="次の回答")
                del next_rows[1]  # user_messageがなくてもturn_contextの変化を保持する。
                if not context:
                    next_rows[1]["payload"]["turn_id"] = "turn2"
                    del next_rows[0]
                rows += next_rows
                path = self.save("codex", rows, session)
                content = path.read_text()
                self.assertEqual(content.count("> [!NOTE] Codex"), 2)
                self.assertEqual(content.count("Usage: 15 tok"), 2)
                self.assertIn("message_count: 3", content)

    def test_bridge_failure_exit_codes_and_stop_hook_compatibility(self):
        environment = os.environ.copy()
        environment.update(HOME=str(self.root/"process-home"), OBSIDIAN_VAULT=str(self.root/"process-vault"),
                           PI_OBSIDIAN_STATE_DIR=str(self.root/"process-pi"), PATH="/usr/bin:/bin")
        for provider in PROVIDERS:
            with self.subTest(provider=provider):
                result = subprocess.run(["/usr/bin/python3", str(ROOT/(provider+"_save.py"))],
                    input="{invalid", text=True, env=environment, capture_output=True, timeout=10)
                self.assertEqual(result.returncode, 1 if provider in ("opencode", "pi") else 0)
                self.assertFalse(list((self.root/"process-vault").rglob("*.md")))


if __name__ == "__main__":
    unittest.main()
