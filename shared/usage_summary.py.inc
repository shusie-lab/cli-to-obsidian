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
    lines = ["usage:", "  cost_basis: \"reference\"", *usage_price_fields()]
    lines.extend(f"  {field}: {int(usage.get(field, 0))}" for field in USAGE_FIELDS)
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


def build_usage_section(usage: dict | None) -> str:
    if not usage:
        return "💰 **Usage (USD換算)**: N/A\n"
    parts = [f"{model} {_usage_cost_label(entry, 'cost_usd')}" for model, entry in usage.get("by_model", {}).items()]
    details = f" ({'、'.join(parts)})" if parts else ""
    scope = usage.get("scope")
    scope_line = ""
    if scope:
        scope_line = ("📊 **Usage集計範囲**: 親 " + f"{int(scope.get('parent_tokens', 0)):,} tok"
            + "・子 " + f"{int(scope.get('subagent_tokens', 0)):,} tok"
            + "（回答に紐付かない子 " + f"{int(scope.get('unattributed_subagent_tokens', 0)):,} tok）\n")
    return (f"💰 **Usage (USD換算)**: Total: {_usage_cost_label(usage, 'total_cost_usd')}{details}\n"
            + scope_line + build_reported_cost_section(usage.get("reported_cost")))


def format_message_usage(usage: dict | None, subagent_usage: dict | None = None) -> str:
    if not usage:
        return ""
    warning = " ⚠︎ 272K+" if usage.get("long_context") else ""
    label = f"Usage: {int(usage.get('total_tokens', 0)):,} tok · {_usage_cost_label(usage, 'total_cost_usd')}{warning}"
    if usage.get("reported_cost"):
        label += " · 取得コスト: " + reported_cost_label(usage["reported_cost"])
    if subagent_usage:
        parts = [f"{model} {int(entry.get('total_tokens', 0)):,} tok・{_usage_cost_label(entry, 'cost_usd')}"
                 for model, entry in sorted(subagent_usage.get("by_model", {}).items())]
        if parts:
            label += f" ({'、'.join(parts)})"
    return label


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
    return merged
