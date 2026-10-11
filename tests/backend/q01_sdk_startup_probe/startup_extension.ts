import { execFile } from "node:child_process";
import { createHash, randomUUID } from "node:crypto";
import { appendFileSync, readFileSync, unlinkSync, writeFileSync } from "node:fs";
import path from "node:path";
import { toolWireSchema } from "@oh-my-pi/pi-ai";
import { AgentRegistry, createAgentSession, SessionManager, Settings } from "@oh-my-pi/pi-coding-agent";

const trace = process.env.Q01_STARTUP_TRACE!;
const permits = process.env.Q01_STARTUP_PERMITS!;
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
const cases = ["normal", "recover_missing", "recover_noop", "recover_api", "recover_stale", "recover_exception",
  "repeat_fail", "runtime_roster", "bash"] as const;
const positives = new Set(["normal", "recover_missing", "recover_noop", "recover_api", "recover_stale", "recover_exception"]);
const schema = { type: "object", properties: { summary: { type: "string", minLength: 1, maxLength: 2000 } },
  required: ["summary"], additionalProperties: true };
const consumed = new Set<string>();
type Receipt = { requestId: string; sessionId: string; epoch: string; route: string; nonce: string;
  rosterSha: string; schemaSha: string; policySha: string; modelKey: string };
const modelKey = (model: any) => [model?.provider, model?.id, model?.api, model?.baseUrl].join("|");

async function run(ctx: any) {
  const parentBefore = ctx.sessionManager?.getEntries?.().length ?? null;
  log({ event: "begin", parentBefore, model: ctx.model && { provider: ctx.model.provider, id: ctx.model.id, api: ctx.model.api },
    bodySha: sha(body), skillSha: sha(skill) });
  if (!ctx.model) { log({ event: "done", ok: false, reason: "model_missing" }); return; }
  let ok = true;
  for (const caseName of cases) {
    let admitted = false;
    let saved = false;
    let protectedResult = false;
    let forbiddenRefused = false;
    let abortPromise: Promise<void> | undefined;
    let attemptCount = 0;
    let promptCount = 0;
    for (let attempt = 1; attempt <= 2; attempt++) {
      attemptCount++;
      const registry = new AgentRegistry();
      const settings = Settings.isolated({ externalThinking: false, "prewalk.enabled": false,
        "tools.intentTracing": false });
      const agentId = "q01-startup-" + caseName + "-" + attempt;
      const policy = { revision: "v1" };
      const state = { armed: false, spent: false, saved: false, protected: false,
        receipt: undefined as Receipt | undefined, child: undefined as any };
      const saveTool = {
        name: "db_summary_save", label: "Save summary", description: "Fixed private SQLite summary writer.",
        approval: "write" as const, loadMode: "essential" as const, intent: "omit" as const,
        parameters: schema,
        async execute(callId: string, params: Record<string, unknown>) {
          log({ event: "execute_received", case: caseName, attempt, callId, params,
            armed: state.armed, spent: state.spent, protected: state.protected });
          const r = state.receipt;
          if (!r || !state.armed || state.spent || state.protected || !state.child) return errorResult("owner_binding");
          state.spent = true;
          if (String(state.child.sessionManager.getSessionId()) !== r.sessionId ||
              sha(stable(policy)) !== r.policySha || consumed.has(r.nonce)) return errorResult("changed_binding");
          consumed.add(r.nonce);
          if (!params || typeof params !== "object" || Array.isArray(params) ||
              Object.keys(params).length !== 1 || typeof params.summary !== "string" ||
              params.summary.length < 1 || params.summary.length > 2000) return errorResult("invalid_data");
          const text = JSON.stringify({ request_id: r.requestId, session_id: r.sessionId, saved: true });
          if (Buffer.byteLength(text, "utf8") > 8192) return errorResult("result_bytes");
          const argv = [writer, JSON.stringify({ summary: params.summary })];
          log({ event: "writer_invoked", case: caseName, attempt, executable: python, argv });
          const out = await new Promise<{ error: string | null; stdout: string }>(resolve =>
            execFile(python, argv, { timeout: 5000, maxBuffer: 4096, env: { PATH: process.env.PATH || "" } },
              (e, stdout) => resolve({ error: e ? String(e) : null, stdout })));
          if (out.error || out.stdout.trim() !== '{"saved":true}') return errorResult("writer_failed");
          state.saved = true;
          log({ event: "saved", case: caseName, attempt, receiptBytes: Buffer.byteLength(text, "utf8") });
          return { content: [{ type: "text", text }] };
        },
      };
      const expectedSchemaSha = sha(stable(toolWireSchema(saveTool)));
      let child: any;
      let unsubscribe: (() => void) | undefined;
      let permit = false;
      const permitPath = path.join(permits, caseName);
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
        log({ event: "session_created", case: caseName, attempt, sessionId });
        const receipt: Receipt = Object.freeze({ requestId: "q01-" + caseName, sessionId,
          epoch: "epoch-" + caseName + "-" + attempt, route: "private-manager-task",
          nonce: randomUUID(), rosterSha: sha("db_summary_save"), schemaSha: expectedSchemaSha,
          policySha: sha(stable(policy)), modelKey: modelKey(ctx.model) });
        state.receipt = caseName === "recover_stale" && attempt === 1
          ? { ...receipt, sessionId: "stale-session" } : receipt;
        state.armed = true;
        const original = child.agent.streamFn;
        const check = (model: any, context: any): string | undefined => {
          const r = state.receipt;
          const names = Array.isArray(context?.tools) ? context.tools.map((x: any) => x.name) : [];
          const schemaSha = context?.tools?.length === 1 ? sha(stable(context.tools[0].parameters)) : null;
          return state.protected || !state.armed || !r || child.isDisposed ? "owner_inactive"
            : String(child.sessionManager.getSessionId()) !== r.sessionId || r.requestId !== receipt.requestId ||
              r.epoch !== receipt.epoch || r.route !== receipt.route || r.nonce !== receipt.nonce ? "stale_binding"
            : names.length !== 1 || names[0] !== "db_summary_save" || sha(names.join("\n")) !== r.rosterSha
              ? "roster_mismatch"
            : schemaSha !== expectedSchemaSha || r.schemaSha !== expectedSchemaSha ? "schema_mismatch"
            : sha(stable(policy)) !== r.policySha ? "policy_mismatch"
            : modelKey(model) !== r.modelKey ? "model_mismatch" : undefined;
        };
        const activeCheck = caseName === "recover_exception" && attempt === 1
          ? (_model: any, _context: any): string | undefined => { throw new Error("controlled_startup_checker_throw"); }
          : caseName === "recover_noop" && attempt === 1 || caseName === "repeat_fail"
            ? (_model: any, _context: any) => undefined : check;
        const wrapper = (model: any, context: any, options: any) => {
          const reason = activeCheck(model, context);
          log({ event: "stream_guard", case: caseName, attempt,
            names: Array.isArray(context?.tools) ? context.tools.map((x: any) => x.name) : [], reason });
          if (reason) { state.protected = true; state.armed = false;
            throw new Error("q01_stream_guard_refused:" + reason); }
          log({ event: "original_stream_called", case: caseName, attempt });
          return original(model, context, options);
        };
        if (!(caseName === "recover_missing" && attempt === 1)) child.agent.streamFn = wrapper;
        if (caseName === "recover_api" && attempt === 1) child.agent.streamFn = undefined;
        const expectedContext = { tools: [{ name: "db_summary_save", parameters: toolWireSchema(saveTool) }] };
        const badContext = { tools: [{ name: "bash", parameters: toolWireSchema(saveTool) }] };
        const selfcheck = {
          config: child.settings === settings && !child.isDisposed && !AgentRegistry.global().get(agentId) &&
            Boolean(registry.get(agentId)) && active.join(",") === "db_summary_save",
          api: typeof original === "function" && child.agent.streamFn === wrapper,
          valid: activeCheck(ctx.model, expectedContext),
          wrongRoster: activeCheck(ctx.model, badContext),
          stale: undefined as string | undefined,
        };
        const before = state.receipt;
        state.receipt = { ...receipt, sessionId: "invalid-local-selfcheck" };
        selfcheck.stale = activeCheck(ctx.model, expectedContext);
        state.receipt = before;
        const admittedNow = selfcheck.config && selfcheck.api && selfcheck.valid === undefined &&
          selfcheck.wrongRoster === "roster_mismatch" && selfcheck.stale === "stale_binding";
        log({ event: "startup_selfcheck", case: caseName, attempt, active, sessionId,
          privateHas: Boolean(registry.get(agentId)), globalHas: Boolean(AgentRegistry.global().get(agentId)),
          settingsIdentity: child.settings === settings, checks: selfcheck, admitted: admittedNow });
        if (!admittedNow) {
          log({ event: "startup_rejected", case: caseName, attempt, promptCount,
            reason: !selfcheck.config ? "config_readback" : !selfcheck.api ? "guard_api_or_missing" :
              selfcheck.valid ? "stale_or_invalid_valid_case" : "ineffective_guard" });
          continue;
        }
        admitted = true;
        log({ event: "startup_admitted", case: caseName, attempt, receipt });
        if (caseName === "runtime_roster") await child.setActiveToolsByName([]);
        log({ event: "before_prompt", case: caseName, attempt, active: child.getEnabledToolNames(),
          model: modelKey(child.model), policy: policy.revision, receipt: state.receipt });
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
          if (event.type === "tool_execution_end") log({ event: "tool_end", case: caseName,
            toolName: event.toolName, isError: event.isError,
            result: (event.result?.content || []).filter((x: any) => x.type === "text")
              .map((x: any) => x.text).join("").slice(0, 180) });
        });
        writeFileSync(permitPath, receipt.requestId);
        permit = true;
        promptCount++;
        log({ event: "prompt_start", case: caseName, attempt });
        try {
          await child.prompt("q01-case:" + caseName + " literal summary request", { runCommands: false, throwOnDrop: true });
        } catch (e) { log({ event: "prompt_error", case: caseName, error: String(e) }); }
        if (abortPromise) await abortPromise;
        saved = state.saved;
        protectedResult = state.protected;
        log({ event: "prompt_done", case: caseName, attempt, saved, protected: protectedResult,
          spent: state.spent, forbiddenRefused });
        if (positives.has(caseName) && saved && !protectedResult) log({ event: "result_accepted", case: caseName });
        else { protectedResult = true; log({ event: "result_rejected", case: caseName }); }
        break;
      } catch (e) {
        if (promptCount === 0) {
          admitted = false;
          log({ event: "startup_exception", case: caseName, attempt, promptCount,
            error: String(e), stack: String((e as Error)?.stack || "").slice(0, 1000) });
          continue;
        }
        ok = false;
        log({ event: "case_error", case: caseName, attempt, promptCount, error: String(e),
          stack: String((e as Error)?.stack || "").slice(0, 1000) });
        break;
      } finally {
        state.armed = false;
        state.receipt = undefined;
        state.child = undefined;
        if (permit) { try { unlinkSync(permitPath); } catch {} }
        unsubscribe?.();
        if (child) { await child.dispose(); log({ event: "disposed", case: caseName, attempt }); }
      }
    }
    if (!admitted) {
      protectedResult = true;
      log({ event: "summary_deferred", case: caseName, attempts: attemptCount, promptCount,
        parentAfter: ctx.sessionManager?.getEntries?.().length ?? null });
    }
    const caseOk = positives.has(caseName) ? saved && admitted && !protectedResult :
      !saved && protectedResult && (caseName !== "bash" || forbiddenRefused);
    log({ event: "case_done", case: caseName, ok: caseOk, attempts: attemptCount, promptCount,
      admitted, saved, protected: protectedResult, forbiddenRefused });
    if (!caseOk || !ok) { ok = false; break; }
  }
  log({ event: "done", ok, parentBefore, parentAfter: ctx.sessionManager?.getEntries?.().length ?? null });
}

export default function startupProbe(pi: any) {
  pi.on("session_start", (_: unknown, ctx: any) => { void run(ctx); });
}
