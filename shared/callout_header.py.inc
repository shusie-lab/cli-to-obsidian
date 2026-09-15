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
