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

USD参考換算は、実際のサブスクリプション請求額やquota消費量を示すものではありません。また、token数や参考金額の比重は作業時間の比重とは異なります。取得できる情報は、次の対応表を確認してください。

### 回答所要時間

各ツールの最後の回答には、`⏱ 回答所要時間: 2分15秒` の形式で時間を表示します。
ユーザーの発話から回答完了までの経過時間で、途中経過・ツール実行・通信・回答生成を含みます。
AI内部の推論だけに費やした時間ではありません。同じ依頼でモデルが変わって複数の回答calloutに
分かれる場合も、その依頼の最後のcalloutに総時間を表示します。秒未満は切り捨てます。

- Codex: ターン完了イベントの完了時刻を使用します。まだ記録されていない場合は、明示的に
  最終回答と識別できる回答完了イベントの時刻を使用します。
- Claude Code: `turn_duration` 終了記録の時刻を使用します。Stopフックより後に記録される場合は
  一旦「取得不可」となり、次の保存で読み取れた終了記録を反映します。
- OpenCode: 回答の `time.completed` を使用します。
- Pi: 回答終了時に履歴へ保存されるentryの時刻を使用します。モデル側の開始時刻とは区別します。
- Antigravity: `terminationReason: model_stop`・`fullyIdle: true`・エラーなしのStopフックを
  受信した時刻を、今回の回答の終了観測時刻として使用します。フック起動までの遅れを含みますが、
  ロック待ち・quota取得など保存処理の時間は含みません。同じ回答の再実行では時刻を上書きしません。
  観測時刻はstateとノート内の非表示コメントに保持し、state復旧・既存ノートの再構成でも引き継ぎます。
  終了状態の情報が不足するStopや、終了を観測していない過去履歴だけからの再構成では「取得不可」と表示します。

依頼時刻・完了時刻の欠落、不正な日時、完了が依頼より前になる記録でも「取得不可」と表示し、
作成時刻やフック再実行時の現在時刻から推測しません。過去ノートを一括で再生成する処理は行いません。

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

DeepSeek V4.1 Flashは`deepseek-flash`、`deepseek-v4.1-flash`（プロバイダ接頭辞付き・無料版の表記を含む）に対応します。[公式料金](https://api-docs.deepseek.com/quick_start/pricing/)のピーク単価（100万tokenあたり入力$0.30、キャッシュ読込$0.006、出力$1.20）で固定の参考換算を行います。キャッシュ書込は通常入力と同単価です。オフピークの半額料金への時刻別切り替えは行いません。無料版も有料API単価での参考値です。

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

すでに `hooks.json` を使っている場合は、上記全体で置き換えず、既存の `hooks.Stop` 配列の末尾へ内側のオブジェクト（`{"hooks": [...]}`）を追加してください。

`hooks.json`を使わず、`config.toml`でhookを管理する場合は、代わりに以下を追加します。

```toml
[features]
hooks = true

[[hooks.Stop]]
[[hooks.Stop.hooks]]
type = "command"
command = 'OBSIDIAN_VAULT="$HOME/obsidian" /usr/bin/python3 ~/.codex/codex-obsidian/scripts/codex_save.py'
timeout = 10
```

設定後はCodexの `/hooks` でhookの登録と信頼状態を確認してください。

CodexのCLIとアプリは会話を共有できるため、transcriptの起動元にかかわらず、既定では`codex-cli/`に保存し、frontmatterの`source: codex-cli`とタグ`codex-cli`を記録します。端末別に区別する場合は[共通設定](#共通設定)の`OBSIDIAN_SOURCE`を指定してください。

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

子Usageを完了直後に反映する場合は`SubagentStop`、APIエラー時も途中の会話を保存する場合は`StopFailure`へ、同じコマンドを登録します。設定例と集計範囲は[保存基盤の改善](docs/saver-improvements.md#3-claudeの子usageapi失敗)を参照してください。
Claudeノートには親・子・回答に紐付かない子のtoken数を別々に表示します。回答に紐付かない子Usageもセッション合計には含まれます。

quotaも記録する場合は、`~/.claude/settings.json`の同じトップレベルに次を追加します。既存のstatusLine設定がある場合は、そのコマンドに`OBSIDIAN_VAULT="$HOME/obsidian" /usr/bin/python3 ~/.claude/claude-obsidian/scripts/claude_save.py --capture-quota`を組み込んでください。入力はClaude Codeが渡すJSONです。statusLineにはquotaと、提供される場合はCLIのUSD概算が表示されます。

```json
{
  "statusLine": {
    "type": "command",
    "command": "OBSIDIAN_VAULT=\"$HOME/obsidian\" /usr/bin/python3 ~/.claude/claude-obsidian/scripts/claude_save.py --capture-quota"
  }
}
```

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

OpenCode v2で設定ファイルから明示的に読み込む場合は、`opencode.jsonc`の`plugins`へファイルURIを指定します。以下の`/absolute/path/to/opencode_obsidian.js`は、配置したプラグインの実際の絶対パスへ置き換えてください。JSON内では`~`や`$HOME`の展開を前提にしません。

```json
{
  "plugins": [
    "file:///absolute/path/to/opencode_obsidian.js"
  ]
}
```

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

自動リトライ・圧縮・キュー処理まで終了した`agent_settled`を保存の境界にします。本文は現在の分岐のユーザー発話・回答のみを表示し、ツール結果・推論本文は表示しません。分岐切替時は同じノートを現在の分岐で再構成します。ノートは自動生成されるため、本文への手動追記は次回保存で置き換わります。Usageはentry IDごとに一度だけ集計し、別分岐・本文のないassistant・圧縮・分岐要約・追加usageレコード・usage付きツール結果も含めたセッション全体です。同じユーザー発話に対する連続する同一モデルの途中経過と最終回答は1つのcalloutにまとめ、Usageも合算します。本文のないUsageは、同じユーザー発話内の同一モデルを優先して最も近い回答calloutへ加算します。回答calloutは現在の分岐のUsageだけを表示します。本文のない分岐へ切り替えた場合も以前の本文を残さず再構成します。`message_count`は表示するcallout数です。モデルを特定できないツール内のUsageは`unknown`としてtokenのみ集計します。

USD換算にはCodex・Claude Code・OpenCodeと共通の参考単価表を使います。単価表の正本は`shared/usage_prices.py.inc`で、各スクリプトへ埋め込みます。登録済みモデルをすべて対象にし、Piが返す`usage.cost.total`は別の取得コストとして保存し、参考換算と併記します。Piの`input`・`cacheRead`・`cacheWrite`を足して共通の入力総数にし、`reasoning`は`output`の内数として扱います。GPT系ではリクエスト単位の入力が272Kを超える場合の倍率も適用し、対象回答に`⚠︎ 272K+`を付けます。未登録モデルや1時間cache writeなど単価条件を確定できないUsageは参考換算額を省略し、一部だけ換算できた総計には`cost_incomplete: true`を付けます。サブスクの請求額・quota消費額ではありません。

一時保存先で試す場合は、次のようにextensionを直接指定できます。

```bash
OBSIDIAN_VAULT=/private/tmp/pi-obsidian-test/vault \
PI_OBSIDIAN_STATE_DIR=/private/tmp/pi-obsidian-test/state \
PI_OBSIDIAN_SCRIPT="$PWD/pi_save.py" \
pi --extension "$PWD/pi_obsidian.ts" --provider openai --model gpt-6-luna -p "OKとだけ回答してください。"
```

## 動作確認とトラブルシューティング

各CLIまたはアプリで会話を1ターン実行し、対応する保存先にMarkdownが作成されることを確認してください。保存されない場合は、次のログを確認します。

- Codex: `~/.codex/codex-obsidian/log/codex_obsidian_save.log`
- Antigravity: `~/.gemini/agy-obsidian/log/obsidian_save.log`
- Claude Code: `~/.claude/claude-obsidian/log/claude_obsidian_save.log`
- OpenCode: `~/.local/state/opencode-obsidian/opencode_obsidian_save.log`
- Pi: `~/.local/state/pi-obsidian/pi_obsidian_save.log`

詳細ログが必要な場合は、hookを起動する環境で`DEBUG=1`を設定します。`OBSIDIAN_VAULT`が存在しない場合は自動作成されますが、親ディレクトリへの書き込み権限が必要です。


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

`OBSIDIAN_SOURCE`を設定すると、各スクリプトの保存先末尾とfrontmatterの`source`・タグを同じ名前に変更できます。MacとRaspberry Piで保存先を分ける場合は、それぞれのhookを起動する環境で異なる値を指定します。

```bash
export OBSIDIAN_SOURCE="codex-pi4"
```

指定できるのは英数字で始まり、以降は英数字・`.`・`_`・`-`からなる64文字以内の名前です。不正な値は各スクリプトの既定名へフォールバックします。既にstateに保存先が記録されている会話は従来のファイルを更新し、新しい会話から指定した保存先を使います。既存ノートの`created`・`source`・タグを引き継ぐため、設定変更後やstate復旧時も会話の識別情報を保持します。AntigravityもCLI・アプリ共通の`source`・タグを記録します。

### ファイル名

保存ファイル名は次の形式です。

```text
{YYYYMMDD}_{HHMMSS}_{project}_{session_id}.md
```

プロジェクト名がない場合は `{YYYYMMDD}_{HHMMSS}_{session_id}.md` です。`session_id` はファイル名として安全な文字に変換されます。過去に保存された先頭8文字形式のファイルも自動探索され、追記が継続されます。

### Templaterテンプレート（任意）

[templates/templater](./templates/templater)から使いたいテンプレートをダウンロードするか、[リポジトリのZIP](https://github.com/shusie-lab/cli-to-obsidian/archive/refs/heads/main.zip)を展開して利用できます。通常の会話保存には不要です。

会話記録のファイル名を変えずに、Obsidian上ではfrontmatterの`title`を使って会話を探したり表示したりするための、任意のTemplaterテンプレートを`templates/templater/`に収録しています。ObsidianのTemplaterプラグインを有効にし、設定したテンプレートフォルダへファイルをコピーしてください。たとえば両方を`~/Downloads`へダウンロードし、テンプレートフォルダが`~/obsidian/Templates`の場合は次のようにします。

```bash
cp "$HOME/Downloads/ChatLogLink.md" ~/obsidian/Templates/
cp "$HOME/Downloads/ChatLogリンク表示名更新.md" ~/obsidian/Templates/
```

- **`ChatLogLink.md`**: テンプレートを挿入すると、`生成AI/ChatLog/`内の最近更新されたノート（最大10件）が候補に表示されます。選択したノートへの内部リンクを、frontmatterの`title`（なければファイル名）で表示します。Usageの`total_cost_usd`があれば、タイトルの後ろに金額も表示します。候補一覧ではフォルダ内の相対パスも確認できます。
- **`ChatLogリンク表示名更新.md`**: 開いているノート内のWikiリンクから、`生成AI/ChatLog/`へ解決されるリンクの表示名をリンク先の最新の`title`に更新します。titleがない場合はファイル名を使い、金額があれば付記します。YAML frontmatter、コードブロック、インラインコード内は処理しません。ファイル名や会話記録そのものは変更しません。

使うときは、リンクを挿入する位置または更新したいノートを開き、コマンドパレットから **Templater: Open insert template modal** を実行してテンプレートを選びます。頻繁に使う場合は、Obsidianの設定にある「ホットキー」で同じコマンドにショートカットを割り当てられます。

両テンプレートの`DEFAULT_OUTPUT_DIR`は各`*_save.py`の`DEFAULT_OUTPUT_DIR`（既定値`生成AI/ChatLog`）と同じ名前・値にしています。各保存スクリプトはこのフォルダの下にsource名のサブフォルダを作ります。`OBSIDIAN_OUTPUT_DIR`で保存先を変更した場合は、両テンプレートの`DEFAULT_OUTPUT_DIR`も同じ保存先に変更してください。

## 使用量・quotaの記録仕様

Codexのquotaは、Stop hookに渡されるtranscript内の`token_count.rate_limits`から取得します。frontmatterの`quota`にはweekly・5hなど取得できた枠ごとに`first_observed`と`last_observed`を階層化し、残量・観測時刻・リセット時刻を保存します。「最初」は会話開始時ではなく最初に取得できた観測値です。quotaはアカウント全体の状態なので、差分を会話の消費量として扱わず、回答ごとのquotaも表示しません。quota情報を取得できない場合も、会話保存は継続します。Antigravityも同じ形式で保存します。

Codex・Claude Code・OpenCode・Piの参考換算は、共通表`shared/usage_prices.py.inc`の`STANDARD_USAGE_RATES`に照合できるモデルをすべて対象にします。現在の登録モデルはGPT-6 Astra、GPT-6.1 Sol、GPT-6 Luna、Gemini 3.8 Flash、Claude Fable 5.1、Claude Opus 5.5、Claude Sonnet 5.5、Claude Haiku 5.5、Muse Spark 1.3 Contributor、Nemotron 3 Ultraです。プロバイダ接頭辞と対応する別名を正規化して照合します。単価を削除したGPT-6 Sol・GPT-5.6系列も会話本文とtoken数は保存し、参考金額は換算不可とします。OpenCodeが返す正の`cost`は、単価登録の有無によらず優先します。

Gemini 3.8 Flashは`google/gemini-3.8-flash`・`gemini-3.8-flash`・`gemini3.8-flash`を照合できます。2026-10-06時点のStandard参考単価は100万tokenあたり入力$0.75、キャッシュ読込$0.075、出力（推論を含む）$3.75です。[Googleの価格表](https://ai.google.dev/gemini-api/docs/pricing#gemini-3.8-flash)に記載された2026年末までの割引価格を登録しています。cache writeは[OpenRouterのStandard endpoint掲載値](https://openrouter.ai/api/v1/models/google/gemini-3.8-flash/endpoints)を100万token単位へ換算した$0.0416666666666667を使います。Googleの時間単位のキャッシュ保管料は別途計算しません。Batch・Flex・Priority・ツール料金は参考換算に含めず、OpenCodeの正の`cost`は引き続き優先します。登録単価は固定値のため、2027年の価格変更時に表を更新してください。

Haiku 5.5は`claude-haiku-5-5`・`anthropic/claude-haiku-5.5`に対応します。100万tokenあたり入力$0.10、キャッシュ読込$0.01、5分書込$0.125、1時間書込$0.20、出力$0.50です。1リクエストの入力総数（キャッシュ読込・書込を含む）が10万tokenを超える場合は、すべての単価を5倍にします。[公式料金表](https://platform.claude.com/docs/en/about-claude/pricing)を2026-10-09に確認しました。

CodexのUsageは、親セッションと`parent_thread_id`で紐付くサブエージェントのtranscriptにある`token_usage_record.payload.usage`を各レコード一度ずつ加算して集計します。子のusageは同じ`session_id`を持つレコードだけを対象にします。`thread_token_usage`の累計値は二重計上を避けるため使用しません。frontmatterの`usage`にはtoken総計・モデル別内訳・`total_cost_usd`・モデル別`cost_usd`を保存します。USD概算は、`shared/usage_prices.py.inc`から各スクリプトへ埋め込むモデル別標準API単価を用い、通常入力（`input_tokens`からキャッシュ入力・cache writeを除いた値）、キャッシュ入力、cache write、出力を別々に計算します。推論トークンは出力の内数のため二重計上しません。Codexでは、スクリプト先頭の`APPLY_LONG_CONTEXT_PRICING = False`により、272K input tokensを超えても通常単価で換算します。`True`にすると、従来どおりリクエスト単位で入力・キャッシュ系を2倍、出力を1.5倍にします。これはCodexの契約上の請求額やquota消費額ではなく、ツール料金・割引モードなどを含まない参考単価による概算です。係数が未登録のモデルはtokenだけを保存し、金額を換算しません。既知モデルが一部でもある場合はその分の`total_cost_usd`を保存し、`cost_incomplete: true`で総計が部分換算であることを示します。USD表示・frontmatterの金額は小数桁を丸めません。

本文中のCodex回答calloutには、親transcriptの回答と、同じ親ターンに紐付くサブエージェントのUsage（`total_tokens`とUSD概算）を表示します。子の`root_turn_id`で親ターンを特定し、そのターンの最後に表示される回答calloutへ加えます。サブエージェント分がある回答では、合計のtoken数とUSD概算に加え、括弧内にサブエージェントのモデル別token数・USD概算を表示します。換算できないモデルは`換算不可`と表示します。親ターンを特定できない子のusageや、表示できる親回答がないターンのusageはセッション累計だけに含めます。本文冒頭のUsageセクションとfrontmatterの`usage`はセッション累計です。`APPLY_LONG_CONTEXT_PRICING = True`で272K input tokensを超えるusage recordが回答に含まれる場合は、回答のUSD表示に`⚠︎ 272K+`を付けます。無効時はこの印を表示しません。設定変更後にhookが動くと、そのセッションの累計と既存の回答Usageも再計算します。他の過去ノートを一括更新することはありません。

Claude CodeのUsageはtranscriptの`message.usage`をmessage IDごとに最後の値だけ集計します。通常入力、キャッシュ読み込み、キャッシュ書き込み、出力を合わせてtoken総数を出します。換算対象は他のUsage対応saverと共通です。`shared/usage_prices.py.inc`から埋め込むtoken種別ごとの単価表からUSD概算を計算します。Opus 5.5のキャッシュ読込は100万tokenあたり$0.20です。Opus 5.5・Sonnet 5.5は入力20万token超でも標準単価で換算します。キャッシュ書込は5分・1時間の内訳に応じて換算し、価格に影響する条件を確定できない場合や未登録モデルはtokenのみを保存します。`anthropic/claude-sonnet-5.5`にも対応し、`nvidia/nemotron-3-ultra-550b-a55b:free`は対応する有料版の単価を参考値として換算します。これはClaude Codeのサブスクリプション請求額ではありません。quotaは任意のstatusLine設定から5h・weeklyの残量を観測し、Codexと同じ`first_observed`・`last_observed`形式で保存します。statusLineがquotaを返さない場合は省略します。

2026-10-25確認の公式価格に合わせ、Sonnet 5.5のキャッシュ読込は$0.10/100万tokenです。Fable 5.1にも対応し、通常入力$10・出力$50・キャッシュ読込$0.25・5分書込$12.50・1時間書込$20/100万tokenで参考換算します。

既存の`--capture-quota`は、statusLine入力に`cost.total_cost_usd`があれば`CLI概算: $...`も表示します。quota未提供でも金額は表示できます。この値は`reported_cost`へセッション累計として保存し、ノートに参考換算と併記します。同じ通知を繰り返し受け取っても加算しません。quota・金額とも未提供なら空表示です。設定方法は従来と同じです。

Claude Codeの本文冒頭にも共通形式のUSD換算サマリを表示し、回答calloutには同じユーザー発話内の連続する同一モデルの回答に対応するtoken数・USD参考換算を表示します。遅れて書き込まれたUsageは既存calloutへ反映します。Codex・Claude Code・OpenCode・Piで、換算できない金額は`換算不可`、既知モデル分だけの金額は`部分換算`と表示します。金額は小数桁を丸めず、固定小数で出力します。

OpenCodeのUsageはSQLiteのassistantレコードにある`tokens`をmessage IDごとに集計します。本文のないツール専用レコードもfrontmatterの合計に含めます。OpenCodeのUSD参考換算はすべて共通単価表で計算し、`cost`は別の取得コストとして併記します。明示的な0も取得値として保存します。モデルごとの単価は他のUsage対応saverと共通です。GPT系の参考換算ではPiと同じく、1リクエストの入力総数が272Kを超えた場合に入力・キャッシュ系2倍、出力1.5倍を適用します。長文料金で参考換算した回答には`⚠︎ 272K+`を付けます。CLIから取得した`cost`にはこの倍率を掛けません。無料モデルも無料利用の請求額ではなく、対応する有料モデルの単価を参考値として換算します。`cost: 0`もCLI報告値であり、請求額の保証ではありません。単価未登録モデルは参考換算を省略し、取得値があればその金額を表示します。本文先頭にはCodexと同じ形式で合計・モデル別のUSD換算サマリを表示します。OpenRouter残高APIには接続しません。

OpenCodeの連続する同一モデルの回答は、途中経過と最終回答を1つのcalloutにまとめます。本文のないassistantのツール専用レコードは、同じユーザー発話内で最も近い回答calloutのUsageへ加算します。frontmatterのUsageにはすべてのassistantレコードを含みます。

## 出力例

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
  total_cost_usd: 0.13
  by_model:
    "gpt-6.1-sol":
      total_tokens: 128000
      cost_usd: 0.13
---

📊 **Quota**:
- **Codex**: W: 94.0% ➔ 91.0%

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

Claude CodeのquotaはstatusLine設定があり、入力に`rate_limits`が含まれる場合だけ記録します。Antigravityでは公式の`/quota`出力が提供する範囲
（アカウントによってweekly、5hなど）を記録します。5h quotaが提供されないアカウントでは
その項目を省きます。新しい形式で更新したMarkdownでは、旧形式の数値`quota`・`quota_claude`
を除去します。過去のファイルを一括変換する処理は行いません。

## このプロジェクトについて

このプロジェクトは各CLI、ChatGPT、Antigravity、Claude CodeおよびObsidianの非公式ツールです。各サービスの開発元とは関係ありません。

### USD参考換算と取得コストの併記（4.0.0）

OpenCode・Pi・Claudeは共通単価表による`usage.total_cost_usd`と、CLIが返した`reported_cost.usd`を別々に保存・表示します。0、未取得、部分取得を区別します。ClaudeはstatusLineのセッション累計の最新観測値を保存し、回答ごとの金額へ推測配分しません。OpenCodeの3.xからは`usage.total_cost_usd`の意味が変わるため4.0.0としました。過去ノートの一括更新は行いません。

Codex用の読取専用`compare_codex_telemetry.py`も追加しました。既存Claude照合ツールに加え、Codexの完了イベントとtranscriptを比較できます。使い方、金額の保存形式、OTelの負荷・送信内容・運用上の注意は[金額併記とCodex telemetry](./docs/costs-and-codex-telemetry.md)を参照してください。

## セッション全履歴の再構成

`rebuild_session.py` はセッションIDから元の履歴を探し、現在の保存スクリプトで会話本文・Usage・USD換算を全件再生成します。Codex・Claude Code・Antigravity・OpenCodeの4つの `*_save.py` と同じディレクトリに置いて実行してください。通常hookの配置変更は不要です。Piはこの再構成ツールの対象外です。

置換前の確認には`--dry-run`を追加します。隔離領域で再生成し、現在のノートと予定内容の件数・token・金額・本文差分をJSONで表示します。ノート・state・バックアップは変更しません。実際の置換時には元履歴やCLI報告値が変わる場合があるため、直前に確認してください。

```bash
/usr/bin/python3 rebuild_session.py claude SESSION_ID --obsidian --dry-run --pricing preserve
```

動作テスト用の出力先を指定する例です。指定先へMarkdownを直接保存し、通常のObsidianノート・stateは更新しません。

```bash
/usr/bin/python3 rebuild_session.py codex SESSION_ID --output-dir /private/tmp/obsidian-save-test
```

通常のObsidian保存先を更新する場合は `--obsidian` を指定します。

```bash
/usr/bin/python3 rebuild_session.py codex SESSION_ID --obsidian
/usr/bin/python3 rebuild_session.py claude SESSION_ID --obsidian
/usr/bin/python3 rebuild_session.py agy SESSION_ID --obsidian
/usr/bin/python3 rebuild_session.py opencode SESSION_ID --obsidian
```

通常保存先は各保存スクリプトの `OBSIDIAN_VAULT`、`OBSIDIAN_OUTPUT_DIR`、`OBSIDIAN_SOURCE` に従います。stateに保存先がある場合は、その既存パスを優先します。既存ノートはファイル名・作成日時・sourceを引き継いで置換し、通常hookのstateも更新します。置換前の内容はノートと同じフォルダの `.rebuild-backups/` に日時付き `.bak` として保存し、そのパスを表示します。再構成は本文への手動追記も置き換えます。

USD単価を変更した場合は、`shared/usage_prices.py.inc`を更新し、`/usr/bin/python3 sync_shared.py`で各保存スクリプトへ反映してから実行してください。OpenCodeで元の回答に正の `cost` がある場合は、通常保存と同様にその値を優先します。

自動探索する場所は、Codexの `~/.codex/sessions/` と `~/.codex/archived_sessions/`、Claude Codeの `~/.claude/projects/`、Antigravityの `~/.gemini/antigravity-cli/brain/SESSION_ID/.system_generated/logs/transcript.jsonl` です。Codexは `CODEX_HOME`、Claude Codeは `CLAUDE_CONFIG_DIR` で探索元を変更できます。JSONLを直接指定することもできます。Codex・Claude Codeではログ内のセッションIDの完全一致を確認します。

```bash
/usr/bin/python3 rebuild_session.py codex SESSION_ID --transcript /path/to/rollout.jsonl --obsidian
```

OpenCode v2は `~/.local/share/opencode/opencode.db` を読み取り専用で開きます。`OPENCODE_DATABASE` または `--database /path/to/opencode.db` で変更できます。

履歴が見つからない場合や保存対象の本文がない場合は、既存ノートを置換せず終了コード1を返します。元のJSONL・SQLiteに残っている会話だけが対象です。Claude Code・Antigravityの過去quotaは既存ノート・stateにある観測値を引き継ぎ、Antigravityの現在quotaは取得しません。Codexのquotaは元のJSONLから再集計します。出力先は `--output-dir` と `--obsidian` のどちらかを必ず指定してください。

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

## 実装上の特徴

- **ノイズの少ないログ保存**: ユーザーおよびエージェントの発言だけを抽出し、ツール呼び出しや実行結果を除外します。
- **増分管理**: CodexとClaude Codeは`last_line`、Antigravityは`last_id`で読み込み済み位置を管理します。
- **cursorとstateの自動復旧**: Markdown内のcursorを正としてstateとの不一致を補正し、state消失時はfrontmatterの`session_id`から既存Markdownを探索します。
- **内容変更時だけノート更新**: 保存対象の本文がない新規セッションではノートを作らず、同じ内容の再保存で`modified`だけを変更しません。
- **Atomic書き込み**: Markdownとstateを同一ディレクトリの一時ファイルへ書いてからrenameします。
- **同時実行への対応**: セッション単位のファイルロックでhookの重複実行を防ぎます。
- **安全なメタデータとファイル名**: YAML文字列、session ID、プロジェクト名を安全な形式に変換します。
- **古いstateのクリーンアップ**: 30日間利用されていないstateファイルを自動的に削除します（非ブロッキングロックで実行中セッションを保護）。

## 謝辞

Markdownの出力形式では、[Obsidian AI Exporter](https://github.com/sho7650/obsidian-AI-exporter)のYAML frontmatter、ユーザー発言の見出し、Calloutを使った会話表現を参考にしました。優れたアイデアを公開されている開発者に感謝します。

## バージョン管理

このソースのバージョンは4.2.0です。リリースはリポジトリ全体で共通のバージョン番号を使用し、[Semantic Versioning](https://semver.org/lang/ja/)に従います。各スクリプトの`__version__`、ルートの[VERSION](./VERSION)、Gitタグは同じ値です。

## ライセンス

このプロジェクトは[MIT License](./LICENSE)の下で公開されています。

構造・堅牢性・Claude対応の検証結果と今後の改善案は、[2026-10-25の検証報告](docs/saver-audit-2026-10-25.md)を参照してください。

3.13.0ではCodexの単一snapshot解析、価格版を選んだ再構成、Claudeの子Usage・API失敗保存、読取専用doctor、任意の公式telemetry照合を追加しました。[使い方・検証結果](docs/saver-improvements.md)を参照してください。
