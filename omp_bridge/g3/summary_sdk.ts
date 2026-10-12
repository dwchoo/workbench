/** Request-specific task@Manager session; no parent turn or permission changes. */
import { execFile } from "node:child_process";
import { createHash, randomUUID } from "node:crypto";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { canonicalJson, parseSummaryRequest, parseSummaryResult } from "../contract/logs_v1.ts";

type RecordValue = Record<string, any>;
type SDK = Record<string, any>;
type Result = { status: string; sent: boolean; protected: boolean; reason?: string;
  result?: RecordValue; writer?: RecordValue; receipt?: RecordValue };
export const SUMMARY_TOOL_NAME = "db_summary_save";
export const SUMMARY_PARAMETERS = {
  type: "object", properties: Object.fromEntries(["facts", "interpretations", "unknowns"].map(name =>
    [name, { type: "array", items: { type: "string" }, maxItems: 128 }])),
  required: ["facts", "interpretations", "unknowns"], additionalProperties: true,
};
const sha = (text: string) => createHash("sha256").update(text).digest("hex");
const stable = (value: unknown) => canonicalJson(value);
const bytes = (text: string) => Buffer.byteLength(text, "utf8");
const boundInput = (prepared: RecordValue) => ({ request: parseSummaryRequest(prepared?.request),
  selected: prepared.selected, omitted: prepared.omitted, priorCumulative: prepared.priorCumulative,
  definitionDigest: prepared.definitionDigest, metadata: prepared.metadata });
function freezeInput(value: any): any {
  if (value && typeof value === "object") {
    Object.values(value).forEach(freezeInput);
    Object.freeze(value);
  }
  return value;
}
const modelKey = (model: any) => stable({ provider: model?.provider ?? null,
  id: model?.id ?? null, api: model?.api ?? null, baseUrl: model?.baseUrl ?? null,
  contextWindow: model?.contextWindow ?? null, maxTokens: model?.maxTokens ?? null });

export function validateSummaryParams(value: unknown): RecordValue {
  if (!value || typeof value !== "object" || Array.isArray(value)
      || Object.keys(value).sort().join(",") !== "facts,interpretations,unknowns")
    throw new Error("invalid_result_parameters");
  const data = value as RecordValue;
  for (const name of ["facts", "interpretations", "unknowns"]) {
    if (!Array.isArray(data[name]) || data[name].length > 128
        || data[name].some((item: unknown) => typeof item !== "string"))
      throw new Error("invalid_result_parameters");
  }
  return data;
}

export async function fixedSummaryWriter(writer: RecordValue, requestId: string,
                                         result: RecordValue): Promise<RecordValue> {
  if (!writer || Object.keys(writer).sort().join(",") !== "dataRoot,moduleRoot,python"
      || [writer.python, writer.moduleRoot, writer.dataRoot].some(value =>
        typeof value !== "string" || !value.startsWith("/") || value.includes("\0")))
    throw new Error("writer_configuration_unavailable");
  return new Promise((resolve, reject) => {
    const child = execFile(writer.python, ["-m", "workbench.backend.summary_writer", writer.dataRoot, requestId],
      { timeout: 5000, maxBuffer: 4096, env: { PATH: process.env.PATH ?? "",
        PYTHONPATH: writer.moduleRoot, PYTHONDONTWRITEBYTECODE: "1" } }, (error, stdout) => {
        if (error) { reject(new Error("writer_outcome_unknown")); return; }
        try { resolve(JSON.parse(stdout)); } catch { reject(new Error("writer_outcome_unknown")); }
      });
    child.stdin?.end(JSON.stringify(result));
  });
}

export async function dispatchSummarySDK(options: {
  role: string; context: any; prepared: RecordValue; writer: RecordValue;
  observeMetadata: () => RecordValue | undefined;
  current: () => boolean; emit?: (event: RecordValue) => void;
  sdk?: SDK; save?: typeof fixedSummaryWriter;
}): Promise<Result> {
  const { role, context, prepared, writer, observeMetadata, current } = options;
  const emit = options.emit ?? (() => {});
  const defer = (reason: string): Result => ({ status: "deferred", sent: false, protected: false, reason });
  if (role !== "manager") return defer("role_not_allowed");
  let input: RecordValue;
  let inputDigest: string;
  try {
    const serialized = stable(boundInput(prepared));
    inputDigest = sha(serialized);
    // Contract parsers validate but retain aliases. Only this detached snapshot is used below.
    input = freezeInput(JSON.parse(serialized));
  }
  catch { return defer("invalid_request"); }
  const request = input.request;
  const inputMatches = () => {
    try { return sha(stable(boundInput(prepared))) === inputDigest; }
    catch { return false; }
  };
  let metadata: RecordValue;
  let skill: string;
  let sdk: SDK;
  let policy: string;
  let bundled: RecordValue;
  try {
    // Copy the observer result before validation can call any other host callback.
    metadata = freezeInput(JSON.parse(stable(observeMetadata() ?? null)));
    if (!metadata || metadata.source !== "parent_session_readback" || !current()
        || metadata.parentSessionId !== String(context.sessionManager?.getSessionId())
        || stable(metadata) !== stable(input.metadata)
        || modelKey(metadata.model) !== modelKey(context.model)) return defer("model_metadata_unavailable");
    skill = readFileSync(fileURLToPath(new URL("../skills/important-log-summary/SKILL.md", import.meta.url)), "utf8");
    policy = readFileSync(fileURLToPath(new URL("../../src/workbench/contracts/summary_compat_policy_v1.json", import.meta.url)), "utf8");
    if (!options.sdk) return defer("sdk_api_unavailable");
    sdk = options.sdk;
    if (![sdk.AgentRegistry, sdk.AgentRegistry?.global, sdk.createAgentSession,
      sdk.SessionManager?.inMemory, sdk.Settings?.isolated, sdk.Tokenizer,
      sdk.fitOutputTokensToContextWindow, sdk.toolWireSchema, sdk.getBundledAgent]
      .every(api => typeof api === "function")) return defer("sdk_api_unavailable");
    bundled = sdk.getBundledAgent("task");
    if (!bundled || bundled.source !== "bundled" || bundled.filePath !== "embedded:task.md"
        || typeof bundled.systemPrompt !== "string"
        || sha(bundled.systemPrompt) !== input.definitionDigest) return defer("agent_definition_mismatch");
  } catch { return defer("startup_configuration_unavailable"); }
  if (request.skillName !== "important-log-summary" || request.skillDigest !== sha(skill))
    return defer("skill_digest_mismatch");
  const policySha = sha(policy);
  const state = { armed: false, spent: false, protected: false, sent: false,
    result: undefined as RecordValue | undefined, receipt: undefined as RecordValue | undefined,
    contractReason: undefined as "invalid_result" | undefined,
    dispatchReason: undefined as string | undefined,
    violationReason: undefined as "guard_violation_observed" | "request_mismatch" | "compatibility_failed" | undefined };
  const requestMismatch = () => {
    state.protected = true; state.armed = false;
    if (state.violationReason !== "compatibility_failed") state.violationReason = "request_mismatch";
  };
  const reject = (reason: string) => {
    state.protected = true; state.armed = false;
    if (reason === "invalid_result") state.contractReason = "invalid_result";
    else if (reason === "result_binding_or_duplicate") state.violationReason ??= "guard_violation_observed";
    return { isError: true, content: [{ type: "text", text: JSON.stringify({ received: false, reason }) }] };
  };
  const receivedContent = () => [{ type: "text", text: JSON.stringify({ received: true, saved: false,
    requestId: request.requestId }) }];
  const tool = { name: SUMMARY_TOOL_NAME, label: "Submit work summary",
    description: "Submit one bound summary for the fixed DB writer after session validation.",
    approval: "write", loadMode: "essential", intent: "omit", parameters: SUMMARY_PARAMETERS,
    async execute(_callId: string, params: unknown) {
      if (!inputMatches()) { requestMismatch(); return reject("request_mismatch"); }
      if (!state.armed || state.spent || state.protected || !state.receipt
          || state.receipt.requestId !== request.requestId || state.receipt.inputKey !== request.inputKey
          || state.receipt.sessionId !== String(child?.sessionManager.getSessionId())
          || state.receipt.epoch !== metadata.epoch || state.receipt.route !== "private-manager-task"
          || state.receipt.policySha !== policySha || state.receipt.schemaSha !== schemaSha)
        return reject("result_binding_or_duplicate");
      state.spent = true;
      try {
        const body = validateSummaryParams(params);
        const result = parseSummaryResult({ schemaVersion: 1, requestId: request.requestId,
          inputKey: request.inputKey, skillDigest: request.skillDigest, sources: request.sources,
          ...body, usage: { schemaVersion: 1, inputTokens: null, outputTokens: null, costUsd: null } });
        state.result = result;
        emit({ event: "result_received", requestId: request.requestId, bytes: bytes(JSON.stringify(result)) });
        // This is acceptance into bounded memory, not a claim of durable save.
        return { content: receivedContent() };
      } catch { return reject("invalid_result"); }
    },
  };
  let schemaSha: string;
  try { schemaSha = sha(stable(sdk.toolWireSchema(tool))); }
  catch { return defer("sdk_api_unavailable"); }
  const prompt = JSON.stringify({ request, selected: input.selected, omitted: input.omitted,
    priorCumulative: input.priorCumulative, resultByteLimit: 8192,
    instruction: "Use db_summary_save exactly once, then stop." });
  const outputCap = metadata.outputCap;
  const inputLimit = Math.min(8192, Math.floor(metadata.model.contextWindow / 10));
  const tokenBudget = (text: string) => bytes(text); // Explicit conservative estimator, not exact token count.
  const projection = (sdkContext: any) => JSON.stringify({ systemPrompt: sdkContext.systemPrompt,
    messages: sdkContext.messages,
    tools: sdkContext.tools?.map((t: any) => ({ name: t.name, description: t.description, parameters: t.parameters })),
    inactiveTools: [] });
  const mandatoryContinuation = (model: any, sdkContext: any) => {
    // SDK arguments are an object, not an escaped JSON string. Any valid result's
    // serialized arguments fit within 8KiB; this valid maximum-size object reserves
    // those bytes without counting them again as context tokens alongside outputCap.
    const argumentsValue = { facts: [""], interpretations: [], unknowns: [] };
    argumentsValue.facts[0] = "x".repeat(8192 - bytes(JSON.stringify(argumentsValue)));
    const argumentsBytes = bytes(JSON.stringify(argumentsValue));
    const toolCallId = "summary-preflight"; // Placeholder, not a bound on provider IDs.
    const messages = [{ role: "assistant", content: [{ type: "toolCall", id: toolCallId,
      name: SUMMARY_TOOL_NAME, arguments: argumentsValue }], api: model.api, provider: model.provider,
      model: model.id, usage: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0,
        totalTokens: 0, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } },
      stopReason: "toolUse", timestamp: Date.now() },
    { role: "toolResult", toolCallId, toolName: SUMMARY_TOOL_NAME,
      content: receivedContent(),
      isError: false, timestamp: Date.now() }];
    return { context: { ...sdkContext, messages: [...sdkContext.messages, ...messages] }, argumentsBytes };
  };
  const budgetCheck = (model: any, sdkContext: any, initial: boolean): string | undefined => {
    if (metadata.outputMeaning !== "reasoning_and_visible" || metadata.outputLimitEnforced !== true
        || metadata.estimator !== "utf8_bytes_upper_bound" || !Number.isSafeInteger(outputCap)
        || typeof metadata.outputLimitSource !== "string" || !metadata.outputLimitSource
        || !Number.isSafeInteger(metadata.existingContextTokens) || metadata.existingContextTokens < 0
        || outputCap < 1 || outputCap > Math.min(8192, model.maxTokens)
        || !Number.isSafeInteger(metadata.reasoningTokens) || metadata.reasoningTokens < 0
        || !Number.isSafeInteger(metadata.visibleResultTokens) || metadata.visibleResultTokens < 1
        || metadata.reasoningTokens + metadata.visibleResultTokens > outputCap)
      return "output_budget_unavailable";
    const projected = projection(sdkContext);
    const estimate = tokenBudget(projected);
    const fitted = sdk.fitOutputTokensToContextWindow(model, sdkContext, outputCap, new sdk.Tokenizer(model));
    const continuation = initial ? mandatoryContinuation(model, sdkContext) : undefined;
    const nextEstimate = continuation ? tokenBudget(projection(continuation.context)) : undefined;
    const fixedFraming = continuation ? nextEstimate! - estimate - continuation.argumentsBytes : 0;
    const nextFitted = continuation ? sdk.fitOutputTokensToContextWindow(model, continuation.context,
      outputCap, new sdk.Tokenizer(model)) : undefined;
    emit({ event: "dispatch_preflight", requestId: request.requestId, inputEstimate: estimate,
      estimator: "utf8_bytes_upper_bound", inputLimit, outputCap, fittedCap: fitted,
      initialInputQuota: initial, conservativeWireBytes: bytes(projected) + 8192,
      ...(continuation ? { mandatoryContinuationEstimate: nextEstimate,
        mandatoryArgumentsBytes: continuation.argumentsBytes, fixedContinuationEstimate: fixedFraming,
        previousOutputReserve: outputCap, nextOutputReserve: outputCap, nextFittedCap: nextFitted,
        mandatoryWireBytes: nextEstimate! + 8192,
        // Remaining headroom after the existing allowance is a reservation only;
        // dynamic IDs, thinking/text and provider metadata have no absolute byte bound.
        dynamicExtrasReserve: 32768 - nextEstimate! - 8192 } : {}) });
    return fitted !== outputCap || (continuation && nextFitted !== outputCap) ? "output_budget_unavailable" :
      (initial && estimate > inputLimit) || bytes(projected) + 8192 > 32768
      || estimate + metadata.existingContextTokens + outputCap > model.contextWindow
      || (continuation && (nextEstimate! + 8192 > 32768
        || estimate + fixedFraming + metadata.existingContextTokens + outputCap + outputCap > model.contextWindow))
        ? "input_budget_unavailable" : undefined;
  };
  let child: any;
  let unsubscribe: (() => void) | undefined;
  let abortPromise: Promise<unknown> | undefined;
  let promptError = false;
  try {
    for (let attempt = 1; attempt <= 2; attempt++) {
      const registry = new sdk.AgentRegistry();
      const settings = sdk.Settings.isolated({ externalThinking: false, "prewalk.enabled": false,
        "tools.intentTracing": false, "retry.usageAwareFallback": false });
      const agentId = "workbench-summary-" + randomUUID();
      state.armed = false;
      state.receipt = undefined;
      let admitted = false;
      try {
        child = (await sdk.createAgentSession({ cwd: process.cwd(), model: context.model,
          thinkingLevel: metadata.thinkingLevel,
          resolveServiceTierByFamily: () => structuredClone(metadata.serviceTierByFamily),
          settings, sessionManager: sdk.SessionManager.inMemory(process.cwd()), agentRegistry: registry, agentId,
          agentName: "task", bindProcessState: false, systemPrompt: [bundled.systemPrompt, skill],
          cacheWarming: false, disableExtensionDiscovery: true, preloadedPreparedExtensions: [],
          enableMCP: false, enableLsp: false, enableIrc: false, skipPythonPreflight: true,
          skills: [], rules: [], contextFiles: [], promptTemplates: [], slashCommands: [],
          toolNames: [SUMMARY_TOOL_NAME], restrictToolNames: true, allowRestrictedCustomTools: true,
          requireYieldTool: false, customTools: [tool], hasUI: false, autoApprove: true,
          deadline: Date.now() + 25000 })).session;
        if (!inputMatches()) {
          requestMismatch();
          return { status: "deferred", sent: false, protected: true, reason: "request_mismatch" };
        }
        const receipt = Object.freeze({ requestId: request.requestId, inputKey: request.inputKey,
          sessionId: String(child.sessionManager.getSessionId()), nonce: randomUUID(),
          epoch: metadata.epoch, route: "private-manager-task", policySha, schemaSha });
        state.receipt = receipt;
        state.armed = true;
        const check = (model: any, sdkContext: any): string | undefined => {
          const binding = state.receipt;
          const tools = sdkContext?.tools;
          return !inputMatches() ? "request_mismatch" :
            !state.armed || state.protected || child.isDisposed || !current() ? "owner_inactive" :
            !binding || stable(binding) !== stable(receipt)
              || String(child.sessionManager.getSessionId()) !== receipt.sessionId ? "stale_binding" :
            !Array.isArray(tools) || tools.length !== 1 || tools[0].name !== SUMMARY_TOOL_NAME ? "roster_mismatch" :
            sha(stable(tools[0].parameters)) !== schemaSha ? "schema_mismatch" :
            sha(readFileSync(fileURLToPath(new URL("../../src/workbench/contracts/summary_compat_policy_v1.json", import.meta.url)), "utf8")) !== policySha
              ? "policy_mismatch" :
            modelKey(model) !== modelKey(metadata.model) || stable(observeMetadata() ?? null) !== stable(metadata)
              ? "model_mismatch" : undefined;
        };
        const original = child.agent.streamFn;
        let streamInvocations = 0;
        const wrapper = (model: any, sdkContext: any, streamOptions: any) => {
          // LG-D26 exempts only subsequent invocations in this same child from
          // the host-input quota. All actual wire/context/output guards remain.
          const initial = ++streamInvocations === 1;
          let reason = check(model, sdkContext) ?? budgetCheck(model, sdkContext, initial);
          emit({ event: "stream_guard", requestId: request.requestId, reason: reason ?? null });
          // Observer callbacks are host code too; recheck before the synchronous dispatch call.
          if (!inputMatches()) reason = "request_mismatch";
          if (reason) {
            if (reason === "request_mismatch") requestMismatch();
            // Inactivity caused by an already rejected result is not a new safety cause.
            if (reason !== "owner_inactive" || !state.contractReason) state.dispatchReason ??= reason;
            state.protected = true; state.armed = false; throw new Error(reason);
          }
          state.sent = true;
          return original(model, sdkContext, { ...streamOptions, maxTokens: outputCap });
        };
        child.agent.streamFn = wrapper;
        const validContext = { tools: [{ name: SUMMARY_TOOL_NAME, parameters: sdk.toolWireSchema(tool) }] };
        const originalReceipt = state.receipt;
        state.receipt = { ...receipt, sessionId: "startup-selfcheck-stale" };
        const stale = check(context.model, validContext);
        state.receipt = originalReceipt;
        const wrong = check(context.model, { tools: [{ name: "bash", parameters: sdk.toolWireSchema(tool) }] });
        const active = child.getEnabledToolNames();
        admitted = child.settings === settings && !child.isDisposed && !!registry.get(agentId)
          && !sdk.AgentRegistry.global().get(agentId) && typeof original === "function"
          && child.agent.streamFn === wrapper && active.length === 1 && active[0] === SUMMARY_TOOL_NAME
          && modelKey(child.model) === modelKey(metadata.model)
          && child.thinkingLevel === metadata.thinkingLevel
          && stable(child.serviceTierByFamily) === stable(metadata.serviceTierByFamily)
          && check(context.model, validContext) === undefined && wrong === "roster_mismatch" && stale === "stale_binding";
        emit({ event: "startup_selfcheck", requestId: request.requestId, attempt, admitted,
          sessionId: receipt.sessionId, active, definitionDigest: sha(bundled.systemPrompt), skillDigest: sha(skill) });
        if (!admitted) continue;
        const preliminary = { systemPrompt: [bundled.systemPrompt, skill],
          messages: [{ role: "user", content: [{ type: "text", text: prompt }], timestamp: Date.now() }],
          tools: [{ name: tool.name, description: tool.description, parameters: sdk.toolWireSchema(tool) }] };
        const reason = budgetCheck(context.model, preliminary, true);
        if (reason) return defer(reason);
        break;
      } catch { emit({ event: "startup_rejected", requestId: request.requestId, attempt }); }
      finally {
        if (!admitted && child) { state.armed = false; await child.dispose(); child = undefined; }
      }
    }
    if (!child || !state.armed) return defer("startup_validation_failed");
    unsubscribe = child.subscribe((event: any) => {
      const badTool = (event.type === "tool_execution_start" || event.type === "tool_execution_end")
        && event.toolName !== SUMMARY_TOOL_NAME;
      const badMessage = event.type === "message_end" && event.message?.role === "assistant"
        && Array.isArray(event.message.content) && event.message.content.some((part: any) =>
          (part.type === "toolCall" || part.type === "tool_use") && part.name !== SUMMARY_TOOL_NAME);
      if (badTool || badMessage) {
        state.protected = true; state.armed = false;
        if (badTool && event.type === "tool_execution_end" && event.isError === false)
          state.violationReason = "compatibility_failed";
        else state.violationReason ??= "guard_violation_observed";
        emit({ event: state.violationReason, requestId: request.requestId });
        abortPromise ??= child.abort({ goalReason: "internal", reason: "summary_guard_violation" });
      }
    });
    emit({ event: "prompt_start", requestId: request.requestId, sessionId: state.receipt!.sessionId });
    try { await child.prompt(prompt, { runCommands: false, throwOnDrop: true }); }
    catch { promptError = true; }
    if (abortPromise) await abortPromise;
    state.armed = false;
    unsubscribe?.(); unsubscribe = undefined;
    await child.dispose(); child = undefined;
    emit({ event: "disposed", requestId: request.requestId });
    if (!inputMatches()) requestMismatch();
    if (promptError && !state.protected) return { status: "unknown", sent: state.sent,
      protected: false, reason: "summary_outcome_unknown" };
    if (state.protected || promptError || !state.result) return {
      status: state.sent ? "rejected" : "deferred", sent: state.sent,
      protected: state.protected, reason: state.protected
        ? state.violationReason ?? state.dispatchReason ?? state.contractReason ?? "guard_violation_observed" : "result_unavailable" };
    const written = await (options.save ?? fixedSummaryWriter)(writer, request.requestId, state.result);
    if (written.saved !== true) return { status: "storage_failed", sent: true,
      protected: false, reason: written.reason ?? "storage_failed", writer: written };
    const receipt = { kind: "summary_result", requestId: request.requestId,
      resultSha: sha(JSON.stringify(state.result)), status: "stored", entryId: written.entryId };
    if (bytes(JSON.stringify(receipt)) > 4096) throw new Error("parent_receipt_overflow");
    emit({ event: "saved", requestId: request.requestId, entryId: written.entryId });
    return { status: "summary_result", sent: true, protected: false, result: state.result, writer: written, receipt };
  } catch {
    return { status: state.sent ? "unknown" : "deferred", sent: state.sent,
      protected: state.protected, reason: state.violationReason ?? state.dispatchReason ?? state.contractReason
        ?? (state.sent ? "summary_outcome_unknown" : "startup_validation_failed") };
  } finally {
    state.armed = false;
    unsubscribe?.();
    if (child) await child.dispose();
  }
}
