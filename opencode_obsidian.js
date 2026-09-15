// OpenCode の会話完了イベントを Obsidian 保存スクリプトへ渡すプラグイン。
import { mkdtemp, rm, writeFile } from "node:fs/promises"
import { homedir, tmpdir } from "node:os"
import { join } from "node:path"

const SAVE_SCRIPT = process.env.OPENCODE_OBSIDIAN_SAVE_SCRIPT ||
  join(homedir(), ".config", "opencode", "scripts", "opencode_save.py")

const OpenCodeObsidianPlugin = async ({ client, directory, $ }) => {
  const saveQueues = new Map()
  const drafts = new Map()

  const runSavePayload = async (payload) => {
    const sessionID = payload.session_id
    if (!sessionID) return
    let temporaryDirectory
    try {
      temporaryDirectory = await mkdtemp(join(tmpdir(), "opencode-obsidian-"))
      const inputPath = join(temporaryDirectory, "payload.json")
      await writeFile(inputPath, JSON.stringify(payload), "utf8")
      const result = await $`/usr/bin/python3 ${SAVE_SCRIPT} --input ${inputPath}`.nothrow().quiet()
      if (result.exitCode !== 0) {
        await client.app.log({
          body: { service: "opencode-obsidian", level: "error", message: `保存スクリプトが終了コード ${result.exitCode} で終了しました` },
        })
      }
    } catch (error) {
      await client.app.log({
        body: { service: "opencode-obsidian", level: "error", message: `Obsidian 保存に失敗しました: ${String(error)}` },
      }).catch(() => {})
    } finally {
      if (temporaryDirectory) await rm(temporaryDirectory, { recursive: true, force: true }).catch(() => {})
    }
  }

  const enqueueSave = (sessionID, payloadFactory) => {
    const previous = saveQueues.get(sessionID) || Promise.resolve()
    const current = previous
      .catch(() => {})
      .then(async () => runSavePayload(await payloadFactory()))
    saveQueues.set(sessionID, current)
    current.finally(() => {
      if (saveQueues.get(sessionID) === current) saveQueues.delete(sessionID)
    }).catch(() => {})
    return current
  }

  const fetchFullPayload = async (sessionID) => {
    const [messagesResponse, sessionResponse] = await Promise.all([
      client.session.messages({ path: { id: sessionID } }),
      client.session.get({ path: { id: sessionID } }),
    ])
    const messages = messagesResponse?.data ?? messagesResponse ?? []
    const session = sessionResponse?.data ?? sessionResponse ?? {}
    return {
      session_id: sessionID,
      cwd: session.directory || directory || process.cwd(),
      created: session.time?.created || "",
      full_sync: true,
      messages,
    }
  }

  return {
    "chat.message": async (input, output) => {
      const texts = (output.parts || [])
        .filter((part) => part?.type === "text" && typeof part.text === "string" && part.text.trim())
        .map((part) => ({ type: "text", text: part.text }))
      if (!texts.length) return
      drafts.set(input.sessionID, {
        session_id: input.sessionID,
        cwd: directory || process.cwd(),
        created: Date.now(),
        modelID: input.model?.modelID || "",
        providerID: input.model?.providerID || input.providerID || "",
        messages: [{
          info: {
            role: "user",
            id: output.message?.id || input.messageID || "",
            time: output.message?.time || { created: Date.now() },
          },
          parts: texts,
        }],
      })
    },
    "experimental.text.complete": async (input, output) => {
      const draft = drafts.get(input.sessionID)
      if (!draft || !output.text?.trim()) return
      draft.messages.push({
        info: {
          role: "assistant",
          modelID: draft.modelID,
          providerID: draft.providerID,
          time: { created: Date.now() },
          id: input.messageID,
        },
        parts: [{ type: "text", text: output.text }],
      })
      await enqueueSave(input.sessionID, async () => draft)
      drafts.delete(input.sessionID)
    },
    event: async ({ event }) => {
      if (event.type !== "session.idle") return
      const sessionID = event.properties?.sessionID
      if (!sessionID) return
      try {
        await enqueueSave(sessionID, () => fetchFullPayload(sessionID))
      } catch (error) {
        await client.app.log({
          body: { service: "opencode-obsidian", level: "error", message: `Obsidian 保存に失敗しました: ${String(error)}` },
        }).catch(() => {})
      }
    },
  }
}

export default {
  id: "opencode-obsidian",
  server: OpenCodeObsidianPlugin,
}
