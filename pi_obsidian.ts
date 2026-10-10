/** Pi 1.0+ の会話完了時に、標準Pythonの単独saverへsnapshotを渡す。 */
import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";
import { spawn } from "node:child_process";
import { homedir } from "node:os";
import { join } from "node:path";

export default function (pi: ExtensionAPI) {
  let queue = Promise.resolve();
  const save = (_event: unknown, ctx: ExtensionContext) => {
    const manager = ctx.sessionManager;
    // snapshotはイベント時に確定し、session切替後の履歴と混ぜない。
    const payload = JSON.stringify({ session_id: manager.getSessionId(), cwd: manager.getCwd(),
      created: manager.getHeader()?.timestamp, entries: manager.getEntries(), branch: manager.getBranch() });
    const script = process.env.PI_OBSIDIAN_SCRIPT ?? join(homedir(), ".pi", "agent", "scripts", "pi_save.py");
    queue = queue.then(() => new Promise<void>((resolve) => {
      const child = spawn("/usr/bin/python3", [script], { stdio: ["pipe", "ignore", "ignore"] });
      let done = false;
      const finish = (failed: boolean) => {
        if (done) return;
        done = true;
        clearTimeout(timer);
        if (failed && ctx.hasUI) ctx.ui.notify("ObsidianへのPi会話保存に失敗しました。保存ログを確認してください。", "warning");
        resolve();
      };
      const timer = setTimeout(() => { child.kill("SIGKILL"); finish(true); }, 10000);
      child.on("error", () => finish(true));
      child.on("close", code => finish(code !== 0));
      child.stdin.on("error", () => finish(true));
      child.stdin.end(payload);
    }));
    return queue;
  };
  pi.on("agent_settled", save);
  pi.on("session_tree", save);
  pi.on("session_shutdown", save);
}
