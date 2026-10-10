<%*
const DEFAULT_OUTPUT_DIR = "生成AI/ChatLog";
const RECENT_FILE_LIMIT = 10;
const MAX_TITLE_LENGTH = 50;
const MAX_DISPLAY_PATH_LENGTH = 50;

const currentPath = tp.file.path(true);
const templatePath = tp.config.template_file?.path;
const allFiles = tp.app.vault.getMarkdownFiles();
const recentFiles = allFiles
  .filter(file =>
    file.path.startsWith(`${DEFAULT_OUTPUT_DIR}/`) &&
    file.path !== currentPath &&
    file.path !== templatePath
  )
  .sort((a, b) => b.stat.mtime - a.stat.mtime)
  .slice(0, RECENT_FILE_LIMIT);

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

function displayPath(file) {
  const path = file.path.startsWith(`${DEFAULT_OUTPUT_DIR}/`)
    ? file.path.slice(DEFAULT_OUTPUT_DIR.length + 1)
    : file.path;
  const characters = Array.from(path);

  return characters.length > MAX_DISPLAY_PATH_LENGTH
    ? `${characters.slice(0, MAX_DISPLAY_PATH_LENGTH - 1).join("")}…`
    : path;
}

const selected = await tp.system.suggester(
  file => `▶ ${displayTitle(file)}\n${displayPath(file)}`,
  recentFiles,
  false,
  "最近更新したノート"
);

if (selected) {
  const uniqueName =
    allFiles.filter(file => file.basename === selected.basename).length === 1;
  const target = uniqueName
    ? selected.basename
    : selected.path.slice(0, -3);

  tR += `[[${target}|${displayTitle(selected)}]]`;
}
-%>
