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
    body = text
    if continued:
        return body
    separator = "\n\n\n" if extra_blank_line else "\n\n"
    return f"{assistant_callout_header(agent_name, metadata)}{separator}{body}\n\n"
