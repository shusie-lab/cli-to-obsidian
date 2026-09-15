#!/usr/bin/python3
"""共通ソースを、単独実行可能な保存スクリプトへ埋め込む。"""

import argparse
import ast
import os
from pathlib import Path
import re
import tempfile


ROOT = Path(__file__).resolve().parent
BLOCK_TARGETS = {
    "output-directory": ("agy_save.py", "claude_save.py", "codex_save.py", "opencode_save.py"),
    "session-storage": ("agy_save.py", "claude_save.py", "codex_save.py"),
    "incremental-utilities": ("agy_save.py", "claude_save.py", "codex_save.py"),
    "markdown-formatting": ("agy_save.py", "claude_save.py", "codex_save.py", "opencode_save.py"),
    "message-metadata": ("agy_save.py", "claude_save.py", "codex_save.py", "opencode_save.py"),
    "markdown-rendering": ("agy_save.py", "claude_save.py", "codex_save.py", "opencode_save.py"),
    "incremental-callout": ("agy_save.py", "claude_save.py", "codex_save.py"),
    "callout-header": ("agy_save.py", "claude_save.py", "codex_save.py"),
    "find-existing-markdown": ("agy_save.py", "claude_save.py", "codex_save.py"),
    "frontmatter-update": ("agy_save.py", "claude_save.py", "codex_save.py"),
    "atomic-markdown-write": ("agy_save.py", "claude_save.py", "codex_save.py"),
    "line-cursor": ("claude_save.py", "codex_save.py"),
    "incremental-main": ("agy_save.py", "claude_save.py", "codex_save.py"),
}


def source_block(name: str, root: Path = ROOT) -> str:
    path = root / "shared" / f"{name.replace('-', '_')}.py.inc"
    return path.read_text(encoding="utf-8").rstrip("\n")


def replace_block(content: str, name: str, replacement: str) -> str:
    start = f"# BEGIN GENERATED: {name}"
    end = f"# END GENERATED: {name}"
    pattern = re.compile(rf"(?ms)^{re.escape(start)}\n.*?^{re.escape(end)}$")
    generated = f"{start}\n{replacement}\n{end}"
    updated, count = pattern.subn(lambda _match: generated, content)
    if count != 1:
        raise ValueError(f"生成領域{name}が1つではありません: {count}")
    return updated


def expected_script(path: Path, root: Path = ROOT) -> str:
    content = path.read_text(encoding="utf-8")
    for name, targets in BLOCK_TARGETS.items():
        if path.name in targets:
            content = replace_block(content, name, source_block(name, root))
    return content


def expected_scripts(root: Path = ROOT) -> dict[Path, str]:
    """全対象を生成し、どれかが不正なら書き込み前に失敗する。"""
    targets = sorted({target for values in BLOCK_TARGETS.values() for target in values})
    generated = {root / target: expected_script(root / target, root) for target in targets}
    for path, content in generated.items():
        ast.parse(content, filename=str(path))
    return generated


def check(root: Path = ROOT) -> list[str]:
    return [
        path.name
        for path, expected in expected_scripts(root).items()
        if path.read_text(encoding="utf-8") != expected
    ]


def write(root: Path = ROOT) -> None:
    generated = expected_scripts(root)
    for path, expected in generated.items():
        if path.read_text(encoding="utf-8") == expected:
            continue
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=str(path.parent),
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as stream:
                stream.write(expected)
                stream.flush()
                os.fsync(stream.fileno())
                temporary = Path(stream.name)
            os.chmod(temporary, path.stat().st_mode & 0o7777)
            os.replace(temporary, path)
            temporary = None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="共通コードを単一ファイルの保存スクリプトへ同期します")
    parser.add_argument("--check", action="store_true", help="生成結果が最新かだけを確認する")
    args = parser.parse_args()
    if args.check:
        stale = check()
        if stale:
            parser.error("共通コードと一致しない保存スクリプト: " + ", ".join(stale))
        print("shared code: OK")
        return 0
    write()
    print("共通コードを保存スクリプトへ同期しました")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
