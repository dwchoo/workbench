import { execFile } from "node:child_process";
import { createHash, randomUUID } from "node:crypto";
import { appendFileSync, readFileSync, unlinkSync, writeFileSync } from "node:fs";
import path from "node:path";
import { toolWireSchema } from "@oh-my-pi/pi-ai";
import { AgentRegistry, createAgentSession, SessionManager, Settings } from "@oh-my-pi/pi-coding-agent";

const trace = process.env.Q01_STREAM_TRACE!;
const permits = process.env.Q01_STREAM_PERMITS!;
const writer = process.env.Q01_FIXED_WRITER!;
const python = process.env.Q01_FIXED_PYTHON!;
const body = readFileSync(process.env.Q01_AGENT_BODY!, "utf8");
const skill = readFileSync(process.env.Q01_SUMMARY_SKILL!, "utf8");
const sha = (x: string) => createHash("sha256").update(x).digest("hex");
const stable = (x: any): string => JSON.stringify(x, (_key, value) =>
  value && typeof value === "object" && !Array.isArray(value)
    ? Object.fromEntries(Object.entries(value).sort(([a], [b]) => a.localeCompare(b))) : value);
const log = (x: Record<string, unknown>) => appendFileSync(trace, JSON.stringify({ pid: process.pid, ...x }) + "\n");
const errorResult = (reason: string) => ({ content: [{ type: "text", text: JSON.stringify({ saved: false, reason }) }], isError: true });
const expectedSchema = { type: "object", properties: { summary: { type: "string", minLength: 1, maxLength: 2000 } },
  required: ["summary"], additionalProperties: true };
const rawSchemaSha = sha(stable(expectedSchema));
const cases = ["normal", "retry", "checksend", "stale", "schema", "policy", "model",
  "missing", "noop", "bash"] as const;
const consumedNonces = new Set<string>();
type Receipt = { requestId: string; sessionId: string; epoch: string; nonce: string;
  rosterSha: string; schemaSha: string; policySha: string; modelKey: string };

async function run(ctx: any) {
  const parentBefore = ctx.sessionManager?.getEntries?.().length ?? null;
  log({ event: "begin", parentBefore, model: ctx.model && { provider: ctx.model.provider, id: ctx.model.id, api: ctx.model.api },
    bodySha: sha(body), skillSha: sha(skill), rawSchemaSha });
  if (!ctx.model) { log({ event: "done", ok: false, reason: "model_missing" }); return; }
  const modelKey = (model: any) => [model?.provider, model?.id, model?.api, model?.baseUrl].join("|");
  let ok = true;
  for (const caseName of cases) {
    const registry = new AgentRegistry();
    const settings = Settings.isolated({ externalThinking: false, "prewalk.enabled": false,
      "tools.intentTracing": false });
    const agentId = `q01-stream-${caseName}`;
    const policy = { revision: "v1" };
    const state = { armed: false, spent: false, saved: false, protected: false,
      receipt: undefined as Receipt | undefined, child: undefined as any,
      wrapperCalls: 0, originalCalls: 0 };
    const toolSchema = caseName === "schema"
      ? { ...expectedSchema, required: ["summary", "unexpected"] } : expectedSchema;
    const saveTool = {
      name: "db_summary_save", label: "Save summary", description: "Fixed private SQLite summary writer.",
      approval: "write" as const, loadMode: "essential" as const, intent: "omit" as const,
      parameters: toolSchema,
      async execute(callId: string, params: Record<string, unknown>) {
        log({ event: "execute_received", case: caseName, callId, params,
          armed: state.armed, spent: state.spent, protected: state.protected });
        const receipt = state.receipt;
        if (!receipt || !state.armed || state.spent || state.protected || !state.child)
          return errorResult("owner_binding");
        state.spent = true;
        if (String(state.child.sessionManager.getSessionId()) !== receipt.sessionId ||
            policy.revision !== "v1" || consumedNonces.has(receipt.nonce)) return errorResult("changed_binding");
        consumedNonces.add(receipt.nonce);
        if (!params || typeof params !== "object" || Array.isArray(params) ||
            Object.keys(params).length !== 1 || typeof params.summary !== "string" ||
            params.summary.length < 1 || params.summary.length > 2000) return errorResult("invalid_data");
        const text = JSON.stringify({ request_id: receipt.requestId, session_id: receipt.sessionId, saved: true });
        if (Buffer.byteLength(text, "utf8") > 8192) return errorResult("result_bytes");
        const argv = [writer, JSON.stringify({ summary: params.summary })];
        log({ event: "writer_invoked", case: caseName, executable: python, argv });
        const out = await new Promise<{ error: string | null; stdout: string }>(resolve =>
          execFile(python, argv, { timeout: 5000, maxBuffer: 4096, env: { PATH: process.env.PATH || "" } },
            (e, stdout) => resolve({ error: e ? String(e) : null, stdout })));
        if (out.error || out.stdout.trim() !== '{"saved":true}') return errorResult("writer_failed");
        state.saved = true;
        log({ event: "saved", case: caseName, receiptBytes: Buffer.byteLength(text, "utf8") });
        return { content: [{ type: "text", text }] };
      },
    };
    // Independent expected wire schema: the immutable owner descriptor, not llmContext input.
    const expectedSchemaSha = sha(stable(toolWireSchema({ ...saveTool, parameters: expectedSchema })));
    let child: any;
    let unsubscribe: (() => void) | undefined;
    let permitted = false;
    let abortPromise: Promise<void> | undefined;
    let forbiddenRefused = false;
    const permit = path.join(permits, caseName);
    try {
      child = (await createAgentSession({
        cwd: process.cwd(), model: ctx.model, settings, sessionManager: SessionManager.inMemory(process.cwd()),
        agentRegistry: registry, agentId, agentName: "task", bindProcessState: false,
        systemPrompt: [body, skill], cacheWarming: false, disableExtensionDiscovery: true,
        preloadedPreparedExtensions: [], enableMCP: false, enableLsp: false, enableIrc: false,
        skipPythonPreflight: true, skills: [], rules: [], contextFiles: [], promptTemplates: [], slashCommands: [],
        toolNames: ["db_summary_save"], restrictToolNames: true, allowRestrictedCustomTools: true,
        requireYieldTool: false, customTools: [saveTool], hasUI: false, autoApprove: true,
        deadline: Date.now() + 25000,
      })).session;
      state.child = child;
      const active = child.getEnabledToolNames();
      const sessionId = String(child.sessionManager.getSessionId());
      const receipt: Receipt = Object.freeze({ requestId: `q01-${caseName}`, sessionId,
        epoch: `epoch-${caseName}`, nonce: randomUUID(), rosterSha: sha("db_summary_save"),
        schemaSha: expectedSchemaSha, policySha: sha(stable(policy)), modelKey: modelKey(ctx.model) });
      log({ event: "initial_check", case: caseName, active, sessionId, receipt,
        globalHas: Boolean(AgentRegistry.global().get(agentId)), privateHas: Boolean(registry.get(agentId)) });
      if (active.join(",") !== "db_summary_save" || child.isDisposed) throw new Error("initial_not_sole_db");
      const original = child.agent.streamFn;
      if (typeof original !== "function") throw new Error("original_streamFn_missing");
      if (caseName === "missing") {
        // Negative control: no wrapper is installed. Its provider POST is evidence of non-enforcement.
        log({ event: "wrapper_absent_control", case: caseName });
      } else if (caseName === "noop") {
        // Negative control: a registered function that delegates without checking has no enforcement effect.
        child.agent.streamFn = (model: any, context: any, options: any) => {
          state.wrapperCalls++;
          state.originalCalls++;
          log({ event: "noop_wrapper", case: caseName, tools: context.tools?.map((x: any) => x.name) ?? [] });
          return original(model, context, options);
        };
      } else {
        child.agent.streamFn = (model: any, context: any, options: any) => {
          state.wrapperCalls++;
          const names = Array.isArray(context.tools) ? context.tools.map((x: any) => x.name) : [];
          const schemaSha = context.tools?.length === 1 ? sha(stable(context.tools[0].parameters)) : null;
          const actualModelKey = modelKey(model);
          const r = state.receipt;
          const reason = state.protected ? "already_protected"
            : !r || !state.armed || child.isDisposed ? "owner_inactive"
            : String(child.sessionManager.getSessionId()) !== r.sessionId || r.requestId !== receipt.requestId ||
              r.epoch !== receipt.epoch || r.nonce !== receipt.nonce ? "stale_binding"
            : names.length !== 1 || names[0] !== "db_summary_save" ||
              sha(names.join("\n")) !== r.rosterSha ? "roster_mismatch"
            : schemaSha !== expectedSchemaSha || r.schemaSha !== expectedSchemaSha ? "schema_mismatch"
            : sha(stable(policy)) !== r.policySha ? "policy_mismatch"
            : actualModelKey !== r.modelKey ? "model_mismatch" : undefined;
          log({ event: "stream_guard", case: caseName, call: state.wrapperCalls, names,
            schemaSha, expectedSchemaSha, actualSchema: reason === "schema_mismatch"
              ? context.tools?.[0]?.parameters : undefined,
            modelKey: actualModelKey, reason, protected: state.protected });
          if (reason) {
            state.protected = true;
            state.armed = false;
            throw new Error(`q01_stream_guard_refused:${reason}`);
          }
          state.originalCalls++;
          log({ event: "original_stream_called", case: caseName, call: state.originalCalls });
          return original(model, context, options);
        };
      }
      // The logical request is admitted once; provider continuations and SDK retry use the same receipt.
      state.receipt = receipt;
      state.armed = true;
      log({ event: "final_gate", case: caseName, allowed: true, active: child.getEnabledToolNames() });
      if (caseName === "stale") state.receipt = { ...receipt, sessionId: "stale-session" };
      if (caseName === "policy") policy.revision = "v2";
      if (caseName === "model") {
        const alternate = child.modelRegistry.find("q01streamresp", "scripted");
        if (!alternate) throw new Error("alternate_model_missing");
        await child.setModelTemporary(alternate);
        log({ event: "controlled_model_change", case: caseName, active: child.getEnabledToolNames(),
          modelKey: modelKey(child.model) });
      }
      if (["checksend", "missing", "noop"].includes(caseName)) {
        log({ event: "fault_after_gate_before_prompt", case: caseName,
          kind: "public_setActiveToolsByName_empty" });
        await child.setActiveToolsByName([]);
        log({ event: "fault_applied", case: caseName, active: child.getEnabledToolNames() });
      }
      unsubscribe = child.subscribe((event: any) => {
        if (event.type === "tool_execution_end" && event.toolName === "bash" && event.isError === true) {
          forbiddenRefused = true;
          state.armed = false;
          state.protected = true;
          log({ event: "abort_requested", case: caseName });
          abortPromise = child.abort({ goalReason: "internal", reason: "q01_forbidden_tool" })
            .then(() => log({ event: "abort_completed", case: caseName }))
            .catch((e: unknown) => log({ event: "abort_failed", case: caseName, error: String(e) }));
        }
        if (event.type === "tool_execution_end")
          log({ event: "tool_end", case: caseName, toolName: event.toolName, isError: event.isError,
            result: (event.result?.content || []).filter((x: any) => x.type === "text").map((x: any) => x.text).join("").slice(0, 180) });
      });
      writeFileSync(permit, receipt.requestId);
      permitted = true;
      log({ event: "prompt_start", case: caseName, active: child.getEnabledToolNames() });
      try {
        await child.prompt(`q01-case:${caseName} literal summary request`, { runCommands: false, throwOnDrop: true });
      } catch (e) {
        log({ event: "prompt_error", case: caseName, error: String(e) });
      }
      if (abortPromise) await abortPromise;
      log({ event: "prompt_done", case: caseName, saved: state.saved, spent: state.spent,
        wrapperCalls: caseName === "missing" ? null : state.wrapperCalls,
        originalCalls: caseName === "missing" ? null : state.originalCalls,
        streamCounter: caseName === "missing" ? "uninstrumented"
          : caseName === "noop" ? "noop_delegate" : "guarded_wrapper",
        protected: state.protected });
      if (["normal", "retry"].includes(caseName)) {
        if (!state.saved) throw new Error("expected_save_missing");
        log({ event: "result_accepted", case: caseName });
      } else {
        if (state.saved) throw new Error("negative_saved");
        if (caseName === "bash" && !forbiddenRefused) throw new Error("forbidden_tool_not_refused");
        log({ event: "result_rejected", case: caseName });
      }
    } catch (e) {
      ok = false;
      log({ event: "case_error", case: caseName, error: String(e), stack: String((e as Error)?.stack || "").slice(0, 1000) });
      break;
    } finally {
      state.armed = false;
      state.receipt = undefined;
      state.child = undefined;
      if (permitted) { try { unlinkSync(permit); } catch {} }
      unsubscribe?.();
      if (child) { await child.dispose(); log({ event: "disposed", case: caseName }); }
    }
  }
  log({ event: "done", ok, parentBefore, parentAfter: ctx.sessionManager?.getEntries?.().length ?? null });
}

export default function streamGuardProbe(pi: any) {
  pi.on("session_start", (_: unknown, ctx: any) => { void run(ctx); });
}
