from contextvars import ContextVar

PRICE_VERSION = "4.2.0"
PRICE_RECORDED_AT = "2026-10-27"
PRICE_CHECKED_AT = {
    "gpt-6.1-terra": "2026-10-27",
    "claude-fable-5-1": "2026-10-25", "claude-sonnet-5-5": "2026-10-25",
    "claude-opus-5-5": "2026-10-25", "claude-haiku-5-5": "2026-10-25",
    "gemini-3.8-flash": "2026-10-06", "deepseek-v4.1-flash": "2026-10-16",
}
PRICE_SOURCES = {
    "claude": "https://platform.claude.com/docs/en/about-claude/pricing",
    "gpt": "https://developers.openai.com/api/docs/pricing",
    "gemini": "https://ai.google.dev/gemini-api/docs/pricing",
    "deepseek": "https://api-docs.deepseek.com/quick_start/pricing/",
    "muse": "https://dev.meta.ai/docs/pricing-rate-limits/",
    "nemotron": "https://openrouter.ai/nvidia/nemotron-3-ultra-550b-a55b",
}
# 過去表を現在表への差分として生成せず、独立した値で保持する。
PRICE_HISTORY = {
    "3.12.0": {"recorded_at": "2026-10-25", "rates": {
        'claude-fable-5-1': {'input': Decimal('10.00'), 'cached_input': Decimal('0.25'), 'cache_write_input': Decimal('12.50'), 'output': Decimal('50.00'), 'cache_write_input_1h': Decimal('20.00')},
        'deepseek-v4.1-flash': {'input': Decimal('0.30'), 'cached_input': Decimal('0.006'), 'cache_write_input': Decimal('0.30'), 'output': Decimal('1.20')},
        'gpt-6-astra': {'input': Decimal('10.00'), 'cached_input': Decimal('1.00'), 'cache_write_input': Decimal('12.50'), 'output': Decimal('50.00')},
        'gpt-6.1-sol': {'input': Decimal('2.00'), 'cached_input': Decimal('0.10'), 'cache_write_input': Decimal('2.50'), 'output': Decimal('10.00')},
        'gpt-6-luna': {'input': Decimal('0.10'), 'cached_input': Decimal('0.01'), 'cache_write_input': Decimal('0.125'), 'output': Decimal('0.50')},
        'gemini-3.8-flash': {'input': Decimal('0.75'), 'cached_input': Decimal('0.075'), 'cache_write_input': Decimal('0.0416666666666667'), 'output': Decimal('3.75')},
        'muse-spark-1.3-contributor-free': {'input': Decimal('0.10'), 'cached_input': Decimal('0.002'), 'cache_write_input': Decimal('0.10'), 'output': Decimal('0.20')},
        'nemotron-3-ultra-free': {'input': Decimal('0.50'), 'cached_input': Decimal('0.10'), 'cache_write_input': Decimal('0.50'), 'output': Decimal('2.20')},
        'claude-opus-5-5': {'input': Decimal('4.00'), 'cached_input': Decimal('0.20'), 'cache_write_input': Decimal('5.00'), 'output': Decimal('20.00'), 'cache_write_input_1h': Decimal('8.00')},
        'claude-haiku-5-5': {'input': Decimal('0.10'), 'cached_input': Decimal('0.01'), 'cache_write_input': Decimal('0.125'), 'output': Decimal('0.50'), 'cache_write_input_1h': Decimal('0.20')},
        'claude-sonnet-5-5': {'input': Decimal('2.00'), 'cached_input': Decimal('0.10'), 'cache_write_input': Decimal('2.50'), 'output': Decimal('10.00'), 'cache_write_input_1h': Decimal('4.00')},
    }},
    "3.11.1": {"recorded_at": "2026-10-21", "rates": {
        'deepseek-v4.1-flash': {'input': Decimal('0.30'), 'cached_input': Decimal('0.006'), 'cache_write_input': Decimal('0.30'), 'output': Decimal('1.20')},
        'gpt-6-astra': {'input': Decimal('10.00'), 'cached_input': Decimal('1.00'), 'cache_write_input': Decimal('12.50'), 'output': Decimal('50.00')},
        'gpt-6.1-sol': {'input': Decimal('2.00'), 'cached_input': Decimal('0.10'), 'cache_write_input': Decimal('2.50'), 'output': Decimal('10.00')},
        'gpt-6-luna': {'input': Decimal('0.10'), 'cached_input': Decimal('0.01'), 'cache_write_input': Decimal('0.125'), 'output': Decimal('0.50')},
        'gemini-3.8-flash': {'input': Decimal('0.75'), 'cached_input': Decimal('0.075'), 'cache_write_input': Decimal('0.0416666666666667'), 'output': Decimal('3.75')},
        'muse-spark-1.3-contributor-free': {'input': Decimal('0.10'), 'cached_input': Decimal('0.002'), 'cache_write_input': Decimal('0.10'), 'output': Decimal('0.20')},
        'nemotron-3-ultra-free': {'input': Decimal('0.50'), 'cached_input': Decimal('0.10'), 'cache_write_input': Decimal('0.50'), 'output': Decimal('2.20')},
        'claude-opus-5-5': {'input': Decimal('4.00'), 'cached_input': Decimal('0.20'), 'cache_write_input': Decimal('5.00'), 'output': Decimal('20.00'), 'cache_write_input_1h': Decimal('8.00')},
        'claude-haiku-5-5': {'input': Decimal('0.10'), 'cached_input': Decimal('0.01'), 'cache_write_input': Decimal('0.125'), 'output': Decimal('0.50'), 'cache_write_input_1h': Decimal('0.20')},
        'claude-sonnet-5-5': {'input': Decimal('2.00'), 'cached_input': Decimal('0.20'), 'cache_write_input': Decimal('2.50'), 'output': Decimal('10.00'), 'cache_write_input_1h': Decimal('4.00')},
    }},
}
_PRICE_CONTEXT = ContextVar("obsidian_usage_price_version", default=PRICE_VERSION)


def active_price_version() -> str:
    return _PRICE_CONTEXT.get()


def active_usage_rates() -> dict:
    version = active_price_version()
    return STANDARD_USAGE_RATES if version == PRICE_VERSION else PRICE_HISTORY[version]["rates"]


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


def usage_price_fields() -> list[str]:
    version = active_price_version()
    recorded = PRICE_RECORDED_AT if version == PRICE_VERSION else PRICE_HISTORY[version]["recorded_at"]
    # 過去料金の正確な適用開始日は確認できないため推定しない。
    return ["  price_version: " + yaml_quote(version),
            "  price_recorded_at: " + yaml_quote(recorded),
            "  price_effective_from: null", "  price_checked_at:",
            *("    " + yaml_quote(model) + ": " + (yaml_quote(PRICE_CHECKED_AT[model])
              if version == PRICE_VERSION and model in PRICE_CHECKED_AT else "null")
              for model in active_usage_rates()), "  price_sources:",
            *("    " + yaml_quote(key) + ": " + yaml_quote(value) for key, value in PRICE_SOURCES.items())]
