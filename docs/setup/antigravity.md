# Antigravity CLIとアプリの導入

Antigravityの会話をObsidian保管庫へ保存する手順です。

Antigravity CLIおよびAntigravityアプリのStop hookから会話履歴を保存します。
quotaは、公式の読み取り専用コマンド `agy --output-format json --print='/quota'`
から取得します。このコマンドは会話ターンを作らず、quotaを消費しません。`agy` が
見つからない、または quota を返さない場合でも、会話保存は継続します。


## 準備

macOS、Python 3.9以上、Obsidian、対象のCLIまたはアプリが必要です。PythonはmacOS標準の`/usr/bin/python3`を使用し、追加パッケージは不要です。

下のファイルリンクを開き、GitHubの **Download raw file** から`~/Downloads`へ保存してください。コマンド例の`$HOME/obsidian`は実際のObsidian保管庫のパスに置き換えます。既存の設定ファイルは上書きせず、このツールに必要な項目だけ追加してください。

## 1. ファイルを配置する

[agy_save.py](../../agy_save.py)をダウンロードし、配置します。

```bash
mkdir -p ~/.gemini/agy-obsidian/scripts
cp "$HOME/Downloads/agy_save.py" ~/.gemini/agy-obsidian/scripts/agy_save.py
```

## 2. hookを登録する

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

## 次に確認すること

設定後にツールを再起動して1ターン会話し、指定した保管庫の`生成AI/ChatLog/antigravity-cli/`にMarkdownが作成されることを確認します。作成されない場合は[動作確認とトラブルシューティング](../troubleshooting.md)を参照してください。

保存先や名前を変える場合は[共通設定](../configuration.md)、記録項目は[記録仕様](../usage-spec.md)、他のツールは[README](../../README.md#対応ツール)を参照してください。
