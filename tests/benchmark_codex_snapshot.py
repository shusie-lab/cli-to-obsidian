#!/usr/bin/python3
"""Stopの解析工程だけを比較する任意ベンチマーク（unittest対象外）。"""
import argparse
import importlib.util
import json
import logging
from pathlib import Path
import statistics
import tempfile
import time
import tracemalloc


def load(path, name):
    logger = logging.getLogger("codex_obsidian_save")
    logger.addHandler(logging.NullHandler())
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run(module, path, before):
    if before:
        metadata = module.read_transcript_metadata(path)
        quota = module.retrieve_quota(path)
        usage = module.retrieve_usage(path)
        preview, _ = module.load_jsonl_messages(path, 0, [])
        new, _ = module.load_jsonl_messages(path, 0, [])
        usage_messages, _ = module.load_jsonl_messages(path, 0, [])
        timing_messages, last = module.load_jsonl_messages(path, 0, [])
        return usage["total_tokens"], len(timing_messages), last
    snapshot = module.read_jsonl_snapshot(path)
    metadata = module.read_transcript_metadata(path, snapshot=snapshot)
    quota = module.retrieve_quota(path, snapshot=snapshot)
    records = module._read_usage_records(path, snapshot=snapshot)
    usage = module._summarize_usage_records(records)
    messages, last = module.load_jsonl_messages(path, 0, [], snapshot=snapshot, usage_records=records)
    return usage["total_tokens"], len(messages), last


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True, help="変更前のcodex_save.py")
    parser.add_argument("--turns", type=int, default=5000)
    args = parser.parse_args()
    if args.turns <= 0:
        parser.error("--turnsは正の整数です")
    results = []
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "benchmark.jsonl"
        with path.open("w", encoding="utf-8") as stream:
            for index in range(args.turns):
                rows = [
                    {"type": "turn_context", "payload": {"turn_id": str(index), "model": "gpt-6-luna"}},
                    {"type": "event_msg", "payload": {"turn_id": str(index), "type": "user_message", "message": "Question " + str(index)}},
                    {"type": "event_msg", "payload": {"turn_id": str(index), "type": "agent_message", "message": "Answer " + str(index)}},
                    {"type": "token_usage_record", "payload": {"turn_id": str(index), "usage": {
                        "input_tokens": 100, "output_tokens": 10, "total_tokens": 110}}}]
                stream.write("".join(json.dumps(row) + "\n" for row in rows))
        for phase, source in (("before", args.baseline), ("after", Path(__file__).resolve().parents[1] / "codex_save.py")):
            module = load(source, "benchmark_" + phase)
            samples = []
            for _ in range(3):
                start = time.perf_counter()
                result = run(module, path, phase == "before")
                samples.append(time.perf_counter() - start)
            tracemalloc.start()
            run(module, path, phase == "before")
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            results.append({"phase": phase, "turns": args.turns, "lines": args.turns * 4,
                "median_seconds": statistics.median(samples), "peak_mib": peak / 1024**2, "result": result})
    print(json.dumps(results, indent=2))
    return int(results[0]["result"] != results[1]["result"])


if __name__ == "__main__":
    raise SystemExit(main())
