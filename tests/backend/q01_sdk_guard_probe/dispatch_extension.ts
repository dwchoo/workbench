import { appendFileSync } from "node:fs";
import { createAgentSession, SessionManager, Settings } from "@oh-my-pi/pi-coding-agent";

const tracePath = process.env.Q01_DISPATCH_TRACE;

function log(item: Record<string, unknown>) {
  if (tracePath) appendFileSync(tracePath, JSON.stringify({ pid: process.pid, ...item }) + "\n");
}

const saveTool = {
  name: "db_summary_save", label: "Save summary", description: "Fixed summary writer fixture; never called in this probe.",
  approval: "write" as const, loadMode: "essential" as const,
  parameters: { type: "object", properties: { summary: { type: "string" } }, required: ["summary"], additionalProperties: false },
  async execute(callId: string) {
    log({ event: "unexpected_save_execute", callId });
    return { content: [{ type: "text", text: "unexpected invocation" }], isError: true };
  },
};

async function makeChild(model: any, settings: any) {
  return (await createAgentSession({
    cwd: process.cwd(), model, settings, sessionManager: SessionManager.inMemory(process.cwd()),
    agentId: "q01-dispatch-guard", agentName: "task", bindProcessState: false,
    systemPrompt: "Guard roster fixture", cacheWarming: false, disableExtensionDiscovery: true,
    preloadedPreparedExtensions: [], enableMCP: false, enableLsp: false, enableIrc: false,
    skipPythonPreflight: true, skills: [], rules: [], contextFiles: [], promptTemplates: [], slashCommands: [],
    toolNames: ["db_summary_save"], restrictToolNames: true, allowRestrictedCustomTools: true,
    requireYieldTool: false, customTools: [saveTool], hasUI: false, autoApprove: true,
    deadline: Date.now() + 15000,
  })).session;
}

async function run(ctx: any) {
  log({ event: "run_begin", model: ctx.model ? { id: ctx.model.id, provider: ctx.model.provider, api: ctx.model.api } : null });
  if (!ctx.model) {
    log({ event: "run_done", ok: false, reason: "model_missing" });
    return;
  }
  const parentBefore = ctx.sessionManager?.getEntries?.().length ?? null;
  let guardChild: any;
  try {
    const settings = Settings.isolated({ externalThinking: true });
    guardChild = await makeChild(ctx.model, settings);
    const initial = guardChild.getEnabledToolNames();
    log({ event: "guard_readback", phase: "before_model_change", active: initial,
      modelApi: guardChild.model?.api, sessionId: guardChild.sessionManager.getSessionId() });
    if (initial.length !== 1 || initial[0] !== "db_summary_save") {
      log({ event: "guard_result", kind: "initial_roster_not_sole", active: initial });
    } else {
      try {
        const alternate = guardChild.modelRegistry.find("q01guardresp", "scripted");
        if (!alternate) throw new Error("registered_response_model_missing");
        log({ event: "alternate_model", provider: alternate.provider, id: alternate.id, api: alternate.api,
          reasoning: alternate.reasoning });
        await guardChild.setModelTemporary(alternate);
        const changed = guardChild.getEnabledToolNames();
        log({ event: "guard_readback", phase: "after_model_change", active: changed,
          modelApi: guardChild.model?.api, sessionId: guardChild.sessionManager.getSessionId() });
        log({ event: "guard_result", kind: changed.includes("think") ? "think_expanded" : "no_think_expansion",
          active: changed });
      } catch (error) {
        log({ event: "guard_result", kind: "model_change_error", error: String(error).slice(0, 1600) });
      }
    }
    await guardChild.dispose();
    guardChild = undefined;
    log({ event: "guard_disposed" });
    const parentAfter = ctx.sessionManager?.getEntries?.().length ?? null;
    log({ event: "run_done", ok: true, parentEntriesBefore: parentBefore, parentEntriesAfter: parentAfter });
  } catch (error) {
    log({ event: "run_done", ok: false, error: String(error).slice(0, 1800), stack: String((error as Error)?.stack || "").slice(0, 3000) });
  } finally {
    if (guardChild) { await guardChild.dispose(); log({ event: "guard_disposed_after_error" }); }
  }
}

export default function dispatchProbe(pi: any) {
  pi.on("session_start", (_event: unknown, ctx: any) => { void run(ctx); });
}
