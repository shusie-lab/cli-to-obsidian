# Piの導入

Piの会話をObsidian保管庫へ保存する手順です。

Pi 1.0以降では、TypeScript extensionからmacOS標準Pythonの単独スクリプトへ保存を依頼します。グローバルextensionとして設置すると全プロジェクトで有効になります。既存のextensionは残してください。


## 準備

macOS、Python 3.9以上、Obsidian、対象のCLIまたはアプリが必要です。PythonはmacOS標準の`/usr/bin/python3`を使用し、追加パッケージは不要です。

下のファイルリンクを開き、GitHubの **Download raw file** から`~/Downloads`へ保存してください。コマンド例の`$HOME/obsidian`は実際のObsidian保管庫のパスに置き換えます。既存の設定ファイルは上書きせず、このツールに必要な項目だけ追加してください。

## 1. ファイルを配置する

[pi_save.py](../../pi_save.py)と[pi_obsidian.ts](../../pi_obsidian.ts)をダウンロードし、配置します。

```bash
mkdir -p ~/.pi/agent/scripts ~/.pi/agent/extensions
cp "$HOME/Downloads/pi_save.py" ~/.pi/agent/scripts/pi_save.py
cp "$HOME/Downloads/pi_obsidian.ts" ~/.pi/agent/extensions/pi_obsidian.ts
```

## 2. 保管庫を指定して起動する

保管庫を指定して、同じターミナルからPiを起動します。

```bash
export OBSIDIAN_VAULT="$HOME/obsidian"
pi
```

Piを再起動するか`/reload`を実行します。既定の保存先は`生成AI/ChatLog/pi/`です。`OBSIDIAN_VAULT`、`OBSIDIAN_OUTPUT_DIR`、`OBSIDIAN_SOURCE`はPi起動時の環境を引き継ぎます。saverのパスは`PI_OBSIDIAN_SCRIPT`、state・ログのディレクトリは`PI_OBSIDIAN_STATE_DIR`で変更できます。

## 保存されるタイミングと内容

自動リトライ・圧縮・キュー処理まで終了した`agent_settled`、分岐切替、セッション終了時に保存します。本文は現在の分岐、Usageのセッション合計は全分岐が対象です。分岐切替時は同じノートを再構成するため、本文への手動追記は次回保存で置き換わります。

## 一時保存先での試験

2ファイルを置いたディレクトリから、一時保存先で試す例です。指定モデルを利用できる認証設定が必要で、1ターンの利用が発生します。グローバルextensionとして設置している場合は、同じextensionを重ねて読み込まないでください。

```bash
OBSIDIAN_VAULT=/private/tmp/pi-obsidian-test/vault \
PI_OBSIDIAN_STATE_DIR=/private/tmp/pi-obsidian-test/state \
PI_OBSIDIAN_SCRIPT="$PWD/pi_save.py" \
pi --extension "$PWD/pi_obsidian.ts" --provider openai --model gpt-6-luna -p "OKとだけ回答してください。"
```

## 次に確認すること

設定後にツールを再起動して1ターン会話し、指定した保管庫の`生成AI/ChatLog/pi/`にMarkdownが作成されることを確認します。作成されない場合は[動作確認とトラブルシューティング](../troubleshooting.md)を参照してください。

保存先や名前を変える場合は[共通設定](../configuration.md)、記録項目は[記録仕様](../usage-spec.md)、他のツールは[README](../../README.md#対応ツール)を参照してください。
