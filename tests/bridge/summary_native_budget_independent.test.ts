/** Independent cross-language binding + controlled SDK behavior. No native accuracy/model/POST claim. */
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { createHash, randomUUID } from "node:crypto";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { canonicalJson } from "../../omp_bridge/contract/logs_v1.ts";
import { countSummaryFragments, dispatchSummarySDK } from "../../omp_bridge/g3/summary_sdk.ts";

const definition = "Independent existing task definition.";
const sha = (text: string) => createHash("sha256").update(text).digest("hex");
const count = (text: string) => Math.ceil(Buffer.byteLength(text) / 8);
function fixture(text = "한글\\\"\n".repeat(350)) {
  const prepared = JSON.parse(execFileSync("python3", ["-c", `import json,sys
from tests.backend.test_summary_native_budget_independent import native_case, controlled_count, DEFINITION
from workbench.backend.summary_adapter import prepare_summary
r,m,p=native_case((json.load(sys.stdin),))
print(json.dumps(prepare_summary(r,p,metadata=m,instructions=DEFINITION,count_fragments=controlled_count)))`],
    { input: JSON.stringify(text), encoding: "utf8", timeout: 10000,
      env: { ...process.env, PYTHONPATH: "src:.", PYTHONDONTWRITEBYTECODE: "1" } }));
  return { role: "manager", prepared, metadata: structuredClone(prepared.metadata), writer: {},
    context: { model: structuredClone(prepared.metadata.model), modelRegistry:{getAll:(mode:string)=>{assert.equal(mode,"all");return [prepared.metadata.childSelection.model];}},
      sessionManager: { getSessionId: () => prepared.metadata.parentSessionId } } };
}
const fragments = (input: any) => [definition + "\n\n" + readFileSync("omp_bridge/skills/important-log-summary/SKILL.md", "utf8"),
  canonicalJson({ request: input.prepared.request, selected: input.prepared.selected,
    omitted: input.prepared.omitted, priorCumulative: input.prepared.priorCumulative, resultByteLimit: 8192 })];
type Control = { fullCount?: (text: string) => number; beforeStream?: (child: any) => void;
  text?: (original: string) => string; followup?: (ctx: any) => any; fit?: (ctx: any, cap: number) => number };
function controlled(control: Control = {}) {
  const observed = { creates: 0, prompts: 0, streams: 0, writes: 0, disposals: 0, strict: [] as string[], trace: [] as any[] };
  class Registry {
    entries = new Map<string, any>();
    get(key: string) { return this.entries.get(key); }
    static global() { return { get: () => undefined }; }
  }
  const sdk: any = { AgentRegistry: Registry, Settings: { isolated: (x: any) => x },
    SessionManager: { inMemory: () => ({}) }, toolWireSchema: (tool: any) => tool.parameters,
    getBundledAgent: () => ({ source: "bundled", filePath: "embedded:task.md", systemPrompt: definition }),
    Tokenizer: class {
      encoding = "ClaudeV3";
      countTokens(text: string, mode: string) { assert.equal(mode, "strict"); observed.strict.push(text);
        return text.startsWith('{"systemPrompt":') && control.fullCount ? control.fullCount(text) : count(text); }
      checkTokenBudget(text: string) { return { tokens: this.countTokens(text, "strict"), exact: true }; }
    },
    fitOutputTokensToContextWindow: (_m: any, ctx: any, cap: number) => control.fit?.(ctx, cap) ?? cap,
    async createAgentSession(options: any) {
      observed.creates++;
      const child: any = { settings: options.settings, model: options.model, thinkingLevel: options.thinkingLevel,
        serviceTierByFamily: options.resolveServiceTierByFamily(), isDisposed: false,
        sessionManager: { getSessionId: () => "independent-native-child-" + observed.creates },
        getEnabledToolNames: () => ["db_summary_save"], subscribe: () => () => {}, abort: async () => {},
        agent: { streamFn: (_m: any, _c: any, opts: any) => { assert.equal(opts.maxTokens, 8192); observed.streams++; } },
        dispose: async () => { observed.disposals++; child.isDisposed = true; },
        async prompt(text: string) {
          observed.prompts++;
          control.beforeStream?.(child);
          const tool = options.customTools[0];
          const ctx = { systemPrompt: options.systemPrompt,
            messages: [{ role: "user", content: control.text?.(text) ?? text }],
            tools: [{ name: tool.name, description: tool.description, parameters: tool.parameters }] };
          await child.agent.streamFn(child.model, ctx, {});
          if (control.followup) await child.agent.streamFn(child.model, control.followup(ctx), {});
          await tool.execute("bound-result", { facts: ["controlled result"], interpretations: [], unknowns: [] });
        } };
      options.agentRegistry.entries.set(options.agentId, child);
      return { session: child };
    } };
  return { sdk, observed };
}
async function run(input: any, mock: ReturnType<typeof controlled>, mutate?: (event: any) => void) {
  return dispatchSummarySDK({ ...input, sdk: mock.sdk, current: () => true, observeMetadata: () => input.metadata,
    emit: event => { mock.observed.trace.push(event); mutate?.(event); },
    save: async () => { mock.observed.writes++; return { saved: true, entryId: randomUUID() }; } });
}

test("Python prepared ordered fragments and nonuniform proof agree with SDK without changing owner metadata", async () => {
  const input = fixture(); const mock = controlled();
  const before = structuredClone(input.prepared);
  const proof = countSummaryFragments(input.metadata, fragments(input), mock.sdk);
  assert.deepEqual(proof, input.prepared.countBinding);
  assert.deepEqual(proof.fragmentDigests, fragments(input).map(sha));
  assert.notEqual(proof.tokens[0], proof.tokens[1]);
  const outcome = await run(input, mock);
  assert.equal(outcome.status, "summary_result");
  assert.equal(mock.observed.writes, 1); assert.equal(mock.observed.streams, 1);
  assert.deepEqual(input.prepared, before);
  const pre = mock.observed.trace.find(e => e.event === "dispatch_preflight");
  assert.equal(pre.sharedInputEstimate, input.prepared.budget.inputEstimate);
  assert.equal(pre.estimator, "native_known_family_strict");
  assert.equal(pre.sharedQuotaIsFullProjectionBound, false);
});

test("strict known-family matrix uses adopted encodings; unsupported/null/inexact/disagreeing counters are explicit bytes", () => {
  const input = fixture(); const texts = ["", "한글\\\"\n", "ascii ascii"];
  const families = { "claude-v3": "ClaudeV3", "claude-v47": "ClaudeV47", "claude-v5": "ClaudeV5",
    "claude-v5-sonnet": "ClaudeV5Sonnet", qwen3: "Qwen3", "deepseek-v3": "DeepSeekV3", "kimi-k2": "KimiK2", glm5: "Glm5" };
  for (const [family, encoding] of Object.entries(families)) {
    input.metadata.childSelection.model.tokenizer = family;
    const calls: any[] = [];
    const sdk = { Tokenizer: class {
      encoding = encoding;
      countTokens(text: string, mode: string) { calls.push([text, mode]); return count(text); }
      checkTokenBudget(text: string) { return { tokens: count(text), exact: true }; }
    } };
    const actual = countSummaryFragments(input.metadata, texts, sdk);
    assert.equal(actual.encoding, encoding); assert.equal(actual.exact, true);
    assert.deepEqual(actual.tokens, texts.map(count));
    assert.deepEqual(calls, texts.map(t => [t, "strict"]));
  }
  for (const family of [null, "unknown", "o200k_base"]) {
    input.metadata.childSelection.model.tokenizer = family;
    const actual = countSummaryFragments(input.metadata, texts, { Tokenizer: class { constructor() { assert.fail("unknown family constructed tokenizer"); } } });
    assert.equal(actual.exact, false); assert.equal(actual.fallbackReason, "tokenizer_family_unavailable");
    assert.deepEqual(actual.tokens, texts.map(t => Buffer.byteLength(t)));
  }
  input.metadata.childSelection.model.tokenizer = "claude-v3";
  for (const [encoding, strict, checked, exact, reason] of [
    [null, 1, 1, true, "native_encoding_unavailable"], ["o200k_base", 1, 1, true, "native_encoding_unavailable"],
    ["ClaudeV3", 1, 1, false, "native_count_unavailable"], ["ClaudeV3", 1, 2, true, "native_count_unavailable"],
    ["ClaudeV3", -1, -1, true, "native_count_unavailable"], ["ClaudeV3", 1.5, 1.5, true, "native_count_unavailable"],
    ["ClaudeV3", Number.MAX_SAFE_INTEGER + 1, Number.MAX_SAFE_INTEGER + 1, true, "native_count_unavailable"]
  ]) {
    const actual = countSummaryFragments(input.metadata, texts.slice(1), { Tokenizer: class {
      encoding = encoding; countTokens() { return strict; } checkTokenBudget() { return { tokens: checked, exact }; }
    } });
    assert.equal(actual.exact, false); assert.equal(actual.estimator, "utf8_bytes_upper_bound");
    assert.equal(actual.fallbackReason, reason); assert.deepEqual(actual.tokens, texts.slice(1).map(t => Buffer.byteLength(t)));
  }
});

test("cross-language proof order owner encoding and counts cannot be substituted before child or prompt", async () => {
  const base = fixture();
  for (const change of [
    (p: any) => p.fragmentDigests.reverse(), (p: any) => p.tokens.reverse(),
    (p: any) => { p.ownerDigest = "0".repeat(64); }, (p: any) => { p.encoding = "o200k_base"; },
    (p: any) => { p.exact = false; }, (p: any) => { p.tokens = [1]; }
  ]) {
    const input = structuredClone({ ...base, context: undefined });
    input.context = { model: structuredClone(input.metadata.model), modelRegistry:{getAll:()=>[input.metadata.childSelection.model]}, sessionManager: { getSessionId: () => input.metadata.parentSessionId } };
    change(input.prepared.countBinding); const mock = controlled();
    assert.deepEqual(await run(input, mock), { status: "deferred", sent: false, protected: false, reason: "input_count_binding_mismatch" });
    assert.equal(mock.observed.creates, 0); assert.equal(mock.observed.prompts, 0); assert.equal(mock.observed.writes, 0);
  }
});

test("removed cost quota does not prevent native-counted child context fit", async () => {
  for (const window of [131072, 50000]) {
    const input = fixture("work");
    input.metadata.childSelection.model.contextWindow=window;
    input.prepared.metadata=structuredClone(input.metadata);
    const mock = controlled({fullCount:()=>10000});
    input.prepared.countBinding=countSummaryFragments(input.metadata,fragments(input),mock.sdk);
    const outcome=await run(input,mock);
    assert.equal(outcome.status,"summary_result");assert.equal(mock.observed.streams,1);
    assert.equal(mock.observed.writes,1);
    const pre=mock.observed.trace.find(e=>e.event==="dispatch_preflight");
    assert.equal(pre.inputLimit,window);assert.equal(pre.inputEstimate,10000);
  }
});

test("tokenizer mutation in live parent or bound proof blocks the next dispatch", async () => {
  for (const mutation of ["parent", "proof"]) {
    const input = fixture("work");
    const mock = controlled({ beforeStream: () => {
      if (mutation === "parent") input.context.model.tokenizer = "claude-v47";
      else input.prepared.countBinding.tokens[0]++;
    } });
    const outcome = await run(input, mock);
    assert.equal(outcome.sent, false); assert.equal(mock.observed.streams, 0); assert.equal(mock.observed.writes, 0);
    assert.equal(outcome.reason, mutation === "parent" ? "model_mismatch" : "request_mismatch");
  }
});

test("native token estimate stays independent from high UTF8 bytes and mandatory continuation wire allowance", async () => {
  const input = fixture("work"); const mock = controlled({ fullCount: () => 10, text: () => "한".repeat(6500) });
  const outcome = await run(input, mock);
  assert.equal(outcome.reason, "input_budget_unavailable"); assert.equal(outcome.sent, false);
  assert.equal(mock.observed.streams, 0); assert.equal(mock.observed.writes, 0);
  const pre = mock.observed.trace.filter(e => e.event === "dispatch_preflight").at(-1);
  assert.ok(pre.conservativeWireBytes <= 32768); assert.ok(pre.mandatoryWireBytes > 32768);
  assert.equal(pre.inputEstimate, 10); assert.equal(pre.mandatoryArgumentsBytes, 8192);
});

test("two full output reserves and fitted continuation cap remain despite tiny native estimates", async () => {
  for (const mode of ["two-reserves", "fitted"]) {
    const input = fixture("work");
    const mock = controlled({ fullCount: () => 10,
      fit: (ctx, cap) => mode === "fitted" && ctx.messages.length > 1 ? cap - 1 : cap });
    if (mode === "two-reserves") input.metadata.childSelection.model.contextWindow = 12000;
    input.prepared.metadata=structuredClone(input.metadata);
    input.prepared.countBinding=countSummaryFragments(input.metadata,fragments(input),mock.sdk);
    const outcome = await run(input, mock);
    assert.equal(outcome.reason, mode === "fitted" ? "output_budget_unavailable" : "input_budget_unavailable");
    assert.equal(mock.observed.prompts, 0); assert.equal(mock.observed.streams, 0); assert.equal(mock.observed.writes, 0);
    const pre = mock.observed.trace.find(e => e.event === "dispatch_preflight");
    assert.equal(pre.previousOutputReserve, 8192); assert.equal(pre.nextOutputReserve, 8192);
    if (mode === "two-reserves") assert.ok(pre.inputEstimate + 8192 < input.metadata.childSelection.model.contextWindow);
    else assert.equal(pre.nextFittedCap, 8191);
  }
});

test("later actual native projection count still prevents context overflow after initial dispatch", async () => {
  const input = fixture("work");
  const mock = controlled({ fullCount: text => text.includes("NATIVE_LATER_OVERSIZE") ? 131072 : 10,
    followup: ctx => ({ ...ctx, messages: [...ctx.messages, { role: "assistant", content: "NATIVE_LATER_OVERSIZE" }] }) });
  const outcome = await run(input, mock);
  assert.equal(outcome.sent, true); assert.equal(outcome.reason, "input_budget_unavailable");
  assert.equal(mock.observed.streams, 1); assert.equal(mock.observed.writes, 0);
  const pre = mock.observed.trace.filter(e => e.event === "dispatch_preflight").at(-1);
  assert.equal(pre.initialInvocation, false); assert.equal(pre.inputEstimate, 131072);
});
