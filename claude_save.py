#!/usr/bin/python3
from __future__ import annotations
"""
Claude Code CLI の会話履歴を Obsidian に自動保存するフックスクリプト。
出力形式: Obsidian AI Exporter 互換（YAMLフロントマター + callout block形式）

対応イベント:
  - Stop : ターン終了時に Markdown ファイルの作成・追記を行う
"""

__version__ = "1.4.0"

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


# BEGIN GENERATED: output-directory
def resolve_output_dir(value: str | None = None) -> Path:
    """保管庫からの相対保存先を検証し、不正なら既定値を返す。"""
    raw = os.environ.get("OBSIDIAN_OUTPUT_DIR", DEFAULT_OUTPUT_DIR) if value is None else value
    candidate = Path(raw) if str(raw).strip() else Path(DEFAULT_OUTPUT_DIR)
    if candidate.is_absolute() or ".." in candidate.parts:
        return Path(DEFAULT_OUTPUT_DIR)
    return candidate
# END GENERATED: output-directory


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
# BEGIN GENERATED: session-storage
def safe_session_key(session_id: str) -> str:
    """session_idをパストラバーサルできないファイル名へ変換する。"""
    raw = str(session_id)
    safe = SAFE_SESSION_CHARS.sub("_", raw).strip("._")[:80]
    if safe and safe == raw:
        return safe
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
    return f"{safe or 'session'}-{digest}"


def state_path(session_id: str) -> Path:
    return STATE_DIR / f"{safe_session_key(session_id)}.json"


def atomic_write_text(path: Path, content: str) -> None:
    """同じディレクトリの一時ファイルをrenameして文字列を保存する。"""
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


class ExistingMarkdownSearchError(Exception):
    """既存Markdown探索中のI/Oエラー。誤上書き・重複作成を防ぐために使用。"""


@contextmanager
def session_lock(session_id: str):
    """同一セッションのhook実行を直列化する。"""
    lock_dir = STATE_DIR / ".locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / f"{safe_session_key(session_id)}.lock"
    with open(lock_path, "a", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


@contextmanager
def try_session_lock(session_id: str, *, is_key: bool = False):
    """非ブロッキングでセッションロックを試み、取得結果をyieldする。"""
    lock_dir = STATE_DIR / ".locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / f"{session_id if is_key else safe_session_key(session_id)}.lock"
    try:
        lock_file = open(lock_path, "a", encoding="utf-8")
    except OSError:
        yield False
        return
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        lock_file.close()
        yield False
        return
    try:
        yield True
    finally:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        lock_file.close()


def session_locked(input_key: str):
    """hook input内のsession idを使って関数を排他実行する。"""
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
        except Exception as error:
            logger.info("stateファイルの読み込みに失敗しました（破損の可能性）: %s: %s", path, error)
            return {}
    return {}


def save_state(session_id: str, state: dict) -> None:
    """stateをatomicに保存する。"""
    state["last_used_at"] = format_iso(now_jst())
    atomic_write_text(state_path(session_id), json.dumps(state, ensure_ascii=False, indent=2))


def cleanup_old_states() -> None:
    """保持期間を過ぎたstateを、実行中セッションを保護しながら削除する。"""
    if not STATE_DIR.exists():
        return
    cutoff = datetime.now(JST) - timedelta(days=STATE_RETENTION_DAYS)
    removed = 0
    try:
        candidates = list(STATE_DIR.glob("*.json"))
    except OSError as error:
        logger.warning("state探索に失敗したためクリーンアップを見送ります: %s", error)
        return
    for path in candidates:
        with try_session_lock(path.stem, is_key=True) as acquired:
            if not acquired:
                continue
            try:
                if not path.exists():
                    continue
                data = json.loads(path.read_text(encoding="utf-8"))
                last_used = None
                if data.get("last_used_at"):
                    try:
                        last_used = datetime.fromisoformat(str(data["last_used_at"]))
                        if last_used.tzinfo is None:
                            last_used = last_used.replace(tzinfo=JST)
                    except ValueError:
                        pass
                if last_used is None:
                    last_used = datetime.fromtimestamp(path.stat().st_mtime, tz=JST)
                if last_used < cutoff:
                    path.unlink()
                    removed += 1
            except Exception as error:
                logger.info("stateファイルのクリーンアップ中にスキップ: %s: %s", path.name, error)
    if removed > 0:
        logger.info("古いstateファイル %d 件を削除しました", removed)
# END GENERATED: session-storage

# ============================================================
# ユーティリティ
# ============================================================
# BEGIN GENERATED: incremental-utilities
def read_stdin() -> dict:
    """hookの標準入力を読み、空入力や不正JSONでは空のdictを返す。"""
    try:
        data = sys.stdin.read()
        if data.strip():
            return json.loads(data)
    except Exception as error:
        logger.info("stdinの読み込みまたはJSONパースに失敗しました: %s", error)
    return {}


def now_jst() -> datetime:
    return datetime.now(JST)


def format_iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def yaml_quote(value: object) -> str:
    """JSON互換の引用形式でYAML文字列を安全に生成する。"""
    return json.dumps(str(value), ensure_ascii=False)


def safe_filename_component(value: str) -> str:
    """値を単一の安全なファイル名要素へ変換する。"""
    return re.sub(r"[\x00-\x1f/:*?\[\]\\]", "_", value).strip()[:80]
# END GENERATED: incremental-utilities


# BEGIN GENERATED: markdown-formatting
def sanitize_markdown(text: str) -> str:
    """会話本文のMarkdown記述を維持し、空値を空文字列へそろえる。"""
    return text if text else ""


def escape_markdown_heading(text: str) -> str:
    """見出し内でMarkdown構文として解釈される文字をエスケープする。"""
    return re.sub(r"([\\`*_{}\[\]()#+\-.!|<>~])", r"\\\1", text)


def unescape_markdown_heading(text: str) -> str:
    """見出し用に追加したMarkdownエスケープを取り除く。"""
    escaped_chars = r"\\`*_{}[]()#+-.!|<>~"
    result = []
    index = 0
    while index < len(text):
        if text[index] == "\\" and index + 1 < len(text) and text[index + 1] in escaped_chars:
            result.append(text[index + 1])
            index += 2
        else:
            result.append(text[index])
            index += 1
    return "".join(result)


def callout_lines(text: str) -> str:
    """本文をObsidian callout内の行へ変換する。"""
    lines = []
    for raw_line in str(text).split("\n"):
        line = raw_line.replace("<", "&lt;")
        stripped = line.lstrip()
        if stripped.startswith(">"):
            line = line[: len(line) - len(stripped)] + "&gt;" + stripped[1:]
        lines.append(f"> {line}" if line else ">")
    return "\n".join(lines)
# END GENERATED: markdown-formatting


# BEGIN GENERATED: incremental-callout
MARKDOWN_FENCE_PATTERN = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")


def _strip_blockquote_prefix(line: str) -> str:
    """Markdownの通常行またはblockquote内行からblockquote記号を除く。"""
    candidate = line.rstrip("\r\n")
    while True:
        match = re.match(r"^ {0,3}>[ \t]?", candidate)
        if not match:
            return candidate
        candidate = candidate[match.end():]


def _fence_marker(line: str) -> tuple[str, int, str] | None:
    """行のMarkdownコードフェンスを、blockquote内も含めて返す。"""
    match = MARKDOWN_FENCE_PATTERN.match(_strip_blockquote_prefix(line))
    if not match:
        return None
    fence = match.group(1)
    return fence[0], len(fence), match.group(2)


def _iter_lines_outside_fences(text: str):
    """Markdownコードフェンス内を除いた行を順に返す。"""
    active_fence = None
    for line in text.splitlines():
        marker = _fence_marker(line)
        if active_fence is not None:
            if (
                marker is not None
                and marker[0] == active_fence[0]
                and marker[1] >= active_fence[1]
                and not marker[2].strip()
            ):
                active_fence = None
            continue
        if marker is not None:
            active_fence = marker[:2]
            continue
        yield line
# END GENERATED: incremental-callout


# BEGIN GENERATED: message-metadata
def format_message_time(timestamp: object) -> str:
    """ISO日時またはUnix epoch millisecondsをJSTの共通メタデータ行へ変換する。"""
    if timestamp in (None, ""):
        return ""
    try:
        value = int(timestamp) if isinstance(timestamp, str) and timestamp.isdigit() else timestamp
        if isinstance(value, (int, float)):
            dt = datetime.fromtimestamp(float(value) / 1000, tz=timezone.utc)
        else:
            text = str(value)
            dt = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        return f"> <small>⏱ {dt.astimezone(JST):%Y-%m-%d %H:%M:%S}</small>\n>\n"
    except (OSError, OverflowError, TypeError, ValueError) as error:
        logger.warning("timestampのパース失敗: %s: %s", timestamp, error)
        return ""


def _heading_text(text: str) -> str:
    """ユーザー発言から見出し・タイトル共通の短縮済み文字列を返す。"""
    first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
    first_line = first_line.replace("\\\\", "\\")
    for escaped, plain in ((r"\[", "["), (r"\]", "]"), (r"\_", "_"), (r"\*", "*")):
        first_line = first_line.replace(escaped, plain)
    if len(first_line) > 40:
        first_line = first_line[:40] + "..."
    return first_line


def user_heading(text: str) -> str:
    """ユーザー発言の先頭非空行から安全な共通見出しを作る。"""
    first_line = _heading_text(text)
    escaped = escape_markdown_heading(first_line)
    return f"# {escaped}\n" if escaped else "# User\n"


def heading_title(text: str) -> str:
    """ユーザー発話から、frontmatter titleに使う見出し文字列を返す。"""
    return _heading_text(text) or "User"
# END GENERATED: message-metadata


# BEGIN GENERATED: markdown-rendering
def user_callout_header(timestamp: object) -> str:
    """連続発言の判定にも使うUser calloutヘッダーを生成する。"""
    return f"> [!QUESTION] User\n{format_message_time(timestamp)}".strip()


def render_user_message_block(text: str, timestamp: object, *, continued: bool = False) -> str:
    """User発言を、ファイルI/Oを行わずMarkdownブロックへ変換する。"""
    body = callout_lines(text)
    if continued:
        return f"---\n\n{body}\n\n"
    return f"{user_heading(text)}> [!QUESTION] User\n{format_message_time(timestamp)}{body}\n\n"


def assistant_callout_header(agent_name: str, metadata: list[str]) -> str:
    """連続回答の判定にも使うAssistant calloutヘッダーを生成する。"""
    lines = [f"> [!NOTE] {agent_name}"]
    lines.extend(f"> <small>{item}</small>" for item in metadata if item)
    return "\n".join(lines)


def render_assistant_message_block(
    text: str,
    agent_name: str,
    metadata: list[str],
    *,
    continued: bool = False,
    extra_blank_line: bool = False,
) -> str:
    """Assistant発言を、ファイルI/Oを行わずMarkdownブロックへ変換する。"""
    body = text
    if continued:
        return body
    separator = "\n\n\n" if extra_blank_line else "\n\n"
    return f"{assistant_callout_header(agent_name, metadata)}{separator}{body}\n\n"
# END GENERATED: markdown-rendering


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
    title: str | None = None,
) -> str:
    ts = format_iso(start_dt)
    fm_lines = [
        "---",
        "source: claude-code",
        *( [f"title: {yaml_quote(title)}"] if title is not None else [] ),
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
    title: str | None = None,
) -> Path:
    date_dir = start_dt.strftime("%Y%m%d")
    time_prefix = start_dt.strftime("%H%M%S")
    session_key = safe_session_key(session_id) if session_id else "unknown"
    session_key = session_key or "unknown"
    project_name = safe_filename_component(Path(cwd).name) if cwd else ""

    output_dir = OUTPUT_BASE
    output_dir.mkdir(parents=True, exist_ok=True)

    if project_name:
        filename = f"{date_dir}_{time_prefix}_{project_name}_{session_key}.md"
    else:
        filename = f"{date_dir}_{time_prefix}_{session_key}.md"

    path = output_dir / filename
    fm = build_frontmatter(session_id, cwd, start_dt, message_count=0, title=title)
    content = fm + "<!-- last_line: 0 -->\n\n"
    atomic_write_md(path, content)
    return path


# BEGIN GENERATED: find-existing-markdown
def find_existing_md(session_id: str) -> Path | None:
    """state消失時にfrontmatterのsession_idから既存Markdownを探す。"""
    if not OUTPUT_BASE.exists():
        return None
    identifiers = []
    full_key = safe_session_key(session_id)
    short_key = safe_filename_component(session_id[:8]) if session_id else ""
    if full_key:
        identifiers.append(full_key)
    if short_key and short_key not in identifiers:
        identifiers.append(short_key)
    if not identifiers:
        identifiers.append("unknown")

    expected = {
        f"session_id: {yaml_quote(session_id)}",
        f"session_id: {session_id}",
    }
    candidates = set()
    try:
        for path in OUTPUT_BASE.iterdir():
            if any(path.name.endswith(f"_{identifier}.md") for identifier in identifiers):
                candidates.add(path)
    except OSError as error:
        raise ExistingMarkdownSearchError(f"既存Markdown候補の列挙に失敗しました: {error}")

    candidate_entries = []
    for candidate in candidates:
        try:
            candidate_entries.append((candidate, candidate.stat().st_mtime))
        except OSError as error:
            raise ExistingMarkdownSearchError(
                f"既存Markdown候補の情報取得に失敗しました: {candidate}: {error}"
            )
    candidate_entries.sort(key=lambda item: item[1], reverse=True)

    last_read_error = None
    for candidate, _ in candidate_entries:
        try:
            content = candidate.read_text(encoding="utf-8")
            parts = content.split("---", 2)
            if len(parts) >= 3:
                frontmatter_lines = [line.strip() for line in parts[1].splitlines()]
                if any(item in frontmatter_lines for item in expected):
                    return candidate
        except (OSError, UnicodeError) as error:
            last_read_error = error
            logger.warning("既存Markdown候補の読み込みに失敗したためスキップします: %s: %s", candidate, error)
        except IndexError:
            continue
    if last_read_error is not None:
        raise ExistingMarkdownSearchError(
            "既存Markdown候補の読み取りに失敗したファイルが存在し、"
            f"一致を確認できなかったため探索を中断します: {last_read_error}"
        )
    return None
# END GENERATED: find-existing-markdown


# BEGIN GENERATED: atomic-markdown-write
def atomic_write_md(path: Path, content: str) -> None:
    """Markdown全文を一時ファイル経由でatomicに保存する。"""
    atomic_write_text(path, content)
# END GENERATED: atomic-markdown-write


# BEGIN GENERATED: line-cursor
def read_last_line_from_md(path: Path) -> int | None:
    try:
        content = path.read_text(encoding="utf-8")
        match = LAST_LINE_PATTERN.search(content)
        if match:
            return int(match.group(1))
        logger.debug("MDファイルに last_line コメントが見つかりません: %s", path)
    except Exception as error:
        logger.warning("MDファイルからの last_line 読み取りに失敗: %s: %s", path, error)
    return None


def advance_last_line(path: Path, last_line: int) -> None:
    """会話がない行だけでもMarkdown側のcursorをatomicに進める。"""
    content = path.read_text(encoding="utf-8")
    updated, count = LAST_LINE_PATTERN.subn(f"<!-- last_line: {last_line} -->", content, count=1)
    if count:
        atomic_write_md(path, updated)
# END GENERATED: line-cursor


# BEGIN GENERATED: callout-header
def is_callout_header_line(line: str) -> bool:
    return line.startswith("> [!") or line.startswith("> <small>") or line == ">"


def get_last_callout_header(content: str) -> str | None:
    """コードフェンス外にある最後のCalloutヘッダーを抽出する。"""
    last_header = None
    header_lines = []
    for line in _iter_lines_outside_fences(content):
        if line.startswith("> [!"):
            header_lines = [line]
            last_header = line
        elif header_lines and is_callout_header_line(line):
            header_lines.append(line)
            last_header = "\n".join(header_lines)
        else:
            header_lines = []
    return last_header.strip() if last_header else None
# END GENERATED: callout-header

# BEGIN GENERATED: frontmatter-update
def update_frontmatter_fields(
    content: str,
    modified_dt: datetime,
    appended: int,
    *,
    extra_removed_keys: tuple[str, ...] = (),
    extra_fields: tuple[str, ...] = (),
) -> tuple[list[str], str] | None:
    """frontmatterの共通項目を更新し、項目行と本文を返す。"""
    fm_match = re.match(r"^---\s*\n(.*?)\n---\s*\n", content, re.DOTALL)
    if not fm_match:
        return None

    fm_lines = fm_match.group(1).splitlines()
    body_text = content[fm_match.end():]
    title_match = re.search(r"(?m)^# (?:User: )?(.+?)\s*$", body_text)
    title = unescape_markdown_heading(title_match.group(1)) if title_match else None
    keys_to_remove = {"modified", "message_count", *extra_removed_keys}
    if title is not None:
        keys_to_remove.add("title")
    new_fm_lines = []
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
            if key_match.group(1) in keys_to_remove:
                skip_list = True
                continue
            skip_list = False
            new_fm_lines.append(line)
            continue
        if line.startswith("  - ") or line.startswith("    - "):
            if skip_list:
                continue
        else:
            skip_list = False
        new_fm_lines.append(line)

    if title is not None:
        new_fm_lines.append(f"title: {yaml_quote(title)}")
    new_fm_lines.append(f"modified: {yaml_quote(format_iso(modified_dt))}")
    new_fm_lines.append(f"message_count: {old_count + appended}")
    new_fm_lines.extend(extra_fields)
    return new_fm_lines, body_text


def compose_markdown(new_fm_lines: list[str], body_text: str) -> str:
    """更新済みfrontmatterと本文をMarkdown全文へ戻す。"""
    return "---\n" + "\n".join(new_fm_lines) + "\n---\n" + body_text
# END GENERATED: frontmatter-update


def update_markdown_metadata(
    content: str,
    modified_dt: datetime,
    appended: int,
    last_line: int,
) -> str:
    """本文のfrontmatterとcursorを更新し、書き込み前の全文を返す。"""
    updated = update_frontmatter_fields(content, modified_dt, appended)
    if updated is None:
        return content
    new_fm_lines, body_text = updated
    body_text = re.sub(
        r"<!--\s*last_line:.*?-->",
        f"<!-- last_line: {last_line} -->",
        body_text,
    )
    return compose_markdown(new_fm_lines, body_text)


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

        if m_type == "user_message":
            callout_header = user_callout_header(msg.get("timestamp"))
            blocks.append(
                render_user_message_block(
                    text,
                    msg.get("timestamp"),
                    continued=callout_header == last_callout_header,
                )
            )

            last_callout_header = callout_header
            appended += 1

        elif m_type == "agent_message":
            # 使用されたモデル名の取得
            metadata = [f"🤖 {m_model}"] if m_model else []
            callout_header = assistant_callout_header(AGENT_NAME, metadata)

            if callout_header == last_callout_header:
                # Claude Code のストリーミング結果が複数の agent_message に分割される
                # ことがある。同じ callout の続きは区切り線を入れず、改行を残して連結し、
                # 1つの回答として message_count に加算する。
                fragment = render_assistant_message_block(
                    text,
                    AGENT_NAME,
                    metadata,
                    continued=True,
                )
                if blocks:
                    blocks[-1] = blocks[-1].rstrip("\n") + "\n" + fragment + "\n\n"
                else:
                    content = content.rstrip("\n") + "\n"
                    blocks.append(fragment + "\n\n")
            else:
                blocks.append(
                    render_assistant_message_block(
                        text,
                        AGENT_NAME,
                        metadata,
                        extra_blank_line=True,
                    )
                )
                appended += 1

            last_callout_header = callout_header

    if not blocks:
        return 0, False

    new_content = content + "\n".join(blocks)
    final_content = update_markdown_metadata(new_content, modified_dt, appended, last_line)
    # 本文とメタデータを1回のatomic writeで確定する。
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
        try:
            recovered_path = find_existing_md(session_id)
        except ExistingMarkdownSearchError as e:
            logger.error(f"既存Markdownの探索中にエラーが発生したため保存を見送ります: {e}")
            return
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
# BEGIN GENERATED: incremental-main
def main() -> None:
    hook_input = read_stdin()
    handle_stop_event(hook_input)
    print("{}")
# END GENERATED: incremental-main


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logger.exception(f"予期しないエラー: {e}")
        print("{}")
        sys.exit(0)
