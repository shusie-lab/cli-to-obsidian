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

    def test_failed_sync_preserves_original_and_removes_temporary_file(self):
        for provider, module in self.modules.items():
            with self.subTest(provider=provider):
                path = self.root / provider / 'note.md'
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('original')
                with patch.object(module.os, 'fsync', side_effect=OSError('disk sync failed')):
                    with self.assertRaises(OSError):
                        module.atomic_write_text(path, 'replacement')
                self.assertEqual(path.read_text(), 'original')
                self.assertEqual(list(path.parent.iterdir()), [path])

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
                self.assertEqual(path.read_text().count("\n回答\n"), 1)

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

    def test_deepseek_flash_aliases_and_cached_input(self):
        aliases = ("deepseek-flash", "deepseek-v4.1-flash", "deepseek-v4-1-flash",
                   "deepseek/deepseek-v4.1-flash", "opencode/deepseek-v4.1-flash-free",
                   "deepseek/deepseek-v4.1-flash:free")
        for provider in ("codex", "claude", "opencode", "pi"):
            module = self.modules[provider]
            for model in aliases:
                with self.subTest(provider=provider, model=model):
                    usage = {"input_tokens": 1_000_000, "cached_input_tokens": 600_000,
                             "cache_write_input_tokens": 100_000, "output_tokens": 100_000}
                    # fresh .09 + read .0036 + write .03 + output .12
                    self.assertEqual(module._usage_cost(model, usage), Decimal("0.2436"))
                    self.assertFalse(module.is_long_context_usage(model, usage))
                    content = self.save(provider, self.price_rows(provider, model), model).read_text()
                    self.assertIn("total_cost_usd: 0.000009", content)

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
            self.assertEqual(module.APPLY_LONG_CONTEXT_PRICING, provider != "codex")
            for enabled in (False, True):
                with patch.object(module, "APPLY_LONG_CONTEXT_PRICING", enabled):
                    for tokens, normal, increased in ((272_000, "0.3791", "0.3791"),
                                                      (272_001, "0.379102", "0.758154")):
                        with self.subTest(provider=provider, enabled=enabled, tokens=tokens):
                            # 総入力にはread10万/write5万を含む。倍率は通常入力とcacheの双方へ。
                            usage = {"input_tokens": tokens, "cached_input_tokens": 100_000,
                                     "cache_write_input_tokens": 50_000, "output_tokens": 10}
                            self.assertEqual(module._usage_cost("openai/gpt-6.1-sol", usage),
                                             Decimal(increased if enabled else normal))
                            self.assertEqual(module.is_long_context_usage("openai/gpt-6.1-sol", usage),
                                             enabled and tokens > 272_000)
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
                self.assertEqual(module._usage_cost("gpt-6.1-sol", large),
                                 Decimal("0.7" if provider == "codex" else "1.35"))
                if provider != "codex":
                    self.assertTrue(large["long_context"])
        native = normalized("opencode", 300_000, cost=7.5)
        self.assertEqual(Decimal(native["cost_usd"]), Decimal("1.35"))
        self.assertEqual(Decimal(native["reported_cost_usd"]), Decimal("7.5"))
        self.assertTrue(native["long_context"])
        self.assertEqual(opencode.summarize_usage([
            {"type": "assistant", "model": "gpt-6.1-sol", "usage": native}])["total_cost_usd"], Decimal("1.35"))
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
                    self.assertIn("total_cost_usd: 1.35", path.read_text())
                    self.assertIn("Usage: 310,000 tok · $1.35 ⚠︎ 272K+ · 取得コスト: $7.5</small>", path.read_text())

    def test_codex_long_context_toggle_refreshes_existing_note(self):
        module = self.modules["codex"]
        rows = self.price_rows("codex", "gpt-6.1-sol")
        rows[-1]["payload"]["usage"] = {"input_tokens": 300_000, "cached_input_tokens": 100_000,
            "cache_write_input_tokens": 50_000, "output_tokens": 10, "total_tokens": 300_010}
        for enabled, expected in ((False, "0.4351"), (True, "0.87015"), (False, "0.4351")):
            with self.subTest(enabled=enabled), patch.object(module, "APPLY_LONG_CONTEXT_PRICING", enabled):
                path = self.save("codex", rows)
                content = path.read_text()
                self.assertIn("total_cost_usd: " + expected + "\n", content)
                self.assertIn("Usage: 300,010 tok · $" + expected +
                              (" ⚠︎ 272K+" if enabled else "") + "</small>", content)
                self.assertEqual("⚠︎ 272K+" in content, enabled)
                self.assertEqual(content.count("> [!QUESTION] User"), 1)
                self.assertEqual(content.count("> [!NOTE] Codex"), 1)

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
        prices = {"deepseek-v4.1-flash": "0.000009", "gpt-6-astra": "0.00035", "gpt-6.1-sol": "0.00007",
                  "gpt-6.1-terra": "0.000035", "gpt-6-luna": "0.0000035", "muse-spark-1.3-contributor-free": "0.000002",
                  "nemotron-3-ultra-free": "0.000016", "claude-opus-5-5": "0.00014",
                  "claude-sonnet-5-5": "0.00007", "claude-haiku-5-5": "0.0000035", "gemini-3.8-flash": "0.00002625",
                  "claude-fable-5-1": "0.00035"}
        for provider in ("codex", "claude", "opencode", "pi"):
            self.assertEqual(set(self.modules[provider].STANDARD_USAGE_RATES), set(prices))
            for model, price in prices.items():
                with self.subTest(provider=provider, model=model):
                    path = self.save(provider, self.price_rows(provider, model), model)
                    content = path.read_text()
                    self.assertIn("total_cost_usd: " + price, content)
                    suffix = " · 取得コスト: 未取得" if provider in {"opencode", "pi"} else ""
                    self.assertIn("Usage: 15 tok · $" + price + suffix + "</small>", content)

        claude = self.modules["claude"]
        usage = claude._claude_usage_record({"model": "gpt-6.1-sol", "usage": {
            "input_tokens": 150_001, "cache_read_input_tokens": 100_000,
            "cache_creation_input_tokens": 50_000, "output_tokens": 10}})
        self.assertTrue(usage["long_context"])
        self.assertEqual(usage["cost_usd"], Decimal("0.870154"))

    def test_terra_token_rates_long_context_and_price_history(self):
        for provider in ("codex", "claude", "opencode", "pi"):
            module = self.modules[provider]
            for model in ("gpt-6.1-terra", "openai/gpt-6.1-terra"):
                with self.subTest(provider=provider, model=model):
                    with patch.object(module, "APPLY_LONG_CONTEXT_PRICING", False):
                        for usage, expected in (
                            ({"input_tokens": 1_000_000}, "1"),
                            ({"input_tokens": 1_000_000, "cached_input_tokens": 1_000_000}, "0.05"),
                            ({"input_tokens": 1_000_000, "cache_write_input_tokens": 1_000_000}, "1.25"),
                            ({"output_tokens": 1_000_000}, "5"),
                        ):
                            self.assertEqual(module._usage_cost(model, usage), Decimal(expected))
                    with patch.object(module, "APPLY_LONG_CONTEXT_PRICING", True):
                        for tokens, expected in ((272_000, "0.18955"), (272_001, "0.379077")):
                            usage = {"input_tokens": tokens, "cached_input_tokens": 100_000,
                                     "cache_write_input_tokens": 50_000, "output_tokens": 10}
                            self.assertEqual(module._usage_cost(model, usage), Decimal(expected))
                    for version in ("3.11.1", "3.12.0"):
                        with module.usage_price_version(version):
                            self.assertIsNone(module._usage_cost(model, {"input_tokens": 10}))
                    self.assertEqual(module.PRICE_CHECKED_AT["gpt-6.1-terra"], "2026-10-27")

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
                        self.assertNotIn("total_cost_usd:", path.read_text())
                        self.assertIn("  usd: 7.5", path.read_text())

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

    def completed_rows(self, provider, turn=1, user="質問", answer="回答", end="2026-01-01T01:02:15Z"):
        rows = self.rows(provider, turn, user, answer)
        if provider == "codex":
            rows.append({"type": "event_msg", "timestamp": end,
                         "payload": {"type": "task_complete", "turn_id": "turn" + str(turn), "completed_at": end}})
        elif provider == "claude":
            rows.append({"type": "system", "subtype": "turn_duration", "timestamp": end, "durationMs": 100})
        elif provider == "opencode":
            rows[1]["info"]["time"]["completed"] = end
        elif provider == "pi":
            rows[1]["timestamp"] = end
            # モデル側timestamp（開始時刻）とは区別する。
            rows[1]["message"]["timestamp"] = 1767229200000
        return rows

    def test_answer_duration_uses_completion_and_is_stable(self):
        for provider in PROVIDERS:
            with self.subTest(provider=provider):
                rows = self.completed_rows(provider)
                path = self.save(provider, rows)
                content = path.read_text()
                expected = "取得不可" if provider == "agy" else "2分15秒"
                self.assertEqual(content.count("⏱ 回答所要時間: " + expected), 1)
                self.assertIn("⏱ 2026-01-01 10:00:00", content)
                self.save(provider, rows)
                self.assertEqual(path.read_text(), content)
                # 次の依頼の時刻が過去の回答時間を変えない。
                next_rows = self.completed_rows(provider, 2, "次の質問", "次の回答")
                path = self.save(provider, rows + next_rows)
                self.assertEqual(path.read_text().count("⏱ 回答所要時間: " + expected), 2)

    def test_missing_invalid_or_reversed_completion_is_unavailable(self):
        for provider in PROVIDERS:
            for end in (None, "broken", "2026-01-01T00:59:59Z"):
                with self.subTest(provider=provider, end=end):
                    rows = self.completed_rows(provider, end=end)
                    path = self.save(provider, rows, "invalid-" + str(end))
                    self.assertIn("⏱ 回答所要時間: 取得不可", path.read_text())

    def test_late_completion_updates_without_visible_messages_or_usage(self):
        for provider in ("codex", "claude"):
            with self.subTest(provider=provider):
                rows = self.rows(provider, usage=False)
                path = self.save(provider, rows)
                self.assertIn("⏱ 回答所要時間: 取得不可", path.read_text())
                rows.append(self.completed_rows(provider)[-1])
                self.save(provider, rows)
                content = path.read_text()
                self.assertEqual(content.count("⏱ 回答所要時間: 2分15秒"), 1)
                self.assertNotIn("取得不可", content)
                self.assertEqual(content.count("> [!NOTE]"), 1)
                self.save(provider, rows)
                self.assertEqual(path.read_text(), content)

    def test_duration_formats_and_turn_boundaries_are_shared(self):
        for provider, module in self.modules.items():
            with self.subTest(provider=provider):
                for seconds, expected in ((0, "0秒"), (0.9, "0秒"), (59, "59秒"),
                                          (60, "1分0秒"), (3661, "1時間1分1秒"),
                                          (None, "取得不可"), (-1, "取得不可"),
                                          (True, "取得不可"), (float("nan"), "取得不可")):
                    self.assertEqual(module.format_answer_duration(seconds), "⏱ 回答所要時間: " + expected)
                messages = [
                    {"type": "user", "text": "質問", "timestamp": self.stamp},
                    {"type": "assistant", "text": "途中", "model": "one", "completed_at": "2026-01-01T01:00:10Z"},
                    {"type": "assistant", "text": "最終", "model": "two", "completed_at": "2026-01-01T01:02:15Z"},
                    {"type": "user", "text": "", "timestamp": None},
                    {"type": "assistant", "text": "別の回答", "completed_at": "2026-01-01T01:03:00Z"},
                ]
                result = module.annotate_answer_timings(messages)
                self.assertNotIn("answer_duration_seconds", result[1])
                self.assertEqual(result[2]["answer_duration_seconds"], 135)
                self.assertIsNone(result[4]["answer_duration_seconds"])
                self.assertNotIn("answer_duration_seconds", messages[2])

    def test_intermediate_and_final_answers_have_one_total_even_across_saves(self):
        for provider in ("codex", "claude", "opencode", "pi"):
            with self.subTest(provider=provider):
                rows = self.rows(provider, answer="途中経過")
                path = self.save(provider, rows)
                completed = self.completed_rows(provider, answer="最終回答")
                final = completed[2] if provider == "codex" else completed[1]
                if provider == "opencode":
                    final["info"]["id"] = "final"
                elif provider == "pi":
                    final["id"] = "final"
                elif provider == "claude":
                    final["message"]["id"] = "final"
                rows.append(final)
                if provider in ("codex", "claude"):
                    rows.append(completed[-1])
                self.save(provider, rows)
                content = path.read_text()
                self.assertIn("途中経過", content)
                self.assertIn("最終回答", content)
                self.assertEqual(content.count("⏱ 回答所要時間:"), 1)
                self.assertIn("⏱ 回答所要時間: 2分15秒", content)
                self.assertEqual(content.count("> [!NOTE]"), 1)

    def test_codex_completion_payload_uses_unix_seconds_not_milliseconds(self):
        rows = self.completed_rows("codex")
        rows[-1]["payload"]["completed_at"] = 1767229335
        path = self.save("codex", rows)
        self.assertIn("回答所要時間: 2分15秒", path.read_text())
        del rows[-1]["timestamp"]
        self.save("codex", rows)
        self.assertIn("回答所要時間: 2分15秒", path.read_text())

    def test_codex_explicit_final_item_completion_is_available_before_task_complete(self):
        rows = self.rows("codex", usage=False)
        rows[-1] = {"type": "event_msg", "timestamp": "2026-01-01T01:02:15Z",
                    "payload": {"type": "item_completed", "item": {"type": "AgentMessage",
                        "phase": "final_answer", "content": [{"type": "text", "text": "最終回答"}]}}}
        path = self.save("codex", rows)
        self.assertIn("回答所要時間: 2分15秒", path.read_text())
        rows[-1]["payload"]["item"]["phase"] = "commentary"
        # 別セッションなら途中経過を最終回答と誤認しない。
        path = self.save("codex", rows, "commentary-only")
        self.assertIn("回答所要時間: 取得不可", path.read_text())

    def test_millisecond_times_missing_start_and_model_switch(self):
        for provider, module in self.modules.items():
            with self.subTest(provider=provider):
                messages = [
                    {"type": "user", "text": "質問", "timestamp": 1767229200000},
                    {"type": "assistant", "text": "途中", "model": "one", "completed_at": 1767229210000},
                    {"type": "assistant", "text": "最後", "model": "two", "completed_at": 1767229335000},
                ]
                result = module.annotate_answer_timings(messages)
                self.assertEqual(result[-1]["answer_duration_seconds"], 135)
                self.assertNotIn("answer_duration_seconds", result[1])
                for start in (None, "invalid", False):
                    messages[0]["timestamp"] = start
                    self.assertIsNone(module.annotate_answer_timings(messages)[-1]["answer_duration_seconds"])

    def test_timing_refresh_does_not_edit_headers_in_answer_code_fences(self):
        module = self.modules["codex"]
        rows = self.completed_rows("codex", answer="例:\n```markdown\n> [!NOTE] Codex\n> <small>⏱ 回答所要時間: 例</small>\n```\n以上")
        path = self.save("codex", rows)
        content = path.read_text()
        self.assertIn("> <small>⏱ 回答所要時間: 例</small>", content)
        self.assertEqual(content.count("回答所要時間: 2分15秒"), 1)
        self.save("codex", rows)
        self.assertEqual(path.read_text(), content)

    def save_agy_stop(self, rows, received, session="observed", **fields):
        module = self.modules["agy"]
        transcript = self.root / (session + ".jsonl")
        transcript.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        payload = {"conversationId": session, "transcriptPath": str(transcript),
                   "workspacePaths": ["/tmp/example"], "fullyIdle": True, "terminationReason": "model_stop"}
        payload.update(fields)
        with patch.object(module, "now_jst", return_value=received), patch.object(module, "retrieve_quota", return_value=None):
            module.handle_stop_event(payload)
        return Path(module.load_state(session)["output_path"])

    def test_agy_stop_observation_is_fixed_and_recovers_after_state_loss(self):
        module = self.modules["agy"]
        rows = self.rows("agy")
        received = self.created + timedelta(seconds=135)
        path = self.save_agy_stop(rows, received)
        original = path.read_text()
        self.assertIn("回答所要時間: 2分15秒", original)
        self.assertEqual(len(module.recover_answer_completions(original)), 1)
        self.save_agy_stop(rows, received + timedelta(hours=1))
        self.assertEqual(path.read_text(), original)
        module.state_path("observed").unlink()
        self.save_agy_stop(rows, received + timedelta(hours=2))
        self.assertEqual(path.read_text(), original)
        rows += self.rows("agy", 2, "次の質問", "次の回答")
        path = self.save_agy_stop(rows, self.created + timedelta(seconds=200))
        content = path.read_text()
        self.assertEqual(content.count("回答所要時間: 2分15秒"), 1)
        self.assertEqual(content.count("回答所要時間: 3分20秒"), 1)

    def test_agy_only_current_turn_is_observed_and_no_quota_delay_is_added(self):
        module = self.modules["agy"]
        rows = self.rows("agy") + self.rows("agy", 2, "次の質問", "次の回答")
        received = self.created + timedelta(seconds=135)
        transcript = self.root / "quota-delay.jsonl"
        transcript.write_text("".join(json.dumps(row) + "\n" for row in rows))
        clock = [received]
        def slow_quota():
            clock[0] += timedelta(seconds=30)
            return None
        with patch.object(module, "now_jst", side_effect=lambda: clock[0]), patch.object(module, "retrieve_quota", side_effect=slow_quota):
            module.handle_stop_event({"conversationId": "delay", "transcriptPath": str(transcript),
                                      "fullyIdle": True, "terminationReason": "model_stop"})
        content = Path(module.load_state("delay")["output_path"]).read_text()
        self.assertEqual(content.count("回答所要時間: 取得不可"), 1)
        self.assertEqual(content.count("回答所要時間: 2分15秒"), 1)

    def test_agy_busy_error_or_incomplete_stop_does_not_complete_answer(self):
        for fields in ({"fullyIdle": False}, {"terminationReason": "error"},
                       {"terminationReason": "max_steps_exceeded"}, {"error": "failed"},
                       {"fullyIdle": None}, {"terminationReason": None}):
            with self.subTest(fields=fields):
                session = "not-complete-" + str(len(list(self.root.glob("not-complete-*.jsonl"))))
                rows = self.rows("agy")
                received = self.created + timedelta(seconds=20)
                path = self.save_agy_stop(rows, received, session, **fields)
                self.assertIn("回答所要時間: 取得不可", path.read_text())
                path = self.save_agy_stop(rows, received + timedelta(seconds=115), session)
                content = path.read_text()
                self.assertIn("回答所要時間: 2分15秒", content)
                self.assertEqual(content.count("> [!NOTE]"), 1)

    def test_agy_intermediate_stop_and_final_answer_keep_one_total(self):
        rows = self.rows("agy", answer="途中経過")
        self.save_agy_stop(rows, self.created + timedelta(seconds=10), fullyIdle=False)
        final = self.rows("agy", 2, answer="最終回答")[1]
        rows.append(final)
        path = self.save_agy_stop(rows, self.created + timedelta(seconds=135))
        content = path.read_text()
        self.assertIn("途中経過", content)
        self.assertIn("最終回答", content)
        self.assertEqual(content.count("回答所要時間:"), 1)
        self.assertIn("回答所要時間: 2分15秒", content)

    def test_agy_replay_without_observation_does_not_use_now(self):
        module = self.modules["agy"]
        rows = self.rows("agy")
        transcript = self.root / "replay.jsonl"
        transcript.write_text("".join(json.dumps(row) + "\n" for row in rows))
        with patch.object(module, "retrieve_quota", return_value=None):
            module.handle_stop_event({"conversationId": "replay", "transcriptPath": str(transcript),
                                      "fullyIdle": True, "terminationReason": "model_stop"}, observe_completion=False)
        content = Path(module.load_state("replay")["output_path"]).read_text()
        self.assertIn("回答所要時間: 取得不可", content)
        self.assertNotIn("agy_answer_completions:", content)

    def test_agy_observation_survives_unclosed_code_fence_in_answer(self):
        module = self.modules["agy"]
        rows = self.rows("agy", answer="例:\n```text\n未閉鎖のコード例")
        received = self.created + timedelta(seconds=135)
        path = self.save_agy_stop(rows, received)
        original = path.read_text()
        self.assertEqual(len(module.recover_answer_completions(original)), 1)
        module.state_path("observed").unlink()
        self.save_agy_stop(rows, received + timedelta(hours=1))
        self.assertEqual(path.read_text(), original)


if __name__ == "__main__":
    unittest.main()
