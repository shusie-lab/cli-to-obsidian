def update_frontmatter_fields(
    content: str,
    modified_dt: datetime,
    appended: int,
    *,
    extra_removed_keys: tuple[str, ...] = (),
    extra_fields: tuple[str, ...] = (),
    note_fields: dict[str, list[str]] | None = None,
) -> tuple[list[str], str] | None:
    """frontmatterの共通項目を更新し、項目行と本文を返す。"""
    fm_match = FRONTMATTER.match(content)
    if not fm_match:
        return None

    fm_lines = fm_match.group(1).splitlines()
    body_text = re.sub(r"\A(?:[ \t]*\n)+", "", content[fm_match.end():])
    existing_keys = set()
    keys_to_remove = {"modified", "message_count", *extra_removed_keys}
    new_fm_lines = []
    skip_list = False
    old_count = 0
    for line in fm_lines:
        key = frontmatter_key(line)
        if key == "message_count":
            try:
                old_count = int(line.split(":", 1)[1].strip())
            except ValueError:
                pass
        if key is not None:
            existing_keys.add(key)
            if key in keys_to_remove:
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

    if "title" not in existing_keys:
        title_match = re.search(r"(?m)^# (?:User: )?(.+?)\s*$", body_text)
        if title_match:
            title = unescape_markdown_heading(title_match.group(1))
            new_fm_lines.append(f"title: {yaml_quote(title)}")
    for key, lines in (note_fields or {}).items():
        if key not in existing_keys:
            new_fm_lines.extend(lines)
    new_fm_lines.append(f"modified: {yaml_quote(format_iso(modified_dt))}")
    new_fm_lines.append(f"message_count: {old_count + appended}")
    new_fm_lines.extend(extra_fields)
    return new_fm_lines, body_text


def compose_markdown(new_fm_lines: list[str], body_text: str) -> str:
    """更新済みfrontmatterと本文をMarkdown全文へ戻す。"""
    return "---\n" + "\n".join(new_fm_lines) + "\n---\n" + body_text
