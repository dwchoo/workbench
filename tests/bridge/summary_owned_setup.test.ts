/** Actual supplier/Worker readiness snippets with controlled public getters; no Native loader proof. */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { stripTypeScriptTypes } from "node:module";
import vm from "node:vm";
import { test } from "node:test";
import { observeModelMetadata } from "../../omp_bridge/g3/model_metadata.ts";

const sources = ["summary_verification.ts", "summary_priority_verification.ts"].map(name =>
  readFileSync(new URL("../../omp_bridge/g3/" + name, import.meta.url), "utf8"));
const fields = ["provider", "id", "api", "baseUrl", "contextWindow", "maxTokens", "tokenizer"];

test("both actual owned metadata suppliers use parsed managed selection and preserve observer unknowns", () => {
  const parentModel = { provider: "ownedverify", id: "scripted", api: "openai-completions",
    baseUrl: "http://127.0.0.1:1234/v1", contextWindow: 131072, maxTokens: 8192, tokenizer: "claude-v3" };
  const childModel = { ...parentModel, id: "worker" };
  const registry = { getAll: () => [parentModel, childModel] };
  const sessionManager = { getSessionId: () => "owned-parent" };
  const parent = { model: parentModel, modelRegistry: registry, sessionManager, thinkingLevel: "off", serviceTierByFamily: {} };
  const ctx = { model: parentModel, modelRegistry: registry, sessionManager };
  const AgentRegistry = { global: () => ({ list: () => [{ session: parent }] }) };
  const selection = { source: "configured_worker_default", selector: "ownedverify/worker:high", modelTableDigest: "a".repeat(64) };
  const expected = observeModelMetadata(ctx, { AgentRegistry }, selection);
  for (const source of sources) {
    const selectionCode = source.match(/const selection = JSON\.parse\(process\.env\.WORKBENCH_G3_SUMMARY_SELECTION[^\n]+;/)?.[0];
    const supplier = source.match(/const (metadata|liveMetadata) = \(\) => observeModelMetadata\([^\n]+;/);
    assert.ok(selectionCode && supplier);
    const actual = vm.runInNewContext(selectionCode + "\n" + supplier[0] + "\n" + supplier[1] + "();", {
      process: { env: { WORKBENCH_G3_SUMMARY_SELECTION: JSON.stringify(selection) } }, ctx, AgentRegistry, observeModelMetadata });
    assert.deepEqual(actual, expected);
    assert.equal(actual.model.id, "scripted"); assert.equal(actual.childSelection.model.id, "worker");
    assert.equal(actual.childSelection.thinkingLevel, "high"); assert.equal(actual.outputLimitSource, "host_default");
    assert.equal(actual.outputLimitEnforced, null); assert.equal(actual.existingContextTokens, null);
    assert.equal(actual.reasoningTokens, null); assert.equal(actual.status, "inconclusive");
  }
});

test("actual priority Worker ready branch publishes parent SID/model without summary DTO or task definition", () => {
  const source = sources[1];
  const start = source.indexOf('if (process.env.WORKBENCH_G3_ROLE !== "manager") {');
  const end = source.indexOf("\n      const selection =", start);
  assert.ok(start > 0 && end > start);
  const parentModel = { id: "scripted", provider: "ownedverify", tokenizer: "claude-v3" };
  const observed: any[] = [];
  const code = "let record; function ready() {\n" + source.slice(start, end) + "\n}; ready(); record;";
  const record = vm.runInNewContext(stripTypeScriptTypes(code), { process: { env: { WORKBENCH_G3_ROLE: "worker" } },
    parent: { model: parentModel, sessionManager: { getSessionId: () => "owned-worker-parent" } }, fields,
    snapshot: () => ({ publicDiagnostic: true }),
    publish(kind: string) { observed.push(kind); },
    observeModelMetadata() { assert.fail("Worker summary DTO access"); },
    getBundledAgent() { assert.fail("Worker task definition access"); } });
  assert.deepEqual(observed, ["ready"]);
  assert.equal(record.parent.parentSessionId, "owned-worker-parent");
  assert.equal(record.parent.model.id, "scripted");
  assert.equal(Object.hasOwn(record, "metadata"), false); assert.equal(Object.hasOwn(record, "definition"), false);
  assert.equal(code.includes('definition'), false); assert.equal(code.includes('metadata'), false);
});
