#!/usr/bin/python3
"""Claudeの公式OTLP JSONログとtranscriptのUsageをローカルで照合する。"""
import argparse
from decimal import Decimal, InvalidOperation
import json
import io
from pathlib import Path
import sys

TOKEN_FIELDS = {"input_tokens": "input_tokens", "output_tokens": "output_tokens",
                "cache_read_tokens": "cache_read_input_tokens",
                "cache_creation_tokens": "cache_creation_input_tokens"}


def json_documents(path):
    text = Path(path).read_text(encoding="utf-8")
    try:
        value = json.loads(text)
    except ValueError:
        for number, line in enumerate(io.StringIO(text, newline=None), 1):
            if line.strip():
                try:
                    yield json.loads(line)
                except ValueError:
                    raise ValueError("不正なJSON行: %d" % number)
    else:
        yield value


def scalar(value):
    if isinstance(value, dict):
        for key in ("stringValue", "intValue", "doubleValue", "boolValue"):
            if key in value:
                return value[key]
        return None
    return value


def attributes(value):
    if isinstance(value, dict):
        return {key: scalar(item) for key, item in value.items()}
    if isinstance(value, list):
        return {item["key"]: scalar(item.get("value")) for item in value
                if isinstance(item, dict) and isinstance(item.get("key"), str)}
    return {}


def event_attributes(document):
    """OTLP ExportLogsServiceRequest、配列、正規化済みイベントJSONに対応。"""
    if isinstance(document, list):
        for item in document:
            yield from event_attributes(item)
    elif isinstance(document, dict) and "resourceLogs" in document:
        if not isinstance(document["resourceLogs"], list):
            raise ValueError("resourceLogs must be an array")
        for resource in document["resourceLogs"]:
            if not isinstance(resource, dict) or not isinstance(resource.get("resource", {}), dict):
                raise ValueError("invalid OTLP resource")
            common = attributes(resource.get("resource", {}).get("attributes"))
            if not isinstance(resource.get("scopeLogs", []), list):
                raise ValueError("scopeLogs must be an array")
            for scope in resource.get("scopeLogs", []):
                if not isinstance(scope, dict) or not isinstance(scope.get("logRecords", []), list):
                    raise ValueError("invalid OTLP scope")
                for record in scope.get("logRecords", []):
                    if not isinstance(record, dict):
                        raise ValueError("invalid OTLP record")
                    attrs = {**common, **attributes(record.get("attributes"))}
                    attrs.setdefault("event.name", record.get("eventName") or scalar(record.get("body")))
                    yield attrs
    elif isinstance(document, dict):
        yield {**attributes(document), **attributes(document.get("attributes"))}


def nonnegative(value):
    if isinstance(value, bool):
        raise ValueError("boolは数値として扱いません")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError("不正な数値")
    if not number.is_finite() or number < 0:
        raise ValueError("非有限または負の数値")
    return number


def telemetry_requests(path, session_id):
    result = {}
    skipped = 0
    for document in json_documents(path):
        for attrs in event_attributes(document):
            if attrs.get("event.name") not in ("api_request", "claude_code.api_request"):
                continue
            if attrs.get("session.id") != session_id:
                continue
            request_id = attrs.get("request_id")
            if not isinstance(request_id, str) or not request_id:
                skipped += 1
                continue
            tokens = {}
            for field in TOKEN_FIELDS:
                number = nonnegative(attrs.get(field))
                if number != number.to_integral_value():
                    raise ValueError("token数が整数ではありません")
                tokens[field] = int(number)
            cost = attrs.get("cost_usd")
            cost = nonnegative(cost) if cost is not None else nonnegative(attrs.get("cost_usd_micros")) / 1_000_000
            # 同じrequestの再配送は上書きし、api_errorやcounterは加算しない。
            result[request_id] = {**tokens, "cost_usd": str(cost), "model": attrs.get("model")}
    return result, skipped


def transcript_requests(path, session_id, include_subagents=False, module=None):
    # import時のログ初期化を避けるためrebuildの読取専用loaderを使う。
    if module is None:
        from rebuild_session import load_saver
        module = load_saver("claude")
    paths = [Path(path)]
    if include_subagents:
        base = Path(path).with_suffix("") / "subagents"
        if base.is_dir() and not base.is_symlink():
            paths.extend(sorted(base.rglob("agent-*.jsonl")))
    messages = {}
    skipped = 0
    for source in paths:
        if source.is_symlink():
            continue
        if source != paths[0]:
            try:
                source.resolve().relative_to(base.resolve())
            except ValueError:
                continue
        for document in json_documents(source):
            if not isinstance(document, dict) or document.get("type") != "assistant":
                continue
            if document.get("sessionId") != session_id:
                continue
            if source == paths[0] and document.get("isSidechain"):
                continue
            message = document.get("message")
            if not isinstance(message, dict) or not isinstance(message.get("usage"), dict):
                continue
            request_id = document.get("requestId")
            if not isinstance(request_id, str) or not request_id:
                skipped += 1
                continue
            usage = module._claude_usage_record(message)
            if usage is None:
                raise ValueError("不正なtranscript Usage")
            raw = message["usage"]
            messages[request_id] = {field: raw.get(source_field, 0) for field, source_field in TOKEN_FIELDS.items()}
            messages[request_id]["cost_usd"] = str(usage["cost_usd"]) if "cost_usd" in usage else None
            messages[request_id]["model"] = message.get("model")
    return messages, skipped


def compare(telemetry, transcript, *, tolerance=Decimal("0.000001")):
    differences = []
    cost_unavailable = []
    for request_id in sorted(set(telemetry) & set(transcript)):
        first, second = telemetry[request_id], transcript[request_id]
        for field in (*TOKEN_FIELDS, "model"):
            if first[field] != second[field]:
                differences.append({"request_id": request_id, "field": field,
                                    "telemetry": first[field], "transcript": second[field]})
        if second["cost_usd"] is None:
            cost_unavailable.append(request_id)
        elif abs(Decimal(first["cost_usd"]) - Decimal(second["cost_usd"])) > tolerance:
            differences.append({"request_id": request_id, "field": "cost_usd",
                                "telemetry": first["cost_usd"], "transcript": second["cost_usd"]})
    return {"matched_requests": len(set(telemetry) & set(transcript)), "differences": differences,
            "only_telemetry": sorted(set(telemetry) - set(transcript)),
            "only_transcript": sorted(set(transcript) - set(telemetry)), "cost_unavailable": cost_unavailable}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--telemetry", required=True, help="OTLP JSON、JSON配列、またはイベントJSONL")
    parser.add_argument("--transcript", required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--include-subagents", action="store_true")
    parser.add_argument("--price-version", help="指定した登録価格版で比較")
    args = parser.parse_args(argv)
    try:
        from rebuild_session import load_saver
        module = load_saver("claude")
        with module.usage_price_version(args.price_version or module.PRICE_VERSION):
            observed, skipped_events = telemetry_requests(args.telemetry, args.session_id)
            recorded, skipped_messages = transcript_requests(args.transcript, args.session_id, args.include_subagents, module)
        result = compare(observed, recorded)
        result.update(unidentified_events=skipped_events, unidentified_messages=skipped_messages,
                      price_version=args.price_version or module.PRICE_VERSION)
    except (OSError, ValueError, TypeError, KeyError) as error:
        print("照合失敗: " + type(error).__name__, file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return int(not result["matched_requests"] or bool(result["differences"] or result["only_telemetry"]
        or result["only_transcript"] or result["cost_unavailable"] or skipped_events or skipped_messages))


if __name__ == "__main__":
    sys.exit(main())
