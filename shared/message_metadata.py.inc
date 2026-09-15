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
