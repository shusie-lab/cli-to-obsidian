def atomic_write(path: Path, content: str) -> None:
    """既存snapshot saver APIを共通atomic writeへ接続する。"""
    atomic_write_text(path, content)


def read_payload(path: str | None) -> dict:
    if path:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    return json.load(sys.stdin)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", help="会話snapshotのJSONファイル。省略時はstdin")
    args = parser.parse_args()
    handle_payload(read_payload(args.input))
