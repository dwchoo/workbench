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

// WBX-A7 (2026-10-28 사용자 결정, Q39 a): a page shown again by Back/Forward hides its content until the session
// check answers. The script runs synchronously in <head>, so the class on <html> ("wb-checking"; base.css hides
// <body> by it) is there before first paint. 2xx, another status or a network error reveal the page (fail open: the
// server still refuses any action); 401 goes to /login while still hidden; a safety timer reveals the page if no
// answer comes, so the screen never stays blank. A normal load, a reload or no navigation timing never hides.
const CHECKING = "wb-checking";

function loadHidden(answer, navigation) {
  const listeners = {};
  const fetches = [];
  const navigations = [];
  const timers = [];
  const classes = new Set();
  const classList = {
    add(name) { classes.add(name); },
    remove(name) { classes.delete(name); },
    contains(name) { return classes.has(name); },
  };
  const window = {
    addEventListener(type, handler) { (listeners[type] ||= []).push(handler); },
    location: {
      replace(url) { navigations.push(["replace", url]); },
      assign(url) { navigations.push(["assign", url]); },
      reload() { navigations.push(["reload"]); },
      set href(url) { navigations.push(["href", url]); },
    },
    document: { documentElement: { classList } },
    setTimeout(handler, delay) { timers.push({ handler, delay, cleared: false }); return timers.length; },
    clearTimeout(id) { if (timers[id - 1]) timers[id - 1].cleared = true; },
  };
  if (answer) window.fetch = (url, options) => { fetches.push([url, options]); return answer(); };
  if (navigation !== undefined) {
    window.performance = {
      getEntriesByType(type) { return type === "navigation" && navigation !== null ? [{ type: navigation }] : []; },
    };
  }
  window.window = window;
  window.self = window;
  vm.createContext(window);
  vm.runInContext(SOURCE, window, { filename: "session-check.js" });
  async function settle() { for (let i = 0; i < 10; i += 1) await new Promise((resolve) => setImmediate(resolve)); }
  function fire(type, persisted) { for (const handler of listeners[type] || []) handler({ type, persisted }); }
  function runTimers() {
    for (const timer of timers.splice(0)) { if (!timer.cleared) timer.handler(); }
  }
  return { listeners, fetches, navigations, timers, settle, fire, runTimers,
           hidden: () => classes.has(CHECKING) };
}

const never = () => new Promise(() => {});

test("A7: a page loaded by Back/Forward is hidden as soon as the script runs, before any answer", () => {
  const page = loadHidden(never, "back_forward");
  assert.equal(page.hidden(), true);
  assert.equal(page.fetches.length, 1);
});

test("A7: a live session (2xx) reveals the Back/Forward page and stays on it", async () => {
  for (const status of [200, 204]) {
    const page = loadHidden(reply(status), "back_forward");
    await page.settle();
    assert.equal(page.hidden(), false, String(status));
    assert.deepEqual(page.navigations, [], String(status));
  }
});

test("A7: a Back/Forward page whose session is gone (401) goes to /login while still hidden", async () => {
  const page = loadHidden(reply(401), "back_forward");
  await page.settle();
  assert.deepEqual(page.navigations, [["replace", "/login"]]);
  assert.equal(page.hidden(), true, "the old content is never shown");
});

test("A7: a network error or another status reveals the Back/Forward page (fail open) and goes nowhere", async () => {
  for (const [label, answer] of [["network error", () => Promise.reject(new TypeError("Failed to fetch"))],
                                 ["403", reply(403)], ["500", reply(500)], ["502", reply(502)],
                                 ["opaque", () => Promise.resolve({})], ["null", () => Promise.resolve(null)]]) {
    const page = loadHidden(answer, "back_forward");
    await page.settle();
    assert.equal(page.hidden(), false, label);
    assert.deepEqual(page.navigations, [], label);
  }
});

test("A7: with no answer a safety timer (at most 4 s) reveals the Back/Forward page", async () => {
  const page = loadHidden(never, "back_forward");
  await page.settle();
  assert.equal(page.hidden(), true);
  const armed = page.timers.filter((timer) => !timer.cleared);
  assert.equal(armed.length, 1, "one safety timer");
  assert.ok(armed[0].delay > 0 && armed[0].delay <= 4000, String(armed[0].delay));
  page.runTimers();
  assert.equal(page.hidden(), false);
  assert.deepEqual(page.navigations, []);
});

test("A7: a normal load, a reload or a browser without navigation timing is never hidden and arms no timer", async () => {
  for (const navigation of ["navigate", "reload", "prerender", null, undefined]) {
    const page = loadHidden(reply(401), navigation);
    await page.settle();
    page.fire("pageshow", false);
    await page.settle();
    assert.equal(page.hidden(), false, String(navigation));
    assert.deepEqual(page.timers, [], String(navigation));
  }
});

test("A7: leaving for the bfcache (pagehide persisted) hides the page so it comes back hidden; a plain unload does not", () => {
  const kept = loadHidden(reply(200), "navigate");
  kept.fire("pagehide", true);
  assert.equal(kept.hidden(), true);
  assert.deepEqual(kept.fetches, [], "nothing is asked on the way out");
  for (const persisted of [false, undefined]) {
    const gone = loadHidden(reply(200), "navigate");
    gone.fire("pagehide", persisted);
    assert.equal(gone.hidden(), false, String(persisted));
  }
});

test("A7: a bfcache restore stays hidden while it asks, with the safety timer, and is revealed by a live session", async () => {
  const waiting = loadHidden(never, "navigate");
  waiting.fire("pagehide", true);
  waiting.fire("pageshow", true);
  await waiting.settle();
  assert.equal(waiting.hidden(), true);
  assert.equal(waiting.fetches.length, 1);
  assert.equal(waiting.timers.filter((timer) => !timer.cleared).length, 1, "one safety timer");
  waiting.runTimers();
  assert.equal(waiting.hidden(), false, "the timer reveals it");

  const live = loadHidden(reply(200), "navigate");
  live.fire("pagehide", true);
  live.fire("pageshow", true);
  await live.settle();
  assert.equal(live.hidden(), false);
  assert.equal(live.timers.filter((timer) => !timer.cleared).length, 0, "the answer stops the timer");

  const gone = loadHidden(reply(401), "navigate");
  gone.fire("pagehide", true);
  gone.fire("pageshow", true);
  await gone.settle();
  assert.deepEqual(gone.navigations, [["replace", "/login"]]);
  assert.equal(gone.hidden(), true, "the old content is never shown");
});

test("A7: no fetch at all, or a fetch that throws at once, reveals the Back/Forward page", async () => {
  for (const [label, answer] of [["no fetch", null], ["throws", () => { throw new Error("blocked by policy"); }]]) {
    const page = loadHidden(answer, "back_forward");
    await page.settle();
    assert.equal(page.hidden(), false, label);
    assert.deepEqual(page.navigations, [], label);
  }
});

test("A7: after a 401 no safety timer is left to show the old content while /login loads", async () => {
  const page = loadHidden(reply(401), "back_forward");
  await page.settle();
  page.runTimers();
  assert.equal(page.hidden(), true);
  assert.deepEqual(page.navigations, [["replace", "/login"]]);
});

test("A7: a late answer to an earlier check does not reveal a page whose newer check is still waiting", async () => {
  const resolvers = [];
  const page = loadHidden(() => new Promise((resolve) => resolvers.push(resolve)), "back_forward");
  page.fire("pagehide", true);
  page.fire("pageshow", true);
  resolvers[0]({ status: 200 });
  await page.settle();
  assert.equal(page.hidden(), true, "the restore's own check has not answered");
  resolvers[1]({ status: 200 });
  await page.settle();
  assert.equal(page.hidden(), false);
});
