#!/usr/bin/python3
from __future__ import annotations
"""
Claude Code CLI の会話履歴を Obsidian に自動保存するフックスクリプト。
出力形式: Obsidian AI Exporter 互換（YAMLフロントマター + callout block形式）

対応イベント:
  - Stop : ターン終了時に Markdown ファイルの作成・追記を行う
"""

__version__ = "3.11.1"

from bisect import bisect_right
import json
import math
import io
import time
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
LOGGER_NAME = "claude_obsidian_save"
JST = timezone(timedelta(hours=9))

# GPT参考換算の272K超倍率（入力・キャッシュ系2倍、出力1.5倍）。
APPLY_LONG_CONTEXT_PRICING = True

OBSIDIAN_VAULT = Path(
    os.environ.get(
        "OBSIDIAN_VAULT",
        str(Path.home() / "obsidian")
    )
)
DEFAULT_OUTPUT_DIR = "生成AI/ChatLog"
DEFAULT_SOURCE = "claude-code"


# Usage の標準 API 単価（USD / 1,000,000 tokens）。集計した input_tokens には
# cached_input_tokens と cache_write_input_tokens が含まれるため、通常入力の
# 単価はそれらを除いた残りにだけ適用する。
# cache_write_input は5分、cache_write_input_1h は1時間のキャッシュ書き込み。
# Claude Code の契約上の請求額・サブスクリプション消費量は表せない。
# 無料モデルも、請求額ではなく参考値として対応する有料版の単価で換算する。
# Claude: https://platform.claude.com/docs/en/about-claude/pricing
# Nemotron: https://openrouter.ai/nvidia/nemotron-3-ultra-550b-a55b

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
# DeepSeek V4.1 Flash: 2026-10-16確認。固定の参考換算にはピーク単価を使用。
# https://api-docs.deepseek.com/quick_start/pricing/
STANDARD_USAGE_RATES = {
    "deepseek-v4.1-flash": {
        "input": Decimal("0.30"),
        "cached_input": Decimal("0.006"),
        # 自動キャッシュの書込分もcache missの入力単価で換算。
        "cache_write_input": Decimal("0.30"),
        "output": Decimal("1.20"),
    },
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

# ============================================================
# ユーティリティ
# ============================================================
# BEGIN GENERATED: incremental-utilities
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


def read_stdin() -> dict:
    """hookの標準入力を読み、空入力や不正JSONでは空のdictを返す。"""
    try:
        data = sys.stdin.read()
        if data.strip():
            return json.loads(data)
    except Exception as error:
        logger.info("stdinの読み込みまたはJSONパースに失敗しました: %s", error)
    return {}


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


def _assistant_header_spans(content: str) -> list[tuple[int, int]]:
    """コードフェンス外のassistant metadata行の範囲を返す。"""
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


def refresh_answer_timing_metadata(content: str, messages: list[dict]) -> str:
    """終了記録が後から届いた場合も、依頼の最後のcalloutだけを更新する。"""
    timings = {}
    turn_index = 0
    last_turn_id = None
    for message in messages:
        turn_id = message.get("turn_id") or None
        if message.get("type") == "user_message":
            turn_index += 1
            last_turn_id = turn_id
            continue
        if message.get("type") != "agent_message":
            continue
        if message.get("after_turn_boundary") or (turn_id and last_turn_id and turn_id != last_turn_id):
            turn_index += 1
        last_turn_id = turn_id or last_turn_id
        if "answer_duration_seconds" in message:
            timings[turn_index] = message["answer_duration_seconds"]
    lines = content.splitlines(keepends=True)
    user_positions = []
    active_fence = None
    for index, line in enumerate(lines):
        marker = _fence_marker(line)
        if active_fence is not None:
            if marker and marker[0] == active_fence[0] and marker[1] >= active_fence[1] and not marker[2].strip():
                active_fence = None
            continue
        if marker:
            active_fence = marker[:2]
            continue
        if line.rstrip("\r\n") in ("> [!QUESTION] User", HIDDEN_USER_BOUNDARY):
            user_positions.append(index)
    spans_by_turn = {}
    for start, end in _assistant_header_spans(content):
        turn = sum(position < start for position in user_positions)
        spans_by_turn.setdefault(turn, []).append((start, end))
    replacements = []
    for turn, spans in spans_by_turn.items():
        if turn not in timings:
            continue
        for index, (start, end) in enumerate(spans):
            metadata = [line for line in lines[start:end]
                        if not line.startswith("> <small>⏱ 回答所要時間: ")]
            if index == len(spans) - 1:
                newline = "\r\n" if lines[start].endswith("\r\n") else "\n"
                metadata.append("> <small>" + format_answer_duration(timings[turn]) + "</small>" + newline)
            replacements.append((start, end, metadata))
    for start, end, metadata in reversed(replacements):
        lines[start:end] = metadata
    return "".join(lines)
# END GENERATED: incremental-callout


# BEGIN GENERATED: message-metadata
def annotate_answer_timings(messages: list[dict]) -> list[dict]:
    """依頼から最後の回答完了までを測る。元の入力は変更しない。"""
    result = [dict(message) for message in messages]
    started = None
    last_answer = None
    last_turn = None

    def finish():
        if last_answer is None:
            return
        value = result[last_answer].get("completed_at")
        ended = _try_parse_datetime(value) if not isinstance(value, bool) else None
        seconds = (ended - started).total_seconds() if started is not None and ended is not None else None
        result[last_answer]["answer_duration_seconds"] = seconds if seconds is not None and seconds >= 0 else None

    for index, message in enumerate(result):
        message.pop("answer_duration_seconds", None)
        role = message.get("type")
        turn = message.get("turn_id") or None
        if role in ("user", "user_message"):
            finish()
            value = message.get("timestamp")
            started = _try_parse_datetime(value) if not isinstance(value, bool) else None
            last_answer = None
            last_turn = turn
        elif role in ("assistant", "agent_message"):
            if message.get("after_turn_boundary") or (turn and last_turn and turn != last_turn):
                finish()
                started = None
                last_answer = None
            last_turn = turn or last_turn
            if str(message.get("text", message.get("message", ""))).strip():
                last_answer = index
    finish()
    return result


def format_answer_duration(seconds: object) -> str:
    """取得できない時間を推測せず、全saverで同じ表示を使う。"""
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not 0 <= seconds < float("inf"):
        return "⏱ 回答所要時間: 取得不可"
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    duration = (f"{hours}時間" if hours else "") + (f"{minutes}分" if minutes or hours else "") + f"{secs}秒"
    return f"⏱ 回答所要時間: {duration}"


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

# BEGIN GENERATED: incremental-answer-usage
def _answer_usage_groups(messages: list[dict]) -> list[tuple[int, dict | None, dict | None]]:
    """User発話ごとにAgentMessageを回答単位へまとめる。"""
    groups: list[tuple[int, dict | None, dict | None]] = []
    turn_index = 0
    last_model = None
    last_turn_id = None
    for message in messages:
        turn_id = message.get("turn_id") or None
        if message.get("type") == "user_message":
            last_turn_id = turn_id
            turn_index += 1
            last_model = None
            continue
        if message.get("type") != "agent_message" or not str(message.get("message", "")).strip():
            continue
        if message.get("after_turn_boundary") or (turn_id and last_turn_id and turn_id != last_turn_id):
            turn_index += 1
            last_model = None
        last_turn_id = turn_id or last_turn_id
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


def refresh_answer_usage_metadata(content: str, messages: list[dict]) -> str:
    """遅れて書かれたusage recordを既存assistant calloutへ反映する。

    本文・ユーザーcallout・既存のquota行は変更せず、assistant metadata内のUsage行だけを
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
        if line.rstrip("\r\n") in ("> [!QUESTION] User", HIDDEN_USER_BOUNDARY):
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
                    timing_index = next((index for index, line in enumerate(metadata_lines)
                                         if line.startswith("> <small>⏱ 回答所要時間: ")), len(metadata_lines))
                    metadata_lines.insert(timing_index, f"> <small>{usage_text}</small>{newline}")
                replacements.append((start, end, metadata_lines))
    for start, end, metadata_lines in sorted(replacements, reverse=True):
        lines[start:end] = metadata_lines
    return "".join(lines)
# END GENERATED: incremental-answer-usage

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

# BEGIN GENERATED: incremental-rendering
HIDDEN_USER_BOUNDARY = "<!-- user_turn_boundary -->"


def incremental_display_messages(messages: list[dict], model_name: str = "", effort: str = "") -> list[dict]:
    """増分入力を共通の表示recordへ変換する。非表示のuserも境界として残す。"""
    result = []
    turn = -1
    for index, message in enumerate(messages):
        role = {"user_message": "user", "agent_message": "assistant"}.get(message.get("type"))
        if role is None:
            continue
        if role == "user":
            turn += 1
        result.append(dict(message, type=role, text=message.get("message", ""),
            model=format_model_name(message.get("model") or model_name, message.get("effort") or effort),
            turn_index=message.get("turn_id") or message.get("turn_index", turn), sequence=index))
    return result


def render_incremental_messages(content: str, messages: list[dict], *, model_name: str = "", effort: str = "",
                                user_transform=None, reference_renderer=None) -> tuple[str, int, bool]:
    normalized = incremental_display_messages(messages, model_name, effort)
    groups = _display_message_groups(normalized)
    last_header = get_last_callout_header(content)
    if (last_header is None and HIDDEN_USER_BOUNDARY not in content
            and not any(message["text"].strip() for message in normalized)):
        return content, 0, False
    blocks = []
    appended = 0
    previous_turn = None
    for group in groups:
        message = group[0]
        turn = message.get("turn_index")
        if message["type"] == "assistant" and (message.get("after_turn_boundary") or
                (previous_turn is not None and previous_turn != turn)):
            blocks.append(HIDDEN_USER_BOUNDARY + "\n\n")
            last_header = None
        previous_turn = turn
        text = message["text"]
        if message["type"] == "user":
            if not text.strip():
                blocks.append(HIDDEN_USER_BOUNDARY + "\n\n")
                last_header = None
                continue
            display_text, reference = user_transform(text) if user_transform else (text, None)
            header = user_callout_header(message.get("timestamp"))
            user_block = render_user_message_block(display_text, message.get("timestamp"), continued=header == last_header)
            if reference is not None and reference_renderer is not None:
                user_block += reference_renderer(reference) + "\n\n"
            blocks.append(user_block)
            last_header = REFERENCE_CALLOUT_HEADER if reference is not None else header
            appended += 1
            continue
        visible = [item for item in group if item["text"].strip()]
        if not visible:
            continue
        usage = subagent_usage = None
        for item in group:
            if item.get("usage"):
                usage = _merge_usage_summaries(usage, item["usage"])
            if item.get("subagent_usage"):
                subagent_usage = _merge_usage_summaries(subagent_usage, item["subagent_usage"])
        model = message.get("model", "")
        metadata = [f"🤖 {model}"] if model else []
        if usage:
            metadata.append(format_message_usage(usage, subagent_usage))
        for item in group:
            if "answer_duration_seconds" in item:
                metadata.append(format_answer_duration(item["answer_duration_seconds"]))
        body = visible[0]["text"]
        for item in visible[1:]:
            separator = "\n\n" if item.get("after_tool_use") else "\n"
            body = body.rstrip("\n") + separator + item["text"]
        header = assistant_callout_header(AGENT_NAME, metadata)
        current_model = next((line for line in header.splitlines() if line.startswith("> <small>🤖 ")), "")
        previous_model = next((line for line in (last_header or "").splitlines() if line.startswith("> <small>🤖 ")), "")
        continued = bool(last_header and last_header.splitlines()[0] == f"> [!NOTE] {AGENT_NAME}" and previous_model == current_model)
        if continued:
            fragment = render_assistant_message_block(body, AGENT_NAME, metadata, continued=True)
            separator = "\n\n" if message.get("after_tool_use") else "\n"
            if blocks:
                blocks[-1] = blocks[-1].rstrip("\n") + separator + fragment + "\n\n"
            else:
                content = content.rstrip("\n") + separator
                blocks.append(fragment + "\n\n")
        else:
            blocks.append(render_assistant_message_block(body, AGENT_NAME, metadata, extra_blank_line=True))
            appended += 1
        last_header = header
    return content + "".join(blocks), appended, bool(blocks)
# END GENERATED: incremental-rendering

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


# BEGIN GENERATED: usage-model
def _normalize_usage_model(model: object) -> str:
    """プロバイダ接頭辞などを除き、単価表と照合できるモデル名へ変換する。"""
    name = str(model or "").strip().lower()
    if "/" in name:
        name = name.rsplit("/", 1)[-1]
    aliases = {
        "deepseek-flash": "deepseek-v4.1-flash",
        "deepseek-v4-1-flash": "deepseek-v4.1-flash",
        "deepseek-v4.1-flash-free": "deepseek-v4.1-flash",
        "deepseek-v4.1-flash:free": "deepseek-v4.1-flash",
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


# BEGIN GENERATED: usage-reference-cost
LONG_CONTEXT_INPUT_TOKENS = 272_000


def reference_price_model(model: object) -> str:
    name = _normalize_usage_model(model)
    if (name not in STANDARD_USAGE_RATES and name.endswith("-free")
            and name[:-len("-free")] in STANDARD_USAGE_RATES):
        name = name[:-len("-free")]
    return name


def is_long_context_usage(model: object, usage: dict) -> bool:
    """設定が有効な場合、登録済みGPTのリクエスト単位で長文料金を判定する。"""
    name = reference_price_model(model)
    tokens = usage.get("input_tokens", 0)
    return (APPLY_LONG_CONTEXT_PRICING
            and isinstance(tokens, int) and not isinstance(tokens, bool)
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
        "pricing_unknown": True,
    }
    if (raw.get("speed", "standard") not in (None, "standard")
            or raw.get("service_tier", "standard") not in (None, "standard")
            or raw.get("inference_geo") not in (None, "", "global")):
        return usage
    model = _normalize_usage_model(message.get("model"))
    reference_rates = STANDARD_USAGE_RATES.get(reference_price_model(model))
    # 1時間単価のないモデルはTTLを分けず、共通の書き込み単価を使う。
    if reference_rates is not None and "cache_write_input_1h" not in reference_rates:
        usage.pop("pricing_unknown")
        cost = _usage_cost(model, usage)
        if cost is not None:
            usage["cost_usd"] = cost
            if is_long_context_usage(model, usage):
                usage["long_context"] = True
        return usage
    # OpenRouter形式とAnthropic形式を同じ単価表に対応させる。
    model = model.replace(".", "-")
    for prefix, rates in STANDARD_USAGE_RATES.items():
        if "cache_write_input_1h" in rates and (model == prefix or model.startswith(prefix + "-")):
            break
    else:
        return usage
    cache_creation = raw.get("cache_creation") or {}
    five_min = cache_creation.get("ephemeral_5m_input_tokens", 0) if isinstance(cache_creation, dict) else None
    one_hour = cache_creation.get("ephemeral_1h_input_tokens", 0) if isinstance(cache_creation, dict) else None
    if (not isinstance(five_min, int) or not isinstance(one_hour, int)
            or isinstance(five_min, bool) or isinstance(one_hour, bool)
            or five_min < 0 or one_hour < 0 or five_min + one_hour != written):
        return usage
    cost = (Decimal(fresh) * rates["input"] + Decimal(cached) * rates["cached_input"]
            + Decimal(five_min) * rates["cache_write_input"]
            + Decimal(one_hour) * rates["cache_write_input_1h"]
            + Decimal(output) * rates["output"]) / Decimal(1_000_000)
    cost *= haiku_prompt_price_multiplier(prefix, usage)
    usage.pop("pricing_unknown")
    usage["cost_usd"] = cost
    return usage


def retrieve_usage(jsonl_path: Path, transcript_text: str | None = None) -> dict | None:
    """同じmessage IDの更新行は最後のusageだけを数える。"""
    records = {}
    try:
        with (io.StringIO(transcript_text) if transcript_text is not None
              else jsonl_path.open(encoding="utf-8")) as stream:
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
    return summarize_usage(
        ({"type": "assistant", "model": model, "usage": usage} for model, usage in records.values()),
        estimate_missing=False,
    )


def load_jsonl_messages(jsonl_path: Path, start_line: int, transcript_text: str | None = None) -> tuple[list[dict], int]:
    messages = []
    records = {}
    current_line = 0
    turn = -1
    try:
        with (io.StringIO(transcript_text) if transcript_text is not None else jsonl_path.open(encoding="utf-8")) as stream:
            for current_line, raw_line in enumerate(stream, 1):
                if not raw_line.strip():
                    continue
                try:
                    data = json.loads(raw_line)
                except json.JSONDecodeError:
                    if not raw_line.endswith(("\n", "\r")):
                        current_line -= 1
                        break
                    continue
                if not isinstance(data, dict) or data.get("isSidechain"):
                    continue
                kind = data.get("type")
                if kind == "system" and data.get("subtype") == "turn_duration":
                    for answer in reversed(messages):
                        if answer["type"] == "user_message":
                            break
                        if answer["type"] == "agent_message":
                            answer["completed_at"] = data.get("timestamp")
                            break
                msg = data.get("message") or {}
                if not isinstance(msg, dict):
                    continue
                if kind == "user":
                    if data.get("isMeta"):
                        continue
                    text = extract_user_text(msg)
                    if text.strip().startswith(("<local-command-", "<command-name>")):
                        continue
                    turn += 1
                    model = ""
                elif kind == "assistant":
                    text, model = extract_assistant_text_and_model(data)
                    usage = _claude_usage_record(msg)
                    message_id = str(msg.get("id") or data.get("uuid") or current_line)
                    if usage is not None:
                        records[message_id] = {"id": message_id, "type": "assistant", "model": model or "unknown",
                            "turn_index": turn, "sequence": current_line, "usage": usage}
                    if not text.strip():
                        continue
                else:
                    continue
                messages.append({"line_number": current_line, "sequence": current_line, "turn_index": turn,
                    "id": str(msg.get("id") or data.get("uuid") or current_line),
                    "type": "user_message" if kind == "user" else "agent_message",
                    "message": text, "timestamp": data.get("timestamp", ""), "model": model})
    except (OSError, UnicodeError) as error:
        logger.warning("JSONL読み込み失敗: %s", type(error).__name__)
        return [], start_line
    display = incremental_display_messages(messages)
    # Single-message groups attach each deduplicated usage only once. The renderer later merges replies.
    assigned = _usage_records_by_display_group([[message] for message in display], records.values())
    for message, usage_records in zip(messages, assigned):
        usage = summarize_usage(usage_records, estimate_missing=False)
        if usage is not None:
            message["usage"] = usage
    messages = annotate_answer_timings(messages)
    return [message for message in messages if message["line_number"] > start_line], current_line

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
    *, source: str | None = None, tags=None,
) -> str:
    return build_common_frontmatter(session_id, cwd, start_dt, start_dt, message_count,
                                    title, source=source, tags=tags, extra_fields=build_quota_frontmatter_fields(initial_quota, final_quota) + build_usage_frontmatter_fields(usage))


def create_md_file(session_id: str, cwd: str, start_dt: datetime, title: str | None = None,
                   usage: dict | None = None, initial_quota: dict | None = None, final_quota: dict | None = None,
                   *, source: str | None = None, tags=None) -> Path:
    path = note_output_path(session_id, cwd, start_dt)
    fm = build_frontmatter(session_id, cwd, start_dt, title=title, source=source, tags=tags,
        usage=usage, initial_quota=initial_quota, final_quota=final_quota)
    quota = build_quota_section(initial_quota, final_quota)
    content = fm + (quota + "\n" if quota else "") + build_usage_section(usage) + "\n<!-- last_line: 0 -->\n\n"
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

# BEGIN GENERATED: incremental-storage
def recover_incremental_state(session_id: str, cwd: str, state: dict, cursor_key: str, read_cursor) -> dict:
    """state消失時は既存ノートを探索し、識別情報・quota・cursorを復旧する。"""
    if not state.get("output_path"):
        cleanup_old_states()
        path = find_existing_md(session_id)
        if path is None:
            return state
        metadata = read_markdown_metadata(path)
        start = parse_datetime(metadata.get("created"), datetime.fromtimestamp(path.stat().st_mtime, tz=JST))
        first, last = recover_quota_frontmatter(path.read_text(encoding="utf-8"))
        state.update(output_path=str(path), start_time=format_iso(start), cwd=metadata.get("project") or cwd)
        state[cursor_key] = read_cursor(path) or (0 if cursor_key == "last_line" else "")
        state["initial_quota"] = merge_first_quota(first, state.get("initial_quota") or {})
        state["final_quota"] = merge_first_quota(state.get("final_quota"), last)
        logger.warning("stateを既存Markdownから復旧しました: %s", path)
    path = Path(state["output_path"])
    if path.exists():
        metadata = read_markdown_metadata(path)
        state["source"] = metadata.get("source") or state.get("source") or SOURCE_NAME
        state["tags"] = metadata.get("tags", state.get("tags", ["ai-conversation", state["source"]]))
    return state


def ensure_incremental_note(session_id: str, cwd: str, state: dict, cursor_key: str,
                            read_cursor, has_body: bool, create_note):
    """保存済みノートの識別を保持し、本文がある場合だけ新規作成・再作成する。"""
    path = Path(state["output_path"]) if state.get("output_path") else None
    if path is None or not path.exists():
        if not has_body:
            return None
        start = parse_datetime(state.get("start_time"), now_jst())
        state.setdefault("cwd", cwd)
        state.setdefault("source", SOURCE_NAME)
        state.setdefault("tags", ["ai-conversation", state["source"]])
        path = create_note(start, state)
        state.update(output_path=str(path), start_time=format_iso(start))
        state[cursor_key] = 0 if cursor_key == "last_line" else ""
        logger.info("セッション初期化 %s -> %s", session_id[:8], path)
    if cursor_key == "last_line":
        try:
            saved = int(state.get(cursor_key, 0))
        except (TypeError, ValueError):
            saved = 0
    else:
        saved = str(state.get(cursor_key, ""))
    observed = read_cursor(path)
    cursor = observed if observed is not None else saved
    if observed is not None and observed != saved:
        logger.warning("%sの不一致をMarkdownから復旧: state=%r, markdown=%r", cursor_key, saved, observed)
    state[cursor_key] = cursor
    save_state(session_id, state)
    return path, cursor
# END GENERATED: incremental-storage


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
        if line == HIDDEN_USER_BOUNDARY:
            last_header = None
            header_lines = []
        elif line.startswith("> [!"):
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
        body_text = re.sub(r"(?m)^(?:📈 \*\*Usage\*\*|💰 \*\*Usage \((?:API|USD)換算\)\*\*):.*\n(?:\n)?", "", body_text, count=1)
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
    rendered, appended, changed = render_incremental_messages(content, messages)
    if not changed:
        return 0, False
    final_content = update_markdown_metadata(rendered, modified_dt, appended, last_line,
                                             usage, initial_quota, final_quota)
    atomic_write_md(path, final_content)
    return appended, True

# ============================================================
# イベントハンドラ
# ============================================================
def read_stop_transcript(jsonl_path: Path, expected_answer: str = "") -> str:
    """Stop時の遅延書き込みを最大2秒待ち、本文とUsage共通のsnapshotを返す。"""
    deadline = time.monotonic() + 2.0
    previous = None
    stable_since = time.monotonic()
    while True:
        text = jsonl_path.read_text(encoding="utf-8")
        now = time.monotonic()
        if text != previous:
            previous = text
            stable_since = now
        answer_ready = not expected_answer
        last_answer = ""
        last_usage_ready = False
        complete = True
        for line in text.splitlines(keepends=True):
            try:
                data = json.loads(line)
            except ValueError:
                if not line.endswith(("\n", "\r")):
                    complete = False
                continue
            if not isinstance(data, dict):
                continue
            if data.get("type") == "assistant":
                answer, _ = extract_assistant_text_and_model(data)
                if answer.strip():
                    last_answer = answer.strip()
                    last_usage_ready = _claude_usage_record(data.get("message", {})) is not None
        if expected_answer:
            answer_ready = last_answer == expected_answer.strip() and last_usage_ready
        if complete and answer_ready and now - stable_since >= 0.3:
            return text
        if now >= deadline:
            logger.warning("Stop transcriptの書き込み待機が上限に達しました")
            return text
        time.sleep(0.05)


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
    try:
        transcript_text = read_stop_transcript(
            jsonl_path, hook_input.get("last_assistant_message") or "",
        )
    except (OSError, UnicodeError) as error:
        logger.warning("JSONL読み込み失敗: %s", error)
        return
    current_usage = retrieve_usage(jsonl_path, transcript_text)

    state = load_state(session_id)
    try:
        state = recover_incremental_state(session_id, cwd, state, "last_line", read_last_line_from_md)
    except ExistingMarkdownSearchError as error:
        logger.error("既存Markdownの探索中にエラーが発生したため保存を見送ります: %s", type(error).__name__)
        return
    preview, _ = load_jsonl_messages(jsonl_path, 0, transcript_text)
    prepared = ensure_incremental_note(session_id, cwd, state, "last_line", read_last_line_from_md,
        any(str(message.get("message", "")).strip() for message in preview),
        lambda start, saved: create_md_file(session_id, saved.get("cwd", cwd), start,
            usage=current_usage, initial_quota=saved.get("initial_quota"), final_quota=saved.get("final_quota"),
            source=saved.get("source"), tags=saved.get("tags")))
    if prepared is None:
        return
    path, last_line = prepared

    # メッセージのロードと追記
    new_messages, new_last_line = load_jsonl_messages(jsonl_path, last_line, transcript_text)

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

    # All answer Usage, including late or updated records, comes from the same Stop snapshot.
    if current_usage is not None or state.get("final_quota"):
        content = path.read_text(encoding="utf-8")
        all_messages, _ = load_jsonl_messages(jsonl_path, 0, transcript_text)
        refreshed = update_markdown_metadata(content, now_jst(), 0, new_last_line, current_usage,
            state.get("initial_quota"), state.get("final_quota"))
        refreshed = refresh_answer_usage_metadata(refreshed, all_messages)
        refreshed = preserve_modified_if_unchanged(content, refreshed)
        if refreshed != content:
            atomic_write_md(path, refreshed)
    content = path.read_text(encoding="utf-8")
    all_messages, _ = load_jsonl_messages(jsonl_path, 0, transcript_text)
    refreshed = refresh_answer_timing_metadata(content, all_messages)
    if refreshed != content:
        atomic_write_md(path, refreshed)
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
        if "--capture-quota" in sys.argv[1:]:
            print(capture_statusline_quota(read_stdin()))
        else:
            main()
    except Exception as e:
        logger.exception(f"予期しないエラー: {e}")
        print("{}")
        sys.exit(0)
