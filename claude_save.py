#!/usr/bin/python3
from __future__ import annotations
"""
Claude Code CLI の会話履歴を Obsidian に自動保存するフックスクリプト。
出力形式: Obsidian AI Exporter 互換（YAMLフロントマター + callout block形式）

対応イベント:
  - Stop : ターン終了時に Markdown ファイルの作成・追記を行う
"""

__version__ = "3.1.0"

import json
import math
from decimal import Decimal
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
DEFAULT_SOURCE = "claude-code"


# BEGIN GENERATED: output-directory
def resolve_output_dir(value: str | None = None) -> Path:
    """保管庫からの相対保存先を検証し、不正なら既定値を返す。"""
    raw = os.environ.get("OBSIDIAN_OUTPUT_DIR", DEFAULT_OUTPUT_DIR) if value is None else value
    candidate = Path(raw) if str(raw).strip() else Path(DEFAULT_OUTPUT_DIR)
    if candidate.is_absolute() or ".." in candidate.parts:
        return Path(DEFAULT_OUTPUT_DIR)
    return candidate


SOURCE_NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")


def resolve_source_name(default: str, value: str | None = None) -> str:
    """保存先とfrontmatterに使う名前を検証し、不正なら既定値を返す。"""
    candidate = os.environ.get("OBSIDIAN_SOURCE", default) if value is None else value
    if candidate not in {".", ".."} and SOURCE_NAME_PATTERN.fullmatch(candidate):
        return candidate
    return default
# END GENERATED: output-directory


OBSIDIAN_OUTPUT_DIR = resolve_output_dir()
SOURCE_NAME = resolve_source_name(DEFAULT_SOURCE)
OUTPUT_BASE = OBSIDIAN_VAULT / OBSIDIAN_OUTPUT_DIR / SOURCE_NAME

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


def merge_first_quota(first: dict | None, observed: dict) -> dict:
    """各枠について、最初に取得できた観測値を保持する。"""
    merged = {group: dict(windows) for group, windows in (first or {}).items()}
    for group, windows in observed.items():
        target = merged.setdefault(group, {})
        for window, value in windows.items():
            target.setdefault(window, value)
    return merged


def build_quota_frontmatter_fields(first: dict | None, last: dict | None) -> list[str]:
    """quotaは消費量に換算せず、枠ごとの最初と最後の観測値を保存する。"""
    if not first and not last:
        return []
    lines = ["quota:"]
    for group in sorted(set(first or {}) | set(last or {})):
        lines.append(f"  {yaml_quote(group)}:")
        first_windows = (first or {}).get(group, {})
        last_windows = (last or {}).get(group, {})
        for window in sorted(set(first_windows) | set(last_windows)):
            lines.append(f"    {yaml_quote(window)}:")
            for label, value in (("first_observed", first_windows.get(window)),
                                 ("last_observed", last_windows.get(window))):
                if not isinstance(value, dict) or value.get("remaining") is None:
                    continue
                lines.append(f"      {label}:")
                lines.append(f"        remaining_percent: {value['remaining']}")
                if value.get("observed_at"):
                    lines.append(f"        observed_at: {yaml_quote(value['observed_at'])}")
                if value.get("reset"):
                    lines.append(f"        reset_at_jst: {yaml_quote(value['reset'])}")
    return lines


def recover_quota_frontmatter(content: str) -> tuple[dict, dict]:
    """このスクリプトが出力した階層化quotaだけをstateへ復旧する。"""
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n", content, re.DOTALL)
    if not match:
        return {}, {}
    first, last = {}, {}
    in_quota = False
    group = window = observation = ""
    for line in match.group(1).splitlines():
        if line == "quota:":
            in_quota = True
            continue
        if not in_quota:
            continue
        if line and not line.startswith(" "):
            break
        indent = len(line) - len(line.lstrip(" "))
        key = line.strip()
        if indent == 2 and key.endswith(":"):
            try:
                group = json.loads(key[:-1])
            except (TypeError, ValueError):
                return {}, {}
        elif indent == 4 and key.endswith(":"):
            try:
                window = json.loads(key[:-1])
            except (TypeError, ValueError):
                return {}, {}
        elif indent == 6 and key in ("first_observed:", "last_observed:"):
            observation = key[:-1]
        elif indent == 8 and group and window and observation and ": " in key:
            field, raw = key.split(": ", 1)
            target = first if observation == "first_observed" else last
            item = target.setdefault(group, {}).setdefault(window, {})
            if field == "remaining_percent":
                try:
                    value = float(raw)
                except ValueError:
                    return {}, {}
                if not 0 <= value <= 100:
                    return {}, {}
                item["remaining"] = value
            elif field in ("observed_at", "reset_at_jst"):
                try:
                    value = json.loads(raw)
                except ValueError:
                    return {}, {}
                item["observed_at" if field == "observed_at" else "reset"] = value
    return first, last
# END GENERATED: incremental-utilities


# BEGIN GENERATED: markdown-formatting
def sanitize_markdown(text: str) -> str:
    """会話本文のMarkdown記述を維持し、空値を空文字列へそろえる。"""
    return text if text else ""


def _file_uri_path(path: str) -> str:
    """Markdownリンクのローカル絶対パスをfile URIのパスへ変換する。"""
    from urllib.parse import quote

    path_part, separator, fragment = path.partition("#")
    encoded = quote(path_part, safe="/%:@!$&'()*+,;=-._~")
    if separator:
        encoded += "#" + quote(fragment, safe="/%:@!$&'()*+,;=-._~")
    return "file://" + encoded


def _normalize_link_destination(destination: str) -> str:
    """ローカル絶対パスだけを外部ファイルURIへ変換する。"""
    if not destination.startswith("/") or destination.startswith("//"):
        return destination
    return _file_uri_path(destination)


def _find_link_end(text: str, start: int) -> int | None:
    """リンク先の閉じ括弧位置を返す（エスケープと入れ子の括弧に対応）。"""
    depth = 0
    escaped = False
    for index in range(start, len(text)):
        character = text[index]
        if escaped:
            escaped = False
        elif character == "\\":
            escaped = True
        elif character == "(":
            depth += 1
        elif character == ")":
            if depth == 0:
                return index
            depth -= 1
    return None


def _strip_markdown_blockquote_prefix(line: str) -> str:
    """Markdown行からblockquoteの入れ子接頭辞を取り除く。"""
    candidate = line.rstrip("\r\n")
    while True:
        match = re.match(r"^ {0,3}>[ \t]?", candidate)
        if not match:
            return candidate
        candidate = candidate[match.end():]


def _fenced_code_ranges(text: str) -> list[tuple[int, int]]:
    """Markdown本文中のコードフェンス範囲を、元の文字列位置で返す。"""
    ranges = []
    active = None
    offset = 0
    for line in text.splitlines(keepends=True):
        content = _strip_markdown_blockquote_prefix(line)
        fence = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", content)
        if active is None:
            if fence:
                active = (offset, fence.group(1)[0], len(fence.group(1)))
        elif (
            fence
            and fence.group(1)[0] == active[1]
            and len(fence.group(1)) >= active[2]
            and not fence.group(2).strip()
        ):
            ranges.append((active[0], offset + len(line)))
            active = None
        offset += len(line)
    if active is not None:
        ranges.append((active[0], len(text)))
    return ranges


def _iter_inline_markdown_segments(text: str):
    """通常Markdown部分をtext/code_spanの順で走査する。"""
    index = 0
    text_start = 0
    while index < len(text):
        if text[index] != "`":
            index += 1
            continue
        run_end = index
        while run_end < len(text) and text[run_end] == "`":
            run_end += 1
        marker = text[index:run_end]
        closing = text.find(marker, run_end)
        if closing == -1:
            index = run_end
            continue
        if text_start < index:
            yield "text", text[text_start:index]
        yield "code_span", text[index:closing + len(marker)]
        index = closing + len(marker)
        text_start = index
    if text_start < len(text):
        yield "text", text[text_start:]


def iter_markdown_segments(text: str):
    """回答本文をtext、code_span、fenced_codeの領域へ分類して返す。

    各領域の値は元の文字列をそのまま保持する。コード領域を除外する
    変換と、callout用に行頭へ接頭辞だけを追加する変換が同じ分類を
    利用できるようにする。
    """
    text = str(text)
    cursor = 0
    for start, end in _fenced_code_ranges(text):
        if cursor < start:
            yield from _iter_inline_markdown_segments(text[cursor:start])
        yield "fenced_code", text[start:end]
        cursor = end
    if cursor < len(text):
        yield from _iter_inline_markdown_segments(text[cursor:])


def _normalize_markdown_links_in_line(line: str) -> str:
    """コードスパンを保ったまま、Markdownリンクを正規化する。"""
    result = []
    index = 0
    while index < len(line):
        if line[index] != "[":
            result.append(line[index])
            index += 1
            continue

        label_end = index + 1
        depth = 1
        escaped = False
        while label_end < len(line):
            character = line[label_end]
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == "[":
                depth += 1
            elif character == "]":
                depth -= 1
                if depth == 0:
                    break
            label_end += 1
        if label_end >= len(line):
            result.append(line[index])
            index += 1
            continue

        open_paren = label_end + 1
        while open_paren < len(line) and line[open_paren] in " \t":
            open_paren += 1
        if open_paren >= len(line) or line[open_paren] != "(":
            result.append(line[index:label_end + 1])
            index = label_end + 1
            continue

        close_paren = _find_link_end(line, open_paren + 1)
        if close_paren is None:
            result.append(line[index])
            index += 1
            continue

        raw_destination = line[open_paren + 1:close_paren]
        destination = raw_destination
        prefix = ""
        suffix = ""
        if raw_destination.startswith("<"):
            destination_end = raw_destination.find(">", 1)
            if destination_end != -1:
                destination = raw_destination[1:destination_end]
                prefix = "<"
                suffix = ">" + raw_destination[destination_end + 1:]
        elif "\n" not in raw_destination:
            # タイトル付きリンクはタイトルを残し、パス部分だけを変換する。
            title_match = re.match(r"^(\S+)(\s+(?:\"[^\"]*\"|'[^']*'|\([^)]*\)))$", raw_destination)
            if title_match:
                destination, suffix = title_match.groups()

        normalized = _normalize_link_destination(destination)
        if normalized == destination:
            result.append(line[index:close_paren + 1])
        else:
            result.append(
                line[index:open_paren + 1]
                + prefix
                + normalized
                + suffix
                + ")"
            )
        index = close_paren + 1
    return "".join(result)


def normalize_local_file_links(text: str) -> str:
    """回答本文のローカル絶対パスリンクをfile URIへ変換する。"""
    result = []
    for kind, segment in iter_markdown_segments(text):
        if kind == "text":
            result.append(_normalize_markdown_links_in_line(segment))
        else:
            result.append(segment)
    return "".join(result)


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
    segments = list(iter_markdown_segments(text))
    lines = [[]]
    for kind, segment in segments:
        for part in segment.split("\n")[:-1]:
            lines[-1].append((kind, part))
            lines.append([])
        if segment:
            lines[-1].append((kind, segment.split("\n")[-1]))

    output = []
    for line_parts in lines:
        transformed = []
        first_non_space = True
        for kind, part in line_parts:
            if kind == "text":
                value = part.replace("<", "&lt;")
                if first_non_space:
                    leading = len(value) - len(value.lstrip())
                    if value[leading:leading + 1] == ">":
                        value = value[:leading] + "&gt;" + value[leading + 1:]
                if value.strip():
                    first_non_space = False
                transformed.append(value)
            else:
                if first_non_space and part.lstrip():
                    first_non_space = False
                transformed.append(part)
        line = "".join(transformed)
        output.append(f"> {line}" if line else ">")
    return "\n".join(output)
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
    body = normalize_local_file_links(text)
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


def parse_statusline_quota(data: dict) -> dict:
    """Claude Code statusLine入力のrate_limitsをquota観測値へ正規化する。"""
    limits = data.get("rate_limits")
    if not isinstance(limits, dict):
        return {}
    windows = {}
    for raw_key, window in (("five_hour", "5h"), ("seven_day", "weekly")):
        source = limits.get(raw_key)
        if not isinstance(source, dict):
            continue
        used = source.get("used_percentage")
        if isinstance(used, bool) or not isinstance(used, (int, float)):
            continue
        used = float(used)
        if not math.isfinite(used) or not 0 <= used <= 100:
            continue
        reset = ""
        try:
            reset = datetime.fromtimestamp(int(source["resets_at"]), tz=timezone.utc).astimezone(JST).strftime("%Y-%m-%d %H:%M")
        except (KeyError, TypeError, ValueError, OSError, OverflowError):
            pass
        windows[window] = {"remaining": round(100 - used, 10), "reset": reset,
                           "observed_at": format_iso(now_jst())}
    return {"claude": windows} if windows else {}


def build_quota_section(first: dict | None, last: dict | None) -> str:
    first_windows = (first or {}).get("claude", {})
    last_windows = (last or {}).get("claude", {})
    parts = []
    for window, label in (("weekly", "W"), ("5h", "5h")):
        initial = first_windows.get(window, {}).get("remaining")
        final = last_windows.get(window, {}).get("remaining")
        if initial is None and final is None:
            continue
        if initial is not None and final is not None and initial != final:
            parts.append(f"{label}: {initial:.1f}% ➔ {final:.1f}%")
        else:
            parts.append(f"{label}: {(final if final is not None else initial):.1f}%")
    return "📊 **Quota**:\n- **Claude**: " + " / ".join(parts) + "\n" if parts else ""


@session_locked("session_id")
def capture_statusline_quota(data: dict) -> str:
    """statusLine経由の観測値を保存し、statusLineに表示する短い文字列を返す。"""
    session_id = str(data.get("session_id") or "")
    quota = parse_statusline_quota(data)
    if not session_id or not quota:
        return ""
    state = load_state(session_id)
    state["initial_quota"] = merge_first_quota(state.get("initial_quota"), quota)
    quota = merge_first_quota(quota, state.get("final_quota") or {})
    state["final_quota"] = quota
    save_state(session_id, state)
    output_path = state.get("output_path")
    if output_path and Path(output_path).exists():
        path = Path(output_path)
        content = path.read_text(encoding="utf-8")
        refreshed = update_markdown_metadata(
            content, now_jst(), 0, read_last_line_from_md(path) or 0,
            initial_quota=state["initial_quota"], final_quota=quota,
        )
        if refreshed != content:
            atomic_write_md(path, refreshed)
    return build_quota_section(quota, quota).splitlines()[-1].removeprefix("- **Claude**: ")


USAGE_TOKEN_FIELDS = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens",
                      "output_tokens", "reasoning_output_tokens", "total_tokens")
# 標準API単価の比較値。契約上の請求額やサブスクリプション消費量ではない。
# https://platform.claude.com/docs/en/about-claude/pricing
CLAUDE_PRICE_PER_1M = {
    "claude-opus-5-5": ("4", "20"),
    "claude-opus-5": ("5", "25"),
    "claude-opus-4-8": ("5", "25"),
    "claude-opus-4-7": ("5", "25"),
    "claude-opus-4-6": ("5", "25"),
    "claude-opus-4-5": ("5", "25"),
    "claude-sonnet-5": ("2", "10"),
    "claude-sonnet-4-6": ("3", "15"),
    "claude-sonnet-4-5": ("3", "15"),
    "claude-haiku-4-5": ("1", "5"),
}


def _claude_usage_record(message: dict) -> dict | None:
    raw = message.get("usage")
    if not isinstance(raw, dict):
        return None
    values = [raw.get("input_tokens"), raw.get("cache_creation_input_tokens", 0),
              raw.get("cache_read_input_tokens", 0), raw.get("output_tokens")]
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in values):
        return None
    fresh, written, cached, output = values
    usage = {
        "input_tokens": fresh + written + cached,
        "cached_input_tokens": cached,
        "cache_write_input_tokens": written,
        "output_tokens": output,
        "reasoning_output_tokens": 0,
        "total_tokens": fresh + written + cached + output,
    }
    model = str(message.get("model") or "unknown")
    for prefix, (input_rate, output_rate) in CLAUDE_PRICE_PER_1M.items():
        if model == prefix or model.startswith(prefix + "-"):
            break
    else:
        return usage
    cache_creation = raw.get("cache_creation") or {}
    five_min = cache_creation.get("ephemeral_5m_input_tokens", 0) if isinstance(cache_creation, dict) else None
    one_hour = cache_creation.get("ephemeral_1h_input_tokens", 0) if isinstance(cache_creation, dict) else None
    if (not isinstance(five_min, int) or not isinstance(one_hour, int)
            or isinstance(five_min, bool) or isinstance(one_hour, bool)
            or five_min < 0 or one_hour < 0 or five_min + one_hour != written
            or usage["input_tokens"] > 200_000
            or raw.get("speed", "standard") not in (None, "standard")
            or raw.get("service_tier", "standard") not in (None, "standard")
            or raw.get("inference_geo") not in (None, "", "global")):
        return usage
    rate = Decimal(input_rate)
    cost = (Decimal(fresh) * rate + Decimal(cached) * rate * Decimal("0.1")
            + Decimal(five_min) * rate * Decimal("1.25")
            + Decimal(one_hour) * rate * 2 + Decimal(output) * Decimal(output_rate)) / Decimal(1_000_000)
    usage["cost_usd"] = cost
    return usage


def retrieve_usage(jsonl_path: Path) -> dict | None:
    """同じmessage IDの更新行は最後のusageだけを数える。"""
    records = {}
    try:
        with jsonl_path.open(encoding="utf-8") as stream:
            for line_number, line in enumerate(stream):
                try:
                    data = json.loads(line)
                except ValueError:
                    continue
                if data.get("type") != "assistant" or data.get("isSidechain"):
                    continue
                message = data.get("message")
                if not isinstance(message, dict):
                    continue
                usage = _claude_usage_record(message)
                if usage is None:
                    continue
                message_id = str(message.get("id") or data.get("uuid") or line_number)
                records[message_id] = (str(message.get("model") or "unknown"), usage)
    except (OSError, UnicodeError) as error:
        logger.warning("Usage情報の読み込みに失敗: %s", error)
        return None
    if not records:
        return None
    total = {field: 0 for field in USAGE_TOKEN_FIELDS}
    by_model = {}
    priced = 0
    for model, usage in records.values():
        entry = by_model.setdefault(model, {field: 0 for field in USAGE_TOKEN_FIELDS})
        for field in USAGE_TOKEN_FIELDS:
            total[field] += usage[field]
            entry[field] += usage[field]
        if "cost_usd" in usage:
            priced += 1
            entry["cost_usd"] = entry.get("cost_usd", Decimal(0)) + usage["cost_usd"]
    total["by_model"] = by_model
    if priced:
        total["total_cost_usd"] = sum((entry.get("cost_usd", Decimal(0)) for entry in by_model.values()), Decimal(0))
        if priced < len(records):
            total["cost_incomplete"] = True
    else:
        total["cost_unavailable"] = True
    return total


def build_usage_frontmatter_fields(usage: dict | None) -> list[str]:
    if not usage:
        return []
    lines = ["usage:"]
    lines.extend(f"  {field}: {usage[field]}" for field in USAGE_TOKEN_FIELDS)
    if "total_cost_usd" in usage:
        lines.append(f"  total_cost_usd: {usage['total_cost_usd']}")
        if usage.get("cost_incomplete"):
            lines.append("  cost_incomplete: true")
    else:
        lines.append("  cost_unavailable: true")
    lines.append("  by_model:")
    for model, entry in usage["by_model"].items():
        lines.append(f"    {yaml_quote(model)}:")
        lines.extend(f"      {field}: {entry[field]}" for field in USAGE_TOKEN_FIELDS)
        if "cost_usd" in entry:
            lines.append(f"      cost_usd: {entry['cost_usd']}")
        else:
            lines.append("      cost_unavailable: true")
    return lines


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
    usage: dict | None = None,
    initial_quota: dict | None = None,
    final_quota: dict | None = None,
) -> str:
    ts = format_iso(start_dt)
    fm_lines = [
        "---",
        f"source: {SOURCE_NAME}",
        *( [f"title: {yaml_quote(title)}"] if title is not None else [] ),
        f"session_id: {yaml_quote(session_id)}",
        f"project: {yaml_quote(cwd)}",
        f"created: {yaml_quote(ts)}",
        f"modified: {yaml_quote(ts)}",
        "tags:",
        "  - ai-conversation",
        f"  - {SOURCE_NAME}",
        f"message_count: {message_count}",
    ]
    fm_lines.extend(build_quota_frontmatter_fields(initial_quota, final_quota))
    fm_lines.extend(build_usage_frontmatter_fields(usage))
    fm_lines.append("---")
    return "\n".join(fm_lines) + "\n\n"


def create_md_file(
    session_id: str,
    cwd: str,
    start_dt: datetime,
    title: str | None = None,
    usage: dict | None = None,
    initial_quota: dict | None = None,
    final_quota: dict | None = None,
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
    fm = build_frontmatter(session_id, cwd, start_dt, message_count=0, title=title,
                           usage=usage, initial_quota=initial_quota, final_quota=final_quota)
    quota_section = build_quota_section(initial_quota, final_quota)
    usage_section = build_usage_section(usage)
    content = fm + (quota_section + "\n" if quota_section else "") + (usage_section + "\n" if usage_section else "") + "<!-- last_line: 0 -->\n\n"
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


def build_usage_section(usage: dict | None) -> str:
    if not usage:
        return ""
    cost = usage.get("total_cost_usd")
    amount = f" · ${cost}" if cost is not None else " · USD換算不可"
    return f"📈 **Usage**: {usage['total_tokens']:,} tokens{amount}\n"


def _without_frontmatter_keys(content: str, keys: set[str]) -> str:
    match = re.match(r"^(---\s*\n)(.*?)(\n---\s*\n)", content, re.DOTALL)
    if not match:
        return content
    lines = []
    skipping = False
    for line in match.group(2).splitlines():
        if any(line.startswith(key + ":") for key in keys):
            skipping = True
            continue
        if skipping and (line.startswith((" ", "\t")) or not line.strip()):
            continue
        skipping = False
        lines.append(line)
    return match.group(1) + "\n".join(lines) + match.group(3) + content[match.end():]


def update_markdown_metadata(
    content: str,
    modified_dt: datetime,
    appended: int,
    last_line: int,
    usage: dict | None = None,
    initial_quota: dict | None = None,
    final_quota: dict | None = None,
) -> str:
    """本文のfrontmatterとcursorを更新し、書き込み前の全文を返す。"""
    removed = {"usage"} if usage is not None else set()
    if initial_quota or final_quota:
        removed.add("quota")
    source = _without_frontmatter_keys(content, removed) if removed else content
    updated = update_frontmatter_fields(
        source, modified_dt, appended,
        extra_fields=tuple(build_quota_frontmatter_fields(initial_quota, final_quota)
                           + build_usage_frontmatter_fields(usage)),
    )
    if updated is None:
        return content
    new_fm_lines, body_text = updated
    if initial_quota or final_quota:
        section = build_quota_section(initial_quota, final_quota)
        body_text = re.sub(r"(?m)^📊 \*\*Quota\*\*:\n- \*\*Claude\*\*:.*\n(?:\n)?", "", body_text, count=1)
        body_text = section + "\n" + body_text.lstrip("\n")
    if usage is not None:
        section = build_usage_section(usage)
        body_text = re.sub(r"(?m)^📈 \*\*Usage\*\*:.*\n(?:\n)?", "", body_text, count=1)
        body_text = section + "\n" + body_text.lstrip("\n")
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
    usage: dict | None = None,
    initial_quota: dict | None = None,
    final_quota: dict | None = None,
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
    final_content = update_markdown_metadata(new_content, modified_dt, appended, last_line, usage,
                                             initial_quota, final_quota)
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
    current_usage = retrieve_usage(jsonl_path)

    state = load_state(session_id)
    if not state or "output_path" not in state:
        cleanup_old_states()
        try:
            recovered_path = find_existing_md(session_id)
        except ExistingMarkdownSearchError as e:
            logger.error(f"既存Markdownの探索中にエラーが発生したため保存を見送ります: {e}")
            return
        if recovered_path is not None:
            recovered_last_line = read_last_line_from_md(recovered_path) or 0
            start_dt = datetime.fromtimestamp(recovered_path.stat().st_mtime, tz=JST)
            recovered_content = recovered_path.read_text(encoding="utf-8")
            first_quota, last_quota = recover_quota_frontmatter(recovered_content)
            state = {
                "output_path": str(recovered_path),
                "start_time": start_dt.isoformat(),
                "cwd": cwd,
                "last_line": recovered_last_line,
                "initial_quota": merge_first_quota(first_quota, state.get("initial_quota") or {}),
                "final_quota": merge_first_quota(state.get("final_quota"), last_quota),
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
            usage=current_usage,
            initial_quota=state.get("initial_quota"),
            final_quota=state.get("final_quota"),
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
            usage=current_usage,
            initial_quota=state.get("initial_quota"),
            final_quota=state.get("final_quota"),
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
            usage=current_usage,
            initial_quota=state.get("initial_quota"),
            final_quota=state.get("final_quota"),
        )
        if updated:
            state["last_line"] = new_last_line
            save_state(session_id, state)
            logger.info(f"{appended}件追記完了 {session_id[:8]} (last_line: {new_last_line})")
    elif new_last_line != last_line:
        advance_last_line(path, new_last_line)
        state["last_line"] = new_last_line
        save_state(session_id, state)

    if (current_usage is not None or state.get("final_quota")) and not new_messages:
        content = path.read_text(encoding="utf-8")
        refreshed = update_markdown_metadata(
            content, now_jst(), 0, new_last_line, current_usage,
            state.get("initial_quota"), state.get("final_quota"),
        )
        if refreshed != content:
            atomic_write_md(path, refreshed)

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
        if "--capture-quota" in sys.argv[1:]:
            print(capture_statusline_quota(read_stdin()))
        else:
            main()
    except Exception as e:
        logger.exception(f"予期しないエラー: {e}")
        print("{}")
        sys.exit(0)
