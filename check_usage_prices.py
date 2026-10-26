#!/usr/bin/python3
"""公式資料から確認した候補単価と現行表の差分を表示する。単価を自動採用しない。"""
import argparse
from decimal import Decimal
import json
from pathlib import Path
import sys


def catalog(module):
    return {"version": module.active_price_version(), "recorded_at": module.PRICE_RECORDED_AT,
            "sources": module.PRICE_SOURCES, "checked_at": module.PRICE_CHECKED_AT, "effective_from": None,
            "rates": {model: {field: str(value) for field, value in rates.items()}
                      for model, rates in module.active_usage_rates().items()}}


def price_diff(current, candidate):
    if not isinstance(candidate, dict) or not isinstance(candidate.get("rates"), dict):
        raise ValueError("候補にratesオブジェクトが必要です")
    rates = candidate["rates"]
    required = {"input", "output", "cached_input", "cache_write_input"}
    for model, fields in rates.items():
        if not isinstance(model, str) or not isinstance(fields, dict) or not required <= set(fields):
            raise ValueError("token種別が不足しています")
        if set(fields) - (required | {"cache_write_input_1h"}):
            raise ValueError("未対応の価格条件です")
        for value in fields.values():
            if isinstance(value, bool):
                raise ValueError("bool単価")
            value = Decimal(str(value))
            if not value.is_finite() or value < 0:
                raise ValueError("不正な単価")
    before = current["rates"]
    changes = []
    for model in sorted(set(before) | set(rates)):
        for field in sorted(set(before.get(model, {})) | set(rates.get(model, {}))):
            old, new = before.get(model, {}).get(field), rates.get(model, {}).get(field)
            if old is None or new is None or Decimal(str(old)) != Decimal(str(new)):
                changes.append({"model": model, "field": field, "current": old, "candidate": new})
    return changes


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", help="公式資料を確認して用意した候補JSON。省略時は現行表を出力")
    args = parser.parse_args(argv)
    try:
        from rebuild_session import load_saver
        current = catalog(load_saver("claude"))
        if args.candidate:
            candidate = json.loads(Path(args.candidate).read_text(encoding="utf-8"))
            changes = price_diff(current, candidate)
            print(json.dumps({"current_version": current["version"], "changes": changes}, ensure_ascii=False, indent=2))
            return int(bool(changes))
        print(json.dumps(current, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, ArithmeticError) as error:
        print("価格検査失敗: " + type(error).__name__, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
