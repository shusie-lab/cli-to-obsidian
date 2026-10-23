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
