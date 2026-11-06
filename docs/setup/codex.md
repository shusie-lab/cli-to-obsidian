# Codex CLIとChatGPTデスクトップアプリの導入

Codexの会話をObsidian保管庫へ保存する手順です。

Codex CLIのhooks機構を利用します。ChatGPTデスクトップアプリ内のCodexも、transcriptの形式に応じて同じスクリプトで保存できます。


## 準備

macOS、Python 3.9以上、Obsidian、対象のCLIまたはアプリが必要です。PythonはmacOS標準の`/usr/bin/python3`を使用し、追加パッケージは不要です。

下のファイルリンクを開き、GitHubの **Download raw file** から`~/Downloads`へ保存してください。コマンド例の`$HOME/obsidian`は実際のObsidian保管庫のパスに置き換えます。既存の設定ファイルは上書きせず、このツールに必要な項目だけ追加してください。

## 1. ファイルを配置する

[codex_save.py](../../codex_save.py)をダウンロードし、配置します。

```bash
mkdir -p ~/.codex/codex-obsidian/scripts
cp "$HOME/Downloads/codex_save.py" ~/.codex/codex-obsidian/scripts/codex_save.py
```

## 2. hookを登録する

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

## 保存先とsource名

CodexのCLIとアプリは会話を共有できるため、transcriptの起動元にかかわらず、既定では`codex-cli/`に保存し、frontmatterの`source: codex-cli`とタグ`codex-cli`を記録します。端末別に区別する場合は[共通設定](../configuration.md)の`OBSIDIAN_SOURCE`を指定してください。

## 次に確認すること

設定後にツールを再起動して1ターン会話し、指定した保管庫の`生成AI/ChatLog/codex-cli/`にMarkdownが作成されることを確認します。作成されない場合は[動作確認とトラブルシューティング](../troubleshooting.md)を参照してください。

保存先や名前を変える場合は[共通設定](../configuration.md)、記録項目は[記録仕様](../usage-spec.md)、他のツールは[README](../../README.md#対応ツール)を参照してください。
