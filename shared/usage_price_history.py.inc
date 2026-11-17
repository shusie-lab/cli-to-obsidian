from contextvars import ContextVar

PRICE_VERSION = "4.4.0"
PRICE_RECORDED_AT = "2026-11-18"
PRICE_CHECKED_AT = {
    "gpt-6.2-astra": "2026-11-18",
    "gpt-6.2-sol": "2026-11-18",
    'claude-fable-5-1': "2026-10-28",
    'deepseek-v4.1-flash': "2026-10-28",
    'gpt-6-astra': "2026-10-28",
    'gpt-6.1-sol': "2026-10-28",
    'gpt-6.1-terra': "2026-10-28",
    'gpt-6-luna': "2026-10-28",
    'gemini-3.8-flash': "2026-10-28",
    'muse-spark-1.3-contributor-free': "2026-10-28",
    'nemotron-3-ultra-free': "2026-10-28",
    'claude-opus-5-5': "2026-10-28",
    'claude-haiku-5-5': "2026-10-28",
    'claude-sonnet-5-5': "2026-10-28",
}
PRICE_SOURCES = {
    "claude": "https://platform.claude.com/docs/en/about-claude/pricing",
    "gpt": "https://developers.openai.com/api/docs/pricing",
    "gemini": "https://ai.google.dev/gemini-api/docs/pricing",
    "deepseek": "https://api-docs.deepseek.com/quick_start/pricing/",
    "muse": "https://dev.meta.ai/docs/pricing-rate-limits/",
    "nemotron": "https://openrouter.ai/nvidia/nemotron-3-ultra-550b-a55b",
}
# 現在表から過去へ戻す差分。next_versionは直後の価格版を明示する。
# changesは変更したtoken単価の旧値。削除モデルは全単価を残し、追加モデルは除外しない。
PRICE_HISTORY = {
    "4.2.0": {"recorded_at": "2026-10-27", "next_version": "4.4.0", "changes": {}},
    "3.12.0": {"recorded_at": "2026-10-25", "next_version": "4.2.0", "changes": {}},
    "3.11.1": {"recorded_at": "2026-10-21", "next_version": "3.12.0", "changes": {
        "claude-sonnet-5-5": {"cached_input": Decimal("0.20")},
    }},
}
_PRICE_CONTEXT = ContextVar("obsidian_usage_price_version", default=PRICE_VERSION)


def active_price_version() -> str:
    return _PRICE_CONTEXT.get()


def _usage_rates_for_version(version: str) -> dict:
    if version == PRICE_VERSION:
        return STANDARD_USAGE_RATES
    history = PRICE_HISTORY[version]
    # コピーに差分を適用し、現在表や他の価格版を変更しない。
    rates = {model: dict(values) for model, values in
             _usage_rates_for_version(history["next_version"]).items()}
    for model, changes in history["changes"].items():
        rates.setdefault(model, {}).update(changes)
    return rates


def active_usage_rates() -> dict:
    return _usage_rates_for_version(active_price_version())


@contextmanager
def usage_price_version(version: str):
    if version != PRICE_VERSION and version not in PRICE_HISTORY:
        raise ValueError("未登録の価格版: " + str(version))
    token = _PRICE_CONTEXT.set(version)
    try:
        yield
    finally:
        _PRICE_CONTEXT.reset(token)


def note_price_version(content: str) -> str | None:
    frontmatter = re.match(r"\A---\s*\n(.*?)\n---", content, re.DOTALL)
    if not frontmatter:
        return None
    usage = re.search(r"(?m)^usage:\n((?:[ \t].*\n|\n)*)", frontmatter.group(1) + "\n")
    match = re.search(r'^  price_version: (.+)$', usage.group(1), re.M) if usage else None
    if not match:
        return None
    try:
        value = json.loads(match.group(1))
    except ValueError:
        value = match.group(1).strip()
    return value if isinstance(value, str) else None


def usage_price_fields(usage: dict) -> list[str]:
    version = active_price_version()
    recorded = PRICE_RECORDED_AT if version == PRICE_VERSION else PRICE_HISTORY[version]["recorded_at"]
    models = sorted({reference_price_model(model) for model in usage.get("by_model", {})})
    sources = {key: value for key, value in PRICE_SOURCES.items()
               if any(model.startswith(key + "-") for model in models)}
    # 過去料金の正確な適用開始日は確認できないため推定しない。
    return ["  price_version: " + yaml_quote(version),
            "  price_recorded_at: " + yaml_quote(recorded),
            "  price_effective_from: null", "  price_checked_at:" + ("" if models else " {}"),
            *("    " + yaml_quote(model) + ": " + (yaml_quote(PRICE_CHECKED_AT[model])
              if version == PRICE_VERSION and model in PRICE_CHECKED_AT else "null")
              for model in models), "  price_sources:" + ("" if sources else " {}"),
            *("    " + yaml_quote(key) + ": " + yaml_quote(value) for key, value in sources.items())]
