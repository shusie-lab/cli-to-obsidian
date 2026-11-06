# 開発・テスト

開発やテストをする人向けの手順です。通常の利用ではリポジトリ全体の取得は不要です。

開発やテストをする場合は、[リポジトリのZIP](https://github.com/shusie-lab/cli-to-obsidian/archive/refs/heads/main.zip)を展開するか、Gitで取得してください。

外部パッケージは不要です。リポジトリのルートで次を実行します。

```bash
/usr/bin/python3 sync_shared.py --check
/usr/bin/python3 -m unittest discover -s tests -v
/usr/bin/python3 -X pycache_prefix=/tmp/cli_obsidian_save_pycache -m py_compile agy_save.py codex_save.py claude_save.py opencode_save.py pi_save.py
```

## 共通コードの管理

5本のMarkdown整形・日時・ログ・frontmatter・state・ロック・atomic書き込み・既存Markdown探索、
4本のUsage集計・金額表示・モデル別名・参考単価を`shared/`の正本で管理します。
Codex・Antigravity・Claude Codeは増分描画とcursor復旧を、OpenCode・Piはsnapshot描画を共有します。
入力解析・重複排除・quota取得・価格条件・Usageの集計範囲は各saverに残します。変更後に
`/usr/bin/python3 sync_shared.py`で各`*_save.py`へ埋め込みます。生成後の各スクリプトは
共通モジュールをimportせず、従来どおり単一ファイルで動作します。生成領域を変更する場合は
各スクリプトを直接編集せず、`shared/`を変更してから同期してください。同期時は全スクリプトの
Python構文を先に検証し、1本でも不正なら書き込みません。

## テストの内容

テストでは、5本の空入力・同一内容の再保存・source設定変更・state消失・非表示ユーザー発話の共通契約、生成領域内外のコード重複、単独ファイル実行を検証します。さらに、通常の追記、CodexとAntigravityのquota観測値、Claude CodeとOpenCodeのtoken集計、連続発言の集約に加えて、cursor復旧、state消失時の既存Markdown再利用、書き込み途中のJSONL最終行の再試行、YAML文字列とstateファイル名の安全化、保存先相対パスの検証を確認しています。

## 単価表の差分確認

単価表の正本は`shared/usage_prices.py.inc`、履歴と出典は`shared/usage_price_history.py.inc`です。公式出典を確認して候補JSONを作成し、次のコマンドで差分を確認します。候補の`rates`には全モデルを含めてください。省略したモデルは削除差分になります。

```bash
/usr/bin/python3 check_usage_prices.py > /private/tmp/current-prices.json
# 公式出典を確認して候補JSONを作成した後
/usr/bin/python3 check_usage_prices.py --candidate /private/tmp/candidate-prices.json
```

終了コードは差分なし0、差分あり1、入力エラー2です。候補の自動採用・Web取得は行いません。変更時は旧表を履歴として残し、価格版・確認日・出典を更新してから`/usr/bin/python3 sync_shared.py`で各saverへ反映します。

## 実装上の特徴

- **ノイズの少ないログ保存**: ユーザーおよびエージェントの発言だけを抽出し、ツール呼び出しや実行結果を除外します。
- **Codexのsnapshot解析**: Stopの最初にJSONLを一度読み、metadata・quota・Usage・本文を同じsnapshotから計算します。不完全な末尾は次回へ保留します。子transcriptはファイルごとのsnapshotです。
- **増分管理**: CodexとClaude Codeは`last_line`、Antigravityは`last_id`で読み込み済み位置を管理します。
- **cursorとstateの自動復旧**: Markdown内のcursorを正としてstateとの不一致を補正し、state消失時はfrontmatterの`session_id`から既存Markdownを探索します。
- **内容変更時だけノート更新**: 保存対象の本文がない新規セッションではノートを作らず、同じ内容の再保存で`modified`だけを変更しません。
- **Atomic書き込み**: Markdownとstateを同一ディレクトリの一時ファイルへ書いてからrenameします。
- **同時実行への対応**: セッション単位のファイルロックでhookの重複実行を防ぎます。
- **安全なメタデータとファイル名**: YAML文字列、session ID、プロジェクト名を安全な形式に変換します。
- **古いstateのクリーンアップ**: 30日間利用されていないstateファイルを自動的に削除します（非ブロッキングロックで実行中セッションを保護）。
