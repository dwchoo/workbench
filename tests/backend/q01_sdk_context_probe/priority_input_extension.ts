/** Interactive fixture gate around the unchanged public context child callback. */
import { appendFileSync, readFileSync } from "node:fs";
import { createHash } from "node:crypto";
import contextExtension from "./context_extension.ts";

const trace = process.env.Q01_PRIORITY_TRACE!;
const control = process.env.Q01_PRIORITY_CONTROL!;
const log = (event: string, more: Record<string, unknown> = {}) =>
  appendFileSync(trace, JSON.stringify({ event, pid: process.pid, ...more }) + "\n");
const sha = (value: string) => createHash("sha256").update(value).digest("hex");

export default function priorityInputProbe(pi: any) {
  let startCallback: ((event: unknown, ctx: any) => void) | undefined;
  contextExtension({
    on(event: string, callback: (event: unknown, ctx: any) => void) {
      if (event === "session_start") startCallback = callback;
      else if (event === "agent_end") log("automatic_cycle_not_registered");
      else throw new Error("unexpected_context_handler:" + event);
    },
    getActiveTools: (...args: unknown[]) => pi.getActiveTools(...args),
    sendMessage: (...args: unknown[]) => pi.sendMessage(...args),
  });
  if (!startCallback) throw new Error("context_start_callback_missing");
  pi.on("agent_end", () => log("main_agent_end"));
  pi.on("session_start", (event: unknown, ctx: any) => {
    let lastSignal = 0;
    let dispatched = false;
    log("interactive_session", { mode: ctx.mode, hasUI: ctx.hasUI,
      editorGetter: typeof ctx.ui?.getEditorText, idleGetter: typeof ctx.isIdle,
      pendingGetter: typeof ctx.hasPendingMessages, activeTools: pi.getActiveTools() });
    const timer = setInterval(() => {
      if (dispatched) return;
      let signal: number;
      try { signal = Number(readFileSync(control, "utf8").trim()); }
      catch { return; }
      if (!Number.isInteger(signal) || signal <= lastSignal || signal > 3) return;
      lastSignal = signal;
      try {
        const editor = ctx.ui.getEditorText();
        const idle = ctx.isIdle();
        const pending = ctx.hasPendingMessages();
        const ready = ctx.mode === "tui" && ctx.hasUI === true && editor === "" &&
          idle === true && pending === false;
        log("admission_attempt", { attempt: signal, mode: ctx.mode, hasUI: ctx.hasUI,
          editor, editorBytes: Buffer.byteLength(editor, "utf8"), editorSha: sha(editor),
          idle, pending, ready, childCallbackCalls: dispatched ? 1 : 0 });
        if (!ready) { log("deferred", { attempt: signal, reason: editor !== "" ? "draft" :
          !idle ? "busy" : pending ? "pending" : "ui_unavailable" }); return; }
        if (signal !== 3) { log("unexpected_early_ready", { attempt: signal }); return; }
        dispatched = true;
        clearInterval(timer);
        log("captured_callback_invoked", { attempt: signal });
        startCallback!(event, ctx);
      } catch (error) {
        log("admission_error", { attempt: signal, error: String(error) });
      }
    }, 25);
  });
}
