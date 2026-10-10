def recover_incremental_state(session_id: str, cwd: str, state: dict, cursor_key: str, read_cursor) -> dict:
    """state消失時は既存ノートを探索し、識別情報・quota・cursorを復旧する。"""
    if not state.get("output_path"):
        cleanup_old_states()
        path = find_existing_md(session_id)
        if path is None:
            return state
        metadata = read_markdown_metadata(path)
        start = parse_datetime(metadata.get("created"), datetime.fromtimestamp(path.stat().st_mtime, tz=JST))
        first, last = recover_quota_frontmatter(path.read_text(encoding="utf-8"))
        state.update(output_path=str(path), start_time=format_iso(start), cwd=metadata.get("project") or cwd)
        state[cursor_key] = read_cursor(path) or (0 if cursor_key == "last_line" else "")
        state["initial_quota"] = merge_first_quota(first, state.get("initial_quota") or {})
        state["final_quota"] = merge_first_quota(state.get("final_quota"), last)
        logger.warning("stateを既存Markdownから復旧しました: %s", path)
    path = Path(state["output_path"])
    if path.exists():
        metadata = read_markdown_metadata(path)
        state["source"] = metadata.get("source") or state.get("source") or SOURCE_NAME
        state["tags"] = metadata.get("tags", state.get("tags", ["ai-conversation", state["source"]]))
    return state


def ensure_incremental_note(session_id: str, cwd: str, state: dict, cursor_key: str,
                            read_cursor, has_body: bool, create_note):
    """保存済みノートの識別を保持し、本文がある場合だけ新規作成・再作成する。"""
    path = Path(state["output_path"]) if state.get("output_path") else None
    if path is None or not path.exists():
        if not has_body:
            return None
        start = parse_datetime(state.get("start_time"), now_jst())
        state.setdefault("cwd", cwd)
        state.setdefault("source", SOURCE_NAME)
        state.setdefault("tags", ["ai-conversation", state["source"]])
        path = create_note(start, state)
        state.update(output_path=str(path), start_time=format_iso(start))
        state[cursor_key] = 0 if cursor_key == "last_line" else ""
        logger.info("セッション初期化 %s -> %s", session_id[:8], path)
    if cursor_key == "last_line":
        try:
            saved = int(state.get(cursor_key, 0))
        except (TypeError, ValueError):
            saved = 0
    else:
        saved = str(state.get(cursor_key, ""))
    observed = read_cursor(path)
    cursor = observed if observed is not None else saved
    if observed is not None and observed != saved:
        logger.warning("%sの不一致をMarkdownから復旧: state=%r, markdown=%r", cursor_key, saved, observed)
    state[cursor_key] = cursor
    save_state(session_id, state)
    return path, cursor
