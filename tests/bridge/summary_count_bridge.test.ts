// Existing G3 count-only frame, real Unix transport, read-only SDK spy.
// The observer is driver-injected; this is not DVL-14 operating metadata proof.
import assert from "node:assert/strict";
import { createHash, randomUUID } from "node:crypto";
import { mkdtemp, rm } from "node:fs/promises";
import net from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import { test } from "node:test";
import { canonicalJson } from "../../omp_bridge/contract/logs_v1.ts";
import workbenchG3Extension from "../../omp_bridge/g3/bridge.ts";

const sha = (text: string) => createHash("sha256").update(text).digest("hex");
const tokens = (text: string) => text.split(/\s+/u).filter(Boolean).length + [...text].filter(c => c === "한").length;
async function harness(role = "manager") {
  const directory = await mkdtemp(join(tmpdir(), "summary-count-"));
  const socketPath = join(directory, "bridge.sock");
  const frames: any[] = [], clients: net.Socket[] = [];
  const server = net.createServer(socket => {
    clients.push(socket); let pending = "";
    socket.on("data", chunk => {
      pending += chunk.toString("utf8");
      while (pending.includes("\n")) {
        const index = pending.indexOf("\n");
        frames.push(JSON.parse(pending.slice(0, index))); pending = pending.slice(index + 1);
      }
    });
  });
  await new Promise<void>(resolve => server.listen(socketPath, resolve));
  const values = { WORKBENCH_G3_BRIDGE_SOCKET: socketPath, WORKBENCH_G3_ROLE: role,
    WORKBENCH_G3_TOKEN: randomUUID(), WORKBENCH_G3_GENERATION: "7" };
  const prior = Object.fromEntries(Object.keys(values).map(k => [k, process.env[k]]));
  Object.assign(process.env, values);
  const handlers = new Map<string, Function>();
  let sessionId = "native-parent-actual", generation = 7;
  const model = { id: "fake-count-only", provider: "fixture", api: "openai-completions",
    baseUrl: "http://127.0.0.1:1/v1", contextWindow: 131072, maxTokens: 8192, tokenizer: "claude-v3" };
  let metadata: any = { source: "parent_session_readback", parentSessionId: sessionId, epoch: "fixture-epoch-1", model,
    childSelection: { source: "configured_worker_default", selector: "fixture/fake-count-only:low",
      modelTableDigest: "a".repeat(64), model, thinkingLevel: "low",
      descriptorSource: "manager_model_registry_getAll_all", serviceTierSource: "parent_session_readback" } };
  const context: any = { model, sessionManager: { getSessionId: () => sessionId },
    isIdle: () => true, hasPendingMessages: () => false, ui: { getEditorText: () => "" },
    workbenchSummaryMetadata: () => metadata, abort() {} };
  const counted: any[] = [];
  let onCount = () => {};
  const forbidden = () => assert.fail("count attempted parent/child/model/provision/settings/DB action");
  const sdk: any = { createAgentSession: forbidden, AgentRegistry: forbidden,
    SessionManager: { inMemory: forbidden }, Settings: { isolated: forbidden },
    getBundledAgent: forbidden, toolWireSchema: forbidden, fitOutputTokensToContextWindow: forbidden,
    Tokenizer: class {
      encoding = "ClaudeV3";
      constructor(actual: any) { assert.deepEqual(actual, metadata.model); }
      countTokens(text: string, mode: string) { counted.push([text, mode]); onCount(); return tokens(text); }
      checkTokenBudget(text: string, cap: number) { assert.equal(cap, 0); return { tokens: tokens(text), exact: true }; }
    } };
  workbenchG3Extension({ on: (name: string, handler: Function) => handlers.set(name, handler),
    registerTool() {}, appendEntry: forbidden, sendUserMessage: forbidden, logger: { error() {} } }, sdk);
  handlers.get("session_start")!(undefined, context);
  async function wait(predicate: (frame: any) => boolean) {
    const deadline = Date.now() + 2000;
    while (Date.now() < deadline) {
      const index = frames.findIndex(predicate);
      if (index >= 0) return frames.splice(index, 1)[0];
      await delay(5);
    }
    throw new Error("count bridge response timed out");
  }
  await wait(frame => frame.kind === "hello");
  const query = (fragments = ["existing definition 한글", "selected source\n\\\" ascii"]) => {
    const owner = { epoch: metadata?.epoch ?? "fixture-epoch-1", parentSessionId: sessionId, model: structuredClone(model) };
    return { version: "summary-host-input-v1", owner, ownerDigest: sha(canonicalJson(owner)),
      fragments, fragmentDigests: fragments.map(sha) };
  };
  async function request(frame: any) {
    const requestId = randomUUID();
    clients.at(-1)!.write(JSON.stringify({ kind: "summary_count", expectedPeer: { sessionId, generation },
      ...frame, requestId }) + "\n");
    return wait(f => f.kind === "api_ack" && f.requestId === requestId);
  }
  return { context, sdk, counted, query, request, setMetadata(value: any) { metadata = value; },
    get metadata() { return metadata; }, onCount(callback: () => void) { onCount = callback; },
    async switchSession() {
      sessionId = "native-parent-next"; generation++;
      metadata.parentSessionId = sessionId;
      handlers.get("session_switch")!(undefined, context);
      await wait(frame => frame.kind === "hello");
    },
    async close() {
      handlers.get("session_shutdown")!();
      clients.forEach(client => client.destroy());
      await new Promise<void>(resolve => server.close(() => resolve()));
      for (const name of Object.keys(values)) {
        if (prior[name] === undefined) delete process.env[name]; else process.env[name] = prior[name];
      }
      await rm(directory, { recursive: true, force: true });
    } };
}

test("count-only frame returns canonical strict proof without parent or child activity", async () => {
  const h = await harness();
  try {
    const query = h.query(); const before = structuredClone(h.metadata);
    const reply = await h.request({ query });
    assert.equal(reply.status, "summary_count"); assert.equal(reply.sent, false);
    assert.deepEqual(reply.proof, { version: query.version, ownerDigest: query.ownerDigest,
      fragmentDigests: query.fragmentDigests, tokens: query.fragments.map(tokens),
      encoding: "ClaudeV3", exact: true, estimator: "native_known_family_strict" });
    assert.deepEqual(h.counted, query.fragments.map(f => [f, "strict"]));
    assert.deepEqual(h.metadata, before);
  } finally { await h.close(); }
});

test("missing observer and unsupported actual family explicitly fall back without tokenizer use", async () => {
  const h = await harness();
  try {
    const query = h.query(); h.setMetadata(undefined);
    const missing = await h.request({ query });
    assert.equal(missing.status, "count_unavailable"); assert.equal(missing.reason, "model_metadata_unavailable");
    const stale = structuredClone(query); stale.owner.parentSessionId = "old-parent";
    stale.ownerDigest = sha(canonicalJson(stale.owner));
    assert.equal((await h.request({ query: stale })).reason, "input_count_owner_mismatch");
    h.setMetadata({ source: "parent_session_readback", epoch: query.owner.epoch,
      parentSessionId: query.owner.parentSessionId, model: h.context.model,
      childSelection: { source: "configured_worker_default", selector: "fixture/fake-count-only:low",
        modelTableDigest: "a".repeat(64), model: h.context.model, thinkingLevel: "low",
        descriptorSource: "manager_model_registry_getAll_all", serviceTierSource: "parent_session_readback" } });
    h.context.model.tokenizer = "unknown";
    const unsupported = await h.request({ query: h.query() });
    assert.equal(unsupported.status, "summary_count"); assert.equal(unsupported.proof.exact, false);
    assert.equal(unsupported.proof.fallbackReason, "tokenizer_family_unavailable");
    assert.equal(h.counted.length, 0);
  } finally { await h.close(); }
});

test("worker pause peer epoch and digest mismatch never invoke local count", async () => {
  const worker = await harness("worker");
  try { assert.equal((await worker.request({ query: worker.query() })).reason, "role_not_allowed");
    assert.equal(worker.counted.length, 0); } finally { await worker.close(); }
  const h = await harness();
  try {
    for (const change of [
      (q: any) => q.ownerDigest = "0".repeat(64),
      (q: any) => q.fragments.reverse(),
      (q: any) => { q.owner.epoch = "wrong"; q.ownerDigest = sha(canonicalJson(q.owner)); },
      (q: any) => { q.owner.parentSessionId = "wrong"; q.ownerDigest = sha(canonicalJson(q.owner)); },
      (q: any) => { q.owner.model.id = "wrong"; q.ownerDigest = sha(canonicalJson(q.owner)); }
    ]) {
      const query = h.query(); change(query);
      assert.equal((await h.request({ query })).status, "deferred");
    }
    assert.equal((await h.request({ query: h.query(), expectedPeer: { sessionId: "wrong", generation: 7 } })).status, "deferred");
    assert.equal(h.counted.length, 0);
    const old = h.query(); await h.switchSession();
    assert.equal((await h.request({ query: old })).reason, "input_count_owner_mismatch");
    await h.request({ kind: "pause" });
    assert.equal((await h.request({ query: h.query() })).reason, "paused");
    assert.equal(h.counted.length, 0);
  } finally { await h.close(); }
});

test("epoch or live model changes during local counting invalidate proof before reply", async () => {
  for (const change of ["epoch", "model"]) {
    const h = await harness();
    try {
      const query = h.query();
      h.onCount(() => { if (change === "epoch") h.metadata.epoch = "next-epoch";
        else h.context.model.id = "changed-during-count"; });
      const reply = await h.request({ query });
      assert.equal(reply.status, "deferred"); assert.equal(reply.reason, "input_count_owner_mismatch");
      assert.equal(reply.proof, undefined); assert.equal(reply.sent, false);
    } finally { await h.close(); }
  }
});
