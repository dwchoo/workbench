// Pure getter contract fixtures. No operating provider or output-cap pass claim.
import assert from "node:assert/strict";
import { test } from "node:test";
import { modelMetadataObserver, observeModelMetadata } from "../../omp_bridge/g3/model_metadata.ts";

function fixture() {
  const model = { id: "resolved", provider: "fixture", api: "openai-responses",
    baseUrl: "https://example.invalid/v1", contextWindow: 131072, maxTokens: 16384, tokenizer: "claude-v3" };
  const tiers = { openai: "priority" };
  const parent: any = { model, thinkingLevel: "high", serviceTierByFamily: tiers,
    sessionManager: { getSessionId: () => "real-parent" },
    getContextUsage() { assert.fail("unestablished stats getter I/O"); },
    prompt() { assert.fail("observer prompted"); }, settings: new Proxy({}, { get() { assert.fail("settings read"); } }) };
  const context = { model, sessionManager: parent.sessionManager };
  const sdk = { AgentRegistry: { global: () => ({ list: () => [{ session: parent }] }) },
    createAgentSession() { assert.fail("observer created session"); } };
  return { parent, context, sdk, tiers };
}

test("public parent getters produce detached sanitized observation with honest output unknowns", () => {
  const f = fixture(); const before = structuredClone(f.parent.model);
  const result = observeModelMetadata(f.context, f.sdk);
  assert.equal(result.source, "parent_session_readback");
  assert.equal(result.parentSessionId, "real-parent");
  assert.equal(result.thinkingLevel, "high");
  assert.equal(result.status, "inconclusive");
  assert.equal(result.outputLimitEnforced, null); assert.equal(result.reasoningTokens, null);
  assert.deepEqual(f.parent.model, before);
  f.tiers.openai = "flex";
  assert.deepEqual(result.serviceTierByFamily, { openai: "priority" });
  const second = observeModelMetadata(f.context, f.sdk);
  assert.notEqual(second.epoch, result.epoch);
});

test("missing registry, ambiguous session, mismatched model and credential-bearing URL never fabricate metadata", () => {
  const f = fixture();
  assert.equal(observeModelMetadata(f.context, undefined), undefined);
  assert.equal(observeModelMetadata(f.context, { AgentRegistry: { global: () => ({ list: () => [{ session: f.parent }, { session: f.parent }] }) } }), undefined);
  assert.equal(observeModelMetadata({ ...f.context, model: { ...f.context.model, id: "other" } }, f.sdk), undefined);
  for (const url of ["https://user:secret@example.invalid/v1", "https://example.invalid/v1?key=secret"]) {
    f.parent.model.baseUrl = url;
    assert.equal(observeModelMetadata(f.context, f.sdk), undefined);
  }
});

test("observer reads current parent selection and does not mutate tier or model", () => {
  const f = fixture();
  const observe = modelMetadataObserver(() => f.context, f.sdk);
  const first = observe(); f.parent.thinkingLevel = "off";
  const next = observe(); assert.equal(next.thinkingLevel, "off"); assert.notEqual(next.epoch, first.epoch);
  assert.equal(first.thinkingLevel, "high"); assert.equal(f.parent.serviceTierByFamily.openai, "priority");
});
