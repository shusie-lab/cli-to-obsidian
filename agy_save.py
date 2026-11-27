#!/usr/bin/python3
from __future__ import annotations
"""
Antigravity CLI の会話履歴を Obsidian に自動保存するフックスクリプト。
出力形式: Obsidian AI Exporter 互換（YAMLフロントマター + callout block形式）

対応イベント:
  - Stop : セッション終了時（またはアイドル移行時）に Markdown ファイルの作成・追記を行う
"""

__version__ = "4.6.3"

import json
import math
import re
import sys
import os
import fcntl
import hashlib
import signal
import subprocess
import tempfile
from bisect import bisect_right
from contextlib import contextmanager
from decimal import Decimal
from functools import wraps
from datetime import datetime, timezone, timedelta
from pathlib import Path
import logging
from logging.handlers import RotatingFileHandler

# ============================================================
# 定数
# ============================================================
LOGGER_NAME = "obsidian_save_antigravity"
JST = timezone(timedelta(hours=9))

OBSIDIAN_VAULT = Path(
    os.environ.get(
        "OBSIDIAN_VAULT",
        str(Path.home() / "obsidian")
    )
)
DEFAULT_OUTPUT_DIR = "生成AI/ChatLog"
DEFAULT_SOURCE = "antigravity-cli"
APPLY_LONG_CONTEXT_PRICING = False

# BEGIN GENERATED: usage-prices
# 参考単価の正本。Opus 5.5のcache readは2026-10-06に公式資料で確認。
# https://developers.openai.com/api/docs/pricing
# https://platform.claude.com/docs/en/about-claude/pricing
# https://dev.meta.ai/docs/pricing-rate-limits/
# https://openrouter.ai/nvidia/nemotron-3-ultra-550b-a55b
# Gemini 3.8 Flash: 2026-10-06時点のStandard（年末までの割引価格）。
# https://ai.google.dev/gemini-api/docs/pricing#gemini-3.8-flash
# https://openrouter.ai/api/v1/models/google/gemini-3.8-flash/endpoints
# Haiku 5.5: 2026-10-09確認。10万token超は全token種別で5倍。
# https://platform.claude.com/docs/en/models/haiku-5-5/overview
# DeepSeek V4.1 Flash: 2026-10-16確認。固定の参考換算にはピーク単価を使用。
# https://api-docs.deepseek.com/quick_start/pricing/
STANDARD_USAGE_RATES = {
    # 2026-11-18確認。cache writeは30分保持のStandard単価。
    # https://developers.openai.com/api/docs/models/gpt-6.2-astra
    "gpt-6.2-astra": {
        "input": Decimal("8.00"),
        "cached_input": Decimal("0.40"),
        "cache_write_input": Decimal("10.00"),
        "output": Decimal("40.00"),
    },
    # 2026-11-18確認。cache writeは30分保持のStandard単価。
    # https://developers.openai.com/api/docs/models/gpt-6.2-sol
    "gpt-6.2-sol": {
        "input": Decimal("2.00"),
        "cached_input": Decimal("0.10"),
        "cache_write_input": Decimal("2.50"),
        "output": Decimal("10.00"),
    },
    # 2026-10-25: https://platform.claude.com/docs/en/models/fable-5-1/overview
    "claude-fable-5-1": {
        "input": Decimal("10.00"),
        "cached_input": Decimal("0.25"),
        "cache_write_input": Decimal("12.50"),
        "output": Decimal("50.00"),
        "cache_write_input_1h": Decimal("20.00"),
    },
    "deepseek-v4.1-flash": {
        "input": Decimal("0.30"),
        "cached_input": Decimal("0.006"),
        # 自動キャッシュの書込分もcache missの入力単価で換算。
        "cache_write_input": Decimal("0.30"),
        "output": Decimal("1.20"),
    },
    "gpt-6-astra": {
        "input": Decimal("10.00"),
        "cached_input": Decimal("1.00"),
        "cache_write_input": Decimal("12.50"),
        "output": Decimal("50.00"),
    },
    "gpt-6.1-sol": {
        "input": Decimal("2.00"),
        "cached_input": Decimal("0.10"),
        "cache_write_input": Decimal("2.50"),
        "output": Decimal("10.00"),
    },
    # GPT-6.1 Terra: 2026-10-27確認。cache writeは30分保持の標準単価。
    # https://developers.openai.com/api/docs/models/gpt-6.1-terra
    "gpt-6.1-terra": {
        "input": Decimal("1.00"),
        "cached_input": Decimal("0.05"),
        "cache_write_input": Decimal("1.25"),
        "output": Decimal("5.00"),
    },
    "gpt-6-luna": {
        "input": Decimal("0.10"),
        "cached_input": Decimal("0.01"),
        "cache_write_input": Decimal("0.125"),
        "output": Decimal("0.50"),
    },
    # 2026-11-19確認。2026-11-30までのStandard割引価格（12月1日以降はinput 4.00 / cached 0.20 / output 20.00）。
    # https://ai.google.dev/gemini-api/docs/pricing#gemini-4-argon
    # https://openrouter.ai/api/v1/models/google/gemini-4-argon/endpoints
    "gemini-4-argon": {
        "input": Decimal("2.00"),
        "cached_input": Decimal("0.10"),
        # OpenRouter Standardのinput_cache_writeを100万token単位へ変換。
        "cache_write_input": Decimal("0.1875"),
        "output": Decimal("10.00"),
    },
    # 2026-11-19確認。Standard単価。
    # https://ai.google.dev/gemini-api/docs/pricing#gemini-4-neon
    # https://openrouter.ai/api/v1/models/google/gemini-4-neon/endpoints
    "gemini-4-neon": {
        "input": Decimal("3.00"),
        "cached_input": Decimal("0.15"),
        # OpenRouter Standardのinput_cache_writeを100万token単位へ変換。
        "cache_write_input": Decimal("0.0833333333333333"),
        "output": Decimal("15.00"),
    },
    # 2026-11-19確認。Standard単価。
    # https://ai.google.dev/gemini-api/docs/pricing#gemini-4-hydrogen
    # https://openrouter.ai/api/v1/models/google/gemini-4-hydrogen/endpoints
    "gemini-4-hydrogen": {
        "input": Decimal("1.50"),
        "cached_input": Decimal("0.075"),
        # OpenRouter Standardのinput_cache_writeを100万token単位へ変換。
        "cache_write_input": Decimal("0.0833333333333333"),
        "output": Decimal("7.50"),
    },
    "gemini-3.8-flash": {
        "input": Decimal("0.75"),
        "cached_input": Decimal("0.075"),
        # OpenRouter Standardのinput_cache_writeを100万token単位へ変換。
        # Googleの時間単位のstorage priceとは別の掲載単位。
        "cache_write_input": Decimal("0.0416666666666667"),
        "output": Decimal("3.75"),
    },
    "muse-spark-1.3-contributor-free": {
        "input": Decimal("0.10"),
        "cached_input": Decimal("0.002"),
        "cache_write_input": Decimal("0.10"),
        "output": Decimal("0.20"),
    },
    "nemotron-3-ultra-free": {
        "input": Decimal("0.50"),
        "cached_input": Decimal("0.10"),
        "cache_write_input": Decimal("0.50"),
        "output": Decimal("2.20"),
    },
    "claude-opus-5-5": {
        "input": Decimal("4.00"),
        "cached_input": Decimal("0.20"),
        "cache_write_input": Decimal("5.00"),
        "output": Decimal("20.00"),
        "cache_write_input_1h": Decimal("8.00"),
    },
    "claude-haiku-5-5": {
        "input": Decimal("0.10"),
        "cached_input": Decimal("0.01"),
        "cache_write_input": Decimal("0.125"),
        "output": Decimal("0.50"),
        "cache_write_input_1h": Decimal("0.20"),
    },
    "claude-sonnet-5-5": {
        "input": Decimal("2.00"),
        # 2026-10-25: https://platform.claude.com/docs/en/models/sonnet-5-5/overview
        "cached_input": Decimal("0.10"),
        "cache_write_input": Decimal("2.50"),
        "output": Decimal("10.00"),
        "cache_write_input_1h": Decimal("4.00"),
    },
}

# 従来の呼び出し名との互換alias。単価の正本は上の辞書1つだけ。
USAGE_COST_USD_PER_1M_TOKEN_TYPE = STANDARD_USAGE_RATES
# END GENERATED: usage-prices

# BEGIN GENERATED: usage-price-history
from contextvars import ContextVar

PRICE_VERSION = "4.5.0"
PRICE_RECORDED_AT = "2026-11-19"
PRICE_CHECKED_AT = {
    "gemini-4-argon": "2026-11-19",
    "gemini-4-neon": "2026-11-19",
    "gemini-4-hydrogen": "2026-11-19",
    "gpt-6.2-astra": "2026-11-18",
    "gpt-6.2-sol": "2026-11-18",
    'claude-fable-5-1': "2026-10-28",
    'deepseek-v4.1-flash': "2026-10-28",
    'gpt-6-astra': "2026-10-28",
    'gpt-6.1-sol': "2026-10-28",
    'gpt-6.1-terra': "2026-10-28",
    'gpt-6-luna': "2026-10-28",
    'gemini-3.8-flash': "2026-10-28",
    'muse-spark-1.3-contributor-free': "2026-10-28",
    'nemotron-3-ultra-free': "2026-10-28",
    'claude-opus-5-5': "2026-10-28",
    'claude-haiku-5-5': "2026-10-28",
    'claude-sonnet-5-5': "2026-10-28",
}
PRICE_SOURCES = {
    "claude": "https://platform.claude.com/docs/en/about-claude/pricing",
    "gpt": "https://developers.openai.com/api/docs/pricing",
    "gemini": "https://ai.google.dev/gemini-api/docs/pricing",
    "deepseek": "https://api-docs.deepseek.com/quick_start/pricing/",
    "muse": "https://dev.meta.ai/docs/pricing-rate-limits/",
    "nemotron": "https://openrouter.ai/nvidia/nemotron-3-ultra-550b-a55b",
}
# 現在表から過去へ戻す差分。next_versionは直後の価格版を明示する。
# changesは変更したtoken単価の旧値。削除モデルは全単価を残し、追加モデルは除外しない。
PRICE_HISTORY = {
    "4.4.0": {"recorded_at": "2026-11-18", "next_version": "4.5.0", "changes": {}},
    "4.2.0": {"recorded_at": "2026-10-27", "next_version": "4.4.0", "changes": {}},
    "3.12.0": {"recorded_at": "2026-10-25", "next_version": "4.2.0", "changes": {}},
    "3.11.1": {"recorded_at": "2026-10-21", "next_version": "3.12.0", "changes": {
        "claude-sonnet-5-5": {"cached_input": Decimal("0.20")},
    }},
}
_PRICE_CONTEXT = ContextVar("obsidian_usage_price_version", default=PRICE_VERSION)


def active_price_version() -> str:
    return _PRICE_CONTEXT.get()


def _usage_rates_for_version(version: str) -> dict:
    if version == PRICE_VERSION:
        return STANDARD_USAGE_RATES
    history = PRICE_HISTORY[version]
    # コピーに差分を適用し、現在表や他の価格版を変更しない。
    rates = {model: dict(values) for model, values in
             _usage_rates_for_version(history["next_version"]).items()}
    for model, changes in history["changes"].items():
        rates.setdefault(model, {}).update(changes)
    return rates


def active_usage_rates() -> dict:
    return _usage_rates_for_version(active_price_version())


@contextmanager
def usage_price_version(version: str):
    if version != PRICE_VERSION and version not in PRICE_HISTORY:
        raise ValueError("未登録の価格版: " + str(version))
    token = _PRICE_CONTEXT.set(version)
    try:
        yield
    finally:
        _PRICE_CONTEXT.reset(token)


def note_price_version(content: str) -> str | None:
    frontmatter = re.match(r"\A---\s*\n(.*?)\n---", content, re.DOTALL)
    if not frontmatter:
        return None
    usage = re.search(r"(?m)^usage:\n((?:[ \t].*\n|\n)*)", frontmatter.group(1) + "\n")
    match = re.search(r'^  price_version: (.+)$', usage.group(1), re.M) if usage else None
    if not match:
        return None
    try:
        value = json.loads(match.group(1))
    except ValueError:
        value = match.group(1).strip()
    return value if isinstance(value, str) else None


def usage_price_fields(usage: dict) -> list[str]:
    version = active_price_version()
    recorded = PRICE_RECORDED_AT if version == PRICE_VERSION else PRICE_HISTORY[version]["recorded_at"]
    models = sorted({reference_price_model(model) for model in usage.get("by_model", {})})
    sources = {key: value for key, value in PRICE_SOURCES.items()
               if any(model.startswith(key + "-") for model in models)}
    # 過去料金の正確な適用開始日は確認できないため推定しない。
    return ["  price_version: " + yaml_quote(version),
            "  price_recorded_at: " + yaml_quote(recorded),
            "  price_effective_from: null", "  price_checked_at:" + ("" if models else " {}"),
            *("    " + yaml_quote(model) + ": " + (yaml_quote(PRICE_CHECKED_AT[model])
              if version == PRICE_VERSION and model in PRICE_CHECKED_AT else "null")
              for model in models), "  price_sources:" + ("" if sources else " {}"),
            *("    " + yaml_quote(key) + ": " + yaml_quote(value) for key, value in sources.items())]
# END GENERATED: usage-price-history

# BEGIN GENERATED: usage-model
def _normalize_usage_model(model: object) -> str:
    """プロバイダ接頭辞などを除き、単価表と照合できるモデル名へ変換する。"""
    name = str(model or "").strip().lower()
    if "/" in name:
        name = name.rsplit("/", 1)[-1]
    aliases = {
        "claude-fable-5.1": "claude-fable-5-1",
        "deepseek-flash": "deepseek-v4.1-flash",
        "deepseek-v4-1-flash": "deepseek-v4.1-flash",
        "deepseek-v4.1-flash-free": "deepseek-v4.1-flash",
        "deepseek-v4.1-flash:free": "deepseek-v4.1-flash",
        "gemini3.8-flash": "gemini-3.8-flash",
        "gemini-4-argon-20261013": "gemini-4-argon",
        "gemini-4-neon-20261118": "gemini-4-neon",
        "gemini-4-hydrogen-20261112": "gemini-4-hydrogen",
        "nemotron-3-ultra-550b-a55b:free": "nemotron-3-ultra-free",
        "claude-opus-5.5": "claude-opus-5-5",
        "claude-haiku-5.5": "claude-haiku-5-5",
        "claude-sonnet-5.5": "claude-sonnet-5-5",
        "muse-spark-1.3-contributor:free": "muse-spark-1.3-contributor-free",
        "muse-spark-1.3-contributor": "muse-spark-1.3-contributor-free",
    }
    return aliases.get(name, name)
# END GENERATED: usage-model

# BEGIN GENERATED: usage-summary
USAGE_FIELDS = (
    "input_tokens", "cached_input_tokens", "cache_write_input_tokens",
    "output_tokens", "reasoning_output_tokens", "total_tokens",
)
USAGE_TOKEN_FIELDS = USAGE_FIELDS


def _empty_usage_totals() -> dict:
    return {field: 0 for field in USAGE_FIELDS}


def _decimal_cost(value: object) -> Decimal | None:
    """数値・文字列金額を有限で非負のDecimalへ変換する。"""
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        return None
    try:
        result = Decimal(str(value))
    except (ArithmeticError, ValueError, TypeError):
        return None
    return result if result.is_finite() and result >= 0 else None


def calculate_usage_cost(rates: dict, usage: dict, *, input_multiplier=1, output_multiplier=1) -> Decimal | None:
    """各recordを換算し、キャッシュ・推論の内数を二重計上しない。"""
    try:
        values = [usage.get(key, 0) for key in ("input_tokens", "cached_input_tokens", "cache_write_input_tokens", "output_tokens")]
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in values):
            return None
        total_input, cached, written, output = values
        fresh = total_input - cached - written
        if fresh < 0:
            return None
        return ((
            (Decimal(fresh) * rates["input"] + Decimal(cached) * rates["cached_input"]
             + Decimal(written) * rates["cache_write_input"]) * Decimal(str(input_multiplier))
            + Decimal(output) * rates["output"] * Decimal(str(output_multiplier))
        ) / Decimal(1_000_000)).normalize()
    except (ArithmeticError, KeyError, TypeError, ValueError):
        return None


def sum_reported_costs(reports) -> dict | None:
    """CLIの回答単位の報告値を合計する。セッション累計は加算しない。"""
    reports = [report for report in reports if report and report.get("scope") == "message_sum"]
    if not reports:
        return None
    total = {"source": ", ".join(sorted({report["source"] for report in reports})),
             "scope": "message_sum", "available_records": 0, "total_records": 0}
    for report in reports:
        total["total_records"] += report.get("total_records", 1)
        total["available_records"] += report.get("available_records", 0)
        amount = _decimal_cost(report.get("usd"))
        if amount is not None:
            total["usd"] = total.get("usd", Decimal(0)) + amount
    if "usd" in total and total["available_records"] < total["total_records"]:
        total["incomplete"] = True
    return total


def reported_cost_fields(report: dict | None) -> list[str]:
    if not report:
        return []
    lines = ["reported_cost:", "  source: " + yaml_quote(report["source"]),
             "  scope: " + yaml_quote(report["scope"])]
    amount = _decimal_cost(report.get("usd"))
    lines.append("  usd: " + (format(amount, "f") if amount is not None else "null"))
    if report.get("incomplete"):
        lines.append("  incomplete: true")
    for key in ("available_records", "total_records"):
        if key in report:
            lines.append("  " + key + ": " + str(report[key]))
    if report.get("observed_at"):
        lines.append("  observed_at: " + yaml_quote(report["observed_at"]))
    return lines


def recover_reported_cost(content: str) -> dict | None:
    """state喪失・再構成時にノートの最後のCLI報告値を復旧する。"""
    fm = re.match(r"\A---\s*\n(.*?)\n---", content, re.S)
    block = re.search(r"(?m)^reported_cost:\n((?:  .*\n)*)", fm.group(1) + "\n") if fm else None
    if not block:
        return None
    fields = {}
    for key, raw in re.findall(r"(?m)^  ([a-z_]+): (.*)$", block.group(1)):
        try:
            fields[key] = json.loads(raw, parse_float=Decimal)
        except ValueError:
            fields[key] = raw
    if not isinstance(fields.get("source"), str) or fields.get("scope") not in {"session", "message_sum"}:
        return None
    amount = _decimal_cost(fields.get("usd"))
    if amount is not None:
        fields["usd"] = str(amount)
    else:
        fields.pop("usd", None)
    return fields


def reported_cost_label(report: dict) -> str:
    amount = _format_usd(report.get("usd"))
    if amount is None:
        return "未取得"
    return amount + ("（部分取得）" if report.get("incomplete") else "")


def build_reported_cost_section(report: dict | None) -> str:
    if not report:
        return ""
    scope = "セッション累計" if report.get("scope") == "session" else "回答の合計"
    return "💵 **取得コスト (CLI報告値)**: " + reported_cost_label(report) + "（" + scope + "）\n"


def summarize_usage(messages, *, estimate_missing: bool = True) -> dict | None:
    """provider側で重複排除・正規化したrecordを、各recordの金額を維持して合計する。"""
    total = _empty_usage_totals()
    by_model = {}
    count = priced = 0
    total_cost = Decimal(0)
    long_context = False
    reports = []
    model_reports = {}
    for message in messages:
        usage = message.get("usage") if message.get("type") == "assistant" else None
        if not isinstance(usage, dict):
            continue
        count += 1
        model = str(message.get("model") or "unknown")
        entry = by_model.setdefault(model, _empty_usage_totals())
        if usage.get("reported_cost_source"):
            amount = _decimal_cost(usage.get("reported_cost_usd"))
            report = {"source": usage["reported_cost_source"], "scope": "message_sum",
                      "available_records": int(amount is not None), "total_records": 1}
            if amount is not None:
                report["usd"] = amount
            reports.append(report)
            model_reports.setdefault(model, []).append(report)
        for field in USAGE_FIELDS:
            value = int(usage.get(field, 0))
            total[field] += value
            entry[field] += value
        long_context = long_context or bool(usage.get("long_context"))
        value = _decimal_cost(usage.get("cost_usd"))
        if value is None and estimate_missing:
            value = _usage_cost(model, usage)
        if value is not None:
            priced += 1
            total_cost += value
            entry["cost_usd"] = entry.get("cost_usd", Decimal(0)) + value
        else:
            entry["cost_unavailable"] = True
    if not count:
        return None
    for entry in by_model.values():
        if "cost_usd" in entry and entry.pop("cost_unavailable", False):
            entry["cost_incomplete"] = True
    total["by_model"] = by_model
    if reports:
        total["reported_cost"] = sum_reported_costs(reports)
        for model, items in model_reports.items():
            by_model[model]["reported_cost"] = sum_reported_costs(items)
    if priced:
        total["total_cost_usd"] = total_cost
        if priced < count:
            total["cost_incomplete"] = True
    else:
        total["cost_unavailable"] = True
    if long_context:
        total["long_context"] = True
    return total


def usage_frontmatter_fields(usage: dict | None) -> list[str]:
    if not usage:
        return []
    lines = ["usage:", "  cost_basis: \"reference\"", *usage_price_fields(usage)]
    lines.extend(f"  {field}: {int(usage.get(field, 0))}" for field in USAGE_FIELDS)
    if usage.get("tokens_incomplete"):
        lines.append("  tokens_incomplete: true")
    if "total_cost_usd" in usage:
        lines.append(f"  total_cost_usd: {format(Decimal(str(usage['total_cost_usd'])), 'f')}")
        if usage.get("cost_incomplete"):
            lines.append("  cost_incomplete: true")
    else:
        lines.append("  cost_unavailable: true")
    scope = usage.get("scope")
    if scope:
        lines.append("  scope:")
        for key in ("parent_tokens", "subagent_tokens", "unattributed_subagent_tokens"):
            lines.append(f"    {key}: {int(scope.get(key, 0))}")
    lines.append("  by_model:")
    for model, entry in usage.get("by_model", {}).items():
        lines.append(f"    {yaml_quote(model)}:")
        lines.extend(f"      {field}: {int(entry.get(field, 0))}" for field in USAGE_FIELDS)
        if "cost_usd" in entry:
            lines.append(f"      cost_usd: {format(Decimal(str(entry['cost_usd'])), 'f')}")
            if entry.get("cost_incomplete") or entry.get("cost_unavailable"):
                lines.append("      cost_incomplete: true")
        else:
            lines.append("      cost_unavailable: true")
        report = entry.get("reported_cost")
        if report:
            amount = _decimal_cost(report.get("usd"))
            lines.append("      reported_cost_usd: " + (format(amount, "f") if amount is not None else "null"))
            if report.get("incomplete"):
                lines.append("      reported_cost_incomplete: true")
    lines.extend(reported_cost_fields(usage.get("reported_cost")))
    return lines


def build_usage_frontmatter_fields(usage: dict | None) -> list[str]:
    """増分saverの従来API。"""
    return usage_frontmatter_fields(usage)


def _format_usd(value: object) -> str | None:
    amount = _decimal_cost(value)
    return f"${format(amount, 'f')}" if amount is not None else None


def _usage_cost_label(usage: dict, key: str) -> str:
    amount = _format_usd(usage.get(key))
    if amount is None:
        return "換算不可"
    partial = usage.get("cost_incomplete") or usage.get("cost_unavailable")
    return amount + ("（部分換算）" if partial else "")


def cache_hit_label(usage: dict) -> str:
    """入力tokenに占めるcache readの割合。書込・出力は分子に含めない。"""
    total = usage.get("input_tokens")
    cached = usage.get("cached_input_tokens")
    if (isinstance(total, bool) or isinstance(cached, bool)
            or not isinstance(total, int) or not isinstance(cached, int)
            or total <= 0 or cached < 0 or cached > total):
        return "Cache: N/A"
    return f"Cache: {Decimal(cached) * 100 / Decimal(total):.1f}%"


def build_usage_section(usage: dict | None) -> str:
    if not usage:
        return "💰 **Usage (USD換算)**: N/A\n"
    parts = [f"{model} {_usage_cost_label(entry, 'cost_usd')} · {cache_hit_label(entry)}" for model, entry in usage.get("by_model", {}).items()]
    details = f" ({'、'.join(parts)})" if parts else ""
    scope = usage.get("scope")
    scope_line = ""
    if scope:
        scope_line = ("📊 **Usage集計範囲**: 親 " + f"{int(scope.get('parent_tokens', 0)):,} tok"
            + "・子 " + f"{int(scope.get('subagent_tokens', 0)):,} tok"
            + "（回答に紐付かない子 " + f"{int(scope.get('unattributed_subagent_tokens', 0)):,} tok）\n")
    coverage = " · token部分取得" if usage.get("tokens_incomplete") else ""
    return (f"💰 **Usage (USD換算)**: Total: {_usage_cost_label(usage, 'total_cost_usd')}{details} · {cache_hit_label(usage)}{coverage}\n"
            + scope_line + build_reported_cost_section(usage.get("reported_cost")))


def format_message_usage(usage: dict | None, subagent_usage: dict | None = None) -> str:
    if not usage:
        return ""
    warning = " ⚠︎ 272K+" if usage.get("long_context") else ""
    coverage = "（部分取得）" if usage.get("tokens_incomplete") else ""
    label = f"Usage: {int(usage.get('total_tokens', 0)):,} tok{coverage} · {_usage_cost_label(usage, 'total_cost_usd')}{warning}"
    if usage.get("reported_cost"):
        label += " · 取得コスト: " + reported_cost_label(usage["reported_cost"])
    if subagent_usage:
        parts = [f"{model} {int(entry.get('total_tokens', 0)):,} tok・{_usage_cost_label(entry, 'cost_usd')}"
                 for model, entry in sorted(subagent_usage.get("by_model", {}).items())]
        if parts:
            label += f" ({'、'.join(parts)})"
    return label + " · " + cache_hit_label(usage)


def _merge_usage_summaries(first: dict | None, second: dict | None) -> dict | None:
    """換算済み小計を足す。再換算せず長文条件をrecord単位に維持する。"""
    if not first:
        return second
    if not second:
        return first
    merged = _empty_usage_totals()
    for field in USAGE_FIELDS:
        merged[field] = int(first.get(field, 0)) + int(second.get(field, 0))
    by_model = {}
    for source in (first, second):
        for model, entry in source.get("by_model", {}).items():
            target = by_model.setdefault(model, _empty_usage_totals())
            for field in USAGE_FIELDS:
                target[field] += int(entry.get(field, 0))
            if "cost_usd" in entry:
                target["cost_usd"] = target.get("cost_usd", Decimal(0)) + Decimal(str(entry["cost_usd"]))
            if entry.get("cost_unavailable") or entry.get("cost_incomplete"):
                target["cost_unavailable"] = True
    for entry in by_model.values():
        if "cost_usd" in entry and entry.pop("cost_unavailable", False):
            entry["cost_incomplete"] = True
    merged["by_model"] = by_model
    reported = sum_reported_costs(source.get("reported_cost") for source in (first, second))
    if reported:
        merged["reported_cost"] = reported
    for model, entry in by_model.items():
        reported = sum_reported_costs(source.get("by_model", {}).get(model, {}).get("reported_cost")
                                     for source in (first, second))
        if reported:
            entry["reported_cost"] = reported
    if "total_cost_usd" in first or "total_cost_usd" in second:
        merged["total_cost_usd"] = sum((Decimal(str(source.get("total_cost_usd", 0))) for source in (first, second)), Decimal(0))
        if any(source.get("cost_unavailable") or source.get("cost_incomplete") for source in (first, second)):
            merged["cost_incomplete"] = True
    else:
        merged["cost_unavailable"] = True
    if first.get("long_context") or second.get("long_context"):
        merged["long_context"] = True
    if first.get("tokens_incomplete") or second.get("tokens_incomplete"):
        merged["tokens_incomplete"] = True
    return merged
# END GENERATED: usage-summary

# BEGIN GENERATED: usage-reference-cost
LONG_CONTEXT_INPUT_TOKENS = 272_000


def reference_price_model(model: object) -> str:
    name = _normalize_usage_model(model)
    if (name not in active_usage_rates() and name.endswith("-free")
            and name[:-len("-free")] in active_usage_rates()):
        name = name[:-len("-free")]
    return name


def is_long_context_usage(model: object, usage: dict) -> bool:
    """設定が有効な場合、登録済みGPTのリクエスト単位で長文料金を判定する。"""
    name = reference_price_model(model)
    tokens = usage.get("input_tokens", 0)
    return (APPLY_LONG_CONTEXT_PRICING
            and isinstance(tokens, int) and not isinstance(tokens, bool)
            and name.startswith("gpt-") and name in active_usage_rates()
            and tokens > LONG_CONTEXT_INPUT_TOKENS)


def haiku_prompt_price_multiplier(model: object, usage: dict) -> int:
    """通常入力・cache read/writeの合計が10万tokenを超えた場合は5倍。"""
    tokens = usage.get("input_tokens", 0)
    return 5 if (reference_price_model(model) == "claude-haiku-5-5"
                 and isinstance(tokens, int) and not isinstance(tokens, bool)
                 and tokens > 100_000) else 1


def _usage_cost(model: object, usage: dict) -> Decimal | None:
    """raw recordを正規化したtoken内訳から標準API参考換算を行う。"""
    rates = active_usage_rates().get(reference_price_model(model))
    if rates is None or usage.get("pricing_unknown"):
        return None
    long_context = is_long_context_usage(model, usage)
    haiku_multiplier = haiku_prompt_price_multiplier(model, usage)
    return calculate_usage_cost(
        rates, usage, input_multiplier=2 if long_context else haiku_multiplier,
        output_multiplier=Decimal("1.5") if long_context else haiku_multiplier,
    )
# END GENERATED: usage-reference-cost


# BEGIN GENERATED: output-directory
def resolve_output_dir(value: str | None = None) -> Path:
    """保管庫からの相対保存先を検証し、不正なら既定値を返す。"""
    raw = os.environ.get("OBSIDIAN_OUTPUT_DIR", DEFAULT_OUTPUT_DIR) if value is None else value
    candidate = Path(raw) if str(raw).strip() else Path(DEFAULT_OUTPUT_DIR)
    if candidate.is_absolute() or ".." in candidate.parts:
        return Path(DEFAULT_OUTPUT_DIR)
    return candidate


SOURCE_NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")


def resolve_source_name(default: str, value: str | None = None) -> str:
    """保存先とfrontmatterに使う名前を検証し、不正なら既定値を返す。"""
    candidate = os.environ.get("OBSIDIAN_SOURCE", default) if value is None else value
    if candidate not in {".", ".."} and SOURCE_NAME_PATTERN.fullmatch(candidate):
        return candidate
    return default
# END GENERATED: output-directory


OBSIDIAN_OUTPUT_DIR = resolve_output_dir()
SOURCE_NAME = resolve_source_name(DEFAULT_SOURCE)
OUTPUT_BASE = OBSIDIAN_VAULT / OBSIDIAN_OUTPUT_DIR / SOURCE_NAME

STATE_DIR = Path.home() / ".gemini" / "agy-obsidian" / "state"
LOG_FILE = Path.home() / ".gemini" / "agy-obsidian" / "log" / "obsidian_save.log"

STATE_RETENTION_DAYS = 30
LAST_ID_PATTERN = re.compile(r"<!--\s*last_id:\s*(\S+)\s*-->")
AGENT_NAME = "Antigravity"
SAFE_SESSION_CHARS = re.compile(r"[^A-Za-z0-9_.-]+")

# ============================================================
# ロギング
# ============================================================
# BEGIN GENERATED: utilities
def now_jst() -> datetime:
    return datetime.now(JST)


def yaml_quote(value: object) -> str:
    """JSON互換の引用形式でYAML文字列を安全に生成する。"""
    return json.dumps(str(value), ensure_ascii=False)


def safe_filename_component(value: str) -> str:
    """値を単一の安全なファイル名要素へ変換する。"""
    return re.sub(r"[\x00-\x1f/:*?\[\]\\]", "_", value).strip()[:80]


def _try_parse_datetime(value: object) -> datetime | None:
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value) / 1000, tz=timezone.utc).astimezone(JST)
        except (OSError, OverflowError, ValueError):
            return None
    if isinstance(value, str) and value:
        try:
            normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
            parsed = datetime.fromisoformat(normalized)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(JST)
        except (OSError, OverflowError, TypeError, ValueError):
            pass
    return None


def parse_datetime(value: object, default: datetime | None = None) -> datetime:
    parsed = _try_parse_datetime(value)
    if parsed is not None:
        return parsed
    return default or datetime.now(JST)


def format_iso(value: datetime) -> str:
    return value.astimezone(JST).isoformat(timespec="seconds")


def _finite_number(value: object) -> float | None:
    """APIレスポンスの数値を厳密に検証する。boolやNaN/Infinityは受け付けない。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (OverflowError, ValueError, TypeError):
        return None
    return number if math.isfinite(number) else None


def format_model_name(model_name: str, effort: str = "") -> str:
    """モデル名に reasoning effort を付加して表示用の名前へ変換する。"""
    model = str(model_name or "").strip()
    effort_value = str(effort or "").strip()
    if not model or not effort_value:
        return model
    if model.endswith(f"-{effort_value}"):
        return model
    return f"{model}-{effort_value}"
# END GENERATED: utilities

# BEGIN GENERATED: logging-setup
def setup_logger() -> logging.Logger:
    logger = logging.getLogger(LOGGER_NAME)
    if logger.handlers:
        return logger
    logger.setLevel(logging.DEBUG if os.environ.get("DEBUG") else logging.INFO)
    formatter = logging.Formatter("[%(asctime)s] %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    file_log_error = None
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            LOG_FILE,
            maxBytes=5 * 1024 * 1024,  # 5MB
            backupCount=3,
            encoding="utf-8",
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    except OSError as e:
        file_log_error = e
    stderr = logging.StreamHandler()
    stderr.setFormatter(formatter)
    logger.addHandler(stderr)
    if file_log_error is not None:
        logger.warning("ファイルログを初期化できないためstderrのみ使用します: %s", file_log_error)
    return logger


logger = setup_logger()
# END GENERATED: logging-setup


AGY_QUOTA_TIMEOUT_SECONDS = 20


# ============================================================
# セッション単位のstateファイル
# ============================================================
# BEGIN GENERATED: session-storage
def safe_session_key(session_id: str) -> str:
    """session_idをパストラバーサルできないファイル名へ変換する。"""
    raw = str(session_id)
    safe = SAFE_SESSION_CHARS.sub("_", raw).strip("._")[:80]
    if safe and safe == raw:
        return safe
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
    return f"{safe or 'session'}-{digest}"


from contextvars import ContextVar

_SAVE_SKIP_REASON = ContextVar("obsidian_save_skip_reason", default=None)


def mark_save_skipped(reason: str) -> None:
    """入力本文やパスを記録せず、保存を見送った理由だけを残す。"""
    if _SAVE_SKIP_REASON.get() is None:
        _SAVE_SKIP_REASON.set(reason)


def diagnostic_path(session_id: str) -> Path:
    return STATE_DIR / ".diagnostics" / (safe_session_key(session_id) + ".status")


def record_save_observation(session_id: str, details: dict) -> None:
    """本文・例外メッセージを含めず、診断情報を保存する。診断失敗は保存を妨げない。"""
    try:
        path = diagnostic_path(session_id)
        try:
            previous = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            previous = {}
        if not isinstance(previous, dict):
            previous = {}
        if details.get("last_outcome") != "skipped":
            previous.pop("skip_reason", None)
        previous.update(details, session_id=str(session_id), saver_version=__version__)
        atomic_write_text(path, json.dumps(previous, ensure_ascii=False, indent=2))
    except (OSError, ValueError):
        logger.warning("診断情報を保存できませんでした")


@contextmanager
def observe_save(session_id: str, payload: dict):
    """hook/bridgeの完了・例外を記録する。通常の終了コード契約は呼出元へ任せる。"""
    details = {"last_attempt_at": format_iso(now_jst()), "last_outcome": "skipped"}
    transcript = payload.get("transcript_path") or payload.get("transcriptPath")
    if isinstance(transcript, str) and transcript:
        details["transcript_path"] = transcript
    state_file = state_path(session_id)
    try:
        before = state_file.stat().st_mtime_ns
    except OSError:
        before = None
    class ErrorObserver(logging.Handler):
        def emit(self, record):
            details.update(last_outcome="failed", last_failure_at=format_iso(now_jst()),
                           last_failure_type="logged_error")
    observer = ErrorObserver(level=logging.ERROR)
    logger.addHandler(observer)
    skip_token = _SAVE_SKIP_REASON.set(None)
    try:
        yield
    except Exception as error:
        details.update(last_outcome="failed", last_failure_at=format_iso(now_jst()),
                       last_failure_type=type(error).__name__)
        raise
    else:
        state = load_state(session_id)
        output = state.get("output_path")
        try:
            after = state_file.stat().st_mtime_ns
        except OSError:
            after = None
        if details["last_outcome"] == "skipped" and _SAVE_SKIP_REASON.get() is not None:
            details["skip_reason"] = _SAVE_SKIP_REASON.get()
        elif (details["last_outcome"] != "failed" and isinstance(output, str)
                and Path(output).is_file() and after != before):
            details.update(last_outcome="saved", last_success_at=format_iso(now_jst()),
                           output_path=str(output))
        elif details["last_outcome"] == "skipped":
            details["skip_reason"] = _SAVE_SKIP_REASON.get() or "unchanged"
    finally:
        _SAVE_SKIP_REASON.reset(skip_token)
        logger.removeHandler(observer)
        record_save_observation(session_id, details)


def state_path(session_id: str) -> Path:
    return STATE_DIR / f"{safe_session_key(session_id)}.json"


def atomic_write_text(path: Path, content: str) -> None:
    """同じディレクトリの一時ファイルをrenameして文字列を保存する。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as tmp:
            tmp_path = Path(tmp.name)
            tmp.write(content)
            tmp.flush()
            os.fsync(tmp.fileno())
        if path.exists():
            os.chmod(tmp_path, path.stat().st_mode & 0o7777)
        os.replace(tmp_path, path)
        tmp_path = None
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)


class ExistingMarkdownSearchError(Exception):
    """既存Markdown探索中のI/Oエラー。誤上書き・重複作成を防ぐために使用。"""


@contextmanager
def session_lock(session_id: str):
    """同一セッションのhook実行を直列化する。"""
    lock_dir = STATE_DIR / ".locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / f"{safe_session_key(session_id)}.lock"
    with open(lock_path, "a", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


@contextmanager
def try_session_lock(session_id: str, *, is_key: bool = False):
    """非ブロッキングでセッションロックを試み、取得結果をyieldする。"""
    lock_dir = STATE_DIR / ".locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / f"{session_id if is_key else safe_session_key(session_id)}.lock"
    try:
        lock_file = open(lock_path, "a", encoding="utf-8")
    except OSError:
        yield False
        return
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        lock_file.close()
        yield False
        return
    try:
        yield True
    finally:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        lock_file.close()


def load_state(session_id: str) -> dict:
    path = state_path(session_id)
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("stateはJSONオブジェクトが必要です")
            return data
        except Exception as error:
            logger.info("stateファイルの読み込みに失敗しました（破損の可能性）: %s: %s", path, error)
            return {}
    return {}


def save_state(session_id: str, state: dict) -> None:
    """stateをatomicに保存する。"""
    state["last_used_at"] = format_iso(now_jst())
    atomic_write_text(state_path(session_id), json.dumps(state, ensure_ascii=False, indent=2))


def cleanup_old_states() -> None:
    """保持期間を過ぎたstateを、実行中セッションを保護しながら削除する。"""
    if not STATE_DIR.exists():
        return
    cutoff = datetime.now(JST) - timedelta(days=STATE_RETENTION_DAYS)
    removed = 0
    try:
        candidates = list(STATE_DIR.glob("*.json"))
    except OSError as error:
        logger.warning("state探索に失敗したためクリーンアップを見送ります: %s", error)
        return
    for path in candidates:
        with try_session_lock(path.stem, is_key=True) as acquired:
            if not acquired:
                continue
            try:
                if not path.exists():
                    continue
                data = json.loads(path.read_text(encoding="utf-8"))
                last_used = None
                if data.get("last_used_at"):
                    try:
                        last_used = datetime.fromisoformat(str(data["last_used_at"]))
                        if last_used.tzinfo is None:
                            last_used = last_used.replace(tzinfo=JST)
                    except ValueError:
                        pass
                if last_used is None:
                    last_used = datetime.fromtimestamp(path.stat().st_mtime, tz=JST)
                if last_used < cutoff:
                    path.unlink()
                    diagnostic = STATE_DIR / ".diagnostics" / (path.stem + ".status")
                    diagnostic.unlink(missing_ok=True)
                    removed += 1
            except Exception as error:
                logger.info("stateファイルのクリーンアップ中にスキップ: %s: %s", path.name, error)
    if removed > 0:
        logger.info("古いstateファイル %d 件を削除しました", removed)
# END GENERATED: session-storage


# ============================================================
# ユーティリティ
# ============================================================
# BEGIN GENERATED: incremental-utilities
def session_locked(input_key: str, *, observe: bool = True):
    """hook input内のsession idを使って関数を排他実行する。"""
    def decorator(func):
        @wraps(func)
        def wrapper(hook_input: dict):
            session_id = hook_input.get(input_key, "")
            if not session_id:
                return func(hook_input)
            with session_lock(str(session_id)):
                if not observe:
                    return func(hook_input)
                with observe_save(str(session_id), hook_input):
                    return func(hook_input)
        return wrapper
    return decorator


def read_stdin() -> dict:
    """hookの標準入力を読み、空入力や不正JSONでは空のdictを返す。"""
    try:
        data = sys.stdin.read()
        if data.strip():
            parsed = json.loads(data)
            return parsed if isinstance(parsed, dict) else {}
    except Exception as error:
        logger.info("stdinの読み込みまたはJSONパースに失敗しました: %s", error)
    return {}


def merge_first_quota(first: dict | None, observed: dict) -> dict:
    """各枠について、最初に取得できた観測値を保持する。"""
    merged = {group: dict(windows) for group, windows in (first or {}).items()}
    for group, windows in observed.items():
        target = merged.setdefault(group, {})
        for window, value in windows.items():
            target.setdefault(window, value)
    return merged


def build_quota_frontmatter_fields(first: dict | None, last: dict | None) -> list[str]:
    """quotaは消費量に換算せず、枠ごとの最初と最後の観測値を保存する。"""
    if not first and not last:
        return []
    lines = ["quota:"]
    for group in sorted(set(first or {}) | set(last or {})):
        lines.append(f"  {yaml_quote(group)}:")
        first_windows = (first or {}).get(group, {})
        last_windows = (last or {}).get(group, {})
        for window in sorted(set(first_windows) | set(last_windows)):
            lines.append(f"    {yaml_quote(window)}:")
            for label, value in (("first_observed", first_windows.get(window)),
                                 ("last_observed", last_windows.get(window))):
                if not isinstance(value, dict) or value.get("remaining") is None:
                    continue
                lines.append(f"      {label}:")
                lines.append(f"        remaining_percent: {value['remaining']}")
                if value.get("observed_at"):
                    lines.append(f"        observed_at: {yaml_quote(value['observed_at'])}")
                if value.get("reset"):
                    lines.append(f"        reset_at_jst: {yaml_quote(value['reset'])}")
    return lines


def recover_quota_frontmatter(content: str) -> tuple[dict, dict]:
    """このスクリプトが出力した階層化quotaだけをstateへ復旧する。"""
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n", content, re.DOTALL)
    if not match:
        return {}, {}
    first, last = {}, {}
    in_quota = False
    group = window = observation = ""
    for line in match.group(1).splitlines():
        if line == "quota:":
            in_quota = True
            continue
        if not in_quota:
            continue
        if line and not line.startswith(" "):
            break
        indent = len(line) - len(line.lstrip(" "))
        key = line.strip()
        if indent == 2 and key.endswith(":"):
            try:
                group = json.loads(key[:-1])
            except (TypeError, ValueError):
                return {}, {}
        elif indent == 4 and key.endswith(":"):
            try:
                window = json.loads(key[:-1])
            except (TypeError, ValueError):
                return {}, {}
        elif indent == 6 and key in ("first_observed:", "last_observed:"):
            observation = key[:-1]
        elif indent == 8 and group and window and observation and ": " in key:
            field, raw = key.split(": ", 1)
            target = first if observation == "first_observed" else last
            item = target.setdefault(group, {}).setdefault(window, {})
            if field == "remaining_percent":
                try:
                    value = float(raw)
                except ValueError:
                    return {}, {}
                if not 0 <= value <= 100:
                    return {}, {}
                item["remaining"] = value
            elif field in ("observed_at", "reset_at_jst"):
                try:
                    value = json.loads(raw)
                except ValueError:
                    return {}, {}
                item["observed_at" if field == "observed_at" else "reset"] = value
    return first, last
# END GENERATED: incremental-utilities


# BEGIN GENERATED: markdown-formatting
def sanitize_markdown(text: str) -> str:
    """会話本文のMarkdown記述を維持し、空値を空文字列へそろえる。"""
    return text if text else ""


def _file_uri_path(path: str) -> str:
    """Markdownリンクのローカル絶対パスをfile URIのパスへ変換する。"""
    from urllib.parse import quote

    path_part, separator, fragment = path.partition("#")
    encoded = quote(path_part, safe="/%:@!$&'()*+,;=-._~")
    if separator:
        encoded += "#" + quote(fragment, safe="/%:@!$&'()*+,;=-._~")
    return "file://" + encoded


def _normalize_link_destination(destination: str) -> str:
    """ローカル絶対パスだけを外部ファイルURIへ変換する。"""
    if not destination.startswith("/") or destination.startswith("//"):
        return destination
    return _file_uri_path(destination)


def _find_link_end(text: str, start: int) -> int | None:
    """リンク先の閉じ括弧位置を返す（エスケープと入れ子の括弧に対応）。"""
    depth = 0
    escaped = False
    for index in range(start, len(text)):
        character = text[index]
        if escaped:
            escaped = False
        elif character == "\\":
            escaped = True
        elif character == "(":
            depth += 1
        elif character == ")":
            if depth == 0:
                return index
            depth -= 1
    return None


def _strip_markdown_blockquote_prefix(line: str) -> str:
    """Markdown行からblockquoteの入れ子接頭辞を取り除く。"""
    candidate = line.rstrip("\r\n")
    while True:
        match = re.match(r"^ {0,3}>[ \t]?", candidate)
        if not match:
            return candidate
        candidate = candidate[match.end():]


def _fenced_code_ranges(text: str) -> list[tuple[int, int]]:
    """Markdown本文中のコードフェンス範囲を、元の文字列位置で返す。"""
    ranges = []
    active = None
    offset = 0
    for line in text.splitlines(keepends=True):
        content = _strip_markdown_blockquote_prefix(line)
        fence = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", content)
        if active is None:
            if fence:
                active = (offset, fence.group(1)[0], len(fence.group(1)))
        elif (
            fence
            and fence.group(1)[0] == active[1]
            and len(fence.group(1)) >= active[2]
            and not fence.group(2).strip()
        ):
            ranges.append((active[0], offset + len(line)))
            active = None
        offset += len(line)
    if active is not None:
        ranges.append((active[0], len(text)))
    return ranges


def _iter_inline_markdown_segments(text: str):
    """通常Markdown部分をtext/code_spanの順で走査する。"""
    index = 0
    text_start = 0
    while index < len(text):
        if text[index] != "`":
            index += 1
            continue
        run_end = index
        while run_end < len(text) and text[run_end] == "`":
            run_end += 1
        marker = text[index:run_end]
        closing = text.find(marker, run_end)
        if closing == -1:
            index = run_end
            continue
        if text_start < index:
            yield "text", text[text_start:index]
        yield "code_span", text[index:closing + len(marker)]
        index = closing + len(marker)
        text_start = index
    if text_start < len(text):
        yield "text", text[text_start:]


def iter_markdown_segments(text: str):
    """回答本文をtext、code_span、fenced_codeの領域へ分類して返す。

    各領域の値は元の文字列をそのまま保持する。コード領域を除外する
    変換と、callout用に行頭へ接頭辞だけを追加する変換が同じ分類を
    利用できるようにする。
    """
    text = str(text)
    cursor = 0
    for start, end in _fenced_code_ranges(text):
        if cursor < start:
            yield from _iter_inline_markdown_segments(text[cursor:start])
        yield "fenced_code", text[start:end]
        cursor = end
    if cursor < len(text):
        yield from _iter_inline_markdown_segments(text[cursor:])


def _normalize_markdown_links_in_line(line: str) -> str:
    """コードスパンを保ったまま、Markdownリンクを正規化する。"""
    result = []
    index = 0
    while index < len(line):
        if line[index] != "[":
            result.append(line[index])
            index += 1
            continue

        label_end = index + 1
        depth = 1
        escaped = False
        while label_end < len(line):
            character = line[label_end]
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == "[":
                depth += 1
            elif character == "]":
                depth -= 1
                if depth == 0:
                    break
            label_end += 1
        if label_end >= len(line):
            result.append(line[index])
            index += 1
            continue

        open_paren = label_end + 1
        while open_paren < len(line) and line[open_paren] in " \t":
            open_paren += 1
        if open_paren >= len(line) or line[open_paren] != "(":
            result.append(line[index:label_end + 1])
            index = label_end + 1
            continue

        close_paren = _find_link_end(line, open_paren + 1)
        if close_paren is None:
            result.append(line[index])
            index += 1
            continue

        raw_destination = line[open_paren + 1:close_paren]
        destination = raw_destination
        prefix = ""
        suffix = ""
        if raw_destination.startswith("<"):
            destination_end = raw_destination.find(">", 1)
            if destination_end != -1:
                destination = raw_destination[1:destination_end]
                prefix = "<"
                suffix = ">" + raw_destination[destination_end + 1:]
        elif "\n" not in raw_destination:
            # タイトル付きリンクはタイトルを残し、パス部分だけを変換する。
            title_match = re.match(r"^(\S+)(\s+(?:\"[^\"]*\"|'[^']*'|\([^)]*\)))$", raw_destination)
            if title_match:
                destination, suffix = title_match.groups()

        normalized = _normalize_link_destination(destination)
        if normalized == destination:
            result.append(line[index:close_paren + 1])
        else:
            result.append(
                line[index:open_paren + 1]
                + prefix
                + normalized
                + suffix
                + ")"
            )
        index = close_paren + 1
    return "".join(result)


def normalize_local_file_links(text: str) -> str:
    """回答本文のローカル絶対パスリンクをfile URIへ変換する。"""
    result = []
    for kind, segment in iter_markdown_segments(text):
        if kind == "text":
            result.append(_normalize_markdown_links_in_line(segment))
        else:
            result.append(segment)
    return "".join(result)


def escape_markdown_heading(text: str) -> str:
    """見出し内でMarkdown構文として解釈される文字をエスケープする。"""
    return re.sub(r"([\\`*_{}\[\]()#+\-.!|<>~])", r"\\\1", text)


def unescape_markdown_heading(text: str) -> str:
    """見出し用に追加したMarkdownエスケープを取り除く。"""
    escaped_chars = r"\\`*_{}[]()#+-.!|<>~"
    result = []
    index = 0
    while index < len(text):
        if text[index] == "\\" and index + 1 < len(text) and text[index + 1] in escaped_chars:
            result.append(text[index + 1])
            index += 2
        else:
            result.append(text[index])
            index += 1
    return "".join(result)


def callout_lines(text: str) -> str:
    """本文をObsidian callout内の行へ変換する。"""
    segments = list(iter_markdown_segments(text))
    lines = [[]]
    for kind, segment in segments:
        for part in segment.split("\n")[:-1]:
            lines[-1].append((kind, part))
            lines.append([])
        if segment:
            lines[-1].append((kind, segment.split("\n")[-1]))

    output = []
    for line_parts in lines:
        transformed = []
        first_non_space = True
        for kind, part in line_parts:
            if kind == "text":
                value = part.replace("<", "&lt;")
                if first_non_space:
                    leading = len(value) - len(value.lstrip())
                    if value[leading:leading + 1] == ">":
                        value = value[:leading] + "&gt;" + value[leading + 1:]
                if value.strip():
                    first_non_space = False
                transformed.append(value)
            else:
                if first_non_space and part.lstrip():
                    first_non_space = False
                transformed.append(part)
        line = "".join(transformed)
        output.append(f"> {line}" if line else ">")
    return "\n".join(output)
# END GENERATED: markdown-formatting


# BEGIN GENERATED: incremental-callout
MARKDOWN_FENCE_PATTERN = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")


def _strip_blockquote_prefix(line: str) -> str:
    """Markdownの通常行またはblockquote内行からblockquote記号を除く。"""
    candidate = line.rstrip("\r\n")
    while True:
        match = re.match(r"^ {0,3}>[ \t]?", candidate)
        if not match:
            return candidate
        candidate = candidate[match.end():]


def _fence_marker(line: str) -> tuple[str, int, str] | None:
    """行のMarkdownコードフェンスを、blockquote内も含めて返す。"""
    match = MARKDOWN_FENCE_PATTERN.match(_strip_blockquote_prefix(line))
    if not match:
        return None
    fence = match.group(1)
    return fence[0], len(fence), match.group(2)


def _iter_lines_outside_fences(text: str):
    """Markdownコードフェンス内を除いた行を順に返す。"""
    active_fence = None
    for line in text.splitlines():
        marker = _fence_marker(line)
        if active_fence is not None:
            if (
                marker is not None
                and marker[0] == active_fence[0]
                and marker[1] >= active_fence[1]
                and not marker[2].strip()
            ):
                active_fence = None
            continue
        if marker is not None:
            active_fence = marker[:2]
            continue
        yield line


def _assistant_header_spans(content: str) -> list[tuple[int, int]]:
    """コードフェンス外のassistant metadata行の範囲を返す。"""
    lines = content.splitlines(keepends=True)
    spans = []
    index = 0
    active_fence = None
    while index < len(lines):
        marker = _fence_marker(lines[index])
        if active_fence is not None:
            if (
                marker is not None
                and marker[0] == active_fence[0]
                and marker[1] >= active_fence[1]
                and not marker[2].strip()
            ):
                active_fence = None
            index += 1
            continue
        if marker is not None:
            active_fence = marker[:2]
            index += 1
            continue
        if lines[index].rstrip("\r\n") != f"> [!NOTE] {AGENT_NAME}":
            index += 1
            continue
        end = index + 1
        while end < len(lines) and lines[end].startswith("> <small>"):
            end += 1
        spans.append((index, end))
        index = end
    return spans


def refresh_answer_timing_metadata(content: str, messages: list[dict]) -> str:
    """終了記録が後から届いた場合も、依頼の最後のcalloutだけを更新する。"""
    timings = {}
    turn_index = 0
    last_turn_id = None
    for message in messages:
        turn_id = message.get("turn_id") or None
        if message.get("type") == "user_message":
            turn_index += 1
            last_turn_id = turn_id
            continue
        if message.get("type") != "agent_message":
            continue
        if message.get("after_turn_boundary") or (turn_id and last_turn_id and turn_id != last_turn_id):
            turn_index += 1
        last_turn_id = turn_id or last_turn_id
        if "answer_duration_seconds" in message:
            timings[turn_index] = message["answer_duration_seconds"]
    lines = content.splitlines(keepends=True)
    user_positions = []
    active_fence = None
    for index, line in enumerate(lines):
        marker = _fence_marker(line)
        if active_fence is not None:
            if marker and marker[0] == active_fence[0] and marker[1] >= active_fence[1] and not marker[2].strip():
                active_fence = None
            continue
        if marker:
            active_fence = marker[:2]
            continue
        if line.rstrip("\r\n") in ("> [!QUESTION] User", HIDDEN_USER_BOUNDARY):
            user_positions.append(index)
    spans_by_turn = {}
    for start, end in _assistant_header_spans(content):
        turn = sum(position < start for position in user_positions)
        spans_by_turn.setdefault(turn, []).append((start, end))
    replacements = []
    for turn, spans in spans_by_turn.items():
        if turn not in timings:
            continue
        for index, (start, end) in enumerate(spans):
            metadata = [line for line in lines[start:end]
                        if not line.startswith("> <small>⏱ 回答所要時間: ")]
            if index == len(spans) - 1:
                newline = "\r\n" if lines[start].endswith("\r\n") else "\n"
                metadata.append("> <small>" + format_answer_duration(timings[turn]) + "</small>" + newline)
            replacements.append((start, end, metadata))
    for start, end, metadata in reversed(replacements):
        lines[start:end] = metadata
    return "".join(lines)
# END GENERATED: incremental-callout


# BEGIN GENERATED: message-metadata
def annotate_answer_timings(messages: list[dict]) -> list[dict]:
    """依頼から最後の回答完了までを測る。元の入力は変更しない。"""
    result = [dict(message) for message in messages]
    started = None
    last_answer = None
    last_turn = None

    def finish():
        if last_answer is None:
            return
        value = result[last_answer].get("completed_at")
        ended = _try_parse_datetime(value) if not isinstance(value, bool) else None
        seconds = (ended - started).total_seconds() if started is not None and ended is not None else None
        result[last_answer]["answer_duration_seconds"] = seconds if seconds is not None and seconds >= 0 else None

    for index, message in enumerate(result):
        message.pop("answer_duration_seconds", None)
        role = message.get("type")
        turn = message.get("turn_id") or None
        if role in ("user", "user_message"):
            finish()
            value = message.get("timestamp")
            started = _try_parse_datetime(value) if not isinstance(value, bool) else None
            last_answer = None
            last_turn = turn
        elif role in ("assistant", "agent_message"):
            if message.get("after_turn_boundary") or (turn and last_turn and turn != last_turn):
                finish()
                started = None
                last_answer = None
            last_turn = turn or last_turn
            if str(message.get("text", message.get("message", ""))).strip():
                last_answer = index
    finish()
    return result


def format_answer_duration(seconds: object) -> str:
    """取得できない時間を推測せず、全saverで同じ表示を使う。"""
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not 0 <= seconds < float("inf"):
        return "⏱ 回答所要時間: 取得不可"
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    duration = (f"{hours}時間" if hours else "") + (f"{minutes}分" if minutes or hours else "") + f"{secs}秒"
    return f"⏱ 回答所要時間: {duration}"


def format_message_time(timestamp: object) -> str:
    """ISO日時またはUnix epoch millisecondsをJSTの共通メタデータ行へ変換する。"""
    if timestamp in (None, ""):
        return ""
    try:
        value = int(timestamp) if isinstance(timestamp, str) and timestamp.isdigit() else timestamp
        if isinstance(value, (int, float)):
            dt = datetime.fromtimestamp(float(value) / 1000, tz=timezone.utc)
        else:
            text = str(value)
            dt = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        return f"> <small>⏱ {dt.astimezone(JST):%Y-%m-%d %H:%M:%S}</small>\n>\n"
    except (OSError, OverflowError, TypeError, ValueError) as error:
        logger.warning("timestampのパース失敗: %s: %s", timestamp, error)
        return ""


def _heading_text(text: str) -> str:
    """ユーザー発言から見出し・タイトル共通の短縮済み文字列を返す。"""
    first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
    first_line = first_line.replace("\\\\", "\\")
    for escaped, plain in ((r"\[", "["), (r"\]", "]"), (r"\_", "_"), (r"\*", "*")):
        first_line = first_line.replace(escaped, plain)
    if len(first_line) > 40:
        first_line = first_line[:40] + "..."
    return first_line


def user_heading(text: str) -> str:
    """ユーザー発言の先頭非空行から安全な共通見出しを作る。"""
    first_line = _heading_text(text)
    escaped = escape_markdown_heading(first_line)
    return f"# {escaped}\n" if escaped else "# User\n"


def heading_title(text: str) -> str:
    """ユーザー発話から、frontmatter titleに使う見出し文字列を返す。"""
    return _heading_text(text) or "User"
# END GENERATED: message-metadata


# BEGIN GENERATED: markdown-rendering
def user_callout_header(timestamp: object) -> str:
    """連続発言の判定にも使うUser calloutヘッダーを生成する。"""
    return f"> [!QUESTION] User\n{format_message_time(timestamp)}".strip()


def render_user_message_block(text: str, timestamp: object, *, continued: bool = False) -> str:
    """User発言を、ファイルI/Oを行わずMarkdownブロックへ変換する。"""
    body = callout_lines(text)
    if continued:
        return f"---\n\n{body}\n\n"
    return f"{user_heading(text)}> [!QUESTION] User\n{format_message_time(timestamp)}{body}\n\n"


def assistant_callout_header(agent_name: str, metadata: list[str]) -> str:
    """連続回答の判定にも使うAssistant calloutヘッダーを生成する。"""
    lines = [f"> [!NOTE] {agent_name}"]
    lines.extend(f"> <small>{item}</small>" for item in metadata if item)
    return "\n".join(lines)


def render_assistant_message_block(
    text: str,
    agent_name: str,
    metadata: list[str],
    *,
    continued: bool = False,
    extra_blank_line: bool = False,
) -> str:
    """Assistant発言を、ファイルI/Oを行わずMarkdownブロックへ変換する。"""
    body = normalize_local_file_links(text)
    if continued:
        return body
    separator = "\n\n\n" if extra_blank_line else "\n\n"
    return f"{assistant_callout_header(agent_name, metadata)}{separator}{body}\n\n"
# END GENERATED: markdown-rendering

# BEGIN GENERATED: full-session-display
def _display_message_groups(messages: Iterable[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
    groups: List[List[Dict[str, Any]]] = []
    for message in messages:
        if (
            message["type"] == "assistant"
            and groups
            and groups[-1][0]["type"] == "assistant"
            and groups[-1][0].get("model", "") == message.get("model", "")
            and groups[-1][0].get("turn_index") == message.get("turn_index")
        ):
            groups[-1].append(message)
        else:
            groups.append([message])
    return groups


def _usage_records_by_display_group(
    groups: List[List[Dict[str, Any]]], usage_records: Iterable[Dict[str, Any]]
) -> List[List[Dict[str, Any]]]:
    """本文のないassistant usageを、同じユーザーターン内の最寄り回答calloutへ割り当てる。"""
    result: List[List[Dict[str, Any]]] = [[] for _ in groups]
    message_groups = {
        str(message.get("id")): group_index
        for group_index, group in enumerate(groups)
        for message in group
        if message.get("type") == "assistant" and message.get("id")
    }
    assistant_groups = [
        index for index, group in enumerate(groups)
        if group and group[0].get("type") == "assistant"
    ]
    for record in usage_records:
        if not isinstance(record, dict) or record.get("type") != "assistant":
            continue
        message_id = str(record.get("id") or "")
        target = message_groups.get(message_id)
        if target is None:
            sequence = record.get("sequence")
            turn_index = record.get("turn_index")
            candidates = [
                index for index in assistant_groups
                if turn_index is None or groups[index][0].get("turn_index") == turn_index
            ]
            model = str(record.get("model") or "")
            same_model = [index for index in candidates if str(groups[index][0].get("model") or "") == model]
            if same_model:
                candidates = same_model
            if not candidates:
                continue

            def distance(group_index: int) -> Tuple[int, int, int]:
                positions = [
                    message.get("sequence") for message in groups[group_index]
                    if isinstance(message.get("sequence"), int)
                ]
                if not isinstance(sequence, int) or not positions:
                    return (0, group_index, group_index)
                start, end = min(positions), max(positions)
                gap = start - sequence if sequence < start else sequence - end if sequence > end else 0
                follows_record = 0 if start >= sequence else 1
                return (gap, follows_record, group_index)

            target = min(candidates, key=distance)
        result[target].append(record)
    return result
# END GENERATED: full-session-display

# BEGIN GENERATED: incremental-rendering
HIDDEN_USER_BOUNDARY = "<!-- user_turn_boundary -->"


def incremental_display_messages(messages: list[dict], model_name: str = "", effort: str = "") -> list[dict]:
    """増分入力を共通の表示recordへ変換する。非表示のuserも境界として残す。"""
    result = []
    turn = -1
    for index, message in enumerate(messages):
        role = {"user_message": "user", "agent_message": "assistant"}.get(message.get("type"))
        if role is None:
            continue
        if role == "user":
            turn += 1
        result.append(dict(message, type=role, text=message.get("message", ""),
            model=format_model_name(message.get("model") or model_name, message.get("effort") or effort),
            turn_index=message.get("turn_id") or message.get("turn_index", turn), sequence=index))
    return result


def render_incremental_messages(content: str, messages: list[dict], *, model_name: str = "", effort: str = "",
                                user_transform=None, reference_renderer=None) -> tuple[str, int, bool]:
    normalized = incremental_display_messages(messages, model_name, effort)
    groups = _display_message_groups(normalized)
    last_header = get_last_callout_header(content)
    if (last_header is None and HIDDEN_USER_BOUNDARY not in content
            and not any(message["text"].strip() for message in normalized)):
        return content, 0, False
    blocks = []
    appended = 0
    previous_turn = None
    for group in groups:
        message = group[0]
        turn = message.get("turn_index")
        if message["type"] == "assistant" and (message.get("after_turn_boundary") or
                (previous_turn is not None and previous_turn != turn)):
            blocks.append(HIDDEN_USER_BOUNDARY + "\n\n")
            last_header = None
        previous_turn = turn
        text = message["text"]
        if message["type"] == "user":
            if not text.strip():
                blocks.append(HIDDEN_USER_BOUNDARY + "\n\n")
                last_header = None
                continue
            display_text, reference = user_transform(text) if user_transform else (text, None)
            header = user_callout_header(message.get("timestamp"))
            user_block = render_user_message_block(display_text, message.get("timestamp"), continued=header == last_header)
            if reference is not None and reference_renderer is not None:
                user_block += reference_renderer(reference) + "\n\n"
            blocks.append(user_block)
            last_header = REFERENCE_CALLOUT_HEADER if reference is not None else header
            appended += 1
            continue
        visible = [item for item in group if item["text"].strip()]
        if not visible:
            continue
        usage = subagent_usage = None
        for item in group:
            if item.get("usage"):
                usage = _merge_usage_summaries(usage, item["usage"])
            if item.get("subagent_usage"):
                subagent_usage = _merge_usage_summaries(subagent_usage, item["subagent_usage"])
        model = message.get("model", "")
        metadata = [f"🤖 {model}"] if model else []
        if usage:
            metadata.append(format_message_usage(usage, subagent_usage))
        for item in group:
            if "answer_duration_seconds" in item:
                metadata.append(format_answer_duration(item["answer_duration_seconds"]))
        body = visible[0]["text"]
        for item in visible[1:]:
            separator = "\n\n" if item.get("after_tool_use") else "\n"
            body = body.rstrip("\n") + separator + item["text"]
        header = assistant_callout_header(AGENT_NAME, metadata)
        current_model = next((line for line in header.splitlines() if line.startswith("> <small>🤖 ")), "")
        previous_model = next((line for line in (last_header or "").splitlines() if line.startswith("> <small>🤖 ")), "")
        continued = bool(last_header and last_header.splitlines()[0] == f"> [!NOTE] {AGENT_NAME}" and previous_model == current_model)
        if continued:
            fragment = render_assistant_message_block(body, AGENT_NAME, metadata, continued=True)
            separator = "\n\n" if message.get("after_tool_use") else "\n"
            if blocks:
                blocks[-1] = blocks[-1].rstrip("\n") + separator + fragment + "\n\n"
            else:
                content = content.rstrip("\n") + separator
                blocks.append(fragment + "\n\n")
        else:
            blocks.append(render_assistant_message_block(body, AGENT_NAME, metadata, extra_blank_line=True))
            appended += 1
        last_header = header
    return content + "".join(blocks), appended, bool(blocks)
# END GENERATED: incremental-rendering

# BEGIN GENERATED: note-metadata
FRONTMATTER = re.compile(r"\A---[ \t]*\n(.*?)^---[ \t]*(?:\n|$)", re.M | re.S)


def frontmatter_key(line: str) -> str | None:
    match = re.match(r"""^(['"]?)([a-zA-Z0-9_-]+)\1[ \t]*:(?=[ \t]|$)""", line)
    return match[2] if match else None


def build_note_fields(
    session_id: str, cwd: str, created: datetime, *,
    source: str | None = None, tags: list[str] | None = None,
) -> dict[str, list[str]]:
    source = SOURCE_NAME if source is None else source
    tag_lines = ["tags:"]
    for tag in tags if tags is not None else ["ai-conversation", source]:
        # Preserve the conventional plain form where it is an unambiguous YAML scalar.
        safe = re.fullmatch(r"[A-Za-z][A-Za-z0-9._-]*", str(tag))
        value = str(tag) if safe and str(tag).lower() not in {"true", "false", "null", "yes", "no", "on", "off"} else yaml_quote(tag)
        tag_lines.append(f"  - {value}")
    return {
        "source": [f"source: {source}"],
        "session_id": [f"session_id: {yaml_quote(session_id)}"],
        "project": [f"project: {yaml_quote(cwd)}"],
        "created": [f"created: {yaml_quote(format_iso(created))}"],
        "tags": tag_lines,
    }


def build_common_frontmatter(
    session_id: str, cwd: str, created: datetime, modified: datetime,
    count: int, title: str | None = None, *, source: str | None = None,
    tags: list[str] | None = None, extra_fields=(),
) -> str:
    fields = build_note_fields(session_id, cwd, created, source=source, tags=tags)
    lines = [
        "---", *fields["source"],
        *([f"title: {yaml_quote(title)}"] if title is not None else []),
        *fields["session_id"], *fields["project"], *fields["created"],
        f"modified: {yaml_quote(format_iso(modified))}", *fields["tags"],
        f"message_count: {count}", *extra_fields, "---",
    ]
    return "\n".join(lines) + "\n\n"


def note_output_path(session_id: str, cwd: str, created: datetime) -> Path:
    project = safe_filename_component(Path(cwd).name) if cwd else ""
    prefix = f"{created:%Y%m%d}_{created:%H%M%S}"
    if project:
        prefix += f"_{project}"
    return OUTPUT_BASE / f"{prefix}_{safe_session_key(session_id) if session_id else 'unknown'}.md"


def read_markdown_metadata(path: Path) -> dict:
    """標準ライブラリで、このプロジェクトのfrontmatterの識別項目だけを読む。"""
    return parse_markdown_metadata(path.read_text(encoding="utf-8"))


def parse_markdown_metadata(content: str) -> dict:
    match = FRONTMATTER.match(content)
    if not match:
        return {}
    result = {}
    in_tags = False
    for line in match.group(1).splitlines():
        if line.startswith("tags:"):
            in_tags = True
            result["tags"] = []
            continue
        if in_tags and line.startswith("  - "):
            raw = line[4:].strip()
            try:
                tag = json.loads(raw)
            except ValueError:
                tag = raw
            if isinstance(tag, str):
                result["tags"].append(tag)
            continue
        if line and not line.startswith((" ", "\t")):
            in_tags = False
        if line.startswith(("source:", "created:", "title:", "session_id:", "project:")):
            key, raw = line.split(":", 1)
            try:
                value = json.loads(raw.strip())
            except ValueError:
                value = raw.strip()
            if isinstance(value, str):
                result[key] = value
    return result


def preserve_modified_if_unchanged(content: str, refreshed: str) -> str:
    """更新時刻以外が一致するなら既存ノートをそのまま返す。"""
    # Scope to frontmatter, so an assistant's code or prose is never replaced.
    pattern = r"\A(---\s*\n)(.*?)(\n---\s*\n)"
    old = re.match(pattern, content, re.DOTALL)
    new = re.match(pattern, refreshed, re.DOTALL)
    if not old or not new:
        return refreshed
    modified = next((line for line in old.group(2).splitlines() if line.startswith("modified:")), None)
    if modified is None:
        return refreshed
    fields = re.sub(r"(?m)^modified:.*$", lambda _: modified, new.group(2), count=1)
    unchanged_time = new.group(1) + fields + new.group(3) + refreshed[new.end():]
    return content if unchanged_time == content else refreshed
# END GENERATED: note-metadata


# ============================================================
# transcript（JSONL）のパース
# ============================================================
def find_jsonl_path(session_id: str, transcript_path: str) -> Path | None:
    """
    transcriptPath が存在する場合はそれを使い、
    存在しない場合はデフォルトの brain ログディレクトリを探す。
    """
    if transcript_path:
        p = Path(transcript_path)
        if p.exists():
            return p
        logger.info(f"transcriptPath が指すファイルが存在しません: {p} → session_id から探索します")

    if session_id:
        p = Path.home() / ".gemini" / "antigravity-cli" / "brain" / session_id / ".system_generated" / "logs" / "transcript.jsonl"
        if p.exists():
            return p

    logger.warning(f"JSONLファイルが見つかりません: session={session_id}")
    return None


def extract_model_name(content) -> str | None:
    """USER_INPUT の content からモデル名設定変更を抽出する"""
    text = ""
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and "text" in block:
                parts.append(block["text"])
            elif isinstance(block, str):
                parts.append(block)
        text = "\n".join(parts)

    match = re.search(r"<USER_SETTINGS_CHANGE>(.*?)</USER_SETTINGS_CHANGE>", text, re.DOTALL | re.IGNORECASE)
    if not match:
        return None

    change_text = match.group(1).strip()
    m = re.search(r"`Model Selection`\s+from\s+.*?\s+to\s+(.*?)\.(?:\s+[A-Z]|\Z)", change_text)
    if m:
        return m.group(1).strip()
    return None


# BEGIN GENERATED: incremental-answer-usage
def _answer_usage_groups(messages: list[dict]) -> list[tuple[int, dict | None, dict | None]]:
    """User発話ごとにAgentMessageを回答単位へまとめる。"""
    groups: list[tuple[int, dict | None, dict | None]] = []
    turn_index = 0
    last_model = None
    last_turn_id = None
    for message in messages:
        turn_id = message.get("turn_id") or None
        if message.get("type") == "user_message":
            last_turn_id = turn_id
            turn_index += 1
            last_model = None
            continue
        if message.get("type") != "agent_message" or not str(message.get("message", "")).strip():
            continue
        if message.get("after_turn_boundary") or (turn_id and last_turn_id and turn_id != last_turn_id):
            turn_index += 1
            last_model = None
        last_turn_id = turn_id or last_turn_id
        model = format_model_name(message.get("model", ""), message.get("effort", ""))
        if groups and model == last_model:
            _, usage, subagent_usage = groups[-1]
            groups[-1] = (
                turn_index,
                _merge_usage_summaries(usage, message.get("usage")),
                _merge_usage_summaries(subagent_usage, message.get("subagent_usage")),
            )
        else:
            groups.append((turn_index, message.get("usage"), message.get("subagent_usage")))
            last_model = model
    return groups


def refresh_answer_usage_metadata(content: str, messages: list[dict]) -> str:
    """遅れて書かれたusage recordを既存assistant calloutへ反映する。

    本文・ユーザーcallout・既存のquota行は変更せず、assistant metadata内のUsage行だけを
    親ターンとモデルの連続区間に沿って差し替える。過去に同じ回答が複数calloutへ
    分割された場合は、その最後のcalloutにUsageを表示する。
    """
    groups = _answer_usage_groups(messages)
    spans = _assistant_header_spans(content)
    if not groups or not spans:
        return content
    lines = content.splitlines(keepends=True)
    user_positions = []
    active_fence = None
    for index, line in enumerate(lines):
        marker = _fence_marker(line)
        if active_fence is not None:
            if (
                marker is not None
                and marker[0] == active_fence[0]
                and marker[1] >= active_fence[1]
                and not marker[2].strip()
            ):
                active_fence = None
            continue
        if marker is not None:
            active_fence = marker[:2]
            continue
        if line.rstrip("\r\n") in ("> [!QUESTION] User", HIDDEN_USER_BOUNDARY):
            user_positions.append(index)

    groups_by_turn: dict[int, list[tuple[dict | None, dict | None]]] = {}
    for turn_index, usage, subagent_usage in groups:
        groups_by_turn.setdefault(turn_index, []).append((usage, subagent_usage))
    spans_by_turn: dict[int, list[tuple[int, int]]] = {}
    for start, end in spans:
        turn_index = bisect_right(user_positions, start)
        spans_by_turn.setdefault(turn_index, []).append((start, end))

    replacements = []
    for turn_index, turn_spans in spans_by_turn.items():
        runs: list[list[tuple[int, int]]] = []
        last_model = None
        for start, end in turn_spans:
            model = next((line for line in lines[start + 1:end] if line.startswith("> <small>🤖 ")), "")
            if runs and model == last_model:
                runs[-1].append((start, end))
            else:
                runs.append([(start, end)])
            last_model = model
        turn_groups = groups_by_turn.get(turn_index, [])
        if len(runs) != len(turn_groups):
            logger.warning("回答Usageのcallout対応を特定できません: turn=%s", turn_index)
            continue
        for run, (usage, subagent_usage) in zip(runs, turn_groups):
            usage_text = format_message_usage(usage, subagent_usage)
            for run_index, (start, end) in enumerate(run):
                metadata_lines = [
                    line for line in lines[start:end]
                    if not re.match(r"^> <small>Usage: .*?</small>\r?\n?$", line)
                ]
                if run_index == len(run) - 1 and usage_text:
                    newline = "\r\n" if lines[start].endswith("\r\n") else "\n"
                    timing_index = next((index for index, line in enumerate(metadata_lines)
                                         if line.startswith("> <small>⏱ 回答所要時間: ")), len(metadata_lines))
                    metadata_lines.insert(timing_index, f"> <small>{usage_text}</small>{newline}")
                replacements.append((start, end, metadata_lines))
    for start, end, metadata_lines in sorted(replacements, reverse=True):
        lines[start:end] = metadata_lines
    return "".join(lines)
# END GENERATED: incremental-answer-usage


def agy_usage_model(value: object) -> str:
    """agyのGeminiモデル名から、価格照合に使うモデルIDを返す。"""
    name = _normalize_usage_model(value)
    match = re.fullmatch(
        r"(gemini-4-(?:argon|neon|hydrogen)|gemini-3\.8-flash)"
        r"(?:-(?:low|medium|high|xhigh|max)| \((?:low|medium|high|xhigh|max)\))?",
        name.replace(" ", "-", 2) if name.startswith("gemini ") else name,
    )
    return match[1] if match else name or "unknown"


def agy_response_model(row: dict, selected_model: str | None) -> str:
    model = row.get("model")
    return model.strip() if isinstance(model, str) and model.strip() else selected_model or ""


def agy_usage_record(row: dict, selected_model: str | None) -> dict:
    model = agy_response_model(row, selected_model)
    fresh, cached, output = (row.get(key) for key in ("input_tokens", "cache_read_tokens", "output_tokens"))
    usage = None
    if all(type(value) is int and value >= 0 for value in (fresh, cached, output)):
        usage = {"input_tokens": fresh + cached, "cached_input_tokens": cached,
                 "output_tokens": output, "total_tokens": fresh + cached + output}
    return {"type": "assistant", "model": agy_usage_model(model), "usage": usage}


def summarize_agy_usage(records: list[dict]) -> dict | None:
    usage = summarize_usage(records)
    if usage is not None:
        missing = {record["model"] for record in records if record["usage"] is None}
        if missing:
            usage["tokens_incomplete"] = True
            if "total_cost_usd" in usage:
                usage["cost_incomplete"] = True
            for model in missing & usage["by_model"].keys():
                entry = usage["by_model"][model]
                if "cost_usd" in entry:
                    entry["cost_incomplete"] = True
    return usage


def load_jsonl_messages(jsonl_path: Path, *, usage_records: list[dict] | None = None) -> list:
    """
    JSONL ファイルを読み込み、type が USER_INPUT/PLANNER_RESPONSE の行だけを返す。
    PLANNER_RESPONSE のうち、tool_calls が存在する（中間ステップ）ものは除外する。
    step_index がキーの重複時には最新のデータを保持して昇順でソートする。
    """
    all_msgs = []
    try:
        content = jsonl_path.read_text(encoding="utf-8")
        for line in content.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                all_msgs.append(json.loads(line))
            except json.JSONDecodeError as e:
                logger.info(f"JSONLの1行をスキップ（不正なJSON）: {e}")
                continue
    except Exception as e:
        mark_save_skipped("transcript_unreadable")
        logger.warning(f"JSONL読み込み失敗: {jsonl_path}: {e}")
        return []

    # step_index で最新のものを保持（JSONLには更新行が含まれる場合があるため）
    unique_messages: dict[int, dict] = {}
    for msg in all_msgs:
        if not isinstance(msg, dict):
            continue
        idx = msg.get("step_index")
        try:
            # JSONL の世代によって数値と数値文字列が混在しても同じ step として扱う。
            normalized_idx = int(idx)
        except (TypeError, ValueError):
            logger.debug(f"step_index が数値でない行をスキップ: {idx!r}")
            continue
        unique_messages[normalized_idx] = msg

    sorted_msgs = [unique_messages[k] for k in sorted(unique_messages)]

    current_model = None
    final_messages = []
    pending_usage = []
    for i, msg in enumerate(sorted_msgs):
        msg_type = msg.get("type")
        
        if msg_type == "USER_INPUT":
            pending_usage.clear()
            model_name = extract_model_name(msg.get("content"))
            if model_name:
                current_model = model_name
            text = extract_user_text(msg.get("content"))
            if text is not None:
                final_messages.append({
                    "step_index": msg.get("step_index", 0),
                    "type": "user_message",
                    "message": text,
                    "timestamp": msg.get("created_at", ""),
                    "model": "",
                })

        elif msg_type == "PLANNER_RESPONSE":
            if usage_records is not None and msg.get("status") == "DONE":
                record = agy_usage_record(msg, current_model)
                usage_records.append(record)
                pending_usage.append(record)
            # ツール呼び出しがある中間ステップは除外
            if msg.get("tool_calls"):
                continue

            # テキストが存在するもの、または最後のメッセージである場合は保持する
            # （is_terminal ロジックが厳しすぎると、バックグラウンドタスク報告などが漏れるため緩和）
            is_terminal = False
            if i == len(sorted_msgs) - 1:
                is_terminal = True
            else:
                next_msg = sorted_msgs[i+1]
                if next_msg.get("type") == "USER_INPUT":
                    is_terminal = True

            # tool_calls がなく、かつ content が空でない場合は原則保持
            has_content = False
            content = msg.get("content")
            if isinstance(content, str) and content.strip():
                has_content = True
            elif isinstance(content, list) and any(block.get("text", "").strip() for block in content if isinstance(block, dict) and block.get("type") == "text"):
                has_content = True

            if is_terminal or has_content:
                parts = extract_agent_parts(msg)
                if not parts:
                    continue
                full_text = "\n\n".join(p["text"] for p in parts)
                final_messages.append({
                    "step_index": msg.get("step_index", 0),
                    "type": "agent_message",
                    "message": full_text,
                    "timestamp": msg.get("created_at", ""),
                    "model": agy_response_model(msg, current_model),
                })
                if pending_usage:
                    final_messages[-1]["usage"] = summarize_agy_usage(pending_usage)
                    pending_usage.clear()

    # この形式のcreated_atは完了時刻と確定できないため、時間は推測しない。
    return annotate_answer_timings(final_messages)


def extract_user_text(content) -> str:
    text = ""
    if isinstance(content, str):
        text = content.strip()
    elif isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and "text" in block:
                parts.append(block["text"].strip())
        text = "\n".join(parts)
    
    # <USER_REQUEST>...</USER_REQUEST> タグがある場合はその中身だけを抽出
    match = re.search(r"<USER_REQUEST>(.*?)</USER_REQUEST>", text, re.DOTALL | re.IGNORECASE)
    if match:
        text = match.group(1).strip()
        
    return sanitize_markdown(text)


def extract_agent_parts(msg: dict) -> list:
    """
    PLANNER_RESPONSE からテキスト部分を抽出する。
    """
    parts = []
    content = msg.get("content", "")

    if isinstance(content, str):
        text = content.strip()
        if text:
            parts.append({"type": "text", "text": sanitize_markdown(text)})

    elif isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type", "")
            if btype == "text":
                text = block.get("text", "").strip()
                if text:
                    parts.append({"type": "text", "text": sanitize_markdown(text)})

    return parts


def get_new_messages(messages: list, last_id: str) -> list:
    """
    last_id (step_index文字列) 以降の新規メッセージを返す。
    数値比較を行うことで、last_id 自体がフィルタリングで除外されていても動作するようにする。
    """
    if last_id == "":
        return messages

    try:
        last_id_int = int(last_id)
    except (ValueError, TypeError):
        # 数値に変換できない場合は、既存の文字列一致ロジックにフォールバック
        logger.info(f"last_id が数値でないため文字列一致にフォールバック: last_id={last_id!r}")
        found = False
        result = []
        for msg in messages:
            msg_id = str(msg.get("step_index", ""))
            if not found:
                if msg_id == last_id:
                    found = True
                continue
            result.append(msg)
        if not found:
            logger.info(f"last_id={last_id!r} がメッセージリスト内に見つかりませんでした")
        return result if found else []

    # 数値比較でそれ以降のメッセージを取得
    result = []
    for msg in messages:
        try:
            msg_id_int = int(msg.get("step_index", -1))
            if msg_id_int > last_id_int:
                result.append(msg)
        except (ValueError, TypeError):
            continue
    
    return result


# ============================================================
# quota（agy の読み取り専用 /quota コマンド）
# ============================================================
def agy_quota_group(model: str) -> str | None:
    """応答モデルに対応する利用枠を識別する。"""
    match = re.match(r"^(gemini|claude|gpt)(?=$|[\s_-]|\d)", _normalize_usage_model(model))
    if match:
        return "gemini" if match[1] == "gemini" else "claude"
    return None


def parse_quota_data(data: dict) -> dict:
    """agy の ``/quota --output-format json`` 応答を保存用形式へ正規化する。"""
    if not isinstance(data, dict):
        return {}
    command = data.get("command", {})
    command_data = command.get("data", {}) if isinstance(command, dict) else {}
    groups = command_data.get("groups", []) if isinstance(command_data, dict) else []
    if not isinstance(groups, list):
        return {}
    quota = {}

    for group in groups:
        if not isinstance(group, dict):
            continue
        group_name = str(group.get("name", ""))
        group_name_lower = group_name.lower()
        if "gemini" in group_name_lower:
            key = "gemini"
        elif "claude" in group_name_lower or "gpt" in group_name_lower:
            key = "claude"
        else:
            continue

        parsed_buckets = {}
        buckets = group.get("buckets", [])
        if not isinstance(buckets, list):
            continue
        for bucket in buckets:
            if not isinstance(bucket, dict):
                continue
            try:
                raw_remaining = bucket["remaining_fraction"]
                if isinstance(raw_remaining, bool):
                    continue
                remaining_fraction = float(raw_remaining)
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
            if not math.isfinite(remaining_fraction) or not 0 <= remaining_fraction <= 1:
                continue
            remaining = round(remaining_fraction * 100, 10)

            window = str(bucket.get("window", "")).lower()
            bucket_id = str(bucket.get("id", "")).lower()
            if "week" in window or "week" in bucket_id:
                window_key = "weekly"
            elif "5" in window or "5" in bucket_id:
                window_key = "5h"
            else:
                continue

            reset = ""
            reset_time = bucket.get("reset_time")
            if reset_time:
                try:
                    reset_dt = datetime.fromisoformat(str(reset_time).replace("Z", "+00:00"))
                    reset = reset_dt.astimezone(JST).strftime("%Y-%m-%d %H:%M")
                except (TypeError, ValueError):
                    logger.info("agy quota の reset_time をパースできませんでした")
            parsed_buckets[window_key] = {"remaining": remaining, "reset": reset}

        if parsed_buckets:
            quota[key] = parsed_buckets
    return quota


def retrieve_quota() -> dict | None:
    """公式の読み取り専用 ``agy /quota`` から quota を取得する。

    このコマンドは会話ターンを作らず、quota も消費しない。失敗しても保存処理は続行する。
    """
    try:
        with subprocess.Popen(
            ["agy", "--output-format", "json", "--print=/quota"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            start_new_session=True,
        ) as process:
            try:
                stdout, _ = process.communicate(timeout=AGY_QUOTA_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                # 起動した言語サーバーも含めて終了し、出力パイプを再度待たない。
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                raise
        if process.returncode != 0:
            raise RuntimeError(f"agy /quota が終了コード {process.returncode} で失敗")
        data = json.loads(stdout)
        if not isinstance(data, dict):
            raise RuntimeError("agy /quota がJSONオブジェクトを返しませんでした")
        if data.get("status") != "SUCCESS" or data.get("num_turns") != 0:
            raise RuntimeError("agy /quota が読み取り専用の成功応答を返しませんでした")
        quota = parse_quota_data(data)
        if not quota:
            raise RuntimeError("agy /quota 応答に利用可能な quota がありません")
        observed_at = format_iso(now_jst())
        for windows in quota.values():
            for window in windows.values():
                window["observed_at"] = observed_at
        return quota
    except (
        OSError,
        subprocess.TimeoutExpired,
        json.JSONDecodeError,
        RuntimeError,
        TypeError,
        ValueError,
        UnicodeError,
    ) as error:
        logger.info("Quota情報の取得に失敗しました: %s", type(error).__name__)
        return None


def format_reset_time(reset_jst: str) -> str:
    if not reset_jst:
        return ""
    try:
        reset_dt = datetime.strptime(reset_jst, "%Y-%m-%d %H:%M").replace(tzinfo=JST)
        seconds = int((reset_dt - now_jst()).total_seconds())
    except (TypeError, ValueError):
        return reset_jst
    if seconds <= 0:
        return "now"
    minutes = (seconds + 59) // 60
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours >= 24:
        days, hours = divmod(hours, 24)
        return f"{days}d {hours}h" if hours else f"{days}d"
    return f"{hours}h {minutes}m" if minutes else f"{hours}h"


def quota_value(quota: dict | None, group: str, window: str, field: str):
    if not quota:
        return None if field == "remaining" else ""
    return quota.get(group, {}).get(window, {}).get(field, None if field == "remaining" else "")


def format_quota_observations(first: object, last: object) -> str:
    first_dt = _try_parse_datetime(first)
    last_dt = _try_parse_datetime(last)
    observations = [
        f"{label} {observed:%Y-%m-%d %H:%M}"
        for label, observed in (("First", first_dt), ("Latest", last_dt))
        if observed is not None
    ]
    return " (" + " · ".join(observations) + " JST)" if observations else ""


def build_quota_section(initial_quota: dict | None, final_quota: dict | None) -> str:
    lines = ["📊 **Quota**:"]
    for group, label in (("gemini", "Gemini"), ("claude", "Claude")):
        values = []
        for window, short_label in (("weekly", "W"), ("5h", "5h")):
            initial = quota_value(initial_quota, group, window, "remaining")
            final = quota_value(final_quota, group, window, "remaining")
            if initial is None and final is None:
                continue
            reset = quota_value(final_quota, group, window, "reset") or quota_value(initial_quota, group, window, "reset")
            reset_text = f" (⟳ {format_reset_time(reset)})" if reset else ""
            if initial is not None and final is not None and abs(initial - final) > 1e-5:
                value = f"{initial:.1f}% ➔ {final:.1f}%"
            else:
                value = f"{(final if final is not None else initial):.1f}%"
            observations = format_quota_observations(
                quota_value(initial_quota, group, window, "observed_at"),
                quota_value(final_quota, group, window, "observed_at"),
            )
            values.append(f"{short_label}: {value}{observations}{reset_text}")
        if values:
            lines.append(f"- **{label}**: " + " / ".join(values))
    return "\n".join(lines) + "\n" if len(lines) > 1 else "📊 **Quota**: N/A\n"


QUOTA_SECTION_PATTERN = r"\A[ \t]*📊 \*\*Quota\*\*:(?:[ \t]*N/A)?\n(?:[ \t]*-[^\n]*(?:\n|$))*"


def add_quota_metadata(content: str, initial_quota: dict | None, final_quota: dict | None) -> str:
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n", content, re.DOTALL)
    if not match:
        return content
    old_lines = match.group(1).splitlines()
    kept = []
    skipping = False
    for line in old_lines:
        if re.match(r"^(?:quota|quota_claude):", line):
            skipping = True
            continue
        if skipping and (line.startswith((" ", "\t")) or not line.strip()):
            continue
        skipping = False
        kept.append(line)
    kept.extend(build_quota_frontmatter_fields(initial_quota, final_quota))
    frontmatter, body = "\n".join(kept), content[match.end():]
    section = build_quota_section(initial_quota, final_quota)
    if re.match(QUOTA_SECTION_PATTERN, body):
        body = re.sub(QUOTA_SECTION_PATTERN, lambda _: section, body, count=1)
    elif initial_quota or final_quota:
        body = section + "\n" + body.lstrip()
    return "---\n" + frontmatter + "\n---\n" + body


def refresh_agy_usage_metadata(content: str, usage: dict, messages: list[dict]) -> str:
    match = re.match(r"\A---\n(.*?)\n---\n", content, re.DOTALL)
    if not match:
        return content
    frontmatter = re.sub(r"(?m)^usage:[^\n]*\n(?:[ \t][^\n]*\n)*", "", match[1] + "\n")
    body = content[match.end():]
    quota = re.match(QUOTA_SECTION_PATTERN, body)
    offset = quota.end() if quota else 0
    section = re.sub(r"\A\n*💰 \*\*Usage \(USD換算\)\*\*:[^\n]*\n\n?", "", body[offset:])
    body = body[:offset] + build_usage_section(usage) + "\n" + section
    updated = "---\n" + frontmatter + "\n".join(usage_frontmatter_fields(usage)) + "\n---\n" + body
    return refresh_answer_usage_metadata(updated, messages)


# ============================================================
# Markdownファイル生成・操作
# ============================================================
def build_frontmatter(
    session_id: str,
    cwd: str,
    start_dt: datetime,
    message_count: int = 0,
    title: str | None = None,
    *, source: str | None = None, tags=None,
) -> str:
    return build_common_frontmatter(session_id, cwd, start_dt, start_dt, message_count,
                                    title, source=source, tags=tags, extra_fields=())


def create_md_file(session_id: str, cwd: str, start_dt: datetime, title: str | None = None,
                   *, source: str | None = None, tags=None) -> Path:
    path = note_output_path(session_id, cwd, start_dt)
    fm = build_frontmatter(session_id, cwd, start_dt, title=title, source=source, tags=tags)
    atomic_write_md(path, fm + "<!-- last_id: -->\n\n")
    return path


# BEGIN GENERATED: find-existing-markdown
def find_existing_md(session_id: str) -> Path | None:
    """state消失時にfrontmatterのsession_idから既存Markdownを探す。"""
    if not OUTPUT_BASE.exists():
        return None
    identifiers = []
    full_key = safe_session_key(session_id)
    short_key = safe_filename_component(session_id[:8]) if session_id else ""
    if full_key:
        identifiers.append(full_key)
    if short_key and short_key not in identifiers:
        identifiers.append(short_key)
    if not identifiers:
        identifiers.append("unknown")

    expected = {
        f"session_id: {yaml_quote(session_id)}",
        f"session_id: {session_id}",
    }
    candidates = set()
    try:
        for path in OUTPUT_BASE.iterdir():
            if any(path.name.endswith(f"_{identifier}.md") for identifier in identifiers):
                candidates.add(path)
    except OSError as error:
        raise ExistingMarkdownSearchError(f"既存Markdown候補の列挙に失敗しました: {error}")

    candidate_entries = []
    for candidate in candidates:
        try:
            candidate_entries.append((candidate, candidate.stat().st_mtime))
        except OSError as error:
            raise ExistingMarkdownSearchError(
                f"既存Markdown候補の情報取得に失敗しました: {candidate}: {error}"
            )
    candidate_entries.sort(key=lambda item: item[1], reverse=True)

    last_read_error = None
    for candidate, _ in candidate_entries:
        try:
            content = candidate.read_text(encoding="utf-8")
            parts = content.split("---", 2)
            if len(parts) >= 3:
                frontmatter_lines = [line.strip() for line in parts[1].splitlines()]
                if any(item in frontmatter_lines for item in expected):
                    return candidate
        except (OSError, UnicodeError) as error:
            last_read_error = error
            logger.warning("既存Markdown候補の読み込みに失敗したためスキップします: %s: %s", candidate, error)
        except IndexError:
            continue
    if last_read_error is not None:
        raise ExistingMarkdownSearchError(
            "既存Markdown候補の読み取りに失敗したファイルが存在し、"
            f"一致を確認できなかったため探索を中断します: {last_read_error}"
        )
    return None
# END GENERATED: find-existing-markdown

# BEGIN GENERATED: incremental-storage
def recover_incremental_state(session_id: str, cwd: str, state: dict, cursor_key: str, read_cursor) -> dict:
    """state消失時は既存ノートを探索し、識別情報・quota・cursorを復旧する。"""
    if not state.get("output_path"):
        cleanup_old_states()
        path = find_existing_md(session_id)
        if path is None:
            return state
        metadata = read_markdown_metadata(path)
        start = parse_datetime(metadata.get("created"), datetime.fromtimestamp(path.stat().st_mtime, tz=JST))
        first, last = recover_quota_frontmatter(path.read_text(encoding="utf-8"))
        state.update(output_path=str(path), start_time=format_iso(start), cwd=metadata.get("project") or cwd)
        state[cursor_key] = read_cursor(path) or (0 if cursor_key == "last_line" else "")
        state["initial_quota"] = merge_first_quota(first, state.get("initial_quota") or {})
        state["final_quota"] = merge_first_quota(state.get("final_quota"), last)
        logger.warning("stateを既存Markdownから復旧しました: %s", path)
    path = Path(state["output_path"])
    if path.exists():
        metadata = read_markdown_metadata(path)
        state["source"] = metadata.get("source") or state.get("source") or SOURCE_NAME
        state["tags"] = metadata.get("tags", state.get("tags", ["ai-conversation", state["source"]]))
    return state


def incremental_note_fields(session_id: str, cwd: str, state: dict) -> dict[str, list[str]]:
    return build_note_fields(session_id, state.get("cwd", cwd), parse_datetime(state.get("start_time"), now_jst()),
                             source=state.get("source"), tags=state.get("tags"))


def ensure_incremental_note(session_id: str, cwd: str, state: dict, cursor_key: str,
                            read_cursor, has_body: bool, create_note):
    """保存済みノートの識別を保持し、本文がある場合だけ新規作成・再作成する。"""
    path = Path(state["output_path"]) if state.get("output_path") else None
    if path is None or not path.exists():
        if not has_body:
            return None
        start = parse_datetime(state.get("start_time"), now_jst())
        state.setdefault("cwd", cwd)
        state.setdefault("source", SOURCE_NAME)
        state.setdefault("tags", ["ai-conversation", state["source"]])
        path = create_note(start, state)
        state.update(output_path=str(path), start_time=format_iso(start))
        state[cursor_key] = 0 if cursor_key == "last_line" else ""
        logger.info("セッション初期化 %s -> %s", session_id[:8], path)
    if cursor_key == "last_line":
        try:
            saved = int(state.get(cursor_key, 0))
        except (TypeError, ValueError):
            saved = 0
    else:
        saved = str(state.get(cursor_key, ""))
    observed = read_cursor(path)
    cursor = observed if observed is not None else saved
    if observed is not None and observed != saved:
        logger.warning("%sの不一致をMarkdownから復旧: state=%r, markdown=%r", cursor_key, saved, observed)
    state[cursor_key] = cursor
    save_state(session_id, state)
    return path, cursor
# END GENERATED: incremental-storage

# BEGIN GENERATED: frontmatter-update
def update_frontmatter_fields(
    content: str,
    modified_dt: datetime,
    appended: int,
    *,
    extra_removed_keys: tuple[str, ...] = (),
    extra_fields: tuple[str, ...] = (),
    note_fields: dict[str, list[str]] | None = None,
) -> tuple[list[str], str] | None:
    """frontmatterの共通項目を更新し、項目行と本文を返す。"""
    fm_match = FRONTMATTER.match(content)
    if not fm_match:
        return None

    fm_lines = fm_match.group(1).splitlines()
    body_text = re.sub(r"\A(?:[ \t]*\n)+", "", content[fm_match.end():])
    existing_keys = set()
    keys_to_remove = {"modified", "message_count", *extra_removed_keys}
    new_fm_lines = []
    skip_list = False
    old_count = 0
    for line in fm_lines:
        key = frontmatter_key(line)
        if key == "message_count":
            try:
                old_count = int(line.split(":", 1)[1].strip())
            except ValueError:
                pass
        if key is not None:
            existing_keys.add(key)
            if key in keys_to_remove:
                skip_list = True
                continue
            skip_list = False
            new_fm_lines.append(line)
            continue
        if line.startswith("  - ") or line.startswith("    - "):
            if skip_list:
                continue
        else:
            skip_list = False
        new_fm_lines.append(line)

    if "title" not in existing_keys:
        title_match = re.search(r"(?m)^# (?:User: )?(.+?)\s*$", body_text)
        if title_match:
            title = unescape_markdown_heading(title_match.group(1))
            new_fm_lines.append(f"title: {yaml_quote(title)}")
    for key, lines in (note_fields or {}).items():
        if key not in existing_keys:
            new_fm_lines.extend(lines)
    new_fm_lines.append(f"modified: {yaml_quote(format_iso(modified_dt))}")
    new_fm_lines.append(f"message_count: {old_count + appended}")
    new_fm_lines.extend(extra_fields)
    return new_fm_lines, body_text


def compose_markdown(new_fm_lines: list[str], body_text: str) -> str:
    """更新済みfrontmatterと本文をMarkdown全文へ戻す。"""
    return "---\n" + "\n".join(new_fm_lines) + "\n---\n" + body_text
# END GENERATED: frontmatter-update


def update_markdown_metadata(
    content: str,
    modified_dt: datetime,
    appended: int,
    last_id: str,
    initial_quota: dict | None = None,
    current_quota: dict | None = None,
    *,
    note_fields: dict[str, list[str]] | None = None,
) -> str:
    """本文のfrontmatter・cursor・quotaを更新し、書き込み前の全文を返す。"""
    updated = update_frontmatter_fields(content, modified_dt, appended, note_fields=note_fields)
    if updated is None:
        return content
    new_fm_lines, body_text = updated
    body_text = re.sub(
        r"<!--\s*last_id:.*?-->",
        f"<!-- last_id: {last_id} -->",
        body_text
    )

    new_content = compose_markdown(new_fm_lines, body_text)
    new_content = add_quota_metadata(new_content, initial_quota, current_quota)
    return new_content


def read_last_id_from_md(path: Path) -> str | None:
    try:
        content = path.read_text(encoding="utf-8")
        m = LAST_ID_PATTERN.search(content)
        if m:
            return m.group(1) if m.group(1) else ""
        logger.debug(f"MDファイルに last_id コメントが見つかりません: {path}")
    except Exception as e:
        logger.warning(f"MDファイルからの last_id 読み取りに失敗: {path}: {e}")
    return None


# BEGIN GENERATED: atomic-markdown-write
def atomic_write_md(path: Path, content: str) -> None:
    """Markdown全文を一時ファイル経由でatomicに保存する。"""
    atomic_write_text(path, content)
# END GENERATED: atomic-markdown-write


# BEGIN GENERATED: callout-header
def is_callout_header_line(line: str) -> bool:
    return line.startswith("> [!") or line.startswith("> <small>") or line == ">"


def get_last_callout_header(content: str) -> str | None:
    """コードフェンス外にある最後のCalloutヘッダーを抽出する。"""
    last_header = None
    header_lines = []
    for line in _iter_lines_outside_fences(content):
        if line == HIDDEN_USER_BOUNDARY:
            last_header = None
            header_lines = []
        elif line.startswith("> [!"):
            header_lines = [line]
            last_header = line
        elif header_lines and is_callout_header_line(line):
            header_lines.append(line)
            last_header = "\n".join(header_lines)
        else:
            header_lines = []
    return last_header.strip() if last_header else None
# END GENERATED: callout-header


# ============================================================
# メッセージ追記（本文・メタデータを単一writeで確定）
# ============================================================
def append_messages(
    path: Path,
    messages: list,
    modified_dt: datetime,
    current_quota: dict | None = None,
    initial_quota: dict | None = None,
    *,
    note_fields: dict[str, list[str]] | None = None,
) -> tuple[str, int, bool]:
    last_id = str(messages[-1].get("step_index", "")) if messages else ""
    content = path.read_text(encoding="utf-8")
    rendered, appended, changed = render_incremental_messages(content, messages)
    if not changed:
        return last_id, 0, False
    final_content = update_markdown_metadata(rendered, modified_dt, appended, last_id,
                                             initial_quota, current_quota, note_fields=note_fields)
    atomic_write_md(path, final_content)
    return last_id, appended, True


# ============================================================
# イベントハンドラ
# ============================================================
ANSWER_COMPLETIONS_PREFIX = "<!-- agy_answer_completions: "


def recover_answer_completions(content: str) -> dict:
    """state消失・履歴再構成でも、以前観測した完了時刻を復元する。"""
    for line in _iter_lines_outside_fences(content):
        if line.startswith(ANSWER_COMPLETIONS_PREFIX) and line.endswith(" -->"):
            try:
                values = json.loads(line[len(ANSWER_COMPLETIONS_PREFIX):-4])
            except (TypeError, ValueError):
                continue
            if isinstance(values, dict):
                return {key: value for key, value in values.items()
                        if re.fullmatch(r"[0-9a-f]{64}", key) and isinstance(value, str)
                        and _try_parse_datetime(value) is not None}
    return {}


def apply_stop_completion(messages: list[dict], state: dict, content: str,
                          last_id: str, hook_input: dict) -> list[dict]:
    """今回の正常終了だけを観測し、回答の識別子ごとに最初の観測時刻を保持する。"""
    completions = dict(state.get("answer_completions") or {})
    completions.update(recover_answer_completions(content))
    result = [dict(message) for message in messages]
    user = None
    keys = {}
    for index, message in enumerate(result):
        if message["type"] == "user_message":
            user = [message.get("step_index"), message.get("timestamp"), message.get("message")]
        elif message["type"] == "agent_message":
            identity = [user, message.get("step_index"), message.get("timestamp"), message.get("message")]
            keys[index] = hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode("utf-8")).hexdigest()
    latest = len(result) - 1
    received_at = hook_input.get("_stop_received_at")
    if latest in keys and received_at:
        key = keys[latest]
        # 復旧したcursor以前の履歴を、今回完了した回答と誤認しない。
        fresh = bool(get_new_messages([result[latest]], last_id))
        pending = state.get("pending_answer_completion") == key
        if fresh or pending:
            normal_stop = (hook_input.get("fullyIdle") is True
                           and hook_input.get("terminationReason") == "model_stop"
                           and not hook_input.get("error"))
            if normal_stop:
                completions.setdefault(key, received_at)
                state.pop("pending_answer_completion", None)
            else:
                state["pending_answer_completion"] = key
    for index, key in keys.items():
        if key in completions:
            result[index]["completed_at"] = completions[key]
    if completions:
        state["answer_completions"] = completions
    return annotate_answer_timings(result)


def handle_stop_event(hook_input: dict, *, observe_completion: bool = True) -> None:
    # ロック待ち・履歴読み込み・quota取得より前に、フック受信時刻を固定する。
    received_at = now_jst().isoformat() if observe_completion else None
    _handle_stop_event(dict(hook_input, _stop_received_at=received_at))


def session_quotas(state: dict, used_groups: set) -> tuple[dict, dict]:
    def for_used_groups(key: str) -> dict:
        return {group: windows for group, windows in (state.get(key) or {}).items()
                if group in used_groups}

    return for_used_groups("initial_quota"), for_used_groups("final_quota")


@session_locked("conversationId")
def _handle_stop_event(hook_input: dict) -> None:
    session_id = hook_input.get("conversationId", "")
    transcript_path = hook_input.get("transcriptPath", "")
    workspace_paths = hook_input.get("workspacePaths", [])
    cwd = workspace_paths[0] if workspace_paths and isinstance(workspace_paths, list) else os.getcwd()

    if not session_id:
        logger.warning("conversationId が取得できませんでした")
        return
    if not transcript_path:
        logger.info("transcriptPath が未提供のため session_id から JSONL を探索します")

    jsonl_path = find_jsonl_path(session_id, transcript_path)
    if not jsonl_path:
        mark_save_skipped("transcript_missing")
        return
    usage_records = []
    all_messages = load_jsonl_messages(jsonl_path, usage_records=usage_records)

    state = load_state(session_id)
    try:
        state = recover_incremental_state(session_id, cwd, state, "last_id", read_last_id_from_md)
    except ExistingMarkdownSearchError as error:
        logger.error("既存Markdownの探索中にエラーが発生したため保存を見送ります: %s", type(error).__name__)
        return
    prepared = ensure_incremental_note(session_id, cwd, state, "last_id", read_last_id_from_md,
        any(str(message.get("message", "")).strip() for message in all_messages),
        lambda start, saved: create_md_file(session_id, saved.get("cwd", cwd), start,
                                           source=saved.get("source"), tags=saved.get("tags")))
    if prepared is None:
        mark_save_skipped("no_content")
        return
    path, last_id = prepared

    all_messages = apply_stop_completion(all_messages, state, path.read_text(encoding="utf-8"), last_id, hook_input)
    new_messages = get_new_messages(all_messages, last_id)

    # Usageの対象には、本文を持たないツール呼び出しの応答も含まれる。
    response_models = {record["model"] for record in usage_records}
    response_models.update(message["model"] for message in all_messages if message["type"] == "agent_message")
    used_groups = {agy_quota_group(model) for model in response_models}
    initial_quota, final_quota = session_quotas(state, used_groups)
    if new_messages:
        new_last_id, appended, updated = append_messages(
            path,
            new_messages,
            now_jst(),
            current_quota=final_quota,
            initial_quota=initial_quota,
            note_fields=incremental_note_fields(session_id, cwd, state),
        )
        if new_last_id:
            state["last_id"] = new_last_id
        save_state(session_id, state)
        logger.info(f"{appended}件追記完了 {session_id[:8]}")
    else:
        save_state(session_id, state)

    content = path.read_text(encoding="utf-8")
    refreshed = refresh_answer_timing_metadata(content, all_messages)
    completions = state.get("answer_completions")
    if completions:
        marker = ANSWER_COMPLETIONS_PREFIX + json.dumps(completions, sort_keys=True) + " -->"
        # 本文中のコード例には触れず、コードフェンス外のコメント行だけを更新する。
        lines = refreshed.splitlines(keepends=True)
        active_fence = None
        for index, line in enumerate(lines):
            fence = _fence_marker(line)
            if active_fence:
                if fence and fence[0] == active_fence[0] and fence[1] >= active_fence[1] and not fence[2].strip():
                    active_fence = None
                continue
            if fence:
                active_fence = fence[:2]
                continue
            if line.startswith(ANSWER_COMPLETIONS_PREFIX) and line.rstrip("\r\n").endswith(" -->"):
                lines[index] = marker + "\n"
                break
        else:
            # 回答が未閉鎖のコードフェンスを含んでも、観測記録は本文より前に置く。
            frontmatter = re.match(r"\A---\s*\n.*?\n---(?:\r?\n|$)", refreshed, re.DOTALL)
            offset = frontmatter.end() if frontmatter else 0
            quota = re.match(QUOTA_SECTION_PATTERN, refreshed[offset:])
            if quota:
                offset += quota.end()
            lines = [refreshed[:offset], marker + "\n", refreshed[offset:]]
        refreshed = "".join(lines)
    if refreshed != content:
        refreshed = update_markdown_metadata(refreshed, now_jst(), 0, state.get("last_id", last_id),
                                              initial_quota, final_quota)
    elif response_models:
        # state保存後に停止した場合も、次のStopでノートに反映する。
        saved_quota = recover_quota_frontmatter(content)
        if build_quota_frontmatter_fields(*saved_quota) != build_quota_frontmatter_fields(initial_quota, final_quota):
            refreshed = add_quota_metadata(refreshed, initial_quota, final_quota)
    usage = summarize_agy_usage(usage_records)
    if usage is not None:
        refreshed = refresh_agy_usage_metadata(refreshed, usage, all_messages)
    if refreshed != content:
        atomic_write_md(path, refreshed)
    save_state(session_id, state)

    # quota取得がStopフックの期限までに終わらなくても、本文と利用量は保存済み。
    if new_messages:
        current_quota = retrieve_quota()
        if current_quota:
            state["initial_quota"] = merge_first_quota(state.get("initial_quota"), current_quota)
            state["final_quota"] = merge_first_quota(current_quota, state.get("final_quota") or {})
            save_state(session_id, state)
            observed_quota = session_quotas(state, used_groups)
            if observed_quota != (initial_quota, final_quota):
                content = path.read_text(encoding="utf-8")
                refreshed = add_quota_metadata(content, *observed_quota)
                if usage is not None:
                    refreshed = refresh_agy_usage_metadata(refreshed, usage, all_messages)
                if refreshed != content:
                    atomic_write_md(path, refreshed)


# メイン
# ============================================================
# BEGIN GENERATED: incremental-main
def main() -> None:
    hook_input = read_stdin()
    handle_stop_event(hook_input)
    print("{}")
# END GENERATED: incremental-main


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logger.exception(f"予期しないエラー: {e}")
        print("{}")
        sys.exit(0)
