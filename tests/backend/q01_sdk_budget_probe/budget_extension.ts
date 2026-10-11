import { execFile } from "node:child_process";
import { createHash } from "node:crypto";
import { appendFileSync, readFileSync, writeFileSync } from "node:fs";
import { Tokenizer, fitOutputTokensToContextWindow } from "@oh-my-pi/pi-agent-core";
import { toolWireSchema } from "@oh-my-pi/pi-ai";
import { AgentRegistry, createAgentSession, SessionManager, Settings } from "@oh-my-pi/pi-coding-agent";
import { getBundledAgent } from "@oh-my-pi/pi-coding-agent/task/agents";
import { budgetAdmission, selectRanges, type Segment } from "./budget_support.ts";

const trace = process.env.Q01_BUDGET_TRACE!;
const log = (value: Record<string, unknown>) => appendFileSync(trace, JSON.stringify(value) + "\n");
const sha = (text: string) => createHash("sha256").update(text).digest("hex");
const bytes = (text: string) => Buffer.byteLength(text, "utf8");
const stable = (x: any): string => JSON.stringify(x, (_key, value) =>
  value && typeof value === "object" && !Array.isArray(value)
    ? Object.fromEntries(Object.entries(value).sort(([a], [b]) => a.localeCompare(b))) : value);

async function fullProbe(ctx: any) {
  const bundled = getBundledAgent("task");
  if (!bundled || bundled.source !== "bundled" || bundled.filePath !== "embedded:task.md")
    throw new Error("bundled_task_unavailable");
  const skill = readFileSync(process.env.Q01_BUDGET_SKILL!, "utf8");
  const request = JSON.parse(readFileSync(process.env.Q01_BUDGET_REQUEST!, "utf8"));
  const segments = JSON.parse(readFileSync(process.env.Q01_BUDGET_INPUT!, "utf8")) as Segment[];
  const configuredMax = process.env.Q01_BUDGET_SELECTION_MAX;
  const selectionMax = configuredMax === undefined ? 850 : Number(configuredMax);
  if (!Number.isInteger(selectionMax) || selectionMax < 234 || selectionMax > 850)
    throw new Error("invalid_fixture_selection_max");
  const selection = selectRanges(segments, selectionMax);
  const requestId = request.requestId;
  if (request.schemaVersion !== 1 || request.skillDigest !== sha(skill) ||
      request.sources?.length !== selection.selected.length ||
      selection.selected.some((x, i) => request.sources[i].digest !== sha(x.text) ||
        request.sources[i].start !== x.start || request.sources[i].end !== x.end))
    throw new Error("host_request_source_binding");
  const prompt = JSON.stringify({ requestId,
    selected: selection.selected.map(x => [x.source, x.revision, x.start, x.end, x.text]),
    omitted: selection.omitted.map(x => [x.source, x.revision, x.start, x.end, x.reason, x.estimatedBytes]),
    instruction: "Save with db_summary_save." });
  const model = ctx.model;
  const tokenizer = new Tokenizer(model);
  const inputLimit = Math.floor(Math.min(8192, model?.contextWindow * 0.1));
  const outputCap = Math.min(8192, model?.maxTokens);
  const schema = { type: "object", properties: { summary: { type: "string", minLength: 1, maxLength: 2000 } },
    required: ["summary"], additionalProperties: true };
  const tool = { name: "db_summary_save", label: "Save summary", description: "Fixed private SQLite writer.",
    approval: "write" as const, loadMode: "essential" as const, intent: "omit" as const, parameters: schema,
    async execute(_callId: string, params: Record<string, unknown>) {
      log({ event: "execute", params });
      if (!state.armed || state.spent || state.protected || !params || typeof params !== "object" ||
          Array.isArray(params) || Object.keys(params).length !== 1 ||
          typeof params.summary !== "string" || params.summary.length < 1 || params.summary.length > 2000)
        return { content: [{ type: "text", text: '{"saved":false}' }], isError: true };
      state.spent = true;
      const result = { schemaVersion: 1, requestId, inputKey: request.inputKey,
        skillDigest: request.skillDigest, sources: request.sources,
        facts: [params.summary], interpretations: [], unknowns: [],
        usage: { schemaVersion: 1, inputTokens: null, outputTokens: null, costUsd: null } };
      const resultText = JSON.stringify(result);
      if (bytes(resultText) > 8192) { state.protected = true;
        return { content: [{ type: "text", text: '{"saved":false}' }], isError: true }; }
      const argv = [process.env.Q01_BUDGET_WRITER!, JSON.stringify({ summary: params.summary })];
      const written = await new Promise<{ error: string | null; stdout: string }>(resolve =>
        execFile(process.env.Q01_BUDGET_PYTHON!, argv,
          { timeout: 5000, maxBuffer: 4096, env: { PATH: process.env.PATH || "" } },
          (error, stdout) => resolve({ error: error ? String(error) : null, stdout })));
      log({ event: "writer", executable: process.env.Q01_BUDGET_PYTHON, argv,
        error: written.error, stdout: written.stdout.trim() });
      if (written.error || written.stdout.trim() !== '{"saved":true}')
        return { content: [{ type: "text", text: '{"saved":false}' }], isError: true };
      state.saved = true;
      writeFileSync(process.env.Q01_BUDGET_TYPED!, resultText);
      log({ event: "saved", resultBytes: bytes(resultText), resultSha: sha(resultText), requestId });
      return { content: [{ type: "text", text: JSON.stringify({ saved: true, requestId }) }] };
    },
  };
  const state = { armed: false, spent: false, saved: false, protected: false };
  const calls = { prompt: 0, originalStream: 0 };
  const preliminary = JSON.stringify({ systemPrompt: [bundled.systemPrompt, skill],
    tools: [{ name: tool.name, description: tool.description, parameters: toolWireSchema(tool) }],
    messages: [{ role: "user", content: prompt }] });
  const preliminaryBytes = bytes(preliminary);
  const roundTripAllowance = 900; // observed scripted tool-call/result framing, rounded upward
  log({ event: "selection", selectionMax, collectedBytes: selection.collectedBytes,
    selected: selection.selected.map(({ text, ...x }) => ({ ...x, textSha: sha(text), textBytes: bytes(text) })),
    omitted: selection.omitted, selectedBytes: selection.selectedBytes,
    omittedBytes: selection.omittedBytes, promptBytes: bytes(prompt), promptSha: sha(prompt),
    preliminaryBytes, roundTripAllowance, inputLimit, outputCap, outputReserve: outputCap + 64,
    tokenizerEncoding: tokenizer.encoding,
    nativeDiagnostic: tokenizer.checkTokenBudget(preliminary, inputLimit),
    source: "scripted_child_resolved_model" });
  // With an unknown model encoding, raw UTF-8 bytes are the conservative token upper bound.
  const provisionalInput = { inputBytes: preliminaryBytes, inputLimit, roundTripAllowance,
    contextWindow: model?.contextWindow, outputCap, fittedCap: outputCap,
    outputMeaningKnown: true }; // Only this private scripted provider declares the cap.
  const provisional = budgetAdmission(provisionalInput);
  log({ event: "preliminary_admission", ...provisional, inputBytes: preliminaryBytes,
    promptCount: calls.prompt, originalStreamCount: calls.originalStream });
  if (!provisional.admitted) {
    log({ event: "budget_unavailable", reason: provisional.reason, stage: "preliminary" });
    return;
  }
  const oversized = "mandatory".repeat(500);
  const crowd = { systemPrompt: ["budget crowd ".repeat(15000)], tools: [],
    messages: [{ role: "user", content: [{ type: "text", text: "short" }], timestamp: Date.now() }] };
  const lowered = fitOutputTokensToContextWindow(model, crowd as any, outputCap, tokenizer);
  const negatives = [
    { name: "mandatory_oversize", input: { ...provisionalInput,
      inputBytes: bytes(oversized) + preliminaryBytes } },
    { name: "fitter_lowered", input: { ...provisionalInput, fittedCap: lowered } },
    { name: "meaning_unknown", input: { ...provisionalInput, outputMeaningKnown: false } },
  ];
  let allNegativesRejected = true;
  for (const negative of negatives) {
    const decision = budgetAdmission(negative.input);
    log({ event: "negative_preflight", case: negative.name, ...decision,
      inputBytes: negative.input.inputBytes, fittedCap: negative.input.fittedCap,
      outputMeaningKnown: negative.input.outputMeaningKnown,
      promptCount: calls.prompt, originalStreamCount: calls.originalStream });
    if (decision.admitted) allNegativesRejected = false;
  }
  if (!allNegativesRejected) {
    log({ event: "fatal", error: "negative_budget_admitted" });
    return;
  }
  const registry = new AgentRegistry();
  const settings = Settings.isolated({ externalThinking: false, "prewalk.enabled": false,
    "tools.intentTracing": false, "retry.usageAwareFallback": false });
  const agentId = "q01-budget-private";
  let child: any;
  try {
    child = (await createAgentSession({ cwd: process.cwd(), model, settings,
      sessionManager: SessionManager.inMemory(process.cwd()), agentRegistry: registry, agentId,
      agentName: "task", bindProcessState: false, systemPrompt: [bundled.systemPrompt, skill],
      cacheWarming: false, disableExtensionDiscovery: true, preloadedPreparedExtensions: [],
      enableMCP: false, enableLsp: false, enableIrc: false, skipPythonPreflight: true,
      skills: [], rules: [], contextFiles: [], promptTemplates: [], slashCommands: [],
      toolNames: ["db_summary_save"], restrictToolNames: true, allowRestrictedCustomTools: true,
      requireYieldTool: false, customTools: [tool], hasUI: false, autoApprove: true,
      deadline: Date.now() + 25000 })).session;
    const selected = child.model;
    const active = child.getEnabledToolNames();
    const original = child.agent.streamFn;
    const schemaSha = sha(stable(toolWireSchema(tool)));
    const check = (m: any, context: any) => {
      const names = Array.isArray(context?.tools) ? context.tools.map((x: any) => x.name) : [];
      return !state.armed || state.protected || child.isDisposed ? "inactive" :
        names.length !== 1 || names[0] !== "db_summary_save" ? "roster" :
        sha(stable(context.tools[0].parameters)) !== schemaSha ? "schema" :
        m?.provider !== model?.provider || m?.id !== model?.id || m?.api !== model?.api ? "model" : undefined;
    };
    const wrapper = (m: any, context: any, options: any) => {
      const reason = check(m, context);
      // The public fitter frames only these tool fields; SDK-internal tool metadata is not prompt text.
      const sentTool = (x: any) => ({ name: x.name, description: x.description, parameters: x.parameters });
      const projected = { systemPrompt: context?.systemPrompt, messages: context?.messages,
        tools: (context?.tools || []).map(sentTool),
        inactiveTools: (context?.inactiveTools || []).map(sentTool) };
      const contextText = JSON.stringify(projected);
      const contextBytes = bytes(contextText);
      const rawContextBytes = bytes(JSON.stringify(context));
      const componentBytes = Object.fromEntries(Object.entries(context ?? {}).map(([key, value]) =>
        [key, bytes(JSON.stringify(value))]));
      const nativeDiagnostic = tokenizer.checkTokenBudget(contextText, inputLimit);
      const fittedCap = fitOutputTokensToContextWindow(m, context, outputCap, tokenizer);
      const conservativeWireBound = contextBytes + 8192;
      const decision = budgetAdmission({ inputBytes: contextBytes, inputLimit,
        roundTripAllowance: 0, contextWindow: m?.contextWindow, outputCap, fittedCap,
        outputMeaningKnown: true });
      const budgetReason = decision.reason;
      log({ event: "dispatch_preflight", names: context?.tools?.map((x: any) => x.name),
        admitted: decision.admitted, reason: reason || budgetReason, contextBytes, rawContextBytes,
        inputLimit, nativeDiagnostic,
        conservativeWireBound, outputCap, outputReserve: outputCap + 64,
        fittedCap, contextWindow: m.contextWindow, systemFragments: context?.systemPrompt?.length,
        messageCount: context?.messages?.length, toolCount: context?.tools?.length,
        toolSchemaSha: context?.tools?.length === 1 ? sha(stable(context.tools[0].parameters)) : null,
        componentBytes, promptCount: calls.prompt, originalStreamCount: calls.originalStream });
      if (reason || budgetReason) { state.protected = true; state.armed = false;
        throw new Error("q01_budget_guard:" + (reason || budgetReason)); }
      calls.originalStream += 1;
      return original(m, context, { ...options, maxTokens: outputCap });
    };
    child.agent.streamFn = wrapper;
    state.armed = true;
    const expectedContext = { tools: [{ name: "db_summary_save", parameters: toolWireSchema(tool) }] };
    const wrongContext = { tools: [{ name: "bash", parameters: toolWireSchema(tool) }] };
    const admitted = child.settings === settings && registry.get(agentId) && !AgentRegistry.global().get(agentId) &&
      selected?.id === model?.id && selected?.api === model?.api &&
      active.length === 1 && active[0] === "db_summary_save" && typeof original === "function" &&
      child.agent.streamFn === wrapper && check(model, expectedContext) === undefined &&
      check(model, wrongContext) === "roster";
    log({ event: "startup", admitted: Boolean(admitted), active,
      childSessionId: String(child.sessionManager.getSessionId()),
      model: { provider: selected?.provider,
      id: selected?.id, api: selected?.api, contextWindow: selected?.contextWindow,
      maxTokens: selected?.maxTokens }, bodySha: sha(bundled.systemPrompt), skillSha: sha(skill),
      systemBytes: bytes(bundled.systemPrompt) + bytes(skill), source: bundled.source,
      filePath: bundled.filePath });
    if (!admitted) { state.armed = false; log({ event: "summary_deferred", reason: "startup" }); return; }
    log({ event: "prompt_start", requestId, promptSha: sha(prompt), promptBytes: bytes(prompt) });
    calls.prompt += 1;
    try { await child.prompt(prompt, { runCommands: false, throwOnDrop: true }); }
    catch (error) { log({ event: "prompt_error", error: String(error) }); }
    log({ event: "done", saved: state.saved, protected: state.protected, spent: state.spent,
      promptCount: calls.prompt, originalStreamCount: calls.originalStream });
  } finally {
    state.armed = false;
    if (child) { await child.dispose(); log({ event: "disposed" }); }
  }
}

export default function budgetProbe(pi: any) {
  if (process.env.Q01_BUDGET_MODE === "full") {
    pi.on("session_start", (_event: unknown, ctx: any) => {
      void fullProbe(ctx).catch(error => log({ event: "fatal", error: String(error) }));
    });
    return;
  }
  pi.on("session_start", (_event: unknown, ctx: any) => {
    try {
      const model = ctx.model;
      const tokenizer = new Tokenizer(model);
      const small = tokenizer.checkTokenBudget("short local text", 100);
      const ambiguous = tokenizer.checkTokenBudget("a".repeat(10000), 100);
      const context = { systemPrompt: ["small system"], tools: [],
        messages: [{ role: "user", content: [{ type: "text", text: "short prompt" }], timestamp: Date.now() }] };
      const crowded = { ...context, systemPrompt: ["budget local crowd ".repeat(15000)] };
      log({ event: "discriminator", model: { provider: model?.provider, id: model?.id,
        api: model?.api, contextWindow: model?.contextWindow, maxTokens: model?.maxTokens },
        tokenizerEncoding: tokenizer.encoding, small, ambiguous,
        unchanged: fitOutputTokensToContextWindow(model, context, 8192, tokenizer),
        reduced: fitOutputTokensToContextWindow(model, crowded, 8192, tokenizer),
        activeTools: pi.getActiveTools() });
    } catch (error) {
      log({ event: "discriminator_error", error: String(error) });
    }
  });
}
