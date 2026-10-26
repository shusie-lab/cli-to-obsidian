#!/usr/bin/python3
"""CodexのOTLP JSONとtranscriptのリクエストUsageを読み取り専用で照合する。"""
import argparse
from decimal import Decimal
import json
from pathlib import Path
import sys

from compare_claude_telemetry import attributes, json_documents, nonnegative, scalar


FIELDS = {"input_tokens": "input_token_count", "cached_input_tokens": "cached_token_count",
          "cache_write_input_tokens": "cache_write_token_count", "output_tokens": "output_token_count",
          "reasoning_output_tokens": "reasoning_token_count"}


def events(document):
    """OTLPログの元timestampとspanを保持して再配送を識別する。"""
    if isinstance(document, list):
        for item in document:
            yield from events(item)
    elif isinstance(document, dict) and "resourceLogs" in document:
        resources = document["resourceLogs"]
        if not isinstance(resources, list):
            raise ValueError("resourceLogs must be an array")
        for resource in resources:
            if not isinstance(resource, dict) or not isinstance(resource.get("resource", {}), dict):
                raise ValueError("invalid OTLP resource")
            common = attributes(resource.get("resource", {}).get("attributes"))
            scopes = resource.get("scopeLogs", [])
            if not isinstance(scopes, list):
                raise ValueError("scopeLogs must be an array")
            for scope in scopes:
                if not isinstance(scope, dict) or not isinstance(scope.get("logRecords", []), list):
                    raise ValueError("invalid OTLP scope")
                for record in scope.get("logRecords", []):
                    if not isinstance(record, dict):
                        raise ValueError("invalid OTLP log record")
                    attrs = {**common, **attributes(record.get("attributes"))}
                    attrs.setdefault("event.name", record.get("eventName") or attrs.get("otel.name") or scalar(record.get("body")))
                    for key in ("timeUnixNano", "traceId", "spanId"):
                        if key in record:
                            attrs[key] = record[key]
                    yield attrs
    elif isinstance(document, dict):
        yield {**document, **attributes(document.get("attributes"))}
    else:
        raise ValueError("JSON object required")


def token_counts(raw, mapping):
    result = {}
    for field, source in mapping.items():
        if source not in raw:
            raise ValueError("missing token field: " + source)
        number = nonnegative(raw[source])
        if number != number.to_integral_value():
            raise ValueError("non-integer token field: " + source)
        result[field] = int(number)
    if (result["cached_input_tokens"] + result["cache_write_input_tokens"] > result["input_tokens"]
            or result["reasoning_output_tokens"] > result["output_tokens"]):
        raise ValueError("inconsistent token categories")
    result["total_tokens"] = result["input_tokens"] + result["output_tokens"]
    return result


def telemetry_records(path, session_ids, module):
    result, seen, unidentified = [], {}, 0
    for document in json_documents(path):
        for attrs in events(document):
            if attrs.get("event.name") not in ("codex.sse_event", "codex.websocket_event"):
                continue
            if attrs.get("conversation.id") not in session_ids:
                continue
            if attrs.get("event.kind", attrs.get("kind")) != "response.completed":
                continue
            success = attrs.get("success", True)
            if success in (False, "false", "False", 0):
                continue
            if success not in (True, "true", "True", 1):
                raise ValueError("invalid success field")
            usage = token_counts(attrs, FIELDS)
            model = attrs.get("model")
            if not isinstance(model, str) or not model:
                raise ValueError("model is missing")
            response_id = attrs.get("response_id") or attrs.get("response.id")
            response_id = response_id if isinstance(response_id, str) and response_id else None
            session = attrs["conversation.id"]
            row = {"session_id": session, "response_id": response_id, "turn_id": attrs.get("turn.id"),
                   "model": module.reference_price_model(model), "usage": usage}
            # response IDがない場合も、元timestampを持つ同じOTLPイベントの再配送は一回にする。
            identity = (("response", session, response_id) if response_id else
                        ("event", session, str(attrs["timeUnixNano"]), str(attrs.get("traceId", "")),
                         str(attrs.get("spanId", ""))) if attrs.get("timeUnixNano") else None)
            if identity is None:
                unidentified += 1
            else:
                if identity in seen:
                    if seen[identity] != row:
                        raise ValueError("conflicting duplicate telemetry event")
                    continue
                seen[identity] = row
            result.append(row)
    return result, unidentified


def transcript_records(path, session_id, include_subagents, module):
    path = Path(path)
    paths = [path] + (module._find_subagent_transcripts(path, session_id) if include_subagents else [])
    result, session_ids, seen = [], {session_id}, set()
    for source in paths:
        if source.is_symlink():
            raise ValueError("symlink transcript is not supported")
        snapshot = module.read_jsonl_snapshot(source)
        if snapshot["malformed_rows"]:
            raise ValueError("malformed transcript rows")
        for _, row in snapshot["rows"]:
            if row.get("type") == "session_meta" and isinstance(row.get("payload"), dict):
                value = row["payload"].get("id")
                if source == path and value != session_id:
                    raise ValueError("transcript session ID mismatch")
                if isinstance(value, str) and value:
                    session_ids.add(value)
                break
        parsed = {row["line_number"]: row for row in module._read_usage_records(source, session_id, snapshot)}
        for number, row in snapshot["rows"]:
            payload = row.get("payload")
            if row.get("type") != "token_usage_record" or not isinstance(payload, dict):
                continue
            if payload.get("session_id") != session_id:
                continue
            if not isinstance(payload.get("usage"), dict):
                raise ValueError("invalid transcript usage")
            usage = token_counts(payload["usage"], {key: key for key in FIELDS})
            if payload["usage"].get("total_tokens") != usage["total_tokens"]:
                raise ValueError("inconsistent transcript total_tokens")
            record = parsed.get(number)
            if record is None:
                raise ValueError("unreadable transcript usage")
            response_id = payload.get("response_id")
            response_id = response_id if isinstance(response_id, str) and response_id else None
            thread_id = payload.get("thread_id") or session_id
            identity = (thread_id, response_id)
            if response_id and identity in seen:
                raise ValueError("duplicate transcript response_id")
            seen.add(identity)
            result.append({"session_id": thread_id, "response_id": response_id, "turn_id": record["turn_id"],
                           "model": module.reference_price_model(record["model"]), "usage": usage})
    return result, session_ids


def grouped(records, group_by, module):
    groups, unidentified = {}, 0
    for record in records:
        identity = record.get("response_id" if group_by == "response" else "turn_id")
        if group_by != "model" and not isinstance(identity, str):
            unidentified += 1
            continue
        key = json.dumps(([record["model"]] if group_by == "model" else
                          [record["session_id"], identity] + ([record["model"]] if group_by == "turn" else [])),
                         ensure_ascii=False)
        target = groups.setdefault(key, {"records": 0, "models": set(), "usage": module._empty_usage_totals(),
                                         "reference_cost_usd": Decimal(0), "unpriced_records": 0})
        target["records"] += 1
        target["models"].add(record["model"])
        for field, number in record["usage"].items():
            target["usage"][field] += number
        cost = module._usage_cost(record["model"], record["usage"])
        if cost is None:
            target["unpriced_records"] += 1
        else:
            target["reference_cost_usd"] += cost
    for value in groups.values():
        value["models"] = sorted(value["models"])
        value["reference_cost_usd"] = (None if value["unpriced_records"] == value["records"]
                                       else str(value["reference_cost_usd"]))
    return groups, unidentified


def compare(observed, recorded, group_by, module):
    first, unknown_first = grouped(observed, group_by, module)
    second, unknown_second = grouped(recorded, group_by, module)
    differences = []
    for key in sorted(set(first) & set(second)):
        for field in ("records", "models", "usage", "reference_cost_usd", "unpriced_records"):
            if first[key][field] != second[key][field]:
                differences.append({"group": key, "field": field,
                                    "telemetry": first[key][field], "transcript": second[key][field]})
    return {"group_by": group_by, "matched_groups": len(set(first) & set(second)),
            "differences": differences, "only_telemetry": sorted(set(first) - set(second)),
            "only_transcript": sorted(set(second) - set(first)),
            "unidentified_events": unknown_first, "unidentified_records": unknown_second,
            "unpriced_groups": sorted({key for groups in (first, second) for key, value in groups.items()
                                       if value["unpriced_records"]}),
            "telemetry": first, "transcript": second}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--telemetry", required=True)
    parser.add_argument("--transcript", required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--include-subagents", action="store_true")
    parser.add_argument("--group-by", choices=("response", "turn", "model"), default="response",
                        help="既定はresponse IDで厳密照合。IDがないログは範囲を明示して集計比較")
    parser.add_argument("--price-version")
    args = parser.parse_args(argv)
    try:
        from rebuild_session import load_saver
        module = load_saver("codex")
        with module.usage_price_version(args.price_version or module.PRICE_VERSION):
            recorded, sessions = transcript_records(args.transcript, args.session_id, args.include_subagents, module)
            observed, no_identity = telemetry_records(args.telemetry, sessions, module)
            result = compare(observed, recorded, args.group_by, module)
            result["undetectable_redeliveries"] = no_identity
            result["price_version"] = module.active_price_version()
            result["cost_basis"] = "reference_only"
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return int(not result["matched_groups"] or bool(result["differences"] or result["only_telemetry"]
                   or result["only_transcript"] or result["unidentified_events"]
                   or result["unidentified_records"] or result["unpriced_groups"] or no_identity))
    except (OSError, ValueError, TypeError, KeyError, ArithmeticError) as error:
        print("照合失敗: " + str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
