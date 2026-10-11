import { execFile } from "node:child_process";
import { createHash } from "node:crypto";
import { appendFileSync, readFileSync } from "node:fs";
import { toolWireSchema } from "@oh-my-pi/pi-ai";
import { AgentRegistry, createAgentSession, SessionManager, Settings } from "@oh-my-pi/pi-coding-agent";
import { getBundledAgent } from "@oh-my-pi/pi-coding-agent/task/agents";

const trace = process.env.Q01_CONTEXT_TRACE!;
const receipt = process.env.Q01_CONTEXT_RECEIPT!;
const log = (event: string, more: Record<string, unknown> = {}) =>
  appendFileSync(trace, JSON.stringify({ event, pid: process.pid, ...more }) + "\n");
const sha = (value: string) => createHash("sha256").update(value).digest("hex");
const stable = (x: any): string => JSON.stringify(x, (_key, value) =>
  value && typeof value === "object" && !Array.isArray(value)
    ? Object.fromEntries(Object.entries(value).sort(([a], [b]) => a.localeCompare(b))) : value);

async function runCycle(pi: any, ctx: any, cycle: number) {
  const bundled = getBundledAgent("task");
  if (!bundled || bundled.source !== "bundled" || bundled.filePath !== "embedded:task.md")
    throw new Error("bundled_task_unavailable");
  const skill = readFileSync(process.env.Q01_CONTEXT_SKILL!, "utf8");
  const summary = `Q01-private-raw-marker-${cycle}-REMOVE-FROM-PARENT`; // child-only source fixture
  const requestId = `q01-context-${cycle}`;
  const beforeRoster = pi.getActiveTools();
  const schema = { type: "object", properties: { summary: { type: "string", minLength: 1, maxLength: 2000 } },
    required: ["summary"], additionalProperties: true };
  const state = { armed: false, spent: false, saved: false, protected: false, sessionId: "" };
  const saveTool = { name: "db_summary_save", label: "Save summary", description: "Fixed private SQLite writer.",
    approval: "write" as const, loadMode: "essential" as const, intent: "omit" as const,
    parameters: schema,
    async execute(_callId: string, params: Record<string, unknown>) {
      log("execute", { cycle, params, armed: state.armed, spent: state.spent });
      if (!state.armed || state.spent || state.protected || !state.sessionId ||
          !params || typeof params !== "object" || Array.isArray(params) ||
          Object.keys(params).length !== 1 || typeof params.summary !== "string" ||
          params.summary.length < 1 || params.summary.length > 2000)
        return { content: [{ type: "text", text: '{"saved":false}' }], isError: true };
      state.spent = true;
      const argv = [process.env.Q01_CONTEXT_WRITER!, JSON.stringify({ summary: params.summary })];
      const output = await new Promise<{ error: string | null; stdout: string }>(resolve =>
        execFile(process.env.Q01_CONTEXT_PYTHON!, argv,
          { timeout: 5000, maxBuffer: 4096, env: { PATH: process.env.PATH || "" } },
          (error, stdout) => resolve({ error: error ? String(error) : null, stdout })));
      log("writer", { cycle, executable: process.env.Q01_CONTEXT_PYTHON, argv,
        error: output.error, stdout: output.stdout.trim() });
      if (output.error || output.stdout.trim() !== '{"saved":true}')
        return { content: [{ type: "text", text: '{"saved":false}' }], isError: true };
      state.saved = true;
      return { content: [{ type: "text", text: JSON.stringify({ saved: true, requestId }) }] };
    },
  };
  let child: any;
  let admitted = false;
  const agentId = `q01-context-${cycle}`;
  const registry = new AgentRegistry();
  const settings = Settings.isolated({ externalThinking: false, "prewalk.enabled": false,
    "tools.intentTracing": false, "retry.usageAwareFallback": false });
  try {
    child = (await createAgentSession({ cwd: process.cwd(), model: ctx.model, settings,
      sessionManager: SessionManager.inMemory(process.cwd()), agentRegistry: registry, agentId,
      agentName: "task", bindProcessState: false, systemPrompt: [bundled.systemPrompt, skill],
      cacheWarming: false, disableExtensionDiscovery: true, preloadedPreparedExtensions: [],
      enableMCP: false, enableLsp: false, enableIrc: false, skipPythonPreflight: true,
      skills: [], rules: [], contextFiles: [], promptTemplates: [], slashCommands: [],
      toolNames: ["db_summary_save"], restrictToolNames: true, allowRestrictedCustomTools: true,
      requireYieldTool: false, customTools: [saveTool], hasUI: false, autoApprove: true,
      deadline: Date.now() + 25000 })).session;
    state.sessionId = String(child.sessionManager.getSessionId());
    const selected = child.model;
    const modelMatch = selected?.id === ctx.model?.id && selected?.api === ctx.model?.api &&
      selected?.provider === ctx.model?.provider;
    const active = child.getEnabledToolNames();
    const expectedSchema = sha(stable(toolWireSchema(saveTool)));
    const original = child.agent.streamFn;
    const check = (model: any, context: any) => {
      const names = Array.isArray(context?.tools) ? context.tools.map((x: any) => x.name) : [];
      return !state.armed || state.protected || child.isDisposed ||
        String(child.sessionManager.getSessionId()) !== state.sessionId ? "owner_inactive" :
        names.length !== 1 || names[0] !== "db_summary_save" ? "roster_mismatch" :
        sha(stable(context.tools[0].parameters)) !== expectedSchema ? "schema_mismatch" :
        model?.provider !== ctx.model?.provider || model?.id !== ctx.model?.id ||
        model?.api !== ctx.model?.api ? "model_mismatch" : undefined;
    };
    const wrapper = (model: any, context: any, options: any) => {
      const reason = check(model, context);
      log("stream_guard", { cycle, names: context?.tools?.map((x: any) => x.name), reason });
      if (reason) { state.armed = false; state.protected = true; throw new Error("q01_context_guard:" + reason); }
      return original(model, context, options);
    };
    child.agent.streamFn = wrapper;
    const validContext = { tools: [{ name: "db_summary_save", parameters: toolWireSchema(saveTool) }] };
    const wrongContext = { tools: [{ name: "bash", parameters: toolWireSchema(saveTool) }] };
    state.armed = true;
    const valid = check(ctx.model, validContext);
    const wrong = check(ctx.model, wrongContext);
    const oldSession = state.sessionId;
    state.sessionId = "stale";
    const stale = check(ctx.model, validContext);
    state.sessionId = oldSession;
    admitted = child.settings === settings && registry.get(agentId) && !AgentRegistry.global().get(agentId) &&
      active.length === 1 && active[0] === "db_summary_save" && modelMatch &&
      typeof original === "function" && child.agent.streamFn === wrapper &&
      valid === undefined && wrong === "roster_mismatch" && stale === "owner_inactive";
    log("startup", { cycle, admitted: Boolean(admitted), active, modelMatch, valid, wrong, stale,
      sessionId: state.sessionId, bodySha: sha(bundled.systemPrompt), skillSha: sha(skill),
      source: bundled.source, filePath: bundled.filePath });
    if (!admitted) throw new Error("startup_selfcheck_failed");
    log("child_prompt", { cycle, requestId, rawMarkerSha: sha(summary), parentRoster: beforeRoster });
    await child.prompt(`q01-context-child:${cycle}; source: ${summary}; summarize with db_summary_save exactly once`,
      { runCommands: false, throwOnDrop: true });
    if (!state.saved || state.protected) throw new Error("child_save_missing_or_protected");
    const compact = JSON.stringify({ kind: "summary_result", requestId, status: "accepted",
      summarySha: sha(summary) });
    if (Buffer.byteLength(compact, "utf8") > 4096) throw new Error("receipt_too_long");
    pi.sendMessage({ customType: "q01-compact-receipt", content: compact, display: false,
      attribution: "agent" }, { triggerTurn: false });
    log("cycle_receipt_sent", { cycle, receipt: compact, bytes: Buffer.byteLength(compact, "utf8"),
      rawMarker: summary, parentRosterBefore: beforeRoster, parentRosterAfter: pi.getActiveTools() });
  } catch (error) {
    log("cycle_failed", { cycle, admitted: Boolean(admitted), saved: state.saved, error: String(error) });
  } finally {
    state.armed = false;
    if (child) { await child.dispose(); log("child_disposed", { cycle }); }
  }
}

export default function (pi: any) {
  if (process.env.Q01_CONTEXT_MODE === "cycles") {
    let cycle = 0;
    let busy = false;
    const advance = (ctx: any) => {
      if (busy || cycle >= 3) return;
      busy = true;
      cycle += 1;
      void runCycle(pi, ctx, cycle).catch(error => log("fatal", { error: String(error) }))
        .finally(() => { busy = false; });
    };
    pi.on("session_start", (_event: unknown, ctx: any) => advance(ctx));
    pi.on("agent_end", (_event: unknown, ctx: any) => advance(ctx));
    return;
  }
  pi.on("session_start", (_event: unknown, ctx: any) => {
    log("session_start", { activeTools: pi.getActiveTools(), entries: ctx.sessionManager.getEntries().length });
    pi.sendMessage({ customType: "q01-compact-receipt", content: receipt, display: false,
      attribution: "agent" }, { triggerTurn: false });
    log("sendMessage_called", { receipt });
  });
}
