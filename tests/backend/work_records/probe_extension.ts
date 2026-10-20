/** WR-01 feasibility only: synthetic context, no record storage or backend changes. */
import { appendFileSync, existsSync, readFileSync } from "node:fs";
import { isSubagentContext } from "../../../omp_bridge/g3/bridge.ts";

const wait = (ms: number) => new Promise(resolve => setTimeout(resolve, ms));
export default function (pi: any) {
  const role = process.env.WORKBENCH_G3_ROLE!;
  const trace = process.env.WR01_TRACE!;
  const ready = process.env.WR01_READY!;
  const log = (event: string, more: Record<string, unknown> = {}) =>
    appendFileSync(trace, JSON.stringify({ event, role, pid: process.pid, at: Date.now(), ...more }) + "\n");
  let generation = 0;
  let sessionId = "";
  let subagent = false;
  let pending: Promise<void> = Promise.resolve();
  pi.registerTool({ name: "workbench_records", label: "Synthetic records probe",
    description: "WR-01 synthetic fixture only, returns no stored records.",
    loadMode: "essential", parameters: { type: "object", properties: {}, additionalProperties: false },
    async execute(_id: string, _args: unknown, _signal: unknown, _update: unknown, ctx: any) {
      const refused = subagent || isSubagentContext(ctx);
      log("synthetic_tool_execute", { refused });
      return { content: [{ type: "text", text: refused ? "subagent_not_allowed" : "synthetic_only" }], isError: refused };
    } });
  const start = (event: string, ctx: any) => {
    subagent = isSubagentContext(ctx);
    log(event, { agent: ctx.agent, sessionId: String(ctx.sessionManager.getSessionId()),
      subagent, active: pi.getActiveTools(), hasUI: ctx.hasUI });
    if (subagent) {
      pending = Promise.resolve(pi.setActiveTools(pi.getActiveTools().filter((x: string) => x !== "workbench_records")))
        .then(() => log("subagent_hidden", { agent: ctx.agent, active: pi.getActiveTools() }));
      return pending;
    }
    const owner = ++generation;
    const id = sessionId = String(ctx.sessionManager.getSessionId());
    pending = (async () => {
      const deadline = Date.now() + 5000;
      while (Date.now() < deadline) {
        if (existsSync(ready) && JSON.parse(readFileSync(ready, "utf8")).sessionId === id) break;
        await wait(20);
      }
      if (!existsSync(ready) || JSON.parse(readFileSync(ready, "utf8")).sessionId !== id)
        throw new Error("WR01_bridge_not_ready");
      // Intentional bounded source latency makes the immediate first-input case meaningful.
      await wait(350);
      if (generation !== owner || sessionId !== id) { log("stale_discarded", { owner, id }); return; }
      const content = `WR01_PREVIEW:${role}:${id}:g${owner}\n최근 기록: 결정 α / **원문** 미리보기`;
      const injected = process.env.WR01_DISABLE_PREVIEW !== "1";
      let returned: unknown;
      if (injected) returned = pi.sendMessage({ customType: "wr01-recent", content, display: true }, { triggerTurn: false });
      log("preview_ready", { sessionId: id, generation: owner, injected,
        apiCalled: injected, returnType: injected ? typeof returned : "not_called", content });
    })();
    pending.catch(error => log("preview_error", { error: String(error) }));
    return pending;
  };
  pi.on("session_start", (_event: unknown, ctx: any) => start("session_start", ctx));
  pi.on("session_switch", (_event: unknown, ctx: any) => start("session_switch", ctx));
  pi.on("input", async (event: any, ctx: any) => {
    log("input_begin", { sessionId: String(ctx.sessionManager.getSessionId()), source: event.source });
    await pending;
    log("input_ready", { sessionId: String(ctx.sessionManager.getSessionId()) });
  });
  pi.on("agent_end", (_event: unknown, ctx: any) => log("agent_end", {
    sessionId: String(ctx.sessionManager.getSessionId()), subagent: isSubagentContext(ctx) }));
  pi.registerCommand("wr-delay", { description: "Schedule an old synthetic preview response for WR-01",
    handler: async (_args: string, ctx: any) => {
      const owner = generation;
      const id = String(ctx.sessionManager.getSessionId());
      log("delayed_started", { sessionId: id, generation: owner });
      void wait(1200).then(() => {
        if (generation !== owner || sessionId !== id) { log("delayed_discarded", { sessionId: id, generation: owner }); return; }
        pi.sendMessage({ customType: "wr01-stale", content: "WR01_STALE_OLD_RESPONSE", display: true }, { triggerTurn: false });
        log("delayed_injected", { sessionId: id });
      });
    } });
}
