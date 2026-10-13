/** STARTUP-NONBLOCKING: actual entry wrapper; SDK/catalog run is controlled.
 * Real fs/uid/root/HOME/project ownership checks and completion publisher run.
 * VM module URL substitution is not native OMP loader or TUI evidence.
 */
import assert from "node:assert/strict";
import { mkdtempSync, mkdirSync, readFileSync, readdirSync, realpathSync, rmSync, statSync } from "node:fs";
import { stripTypeScriptTypes } from "node:module";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { fileURLToPath, pathToFileURL } from "node:url";
import vm from "node:vm";
import { publishVerificationCompletion } from "../../omp_bridge/g3/verification_completion.ts";

const entry = readFileSync(new URL("../../omp_bridge/g3/summary_verification.ts", import.meta.url), "utf8");
const start = entry.indexOf("let started = false;");
const end = entry.indexOf("\nasync function run(", start);
assert.ok(start >= 0 && end > start, "entry wrapper extraction unavailable");
const wrapper = entry.slice(start, end)
  .replace("export default function verification", "function verification")
  .replaceAll("import.meta.url", "moduleURL");

function ownedFixture() {
  const root = mkdtempSync(join(process.env.DVL14_STARTUP_TEST_OUTPUT ?? tmpdir(), "startup-"));
  assert.equal(statSync(root).mode & 0o777, 0o700);
  assert.equal(statSync(root).uid, process.getuid!());
  for (const name of ["home", "project", "runtime/omp_bridge/g3"])
    mkdirSync(join(root, name), { recursive: true, mode: 0o700 });
  const cwd = process.cwd(), home = process.env.HOME, priorRoot = process.env.WORKBENCH_SUMMARY_VERIFY_ROOT;
  process.env.HOME = join(root, "home");
  process.env.WORKBENCH_SUMMARY_VERIFY_ROOT = root;
  process.chdir(join(root, "project"));
  return { root, moduleURL: pathToFileURL(join(root, "runtime/omp_bridge/g3/summary_verification.ts")).href,
    cleanup() {
      process.chdir(cwd);
      if (home === undefined) delete process.env.HOME; else process.env.HOME = home;
      if (priorRoot === undefined) delete process.env.WORKBENCH_SUMMARY_VERIFY_ROOT;
      else process.env.WORKBENCH_SUMMARY_VERIFY_ROOT = priorRoot;
      rmSync(root, { recursive: true });
      assert.throws(() => statSync(root), { code: "ENOENT" });
    } };
}

function registered(fixture: ReturnType<typeof ownedFixture>, run: (root: string, context: any) => Promise<void>) {
  const publications: any[] = [];
  const verification = vm.runInNewContext(stripTypeScriptTypes(wrapper + "\nverification;"), {
    statSync, realpathSync, fileURLToPath, URL, process, moduleURL: fixture.moduleURL, run,
    publishVerificationCompletion(root: string, value: unknown, kind: "ok" | "err") {
      publications.push({ root, value, kind });
      publishVerificationCompletion(root, value, kind);
    } });
  let handler: any;
  verification({ on(event: string, callback: any) {
    assert.equal(event, "session_start"); assert.equal(handler, undefined); handler = callback;
  } });
  assert.equal(typeof handler, "function");
  return { handler, publications };
}

// A setImmediate boundary drains Promise reactions. No wall-clock sleeps or
// probabilistic timeout races are used to distinguish pending readiness.
const turn = () => new Promise<void>(resolve => setImmediate(resolve));

test("owned session_start returns before ready, enabling refresh, and starts only once", async () => {
  const fixture = ownedFixture();
  let release!: () => void, returned = false, calls = 0, executed = 0;
  const ready = new Promise<void>(resolve => { release = resolve; });
  const order: string[] = [];
  const context = { registryFixture: true };
  const { handler, publications } = registered(fixture, async (root, supplied) => {
    assert.equal(root, fixture.root); assert.equal(supplied, context);
    calls++; order.push("run_waiting"); await ready;
    executed++; order.push("prepared_execution");
  });
  const pending = handler({}, context).then(() => { returned = true; order.push("handler_returned"); });
  try {
    await turn();
    assert.equal(returned, true, "initial refresh cannot start until session_start returns");
    assert.equal(calls, 1); assert.equal(executed, 0);
    await handler({}, context); // Duplicate while preparation is still pending.
    assert.equal(calls, 1); assert.equal(executed, 0);
    order.push("initial_refresh_started"); release();
    await pending; await turn();
    assert.equal(executed, 1);
    assert.deepEqual(order, ["run_waiting", "handler_returned", "initial_refresh_started", "prepared_execution"]);
    await handler({}, context); assert.equal(calls, 1);
    assert.deepEqual(publications, []); // Controlled run does not fabricate a PASS receipt.
  } finally {
    release(); await pending; await turn(); fixture.cleanup();
  }
});

test("asynchronous owned run failure publishes exercised[] error once after handler return", async () => {
  const fixture = ownedFixture();
  let reject!: (error: Error) => void, calls = 0, returned = false;
  const ready = new Promise<void>((_resolve, fail) => { reject = fail; });
  const { handler, publications } = registered(fixture, async () => { calls++; await ready; });
  const pending = handler({}, {}).then(() => { returned = true; });
  try {
    await turn(); assert.equal(returned, true); assert.equal(calls, 1);
    assert.equal(publications.length, 0);
    reject(Error("owned asynchronous fixture failure"));
    await pending; await turn();
    await handler({}, {}); assert.equal(calls, 1); assert.equal(publications.length, 1);
    assert.equal(publications[0].kind, "err");
    const path = join(fixture.root, "completed.json");
    assert.deepEqual(JSON.parse(readFileSync(path, "utf8")), {
      error: "Error: owned asynchronous fixture failure", exercised: [] });
    assert.equal(statSync(path).mode & 0o777, 0o600);
    assert.equal(readdirSync(fixture.root).some(name => name.startsWith(".completed.")), false);
  } finally {
    reject(Error("cleanup release")); await pending; await turn(); fixture.cleanup();
  }
});

test("absent owned invocation starts nothing and duplicate event cannot acquire ownership later", async () => {
  const fixture = ownedFixture(); let calls = 0;
  const { handler, publications } = registered(fixture, async () => { calls++; });
  try {
    delete process.env.WORKBENCH_SUMMARY_VERIFY_ROOT;
    await handler({}, {}); await turn();
    process.env.WORKBENCH_SUMMARY_VERIFY_ROOT = fixture.root;
    await handler({}, {}); await turn();
    assert.equal(calls, 0); assert.deepEqual(publications, []);
    assert.equal(readdirSync(fixture.root).includes("completed.json"), false);
  } finally { fixture.cleanup(); }
});

test("actual project ownership mismatch refuses run without a completion or fake pass", async () => {
  const fixture = ownedFixture(); let calls = 0;
  const { handler, publications } = registered(fixture, async () => { calls++; });
  try {
    process.chdir(join(fixture.root, "home"));
    await handler({}, {}); await turn();
    assert.equal(calls, 0); assert.deepEqual(publications, []);
    assert.equal(readdirSync(fixture.root).includes("completed.json"), false);
  } finally { fixture.cleanup(); }
});
