/** Actual G3 handler/metadata observer, controlled SDK getters; Native/model0. */
import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { mkdtemp, rm } from "node:fs/promises";
import net from "node:net";
import { join } from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import { test } from "node:test";
import extension from "../../omp_bridge/g3/bridge.ts";

const definition = "  Existing task definition\n한글 bytes\n";
async function channel(role = "manager") {
  // UNIX socket path must fit Linux's limit; only ephemeral private fixtures live here.
  const root = await mkdtemp("/tmp/dvl14-runtime-");
  const socketPath = join(root, "bridge.sock");
  const clients: net.Socket[] = [], frames: any[] = [];
  const server = net.createServer(client => {
    clients.push(client); let buffer = "";
    client.on("data", data => {
      buffer += data.toString();
      while (buffer.includes("\n")) {
        const end = buffer.indexOf("\n");
        frames.push(JSON.parse(buffer.slice(0, end))); buffer = buffer.slice(end + 1);
      }
    });
  });
  await new Promise<void>(resolve => server.listen(socketPath, resolve));
  const environment = { WORKBENCH_G3_BRIDGE_SOCKET: socketPath, WORKBENCH_G3_ROLE: role,
    WORKBENCH_G3_TOKEN: randomUUID(), WORKBENCH_G3_GENERATION: "7" };
  const previous = Object.fromEntries(Object.keys(environment).map(key => [key, process.env[key]]));
  Object.assign(process.env, environment);
  const calls = { registry: 0, definition: 0, editor: 0, effects: 0 };
  const forbidden = () => { calls.effects++; assert.fail("passive runtime performed an SDK or parent mutation"); };
  let sessionId = randomUUID();
  const parent: any = { model: { id: "owned", provider: "fixture", api: "openai-completions",
    baseUrl: "http://127.0.0.1:1/v1", contextWindow: 131072, maxTokens: 8192, tokenizer: "claude-v3" },
    thinkingLevel: "off", serviceTierByFamily: { fixture: "standard" },
    sessionManager: { getSessionId: () => sessionId } };
  const context: any = { model: parent.model, sessionManager: parent.sessionManager,
    isIdle: () => false, hasPendingMessages: () => true,
    ui: { getEditorText: () => { calls.editor++; return "unsent editor text"; } } };
  const sdk: any = { AgentRegistry: { global: () => { calls.registry++; return { list: () => [{ session: parent }] }; } },
    getBundledAgent(name: string) { assert.equal(name, "task"); calls.definition++;
      return { source: "bundled", filePath: "embedded:task.md", systemPrompt: definition }; },
    createAgentSession: forbidden, SessionManager: { inMemory: forbidden }, Settings: { isolated: forbidden },
    Tokenizer: class { constructor() { forbidden(); } }, authStorage: { get: forbidden } };
  const handlers = new Map<string, Function>();
  extension({ on: (name: string, fn: Function) => handlers.set(name, fn), registerTool() {},
    sendUserMessage: forbidden, appendEntry: forbidden, logger: { error() {} } }, sdk);
  async function receive(kind: string, id?: string) {
    const deadline = Date.now() + 2000;
    while (Date.now() < deadline) {
      const index = frames.findIndex(f => f.kind === kind && (id === undefined || f.requestId === id));
      if (index >= 0) return frames.splice(index, 1)[0];
      await delay(2);
    }
    throw new Error("owned runtime response timeout");
  }
  handlers.get("session_start")!(undefined, context);
  const hello = await receive("hello");
  calls.editor = 0;
  return { parent, context, calls, hello,
    changeSession() { sessionId = randomUUID(); },
    async query(expectedPeer = { sessionId: hello.ompSessionId, generation: 7 }) {
      const requestId = randomUUID();
      clients.at(-1)!.write(JSON.stringify({ kind: "summary_runtime", requestId, expectedPeer }) + "\n");
      return receive("api_ack", requestId);
    }, async close() {
      handlers.get("session_shutdown")!(); clients.forEach(client => client.destroy());
      await new Promise<void>(resolve => server.close(() => resolve()));
      for (const key of Object.keys(environment)) {
        if (previous[key] === undefined) delete process.env[key]; else process.env[key] = previous[key];
      }
      await rm(root, { recursive: true, force: true });
    } };
}

test("passive runtime reads exact task bytes and preserves unknown budgets without editor or SDK actions", async () => {
  const h = await channel();
  try {
    const reply = await h.query();
    assert.equal(reply.status, "summary_runtime"); assert.equal(reply.sent, false); assert.equal(reply.protected, false);
    assert.equal(reply.sessionId, h.hello.ompSessionId); assert.equal(reply.generation, 7);
    assert.equal(reply.runtime.instructions, definition);
    const metadata = reply.runtime.metadata;
    assert.equal(metadata.source, "parent_session_readback");
    assert.equal(metadata.parentSessionId, h.hello.ompSessionId);
    assert.equal(metadata.status, "inconclusive"); assert.equal(metadata.outputMeaning, "unknown");
    for (const field of ["existingContextTokens", "outputCap", "reasoningTokens", "visibleResultTokens", "outputLimitEnforced"])
      assert.equal(metadata[field], null);
    assert.deepEqual(metadata.unknowns, ["current_context_unavailable", "output_ceiling_enforcement_unavailable",
      "reasoning_budget_semantics_unavailable"]);
    assert.equal(h.calls.definition, 1); assert.equal(h.calls.registry, 1);
    assert.equal(h.calls.editor, 0); assert.equal(h.calls.effects, 0);
  } finally { await h.close(); }
});

test("fresh parent model thinking and tier getters change epoch while earlier snapshot stays detached", async () => {
  const h = await channel();
  try {
    const first = (await h.query()).runtime.metadata;
    const original = structuredClone(first);
    let previous = first;
    for (const change of [
      () => { h.parent.model = { ...h.parent.model, id: "next-owned-model" }; h.context.model = h.parent.model; },
      () => { h.parent.thinkingLevel = "high"; },
      () => { h.parent.serviceTierByFamily.fixture = "priority"; },
    ]) {
      const before = structuredClone(previous);
      change();
      const next = (await h.query()).runtime.metadata;
      assert.notEqual(next.epoch, previous.epoch);
      assert.deepEqual(previous, before);
      previous = next;
    }
    const next = previous;
    assert.equal(next.model.id, "next-owned-model");
    assert.equal(next.thinkingLevel, "high"); assert.equal(next.serviceTierByFamily.fixture, "priority");
    assert.deepEqual(first, original); assert.equal(h.calls.effects, 0);
  } finally { await h.close(); }
});

test("Worker and wrong expected Manager peer refuse before metadata getters", async () => {
  for (const role of ["worker", "manager"]) {
    const h = await channel(role);
    try {
      const reply = await h.query({ sessionId: role === "worker" ? h.hello.ompSessionId : randomUUID(), generation: 7 });
      assert.equal(reply.status, "deferred"); assert.equal(reply.sent, false);
      assert.equal(reply.reason, role === "worker" ? "role_not_allowed" : "authority_changed_before_submission");
      assert.equal(h.calls.registry, 0); assert.equal(h.calls.definition, 0); assert.equal(h.calls.effects, 0);
    } finally { await h.close(); }
  }
});

test("context session drift refuses an otherwise matching old bound peer before getters", async () => {
  const h = await channel();
  try {
    h.changeSession(); const reply = await h.query();
    assert.equal(reply.status, "deferred"); assert.equal(reply.reason, "authority_changed_before_submission");
    assert.equal(reply.sent, false); assert.equal(h.calls.registry, 0); assert.equal(h.calls.definition, 0);
    assert.equal(h.calls.effects, 0);
  } finally { await h.close(); }
});
