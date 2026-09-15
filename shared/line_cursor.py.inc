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
