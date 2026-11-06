# telemetryとの照合（任意）

取得済みのOTelログと会話履歴のtoken・金額をローカルで比較する補助ツールです。通常の会話保存にcollectorは不要です。照合ツール自身は通信せず、ノート・state・CLI設定も変更しません。

## 必要なファイルと入力

以下を同じディレクトリに置いて実行します。

| 対象 | 必要なファイル |
| --- | --- |
| Codex | [compare_codex_telemetry.py](../compare_codex_telemetry.py)、[compare_claude_telemetry.py](../compare_claude_telemetry.py)、[rebuild_session.py](../rebuild_session.py)、[codex_save.py](../codex_save.py) |
| Claude | [compare_claude_telemetry.py](../compare_claude_telemetry.py)、[rebuild_session.py](../rebuild_session.py)、[claude_save.py](../claude_save.py) |

入力はOTLP JSONログ（`resourceLogs/scopeLogs/logRecords`）、その配列、または属性を展開したイベントJSONLです。collector等でJSONファイルへ書き出してから渡してください。protobufバイナリ、trace span、Python repr形式のconsole出力は使えません。

## Codex

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

## Claude Code

```bash
/usr/bin/python3 compare_claude_telemetry.py \
  --telemetry /private/tmp/claude-otel.json \
  --transcript /path/to/SESSION_ID.jsonl \
  --session-id SESSION_ID --include-subagents
```

`claude_code.api_request`の`request_id`とtranscriptの`requestId`を照合し、再配送は一回だけ集計します。`session.id`で対象を限定し、APIエラーや累積counterは加算しません。モデル・通常入力・cache read・cache creation・出力を比較し、USDの許容差は0.000001です。`--price-version`で登録版を指定できます。

IDなし、片側だけのrequest、TTL不明等の換算不可、データなしは一致扱いにしません。両ツールとも終了コードは一致0、差異・照合不足1、不正入力2です。実際のCLI版による入力差はあり得ます。Codexの実OTel出力とClaudeの有料契約環境との結合検証は未実施です。

## OTelログを収集する場合

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
