#!/usr/bin/python3
"""保存スクリプトと同じディレクトリに置いて使用する会話再構成ツール。"""
from __future__ import annotations

import argparse
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
import difflib
import importlib.util
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile

PROVIDERS = {"codex": "codex", "claude": "claude", "agy": "agy", "opencode": "opencode"}


def load_saver(provider):
    # import時にも通常ログへ書き込まない。hook本体とは別のmoduleオブジェクトを使う。
    name = PROVIDERS[provider]
    logger_name = "obsidian_save_antigravity" if name == "agy" else name + "_obsidian_save"
    logger = logging.getLogger(logger_name)
    handler = logging.NullHandler()
    logger.addHandler(handler)
    try:
        spec = importlib.util.spec_from_file_location(
            "_rebuild_" + name, Path(__file__).resolve().parent / (name + "_save.py")
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        logger.removeHandler(handler)
    module.logger = logging.getLogger("rebuild_session")
    return module


def transcript_info(path, provider, session_id):
    """IDの完全一致を確認し、元履歴のprojectと開始時刻を取得する。"""
    info = {}
    matched = provider == "agy"  # agyのログには会話IDを持たない形式もある。
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict):
                continue
            if provider == "codex":
                payload = row.get("payload") or {}
                if row.get("type") == "session_meta" and isinstance(payload, dict):
                    if payload.get("id") == session_id:
                        matched = True
                        info.update(cwd=payload.get("cwd", ""), created=payload.get("timestamp") or row.get("timestamp"))
            elif provider == "claude" and row.get("sessionId") == session_id:
                matched = True
            if row.get("cwd") and not info.get("cwd"):
                info["cwd"] = row["cwd"]
            if row.get("timestamp") and not info.get("created"):
                info["created"] = row["timestamp"]
    if not matched:
        raise ValueError("transcriptのセッションIDが指定IDと一致しません: " + str(path))
    return info


def find_transcript(provider, session_id, explicit=None):
    if explicit:
        path = Path(explicit).expanduser().resolve(strict=True)
        return path, transcript_info(path, provider, session_id)
    home = Path.home()
    if provider == "agy":
        # IDをパス構成要素として使うのでパストラバーサルを拒否する。
        if session_id in {".", ".."} or Path(session_id).name != session_id:
            raise ValueError("不正なセッションIDです")
        candidates = [home / ".gemini/antigravity-cli/brain" / session_id / ".system_generated/logs/transcript.jsonl"]
    else:
        if provider == "codex":
            base = Path(os.environ.get("CODEX_HOME", str(home / ".codex"))).expanduser()
            roots = [base / "sessions", base / "archived_sessions"]
        else:
            base = Path(os.environ.get("CLAUDE_CONFIG_DIR", str(home / ".claude"))).expanduser()
            roots = [base / "projects"]
        candidates = [path for root in roots if root.exists() for path in root.rglob("*.jsonl")
                      if session_id in path.name]
    matches = []
    for path in candidates:
        if not path.is_file():
            continue
        try:
            info = transcript_info(path, provider, session_id)
        except ValueError:
            continue
        matches.append((path.resolve(), info))
    if len(matches) != 1:
        raise ValueError("履歴が見つからない、または複数あります。--transcript で指定してください（%d件）" % len(matches))
    return matches[0]


def read_opencode(session_id, database=None):
    path = Path(database or os.environ.get("OPENCODE_DATABASE", str(Path.home() / ".local/share/opencode/opencode.db"))).expanduser().resolve(strict=True)
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as connection:
        connection.execute("BEGIN")
        session = connection.execute(
            "SELECT directory, time_created FROM session_v2 WHERE id = ?", (session_id,)
        ).fetchone()
        if session is None:
            raise ValueError("OpenCodeのセッションが見つかりません")
        rows = connection.execute(
            "SELECT id, type, data FROM session_message WHERE session_id = ? "
            "AND type IN ('user', 'assistant') ORDER BY seq", (session_id,)
        ).fetchall()
    messages = []
    for message_id, role, data in rows:
        message = json.loads(data)
        message.update(id=message_id, type=role)
        messages.append(message)
    return {"session_id": session_id, "cwd": session[0] or "", "created": session[1],
            "full_sync": True, "messages": messages}


def frontmatter_value(content, key):
    frontmatter = content.split("---", 2)
    if len(frontmatter) < 3 or frontmatter[0].strip():
        return None
    match = re.search(r"^" + re.escape(key) + r":\s*(.*?)\s*$", frontmatter[1], re.M)
    if not match:
        return None
    raw = match.group(1)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw.strip("'\"")


@contextmanager
def isolated(module, root):
    saved = module.OUTPUT_BASE, module.STATE_DIR, module.SOURCE_NAME
    module.OUTPUT_BASE, module.STATE_DIR = root / "output", root / "state"
    try:
        yield
    finally:
        module.OUTPUT_BASE, module.STATE_DIR, module.SOURCE_NAME = saved


def rebuild(provider, session_id, output_dir=None, transcript=None, database=None, module=None,
            pricing="current", price_version=None, dry_run=False):
    module = module or load_saver(provider)
    if not session_id:
        raise ValueError("セッションIDが空です")
    if provider == "opencode":
        payload = read_opencode(session_id, database)
        info = payload
    else:
        path, info = find_transcript(provider, session_id, transcript)
        if provider == "codex":
            info["cwd"] = module.project_value(info.get("cwd") or "", module.read_transcript_metadata(path))
        payload = {"session_id": session_id, "transcript_path": str(path), "cwd": info.get("cwd") or ""}
        if provider == "agy":
            payload = {"conversationId": session_id, "transcriptPath": str(path),
                       "workspacePaths": [info.get("cwd") or os.getcwd()]}
    normal = output_dir is None
    destination = module.OUTPUT_BASE if normal else Path(output_dir).expanduser().resolve()
    if not normal:
        known_path = module.load_state(session_id).get("output_path")
        if (destination.resolve() == module.OUTPUT_BASE.resolve()
                or known_path and Path(known_path).resolve().parent == destination):
            raise ValueError("通常保存先の更新には --obsidian を指定してください")
    # 通常hookと同じロックの下で生成・置換・state更新を完了する。
    with module.session_lock(session_id) if normal and not dry_run else nullcontext():
        old_state = module.load_state(session_id) if normal else {}
        existing = None
        if normal and old_state.get("output_path"):
            existing = Path(old_state["output_path"])
        if existing is None:
            original_base = module.OUTPUT_BASE
            try:
                module.OUTPUT_BASE = destination
                existing = module.find_existing_md(session_id)
            finally:
                module.OUTPUT_BASE = original_base
        old_content = existing.read_text(encoding="utf-8") if existing and existing.exists() else ""
        if old_content and frontmatter_value(old_content, "session_id") != session_id:
            raise ValueError("置換対象のsession_idが一致しません")
        if not info.get("cwd") and old_state.get("cwd"):
            info["cwd"] = old_state["cwd"]
            if provider != "agy":
                payload["cwd"] = info["cwd"]
            else:
                payload["workspacePaths"] = [info["cwd"]]
        created = frontmatter_value(old_content, "created") or old_state.get("start_time") or old_state.get("created") or info.get("created")
        selected_price = price_version
        if provider != "agy":
            if pricing == "preserve" and selected_price is None:
                selected_price = module.note_price_version(old_content)
                if selected_price is None:
                    raise ValueError("既存ノートに価格版がありません。--price-version を明示してください")
            selected_price = selected_price or module.PRICE_VERSION
        price_context = module.usage_price_version(selected_price) if provider != "agy" else nullcontext()
        with price_context, tempfile.TemporaryDirectory(prefix="obsidian-rebuild-") as temporary:
            with isolated(module, Path(temporary)):
                old_source = frontmatter_value(old_content, "source")
                if old_source:
                    module.SOURCE_NAME = module.resolve_source_name(module.DEFAULT_SOURCE, old_source)
                if provider == "opencode":
                    payload["created"] = created
                    generated = module.handle_payload(payload)
                else:
                    try:
                        start = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
                    except (TypeError, ValueError):
                        start = module.now_jst()
                    if start.tzinfo is None:
                        start = start.replace(tzinfo=module.JST)
                    seed = {"output_path": str(module.create_md_file(session_id, info.get("cwd") or "", start)), "start_time": start.isoformat(),
                            "cwd": payload.get("cwd") or info.get("cwd") or ""}
                    if provider == "claude" and old_state.get("last_cli_failure"):
                        seed["last_cli_failure"] = old_state["last_cli_failure"]
                    if provider == "claude":
                        report = old_state.get("reported_cost") or module.recover_reported_cost(old_content)
                        if report:
                            seed["reported_cost"] = report
                    if provider in {"claude", "agy"}:
                        first, last = module.recover_quota_frontmatter(old_content)
                        seed.update(initial_quota=first or old_state.get("initial_quota", {}),
                                    final_quota=last or old_state.get("final_quota", {}))
                    if provider == "agy":
                        completions = dict(old_state.get("answer_completions") or {})
                        completions.update(module.recover_answer_completions(old_content))
                        if completions:
                            seed["answer_completions"] = completions
                    module.save_state(session_id, seed)
                    quota_function = getattr(module, "retrieve_quota", None)
                    try:
                        if provider == "agy":
                            module.retrieve_quota = lambda: {}  # 過去セッションへ現在quotaを混入させない。
                        if provider == "agy":
                            module.handle_stop_event(payload, observe_completion=False)
                        else:
                            module.handle_stop_event(payload)
                    finally:
                        if provider == "agy":
                            module.retrieve_quota = quota_function
                    generated = Path(module.load_state(session_id)["output_path"])
                if generated is None or not generated.exists():
                    raise ValueError("会話本文を再生成できませんでした")
                content = generated.read_text(encoding="utf-8")
                state = module.load_state(session_id)
                filename = generated.name
                if not state.get("message_count") and not re.search(r"^message_count: [1-9][0-9]*$", content, re.M):
                    raise ValueError("保存対象の会話本文がありません。既存ノートは変更しません")
            target = existing or destination / filename
            backup = None
            if target.exists() and not old_content:
                raise ValueError("同名のファイルが存在します。置換対象を特定できないため保存を中止します")
            if dry_run:
                return rebuild_preview(target, old_content, content, provider, selected_price)
            if old_content:
                backup_dir = target.parent / ".rebuild-backups"
                backup_dir.mkdir(parents=True, exist_ok=True)
                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
                backup = backup_dir / (target.name + "." + stamp + ".bak")
                backup.write_bytes(target.read_bytes())
            if provider == "opencode":
                module.atomic_write(target, content)
            else:
                module.atomic_write_md(target, content)
            if normal:
                state["output_path"] = str(target)
                module.save_state(session_id, state)
    return target, backup


def rebuild_preview(target, before, after, provider, price_version):
    """隔離領域で生成した結果と現在のノートを、書き込まずに比較する。"""
    def frontmatter(content):
        match = re.match(r"\A---\s*\n(.*?)\n---", content, re.S)
        return match.group(1) + "\n" if match else ""
    def fields(content):
        result = {key: frontmatter_value(content, key) for key in
                  ("message_count", "modified", "created", "source")}
        report = re.search(r"(?m)^reported_cost:\n((?:  .*\n|\n)*)", frontmatter(content))
        amount = re.search(r"(?m)^  usd: (.+)$", report.group(1)) if report else None
        result["reported_cost_usd"] = amount.group(1) if amount else None
        return result
    def usage_fields(content):
        block = re.search(r"(?m)^usage:\n((?:  .*\n|\n)*)", frontmatter(content))
        if not block:
            return {}
        return {key: value for key, value in re.findall(
            r"(?m)^  (input_tokens|output_tokens|total_tokens|total_cost_usd|price_version): (.+)$",
            block.group(1))}
    diff = "".join(difflib.unified_diff(before.splitlines(keepends=True), after.splitlines(keepends=True),
        fromfile="existing" if before else "empty", tofile="proposed"))
    return {"provider": provider, "target": str(target), "exists": bool(before),
            "changed": before != after, "price_version": price_version,
            "before": {**fields(before), "usage": usage_fields(before)},
            "after": {**fields(after), "usage": usage_fields(after)}, "diff": diff}


def main(argv=None):
    parser = argparse.ArgumentParser(description="セッション全履歴を現在の保存処理・USD単価で再構成")
    parser.add_argument("provider", choices=PROVIDERS)
    parser.add_argument("session_id")
    output = parser.add_mutually_exclusive_group(required=True)
    output.add_argument("--output-dir", help="テスト用Markdown出力先（sourceサブフォルダは追加しない）")
    output.add_argument("--obsidian", action="store_true", help="通常のObsidian保存先・stateを更新")
    parser.add_argument("--transcript", help="JSONLを直接指定（Codex/Claude/Antigravity）")
    parser.add_argument("--database", help="OpenCode v2 SQLiteを直接指定")
    parser.add_argument("--pricing", choices=("current", "preserve"), default="current",
                        help="現在価格、または既存ノートの価格版で再構成")
    parser.add_argument("--price-version", help="明示した登録価格版で再構成（版のない過去ノートにも使用可）")
    parser.add_argument("--dry-run", action="store_true", help="隔離領域で生成し、保存予定の差分を表示して終了")
    args = parser.parse_args(argv)
    if args.provider == "agy" and (args.pricing != "current" or args.price_version):
        parser.error("AntigravityはUSD換算に対応していません")
    if ((args.provider == "opencode" and args.transcript)
            or (args.provider != "opencode" and args.database)):
        parser.error("--transcript/--database の対象が一致しません")
    try:
        result = rebuild(args.provider, args.session_id, args.output_dir, args.transcript, args.database,
                         pricing=args.pricing, price_version=args.price_version, dry_run=args.dry_run)
    except (OSError, ValueError, sqlite3.Error, RuntimeError) as error:
        print("再構成失敗: " + str(error), file=sys.stderr)
        return 1
    if args.dry_run:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        target, backup = result
        print("再構成完了: " + str(target))
        if backup:
            print("置換前バックアップ: " + str(backup))
    return 0


if __name__ == "__main__":
    sys.exit(main())
