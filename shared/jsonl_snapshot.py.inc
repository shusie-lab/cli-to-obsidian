import io


def read_jsonl_snapshot(path: Path, text: str | None = None) -> dict:
    """読み取り時点のJSONLを一度だけ解析する。不完全な末尾はcursorへ含めない。"""
    text = path.read_text(encoding="utf-8") if text is None else text
    rows = []
    last_line = 0
    malformed = 0
    for number, line in enumerate(io.StringIO(text, newline=None), 1):
        try:
            data = json.loads(line)
        except ValueError:
            if line.strip() and not line.endswith(("\n", "\r")):
                break
            malformed += bool(line.strip())
        else:
            if isinstance(data, dict):
                rows.append((number, data))
            else:
                malformed += 1
        last_line = number
    return {"rows": rows, "last_line": last_line, "malformed_rows": malformed}
