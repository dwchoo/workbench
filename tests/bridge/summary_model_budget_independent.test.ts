/** LG-D30 independent actual observer/SDK host boundaries; provider I/O is a double. */
import assert from "node:assert/strict";
import { test } from "node:test";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { canonicalJson, summaryInputKey } from "../../omp_bridge/contract/logs_v1.ts";
import { observeModelMetadata, modelMetadataObserver } from "../../omp_bridge/g3/model_metadata.ts";
import { countSummaryFragments, dispatchSummarySDK, dispatchVerificationSummarySDK } from "../../omp_bridge/g3/summary_sdk.ts";

const sha = (s: string) => createHash("sha256").update(s).digest("hex");
const parentDescriptor = () => ({ id: "parent", provider: "parent-provider", api: "parent-api",
  baseUrl: "https://parent.invalid", contextWindow: 4096, maxTokens: 16384, tokenizer: "claude-v5" });
const childDescriptor = () => ({ id: "light", provider: "child-provider", api: "child-api",
  baseUrl: "https://child.invalid", contextWindow: 24000, maxTokens: 1024, tokenizer: "qwen3" });
const definition = "Fixture bundled DB-only task.";
const uid = "11111111-1111-4111-8111-111111111111";
const stamp = "2026-10-14T00:00:00Z";
const skill = readFileSync(new URL("../../omp_bridge/skills/important-log-summary/SKILL.md", import.meta.url), "utf8");

function fixture() {
  const parentModel = parentDescriptor(), selected = childDescriptor();
  const selection = { source: "configured_worker_default", selector: "child-provider/light:low",
    modelTableDigest: "a".repeat(64) };
  const seen: any = { forbidden: [], tokenModels: [], streams: [], options: [], saves: [], events: [],
    fits: [], disposed: 0, prompts: 0, actions: undefined, params: { facts: ["fixture fact"], interpretations: [], unknowns: [] } };
  let rows: any[] = [parentModel, selected];
  const registry = { getAll(scope: string) { assert.equal(scope, "all"); return rows; } };
  const sessionManager = { getSessionId: () => "parent-session" };
  const parent: any = { model: parentModel, modelRegistry: registry, sessionManager,
    thinkingLevel: "high", serviceTierByFamily: { fixture: "standard" } };
  for (const name of ["prompt", "authStorage", "settings", "getContextUsage", "setModel", "setThinkingLevel"]) {
    Object.defineProperty(parent, name, { get() { seen.forbidden.push(name); throw new Error("forbidden parent access"); } });
  }
  const context = { model: parentModel, modelRegistry: registry, sessionManager,
    getContextUsage() { seen.forbidden.push("context usage"); throw new Error("forbidden context stats"); } };
  class AgentRegistry {
    entries = new Map();
    static global() { return { list: () => [{ session: parent }], get: () => undefined }; }
    get(id: string) { return this.entries.get(id); }
  }
  class Tokenizer {
    encoding = "Qwen3";
    constructor(model: any) { seen.tokenModels.push(structuredClone(model)); }
    countTokens(text: string, mode: string) { assert.equal(mode, "strict"); return text ? 9000 : 0; }
    checkTokenBudget(text: string, limit: number) { assert.equal(limit, 0); return { exact: true, tokens: text ? 9000 : 0 }; }
  }
  const sdk: any = { AgentRegistry, Tokenizer,
    SessionManager: { inMemory: () => ({ getSessionId: () => "private-child-session" }) },
    Settings: { isolated: (options: any) => ({ isolatedFixture: true, options }) },
    toolWireSchema: (tool: any) => tool.parameters,
    getBundledAgent: (name: string) => { assert.equal(name, "task");
      return { source: "bundled", filePath: "embedded:task.md", systemPrompt: definition }; },
    fitOutputTokensToContextWindow: (model: any, projection: any, cap: number) => {
      seen.fits.push({ model: structuredClone(model), projection: structuredClone(projection), cap }); return cap;
    },
    async createAgentSession(options: any) {
      seen.options.push(options);
      const child: any = { model: options.model, thinkingLevel: options.thinkingLevel,
        serviceTierByFamily: options.resolveServiceTierByFamily(), settings: options.settings,
        sessionManager: options.sessionManager, isDisposed: false,
        agent: { streamFn(model: any, projection: any, opts: any) {
          seen.streams.push({ model: structuredClone(model), projection: structuredClone(projection), opts });
          return { fixtureStream: true };
        } },
        getEnabledToolNames: () => ["db_summary_save"], subscribe: () => () => {}, abort: async () => {},
        async dispose() { seen.disposed++; child.isDisposed = true; },
        async prompt(prompt: string, promptOptions: any) {
          seen.prompts++;
          assert.deepEqual(promptOptions, { runCommands: false, throwOnDrop: true });
          const projection = { systemPrompt: options.systemPrompt,
            messages: [{ role: "user", content: [{ type: "text", text: prompt }] }],
            tools: [{ name: "db_summary_save", description: options.customTools[0].description,
              parameters: options.customTools[0].parameters }] };
          await child.agent.streamFn(child.model, projection, {});
          if (seen.actions) {
            seen.actions(); // Between completed dispatches, never inside guard-to-original window.
            await child.agent.streamFn(child.model, projection, {});
          }
          await options.customTools[0].execute("fixture-call", seen.params);
        } };
      options.agentRegistry.entries.set(options.agentId, { session: child });
      return { session: child };
    } };
  const observe = modelMetadataObserver(() => context, sdk, selection);
  return { parent, parentModel, selected, selection, seen, sdk, context, observe,
    setRows: (value: any[]) => { rows = value; } };
}

function prepared(f: ReturnType<typeof fixture>, text = "independent source") {
  const subject: any = { schemaVersion: 1, kind: "command", id: uid };
  const range: any = { schemaVersion: 1, source: { schemaVersion: 1, sourceKind: "terminal",
    collectorEpoch: 1, commandId: uid }, start: 0, end: Buffer.byteLength(text), digest: sha(text) };
  const skillDigest = sha(skill);
  const request = { schemaVersion: 1, requestId: uid, subject, revision: null, authorizationRef: uid,
    authorizationEpoch: 1, skillName: "important-log-summary", skillDigest, sources: [range], sourceGaps: [],
    priorCumulativeEntryId: null, inputKey: summaryInputKey(subject, null, [range], [], skillDigest), requestedAt: stamp,
    dbHealth: { schemaVersion: 1, dbUuid: uid, measuredAt: stamp, stale: false, mainBytes: 0, walBytes: 0,
      shmBytes: 0, totalBytes: 0, logicalLiveBytes: 0, freelistBytes: 0, subjectStats: [], cleanupCandidates: null,
      lastWrite: null, lastLock: null, lastMaintenance: null, diskFreeBytes: null, lastQuickCheck: null,
      metadataOutsideBudget: true } };
  return { request, selected: [{ range, text }], omitted: [], priorCumulative: null,
    definitionDigest: sha(definition), metadata: f.observe() };
}

async function dispatch(f: ReturnType<typeof fixture>, p = prepared(f), verification = false) {
  return (verification ? dispatchVerificationSummarySDK : dispatchSummarySDK)({ role: "manager", context: f.context, prepared: p,
    writer: { python: "/fixture/python", moduleRoot: "/fixture/modules", dataRoot: "/fixture/db" },
    observeMetadata: f.observe, current: () => true, sdk: f.sdk,
    emit: event => f.seen.events.push(event),
    save: async (_writer, requestId, result) => {
      f.seen.saves.push({ requestId, result }); return { saved: true, entryId: uid };
    } });
}

test("LG-D30 actual public getter observation is readonly, detached and preserves semantic unknowns", () => {
  const f = fixture();
  const before = canonicalJson({ parentModel: f.parentModel, child: f.selected,
    thinking: f.parent.thinkingLevel, tiers: f.parent.serviceTierByFamily, selection: f.selection });
  const m = observeModelMetadata(f.context, f.sdk, f.selection);
  assert.ok(m);
  assert.deepEqual(m.model, parentDescriptor());
  assert.deepEqual(m.childSelection.model, childDescriptor());
  assert.equal(m.childSelection.thinkingLevel, "low");
  assert.equal(m.thinkingLevel, "high");
  assert.equal(m.childSelection.source, "configured_worker_default");
  assert.equal(m.childSelection.descriptorSource, "manager_model_registry_getAll_all");
  assert.equal(m.childSelection.serviceTierSource, "parent_session_readback");
  assert.deepEqual(m.serviceTierByFamily, { fixture: "standard" });
  for (const key of ["existingContextTokens", "reasoningTokens", "visibleResultTokens", "outputLimitEnforced"])
    assert.equal(m[key], null);
  assert.equal(m.outputCap, 1024);
  assert.equal(m.outputLimitSource, "host_default");
  assert.equal(m.outputMeaning, "unknown");
  assert.equal(m.status, "inconclusive");
  assert.deepEqual(m.unknowns, ["current_context_unavailable", "output_ceiling_enforcement_unavailable",
    "reasoning_budget_semantics_unavailable"]);
  m.model.id = "changed-copy"; m.childSelection.model.id = "changed-copy"; m.serviceTierByFamily.fixture = "changed-copy";
  assert.equal(canonicalJson({ parentModel: f.parentModel, child: f.selected,
    thinking: f.parent.thinkingLevel, tiers: f.parent.serviceTierByFamily, selection: f.selection }), before);
  assert.deepEqual(f.seen.forbidden, []);
  f.selected.maxTokens = 16384;
  assert.equal(f.observe().outputCap, 8192);
});

test("LG-D30 absent or ambiguous configured child and missing actual context defer without parent fallback", () => {
  const f = fixture();
  f.setRows([f.parentModel]); assert.equal(f.observe(), undefined);
  f.setRows([f.parentModel, f.selected, structuredClone(f.selected)]); assert.equal(f.observe(), undefined);
  f.setRows([f.parentModel, f.selected]); f.selected.contextWindow = 0; assert.equal(f.observe(), undefined);
  assert.deepEqual(f.seen.forbidden, []);
  assert.equal(f.seen.options.length, 0);
});

test("LG-D30 SDK uses chosen light descriptor/count/context and fresh DB-only session above old quota", async () => {
  const f = fixture(), p = prepared(f);
  const originalParent = canonicalJson({ model: f.parentModel, thinking: f.parent.thinkingLevel,
    tiers: f.parent.serviceTierByFamily });
  const result = await dispatch(f, p);
  assert.equal(result.status, "summary_result");
  assert.equal(result.sent, true); assert.equal(result.protected, false);
  assert.equal(f.seen.options.length, 1); assert.equal(f.seen.streams.length, 1); assert.equal(f.seen.saves.length, 1);
  const o = f.seen.options[0];
  assert.deepEqual(o.model, childDescriptor()); assert.equal(o.thinkingLevel, "low");
  assert.deepEqual(o.resolveServiceTierByFamily(), { fixture: "standard" });
  assert.equal(o.modelRegistry, f.context.modelRegistry);
  assert.notEqual(o.sessionManager, f.context.sessionManager); assert.equal(o.bindProcessState, false);
  assert.deepEqual(o.toolNames, ["db_summary_save"]); assert.equal(o.restrictToolNames, true);
  assert.equal(o.enableMCP, false); assert.equal(o.enableLsp, false); assert.equal(o.enableIrc, false);
  for (const key of ["skills", "rules", "contextFiles", "promptTemplates", "slashCommands"])
    assert.deepEqual(o[key], []);
  assert.equal(f.seen.streams[0].opts.maxTokens, 1024);
  assert.equal(f.seen.streams[0].projection.messages.length, 1);
  assert.equal(f.seen.streams[0].projection.messages[0].role, "user");
  assert.ok(f.seen.tokenModels.length > 0);
  for (const model of f.seen.tokenModels) assert.deepEqual(model, childDescriptor());
  const events = f.seen.events.filter((e: any) => e.event === "dispatch_preflight");
  assert.ok(events.some((e: any) => e.inputEstimate === 9000 && e.inputLimit === 24000 && e.outputCap === 1024));
  assert.equal(canonicalJson({ model: f.parentModel, thinking: f.parent.thinkingLevel,
    tiers: f.parent.serviceTierByFamily }), originalParent);
  assert.deepEqual(f.seen.forbidden, []); assert.equal(f.seen.disposed, 1);
});

test("LG-D30 count proof belongs to chosen child tokenizer; parent-owned proof cannot authorize dispatch", async () => {
  const f = fixture(), p: any = prepared(f);
  const fragments = [definition + "\n\n" + skill, canonicalJson({ request: p.request, selected: p.selected,
    omitted: p.omitted, priorCumulative: p.priorCumulative, resultByteLimit: 8192 })];
  const proof = countSummaryFragments(p.metadata, fragments, f.sdk);
  assert.equal(proof.encoding, "Qwen3"); assert.equal(proof.exact, true);
  assert.equal(proof.ownerDigest, sha(canonicalJson({ epoch: p.metadata.epoch,
    parentSessionId: "parent-session", model: childDescriptor() })));
  assert.deepEqual(proof.tokens, [9000, 9000]);
  p.countBinding = proof;
  const accepted = await dispatch(f, p);
  assert.equal(accepted.status, "summary_result");
  assert.equal(f.seen.streams.length, 1); assert.equal(f.seen.saves.length, 1);
  for (const model of f.seen.tokenModels) assert.deepEqual(model, childDescriptor());
  const other = fixture(), wrong: any = prepared(other);
  wrong.countBinding = { ...proof, ownerDigest: sha(canonicalJson({ epoch: p.metadata.epoch,
    parentSessionId: "parent-session", model: parentDescriptor() })) };
  const result = await dispatch(other, wrong);
  assert.equal(result.status, "deferred"); assert.equal(result.reason, "input_count_binding_mismatch");
  assert.equal(result.sent, false); assert.equal(other.seen.options.length, 0); assert.equal(other.seen.saves.length, 0);
});

test("LG-D30 next real dispatch refuses changed startup-selection epoch or actual parent descriptor", async () => {
  for (const kind of ["configured-table", "parent-descriptor"]) {
    const f = fixture(), p = prepared(f), epoch = p.metadata.epoch;
    f.seen.actions = () => {
      if (kind === "configured-table") f.selection.modelTableDigest = "b".repeat(64);
      else f.parentModel.contextWindow = 5000;
      assert.notEqual(f.observe().epoch, epoch);
    };
    const result = await dispatch(f, p);
    assert.equal(result.status, "rejected", kind); assert.equal(result.reason, "model_mismatch", kind);
    assert.equal(result.sent, true); assert.equal(result.protected, true);
    assert.equal(f.seen.streams.length, 1); assert.equal(f.seen.options.length, 1);
    assert.equal(f.seen.saves.length, 0); assert.equal(f.seen.disposed, 1);
  }
});

test("LG-D30 child context, 32KiB wire, 8KiB DB result and request guards remain without replay", async () => {
  for (const kind of ["child-context", "wire", "result", "request"]) {
    const f = fixture();
    if (kind === "child-context") f.selected.contextWindow = 6000;
    const p = prepared(f, kind === "wire" ? "x".repeat(40000) : "independent source");
    if (kind === "result") f.seen.params.facts = ["x".repeat(9000)];
    if (kind === "request") f.seen.actions = () => { p.request.authorizationEpoch = 2; };
    const result = await dispatch(f, p);
    assert.equal(result.reason, kind === "result" ? "invalid_result" : kind === "request"
      ? "request_mismatch" : "input_budget_unavailable", kind);
    assert.equal(result.status, ["child-context", "wire"].includes(kind) ? "deferred" : "rejected", kind);
    assert.equal(result.sent, !["child-context", "wire"].includes(kind));
    assert.equal(f.seen.streams.length, ["child-context", "wire"].includes(kind) ? 0 : 1);
    assert.equal(f.seen.saves.length, 0); assert.equal(f.seen.options.length, 1); assert.equal(f.seen.disposed, 1);
  }
});

test("LG-D28 verifier refuses external selected Worker child even with localhost parent", async () => {
  const f = fixture();
  f.parentModel.baseUrl = "http://127.0.0.1:19423";
  (f.context.modelRegistry as any).awaitInitialBackgroundRefresh = async () => {};
  const p = prepared(f);
  assert.equal(p.metadata.childSelection.model.baseUrl, "https://child.invalid");
  const result = await dispatch(f, p, true);
  // A fully controlled SDK performs no POST; stream calls below are the provider-dispatch seam.
  assert.deepEqual({ status: result.status, sent: result.sent, childCreations: f.seen.options.length,
    prompts: f.seen.prompts, providerDispatches: f.seen.streams.length, saves: f.seen.saves.length },
  { status: "deferred", sent: false, childCreations: 0, prompts: 0, providerDispatches: 0, saves: 0 });
});
