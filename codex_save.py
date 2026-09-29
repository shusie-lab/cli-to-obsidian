#!/usr/bin/python3
from __future__ import annotations
"""
Codex CLI の会話履歴を Obsidian に自動保存するフックスクリプト。
出力形式: Obsidian AI Exporter 互換（YAMLフロントマター + callout block形式）

対応イベント:
  - Stop : ターン終了時に Markdown ファイルの作成・追記を行う
"""

__version__ = "3.2.0"

from decimal import Decimal
from bisect import bisect_right
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
DEFAULT_SOURCE = "codex-cli"

# Usage の標準 API 単価（USD / 1,000,000 tokens）。input_tokens には
# cached_input_tokens と cache_write_input_tokens が含まれるため、通常入力の
# 単価はそれらを除いた残りにだけ適用する。推論トークンは output_tokens の内数。
# Codex の契約上の請求額・quota 消費額や、ツール料金・割引モードは表せない。
# 引用元 https://developers.openai.com/api/docs/pricing
USAGE_COST_USD_PER_1M_TOKEN_TYPE = {
    "gpt-6-astra": {
        "input": Decimal("10.00"),
        "cached_input": Decimal("1.00"),
        "cache_write_input": Decimal("12.50"),
        "output": Decimal("50.00"),
    },
    "gpt-6-sol": {
        "input": Decimal("2.00"),
        "cached_input": Decimal("0.20"),
        "cache_write_input": Decimal("2.50"),
        "output": Decimal("10.00"),
    },
    "gpt-6-luna": {
        "input": Decimal("0.10"),
        "cached_input": Decimal("0.01"),
        "cache_write_input": Decimal("0.125"),
        "output": Decimal("0.50"),
    },
    "gpt-5.6-sol": {
        "input": Decimal("4.00"),
        "cached_input": Decimal("0.40"),
        "cache_write_input": Decimal("5.00"),
        "output": Decimal("20.00"),
    },
    "gpt-5.6-terra": {
        "input": Decimal("2.00"),
        "cached_input": Decimal("0.20"),
        "cache_write_input": Decimal("2.50"),
        "output": Decimal("12.00"),
    },
    "gpt-5.6-luna": {
        "input": Decimal("0.20"),
        "cached_input": Decimal("0.02"),
        "cache_write_input": Decimal("0.25"),
        "output": Decimal("1.20"),
    },
}


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

STATE_DIR = Path.home() / ".codex" / "codex-obsidian" / "state"
LOG_FILE = Path.home() / ".codex" / "codex-obsidian" / "log" / "codex_obsidian_save.log"

STATE_RETENTION_DAYS = 30
LAST_LINE_PATTERN = re.compile(r"<!--\s*last_line:\s*(\d+)\s*-->")
AGENT_NAME = "Codex"
SAFE_SESSION_CHARS = re.compile(r"[^A-Za-z0-9_.-]+")
PROJECTLESS_LABEL = "projectless"
STRUCTURED_USER_HEADERS = (
    "## Referenced ChatGPT conversation:",
    "# Files mentioned by the user:",
)
STRUCTURED_REQUEST_MARKER = "## My request:"
REFERENCE_CALLOUT_HEADER = "> [!INFO]- 参照情報（原文）"
QUESTION_REPLY_START = "<send_user_message_question_reply>"
QUESTION_REPLY_END = "</send_user_message_question_reply>"
TOOL_USE_ITEM_TYPES = frozenset({
    "CollabAgentToolCall",
    "CommandExecution",
    "FileChange",
    "McpToolCall",
})
TOOL_USE_RESPONSE_TYPES = frozenset({
    "custom_tool_call",
    "custom_tool_call_output",
    "function_call",
    "function_call_output",
})

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


def split_structured_user_message(text: str) -> tuple[str, str | None]:
    """既知形式の移行メッセージを依頼本文と前置き原文へ分ける。

    構造を確実に確認できない場合は、入力全体を従来どおりの本文として返す。
    """
    lines = text.splitlines(keepends=True)
    first_nonempty = next(
        (line.rstrip("\r\n") for line in lines if line.rstrip("\r\n").strip()),
        "",
    )
    if first_nonempty not in STRUCTURED_USER_HEADERS:
        return text, None

    marker_positions = []
    active_fence = None
    offset = 0
    for line in lines:
        content = line.rstrip("\r\n")
        marker = _fence_marker(line)
        if active_fence is not None:
            if (
                marker is not None
                and marker[0] == active_fence[0]
                and marker[1] >= active_fence[1]
                and not marker[2].strip()
            ):
                active_fence = None
        elif content == STRUCTURED_REQUEST_MARKER:
            marker_positions.append((offset, offset + len(line)))
        elif marker is not None:
            active_fence = marker[:2]
        offset += len(line)

    if len(marker_positions) != 1:
        return text, None

    marker_start, body_start = marker_positions[0]
    request = text[body_start:]
    if not request.strip():
        return text, None
    return request, text[:marker_start]


def parse_question_reply(text: str) -> str | None:
    """質問UIの選択結果をCodex回答へ連結する表示本文へ変換する。

    Codex DesktopはUIからの回答をUserMessageとして記録するが、これは
    ユーザーの自由発話ではなく、直前のCodex質問に対する選択結果である。
    形式を確実に確認できない場合はNoneを返し、従来のUserMessage処理へ戻す。
    """
    normalized = text.strip()
    if not (
        normalized.startswith(QUESTION_REPLY_START)
        and normalized.endswith(QUESTION_REPLY_END)
    ):
        return None

    payload_text = normalized[
        len(QUESTION_REPLY_START) : -len(QUESTION_REPLY_END)
    ].strip()
    try:
        payload = json.loads(payload_text)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, list) or not payload:
        return None

    selections = []
    for item in payload:
        if not isinstance(item, dict):
            return None
        question = item.get("question")
        answer = item.get("answer")
        if not isinstance(question, str) or not isinstance(answer, str):
            return None
        if not question.strip() or not answer.strip():
            return None
        selections.append(f"- {question.strip()} → {answer.strip()}")

    return "**ユーザーの選択**\n\n" + "\n".join(selections)


def reference_callout(text: str) -> str:
    """前置き原文を、内容を解釈せず折りたたみINFO calloutへ入れる。"""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    max_backticks = max(
        (len(run) for run in re.findall(r"`+", normalized)),
        default=0,
    )
    fence = "`" * max(3, max_backticks + 1)
    raw_lines = (fence + "\n" + normalized + "\n" + fence).split("\n")
    body = "\n".join(f"> {line}" if line else ">" for line in raw_lines)
    return f"{REFERENCE_CALLOUT_HEADER}\n>\n{body}"


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


def retrieve_quota(jsonl_path: Path, start_line: int = 0, *, first: bool = False) -> dict | None:
    """start_line より後ろにある各枠の最初または最後の観測を取得する。"""
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
                    for windows in quota.values():
                        for window in windows.values():
                            window["observed_at"] = str(data.get("timestamp") or "")
                    latest_quota = (merge_first_quota(latest_quota, quota) if first
                                    else merge_first_quota(quota, latest_quota or {}))
    except Exception as e:
        logger.warning(f"Quota情報の読み込みに失敗: {jsonl_path}: {e}")

    return latest_quota


# ============================================================
# Usage 集計ユーティリティ
# ============================================================
USAGE_TOKEN_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)
LONG_CONTEXT_INPUT_TOKENS = 272_000


def _empty_usage_totals() -> dict[str, int]:
    return {field: 0 for field in USAGE_TOKEN_FIELDS}


def _add_usage_totals(target: dict[str, int], raw_usage: object) -> bool:
    """usage オブジェクトの数値フィールドを target へ加算する。"""
    if not isinstance(raw_usage, dict):
        return False
    values = {}
    for field in USAGE_TOKEN_FIELDS:
        value = raw_usage.get(field, 0)
        if isinstance(value, bool):
            return False
        try:
            numeric = int(value)
        except (TypeError, ValueError):
            return False
        if numeric < 0:
            return False
        values[field] = numeric
    for field, value in values.items():
        target[field] += value
    return True


def _usage_cost(model: str, usage: dict[str, int]) -> Decimal | None:
    """モデル別・token種別の標準API単価からUSD概算を返す。"""
    rates = USAGE_COST_USD_PER_1M_TOKEN_TYPE.get(model)
    if rates is None:
        return None
    cached_input = usage["cached_input_tokens"]
    cache_write_input = usage["cache_write_input_tokens"]
    uncached_input = usage["input_tokens"] - cached_input - cache_write_input
    if uncached_input < 0:
        return None
    # 272K input tokens を超えるリクエストは、標準API価格で入力・キャッシュ系が
    # 2倍、出力が1.5倍になる。セッション合計ではなく各recordで判定する。
    long_context = usage["input_tokens"] > LONG_CONTEXT_INPUT_TOKENS
    input_multiplier = Decimal(2) if long_context else Decimal(1)
    output_multiplier = Decimal("1.5") if long_context else Decimal(1)
    return (
        Decimal(uncached_input) * rates["input"] * input_multiplier
        + Decimal(cached_input) * rates["cached_input"] * input_multiplier
        + Decimal(cache_write_input) * rates["cache_write_input"] * input_multiplier
        + Decimal(usage["output_tokens"]) * rates["output"] * output_multiplier
    ) / Decimal(1_000_000)


def retrieve_usage(
    jsonl_path: Path, session_id: str = "", subagent_records: list[dict] | None = None,
) -> dict | None:
    """親と子のtranscriptの token_usage_record をモデル別に集計する。

    ``thread_token_usage`` は再開前の使用量も含む累計値になり得るため使わず、各
    record の ``payload.usage`` を一度ずつ加算する。途中書き込みや壊れた行は
    hook の会話保存を止めないよう無視する。
    """
    records = _read_usage_records(jsonl_path)
    if session_id:
        records.extend(
            subagent_records if subagent_records is not None
            else _read_subagent_usage_records(jsonl_path, session_id)
        )
    return _summarize_usage_records(records)


def _find_subagent_transcripts(jsonl_path: Path, session_id: str) -> list[Path]:
    """session_meta の親子関係をたどり、この会話の子transcriptだけを返す。"""
    sessions_root = jsonl_path.parent
    if len(jsonl_path.parents) >= 4 and jsonl_path.parents[3].name == "sessions":
        sessions_root = jsonl_path.parents[3]

    children: dict[str, tuple[str, Path]] = {}
    try:
        candidates = sessions_root.rglob("rollout-*.jsonl")
        for candidate in candidates:
            if candidate == jsonl_path:
                continue
            if candidate.is_symlink():
                continue
            try:
                with candidate.open("r", encoding="utf-8") as stream:
                    metadata = json.loads(stream.readline())
            except (OSError, UnicodeError, ValueError):
                continue
            if not isinstance(metadata, dict):
                continue
            if metadata.get("type") != "session_meta":
                continue
            payload = metadata.get("payload")
            if not isinstance(payload, dict) or not isinstance(payload.get("source"), dict):
                continue
            subagent = payload["source"].get("subagent")
            if not isinstance(subagent, dict) or "thread_spawn" not in subagent:
                continue
            child_id = payload.get("id")
            parent_id = payload.get("parent_thread_id")
            if isinstance(child_id, str) and isinstance(parent_id, str):
                children[child_id] = (parent_id, candidate)
    except OSError as error:
        logger.warning("サブエージェントのtranscript探索に失敗: %s", error)

    descendants = {session_id}
    paths = []
    remaining = dict(children)
    while remaining:
        found = False
        for child_id, (parent_id, path) in list(remaining.items()):
            if parent_id in descendants:
                descendants.add(child_id)
                paths.append(path)
                del remaining[child_id]
                found = True
        if not found:
            break
    return paths


def _read_subagent_usage_records(jsonl_path: Path, session_id: str) -> list[dict]:
    """子transcriptから、この親セッションに属するusage recordを集める。"""
    records = []
    for child_path in _find_subagent_transcripts(jsonl_path, session_id):
        records.extend(_read_usage_records(child_path, session_id=session_id))
    return records


def _read_usage_records(jsonl_path: Path, session_id: str = "") -> list[dict]:
    """有効なusage recordを読み、行番号・turn・モデルとともに返す。"""
    records = []
    current_model = ""
    models_by_turn: dict[str, str] = {}
    try:
        with open(jsonl_path, "r", encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, start=1):
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if data.get("type") == "turn_context":
                    payload = data.get("payload", {})
                    if isinstance(payload, dict):
                        turn_id = str(payload.get("turn_id") or "")
                        if payload.get("model"):
                            current_model = str(payload["model"])
                            if turn_id:
                                models_by_turn[turn_id] = current_model
                    continue

                if data.get("type") != "token_usage_record":
                    continue
                payload = data.get("payload", {})
                if not isinstance(payload, dict):
                    continue
                if session_id and payload.get("session_id") != session_id:
                    continue
                raw_usage = payload.get("usage")
                record_totals = _empty_usage_totals()
                if not _add_usage_totals(record_totals, raw_usage):
                    continue
                turn_id = str(payload.get("turn_id") or "")
                model = str(
                    payload.get("model")
                    or data.get("model")
                    or models_by_turn.get(turn_id)
                    or current_model
                    or "unknown"
                )
                records.append({
                    "line_number": line_number,
                    "turn_id": turn_id,
                    "root_turn_id": str(payload.get("root_turn_id") or ""),
                    "model": model,
                    "usage": record_totals,
                })
    except (OSError, UnicodeError) as error:
        logger.warning("Usage情報の読み込みに失敗: %s: %s", jsonl_path, error)
    return records


def _summarize_usage_records(records: list[dict]) -> dict | None:
    """usage record群を、セッションまたは回答単位の表示形式へ集計する。"""
    total = _empty_usage_totals()
    by_model: dict[str, dict[str, int]] = {}
    cost_by_model: dict[str, Decimal] = {}
    unpriced_models: set[str] = set()
    long_context = False
    if not records:
        return None
    for record in records:
        record_totals = record["usage"]
        model = record["model"]
        model_totals = by_model.setdefault(model, _empty_usage_totals())
        for field, value in record_totals.items():
            total[field] += value
            model_totals[field] += value
        is_long_context = record_totals["input_tokens"] > LONG_CONTEXT_INPUT_TOKENS
        long_context = long_context or is_long_context
        record_cost = _usage_cost(model, record_totals)
        if record_cost is None:
            unpriced_models.add(model)
        else:
            cost_by_model[model] = cost_by_model.get(model, Decimal(0)) + record_cost

    usage = dict(total)
    usage["by_model"] = {}
    total_cost = Decimal(0)
    has_cost = False
    has_unpriced = False
    for model, model_totals in by_model.items():
        entry = dict(model_totals)
        cost = cost_by_model.get(model)
        if cost is not None:
            entry["cost_usd"] = cost
            total_cost += cost
            has_cost = True
        if model in unpriced_models:
            entry["cost_unavailable"] = True
            has_unpriced = True
        usage["by_model"][model] = entry
    if has_cost:
        usage["total_cost_usd"] = total_cost
        if has_unpriced:
            usage["cost_incomplete"] = True
    else:
        usage["cost_unavailable"] = True
    if long_context:
        usage["long_context"] = True
    return usage


# ============================================================
# transcript（JSONL）のパース
# ============================================================
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


def is_tool_use_event(data: dict) -> bool:
    """保存対象外のツール実行イベントを、回答間の区切り判定だけに使う。"""
    payload = data.get("payload", {})
    if not isinstance(payload, dict):
        return False
    if data.get("type") == "response_item":
        return payload.get("type") in TOOL_USE_RESPONSE_TYPES
    if data.get("type") != "event_msg" or payload.get("type") != "item_completed":
        return False
    item = payload.get("item", {})
    return isinstance(item, dict) and item.get("type") in TOOL_USE_ITEM_TYPES


def load_jsonl_messages(
    jsonl_path: Path, start_line: int, subagent_records: list[dict] | None = None,
) -> tuple[list[dict], int]:
    """
    JSONLファイルを読み込み、start_line (1-indexed) より後ろの行を処理する。
    event_msg 内の user_message / agent_message を抽出して返す。
    読み込んだ最終行番号 (1-indexed) も返す。
    """
    messages = []
    current_line = 0
    current_model = ""
    current_effort = ""
    current_turn_id = ""
    tool_use_before_next_agent = False
    incomplete_last_line = None
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
                            current_turn_id = str(context.get("turn_id") or current_turn_id)
                            current_model = context.get("model", "") or current_model
                            current_effort = (
                                context.get("effort")
                                or context.get("reasoning_effort")
                                or current_effort
                            )

                    if is_tool_use_event(data):
                        tool_use_before_next_agent = True
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

                        if message_type not in ("user_message", "agent_message"):
                            continue

                        question_reply = parse_question_reply(message)
                        if question_reply is not None:
                            message_type = "agent_message"
                            message = question_reply

                        if message_type == "user_message":
                            tool_use_before_next_agent = False

                        message_turn_id = str(
                            payload.get("turn_id")
                            or data.get("turn_id")
                            or current_turn_id
                        )
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
                            "turn_id": message_turn_id,
                            "after_tool_use": tool_use_before_next_agent,
                        })
                        if message_type == "agent_message":
                            tool_use_before_next_agent = False
                except json.JSONDecodeError as e:
                    if not raw_line.endswith(("\n", "\r")):
                        logger.debug(f"書き込み途中のJSONL最終行を次回へ延期: line {current_line}: {e}")
                        incomplete_last_line = current_line - 1
                        break
                    logger.debug(f"JSONL parse error at line {current_line}: {e}")
                    continue
    except Exception as e:
        logger.warning(f"JSONL読み込み失敗: {jsonl_path}: {e}")
        return [], start_line

    # token_usage_record は visible なAgentMessageとは別の行に記録される。
    # 同じturn内で直前にある回答へ寄せ、回答より前に現れるrecordはそのturnの
    # 最初の回答へ寄せる。これにより、hidden reasoning/tool responseを含む全recordを
    # 一度だけ回答metadataへ割り当て、回答単位の合計とsession合計を一致させる。
    agent_indices_by_turn: dict[str, list[int]] = {}
    agent_indices = []
    for index, message in enumerate(messages):
        if message["type"] != "agent_message":
            continue
        agent_indices.append(index)
        turn_id = str(message.get("turn_id") or "")
        agent_indices_by_turn.setdefault(turn_id, []).append(index)

    assigned: dict[int, list[dict]] = {}
    records = _read_usage_records(jsonl_path)
    for record in records:
        candidates = agent_indices_by_turn.get(record["turn_id"], [])
        if candidates:
            preceding = [
                index for index in candidates
                if messages[index]["line_number"] <= record["line_number"]
            ]
            target = preceding[-1] if preceding else candidates[0]
        elif agent_indices:
            # turn_idが出力されない旧形式でも、最も近いvisible回答に割り当てる。
            target = min(
                agent_indices,
                key=lambda index: abs(messages[index]["line_number"] - record["line_number"]),
            )
        else:
            continue
        assigned.setdefault(target, []).append(record)

    # 子の行番号は親transcriptと比較できない。root_turn_idで親ターンを特定し、
    # そのターンの最後の表示可能な回答へ寄せる。
    child_assigned: dict[int, list[dict]] = {}
    for record in subagent_records or []:
        root_turn_id = record.get("root_turn_id")
        if not root_turn_id:
            continue
        candidates = [
            index for index in agent_indices_by_turn.get(root_turn_id, [])
            if str(messages[index].get("message", "")).strip()
        ]
        if candidates:
            child_assigned.setdefault(candidates[-1], []).append(record)

    for index in set(assigned) | set(child_assigned):
        parent_records = assigned.get(index, [])
        child_records = child_assigned.get(index, [])
        parent_usage = _summarize_usage_records(parent_records)
        child_usage = _summarize_usage_records(child_records)
        messages[index]["usage"] = _merge_usage_summaries(parent_usage, child_usage)
        if child_usage is not None:
            messages[index]["subagent_usage"] = child_usage
        messages[index]["usage_records"] = parent_records + child_records

    filtered = [message for message in messages if message["line_number"] > start_line]
    return filtered, incomplete_last_line if incomplete_last_line is not None else current_line

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


def _format_usd(value: object) -> str | None:
    if value is None:
        return None
    try:
        return f"${format(Decimal(str(value)), 'f')}"
    except (ArithmeticError, TypeError, ValueError):
        return None


def build_usage_section(usage: dict | None) -> str:
    """Usage の換算額だけを本文先頭の一行に表示する。"""
    if not usage:
        return "💰 **Usage (USD換算)**: N/A\n"

    total_cost = _format_usd(usage.get("total_cost_usd"))
    total_label = total_cost or "換算不可"
    model_parts = []
    for model, model_usage in usage.get("by_model", {}).items():
        model_cost = _format_usd(model_usage.get("cost_usd"))
        model_label = model_cost or "換算不可"
        model_parts.append(f"{model} {model_label}")
    model_summary = f" ({'、'.join(model_parts)})" if model_parts else ""
    return f"💰 **Usage (USD換算)**: Total: {total_label}{model_summary}\n"


def format_message_usage(usage: dict | None, subagent_usage: dict | None = None) -> str:
    """回答calloutへ短く表示する回答単位Usageを返す。"""
    if not usage:
        return ""
    total_tokens = int(usage.get("total_tokens", 0))
    total_cost = _format_usd(usage.get("total_cost_usd"))
    cost_label = total_cost or "換算不可"
    warning = " ⚠︎ 272K+" if usage.get("long_context") else ""
    label = f"Usage: {total_tokens:,} tok · {cost_label}{warning}"
    if subagent_usage:
        model_parts = []
        for model, model_usage in sorted(subagent_usage.get("by_model", {}).items()):
            model_tokens = int(model_usage.get("total_tokens", 0))
            model_cost = _format_usd(model_usage.get("cost_usd")) or "換算不可"
            model_parts.append(f"{model} {model_tokens:,} tok・{model_cost}")
        if model_parts:
            label += f" ({'、'.join(model_parts)})"
    return label


def _assistant_header_model(header: str | None) -> str | None:
    """Usage値に依存しない、連続するCodex回答のモデル行を返す。"""
    if not header:
        return None
    lines = header.splitlines()
    if not lines or lines[0] != f"> [!NOTE] {AGENT_NAME}":
        return None
    return next((line for line in lines[1:] if line.startswith("> <small>🤖 ")), "")


def _merge_usage_summaries(first: dict | None, second: dict | None) -> dict | None:
    """ストリーミングfragmentを同じ回答へ連結する際にUsageも合算する。"""
    if not first:
        return second
    if not second:
        return first
    merged = _empty_usage_totals()
    for field in USAGE_TOKEN_FIELDS:
        merged[field] = int(first.get(field, 0)) + int(second.get(field, 0))
    by_model = {}
    for source in (first, second):
        for model, model_usage in source.get("by_model", {}).items():
            target = by_model.setdefault(model, _empty_usage_totals())
            for field in USAGE_TOKEN_FIELDS:
                target[field] += int(model_usage.get(field, 0))
            if "cost_usd" in model_usage:
                target["cost_usd"] = target.get("cost_usd", Decimal(0)) + Decimal(str(model_usage["cost_usd"]))
            if model_usage.get("cost_unavailable"):
                target["cost_unavailable"] = True
    merged["by_model"] = by_model
    if "total_cost_usd" in first or "total_cost_usd" in second:
        merged["total_cost_usd"] = sum(
            (Decimal(str(source.get("total_cost_usd", 0))) for source in (first, second)),
            Decimal(0),
        )
        if first.get("cost_unavailable") or second.get("cost_unavailable"):
            merged["cost_incomplete"] = True
    elif first.get("cost_unavailable") or second.get("cost_unavailable"):
        merged["cost_unavailable"] = True
    if first.get("cost_incomplete") or second.get("cost_incomplete"):
        merged["cost_incomplete"] = True
    if first.get("long_context") or second.get("long_context"):
        merged["long_context"] = True
    return merged


def _answer_usage_groups(messages: list[dict]) -> list[tuple[int, dict | None, dict | None]]:
    """User発話ごとにAgentMessageを回答単位へまとめる。"""
    groups: list[tuple[int, dict | None, dict | None]] = []
    turn_index = 0
    last_model = None
    for message in messages:
        if message.get("type") == "user_message":
            turn_index += 1
            last_model = None
            continue
        if message.get("type") != "agent_message" or not str(message.get("message", "")).strip():
            continue
        model = format_model_name(message.get("model", ""), message.get("effort", ""))
        if groups and model == last_model:
            _, usage, subagent_usage = groups[-1]
            groups[-1] = (
                turn_index,
                _merge_usage_summaries(usage, message.get("usage")),
                _merge_usage_summaries(subagent_usage, message.get("subagent_usage")),
            )
        else:
            groups.append((turn_index, message.get("usage"), message.get("subagent_usage")))
            last_model = model
    return groups


def _assistant_header_spans(content: str) -> list[tuple[int, int]]:
    """コードフェンス外のCodex assistant metadata行の範囲を返す。"""
    lines = content.splitlines(keepends=True)
    spans = []
    index = 0
    active_fence = None
    while index < len(lines):
        marker = _fence_marker(lines[index])
        if active_fence is not None:
            if (
                marker is not None
                and marker[0] == active_fence[0]
                and marker[1] >= active_fence[1]
                and not marker[2].strip()
            ):
                active_fence = None
            index += 1
            continue
        if marker is not None:
            active_fence = marker[:2]
            index += 1
            continue
        if lines[index].rstrip("\r\n") != f"> [!NOTE] {AGENT_NAME}":
            index += 1
            continue
        end = index + 1
        while end < len(lines) and lines[end].startswith("> <small>"):
            end += 1
        spans.append((index, end))
        index = end
    return spans


def refresh_answer_usage_metadata(content: str, messages: list[dict]) -> str:
    """遅れて書かれたusage recordを既存assistant calloutへ反映する。

    本文・ユーザーcallout・既存のquota行は変更せず、Codex metadata内のUsage行だけを
    親ターンとモデルの連続区間に沿って差し替える。過去に同じ回答が複数calloutへ
    分割された場合は、その最後のcalloutにUsageを表示する。
    """
    groups = _answer_usage_groups(messages)
    spans = _assistant_header_spans(content)
    if not groups or not spans:
        return content
    lines = content.splitlines(keepends=True)
    user_positions = []
    active_fence = None
    for index, line in enumerate(lines):
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
        if line.rstrip("\r\n") == "> [!QUESTION] User":
            user_positions.append(index)

    groups_by_turn: dict[int, list[tuple[dict | None, dict | None]]] = {}
    for turn_index, usage, subagent_usage in groups:
        groups_by_turn.setdefault(turn_index, []).append((usage, subagent_usage))
    spans_by_turn: dict[int, list[tuple[int, int]]] = {}
    for start, end in spans:
        turn_index = bisect_right(user_positions, start)
        spans_by_turn.setdefault(turn_index, []).append((start, end))

    replacements = []
    for turn_index, turn_spans in spans_by_turn.items():
        runs: list[list[tuple[int, int]]] = []
        last_model = None
        for start, end in turn_spans:
            model = next((line for line in lines[start + 1:end] if line.startswith("> <small>🤖 ")), "")
            if runs and model == last_model:
                runs[-1].append((start, end))
            else:
                runs.append([(start, end)])
            last_model = model
        turn_groups = groups_by_turn.get(turn_index, [])
        if len(runs) != len(turn_groups):
            logger.warning("回答Usageのcallout対応を特定できません: turn=%s", turn_index)
            continue
        for run, (usage, subagent_usage) in zip(runs, turn_groups):
            usage_text = format_message_usage(usage, subagent_usage)
            for run_index, (start, end) in enumerate(run):
                metadata_lines = [
                    line for line in lines[start:end]
                    if not re.match(r"^> <small>Usage: .*?</small>\r?\n?$", line)
                ]
                if run_index == len(run) - 1 and usage_text:
                    newline = "\r\n" if lines[start].endswith("\r\n") else "\n"
                    metadata_lines.append(f"> <small>{usage_text}</small>{newline}")
                replacements.append((start, end, metadata_lines))
    for start, end, metadata_lines in sorted(replacements, reverse=True):
        lines[start:end] = metadata_lines
    return "".join(lines)


def build_usage_frontmatter_fields(usage: dict | None) -> list[str]:
    """usage オブジェクトを Obsidian/Dataview 用の YAML 行へ変換する。"""
    if not usage:
        return []
    fields = ["usage:"]
    for field in USAGE_TOKEN_FIELDS:
        fields.append(f"  {field}: {int(usage.get(field, 0))}")
    if "total_cost_usd" in usage:
        fields.append(f"  total_cost_usd: {format(Decimal(str(usage['total_cost_usd'])), 'f')}")
        if usage.get("cost_incomplete"):
            fields.append("  cost_incomplete: true")
    elif usage.get("cost_unavailable"):
        fields.append("  cost_unavailable: true")
    fields.append("  by_model:")
    for model, model_usage in usage.get("by_model", {}).items():
        fields.append(f"    {yaml_quote(model)}:")
        for field in USAGE_TOKEN_FIELDS:
            fields.append(f"      {field}: {int(model_usage.get(field, 0))}")
        if "cost_usd" in model_usage:
            fields.append(f"      cost_usd: {format(Decimal(str(model_usage['cost_usd'])), 'f')}")
        elif model_usage.get("cost_unavailable"):
            fields.append("      cost_unavailable: true")
    return fields


def remove_frontmatter_key_block(content: str, key: str) -> str:
    """frontmatter 内の key とそのインデントされた子要素を取り除く。"""
    fm_match = re.match(r"^(---\s*\n)(.*?)(\n---\s*\n)", content, re.DOTALL)
    if not fm_match:
        return content
    lines = fm_match.group(2).splitlines()
    kept = []
    skip = False
    prefix = f"{key}:"
    for line in lines:
        if line.startswith(prefix) and not line.startswith(" "):
            skip = True
            continue
        if skip and (line.startswith((" ", "\t")) or not line.strip()):
            continue
        skip = False
        kept.append(line)
    return fm_match.group(1) + "\n".join(kept) + fm_match.group(3) + content[fm_match.end():]


def build_frontmatter(
    session_id: str,
    cwd: str,
    start_dt: datetime,
    message_count: int = 0,
    source: str = SOURCE_NAME,
    initial_quota: dict | None = None,
    final_quota: dict | None = None,
    usage: dict | None = None,
    title: str | None = None,
) -> str:
    ts = format_iso(start_dt)
    fm_lines = [
        "---",
        f"source: {source}",
        *( [f"title: {yaml_quote(title)}"] if title is not None else [] ),
        f"session_id: {yaml_quote(session_id)}",
        f"project: {yaml_quote(cwd)}",
        f"created: {yaml_quote(ts)}",
        f"modified: {yaml_quote(ts)}",
        "tags:",
        "  - ai-conversation",
        f"  - {source}",
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
    initial_quota: dict | None = None,
    usage: dict | None = None,
    source: str = SOURCE_NAME,
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
    fm = build_frontmatter(
        session_id,
        cwd,
        start_dt,
        message_count=0,
        source=source,
        initial_quota=initial_quota,
        final_quota=initial_quota,
        usage=usage,
        title=title,
    )
    quota_sec = build_quota_section(initial_quota, initial_quota)
    usage_sec = build_usage_section(usage)
    content = fm + quota_sec + "\n" + usage_sec + "\n<!-- last_line: 0 -->\n\n"
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


# ============================================================
# メッセージ追記
# ============================================================
def update_markdown_metadata(
    content: str,
    modified_dt: datetime,
    appended: int,
    last_line: int,
    initial_quota: dict | None,
    current_quota: dict | None,
    usage: dict | None = None,
) -> str:
    """本文のfrontmatter・cursor・quota・usageを更新し、書き込み前の全文を返す。"""
    content_without_usage = remove_frontmatter_key_block(content, "quota")
    if usage is not None:
        content_without_usage = remove_frontmatter_key_block(content_without_usage, "usage")
    usage_fields = tuple(build_usage_frontmatter_fields(usage)) if usage is not None else ()
    quota_fields = tuple(build_quota_frontmatter_fields(initial_quota, current_quota))
    updated = update_frontmatter_fields(
        content_without_usage,
        modified_dt,
        appended,
        extra_fields=quota_fields + usage_fields,
    )
    if updated is None:
        return content
    new_fm_lines, body_text = updated

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
    if usage is not None:
        usage_sec = build_usage_section(usage)
        usage_pattern = r"(?m)^💰 \*\*Usage \((?:API|USD)換算\)\*\*:.*(?:\n(?:^- .*(?:\n|$))*)?"
        if re.search(usage_pattern, body_text):
            body_text = re.sub(usage_pattern, usage_sec.rstrip("\n") + "\n", body_text, count=1)
        else:
            body_text = usage_sec + "\n" + body_text.lstrip()
    body_text = re.sub(
        r"<!--\s*last_line:.*?-->",
        f"<!-- last_line: {last_line} -->",
        body_text,
        count=1,
    )
    return compose_markdown(new_fm_lines, body_text)


def append_messages(
    path: Path,
    messages: list[dict],
    modified_dt: datetime,
    model_name: str,
    last_line: int,
    current_quota: dict | None = None,
    initial_quota: dict | None = None,
    usage: dict | None = None,
    effort: str = "",
) -> tuple[int, bool]:
    content = path.read_text(encoding="utf-8")

    # 同一回答のstreaming fragmentは、usage metadataを含めても従来どおり
    # 一つのcalloutへ連結する。各fragmentのrecordを先に合算することで、表示値も
    # セッション集計との不変条件を保つ。
    grouped_messages = []
    for message in messages:
        if (
            grouped_messages
            and message.get("type") == "agent_message"
            and grouped_messages[-1].get("type") == "agent_message"
            and format_model_name(
                message.get("model", "") or model_name,
                message.get("effort", "") or effort,
            )
            == format_model_name(
                grouped_messages[-1].get("model", "") or model_name,
                grouped_messages[-1].get("effort", "") or effort,
            )
        ):
            previous = grouped_messages[-1]
            separator = "\n\n" if message.get("after_tool_use") else "\n"
            previous["message"] = previous.get("message", "").rstrip("\n") + separator + message.get("message", "")
            previous["usage"] = _merge_usage_summaries(previous.get("usage"), message.get("usage"))
            previous["subagent_usage"] = _merge_usage_summaries(
                previous.get("subagent_usage"), message.get("subagent_usage"),
            )
            if previous.get("usage_records") or message.get("usage_records"):
                previous["usage_records"] = (previous.get("usage_records") or []) + (message.get("usage_records") or [])
        else:
            grouped_messages.append(dict(message))

    blocks = []
    appended = 0
    last_callout_header = get_last_callout_header(content)

    for msg in grouped_messages:
        m_type = msg["type"]
        text = msg["message"]
        m_model = msg.get("model", "") or model_name
        m_effort = msg.get("effort", "") or effort
        display_model = format_model_name(m_model, m_effort)
        if not text.strip():
            continue

        if m_type == "user_message":
            display_text, reference_text = split_structured_user_message(text)
            callout_header = user_callout_header(msg.get("timestamp"))
            user_block = render_user_message_block(
                display_text,
                msg.get("timestamp"),
                continued=callout_header == last_callout_header,
            )

            if reference_text is not None:
                user_block += reference_callout(reference_text) + "\n\n"
            blocks.append(user_block)

            last_callout_header = REFERENCE_CALLOUT_HEADER if reference_text is not None else callout_header
            appended += 1

        elif m_type == "agent_message":
            # 使用されたモデル名の取得
            metadata = [f"🤖 {display_model}"] if display_model else []
            usage_metadata = format_message_usage(msg.get("usage"), msg.get("subagent_usage"))
            if usage_metadata:
                metadata.append(usage_metadata)
            callout_header = assistant_callout_header(AGENT_NAME, metadata)

            if _assistant_header_model(callout_header) == _assistant_header_model(last_callout_header):
                # Codex のストリーミング結果が複数の agent_message に分割される
                # ことがある。同じ callout の続きは区切り線を入れず、改行を残して連結し、
                # 1つの回答として message_count に加算する。
                fragment = render_assistant_message_block(
                    text,
                    AGENT_NAME,
                    metadata,
                    continued=True,
                )
                if blocks:
                    separator = "\n\n" if msg.get("after_tool_use") else "\n"
                    blocks[-1] = blocks[-1].rstrip("\n") + separator + fragment + "\n\n"
                else:
                    separator = "\n\n" if msg.get("after_tool_use") else "\n"
                    content = content.rstrip("\n") + separator
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

    new_content = content + "".join(blocks)
    final_content = update_markdown_metadata(
        new_content,
        modified_dt,
        appended,
        last_line,
        initial_quota,
        current_quota,
        usage,
    )
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
    source = SOURCE_NAME
    model_name = model_name or transcript_metadata.get("model", "")
    effort = effort or transcript_metadata.get("effort", "")
    cwd = project_value(cwd, transcript_metadata)

    state = load_state(session_id)
    is_new_state = not state
    if is_new_state:
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
            try:
                first_quota, last_quota = recover_quota_frontmatter(
                    recovered_path.read_text(encoding="utf-8")
                )
            except (OSError, UnicodeError):
                first_quota, last_quota = {}, {}
            if first_quota:
                state["initial_quota"] = first_quota
            if last_quota:
                state["final_quota"] = last_quota
            save_state(session_id, state)
            is_new_state = False
            logger.warning(f"stateを既存Markdownから復旧しました: {recovered_path}")
    saved_last_line = state.get("last_line", 0)
    current_quota = retrieve_quota(jsonl_path, saved_last_line)
    subagent_records = _read_subagent_usage_records(jsonl_path, session_id)
    current_usage = retrieve_usage(jsonl_path, session_id, subagent_records)
    if current_quota:
        first_quota = retrieve_quota(jsonl_path, saved_last_line, first=True)
        state["initial_quota"] = merge_first_quota(state.get("initial_quota"), first_quota or {})
        current_quota = merge_first_quota(current_quota, state.get("final_quota") or {})
        state["final_quota"] = current_quota

    # 初回初期化
    if is_new_state or "output_path" not in state:
        start_dt = now_jst()
        path = create_md_file(
            session_id,
            cwd,
            start_dt,
            initial_quota=state.get("initial_quota"),
            usage=current_usage,
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
            usage=current_usage,
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
    new_messages, new_last_line = load_jsonl_messages(jsonl_path, last_line, subagent_records)

    if new_messages:
        appended, updated = append_messages(
            path,
            new_messages,
            now_jst(),
            model_name,
            new_last_line,
            current_quota=current_quota or state.get("final_quota"),
            initial_quota=state.get("initial_quota"),
            usage=current_usage,
            effort=effort,
        )
        if updated:
            state["last_line"] = new_last_line
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

    # Usage は transcript 全体の最新値を使うため、メッセージが増えていない
    # hook 再実行でも frontmatter と本文を更新する（最後の usage record を拾う）。
    if current_usage is not None:
        content = path.read_text(encoding="utf-8")
        all_messages, _ = load_jsonl_messages(jsonl_path, 0, subagent_records)
        refreshed = update_markdown_metadata(
            content,
            now_jst(),
            0,
            new_last_line,
            state.get("initial_quota"),
            current_quota or state.get("final_quota"),
            current_usage,
        )
        refreshed = refresh_answer_usage_metadata(refreshed, all_messages)
        if refreshed != content:
            atomic_write_md(path, refreshed)
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
