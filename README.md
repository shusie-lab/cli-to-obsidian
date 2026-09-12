# cli-to-obsidian

Antigravity、Codex、Claude Code、OpenCode の会話履歴を、MarkdownファイルとしてObsidian保管庫へ保存するmacOS向けhookスクリプト群です。各スクリプトは標準ライブラリだけで動作する単一ファイルです。

> [!NOTE]
> このプロジェクトは各CLI、ChatGPT、Antigravity、Claude CodeおよびObsidianの非公式ツールです。各サービスの開発元とは関係ありません。

## 主な特徴

- Codex CLIと、ChatGPTデスクトップアプリ内のCodexセッションに対応
- Antigravity CLIとAntigravityアプリに対応
- Claude Code CLIに対応
- OpenCode の会話完了プラグイン（`chat.message`、`experimental.text.complete`、`session.idle`）に対応
- 会話本文だけを抽出し、ツール実行結果などのノイズを除いて保存
- ObsidianのYAML frontmatter、見出し、Calloutを使った読みやすい出力
- Codexでは、会話ごとのquota変化とセッション全体のweekly quota消費量を記録
- 外部パッケージ不要で、hookから直接呼び出せる単一ファイル構成

### 対応状況

| 対象 | CLI | アプリ | quota記録 |
| --- | --- | --- | --- |
| Codex | 対応 | ChatGPTデスクトップアプリ内のCodexに対応 | 対応 |
| Antigravity | 対応 | Antigravityアプリに対応 | なし |
| Claude Code | 対応 | — | なし |
| OpenCode | 対応（プラグイン） | 対応 | なし |

ChatGPTの一般的な会話を保存する機能ではありません。対応するのは、ChatGPTデスクトップアプリ内で実行されるCodexセッションです。

Codexのquotaは、Stop hookに渡されるtranscript内の`token_count.rate_limits`から取得します。各回答のメタデータに直前の記録からの変化を表示し、frontmatterにはセッション開始時と終了時のweekly quota残量の差を消費量として記録します。quota情報を取得できない場合も、会話保存は継続します。

## 必要環境

- macOS
- Python 3.9以上（macOS標準の`/usr/bin/python3`で動作確認）
- Obsidian
- 対応するCLIまたはアプリ

## インストール元の取得

リリース版の利用を推奨します。次の例では`v1.1.0`を取得します。

```bash
git clone --branch v1.1.0 --depth 1 https://github.com/shusie1969/cli-to-obsidian.git
cd cli-to-obsidian
```

## 共通設定

### Obsidian保管庫と保存先

`OBSIDIAN_VAULT`でObsidian保管庫のルートを指定できます。省略時は`~/obsidian`です。

`OBSIDIAN_OUTPUT_DIR`で保管庫内の保存先を指定できます。省略時は`生成AI/ChatLog`です。値は保管庫からの相対パスに限定され、絶対パスや`..`を含むパスは安全のため既定値へフォールバックします。

```bash
export OBSIDIAN_VAULT="$HOME/path/to/your/vault"
export OBSIDIAN_OUTPUT_DIR="AI/Conversations"
```

各スクリプトは保存先の末尾に対象名を付けます。

```text
$OBSIDIAN_VAULT/AI/Conversations/codex-cli/
$OBSIDIAN_VAULT/AI/Conversations/antigravity-cli/
$OBSIDIAN_VAULT/AI/Conversations/claude-code/
```

### ファイル名

保存ファイル名は次の形式です。

```text
{YYYYMMDD}_{HHMMSS}_{project}_{session_id先頭8文字}.md
```

## Codex CLIとChatGPTデスクトップアプリ

Codex CLIのhooks機構を利用します。ChatGPTデスクトップアプリ内のCodexも、transcriptの形式に応じて同じスクリプトで保存できます。

```bash
mkdir -p ~/.codex/codex-obsidian/scripts
cp codex_save.py ~/.codex/codex-obsidian/scripts/codex_save.py
```

`~/.codex/config.toml`に以下を追加します。

```toml
[features]
hooks = true

[[hooks.Stop]]
[[hooks.Stop.hooks]]
type = "command"
command = "/usr/bin/python3 ~/.codex/codex-obsidian/scripts/codex_save.py"
timeout = 10
```

Codexのtranscriptに`session_meta.source`が`cli`なら`source: codex-cli`、`vscode`なら`source: codex-app`として記録します。

## Antigravity CLIとアプリ

Antigravity CLIおよびAntigravityアプリのStop hookから会話履歴を保存します。quota取得は公開版には含めません。

```bash
mkdir -p ~/.gemini/agy-obsidian/scripts
cp agy_save.py ~/.gemini/agy-obsidian/scripts/agy_save.py
```

`~/.gemini/config/hooks.json`に以下を追加します。

```json
{
  "obsidian-save": {
    "Stop": [
      {
        "type": "command",
        "command": "/usr/bin/python3 ~/.gemini/agy-obsidian/scripts/agy_save.py"
      }
    ]
  }
}
```

## Claude Code

Claude Code CLIのStop hookから会話履歴を保存します。Claude Codeのquota取得は実装していません。

```bash
mkdir -p ~/.claude/claude-obsidian/scripts
cp claude_save.py ~/.claude/claude-obsidian/scripts/claude_save.py
```

`~/.claude/settings.json`に以下を追加します。

```json
{
  "hooks": {
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "/usr/bin/python3 ~/.claude/claude-obsidian/scripts/claude_save.py"
          }
        ]
      }
    ]
  }
}
```

## OpenCode

OpenCode は会話完了プラグインから保存スクリプトへ渡します。`chat.message` と `experimental.text.complete` で単発実行の応答も保存し、`session.idle` で SDK から全履歴を再同期します。グローバルプラグインとして設置すると、OpenCode の全プロジェクトで有効になります。

```bash
mkdir -p ~/.config/opencode/scripts ~/.config/opencode/plugins
cp opencode_save.py ~/.config/opencode/scripts/opencode_save.py
cp opencode_obsidian.js ~/.config/opencode/plugins/opencode_obsidian.js
```

OpenCode を再起動するとプラグインが読み込まれます。`OBSIDIAN_VAULT` と `OBSIDIAN_OUTPUT_DIR` は OpenCode 起動時の環境変数を引き継ぎます。保存先は `生成AI/ChatLog/opencode/` です。

保存スクリプトを別の場所へ置く場合は、プラグイン起動前に `OPENCODE_OBSIDIAN_SAVE_SCRIPT` へ絶対パスを指定してください。既定値は `~/.config/opencode/scripts/opencode_save.py` です。

## 出力例

```markdown
---
source: codex-cli
session_id: "019f6d84-c321-7750-9532-c4b40fad70df"
project: "/Users/yourname/projects/myapp"
created: "2026-07-17T09:40:55+09:00"
modified: "2026-07-17T09:40:55+09:00"
tags:
  - ai-conversation
  - codex-cli
message_count: 2
quota: 3.00
---

📊 **Quota**:
- **Codex**: W: 94.0% ➔ 91.0%

<!-- last_line: 11 -->

# User: このコードのバグを直してください。
> [!QUESTION] User
> <small>⏱ 2026-07-17 09:40:45</small>
>
> このコードのバグを直してください。

> [!NOTE] Codex
> <small>🤖 gpt-5.6-sol-medium (Quota: W 91.0(-3.00)%)</small>

> 問題を確認しました。原因は...
```

ChatGPTからWorkへ移行した際に生成される構造化されたユーザー入力は、先頭が
`## Referenced ChatGPT conversation:` または `# Files mentioned by the user:` で、
コードフェンス外に `## My request:` が1つあり、後続本文が空でない場合に限って表示を分けます。
依頼本文は通常のUser QUESTION calloutへ表示し、前置きの参照情報は内容を解釈せず、
折りたたみ式の「参照情報（原文）」INFO calloutへ保存します。条件が曖昧な入力は従来どおり全文を保存します。

AntigravityとClaude Codeの新規出力にはquota情報は含まれません。過去にquota対応版で保存したMarkdownのquota記録は、履歴情報として削除されません。

## 動作確認とトラブルシューティング

各CLIまたはアプリで会話を1ターン実行し、対応する保存先にMarkdownが作成されることを確認してください。保存されない場合は、次のログを確認します。

- Codex: `~/.codex/codex-obsidian/log/codex_obsidian_save.log`
- Antigravity: `~/.gemini/agy-obsidian/log/obsidian_save.log`
- Claude Code: `~/.claude/claude-obsidian/log/claude_obsidian_save.log`

詳細ログが必要な場合は、hookを起動する環境で`DEBUG=1`を設定します。`OBSIDIAN_VAULT`が存在しない場合は自動作成されますが、親ディレクトリへの書き込み権限が必要です。

## 開発・テスト

外部パッケージは不要です。リポジトリのルートで次を実行します。

```bash
/usr/bin/python3 -m unittest discover -s tests -v
/usr/bin/python3 -X pycache_prefix=/tmp/cli_obsidian_save_pycache -m py_compile agy_save.py codex_save.py claude_save.py opencode_save.py
```

テストでは、通常の追記、Codex quotaの解析と更新、連続発言の集約に加えて、cursor復旧、state消失時の既存Markdown再利用、書き込み途中のJSONL最終行の再試行、YAML文字列とstateファイル名の安全化、保存先相対パスの検証を確認しています。

## 実装上の特徴

- **ノイズの少ないログ保存**: ユーザーおよびエージェントの発言だけを抽出し、ツール呼び出しや実行結果を除外します。
- **増分管理**: CodexとClaude Codeは`last_line`、Antigravityは`last_id`で読み込み済み位置を管理します。
- **cursorとstateの自動復旧**: Markdown内のcursorを正としてstateとの不一致を補正し、state消失時はfrontmatterの`session_id`から既存Markdownを探索します。
- **Atomic書き込み**: Markdownとstateを同一ディレクトリの一時ファイルへ書いてからrenameします。
- **同時実行への対応**: セッション単位のファイルロックでhookの重複実行を防ぎます。
- **安全なメタデータとファイル名**: YAML文字列、session ID、プロジェクト名を安全な形式に変換します。
- **古いstateのクリーンアップ**: 30日以上経過したstateファイルを自動的に削除します。

## 謝辞

Markdownの出力形式では、[Obsidian AI Exporter](https://github.com/sho7650/obsidian-AI-exporter)のYAML frontmatter、ユーザー発言の見出し、Calloutを使った会話表現を参考にしました。優れたアイデアを公開されている開発者に感謝します。

## バージョン管理

リリースはリポジトリ全体で共通のバージョン番号を使用し、[Semantic Versioning](https://semver.org/lang/ja/)に従います。各スクリプトの`__version__`、ルートの[VERSION](./VERSION)、Gitタグは同じ値です。

## ライセンス

このプロジェクトは[MIT License](./LICENSE)の下で公開されています。
