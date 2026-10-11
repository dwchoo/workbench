import assert from "node:assert/strict";
import { createHash, randomUUID } from "node:crypto";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { canonicalJson, summaryInputKey } from "../../omp_bridge/contract/logs_v1.ts";
import { dispatchSummarySDK, validateSummaryParams } from "../../omp_bridge/g3/summary_sdk.ts";

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
    model, thinkingLevel: "off", serviceTierByFamily: {}, existingContextTokens: 0,
    outputCap: 8192, reasoningTokens: 0, visibleResultTokens: 8192,
    outputMeaning: "reasoning_and_visible", outputLimitEnforced: true,
    outputLimitSource: "fixture-contract", estimator: "utf8_bytes_upper_bound" };
  return { role: "manager", context: { model, sessionManager: { getSessionId: () => parentId } },
    prepared: { request, metadata, selected: [{ range: span, text: "work" }], omitted: [],
      priorCumulative: null, definitionDigest: sha(definition) }, metadata,
    writer: { python: "/fixed/python", moduleRoot: "/fixed/src", dataRoot: "/fixed/data" } };
}

function fakeSDK(mode: "normal" | "saved_then_bash" | "duplicate" | "first_bad_api" | "confirmed_then_refused" = "normal",
                 beforeTool?: () => void) {
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
          await child.agent.streamFn(child.model, { systemPrompt: options.systemPrompt,
            tools: [{ name: "db_summary_save", description: options.customTools[0].description,
              parameters: options.customTools[0].parameters }],
            messages: [{ role: "user", content: text }] }, {});
          beforeTool?.();
          const result = await options.customTools[0].execute("call-1", {
            facts: ["verified work"], interpretations: [], unknowns: [] });
          if (result.isError) return;
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
