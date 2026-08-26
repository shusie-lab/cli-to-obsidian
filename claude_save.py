#!/usr/bin/python3
from __future__ import annotations
"""
Claude Code CLI の会話履歴を Obsidian に自動保存するフックスクリプト。
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
OUTPUT_BASE = OBSIDIAN_VAULT / OBSIDIAN_OUTPUT_DIR / "claude-code"

STATE_DIR = Path.home() / ".claude" / "claude-obsidian" / "state"
LOG_FILE = Path.home() / ".claude" / "claude-obsidian" / "log" / "claude_obsidian_save.log"

STATE_RETENTION_DAYS = 30
LAST_LINE_PATTERN = re.compile(r"<!--\s*last_line:\s*(\d+)\s*-->")
AGENT_NAME = "Claude"
SAFE_SESSION_CHARS = re.compile(r"[^A-Za-z0-9_.-]+")

# ============================================================
# ロギング
# ============================================================
def setup_logger() -> logging.Logger:
    logger = logging.getLogger("claude_obsidian_save")
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
# transcript（JSONL）のパース
# ============================================================
def extract_user_text(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    elif isinstance(content, list):
        texts = []
        for item in content:
            if isinstance(item, str):
                texts.append(item)
            elif isinstance(item, dict):
                if item.get("type") == "text":
                    texts.append(item.get("text", ""))
        return "\n".join(texts)
    return ""


def extract_assistant_text_and_model(data: dict) -> tuple[str, str]:
    msg = data.get("message", {})
    model_name = msg.get("model", "") or data.get("model", "")
    content = msg.get("content")

    texts = []
    if isinstance(content, str):
        texts.append(content)
    elif isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                t = item.get("text", "")
                if t:
                    texts.append(t)
    return "\n".join(texts), model_name


def load_jsonl_messages(jsonl_path: Path, start_line: int) -> tuple[list[dict], int]:
    """
    JSONLファイルを読み込み、start_line (1-indexed) より後ろの行を処理する。
    user / assistant メッセージを抽出して返す。
    読み込んだ最終行番号 (1-indexed) も返す。
    """
    messages = []
    current_line = 0
    try:
        with open(jsonl_path, "r", encoding="utf-8") as f:
            for line in f:
                current_line += 1
                if current_line <= start_line:
                    continue

                raw_line = line
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    msg_type = data.get("type")

                    if msg_type == "user":
                        if data.get("isMeta"):
                            continue

                        msg_obj = data.get("message", {})
                        text = extract_user_text(msg_obj)
                        if not text.strip():
                            continue

                        if text.strip().startswith("<local-command-") or text.strip().startswith("<command-name>"):
                            continue

                        messages.append({
                            "line_number": current_line,
                            "type": "user_message",
                            "message": text,
                            "timestamp": data.get("timestamp", ""),
                            "model": "",
                        })

                    elif msg_type == "assistant":
                        text, model_name = extract_assistant_text_and_model(data)
                        if not text.strip():
                            continue

                        messages.append({
                            "line_number": current_line,
                            "type": "agent_message",
                            "message": text,
                            "timestamp": data.get("timestamp", ""),
                            "model": model_name,
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
def build_frontmatter(
    session_id: str,
    cwd: str,
    start_dt: datetime,
    message_count: int = 0,
) -> str:
    ts = format_iso(start_dt)
    fm_lines = [
        "---",
        "source: claude-code",
        f"session_id: {yaml_quote(session_id)}",
        f"project: {yaml_quote(cwd)}",
        f"created: {yaml_quote(ts)}",
        f"modified: {yaml_quote(ts)}",
        "tags:",
        "  - ai-conversation",
        "  - claude-code",
        f"message_count: {message_count}",
        "---",
    ]
    return "\n".join(fm_lines) + "\n\n"


def create_md_file(
    session_id: str,
    cwd: str,
    start_dt: datetime,
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
    fm = build_frontmatter(session_id, cwd, start_dt, message_count=0)
    content = fm + "<!-- last_line: 0 -->\n\n"
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
def append_messages(
    path: Path,
    messages: list[dict],
    modified_dt: datetime,
    last_line: int,
) -> tuple[int, bool]:
    content = path.read_text(encoding="utf-8")

    blocks = []
    appended = 0
    last_callout_header = get_last_callout_header(content)

    for msg in messages:
        m_type = msg["type"]
        text = msg["message"]
        m_model = msg.get("model", "")
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
            meta_part = f"> <small>🤖 {m_model}</small>\n" if m_model else ""
            meta_line = f"> <small>🤖 {m_model}</small>" if m_model else ""
            callout_header = f"> [!NOTE] {AGENT_NAME}\n{meta_line}".strip() if meta_line else f"> [!NOTE] {AGENT_NAME}"

            if callout_header == last_callout_header:
                # Claude Code のストリーミング結果が複数の agent_message に分割される
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

    state = load_state(session_id)
    if not state:
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
            logger.warning(f"stateを既存Markdownから復旧しました: {recovered_path}")

    # 初回初期化
    if not state or "output_path" not in state:
        start_dt = now_jst()
        path = create_md_file(
            session_id,
            cwd,
            start_dt,
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
            new_last_line,
        )
        if updated:
            state["last_line"] = new_last_line
            save_state(session_id, state)
            logger.info(f"{appended}件追記完了 {session_id[:8]} (last_line: {new_last_line})")
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
