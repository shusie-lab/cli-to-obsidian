# 保存基盤の改善（3.13.0）

2026-10-25の検証報告に挙げた5案を実装した。
開始時点の3.12.0は`00382d3`でコミット済み。
各hookは引き続き標準ライブラリだけの単独Pythonファイルとして動作する。

## 1. Codexのsnapshotと解析分割

Stopの最初にJSONLを一度読み、行番号を保ったsnapshotを作る。
metadata・quota・Usage・本文はそのsnapshotから計算する。
不完全な末尾は次回へ保留し、途中の不正JSONや非オブジェクトは正常行の保存を妨げない。
子transcriptは各ファイルのsnapshotであり、複数ファイルを同時刻に固定する保証はない。

旧`load_jsonl_messages`約200行は、イベント形式変換、完了時刻、表示metadata、
ターン状態、Usage割当の関数へ分割した。公開関数は14行で、ターン状態の処理は71行。
同一ターン内のUsage割当には二分探索を使う。

Stop解析工程相当の合成履歴ベンチマーク（5,000ターン、20,000行）:

| 測定 | 3.12.0 | 3.13.0 |
| --- | --- | --- |
| 3回の中央値 | 1.599秒 | 0.345秒 |
| tracemallocのピーク | 71.73MiB | 38.69MiB |
| 表示メッセージ数 | 10,000 | 10,000 |
| 最終cursor | 20,000 | 20,000 |
| 合計token | 550,000 | 550,000 |

約78%の時間短縮、約46%の追跡メモリ削減。計測日は2026-10-25。
tracemallocはPython割当の追跡であり、OS全体のRSSではない。
実CLI、描画・ファイル更新、子の探索、Obsidian反映は測定範囲外。
旧処理のpreview・増分・Usage更新・時間更新の4回解析を再現して比較している。

再現:

```bash
git show 00382d3:codex_save.py > /private/tmp/codex-before-proposals.py
/usr/bin/python3 tests/benchmark_codex_snapshot.py --baseline /private/tmp/codex-before-proposals.py
```

## 2. 価格版・出典と再構成

Usage対応4本のノートに、`usage.price_version`・`price_recorded_at`・
`price_checked_at`・`price_sources`・`price_effective_from`を保存する。
現在の価格版は`3.12.0`。saverの機能バージョンとは独立している。
過去表`3.11.1`も埋め込んだ。Sonnetのread単価はその表で$0.20、現在表で$0.10/MTok。

価格の正確な適用開始日を確認できていないため`price_effective_from`はnull。
`recorded_at`は価格表の記録日で、`checked_at`は確認済みモデルの確認日。
未確認モデルの日付はnullとして、今回全providerの料金を再確認したようには記録しない。

```bash
# 既存ノートが記録する価格版で、通常保存先をバックアップ付き再構成
/usr/bin/python3 rebuild_session.py claude SESSION_ID --obsidian --pricing preserve

# 価格版を持たない過去ノートには、使う登録表を明示する
/usr/bin/python3 rebuild_session.py claude SESSION_ID --output-dir /private/tmp/rebuilt --price-version 3.11.1

# 現在の単価で再構成（従来どおり既定）
/usr/bin/python3 rebuild_session.py codex SESSION_ID --output-dir /private/tmp/rebuilt --pricing current
```

`preserve`は置換対象のノートを参照する。`--output-dir`ではその出力先の既存ノートが対象。
価格版がないとき・未登録版のときは処理を中止する。日付から単価を推測しない。
通常hookは現在表を使うため、稼働中セッションを過去表で再構成した後の次回hookでは
現在表に戻る。過去価格を固定した検証結果は別の`--output-dir`に保存する。

確認候補との単価差分は次のツールでレビューできる。
JSONの`rates`は全モデルを含む完全な候補表として扱い、省略したモデルは削除差分になる。

```bash
/usr/bin/python3 check_usage_prices.py > /private/tmp/current-prices.json
# 公式出典を確認して候補JSONを作成した後
/usr/bin/python3 check_usage_prices.py --candidate /private/tmp/candidate-prices.json
```

終了コードは差分なし0、差分あり1、入力エラー2。候補を自動採用・Web取得する処理はない。
将来の価格変更では、旧表を独立した履歴として残し、価格版と確認日・出典を更新する。

## 3. Claudeの子Usage・API失敗

既存Stopに加え、同じ`claude_save.py`を`SubagentStop`と`StopFailure`でも呼ぶ。
[公式hook仕様](https://code.claude.com/docs/en/hooks)で確認した入力を使用する。
ユーザーの実際のsettings.jsonは今回変更していない。

```json
{
  "hooks": {
    "SubagentStop": [{"hooks": [{"type": "command", "command": "/usr/bin/python3 ~/.claude/claude-obsidian/scripts/claude_save.py"}]}],
    "StopFailure": [{"hooks": [{"type": "command", "command": "/usr/bin/python3 ~/.claude/claude-obsidian/scripts/claude_save.py"}]}]
  }
}
```

既存の`hooks`内へイベント項目を追加する。保管庫を環境変数で指定している場合は
既存Stopと同じ指定を使う。既存の別hookを上書きしない。
`SubagentStop`では子の最終回答・Usageの遅延書き込みを最大2秒待つ。
待機後の子snapshotを同じ保存処理へ渡す。

子の入力は`親transcript拡張子なし/subagents/agent-*.jsonl`以下だけを読む。
sessionIdが別の行やsymlinkは集計しない。子ごと・message IDごとの最後のUsageを数える。
親の既知UUIDを指す`parentUuid`が一つの親ターンに結び付く場合だけ、そのターンの
最後の回答に付ける。リンクがない場合や曖昧な場合もセッション累計には含める。
現在ターンや近い時刻から親を推測しない。子の本文を親の会話へ混ぜない。
通常Stopでも同じ子フォルダを読み、state喪失後も再集計できる。

`StopFailure`は通常回答との一致待ちをせず、途中の本文・Usageと
`cli_failure.type`・`observed_at`を記録する。本文がない段階でもstateに失敗種別を残す。
認証情報が混ざり得る`error_details`・エラー文そのものは記録しない。
最後の失敗はその後の成功時も履歴として保持する。

## 4. 読取専用doctor

全5本がstate配下`.diagnostics/*.status`へ最終試行、成功、最後の例外の型、
使用したsaver版を記録する。例外メッセージと会話本文は含めない。
診断書込みの失敗は通常の保存を妨げない。古いstateの削除時に対応する診断も削除する。
stateを作成する前に失敗した場合の診断は、自動削除せず原因確認用に残る。
成功は処理後に出力ファイルが存在し、state更新が完了した観測を意味する。
失敗は例外またはERRORログを観測したもの。quota専用呼出しは対象外。
過去の未観測エラーを遡って復元する機能ではない。

```bash
/usr/bin/python3 saver_doctor.py codex --json
/usr/bin/python3 saver_doctor.py claude --session-id SESSION_ID
```

source/配置版のversion・SHA、出力先権限、state/ノートcursor、元ログの行数、
最終成功・最後の失敗を表示する。配置場所が標準外なら`--installed`、stateが標準外なら
`--state-dir`を指定する。Piの既定配置候補も実際の拡張構成と異なる場合は指定する。
4.1.0ではPiの既定配置先をREADMEどおり`~/.pi/agent/scripts/pi_save.py`へ修正した。
保存を見送ると、`skip_reason`に`transcript_missing`・`no_content`・`unchanged`などの理由を残す。
`unchanged`は正常な再通知なのでdoctorの問題件数には含めない。
配置版の検査はPythonを実行せずASTで読む。doctor自身はsaverをimportせず、
ログ・ロック・state・ノートを作成/更新しない。cursorも自動修復しない。
正常0、差異/問題あり1、引数/読取エラー2。`transcript_ahead`は保存後の追記でも発生する。

## 5. 公式telemetryとの任意照合

[公式Monitoring](https://code.claude.com/docs/en/monitoring-usage)の
`claude_code.api_request`を対象とする。OTLP JSONの`resourceLogs/scopeLogs/logRecords`、
その配列、属性を展開したイベントJSONLを読める。
CollectorのJSONログ出力等から得たファイルを指定する。Python repr形式のconsole出力や
protobufのバイナリをそのまま入力することはできない。

```bash
/usr/bin/python3 compare_claude_telemetry.py \
  --telemetry /private/tmp/claude-otel.json \
  --transcript /path/to/SESSION_ID.jsonl \
  --session-id SESSION_ID --include-subagents
```

公式`request_id`とtranscriptの`requestId`で照合し、同じrequestの再配送は一回だけ数える。
session.idで対象を限定し、api_errorや累積counterを成功Usageへ加算しない。
モデル・通常入力・cache read・cache creation・出力を別々に比較し、USDは0.000001の許容差を使う。
TTL内訳が不明等の換算不可、IDなし、片側のみのrequest、データなしも結果に表示し、
一致とは扱わない。`--price-version`で価格版を指定できる。
正常一致0、差異/照合不足1、不正入力2。ノートの金額は変更しない。

常設collectorや通信をhookの必須条件にしていない。telemetryの有効化・送信先設定は
公式の環境変数設定で任意に行う。本ツールがネットワーク送信を行うことはない。
認証なしのローカル照合に加え、将来の実契約環境で同じ手順を使える。

## 検証範囲

旧形式・新形式、遅延Usage、途中追記、子Usage、価格再構成、state消失、I/O失敗、
並行した独立プロセス、OTLPの再配送・別session、5本の単独起動をテストする。
新機能についてClaudeの実契約・有料APIを使った検証は行っていない。
公式文書準拠の合成fixtureであり、実機版ごとの入力差異は今後採取するログで補う。

検証結果（最終ソース、Python 3.9.6）:

- `/usr/bin/python3 -m unittest discover -s tests -v`: 229テスト成功。
- `/usr/bin/python3 sync_shared.py --check`: 成功。
- `/usr/bin/python3 release_public.py --check`: 成功。
- `git diff --check`: 成功。
- doctor/telemetry/価格差分CLIの起動、現行価格表と同じ候補の差分なしを確認。
- 実装は3.13.0として準備。開始時点のコミット以降の変更は未コミットで、hook配置・公開・pushは未実施。

### 実装後の再レビュー

既存225テストの成功を確認後、境界条件を見直し、次を修正した。

- Codexの`item.type`が配列等の場合、辞書検索の例外で処理が止まる。型を検証し、不正イベント後の正常メッセージも保存する。
- Claudeの子Usageの親ターン判定に別セッションの行が混入する。Usage集計と同じsessionId条件で親リンクを判定する。
- telemetry照合がモデル不一致を見逃し、不正なOTLP階層で未処理例外になる。モデル比較と階層の型検証を追加した。
- doctorが`opencode_v2_cursor.json`をセッション状態として誤診断する。既知のbridge管理ファイルを対象外にした。

上記を4件の回帰テストで確認した。実際のClaude契約環境でのhook入力・telemetry収集は引き続き未検証。
