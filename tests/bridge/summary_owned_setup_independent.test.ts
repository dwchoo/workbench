/** Actual owned suppliers + actual public observer. SDK/journal ports controlled;
 * VM import/module-URL substitution is not installed OMP or provider evidence. */
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { existsSync, mkdtempSync, mkdirSync, readFileSync, readdirSync, realpathSync,
  renameSync, rmSync, statSync, writeFileSync } from "node:fs";
import { stripTypeScriptTypes } from "node:module";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { test } from "node:test";
import { fileURLToPath, pathToFileURL } from "node:url";
import vm from "node:vm";
import { canonicalJson, summaryInputKey } from "../../omp_bridge/contract/logs_v1.ts";
import { observeModelMetadata } from "../../omp_bridge/g3/model_metadata.ts";
import { buildSummaryPrompt } from "../../omp_bridge/g3/summary_sdk.ts";

const sha = (s: string) => createHash("sha256").update(s).digest("hex");
const supplierNames = ["summary_verification.ts", "summary_priority_verification.ts"];
const supplierSources = Object.fromEntries(supplierNames.map(name => [name,
  readFileSync(new URL("../../omp_bridge/g3/" + name, import.meta.url), "utf8")]));
const skill = readFileSync(new URL("../../omp_bridge/skills/important-log-summary/SKILL.md", import.meta.url), "utf8");
const plain = (value: any) => JSON.parse(JSON.stringify(value));
const turn = () => new Promise<void>(resolve => setImmediate(resolve));

function fixture(name: string, role = "manager", missingSelection = false) {
  const root = mkdtempSync(join(process.env.WB_DVL_OWNED_TEST_OUTPUT ?? tmpdir(), "owned-node-"));
  for (const path of ["home", "project", "runtime/omp_bridge/g3"])
    mkdirSync(join(root, path), { recursive: true, mode: 0o700 });
  const endpoint = "http://127.0.0.1:19423/v1", parentModel = {
    provider: "ownedverify", id: "scripted", api: "openai-completions", baseUrl: endpoint,
    contextWindow: 131072, maxTokens: 8192, tokenizer: "claude-v3" };
  const childModel = { ...parentModel, id: "worker" };
  const selection = { source: "configured_worker_default", selector: "ownedverify/worker:high",
    modelTableDigest: "a".repeat(64) };
  const calls = { summaryObserver: 0, definition: 0, selection: 0, creates: 0, dispatch: 0,
    journal: 0, forbidden: 0 };
  const forbidden = () => { calls.forbidden++; throw Error("Native/model/network forbidden"); };
  const registry = { getAll(scope: string) { assert.equal(scope, "all"); return [parentModel, childModel]; },
    awaitInitialBackgroundRefresh: async () => {} };
  const sessionManager = { getSessionId: () => "owned-parent", getEntries: () => [] };
  const parent = { model: parentModel, modelRegistry: registry, sessionManager,
    thinkingLevel: "off", serviceTierByFamily: {} };
  const globalRegistry = { list: () => [{ session: parent }], get: () => undefined };
  const AgentRegistry = { global: () => globalRegistry };
  const ctx = { model: parentModel, modelRegistry: registry, sessionManager,
    setInterval: () => ({ controlledTimer: true }), clearTimer: () => {}, shutdown: () => {},
    getContextUsage: forbidden };
  const metadata = observeModelMetadata(ctx, { AgentRegistry }, selection);
  assert.ok(metadata, "actual observer positive control");
  const beforeParent = canonicalJson(parentModel);
  const env: any = { HOME: join(root, "home"), WORKBENCH_SUMMARY_VERIFY_ROOT: root,
    WORKBENCH_SUMMARY_VERIFY_PYTHON: "/fixture/no-executable", WORKBENCH_G3_ROLE: role, PATH: "/fixture" };
  if (!missingSelection && role === "manager") env.WORKBENCH_G3_SUMMARY_SELECTION = JSON.stringify(selection);
  const guardedEnv = new Proxy(env, { get(target, key: any) {
    if (key === "WORKBENCH_G3_SUMMARY_SELECTION") {
      calls.selection++;
      if (role === "worker") throw Error("Worker must not read summary selection");
    }
    return target[key];
  } });
  writeFileSync(join(root, "fixture.json"), JSON.stringify({ root, endpoint, dbHealth: {} }), { mode: 0o600 });
  const definition = "Controlled bundled task, unchanged owned caps.";
  if (name === "summary_priority_verification.ts" && role === "manager") {
    const subject: any = { schemaVersion: 1, kind: "command", id: "11111111-1111-4111-8111-111111111111" };
    const selected = Array.from({ length: 3 }, (_unused, index) => {
      const text = ("private-work-" + index + "-").padEnd(4096, "x");
      return { text, range: { schemaVersion: 1, source: { schemaVersion: 1, sourceKind: "terminal",
        collectorEpoch: 1, commandId: subject.id }, start: index * 4096, end: (index + 1) * 4096, digest: sha(text) } };
    });
    const sources = selected.map(item => item.range), skillDigest = sha(skill);
    writeFileSync(join(root, "prepared.json"), JSON.stringify({ metadata,
      definitionDigest: sha(definition), selected, omitted: [], priorCumulative: null,
      request: { schemaVersion: 1, requestId: "22222222-2222-4222-8222-222222222222", subject, revision: null,
        authorizationRef: "33333333-3333-4333-8333-333333333333", authorizationEpoch: 1,
        inputKey: summaryInputKey(subject, null, sources as any, [], skillDigest), skillName: "important-log-summary",
        skillDigest, sources, sourceGaps: [], priorCumulativeEntryId: null, dbHealth: {}, requestedAt: "2026-10-15T00:00:00Z" }
    }), { mode: 0o600 });
  }
  const completions: any[] = [], dispatches: any[] = [], hooks: any = {};
  // Whole supplier body and guards run. Only imports/export/module URL are adapted
  // like existing independent VM tests; no guard or budget expression is changed.
  const transformed = supplierSources[name].replace(/^import .*;\n/gm, "")
    .replace("export default function", "function").replace("export function", "function")
    .replaceAll("import.meta.url", "moduleURL");
  assert.ok(!transformed.includes("import "), "only dependency declarations removed");
  const virtualURL = pathToFileURL(join(root, "runtime/omp_bridge/g3/" + name)).href;
  const entry = name === "summary_verification.ts" ? "verification" : "observer";
  const actual = vm.runInNewContext(stripTypeScriptTypes(transformed + "\n" + entry + ";"), {
    // A native module shares a JS realm. Normalize VM JSON values at imported
    // contract calls so plain-object checks see that same boundary, not VM prototypes.
    createHash, canonicalJson: (value: any) => canonicalJson(plain(value)),
    summaryInputKey: (subject: any, revision: any, sources: any, gaps: any, digest: string) =>
      summaryInputKey(plain(subject), revision, plain(sources), plain(gaps), digest),
    buildSummaryPrompt, Buffer, URL, fileURLToPath,
    structuredClone, AbortController, statSync, realpathSync, existsSync, readdirSync,
    renameSync, writeFileSync, moduleURL: virtualURL,
    readFileSync(path: any, ...args: any[]) {
      if (String(path) === join(root, "runtime/omp_bridge/g3/" + name)) return supplierSources[name];
      if (String(path) === join(root, "runtime/omp_bridge/skills/important-log-summary/SKILL.md")) return skill;
      return (readFileSync as any)(path, ...args);
    },
    process: { env: guardedEnv, cwd: () => join(root, "project"), getuid: () => process.getuid!() },
    setTimeout: () => ({ controlledTimeout: true }), clearTimeout: () => {},
    AgentRegistry, createAgentSession: forbidden, SessionManager: { inMemory: forbidden },
    Settings: { isolated: forbidden }, Tokenizer: forbidden, fitOutputTokensToContextWindow: forbidden,
    toolWireSchema: forbidden, VERSION: "controlled-no-Native",
    getBundledAgent(agent: string) {
      calls.definition++; assert.equal(role, "manager"); assert.equal(agent, "task");
      return { source: "bundled", filePath: "embedded:task.md", systemPrompt: definition };
    },
    observeModelMetadata(context: any, sdk: any, picked: any) {
      calls.summaryObserver++; assert.equal(role, "manager");
      return observeModelMetadata(context, sdk, picked);
    },
    execFile(_python: any, argv: any[], _options: any, callback: any) {
      assert.equal(role, "manager"); assert.ok(argv.includes("--reserve"));
      return { stdin: { end(input: string) { JSON.parse(input); calls.journal++; queueMicrotask(() => callback(null)); } } };
    },
    async dispatchVerificationSummarySDK(options: any) {
      calls.dispatch++;
      const live = options.observeMetadata();
      assert.deepEqual(plain(options.prepared.metadata), plain(metadata));
      assert.deepEqual(plain(live), plain(metadata));
      dispatches.push({ metadata: plain(options.prepared.metadata), selected: plain(options.prepared.selected) });
      return { status: "deferred", sent: false, protected: false, reason: "controlled-SDK-boundary" };
    },
    publishVerificationCompletion(_root: string, value: any, kind: string) {
      assert.equal(_root, root); completions.push({ value: plain(value), kind });
    }
  });
  actual({ on(event: string, handler: any) { assert.equal(hooks[event], undefined); hooks[event] = handler; } });
  return { root, ctx, calls, hooks, metadata, dispatches, completions, parentModel, beforeParent,
    async start() {
      await hooks.session_start({}, ctx);
      for (let index = 0; index < 8; index++) {
        await turn();
        if (completions.length || existsSync(join(root, "ownerready.json"))) break;
      }
    },
    cleanup() {
      assert.equal(canonicalJson(parentModel), beforeParent);
      rmSync(root, { recursive: true }); assert.equal(existsSync(root), false);
    } };
}

test("owned two suppliers carry actual host-default/null/unknown observer DTO without overrides", async () => {
  for (const name of supplierNames) {
    const f = fixture(name);
    try {
      await f.start();
      assert.equal(f.calls.dispatch, 1, name); assert.equal(f.dispatches.length, 1);
      assert.equal(f.calls.creates, 0); assert.equal(f.calls.forbidden, 0);
      const dto = f.dispatches[0].metadata;
      assert.equal(dto.model.id, "scripted"); assert.equal(dto.childSelection.model.id, "worker");
      assert.equal(dto.childSelection.selector, "ownedverify/worker:high");
      assert.equal(dto.childSelection.thinkingLevel, "high"); assert.equal(dto.thinkingLevel, "off");
      assert.equal(dto.outputLimitSource, "host_default"); assert.equal(dto.outputCap, 8192);
      assert.equal(dto.childSelection.model.contextWindow, 131072);
      for (const key of ["existingContextTokens", "outputLimitEnforced", "reasoningTokens", "visibleResultTokens"])
        assert.equal(dto[key], null);
      assert.equal(dto.outputMeaning, "unknown"); assert.equal(dto.status, "inconclusive");
      assert.deepEqual(dto.unknowns, ["current_context_unavailable", "output_ceiling_enforcement_unavailable",
        "reasoning_budget_semantics_unavailable"]);
      assert.ok(f.calls.summaryObserver >= 2); assert.equal(f.calls.definition, 1);
      if (name === "summary_verification.ts") {
        assert.equal(f.completions.length, 1); assert.equal(f.completions[0].kind, "ok");
        assert.deepEqual(f.completions[0].value.operatingMetadata, plain(f.metadata));
        assert.equal(f.completions[0].value.operatingMetadataPass, false);
        assert.deepEqual(f.completions[0].value.exercised, []);
      } else {
        const ready = JSON.parse(readFileSync(join(f.root, "ownerready.json"), "utf8"));
        assert.deepEqual(ready.metadata, plain(f.metadata));
        assert.equal(f.dispatches[0].selected.length, 3);
        assert.deepEqual(f.dispatches[0].selected.map((s: any) => Buffer.byteLength(s.text)), [4096, 4096, 4096]);
      }
    } finally { f.cleanup(); }
  }
});

test("owned Worker readiness publishes public parent only with no summary selection or task definition access", async () => {
  const f = fixture("summary_priority_verification.ts", "worker");
  try {
    await f.start();
    const ready = JSON.parse(readFileSync(join(f.root, "ownerready.json"), "utf8"));
    assert.equal(ready.parent.parentSessionId, "owned-parent");
    assert.equal(ready.parent.model.id, "scripted");
    assert.equal(ready.parent.model.contextWindow, 131072);
    assert.equal(ready.nativeRegistrySame, true);
    for (const key of ["metadata", "definition", "definitionSha256", "definitionBytes"])
      assert.equal(Object.hasOwn(ready, key), false);
    assert.equal(f.calls.selection, 0); assert.equal(f.calls.summaryObserver, 0); assert.equal(f.calls.definition, 0);
    assert.equal(f.calls.dispatch, 0); assert.equal(f.calls.journal, 0); assert.equal(f.calls.forbidden, 0);
  } finally { f.cleanup(); }
});

test("owned Manager missing selection refuses both suppliers before SDK dispatch", async () => {
  for (const name of supplierNames) {
    const f = fixture(name, "manager", true);
    try {
      await f.start();
      assert.equal(f.calls.dispatch, 0);
      assert.equal(f.calls.journal, 0); assert.equal(f.calls.forbidden, 0);
      if (name === "summary_verification.ts") {
        assert.equal(f.completions.length, 1);
        assert.match(f.completions[0].value.error, /public_metadata_unavailable/);
        assert.equal(f.completions[0].kind, "err");
      } else {
        const ready = JSON.parse(readFileSync(join(f.root, "ownerready.json"), "utf8"));
        assert.match(ready.error, /native_definition_metadata_unavailable/);
      }
    } finally { f.cleanup(); }
  }
});
