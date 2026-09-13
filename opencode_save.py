#!/usr/bin/python3
from __future__ import annotations

"""OpenCode プラグインから渡された会話を Obsidian に保存する。"""

__version__ = "1.3.0"

import argparse
import fcntl
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import re
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


JST = timezone(timedelta(hours=9))
OBSIDIAN_VAULT = Path(os.environ.get("OBSIDIAN_VAULT", str(Path.home() / "obsidian")))
DEFAULT_OUTPUT_DIR = "生成AI/ChatLog"


def resolve_output_dir(value: Optional[str] = None) -> Path:
    raw = os.environ.get("OBSIDIAN_OUTPUT_DIR", DEFAULT_OUTPUT_DIR) if value is None else value
    candidate = Path(raw) if str(raw).strip() else Path(DEFAULT_OUTPUT_DIR)
    if candidate.is_absolute() or ".." in candidate.parts:
        return Path(DEFAULT_OUTPUT_DIR)
    return candidate


OUTPUT_BASE = OBSIDIAN_VAULT / resolve_output_dir() / "opencode"
STATE_DIR = Path.home() / ".local" / "state" / "opencode-obsidian"
LOG_FILE = STATE_DIR / "opencode_obsidian_save.log"
STATE_RETENTION_DAYS = 30
SAFE_SESSION_CHARS = re.compile(r"[^A-Za-z0-9_.-]+")


def setup_logger() -> logging.Logger:
    logger = logging.getLogger("opencode_obsidian_save")
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


class ExistingMarkdownSearchError(Exception):
    """既存Markdown探索中のI/Oエラー。誤上書き・重複作成を防ぐために使用。"""


@contextmanager
def session_lock(session_id: str):
    lock_dir = STATE_DIR / ".locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / f"{safe_session_key(session_id)}.lock"
    with open(lock_path, "a", encoding="utf-8") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


@contextmanager
def try_session_lock(session_id: str, *, is_key: bool = False):
    """同一セッションの hook 実行を非ブロッキングで試みる。取得できなければ False を yield。"""
    lock_dir = STATE_DIR / ".locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / f"{session_id if is_key else safe_session_key(session_id)}.lock"
    try:
        stream = open(lock_path, "a", encoding="utf-8")
    except OSError:
        yield False
        return
    try:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        stream.close()
        yield False
        return
    try:
        yield True
    finally:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        stream.close()


def safe_session_key(session_id: str) -> str:
    raw = str(session_id)
    safe = SAFE_SESSION_CHARS.sub("_", raw).strip("._")[:80]
    if safe and safe == raw:
        return safe
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
    return f"{safe or 'session'}-{digest}"


def state_path(session_id: str) -> Path:
    return STATE_DIR / f"{safe_session_key(session_id)}.json"


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp", delete=False
        ) as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
            temporary = Path(stream.name)
        if path.exists():
            os.chmod(temporary, path.stat().st_mode & 0o7777)
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def load_state(session_id: str) -> Dict[str, Any]:
    try:
        return json.loads(state_path(session_id).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}


def save_state(session_id: str, state: Dict[str, Any]) -> None:
    state["last_used_at"] = format_iso(datetime.now(JST))
    atomic_write(state_path(session_id), json.dumps(state, ensure_ascii=False, indent=2))


def cleanup_old_states() -> None:
    """STATE_RETENTION_DAYS日以上前のstateファイルを削除"""
    if not STATE_DIR.exists():
        return
    cutoff = datetime.now(JST) - timedelta(days=STATE_RETENTION_DAYS)
    removed = 0
    try:
        candidates = list(STATE_DIR.glob("*.json"))
    except OSError as e:
        logger.warning("state探索に失敗したためクリーンアップを見送ります: %s", e)
        return
    for f in candidates:
        session_key = f.stem
        with try_session_lock(session_key, is_key=True) as acquired:
            if not acquired:
                continue
            try:
                if not f.exists():
                    continue
                data = json.loads(f.read_text(encoding="utf-8"))
                last_used_str = data.get("last_used_at")
                last_used = None
                if last_used_str:
                    try:
                        last_used = datetime.fromisoformat(str(last_used_str))
                        if last_used.tzinfo is None:
                            last_used = last_used.replace(tzinfo=JST)
                    except ValueError:
                        pass
                if last_used is None:
                    last_used = datetime.fromtimestamp(f.stat().st_mtime, tz=JST)

                if last_used < cutoff:
                    f.unlink()
                    removed += 1
            except Exception as e:
                logger.info("stateファイルのクリーンアップ中にスキップ: %s: %s", f.name, e)
    if removed > 0:
        logger.info("古いstateファイル %d 件を削除しました", removed)


def yaml_quote(value: Any) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def safe_filename_component(value: str) -> str:
    return re.sub(r"[\x00-\x1f/:*?\[\]\\]", "_", value).strip()[:80]


def _try_parse_datetime(value: Any) -> Optional[datetime]:
    if isinstance(value, (int, float)):
        # OpenCode の time.created は Unix epoch milliseconds。
        return datetime.fromtimestamp(float(value) / 1000, tz=timezone.utc).astimezone(JST)
    if isinstance(value, str) and value:
        try:
            normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
            parsed = datetime.fromisoformat(normalized)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(JST)
        except ValueError:
            pass
    return None


def parse_datetime(value: Any, default: Optional[datetime] = None) -> datetime:
    parsed = _try_parse_datetime(value)
    if parsed is not None:
        return parsed
    return default or datetime.now(JST)


def format_iso(value: datetime) -> str:
    return value.astimezone(JST).isoformat(timespec="seconds")


def format_message_time(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, str) and value.isdigit():
        value = int(value)
    parsed = _try_parse_datetime(value)
    if parsed is None:
        return ""
    return f"> <small>⏱ {parsed:%Y-%m-%d %H:%M:%S}</small>\n>\n"


def callout_lines(text: str) -> str:
    lines = []
    for line in str(text).split("\n"):
        line = line.replace("<", "&lt;")
        stripped = line.lstrip()
        if stripped.startswith(">"):
            line = line[: len(line) - len(stripped)] + "&gt;" + stripped[1:]
        lines.append(f"> {line}" if line else ">")
    return "\n".join(lines)


def escape_heading(text: str) -> str:
    return re.sub(r"([\\`*_{}\[\]()#+\-.!|<>~])", r"\\\1", text)


def user_heading(text: str) -> str:
    first = next((line.strip() for line in text.splitlines() if line.strip()), "User")
    if len(first) > 40:
        first = first[:40] + "..."
    return f"# User: {escape_heading(first)}\n"


def _message_text(envelope: Dict[str, Any]) -> str:
    parts = envelope.get("parts") or []
    texts = []
    for part in parts:
        if not isinstance(part, dict) or part.get("type") != "text":
            continue
        text = part.get("text", "")
        if isinstance(text, str) and text.strip():
            texts.append(text)
    return "\n".join(texts)


def extract_messages(raw_messages: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    messages: List[Dict[str, Any]] = []
    for envelope in raw_messages:
        if not isinstance(envelope, dict):
            continue
        info = envelope.get("info") or envelope.get("message") or {}
        if isinstance(info, dict) and info.get("role"):
            role = info.get("role", "")
            message_id = info.get("id") or info.get("messageID") or ""
            message_time = info.get("time") or {}
            raw_model = info.get("modelID") or info.get("model") or ""
            parts = envelope.get("parts") or []
        else:
            # OpenCode SDK の session.messages は type/id/text/content 形式。
            info = envelope
            role = envelope.get("role") or envelope.get("type", "")
            message_id = envelope.get("id") or envelope.get("messageID") or ""
            message_time = envelope.get("time") or {}
            raw_model = envelope.get("model") or envelope.get("modelID") or ""
            parts = envelope.get("content") or []
        if role not in ("user", "assistant"):
            continue
        if parts:
            text = _message_text({"parts": parts})
        else:
            text = envelope.get("text", "")
            if not isinstance(text, str):
                text = ""
        if not text:
            continue
        if isinstance(message_time, dict):
            timestamp = message_time.get("created") or message_time.get("completed") or ""
        else:
            timestamp = message_time
        if isinstance(raw_model, dict):
            model = str(raw_model.get("modelID") or raw_model.get("id") or "")
        else:
            model = str(raw_model)
        messages.append(
            {
                "id": str(message_id),
                "type": role,
                "text": text,
                "timestamp": timestamp,
                "model": model,
            }
        )
    return messages


def _message_key(message: Dict[str, Any]) -> Tuple[str, ...]:
    message_id = str(message.get("id", ""))
    if message_id:
        return ("id", message_id)
    return (
        "content",
        str(message.get("type", "")),
        str(message.get("text", "")),
        str(message.get("timestamp", "")),
        str(message.get("model", "")),
    )


def _message_content_key(message: Dict[str, Any]) -> Tuple[str, ...]:
    return (
        str(message.get("type", "")),
        str(message.get("text", "")),
        str(message.get("model", "")),
    )


def _normalized_timestamp(value: Any) -> str:
    parsed = _try_parse_datetime(value)
    return format_iso(parsed) if parsed is not None else str(value or "")


def _messages_match_for_recovery(existing: Dict[str, Any], incoming: Dict[str, Any]) -> bool:
    if existing.get("id"):
        return False
    if existing.get("type") != "user" or incoming.get("type") != "user":
        return False
    if _message_content_key(existing) != _message_content_key(incoming):
        return False
    return _normalized_timestamp(existing.get("timestamp")) == _normalized_timestamp(incoming.get("timestamp"))


def merge_messages(
    existing: Iterable[Dict[str, Any]], incoming: Iterable[Dict[str, Any]], full_sync: bool = False
) -> List[Dict[str, Any]]:
    """部分 payload と全履歴 payload を統合する。全履歴は受信順を優先する。"""
    old_messages = [dict(message) for message in existing if isinstance(message, dict)]
    new_messages = [dict(message) for message in incoming if isinstance(message, dict)]
    if not full_sync:
        merged = old_messages
        indexes = {_message_key(message): index for index, message in enumerate(merged)}
        index = 0
        while index < len(new_messages):
            # 状態ファイル消失後は assistant の本文だけではターンを識別できない。
            # user と assistant を組で復元し、質問が異なる新しいターンを誤統合しない。
            if (
                index + 1 < len(new_messages)
                and new_messages[index].get("type") == "user"
                and new_messages[index + 1].get("type") == "assistant"
            ):
                user_message, assistant_message = new_messages[index : index + 2]
                exact_index = next(
                    (
                        old_index
                        for old_index, old_message in enumerate(merged)
                        if _message_key(old_message) in {
                            _message_key(user_message),
                            _message_key(assistant_message),
                        }
                    ),
                    None,
                )
                if exact_index is None:
                    pair_index = next(
                        (
                            old_index
                            for old_index in range(len(merged) - 1)
                            if _messages_match_for_recovery(merged[old_index], user_message)
                            and _message_content_key(merged[old_index + 1])
                            == _message_content_key(assistant_message)
                        ),
                        None,
                    )
                    if pair_index is None:
                        merged.extend((user_message, assistant_message))
                    else:
                        merged[pair_index] = {**merged[pair_index], **user_message}
                        merged[pair_index + 1] = {**merged[pair_index + 1], **assistant_message}
                    indexes = {_message_key(message): item_index for item_index, message in enumerate(merged)}
                    index += 2
                    continue

            message = new_messages[index]
            key = _message_key(message)
            matched_index = indexes.get(key)
            if matched_index is None and message.get("type") == "user":
                matched_index = next(
                    (
                        old_index
                        for old_index, old_message in enumerate(merged)
                        if _messages_match_for_recovery(old_message, message)
                    ),
                    None,
                )
            if matched_index is None:
                indexes[key] = len(merged)
                merged.append(message)
            else:
                merged[matched_index] = {**merged[matched_index], **message}
            index += 1
        return merged

    merged: List[Dict[str, Any]] = []
    used = set()
    for message in new_messages:
        key = _message_key(message)
        content_key = _message_content_key(message)
        index = next(
            (
                index
                for index, old_message in enumerate(old_messages)
                if index not in used
                and _message_key(old_message) == key
            ),
            None,
        )
        if index is None:
            # state消失後にMarkdownから復元した履歴だけは本文で照合する。
            # ID付き履歴は先にIDを優先するため、同じ回答本文でも別メッセージを再利用しない。
            index = next(
                (
                    index
                    for index, old_message in enumerate(old_messages)
                    if index not in used
                    and not old_message.get("id")
                    and _message_content_key(old_message) == content_key
                ),
                None,
            )
        if index is None:
            merged.append(message)
        else:
            used.add(index)
            merged.append({**old_messages[index], **message})
    merged.extend(message for index, message in enumerate(old_messages) if index not in used)
    return merged


def find_existing_md(session_id: str) -> Optional[Path]:
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
        # globは一部の読み取りエラーを抑制するため、ディレクトリを直接列挙する。
        for path in OUTPUT_BASE.iterdir():
            if any(path.name.endswith(f"_{identifier}.md") for identifier in identifiers):
                candidates.add(path)
    except OSError as e:
        raise ExistingMarkdownSearchError(f"既存Markdown候補の列挙に失敗しました: {e}")

    candidate_entries = []
    for candidate in candidates:
        try:
            candidate_entries.append((candidate, candidate.stat().st_mtime))
        except OSError as e:
            raise ExistingMarkdownSearchError(f"既存Markdown候補の情報取得に失敗しました: {candidate}: {e}")

    candidate_entries.sort(key=lambda item: item[1], reverse=True)

    had_read_error = False
    last_read_error = None
    for candidate, _ in candidate_entries:
        try:
            content = candidate.read_text(encoding="utf-8")
            parts = content.split("---", 2)
            if len(parts) >= 3:
                frontmatter_lines = [line.strip() for line in parts[1].splitlines()]
                if any(exp in frontmatter_lines for exp in expected):
                    return candidate
        except (OSError, UnicodeError) as e:
            had_read_error = True
            last_read_error = e
            logger.warning(f"既存Markdown候補の読み込みに失敗したためスキップします: {candidate}: {e}")
            continue
        except IndexError:
            continue

    if had_read_error:
        raise ExistingMarkdownSearchError(
            f"既存Markdown候補の読み取りに失敗したファイルが存在し、一致を確認できなかったため探索を中断します: {last_read_error}"
        )

    return None


def _markdown_timestamp(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value).replace(tzinfo=JST)
    except ValueError:
        return value
    return parsed.isoformat(timespec="seconds")


def parse_existing_markdown(path: Path) -> List[Dict[str, Any]]:
    """状態ファイルが失われた場合に、このスクリプトのMarkdownを復元する。"""
    try:
        sections = path.read_text(encoding="utf-8").split("---", 2)
        lines = sections[2].splitlines() if len(sections) == 3 else []
    except (OSError, UnicodeError):
        return []

    markers = []
    fence_character = None
    fence_length = 0
    for index, line in enumerate(lines):
        fence = re.match(r"^\s*(`{3,}|~{3,})", line)
        if fence:
            character = fence.group(1)[0]
            length = len(fence.group(1))
            if fence_character is None:
                fence_character = character
                fence_length = length
            elif character == fence_character and length >= fence_length:
                fence_character = None
                fence_length = 0
            continue
        if fence_character is not None:
            continue
        if line.startswith("# User: ") and index + 1 < len(lines) and lines[index + 1] == "> [!QUESTION] User":
            markers.append((index, "user"))
        elif line == "> [!NOTE] OpenCode":
            markers.append((index, "assistant"))

    messages: List[Dict[str, Any]] = []
    for marker_index, (start, role) in enumerate(markers):
        end = markers[marker_index + 1][0] if marker_index + 1 < len(markers) else len(lines)
        if role == "user":
            index = start + 2
            timestamp = ""
            if index < end and lines[index].startswith("> <small>⏱ "):
                timestamp = _markdown_timestamp(lines[index][len("> <small>⏱ ") : -len("</small>")])
                index += 1
            if index < end and lines[index] == ">":
                index += 1
            body = []
            while index < end:
                line = lines[index]
                if line == "":
                    break
                if line == ">":
                    body.append("")
                elif line.startswith("> "):
                    body.append(line[2:])
                else:
                    break
                index += 1
            text = "\n".join(body).replace("&lt;", "<").replace("&gt;", ">")
            if text.strip():
                messages.append({"id": "", "type": role, "text": text, "timestamp": timestamp, "model": ""})
            continue

        index = start + 1
        model = ""
        if index < end and lines[index].startswith("> <small>🤖 "):
            model = lines[index][len("> <small>🤖 ") : -len("</small>")]
            index += 1
        if index < end and lines[index] == "":
            index += 1
        body = "\n".join(lines[index:end]).strip("\n")
        if body.strip():
            messages.append({"id": "", "type": role, "text": body, "timestamp": "", "model": model})
    return messages


def build_frontmatter(session_id: str, cwd: str, created: datetime, modified: datetime, count: int) -> str:
    return "\n".join(
        [
            "---",
            "source: opencode",
            f"session_id: {yaml_quote(session_id)}",
            f"project: {yaml_quote(cwd)}",
            f"created: {yaml_quote(format_iso(created))}",
            f"modified: {yaml_quote(format_iso(modified))}",
            "tags:",
            "  - ai-conversation",
            "  - opencode",
            f"message_count: {count}",
            "---",
        ]
    ) + "\n\n"


def render_markdown(session_id: str, cwd: str, created: datetime, messages: List[Dict[str, str]]) -> str:
    modified = datetime.now(JST)
    blocks = [build_frontmatter(session_id, cwd, created, modified, len(messages))]
    for message in messages:
        timestamp = format_message_time(message.get("timestamp"))
        if message["type"] == "user":
            blocks.append(f"{user_heading(message['text'])}> [!QUESTION] User\n{timestamp}{callout_lines(message['text'])}\n\n")
        else:
            model = message.get("model", "")
            meta = f"> <small>🤖 {model}</small>\n" if model else ""
            blocks.append(f"> [!NOTE] OpenCode\n{meta}\n{message['text']}\n\n")
    return "".join(blocks)


def _handle_payload(payload: Dict[str, Any]) -> Optional[Path]:
    session_id = str(payload.get("session_id", ""))
    if not session_id:
        logger.warning("session_id が取得できませんでした")
        return None
    cwd = str(payload.get("cwd", "") or os.getcwd())
    messages = extract_messages(payload.get("messages") or [])
    state = load_state(session_id)
    if not state:
        cleanup_old_states()
    try:
        output_path = Path(state["output_path"]) if state.get("output_path") else find_existing_md(session_id)
    except ExistingMarkdownSearchError as e:
        logger.error("既存Markdownの探索中にエラーが発生したため保存を見送ります: %s", e)
        return None
    stored_messages = state.get("messages") if isinstance(state.get("messages"), list) else []
    if not stored_messages and output_path is not None:
        stored_messages = parse_existing_markdown(output_path)
    messages = merge_messages(stored_messages, messages, full_sync=bool(payload.get("full_sync")))
    if not messages:
        logger.debug("本文がないため保存をスキップ %s", session_id[:8])
        return output_path
    hash_messages = [
        {
            "id": item.get("id", ""),
            "type": item["type"],
            "text": item["text"],
            "timestamp": item.get("timestamp", ""),
            "model": item.get("model", ""),
        }
        for item in messages
    ]
    payload_hash = hashlib.sha256(
        json.dumps({"cwd": cwd, "messages": hash_messages}, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    if state.get("payload_hash") == payload_hash and output_path is not None and output_path.exists():
        logger.debug("同じ payload のため保存をスキップ %s", session_id[:8])
        save_state(session_id, state)
        return output_path
    if output_path is None:
        created = parse_datetime(payload.get("created"))
        project = safe_filename_component(Path(cwd).name) if cwd else ""
        session_key = safe_session_key(session_id) or "unknown"
        OUTPUT_BASE.mkdir(parents=True, exist_ok=True)
        if project:
            output_path = OUTPUT_BASE / f"{created:%Y%m%d}_{created:%H%M%S}_{project}_{session_key}.md"
        else:
            output_path = OUTPUT_BASE / f"{created:%Y%m%d}_{created:%H%M%S}_{session_key}.md"
        state = {"output_path": str(output_path), "created": format_iso(created), "cwd": cwd}
    else:
        fallback_created = datetime.now(JST)
        try:
            fallback_created = datetime.fromtimestamp(output_path.stat().st_mtime, tz=JST)
        except OSError:
            pass
        created = parse_datetime(state.get("created"), fallback_created)
    atomic_write(output_path, render_markdown(session_id, cwd, created, messages))
    state.update({
        "output_path": str(output_path),
        "created": format_iso(created),
        "cwd": cwd,
        "message_count": len(messages),
        "messages": messages,
        "payload_hash": payload_hash,
    })
    save_state(session_id, state)
    logger.info("セッション保存完了 %s -> %s (%d件)", session_id[:8], output_path, len(messages))
    return output_path


def handle_payload(payload: Dict[str, Any]) -> Optional[Path]:
    session_id = str(payload.get("session_id", ""))
    if not session_id:
        return _handle_payload(payload)
    with session_lock(session_id):
        return _handle_payload(payload)


def read_payload(path: Optional[str]) -> Dict[str, Any]:
    if path:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    import sys

    return json.load(sys.stdin)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", help="プラグインが作成した JSON ファイル")
    args = parser.parse_args()
    handle_payload(read_payload(args.input))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        logger.exception("保存に失敗しました: %s", error)
        raise SystemExit(0)
