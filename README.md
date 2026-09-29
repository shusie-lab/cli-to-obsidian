# cli-to-obsidian

Antigravity、Codex、Claude Code、OpenCode の会話履歴を、MarkdownファイルとしてObsidian保管庫へ保存するmacOS向けhookスクリプト群です。各スクリプトは標準ライブラリだけで動作する単一ファイルです。

> [!NOTE]
> このプロジェクトは各CLI、ChatGPT、Antigravity、Claude CodeおよびObsidianの非公式ツールです。各サービスの開発元とは関係ありません。

## 主な特徴

- Codex CLIと、ChatGPTデスクトップアプリ内のCodexセッションに対応
- Antigravity CLIとAntigravityアプリに対応
- Claude Code CLIに対応
- OpenCode v2の会話完了プラグインに対応し、SQLiteの完了済みセッションを保存
- 会話本文だけを抽出し、ツール実行結果などのノイズを除いて保存
- ObsidianのYAML frontmatter、見出し、Calloutを使った読みやすい出力
- 最初のユーザー発話を短い見出しとfrontmatterの`title`へ記録
- CodexとAntigravityでは、取得できた各quota枠の最初と最後の残量・観測時刻・リセット時刻を記録
- Codexでは親セッションとサブエージェントのtranscriptのtoken使用量をモデル別に集計し、Dataview共通名の`usage`と標準API単価によるUSD概算を記録
- Claude Codeではtranscriptのtoken使用量を重複回答を除いて集計し、対応モデルでは標準API単価によるUSD概算を記録
- OpenCodeでは回答レコードのtoken使用量を集計し、取得できた回答のUSD概算を記録
- 外部パッケージ不要で、hookから直接呼び出せる単一ファイル構成

### 対応状況

| 対象 | CLI | アプリ | quota記録 |
| --- | --- | --- | --- |
| Codex | 対応 | ChatGPTデスクトップアプリ内のCodexに対応 | 対応 |
| Antigravity | 対応 | Antigravityアプリに対応 | 対応 |
| Claude Code | 対応 | — | statusLine設定時に対応 |
| OpenCode | 対応（プラグイン） | 対応 | なし |

ChatGPTの一般的な会話を保存する機能ではありません。対応するのは、ChatGPTデスクトップアプリ内で実行されるCodexセッションです。

Codexのquotaは、Stop hookに渡されるtranscript内の`token_count.rate_limits`から取得します。frontmatterの`quota`にはweekly・5hなど取得できた枠ごとに`first_observed`と`last_observed`を階層化し、残量・観測時刻・リセット時刻を保存します。「最初」は会話開始時ではなく最初に取得できた観測値です。quotaはアカウント全体の状態なので、差分を会話の消費量として扱わず、回答ごとのquotaも表示しません。quota情報を取得できない場合も、会話保存は継続します。Antigravityも同じ形式で保存します。

CodexのUsageは、親セッションと`parent_thread_id`で紐付くサブエージェントのtranscriptにある`token_usage_record.payload.usage`を各レコード一度ずつ加算して集計します。子のusageは同じ`session_id`を持つレコードだけを対象にします。`thread_token_usage`の累計値は二重計上を避けるため使用しません。frontmatterの`usage`にはtoken総計・モデル別内訳・`total_cost_usd`・モデル別`cost_usd`を保存します。USD概算は、`codex_save.py`冒頭のモデル別標準API単価を用い、通常入力（`input_tokens`からキャッシュ入力・cache writeを除いた値）、キャッシュ入力、cache write、出力を別々に計算します。推論トークンは出力の内数のため二重計上しません。272K input tokensを超えるリクエストは、標準API価格どおり入力・キャッシュ系を2倍、出力を1.5倍にします。これはCodexの契約上の請求額やquota消費額ではなく、ツール料金・割引モードなどを含まない標準API単価による概算です。係数が未登録のモデルはtokenだけを保存し、金額を換算しません。既知モデルが一部でもある場合はその分の`total_cost_usd`を保存し、`cost_incomplete: true`で総計が部分換算であることを示します。USD表示・frontmatterの金額は小数桁を丸めません。

本文中のCodex回答calloutには、親transcriptの回答と、同じ親ターンに紐付くサブエージェントのUsage（`total_tokens`とUSD概算）を表示します。子の`root_turn_id`で親ターンを特定し、そのターンの最後に表示される回答calloutへ加えます。サブエージェント分がある回答では、合計のtoken数とUSD概算に加え、括弧内にサブエージェントのモデル別token数・USD概算を表示します。換算できないモデルは`換算不可`と表示します。親ターンを特定できない子のusageや、表示できる親回答がないターンのusageはセッション累計だけに含めます。本文冒頭のUsageセクションとfrontmatterの`usage`はセッション累計です。272K input tokensを超えるusage recordが回答に含まれる場合は、回答のUSD表示に`⚠︎ 272K+`を付けます。

Claude CodeのUsageはtranscriptの`message.usage`をmessage IDごとに最後の値だけ集計します。通常入力、キャッシュ読み込み、キャッシュ書き込み、出力を合わせてtoken総数を出します。対応モデルではAnthropicの標準API単価からUSD概算を計算します。価格に影響する条件を確定できない場合や未登録モデルはtokenのみを保存します。これはClaude Codeのサブスクリプション請求額ではありません。quotaは任意のstatusLine設定から5h・weeklyの残量を観測し、Codexと同じ`first_observed`・`last_observed`形式で保存します。statusLineがquotaを返さない場合は省略します。

OpenCodeのUsageはSQLiteのassistantレコードにある`tokens`をmessage IDごとに集計します。本文のないツール専用レコードもfrontmatterの合計に含めます。OpenCodeが正の`cost`を返した回答に限ってUSD概算を記録します。`cost: 0`は無料利用と断定せず、換算不可として扱います。OpenRouter残高APIには接続しません。

OpenCodeの連続する同一モデルの回答は、途中経過と最終回答を1つのcalloutにまとめます。calloutのUsageは本文を持つ回答レコードの合計です。frontmatterのUsageには本文のないツール専用レコードも含みます。

## 必要環境

- macOS
- Python 3.9以上（macOS標準の`/usr/bin/python3`で動作確認）
- Obsidian
- 対応するCLIまたはアプリ

## インストール元の取得

リリース版の利用を推奨します。次の例では`v3.2.0`を取得します。

```bash
git clone --branch v3.2.0 --depth 1 https://github.com/shusie1969/cli-to-obsidian.git
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
$OBSIDIAN_VAULT/AI/Conversations/opencode/
```

`OBSIDIAN_SOURCE`を設定すると、各スクリプトの保存先末尾とfrontmatterの`source`・タグを同じ名前に変更できます。MacとRaspberry Piで保存先を分ける場合は、それぞれのhookを起動する環境で異なる値を指定します。

```bash
export OBSIDIAN_SOURCE="codex-pi4"
```

指定できるのは英数字で始まり、以降は英数字・`.`・`_`・`-`からなる64文字以内の名前です。不正な値は各スクリプトの既定名へフォールバックします。既にstateに保存先が記録されている会話は従来のファイルへ追記し、新しい会話から指定した保存先を使います。AntigravityもCLI・アプリ共通の`source`・タグを記録します。

### ファイル名

保存ファイル名は次の形式です。

```text
{YYYYMMDD}_{HHMMSS}_{project}_{session_id}.md
```

プロジェクト名がない場合は `{YYYYMMDD}_{HHMMSS}_{session_id}.md` です。`session_id` はファイル名として安全な文字に変換されます。過去に保存された先頭8文字形式のファイルも自動探索され、追記が継続されます。

### Templaterテンプレート

会話記録のファイル名を変えずに、Obsidian上ではfrontmatterの`title`を使って会話を探したり表示したりするための、任意のTemplaterテンプレートを`templates/templater/`に収録しています。ObsidianのTemplaterプラグインを有効にし、設定したテンプレートフォルダへファイルをコピーしてください。たとえばテンプレートフォルダが`~/obsidian/Templates`の場合は次のようにします。

```bash
cp templates/templater/*.md ~/obsidian/Templates/
```

- **`ChatLogLink.md`**: テンプレートを挿入すると、`生成AI/ChatLog/`内の最近更新されたノート（最大10件）が候補に表示されます。選択したノートへの内部リンクを、frontmatterの`title`（なければファイル名）で表示します。Usageの`total_cost_usd`があれば、タイトルの後ろに金額も表示します。候補一覧ではフォルダ内の相対パスも確認できます。
- **`ChatLogリンク表示名更新.md`**: 開いているノート内のWikiリンクから、`生成AI/ChatLog/`へ解決されるリンクの表示名をリンク先の最新の`title`に更新します。titleがない場合はファイル名を使い、金額があれば付記します。YAML frontmatter、コードブロック、インラインコード内は処理しません。ファイル名や会話記録そのものは変更しません。

使うときは、リンクを挿入する位置または更新したいノートを開き、コマンドパレットから **Templater: Open insert template modal** を実行してテンプレートを選びます。頻繁に使う場合は、Obsidianの設定にある「ホットキー」で同じコマンドにショートカットを割り当てられます。

両テンプレートの`DEFAULT_OUTPUT_DIR`は各`*_save.py`の`DEFAULT_OUTPUT_DIR`（既定値`生成AI/ChatLog`）と同じ名前・値にしています。各保存スクリプトはこのフォルダの下にsource名のサブフォルダを作ります。`OBSIDIAN_OUTPUT_DIR`で保存先を変更した場合は、両テンプレートの`DEFAULT_OUTPUT_DIR`も同じ保存先に変更してください。

## Codex CLIとChatGPTデスクトップアプリ

Codex CLIのhooks機構を利用します。ChatGPTデスクトップアプリ内のCodexも、transcriptの形式に応じて同じスクリプトで保存できます。

```bash
mkdir -p ~/.codex/codex-obsidian/scripts
cp codex_save.py ~/.codex/codex-obsidian/scripts/codex_save.py
```

Codexのhook定義は `~/.codex/hooks.json` または `~/.codex/config.toml` に記述できます。同じ設定レイヤーで両方を使うと、Codexは両方のhookを読み込んだうえで起動時に警告します。すでに `hooks.json` を使っている場合は、そのファイルにObsidian保存hookを追加し、`config.toml`にはhook定義を重ねて書かないでください。

`~/.codex/hooks.json` の既存の `Stop` 配列に、次の要素を追加します。既存のStop hookは残してください。`Stop` キーがまだない場合は、例のように追加します。

```json
{
  "hooks": {
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "/usr/bin/python3 ~/.codex/codex-obsidian/scripts/codex_save.py",
            "timeout": 10
          }
        ]
      }
    ]
  }
}
```

すでに `hooks.json` を使っている場合は、上記全体で置き換えず、既存の `hooks.Stop` 配列の末尾へ内側のオブジェクト（`{"hooks": [...]}`）を追加してください。

`hooks.json`を使わず、`config.toml`でhookを管理する場合は、代わりに以下を追加します。

```toml
[features]
hooks = true

[[hooks.Stop]]
[[hooks.Stop.hooks]]
type = "command"
command = "/usr/bin/python3 ~/.codex/codex-obsidian/scripts/codex_save.py"
timeout = 10
```

設定後はCodexの `/hooks` でhookの登録と信頼状態を確認してください。

CodexのCLIとアプリは会話を共有できるため、transcriptの起動元にかかわらず、既定では`codex-cli/`に保存し、frontmatterの`source: codex-cli`とタグ`codex-cli`を記録します。端末別に区別する場合は共通設定の`OBSIDIAN_SOURCE`を指定してください。

## Antigravity CLIとアプリ

Antigravity CLIおよびAntigravityアプリのStop hookから会話履歴を保存します。
quotaは、公式の読み取り専用コマンド `agy --output-format json --print='/quota'`
から取得します。このコマンドは会話ターンを作らず、quotaを消費しません。`agy` が
見つからない、または quota を返さない場合でも、会話保存は継続します。

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

Claude Code CLIのStop hookから会話履歴とtoken使用量を保存します。quotaを記録する場合は、同じスクリプトをstatusLineからも呼び出します。契約のない環境ではquotaの実データを検証できていません。

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

quotaも記録する場合は、`~/.claude/settings.json`の同じトップレベルに次を追加します。既存のstatusLine設定がある場合は、そのコマンドに`/usr/bin/python3 ~/.claude/claude-obsidian/scripts/claude_save.py --capture-quota`を組み込んでください。入力はClaude Codeが渡すJSONです。statusLineには短いquota表示が出ます。

```json
{
  "statusLine": {
    "type": "command",
    "command": "/usr/bin/python3 ~/.claude/claude-obsidian/scripts/claude_save.py --capture-quota"
  }
}
```

## OpenCode

OpenCode v2は会話完了プラグインから保存スクリプトへ渡します。プラグインはOpenCodeのSQLiteデータベース（`~/.local/share/opencode/opencode.db`）に記録された`session_message`の`idle`を監視し、完了したセッションの履歴全体を保存します。グローバルプラグインとして設置すると、OpenCodeの全プロジェクトで有効になります。

```bash
mkdir -p ~/.config/opencode/scripts ~/.config/opencode/plugins
cp opencode_save.py ~/.config/opencode/scripts/opencode_save.py
cp opencode_obsidian.js ~/.config/opencode/plugins/opencode_obsidian.js
```

OpenCodeを再起動するとプラグインが読み込まれます。`OBSIDIAN_VAULT`、`OBSIDIAN_OUTPUT_DIR`、`OPENCODE_DATABASE`はOpenCode起動時の環境変数を引き継ぎます。保存先は`生成AI/ChatLog/opencode/`です。macOS標準の`/usr/bin/sqlite3`が必要です。監視カーソルは`~/.local/state/opencode-obsidian/opencode_v2_cursor.json`に保存され、初回起動時は直近10分を再走査します。

OpenCode v2で設定ファイルから明示的に読み込む場合は、`opencode.jsonc`の`plugins`へファイルURIを指定します。

```json
{
  "plugins": [
    "file:///Users/yourname/.config/opencode/plugins/opencode_obsidian.js"
  ]
}
```

OpenCodeが回答レコードに記録したtoken使用量を保存します。OpenRouter残高の取得は行いません。

保存スクリプトを別の場所へ置く場合は、プラグイン起動前に `OPENCODE_OBSIDIAN_SAVE_SCRIPT` へ絶対パスを指定してください。既定値は `~/.config/opencode/scripts/opencode_save.py` です。

## 出力例

```markdown
---
source: codex-cli
session_id: "019f6d84-c321-7750-9532-c4b40fad70df"
title: "このコードのバグを直してください。"
project: "/Users/yourname/projects/myapp"
created: "2026-07-17T09:40:55+09:00"
modified: "2026-07-17T09:40:55+09:00"
tags:
  - ai-conversation
  - codex-cli
message_count: 2
quota:
  "codex":
    "5h":
      first_observed:
        remaining_percent: 80.0
        observed_at: "2026-07-17T09:40:45+09:00"
      last_observed:
        remaining_percent: 78.0
        observed_at: "2026-07-17T09:40:55+09:00"
    "weekly":
      first_observed:
        remaining_percent: 94.0
        observed_at: "2026-07-17T09:40:45+09:00"
        reset_at_jst: "2026-07-21 10:00"
      last_observed:
        remaining_percent: 91.0
        observed_at: "2026-07-17T09:40:55+09:00"
        reset_at_jst: "2026-07-21 10:00"
usage:
  input_tokens: 120000
  cached_input_tokens: 100000
  cache_write_input_tokens: 0
  output_tokens: 8000
  reasoning_output_tokens: 3000
  total_tokens: 128000
  total_cost_usd: 0.28
  by_model:
    "gpt-5.6-sol":
      total_tokens: 128000
      cost_usd: 0.28
---

📊 **Quota**:
- **Codex**: W: 94.0% ➔ 91.0%

💰 **Usage (USD換算)**: Total: $0.28 (gpt-5.6-sol $0.28)

<!-- last_line: 11 -->

# このコードのバグを直してください。
> [!QUESTION] User
> <small>⏱ 2026-07-17 09:40:45</small>
>
> このコードのバグを直してください。

> [!NOTE] Codex
> <small>🤖 gpt-5.6-sol-medium</small>

> 問題を確認しました。原因は...
```

ChatGPTからWorkへ移行した際に生成される構造化されたユーザー入力は、先頭が
`## Referenced ChatGPT conversation:` または `# Files mentioned by the user:` で、
コードフェンス外に `## My request:` が1つあり、後続本文が空でない場合に限って表示を分けます。
依頼本文は通常のUser QUESTION calloutへ表示し、前置きの参照情報は内容を解釈せず、
折りたたみ式の「参照情報（原文）」INFO calloutへ保存します。条件が曖昧な入力は従来どおり全文を保存します。

Claude CodeのquotaはstatusLine設定があり、入力に`rate_limits`が含まれる場合だけ記録します。Antigravityでは公式の`/quota`出力が提供する範囲
（アカウントによってweekly、5hなど）を記録します。5h quotaが提供されないアカウントでは
その項目を省きます。新しい形式で更新したMarkdownでは、旧形式の数値`quota`・`quota_claude`
を除去します。過去のファイルを一括変換する処理は行いません。

## 動作確認とトラブルシューティング

各CLIまたはアプリで会話を1ターン実行し、対応する保存先にMarkdownが作成されることを確認してください。保存されない場合は、次のログを確認します。

- Codex: `~/.codex/codex-obsidian/log/codex_obsidian_save.log`
- Antigravity: `~/.gemini/agy-obsidian/log/obsidian_save.log`
- Claude Code: `~/.claude/claude-obsidian/log/claude_obsidian_save.log`
- OpenCode: `~/.local/state/opencode-obsidian/opencode_obsidian_save.log`

詳細ログが必要な場合は、hookを起動する環境で`DEBUG=1`を設定します。`OBSIDIAN_VAULT`が存在しない場合は自動作成されますが、親ディレクトリへの書き込み権限が必要です。

## 開発・テスト

外部パッケージは不要です。リポジトリのルートで次を実行します。

```bash
/usr/bin/python3 sync_shared.py --check
/usr/bin/python3 -m unittest discover -s tests -v
/usr/bin/python3 -X pycache_prefix=/tmp/cli_obsidian_save_pycache -m py_compile agy_save.py codex_save.py claude_save.py opencode_save.py
```

共通のMarkdown整形・日時・見出し・描画、増分Callout判定・抽出、3つの増分saverで共通する
frontmatter更新・state・ロック・atomic書き込み・既存Markdown探索は`shared/`を開発用の正本とし、変更後に
`/usr/bin/python3 sync_shared.py`で各`*_save.py`へ埋め込みます。生成後の各スクリプトは
共通モジュールをimportせず、従来どおり単一ファイルで動作します。生成領域を変更する場合は
各スクリプトを直接編集せず、`shared/`を変更してから同期してください。同期時は全スクリプトの
Python構文を先に検証し、1本でも不正なら書き込みません。

テストでは、通常の追記、CodexとAntigravityのquota観測値、Claude CodeとOpenCodeのtoken集計、連続発言の集約に加えて、cursor復旧、state消失時の既存Markdown再利用、書き込み途中のJSONL最終行の再試行、YAML文字列とstateファイル名の安全化、保存先相対パスの検証を確認しています。

## 実装上の特徴

- **ノイズの少ないログ保存**: ユーザーおよびエージェントの発言だけを抽出し、ツール呼び出しや実行結果を除外します。
- **増分管理**: CodexとClaude Codeは`last_line`、Antigravityは`last_id`で読み込み済み位置を管理します。
- **cursorとstateの自動復旧**: Markdown内のcursorを正としてstateとの不一致を補正し、state消失時はfrontmatterの`session_id`から既存Markdownを探索します。
- **Atomic書き込み**: Markdownとstateを同一ディレクトリの一時ファイルへ書いてからrenameします。
- **同時実行への対応**: セッション単位のファイルロックでhookの重複実行を防ぎます。
- **安全なメタデータとファイル名**: YAML文字列、session ID、プロジェクト名を安全な形式に変換します。
- **古いstateのクリーンアップ**: 30日間利用されていないstateファイルを自動的に削除します（非ブロッキングロックで実行中セッションを保護）。

## 謝辞

Markdownの出力形式では、[Obsidian AI Exporter](https://github.com/sho7650/obsidian-AI-exporter)のYAML frontmatter、ユーザー発言の見出し、Calloutを使った会話表現を参考にしました。優れたアイデアを公開されている開発者に感謝します。

## バージョン管理

リリースはリポジトリ全体で共通のバージョン番号を使用し、[Semantic Versioning](https://semver.org/lang/ja/)に従います。各スクリプトの`__version__`、ルートの[VERSION](./VERSION)、Gitタグは同じ値です。

## ライセンス

このプロジェクトは[MIT License](./LICENSE)の下で公開されています。
