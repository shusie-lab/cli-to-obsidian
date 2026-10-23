def render_full_session_markdown(
    session_id: str, cwd: str, created: datetime, messages: list[dict],
    agent_name: str, *, modified: datetime | None = None,
    usage_records=None, display_usage_records=None, source: str | None = None, tags=None,
) -> str:
    messages = annotate_answer_timings(messages)
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
        for item in group:
            if "answer_duration_seconds" in item:
                metadata.append(format_answer_duration(item["answer_duration_seconds"]))
        body = "\n\n".join(item["text"].strip("\n") for item in group)
        blocks.append(render_assistant_message_block(body, agent_name, metadata))
    return "".join(blocks)
