# Changelog

このプロジェクトの主要な変更をリリース単位で記録します。

形式は [Keep a Changelog](https://keepachangelog.com/ja/1.1.0/) を参考にし、
バージョン番号は [Semantic Versioning](https://semver.org/lang/ja/) に従います。

## [Unreleased]

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

[Unreleased]: https://github.com/shusie1969/cli-to-obsidian/compare/v1.2.0...HEAD
[1.2.0]: https://github.com/shusie1969/cli-to-obsidian/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/shusie1969/cli-to-obsidian/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/shusie1969/cli-to-obsidian/releases/tag/v1.0.0
