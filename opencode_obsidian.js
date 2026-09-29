// OpenCode v2 の完了済み会話を Obsidian 保存スクリプトへ渡すプラグイン。
import { mkdir, mkdtemp, readFile, rename, rm, writeFile } from "node:fs/promises"
import { homedir, tmpdir } from "node:os"
import { dirname, join } from "node:path"
import { execFile } from "node:child_process"
import { promisify } from "node:util"

const SAVE_SCRIPT = process.env.OPENCODE_OBSIDIAN_SAVE_SCRIPT ||
  join(homedir(), ".config", "opencode", "scripts", "opencode_save.py")
const OPENCODE_DATABASE = process.env.OPENCODE_DATABASE ||
  join(homedir(), ".local", "share", "opencode", "opencode.db")
const CURSOR_FILE = process.env.OPENCODE_OBSIDIAN_CURSOR ||
  join(homedir(), ".local", "state", "opencode-obsidian", "opencode_v2_cursor.json")
const execFileAsync = promisify(execFile)
const POLL_INTERVAL_MS = 2000
const POLL_SETTLE_MS = 1500
const MAX_ERROR_DELAY_MS = 30000
const INITIAL_LOOKBACK_MS = 10 * 60 * 1000
const REPLAY_WINDOW_MS = 10 * 1000
const SQLITE_MAX_BUFFER = 64 * 1024 * 1024
const CURSOR_SCHEMA_VERSION = 1

const sqlQuote = (value) => `'${String(value).replaceAll("'", "''")}'`

const runSqlite = async (query) => {
  const { stdout } = await execFileAsync(
    "/usr/bin/sqlite3",
    ["-readonly", "-json", OPENCODE_DATABASE, query],
    { maxBuffer: SQLITE_MAX_BUFFER },
  )
  return stdout.trim() ? JSON.parse(stdout) : []
}

const saveCursor = async (timeIdle) => {
  const temporaryFile = `${CURSOR_FILE}.tmp-${process.pid}`
  await mkdir(dirname(CURSOR_FILE), { recursive: true })
  await writeFile(
    temporaryFile,
    JSON.stringify({ schema_version: CURSOR_SCHEMA_VERSION, database: OPENCODE_DATABASE, time_idle: timeIdle }),
    "utf8",
  )
  await rename(temporaryFile, CURSOR_FILE)
}

const loadCursor = async () => {
  try {
    const state = JSON.parse(await readFile(CURSOR_FILE, "utf8"))
    if (
      state.schema_version === CURSOR_SCHEMA_VERSION &&
      state.database === OPENCODE_DATABASE &&
      Number.isFinite(state.time_idle)
    ) {
      return state.time_idle
    }
  } catch {
    // 初回起動、または壊れた状態ファイルの場合は現在時刻から監視を開始する。
  }
  const initialCursor = Date.now() - INITIAL_LOOKBACK_MS
  await saveCursor(initialCursor)
  return initialCursor
}

const validateDatabase = async () => {
  const tables = await runSqlite(`
    SELECT name
    FROM sqlite_master
    WHERE type = 'table' AND name IN ('session_v2', 'session_message')
  `)
  const tableNames = new Set(tables.map((table) => table.name))
  if (!tableNames.has("session_v2") || !tableNames.has("session_message")) {
    throw new Error("OpenCode v2のsession_v2/session_messageテーブルが見つかりません")
  }

  const columns = await runSqlite(`
    SELECT m.name AS table_name, p.name AS column_name
    FROM sqlite_master m
    JOIN pragma_table_info(m.name) p
    WHERE m.type = 'table' AND m.name IN ('session_v2', 'session_message')
  `)
  const required = {
    session_v2: new Set(["id", "directory", "time_created"]),
    session_message: new Set(["id", "session_id", "type", "seq", "data"]),
  }
  for (const row of columns) required[row.table_name]?.delete(row.column_name)
  for (const [table, missing] of Object.entries(required)) {
    if (missing.size) throw new Error(`${table}の必須列が不足しています: ${[...missing].join(", ")}`)
  }
}

const databaseMessages = async (sessionID) => {
  const rows = await runSqlite(`
    SELECT id, type, data
    FROM session_message
    WHERE session_id = ${sqlQuote(sessionID)}
      AND type IN ('user', 'assistant')
    ORDER BY seq
  `)
  return rows.map((row) => {
    try {
      const data = JSON.parse(row.data)
      return { ...data, id: row.id, type: row.type }
    } catch (error) {
      throw new Error(`session_message ${row.id} のJSON解析に失敗しました: ${String(error)}`)
    }
  })
}

const runV2Save = async (payload) => {
  const sessionID = payload.session_id
  if (!sessionID) throw new Error("session_idがありません")
  let temporaryDirectory
  try {
    temporaryDirectory = await mkdtemp(join(tmpdir(), "opencode-obsidian-"))
    const inputPath = join(temporaryDirectory, "payload.json")
    await writeFile(inputPath, JSON.stringify(payload), "utf8")
    await execFileAsync("/usr/bin/python3", [SAVE_SCRIPT, "--input", inputPath])
  } catch (error) {
    console.error(`opencode-obsidian: Obsidian 保存に失敗しました (${sessionID}): ${String(error)}`)
    throw error
  } finally {
    if (temporaryDirectory) await rm(temporaryDirectory, { recursive: true, force: true }).catch(() => {})
  }
}

const waitForPoll = (signal, delay) => new Promise((resolve) => {
  let timer
  const finish = () => {
    if (timer) clearTimeout(timer)
    signal?.removeEventListener("abort", finish)
    resolve()
  }
  timer = setTimeout(finish, delay)
  signal?.addEventListener("abort", finish, { once: true })
})

const pollDatabase = async (signal) => {
  let lastSeen = await loadCursor()
  let errorDelay = POLL_INTERVAL_MS
  let lastError = ""
  const processedIdleEvents = new Map()

  try {
    await validateDatabase()
  } catch (error) {
    console.error(`opencode-obsidian: OpenCode v2データベースを検証できません: ${String(error)}`)
  }

  while (!signal.aborted) {
    const upperBound = Date.now() - POLL_SETTLE_MS
    try {
      const idleEvents = await runSqlite(`
        SELECT idle.id AS idle_id, idle.time_created AS time_idle,
          s.id, s.directory, s.time_created
        FROM session_message idle
        JOIN session_v2 s ON s.id = idle.session_id
        WHERE idle.type = 'idle'
          AND idle.time_created > ${Math.max(0, lastSeen - REPLAY_WINDOW_MS)}
          AND idle.time_created <= ${upperBound}
          AND EXISTS (
            SELECT 1 FROM session_message m
            WHERE m.session_id = s.id AND m.type = 'user'
          )
        ORDER BY time_idle
      `)
      const sessions = new Map()
      for (const event of idleEvents) {
        if (processedIdleEvents.has(event.idle_id)) continue
        const session = sessions.get(event.id)
        if (session) {
          session.event_ids.push(event.idle_id)
          session.time_idle = Math.max(session.time_idle, event.time_idle)
          continue
        }
        sessions.set(event.id, {
          event_ids: [event.idle_id],
          time_idle: event.time_idle,
          directory: event.directory,
          time_created: event.time_created,
        })
      }
      for (const [sessionID, session] of sessions) {
        await runV2Save({
          schema_version: 2,
          session_id: sessionID,
          cwd: session.directory || process.cwd(),
          created: session.time_created || "",
          full_sync: true,
          messages: await databaseMessages(sessionID),
        })
        for (const eventID of session.event_ids) processedIdleEvents.set(eventID, session.time_idle)
      }
      if (idleEvents.length || upperBound > lastSeen) {
        await saveCursor(upperBound)
        lastSeen = upperBound
      }
      const retainedAfter = Math.max(0, lastSeen - REPLAY_WINDOW_MS)
      for (const [eventID, eventTime] of processedIdleEvents) {
        if (eventTime <= retainedAfter) processedIdleEvents.delete(eventID)
      }
      errorDelay = POLL_INTERVAL_MS
      lastError = ""
    } catch (error) {
      const message = String(error)
      if (message !== lastError) console.error(`opencode-obsidian: v2監視を一時停止します: ${message}`)
      lastError = message
      errorDelay = Math.min(errorDelay * 2, MAX_ERROR_DELAY_MS)
    }
    await waitForPoll(signal, errorDelay)
  }
}

const setupV2 = () => {
  const controller = new AbortController()
  void pollDatabase(controller.signal).catch((error) => {
    if (!controller.signal.aborted) console.error(`opencode-obsidian: v2監視を終了しました: ${String(error)}`)
  })
  return () => controller.abort()
}

export default {
  id: "opencode-obsidian",
  setup: setupV2,
}
