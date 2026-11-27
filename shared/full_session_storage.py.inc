def atomic_write(path: Path, content: str) -> None:
    """既存snapshot saver APIを共通atomic writeへ接続する。"""
    atomic_write_text(path, content)


def merge_snapshot_frontmatter(existing: str, rendered: str) -> str:
    """既存のプロパティを行ごと引き継ぎ、保存処理が管理する項目だけを更新する。"""
    old = FRONTMATTER.match(existing)
    new = FRONTMATTER.match(rendered)
    if old is None or new is None:
        return rendered

    dynamic_keys = {"modified", "message_count", "usage", "reported_cost"}
    kept = []
    existing_keys = set()
    skip = False
    for line in old[1].splitlines():
        key = frontmatter_key(line)
        if key is not None:
            skip = key in dynamic_keys
            if not skip:
                existing_keys.add(key)
        elif line and not line.startswith((" ", "\t", "- ", "#")):
            skip = False
        if not skip or not line.strip() or line.startswith("#"):
            kept.append(line)

    for line in new[1].splitlines():
        key = frontmatter_key(line)
        if key is not None:
            skip = key in existing_keys
        if not skip:
            kept.append(line)
    return rendered[:new.start(1)] + "\n".join(kept) + "\n" + rendered[new.end(1):]


def read_payload(path: str | None) -> dict:
    if path:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    return json.load(sys.stdin)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", help="会話snapshotのJSONファイル。省略時はstdin")
    args = parser.parse_args()
    handle_payload(read_payload(args.input))
