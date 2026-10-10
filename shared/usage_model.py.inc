def _normalize_usage_model(model: object) -> str:
    """プロバイダ接頭辞などを除き、単価表と照合できるモデル名へ変換する。"""
    name = str(model or "").strip().lower()
    if "/" in name:
        name = name.rsplit("/", 1)[-1]
    aliases = {
        "gemini3.8-flash": "gemini-3.8-flash",
        "nemotron-3-ultra-550b-a55b:free": "nemotron-3-ultra-free",
        "claude-opus-5.5": "claude-opus-5-5",
        "claude-haiku-5.5": "claude-haiku-5-5",
        "claude-sonnet-5.5": "claude-sonnet-5-5",
        "muse-spark-1.3-contributor:free": "muse-spark-1.3-contributor-free",
        "muse-spark-1.3-contributor": "muse-spark-1.3-contributor-free",
    }
    return aliases.get(name, name)
