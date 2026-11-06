# セッション全履歴の再構成

保存済みの会話ノートを、元履歴から作り直す手順です。

[rebuild_session.py](../rebuild_session.py)と、対象の保存スクリプト（`codex_save.py`・`claude_save.py`・`agy_save.py`・`opencode_save.py`のうち必要なもの）を同じディレクトリに置きます。Piは対象外です。`SESSION_ID`はノートのfrontmatterの`session_id`、未保存の会話なら元履歴のIDを使います。

**通常保存先を扱う場合は、hookと同じ保管庫・出力先・sourceを指定してください。** hookコマンド内の環境変数は、このターミナルには自動で引き継がれません。以下は既定の出力先・sourceを使う例です。

```bash
export OBSIDIAN_VAULT="$HOME/obsidian"
# hookで変更している場合だけ、その値に合わせます
# export OBSIDIAN_OUTPUT_DIR="AI/Conversations"
# export OBSIDIAN_SOURCE="codex-work"

# 1. 変更予定を確認（ノート・state・バックアップを変更しません）
/usr/bin/python3 rebuild_session.py codex SESSION_ID --obsidian --dry-run

# 2. 確認した内容で置換（既存ノートのバックアップを作成します）
/usr/bin/python3 rebuild_session.py codex SESSION_ID --obsidian
```

`--dry-run`は現在と予定内容の件数・token・金額・本文差分をJSONで表示します。実行までに元履歴が追記されれば結果も変わるため、置換直前に確認してください。

`--obsidian`は通常のノートとstateを更新します。既存stateの保存先を優先し、既存ノートのファイル名・作成日時・source・タグを引き継ぎます。**本文への手動追記は置き換わります。** 置換前のノートは同じフォルダの`.rebuild-backups/`に日時付き`.bak`で保存し、パスを表示します。

通常ノートを更新せず、指定したフォルダへ直接出力する場合は次を使います。sourceのサブフォルダは追加しません。同じ出力先の既存ノートを置き換える場合もバックアップを作ります。

```bash
/usr/bin/python3 rebuild_session.py claude SESSION_ID --output-dir /private/tmp/rebuilt
```

## 価格版を選ぶ

| 指定 | 換算方法 |
| --- | --- |
| `--pricing current`（既定） | スクリプトに収録した現在の単価表。 |
| `--pricing preserve` | 置換対象ノートの`usage.price_version`。価格版がない・未登録なら中止。 |
| `--price-version 3.11.1` | 指定した登録済みの過去表。価格版のないノートにも指定可能。 |

```bash
/usr/bin/python3 rebuild_session.py claude SESSION_ID --obsidian --dry-run --pricing preserve
/usr/bin/python3 rebuild_session.py claude SESSION_ID --obsidian --pricing preserve
/usr/bin/python3 rebuild_session.py claude SESSION_ID --output-dir /private/tmp/rebuilt --price-version 3.11.1
```

`--output-dir`で`preserve`を指定した場合、その出力先にある既存ノートを参照します。日付から価格版は推測しません。過去表で再構成しても、次の通常hookでは現在表へ戻るため、価格を固定した検証結果は別の出力先へ保存してください。Antigravityは価格版指定に対応しません。

## 元履歴の場所

| 対象 | 自動探索先 | 変更方法 |
| --- | --- | --- |
| Codex | `~/.codex/sessions/`、`~/.codex/archived_sessions/` | `CODEX_HOME`または`--transcript` |
| Claude Code | `~/.claude/projects/` | `CLAUDE_CONFIG_DIR`または`--transcript` |
| Antigravity | `~/.gemini/antigravity-cli/brain/SESSION_ID/.system_generated/logs/transcript.jsonl` | `--transcript` |
| OpenCode v2 | `~/.local/share/opencode/opencode.db`（読取専用） | `OPENCODE_DATABASE`または`--database` |

```bash
/usr/bin/python3 rebuild_session.py codex SESSION_ID --transcript /path/to/rollout.jsonl --obsidian --dry-run
/usr/bin/python3 rebuild_session.py opencode SESSION_ID --database /path/to/opencode.db --obsidian --dry-run
```

Codex・Claudeはログ内のセッションIDの一致を確認します。履歴がない・本文がない場合は既存ノートを置換しません。元履歴に残る会話のみが対象です。Claude・Antigravityの過去quotaとClaudeの取得コストは既存記録を引き継ぎ、Antigravityの現在quotaは取得しません。Codexのquotaは元履歴から再集計します。終了コードは成功0、再構成失敗1、引数エラー2です。
