def read_stdin() -> dict:
    """hookの標準入力を読み、空入力や不正JSONでは空のdictを返す。"""
    try:
        data = sys.stdin.read()
        if data.strip():
            return json.loads(data)
    except Exception as error:
        logger.info("stdinの読み込みまたはJSONパースに失敗しました: %s", error)
    return {}


def now_jst() -> datetime:
    return datetime.now(JST)


def format_iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def yaml_quote(value: object) -> str:
    """JSON互換の引用形式でYAML文字列を安全に生成する。"""
    return json.dumps(str(value), ensure_ascii=False)


def safe_filename_component(value: str) -> str:
    """値を単一の安全なファイル名要素へ変換する。"""
    return re.sub(r"[\x00-\x1f/:*?\[\]\\]", "_", value).strip()[:80]


def merge_first_quota(first: dict | None, observed: dict) -> dict:
    """各枠について、最初に取得できた観測値を保持する。"""
    merged = {group: dict(windows) for group, windows in (first or {}).items()}
    for group, windows in observed.items():
        target = merged.setdefault(group, {})
        for window, value in windows.items():
            target.setdefault(window, value)
    return merged


def build_quota_frontmatter_fields(first: dict | None, last: dict | None) -> list[str]:
    """quotaは消費量に換算せず、枠ごとの最初と最後の観測値を保存する。"""
    if not first and not last:
        return []
    lines = ["quota:"]
    for group in sorted(set(first or {}) | set(last or {})):
        lines.append(f"  {yaml_quote(group)}:")
        first_windows = (first or {}).get(group, {})
        last_windows = (last or {}).get(group, {})
        for window in sorted(set(first_windows) | set(last_windows)):
            lines.append(f"    {yaml_quote(window)}:")
            for label, value in (("first_observed", first_windows.get(window)),
                                 ("last_observed", last_windows.get(window))):
                if not isinstance(value, dict) or value.get("remaining") is None:
                    continue
                lines.append(f"      {label}:")
                lines.append(f"        remaining_percent: {value['remaining']}")
                if value.get("observed_at"):
                    lines.append(f"        observed_at: {yaml_quote(value['observed_at'])}")
                if value.get("reset"):
                    lines.append(f"        reset_at_jst: {yaml_quote(value['reset'])}")
    return lines


def recover_quota_frontmatter(content: str) -> tuple[dict, dict]:
    """このスクリプトが出力した階層化quotaだけをstateへ復旧する。"""
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n", content, re.DOTALL)
    if not match:
        return {}, {}
    first, last = {}, {}
    in_quota = False
    group = window = observation = ""
    for line in match.group(1).splitlines():
        if line == "quota:":
            in_quota = True
            continue
        if not in_quota:
            continue
        if line and not line.startswith(" "):
            break
        indent = len(line) - len(line.lstrip(" "))
        key = line.strip()
        if indent == 2 and key.endswith(":"):
            try:
                group = json.loads(key[:-1])
            except (TypeError, ValueError):
                return {}, {}
        elif indent == 4 and key.endswith(":"):
            try:
                window = json.loads(key[:-1])
            except (TypeError, ValueError):
                return {}, {}
        elif indent == 6 and key in ("first_observed:", "last_observed:"):
            observation = key[:-1]
        elif indent == 8 and group and window and observation and ": " in key:
            field, raw = key.split(": ", 1)
            target = first if observation == "first_observed" else last
            item = target.setdefault(group, {}).setdefault(window, {})
            if field == "remaining_percent":
                try:
                    value = float(raw)
                except ValueError:
                    return {}, {}
                if not 0 <= value <= 100:
                    return {}, {}
                item["remaining"] = value
            elif field in ("observed_at", "reset_at_jst"):
                try:
                    value = json.loads(raw)
                except ValueError:
                    return {}, {}
                item["observed_at" if field == "observed_at" else "reset"] = value
    return first, last
