/** LG-D28 independent boundary regressions. Mock descriptors are not native MODEL/I/O evidence. */
import assert from "node:assert/strict";
import { createHash, randomUUID } from "node:crypto";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { summaryInputKey } from "../../omp_bridge/contract/logs_v1.ts";
import { dispatchVerificationSummarySDK } from "../../omp_bridge/g3/summary_sdk.ts";

const definition = "Independent bundled task fixture.";
const sha = (text: string) => createHash("sha256").update(text).digest("hex");
function fixture() {
  const commandId = randomUUID(), parentId = randomUUID();
  const subject = { schemaVersion: 1, kind: "command", id: commandId };
  const span = { schemaVersion: 1, source: { schemaVersion: 1, sourceKind: "terminal",
    collectorEpoch: 1, commandId }, start: 0, end: 4, digest: sha("work") };
  const skillDigest = sha(readFileSync("omp_bridge/skills/important-log-summary/SKILL.md", "utf8"));
  const request = { schemaVersion: 1, requestId: randomUUID(), subject, revision: null,
    authorizationRef: randomUUID(), authorizationEpoch: 1,
    inputKey: summaryInputKey(subject as any, null, [span] as any, [], skillDigest),
    skillName: "important-log-summary", skillDigest, sources: [span], sourceGaps: [],
    priorCumulativeEntryId: null, requestedAt: "2026-10-13T00:00:00Z",
    dbHealth: { schemaVersion: 1, dbUuid: randomUUID(), measuredAt: "2026-10-13T00:00:00Z", stale: false,
      mainBytes: 0, walBytes: 0, shmBytes: 0, totalBytes: 0, logicalLiveBytes: 0, freelistBytes: 0,
      subjectStats: [], cleanupCandidates: null, lastWrite: null, lastLock: null, lastMaintenance: null,
      diskFreeBytes: null, lastQuickCheck: null, metadataOutsideBudget: true } };
  const model = { id: "scripted", provider: "fixture", api: "openai-completions",
    baseUrl: "http://127.0.0.1:1/v1", contextWindow: 131072, maxTokens: 8192 };
  // Detached readback makes parent model drift different from canonical input tampering.
  const metadata = { source: "parent_session_readback", parentSessionId: parentId, epoch: "fixture-epoch",
    model: structuredClone(model), thinkingLevel: "off", serviceTierByFamily: {}, existingContextTokens: 0,
    outputCap: 8192, reasoningTokens: 0, visibleResultTokens: 8192, outputMeaning: "reasoning_and_visible",
    outputLimitEnforced: true, outputLimitSource: "independent-mock-contract", estimator: "utf8_bytes_upper_bound" };
  let release!: () => void;
  const readiness = new Promise<void>(resolve => { release = resolve; });
  const counters = { ready: 0, observe: 0, write: 0 };
  const registry = { awaitInitialBackgroundRefresh(_signal: AbortSignal) { counters.ready++; return readiness; } };
  const context: any = { model, modelRegistry: registry, settings: { "power.sleepPrevention": "on" },
    sessionManager: { getSessionId: () => parentId } };
  const input = { role: "manager", context, prepared: { request, metadata, selected: [{ range: span, text: "work" }],
    omitted: [], priorCumulative: null, definitionDigest: sha(definition) },
    current: () => true, observeMetadata: () => { counters.observe++; return metadata; },
    writer: { python: "/fixed/python", moduleRoot: "/fixed/src", dataRoot: "/fixed/data" } };
  return { input, registry, release, counters };
}

function boundary(control: { retry?: boolean; forbidden?: boolean } = {}) {
  const created: any[] = [], history: string[] = [], streams: any[] = [];
  class Registry {
    entries = new Map<string, any>();
    get(id: string) { return this.entries.get(id); }
    static global() { return { get: () => undefined }; }
  }
  const sdk = { AgentRegistry: Registry, Settings: { isolated: (settings: any) => structuredClone(settings) },
    SessionManager: { inMemory: () => ({}) }, Tokenizer: class {},
    fitOutputTokensToContextWindow: (_model: any, _context: any, cap: number) => cap,
    toolWireSchema: (tool: any) => tool.parameters,
    getBundledAgent: () => ({ source: "bundled", filePath: "embedded:task.md", systemPrompt: definition }),
    async createAgentSession(options: any) {
      created.push(options);
      const index = created.length, listeners = new Set<(event: any) => void>();
      history.push("create:" + index);
      const child: any = { model: options.model, settings: options.settings,
        thinkingLevel: options.thinkingLevel, serviceTierByFamily: options.resolveServiceTierByFamily(),
        isDisposed: false, sessionManager: { getSessionId: () => "independent-verifier-" + index },
        getEnabledToolNames: () => control.retry && index === 1 ? ["bash"] : ["db_summary_save"],
        agent: { streamFn: (_model: any, _context: any, stream: any) => { streams.push(stream); history.push("stream:" + index); } },
        subscribe(listener: (event: any) => void) { listeners.add(listener); return () => listeners.delete(listener); },
        async abort() { history.push("abort:" + index); },
        async dispose() { child.isDisposed = true; history.push("dispose:" + index); },
        async prompt(text: string) {
          history.push("prompt:" + index);
          const tool = options.customTools[0];
          await child.agent.streamFn(child.model, { systemPrompt: options.systemPrompt,
            messages: [{ role: "user", content: text }],
            tools: [{ name: tool.name, description: tool.description, parameters: tool.parameters }] }, {});
          const accepted = await tool.execute("fixture-call", { facts: ["work"], interpretations: [], unknowns: [] });
          assert.deepEqual(JSON.parse(accepted.content[0].text), {
            received: true, saved: false, requestId: JSON.parse(text).request.requestId });
          if (control.forbidden) for (const listener of listeners)
            listener({ type: "tool_execution_end", toolName: "bash", isError: false });
          history.push("settled:" + index);
        } };
      options.agentRegistry.entries.set(options.agentId, child);
      return { session: child };
    } };
  return { sdk, created, history, streams };
}
function dispatch(f: ReturnType<typeof fixture>, mock: ReturnType<typeof boundary>) {
  return dispatchVerificationSummarySDK({ ...f.input, sdk: mock.sdk,
    save: async () => { f.counters.write++; mock.history.push("writer"); return { saved: true, entryId: randomUUID() }; } });
}
function noChild(f: ReturnType<typeof fixture>, mock: ReturnType<typeof boundary>) {
  assert.equal(mock.created.length, 0); assert.deepEqual(mock.history, []);
  assert.deepEqual(mock.streams, []); assert.equal(f.counters.write, 0);
}

test("LG-D28 inactive owner after native readiness sends no prompt or writer", async () => {
  const f = fixture(), mock = boundary(); let active = true;
  f.input.current = () => active;
  const pending = dispatch(f, mock);
  assert.deepEqual(f.counters, { ready: 1, observe: 0, write: 0 }); noChild(f, mock);
  active = false; f.release();
  assert.deepEqual(await pending, { status: "deferred", sent: false, protected: false, reason: "model_metadata_unavailable" });
  assert.equal(f.counters.observe, 1); noChild(f, mock);
});

test("LG-D28 parent model drift during readiness is refused before SDK creation", async () => {
  const f = fixture(), mock = boundary(), canonical = JSON.stringify(f.input.prepared);
  const pending = dispatch(f, mock);
  f.input.context.model.id = "different-parent-model"; f.release();
  assert.deepEqual(await pending, { status: "deferred", sent: false, protected: false, reason: "model_metadata_unavailable" });
  assert.equal(JSON.stringify(f.input.prepared), canonical); noChild(f, mock);
});

test("LG-D28 timed-out readiness cannot resume child creation when it settles late", async t => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const f = fixture(), mock = boundary();
  const pending = dispatch(f, mock); t.mock.timers.tick(25000);
  assert.deepEqual(await pending, { status: "deferred", sent: false, protected: false, reason: "preparation_not_settled" });
  f.release(); await Promise.resolve(); await Promise.resolve();
  assert.equal(f.counters.observe, 0); noChild(f, mock);
});

test("LG-AC-22 worker verifier refuses before reading native registry", async () => {
  const f = fixture(), mock = boundary(); f.input.role = "worker";
  Object.defineProperty(f.input.context, "modelRegistry", { get() { throw Error("worker registry access"); } });
  assert.deepEqual(await dispatch(f, mock), { status: "deferred", sent: false, protected: false, reason: "role_not_allowed" });
  assert.deepEqual(f.counters, { ready: 0, observe: 0, write: 0 }); noChild(f, mock);
});

test("LG-D28 verifier keeps native reference and isolated options through one pre-prompt recovery", async () => {
  const f = fixture(), mock = boundary({ retry: true });
  const parentBefore = JSON.stringify(f.input.context), preparedBefore = JSON.stringify(f.input.prepared);
  f.release(); const result = await dispatch(f, mock);
  assert.equal(result.status, "summary_result"); assert.equal(result.sent, true); assert.equal(result.protected, false);
  assert.deepEqual(mock.history, ["create:1", "dispose:1", "create:2", "prompt:2", "stream:2", "settled:2", "dispose:2", "writer"]);
  assert.equal(f.counters.ready, 1); assert.equal(f.counters.write, 1);
  for (const options of mock.created) {
    assert.equal(options.modelRegistry, f.registry);
    assert.deepEqual(options.settings, { externalThinking: false, "prewalk.enabled": false,
      "tools.intentTracing": false, "retry.usageAwareFallback": false, "power.sleepPrevention": "off" });
    assert.equal(Object.hasOwn(options, "authStorage"), false);
    assert.deepEqual(options.toolNames, ["db_summary_save"]);
  }
  assert.notEqual(mock.created[0].settings, mock.created[1].settings);
  assert.deepEqual(mock.streams, [{ maxTokens: 8192 }]);
  assert.equal(JSON.stringify(f.input.context), parentBefore); assert.equal(JSON.stringify(f.input.prepared), preparedBefore);
  assert.equal(result.receipt?.status, "stored");
});

test("LG-D28 accepted verifier result followed by forbidden tool has no durable receipt or replay", async () => {
  const f = fixture(), mock = boundary({ forbidden: true }); f.release();
  const result = await dispatch(f, mock);
  assert.deepEqual(result, { status: "rejected", sent: true, protected: true, reason: "compatibility_failed" });
  assert.deepEqual(mock.history, ["create:1", "prompt:1", "stream:1", "abort:1", "settled:1", "dispose:1"]);
  assert.equal(f.counters.write, 0); assert.equal(mock.created.length, 1);
  assert.equal(Object.hasOwn(result, "receipt"), false);
});
