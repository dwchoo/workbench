/** Only installed public SDK APIs; dispatch/guard/result/writer are product code. */
import { appendFileSync, existsSync, readFileSync, writeFileSync } from "node:fs";
import { createHash, randomUUID } from "node:crypto";
import { AgentRegistry, createAgentSession, SessionManager, Settings } from "@oh-my-pi/pi-coding-agent";
import { toolWireSchema } from "@oh-my-pi/pi-ai";
import { Tokenizer, fitOutputTokensToContextWindow } from "@oh-my-pi/pi-agent-core";
import { getBundledAgent } from "@oh-my-pi/pi-coding-agent/task/agents";
import { dispatchSummarySDK } from "../../../omp_bridge/g3/summary_sdk.ts";

const privateRoot = process.env.Q01_PRODUCT_PRIVATE!;
const log = (data: any) => appendFileSync(privateRoot + "/trace.jsonl", JSON.stringify({
  monotonicNs: process.hrtime.bigint().toString(), pid: process.pid, ...data }) + "\n");
const sha = (data: string) => createHash("sha256").update(data).digest("hex");
const canonical = (value: any): string => value === null || typeof value !== "object" ? JSON.stringify(value) :
  Array.isArray(value) ? "[" + value.map(canonical).join(",") + "]" :
  "{" + Object.keys(value).sort().map(key => JSON.stringify(key) + ":" + canonical(value[key])).join(",") + "}";
const sdk = { AgentRegistry, createAgentSession, SessionManager, Settings,
  toolWireSchema, Tokenizer, fitOutputTokensToContextWindow, getBundledAgent };

export default function probe(pi: any) {
  pi.on("session_start", (_event: any, context: any) => {
    void run(pi, context).catch(error => log({ event: "fatal", error: String(error) }));
  });
}

async function run(pi: any, context: any) {
  const parent = AgentRegistry.global().list().find((entry: any) =>
    entry.session?.sessionManager.getSessionId() === context.sessionManager.getSessionId())?.session;
  if (!parent) throw new Error("actual_parent_getters_unavailable");
  const definition = getBundledAgent("task")!;
  const skill = readFileSync(process.env.Q01_PRODUCT_SKILL!, "utf8");
  const descriptor = () => ({ source: "parent_session_readback", epoch: "owned-parent-" + context.sessionManager.getSessionId(),
    parentSessionId: String(parent.sessionManager.getSessionId()),
    model: { id: parent.model.id, provider: parent.model.provider, api: parent.model.api,
      baseUrl: parent.model.baseUrl, contextWindow: parent.model.contextWindow, maxTokens: parent.model.maxTokens },
    thinkingLevel: parent.thinkingLevel, serviceTierByFamily: parent.serviceTierByFamily,
    existingContextTokens: 0, outputCap: 8192, reasoningTokens: 0, visibleResultTokens: 8192,
    outputMeaning: "reasoning_and_visible", outputLimitEnforced: true,
    outputLimitSource: "owned-scripted-provider-capability-not-operating-model-proof", estimator: "utf8_bytes_upper_bound" });
  const nativeState = () => ({ model: descriptor().model, thinkingLevel: parent.thinkingLevel,
    serviceTierByFamily: structuredClone(parent.serviceTierByFamily) });
  const nativeInitial = nativeState();
  const nativeInitialDigest = sha(canonical(nativeInitial));
  await parent.setActiveToolsByName(["read"]);
  const toolsBefore = pi.getActiveTools();
  const entriesBefore = context.sessionManager.getEntries().length;
  writeFileSync(privateRoot + "/definition.txt", definition.systemPrompt);
  writeFileSync(privateRoot + "/metadata.json", JSON.stringify(descriptor()));
  log({ event: "ready", metadata: descriptor(), parentContextUsage: parent.getContextUsage(),
    definition: { name: definition.name, source: definition.source, filePath: definition.filePath },
    definitionDigest: sha(definition.systemPrompt), skillDigest: sha(skill), parentTools: toolsBefore,
    parentEntries: entriesBefore, parentReadbackTools: parent.getEnabledToolNames(), nativeParentState: nativeInitial, nativeParentDigest: nativeInitialDigest });
  const deadline = Date.now() + 30000;
  while (!existsSync(privateRoot + "/prepared.json") && Date.now() < deadline)
    await new Promise(resolve => setTimeout(resolve, 25));
  if (!existsSync(privateRoot + "/prepared.json")) throw new Error("owner_prepared_timeout");
  const cases = JSON.parse(readFileSync(privateRoot + "/prepared.json", "utf8"));
  let priorChildSession: string | undefined;
  for (const [name, prepared] of Object.entries(cases)) {
    const value: any = prepared;
    const canonicalRequestId = value.request.requestId;
    const before = context.sessionManager.getEntries().length;
    log({ event: "case_start", case: name, canonicalRequestId });
    let creates = 0;
    const nativeDescriptor = descriptor();
    let caseMetadata: any = structuredClone(nativeDescriptor);
    const originalCaseMetadata = caseMetadata;
    const nativeTier = nativeDescriptor.serviceTierByFamily;
    const cloneEquivalent = canonical(nativeDescriptor) === canonical(caseMetadata);
    log({ event: "fixture_dto_bound", case: name, cloneCanonicalEquivalent: cloneEquivalent,
      dtoDigest: sha(canonical(caseMetadata)), descriptorDigest: sha(canonical(nativeDescriptor)),
      tierTypeof: typeof nativeTier,
      tierPrototype: Object.getPrototypeOf(nativeTier) === Object.prototype ? "Object.prototype" :
        Object.getPrototypeOf(nativeTier) === null ? "null" : "other",
      tierPlainObject: typeof nativeTier === "object" && nativeTier !== null &&
        Object.getPrototypeOf(nativeTier) === Object.prototype,
      detachedTier: caseMetadata.serviceTierByFamily !== nativeTier });
    if (!cloneEquivalent) throw new Error("fixture_descriptor_clone_not_equivalent");
    const caseSDK = { ...sdk, createAgentSession: async (options: any) => {
      creates++;
      if (name === "sdk_tier_clone") {
        const before = structuredClone(caseMetadata.serviceTierByFamily);
        const first = options.resolveServiceTierByFamily();
        first.openai = "priority";
        const second = options.resolveServiceTierByFamily();
        log({ event: "owner_sdk_tier_return_mutated", case: name, first, second,
          independentObjects: first !== second, expectedOriginal: before,
          observerOriginal: caseMetadata.serviceTierByFamily });
      }
      const result = await createAgentSession({ ...options,
        ...(name === "startup_repeat" || name === "startup_rebuild" && creates === 1
          ? { allowRestrictedCustomTools: false } : {}) });
      const child = result.session;
      const actualId = String(child.sessionManager.getSessionId());
      const originalPrompt = child.prompt.bind(child);
      const originalDispose = child.dispose.bind(child);
      if (name === "startup_missing") child.agent.streamFn = undefined;
      if (name === "startup_noop") {
        const original = child.agent.streamFn;
        Object.defineProperty(child.agent, "streamFn", { configurable: true,
          get: () => original, set: () => {} });
      }
      if (name === "startup_stale") {
        const originalId = child.sessionManager.getSessionId.bind(child.sessionManager);
        let reads = 0;
        child.sessionManager.getSessionId = () => ++reads === 1 ? originalId() : priorChildSession;
      }
      child.prompt = async (...args: any[]) => {
        if (name === "runtime_roster") {
          await child.setActiveToolsByName([]);
          log({ event: "owner_roster_changed", case: name, readback: child.getEnabledToolNames() });
        }
        if (name === "runtime_model") {
          await child.setModelTemporary({ ...child.model, id: "scripted-alternate" });
          log({ event: "owner_model_changed", case: name, readback: child.model.id });
        }
        if (name === "runtime_stale") child.sessionManager.getSessionId = () => priorChildSession;
        log({ event: "actual_prompt", case: name, childSessionId: actualId });
        return originalPrompt(...args);
      };
      child.dispose = async (...args: any[]) => {
        await originalDispose(...args);
        log({ event: "actual_disposed", case: name, childSessionId: actualId });
      };
      log({ event: "actual_created", case: name, attempt: creates, childSessionId: actualId,
        active: child.getEnabledToolNames(), privateRegistry: !!options.agentRegistry.get(options.agentId),
        globalRegistry: !!AgentRegistry.global().get(options.agentId), settingsSame: child.settings === options.settings,
        metadata: { model: child.model.id, thinking: child.thinkingLevel, tier: child.serviceTierByFamily } });
      if (name !== "startup_stale" && name !== "runtime_stale") priorChildSession = actualId;
      return result;
    } };
    const dispatch = name === "runtime_policy" ? (await import(process.env.Q01_PRODUCT_MIRROR! +
      "/omp_bridge/g3/summary_sdk.ts")).dispatchSummarySDK : dispatchSummarySDK;
    const result = await dispatch({ role: name === "worker_role" ? "worker" : "manager",
      sdk: name === "missing_sdk" ? undefined : name === "startup_api" ? { ...caseSDK, toolWireSchema: null } : caseSDK,
      context, prepared: value,
      writer: { python: process.env.Q01_PRODUCT_PYTHON!, moduleRoot: process.env.Q01_PRODUCT_SRC!, dataRoot: privateRoot + "/data" },
      observeMetadata: () => caseMetadata, current: () => true,
      emit: event => {
        log({ ...event, case: name, canonicalRequestId });
        if (event.event === "prompt_start") {
          if (name === "metadata_epoch") caseMetadata.epoch += "-changed";
          if (name === "metadata_thinking") caseMetadata.thinkingLevel = "high";
          if (name === "metadata_tier") caseMetadata.serviceTierByFamily.openai = "priority";
          if (name === "metadata_nested_model") caseMetadata.model.contextWindow += 1024;
          if (name === "metadata_replace") caseMetadata = { ...caseMetadata, epoch: caseMetadata.epoch + "-replacement" };
          if (name === "prepared_metadata") value.metadata.epoch += "-prepared-changed";
          if (name.startsWith("metadata_") || name === "prepared_metadata")
            log({ event: "owner_metadata_changed", case: name, sameObserverObject: caseMetadata === originalCaseMetadata, observer: caseMetadata, preparedMetadata: value.metadata });
        }
        // Supported owner callback, before the first stream. No guard removal.
        if (name === "request_mismatch" && event.event === "prompt_start") {
          value.request.requestId = randomUUID();
          log({ event: "owner_request_changed", case: name, canonicalRequestId, changedRequestId: value.request.requestId });
        }
        if (event.event === "prompt_start" && name === "source_mismatch") value.request.sources[0].end++;
        if (event.event === "prompt_start" && name === "selected_mismatch") value.selected[0].text += "changed";
        if (event.event === "prompt_start" && name === "prior_mismatch") value.priorCumulative = {
          entryId: randomUUID(), body: { schemaVersion: 1, facts: [], interpretations: [], unknowns: [] } };
        if (event.event === "disposed" && name === "late_before_commit") value.selected[0].text += "changed-after-dispose";
        if (name === "runtime_policy" && event.event === "prompt_start") {
          const policy = process.env.Q01_PRODUCT_MIRROR! + "/src/workbench/contracts/summary_compat_policy_v1.json";
          log({ event: "owner_policy_changed", case: name, before: sha(readFileSync(policy, "utf8")) });
          appendFileSync(policy, "\n");
        }
      } });
    const nativeAfter = nativeState();
    const nativeUnchanged = sha(canonical(nativeAfter)) === nativeInitialDigest;
    if (!nativeUnchanged) throw new Error("fixture_parent_metadata_changed:" + name);
    writeFileSync(privateRoot + "/result-" + name + ".json", JSON.stringify(result));
    log({ event: "case_done", case: name, canonicalRequestId, status: result.status, sent: result.sent,
      protected: result.protected, creates, parentEntriesBefore: before,
      parentEntriesAfter: context.sessionManager.getEntries().length, parentTools: pi.getActiveTools(), nativeParentState: nativeAfter, nativeParentDigest: sha(canonical(nativeAfter)), nativeParentUnchanged: nativeUnchanged });
    if (result.receipt) {
      pi.appendEntry("workbench-summary-receipt", result.receipt);
      log({ event: "parent_receipt", case: name, receipt: result.receipt,
        receiptBytes: Buffer.byteLength(JSON.stringify(result.receipt)), parentEntries: context.sessionManager.getEntries().length });
    }
  }
  const nativePreContinuation = nativeState();
  if (sha(canonical(nativePreContinuation)) !== nativeInitialDigest)
    throw new Error("fixture_parent_metadata_changed_before_continuation");
  log({ event: "parent_continuation_start", active: parent.getEnabledToolNames(),
    nativeParentState: nativePreContinuation, nativeParentDigest: sha(canonical(nativePreContinuation)) });
  await parent.prompt("Q01_PRODUCT_MAIN_AFTER_FAILURE", { runCommands: false, throwOnDrop: true });
  log({ event: "parent_continuation_done", active: parent.getEnabledToolNames() });
  log({ event: "done", parentToolsSame: JSON.stringify(toolsBefore) === JSON.stringify(pi.getActiveTools()),
    parentEntriesBefore: entriesBefore, parentEntriesAfter: context.sessionManager.getEntries().length });
}
