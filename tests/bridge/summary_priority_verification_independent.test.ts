/** Actual native-entry JSON view helper; no SDK loader/native capability claim. */
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { existsSync, mkdtempSync, mkdirSync, readFileSync, readdirSync, realpathSync,
  renameSync, rmSync, statSync, writeFileSync } from "node:fs";
import { stripTypeScriptTypes } from "node:module";
import { test } from "node:test";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { fileURLToPath, pathToFileURL } from "node:url";
import vm from "node:vm";

const source = readFileSync(new URL("../../omp_bridge/g3/summary_priority_verification.ts", import.meta.url), "utf8");
const start = source.indexOf("export function observeNativeEntries(");
const end = source.indexOf("\nconst fields =", start);
assert.ok(start >= 0 && end > start, "actual JSON view helper unavailable");
const helper = source.slice(start, end).replace("export function", "function");
const sha = (text: string) => createHash("sha256").update(text).digest("hex");
const observe = vm.runInNewContext(stripTypeScriptTypes(helper + "\nobserveNativeEntries;"), { sha, Buffer });

test("entry facts and marker use the same normal JSON.stringify view without dropping entries", () => {
  const entries = [
    { type: "session", id: "first", parentId: undefined, optional: undefined,
      data: [undefined, 1.25], at: new Date("2026-10-14T00:00:00Z") },
    { type: "message", id: "second", parentId: "first", content: "private-work-owned-marker" },
    { type: "custom", id: "third", parentId: "second", optional: undefined, decimal: 0.125 }
  ];
  const before = entries.map(entry => JSON.stringify(entry));
  const observed = JSON.parse(JSON.stringify(observe(entries)));
  assert.equal(observed.entries, entries.length);
  assert.equal(observed.allEntries.length, entries.length);
  assert.equal(observed.rawUpperMarkerSeen, true);
  for (let index = 0; index < entries.length; index++) {
    const view = JSON.parse(before[index]), facts = observed.allEntries[index];
    assert.equal(facts.type, view.type ?? null); assert.equal(facts.id, view.id ?? null);
    assert.equal(facts.parentId, view.parentId ?? null);
    assert.equal(facts.bytes, Buffer.byteLength(before[index]));
    assert.equal(facts.native_JSON_serialized_entry_sha256, sha(before[index]));
  }
  assert.deepEqual(entries.map(entry => JSON.stringify(entry)), before);
  assert.match(observed.entryView, /native JSON.stringify/);
  assert.match(observed.entryView, /storage serialization equality unknown/);
});

test("serialization failure refuses the whole observation instead of silently dropping an entry", () => {
  const circular: any = { type: "message", id: "cannot-serialize", parentId: null };
  circular.content = circular;
  assert.throws(() => observe([{ type: "session", id: "first" }, circular]), /circular/i);
  assert.throws(() => observe([undefined]), /native_entry_JSON_string_unavailable/);
});

function shutdownFixture(variant = "normal") {
  const root = mkdtempSync(join(process.env.DVL14_PRIORITY_NODE_OUTPUT ?? tmpdir(), "owned-shutdown-"));
  for (const part of ["home", "project", "runtime/omp_bridge/g3"])
    mkdirSync(join(root, part), { recursive: true, mode: 0o700 });
  const cwd = process.cwd(), home = process.env.HOME, prior = process.env.WORKBENCH_SUMMARY_VERIFY_ROOT;
  process.env.HOME = join(root, "home"); process.env.WORKBENCH_SUMMARY_VERIFY_ROOT = root;
  process.chdir(join(root, "project"));
  const endpoint = variant === "endpoint" ? "http://example.invalid:1/v1" : "http://127.0.0.1:1/v1";
  writeFileSync(join(root, "fixture.json"), JSON.stringify({ root, endpoint }), { mode: 0o600 });
  let reject!: (error: Error) => void;
  const readiness = new Promise<void>((_resolve, fail) => { reject = fail; });
  const intervals = new Map<any, () => void>(), hooks: any = {}, activity: string[] = [];
  let shuts = 0, clears = 0, dispatches = 0;
  const registry = { awaitInitialBackgroundRefresh() { activity.push("readiness_wait"); return readiness; } };
  const sessionManager = { getSessionId: () => "owned-parent" };
  const parent = { sessionManager, modelRegistry: registry };
  const ctx = { sessionManager, modelRegistry: registry, model: { provider: "ownedverify", id: "scripted", baseUrl: endpoint },
    setInterval(callback: () => void, ms: number) {
      assert.equal(ms, 20); const token = {}; intervals.set(token, callback);
      activity.push("timer_installed"); return token;
    }, clearTimer(token: any) { assert.equal(intervals.delete(token), true); clears++; activity.push("clearTimer"); },
    shutdown() {
      shuts++; activity.push("shutdown"); hooks.session_shutdown();
      // Public API lifecycle contract supplied by Root: SDK owns automatic
      // session_shutdown disposal. This controlled API is not native proof.
      intervals.clear();
    } };
  const observerSource = source.slice(source.indexOf("const fields ="))
    .replace("export default function observer", "function observer").replaceAll("import.meta.url", "moduleURL");
  const actual = vm.runInNewContext(stripTypeScriptTypes(observerSource + "\nobserver;"), {
    sha, observeNativeEntries: observe, existsSync, readFileSync, readdirSync, realpathSync,
    renameSync, statSync, writeFileSync, fileURLToPath, URL, process, AbortController,
    moduleURL: pathToFileURL(join(root, "runtime/omp_bridge/g3/summary_priority_verification.ts")).href,
    AgentRegistry: { global: () => ({ list() { activity.push("native_parent_checked");
      return variant === "parent" ? [] : [{ session: parent }]; } }) },
    setTimeout: () => "controlled-preparation-timeout", clearTimeout: () => {},
    dispatchVerificationSummarySDK() { dispatches++; throw Error("unexpected fixture dispatch"); }
  });
  actual({ on(event: string, handler: any) { assert.equal(hooks[event], undefined); hooks[event] = handler; } });
  return { root, ctx, hooks, activity, intervals,
    tick() { for (const callback of [...intervals.values()]) callback(); },
    counts: () => ({ shuts, clears, dispatches }),
    async cleanup() {
      if (activity.includes("readiness_wait")) reject(Error("controlled readiness cleanup"));
      await new Promise(resolve => setImmediate(resolve)); intervals.clear();
      process.chdir(cwd);
      if (home === undefined) delete process.env.HOME; else process.env.HOME = home;
      if (prior === undefined) delete process.env.WORKBENCH_SUMMARY_VERIFY_ROOT;
      else process.env.WORKBENCH_SUMMARY_VERIFY_ROOT = prior;
      rmSync(root, { recursive: true }); assert.equal(existsSync(root), false);
    } };
}

test("actual owned public timer ignores absent marker, then clears and shuts down once", async () => {
  const f = shutdownFixture();
  try {
    f.hooks.session_start({}, f.ctx);
    assert.deepEqual(f.activity, ["native_parent_checked", "timer_installed", "readiness_wait"]);
    f.tick(); f.tick(); assert.deepEqual(f.counts(), { shuts: 0, clears: 0, dispatches: 0 });
    writeFileSync(join(f.root, "owned-stop.json"), JSON.stringify({ kind: "owned-graceful-stop" }), { mode: 0o600 });
    f.tick(); assert.deepEqual(f.counts(), { shuts: 1, clears: 1, dispatches: 0 });
    assert.deepEqual(f.activity.slice(-2), ["clearTimer", "shutdown"]);
    assert.equal(f.intervals.size, 0);
    f.tick(); f.hooks.session_start({}, f.ctx);
    assert.deepEqual(f.counts(), { shuts: 1, clears: 1, dispatches: 0 });
  } finally { await f.cleanup(); }
});

test("actual ownership setup refuses public shutdown timer before endpoint or parent is accepted", async () => {
  for (const variant of ["endpoint", "parent"]) {
    const f = shutdownFixture(variant);
    try {
      f.hooks.session_start({}, f.ctx); await new Promise(resolve => setImmediate(resolve));
      assert.equal(f.intervals.size, 0); assert.equal(f.activity.includes("timer_installed"), false);
      assert.deepEqual(f.counts(), { shuts: 0, clears: 0, dispatches: 0 });
      const report = JSON.parse(readFileSync(join(f.root, "ownerready.json"), "utf8"));
      assert.match(report.error, variant === "endpoint" ? /owned_endpoint_unavailable/ : /native_parent_registry_unavailable/);
    } finally { await f.cleanup(); }
  }
});
