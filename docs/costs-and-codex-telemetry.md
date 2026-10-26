# USD参考換算とCLI報告コスト（4.0.0）

## 金額の併記

Codex・Claude・OpenCode・Piの`usage.total_cost_usd`とモデル別`cost_usd`は、
共通単価表とtranscript等のtoken数から計算したUSD参考換算額。
`usage.cost_basis: "reference"`で意味を明示する。既存の価格版選択にも従う。

OpenCode・Pi・Claudeは、CLIから受け取った金額も別の`reported_cost`に保存する。
両者は独立した値として表示し、合算しない。

| CLI | 取得元 | scope | 集計方法 |
| --- | --- | --- | --- |
| OpenCode | assistantの`cost` | `message_sum` | message IDで整理した回答の合計 |
| Pi | assistantの`usage.cost.total` | `message_sum` | entry IDで整理した全分岐の回答の合計 |
| Claude | statusLineの`cost.total_cost_usd` | `session` | 最新観測値で置換。通知ごとに加算しない |

Piのノート本文は現在の分岐、累計は全entryという既存の集計範囲を維持する。
Claudeの取得値はセッション累計なので、回答別の差分へ推測配分しない。
Claudeの公式仕様ではこの金額は定価または`modelPricing`に基づくクライアント側概算で、
実際の請求額と異なる場合がある。
[Claude公式statusLine仕様](https://code.claude.com/docs/en/statusline#available-data)

例えば次のようにノート冒頭に表示する。

```text
💰 Usage (USD換算): Total: $0.000015 (...)
💵 取得コスト (CLI報告値): $7.5（回答の合計）
```

OpenCode・Piの回答calloutにも`取得コスト: $...`を併記する。
明示的な0は`$0`、未提供・不正値は`未取得`、一部の回答だけ取得できた場合は
`部分取得`として区別する。負数・bool・非有限の金額は採用しない。
モデルが未登録でも取得コストがあれば表示できる。

```yaml
usage:
  cost_basis: "reference"
  # token数・価格版等は従来どおり
  total_cost_usd: 0.000015
reported_cost:
  source: "opencode.cost"
  scope: "message_sum"
  usd: 7.5
  available_records: 1
  total_records: 1
```

Claudeは`source: "claude.statusLine.cost.total_cost_usd"`、`scope: "session"`、
`observed_at`を記録する。quotaが未提供でもコストを保存する。
statusLineが先に実行された場合はstateに保持し、会話ノート作成時に反映する。
同じ値の再通知では最初の観測時刻を保持する。state喪失後はノートの最後の取得値を復旧する。
再構成でも既存stateまたはノートの観測値を保持し、過去の取得額を現在の単価で再計算しない。

## 3.xからの変更点

OpenCodeの3.xは正の`cost`を`usage.total_cost_usd`へ優先採用していた。
4.0.0ではこの項目を参考換算額へ統一し、取得値は`reported_cost.usd`に保存する。
意味の互換性が変わるためmajorバージョンを更新した。

過去ノートの一括更新は行わない。通常のhook保存で対象となったセッションと、
利用者が明示的に再構成したノートだけに新方式を適用する。
3.xのstateしか残っていない部分同期では、旧金額が参考換算か取得値か判別できないため、
token数から参考換算し直し、取得値は未取得とする。完全なsnapshotを受信すれば取得値も復元できる。

## Codexのtelemetry照合

`compare_codex_telemetry.py`はOTLP JSONログ（`resourceLogs/scopeLogs/logRecords`）、
その配列、属性を展開したJSONLと、Codex transcriptをローカルで照合する。
同じディレクトリに`compare_claude_telemetry.py`、`rebuild_session.py`、`codex_save.py`も置く。
Claude照合ツールの動作は変更していない。

```bash
/usr/bin/python3 compare_codex_telemetry.py \
  --telemetry /private/tmp/codex-otel.json \
  --transcript /path/to/rollout-SESSION_ID.jsonl \
  --session-id SESSION_ID
```

`codex.sse_event`または`codex.websocket_event`の`response.completed`を使い、
`conversation.id`で対象を限定する。input・cache read・cache write・output・reasoningを比較し、
cacheはinputの内数、reasoningはoutputの内数として検証する。
transcriptはリクエスト単位の`token_usage_record`を使う。累積`thread_token_usage`は加算しない。
`--include-subagents`でsession_metaの親子関係が確認できる子も対象にする。

既定の`--group-by response`は`response_id`または`response.id`がある場合のリクエスト照合。
取得したCLI版のログにresponse IDがない場合は、範囲を明示して次の集計比較を選択できる。

```bash
# turn.idとモデルごとのリクエスト数・token合計
/usr/bin/python3 compare_codex_telemetry.py --telemetry /private/tmp/codex-otel.json \
  --transcript /path/to/rollout-SESSION_ID.jsonl --session-id SESSION_ID --group-by turn

# セッション内のモデル別集計。同一turn内の入れ替わりまでは検出できない
/usr/bin/python3 compare_codex_telemetry.py --telemetry /private/tmp/codex-otel.json \
  --transcript /path/to/rollout-SESSION_ID.jsonl --session-id SESSION_ID --group-by model
```

response IDで同じ完了通知の再配送を一回にまとめる。IDがなくてもOTLP元timestampがあれば
そのtimestamp・trace/spanで再配送を識別する。識別情報が全くないイベントは照合不足とする。
同一IDで内容が違うtelemetry、transcript内の重複response ID、token内訳の矛盾はエラーにする。
失敗イベント・累積counter・ヒストグラムはUsageへ加算しない。

token属性は`input_token_count`、`cached_token_count`、`cache_write_token_count`、
`output_token_count`、`reasoning_token_count`を使う。
これらの名称はローカルのCodex CLI 0.164.0のバイナリ内でも確認したが、
この実装時点では実際のOTel出力を収集していない。実出力との結合検証は未実施。
古い版等で属性が欠ける場合、0とは推測せず入力エラーとして報告する。
OTLP protobufバイナリ・trace span・Python reprのconsole出力は入力対象外。

USDは両側のリクエストtoken数に共通単価表を適用した参考換算を比較する。
`--price-version`で登録価格版を選択できる。これは集計処理の検証であり、
単価の正しさや請求額を独立した請求データと照合する機能ではない。
tokenが一致しても換算できないモデルがあれば`unpriced_groups`として不足を報告する。

終了コードは一致0、差異・データなし・照合不足1、不正入力2。
出力にtoken集計と比較結果を含め、会話本文・認証情報は出力しない。
ノート・state・CLI設定の変更やネットワーク通信は行わない。

## OTelを有効にする場合のデメリット

[OpenAI公式のOTel仕様](https://learn.chatgpt.com/docs/config-file/config-advanced#observability-and-telemetry)
に基づく注意点と、運用上の判断を以下にまとめる。

- イベントの整形・キュー・送信にCPU、メモリ、通信が追加で必要になる。
  公式では非同期バッチ送信と終了時のflushが説明されている。終了待ちへの影響もあり得るが、
  この環境での負荷増加や待ち時間は未測定。
- Collector・保存先の起動管理、ディスク容量、保持期間、ローテーションの運用が増える。
  外部サービスでは通信・取り込み・保管料金が発生する場合がある。
- 会話ID、モデル、CLI版、承認設定、ツール結果の断片等が送信対象になる。
  `log_user_prompt = false`はプロンプト本文を抑止する設定であり、
  他のログ断片に機密が含まれないという保証ではない。外部送信先の範囲と保持を確認する必要がある。
- 収集の開始時点、終了時flush、再配送、CLI版の属性差でログが不完全になることがある。
  照合不足は課金漏れやsaverの不具合と即断せず、両入力の収集範囲を確認する。

本プロジェクトの保存hookはOTelを必須にしない。検証には、必要な期間だけローカルCollectorへ
収集し、`log_user_prompt = false`と短い保存期間を設定する運用を推奨する。
有効化する場合はCollectorを別途用意し、ユーザー設定`~/.codex/config.toml`で指定する。
例は以下。今回この設定は変更していない。

```toml
[otel]
log_user_prompt = false
exporter = { otlp-http = { endpoint = "http://127.0.0.1:4318/v1/logs", protocol = "json" } }
```

設定しただけでJSONファイルが生成されるわけではない。Collector等でOTLP JSONファイルを
書き出してから照合ツールへ渡す。ログ・traceのexportとOpenAIへのanalytics設定は別なので、
OTel設定だけでanalyticsの送信先や有効/無効を変更したと解釈しない。

## 検証結果

macOS標準`/usr/bin/python3`（3.9.6）で全244テスト成功。
共有コード同期、公開前検証、`git diff --check`も成功。
取得値の0・不正値・部分取得・未登録モデル、Claudeのquotaなし保存・再通知・state復旧・
再構成、会話内コード例の保持、Codexの再配送・ID欠落・モデル差異・子Usageを検証した。
既存のClaude照合ツール本体は変更していない。

実際のObsidianノートとCodexのOTel設定は変更していない。
実際のCodex OTel出力およびClaudeの有料契約環境との結合検証は未実施。
4.0.0としてコミットし、hookへの配置まで完了した。実際のCodex OTel出力およびClaudeの有料契約環境との結合検証は未実施。
