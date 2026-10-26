# 保存スクリプト検証（2026-10-25）

対象: 3.11.1のソースを調査し、今回の修正を3.12.0として準備。
実行環境: macOS標準 `/usr/bin/python3`、Python 3.9.6。外部パッケージ追加なし。

## 判定

Codex版は、入力解析・Usage・描画・保存の責務が分かれ、共通基盤を生成して
単一ファイルに埋め込む構造になっている。単純に約2,900行あることを理由に
保守困難とは判断しない。約1,800行は共有の生成領域で、正本は`shared/`にある。
共通化の一致・類似関数の重複・単独実行をテストで検査している点は良い。

ただし、Codex固有の`load_jsonl_messages`は約200行あり、世代別イベント解釈、
ターン境界、所要時間、Usage割当が一つの関数に集中する。Stop処理も約100行あり、
「十分整理され、これ以上の改善は不要」とまでは言えない。
堅牢性にも型検証の抜けがあり、今回修正した。テスト成功は全入力・全CLI版への保証ではない。

## 他のsaverへの反映

| 項目 | Codex | Claude | agy | OpenCode | Pi |
| --- | --- | --- | --- | --- | --- |
| state・セッションロック・atomic write | 共通 | 共通 | 共通 | 共通 | 共通 |
| 保存先・ファイル名・既存ノート探索 | 共通 | 共通 | 共通 | 共通 | 共通 |
| 基本Markdown整形・回答描画 | 共通 | 共通 | 共通 | 共通 | 共通 |
| 単価表・USD集計・部分換算表示 | 共通 | 共通 | 未対応 | 共通 | 共通 |
| 更新方式 | 増分 | 増分 | step増分 | 履歴統合・再構成 | 分岐の再構成 |
| 子エージェントUsage | 親ターンへ割当 | 集計対象外 | 未対応 | 入力依存 | 入力依存 |

`sync_shared.py`の登録が正本。方式の違いは入力仕様に由来するため、全saverを
Codexと同じ行cursor方式にすることは勧めない。agyはtoken入力を利用しておらず、
USD機能が全5本で同等という状態ではない。

確認した防御と既存テスト:

- 同一セッションの排他、stateのatomic保存、実行中stateを除いた保持期間処理。
- state喪失時の既存ノート復旧、探索エラー時の新規作成中断。
- 不完全なJSONL末尾のcursor保留、再実行の重複防止、遅延Usageの更新。
- コードフェンスを壊さない描画、回答メタデータだけの更新。
- tokenの内数・モデル別集計・Decimal換算・未知価格の部分換算表示。
- 5本を`shared/`なしで実行するテストと、生成元の同期・AST重複検査。

## 発見事項と今回の対応

| 重要度 | 発見事項 | 対応 |
| --- | --- | --- |
| 高 | CodexのUsage読取とClaudeのUsage読取で、JSONとして正しい`null`・配列等が混入すると`.get`で中断。Codexの他の走査も後続行を取り落とす場合がある | オブジェクト検証を追加し、正常な本文・金額の保存と再実行をテスト |
| 中 | Claudeの`message: null`や非文字列text blockで保存処理が中断する | 型を確認し、正常text blockを保持 |
| 中 | 共有atomic writeでwrite/flush/fsync失敗時に一時パスが未設定となり、tmpが残る | 生成直後にパスを保持。5本でfsync失敗時の原本維持・tmp削除を確認 |
| 中 | Sonnet 5.5のcache readが$0.20/MTokで、現在の公式$0.10/MTokと不一致 | 共通単価表を修正。入力100・read50・5m書込20・1h書込10・出力10なら$0.000395 |
| 中 | 現行Fable 5.1が未登録でUSD換算不可 | 公式単価と別名を追加。ClaudeのTTL別換算と4本の保存・回答表示を検証 |
| 低 | quotaがないClaude statusLineは空表示になる | `cost.total_cost_usd`があれば`CLI概算: $...`を表示。非有限値・負数・boolは無視 |
| 低 | ClaudeのStopで同じsnapshotの会話解析を最大4回繰り返す | 解析済みの全メッセージを増分・Usage更新・時間更新で再利用 |
| 低 | ClaudeのStop待機がsidechain回答を最終回答と誤認する | 本体と同様にsidechainを除外し、不要なタイムアウトを防止 |

ClaudeのUSD換算そのものは調査前から存在した。message IDごとの最後のUsage、
5分/1時間TTL、未知モデル・Fast/地域条件での換算不可、回答別USD表示も既存機能である。
今回それを維持して最新価格と不足部分を補った。

statusLineのCLI概算は端末表示用であり、ノートの`usage.total_cost_usd`へ加算しない。
ノートは引き続きtranscriptに基づく参考換算。実際の請求額と同一とは限らず、
子エージェント・ツール料金等の集計範囲でもCLI概算と差が出得る。
価格更新後は処理するセッションを現在の単価で再計算する。過去ノートの一括変更はしていない。

## 優先順位付き改善案（調査時点）

以下の5案は3.13.0で実装した。[実装内容・使い方・測定結果](saver-improvements.md)を参照。
以下は提案時の課題と完了条件を残したもの。

### 1. Codexの解析を一つのsnapshotから行う

現状はmetadata・quota・Usage・本文を別々にファイルから読み、本文解析でもUsageを
再読する。追記中には観測時点が揃わない余地がある。単一のsnapshotから正規化済み
イベントを作り、その上で本文・時間・Usageを計算する。世代別イベントの解釈を
小さな関数へ分ければ、約200行の解析関数も短くできる。

完了条件: 旧形式/new形式、途中追記、遅延Usage、子Usageの既存fixtureの出力一致。
巨大履歴の時間・メモリを変更前後で計測する。今回、性能ベンチマークは未実施。

### 2. 単価の確認日・適用日・出典を記録する

今回のSonnetの差のように、コード内の固定単価は時間とともに古くなる。
単価表に確認日・価格版を持ち、ノートにも換算に使った価格版を記録する。
履歴再構成で現在価格を使うか、当時価格を保つかを選べると比較が安定する。
自動でWeb価格を採用せず、差分検知とレビューで更新する構成を勧める。

### 3. Claudeの子エージェントUsageと異常終了保存

Codexとの主な未解消差は子Usageである。公式`SubagentStop`には
`agent_id`・`agent_transcript_path`があり、親の`transcript_path`とは別に取得できる。
子ごとのID重複排除と親ターンへの確実な紐付けを設計する。紐付け不能分を
無理に回答へ足さず、まずセッション計のみへ含める段階導入がよい。

公式`StopFailure`はAPIエラー時に`Stop`の代わりに発生する。
専用処理で途中までの会話・失敗種別を記録する案が有用。通常Stop用の
`last_assistant_message`一致待ちへエラー文を渡す実装にはしない。
いずれも今回hook設定を追加しておらず、実機fixtureを採取してから進める。
[公式hooks仕様](https://code.claude.com/docs/en/hooks)

### 4. 保存状態を診断するdoctor機能

Stop hookが失敗してもCLIを止めない契約は維持しつつ、最終保存成功時刻、
最後の失敗、配置版とソース版の差、出力先権限、cursorとtranscriptの整合を
読取専用で一覧化する。現在は終了コード0だけでは保存成功を判断できない。
契約テストへJSON型変異・I/O失敗・並行プロセスの試験を段階的に追加する。

### 5. 公式telemetryとの照合（必要になった段階）

Claude Codeの公式OpenTelemetryにはtoken/costとAPIイベントの出力がある。
将来、transcript換算と照合する用途に使える。常時collectorを必須にすると
単一ファイル・標準ライブラリという利点を失うため、任意の外部検証として扱う。
[公式Monitoring](https://code.claude.com/docs/en/monitoring-usage)

## Webで確認した根拠

以下は2026-10-25に閲覧。今回の料金再確認はClaudeを対象にしており、
GPT/Gemini/他providerの全単価を再監査したものではない。

- [Sonnet 5.5](https://platform.claude.com/docs/en/models/sonnet-5-5/overview): input/output 2/10、5m/1h書込2.5/4、read 0.10 USD/MTok。
- [Fable 5.1](https://platform.claude.com/docs/en/models/fable-5-1/overview): input/output 10/50、5m/1h書込12.5/20、read 0.25 USD/MTok。
- [Claude料金表](https://platform.claude.com/docs/en/about-claude/pricing): Opus 5.5とHaiku 5.5の既存値、およびHaikuの100K超条件も掲載値と一致。
- [Prompt caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching): 入力は通常入力・cache creation・cache readの合計。TTL内訳を分ける必要がある。
- [statusLine](https://code.claude.com/docs/en/statusline): `cost.total_cost_usd`を提供。quota枠は契約・gateway条件で未提供になり得るため、欠落をゼロ残量扱いしない。

## 検証と残る制限

- 変更前: 205テスト成功。
- 変更後: `/usr/bin/python3 -m unittest discover -s tests -v`、211テスト成功。
- `/usr/bin/python3 sync_shared.py --check`: 成功。
- `/usr/bin/python3 release_public.py --check`: 成功。
- `git diff --check`: 成功。
- 5本の単独起動テストを含む。Claudeの有料API呼出し・実契約によるStop/statusLineは未実施。
- synthetic transcriptと一時保存先で検証。実際のObsidian画面・巨大履歴の負荷・全CLI世代は未検証。
- VERSION、5本の`__version__`、README、CHANGELOGを3.12.0へ更新。
- コミット、hook配置、公開ツリー更新、push、タグ付けは未実施。
