/** Real Unix G3 admission. Controlled local SDK; no installed observer/native accuracy claim. */
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

const digest = (value: string) => createHash("sha256").update(value).digest("hex");

async function channel() {
  const directory = await mkdtemp(join(tmpdir(), "independent-count-channel-"));
  const path = join(directory, "g3.sock");
  const replies: any[] = [];
  const clients: net.Socket[] = [];
  const server = net.createServer(client => {
    clients.push(client);
    let buffer = "";
    client.on("data", data => {
      buffer += data.toString("utf8");
      while (buffer.includes("\n")) {
        const end = buffer.indexOf("\n");
        replies.push(JSON.parse(buffer.slice(0, end)));
        buffer = buffer.slice(end + 1);
      }
    });
  });
  await new Promise<void>(resolve => server.listen(path, resolve));
  const environment = { WORKBENCH_G3_BRIDGE_SOCKET: path, WORKBENCH_G3_ROLE: "manager",
    WORKBENCH_G3_TOKEN: randomUUID(), WORKBENCH_G3_GENERATION: "11" };
  const previous = Object.fromEntries(Object.keys(environment).map(key => [key, process.env[key]]));
  Object.assign(process.env, environment);
  const model: any = { provider: "fixture", id: "local-only", api: "openai-completions",
    baseUrl: "http://127.0.0.1:1/v1", contextWindow: 131072, maxTokens: 8192, tokenizer: "claude-v3" };
  const sessionId = "independent-parent";
  const metadata: any = { source: "parent_session_readback", parentSessionId: sessionId,
    epoch: "turn-1", model: structuredClone(model) };
  let idle = true;
  const context: any = { model, sessionManager: { getSessionId: () => sessionId },
    workbenchSummaryMetadata: () => metadata, isIdle: () => idle,
    hasPendingMessages: () => false, ui: { getEditorText: () => "" } };
  let counts = 0;
  const forbidden = () => assert.fail("count-only frame performed a session/provider/settings/auth/DB action");
  const sdk: any = { createAgentSession: forbidden, AgentRegistry: forbidden,
    SessionManager: { inMemory: forbidden }, Settings: { isolated: forbidden },
    getBundledAgent: forbidden, toolWireSchema: forbidden, fitOutputTokensToContextWindow: forbidden,
    Tokenizer: class {
      encoding: string;
      constructor(actual: any) { this.encoding = actual.tokenizer === "qwen3" ? "Qwen3" : "ClaudeV3"; }
      countTokens(text: string, mode: string) { assert.equal(mode, "strict"); counts++; return text.length; }
      checkTokenBudget(text: string) { return { tokens: text.length, exact: true }; }
    } };
  const handlers = new Map<string, Function>();
  workbenchG3Extension({ on: (name: string, fn: Function) => handlers.set(name, fn),
    registerTool() {}, appendEntry: forbidden, sendUserMessage: forbidden, logger: { error() {} } }, sdk);
  handlers.get("session_start")!(undefined, context);
  async function receive(match: (frame: any) => boolean) {
    const deadline = Date.now() + 2000;
    while (Date.now() < deadline) {
      const index = replies.findIndex(match);
      if (index >= 0) return replies.splice(index, 1)[0];
      await delay(5);
    }
    throw new Error("independent count response timeout");
  }
  await receive(frame => frame.kind === "hello");
  function query() {
    const owner = { parentSessionId: sessionId, epoch: metadata.epoch, model: structuredClone(metadata.model) };
    const fragments = ["independent definition 한글", "source decision\nresult"];
    return { version: "summary-host-input-v1", owner, ownerDigest: digest(canonicalJson(owner)),
      fragments, fragmentDigests: fragments.map(digest) };
  }
  async function count(query: any) {
    const requestId = randomUUID();
    clients.at(-1)!.write(JSON.stringify({ kind: "summary_count", requestId, query,
      expectedPeer: { sessionId, generation: 11 } }) + "\n");
    return receive(frame => frame.kind === "api_ack" && frame.requestId === requestId);
  }
  return { query, count, model, metadata, get counts() { return counts; }, setIdle(value: boolean) { idle = value; },
    async close() {
      handlers.get("session_shutdown")!();
      clients.forEach(client => client.destroy());
      await new Promise<void>(resolve => server.close(() => resolve()));
      for (const key of Object.keys(environment)) {
        if (previous[key] === undefined) delete process.env[key]; else process.env[key] = previous[key];
      }
      await rm(directory, { recursive: true, force: true });
    } };
}

test("separate count interactions reject old epoch and old live tokenizer without fallback", async () => {
  const h = await channel();
  try {
    const original = h.query();
    assert.equal((await h.count(original)).status, "summary_count");
    const firstCount = h.counts;
    h.metadata.epoch = "turn-2";
    const staleEpoch = await h.count(original);
    assert.equal(staleEpoch.status, "deferred");
    assert.equal(staleEpoch.reason, "input_count_owner_mismatch");
    assert.equal(staleEpoch.sent, false);
    assert.equal(staleEpoch.proof, undefined);
    assert.equal(h.counts, firstCount);
    const current = h.query();
    assert.equal((await h.count(current)).status, "summary_count");
    const secondCount = h.counts;
    h.model.tokenizer = "qwen3";
    h.metadata.model.tokenizer = "qwen3";
    const staleModel = await h.count(current);
    assert.equal(staleModel.status, "deferred");
    assert.equal(staleModel.reason, "input_count_owner_mismatch");
    assert.equal(staleModel.sent, false);
    assert.equal(staleModel.proof, undefined);
    assert.equal(h.counts, secondCount);
    const fresh = await h.count(h.query());
    assert.equal(fresh.status, "summary_count");
    assert.equal(fresh.proof.encoding, "Qwen3");
    assert.equal(fresh.proof.exact, true);
  } finally { await h.close(); }
});

test("unsafe current Manager and incomplete readback do not count or manufacture metadata", async () => {
  const h = await channel();
  try {
    const valid = h.query();
    h.setIdle(false);
    const busy = await h.count(valid);
    assert.equal(busy.status, "deferred");
    assert.equal(busy.reason, "parent_busy");
    assert.equal(busy.sent, false);
    h.setIdle(true);
    delete h.metadata.model.contextWindow;
    const unknown = await h.count(valid);
    assert.equal(unknown.status, "deferred");
    assert.equal(unknown.reason, "model_metadata_unavailable");
    assert.equal(unknown.sent, false);
    assert.equal(unknown.proof, undefined);
    assert.equal(h.counts, 0);
    assert.equal(h.metadata.model.contextWindow, undefined);
  } finally { await h.close(); }
});
