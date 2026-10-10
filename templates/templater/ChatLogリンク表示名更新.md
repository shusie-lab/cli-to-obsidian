<%*
const DEFAULT_OUTPUT_DIR = "生成AI/ChatLog";
const MAX_TITLE_LENGTH = 50;

const targetFile = tp.config.target_file;

function displayTitle(file) {
  const frontmatter = tp.app.metadataCache.getFileCache(file)?.frontmatter;
  const title = frontmatter?.title;
  const text = typeof title === "string" && title.trim()
    ? title.trim()
    : file.basename;
  const characters = Array.from(text);
  const shortenedTitle = characters.length > MAX_TITLE_LENGTH
    ? `${characters.slice(0, MAX_TITLE_LENGTH - 1).join("")}…`
    : text;

  const rawCost = frontmatter?.usage?.total_cost_usd;
  const cost = typeof rawCost === "number"
    ? rawCost
    : typeof rawCost === "string" && rawCost.trim() !== ""
      ? Number(rawCost)
      : NaN;
  const costSuffix = Number.isFinite(cost)
    ? ` ($${cost.toFixed(2)})`
    : "";

  return shortenedTitle + costSuffix;
}

function isInsideInlineCode(line, position) {
  const ticks = Array.from(line.matchAll(/`+/g));

  for (let i = 0; i < ticks.length; i++) {
    const opening = ticks[i];
    const closingIndex = ticks.findIndex(
      (tick, index) => index > i && tick[0].length === opening[0].length
    );
    if (closingIndex < 0) continue;

    const closing = ticks[closingIndex];
    if (position >= opening.index && position < closing.index + closing[0].length) {
      return true;
    }
    i = closingIndex;
  }

  return false;
}

if (targetFile) {
  tp.hooks.on_all_templates_executed(() => {
    const activeFile = tp.app.workspace.getActiveFile();
    const editor = tp.app.workspace.activeEditor?.editor;

    if (!activeFile || activeFile.path !== targetFile.path || !editor) return;

    const source = editor.getValue();
    const lines = source.split("\n");
    const edits = [];
    let matchedCount = 0;
    let lineStart = 0;
    let inFrontmatter = lines[0]?.replace(/^\uFEFF/, "").trim() === "---";
    let fenceCharacter = null;
    let fenceLength = 0;

    for (let lineIndex = 0; lineIndex < lines.length; lineIndex++) {
      const line = lines[lineIndex];

      if (inFrontmatter) {
        if (lineIndex > 0 && ["---", "..."].includes(line.trim())) {
          inFrontmatter = false;
        }
        lineStart += line.length + 1;
        continue;
      }

      if (fenceCharacter) {
        const closingFence = new RegExp(
          `^ {0,3}${fenceCharacter}{${fenceLength},}[ \\t]*$`
        );
        if (closingFence.test(line)) {
          fenceCharacter = null;
          fenceLength = 0;
        }
        lineStart += line.length + 1;
        continue;
      }

      const openingFence = line.match(/^ {0,3}(`{3,}|~{3,})/);
      if (openingFence) {
        fenceCharacter = openingFence[1][0];
        fenceLength = openingFence[1].length;
        lineStart += line.length + 1;
        continue;
      }

      const linkPattern = /\[\[([^\[\]|]+)(?:\|([^\]\n]*))?\]\]/g;
      let match;
      while ((match = linkPattern.exec(line)) !== null) {
        if (isInsideInlineCode(line, match.index)) continue;

        const linkTarget = match[1].trim();
        const linkPath = linkTarget.split("#", 1)[0].trim();
        if (!linkPath) continue;

        const destination = tp.app.metadataCache.getFirstLinkpathDest(
          linkPath,
          targetFile.path
        );
        if (!destination?.path.startsWith(`${DEFAULT_OUTPUT_DIR}/`)) continue;
        matchedCount += 1;

        const replacement = `[[${linkTarget}|${displayTitle(destination)}]]`;
        if (replacement !== match[0]) {
          edits.push({
            start: lineStart + match.index,
            end: lineStart + match.index + match[0].length,
            replacement,
          });
        }
      }

      lineStart += line.length + 1;
    }

    if (edits.length === 0) {
      const message = matchedCount > 0
        ? `ChatLogリンク${matchedCount}件を確認しました。すべて最新です。`
        : "更新対象のChatLogリンクはありませんでした。";
      new tp.obsidian.Notice(message);
      return;
    }

    let updated = source;
    for (const edit of edits.reverse()) {
      updated = updated.slice(0, edit.start) + edit.replacement + updated.slice(edit.end);
    }

    const cursorOffset = editor.posToOffset(editor.getCursor());
    editor.replaceRange(
      updated,
      { line: 0, ch: 0 },
      editor.offsetToPos(source.length)
    );
    editor.setCursor(editor.offsetToPos(Math.min(cursorOffset, updated.length)));
    new tp.obsidian.Notice(`${edits.length}件のChatLogリンクを更新しました（対象${matchedCount}件）。`);
  });
}

tR = "";
-%>
