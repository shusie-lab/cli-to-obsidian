#!/usr/bin/python3
from __future__ import annotations
"""
Antigravity CLI の会話履歴を Obsidian に自動保存するフックスクリプト。
出力形式: Obsidian AI Exporter 互換（YAMLフロントマター + callout block形式）

対応イベント:
  - Stop : セッション終了時（またはアイドル移行時）に Markdown ファイルの作成・追記を行う
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
OUTPUT_BASE = OBSIDIAN_VAULT / OBSIDIAN_OUTPUT_DIR / "antigravity-cli"

STATE_DIR = Path.home() / ".gemini" / "agy-obsidian" / "state"
LOG_FILE = Path.home() / ".gemini" / "agy-obsidian" / "log" / "obsidian_save.log"

STATE_RETENTION_DAYS = 30
LAST_ID_PATTERN = re.compile(r"<!--\s*last_id:\s*(\S+)\s*-->")
AGENT_NAME = "Antigravity"
SAFE_SESSION_CHARS = re.compile(r"[^A-Za-z0-9_.-]+")

# ============================================================
# ロギング
# ============================================================
def setup_logger() -> logging.Logger:
    logger = logging.getLogger("obsidian_save_antigravity")
    # デフォルトは INFO。環境変数 DEBUG で切り替え可能。
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
# セッション単位のstateファイル
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


def load_jsonl_messages(jsonl_path: Path) -> list:
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
        logger.warning(f"JSONL読み込み失敗: {jsonl_path}: {e}")
        return []

    # step_index で最新のものを保持（JSONLには更新行が含まれる場合があるため）
    unique_messages: dict[int, dict] = {}
    for msg in all_msgs:
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
    for i, msg in enumerate(sorted_msgs):
        msg_type = msg.get("type")
        
        if msg_type == "USER_INPUT":
            model_name = extract_model_name(msg.get("content"))
            if model_name:
                current_model = model_name
            text = extract_user_text(msg.get("content"))
            if text:
                final_messages.append({
                    "step_index": msg.get("step_index", 0),
                    "type": "user_message",
                    "message": text,
                    "timestamp": msg.get("created_at", ""),
                    "model": "",
                })

        elif msg_type == "PLANNER_RESPONSE":
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
                    "model": current_model or "",
                })

    return final_messages


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
# Markdownファイル生成・操作
# ============================================================
def build_frontmatter(
    session_id: str,
    cwd: str,
    start_dt: datetime,
    message_count: int = 0,
    is_app: bool = False,
) -> str:
    ts = format_iso(start_dt)
    source = "antigravity-app" if is_app else "antigravity-cli"
    tag_name = "antigravity-app" if is_app else "antigravity-cli"

    fm_lines = [
        "---",
        f"source: {source}",
        f"session_id: {yaml_quote(session_id)}",
        f"project: {yaml_quote(cwd)}",
        f"created: {yaml_quote(ts)}",
        f"modified: {yaml_quote(ts)}",
        "tags:",
        "  - ai-conversation",
        f"  - {tag_name}",
        f"message_count: {message_count}",
    ]
    fm_lines.append("---")
    fm_text = "\n".join(fm_lines) + "\n\n"
    return fm_text


def create_md_file(
    session_id: str,
    cwd: str,
    start_dt: datetime,
    is_app: bool = False,
) -> Path:
    date_dir = start_dt.strftime("%Y%m%d")
    time_prefix = start_dt.strftime("%H%M%S")
    short_id = safe_filename_component(session_id[:8]) if session_id else "unknown"
    short_id = short_id or "unknown"

    # プロジェクト名の取得
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
        is_app=is_app,
    )
    content = fm + "<!-- last_id: -->\n\n"
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


def update_markdown_metadata(
    path: Path,
    modified_dt: datetime,
    appended: int,
    last_id: str,
    content: str | None = None,
) -> None:
    if content is None:
        content = path.read_text(encoding="utf-8")
    
    fm_match = re.match(r"^---\s*\n(.*?)\n---\s*\n", content, re.DOTALL)
    if not fm_match:
        atomic_write_md(path, content)
        return
        
    fm_text = fm_match.group(1)
    body_text = content[fm_match.end():]
    
    fm_lines = fm_text.splitlines()
    new_fm_lines = []
    
    keys_to_remove = {"modified", "message_count"}
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
            new_fm_lines.append(line)
            continue
            
        skip_list = False
        new_fm_lines.append(line)

    new_count = old_count + appended
    new_fm_lines.append(f"modified: {yaml_quote(format_iso(modified_dt))}")
    new_fm_lines.append(f"message_count: {new_count}")
    body_text = re.sub(
        r"<!--\s*last_id:.*?-->",
        f"<!-- last_id: {last_id} -->",
        body_text
    )

    new_content = "---\n" + "\n".join(new_fm_lines) + "\n---\n" + body_text
    atomic_write_md(path, new_content)


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


def atomic_write_md(path: Path, content: str) -> None:
    """atomicに保存（temp→rename）"""
    atomic_write_text(path, content)


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
# メッセージ追記（本文・メタデータを単一writeで確定）
# ============================================================
def append_messages(
    path: Path,
    messages: list,
    modified_dt: datetime,
) -> tuple[str, int, bool]:
    content = path.read_text(encoding="utf-8")

    blocks = []
    last_id = ""
    appended = 0
    last_callout_header = get_last_callout_header(content)

    for msg in messages:
        m_type = msg.get("type", "")
        msg_id = str(msg.get("step_index", ""))
        text = msg.get("message", "")
        m_model = msg.get("model", "")
        
        if not text.strip():
            last_id = msg_id
            continue

        time_part = format_message_time(msg.get("timestamp"))

        if m_type == "user_message":
            # 発言の最初のテキスト行から見出しテキストを抽出
            heading = user_heading(text)
            callout_header = f"> [!QUESTION] User\n{time_part}".strip()

            if callout_header == last_callout_header:
                blocks.append(f"---\n\n{callout_lines(text)}\n\n")
            else:
                blocks.append(f"{heading}> [!QUESTION] User\n{time_part}{callout_lines(text)}\n\n")

            last_callout_header = callout_header
            appended += 1

        elif m_type == "agent_message":
            meta_part = f"> <small>🤖 {m_model}</small>\n" if m_model else ""
            meta_line = f"> <small>🤖 {m_model}</small>" if m_model else ""
            callout_header = f"> [!NOTE] {AGENT_NAME}\n{meta_line}".strip() if meta_line else f"> [!NOTE] {AGENT_NAME}"

            if callout_header == last_callout_header:
                # Antigravity のストリーミング結果が複数の agent_message に分割される
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
        last_id = msg_id

    if not blocks:
        logger.info(f"追記対象のブロックがありませんでした（全メッセージがスキップ）: last_id={last_id!r}")
        return last_id, appended, False

    content += "\n".join(blocks)
    update_markdown_metadata(
        path,
        modified_dt,
        appended,
        last_id,
        content=content,
    )

    return last_id, appended, True


# ============================================================
# イベントハンドラ
# ============================================================
@session_locked("conversationId")
def handle_stop_event(hook_input: dict) -> None:
    session_id = hook_input.get("conversationId", "")
    transcript_path = hook_input.get("transcriptPath", "")
    workspace_paths = hook_input.get("workspacePaths", [])
    cwd = workspace_paths[0] if workspace_paths and isinstance(workspace_paths, list) else os.getcwd()

    if not session_id:
        logger.warning("conversationId が取得できませんでした")
        return
    if not transcript_path:
        logger.info("transcriptPath が未提供のため session_id から JSONL を探索します")

    # アプリ版セッションかどうかの判定（transcriptPath のパスで識別）
    is_app = (
        bool(transcript_path)
        and "/.gemini/antigravity/" in transcript_path
        and "/.gemini/antigravity-cli/" not in transcript_path
    )

    state = load_state(session_id)
    if not state:
        cleanup_old_states()
        recovered_path = find_existing_md(session_id)
        if recovered_path is not None:
            recovered_last_id = read_last_id_from_md(recovered_path) or ""
            start_dt = datetime.fromtimestamp(recovered_path.stat().st_mtime, tz=JST)
            state = {
                "output_path": str(recovered_path),
                "start_time": start_dt.isoformat(),
                "cwd": cwd,
                "last_id": recovered_last_id,
                "message_count": 0,
            }
            save_state(session_id, state)
            logger.warning(f"stateを既存Markdownから復旧しました: {recovered_path}")

    if not state or "output_path" not in state:
        start_dt = now_jst()
        path = create_md_file(
            session_id,
            cwd,
            start_dt,
            is_app=is_app,
        )
        state["output_path"] = str(path)
        state["start_time"] = start_dt.isoformat()
        state["cwd"] = cwd
        state.setdefault("last_id", "")
        state.setdefault("message_count", 0)
        save_state(session_id, state)
        logger.info(f"セッション初期化 {session_id[:8]} -> {path}")

    path = Path(state["output_path"])

    # MDファイルが存在しない場合（手動削除・Vault移動等）はリカバリ
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
            is_app=is_app,
        )
        state["output_path"] = str(path)
        state["last_id"] = ""  # 再作成時は全メッセージを再取得
        save_state(session_id, state)
        logger.info(f"MDファイルを再作成しました: {path}")

    state_last_id = str(state.get("last_id", ""))
    md_last_id = read_last_id_from_md(path)
    last_id = md_last_id if md_last_id is not None else state_last_id
    if md_last_id is not None and md_last_id != state_last_id:
        logger.warning(
            f"last_idの不一致をMarkdownから復旧: state={state_last_id!r}, markdown={md_last_id!r}"
        )
        state["last_id"] = md_last_id
        save_state(session_id, state)
    
    jsonl_path = find_jsonl_path(session_id, transcript_path)
    if not jsonl_path:
        logger.warning(f"JSONLファイルが見つかりません: session={session_id}")
        return

    all_messages = load_jsonl_messages(jsonl_path)
    new_messages = get_new_messages(all_messages, last_id)

    if new_messages:
        new_last_id, appended, updated = append_messages(
            path,
            new_messages,
            now_jst(),
        )
        if new_last_id:
            state["last_id"] = new_last_id
        save_state(session_id, state)
        logger.info(f"{appended}件追記完了 {session_id[:8]}")



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
