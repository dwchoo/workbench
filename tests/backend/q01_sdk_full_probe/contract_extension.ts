import { execFile } from "node:child_process";
import { createHash, randomUUID } from "node:crypto";
import { appendFileSync, readFileSync, unlinkSync, writeFileSync } from "node:fs";
import path from "node:path";
import { toolWireSchema } from "@oh-my-pi/pi-ai";
import { AgentRegistry, createAgentSession, SessionManager, Settings } from "@oh-my-pi/pi-coding-agent";
import { getBundledAgent } from "@oh-my-pi/pi-coding-agent/task/agents";
import { admitSummaryRole } from "./role_admission.ts";

const trace = process.env.Q01_CONTRACT_TRACE!;
const privateDir = process.env.Q01_CONTRACT_PRIVATE!;
const python = process.env.Q01_FIXED_PYTHON!;
const writer = process.env.Q01_FIXED_WRITER!;
const skill = readFileSync(process.env.Q01_SUMMARY_SKILL!, "utf8");
const requests = JSON.parse(readFileSync(path.join(privateDir, "requests.json"), "utf8"));
const roleInputs = JSON.parse(readFileSync(path.join(privateDir, "role-inputs.json"), "utf8"));
const bundled = getBundledAgent("task");
if (!bundled || bundled.source !== "bundled" || bundled.filePath !== "embedded:task.md") {
  throw new Error("bundled_task_definition_unavailable");
}
const body = bundled.systemPrompt;
writeFileSync(path.join(privateDir, "bundle-body.txt"), body);
const sha = (x: string) => createHash("sha256").update(x).digest("hex");
const stable = (x: any): string => JSON.stringify(x, (_key, value) =>
  value && typeof value === "object" && !Array.isArray(value)
    ? Object.fromEntries(Object.entries(value).sort(([a], [b]) => a.localeCompare(b))) : value);
const log = (x: Record<string, unknown>) => appendFileSync(trace, JSON.stringify({ pid: process.pid, ...x }) + "\n");
const error = (reason: string) => ({ content: [{ type: "text", text: JSON.stringify({ saved: false, reason }) }], isError: true });
const schema = { type: "object", properties: { summary: { type: "string", minLength: 1, maxLength: 2000 } },
  required: ["summary"], additionalProperties: true };
const modelKey = (m: any) => [m?.provider, m?.id, m?.api, m?.baseUrl].join("|");

async function run(pi: any, ctx: any) {
  const parentBefore = ctx.sessionManager.getEntries().length;
  log({ event: "begin", parentBefore, definition: { name: bundled!.name, source: bundled!.source,
    filePath: bundled!.filePath, bodySha: sha(body), skillSha: sha(skill), bodyUtf8Bytes: Buffer.byteLength(body, "utf8") } });
  for (const caseName of ["positive", "saved_then_bash", "worker_role"]) {
    const roleInput = roleInputs[caseName];
    const roleDecision = admitSummaryRole(roleInput?.role, roleInput?.agentName);
    log({ event: "role_admission", case: caseName, input: roleInput ?? null,
      admitted: roleDecision.admitted, reason: roleDecision.reason });
    if (!roleDecision.admitted) {
      log({ event: "role_rejected", case: caseName, input: roleInput ?? null,
        reason: roleDecision.reason, childCreated: false, promptCount: 0 });
      log({ event: "case_done", case: caseName, accepted: false, protected: true });
      continue;
    }
    const request = requests[caseName];
    if (!request || request.schemaVersion !== 1) throw new Error("host_request_unavailable");
    const state = { armed: false, spent: false, saved: false, protected: false,
      sessionId: "", receipt: undefined as any, typed: undefined as any };
    const consumedNonces = new Set<string>();
    const policy = { revision: "v1" };
    const saveTool = { name: "db_summary_save", label: "Save summary", description: "Fixed private SQLite writer.",
      approval: "write" as const, loadMode: "essential" as const, intent: "omit" as const,
      parameters: schema,
      async execute(_callId: string, params: Record<string, unknown>) {
        log({ event: "execute_received", case: caseName, params, armed: state.armed, spent: state.spent });
        const binding = state.receipt;
        if (!state.armed || state.spent || state.protected || !state.sessionId || !binding ||
            binding.requestId !== request.requestId || binding.inputKey !== request.inputKey ||
            binding.sessionId !== state.sessionId || binding.route !== "private-manager-task" ||
            binding.policySha !== sha(stable(policy)) || consumedNonces.has(binding.nonce)) return error("owner_binding");
        state.spent = true;
        consumedNonces.add(binding.nonce);
        if (!params || typeof params !== "object" || Array.isArray(params) ||
            Object.keys(params).length !== 1 || typeof params.summary !== "string" ||
            params.summary.length < 1 || params.summary.length > 2000) return error("invalid_data");
        const result = { schemaVersion: 1, requestId: request.requestId, inputKey: request.inputKey,
          skillDigest: request.skillDigest, sources: request.sources, facts: [params.summary],
          interpretations: [], unknowns: [],
          usage: { schemaVersion: 1, inputTokens: null, outputTokens: null, costUsd: null } };
        const resultText = JSON.stringify(result);
        if (Buffer.byteLength(resultText, "utf8") > 8192) return error("result_bytes");
        log({ event: "writer_invoked", case: caseName, executable: python,
          argv: [writer, JSON.stringify({ summary: params.summary })] });
        const output = await new Promise<{ err: string | null; stdout: string }>(resolve =>
          execFile(python, [writer, JSON.stringify({ summary: params.summary })],
            { timeout: 5000, maxBuffer: 4096, env: { PATH: process.env.PATH || "" } },
            (e, stdout) => resolve({ err: e ? String(e) : null, stdout })));
        if (output.err || output.stdout.trim() !== '{"saved":true}') return error("writer_failed");
        state.saved = true;
        state.typed = result;
        const receipt = JSON.stringify({ request_id: request.requestId, session_id: state.sessionId, saved: true });
        log({ event: "saved", case: caseName, requestId: request.requestId,
          sessionId: state.sessionId, resultSha: sha(resultText), resultBytes: Buffer.byteLength(resultText, "utf8") });
        return { content: [{ type: "text", text: receipt }] };
      },
    };
    let child: any;
    let unsubscribe: (() => void) | undefined;
    let abortPromise: Promise<void> | undefined;
    let forbiddenRefused = false;
    let permit = false;
    const permitPath = path.join(privateDir, "permit-" + caseName);
    const expectedSchema = sha(stable(toolWireSchema(saveTool)));
    async function verifiedChild(label: string, faults: string[] = []): Promise<any | undefined> {
      for (let attempt = 1; attempt <= 2; attempt++) {
        let candidate: any;
        let admitted = false;
        const registry = new AgentRegistry();
        const agentId = "q01-contract-" + label + "-" + attempt;
        const settings = Settings.isolated({ externalThinking: false, "prewalk.enabled": false,
          "tools.intentTracing": false, "retry.usageAwareFallback": false });
        const fault = faults[attempt - 1];
        state.armed = false; state.protected = false; state.sessionId = ""; state.receipt = undefined;
        try {
          candidate = (await createAgentSession({ cwd: process.cwd(), model: ctx.model, settings,
            sessionManager: SessionManager.inMemory(process.cwd()), agentRegistry: registry, agentId,
            agentName: "task", bindProcessState: false, systemPrompt: [body, skill], cacheWarming: false,
            disableExtensionDiscovery: true, preloadedPreparedExtensions: [], enableMCP: false,
            enableLsp: false, enableIrc: false, skipPythonPreflight: true, skills: [], rules: [],
            contextFiles: [], promptTemplates: [], slashCommands: [], toolNames: ["db_summary_save"],
            restrictToolNames: true, allowRestrictedCustomTools: true, requireYieldTool: false,
            customTools: [saveTool], hasUI: false, autoApprove: true, deadline: Date.now() + 25000 })).session;
          state.sessionId = String(candidate.sessionManager.getSessionId());
          log({ event: "session_created", case: label, attempt, sessionId: state.sessionId });
          const selected = candidate.model;
          const selectedModel = selected && { provider: selected.provider, id: selected.id, api: selected.api,
            contextWindow: selected.contextWindow, maxTokens: selected.maxTokens, baseUrl: selected.baseUrl };
          log({ event: "child_model_readback", case: label, attempt, sessionId: state.sessionId,
            model: selectedModel, thinkingLevel: candidate.thinkingLevel,
            serviceTierByFamily: candidate.serviceTierByFamily });
          const active = candidate.getEnabledToolNames();
          const receipt = Object.freeze({ requestId: request.requestId, inputKey: request.inputKey,
            sessionId: state.sessionId, epoch: "contract-" + label + "-" + attempt,
            route: "private-manager-task", policySha: sha(stable(policy)), nonce: randomUUID(),
            rosterSha: sha("db_summary_save"), schemaSha: expectedSchema, modelKey: modelKey(ctx.model) });
          state.receipt = fault === "stale" ? { ...receipt, sessionId: "stale-session" } : receipt;
          state.armed = true;
          const original = candidate.agent.streamFn;
          const check = (model: any, context: any): string | undefined => {
            const binding = state.receipt;
            const names = Array.isArray(context?.tools) ? context.tools.map((x: any) => x.name) : [];
            const actualSchema = context?.tools?.length === 1 ? sha(stable(context.tools[0].parameters)) : null;
            return !state.armed || state.protected || !binding || candidate.isDisposed ? "owner_inactive" :
              String(candidate.sessionManager.getSessionId()) !== binding.sessionId ||
              binding.requestId !== receipt.requestId || binding.inputKey !== receipt.inputKey ||
              binding.epoch !== receipt.epoch || binding.route !== receipt.route ||
              binding.nonce !== receipt.nonce ? "stale_binding" :
              names.length !== 1 || names[0] !== "db_summary_save" || sha(names.join("\n")) !== binding.rosterSha
                ? "roster_mismatch" :
              actualSchema !== expectedSchema || binding.schemaSha !== expectedSchema ? "schema_mismatch" :
              sha(stable(policy)) !== binding.policySha ? "policy_mismatch" :
              modelKey(model) !== binding.modelKey ? "model_mismatch" : undefined;
          };
          const activeCheck = fault === "noop" ? (_model: any, _context: any): string | undefined => undefined : check;
          const wrapper = (model: any, context: any, options: any) => {
            const reason = activeCheck(model, context);
            const names = Array.isArray(context?.tools) ? context.tools.map((x: any) => x.name) : [];
            log({ event: "stream_guard", case: label, attempt, names, reason });
            if (reason) { state.protected = true; state.armed = false;
              throw new Error("q01_contract_guard:" + reason); }
            log({ event: "original_stream_called", case: label, attempt });
            return original(model, context, { ...options, maxTokens: Math.min(8192, model.maxTokens) });
          };
          if (fault !== "missing") candidate.agent.streamFn = wrapper;
          const expectedContext = { tools: [{ name: "db_summary_save", parameters: toolWireSchema(saveTool) }] };
          const wrongContext = { tools: [{ name: "bash", parameters: toolWireSchema(saveTool) }] };
          const selfcheck = {
            config: candidate.settings === settings && !candidate.isDisposed &&
              Boolean(registry.get(agentId)) && !AgentRegistry.global().get(agentId) &&
              active.length === 1 && active[0] === "db_summary_save" &&
              selectedModel?.id === ctx.model?.id && selectedModel?.api === ctx.model?.api &&
              selectedModel?.contextWindow === ctx.model?.contextWindow &&
              selectedModel?.maxTokens === ctx.model?.maxTokens &&
              typeof selectedModel?.contextWindow === "number" && selectedModel.contextWindow > 0 &&
              typeof selectedModel?.maxTokens === "number" && selectedModel.maxTokens > 0,
            api: typeof original === "function" && candidate.agent.streamFn === wrapper,
            valid: activeCheck(ctx.model, expectedContext),
            wrongRoster: activeCheck(ctx.model, wrongContext),
            stale: undefined as string | undefined,
          };
          const before = state.receipt;
          state.receipt = { ...receipt, sessionId: "invalid-local-selfcheck" };
          selfcheck.stale = activeCheck(ctx.model, expectedContext);
          state.receipt = before;
          admitted = selfcheck.config && selfcheck.api && selfcheck.valid === undefined &&
            selfcheck.wrongRoster === "roster_mismatch" && selfcheck.stale === "stale_binding";
          log({ event: "startup_selfcheck", case: label, attempt, fault: fault || null,
            active, sessionId: state.sessionId, checks: selfcheck, admitted });
          if (!admitted) {
            log({ event: "startup_rejected", case: label, attempt, fault: fault || null, promptCount: 0,
              reason: !selfcheck.config ? "config_readback" : !selfcheck.api ? "guard_api_or_missing" :
                selfcheck.valid ? "stale_or_invalid_valid_case" : "ineffective_guard" });
            continue;
          }
          log({ event: "startup_admitted", case: label, attempt, receipt });
          return candidate;
        } catch (e) {
          log({ event: "startup_exception", case: label, attempt, promptCount: 0, error: String(e) });
        } finally {
          if (!admitted) {
            state.armed = false; state.receipt = undefined;
            if (candidate) { await candidate.dispose(); log({ event: "disposed", case: label, attempt }); }
          }
        }
      }
      log({ event: "summary_deferred", case: label, attempts: 2, promptCount: 0 });
      return undefined;
    }
    if (caseName === "positive") {
      for (const [control, faults] of [
        ["startup_missing", ["missing"]], ["startup_noop", ["noop"]],
        ["startup_stale", ["stale"]], ["startup_defer", ["noop", "noop"]],
      ] as [string, string[]][]) {
        const probe = await verifiedChild(control, faults);
        log({ event: "startup_control_done", case: control, recovered: Boolean(probe), promptCount: 0 });
        state.armed = false; state.receipt = undefined;
        if (probe) { await probe.dispose(); log({ event: "disposed", case: control, attempt: 2 }); }
      }
    }
    try {
      child = await verifiedChild(caseName);
      if (!child) {
        state.protected = true;
        log({ event: "case_done", case: caseName, accepted: false, saved: false,
          protected: true, summaryDeferred: true });
        continue;
      }
      unsubscribe = child.subscribe((event: any) => {
        if (event.type === "tool_execution_end") {
          log({ event: "tool_end", case: caseName, toolName: event.toolName, isError: event.isError,
            result: (event.result?.content || []).filter((x: any) => x.type === "text")
              .map((x: any) => x.text).join("").slice(0, 200) });
        }
        if (event.type === "tool_execution_end" && event.toolName === "bash" && event.isError === true) {
          forbiddenRefused = true; state.protected = true; state.armed = false;
          log({ event: "abort_requested", case: caseName });
          abortPromise = child.abort({ goalReason: "internal", reason: "q01_forbidden_tool" })
            .then(() => log({ event: "abort_completed", case: caseName }))
            .catch((e: unknown) => log({ event: "abort_failed", case: caseName, error: String(e) }));
        }
      });
      state.armed = true;
      writeFileSync(permitPath, request.requestId);
      permit = true;
      log({ event: "prompt_start", case: caseName, sessionId: state.sessionId, requestId: request.requestId });
      try { await child.prompt("q01-contract:" + caseName, { runCommands: false, throwOnDrop: true }); }
      catch (e) { log({ event: "prompt_error", case: caseName, error: String(e) }); }
      if (abortPromise) await abortPromise;
      const accepted = caseName === "positive" && state.saved && !state.protected && Boolean(state.typed);
      if (accepted) {
        const text = JSON.stringify(state.typed);
        const parentReceipt = JSON.stringify({ kind: "summary_result", requestId: request.requestId,
          resultSha: sha(text), status: "accepted" });
        if (Buffer.byteLength(parentReceipt, "utf8") > 4096) throw new Error("parent_receipt_bytes");
        const before = ctx.sessionManager.getEntries().length;
        pi.appendEntry("q01-summary-receipt", { receipt: parentReceipt });
        const after = ctx.sessionManager.getEntries().length;
        if (after !== before + 1) throw new Error("parent_receipt_not_appended");
        const appended = JSON.stringify(ctx.sessionManager.getEntries().slice(before));
        log({ event: "parent_receipt", case: caseName, requestId: request.requestId,
          bytes: Buffer.byteLength(parentReceipt, "utf8"), sha: sha(parentReceipt),
          parentEntriesBefore: before, parentEntriesAfter: after, parentReceipt,
          entryContainsRawSummary: appended.includes(state.typed.facts[0]) });
        writeFileSync(path.join(privateDir, "typed-positive.json"), text);
        log({ event: "result_accepted", case: caseName, resultSha: sha(text),
          resultBytes: Buffer.byteLength(text, "utf8") });
      } else {
        state.protected = true;
        log({ event: "result_rejected", case: caseName, saved: state.saved,
          forbiddenRefused, typedProvisional: Boolean(state.typed) });
      }
      log({ event: "case_done", case: caseName, accepted, saved: state.saved,
        protected: state.protected, forbiddenRefused });
    } finally {
      state.armed = false;
      if (permit) { try { unlinkSync(permitPath); } catch {} }
      unsubscribe?.();
      if (child) { await child.dispose(); log({ event: "disposed", case: caseName }); }
    }
  }
  log({ event: "done", parentBefore, parentAfter: ctx.sessionManager.getEntries().length });
}

export default function contractProbe(pi: any) {
  pi.on("session_start", (_: unknown, ctx: any) => { void run(pi, ctx).catch(e => log({ event: "fatal", error: String(e) })); });
}
