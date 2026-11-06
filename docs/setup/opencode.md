# OpenCodeの導入

OpenCodeの会話をObsidian保管庫へ保存する手順です。

OpenCode v2は会話完了プラグインから保存スクリプトへ渡します。プラグインはOpenCodeのSQLiteデータベース（`~/.local/share/opencode/opencode.db`）に記録された`session_message`の`idle`を監視し、完了したセッションの履歴全体を保存します。グローバルプラグインとして設置すると、OpenCodeの全プロジェクトで有効になります。


## 準備

macOS、Python 3.9以上、Obsidian、対象のCLIまたはアプリが必要です。PythonはmacOS標準の`/usr/bin/python3`を使用し、追加パッケージは不要です。

下のファイルリンクを開き、GitHubの **Download raw file** から`~/Downloads`へ保存してください。コマンド例の`$HOME/obsidian`は実際のObsidian保管庫のパスに置き換えます。既存の設定ファイルは上書きせず、このツールに必要な項目だけ追加してください。

## 1. ファイルを配置する

[opencode_save.py](../../opencode_save.py)と[opencode_obsidian.js](../../opencode_obsidian.js)をダウンロードし、配置します。

```bash
mkdir -p ~/.config/opencode/scripts ~/.config/opencode/plugins
cp "$HOME/Downloads/opencode_save.py" ~/.config/opencode/scripts/opencode_save.py
cp "$HOME/Downloads/opencode_obsidian.js" ~/.config/opencode/plugins/opencode_obsidian.js
```

## 2. 保管庫を指定して起動する

保管庫を指定して、同じターミナルからOpenCodeを起動します。

```bash
export OBSIDIAN_VAULT="$HOME/obsidian"
opencode
```

アプリで利用する場合も、OpenCodeの起動環境に`OBSIDIAN_VAULT`を設定してください。省略した場合は`~/obsidian`を使います。

OpenCodeを再起動するとプラグインが読み込まれます。`OBSIDIAN_VAULT`、`OBSIDIAN_OUTPUT_DIR`、`OPENCODE_DATABASE`はOpenCode起動時の環境変数を引き継ぎます。保存先は`生成AI/ChatLog/opencode/`です。macOS標準の`/usr/bin/sqlite3`が必要です。監視カーソルは`~/.local/state/opencode-obsidian/opencode_v2_cursor.json`に保存され、初回起動時は直近10分を再走査します。

上の`~/.config/opencode/plugins/`への配置では設定ファイルへの追記は不要です。単一の`.js`ファイルを`opencode.jsonc`の`plugins`へ直接指定すると読み飛ばされるため、自動読み込みの配置を使ってください。[OpenCode v2公式プラグイン仕様](https://opencode.ai/v2/docs/plugins/)

## 記録される使用量

OpenCodeが回答レコードに記録したtoken使用量を保存します。OpenRouter残高の取得は行いません。

## 保存スクリプトの場所を変える

保存スクリプトを別の場所へ置く場合は、プラグイン起動前に `OPENCODE_OBSIDIAN_SAVE_SCRIPT` へ絶対パスを指定してください。既定値は `~/.config/opencode/scripts/opencode_save.py` です。

## 次に確認すること

設定後にツールを再起動して1ターン会話し、指定した保管庫の`生成AI/ChatLog/opencode/`にMarkdownが作成されることを確認します。作成されない場合は[動作確認とトラブルシューティング](../troubleshooting.md)を参照してください。

保存先や名前を変える場合は[共通設定](../configuration.md)、記録項目は[記録仕様](../usage-spec.md)、他のツールは[README](../../README.md#対応ツール)を参照してください。
