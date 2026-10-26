#!/usr/bin/python3
"""保存状態を読むだけの診断。saverをimportせず、ログやロックも作らない。"""
import argparse
import ast
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sys

LOCATIONS = {
    "codex": (".codex/codex-obsidian/state", ".codex/codex-obsidian/scripts/codex_save.py"),
    "claude": (".claude/claude-obsidian/state", ".claude/claude-obsidian/scripts/claude_save.py"),
    "agy": (".gemini/agy-obsidian/state", ".gemini/agy-obsidian/scripts/agy_save.py"),
    "opencode": (".local/state/opencode-obsidian", ".config/opencode/scripts/opencode_save.py"),
    "pi": (".local/state/pi-obsidian", ".pi/agent/scripts/pi_save.py"),
}


def read_object(path):
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("JSON object required")
    return value


def script_identity(path):
    raw = path.read_bytes()
    tree = ast.parse(raw, filename=str(path))
    version = next((node.value.value for node in tree.body if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Constant)
        and any(isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets)), None)
    return {"path": str(path), "version": version, "sha256": hashlib.sha256(raw).hexdigest()}


def writable_parent(path):
    parent = path if path.is_dir() else path.parent
    while not parent.exists() and parent != parent.parent:
        parent = parent.parent
    return parent.is_dir() and os.access(parent, os.W_OK | os.X_OK)


def inspect_session(path, transcript=None):
    result = {"state_path": str(path), "issues": []}
    diagnostic = path.parent / ".diagnostics" / (path.stem + ".status")
    try:
        result["observation"] = read_object(diagnostic)
    except (OSError, ValueError):
        result["observation"] = None
    observation = result["observation"] or {}
    if observation.get("last_outcome") == "failed":
        result["issues"].append("last_save_failed")
    if observation.get("last_outcome") == "skipped" and observation.get("skip_reason") != "unchanged":
        result["issues"].append("last_save_skipped")
    try:
        state = read_object(path)
    except (OSError, ValueError):
        result["issues"].append("invalid_state" if path.exists() else "missing_state")
        return result
    result["cli_failure"] = state.get("last_cli_failure")
    output = state.get("output_path")
    if not isinstance(output, str) or not output:
        result["issues"].append("missing_output_path")
        return result
    output_path = Path(output)
    result["output_path"] = output
    result["output_parent_writable"] = writable_parent(output_path)
    if not result["output_parent_writable"]:
        result["issues"].append("output_not_writable")
    try:
        content = output_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        result["issues"].append("output_unreadable")
        return result
    cursor = state.get("last_line")
    if "last_id" in state:
        match = re.search(r'^<!--\s*last_id:\s*(.*?)\s*-->$', content, re.M)
        result["state_cursor"] = str(state["last_id"])
        result["note_cursor"] = match.group(1) if match else None
        if result["state_cursor"] != result["note_cursor"]:
            result["issues"].append("cursor_mismatch")
    if "payload_hash" in state:
        result["payload_hash"] = state["payload_hash"]
        if not isinstance(state["payload_hash"], str) or not re.fullmatch(r"[0-9a-f]{64}", state["payload_hash"]):
            result["issues"].append("invalid_payload_hash")
    if cursor is not None:
        match = re.search(r'^<!--\s*last_line:\s*(\d+)\s*-->$', content, re.M)
        result["state_cursor"] = cursor
        result["note_cursor"] = int(match.group(1)) if match else None
        if isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < 0:
            result["issues"].append("invalid_cursor")
        elif result["note_cursor"] != cursor:
            result["issues"].append("cursor_mismatch")
        transcript = transcript or observation.get("transcript_path")
        if transcript:
            try:
                lines = list(io.StringIO(Path(transcript).read_text(encoding="utf-8"), newline=None))
                limit = len(lines)
                if lines and not lines[-1].endswith(("\n", "\r")):
                    try:
                        json.loads(lines[-1])
                    except ValueError:
                        limit -= 1
                result["transcript_lines"] = limit
                if isinstance(cursor, int) and not isinstance(cursor, bool):
                    if cursor > limit:
                        result["issues"].append("cursor_beyond_transcript")
                    elif cursor < limit:
                        result["issues"].append("transcript_ahead")
            except (OSError, UnicodeError):
                result["issues"].append("transcript_unreadable")
    return result


def diagnose(provider, state_dir=None, installed=None, source_dir=None, session_id=None, transcript=None):
    home = Path.home()
    default_state, default_script = LOCATIONS[provider]
    state_dir = Path(state_dir or (home / default_state)).expanduser()
    installed = Path(installed or (home / default_script)).expanduser()
    source = Path(source_dir or Path(__file__).resolve().parent) / (provider + "_save.py")
    result = {"provider": provider, "state_dir": str(state_dir), "issues": [], "sessions": []}
    for label, path in (("source", source), ("installed", installed)):
        try:
            result[label] = script_identity(path)
        except (OSError, ValueError, SyntaxError):
            result[label] = {"path": str(path), "version": None, "sha256": None}
            result["issues"].append(label + "_unreadable")
    if result["source"]["sha256"] and result["installed"]["sha256"]:
        if result["source"]["sha256"] != result["installed"]["sha256"]:
            result["issues"].append("installed_differs")
    if not state_dir.is_dir():
        result["issues"].append("state_directory_missing")
        return result
    paths = sorted(set(state_dir.glob("*.json")) | {
        state_dir / (path.stem + ".json") for path in (state_dir / ".diagnostics").glob("*.status")})
    if provider == "opencode":
        # bridgeの取得位置は保存ノートのセッション状態ではない。
        paths = [path for path in paths if path.name != "opencode_v2_cursor.json"]
    if session_id:
        # stateの識別規則だけを再現し、hook本体は実行しない。
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", session_id).strip("._")[:80]
        key = safe if safe and safe == session_id else (safe or "session") + "-" + hashlib.sha256(session_id.encode()).hexdigest()[:12]
        paths = [state_dir / (key + ".json")]
    result["sessions"] = [inspect_session(path, transcript) for path in paths]
    if not paths:
        result["issues"].append("no_saved_sessions")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("provider", choices=LOCATIONS)
    parser.add_argument("--state-dir")
    parser.add_argument("--installed")
    parser.add_argument("--source-dir")
    parser.add_argument("--session-id")
    parser.add_argument("--transcript")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.transcript and not args.session_id:
        parser.error("--transcriptには--session-idを指定してください")
    try:
        result = diagnose(args.provider, args.state_dir, args.installed, args.source_dir, args.session_id, args.transcript)
    except OSError as error:
        print("診断失敗: " + type(error).__name__, file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(args.provider + ": source=" + str(result["source"]["version"]) + " installed=" + str(result["installed"]["version"]))
        print("issues: " + (", ".join(result["issues"]) or "none"))
        for session in result["sessions"]:
            observed = session.get("observation") or {}
            print(session["state_path"] + ": " + (", ".join(session["issues"]) or "OK")
                  + " last_success=" + str(observed.get("last_success_at"))
                  + " last_failure=" + str(observed.get("last_failure_type"))
                  + " skip_reason=" + str(observed.get("skip_reason")))
    return int(bool(result["issues"] or any(session["issues"] for session in result["sessions"])))


if __name__ == "__main__":
    sys.exit(main())
