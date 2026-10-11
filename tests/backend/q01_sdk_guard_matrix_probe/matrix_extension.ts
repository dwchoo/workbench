import { execFile } from "node:child_process";
import { createHash, randomUUID } from "node:crypto";
import { appendFileSync, readFileSync, unlinkSync, writeFileSync } from "node:fs";
import path from "node:path";
import { AgentRegistry, createAgentSession, SessionManager, Settings } from "@oh-my-pi/pi-coding-agent";

const trace = process.env.Q01_MATRIX_TRACE!;
const permits = process.env.Q01_MATRIX_PERMITS!;
const writer = process.env.Q01_FIXED_WRITER!;
const python = process.env.Q01_FIXED_PYTHON!;
const body = readFileSync(process.env.Q01_AGENT_BODY!, "utf8");
const skill = readFileSync(process.env.Q01_SUMMARY_SKILL!, "utf8");
const sha = (s: string) => createHash("sha256").update(s).digest("hex");
const log = (v: Record<string, unknown>) => appendFileSync(trace, JSON.stringify({ pid: process.pid, ...v }) + "\n");
const fail = (reason: string) => ({ content: [{ type: "text", text: JSON.stringify({ saved: false, reason }) }], isError: true });
const schema = { type: "object", properties: { summary: { type: "string", minLength: 1, maxLength: 2000 } },
  required: ["summary"], additionalProperties: true };
const cases = ["normal", "missing_guard", "noop_guard", "api_shape_absent", "stale",
  "wrong_session", "disposed", "roster_change", "schema_change", "policy_change", "bash", "checksend"] as const;
type Receipt = { requestId: string; sessionId: string; epoch: string; rosterSha: string;
  schemaSha: string; policySha: string; nonce: string };
const consumed = new Set<string>();

function makeTool(caseName: string, state: { armed: boolean; spent: boolean; saved: boolean;
    receipt?: Receipt; child?: any; schemaState: { revision: string }; policyState: { revision: string } }) {
  return { name: "db_summary_save", label: "Save summary", description: "Fixed private SQLite summary writer.",
    approval: "write" as const, loadMode: "essential" as const, parameters: schema,
    async execute(callId: string, params: Record<string, unknown>) {
      log({ event: "execute_received", case: caseName, callId, params, armed: state.armed, spent: state.spent });
      const receipt = state.receipt;
      if (!receipt || !state.armed || state.spent || !state.child) return fail("owner_binding");
      state.spent = true;
      const currentRoster = state.child.getEnabledToolNames();
      if (state.child.isDisposed || String(state.child.sessionManager.getSessionId()) !== receipt.sessionId ||
          sha(currentRoster.join("\n")) !== receipt.rosterSha || currentRoster.join(",") !== "db_summary_save" ||
          sha(JSON.stringify(state.schemaState)) !== receipt.schemaSha ||
          sha(JSON.stringify(state.policyState)) !== receipt.policySha ||
          consumed.has(receipt.nonce)) return fail("changed_binding");
      consumed.add(receipt.nonce);
      if (!params || typeof params !== "object" || Array.isArray(params) ||
          Object.keys(params).length !== 1 || typeof params.summary !== "string" ||
          params.summary.length < 1 || params.summary.length > 2000) return fail("invalid_data");
      const text = JSON.stringify({ request_id: receipt.requestId, session_id: receipt.sessionId, saved: true });
      if (Buffer.byteLength(text, "utf8") > 8192) return fail("result_bytes");
      const argv = [writer, JSON.stringify({ summary: params.summary })];
      log({ event: "writer_invoked", case: caseName, executable: python, argv });
      const out = await new Promise<{ error: string | null; stdout: string }>(resolve =>
        execFile(python, argv, { timeout: 5000, maxBuffer: 4096, env: { PATH: process.env.PATH || "" } },
          (e, stdout) => resolve({ error: e ? String(e) : null, stdout })));
      if (out.error || out.stdout.trim() !== '{"saved":true}') return fail("writer_failed");
      state.saved = true;
      log({ event: "saved", case: caseName, receiptBytes: Buffer.byteLength(text, "utf8") });
      return { content: [{ type: "text", text }] };
    } };
}

async function run(ctx: any) {
  const parentBefore = ctx.sessionManager?.getEntries?.().length ?? null;
  log({ event: "begin", parentBefore, bodySha: sha(body), skillSha: sha(skill),
    model: ctx.model && { provider: ctx.model.provider, id: ctx.model.id, api: ctx.model.api } });
  if (!ctx.model) { log({ event: "done", ok: false, reason: "model_missing" }); return; }
  let previousReceipt: Receipt | undefined;
  let ok = true;
  for (const caseName of cases) {
    const registry = new AgentRegistry();
    const settings = Settings.isolated({ externalThinking: false, "prewalk.enabled": false });
    const state = { armed: false, spent: false, saved: false, receipt: undefined as Receipt | undefined,
      child: undefined as any, schemaState: { revision: "v1" }, policyState: { revision: "v1" } };
    const agentId = `q01-matrix-${caseName}`;
    let child: any;
    let unsubscribe: (() => void) | undefined;
    let permitted = false;
    let disposedEarly = false;
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
        requireYieldTool: false, customTools: [makeTool(caseName, state)], hasUI: false, autoApprove: true,
        deadline: Date.now() + 20000,
      })).session;
      state.child = child;
      const sessionId = String(child.sessionManager.getSessionId());
      const initial = child.getEnabledToolNames();
      const receipt: Receipt = Object.freeze({ requestId: `q01-${caseName}`, sessionId, epoch: `epoch-${caseName}`,
        rosterSha: sha(initial.join("\n")), schemaSha: sha(JSON.stringify(state.schemaState)),
        policySha: sha(JSON.stringify(state.policyState)), nonce: randomUUID() });
      log({ event: "initial_check", case: caseName, sessionId, active: initial, receipt,
        globalHas: Boolean(AgentRegistry.global().get(agentId)), privateHas: Boolean(registry.get(agentId)) });
      if (initial.join(",") !== "db_summary_save") throw new Error("initial_not_sole_db");
      if (caseName === "normal") previousReceipt = receipt;
      if (caseName === "roster_change") await child.setActiveToolsByName([]);
      if (caseName === "schema_change") state.schemaState.revision = "v2";
      if (caseName === "policy_change") state.policyState.revision = "v2";
      if (caseName === "disposed") { await child.dispose(); disposedEarly = true; log({ event: "disposed_before_gate", case: caseName }); }

      // These three are explicitly controlled owner-wrapper shape faults, not native SDK guard APIs.
      const guard: { readback?: unknown } | undefined =
        caseName === "missing_guard" ? undefined :
        caseName === "noop_guard" ? { readback: () => undefined } :
        caseName === "api_shape_absent" ? { readback: "absent" } :
        { readback: () => child.getEnabledToolNames() };
      const presented = caseName === "stale" ? previousReceipt :
        caseName === "wrong_session" ? { ...receipt, sessionId: "wrong-session" } : receipt;
      const gate = (): string | undefined => {
        if (!guard) return "missing_guard";
        if (typeof guard.readback !== "function" || typeof child.prompt !== "function") return "method_shape";
        const active = (guard.readback as () => unknown)();
        if (!Array.isArray(active) || !active.every(x => typeof x === "string")) return "noop_readback";
        if (!presented || child.isDisposed) return "missing_receipt_or_disposed";
        if (presented.requestId !== receipt.requestId || presented.sessionId !== sessionId ||
            presented.epoch !== receipt.epoch || presented.nonce !== receipt.nonce) return "stale_or_wrong_session";
        if (active.join(",") !== "db_summary_save" || sha(active.join("\n")) !== presented.rosterSha) return "roster_changed";
        if (sha(JSON.stringify(state.schemaState)) !== presented.schemaSha) return "schema_changed";
        if (sha(JSON.stringify(state.policyState)) !== presented.policySha) return "policy_changed";
        if (consumed.has(presented.nonce)) return "nonce_used";
        return undefined;
      };
      const reason = gate();
      log({ event: "final_gate", case: caseName, allowed: !reason, reason,
        active: !child.isDisposed ? child.getEnabledToolNames() : null });
      if (reason) { log({ event: "admission_rejected", case: caseName, reason }); continue; }
      state.receipt = receipt;
      state.armed = true;
      unsubscribe = child.subscribe((event: any) => {
        if (event.type === "tool_execution_end" && event.toolName === "bash" && event.isError === true) {
          forbiddenRefused = true;
          state.armed = false;
          log({ event: "abort_requested", case: caseName });
          abortPromise = child.abort({ goalReason: "internal", reason: "q01_forbidden_tool" })
            .then(() => log({ event: "abort_completed", case: caseName }))
            .catch((e: unknown) => log({ event: "abort_failed", case: caseName, error: String(e) }));
        }
        if (event.type === "tool_execution_end")
          log({ event: "tool_end", case: caseName, toolName: event.toolName, isError: event.isError,
            result: (event.result?.content || []).filter((x: any) => x.type === "text").map((x: any) => x.text).join("").slice(0, 180) });
      });
      if (caseName === "checksend") {
        // Intentional fault AFTER the final gate and BEFORE prompt; this is the measured seam.
        log({ event: "fault_after_gate_before_prompt", case: caseName, kind: "public_setActiveToolsByName_empty" });
        await child.setActiveToolsByName([]);
        log({ event: "fault_applied", case: caseName, active: child.getEnabledToolNames() });
      }
      writeFileSync(permit, receipt.requestId);
      permitted = true;
      log({ event: "prompt_start", case: caseName, active: child.getEnabledToolNames() });
      try {
        await child.prompt(`q01-case:${caseName} literal summary request`, { runCommands: false, throwOnDrop: true });
      } catch (e) {
        if (caseName !== "bash" || !abortPromise) throw e;
        log({ event: "prompt_aborted", case: caseName, error: String(e) });
      }
      if (abortPromise) await abortPromise;
      log({ event: "prompt_done", case: caseName, spent: state.spent, saved: state.saved,
        active: child.getEnabledToolNames() });
      if (caseName === "normal") {
        if (!state.saved) throw new Error("positive_not_saved");
        log({ event: "result_accepted", case: caseName });
      } else {
        if (state.saved) throw new Error("negative_saved");
        if (caseName === "bash" && !forbiddenRefused) throw new Error("forbidden_not_refused");
        log({ event: "result_rejected", case: caseName,
          reason: caseName === "bash" ? "forbidden_tool" : "checksend_fault" });
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
      if (child && !disposedEarly) { await child.dispose(); log({ event: "disposed", case: caseName }); }
    }
  }
  log({ event: "done", ok, parentBefore, parentAfter: ctx.sessionManager?.getEntries?.().length ?? null });
}

export default function matrixProbe(pi: any) {
  pi.on("session_start", (_: unknown, ctx: any) => { void run(ctx); });
}
