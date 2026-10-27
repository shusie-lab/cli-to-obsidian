# cli-to-obsidian

**AIと何をして、どれだけ使ったか。会話と使用量を、Obsidianに自動で残します。**

バグ修正、調査、文章作成。AIと進めた仕事は、会話の内容だけでなく、どこにAIの使用量を多く使ったかも振り返りたいものです。

cli-to-obsidianは、対応するAIツールの会話をMarkdownとしてObsidian保管庫へ自動保存します。使用量を取得できるツールでは、token数・モデル別の内訳・USD参考換算も一緒に記録します。いつものツールで会話するだけで、作業の記録が蓄積されます。

- **判断の経緯を読み返す**：依頼と回答を読みやすいノートに保存。ツール実行結果を除いた会話本文から、前回の作業を振り返れます。
- **AI使用量の比重を知る**：会話ごとの使用量やモデル別内訳を見て、どんな仕事に多く使ったかを確認できます。
- **日報や振り返りにつなげる**：会話ノートを日報やプロジェクトノートからリンクして、成果とその過程を一緒に残せます。

**Git・pip・仮想環境は不要です。使うツールに応じて1〜2ファイルを配置し、保存先とhookを設定すれば始められます。**

## 保存すると、どう見える？

会話ノートには、最初の依頼をもとにしたタイトル、使用量のサマリ、ユーザーとAIの発言が並びます。使用量を記録できるツールでは、対応する回答にもtoken数やUSD参考換算が付きます。

> **このコードのバグを直してください。**
>
> 💰 **Usage (USD換算)**: Total: $0.13
>
> **User** — このコードのバグを直してください。
>
> **Codex** — 問題を確認しました。原因は…

上は表示を簡略化した例です。使用量の詳細はノートのプロパティ（YAML frontmatter）にも保存するため、ObsidianのDataviewで会話別・モデル別に集計できます。

USD参考換算は、実際のサブスクリプション請求額やquota消費量を示すものではありません。また、token数や参考金額の比重は作業時間の比重とは異なります。

導入するツールは下の対応表から選べます。導入済みの場合は、[保存確認](#動作確認とトラブルシューティング)、[記録内容](#usage-spec)、[全履歴の再構成](#セッション全履歴の再構成)、[telemetry照合](#telemetry)へ進んでください。

## 対応ツール

**Codex対応を中心に開発しています。** 開発者自身がメインで利用し、日々の開発・動作確認の中心にしています。

OpenCodeは補助として利用し、Piは使い始めた段階です。両ツールは今後対応を充実させていく予定です。Claude Codeは実利用の経験が少なく、Antigravityは以前メインで使っていましたが現在はほぼ利用していないため、どちらも参考実装として公開しています。

| ツール・導入手順 | 位置づけ | 利用できる環境 | token数・USD参考換算 | quota残量の記録 | 配置するファイル |
| --- | --- | --- | --- | --- | --- |
| [Codex](#codex-cliとchatgptデスクトップアプリ) | **主な開発・検証対象** | CLI・ChatGPTデスクトップアプリ内のCodex | 対応 | 対応 | 1個 |
| [OpenCode](#opencode) | 今後拡充 | CLI・アプリ（v2プラグイン） | 対応 | なし | 2個 |
| [Pi](#pi) | 今後拡充 | CLI（extension） | 対応 | なし | 2個 |
| [Claude Code](#claude-code) | 参考実装 | CLI | 対応 | statusLine設定時に対応 | 1個 |
| [Antigravity](#antigravity-cliとアプリ) | 参考実装 | CLI・Antigravityアプリ | なし | 対応 | 1個 |

USD参考換算は登録済みモデルが対象です。単価が未登録の場合も、会話と取得できたtoken数は保存します。

ChatGPTの一般的な会話は対象外です。対応するのは、ChatGPTデスクトップアプリ内で実行されるCodexセッションです。

## はじめる

必要なのはmacOS、Python 3.9以上、Obsidian、対応するCLIまたはアプリです。macOS標準の`/usr/bin/python3`で動作確認しており、追加のPythonパッケージは不要です。

1. **使うツールのファイルだけダウンロード**し、下の手順で配置します。
2. **Obsidian保管庫を指定して有効化**します。Codex・Claude Code・Antigravityはhook設定を追加し、OpenCode・Piはプラグイン／extensionを読み込みます。
3. **1ターン会話して確認**します。既定では保管庫内の`生成AI/ChatLog/<ツール名>/`にMarkdownが作成されます。

以下のファイルリンクを開き、GitHubのファイル画面右上にある **Download raw file** からダウンロードしてください。配置コマンドは`~/Downloads`に保存した場合の例です。別の場所へ保存した場合はコピー元を変更します。

各設定例の`$HOME/obsidian`は、実際のObsidian保管庫のパスへ置き換えてください。hookを使う3ツールはコマンド内に保存先を指定するため、デスクトップアプリでもシェルの環境変数設定に依存しません。

既存の設定ファイルは上書きせず、必要な項目だけ追加してください。利用したいツールの設定だけで構いません。

以降のコマンドはmacOS向けです。補助ツールも、ダウンロードしたファイルを置いたディレクトリで実行します。

## Codex CLIとChatGPTデスクトップアプリ

Codex CLIのhooks機構を利用します。ChatGPTデスクトップアプリ内のCodexも、transcriptの形式に応じて同じスクリプトで保存できます。

[codex_save.py](./codex_save.py)をダウンロードし、配置します。

```bash
mkdir -p ~/.codex/codex-obsidian/scripts
cp "$HOME/Downloads/codex_save.py" ~/.codex/codex-obsidian/scripts/codex_save.py
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
            "command": "OBSIDIAN_VAULT=\"$HOME/obsidian\" /usr/bin/python3 ~/.codex/codex-obsidian/scripts/codex_save.py",
            "timeout": 10
          }
        ]
      }
    ]
  }
}
```

`hooks.json`を使わず、`config.toml`でhookを管理する場合は、代わりに以下を追加します。

```toml
[[hooks.Stop]]
[[hooks.Stop.hooks]]
type = "command"
command = 'OBSIDIAN_VAULT="$HOME/obsidian" /usr/bin/python3 ~/.codex/codex-obsidian/scripts/codex_save.py'
timeout = 10
```

設定後はCodexを再起動し、`/hooks`で保存hookを確認・信頼してください。新規または変更したhookは、信頼するまで実行されません。`[features]`で`hooks = false`にしている場合は`true`へ変更します。設定形式と信頼の扱いは[公式Hooks仕様](https://learn.chatgpt.com/docs/hooks)に基づきます。

CodexのCLIとアプリは会話を共有できるため、transcriptの起動元にかかわらず、既定では`codex-cli/`に保存し、frontmatterの`source: codex-cli`とタグ`codex-cli`を記録します。端末別に区別する場合は[共通設定](#共通設定)の`OBSIDIAN_SOURCE`を指定してください。

## OpenCode

OpenCode v2は会話完了プラグインから保存スクリプトへ渡します。プラグインはOpenCodeのSQLiteデータベース（`~/.local/share/opencode/opencode.db`）に記録された`session_message`の`idle`を監視し、完了したセッションの履歴全体を保存します。グローバルプラグインとして設置すると、OpenCodeの全プロジェクトで有効になります。

[opencode_save.py](./opencode_save.py)と[opencode_obsidian.js](./opencode_obsidian.js)をダウンロードし、配置します。

```bash
mkdir -p ~/.config/opencode/scripts ~/.config/opencode/plugins
cp "$HOME/Downloads/opencode_save.py" ~/.config/opencode/scripts/opencode_save.py
cp "$HOME/Downloads/opencode_obsidian.js" ~/.config/opencode/plugins/opencode_obsidian.js
```

保管庫を指定して、同じターミナルからOpenCodeを起動します。

```bash
export OBSIDIAN_VAULT="$HOME/obsidian"
opencode
```

アプリで利用する場合も、OpenCodeの起動環境に`OBSIDIAN_VAULT`を設定してください。省略した場合は`~/obsidian`を使います。

OpenCodeを再起動するとプラグインが読み込まれます。`OBSIDIAN_VAULT`、`OBSIDIAN_OUTPUT_DIR`、`OPENCODE_DATABASE`はOpenCode起動時の環境変数を引き継ぎます。保存先は`生成AI/ChatLog/opencode/`です。macOS標準の`/usr/bin/sqlite3`が必要です。監視カーソルは`~/.local/state/opencode-obsidian/opencode_v2_cursor.json`に保存され、初回起動時は直近10分を再走査します。

上の`~/.config/opencode/plugins/`への配置では設定ファイルへの追記は不要です。単一の`.js`ファイルを`opencode.jsonc`の`plugins`へ直接指定すると読み飛ばされるため、自動読み込みの配置を使ってください。[OpenCode v2公式プラグイン仕様](https://opencode.ai/v2/docs/plugins/)

OpenCodeが回答レコードに記録したtoken使用量を保存します。OpenRouter残高の取得は行いません。

保存スクリプトを別の場所へ置く場合は、プラグイン起動前に `OPENCODE_OBSIDIAN_SAVE_SCRIPT` へ絶対パスを指定してください。既定値は `~/.config/opencode/scripts/opencode_save.py` です。

## Pi

Pi 1.0以降では、TypeScript extensionからmacOS標準Pythonの単独スクリプトへ保存を依頼します。グローバルextensionとして設置すると全プロジェクトで有効になります。既存のextensionは残してください。

[pi_save.py](./pi_save.py)と[pi_obsidian.ts](./pi_obsidian.ts)をダウンロードし、配置します。

```bash
mkdir -p ~/.pi/agent/scripts ~/.pi/agent/extensions
cp "$HOME/Downloads/pi_save.py" ~/.pi/agent/scripts/pi_save.py
cp "$HOME/Downloads/pi_obsidian.ts" ~/.pi/agent/extensions/pi_obsidian.ts
```

保管庫を指定して、同じターミナルからPiを起動します。

```bash
export OBSIDIAN_VAULT="$HOME/obsidian"
pi
```

Piを再起動するか`/reload`を実行します。既定の保存先は`生成AI/ChatLog/pi/`です。`OBSIDIAN_VAULT`、`OBSIDIAN_OUTPUT_DIR`、`OBSIDIAN_SOURCE`はPi起動時の環境を引き継ぎます。saverのパスは`PI_OBSIDIAN_SCRIPT`、state・ログのディレクトリは`PI_OBSIDIAN_STATE_DIR`で変更できます。

自動リトライ・圧縮・キュー処理まで終了した`agent_settled`、分岐切替、セッション終了時に保存します。本文は現在の分岐、Usageのセッション合計は全分岐が対象です。分岐切替時は同じノートを再構成するため、本文への手動追記は次回保存で置き換わります。

2ファイルを置いたディレクトリから、一時保存先で試す例です。指定モデルを利用できる認証設定が必要で、1ターンの利用が発生します。グローバルextensionとして設置している場合は、同じextensionを重ねて読み込まないでください。

```bash
OBSIDIAN_VAULT=/private/tmp/pi-obsidian-test/vault \
PI_OBSIDIAN_STATE_DIR=/private/tmp/pi-obsidian-test/state \
PI_OBSIDIAN_SCRIPT="$PWD/pi_save.py" \
pi --extension "$PWD/pi_obsidian.ts" --provider openai --model gpt-6-luna -p "OKとだけ回答してください。"
```

## Claude Code

Claude Code CLIのStop hookから会話履歴とtoken使用量を保存します。quotaを記録する場合は、同じスクリプトをstatusLineからも呼び出します。契約のない環境ではquotaの実データを検証できていません。

[claude_save.py](./claude_save.py)をダウンロードし、配置します。

```bash
mkdir -p ~/.claude/claude-obsidian/scripts
cp "$HOME/Downloads/claude_save.py" ~/.claude/claude-obsidian/scripts/claude_save.py
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
            "command": "OBSIDIAN_VAULT=\"$HOME/obsidian\" /usr/bin/python3 ~/.claude/claude-obsidian/scripts/claude_save.py"
          }
        ]
      }
    ]
  }
}
```

子Usageを完了直後に反映する場合は`SubagentStop`、APIエラー時も途中の会話を保存する場合は`StopFailure`へ、同じコマンドを登録します。

```json
{
  "hooks": {
    "SubagentStop": [{"hooks": [{"type": "command", "command": "OBSIDIAN_VAULT=\"$HOME/obsidian\" /usr/bin/python3 ~/.claude/claude-obsidian/scripts/claude_save.py"}]}],
    "StopFailure": [{"hooks": [{"type": "command", "command": "OBSIDIAN_VAULT=\"$HOME/obsidian\" /usr/bin/python3 ~/.claude/claude-obsidian/scripts/claude_save.py"}]}]
  }
}
```

既存の`hooks`内へ上のイベント項目を追加します。保管庫の指定は既存Stopと同じ値にしてください。`SubagentStop`は子の最終回答・Usageの遅延書き込みを最大2秒待って保存します。子Usageは通常Stopでも集計します。イベント入力は[Claude公式hook仕様](https://code.claude.com/docs/en/hooks)に基づきます。

`StopFailure`では途中の会話・Usageと失敗種別を保存します。ノートには`cli_failure.type`・`observed_at`を記録し、本文がまだない場合はstateに残します。エラー文そのものは保存せず、最後の失敗種別は成功後も履歴として保持します。

quotaも記録する場合は、`~/.claude/settings.json`の同じトップレベルに次を追加します。既存のstatusLine設定がある場合は、そのコマンドに`OBSIDIAN_VAULT="$HOME/obsidian" /usr/bin/python3 ~/.claude/claude-obsidian/scripts/claude_save.py --capture-quota`を組み込んでください。入力はClaude Codeが渡すJSONです。statusLineにはquotaと、提供される場合はCLIのUSD概算が表示されます。

```json
{
  "statusLine": {
    "type": "command",
    "command": "OBSIDIAN_VAULT=\"$HOME/obsidian\" /usr/bin/python3 ~/.claude/claude-obsidian/scripts/claude_save.py --capture-quota"
  }
}
```

## Antigravity CLIとアプリ

Antigravity CLIおよびAntigravityアプリのStop hookから会話履歴を保存します。
quotaは、公式の読み取り専用コマンド `agy --output-format json --print='/quota'`
から取得します。このコマンドは会話ターンを作らず、quotaを消費しません。`agy` が
見つからない、または quota を返さない場合でも、会話保存は継続します。

[agy_save.py](./agy_save.py)をダウンロードし、配置します。

```bash
mkdir -p ~/.gemini/agy-obsidian/scripts
cp "$HOME/Downloads/agy_save.py" ~/.gemini/agy-obsidian/scripts/agy_save.py
```

`~/.gemini/config/hooks.json`に以下を追加します。

```json
{
  "obsidian-save": {
    "Stop": [
      {
        "type": "command",
        "command": "OBSIDIAN_VAULT=\"$HOME/obsidian\" /usr/bin/python3 ~/.gemini/agy-obsidian/scripts/agy_save.py"
      }
    ]
  }
}
```

## 動作確認とトラブルシューティング

設定後にCLIまたはアプリを再起動し、1ターン会話して、保管庫の`生成AI/ChatLog/<ツール名>/`にMarkdownが作られることを確認します。

| 症状 | 確認すること |
| --- | --- |
| ノートが作られない | hookの登録・信頼、プラグインの読み込み、スクリプトの配置先を確認します。本文がない新規セッションでは空ノートを作りません。 |
| 別の保管庫に保存される | hookコマンドまたはCLIの起動環境の`OBSIDIAN_VAULT`を確認します。省略時は`~/obsidian`です。 |
| 金額・quota・所要時間がない | 元の履歴やstatusLineに必要な値があるか確認します。未取得の値を0とは扱いません。 |
| 更新時刻が変わらない | 同じ内容の再通知ではノートを更新しません。新しい発言も反映されない場合はログとdoctorを確認します。 |
| 過去の会話を保存し直したい | 下の「セッション全履歴の再構成」で、差分確認後に再生成できます。OpenCodeのプラグイン初回走査は直近10分のみです。 |

ログの既定位置は次のとおりです。`DEBUG=1`をhookの起動環境に設定すると詳細ログを出力します。

| ツール | 保存ログ |
| --- | --- |
| Codex | `~/.codex/codex-obsidian/log/codex_obsidian_save.log` |
| Claude Code | `~/.claude/claude-obsidian/log/claude_obsidian_save.log` |
| Antigravity | `~/.gemini/agy-obsidian/log/obsidian_save.log` |
| OpenCode | `~/.local/state/opencode-obsidian/opencode_obsidian_save.log` |
| Pi | `~/.local/state/pi-obsidian/pi_obsidian_save.log`（`PI_OBSIDIAN_STATE_DIR`で変更可能） |

### 保存状態の診断（doctor）

[saver_doctor.py](./saver_doctor.py)と比較元の最新`*_save.py`を同じディレクトリに置き、そこで実行します。診断対象は`codex`・`claude`・`agy`・`opencode`・`pi`です。

```bash
/usr/bin/python3 saver_doctor.py codex --json
/usr/bin/python3 saver_doctor.py claude --session-id SESSION_ID
```

配置版と比較元のversion・SHA、出力先権限、stateとノートの保存位置、最終保存成功時刻、最後の失敗・見送り理由を確認できます。`SESSION_ID`はノートのfrontmatterの`session_id`です。ログ・state・ノートを変更する処理や、自動修復は行いません。

標準外の配置先は`--installed /path/to/codex_save.py`、stateは`--state-dir /path/to/state`、比較元は`--source-dir /path/to/source`で指定します。元履歴を明示する場合は`--session-id SESSION_ID --transcript /path/to/rollout.jsonl`を併用します。

| 主な結果 | 意味・次の確認 |
| --- | --- |
| `installed_differs` | 配置版と比較元が異なります。自分で変更した設定も差分になるため、更新前に内容を確認します。 |
| `last_save_failed` | 最後の保存に失敗しています。保存ログと`last_failure_type`を確認します。 |
| `last_save_skipped` | `skip_reason`を確認します。`transcript_missing`は元履歴なし、`no_content`は保存本文なしです。 |
| `cursor_mismatch` | stateとノートの保存位置が異なります。ログと元履歴を確認し、必要なら再構成します。 |
| `transcript_ahead` | 保存後に元履歴が追記された場合も出ます。次の通常保存後に再確認します。 |

`unchanged`は正常な再通知のため問題件数に含めません。診断情報は各saverがstate配下の`.diagnostics/`へ記録した観測値です。古い版で未記録の成功・失敗は復元できません。終了コードは問題なし0、差異・問題あり1、引数・診断実行エラー2です。

### 更新・停止

更新時は配置済みファイルをバックアップし、新しいファイルで置き換えてCLIを再起動します。OpenCode・PiはPythonとプラグイン／extensionの両方を同じ版に揃えてください。Codexのhook定義も変更した場合は`/hooks`で再度信頼します。

保存を止める場合は、このツールのhook項目・プラグイン／extension登録を外して再起動します。ClaudeのstatusLineへ設定した保存コマンドも対象です。既存の会話ノートは残ります。

## 共通設定

### Obsidian保管庫と保存先

上のhook設定例を使う場合は、コマンド内の`OBSIDIAN_VAULT`を変更します。以下の`export`は、CLIを同じターミナルから起動して環境変数で指定する場合の例です。

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
$OBSIDIAN_VAULT/AI/Conversations/pi/
```

`OBSIDIAN_SOURCE`を設定すると、各スクリプトの保存先末尾とfrontmatterの`source`・タグを同じ名前に変更できます。複数のMacや用途で保存先を分ける場合は、それぞれのhookを起動する環境で異なる値を指定します。

```bash
export OBSIDIAN_SOURCE="codex-work"
```

指定できるのは英数字で始まり、以降は英数字・`.`・`_`・`-`からなる64文字以内の名前です。不正な値は各スクリプトの既定名へフォールバックします。既にstateに保存先が記録されている会話は従来のファイルを更新し、新しい会話から指定した保存先を使います。既存ノートの`created`・`source`・タグを引き継ぐため、設定変更後やstate復旧時も会話の識別情報を保持します。AntigravityもCLI・アプリ共通の`source`・タグを記録します。

### ファイル名

保存ファイル名は次の形式です。

```text
{YYYYMMDD}_{HHMMSS}_{project}_{session_id}.md
```

プロジェクト名がない場合は `{YYYYMMDD}_{HHMMSS}_{session_id}.md` です。`session_id` はファイル名として安全な文字に変換されます。過去に保存された先頭8文字形式のファイルも自動探索され、追記が継続されます。

### Templaterテンプレート（任意）

会話記録のファイル名を変えずに、Obsidian上ではfrontmatterの`title`を使って会話を探したり表示したりするための、[ChatLogLink.md](./templates/templater/ChatLogLink.md)と[ChatLogリンク表示名更新.md](./templates/templater/ChatLogリンク表示名更新.md)を使えます。ObsidianのTemplaterプラグインを有効にし、設定したテンプレートフォルダへファイルをコピーしてください。たとえば両方を`~/Downloads`へダウンロードし、テンプレートフォルダが`~/obsidian/Templates`の場合は次のようにします。

```bash
cp "$HOME/Downloads/ChatLogLink.md" ~/obsidian/Templates/
cp "$HOME/Downloads/ChatLogリンク表示名更新.md" ~/obsidian/Templates/
```

- **`ChatLogLink.md`**: テンプレートを挿入すると、`生成AI/ChatLog/`内の最近更新されたノート（最大10件）が候補に表示されます。選択したノートへの内部リンクを、frontmatterの`title`（なければファイル名）で表示します。Usageの`total_cost_usd`があれば、タイトルの後ろに参考金額を小数2桁で表示します。候補一覧ではフォルダ内の相対パスも確認できます。
- **`ChatLogリンク表示名更新.md`**: 開いているノート内のWikiリンクから、`生成AI/ChatLog/`へ解決されるリンクの表示名をリンク先の最新の`title`に更新します。titleがない場合はファイル名を使い、金額があれば付記します。YAML frontmatter、コードブロック、インラインコード内は処理しません。ファイル名や会話記録そのものは変更しません。

使うときは、リンクを挿入する位置または更新したいノートを開き、コマンドパレットから **Templater: Open insert template modal** を実行してテンプレートを選びます。頻繁に使う場合は、Obsidianの設定にある「ホットキー」で同じコマンドにショートカットを割り当てられます。

両テンプレートの`DEFAULT_OUTPUT_DIR`は各`*_save.py`の`DEFAULT_OUTPUT_DIR`（既定値`生成AI/ChatLog`）と同じ名前・値にしています。各保存スクリプトはこのフォルダの下にsource名のサブフォルダを作ります。`OBSIDIAN_OUTPUT_DIR`で保存先を変更した場合は、両テンプレートの`DEFAULT_OUTPUT_DIR`も同じ保存先に変更してください。

<a id="usage-spec"></a>

## 使用量・金額・quotaの記録仕様

### セッション全体と回答ごとの集計

本文冒頭とfrontmatterの`usage`はセッション全体、各回答calloutは対応する回答の使用量です。本文のないUsageや回答へ紐付けられない子Usageがあるため、回答の合計とセッション合計が一致しない場合があります。

| ツール | セッション全体の集計 | 回答への割当 |
| --- | --- | --- |
| Codex | 親と`parent_thread_id`で紐付く子の`token_usage_record`を集計。`thread_token_usage`の累計は加算しません。 | 子の`root_turn_id`で親ターンを特定し、最後の回答へ加算。紐付かない子はセッション計のみです。 |
| Claude Code | 親・子それぞれのmessage IDごとの最後の`message.usage`を集計。 | 子の`parentUuid`が一つの親ターンに結び付く場合のみ回答へ加算。親・子・回答に紐付かない子のtoken内訳も表示します。 |
| OpenCode | SQLiteのassistantレコードをmessage IDごとに集計。本文のないツール専用レコードも含めます。 | 同じユーザー発話内の連続する同一モデルの回答をまとめ、本文のないUsageも近い回答へ加算します。 |
| Pi | 全分岐のentryをIDごとに集計。圧縮・分岐要約・追加Usage・Usage付きツール結果も含めます。 | 現在の分岐のUsageのみ表示。同一ターン・同一モデルを優先して近い回答へ加算します。 |

Codexの子Usageは対象の`session_id`と一致するレコードに限定します。Claudeの子は`親transcript拡張子なし/subagents/agent-*.jsonl`から読み、別sessionId・symlinkを除外します。子の本文は親ノートへ混ぜません。Piでモデルを特定できないUsageは`unknown`としてtokenのみ集計します。

共通形式の`input_tokens`はキャッシュ読込・書込を含む入力総数、`reasoning_output_tokens`は出力の内数です。これらを二重加算しません。遅れて書き込まれたUsageは次の保存で反映します。

### USD参考換算とCLI報告コスト

**参考換算と取得コストは別々に表示し、合算しません。** 参考換算は共通単価表から計算した比較用の金額です。サブスクリプション請求額やquota消費額ではありません。

| 値 | 記録先 | 対象・扱い |
| --- | --- | --- |
| USD参考換算 | `usage.total_cost_usd`、`usage.by_model.*.cost_usd` | Codex・Claude・OpenCode・Pi。`usage.cost_basis: reference`で意味を示します。 |
| CLI報告コスト | `reported_cost.usd` | OpenCode・Piは回答の取得値の合計（`scope: message_sum`）、ClaudeはstatusLineのセッション累計の最新観測値（`scope: session`）です。 |

OpenCodeはassistantの`cost`、Piは`usage.cost.total`、ClaudeはstatusLineの`cost.total_cost_usd`を取得します。Claudeは再通知ごとに加算せず、回答へ推測配分しません。OpenCode・Piは回答calloutにも取得コストを表示します。

参考換算できない金額は`換算不可`、一部の既知モデルだけ換算した総計は`部分換算`（`cost_incomplete: true`）です。CLI報告値は0を`$0`、未提供・負数・非有限等を`未取得`、一部だけ取得できた場合を`部分取得`として区別します。無料モデルも対応する有料版の参考単価で換算するため、参考換算が正でもCLI報告値が0になることがあります。

金額はノートでは小数桁を丸めず固定小数で保存します。CLI報告値も実請求額の保証ではありません。Claudeの値は定価または`modelPricing`を基にしたクライアント側概算です。[Claude公式statusLine仕様](https://code.claude.com/docs/en/statusline#available-data)Claudeの取得値はstate・ノートから復旧し、再構成時も現在の単価で再計算しません。

**OpenCode 3.xからの移行:** 4.0.0以降の`usage.total_cost_usd`は参考換算に統一しました。以前の正の`cost`を優先する方式とは意味が異なります。通常保存・明示的な再構成から新方式になり、過去ノートは一括変更しません。3.xのstateしかない部分同期で取得金額を判別できない場合は未取得とし、完全なsnapshotで復元します。

### 対応モデルと価格条件

単価表に登録しているモデルは以下です。これはこのソースに収録した参考単価の対応表で、各社の最新料金・提供状況を示す一覧ではありません。プロバイダ接頭辞と対応する別名を正規化して照合します。未登録でも本文・token数は保存します。

| 系列 | 登録モデル |
| --- | --- |
| GPT | GPT-6 Astra、GPT-6.1 Sol、GPT-6.1 Terra、GPT-6 Luna |
| Claude | Fable 5.1、Opus 5.5、Sonnet 5.5、Haiku 5.5 |
| Gemini | Gemini 3.8 Flash |
| DeepSeek | DeepSeek V4.1 Flash（`deepseek-flash`も対応） |
| その他 | Muse Spark 1.3 Contributor、Nemotron 3 Ultra |

- GPTの272K超入力: Codexは先頭の`APPLY_LONG_CONTEXT_PRICING = False`で倍率なしです。`True`にするとリクエスト単位で入力・キャッシュ系2倍、出力1.5倍となり、対象回答に`⚠︎ 272K+`を表示します。Claude・OpenCode・Piの既定は`True`です。CLI報告コストには掛けません。
- Claude: cache writeの5分・1時間を区別します。Haiku 5.5は1リクエストの入力総数が10万token超の場合に全種別5倍、Opus 5.5・Sonnet 5.5は20万token超でも標準単価です。Fast・地域・TTL等の価格条件を確定できない場合は換算を省略します。
- DeepSeek: 登録済みピーク単価を固定で使用し、時刻によるオフピーク切り替えは行いません。
- Gemini: 登録表は2026年末までのStandard割引価格を基にしています。cache writeはOpenRouter掲載のtoken単価を使い、Googleの時間単位の保管料は加算しません。
- Batch・Flex・Priority・ツール料金などは参考換算に含めません。単価は自動更新しません。

収録単価・価格版・確認日・出典は、[check_usage_prices.py](./check_usage_prices.py)・[rebuild_session.py](./rebuild_session.py)・[claude_save.py](./claude_save.py)を同じディレクトリへ置き、次で確認できます。

```bash
/usr/bin/python3 check_usage_prices.py
```

ノートには`usage.price_version`・`price_recorded_at`・`price_checked_at`・`price_sources`・`price_effective_from`も保存します。価格版はsaverの機能バージョンと独立しています。確認日がないモデルや適用開始日が不明な値はnullです。価格設定を変えた後の通常保存では、そのセッションの累計・既存回答Usageを現在の設定で再計算します。

### quota残量

Codexはtranscript、Claudeは任意のstatusLine入力の`rate_limits`、Antigravityは`/quota`から取得します。weekly・5hなど取得できた枠だけを記録し、最初と最後の観測値を`first_observed`・`last_observed`へ保存します。「最初」は会話開始時ではなく最初に取得できた値です。

quotaはアカウント全体の残量です。差分を会話の消費量とは扱わず、回答別の差分も表示しません。未提供の枠は省略し、会話保存は続けます。ClaudeのstatusLineはquotaがなくてもコストがあれば表示し、どちらもなければ空表示です。新形式の保存時に旧`quota`・`quota_claude`の数値形式を置換します。

### 回答所要時間

依頼から回答完了までを、最後の回答に`⏱ 回答所要時間: 2分15秒`の形式で表示します。途中経過・ツール実行・通信・生成を含み、AI内部の推論時間だけを測るものではありません。同じ依頼でモデルが変わっても最後のcalloutに総時間を表示し、秒未満は切り捨てます。

| ツール | 完了時刻の取得元 |
| --- | --- |
| Codex | ターン完了イベント。未記録なら、最終回答と識別できる回答完了イベント。 |
| Claude Code | `turn_duration`終了記録。Stopより後の記録は次の保存で反映。 |
| OpenCode | 回答の`time.completed`。 |
| Pi | 回答終了時に履歴へ保存されるentryの時刻。 |
| Antigravity | `model_stop`・`fullyIdle: true`・エラーなしのStop受信時刻。hook起動までの遅れは含み、ロック待ち・quota取得などの保存処理は含みません。 |

開始・完了時刻がない、不正、逆転している場合は`取得不可`とします。Antigravityの観測済み時刻はstate・ノート内コメントに保持し、再通知・復旧・再構成でも引き継ぎます。未観測の過去履歴に現在時刻を付けることはありません。

## 出力例

主要項目を抜粋した例です。価格の出典・quota・回答別Usage等は入力に応じて追加されます。

```markdown
---
source: codex-cli
session_id: "019f6d84-c321-7750-9532-c4b40fad70df"
title: "このコードのバグを直してください。"
project: "/path/to/myapp"
created: "2026-07-17T09:40:55+09:00"
modified: "2026-07-17T09:40:55+09:00"
tags:
  - ai-conversation
  - codex-cli
message_count: 2
usage:
  cost_basis: "reference"
  price_version: "4.2.0"
  input_tokens: 120000
  cached_input_tokens: 100000
  cache_write_input_tokens: 0
  output_tokens: 8000
  reasoning_output_tokens: 3000
  total_tokens: 128000
  total_cost_usd: 0.13
  by_model:
    "gpt-6.1-sol":
      total_tokens: 128000
      cost_usd: 0.13
---

💰 **Usage (USD換算)**: Total: $0.13 (gpt-6.1-sol $0.13)

<!-- last_line: 11 -->

# このコードのバグを直してください。
> [!QUESTION] User
> <small>⏱ 2026-07-17 09:40:45</small>
>
> このコードのバグを直してください。

> [!NOTE] Codex
> <small>🤖 gpt-6.1-sol-medium</small>

> 問題を確認しました。原因は...
```

ChatGPTからWorkへ移行した際に生成される構造化されたユーザー入力は、先頭が
`## Referenced ChatGPT conversation:` または `# Files mentioned by the user:` で、
コードフェンス外に `## My request:` が1つあり、後続本文が空でない場合に限って表示を分けます。
依頼本文は通常のUser QUESTION calloutへ表示し、前置きの参照情報は内容を解釈せず、
折りたたみ式の「参照情報（原文）」INFO calloutへ保存します。条件が曖昧な入力は従来どおり全文を保存します。

## セッション全履歴の再構成

[rebuild_session.py](./rebuild_session.py)と、対象の保存スクリプト（`codex_save.py`・`claude_save.py`・`agy_save.py`・`opencode_save.py`のうち必要なもの）を同じディレクトリに置きます。Piは対象外です。`SESSION_ID`はノートのfrontmatterの`session_id`、未保存の会話なら元履歴のIDを使います。

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

### 価格版を選ぶ

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

### 元履歴の場所

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

<a id="telemetry"></a>

## telemetryとの照合（任意）

取得済みのOTelログと会話履歴のtoken・金額をローカルで比較する補助ツールです。通常の会話保存にcollectorは不要です。照合ツール自身は通信せず、ノート・state・CLI設定も変更しません。

### 必要なファイルと入力

以下を同じディレクトリに置いて実行します。

| 対象 | 必要なファイル |
| --- | --- |
| Codex | [compare_codex_telemetry.py](./compare_codex_telemetry.py)、[compare_claude_telemetry.py](./compare_claude_telemetry.py)、[rebuild_session.py](./rebuild_session.py)、[codex_save.py](./codex_save.py) |
| Claude | [compare_claude_telemetry.py](./compare_claude_telemetry.py)、[rebuild_session.py](./rebuild_session.py)、[claude_save.py](./claude_save.py) |

入力はOTLP JSONログ（`resourceLogs/scopeLogs/logRecords`）、その配列、または属性を展開したイベントJSONLです。collector等でJSONファイルへ書き出してから渡してください。protobufバイナリ、trace span、Python repr形式のconsole出力は使えません。

### Codex

```bash
/usr/bin/python3 compare_codex_telemetry.py \
  --telemetry /private/tmp/codex-otel.json \
  --transcript /path/to/rollout-SESSION_ID.jsonl \
  --session-id SESSION_ID
```

`codex.sse_event`・`codex.websocket_event`の`response.completed`と、transcriptの`token_usage_record`を照合します。`conversation.id`で対象を限定し、`--include-subagents`を加えると親子関係を確認できる子も集計します。

| `--group-by` | 比較単位 |
| --- | --- |
| `response`（既定） | response IDごとのリクエスト。 |
| `turn` | turn ID・モデルごとの件数とtoken合計。 |
| `model` | セッション内のモデル別合計。同一turn内の入れ替わりは検出できません。 |

response IDがない入力で集計比較を選ぶ例です。

```bash
/usr/bin/python3 compare_codex_telemetry.py --telemetry /private/tmp/codex-otel.json \
  --transcript /path/to/rollout-SESSION_ID.jsonl --session-id SESSION_ID --group-by turn
```

token属性は`input_token_count`・`cached_token_count`・`cache_write_token_count`・`output_token_count`・`reasoning_token_count`が必要です。欠落を0とは推測しません。同じresponse ID、またはOTLPのtimestamp・trace/spanから再配送を除外し、識別できないイベントは照合不足とします。重複IDの内容不一致やtoken内訳の矛盾はエラーです。失敗イベント・累積counter・ヒストグラムは加算しません。

USDは両側のtokenへ同じ参考単価を適用した値を比較します。`--price-version`で登録版を指定できます。tokenが一致しても換算できないモデルがあれば`unpriced_groups`として不足を報告します。請求データとの照合ではありません。

### Claude Code

```bash
/usr/bin/python3 compare_claude_telemetry.py \
  --telemetry /private/tmp/claude-otel.json \
  --transcript /path/to/SESSION_ID.jsonl \
  --session-id SESSION_ID --include-subagents
```

`claude_code.api_request`の`request_id`とtranscriptの`requestId`を照合し、再配送は一回だけ集計します。`session.id`で対象を限定し、APIエラーや累積counterは加算しません。モデル・通常入力・cache read・cache creation・出力を比較し、USDの許容差は0.000001です。`--price-version`で登録版を指定できます。

IDなし、片側だけのrequest、TTL不明等の換算不可、データなしは一致扱いにしません。両ツールとも終了コードは一致0、差異・照合不足1、不正入力2です。実際のCLI版による入力差はあり得ます。Codexの実OTel出力とClaudeの有料契約環境との結合検証は未実施です。

### OTelログを収集する場合

Codexではcollectorを別途用意し、`~/.codex/config.toml`の既存設定へ次を追加します。`protocol = "json"`で送信しても、collector側の保存形式が照合ツールで読めるJSONになるよう設定する必要があります。

```toml
[otel]
log_user_prompt = false
exporter = { otlp-http = { endpoint = "http://127.0.0.1:4318/v1/logs", protocol = "json" } }
```

- 収集・送信にCPU・メモリ・通信が増え、collectorの起動管理・保持期間・容量管理が必要です。外部サービスには利用料金が発生する場合があります。
- `log_user_prompt = false`でも会話ID・モデル・CLI版・ツール結果の断片等は送信され得ます。送信先と保持範囲を確認してください。
- 終了時のflush、再配送、収集開始時点、CLI版の属性差でデータが不足する場合があります。照合不足なら両入力の範囲を確認します。

必要な期間だけローカルで収集し、検証後はexporterを無効にできます。ログ・traceのexportとOpenAIへのanalytics設定は別です。設定の根拠は[Codex公式OTel仕様](https://learn.chatgpt.com/docs/config-file/config-advanced#observability-and-telemetry)、Claudeの収集設定は[公式Monitoring](https://code.claude.com/docs/en/monitoring-usage)で確認できます。

## 開発・テスト

通常の利用ではリポジトリ全体の取得は不要です。開発やテストをする場合は、[リポジトリのZIP](https://github.com/shusie-lab/cli-to-obsidian/archive/refs/heads/main.zip)を展開するか、Gitで取得してください。

外部パッケージは不要です。リポジトリのルートで次を実行します。

```bash
/usr/bin/python3 sync_shared.py --check
/usr/bin/python3 -m unittest discover -s tests -v
/usr/bin/python3 -X pycache_prefix=/tmp/cli_obsidian_save_pycache -m py_compile agy_save.py codex_save.py claude_save.py opencode_save.py pi_save.py
```

5本のMarkdown整形・日時・ログ・frontmatter・state・ロック・atomic書き込み・既存Markdown探索、
4本のUsage集計・金額表示・モデル別名・参考単価を`shared/`の正本で管理します。
Codex・Antigravity・Claude Codeは増分描画とcursor復旧を、OpenCode・Piはsnapshot描画を共有します。
入力解析・重複排除・quota取得・価格条件・Usageの集計範囲は各saverに残します。変更後に
`/usr/bin/python3 sync_shared.py`で各`*_save.py`へ埋め込みます。生成後の各スクリプトは
共通モジュールをimportせず、従来どおり単一ファイルで動作します。生成領域を変更する場合は
各スクリプトを直接編集せず、`shared/`を変更してから同期してください。同期時は全スクリプトの
Python構文を先に検証し、1本でも不正なら書き込みません。

テストでは、5本の空入力・同一内容の再保存・source設定変更・state消失・非表示ユーザー発話の共通契約、生成領域内外のコード重複、単独ファイル実行を検証します。さらに、通常の追記、CodexとAntigravityのquota観測値、Claude CodeとOpenCodeのtoken集計、連続発言の集約に加えて、cursor復旧、state消失時の既存Markdown再利用、書き込み途中のJSONL最終行の再試行、YAML文字列とstateファイル名の安全化、保存先相対パスの検証を確認しています。

### 単価表の差分確認

単価表の正本は`shared/usage_prices.py.inc`、履歴と出典は`shared/usage_price_history.py.inc`です。公式出典を確認して候補JSONを作成し、次のコマンドで差分を確認します。候補の`rates`には全モデルを含めてください。省略したモデルは削除差分になります。

```bash
/usr/bin/python3 check_usage_prices.py > /private/tmp/current-prices.json
# 公式出典を確認して候補JSONを作成した後
/usr/bin/python3 check_usage_prices.py --candidate /private/tmp/candidate-prices.json
```

終了コードは差分なし0、差分あり1、入力エラー2です。候補の自動採用・Web取得は行いません。変更時は旧表を履歴として残し、価格版・確認日・出典を更新してから`/usr/bin/python3 sync_shared.py`で各saverへ反映します。

## 実装上の特徴

- **ノイズの少ないログ保存**: ユーザーおよびエージェントの発言だけを抽出し、ツール呼び出しや実行結果を除外します。
- **Codexのsnapshot解析**: Stopの最初にJSONLを一度読み、metadata・quota・Usage・本文を同じsnapshotから計算します。不完全な末尾は次回へ保留します。子transcriptはファイルごとのsnapshotです。
- **増分管理**: CodexとClaude Codeは`last_line`、Antigravityは`last_id`で読み込み済み位置を管理します。
- **cursorとstateの自動復旧**: Markdown内のcursorを正としてstateとの不一致を補正し、state消失時はfrontmatterの`session_id`から既存Markdownを探索します。
- **内容変更時だけノート更新**: 保存対象の本文がない新規セッションではノートを作らず、同じ内容の再保存で`modified`だけを変更しません。
- **Atomic書き込み**: Markdownとstateを同一ディレクトリの一時ファイルへ書いてからrenameします。
- **同時実行への対応**: セッション単位のファイルロックでhookの重複実行を防ぎます。
- **安全なメタデータとファイル名**: YAML文字列、session ID、プロジェクト名を安全な形式に変換します。
- **古いstateのクリーンアップ**: 30日間利用されていないstateファイルを自動的に削除します（非ブロッキングロックで実行中セッションを保護）。

このプロジェクトは各AIサービス・CLI・Obsidianの非公式ツールで、各サービスの開発元とは関係ありません。

## 謝辞

Markdownの出力形式では、[Obsidian AI Exporter](https://github.com/sho7650/obsidian-AI-exporter)のYAML frontmatter、ユーザー発言の見出し、Calloutを使った会話表現を参考にしました。優れたアイデアを公開されている開発者に感謝します。

## バージョン管理

このソースのバージョンは4.2.0です。リリースはリポジトリ全体で共通のバージョン番号を使用し、[Semantic Versioning](https://semver.org/lang/ja/)に従います。各スクリプトの`__version__`と[VERSION](./VERSION)を揃え、公開時に対応するGitタグを付けます。変更履歴は[CHANGELOG](./CHANGELOG.md)に記録しています。

## ライセンス

このプロジェクトは[MIT License](./LICENSE)の下で公開されています。
