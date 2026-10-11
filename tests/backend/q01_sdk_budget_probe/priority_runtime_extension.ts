/** Probe-only capture of the unchanged budget child callback in a real Manager TUI. */
import { appendFileSync, readFileSync } from "node:fs";
import budgetExtension from "./budget_extension.ts";

const trace = process.env.Q01_PRIORITY_RUNTIME_TRACE!;
const control = process.env.Q01_PRIORITY_RUNTIME_CONTROL!;
const log = (event: string, fields: Record<string, unknown> = {}) =>
  appendFileSync(trace, JSON.stringify({ event, monotonicNs: process.hrtime.bigint().toString(),
    pid: process.pid, ...fields }) + "\n");

export default function priorityRuntimeExtension(pi: any) {
  let captured: ((event: unknown, ctx: any) => void) | undefined;
  budgetExtension({
    on(event: string, callback: (event: unknown, ctx: any) => void) {
      if (event !== "session_start" || captured) throw new Error("unexpected_budget_handler");
      captured = callback;
    },
  });
  if (!captured) throw new Error("budget_start_callback_missing");
  pi.on("session_start", (event: unknown, ctx: any) => {
    log("manager_session_start", { mode: ctx.mode, hasUI: ctx.hasUI,
      parentSessionId: String(ctx.sessionManager.getSessionId()),
      activeTools: pi.getActiveTools().map((tool: any) => tool.name) });
    const timer = setInterval(() => {
      let signal: string;
      try { signal = readFileSync(control, "utf8").trim(); }
      catch { return; }
      if (signal !== "start") return;
      clearInterval(timer);
      log("budget_callback_invoked", { mode: ctx.mode, hasUI: ctx.hasUI,
        activeTools: pi.getActiveTools().map((tool: any) => tool.name) });
      captured!(event, ctx);
    }, 25);
  });
}
