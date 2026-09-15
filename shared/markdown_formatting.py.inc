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
