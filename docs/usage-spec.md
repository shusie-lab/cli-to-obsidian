# 使用量・金額・quotaの記録仕様

ノートに記録するtoken数・USD参考換算・quota・回答所要時間の仕様と、ノートの出力例です。

## セッション全体と回答ごとの集計

本文冒頭とfrontmatterの`usage`はセッション全体、各回答calloutは対応する回答の使用量です。本文のないUsageや回答へ紐付けられない子Usageがあるため、回答の合計とセッション合計が一致しない場合があります。

| ツール | セッション全体の集計 | 回答への割当 |
| --- | --- | --- |
| Codex | 親と`parent_thread_id`で紐付く子の`token_usage_record`を集計。`thread_token_usage`の累計は加算しません。 | 子の`root_turn_id`で親ターンを特定し、最後の回答へ加算。紐付かない子はセッション計のみです。 |
| Claude Code | 親・子それぞれのmessage IDごとの最後の`message.usage`を集計。 | 子の`parentUuid`が一つの親ターンに結び付く場合のみ回答へ加算。親・子・回答に紐付かない子のtoken内訳も表示します。 |
| OpenCode | SQLiteのassistantレコードをmessage IDごとに集計。本文のないツール専用レコードも含めます。 | 同じユーザー発話内の連続する同一モデルの回答をまとめ、本文のないUsageも近い回答へ加算します。 |
| Pi | 全分岐のentryをIDごとに集計。圧縮・分岐要約・追加Usage・Usage付きツール結果も含めます。 | 現在の分岐のUsageのみ表示。同一ターン・同一モデルを優先して近い回答へ加算します。 |

Codexの子Usageは対象の`session_id`と一致するレコードに限定します。Claudeの子は`親transcript拡張子なし/subagents/agent-*.jsonl`から読み、別sessionId・symlinkを除外します。子の本文は親ノートへ混ぜません。Piでモデルを特定できないUsageは`unknown`としてtokenのみ集計します。

共通形式の`input_tokens`はキャッシュ読込・書込を含む入力総数、`reasoning_output_tokens`は出力の内数です。これらを二重加算しません。遅れて書き込まれたUsageは次の保存で反映します。

## USD参考換算とCLI報告コスト

**参考換算と取得コストは別々に表示し、合算しません。** 参考換算は共通単価表から計算した比較用の金額です。サブスクリプション請求額やquota消費額ではありません。

| 値 | 記録先 | 対象・扱い |
| --- | --- | --- |
| USD参考換算 | `usage.total_cost_usd`、`usage.by_model.*.cost_usd` | Codex・Claude・OpenCode・Pi。`usage.cost_basis: reference`で意味を示します。 |
| CLI報告コスト | `reported_cost.usd` | OpenCode・Piは回答の取得値の合計（`scope: message_sum`）、ClaudeはstatusLineのセッション累計の最新観測値（`scope: session`）です。 |

OpenCodeはassistantの`cost`、Piは`usage.cost.total`、ClaudeはstatusLineの`cost.total_cost_usd`を取得します。Claudeは再通知ごとに加算せず、回答へ推測配分しません。OpenCode・Piは回答calloutにも取得コストを表示します。

参考換算できない金額は`換算不可`、一部の既知モデルだけ換算した総計は`部分換算`（`cost_incomplete: true`）です。CLI報告値は0を`$0`、未提供・負数・非有限等を`未取得`、一部だけ取得できた場合を`部分取得`として区別します。無料モデルも対応する有料版の参考単価で換算するため、参考換算が正でもCLI報告値が0になることがあります。

金額はノートでは小数桁を丸めず固定小数で保存します。CLI報告値も実請求額の保証ではありません。Claudeの値は定価または`modelPricing`を基にしたクライアント側概算です。[Claude公式statusLine仕様](https://code.claude.com/docs/en/statusline#available-data)Claudeの取得値はstate・ノートから復旧し、再構成時も現在の単価で再計算しません。

**OpenCode 3.xからの移行:** 4.0.0以降の`usage.total_cost_usd`は参考換算に統一しました。以前の正の`cost`を優先する方式とは意味が異なります。通常保存・明示的な再構成から新方式になり、過去ノートは一括変更しません。3.xのstateしかない部分同期で取得金額を判別できない場合は未取得とし、完全なsnapshotで復元します。

## 対応モデルと価格条件

単価表に登録しているモデルは以下です。これはこのソースに収録した参考単価の対応表で、各社の最新料金・提供状況を示す一覧ではありません。プロバイダ接頭辞と対応する別名を正規化して照合します。未登録でも本文・token数は保存します。

| 系列 | 登録モデル |
| --- | --- |
| GPT | GPT-6 Astra、GPT-6.1 Sol、GPT-6.1 Terra、GPT-6 Luna |
| Claude | Fable 5.1、Opus 5.5、Sonnet 5.5、Haiku 5.5 |
| Gemini | Gemini 3.8 Flash |
| DeepSeek | DeepSeek V4.1 Flash（`deepseek-flash`も対応） |
| その他 | Muse Spark 1.3 Contributor、Nemotron 3 Ultra |

- GPTの272K超入力: Codexは先頭の`APPLY_LONG_CONTEXT_PRICING = False`で倍率なしです。`True`にするとリクエスト単位で入力・キャッシュ系2倍、出力1.5倍となり、対象回答に`⚠︎ 272K+`を表示します。Claude・OpenCode・Piの既定は`True`です。CLI報告コストには掛けません。
- Claude: cache writeの5分・1時間を区別します。Haiku 5.5は1リクエストの入力総数が10万token超の場合に全種別5倍、Opus 5.5・Sonnet 5.5は20万token超でも標準単価です。Fast・地域・TTL等の価格条件を確定できない場合は換算を省略します。
- DeepSeek: 登録済みピーク単価を固定で使用し、時刻によるオフピーク切り替えは行いません。
- Gemini: 登録表は2026年末までのStandard割引価格を基にしています。cache writeはOpenRouter掲載のtoken単価を使い、Googleの時間単位の保管料は加算しません。
- Batch・Flex・Priority・ツール料金などは参考換算に含めません。単価は自動更新しません。

収録単価・価格版・確認日・出典は、[check_usage_prices.py](../check_usage_prices.py)・[rebuild_session.py](../rebuild_session.py)・[claude_save.py](../claude_save.py)を同じディレクトリへ置き、次で確認できます。

```bash
/usr/bin/python3 check_usage_prices.py
```

Usageには全体・モデル別・回答別の`Cache: xx.x%`（キャッシュ入力token ÷ 全入力token）も表示します。入力が0または割合を算出できない場合は`Cache: N/A`と表示します。

ノートには`usage.price_version`・`price_recorded_at`・`price_checked_at`・`price_sources`・`price_effective_from`も保存します。価格版はsaverの機能バージョンと独立しています。確認日がないモデルや適用開始日が不明な値はnullです。価格設定を変えた後の通常保存では、そのセッションの累計・既存回答Usageを現在の設定で再計算します。

## quota残量

Codexはtranscript、Claudeは任意のstatusLine入力の`rate_limits`、Antigravityは`/quota`から取得します。weekly・5hなど取得できた枠だけを記録し、最初と最後の観測値を`first_observed`・`last_observed`へ保存します。「最初」は会話開始時ではなく最初に取得できた値です。

quotaはアカウント全体の残量です。差分を会話の消費量とは扱わず、回答別の差分も表示しません。未提供の枠は省略し、会話保存は続けます。ClaudeのstatusLineはquotaがなくてもコストがあれば表示し、どちらもなければ空表示です。新形式の保存時に旧`quota`・`quota_claude`の数値形式を置換します。

## 回答所要時間

依頼から回答完了までを、最後の回答に`⏱ 回答所要時間: 2分15秒`の形式で表示します。途中経過・ツール実行・通信・生成を含み、AI内部の推論時間だけを測るものではありません。同じ依頼でモデルが変わっても最後のcalloutに総時間を表示し、秒未満は切り捨てます。

| ツール | 完了時刻の取得元 |
| --- | --- |
| Codex | ターン完了イベント。未記録なら、最終回答と識別できる回答完了イベント。 |
| Claude Code | `turn_duration`終了記録。Stopより後の記録は次の保存で反映。 |
| OpenCode | 回答の`time.completed`。 |
| Pi | 回答終了時に履歴へ保存されるentryの時刻。 |
| Antigravity | `model_stop`・`fullyIdle: true`・エラーなしのStop受信時刻。hook起動までの遅れは含み、ロック待ち・quota取得などの保存処理は含みません。 |

開始・完了時刻がない、不正、逆転している場合は`取得不可`とします。Antigravityの観測済み時刻はstate・ノート内コメントに保持し、再通知・復旧・再構成でも引き継ぎます。未観測の過去履歴に現在時刻を付けることはありません。

## 出力例

主要項目を抜粋した例です。価格の出典・quota・回答別Usage等は入力に応じて追加されます。

```markdown
---
source: codex-cli
session_id: "019f6d84-c321-7750-9532-c4b40fad70df"
title: "このコードのバグを直してください。"
project: "/path/to/myapp"
created: "2026-07-17T09:40:55+09:00"
modified: "2026-07-17T09:40:55+09:00"
tags:
  - ai-conversation
  - codex-cli
message_count: 2
usage:
  cost_basis: "reference"
  price_version: "4.2.0"
  input_tokens: 120000
  cached_input_tokens: 100000
  cache_write_input_tokens: 0
  output_tokens: 8000
  reasoning_output_tokens: 3000
  total_tokens: 128000
  total_cost_usd: 0.13
  by_model:
    "gpt-6.1-sol":
      total_tokens: 128000
      cost_usd: 0.13
---

💰 **Usage (USD換算)**: Total: $0.13 (gpt-6.1-sol $0.13)

<!-- last_line: 11 -->

# このコードのバグを直してください。
> [!QUESTION] User
> <small>⏱ 2026-07-17 09:40:45</small>
>
> このコードのバグを直してください。

> [!NOTE] Codex
> <small>🤖 gpt-6.1-sol-medium</small>

問題を確認しました。原因は...
```

ChatGPTからWorkへ移行した際に生成される構造化されたユーザー入力は、先頭が
`## Referenced ChatGPT conversation:` または `# Files mentioned by the user:` で、
コードフェンス外に `## My request:` が1つあり、後続本文が空でない場合に限って表示を分けます。
依頼本文は通常のUser QUESTION calloutへ表示し、前置きの参照情報は内容を解釈せず、
折りたたみ式の「参照情報（原文）」INFO calloutへ保存します。条件が曖昧な入力は従来どおり全文を保存します。
