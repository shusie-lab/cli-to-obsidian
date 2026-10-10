#!/usr/bin/python3
from __future__ import annotations

"""Pi extensionから渡された会話を Obsidian に保存する。"""

__version__ = "3.8.0"

import argparse
import sys
from decimal import Decimal, InvalidOperation
import fcntl
import hashlib
import json
import logging
import math
from logging.handlers import RotatingFileHandler
import os
import re
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


LOGGER_NAME = "pi_obsidian_save"
JST = timezone(timedelta(hours=9))
OBSIDIAN_VAULT = Path(os.environ.get("OBSIDIAN_VAULT", str(Path.home() / "obsidian")))
DEFAULT_OUTPUT_DIR = "生成AI/ChatLog"
DEFAULT_SOURCE = "pi"

# 標準API参考単価。サブスク請求額・quota消費額ではない。
# 共通の参考単価表を埋め込み、登録済みモデルを換算する。
# https://developers.openai.com/api/docs/pricing
# https://dev.meta.ai/docs/pricing-rate-limits/
# https://platform.claude.com/docs/en/about-claude/pricing
# https://openrouter.ai/nvidia/nemotron-3-ultra-550b-a55b

# BEGIN GENERATED: usage-prices
# 参考単価の正本。Opus 5.5のcache readは2026-10-06に公式資料で確認。
# https://developers.openai.com/api/docs/pricing
# https://platform.claude.com/docs/en/about-claude/pricing
# https://dev.meta.ai/docs/pricing-rate-limits/
# https://openrouter.ai/nvidia/nemotron-3-ultra-550b-a55b
# Gemini 3.8 Flash: 2026-10-06時点のStandard（年末までの割引価格）。
# https://ai.google.dev/gemini-api/docs/pricing#gemini-3.8-flash
# https://openrouter.ai/api/v1/models/google/gemini-3.8-flash/endpoints
# Haiku 5.5: 2026-10-09確認。10万token超は全token種別で5倍。
# https://platform.claude.com/docs/en/models/haiku-5-5/overview
STANDARD_USAGE_RATES = {
    "gpt-6-astra": {
        "input": Decimal("10.00"),
        "cached_input": Decimal("1.00"),
        "cache_write_input": Decimal("12.50"),
        "output": Decimal("50.00"),
    },
    "gpt-6.1-sol": {
        "input": Decimal("2.00"),
        "cached_input": Decimal("0.10"),
        "cache_write_input": Decimal("2.50"),
        "output": Decimal("10.00"),
    },
    "gpt-6-luna": {
        "input": Decimal("0.10"),
        "cached_input": Decimal("0.01"),
        "cache_write_input": Decimal("0.125"),
        "output": Decimal("0.50"),
    },
    "gemini-3.8-flash": {
        "input": Decimal("0.75"),
        "cached_input": Decimal("0.075"),
        # OpenRouter Standardのinput_cache_writeを100万token単位へ変換。
        # Googleの時間単位のstorage priceとは別の掲載単位。
        "cache_write_input": Decimal("0.0416666666666667"),
        "output": Decimal("3.75"),
    },
    "muse-spark-1.3-contributor-free": {
        "input": Decimal("0.10"),
        "cached_input": Decimal("0.002"),
        "cache_write_input": Decimal("0.10"),
        "output": Decimal("0.20"),
    },
    "nemotron-3-ultra-free": {
        "input": Decimal("0.50"),
        "cached_input": Decimal("0.10"),
        "cache_write_input": Decimal("0.50"),
        "output": Decimal("2.20"),
    },
    "claude-opus-5-5": {
        "input": Decimal("4.00"),
        "cached_input": Decimal("0.20"),
        "cache_write_input": Decimal("5.00"),
        "output": Decimal("20.00"),
        "cache_write_input_1h": Decimal("8.00"),
    },
    "claude-haiku-5-5": {
        "input": Decimal("0.10"),
        "cached_input": Decimal("0.01"),
        "cache_write_input": Decimal("0.125"),
        "output": Decimal("0.50"),
        "cache_write_input_1h": Decimal("0.20"),
    },
    "claude-sonnet-5-5": {
        "input": Decimal("2.00"),
        "cached_input": Decimal("0.20"),
        "cache_write_input": Decimal("2.50"),
        "output": Decimal("10.00"),
        "cache_write_input_1h": Decimal("4.00"),
    },
}

# 従来の呼び出し名との互換alias。単価の正本は上の辞書1つだけ。
USAGE_COST_USD_PER_1M_TOKEN_TYPE = STANDARD_USAGE_RATES
# END GENERATED: usage-prices

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

SOURCE_NAME = resolve_source_name(DEFAULT_SOURCE)
OUTPUT_BASE = OBSIDIAN_VAULT / resolve_output_dir() / SOURCE_NAME
STATE_DIR = Path(os.environ.get("PI_OBSIDIAN_STATE_DIR", str(Path.home() / ".local/state/pi-obsidian")))
LOG_FILE = STATE_DIR / "pi_obsidian_save.log"
STATE_RETENTION_DAYS = 30
SAFE_SESSION_CHARS = re.compile(r"[^A-Za-z0-9_.-]+")

# BEGIN GENERATED: utilities
def now_jst() -> datetime:
    return datetime.now(JST)


def yaml_quote(value: object) -> str:
    """JSON互換の引用形式でYAML文字列を安全に生成する。"""
    return json.dumps(str(value), ensure_ascii=False)


def safe_filename_component(value: str) -> str:
    """値を単一の安全なファイル名要素へ変換する。"""
    return re.sub(r"[\x00-\x1f/:*?\[\]\\]", "_", value).strip()[:80]


def _try_parse_datetime(value: object) -> datetime | None:
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value) / 1000, tz=timezone.utc).astimezone(JST)
        except (OSError, OverflowError, ValueError):
            return None
    if isinstance(value, str) and value:
        try:
            normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
            parsed = datetime.fromisoformat(normalized)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(JST)
        except (OSError, OverflowError, TypeError, ValueError):
            pass
    return None


def parse_datetime(value: object, default: datetime | None = None) -> datetime:
    parsed = _try_parse_datetime(value)
    if parsed is not None:
        return parsed
    return default or datetime.now(JST)


def format_iso(value: datetime) -> str:
    return value.astimezone(JST).isoformat(timespec="seconds")


def _finite_number(value: object) -> float | None:
    """APIレスポンスの数値を厳密に検証する。boolやNaN/Infinityは受け付けない。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (OverflowError, ValueError, TypeError):
        return None
    return number if math.isfinite(number) else None


def format_model_name(model_name: str, effort: str = "") -> str:
    """モデル名に reasoning effort を付加して表示用の名前へ変換する。"""
    model = str(model_name or "").strip()
    effort_value = str(effort or "").strip()
    if not model or not effort_value:
        return model
    if model.endswith(f"-{effort_value}"):
        return model
    return f"{model}-{effort_value}"
# END GENERATED: utilities

# BEGIN GENERATED: logging-setup
def setup_logger() -> logging.Logger:
    logger = logging.getLogger(LOGGER_NAME)
    if logger.handlers:
        return logger
    logger.setLevel(logging.DEBUG if os.environ.get("DEBUG") else logging.INFO)
    formatter = logging.Formatter("[%(asctime)s] %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    file_log_error = None
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            LOG_FILE,
            maxBytes=5 * 1024 * 1024,  # 5MB
            backupCount=3,
            encoding="utf-8",
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    except OSError as e:
        file_log_error = e
    stderr = logging.StreamHandler()
    stderr.setFormatter(formatter)
    logger.addHandler(stderr)
    if file_log_error is not None:
        logger.warning("ファイルログを初期化できないためstderrのみ使用します: %s", file_log_error)
    return logger


logger = setup_logger()
# END GENERATED: logging-setup

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


def load_state(session_id: str) -> dict:
    path = state_path(session_id)
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("stateはJSONオブジェクトが必要です")
            return data
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

# BEGIN GENERATED: full-session-storage
def atomic_write(path: Path, content: str) -> None:
    """既存snapshot saver APIを共通atomic writeへ接続する。"""
    atomic_write_text(path, content)


def read_payload(path: str | None) -> dict:
    if path:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    return json.load(sys.stdin)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", help="会話snapshotのJSONファイル。省略時はstdin")
    args = parser.parse_args()
    handle_payload(read_payload(args.input))
# END GENERATED: full-session-storage

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

# BEGIN GENERATED: note-metadata
def build_common_frontmatter(
    session_id: str, cwd: str, created: datetime, modified: datetime,
    count: int, title: str | None = None, *, source: str | None = None,
    tags: list[str] | None = None, extra_fields=(),
) -> str:
    source = SOURCE_NAME if source is None else source
    lines = [
        "---", f"source: {source}",
        *([f"title: {yaml_quote(title)}"] if title is not None else []),
        f"session_id: {yaml_quote(session_id)}", f"project: {yaml_quote(cwd)}",
        f"created: {yaml_quote(format_iso(created))}",
        f"modified: {yaml_quote(format_iso(modified))}", "tags:",
    ]
    for tag in tags if tags is not None else ["ai-conversation", source]:
        # Preserve the conventional plain form where it is an unambiguous YAML scalar.
        safe = re.fullmatch(r"[A-Za-z][A-Za-z0-9._-]*", str(tag))
        value = str(tag) if safe and str(tag).lower() not in {"true", "false", "null", "yes", "no", "on", "off"} else yaml_quote(tag)
        lines.append(f"  - {value}")
    lines.append(f"message_count: {count}")
    lines.extend(extra_fields)
    lines.append("---")
    return "\n".join(lines) + "\n\n"


def note_output_path(session_id: str, cwd: str, created: datetime) -> Path:
    project = safe_filename_component(Path(cwd).name) if cwd else ""
    prefix = f"{created:%Y%m%d}_{created:%H%M%S}"
    if project:
        prefix += f"_{project}"
    return OUTPUT_BASE / f"{prefix}_{safe_session_key(session_id) if session_id else 'unknown'}.md"


def read_markdown_metadata(path: Path) -> dict:
    """標準ライブラリで、このプロジェクトのfrontmatterの識別項目だけを読む。"""
    content = path.read_text(encoding="utf-8")
    match = re.match(r"\A---\s*\n(.*?)\n---\s*\n", content, re.DOTALL)
    if not match:
        return {}
    result = {}
    in_tags = False
    for line in match.group(1).splitlines():
        if line.startswith("tags:"):
            in_tags = True
            result["tags"] = []
            continue
        if in_tags and line.startswith("  - "):
            raw = line[4:].strip()
            try:
                tag = json.loads(raw)
            except ValueError:
                tag = raw
            if isinstance(tag, str):
                result["tags"].append(tag)
            continue
        if line and not line.startswith((" ", "\t")):
            in_tags = False
        if line.startswith(("source:", "created:", "title:", "session_id:", "project:")):
            key, raw = line.split(":", 1)
            try:
                value = json.loads(raw.strip())
            except ValueError:
                value = raw.strip()
            if isinstance(value, str):
                result[key] = value
    return result


def preserve_modified_if_unchanged(content: str, refreshed: str) -> str:
    """更新時刻以外が一致するなら既存ノートをそのまま返す。"""
    # Scope to frontmatter, so an assistant's code or prose is never replaced.
    pattern = r"\A(---\s*\n)(.*?)(\n---\s*\n)"
    old = re.match(pattern, content, re.DOTALL)
    new = re.match(pattern, refreshed, re.DOTALL)
    if not old or not new:
        return refreshed
    modified = next((line for line in old.group(2).splitlines() if line.startswith("modified:")), None)
    if modified is None:
        return refreshed
    fields = re.sub(r"(?m)^modified:.*$", lambda _: modified, new.group(2), count=1)
    unchanged_time = new.group(1) + fields + new.group(3) + refreshed[new.end():]
    return content if unchanged_time == content else refreshed
# END GENERATED: note-metadata

# BEGIN GENERATED: usage-model
def _normalize_usage_model(model: object) -> str:
    """プロバイダ接頭辞などを除き、単価表と照合できるモデル名へ変換する。"""
    name = str(model or "").strip().lower()
    if "/" in name:
        name = name.rsplit("/", 1)[-1]
    aliases = {
        "gemini3.8-flash": "gemini-3.8-flash",
        "nemotron-3-ultra-550b-a55b:free": "nemotron-3-ultra-free",
        "claude-opus-5.5": "claude-opus-5-5",
        "claude-haiku-5.5": "claude-haiku-5-5",
        "claude-sonnet-5.5": "claude-sonnet-5-5",
        "muse-spark-1.3-contributor:free": "muse-spark-1.3-contributor-free",
        "muse-spark-1.3-contributor": "muse-spark-1.3-contributor-free",
    }
    return aliases.get(name, name)
# END GENERATED: usage-model


# BEGIN GENERATED: usage-summary
USAGE_FIELDS = (
    "input_tokens", "cached_input_tokens", "cache_write_input_tokens",
    "output_tokens", "reasoning_output_tokens", "total_tokens",
)
USAGE_TOKEN_FIELDS = USAGE_FIELDS


def _empty_usage_totals() -> dict:
    return {field: 0 for field in USAGE_FIELDS}


def _decimal_cost(value: object) -> Decimal | None:
    """数値・文字列金額を有限で非負のDecimalへ変換する。"""
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        return None
    try:
        result = Decimal(str(value))
    except (ArithmeticError, ValueError, TypeError):
        return None
    return result if result.is_finite() and result >= 0 else None


def calculate_usage_cost(rates: dict, usage: dict, *, input_multiplier=1, output_multiplier=1) -> Decimal | None:
    """各recordを換算し、キャッシュ・推論の内数を二重計上しない。"""
    try:
        values = [usage.get(key, 0) for key in ("input_tokens", "cached_input_tokens", "cache_write_input_tokens", "output_tokens")]
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in values):
            return None
        total_input, cached, written, output = values
        fresh = total_input - cached - written
        if fresh < 0:
            return None
        return ((
            (Decimal(fresh) * rates["input"] + Decimal(cached) * rates["cached_input"]
             + Decimal(written) * rates["cache_write_input"]) * Decimal(str(input_multiplier))
            + Decimal(output) * rates["output"] * Decimal(str(output_multiplier))
        ) / Decimal(1_000_000)).normalize()
    except (ArithmeticError, KeyError, TypeError, ValueError):
        return None


def summarize_usage(messages, *, estimate_missing: bool = True) -> dict | None:
    """provider側で重複排除・正規化したrecordを、各recordの金額を維持して合計する。"""
    total = _empty_usage_totals()
    by_model = {}
    count = priced = 0
    total_cost = Decimal(0)
    long_context = False
    for message in messages:
        usage = message.get("usage") if message.get("type") == "assistant" else None
        if not isinstance(usage, dict):
            continue
        count += 1
        model = str(message.get("model") or "unknown")
        entry = by_model.setdefault(model, _empty_usage_totals())
        for field in USAGE_FIELDS:
            value = int(usage.get(field, 0))
            total[field] += value
            entry[field] += value
        long_context = long_context or bool(usage.get("long_context"))
        value = _decimal_cost(usage.get("cost_usd"))
        if value is None and estimate_missing:
            value = _usage_cost(model, usage)
        if value is not None:
            priced += 1
            total_cost += value
            entry["cost_usd"] = entry.get("cost_usd", Decimal(0)) + value
        else:
            entry["cost_unavailable"] = True
    if not count:
        return None
    for entry in by_model.values():
        if "cost_usd" in entry and entry.pop("cost_unavailable", False):
            entry["cost_incomplete"] = True
    total["by_model"] = by_model
    if priced:
        total["total_cost_usd"] = total_cost
        if priced < count:
            total["cost_incomplete"] = True
    else:
        total["cost_unavailable"] = True
    if long_context:
        total["long_context"] = True
    return total


def usage_frontmatter_fields(usage: dict | None) -> list[str]:
    if not usage:
        return []
    lines = ["usage:"]
    lines.extend(f"  {field}: {int(usage.get(field, 0))}" for field in USAGE_FIELDS)
    if "total_cost_usd" in usage:
        lines.append(f"  total_cost_usd: {format(Decimal(str(usage['total_cost_usd'])), 'f')}")
        if usage.get("cost_incomplete"):
            lines.append("  cost_incomplete: true")
    else:
        lines.append("  cost_unavailable: true")
    lines.append("  by_model:")
    for model, entry in usage.get("by_model", {}).items():
        lines.append(f"    {yaml_quote(model)}:")
        lines.extend(f"      {field}: {int(entry.get(field, 0))}" for field in USAGE_FIELDS)
        if "cost_usd" in entry:
            lines.append(f"      cost_usd: {format(Decimal(str(entry['cost_usd'])), 'f')}")
            if entry.get("cost_incomplete") or entry.get("cost_unavailable"):
                lines.append("      cost_incomplete: true")
        else:
            lines.append("      cost_unavailable: true")
    return lines


def build_usage_frontmatter_fields(usage: dict | None) -> list[str]:
    """増分saverの従来API。"""
    return usage_frontmatter_fields(usage)


def _format_usd(value: object) -> str | None:
    amount = _decimal_cost(value)
    return f"${format(amount, 'f')}" if amount is not None else None


def _usage_cost_label(usage: dict, key: str) -> str:
    amount = _format_usd(usage.get(key))
    if amount is None:
        return "換算不可"
    partial = usage.get("cost_incomplete") or usage.get("cost_unavailable")
    return amount + ("（部分換算）" if partial else "")


def build_usage_section(usage: dict | None) -> str:
    if not usage:
        return "💰 **Usage (USD換算)**: N/A\n"
    parts = [f"{model} {_usage_cost_label(entry, 'cost_usd')}" for model, entry in usage.get("by_model", {}).items()]
    details = f" ({'、'.join(parts)})" if parts else ""
    return f"💰 **Usage (USD換算)**: Total: {_usage_cost_label(usage, 'total_cost_usd')}{details}\n"


def format_message_usage(usage: dict | None, subagent_usage: dict | None = None) -> str:
    if not usage:
        return ""
    warning = " ⚠︎ 272K+" if usage.get("long_context") else ""
    label = f"Usage: {int(usage.get('total_tokens', 0)):,} tok · {_usage_cost_label(usage, 'total_cost_usd')}{warning}"
    if subagent_usage:
        parts = [f"{model} {int(entry.get('total_tokens', 0)):,} tok・{_usage_cost_label(entry, 'cost_usd')}"
                 for model, entry in sorted(subagent_usage.get("by_model", {}).items())]
        if parts:
            label += f" ({'、'.join(parts)})"
    return label


def _merge_usage_summaries(first: dict | None, second: dict | None) -> dict | None:
    """換算済み小計を足す。再換算せず長文条件をrecord単位に維持する。"""
    if not first:
        return second
    if not second:
        return first
    merged = _empty_usage_totals()
    for field in USAGE_FIELDS:
        merged[field] = int(first.get(field, 0)) + int(second.get(field, 0))
    by_model = {}
    for source in (first, second):
        for model, entry in source.get("by_model", {}).items():
            target = by_model.setdefault(model, _empty_usage_totals())
            for field in USAGE_FIELDS:
                target[field] += int(entry.get(field, 0))
            if "cost_usd" in entry:
                target["cost_usd"] = target.get("cost_usd", Decimal(0)) + Decimal(str(entry["cost_usd"]))
            if entry.get("cost_unavailable") or entry.get("cost_incomplete"):
                target["cost_unavailable"] = True
    for entry in by_model.values():
        if "cost_usd" in entry and entry.pop("cost_unavailable", False):
            entry["cost_incomplete"] = True
    merged["by_model"] = by_model
    if "total_cost_usd" in first or "total_cost_usd" in second:
        merged["total_cost_usd"] = sum((Decimal(str(source.get("total_cost_usd", 0))) for source in (first, second)), Decimal(0))
        if any(source.get("cost_unavailable") or source.get("cost_incomplete") for source in (first, second)):
            merged["cost_incomplete"] = True
    else:
        merged["cost_unavailable"] = True
    if first.get("long_context") or second.get("long_context"):
        merged["long_context"] = True
    return merged
# END GENERATED: usage-summary

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

# BEGIN GENERATED: full-session-search
def recover_snapshot_metadata(path: Path | None, state: dict, created: object) -> dict:
    """既存frontmatterを優先し、state消失や環境設定変更でもノートの識別を保持する。"""
    metadata = read_markdown_metadata(path) if path is not None and path.exists() else {}
    source = metadata.get("source") or state.get("source") or SOURCE_NAME
    return {
        "created": parse_datetime(metadata.get("created") or state.get("created") or created),
        "source": source,
        "tags": metadata.get("tags", state.get("tags", ["ai-conversation", source])),
    }
# END GENERATED: full-session-search

def pi_usage_model(model: object) -> str:
    """既存Pi API。別名の正本は共有する。"""
    return _normalize_usage_model(model)


# BEGIN GENERATED: usage-reference-cost
LONG_CONTEXT_INPUT_TOKENS = 272_000


def reference_price_model(model: object) -> str:
    name = _normalize_usage_model(model)
    if (name not in STANDARD_USAGE_RATES and name.endswith("-free")
            and name[:-len("-free")] in STANDARD_USAGE_RATES):
        name = name[:-len("-free")]
    return name


def is_long_context_usage(model: object, usage: dict) -> bool:
    """登録済みGPTの1リクエストの入力総数で、参考単価の長文条件を判定する。"""
    name = reference_price_model(model)
    tokens = usage.get("input_tokens", 0)
    return (isinstance(tokens, int) and not isinstance(tokens, bool)
            and name.startswith("gpt-") and name in STANDARD_USAGE_RATES
            and tokens > LONG_CONTEXT_INPUT_TOKENS)


def haiku_prompt_price_multiplier(model: object, usage: dict) -> int:
    """通常入力・cache read/writeの合計が10万tokenを超えた場合は5倍。"""
    tokens = usage.get("input_tokens", 0)
    return 5 if (reference_price_model(model) == "claude-haiku-5-5"
                 and isinstance(tokens, int) and not isinstance(tokens, bool)
                 and tokens > 100_000) else 1


def _usage_cost(model: object, usage: dict) -> Decimal | None:
    """raw recordを正規化したtoken内訳から標準API参考換算を行う。"""
    rates = STANDARD_USAGE_RATES.get(reference_price_model(model))
    if rates is None or usage.get("pricing_unknown"):
        return None
    long_context = is_long_context_usage(model, usage)
    haiku_multiplier = haiku_prompt_price_multiplier(model, usage)
    return calculate_usage_cost(
        rates, usage, input_multiplier=2 if long_context else haiku_multiplier,
        output_multiplier=Decimal("1.5") if long_context else haiku_multiplier,
    )
# END GENERATED: usage-reference-cost


def normalize_pi_usage(raw: object, model: str) -> Optional[Dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    values = [raw.get(key, 0) for key in ("input", "cacheRead", "cacheWrite", "output", "reasoning")]
    if any(isinstance(v, bool) or not isinstance(v, int) or v < 0 for v in values):
        return None
    fresh, read, write, output, reasoning = values
    if reasoning > output:
        return None
    usage = dict(zip(USAGE_FIELDS, (fresh + read + write, read, write, output, reasoning,
                                   fresh + read + write + output)))
    # 単価表は5分cache write。1時間保持を含む場合は推定しない。
    if raw.get("cacheWrite1h"):
        usage["pricing_unknown"] = True
    cost = _usage_cost(model, usage)
    if cost is not None:
        usage["cost_usd"] = str(cost)
        if is_long_context_usage(model, usage):
            usage["long_context"] = True
    return usage


def entry_records(entries: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    messages, records = [], {}
    model = "unknown"
    turn = -1
    seen = set()
    for sequence, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        entry_id = entry.get("id")
        if not isinstance(entry_id, str) or not entry_id or entry_id in seen:
            continue
        seen.add(entry_id)
        kind = entry.get("type")
        if kind == "model_change":
            model = str(entry.get("modelId") or "unknown")
            continue
        msg = entry.get("message") if kind == "message" else entry
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        current_model = str(msg.get("model") or ("unknown" if role == "toolResult" else model))
        if role == "user":
            turn += 1
        usage = normalize_pi_usage(msg.get("usage"), current_model)
        if usage and (role in ("assistant", "toolResult") or kind in ("usage", "compaction", "branch_summary")):
            records[entry_id] = {"id": entry_id, "type": "assistant", "model": current_model,
                                 "sequence": sequence, "turn_index": turn, "usage": usage}
        if role not in ("user", "assistant"):
            continue
        content = msg.get("content", "")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = "\n".join(p["text"] for p in content if isinstance(p, dict)
                             and p.get("type") == "text" and isinstance(p.get("text"), str))
        else:
            text = ""
        if not text.strip():
            continue
        messages.append({"id": entry_id, "type": role, "text": text,
                         "timestamp": entry.get("timestamp"), "model": current_model,
                         "turn_index": turn, "sequence": sequence})
    return messages, records


# BEGIN GENERATED: full-session-display
def _display_message_groups(messages: Iterable[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
    groups: List[List[Dict[str, Any]]] = []
    for message in messages:
        if (
            message["type"] == "assistant"
            and groups
            and groups[-1][0]["type"] == "assistant"
            and groups[-1][0].get("model", "") == message.get("model", "")
            and groups[-1][0].get("turn_index") == message.get("turn_index")
        ):
            groups[-1].append(message)
        else:
            groups.append([message])
    return groups


def _usage_records_by_display_group(
    groups: List[List[Dict[str, Any]]], usage_records: Iterable[Dict[str, Any]]
) -> List[List[Dict[str, Any]]]:
    """本文のないassistant usageを、同じユーザーターン内の最寄り回答calloutへ割り当てる。"""
    result: List[List[Dict[str, Any]]] = [[] for _ in groups]
    message_groups = {
        str(message.get("id")): group_index
        for group_index, group in enumerate(groups)
        for message in group
        if message.get("type") == "assistant" and message.get("id")
    }
    assistant_groups = [
        index for index, group in enumerate(groups)
        if group and group[0].get("type") == "assistant"
    ]
    for record in usage_records:
        if not isinstance(record, dict) or record.get("type") != "assistant":
            continue
        message_id = str(record.get("id") or "")
        target = message_groups.get(message_id)
        if target is None:
            sequence = record.get("sequence")
            turn_index = record.get("turn_index")
            candidates = [
                index for index in assistant_groups
                if turn_index is None or groups[index][0].get("turn_index") == turn_index
            ]
            model = str(record.get("model") or "")
            same_model = [index for index in candidates if str(groups[index][0].get("model") or "") == model]
            if same_model:
                candidates = same_model
            if not candidates:
                continue

            def distance(group_index: int) -> Tuple[int, int, int]:
                positions = [
                    message.get("sequence") for message in groups[group_index]
                    if isinstance(message.get("sequence"), int)
                ]
                if not isinstance(sequence, int) or not positions:
                    return (0, group_index, group_index)
                start, end = min(positions), max(positions)
                gap = start - sequence if sequence < start else sequence - end if sequence > end else 0
                follows_record = 0 if start >= sequence else 1
                return (gap, follows_record, group_index)

            target = min(candidates, key=distance)
        result[target].append(record)
    return result
# END GENERATED: full-session-display

# BEGIN GENERATED: full-session-rendering
def render_full_session_markdown(
    session_id: str, cwd: str, created: datetime, messages: list[dict],
    agent_name: str, *, modified: datetime | None = None,
    usage_records=None, display_usage_records=None, source: str | None = None, tags=None,
) -> str:
    groups = _display_message_groups(messages)
    all_records = list(usage_records) if usage_records is not None else messages
    display_records = list(display_usage_records) if display_usage_records is not None else all_records
    usage = summarize_usage(all_records)
    group_records = _usage_records_by_display_group(groups, display_records)
    first_user = next((message["text"] for message in messages if message["type"] == "user"), None)
    title = heading_title(first_user) if first_user is not None else None
    blocks = [build_common_frontmatter(
        session_id, cwd, created, modified or now_jst(), len(groups), title,
        source=source, tags=tags, extra_fields=usage_frontmatter_fields(usage),
    ), build_usage_section(usage) + "\n"]
    for index, group in enumerate(groups):
        message = group[0]
        if message["type"] == "user":
            blocks.append(render_user_message_block(message["text"], message.get("timestamp")))
            continue
        model = str(message.get("model") or "")
        metadata = [f"🤖 {model}"] if model else []
        answer_usage = summarize_usage(group_records[index]) or summarize_usage(group)
        usage_text = format_message_usage(answer_usage)
        if usage_text:
            metadata.append(usage_text)
        body = "\n\n".join(item["text"].strip("\n") for item in group)
        blocks.append(render_assistant_message_block(body, agent_name, metadata))
    return "".join(blocks)
# END GENERATED: full-session-rendering


def render_pi_markdown(session_id: str, cwd: str, created: datetime, messages: list[dict],
                       all_records: dict, branch_records: dict, *, source: str | None = None,
                       tags=None) -> str:
    return render_full_session_markdown(session_id, cwd, created, messages, "Pi",
        usage_records=all_records.values(), display_usage_records=branch_records.values(),
        source=source, tags=tags)


def handle_payload(payload: Dict[str, Any]) -> Optional[Path]:
    session_id = payload.get("session_id")
    entries, branch = payload.get("entries"), payload.get("branch")
    if not isinstance(session_id, str) or not session_id or not isinstance(entries, list) or not isinstance(branch, list):
        raise ValueError("session_id / entries / branch が必要です")
    messages, branch_records = entry_records(branch)
    _, all_records = entry_records(entries)
    cwd = str(payload.get("cwd") or "")
    with session_lock(session_id):
        state = load_state(session_id)
        path = Path(state["output_path"]) if state.get("output_path") else find_existing_md(session_id)
        # 新規の空セッションは作らず、既存ノートは本文のない分岐でも再構成する。
        if path is None and not messages:
            return None
        metadata = recover_snapshot_metadata(path, state, payload.get("created"))
        created = metadata["created"]
        fingerprint = hashlib.sha256(json.dumps({"version": __version__, "cwd": cwd,
            "messages": messages, "all_records": all_records, "branch_records": branch_records,
            "source": metadata["source"], "tags": metadata["tags"]},
            sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
        if path is not None and path.exists() and state.get("payload_hash") == fingerprint:
            save_state(session_id, state)
            return path
        if path is None:
            path = note_output_path(session_id, cwd, created)
        atomic_write(path, render_pi_markdown(session_id, cwd, created, messages, all_records,
                                              branch_records, source=metadata["source"],
                                              tags=metadata["tags"]))
        save_state(session_id, {"output_path": str(path), "created": format_iso(created),
                                "payload_hash": fingerprint, "source": metadata["source"],
                                "tags": metadata["tags"]})
        logger.info("セッション保存完了 %s (%d件)", session_id[:8], len(messages))
        cleanup_old_states()
        return path


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # payload本文や認証情報を例外ログに含めない。
        logger.error("Piの会話保存に失敗しました")
        raise SystemExit(1)
