def recover_snapshot_metadata(path: Path | None, state: dict, created: object) -> dict:
    """既存frontmatterを優先し、state消失や環境設定変更でもノートの識別を保持する。"""
    metadata = read_markdown_metadata(path) if path is not None and path.exists() else {}
    source = metadata.get("source") or state.get("source") or SOURCE_NAME
    return {
        "created": parse_datetime(metadata.get("created") or state.get("created") or created),
        "source": source,
        "tags": metadata.get("tags", state.get("tags", ["ai-conversation", source])),
    }
