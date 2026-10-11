import { execFile } from "node:child_process";
import { createHash, randomUUID } from "node:crypto";
import { appendFileSync, readFileSync, unlinkSync, writeFileSync } from "node:fs";
import path from "node:path";
import { AgentRegistry, createAgentSession, SessionManager, Settings } from "@oh-my-pi/pi-coding-agent";

const trace = process.env.Q01_PRIVATE_TRACE!;
const permits = process.env.Q01_PRIVATE_PERMITS!;
const writer = process.env.Q01_FIXED_WRITER!;
const failingWriter = process.env.Q01_FAILING_WRITER!;
const python = process.env.Q01_FIXED_PYTHON!;
const body = readFileSync(process.env.Q01_AGENT_BODY!, "utf8");
const skill = readFileSync(process.env.Q01_SUMMARY_SKILL!, "utf8");
const digest = (s: string) => createHash("sha256").update(s).digest("hex");
const cases = ["p1", "p2", "switch", "missing", "expand", "stale", "invalid", "writer_fail",
  "bash", "think", "retry", "noop"] as const;
const promptCases = new Set(["p1", "p2", "switch", "invalid", "writer_fail", "bash", "think", "retry", "noop"]);
const usedRequests = new Set<string>();
const usedNonces = new Set<string>();
const log = (x: Record<string, unknown>) => appendFileSync(trace, JSON.stringify({ pid: process.pid, ...x }) + "\n");
const errorResult = (reason: string) => ({ content: [{ type: "text", text: JSON.stringify({ saved: false, reason }) }], isError: true });

type Binding = { id: string; sessionId: string; epoch: string; rosterDigest: string; nonce: string };
function tool(caseName: string, identityRef: { current?: Binding }, sessionRef: { current?: any },
    state: { spent: boolean; savedReceipt?: string; armed: boolean }) {
  return {
    name: "db_summary_save", label: "Save summary", description: "Save one literal summary with the fixed SQLite writer.",
    approval: "write" as const, loadMode: "essential" as const,
    parameters: { type: "object", properties: { summary: { type: "string", minLength: 1, maxLength: 2000 } },
      required: ["summary"], additionalProperties: true },
    async execute(callId: string, params: Record<string, unknown>) {
      log({ event: "execute_received", case: caseName, callId, params, armed: state.armed, spent: state.spent });
      const identity = identityRef.current;
      if (!identity || !state.armed || state.spent) return errorResult("identity_or_single_use");
      state.spent = true;
      const actualId = String(sessionRef.current?.sessionManager?.getSessionId?.() ?? "");
      const actualRoster = digest((sessionRef.current?.getEnabledToolNames?.() || []).join("\n"));
      if (identity.sessionId !== actualId || identity.rosterDigest !== actualRoster ||
          identity.rosterDigest !== digest("db_summary_save") || usedNonces.has(identity.nonce)) {
        log({ event: "execute_rejected", case: caseName, reason: "binding_mismatch",
          actualId, actualRoster });
        return errorResult("binding_mismatch");
      }
      usedNonces.add(identity.nonce);
      if (!params || typeof params !== "object" || Array.isArray(params) ||
          Object.keys(params).length !== 1 || typeof params.summary !== "string" ||
          params.summary.length < 1 || params.summary.length > 2000) {
        log({ event: "execute_rejected", case: caseName, reason: "invalid_data" });
        return errorResult("invalid_data");
      }
      const receipt = JSON.stringify({ request_id: identity.id, session_id: identity.sessionId,
        epoch: identity.epoch, saved: true });
      if (Buffer.byteLength(receipt, "utf8") > 8192) return errorResult("result_bytes");
      const argv = [caseName === "writer_fail" ? failingWriter : writer,
        JSON.stringify({ summary: params.summary })];
      log({ event: "writer_invoked", case: caseName, executable: python, argv });
      const out = await new Promise<{ error: string | null; stdout: string }>((resolve) =>
        execFile(python, argv, { timeout: 5000, maxBuffer: 4096, env: { PATH: process.env.PATH || "" } },
          (e, stdout) => resolve({ error: e ? String(e) : null, stdout })));
      if (out.error || out.stdout.trim() !== '{"saved":true}') {
        log({ event: "writer_failed", case: caseName, error: out.error, stdout: out.stdout.slice(0, 200) });
        return errorResult("writer_failed");
      }
      state.savedReceipt = receipt;
      log({ event: "saved", case: caseName, receiptBytes: Buffer.byteLength(receipt, "utf8") });
      return { content: [{ type: "text", text: receipt }] };
    },
  };
}

async function run(ctx: any) {
  const parentBefore = ctx.sessionManager?.getEntries?.().length ?? null;
  log({ event: "begin", parentBefore, model: ctx.model && { provider: ctx.model.provider, id: ctx.model.id, api: ctx.model.api },
    bodySha: digest(body), skillSha: digest(skill), bodyBytes: Buffer.byteLength(body), skillBytes: Buffer.byteLength(skill) });
  if (!ctx.model) { log({ event: "done", ok: false, reason: "model_missing" }); return; }
  let previousId = "";
  let ok = true;
  for (const caseName of cases) {
    const registry = new AgentRegistry();
    const settings = Settings.isolated({ externalThinking: caseName === "expand", "prewalk.enabled": false });
    const agentId = `q01-private-${caseName}`;
    const identityRef: { current?: Binding } = {};
    const sessionRef: { current?: any } = {};
    const state: { spent: boolean; savedReceipt?: string; armed: boolean } =
      { spent: false, savedReceipt: undefined, armed: false };
    let child: any;
    let unsubscribe: (() => void) | undefined;
    let forbiddenRefused = false;
    let abortPromise: Promise<void> | undefined;
    let abortError: string | undefined;
    let permitted = false;
    const permit = path.join(permits, caseName);
    try {
      const created = await createAgentSession({
        cwd: process.cwd(), model: ctx.model, settings, sessionManager: SessionManager.inMemory(process.cwd()),
        agentRegistry: registry, agentId, agentName: "task", bindProcessState: false,
        systemPrompt: [body, skill], cacheWarming: false, disableExtensionDiscovery: true,
        preloadedPreparedExtensions: [], enableMCP: false, enableLsp: false, enableIrc: false,
        skipPythonPreflight: true, skills: [], rules: [], contextFiles: [], promptTemplates: [], slashCommands: [],
        toolNames: ["db_summary_save"], restrictToolNames: true,
        allowRestrictedCustomTools: caseName !== "missing", requireYieldTool: false,
        customTools: [tool(caseName, identityRef, sessionRef, state)], hasUI: false, autoApprove: true,
        deadline: Date.now() + 25000,
      });
      child = created.session;
      sessionRef.current = child;
      const sessionId = String(child.sessionManager.getSessionId());
      const current = Object.freeze({ id: `q01-${caseName}`, sessionId, epoch: `epoch-${caseName}`,
        rosterDigest: digest("db_summary_save"), nonce: randomUUID() });
      // The private object is the only owner of this child. No handle is returned to a model or parent tool.
      if (caseName === "switch" || caseName === "expand") {
        const alternate = child.modelRegistry.find("q01privresp", "scripted");
        if (!alternate) throw new Error("alternate_model_missing");
        await child.setModelTemporary(alternate);
        log({ event: "model_switch", case: caseName, to: alternate.api,
          active: child.getEnabledToolNames() });
        if (caseName === "switch") await child.setModelTemporary(ctx.model);
      }
      const active = child.getEnabledToolNames();
      const expectedDigest = digest("db_summary_save");
      const actualDigest = digest(active.join("\n"));
      const globalHas = Boolean(AgentRegistry.global().get(agentId));
      const privateHas = Boolean(registry.get(agentId));
      log({ event: "readback", case: caseName, sessionId, active, rosterDigest: actualDigest,
        expectedDigest, globalHas, privateHas, modelApi: child.model?.api,
        parentEntries: ctx.sessionManager?.getEntries?.().length ?? null });
      const stale = caseName === "stale" && previousId !== sessionId;
      const reason = active.length !== 1 || active[0] !== "db_summary_save" ? "roster_mismatch"
        : stale ? "stale_receipt" : usedRequests.has(current.id) ? "reused_request" : undefined;
      if (reason) { log({ event: "admission_rejected", case: caseName, reason }); continue; }
      if (!promptCases.has(caseName)) throw new Error("negative_case_admitted");
      if (globalHas) throw new Error("private_agent_visible_in_global_registry");
      if (actualDigest !== expectedDigest) throw new Error("roster_digest_mismatch");
      // This check and prompt are synchronous owner steps; no child handle escapes between them.
      identityRef.current = current;
      usedRequests.add(current.id);
      state.armed = true;
      unsubscribe = child.subscribe((event: any) => {
        if (event.type === "tool_execution_end" && ["bash", "think"].includes(String(event.toolName))
            && event.isError === true) {
          forbiddenRefused = true;
          state.armed = false;
          log({ event: "writer_disarmed", case: caseName, reason: "unadvertised_tool_attempt" });
          log({ event: "abort_requested", case: caseName });
          abortPromise = child.abort({ goalReason: "internal", reason: "q01_forbidden_tool" })
            .then(() => log({ event: "abort_completed", case: caseName }))
            .catch((e: unknown) => { abortError = String(e); log({ event: "abort_failed", case: caseName, error: abortError }); });
        }
        if (["tool_execution_start", "tool_execution_end", "agent_end"].includes(event.type))
          log({ event: "child_event", case: caseName, type: event.type, toolName: event.toolName,
            isError: event.isError, result: event.type === "tool_execution_end"
              ? (event.result?.content || []).filter((x: any) => x.type === "text").map((x: any) => x.text).join("").slice(0, 300) : undefined });
      });
      if (caseName === "p1") previousId = sessionId;
      writeFileSync(permit, current.id);
      permitted = true;
      log({ event: "prompt_start", case: caseName, current, active: child.getEnabledToolNames() });
      let promptError: string | undefined;
      try {
        await child.prompt(`q01-case:${caseName} literal validation log. Save one short summary.`,
          { runCommands: false, throwOnDrop: true });
      } catch (e) {
        promptError = String(e);
        if (!["bash", "think"].includes(caseName) || !abortPromise) throw e;
        log({ event: "prompt_aborted", case: caseName, error: promptError });
      }
      if (abortPromise) await abortPromise;
      log({ event: "prompt_done", case: caseName, spent: state.spent,
        savedReceipt: state.savedReceipt, promptError,
        active: child.getEnabledToolNames(), modelApi: child.model?.api,
        entries: child.sessionManager.getEntries().length });
      if (["bash", "think"].includes(caseName)) {
        if (state.savedReceipt || !forbiddenRefused || !abortPromise || abortError)
          throw new Error("forbidden_call_not_safely_aborted");
        log({ event: "result_rejected", case: caseName, reason: "unadvertised_tool_attempt" });
      } else if (caseName === "noop") {
        if (state.savedReceipt) throw new Error("noop_saved");
        log({ event: "result_rejected", case: caseName, reason: "missing_db_summary_save" });
      } else if (["invalid", "writer_fail"].includes(caseName)) {
        if (!state.spent || state.savedReceipt) throw new Error("failed_save_accepted");
        log({ event: "result_rejected", case: caseName,
          reason: caseName === "invalid" ? "invalid_data" : "writer_failed" });
      } else {
        if (!state.spent || !state.savedReceipt) throw new Error("verified_save_missing");
        log({ event: "result_accepted", case: caseName, verifiedReceipt: state.savedReceipt });
      }
    } catch (e) {
      ok = false;
      log({ event: "case_error", case: caseName, error: String(e), stack: String((e as Error)?.stack || "").slice(0, 1200) });
      break;
    } finally {
      state.armed = false;
      identityRef.current = undefined;
      sessionRef.current = undefined;
      if (permitted) { try { unlinkSync(permit); } catch {} }
      unsubscribe?.();
      if (child) { await child.dispose(); log({ event: "disposed", case: caseName }); }
    }
  }
  log({ event: "done", ok, parentBefore, parentAfter: ctx.sessionManager?.getEntries?.().length ?? null });
}

export default function privateProbe(pi: any) {
  pi.on("session_start", (_: unknown, ctx: any) => { void run(ctx); });
}
