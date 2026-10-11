/** Probe-local pause admission before the unchanged canonical contract callback. */
import { appendFileSync, readFileSync } from "node:fs";
import contractExtension from "./contract_extension.ts";

const trace = process.env.Q01_RECOVERY_TRACE!;
const control = process.env.Q01_RECOVERY_CONTROL!;
const pause = process.env.Q01_RECOVERY_PAUSE!;
const log = (event: string, more: Record<string, unknown> = {}) =>
  appendFileSync(trace, JSON.stringify({ event, pid: process.pid,
    monotonicNs: Number(process.hrtime.bigint()), ...more }) + "\n");

export default function recoveryProbe(pi: any) {
  let originalStart: ((event: unknown, ctx: any) => void) | undefined;
  contractExtension({
    on(event: string, callback: (event: unknown, ctx: any) => void) {
      if (event !== "session_start") throw new Error("unexpected_contract_handler:" + event);
      originalStart = callback;
    },
    appendEntry: (...args: unknown[]) => pi.appendEntry(...args),
  });
  if (!originalStart) throw new Error("contract_start_callback_missing");
  pi.on("session_start", (event: unknown, ctx: any) => {
    let lastSignal = 0;
    let invoked = false;
    log("session_start_captured", { mode: ctx.mode });
    const timer = setInterval(() => {
      if (invoked) return;
      let signal: number;
      try { signal = Number(readFileSync(control, "utf8").trim()); }
      catch { return; }
      if (!Number.isInteger(signal) || signal <= lastSignal || signal > 3) return;
      lastSignal = signal;
      let state: string;
      try { state = readFileSync(pause, "utf8").trim(); }
      catch { state = "load_failed"; }
      const admitted = state === "run";
      const reason = state === "paused" ? "paused" : admitted ? null : "load_failed";
      log("admission_attempt", { attempt: signal, state, admitted, reason });
      if (!admitted) { log("deferred", { attempt: signal, reason, childCallbackCalls: 0 }); return; }
      invoked = true;
      clearInterval(timer);
      log("captured_callback_invoked", { attempt: signal });
      originalStart!(event, ctx);
    }, 25);
  });
}
