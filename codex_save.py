#!/usr/bin/python3
from __future__ import annotations
"""
Codex CLI の会話履歴を Obsidian に自動保存するフックスクリプト。
出力形式: Obsidian AI Exporter 互換（YAMLフロントマター + callout block形式）

対応イベント:
  - Stop : ターン終了時に Markdown ファイルの作成・追記を行う
"""

__version__ = "1.0.0"

import json
import re
import sys
import os
import fcntl
import hashlib
import tempfile
from contextlib import contextmanager
from functools import wraps
from datetime import datetime, timezone, timedelta
from pathlib import Path
import logging
from logging.handlers import RotatingFileHandler

# ============================================================
# 定数
# ============================================================
JST = timezone(timedelta(hours=9))

OBSIDIAN_VAULT = Path(
    os.environ.get(
        "OBSIDIAN_VAULT",
        str(Path.home() / "obsidian")
    )
)
DEFAULT_OUTPUT_DIR = "生成AI/ChatLog"


def resolve_output_dir(value: str | None = None) -> Path:
    """保管庫からの相対保存先を検証し、不正なら既定値を返す。"""
    raw = os.environ.get("OBSIDIAN_OUTPUT_DIR", DEFAULT_OUTPUT_DIR) if value is None else value
    candidate = Path(raw) if str(raw).strip() else Path(DEFAULT_OUTPUT_DIR)
    if candidate.is_absolute() or ".." in candidate.parts:
        return Path(DEFAULT_OUTPUT_DIR)
    return candidate


OBSIDIAN_OUTPUT_DIR = resolve_output_dir()
OUTPUT_BASE = OBSIDIAN_VAULT / OBSIDIAN_OUTPUT_DIR / "codex-cli"

STATE_DIR = Path.home() / ".codex" / "codex-obsidian" / "state"
LOG_FILE = Path.home() / ".codex" / "codex-obsidian" / "log" / "codex_obsidian_save.log"

STATE_RETENTION_DAYS = 30
LAST_LINE_PATTERN = re.compile(r"<!--\s*last_line:\s*(\d+)\s*-->")
AGENT_NAME = "Codex"
SAFE_SESSION_CHARS = re.compile(r"[^A-Za-z0-9_.-]+")
PROJECTLESS_LABEL = "projectless"

# ============================================================
# ロギング
# ============================================================
def setup_logger() -> logging.Logger:
    logger = logging.getLogger("codex_obsidian_save")
    if os.environ.get("DEBUG"):
        logger.setLevel(logging.DEBUG)
    else:
        logger.setLevel(logging.INFO)

    if logger.handlers:
        return logger

    fmt = logging.Formatter("[%(asctime)s] %(levelname)s %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")
    file_log_error = None
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            LOG_FILE,
            maxBytes=5 * 1024 * 1024,  # 5MB
            backupCount=3,
            encoding="utf-8",
        )
        handler.setFormatter(fmt)
        logger.addHandler(handler)
    except OSError as e:
        file_log_error = e

    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setFormatter(fmt)
    logger.addHandler(stderr_handler)
    if file_log_error is not None:
        logger.warning(f"ファイルログを初期化できないためstderrのみ使用します: {file_log_error}")
    return logger


logger = setup_logger()

# ============================================================
# 状態（State）ファイル操作
# ============================================================
def safe_session_key(session_id: str) -> str:
    """session_id をパストラバーサルできないファイル名へ変換する。"""
    raw = str(session_id)
    safe = SAFE_SESSION_CHARS.sub("_", raw).strip("._")[:80]
    if safe and safe == raw:
        return safe
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
    return f"{safe or 'session'}-{digest}"


def state_path(session_id: str) -> Path:
    return STATE_DIR / f"{safe_session_key(session_id)}.json"


def atomic_write_text(path: Path, content: str) -> None:
    """同じディレクトリの一時ファイルを rename して文字列を保存する。"""
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
            tmp.write(content)
            tmp.flush()
            os.fsync(tmp.fileno())
            tmp_path = Path(tmp.name)
        if path.exists():
            os.chmod(tmp_path, path.stat().st_mode & 0o7777)
        os.replace(tmp_path, path)
        tmp_path = None
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)


@contextmanager
def session_lock(session_id: str):
    """同一セッションの hook 実行を直列化する。"""
    lock_dir = STATE_DIR / ".locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / f"{safe_session_key(session_id)}.lock"
    with open(lock_path, "a", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def session_locked(input_key: str):
    """hook input 内の session id を使って関数を排他実行する。"""
    def decorator(func):
        @wraps(func)
        def wrapper(hook_input: dict):
            session_id = hook_input.get(input_key, "")
            if not session_id:
                return func(hook_input)
            with session_lock(str(session_id)):
                return func(hook_input)
        return wrapper
    return decorator


def load_state(session_id: str) -> dict:
    path = state_path(session_id)
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.info(f"stateファイルの読み込みに失敗しました（破損の可能性）: {path}: {e}")
            return {}
    return {}


def save_state(session_id: str, state: dict) -> None:
    """atomicに保存（temp→rename）"""
    path = state_path(session_id)
    atomic_write_text(path, json.dumps(state, ensure_ascii=False, indent=2))


def cleanup_old_states() -> None:
    """STATE_RETENTION_DAYS日以上前のstateファイルを削除"""
    if not STATE_DIR.exists():
        return
    cutoff = datetime.now(JST) - timedelta(days=STATE_RETENTION_DAYS)
    removed = 0
    for f in STATE_DIR.glob("*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            start_time_str = data.get("start_time", "")
            if not start_time_str:
                continue
            start = datetime.fromisoformat(start_time_str)
            if start.tzinfo is None:
                start = start.replace(tzinfo=JST)
            if start < cutoff:
                f.unlink()
                removed += 1
        except Exception as e:
            logger.info(f"stateファイルのクリーンアップ中にスキップ: {f.name}: {e}")
    if removed > 0:
        logger.info(f"古いstateファイル {removed} 件を削除しました")

# ============================================================
# ユーティリティ
# ============================================================
def read_stdin() -> dict:
    try:
        data = sys.stdin.read()
        if data.strip():
            return json.loads(data)
    except Exception as e:
        logger.info(f"stdinの読み込みまたはJSONパースに失敗しました: {e}")
    return {}


def now_jst() -> datetime:
    return datetime.now(JST)


def format_iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def yaml_quote(value: object) -> str:
    """JSON互換の引用形式で YAML 文字列を安全に生成する。"""
    return json.dumps(str(value), ensure_ascii=False)


def safe_filename_component(value: str) -> str:
    """プロジェクト名を単一の安全なファイル名要素へ変換する。"""
    return re.sub(r"[\x00-\x1f/:*?\[\]\\]", "_", value).strip()[:80]


def sanitize_markdown(text: str) -> str:
    """
    会話本文の Markdown 記述（リンクや強調等）を維持するため、
    過剰なエスケープは行わずテキストをそのまま返します。
    """
    return text if text else ""


def escape_markdown_heading(text: str) -> str:
    """見出し内でMarkdown構文として解釈される文字をエスケープする。"""
    return re.sub(r"([\\`*_{}\[\]()#+\-.!|<>~])", r"\\\1", text)


def callout_lines(text: str) -> str:
    """テキストをcallout block内の行形式に変換（> プレフィックス付き）。
    - HTML タグ誤認識による Callout の表示破損を防ぐため < は &lt; にエスケープする。
    - 行頭（インデント空白含む）の > は callout の入れ子を避けるため &gt; にエスケープする。
    """
    def _escape_line(line: str) -> str:
        # 1. HTML タグ解釈の誤作動を防ぐため < を &lt; に置換
        line = line.replace("<", "&lt;")

        # 2. 行頭（インデント含む）の > を &gt; に置換
        stripped = line.lstrip()
        if stripped.startswith(">"):
            indent = line[: len(line) - len(stripped)]
            return indent + "&gt;" + stripped[1:]
        return line

    return "\n".join(f"> {_escape_line(line)}" if line else ">" for line in text.split("\n"))


def format_message_time(timestamp: object) -> str:
    """ISO timestamp を JST の共通メタデータ行へ変換する。"""
    if not timestamp:
        return ""
    try:
        value = str(timestamp)
        dt = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return f"> <small>⏱ {dt.astimezone(JST):%Y-%m-%d %H:%M:%S}</small>\n>\n"
    except (TypeError, ValueError) as e:
        logger.warning(f"timestampのパース失敗: {timestamp}: {e}")
        return ""


def user_heading(text: str) -> str:
    """ユーザー発言の先頭非空行から安全な共通見出しを作る。"""
    first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
    first_line = first_line.replace("\\\\", "\\")
    for escaped, plain in ((r"\[", "["), (r"\]", "]"), (r"\_", "_"), (r"\*", "*")):
        first_line = first_line.replace(escaped, plain)
    if len(first_line) > 40:
        first_line = first_line[:40] + "..."
    escaped = escape_markdown_heading(first_line)
    return f"# User: {escaped}\n" if escaped else "# User\n"


# ============================================================
# Quota 取得ユーティリティ
# ============================================================
def quota_window_key(window_minutes: int) -> str:
    """Quota の時間枠（分）を表示・保存用のキーへ変換する。"""
    if window_minutes == 7 * 24 * 60:
        return "weekly"
    if window_minutes % (24 * 60) == 0:
        return f"{window_minutes // (24 * 60)}d"
    if window_minutes % 60 == 0:
        return f"{window_minutes // 60}h"
    return f"{window_minutes}m"


def parse_quota_data(rate_limits: dict) -> dict:
    """token_count.rate_limits を agy_save.py と共通の quota 形式へ変換する。"""
    if not isinstance(rate_limits, dict):
        return {}

    windows = {}
    for limit_name in ("primary", "secondary"):
        limit = rate_limits.get(limit_name)
        if not isinstance(limit, dict):
            continue

        try:
            window_minutes = int(limit.get("window_minutes"))
            used_percent = float(limit.get("used_percent"))
        except (TypeError, ValueError):
            continue
        if window_minutes <= 0:
            continue

        reset_jst = ""
        try:
            reset_timestamp = int(limit.get("resets_at"))
            reset_jst = datetime.fromtimestamp(reset_timestamp, tz=timezone.utc).astimezone(JST).strftime(
                "%Y-%m-%d %H:%M"
            )
        except (TypeError, ValueError, OSError, OverflowError):
            pass

        windows[quota_window_key(window_minutes)] = {
            "remaining": max(0.0, min(100.0, 100.0 - used_percent)),
            "reset": reset_jst,
            "window_minutes": window_minutes,
        }

    if not windows:
        return {}

    return {"codex": windows}


def retrieve_quota(jsonl_path: Path, start_line: int = 0) -> dict | None:
    """start_line より後ろにある最後の token_count から quota を取得する。"""
    latest_quota = None
    try:
        with open(jsonl_path, "r", encoding="utf-8") as f:
            for current_line, line in enumerate(f, start=1):
                if current_line <= start_line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue

                payload = data.get("payload", {})
                if data.get("type") != "event_msg" or payload.get("type") != "token_count":
                    continue

                quota = parse_quota_data(payload.get("rate_limits", {}))
                if quota:
                    latest_quota = quota
    except Exception as e:
        logger.warning(f"Quota情報の読み込みに失敗: {jsonl_path}: {e}")

    return latest_quota


# ============================================================
# transcript（JSONL）のパース
# ============================================================
def transcript_source_label(raw_source: str, originator: str = "") -> str:
    """Codex transcript の source を Obsidian の source 名へ変換する。"""
    source = str(raw_source or "").lower()
    if source == "cli":
        return "codex-cli"
    if source in {"vscode", "app", "desktop"} or "desktop" in str(originator).lower():
        return "codex-app"
    return "codex-cli"


def read_transcript_metadata(jsonl_path: Path) -> dict:
    """session_meta / turn_context から保存に必要なメタデータを取得する。"""
    metadata = {}
    try:
        with open(jsonl_path, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if data.get("type") == "session_meta":
                    payload = data.get("payload", {})
                    if isinstance(payload, dict):
                        if payload.get("source"):
                            metadata["source"] = payload["source"]
                        if payload.get("originator"):
                            metadata["originator"] = payload["originator"]
                        if payload.get("thread_source"):
                            metadata["thread_source"] = payload["thread_source"]
                elif data.get("type") == "turn_context":
                    payload = data.get("payload", {})
                    if not isinstance(payload, dict):
                        continue
                    if payload.get("model"):
                        metadata["model"] = payload["model"]
                    workspace_roots = payload.get("workspace_roots")
                    if isinstance(workspace_roots, list):
                        metadata["workspace_roots"] = workspace_roots
                    effort = payload.get("effort") or payload.get("reasoning_effort")
                    if effort:
                        metadata["effort"] = effort
    except Exception as e:
        logger.warning(f"transcriptメタデータの読み込みに失敗: {jsonl_path}: {e}")
    return metadata


def is_projectless_session(cwd: str, transcript_metadata: dict) -> bool:
    """ChatGPTアプリが作るプロジェクトなし用の一時workspaceを判定する。

    プロジェクトなしの場合、アプリは共通の Codex workspace root と、日付配下の
    一時 cwd を workspace_roots に並べる。通常のプロジェクトでは先頭 root と
    cwd が一致するため、この差を使って判定する。
    """
    raw_source = str(transcript_metadata.get("source", "")).lower()
    originator = str(transcript_metadata.get("originator", "")).lower()
    is_app = raw_source in {"vscode", "app", "desktop"} or "desktop" in originator
    if not is_app or not cwd:
        return False

    roots = transcript_metadata.get("workspace_roots", [])
    if not isinstance(roots, list) or len(roots) < 2:
        return False
    try:
        cwd_path = Path(cwd).resolve()
        root_path = Path(str(roots[0])).resolve()
        return cwd_path != root_path and root_path in cwd_path.parents
    except (OSError, ValueError, TypeError):
        return False


def project_value(cwd: str, transcript_metadata: dict) -> str:
    """frontmatter とファイル名に使うプロジェクト値を返す。"""
    if is_projectless_session(cwd, transcript_metadata):
        return PROJECTLESS_LABEL
    return cwd


def format_model_name(model_name: str, effort: str = "") -> str:
    """モデル名に reasoning effort を付加して表示用の名前へ変換する。"""
    model = str(model_name or "").strip()
    effort_value = str(effort or "").strip()
    if not model or not effort_value:
        return model
    if model.endswith(f"-{effort_value}"):
        return model
    return f"{model}-{effort_value}"


def load_jsonl_messages(jsonl_path: Path, start_line: int) -> tuple[list[dict], int]:
    """
    JSONLファイルを読み込み、start_line (1-indexed) より後ろの行を処理する。
    event_msg 内の user_message / agent_message を抽出して返す。
    読み込んだ最終行番号 (1-indexed) も返す。
    """
    messages = []
    current_line = 0
    current_model = ""
    current_effort = ""
    try:
        with open(jsonl_path, "r", encoding="utf-8") as f:
            for line in f:
                current_line += 1

                raw_line = line
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    if data.get("type") == "turn_context":
                        context = data.get("payload", {})
                        if isinstance(context, dict):
                            current_model = context.get("model", "") or current_model
                            current_effort = (
                                context.get("effort")
                                or context.get("reasoning_effort")
                                or current_effort
                            )

                    if current_line <= start_line:
                        continue

                    if data.get("type") == "event_msg":
                        payload = data.get("payload", {})
                        p_type = payload.get("type")
                        message_type = p_type
                        message = payload.get("message", "")
                        item = payload.get("item", {})

                        # Codex CLI 0.147.0 以降は、会話メッセージを
                        # item_completed の item.type/content に記録する。
                        if p_type == "item_completed" and isinstance(item, dict):
                            item_type = item.get("type")
                            message_type = {
                                "UserMessage": "user_message",
                                "AgentMessage": "agent_message",
                            }.get(item_type, "")
                            content = item.get("content", [])
                            if isinstance(content, list):
                                message = "\n".join(
                                    str(part.get("text", ""))
                                    for part in content
                                    if (
                                        isinstance(part, dict)
                                        and str(part.get("type", "")).lower() == "text"
                                    )
                                )

                        if message_type in ("user_message", "agent_message"):
                            messages.append({
                                "line_number": current_line,
                                "type": message_type,
                                "message": message,
                                "timestamp": data.get("timestamp", ""),
                                "model": data.get("model", "") or payload.get("model", "") or current_model,
                                "effort": (
                                    data.get("effort")
                                    or payload.get("effort")
                                    or data.get("reasoning_effort")
                                    or payload.get("reasoning_effort")
                                    or current_effort
                                ),
                                "quota": None,
                            })
                except json.JSONDecodeError as e:
                    if not raw_line.endswith(("\n", "\r")):
                        logger.debug(f"書き込み途中のJSONL最終行を次回へ延期: line {current_line}: {e}")
                        return messages, current_line - 1
                    logger.debug(f"JSONL parse error at line {current_line}: {e}")
                    continue
    except Exception as e:
        logger.warning(f"JSONL読み込み失敗: {jsonl_path}: {e}")
        return [], start_line

    return messages, current_line

# ============================================================
# Markdownファイル生成・操作
# ============================================================
def format_reset_time(reset_jst: str) -> str:
    """JST文字列 'YYYY-MM-DD HH:MM' からリセットまでの残り時間を返す。"""
    if not reset_jst:
        return ""
    try:
        dt = datetime.strptime(reset_jst, "%Y-%m-%d %H:%M").replace(tzinfo=JST)
        diff = int((dt - datetime.now(JST)).total_seconds())
    except Exception:
        return reset_jst

    if diff <= 0:
        return "now"
    minutes = (diff + 59) // 60
    if minutes < 60:
        return f"{minutes}m"
    hours, mins = divmod(minutes, 60)
    if hours >= 24:
        days, rem_hours = divmod(hours, 24)
        return f"{days}d {rem_hours}h" if rem_hours else f"{days}d"
    return f"{hours}h {mins}m" if mins else f"{hours}h"


def quota_window_sort_key(item: tuple[str, dict]) -> tuple[int, int]:
    key, data = item
    preferred_order = {"weekly": 0, "5h": 1}
    return preferred_order.get(key, 2), -int(data.get("window_minutes", 0))


def quota_window_label(key: str) -> str:
    return "W" if key == "weekly" else key


def quota_consumption(initial_quota: dict | None, final_quota: dict | None) -> float | None:
    """セッション中に消費した weekly quota を返す。"""
    initial = initial_quota.get("codex", {}).get("weekly", {}).get("remaining") if initial_quota else None
    final = final_quota.get("codex", {}).get("weekly", {}).get("remaining") if final_quota else initial
    if initial is None or final is None:
        return None
    return round(initial - final, 2)


def build_quota_section(initial_quota: dict | None, final_quota: dict | None) -> str:
    """agy_save.py と同形式の Quota セクションを組み立てる。"""
    if not initial_quota and not final_quota:
        return "📊 **Quota**: N/A\n"

    initial_windows = initial_quota.get("codex", {}) if initial_quota else {}
    final_windows = final_quota.get("codex", {}) if final_quota else {}
    all_windows = {**initial_windows, **final_windows}
    if not all_windows:
        return "📊 **Quota**: N/A\n"

    parts = []
    changed = False
    for key, _ in sorted(all_windows.items(), key=quota_window_sort_key):
        initial = initial_windows.get(key, {}).get("remaining")
        final = final_windows.get(key, {}).get("remaining")
        if initial is None and final is None:
            continue
        reset = final_windows.get(key, {}).get("reset") or initial_windows.get(key, {}).get("reset", "")
        reset_remaining = format_reset_time(reset)
        reset_text = f" (⟳ {reset_remaining})" if reset_remaining else ""

        if initial is not None and final is not None and abs(initial - final) > 1e-5:
            value = f"{initial:.1f}% ➔ {final:.1f}%"
            changed = True
        else:
            displayed = initial if initial is not None else final
            value = f"{displayed:.1f}%"
        parts.append(f"{quota_window_label(key)}: {value}{reset_text}")

    if not parts:
        return "📊 **Quota**: N/A\n"

    quota_text = " / ".join(parts)
    if changed:
        line = f"- **Codex**: {quota_text}"
    else:
        line = f"- <small>Codex: {quota_text}</small>"
    return f"📊 **Quota**:\n{line}\n"


def build_frontmatter(
    session_id: str,
    cwd: str,
    start_dt: datetime,
    message_count: int = 0,
    source: str = "codex-cli",
    initial_quota: dict | None = None,
    final_quota: dict | None = None,
) -> str:
    ts = format_iso(start_dt)
    fm_lines = [
        "---",
        f"source: {source}",
        f"session_id: {yaml_quote(session_id)}",
        f"project: {yaml_quote(cwd)}",
        f"created: {yaml_quote(ts)}",
        f"modified: {yaml_quote(ts)}",
        "tags:",
        "  - ai-conversation",
        f"  - {source}",
        f"message_count: {message_count}",
    ]
    consumption = quota_consumption(initial_quota, final_quota)
    if consumption is not None:
        fm_lines.append(f"quota: {consumption:.2f}")
    fm_lines.append("---")
    return "\n".join(fm_lines) + "\n\n"


def create_md_file(
    session_id: str,
    cwd: str,
    start_dt: datetime,
    initial_quota: dict | None = None,
    source: str = "codex-cli",
) -> Path:
    date_dir = start_dt.strftime("%Y%m%d")
    time_prefix = start_dt.strftime("%H%M%S")
    short_id = safe_filename_component(session_id[:8]) if session_id else "unknown"
    short_id = short_id or "unknown"
    project_name = safe_filename_component(Path(cwd).name) if cwd else ""

    output_dir = OUTPUT_BASE
    output_dir.mkdir(parents=True, exist_ok=True)

    if project_name:
        filename = f"{date_dir}_{time_prefix}_{project_name}_{short_id}.md"
    else:
        filename = f"{date_dir}_{time_prefix}_{short_id}.md"

    path = output_dir / filename
    fm = build_frontmatter(
        session_id,
        cwd,
        start_dt,
        message_count=0,
        source=source,
        initial_quota=initial_quota,
        final_quota=initial_quota,
    )
    quota_sec = build_quota_section(initial_quota, initial_quota)
    content = fm + quota_sec + "\n<!-- last_line: 0 -->\n\n"
    atomic_write_md(path, content)
    return path


def find_existing_md(session_id: str) -> Path | None:
    """state消失時に frontmatter の session_id から既存Markdownを探す。"""
    if not OUTPUT_BASE.exists():
        return None
    short_id = safe_filename_component(session_id[:8]) or "unknown"
    expected = {
        f"session_id: {yaml_quote(session_id)}",
        f"session_id: {session_id}",  # 旧形式との互換性
    }
    try:
        candidates = sorted(
            OUTPUT_BASE.glob(f"*_{short_id}.md"),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
    except OSError as e:
        logger.warning(f"既存Markdownの探索に失敗: {e}")
        return None
    for candidate in candidates:
        try:
            content = candidate.read_text(encoding="utf-8")
            frontmatter = content.split("---", 2)[1]
            if any(line in frontmatter.splitlines() for line in expected):
                return candidate
        except (OSError, IndexError, UnicodeError):
            continue
    return None


def read_last_line_from_md(path: Path) -> int | None:
    try:
        content = path.read_text(encoding="utf-8")
        m = LAST_LINE_PATTERN.search(content)
        if m:
            return int(m.group(1))
        logger.debug(f"MDファイルに last_line コメントが見つかりません: {path}")
    except Exception as e:
        logger.warning(f"MDファイルからの last_line 読み取りに失敗: {path}: {e}")
    return None


def atomic_write_md(path: Path, content: str) -> None:
    """atomicに保存（temp→rename）"""
    atomic_write_text(path, content)


def advance_last_line(path: Path, last_line: int) -> None:
    """会話がない行だけでも Markdown 側の cursor を atomic に進める。"""
    content = path.read_text(encoding="utf-8")
    updated, count = LAST_LINE_PATTERN.subn(f"<!-- last_line: {last_line} -->", content, count=1)
    if count:
        atomic_write_md(path, updated)


def is_callout_header_line(line: str) -> bool:
    if line.startswith("> [!"):
        return True
    if line.startswith("> <small>"):
        return True
    if line == ">":
        return True
    return False


def get_last_callout_header(content: str) -> str | None:
    """Markdown コンテンツから最後の Callout ヘッダー文字列を抽出する"""
    lines = content.splitlines()
    header_indices = [i for i, line in enumerate(lines) if line.startswith("> [!")]
    if not header_indices:
        return None

    last_start = header_indices[-1]
    header_lines = []
    for i in range(last_start, len(lines)):
        line = lines[i]
        if is_callout_header_line(line):
            header_lines.append(line)
        else:
            break

    return "\n".join(header_lines).strip() if header_lines else None


# ============================================================
# メッセージ追記
# ============================================================
def get_quota_diff_str(model_name: str, current_quota: dict | None, last_quota: dict | None) -> str:
    """agy_save.py と同形式で、直前の記録からの quota 変化を返す。"""
    if not current_quota or not model_name:
        return ""

    current_windows = current_quota.get("codex", {})
    last_windows = last_quota.get("codex", {}) if last_quota else {}
    if not current_windows:
        return ""

    def format_diff(current: float, last: float | None) -> str:
        if last is None:
            return f"{current:.1f}%"
        diff = current - last
        if diff > 0:
            return f"{current:.1f}(+{diff:.2f})%"
        if diff < 0:
            return f"{current:.1f}({diff:.2f})%"
        return f"{current:.1f}(±0.00)%"

    parts = []
    for key, window in sorted(current_windows.items(), key=quota_window_sort_key):
        current = window.get("remaining")
        if current is None:
            continue
        last = last_windows.get(key, {}).get("remaining")
        parts.append(f"{quota_window_label(key)} {format_diff(current, last)}")

    return f" (Quota: {' / '.join(parts)})" if parts else ""


def append_messages(
    path: Path,
    messages: list[dict],
    modified_dt: datetime,
    model_name: str,
    last_line: int,
    current_quota: dict | None = None,
    last_quota: dict | None = None,
    initial_quota: dict | None = None,
    effort: str = "",
) -> tuple[int, bool]:
    content = path.read_text(encoding="utf-8")

    blocks = []
    appended = 0
    last_callout_header = get_last_callout_header(content)

    for msg in messages:
        m_type = msg["type"]
        text = msg["message"]
        m_model = msg.get("model", "") or model_name
        m_effort = msg.get("effort", "") or effort
        display_model = format_model_name(m_model, m_effort)
        if not text.strip():
            continue

        time_part = format_message_time(msg.get("timestamp"))

        if m_type == "user_message":
            # 発言の最初のテキスト行から見出しテキストを抽出
            heading = user_heading(text)
            callout_header = f"> [!QUESTION] User\n{time_part}".strip()

            if callout_header == last_callout_header:
                blocks.append(f"---\n\n{callout_lines(sanitize_markdown(text))}\n\n")
            else:
                blocks.append(f"{heading}> [!QUESTION] User\n{time_part}{callout_lines(sanitize_markdown(text))}\n\n")

            last_callout_header = callout_header
            appended += 1

        elif m_type == "agent_message":
            # 使用されたモデル名の取得
            m_quota = msg.get("quota")
            if not m_quota and display_model and current_quota:
                m_quota = get_quota_diff_str(display_model, current_quota, last_quota)
            quota_part = f"{m_quota}" if m_quota else ""
            meta_part = f"> <small>🤖 {display_model}{quota_part}</small>\n" if display_model else ""
            meta_line = f"> <small>🤖 {display_model}{quota_part}</small>" if display_model else ""
            callout_header = f"> [!NOTE] {AGENT_NAME}\n{meta_line}".strip() if meta_line else f"> [!NOTE] {AGENT_NAME}"

            if callout_header == last_callout_header:
                # Codex のストリーミング結果が複数の agent_message に分割される
                # ことがある。同じ callout の続きは区切り線を入れず、改行を残して連結し、
                # 1つの回答として message_count に加算する。
                fragment = sanitize_markdown(text)
                if blocks:
                    blocks[-1] = blocks[-1].rstrip("\n") + "\n" + fragment + "\n\n"
                else:
                    content = content.rstrip("\n") + "\n"
                    blocks.append(fragment + "\n\n")
            else:
                callout_block = f"> [!NOTE] {AGENT_NAME}\n{meta_part}" if meta_part else f"> [!NOTE] {AGENT_NAME}\n"
                blocks.append(f"{callout_block}\n\n{sanitize_markdown(text)}\n\n")
                appended += 1

            last_callout_header = callout_header

    if not blocks:
        return 0, False

    # 本文追記とメタデータ更新を一括で行い、atomic_write_md 1回で完結させる
    # （2段階書き込みによるクラッシュ時の不整合を防ぐ）
    new_content = content + "\n".join(blocks)

    fm_match = re.match(r"^---\s*\n(.*?)\n---\s*\n", new_content, re.DOTALL)
    if fm_match:
        fm_text = fm_match.group(1)
        body_text = new_content[fm_match.end():]

        fm_lines = fm_text.splitlines()
        new_fm_lines = []
        keys_to_remove = {"modified", "message_count"}
        consumption = quota_consumption(initial_quota, current_quota)
        if consumption is not None:
            keys_to_remove.add("quota")
        skip_list = False
        old_count = 0
        for line in fm_lines:
            if line.startswith("message_count:"):
                try:
                    old_count = int(line.split(":", 1)[1].strip())
                except ValueError:
                    pass
            key_match = re.match(r"^([a-zA-Z0-9_-]+):", line)
            if key_match:
                key = key_match.group(1)
                if key in keys_to_remove:
                    skip_list = True
                    continue
                else:
                    skip_list = False
                    new_fm_lines.append(line)
                    continue
            if line.startswith("  - ") or line.startswith("    - "):
                if skip_list:
                    continue
            else:
                skip_list = False
            new_fm_lines.append(line)

        new_count = old_count + appended
        new_fm_lines.append(f'modified: "{format_iso(modified_dt)}"')
        new_fm_lines.append(f"message_count: {new_count}")
        if consumption is not None:
            new_fm_lines.append(f"quota: {consumption:.2f}")

        new_quota_sec = build_quota_section(initial_quota, current_quota)
        quota_pattern = r"(?m)^📊 \*\*Quota\*\*:(?:\s*N/A)?\n(?:^- .*\n?)*"
        if re.search(quota_pattern, body_text):
            body_text = re.sub(
                quota_pattern,
                new_quota_sec.rstrip("\n") + "\n",
                body_text,
                count=1,
            )
        else:
            body_text = new_quota_sec + "\n" + body_text.lstrip()

        body_text = re.sub(
            r"<!--\s*last_line:.*?-->",
            f"<!-- last_line: {last_line} -->",
            body_text,
        )
        final_content = "---\n" + "\n".join(new_fm_lines) + "\n---\n" + body_text
    else:
        # フロントマターが見つからない場合は本文だけ書き込む
        final_content = new_content

    atomic_write_md(path, final_content)
    return appended, True

# ============================================================
# イベントハンドラ
# ============================================================
@session_locked("session_id")
def handle_stop_event(hook_input: dict) -> None:
    session_id = hook_input.get("session_id", "")
    transcript_path = hook_input.get("transcript_path", "")
    cwd = hook_input.get("cwd", "") or os.getcwd()
    model_name = hook_input.get("model", "")
    effort = hook_input.get("effort", "") or hook_input.get("reasoning_effort", "")

    if not session_id:
        logger.warning("session_id が取得できませんでした")
        return
    if not transcript_path:
        logger.warning("transcript_path が未提供です")
        return

    jsonl_path = Path(transcript_path)
    if not jsonl_path.exists():
        logger.warning(f"JSONLファイルが存在しません: {jsonl_path}")
        return

    transcript_metadata = read_transcript_metadata(jsonl_path)
    source = transcript_source_label(
        hook_input.get("source", "") or transcript_metadata.get("source", ""),
        hook_input.get("originator", "") or transcript_metadata.get("originator", ""),
    )
    model_name = model_name or transcript_metadata.get("model", "")
    effort = effort or transcript_metadata.get("effort", "")
    cwd = project_value(cwd, transcript_metadata)

    state = load_state(session_id)
    is_new_state = not state
    if is_new_state:
        cleanup_old_states()
        recovered_path = find_existing_md(session_id)
        if recovered_path is not None:
            recovered_last_line = read_last_line_from_md(recovered_path) or 0
            start_dt = datetime.fromtimestamp(recovered_path.stat().st_mtime, tz=JST)
            state = {
                "output_path": str(recovered_path),
                "start_time": start_dt.isoformat(),
                "cwd": cwd,
                "last_line": recovered_last_line,
            }
            save_state(session_id, state)
            is_new_state = False
            logger.warning(f"stateを既存Markdownから復旧しました: {recovered_path}")
    saved_last_line = state.get("last_line", 0)
    current_quota = retrieve_quota(jsonl_path, saved_last_line)
    if current_quota:
        if "initial_quota" not in state:
            state["initial_quota"] = current_quota
        state["final_quota"] = current_quota

    # 初回初期化
    if is_new_state or "output_path" not in state:
        start_dt = now_jst()
        path = create_md_file(
            session_id,
            cwd,
            start_dt,
            initial_quota=state.get("initial_quota"),
            source=source,
        )
        state["output_path"] = str(path)
        state["start_time"] = start_dt.isoformat()
        state["cwd"] = cwd
        state.setdefault("last_line", 0)
        save_state(session_id, state)
        logger.info(f"セッション初期化 {session_id[:8]} -> {path}")

    path = Path(state["output_path"])

    # MDファイル消失時のリカバリ
    if not path.exists():
        logger.warning(f"MDファイルが見つかりません: {path} → 再作成します")
        try:
            start_dt = datetime.fromisoformat(state.get("start_time", ""))
            if start_dt.tzinfo is None:
                start_dt = start_dt.replace(tzinfo=JST)
        except (ValueError, KeyError) as e:
            logger.info(f"start_time のパースに失敗したため現在時刻を使用: {e}")
            start_dt = now_jst()
        path = create_md_file(
            session_id,
            state.get("cwd", cwd),
            start_dt,
            initial_quota=state.get("initial_quota"),
            source=source,
        )
        state["output_path"] = str(path)
        state["last_line"] = 0
        save_state(session_id, state)
        logger.info(f"MDファイルを再作成しました: {path}")

    try:
        state_last_line = int(state.get("last_line", 0))
    except (TypeError, ValueError):
        state_last_line = 0
    md_last_line = read_last_line_from_md(path)
    last_line = md_last_line if md_last_line is not None else state_last_line
    if md_last_line is not None and md_last_line != state_last_line:
        logger.warning(
            f"last_lineの不一致をMarkdownから復旧: state={state_last_line}, markdown={md_last_line}"
        )
        state["last_line"] = md_last_line
        save_state(session_id, state)

    # メッセージのロードと追記
    new_messages, new_last_line = load_jsonl_messages(jsonl_path, last_line)

    if new_messages:
        appended, updated = append_messages(
            path,
            new_messages,
            now_jst(),
            model_name,
            new_last_line,
            current_quota=current_quota or state.get("final_quota"),
            last_quota=state.get("last_quota"),
            initial_quota=state.get("initial_quota"),
            effort=effort,
        )
        if updated:
            state["last_line"] = new_last_line
            if current_quota and any(
                msg.get("type") == "agent_message" and msg.get("message", "").strip()
                for msg in new_messages
            ):
                state["last_quota"] = current_quota
            save_state(session_id, state)
            logger.info(f"{appended}件追記完了 {session_id[:8]} (last_line: {new_last_line})")
        elif new_last_line != last_line:
            advance_last_line(path, new_last_line)
            state["last_line"] = new_last_line
            save_state(session_id, state)
    elif new_last_line != last_line:
        advance_last_line(path, new_last_line)
        state["last_line"] = new_last_line
        save_state(session_id, state)

# ============================================================
# メイン
# ============================================================
def main() -> None:
    hook_input = read_stdin()
    handle_stop_event(hook_input)
    print("{}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logger.exception(f"予期しないエラー: {e}")
        print("{}")
        sys.exit(0)
