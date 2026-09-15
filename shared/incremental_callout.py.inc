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
