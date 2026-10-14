import assert from "node:assert/strict";
import { createHash, randomUUID } from "node:crypto";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { canonicalJson, summaryInputKey } from "../../omp_bridge/contract/logs_v1.ts";
import { countSummaryFragments, dispatchSummarySDK, validateSummaryParams } from "../../omp_bridge/g3/summary_sdk.ts";

const sha = (x: string) => createHash("sha256").update(x).digest("hex");
const definition = "Existing bundled task definition.";
function fixture() {
  const commandId = randomUUID();
  const parentId = randomUUID();
  const span = { schemaVersion: 1, source: { schemaVersion: 1, sourceKind: "terminal",
    collectorEpoch: 1, commandId }, start: 0, end: 4, digest: sha("work") };
  const subject = { schemaVersion: 1, kind: "command", id: commandId };
  const skillDigest = sha(readFileSync("omp_bridge/skills/important-log-summary/SKILL.md", "utf8"));
  const health = { schemaVersion: 1, dbUuid: randomUUID(), measuredAt: "2026-10-12T00:00:00Z", stale: false,
    mainBytes: 0, walBytes: 0, shmBytes: 0, totalBytes: 0, logicalLiveBytes: 0, freelistBytes: 0,
    subjectStats: [], cleanupCandidates: null, lastWrite: null, lastLock: null, lastMaintenance: null,
    diskFreeBytes: null, lastQuickCheck: null, metadataOutsideBudget: true };
  const request = { schemaVersion: 1, requestId: randomUUID(), subject, revision: null,
    authorizationRef: randomUUID(), authorizationEpoch: 1,
    inputKey: summaryInputKey(subject as any, null, [span] as any, [], skillDigest),
    skillName: "important-log-summary", skillDigest, sources: [span], sourceGaps: [],
    priorCumulativeEntryId: null, dbHealth: health, requestedAt: "2026-10-12T00:00:00Z" };
  const model = { id: "scripted", provider: "fixture", api: "openai-completions",
    baseUrl: "http://127.0.0.1:1/v1", contextWindow: 131072, maxTokens: 8192 };
  const metadata = { source: "parent_session_readback", parentSessionId: parentId, epoch: "readback-1",
    childSelection: { source: "configured_worker_default", selector: "fixture/scripted:max",
      modelTableDigest: "a".repeat(64), model, thinkingLevel: "max",
      descriptorSource: "manager_model_registry_getAll_all", serviceTierSource: "parent_session_readback" },
    model, thinkingLevel: "off", serviceTierByFamily: {}, existingContextTokens: 0,
    outputCap: 8192, reasoningTokens: 0, visibleResultTokens: 8192,
    outputMeaning: "reasoning_and_visible", outputLimitEnforced: true,
    outputLimitSource: "host_default", estimator: "utf8_bytes_upper_bound" };
  return { role: "manager", context: { model, modelRegistry: { getAll: () => [metadata.childSelection.model] }, sessionManager: { getSessionId: () => parentId } },
    prepared: { request, metadata, selected: [{ range: span, text: "work" }], omitted: [],
      priorCumulative: null, definitionDigest: sha(definition) }, metadata,
    writer: { python: "/fixed/python", moduleRoot: "/fixed/src", dataRoot: "/fixed/data" } };
}

function fakeSDK(mode: "normal" | "saved_then_bash" | "duplicate" | "first_bad_api" | "confirmed_then_refused" | "invalid" | "invalid_then_confirmed" | "invalid_then_refused" | "invalid_then_duplicate" = "normal",
                 beforeTool?: () => void,
                 streamControl?: (child: any, options: any, listeners: Array<(event: any) => void>) => Promise<void>) {
  const events: string[] = [];
  const prompts: string[] = [];
  let creates = 0;
  class Registry {
    values = new Map();
    get(id: string) { return this.values.get(id); }
    static global() { return { get() { return undefined; } }; }
  }
  const sdk = {
    AgentRegistry: Registry, Settings: { isolated: (value: unknown) => value },
    SessionManager: { inMemory: () => ({}) }, Tokenizer: class {},
    fitOutputTokensToContextWindow: (_m: any, _c: any, cap: number) => cap,
    toolWireSchema: (tool: any) => tool.parameters,
    getBundledAgent: () => ({ name: "task", source: "bundled", filePath: "embedded:task.md", systemPrompt: definition }),
    async createAgentSession(options: any) {
      creates++;
      const listeners: Array<(event: any) => void> = [];
      const id = randomUUID();
      const child: any = { settings: options.settings, model: options.model,
        thinkingLevel: options.thinkingLevel, serviceTierByFamily: options.resolveServiceTierByFamily(),
        isDisposed: false, sessionManager: { getSessionId: () => id },
        getEnabledToolNames: () => ["db_summary_save"],
        agent: { streamFn: mode === "first_bad_api" && creates === 1 ? undefined :
          () => { events.push("stream"); } },
        subscribe(fn: (event: any) => void) { listeners.push(fn); return () => {}; },
        async abort() { events.push("abort"); },
        async dispose() { child.isDisposed = true; events.push("dispose"); },
        async prompt(text: string) {
          events.push("prompt");
          prompts.push(text);
          if (streamControl) { await streamControl(child, options, listeners); return; }
          await child.agent.streamFn(child.model, { systemPrompt: options.systemPrompt,
            tools: [{ name: "db_summary_save", description: options.customTools[0].description,
              parameters: options.customTools[0].parameters }],
            messages: [{ role: "user", content: text }] }, {});
          beforeTool?.();
          const result = await options.customTools[0].execute("call-1", {
            facts: mode.startsWith("invalid") ? "invalid" : ["verified work"], interpretations: [], unknowns: [] });
          if (result.isError) {
            if (mode === "invalid_then_confirmed" || mode === "invalid_then_refused")
              listeners.forEach(fn => fn({ type: "tool_execution_end", toolName: "bash",
                isError: mode === "invalid_then_refused" }));
            if (mode === "invalid_then_duplicate") await options.customTools[0].execute("call-2", {
              facts: ["second"], interpretations: [], unknowns: [] });
            return;
          }
          assert.deepEqual(JSON.parse(result.content[0].text), {
            received: true, saved: false, requestId: JSON.parse(text).request.requestId });
          if (mode === "duplicate") await options.customTools[0].execute("call-2", {
            facts: ["again"], interpretations: [], unknowns: [] });
          if (mode === "saved_then_bash") listeners.forEach(fn => fn({
            type: "tool_execution_end", toolName: "bash", isError: true }));
          if (mode === "confirmed_then_refused") {
            for (const event of [
              { type: "tool_execution_end", toolName: "bash", isError: false },
              { type: "tool_execution_start", toolName: "edit" },
              { type: "tool_execution_end", toolName: "edit", isError: true },
            ]) listeners.forEach(fn => fn(event));
          }
        },
      };
      options.agentRegistry.values.set(options.agentId, child);
      return { session: child };
    },
  };
  return { sdk, events, prompts, count: () => creates };
}

test("normal result writes only after child disposal, preserving unknown usage", async () => {
  const input = fixture();
  const mock = fakeSDK();
  let writes = 0;
  const result = await dispatchSummarySDK({ ...input, sdk: mock.sdk,
    current: () => true, observeMetadata: () => input.metadata,
    save: async (_writer, requestId, typed) => {
      writes++;
      assert.equal(mock.events.at(-1), "dispose");
      assert.equal(typed.requestId, requestId);
      assert.equal(typed.inputKey, input.prepared.request.inputKey);
      assert.deepEqual(typed.usage, { schemaVersion: 1, inputTokens: null, outputTokens: null, costUsd: null });
      return { saved: true, entryId: randomUUID() };
    } });
  assert.equal(result.status, "summary_result");
  assert.equal(writes, 1);
  assert.deepEqual(mock.events, ["prompt", "stream", "dispose"]);
  assert.ok(Buffer.byteLength(canonicalJson(result.receipt)) <= 4096);
});

test("accepted-in-memory then forbidden tool or duplicate cannot write any DB entry", async () => {
  for (const mode of ["saved_then_bash", "duplicate"] as const) {
    const input = fixture();
    const mock = fakeSDK(mode);
    let writes = 0;
    const result = await dispatchSummarySDK({ ...input, sdk: mock.sdk,
      current: () => true, observeMetadata: () => input.metadata,
      save: async () => { writes++; return { saved: true }; } });
    assert.equal(result.status, "rejected");
    assert.equal(result.protected, true);
    assert.equal(writes, 0);
    assert.equal(mock.events.at(-1), "dispose");
  }
});

test("startup API failure rebuilds once without prompting the failed session", async () => {
  const input = fixture();
  const mock = fakeSDK("first_bad_api");
  const result = await dispatchSummarySDK({ ...input, sdk: mock.sdk,
    current: () => true, observeMetadata: () => input.metadata,
    save: async () => ({ saved: true, entryId: randomUUID() }) });
  assert.equal(result.status, "summary_result");
  assert.equal(mock.count(), 2);
  assert.deepEqual(mock.events, ["dispose", "prompt", "stream", "dispose"]);
});

test("unknown metadata and worker role do not create or prompt a child", async () => {
  for (const role of ["manager", "worker"]) {
    const input = fixture();
    const mock = fakeSDK();
    const result = await dispatchSummarySDK({ ...input, role, sdk: mock.sdk,
      current: () => true, observeMetadata: () => undefined });
    assert.equal(result.sent, false);
    assert.equal(result.status, "deferred");
    assert.equal(mock.count(), 0);
  }
});

test("raw tool parameters cannot smuggle host identity or executable paths", () => {
  assert.throws(() => validateSummaryParams({ facts: ["ok"], interpretations: [], unknowns: [], path: "/tmp/x" }));
  assert.throws(() => validateSummaryParams({ facts: "text", interpretations: [], unknowns: [] }));
});

test("metadata observer failure is confirmed not sent, not a successful readback", async () => {
  const input = fixture();
  const mock = fakeSDK();
  const result = await dispatchSummarySDK({ ...input, sdk: mock.sdk, current: () => true,
    observeMetadata: () => { throw new Error("getter unavailable"); } });
  assert.equal(result.sent, false);
  assert.equal(result.status, "deferred");
  assert.equal(mock.count(), 0);
});

test("missing SDK is a pre-send refusal, without dynamic resolution fallback", async () => {
  const input = fixture();
  const result = await dispatchSummarySDK({ ...input, current: () => true,
    observeMetadata: () => input.metadata });
  assert.equal(result.status, "deferred");
  assert.equal(result.sent, false);
  assert.equal(result.reason, "sdk_api_unavailable");
});

test("confirmed execution reason survives later refused events without any write", async () => {
  const input = fixture();
  const mock = fakeSDK("confirmed_then_refused");
  let writes = 0;
  const result = await dispatchSummarySDK({ ...input, sdk: mock.sdk, current: () => true,
    observeMetadata: () => input.metadata,
    save: async () => { writes++; return { saved: true }; } });
  assert.equal(result.status, "rejected");
  assert.equal(result.protected, true);
  assert.equal(result.reason, "compatibility_failed");
  assert.equal(writes, 0);
  assert.equal(mock.events.at(-1), "dispose");
});

test("all bound input changes after startup block first dispatch and retain canonical log identity", async () => {
  const mutations = [
    (prepared: any) => { prepared.request.requestId = randomUUID(); },
    (prepared: any) => { prepared.selected[0].text = "changed"; },
    (prepared: any) => { prepared.omitted.push({ reason: "changed" }); },
    (prepared: any) => { prepared.priorCumulative = "changed"; },
    (prepared: any) => { prepared.definitionDigest = sha("changed"); },
    (prepared: any) => { prepared.request.sources[0].digest = "invalid"; },
    (prepared: any) => { prepared.selected.push(prepared.selected); },
  ];
  for (const mutate of mutations) {
    const input = fixture();
    const originalId = input.prepared.request.requestId;
    const originalSelected = canonicalJson(input.prepared.selected);
    const mock = fakeSDK();
    const logged: any[] = [];
    let writes = 0;
    const result = await dispatchSummarySDK({ ...input, sdk: mock.sdk, current: () => true,
      observeMetadata: () => input.metadata,
      emit(event) { logged.push(event); if (event.event === "prompt_start") mutate(input.prepared); },
      save: async () => { writes++; return { saved: true }; } });
    assert.deepEqual(result, { status: "deferred", sent: false, protected: true, reason: "request_mismatch" });
    assert.deepEqual(mock.events, ["prompt", "dispose"]);
    assert.equal(writes, 0);
    assert.ok(logged.every(event => event.requestId === originalId));
    assert.equal(JSON.parse(mock.prompts[0]).request.requestId, originalId);
    assert.equal(canonicalJson(JSON.parse(mock.prompts[0]).selected), originalSelected);
  }
});

test("mutation in the stream observer is checked again before the transport call", async () => {
  const input = fixture();
  const mock = fakeSDK();
  const result = await dispatchSummarySDK({ ...input, sdk: mock.sdk, current: () => true,
    observeMetadata: () => input.metadata,
    emit(event) { if (event.event === "stream_guard") input.prepared.request.requestId = randomUUID(); },
    save: async () => { throw new Error("must not write"); } });
  assert.equal(result.reason, "request_mismatch");
  assert.equal(result.sent, false);
  assert.equal(result.protected, true);
  assert.deepEqual(mock.events, ["prompt", "dispose"]);
});

test("input change after transport but before tool execution rejects without any write", async () => {
  const input = fixture();
  const mock = fakeSDK("normal", () => { input.prepared.request.requestId = randomUUID(); });
  let writes = 0;
  const result = await dispatchSummarySDK({ ...input, sdk: mock.sdk, current: () => true,
    observeMetadata: () => input.metadata,
    save: async () => { writes++; return { saved: true }; } });
  assert.deepEqual(result, { status: "rejected", sent: true, protected: true, reason: "request_mismatch" });
  assert.equal(writes, 0);
  assert.deepEqual(mock.events, ["prompt", "stream", "dispose"]);
});

test("accepted result cannot reach writer if bound input changes before commit", async () => {
  for (const boundary of ["result_received", "disposed"]) {
    const input = fixture();
    const mock = fakeSDK();
    let writes = 0;
    const result = await dispatchSummarySDK({ ...input, sdk: mock.sdk, current: () => true,
      observeMetadata: () => input.metadata,
      emit(event) { if (event.event === boundary) input.prepared.selected[0].text = "changed"; },
      save: async () => { writes++; return { saved: true }; } });
    assert.deepEqual(result, { status: "rejected", sent: true, protected: true, reason: "request_mismatch" });
    assert.equal(writes, 0);
    assert.deepEqual(mock.events, ["prompt", "stream", "dispose"]);
  }
});

test("request mismatch never downgrades confirmed compatibility failure", async () => {
  const input = fixture();
  const mock = fakeSDK("confirmed_then_refused");
  let writes = 0;
  const result = await dispatchSummarySDK({ ...input, sdk: mock.sdk, current: () => true,
    observeMetadata: () => input.metadata,
    emit(event) { if (event.event === "compatibility_failed") input.prepared.request.requestId = randomUUID(); },
    save: async () => { writes++; return { saved: true }; } });
  assert.equal(result.reason, "compatibility_failed");
  assert.equal(result.status, "rejected");
  assert.equal(result.protected, true);
  assert.equal(writes, 0);
});

test("in-place observer epoch, thinking, tier and nested model changes cannot shift expected metadata", async () => {
  const mutations = [
    (metadata: any) => { metadata.epoch = "readback-2"; },
    (metadata: any) => { metadata.thinkingLevel = "high"; },
    (metadata: any) => { metadata.serviceTierByFamily.openai = "priority"; },
    (metadata: any) => { metadata.model.contextWindow = 65536; },
  ];
  for (const mutate of mutations) {
    const input = fixture();
    const observed = structuredClone(input.metadata);
    const mock = fakeSDK();
    const logged: any[] = [];
    let writes = 0;
    const result = await dispatchSummarySDK({ ...input, sdk: mock.sdk, current: () => true,
      observeMetadata: () => observed,
      emit(event) { logged.push(event); if (event.event === "prompt_start") mutate(observed); },
      save: async () => { writes++; return { saved: true }; } });
    assert.equal(result.status, "deferred");
    assert.equal(result.sent, false);
    assert.equal(result.protected, true);
    assert.ok(logged.some(event => event.event === "stream_guard" && event.reason === "model_mismatch"));
    assert.deepEqual(mock.events, ["prompt", "dispose"]);
    assert.equal(writes, 0);
  }
});

test("prepared metadata changes are part of the existing request digest", async () => {
  const input = fixture();
  const observed = structuredClone(input.metadata);
  const mock = fakeSDK();
  let writes = 0;
  const result = await dispatchSummarySDK({ ...input, sdk: mock.sdk, current: () => true,
    observeMetadata: () => observed,
    emit(event) { if (event.event === "prompt_start") input.prepared.metadata.epoch = "readback-2"; },
    save: async () => { writes++; return { saved: true }; } });
  assert.deepEqual(result, { status: "deferred", sent: false, protected: true, reason: "request_mismatch" });
  assert.deepEqual(mock.events, ["prompt", "dispose"]);
  assert.equal(writes, 0);
});

test("SDK tier resolution gets a fresh mutable clone without mutating the bound metadata", async () => {
  const input = fixture();
  const mock = fakeSDK();
  const create = mock.sdk.createAgentSession;
  let checked = false;
  mock.sdk.createAgentSession = async (options: any) => {
    const first = options.resolveServiceTierByFamily();
    assert.notStrictEqual(first, input.metadata.serviceTierByFamily);
    assert.equal(Object.isFrozen(first), false);
    first.openai = "priority";
    const second = options.resolveServiceTierByFamily();
    assert.notStrictEqual(first, second);
    assert.deepEqual(second, {});
    assert.deepEqual(input.metadata.serviceTierByFamily, {});
    checked = true;
    return create(options);
  };
  let writes = 0;
  const result = await dispatchSummarySDK({ ...input, sdk: mock.sdk, current: () => true,
    observeMetadata: () => input.metadata,
    save: async () => { writes++; return { saved: true }; } });
  assert.equal(checked, true);
  assert.equal(result.status, "summary_result");
  assert.equal(writes, 1);
  assert.deepEqual(input.metadata.serviceTierByFamily, {});
});

test("invalid contract stays distinct from later sticky safety violations", async () => {
  for (const [mode, reason] of [["invalid", "invalid_result"],
    ["invalid_then_confirmed", "compatibility_failed"],
    ["invalid_then_refused", "guard_violation_observed"],
    ["invalid_then_duplicate", "guard_violation_observed"]] as const) {
    const input = fixture(); const mock = fakeSDK(mode); let writes = 0;
    const outcome = await dispatchSummarySDK({ ...input, sdk: mock.sdk,
      current: () => true, observeMetadata: () => input.metadata,
      save: async () => { writes++; return { saved: true }; } });
    assert.equal(outcome.status, "rejected"); assert.equal(outcome.sent, true);
    assert.equal(outcome.protected, true); assert.equal(outcome.reason, reason);
    assert.equal(writes, 0); assert.equal(mock.events.at(-1), "dispose");
  }
});

test("first and followup stream budget reasons survive disposal with no write", async () => {
  for (const followup of [false, true]) for (const reason of ["input_budget_unavailable", "output_budget_unavailable"]) {
    const input = fixture(); let writes = 0;
    const mock = fakeSDK("normal", undefined, async (child, options) => {
      const context = { systemPrompt: options.systemPrompt, messages: [{ role: "user", content: "work" }],
        tools: [{ name: options.customTools[0].name, description: options.customTools[0].description,
          parameters: options.customTools[0].parameters }] };
      if (followup) await child.agent.streamFn(child.model, context, {});
      // First and followup actual projections both keep the transport ceiling.
      if (reason === "input_budget_unavailable") context.messages[0].content = "x".repeat(30000);
      else mock.sdk.fitOutputTokensToContextWindow = (_m: any, _c: any, cap: number) => cap - 1;
      await child.agent.streamFn(child.model, context, {});
    });
    const outcome = await dispatchSummarySDK({ ...input, sdk: mock.sdk, current: () => true,
      observeMetadata: () => input.metadata, save: async () => { writes++; return { saved: true }; } });
    assert.equal(outcome.reason, reason); assert.equal(outcome.sent, followup);
    assert.equal(outcome.status, followup ? "rejected" : "deferred"); assert.equal(outcome.protected, true);
    assert.equal(writes, 0); assert.equal(mock.events.filter(x => x === "stream").length, Number(followup));
    assert.equal(mock.events.at(-1), "dispose");
  }
});

test("confirmed forbidden or duplicate safety outranks an earlier stream budget reason", async () => {
  for (const duplicate of [false, true]) {
    const input = fixture(); let writes = 0;
    const mock = fakeSDK("normal", undefined, async (child, options, listeners) => {
      const context = { systemPrompt: options.systemPrompt, messages: [{ role: "user", content: "work" }],
        tools: [{ name: options.customTools[0].name, description: options.customTools[0].description,
          parameters: options.customTools[0].parameters }] };
      await child.agent.streamFn(child.model, context, {});
      // Exercise actual wire overflow, rather than the removed followup input quota.
      context.messages[0].content = "x".repeat(30000);
      try { await child.agent.streamFn(child.model, context, {}); } catch {
        if (duplicate) await options.customTools[0].execute("after-budget", { facts: ["ignored"], interpretations: [], unknowns: [] });
        else listeners.forEach(fn => fn({ type: "tool_execution_end", toolName: "bash", isError: false }));
      }
    });
    const outcome = await dispatchSummarySDK({ ...input, sdk: mock.sdk, current: () => true,
      observeMetadata: () => input.metadata, save: async () => { writes++; return { saved: true }; } });
    assert.equal(outcome.reason, duplicate ? "guard_violation_observed" : "compatibility_failed");
    assert.equal(outcome.status, "rejected"); assert.equal(outcome.sent, true); assert.equal(outcome.protected, true);
    assert.equal(writes, 0); assert.equal(mock.events.at(-1), "dispose");
  }
});

test("followup refused by prior invalid result preserves the contract cause", async () => {
  const input = fixture(); let writes = 0;
  const mock = fakeSDK("normal", undefined, async (child, options) => {
    const context = { systemPrompt: options.systemPrompt, messages: [{ role: "user", content: "work" }],
      tools: [{ name: options.customTools[0].name, description: options.customTools[0].description,
        parameters: options.customTools[0].parameters }] };
    await child.agent.streamFn(child.model, context, {});
    await options.customTools[0].execute("invalid", { facts: "bad", interpretations: [], unknowns: [] });
    await child.agent.streamFn(child.model, context, {});
  });
  const outcome = await dispatchSummarySDK({ ...input, sdk: mock.sdk, current: () => true,
    observeMetadata: () => input.metadata, save: async () => { writes++; return { saved: true }; } });
  assert.equal(outcome.reason, "invalid_result"); assert.equal(outcome.sent, true);
  assert.equal(outcome.status, "rejected"); assert.equal(writes, 0); assert.equal(mock.events.at(-1), "dispose");
});


test("provider exception is unknown while successful completion without a result is contract rejection", async () => {
  for (const throwing of [true, false]) {
    const input = fixture(); let writes = 0;
    const mock = fakeSDK("normal", undefined, async (child, options) => {
      await child.agent.streamFn(child.model, { systemPrompt: options.systemPrompt,
        messages: [{ role: "user", content: "work" }], tools: [{ name: "db_summary_save",
          description: options.customTools[0].description, parameters: options.customTools[0].parameters }] }, {});
      if (throwing) throw new Error("owned provider fixture failure");
    });
    const outcome = await dispatchSummarySDK({ ...input, sdk: mock.sdk, current: () => true,
      observeMetadata: () => input.metadata, save: async () => { writes++; return { saved: true }; } });
    assert.equal(outcome.status, throwing ? "unknown" : "rejected");
    assert.equal(outcome.reason, throwing ? "summary_outcome_unknown" : "result_unavailable");
    assert.equal(outcome.sent, true); assert.equal(outcome.protected, false);
    assert.equal(writes, 0); assert.equal(mock.events.at(-1), "dispose");
  }
});

test("continuation above the old quota writes once after actual wire and context checks", async () => {
  const input = fixture(); const preflights: any[] = []; let writes = 0;
  const mock = fakeSDK("normal", undefined, async (child, options) => {
    const context: any = { systemPrompt: options.systemPrompt,
      messages: [{ role: "user", content: [{ type: "text", text: mock.prompts[0] }], timestamp: Date.now() }],
      tools: [{ name: "db_summary_save", description: options.customTools[0].description,
        parameters: options.customTools[0].parameters }] };
    await child.agent.streamFn(child.model, context, {});
    const args = { facts: ["x".repeat(6500)], interpretations: [], unknowns: [] };
    const receipt = await options.customTools[0].execute("real-call", args);
    assert.equal(receipt.isError, undefined);
    context.messages.push({ role: "assistant", content: [{ type: "toolCall", id: "real-call",
      name: "db_summary_save", arguments: args }], timestamp: Date.now() },
      { role: "toolResult", toolCallId: "real-call", toolName: "db_summary_save",
        content: receipt.content, isError: false, timestamp: Date.now() });
    await child.agent.streamFn(child.model, context, {});
  });
  const fittedContexts: any[] = [];
  mock.sdk.fitOutputTokensToContextWindow = (_m: any, context: any, cap: number) => {
    fittedContexts.push(context); return cap;
  };
  const outcome = await dispatchSummarySDK({ ...input, sdk: mock.sdk, current: () => true,
    observeMetadata: () => input.metadata, emit: event => {
      if (event.event === "dispatch_preflight") preflights.push(event);
    }, save: async () => { writes++; assert.equal(mock.events.at(-1), "dispose"); return { saved: true }; } });
  assert.equal(outcome.status, "summary_result"); assert.equal(writes, 1);
  assert.equal(mock.events.filter(x => x === "stream").length, 2);
  const later = preflights.find(p => p.initialInvocation === false);
  assert.ok(later.inputEstimate > 8192); assert.ok(later.conservativeWireBytes <= 32768);
  for (const early of preflights.filter(p => p.initialInvocation)) {
    assert.equal(early.mandatoryArgumentsBytes, 8192);
    assert.equal(early.previousOutputReserve, 8192); assert.equal(early.nextOutputReserve, 8192);
    assert.equal(early.mandatoryWireBytes, early.mandatoryContinuationEstimate + 8192);
    assert.equal(early.dynamicExtrasReserve, 32768 - early.mandatoryWireBytes);
  }
  const synthetic = fittedContexts.find(c => c.messages.at(-2)?.content?.[0]?.id === "summary-preflight");
  const args = synthetic.messages.at(-2).content[0].arguments;
  assert.equal(typeof args, "object"); assert.equal(Buffer.byteLength(JSON.stringify(args)), 8192);
  assert.deepEqual(JSON.parse(JSON.stringify(args)), args); validateSummaryParams(args);
  const receipt = synthetic.messages.at(-1);
  assert.deepEqual(JSON.parse(receipt.content[0].text), {
    received: true, saved: false, requestId: input.prepared.request.requestId });
  // Quotes inside the SDK text receipt are escaped by its outer projection; args stay an object.
  assert.ok(JSON.stringify(receipt).includes('\\"received\\"'));
  assert.equal(receipt.isError, false); assert.equal(receipt.details, undefined);
  assert.equal(receipt.providerMetadata, undefined);
});

test("mandatory next full output reservation or fitted cap refuses before prompt", async () => {
  for (const failFit of [false, true]) {
    const input = fixture(); const mock = fakeSDK(); let writes = 0;
    // A single output fits; the required second full output plus receipt does not.
    if (!failFit) input.metadata.childSelection.model.contextWindow = 20000;
    else mock.sdk.fitOutputTokensToContextWindow = (_m: any, c: any, cap: number) =>
      c.messages.length >= 3 ? cap - 1 : cap;
    const preflights: any[] = [];
    const outcome = await dispatchSummarySDK({ ...input, sdk: mock.sdk, current: () => true,
      observeMetadata: () => input.metadata, emit: event => { if (event.event === "dispatch_preflight") preflights.push(event); },
      save: async () => { writes++; return { saved: true }; } });
    assert.equal(outcome.status, "deferred"); assert.equal(outcome.sent, false); assert.equal(writes, 0);
    assert.equal(outcome.reason, failFit ? "output_budget_unavailable" : "input_budget_unavailable");
    assert.deepEqual(mock.events, ["dispose"]);
    if (!failFit) assert.ok(preflights[0].inputEstimate + 8192 <= 20000);
  }
});

test("actual later context overflow is still rejected with sent true and no write", async () => {
  const input = fixture(); input.metadata.childSelection.model.contextWindow = 25000;
  let writes = 0; const preflights: any[] = [];
  const mock = fakeSDK("normal", undefined, async (child, options) => {
    const context = { systemPrompt: options.systemPrompt, messages: [{ role: "user", content: "work" }],
      tools: [{ name: "db_summary_save", description: options.customTools[0].description,
        parameters: options.customTools[0].parameters }] };
    await child.agent.streamFn(child.model, context, {});
    context.messages[0].content = "x".repeat(18000);
    await child.agent.streamFn(child.model, context, {});
  });
  const outcome = await dispatchSummarySDK({ ...input, sdk: mock.sdk, current: () => true,
    observeMetadata: () => input.metadata, emit: event => { if (event.event === "dispatch_preflight") preflights.push(event); },
    save: async () => { writes++; return { saved: true }; } });
  assert.equal(outcome.status, "rejected"); assert.equal(outcome.sent, true);
  assert.equal(outcome.reason, "input_budget_unavailable"); assert.equal(writes, 0);
  const later = preflights.find(p => !p.initialInvocation);
  assert.ok(later.conservativeWireBytes <= 32768);
  assert.ok(later.inputEstimate + 8192 > 25000);
});

// These counters are contract stand-ins. The owned installed-SDK conformance
// probe supplies actual native values separately; no native accuracy is inferred.
function nativeContractSDK(mock: ReturnType<typeof fakeSDK>, fullCount?: (text: string) => number) {
  mock.sdk.Tokenizer = class {
    encoding = "ClaudeV3";
    countTokens(text: string, mode: string) {
      assert.equal(mode, "strict");
      return fullCount ? fullCount(text) : Math.ceil(Buffer.byteLength(text) / 4);
    }
    checkTokenBudget(text: string, _budget: number) {
      return { exact: true, fits: false, tokens: this.countTokens(text, "strict") };
    }
  } as any;
  return mock;
}
function attachNativeProof(input: any, sdk: any) {
  input.metadata.model.tokenizer = "claude-v3";
  const skill = readFileSync("omp_bridge/skills/important-log-summary/SKILL.md", "utf8");
  const shared = [definition + "\n\n" + skill, canonicalJson({ request: input.prepared.request,
    selected: input.prepared.selected, omitted: input.prepared.omitted, priorCumulative: null, resultByteLimit: 8192 })];
  input.prepared.countBinding = countSummaryFragments(input.metadata, shared, sdk);
}

test("known-family strict proof and unavailable encodings remain distinguishable", () => {
  const input: any = fixture();
  const mock = nativeContractSDK(fakeSDK());
  input.metadata.model.tokenizer = "claude-v3";
  const proof = countSummaryFragments(input.metadata, ["한국어", "quoted\\\""], mock.sdk);
  assert.equal(proof.exact, true);
  assert.equal(proof.encoding, "ClaudeV3");
  assert.equal(proof.estimator, "native_known_family_strict");
  input.metadata.model.tokenizer = "unknown";
  const unknown = countSummaryFragments(input.metadata, ["한국어"], mock.sdk);
  assert.equal(unknown.exact, false);
  assert.equal(unknown.tokens[0], Buffer.byteLength("한국어"));
  input.metadata.model.tokenizer = "claude-v3";
  mock.sdk.Tokenizer = class { encoding = "o200k_base"; } as any;
  assert.equal(countSummaryFragments(input.metadata, ["text"], mock.sdk).exact, false);
  mock.sdk.Tokenizer = class { encoding = "ClaudeV3"; countTokens() { return 1; }
    checkTokenBudget() { return { exact: false, tokens: 1 }; } } as any;
  assert.equal(countSummaryFragments(input.metadata, ["text"], mock.sdk).exact, false);
});

test("native binding is checked before child creation and participates in mutation guards", async () => {
  for (const field of ["ownerDigest", "fragmentDigests", "tokens", "encoding"]) {
    const input: any = fixture(); const mock = nativeContractSDK(fakeSDK());
    attachNativeProof(input, mock.sdk);
    input.prepared.countBinding[field] = field === "tokens" ? [1, 1] : "wrong";
    const result = await dispatchSummarySDK({ ...input, sdk: mock.sdk, current: () => true,
      observeMetadata: () => input.metadata });
    assert.equal(result.reason, "input_count_binding_mismatch");
    assert.equal(result.sent, false); assert.equal(mock.count(), 0);
  }
  const input: any = fixture(); const mock = nativeContractSDK(fakeSDK());
  attachNativeProof(input, mock.sdk);
  const result = await dispatchSummarySDK({ ...input, sdk: mock.sdk, current: () => true,
    observeMetadata: () => input.metadata,
    emit(event) { if (event.event === "prompt_start") input.prepared.countBinding.tokens[0]++; } });
  assert.equal(result.reason, "request_mismatch"); assert.equal(result.sent, false);
});

test("shared native count never replaces the actual full-projection context guard", async () => {
  const input: any = fixture();
  const mock = nativeContractSDK(fakeSDK(), text => text.startsWith('{"systemPrompt":') ? 130000 : 100);
  attachNativeProof(input, mock.sdk);
  const observed: any[] = [];
  const result = await dispatchSummarySDK({ ...input, sdk: mock.sdk, current: () => true,
    observeMetadata: () => input.metadata, emit: event => observed.push(event) });
  assert.equal(result.reason, "input_budget_unavailable"); assert.equal(result.sent, false);
  assert.equal(mock.prompts.length, 0);
  const preflight = observed.find(x => x.event === "dispatch_preflight");
  assert.equal(preflight.sharedInputEstimate, 2248);
  assert.equal(preflight.inputEstimate, 130000);
  assert.equal(preflight.sharedQuotaIsFullProjectionBound, false);
});

test("native projection keeps byte wire bounds, two reserves and family/model detachment", async () => {
  const input: any = fixture(); const mock = nativeContractSDK(fakeSDK());
  attachNativeProof(input, mock.sdk);
  const create = mock.sdk.createAgentSession;
  mock.sdk.createAgentSession = async options => {
    assert.notStrictEqual(options.model, input.context.model);
    assert.equal(options.model.tokenizer, "claude-v3");
    return create(options);
  };
  const events: any[] = [];
  const result = await dispatchSummarySDK({ ...input, sdk: mock.sdk, current: () => true,
    observeMetadata: () => input.metadata, emit: event => events.push(event),
    save: async () => ({ saved: true, entryId: randomUUID() }) });
  assert.equal(result.status, "summary_result");
  const preflight = events.find(x => x.event === "dispatch_preflight");
  assert.equal(preflight.estimator, "native_known_family_strict");
  assert.equal(preflight.previousOutputReserve, 8192); assert.equal(preflight.nextOutputReserve, 8192);
  assert.ok(preflight.mandatoryWireBytes > preflight.mandatoryContinuationEstimate + 8192);
  assert.equal(preflight.dynamicExtrasReserve, 32768 - preflight.mandatoryWireBytes);
  const changed: any = fixture(); const changedMock = nativeContractSDK(fakeSDK());
  attachNativeProof(changed, changedMock.sdk);
  const refused = await dispatchSummarySDK({ ...changed, sdk: changedMock.sdk, current: () => true,
    observeMetadata: () => changed.metadata,
    emit(event) { if (event.event === "prompt_start") changed.context.model.tokenizer = "qwen3"; } });
  assert.equal(refused.sent, false); assert.equal(refused.protected, true);
});

test("native-advertising metadata without an optional proof does not rewrite observer truth", async () => {
  const input: any = fixture(); const mock = nativeContractSDK(fakeSDK());
  input.metadata.model.tokenizer = "claude-v3";
  input.metadata.estimator = "native_known_family_strict";
  const original = structuredClone(input.metadata);
  const result = await dispatchSummarySDK({ ...input, sdk: mock.sdk, current: () => true,
    observeMetadata: () => input.metadata, save: async () => ({ saved: true, entryId: randomUUID() }) });
  assert.equal(result.status, "summary_result"); assert.deepEqual(input.metadata, original);
});


test("configured child selection and unknown observed budgets use a fresh context host default", async () => {
  const input: any = fixture(); const mock = fakeSDK();
  const selected = { ...input.metadata.model, id: "worker-light", maxTokens: 4000 };
  Object.assign(input.metadata.childSelection, { model: selected, selector: "fixture/worker-light:max" });
  Object.assign(input.metadata, { outputCap: 4000, outputMeaning: "unknown", outputLimitEnforced: null,
    existingContextTokens: null, reasoningTokens: null, visibleResultTokens: null });
  const before = structuredClone(input.metadata);
  const create = mock.sdk.createAgentSession;
  mock.sdk.createAgentSession = async options => {
    assert.equal(options.model.id, "worker-light"); assert.notStrictEqual(options.model, selected);
    assert.equal(options.thinkingLevel, "max"); assert.equal(input.context.model.id, "scripted");
    return create(options);
  };
  const result = await dispatchSummarySDK({ ...input, sdk: mock.sdk, current: () => true,
    observeMetadata: () => input.metadata, save: async () => ({ saved: true }) });
  assert.equal(result.status, "summary_result"); assert.deepEqual(input.metadata, before);
  const counted: any[] = [];
  selected.tokenizer = "claude-v3";
  mock.sdk.Tokenizer = class { encoding = "ClaudeV3"; constructor(model: any) { counted.push(model); }
    countTokens() { return 1; } checkTokenBudget() { return { exact: true, tokens: 1 }; } };
  const proof = countSummaryFragments(input.metadata, ["fact"], mock.sdk);
  assert.equal(proof.exact, true); assert.equal(counted[0].id, "worker-light");
  delete input.metadata.childSelection;
  const legacyMock = fakeSDK();
  const legacy = await dispatchSummarySDK({ ...input, sdk: legacyMock.sdk, current: () => true,
    observeMetadata: () => input.metadata });
  assert.equal(legacy.sent, false); assert.equal(legacy.reason, "model_metadata_unavailable");
  assert.equal(legacyMock.count(), 0);
});
