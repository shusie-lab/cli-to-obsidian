# 動作確認とトラブルシューティング

保存されない、値が入らないなどの場合の確認方法です。導入手順は[README](../README.md#対応ツール)の対応表から開けます。

設定後にCLIまたはアプリを再起動し、1ターン会話して、保管庫の`生成AI/ChatLog/<ツール名>/`にMarkdownが作られることを確認します。

| 症状 | 確認すること |
| --- | --- |
| ノートが作られない | hookの登録・信頼、プラグインの読み込み、スクリプトの配置先を確認します。本文がない新規セッションでは空ノートを作りません。 |
| 別の保管庫に保存される | hookコマンドまたはCLIの起動環境の`OBSIDIAN_VAULT`を確認します。省略時は`~/obsidian`です。 |
| 金額・quota・所要時間がない | 元の履歴やstatusLineに必要な値があるか確認します。未取得の値を0とは扱いません。 |
| 更新時刻が変わらない | 同じ内容の再通知ではノートを更新しません。新しい発言も反映されない場合はログとdoctorを確認します。 |
| 過去の会話を保存し直したい | [セッション全履歴の再構成](rebuild.md)で、差分確認後に再生成できます。OpenCodeのプラグイン初回走査は直近10分のみです。 |

## ログの場所

ログの既定位置は次のとおりです。`DEBUG=1`をhookの起動環境に設定すると詳細ログを出力します。

| ツール | 保存ログ |
| --- | --- |
| Codex | `~/.codex/codex-obsidian/log/codex_obsidian_save.log` |
| Claude Code | `~/.claude/claude-obsidian/log/claude_obsidian_save.log` |
| Antigravity | `~/.gemini/agy-obsidian/log/obsidian_save.log` |
| OpenCode | `~/.local/state/opencode-obsidian/opencode_obsidian_save.log` |
| Pi | `~/.local/state/pi-obsidian/pi_obsidian_save.log`（`PI_OBSIDIAN_STATE_DIR`で変更可能） |

## 保存状態の診断（doctor）

[saver_doctor.py](../saver_doctor.py)と比較元の最新`*_save.py`を同じディレクトリに置き、そこで実行します。診断対象は`codex`・`claude`・`agy`・`opencode`・`pi`です。

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

## 更新・停止

更新時は配置済みファイルをバックアップし、新しいファイルで置き換えてCLIを再起動します。OpenCode・PiはPythonとプラグイン／extensionの両方を同じ版に揃えてください。Codexのhook定義も変更した場合は`/hooks`で再度信頼します。

保存を止める場合は、このツールのhook項目・プラグイン／extension登録を外して再起動します。ClaudeのstatusLineへ設定した保存コマンドも対象です。既存の会話ノートは残ります。
