FRONTMATTER = re.compile(r"\A---[ \t]*\n(.*?)^---[ \t]*(?:\n|$)", re.M | re.S)


def frontmatter_key(line: str) -> str | None:
    match = re.match(r"""^(['"]?)([a-zA-Z0-9_-]+)\1[ \t]*:(?=[ \t]|$)""", line)
    return match[2] if match else None


def build_note_fields(
    session_id: str, cwd: str, created: datetime, *,
    source: str | None = None, tags: list[str] | None = None,
) -> dict[str, list[str]]:
    source = SOURCE_NAME if source is None else source
    tag_lines = ["tags:"]
    for tag in tags if tags is not None else ["ai-conversation", source]:
        # Preserve the conventional plain form where it is an unambiguous YAML scalar.
        safe = re.fullmatch(r"[A-Za-z][A-Za-z0-9._-]*", str(tag))
        value = str(tag) if safe and str(tag).lower() not in {"true", "false", "null", "yes", "no", "on", "off"} else yaml_quote(tag)
        tag_lines.append(f"  - {value}")
    return {
        "source": [f"source: {source}"],
        "session_id": [f"session_id: {yaml_quote(session_id)}"],
        "project": [f"project: {yaml_quote(cwd)}"],
        "created": [f"created: {yaml_quote(format_iso(created))}"],
        "tags": tag_lines,
    }


def build_common_frontmatter(
    session_id: str, cwd: str, created: datetime, modified: datetime,
    count: int, title: str | None = None, *, source: str | None = None,
    tags: list[str] | None = None, extra_fields=(),
) -> str:
    fields = build_note_fields(session_id, cwd, created, source=source, tags=tags)
    lines = [
        "---", *fields["source"],
        *([f"title: {yaml_quote(title)}"] if title is not None else []),
        *fields["session_id"], *fields["project"], *fields["created"],
        f"modified: {yaml_quote(format_iso(modified))}", *fields["tags"],
        f"message_count: {count}", *extra_fields, "---",
    ]
    return "\n".join(lines) + "\n\n"


def note_output_path(session_id: str, cwd: str, created: datetime) -> Path:
    project = safe_filename_component(Path(cwd).name) if cwd else ""
    prefix = f"{created:%Y%m%d}_{created:%H%M%S}"
    if project:
        prefix += f"_{project}"
    return OUTPUT_BASE / f"{prefix}_{safe_session_key(session_id) if session_id else 'unknown'}.md"


def read_markdown_metadata(path: Path) -> dict:
    """標準ライブラリで、このプロジェクトのfrontmatterの識別項目だけを読む。"""
    return parse_markdown_metadata(path.read_text(encoding="utf-8"))


def parse_markdown_metadata(content: str) -> dict:
    match = FRONTMATTER.match(content)
    if not match:
        return {}
    result = {}
    in_tags = False
    for line in match.group(1).splitlines():
        if line.startswith("tags:"):
            in_tags = True
            result["tags"] = []
            continue
        if in_tags and line.startswith("  - "):
            raw = line[4:].strip()
            try:
                tag = json.loads(raw)
            except ValueError:
                tag = raw
            if isinstance(tag, str):
                result["tags"].append(tag)
            continue
        if line and not line.startswith((" ", "\t")):
            in_tags = False
        if line.startswith(("source:", "created:", "title:", "session_id:", "project:")):
            key, raw = line.split(":", 1)
            try:
                value = json.loads(raw.strip())
            except ValueError:
                value = raw.strip()
            if isinstance(value, str):
                result[key] = value
    return result


def preserve_modified_if_unchanged(content: str, refreshed: str) -> str:
    """更新時刻以外が一致するなら既存ノートをそのまま返す。"""
    # Scope to frontmatter, so an assistant's code or prose is never replaced.
    pattern = r"\A(---\s*\n)(.*?)(\n---\s*\n)"
    old = re.match(pattern, content, re.DOTALL)
    new = re.match(pattern, refreshed, re.DOTALL)
    if not old or not new:
        return refreshed
    modified = next((line for line in old.group(2).splitlines() if line.startswith("modified:")), None)
    if modified is None:
        return refreshed
    fields = re.sub(r"(?m)^modified:.*$", lambda _: modified, new.group(2), count=1)
    unchanged_time = new.group(1) + fields + new.group(3) + refreshed[new.end():]
    return content if unchanged_time == content else refreshed
