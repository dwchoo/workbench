/** Controlled SDK fixture + actual fixed Python writer. Not installed-SDK evidence. */
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { createHash, randomUUID } from "node:crypto";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { test } from "node:test";
import { dispatchSummarySDK } from "../../omp_bridge/g3/summary_sdk.ts";

const definition = "Existing task definition bytes from a controlled fixture.";
const sha = (text: string) => createHash("sha256").update(text).digest("hex");
const python = execFileSync("python3", ["-c", "import sys; print(sys.executable)"], { encoding: "utf8" }).trim();
const moduleRoot = path.resolve("src");
function runPython(program: string, value: any): any {
  return JSON.parse(execFileSync(python, ["-c", program], {
    input: JSON.stringify(value), encoding: "utf8", timeout: 30000,
    env: { ...process.env, PYTHONPATH: moduleRoot, PYTHONDONTWRITEBYTECODE: "1" },
  }));
}
function privateFixture() {
  const root = mkdtempSync(path.join(tmpdir(), "dvl09-independent-"));
  const value = runPython(`import json,sys
from pathlib import Path
from tests.backend.test_summary_writer_independent import create_private_case
print(json.dumps(create_private_case(Path(json.load(sys.stdin)['root']))))`, { root });
  const metadata = value.metadata;
  return { root, case: value, input: {
    role: "manager", context: { model: metadata.model,
      sessionManager: { getSessionId: () => metadata.parentSessionId } },
    prepared: { request: value.request, metadata, selected: [{ range: value.request.sources[0], text: "work" }],
      omitted: [], priorCumulative: null, definitionDigest: sha(definition) },
    writer: { python, moduleRoot, dataRoot: root },
    current: () => true, observeMetadata: () => metadata,
  }, observe() {
    return runPython(`import json,sys
from pathlib import Path
from tests.backend.test_summary_writer_independent import observe_private_case
x=json.load(sys.stdin)
print(json.dumps(observe_private_case(Path(x['root']),x['request'])))`, { root, request: value.request });
  }, cleanup() { rmSync(root, { recursive: true, force: true }); } };
}

type Control = {
  firstRoster?: string[]; alwaysRoster?: string[]; wireSchemaMissing?: boolean;
  fittedCapDelta?: number; streamMutation?: (streamContext: any) => void;
  beforeStream?: () => void;
  run?: (tool: any, emit: (event: any) => void, child: any, text: string) => Promise<void>;
  abort?: (emit: (event: any) => void) => Promise<void>;
};
function controlledSDK(control: Control = {}) {
  const history: string[] = [];
  const createdOptions: any[] = [];
  const streamOptions: any[] = [];
  const submitted: any[] = [];
  class Registry {
    entries = new Map<string, any>();
    get(key: string) { return this.entries.get(key); }
    static global() { return { get: () => undefined }; }
  }
  const sdk: any = {
    AgentRegistry: Registry,
    Settings: { isolated: (settings: any) => structuredClone(settings) },
    SessionManager: { inMemory: () => ({}) }, Tokenizer: class {},
    fitOutputTokensToContextWindow: (_model: any, _context: any, cap: number) =>
      cap + (control.fittedCapDelta ?? 0),
    toolWireSchema: (tool: any) => tool.parameters,
    getBundledAgent: () => ({ name: "task", source: "bundled", filePath: "embedded:task.md", systemPrompt: definition }),
    async createAgentSession(options: any) {
      createdOptions.push(options);
      const index = createdOptions.length;
      const listeners = new Set<(event: any) => void>();
      const emit = (event: any) => { for (const listener of listeners) listener(event); };
      const tool = options.customTools[0];
      const child: any = {
        settings: options.settings, model: options.model, thinkingLevel: options.thinkingLevel,
        serviceTierByFamily: options.resolveServiceTierByFamily(), isDisposed: false,
        sessionManager: { getSessionId: () => "independent-child-" + index },
        getEnabledToolNames: () => control.alwaysRoster ??
          (index === 1 && control.firstRoster ? control.firstRoster : ["db_summary_save"]),
        agent: { streamFn: (_model: any, _context: any, stream: any) => {
          history.push("original_stream"); streamOptions.push(stream);
        } },
        subscribe(listener: (event: any) => void) { listeners.add(listener); return () => listeners.delete(listener); },
        async abort() { history.push("abort_start"); await control.abort?.(emit); history.push("abort_settled"); },
        async dispose() { child.isDisposed = true; history.push("dispose:" + index); },
        async prompt(text: string) {
          history.push("prompt:" + index);
          control.beforeStream?.();
          const streamContext = { systemPrompt: options.systemPrompt,
            messages: [{ role: "user", content: text }],
            tools: [{ name: tool.name, description: tool.description, parameters: tool.parameters }] };
          control.streamMutation?.(streamContext);
          await child.agent.streamFn(child.model, streamContext, { maxTokens: 1 });
          const submit = async (params: any) => {
            const response = await tool.execute("call-" + randomUUID(), params);
            submitted.push(JSON.parse(response.content[0].text));
            return response;
          };
          if (control.run) await control.run({ ...tool, execute: (_id: string, params: any) => submit(params) }, emit, child, text);
          else await submit({ facts: ["실제 private DB에 저장할 사실"], interpretations: [], unknowns: ["usage unknown"] });
          history.push("prompt_settled:" + index);
        },
      };
      options.agentRegistry.entries.set(options.agentId, child);
      return { session: child };
    },
  };
  if (control.wireSchemaMissing) delete sdk.toolWireSchema;
  return { sdk, history, createdOptions, streamOptions, submitted };
}

test("sole bound result crosses TS/Python contract and commits only after prompt + disposal", async () => {
  const fixture = privateFixture();
  const mock = controlledSDK();
  const events: any[] = [];
  try {
    const result = await dispatchSummarySDK({ ...fixture.input, sdk: mock.sdk,
      emit: event => { events.push(event); mock.history.push("event:" + event.event); } });
    assert.equal(result.status, "summary_result");
    assert.equal(result.protected, false);
    assert.equal(result.sent, true);
    assert.equal(mock.history.indexOf("event:saved") > mock.history.indexOf("dispose:1"), true);
    assert.equal(mock.history.indexOf("dispose:1") > mock.history.indexOf("prompt_settled:1"), true);
    assert.deepEqual(mock.submitted, [{ received: true, saved: false,
      requestId: fixture.case.request.requestId }]);
    assert.equal(mock.streamOptions[0].maxTokens, 8192);
    const observed = fixture.observe();
    assert.equal(observed.entries.length, 1);
    assert.equal(observed.record.state, "stored");
    assert.equal(observed.record.receipt.entry_id, result.writer?.entryId);
    assert.deepEqual(observed.entries[0].sourceGaps, fixture.case.request.sourceGaps);
    assert.deepEqual(result.result?.usage, { schemaVersion: 1, inputTokens: null, outputTokens: null, costUsd: null });
    assert.ok(Buffer.byteLength(JSON.stringify(result.receipt)) <= 4096);
    const options = mock.createdOptions[0];
    assert.deepEqual(options.toolNames, ["db_summary_save"]);
    assert.equal(options.restrictToolNames, true);
    assert.equal(options.allowRestrictedCustomTools, true);
    assert.equal(options.bindProcessState, false);
    assert.equal(options.cacheWarming, false);
    assert.equal(options.settings.externalThinking, false);
    assert.equal(options.settings["prewalk.enabled"], false);
    assert.equal(options.systemPrompt[0], definition);
    assert.ok(events.some(e => e.event === "startup_selfcheck" && e.admitted));
  } finally { fixture.cleanup(); }
});

test("received result then refused forbidden tool and duplicate both leave real catalog and receipt empty", async () => {
  for (const violation of ["refused", "duplicate", "assistant_tool_call"] as const) {
    const fixture = privateFixture();
    const mock = controlledSDK({ run: async (tool, emit) => {
      await tool.execute("first", { facts: ["tentative"], interpretations: [], unknowns: [] });
      if (violation === "duplicate") await tool.execute("second", { facts: ["duplicate"], interpretations: [], unknowns: [] });
      else if (violation === "refused") emit({ type: "tool_execution_end", toolName: "bash", isError: true });
      else emit({ type: "message_end", message: { role: "assistant",
        content: [{ type: "toolCall", name: "edit", arguments: {} }] } });
    } });
    try {
      const result = await dispatchSummarySDK({ ...fixture.input, sdk: mock.sdk });
      assert.equal(result.status, "rejected", violation);
      assert.equal(result.protected, true, violation);
      assert.deepEqual(fixture.observe().entries, []);
      assert.equal(fixture.observe().record.receipt, null);
      assert.ok(mock.history.includes("dispose:1"));
    } finally { fixture.cleanup(); }
  }
});

test("abort is settled before disposal and result rejection; ordinary parent event is not a violation", async () => {
  const fixture = privateFixture();
  let abortFinished = false;
  const mock = controlledSDK({
    run: async (tool, emit) => {
      // User-side text is not an SDK assistant tool event.
      emit({ type: "message_end", message: { role: "user", content: [{ type: "toolCall", name: "bash" }] } });
      await tool.execute("first", { facts: ["ok"], interpretations: [], unknowns: [] });
      emit({ type: "tool_execution_start", toolName: "bash" });
    },
    abort: async () => { await new Promise(resolve => setTimeout(resolve, 10)); abortFinished = true; },
  });
  try {
    const result = await dispatchSummarySDK({ ...fixture.input, sdk: mock.sdk });
    assert.equal(result.status, "rejected");
    assert.equal(abortFinished, true);
    assert.ok(mock.history.indexOf("abort_settled") < mock.history.indexOf("dispose:1"));
    assert.equal(fixture.observe().record.receipt, null);
  } finally { fixture.cleanup(); }
});

test("raw identity key and UTF8 envelope overflow cannot reach fixed writer", async () => {
  for (const params of [
    { facts: ["ok"], interpretations: [], unknowns: [], requestId: randomUUID() },
    { facts: ["한".repeat(2700)], interpretations: [], unknowns: [] },
  ]) {
    const fixture = privateFixture();
    const mock = controlledSDK({ run: async tool => { await tool.execute("first", params); } });
    try {
      const result = await dispatchSummarySDK({ ...fixture.input, sdk: mock.sdk });
      assert.equal(result.status, "rejected");
      assert.equal(result.protected, true);
      assert.equal(fixture.observe().entries.length, 0);
      assert.equal(fixture.observe().record.receipt, null);
    } finally { fixture.cleanup(); }
  }
});

test("startup roster failure rebuilds once; repeated failure never prompts or writes", async () => {
  for (const repeated of [false, true]) {
    const fixture = privateFixture();
    const mock = controlledSDK(repeated ? { alwaysRoster: [] } : { firstRoster: [] });
    try {
      const result = await dispatchSummarySDK({ ...fixture.input, sdk: mock.sdk });
      assert.equal(mock.createdOptions.length, 2);
      assert.equal(mock.history.filter(x => x.startsWith("prompt:")).length, repeated ? 0 : 1);
      assert.equal(mock.history.filter(x => x.startsWith("dispose:")).length, 2);
      assert.equal(result.status, repeated ? "deferred" : "summary_result");
      assert.equal(result.sent, !repeated);
      assert.equal(fixture.observe().entries.length, repeated ? 0 : 1);
    } finally { fixture.cleanup(); }
  }
});

test("unknown metadata and authority prevent creation; runtime epoch change prevents original stream", async () => {
  for (const situation of ["missing", "inactive", "epoch_change"] as const) {
    const fixture = privateFixture();
    let metadata = fixture.case.metadata;
    const mock = controlledSDK({ beforeStream: () => {
      if (situation === "epoch_change") metadata = { ...metadata, epoch: "different-parent-epoch" };
    } });
    try {
      const result = await dispatchSummarySDK({ ...fixture.input, sdk: mock.sdk,
        current: () => situation !== "inactive", observeMetadata: () => situation === "missing" ? undefined : metadata });
      assert.equal(result.sent, false);
      assert.equal(result.status, "deferred");
      assert.equal(mock.streamOptions.length, 0);
      assert.equal(mock.createdOptions.length, situation === "epoch_change" ? 1 : 0);
      assert.equal(fixture.observe().entries.length, 0);
    } finally { fixture.cleanup(); }
  }
});

test("provider cap shrink or unknown reasoning semantics is deferred before prompt", async () => {
  for (const problem of ["fit_shrink", "semantics"] as const) {
    const fixture = privateFixture();
    const mock = controlledSDK({ fittedCapDelta: problem === "fit_shrink" ? -1 : 0 });
    if (problem === "semantics") fixture.case.metadata.outputMeaning = "unknown";
    try {
      const result = await dispatchSummarySDK({ ...fixture.input, sdk: mock.sdk });
      assert.equal(result.status, "deferred");
      assert.equal(result.reason, "output_budget_unavailable");
      assert.equal(result.sent, false);
      assert.equal(mock.streamOptions.length, 0);
      assert.equal(mock.history.some(x => x.startsWith("prompt:")), false);
      assert.equal(fixture.observe().record.receipt, null);
    } finally { fixture.cleanup(); }
  }
});

test("ordinary user text mentioning tool names leaves normal result accepted", async () => {
  const fixture = privateFixture();
  const mock = controlledSDK({ run: async (tool, emit) => {
    emit({ type: "message_end", message: { role: "user", content: [{ type: "toolCall", name: "bash" }] } });
    emit({ type: "message_end", message: { role: "assistant", content: [{ type: "text", text: "raw text: bash/edit" }] } });
    await tool.execute("first", { facts: ["raw words are data"], interpretations: [], unknowns: [] });
  } });
  try {
    const result = await dispatchSummarySDK({ ...fixture.input, sdk: mock.sdk });
    assert.equal(result.status, "summary_result");
    assert.equal(result.protected, false);
    assert.equal(fixture.observe().entries.length, 1);
  } finally { fixture.cleanup(); }
});

test("startup missing SDK API returns a confirmed no-send refusal", async () => {
  const fixture = privateFixture();
  const mock = controlledSDK({ wireSchemaMissing: true });
  try {
    const result = await dispatchSummarySDK({ ...fixture.input, sdk: mock.sdk });
    assert.equal(result.status, "deferred");
    assert.equal(result.sent, false);
    assert.equal(mock.createdOptions.length, 0);
    assert.equal(fixture.observe().record.receipt, null);
  } finally {
    assert.equal(mock.createdOptions.length, 0);
    assert.equal(fixture.observe().entries.length, 0);
    assert.equal(fixture.observe().record.receipt, null);
    fixture.cleanup();
  }
});

test("confirmed nonDB execution is distinguished from a refused attempt", async () => {
  const fixture = privateFixture();
  const events: any[] = [];
  const mock = controlledSDK({ run: async (tool, emit) => {
    await tool.execute("first", { facts: ["tentative"], interpretations: [], unknowns: [] });
    emit({ type: "tool_execution_end", toolName: "bash", isError: false,
      result: { content: [{ type: "text", text: "executed successfully" }] } });
  } });
  try {
    const result = await dispatchSummarySDK({ ...fixture.input, sdk: mock.sdk, emit: e => events.push(e) });
    assert.equal(result.status, "rejected");
    assert.equal(result.protected, true);
    assert.equal(fixture.observe().entries.length, 0);
    assert.equal(fixture.observe().record.receipt, null);
    assert.equal(result.reason, "compatibility_failed");
    assert.ok(events.some(e => e.event === "compatibility_failed"));
  } finally { fixture.cleanup(); }
});

// SPEC8.1/8.2: the expected request/model epoch must not alias a live observer DTO.
test("same observer object in-place epoch thinking tier and nested model changes send nothing", async () => {
  for (const field of ["epoch", "thinking", "tier", "nested_model"] as const) {
    const fixture = privateFixture();
    const observedMetadata = fixture.case.metadata;
    // These independent consumers have matching values, not the same mutable DTO.
    fixture.input.prepared.metadata = structuredClone(observedMetadata);
    fixture.input.context.model = structuredClone(observedMetadata.model);
    const mock = controlledSDK({ beforeStream: () => {
      if (field === "epoch") observedMetadata.epoch += "-changed";
      if (field === "thinking") observedMetadata.thinkingLevel = "high";
      if (field === "tier") observedMetadata.serviceTierByFamily.openai = "priority";
      if (field === "nested_model") observedMetadata.model.contextWindow += 1024;
    } });
    try {
      const result = await dispatchSummarySDK({ ...fixture.input, sdk: mock.sdk,
        observeMetadata: () => observedMetadata });
      assert.equal(result.status, "deferred", field);
      assert.equal(result.sent, false, field);
      assert.equal(mock.streamOptions.length, 0, field);
      assert.equal(mock.history.includes("original_stream"), false, field);
      assert.equal(fixture.observe().entries.length, 0, field);
      assert.equal(fixture.observe().record.receipt, null, field);
      assert.ok(mock.history.includes("dispose:1"), field);
    } finally { fixture.cleanup(); }
  }
});

test("prepared metadata mutation is a request binding mismatch with no transport or durable result", async () => {
  const fixture = privateFixture();
  const observedMetadata = structuredClone(fixture.case.metadata);
  const mock = controlledSDK({ beforeStream: () => {
    fixture.input.prepared.metadata.epoch += "-prepared-changed";
  } });
  try {
    const result = await dispatchSummarySDK({ ...fixture.input, sdk: mock.sdk,
      observeMetadata: () => observedMetadata });
    assert.equal(result.status, "deferred");
    assert.equal(result.reason, "request_mismatch");
    assert.equal(result.sent, false);
    assert.equal(mock.streamOptions.length, 0);
    assert.equal(fixture.observe().entries.length, 0);
    assert.equal(fixture.observe().record.receipt, null);
  } finally { fixture.cleanup(); }
});

test("SDK tier getter returns independent writable maps while normal result remains stored", async () => {
  const fixture = privateFixture();
  const expectedTier = structuredClone(fixture.case.metadata.serviceTierByFamily);
  const mock = controlledSDK();
  const create = mock.sdk.createAgentSession;
  let getterVerified = false;
  mock.sdk.createAgentSession = async (options: any) => {
    const first = options.resolveServiceTierByFamily();
    first.openai = "priority";
    const second = options.resolveServiceTierByFamily();
    assert.notEqual(first, second);
    assert.deepEqual(second, expectedTier);
    assert.deepEqual(fixture.case.metadata.serviceTierByFamily, expectedTier);
    getterVerified = true;
    return create(options);
  };
  try {
    const result = await dispatchSummarySDK({ ...fixture.input, sdk: mock.sdk });
    assert.equal(getterVerified, true);
    assert.equal(result.status, "summary_result");
    assert.equal(result.sent, true);
    assert.equal(mock.streamOptions.length, 1);
    assert.equal(fixture.observe().entries.length, 1);
    assert.equal(fixture.observe().record.state, "stored");
  } finally { fixture.cleanup(); }
});

// P5: public assistant provider errors are transient; a safety violation still
// wins, while an already received valid result survives a resolving prompt.
test("resolving prompt with public assistant error and no result stays sent unknown without storage", async () => {
  const fixture = privateFixture();
  const events: any[] = [];
  const mock = controlledSDK({ run: async (_tool, emit) => {
    const message = { role: "assistant", content: [], stopReason: "error",
      errorMessage: "Owned fixture HTTP 400 provider failure" };
    emit({ type: "message_end", message });
    emit({ type: "agent_end", messages: [message] });
  } });
  try {
    const result = await dispatchSummarySDK({ ...fixture.input, sdk: mock.sdk,
      emit: event => events.push(event) });
    assert.equal(result.status, "unknown");
    assert.equal(result.reason, "summary_outcome_unknown");
    assert.equal(result.sent, true);
    assert.equal(result.protected, false);
    assert.equal(result.writer, undefined);
    assert.ok(mock.history.includes("prompt_settled:1"));
    assert.ok(mock.history.includes("dispose:1"));
    assert.equal(events.filter(event => event.event === "saved").length, 0);
    assert.deepEqual(mock.submitted, []);
    const observed = fixture.observe();
    assert.deepEqual(observed.entries, []);
    assert.equal(observed.record.receipt, null);
    // This dispatch fixture starts in sending; scheduler journal classification
    // is a separate caller boundary, not performed by dispatchSummarySDK.
    assert.equal(observed.record.state, "sending");
  } finally { fixture.cleanup(); }
});

test("resolving prompt without provider error or DB result remains a missing-result rejection", async () => {
  const fixture = privateFixture();
  const mock = controlledSDK({ run: async (_tool, emit) => {
    emit({ type: "message_end", message: { role: "assistant",
      content: [{ type: "text", text: "No summary was submitted." }], stopReason: "stop" } });
    emit({ type: "agent_end", messages: [] });
  } });
  try {
    const result = await dispatchSummarySDK({ ...fixture.input, sdk: mock.sdk });
    assert.equal(result.status, "rejected");
    assert.equal(result.reason, "result_unavailable");
    assert.equal(result.sent, true);
    assert.equal(result.protected, false);
    assert.equal(result.writer, undefined);
    assert.deepEqual(fixture.observe().entries, []);
    assert.equal(fixture.observe().record.receipt, null);
    assert.ok(mock.history.includes("dispose:1"));
  } finally { fixture.cleanup(); }
});

test("successful forbidden-tool event retains compatibility protection despite a provider error", async () => {
  const fixture = privateFixture();
  const events: any[] = [];
  const mock = controlledSDK({ run: async (_tool, emit) => {
    emit({ type: "message_end", message: { role: "assistant", content: [],
      stopReason: "error", errorMessage: "Owned fixture provider failure" } });
    // Observed successful tool event only: no bash command is actually executed.
    emit({ type: "tool_execution_end", toolName: "bash", isError: false,
      result: { content: [{ type: "text", text: "controlled successful tool event" }] } });
    emit({ type: "agent_end", messages: [] });
  } });
  try {
    const result = await dispatchSummarySDK({ ...fixture.input, sdk: mock.sdk,
      emit: event => events.push(event) });
    assert.equal(result.status, "rejected");
    assert.equal(result.reason, "compatibility_failed");
    assert.equal(result.sent, true);
    assert.equal(result.protected, true);
    assert.equal(result.writer, undefined);
    assert.ok(events.some(event => event.event === "compatibility_failed"));
    assert.equal(events.filter(event => event.event === "saved").length, 0);
    assert.deepEqual(fixture.observe().entries, []);
    assert.equal(fixture.observe().record.receipt, null);
    assert.ok(mock.history.includes("dispose:1"));
  } finally { fixture.cleanup(); }
});

test("valid sole DB result survives a later provider error when the prompt resolves", async () => {
  const fixture = privateFixture();
  const events: any[] = [];
  const mock = controlledSDK({ run: async (tool, emit) => {
    await tool.execute("result-before-error", { facts: ["valid received result"],
      interpretations: [], unknowns: ["provider followup unavailable"] });
    const message = { role: "assistant", content: [], stopReason: "error",
      errorMessage: "Owned fixture followup provider failure" };
    emit({ type: "message_end", message });
    emit({ type: "agent_end", messages: [message] });
  } });
  try {
    const result = await dispatchSummarySDK({ ...fixture.input, sdk: mock.sdk,
      emit: event => events.push(event) });
    assert.equal(result.status, "summary_result");
    assert.equal(result.sent, true);
    assert.equal(result.protected, false);
    assert.equal(result.writer?.saved, true);
    assert.equal(mock.submitted.length, 1);
    assert.equal(events.filter(event => event.event === "saved").length, 1);
    const observed = fixture.observe();
    assert.equal(observed.entries.length, 1);
    assert.equal(observed.record.state, "stored");
    assert.equal(observed.record.receipt.entry_id, result.writer?.entryId);
    assert.equal(result.receipt?.entryId, observed.record.receipt.entry_id);
    assert.ok(mock.history.indexOf("dispose:1") > mock.history.indexOf("prompt_settled:1"));
  } finally { fixture.cleanup(); }
});
