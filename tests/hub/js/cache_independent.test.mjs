// WBX-A2 independent S8 verification: static/session-check.js and static/login-restore.js against stub browser
// globals. Expected values come from the web-board-ux SPEC "Cache 정책과 bfcache" bullets (WBX-D02) and BRIEF
// WBX-D21, not from the scripts: only a persisted pageshow acts; session-check redirects on 401 only, with
// location.replace("/login"); anything else (5xx, 403, network error) leaves the page; login-restore reloads.
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const DIR = "../../../src/workbench/hub/web/static/";
const CHECK = readFileSync(new URL(DIR + "session-check.js", import.meta.url), "utf8");
const RESTORE = readFileSync(new URL(DIR + "login-restore.js", import.meta.url), "utf8");

function boot(source, name, fetchImpl) {
  const listeners = {};
  const nav = [];
  const fetches = [];
  const window = {
    addEventListener(type, handler, options) { (listeners[type] ||= []).push({ handler, options }); },
    location: {
      replace(url) { nav.push(["replace", url]); },
      assign(url) { nav.push(["assign", url]); },
      reload() { nav.push(["reload"]); },
      get href() { return "http://hub/requests"; },
      set href(url) { nav.push(["href", url]); },
    },
    history: { go() { nav.push(["history.go"]); }, back() { nav.push(["history.back"]); } },
    document: { cookie: "" },
  };
  if (fetchImpl) {
    window.fetch = (url, options) => { fetches.push([url, options]); return fetchImpl(url, options); };
  }
  window.window = window;
  window.self = window;
  vm.createContext(window);
  const before = new Set(Object.getOwnPropertyNames(window));
  vm.runInContext(source, window, { filename: name });
  const leaked = Object.getOwnPropertyNames(window).filter((key) => !before.has(key));
  async function fire(event) {
    let thrown = null;
    for (const { handler } of listeners.pageshow || []) {
      try { handler(event); } catch (error) { thrown = error; }
    }
    for (let i = 0; i < 20; i += 1) await new Promise((resolve) => setImmediate(resolve));
    return thrown;
  }
  return { listeners, nav, fetches, fire, leaked };
}

const answer = (status) => () => Promise.resolve({ status, ok: status >= 200 && status < 300 });

// --- both scripts: shape ------------------------------------------------------------------------------------------

for (const [label, source, name] of [["session-check", CHECK, "session-check.js"], ["login-restore", RESTORE, "login-restore.js"]]) {
  test(`${label}: listens to pageshow only and leaks no global`, () => {
    const page = boot(source, name, answer(200));
    assert.deepEqual(Object.keys(page.listeners), ["pageshow"]);
    assert.equal(page.listeners.pageshow.length, 1);
    assert.deepEqual(page.leaked, [], "no global added by the script");
    assert.deepEqual(page.nav, []);
    assert.deepEqual(page.fetches, [], "nothing at load time");
  });

  test(`${label}: no dynamic code execution, no inline-script need, no storage or cookie use`, () => {
    for (const pattern of [/\beval\s*\(/, /new\s+Function/, /document\.write/, /innerHTML/, /setTimeout\s*\(\s*["']/,
                           /localStorage|sessionStorage|document\.cookie/, /import\s*\(/, /XMLHttpRequest/]) {
      assert.doesNotMatch(source, pattern, `${label} must not contain ${pattern}`);
    }
  });

  for (const [what, event] of [["persisted false", { persisted: false }], ["persisted missing", {}],
                               ["persisted null", { persisted: null }], ["no event object", undefined],
                               ["null event", null], ["persisted 0", { persisted: 0 }], ["persisted ''", { persisted: "" }]]) {
    test(`${label}: ${what} does nothing`, async () => {
      const page = boot(source, name, answer(401));
      const thrown = await page.fire(event);
      assert.equal(thrown, null, "must not throw on a malformed event");
      assert.deepEqual(page.nav, []);
      assert.deepEqual(page.fetches, []);
    });
  }
}

// --- session-check ------------------------------------------------------------------------------------------------

test("session-check: persisted -> exactly one GET-style fetch of /api/badge with same-origin credentials, no-store", async () => {
  const page = boot(CHECK, "session-check.js", answer(200));
  await page.fire({ persisted: true });
  assert.equal(page.fetches.length, 1);
  const [url, options] = page.fetches[0];
  assert.equal(url, "/api/badge");
  assert.equal(options.credentials, "same-origin");
  assert.equal(options.cache, "no-store");
  assert.ok(!options.method || options.method.toUpperCase() === "GET", "a read, not a state change");
  assert.ok(!options.body && !options.headers, "no body, no custom headers (no token leaks to the check)");
  assert.ok(!options.redirect || options.redirect === "follow" || options.redirect === "manual");
  assert.deepEqual(page.nav, []);
});

test("session-check: 401 -> location.replace('/login') once, never assign/href/reload/history", async () => {
  const page = boot(CHECK, "session-check.js", answer(401));
  await page.fire({ persisted: true });
  assert.deepEqual(page.nav, [["replace", "/login"]]);
});

for (const status of [200, 201, 204, 301, 302, 303, 304, 400, 403, 404, 408, 421, 429, 500, 502, 503, 504, 0, 418]) {
  test(`session-check: status ${status} leaves the page alone`, async () => {
    const page = boot(CHECK, "session-check.js", answer(status));
    await page.fire({ persisted: true });
    assert.deepEqual(page.nav, []);
  });
}

test("session-check: a rejected fetch (network error, Hub restarting) does nothing and leaves no unhandled rejection", async () => {
  const rejections = [];
  const listener = (reason) => rejections.push(reason);
  process.on("unhandledRejection", listener);
  try {
    for (const make of [() => new TypeError("Failed to fetch"), () => new DOMException("aborted", "AbortError"), () => undefined, () => null]) {
      const page = boot(CHECK, "session-check.js", () => Promise.reject(make()));
      const thrown = await page.fire({ persisted: true });
      assert.equal(thrown, null);
      assert.deepEqual(page.nav, []);
    }
    await new Promise((resolve) => setImmediate(resolve));
  } finally {
    process.off("unhandledRejection", listener);
  }
  assert.deepEqual(rejections, []);
});

test("session-check: response without a numeric status (opaque) does nothing", async () => {
  for (const response of [{}, { status: "401" }, { status: undefined, ok: false }, null, undefined]) {
    const page = boot(CHECK, "session-check.js", () => Promise.resolve(response));
    await page.fire({ persisted: true });
    assert.deepEqual(page.nav, [], JSON.stringify(response));
  }
});

test("session-check: fetch missing or throwing synchronously never navigates", async () => {
  const none = boot(CHECK, "session-check.js", null);
  await none.fire({ persisted: true });
  assert.deepEqual(none.nav, []);
  const sync = boot(CHECK, "session-check.js", () => { throw new Error("blocked by policy"); });
  await sync.fire({ persisted: true });
  assert.deepEqual(sync.nav, []);
});

test("session-check: every persisted restore checks again (a second restore after re-login is not cached)", async () => {
  const statuses = [200, 401, 200];
  const page = boot(CHECK, "session-check.js", () => Promise.resolve({ status: statuses.shift() }));
  await page.fire({ persisted: true });
  await page.fire({ persisted: true });
  await page.fire({ persisted: false });
  await page.fire({ persisted: true });
  assert.equal(page.fetches.length, 3, "the non-persisted pageshow in between sent nothing");
  assert.deepEqual(page.nav, [["replace", "/login"]]);
});

test("session-check: a slow 401 that arrives after a later 200 still only navigates for the 401 answer", async () => {
  const resolvers = [];
  const page = boot(CHECK, "session-check.js", () => new Promise((resolve) => resolvers.push(resolve)));
  await page.fire({ persisted: true });
  await page.fire({ persisted: true });
  resolvers[1]({ status: 200 });
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(page.nav, []);
  resolvers[0]({ status: 401 });
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(page.nav, [["replace", "/login"]]);
});

// --- login-restore ------------------------------------------------------------------------------------------------

test("login-restore: persisted -> one reload, no fetch, no redirect, no replace", async () => {
  const page = boot(RESTORE, "login-restore.js", answer(401));
  await page.fire({ persisted: true });
  assert.deepEqual(page.nav, [["reload"]]);
  assert.deepEqual(page.fetches, []);
});

test("login-restore: does not read or rewrite the form (it only reloads for a fresh token/cookie pair)", () => {
  assert.doesNotMatch(RESTORE, /login_token|querySelector|getElementById|\.value|submit\(/);
});

test("login-restore: a normal load never reloads (no reload loop)", async () => {
  const page = boot(RESTORE, "login-restore.js", null);
  await page.fire({ persisted: false });
  await page.fire({ persisted: false });
  assert.deepEqual(page.nav, []);
});
