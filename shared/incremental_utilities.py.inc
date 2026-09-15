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
