# Changelog

このプロジェクトの主要な変更をリリース単位で記録します。

形式は [Keep a Changelog](https://keepachangelog.com/ja/1.1.0/) を参考にし、
バージョン番号は [Semantic Versioning](https://semver.org/lang/ja/) に従います。

## [Unreleased]

### Added

- セッションIDから全履歴を再構成する `rebuild_session.py` を追加。テスト用出力と、バックアップ付きの既存Obsidianノート置換に対応

## [3.8.0] - 2026-10-09

### Added

- Claude Haiku 5.5のUSD参考換算に対応。入力10万token超の料金と、Claude Codeの5分・1時間キャッシュ書込を換算

## [3.7.0] - 2026-10-06

### Added

- Claude Codeの回答calloutにも、回答ごとのtoken数とUSD参考換算を表示
- Codex・Claude Code・OpenCode・Piで、共通単価表に登録されたモデルをすべてUSD参考換算の対象にする
- Gemini 3.8 FlashのUSD参考換算に対応し、OpenRouterの`google/gemini-3.8-flash`を認識

### Changed

- Codex・Claude Code・OpenCode・PiのUsageサマリ、換算不可・部分換算の表示、金額の固定小数表記を統一
- GPT-6 Sol・GPT-5.6系列の参考単価を削除。これらのモデルは本文とtoken数を保存し、参考金額は換算不可とする
- 保存対象の本文がない新規セッションでは、空の会話ノートを作成しない

### Fixed

- Claude CodeのOpus 5.5キャッシュ読込を100万tokenあたり$0.20へ修正し、他の保存スクリプトと統一
- OpenCodeのGPT参考換算にもリクエスト単位の272K超料金を適用し、Piとともに対象回答へ長文料金の印を表示（記録済みの正のcostは優先）
- Claude CodeのOpus 5.5・Sonnet 5.5は入力20万token超でも標準単価で換算
- 同じ内容のStop hookを再実行しても、Codexのノートの更新時刻だけが変わらないよう修正
- 本文のないユーザー発話やCodexの明示的なturn ID変更を挟んだ回答を、増分保存でも別のcalloutとして表示
- OpenCode・Piで既存ノートを再構成する際、作成日時・source・タグを保持
- OpenCodeの保存失敗をプラグインへ返し、失敗した会話を保存済みとして扱わないよう修正
- OpenCodeでもClaude・Muse Sparkのプロバイダ接頭辞付きモデル別名を認識

## [3.6.2] - 2026-10-05

### Fixed

- Piで同じユーザー発話に対する同一モデルの途中経過と最終回答を1つのcalloutにまとめ、回答のUsageと表示件数も集約
- Piのツール専用Usageを同じユーザー発話内の同一モデルを優先した最寄り回答へ加算
- Piで本文が同じ分岐間の切替でも回答のUsageを更新し、本文のない分岐へ切り替えた際に以前の本文が残る問題を修正
- Piでプロジェクト名がない場合の保存ファイル名を共通仕様に統一
- OpenCodeで本文のないユーザー発話を挟んだ回答を別々のcalloutとして表示

## [3.6.1] - 2026-10-05

### Changed

- Piでquotaを取得できなかったため、quota取得コードを削除

## [3.6.0] - 2026-10-05

### Added

- PiのTypeScript extensionから会話をObsidianへ自動保存。本文は現在の分岐を表示し、Usageは別分岐・圧縮なども含むセッション全体を集計
- PiでGPT系とMuse Spark・Claude・Nemotronの標準API単価によるUSD参考換算に対応

## [3.5.3] - 2026-10-04

### Changed

- Claude CodeのUSD換算対象をOpus 5.5、Sonnet 5.5、Nemotron 3 Ultraの3モデルに限定

### Fixed

- Claude CodeとOpenCodeでNemotronのプロバイダ接頭辞付き正式モデルIDを共通の単価表名に対応
- Claude CodeのUSD換算で`anthropic/claude-sonnet-5.5`を認識し、`nvidia/nemotron-3-ultra-550b-a55b:free`も有料版の参考単価で換算

## [3.5.2] - 2026-10-04

### Fixed

- Claude Codeの会話ログの書き込みが遅れた場合に、最終回答やtoken使用量が保存されない問題を修正

## [3.5.1] - 2026-09-30

### Changed

- OpenCodeの無料モデルも、対応する有料モデルの単価を参考値としてUSD換算
- Claude Opus 5.5のモデルIDを正しく認識

## [3.5.0] - 2026-09-29

### Changed

- OpenCodeの回答ごとのUsageに、同じユーザー発話中の本文のないツール専用レコードも加算

## [3.4.0] - 2026-09-29

### Added

- OpenCodeの保存本文先頭に、quotaを含めずUSD換算額とモデル別金額のサマリを表示

## [3.3.0] - 2026-09-29

### Added

- OpenCodeの会話UsageにMuse Spark 1.3系の標準API単価によるUSD概算を追加し、`muse-spark-1.3-contributor-free`などOpenCodeが正の`cost`を返さない場合も金額を記録

## [3.2.0] - 2026-09-29

### Added

- Codexの回答ごとのUsageに、対応する親ターンのサブエージェント使用量を含め、合計とサブエージェントのモデル別token数・USD概算を表示

### Fixed

- Codexの遅れて届くUsageにより、同じ回答のcalloutが分かれる場合を抑止し、既存の分割calloutでは最後の枠に回答全体のUsageを表示

## [3.1.0] - 2026-09-29

### Added

- Codexの会話Usageにサブエージェントとその子セッションのtoken使用量を含め、モデル別内訳とUSD概算へ反映

## [3.0.1] - 2026-09-27

### Fixed

- OpenCodeで同じユーザー発話に続く同一モデルの回答を1つのcalloutにまとめ、進捗ごとに回答枠が増える問題を修正
- stateが失われた後の全履歴再保存で、まとめた回答が重複する問題を修正

## [3.0.0] - 2026-09-26

### Changed

- CodexとAntigravityのquotaを、各枠の最初と最後の残量・観測時刻・リセット時刻として階層化して記録。従来の消費量数値と回答別quota差分を終了
- OpenCodeのOpenRouter残高取得・表示を終了

### Added

- Claude Codeのtranscriptから会話単位のtoken usageを集計し、対応モデルの標準API単価によるUSD概算を記録
- Claude CodeのstatusLineを設定した場合に、取得できた5h・weekly quotaの最初と最後の観測値を記録
- OpenCodeの回答に記録されたtoken usageと取得可能なUSD概算を会話単位・モデル別に記録

## [2.1.0] - 2026-09-26

### Changed

- Codexの会話は起動元にかかわらず既定の`codex-cli`名で保存
- Antigravityの会話も起動元にかかわらず既定の`antigravity-cli`名で保存
- 全保存スクリプトの保存先・source・タグを`OBSIDIAN_SOURCE`で指定可能に変更

## [2.0.1] - 2026-09-23

### Added

- CodexのUsage USD換算でGPT-6 SolとGPT-6 Lunaを計算

## [2.0.0] - 2026-09-21

### Changed

- OpenCode v2専用として再構成し、完了済み会話をデータベースから取得してObsidianへ保存
- 保存失敗時に次回監視で再試行できるよう変更
- 長い会話とSQLiteの遅延確定時にも保存漏れを防止
- OpenCode v1のプラグインhook対応を終了

## [1.6.3] - 2026-09-21

### Fixed

- OpenCode v2の会話履歴取得APIを正しい`session.messages()`呼び出しへ修正
- v2の保存失敗時に原因がログへ記録されるよう修正

## [1.6.2] - 2026-09-21

### Fixed

- Codexでツール実行を挟んだ連続回答を保存する際、回答間の空行1行を保持

## [1.6.1] - 2026-09-21

### Fixed

- OpenCode v2の実行完了・失敗・中断イベントで会話が保存されない問題を修正
- 互換イベントが重複して届いた場合の二重保存を抑止

## [1.6.0] - 2026-09-21

### Added

- OpenCode v2のプラグインAPIに対応し、会話がidleになった時点で全履歴をObsidianへ保存

## [1.5.3] - 2026-09-19

### Fixed

- 回答内のローカル絶対パスへのMarkdownリンクを、Obsidianで外部ファイルとして開ける`file:///`リンクへ変換

## [1.5.2] - 2026-09-18

### Added

- Codex回答のcallout metadataに、回答単位のtoken使用量とUSD概算を表示
- 272K input tokens超のusage recordを含む回答のUSD表示に注意マークを表示

## [1.5.1] - 2026-09-18

### Fixed

- Codex UsageのUSD概算を、モデル別の通常入力・キャッシュ入力・cache write・出力の標準API単価で計算するよう修正
- UsageのUSD金額を本文・frontmatterで小数桁を丸めずに記録するよう修正

## [1.5.0] - 2026-09-18

### Added

- Codexセッションのtranscriptに記録されたtoken使用量をモデル別に集計し、Dataviewで共通利用できる`usage` frontmatterへ保存
- モデル別の固定係数によるUSD換算額と、本文のUsage（USD換算）セクションを追加

## [1.4.1] - 2026-09-16

### Fixed

- Codex Desktop の質問 UI に対する選択結果が、ユーザーの独立した発話ではなく、質問を含む Codex 回答へ連結されるよう修正

## [1.4.0] - 2026-09-15

### Added

- 最初のユーザー発話を短い見出しとfrontmatterの`title`へ記録
- OpenRouter接続のOpenCode回答に、回答時点のUSD残高と取得日時を記録

### Fixed

- frontmatterの`title`にMarkdown見出し用のエスケープが残る問題を修正

## [1.3.0] - 2026-09-13

### Added

- Antigravityの公式読み取り専用`agy /quota` JSON出力を用いたquota記録を`agy_save.py`へ追加
- Antigravityのweekly quota残量・リセット時刻・セッション中の消費量をMarkdownとstateへ記録

## [1.2.0] - 2026-09-13

### Changed

- 全スクリプト（Codex, Antigravity, Claude Code, OpenCode）の保存ファイル名末尾をセッションID全体（安全化したキー）に統一し、先頭8文字の衝突を防止
- 過去に短いID（先頭8文字）で保存された既存Markdownファイルへの追記互換性を維持
- `find_existing_md` でのI/Oエラー・読み取り失敗時に誤上書きや重複作成を防ぐ安全な例外ハンドリングを導入
- state クリーンアップ基準を `last_used_at`（最終利用日時）に統一し、非ブロッキングロックで実行中セッションを保護

### Added

- OpenCode のログローテーション（`RotatingFileHandler` 5MB/3世代）と古い state の自動クリーンアップ（30日経過）を追加
- README に OpenCode のログファイルパスを追記

## [1.1.0] - 2026-09-11

### Added

- OpenCode の会話完了プラグインから会話を Obsidian に保存する機能を追加

## [1.0.0] - 2026-08-26

### Added

- Antigravity CLI、Codex CLI、Claude Code CLI の会話を Obsidian に保存する単一ファイルの hook スクリプトを公開
- ChatGPTデスクトップアプリ内のCodexセッションとAntigravityアプリに対応
- 会話履歴の増分保存、セッション状態の復旧、多重実行防止に対応
- Codexのtranscriptからquotaの変化とセッション全体のweekly quota消費量を記録
- `OBSIDIAN_OUTPUT_DIR`による保管庫内の保存先カスタマイズに対応
- macOS 標準 Python 3.9 で実行できる標準ライブラリのみの構成

[Unreleased]: https://github.com/shusie1969/cli-to-obsidian/compare/v3.0.0...HEAD
[3.0.0]: https://github.com/shusie1969/cli-to-obsidian/compare/v2.1.0...v3.0.0
[2.0.1]: https://github.com/shusie1969/cli-to-obsidian/compare/v2.0.0...v2.0.1
[2.0.0]: https://github.com/shusie1969/cli-to-obsidian/compare/v1.6.3...v2.0.0
[1.6.3]: https://github.com/shusie1969/cli-to-obsidian/compare/v1.6.2...v1.6.3
[1.6.1]: https://github.com/shusie1969/cli-to-obsidian/compare/v1.6.0...v1.6.1
[1.6.0]: https://github.com/shusie1969/cli-to-obsidian/compare/v1.5.3...v1.6.0
[1.5.3]: https://github.com/shusie1969/cli-to-obsidian/compare/v1.5.2...v1.5.3
[1.5.2]: https://github.com/shusie1969/cli-to-obsidian/compare/v1.5.1...v1.5.2
[1.5.1]: https://github.com/shusie1969/cli-to-obsidian/compare/v1.5.0...v1.5.1
[1.5.0]: https://github.com/shusie1969/cli-to-obsidian/compare/v1.4.1...v1.5.0
[1.4.1]: https://github.com/shusie1969/cli-to-obsidian/compare/v1.4.0...v1.4.1
[1.4.0]: https://github.com/shusie1969/cli-to-obsidian/compare/v1.3.0...v1.4.0
[1.3.0]: https://github.com/shusie1969/cli-to-obsidian/compare/v1.2.0...v1.3.0
[1.2.0]: https://github.com/shusie1969/cli-to-obsidian/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/shusie1969/cli-to-obsidian/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/shusie1969/cli-to-obsidian/releases/tag/v1.0.0
