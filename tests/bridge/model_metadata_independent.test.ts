// Getter-double observation only; no actual OMP or operating model pass.
import assert from "node:assert/strict";
import { test } from "node:test";
import { observeModelMetadata, modelMetadataObserver } from "../../omp_bridge/g3/model_metadata.ts";

function fixture() {
  const calls: string[] = [];
  const model = { id: "chosen", provider: "owned", api: "openai-responses",
    baseUrl: "https://example.invalid/v1", contextWindow: 100000, maxTokens: 8192,
    tokenizer: "claude-v3" };
  let thinking = "high";
  const tier = { openai: "priority" };
  const forbidden = () => { throw new Error("forbidden non-observer operation"); };
  const registry = { getAll: (scope: string) => { assert.equal(scope, "all"); return [model]; } };
  const selection = { source: "configured_worker_default", selector: "owned/chosen:low",
    modelTableDigest: "a".repeat(64) };
  const parent: any = {
    get model() { calls.push("model"); return model; },
    get thinkingLevel() { calls.push("thinkingLevel"); return thinking; },
    get serviceTierByFamily() { calls.push("serviceTierByFamily"); return tier; },
    sessionManager: { getSessionId: () => "parent-id" },
    getContextUsage: forbidden, prompt: forbidden,
    get settings() { return forbidden(); }, modelRegistry: registry,
    get authStorage() { return forbidden(); },
  };
  const context = { model, modelRegistry: registry, sessionManager: parent.sessionManager };
  const sdk = { AgentRegistry: { global: () => ({ list: () => [{ session: parent }] }) },
    createAgentSession: forbidden, ModelRegistry: forbidden, AuthStorage: forbidden };
  return { calls, model, tier, parent, context, sdk, selection, setThinking: (v: string) => { thinking = v; } };
}

test("only known pure getters are read and missing semantics remain unavailable", () => {
  const f = fixture(); const before = structuredClone({ model: f.model, tier: f.tier });
  const result = observeModelMetadata(f.context, f.sdk, f.selection);
  assert.equal(result.status, "inconclusive");
  assert.deepEqual(f.calls, ["model", "thinkingLevel", "serviceTierByFamily"]);
  assert.equal(result.outputCap, 8192);
  for (const key of ["existingContextTokens", "reasoningTokens",
    "visibleResultTokens", "outputLimitEnforced"]) assert.equal(result[key], null);
  assert.equal(result.outputMeaning, "unknown");
  assert.deepEqual({ model: f.model, tier: f.tier }, before);
  assert.deepEqual(result.unknowns, ["current_context_unavailable",
    "output_ceiling_enforcement_unavailable", "reasoning_budget_semantics_unavailable"]);
});

test("observer snapshots are detached and epoch follows actual getter changes", () => {
  const f = fixture(); const observe = modelMetadataObserver(() => f.context, f.sdk, f.selection);
  const first = observe(); f.setThinking("off"); f.tier.openai = "flex";
  f.model.maxTokens = 4096;
  const second = observe();
  assert.notEqual(first.epoch, second.epoch);
  assert.equal(first.thinkingLevel, "high");
  assert.equal(first.serviceTierByFamily.openai, "priority");
  assert.equal(first.model.maxTokens, 8192);
  second.model.id = "local-copy";
  assert.equal(f.model.id, "chosen");
});

test("missing or duplicate parent, changed context, and credential URLs are rejected", () => {
  const f = fixture();
  for (const entries of [[], [{ session: f.parent }, { session: f.parent }]]) {
    assert.equal(observeModelMetadata(f.context, { AgentRegistry: {
      global: () => ({ list: () => entries }) } }, f.selection), undefined);
  }
  assert.equal(observeModelMetadata({ ...f.context, model: { ...f.model, tokenizer: "qwen3" } }, f.sdk, f.selection), undefined);
  for (const url of ["https://user@example.invalid/v1", "https://example.invalid/v1#secret",
    "https://example.invalid/v1?access_token=secret"]) {
    f.model.baseUrl = url;
    assert.equal(observeModelMetadata(f.context, f.sdk, f.selection), undefined);
  }
});
