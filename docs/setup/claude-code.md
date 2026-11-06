# Claude Codeの導入

Claude Codeの会話をObsidian保管庫へ保存する手順です。

Claude Code CLIのStop hookから会話履歴とtoken使用量を保存します。quotaを記録する場合は、同じスクリプトをstatusLineからも呼び出します。契約のない環境ではquotaの実データを検証できていません。


## 準備

macOS、Python 3.9以上、Obsidian、対象のCLIまたはアプリが必要です。PythonはmacOS標準の`/usr/bin/python3`を使用し、追加パッケージは不要です。

下のファイルリンクを開き、GitHubの **Download raw file** から`~/Downloads`へ保存してください。コマンド例の`$HOME/obsidian`は実際のObsidian保管庫のパスに置き換えます。既存の設定ファイルは上書きせず、このツールに必要な項目だけ追加してください。

## 1. ファイルを配置する

[claude_save.py](../../claude_save.py)をダウンロードし、配置します。

```bash
mkdir -p ~/.claude/claude-obsidian/scripts
cp "$HOME/Downloads/claude_save.py" ~/.claude/claude-obsidian/scripts/claude_save.py
```

## 2. Stop hookを登録する

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

## 3. 子Usageや失敗時の保存（任意）

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

## 4. quotaを記録する（任意）

quotaも記録する場合は、`~/.claude/settings.json`の同じトップレベルに次を追加します。既存のstatusLine設定がある場合は、そのコマンドに`OBSIDIAN_VAULT="$HOME/obsidian" /usr/bin/python3 ~/.claude/claude-obsidian/scripts/claude_save.py --capture-quota`を組み込んでください。入力はClaude Codeが渡すJSONです。statusLineにはquotaと、提供される場合はCLIのUSD概算が表示されます。

```json
{
  "statusLine": {
    "type": "command",
    "command": "OBSIDIAN_VAULT=\"$HOME/obsidian\" /usr/bin/python3 ~/.claude/claude-obsidian/scripts/claude_save.py --capture-quota"
  }
}
```

## 次に確認すること

設定後にツールを再起動して1ターン会話し、指定した保管庫の`生成AI/ChatLog/claude-code/`にMarkdownが作成されることを確認します。作成されない場合は[動作確認とトラブルシューティング](../troubleshooting.md)を参照してください。

保存先や名前を変える場合は[共通設定](../configuration.md)、記録項目は[記録仕様](../usage-spec.md)、他のツールは[README](../../README.md#対応ツール)を参照してください。
