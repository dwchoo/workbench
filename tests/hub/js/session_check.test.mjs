// WBX-A2 / seam S8: static/session-check.js against stub browser globals (no browser). A logged-in screen restored
// from the back/forward cache (pageshow with persisted) asks /api/badge; 401 -> location.replace("/login"); any other
// answer or a network error changes nothing. A normal load (persisted false) sends nothing. Expected values come
// from the web-board-ux SPEC "Cache 정책과 bfcache" bullets. The real bfcache is a user Chrome check (S7).
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const SOURCE = readFileSync(new URL("../../../src/workbench/hub/web/static/session-check.js", import.meta.url), "utf8");

// A fresh window per test: the script runs as a classic deferred script with window === globalThis. ``navigation`` is
// the type of this document's navigation timing entry ("navigate", "reload", "back_forward"); undefined: no
// performance API at all.
function load(answer, navigation) {
  const listeners = {};
  const fetches = [];
  const navigations = [];
  const window = {
    addEventListener(type, handler) { (listeners[type] ||= []).push(handler); },
    location: {
      replace(url) { navigations.push(["replace", url]); },
      assign(url) { navigations.push(["assign", url]); },
      reload() { navigations.push(["reload"]); },
      set href(url) { navigations.push(["href", url]); },
    },
    fetch(url, options) {
      fetches.push([url, options]);
      return answer();
    },
  };
  if (navigation !== undefined) {
    window.performance = {
      getEntriesByType(type) { return type === "navigation" && navigation !== null ? [{ type: navigation }] : []; },
    };
  }
  window.window = window;
  window.self = window;
  vm.createContext(window);
  vm.runInContext(SOURCE, window, { filename: "session-check.js" });
  async function pageshow(persisted) {
    for (const handler of listeners.pageshow || []) handler({ type: "pageshow", persisted });
    for (let i = 0; i < 10; i += 1) await new Promise((resolve) => setImmediate(resolve));  // settle the fetch chain
  }
  async function settle() { for (let i = 0; i < 10; i += 1) await new Promise((resolve) => setImmediate(resolve)); }
  return { listeners, fetches, navigations, pageshow, settle };
}

const reply = (status) => () => Promise.resolve({ status, ok: status >= 200 && status < 300,
                                                   json: () => Promise.resolve({ pending: 0, needs_info: 0 }) });

test("a normal load sends nothing and goes nowhere", async () => {
  const page = load(reply(401));
  assert.equal((page.listeners.pageshow || []).length, 1, "one pageshow listener");
  await page.pageshow(false);
  assert.deepEqual(page.fetches, []);
  assert.deepEqual(page.navigations, []);
});

test("a restored page asks /api/badge with the session cookie and no cache", async () => {
  const page = load(reply(200));
  await page.pageshow(true);
  assert.equal(page.fetches.length, 1);
  const [url, options] = page.fetches[0];
  assert.equal(url, "/api/badge");
  assert.equal(options.credentials, "same-origin");
  assert.equal(options.cache, "no-store");
  assert.deepEqual(page.navigations, [], "a live session stays on the page");
});

test("a restored page whose session is gone (401) replaces itself with /login", async () => {
  const page = load(reply(401));
  await page.pageshow(true);
  assert.deepEqual(page.navigations, [["replace", "/login"]]);
});

test("other answers change nothing", async () => {
  for (const status of [200, 204, 403, 404, 500, 502, 503]) {
    const page = load(reply(status));
    await page.pageshow(true);
    assert.equal(page.fetches.length, 1, String(status));
    assert.deepEqual(page.navigations, [], String(status));
  }
});

test("a network error (the Hub restarting) changes nothing and is not left unhandled", async () => {
  const unhandled = [];
  const onUnhandled = (reason) => unhandled.push(reason);
  process.on("unhandledRejection", onUnhandled);
  try {
    const page = load(() => Promise.reject(new TypeError("Failed to fetch")));
    await page.pageshow(true);
    assert.equal(page.fetches.length, 1);
    assert.deepEqual(page.navigations, []);
    await new Promise((resolve) => setTimeout(resolve, 20));
    assert.deepEqual(unhandled, []);
  } finally {
    process.off("unhandledRejection", onUnhandled);
  }
});

test("each restore asks again", async () => {
  const page = load(reply(200));
  await page.pageshow(true);
  await page.pageshow(true);
  assert.equal(page.fetches.length, 2);
});

// WBX-A6: Back/Forward served from the HTTP cache (bfcache not used: the user's Chrome after a logout) loads the page
// afresh, so pageshow says persisted=false; the navigation timing entry says "back_forward". The same check runs then.
test("a page loaded by Back/Forward from the HTTP cache asks /api/badge once at load", async () => {
  const page = load(reply(200), "back_forward");
  await page.settle();
  assert.equal(page.fetches.length, 1);
  const [url, options] = page.fetches[0];
  assert.equal(url, "/api/badge");
  assert.equal(options.credentials, "same-origin");
  assert.equal(options.cache, "no-store");
  await page.pageshow(false);  // the load's own pageshow is not persisted: no second ask
  assert.equal(page.fetches.length, 1);
  assert.deepEqual(page.navigations, [], "a live session stays on the page");
});

test("a Back/Forward page whose session is gone (401) replaces itself with /login", async () => {
  const page = load(reply(401), "back_forward");
  await page.settle();
  assert.deepEqual(page.navigations, [["replace", "/login"]]);
});

test("a Back/Forward page with a network error or another answer stays", async () => {
  for (const answer of [() => Promise.reject(new TypeError("Failed to fetch")), reply(500), reply(403)]) {
    const page = load(answer, "back_forward");
    await page.settle();
    assert.equal(page.fetches.length, 1);
    assert.deepEqual(page.navigations, []);
  }
});

test("a normal load, a reload or a browser without navigation timing sends nothing", async () => {
  for (const navigation of ["navigate", "reload", "prerender", null, undefined]) {
    const page = load(reply(401), navigation);
    await page.settle();
    assert.deepEqual(page.fetches, [], String(navigation));
    assert.deepEqual(page.navigations, [], String(navigation));
  }
});

test("a Back/Forward page later restored from the bfcache asks again", async () => {
  const page = load(reply(200), "back_forward");
  await page.settle();
  await page.pageshow(true);
  assert.equal(page.fetches.length, 2);
});
