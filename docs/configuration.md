# 共通設定

保存先、ファイル名、Templaterテンプレートなど、全ツールに共通する設定です。ツール別の導入手順は[README](../README.md#対応ツール)の対応表から開けます。

## Obsidian保管庫と保存先

各導入手順のhook設定例を使う場合は、コマンド内の`OBSIDIAN_VAULT`を変更します。以下の`export`は、CLIを同じターミナルから起動して環境変数で指定する場合の例です。

`OBSIDIAN_VAULT`でObsidian保管庫のルートを指定できます。省略時は`~/obsidian`です。

`OBSIDIAN_OUTPUT_DIR`で保管庫内の保存先を指定できます。省略時は`生成AI/ChatLog`です。値は保管庫からの相対パスに限定され、絶対パスや`..`を含むパスは安全のため既定値へフォールバックします。

```bash
export OBSIDIAN_VAULT="$HOME/path/to/your/vault"
export OBSIDIAN_OUTPUT_DIR="AI/Conversations"
```

各スクリプトは保存先の末尾に対象名を付けます。

```text
$OBSIDIAN_VAULT/AI/Conversations/codex-cli/
$OBSIDIAN_VAULT/AI/Conversations/antigravity-cli/
$OBSIDIAN_VAULT/AI/Conversations/claude-code/
$OBSIDIAN_VAULT/AI/Conversations/opencode/
$OBSIDIAN_VAULT/AI/Conversations/pi/
```

`OBSIDIAN_SOURCE`を設定すると、各スクリプトの保存先末尾とfrontmatterの`source`・タグを同じ名前に変更できます。複数のMacや用途で保存先を分ける場合は、それぞれのhookを起動する環境で異なる値を指定します。

```bash
export OBSIDIAN_SOURCE="codex-work"
```

指定できるのは英数字で始まり、以降は英数字・`.`・`_`・`-`からなる64文字以内の名前です。不正な値は各スクリプトの既定名へフォールバックします。既にstateに保存先が記録されている会話は従来のファイルを更新し、新しい会話から指定した保存先を使います。既存ノートの`created`・`source`・タグを引き継ぐため、設定変更後やstate復旧時も会話の識別情報を保持します。AntigravityもCLI・アプリ共通の`source`・タグを記録します。

## ファイル名

保存ファイル名は次の形式です。

```text
{YYYYMMDD}_{HHMMSS}_{project}_{session_id}.md
```

プロジェクト名がない場合は `{YYYYMMDD}_{HHMMSS}_{session_id}.md` です。`session_id` はファイル名として安全な文字に変換されます。過去に保存された先頭8文字形式のファイルも自動探索され、追記が継続されます。

## Templaterテンプレート（任意）

会話記録のファイル名を変えずに、Obsidian上ではfrontmatterの`title`を使って会話を探したり表示したりするための、[ChatLogLink.md](../templates/templater/ChatLogLink.md)と[ChatLogリンク表示名更新.md](../templates/templater/ChatLogリンク表示名更新.md)を使えます。ObsidianのTemplaterプラグインを有効にし、設定したテンプレートフォルダへファイルをコピーしてください。たとえば両方を`~/Downloads`へダウンロードし、テンプレートフォルダが`~/obsidian/Templates`の場合は次のようにします。

```bash
cp "$HOME/Downloads/ChatLogLink.md" ~/obsidian/Templates/
cp "$HOME/Downloads/ChatLogリンク表示名更新.md" ~/obsidian/Templates/
```

- **`ChatLogLink.md`**: テンプレートを挿入すると、`生成AI/ChatLog/`内の最近更新されたノート（最大10件）が候補に表示されます。選択したノートへの内部リンクを、frontmatterの`title`（なければファイル名）で表示します。Usageの`total_cost_usd`があれば、タイトルの後ろに参考金額を小数2桁で表示します。候補一覧ではフォルダ内の相対パスも確認できます。
- **`ChatLogリンク表示名更新.md`**: 開いているノート内の表示名付きWikiリンク（`[[リンク先|表示名]]`）から、`生成AI/ChatLog/`へ解決されるリンクの表示名をリンク先の最新の`title`に更新します。titleがない場合はファイル名を使い、金額があれば付記します。更新後の表示名がファイル名と同じで金額もない場合は、通常のリンク（`[[リンク先]]`）にします（見出し・ブロックへのリンクは表示名を維持します）。`|`のない通常のリンク、YAML frontmatter、コードブロック、インラインコード内は処理しません。ファイル名や会話記録そのものは変更しません。

使うときは、リンクを挿入する位置または更新したいノートを開き、コマンドパレットから **Templater: Open insert template modal** を実行してテンプレートを選びます。頻繁に使う場合は、Obsidianの設定にある「ホットキー」で同じコマンドにショートカットを割り当てられます。

両テンプレートの`DEFAULT_OUTPUT_DIR`は各`*_save.py`の`DEFAULT_OUTPUT_DIR`（既定値`生成AI/ChatLog`）と同じ名前・値にしています。各保存スクリプトはこのフォルダの下にsource名のサブフォルダを作ります。`OBSIDIAN_OUTPUT_DIR`で保存先を変更した場合は、両テンプレートの`DEFAULT_OUTPUT_DIR`も同じ保存先に変更してください。
