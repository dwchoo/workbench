// WBX-A2 / seam S8: static/login-restore.js against stub browser globals (no browser). A login screen restored from
// the back/forward cache (pageshow with persisted) reloads once to get a fresh login token and cookie pair; a normal
// load does nothing. Expected values from the web-board-ux SPEC "Cache 정책과 bfcache" (로그인 화면). Real bfcache: S7.
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const SOURCE = readFileSync(new URL("../../../src/workbench/hub/web/static/login-restore.js", import.meta.url), "utf8");

function load() {
  const listeners = {};
  const navigations = [];
  const fetches = [];
  const window = {
    addEventListener(type, handler) { (listeners[type] ||= []).push(handler); },
    location: {
      reload() { navigations.push(["reload"]); },
      replace(url) { navigations.push(["replace", url]); },
      assign(url) { navigations.push(["assign", url]); },
    },
    fetch(url) { fetches.push(url); return Promise.resolve({ status: 200 }); },
  };
  window.window = window;
  vm.createContext(window);
  vm.runInContext(SOURCE, window, { filename: "login-restore.js" });
  const pageshow = (persisted) => { for (const handler of listeners.pageshow || []) handler({ type: "pageshow", persisted }); };
  return { listeners, navigations, fetches, pageshow };
}

test("a normal load of the login screen does nothing", () => {
  const page = load();
  assert.equal((page.listeners.pageshow || []).length, 1, "one pageshow listener");
  page.pageshow(false);
  assert.deepEqual(page.navigations, []);
});

test("a restored login screen reloads once and asks nothing else", () => {
  const page = load();
  page.pageshow(true);
  assert.deepEqual(page.navigations, [["reload"]]);
  assert.deepEqual(page.fetches, []);
});

test("a missing or odd event does nothing", () => {
  const page = load();
  for (const handler of page.listeners.pageshow) {
    handler(undefined);
    handler({ type: "pageshow" });
    handler({ type: "pageshow", persisted: "yes" });
  }
  assert.deepEqual(page.navigations, []);
});
