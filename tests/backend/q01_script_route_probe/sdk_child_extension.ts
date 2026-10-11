import { execFile } from "node:child_process";
import { appendFileSync, unlinkSync, writeFileSync } from "node:fs";
import path from "node:path";
import { createAgentSession, SessionManager } from "@oh-my-pi/pi-coding-agent";

const tracePath = process.env.Q01_CHILD_TRACE;
const permitDir = process.env.Q01_PROMPT_PERMIT_DIR;
const writer = process.env.Q01_FIXED_WRITER;
const python = process.env.Q01_FIXED_PYTHON;
const SUMMARY = "literal: touch /tmp/q01-should-not-run; DROP TABLE summaries; -- data only";
const cases = ["positive", "bash", "write", "edit", "inject_command", "inject_sql", "inject_path",
  "missing_allow", "extra_tool", "stale_session", "reused_request"] as const;
const consumed = new Set<string>();

function record(event: Record<string, unknown>) {
  if (tracePath) appendFileSync(tracePath, JSON.stringify({ pid: process.pid, ...event }) + "\n", "utf8");
}

function fail(id: string, reason: string) {
  const text = JSON.stringify({ request_id: id, saved: false, reason });
  const bounded = Buffer.byteLength(text, "utf8") <= 8192
    ? text : '{"request_id":null,"saved":false,"reason":"result_bytes"}';
  return { content: [{ type: "text", text: bounded }], isError: true };
}

function makeSaveTool(caseName: string, requestId: string, identityRef: { current?: Readonly<{ id: string; sessionId: string }> }) {
  let used = false;
  return {
    name: "db_summary_save",
    label: "Save summary",
    description: "Save one validated summary using the fixed private SQLite writer.",
    approval: "write" as const,
    loadMode: "essential" as const,
    parameters: { type: "object", properties: { summary: { type: "string", minLength: 1, maxLength: 2000 } },
      required: ["summary"], additionalProperties: true },
    async execute(callId: string, params: Record<string, unknown>) {
      const identity = identityRef.current;
      record({ event: "execute_received", case: caseName, requestId, callId, params, used, identity });
      if (!identity || identity.id !== requestId || used) {
        record({ event: "execute_rejected", case: caseName, callId, reason: "missing_identity_or_used" });
        return fail(requestId, "missing_identity_or_used");
      }
      used = true;
      if (!params || typeof params !== "object" || Array.isArray(params) ||
          Object.keys(params).length !== 1 || typeof params.summary !== "string" ||
          params.summary.length < 1 || params.summary.length > 2000 || !writer || !python) {
        record({ event: "execute_rejected", case: caseName, callId, reason: "invalid_data" });
        return fail(requestId, "invalid_data");
      }
      const text = JSON.stringify({ request_id: identity.id, session_id: identity.sessionId, saved: true });
      const bytes = Buffer.byteLength(text, "utf8");
      if (bytes > 8192) {
        record({ event: "execute_rejected", case: caseName, callId, reason: "result_bytes", bytes });
        return fail(requestId, "result_bytes");
      }
      const argv = [writer, JSON.stringify({ summary: params.summary })];
      record({ event: "writer_invoked", case: caseName, callId, executable: python, argv });
      const output = await new Promise<{ error: string | null; stdout: string }>((resolve) => {
        execFile(python, argv, { timeout: 5000, maxBuffer: 4096, env: { PATH: process.env.PATH || "" } },
          (error, stdout) => resolve({ error: error ? String(error) : null, stdout }));
      });
      if (output.error || output.stdout.trim() !== '{"saved":true}') {
        record({ event: "writer_failed", case: caseName, callId, error: output.error, stdout: output.stdout.slice(0, 200) });
        return fail(requestId, "writer_failed");
      }
      record({ event: "execute_saved", case: caseName, callId, resultBytes: bytes, identity });
      return { content: [{ type: "text", text }], details: { resultBytes: bytes } };
    },
  };
}

async function run(ctx: any) {
  record({ event: "run_begin", model: ctx.model ? { provider: ctx.model.provider, id: ctx.model.id } : null,
    cases });
  if (!ctx.model || !permitDir) {
    record({ event: "run_done", ok: false, reason: "model_or_permit_missing" });
    return;
  }
  let priorSessionId: string | undefined;
  let failed = false;
  try {
    for (const caseName of cases) {
      const requestId = caseName === "reused_request" ? "q01-positive-request" : `q01-${caseName}-request`;
      const identityRef: { current?: Readonly<{ id: string; sessionId: string }> } = {};
      const customTools: any[] = [makeSaveTool(caseName, requestId, identityRef)];
      const toolNames = ["db_summary_save"];
      if (caseName === "extra_tool") {
        customTools.push({ name: "other_probe", label: "Other", description: "Negative admission fixture",
          loadMode: "essential",
          parameters: { type: "object", properties: {}, additionalProperties: false },
          async execute() { return fail(requestId, "forbidden_extra_tool"); } });
        toolNames.push("other_probe");
      }
      let session: any;
      let unsubscribe: (() => void) | undefined;
      let permitted = false;
      const permit = path.join(permitDir, caseName);
      try {
        record({ event: "child_create_start", case: caseName, requestId, toolNames,
          allowRestrictedCustomTools: caseName !== "missing_allow" });
        const created = await createAgentSession({
          cwd: process.cwd(), model: ctx.model, sessionManager: SessionManager.inMemory(process.cwd()),
          agentId: `q01-sdk-child-${caseName}`, agentName: "sub", bindProcessState: false,
          systemPrompt: "Fixture summary turn. Follow the supplied scripted provider tool calls.",
          cacheWarming: false, disableExtensionDiscovery: true, preloadedPreparedExtensions: [],
          enableMCP: false, enableLsp: false, enableIrc: false, skipPythonPreflight: true,
          skills: [], rules: [], contextFiles: [], promptTemplates: [], slashCommands: [],
          toolNames, restrictToolNames: true, allowRestrictedCustomTools: caseName !== "missing_allow",
          requireYieldTool: false, customTools, hasUI: false, autoApprove: true,
          deadline: Date.now() + 15000,
        });
        session = created.session;
        const sessionId = String(session.sessionManager.getSessionId());
        const identity = Object.freeze({ id: requestId,
          sessionId: caseName === "stale_session" ? priorSessionId || "missing-prior" : sessionId });
        identityRef.current = identity;
        const active = session.getEnabledToolNames();
        record({ event: "child_readback", case: caseName, requestId, sessionId, identity, active });
        const reason = active.length !== 1 || active[0] !== "db_summary_save" ? "tool_set_mismatch"
          : identity.sessionId !== sessionId ? "stale_session"
          : consumed.has(identity.id) ? "request_reused" : undefined;
        if (reason) {
          record({ event: "admission_rejected", case: caseName, requestId, reason, active, sessionId });
          continue;
        }
        consumed.add(identity.id);
        if (caseName === "positive") priorSessionId = sessionId;
        record({ event: "admission_accepted", case: caseName, requestId, sessionId, active });
        unsubscribe = session.subscribe((event: any) => {
          if (["agent_start", "agent_end", "tool_execution_start", "tool_execution_end"].includes(event.type)) {
            record({ event: "child_event", case: caseName, type: event.type,
              toolName: event.toolName, toolCallId: event.toolCallId, isError: event.isError,
              resultText: event.type === "tool_execution_end"
                ? (event.result?.content || []).filter((x: any) => x.type === "text").map((x: any) => x.text).join("").slice(0, 1000)
                : undefined });
          }
        });
        writeFileSync(permit, requestId, "utf8");
        permitted = true;
        record({ event: "prompt_start", case: caseName, requestId, sessionId,
          active: session.getEnabledToolNames() });
        await session.prompt(`q01-case:${caseName} request:${requestId}`, { runCommands: false, throwOnDrop: true });
        record({ event: "prompt_done", case: caseName, requestId, sessionId,
          active: session.getEnabledToolNames() });
      } catch (error) {
        failed = true;
        record({ event: "case_error", case: caseName, requestId,
          errorName: (error as Error)?.name, errorMessage: String((error as Error)?.message ?? error).slice(0, 1600),
          errorStack: String((error as Error)?.stack ?? "").slice(0, 3000) });
        break;
      } finally {
        if (permitted) {
          try { unlinkSync(permit); } catch { /* evidence records permit state below */ }
        }
        unsubscribe?.();
        if (session) {
          await session.dispose();
          record({ event: "child_disposed", case: caseName, requestId });
        }
      }
    }
    record({ event: "run_done", ok: !failed });
  } catch (error) {
    record({ event: "run_done", ok: false, errorMessage: String((error as Error)?.message ?? error).slice(0, 2000) });
  }
}

export default function sdkChildProbe(pi: any) {
  pi.on("session_start", (_event: unknown, ctx: any) => { void run(ctx); });
}
