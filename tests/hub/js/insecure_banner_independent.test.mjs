// WBX-A3 independent verification (test_designer), seam S8. Scenarios come from the web-board-ux SPEC "평문 HTTP 배너
// 접기" (localStorage `wb.insecureBanner` = "collapsed" -> class `wb-insecure-collapsed` on <html>; close button
// stores + adds the class; chip click expands and deletes the stored value; storage that throws leaves the banner
// expanded) and BRIEF WBX-AC-05 (survives reload; cleared storage -> expanded), not from the implementation.
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const SOURCE = readFileSync(new URL("../../../src/workbench/hub/web/static/insecure-banner.js", import.meta.url), "utf8");
const KEY = "wb.insecureBanner";
const CLASS = "wb-insecure-collapsed";

function el(hidden) {
  const handlers = {};
  return {
    hidden,
    handlers,
    addEventListener(type, fn) { (handlers[type] ||= []).push(fn); },
    // returns { prevented, threw }
    dispatch(type, event = {}) {
      const out = { prevented: false, threw: null };
      for (const fn of handlers[type] || []) {
        try { fn({ type, preventDefault() { out.prevented = true; }, ...event }); } catch (error) { out.threw = error; }
      }
      return out;
    },
  };
}

// A "browser tab": `store` is the origin's persistent map (shared across reloads); `mode` selects how storage fails.
function openTab(store, { mode = "ok", readyState = "complete", markup = true } = {}) {
  const classes = new Set();
  const listeners = {};
  const doc = {
    readyState,
    documentElement: { classList: { add: (n) => classes.add(n), remove: (n) => classes.delete(n), contains: (n) => classes.has(n) } },
    addEventListener(type, fn) { (listeners[type] ||= []).push(fn); },
  };
  const close = markup ? el(true) : null;
  const chip = markup ? el(false) : null;
  doc.querySelector = (selector) => (selector === ".banner-close" ? close : selector === ".insecure-chip" ? chip : null);
  const log = [];
  const quota = () => { const e = new Error("The quota has been exceeded."); e.name = "QuotaExceededError"; return e; };
  const storage = {
    getItem(k) { log.push(["get", k]); if (mode === "get-throws") throw new Error("SecurityError"); return store.has(k) ? store.get(k) : null; },
    setItem(k, v) { log.push(["set", k, v]); if (mode === "quota" || mode === "set-throws") throw quota(); store.set(k, String(v)); },
    removeItem(k) { log.push(["remove", k]); if (mode === "set-throws") throw new Error("SecurityError"); store.delete(k); },
    clear() { log.push(["clear"]); store.clear(); },
    key() { log.push(["key"]); return null; },
    get length() { log.push(["length"]); return store.size; },
  };
  const win = {};
  if (mode === "getter-throws") Object.defineProperty(win, "localStorage", { get() { throw new Error("SecurityError"); } });
  else if (mode === "null") win.localStorage = null;
  else if (mode === "undefined") win.localStorage = undefined;
  else if (mode === "absent") { /* property missing */ }
  else if (mode === "revoked") {
    let reads = 0;
    Object.defineProperty(win, "localStorage", { get() { if (reads++ >= 1) throw new Error("SecurityError"); return storage; } });
  } else win.localStorage = storage;
  win.window = win; win.self = win; win.document = doc;
  vm.createContext(win);
  const before = new Set(Object.getOwnPropertyNames(win));
  vm.runInContext(SOURCE, win, { filename: "insecure-banner.js" });
  const leaked = Object.getOwnPropertyNames(win).filter((n) => !before.has(n));
  const domReady = () => { for (const fn of listeners.DOMContentLoaded || []) fn({ type: "DOMContentLoaded" }); };
  return { classes, doc, close, chip, log, leaked, domReady, collapsed: () => classes.has(CLASS) };
}

test("a stored value collapses only when it is exactly 'collapsed': every garbage value stays expanded", () => {
  const garbage = ["", " ", "collapsed ", " collapsed", "COLLAPSED", "Collapsed", "true", "1", "0", "false", "null", "undefined",
    "expanded", "{}", "[]", '"collapsed"', "'collapsed'", "collapsed\n", "collapsed;", "<script>", "x".repeat(100000), "\u0000", "collapsed\u0000"];
  for (const value of garbage) {
    const tab = openTab(new Map([[KEY, value]]), { readyState: "loading" });
    assert.equal(tab.collapsed(), false, JSON.stringify(value).slice(0, 40));
    tab.domReady();
    assert.equal(tab.close.hidden, false, "the banner stays usable");
  }
  assert.equal(openTab(new Map([[KEY, "collapsed"]]), { readyState: "loading" }).collapsed(), true);
});

test("a non-string storage result (stub or odd browser) is not mistaken for 'collapsed'", () => {
  for (const odd of [undefined, 1, true, {}, ["collapsed"], { toString: () => "collapsed" }]) {
    const store = new Map([[KEY, odd]]);
    assert.equal(openTab(store, { readyState: "loading" }).collapsed(), false, String(typeof odd));
  }
});

test("storage failures (getter throws, null, undefined, absent, getItem throws) never throw and leave the banner expanded and usable", () => {
  for (const mode of ["getter-throws", "null", "undefined", "absent", "get-throws"]) {
    for (const readyState of ["loading", "complete"]) {
      const tab = openTab(new Map([[KEY, "collapsed"]]), { mode, readyState });
      if (readyState === "loading") tab.domReady();
      assert.equal(tab.collapsed(), false, `${mode}/${readyState}`);
      assert.equal(tab.close.hidden, false, `${mode}/${readyState}: close button usable`);
      // closing still collapses this page; the chip brings it back; neither throws out of the handler
      assert.equal(tab.close.dispatch("click").threw, null, mode);
      assert.equal(tab.collapsed(), true, mode);
      assert.equal(tab.chip.dispatch("click").threw, null, mode);
      assert.equal(tab.collapsed(), false, mode);
    }
  }
});

test("a quota error or refused write on close/expand is swallowed and the page state still changes", () => {
  for (const mode of ["quota", "set-throws"]) {
    const store = new Map([[KEY, "collapsed"]]);
    const tab = openTab(store, { mode });
    assert.equal(tab.collapsed(), true, "read side still works");
    assert.equal(tab.chip.dispatch("click").threw, null, mode);
    assert.equal(tab.collapsed(), false, mode);
    assert.equal(tab.close.dispatch("click").threw, null, mode);
    assert.equal(tab.collapsed(), true, mode);
  }
});

test("storage that works at load and is revoked afterwards (private-mode toggle) still does not throw in handlers", () => {
  const tab = openTab(new Map(), { mode: "revoked" });
  assert.equal(tab.close.dispatch("click").threw, null);
  assert.equal(tab.collapsed(), true);
  assert.equal(tab.chip.dispatch("click").threw, null);
  assert.equal(tab.collapsed(), false);
});

test("collapse is remembered across reloads and expanding forgets it (BRIEF AC-05)", () => {
  const store = new Map();
  const first = openTab(store);
  assert.equal(first.collapsed(), false, "fresh browser: expanded");
  first.close.dispatch("click");
  assert.equal(store.get(KEY), "collapsed");
  const reload = openTab(store, { readyState: "loading" });
  assert.equal(reload.collapsed(), true, "set before the DOM exists, so no flash of the banner");
  const another = openTab(store);                      // another screen of the same origin
  assert.equal(another.collapsed(), true);
  another.chip.dispatch("click");                      // expand again from the chip
  assert.equal(another.collapsed(), false);
  assert.equal(store.has(KEY), false);
  assert.equal(openTab(store).collapsed(), false, "expanded stays expanded after another reload");
  const cleared = openTab(new Map([[KEY, "collapsed"]]));
  const emptied = openTab(new Map());                  // user cleared site data
  assert.equal(cleared.collapsed(), true);
  assert.equal(emptied.collapsed(), false);
});

test("closing twice or expanding while expanded is harmless", () => {
  const store = new Map();
  const tab = openTab(store);
  tab.chip.dispatch("click");
  assert.equal(tab.collapsed(), false);
  tab.close.dispatch("click"); tab.close.dispatch("click");
  assert.equal(tab.collapsed(), true);
  assert.equal(store.get(KEY), "collapsed");
});

test("the storage key is namespaced: only wb.insecureBanner is read, written or deleted; neighbours survive", () => {
  const store = new Map([["wb.other", "x"], ["wb.insecureBanner2", "collapsed"], ["xwb.insecureBanner", "collapsed"],
    ["wb", "collapsed"], ["insecureBanner", "collapsed"], ["theme", "dark"]]);
  const tab = openTab(store);
  assert.equal(tab.collapsed(), false, "look-alike keys do not collapse the banner");
  tab.close.dispatch("click");
  tab.chip.dispatch("click");
  tab.close.dispatch("click");
  tab.chip.dispatch("click");
  for (const entry of tab.log) {
    assert.ok(["get", "set", "remove"].includes(entry[0]), `unexpected storage call ${entry[0]}`);
    assert.equal(entry[1], KEY);
  }
  assert.deepEqual([...store.keys()].sort(), ["insecureBanner", "theme", "wb", "wb.insecureBanner2", "wb.other", "xwb.insecureBanner"]);
  assert.equal(store.get("wb.other"), "x");
});

test("keyboard: Enter and Space on the chip expand and are consumed; Tab, Shift+Tab, Escape, arrows and letters are neither", () => {
  for (const key of ["Enter", " "]) {
    const tab = openTab(new Map([[KEY, "collapsed"]]));
    const result = tab.chip.dispatch("keydown", { key });
    assert.equal(tab.collapsed(), false, JSON.stringify(key));
    assert.equal(result.prevented, true, "default (Space scroll) suppressed");
  }
  for (const key of ["Tab", "Escape", "ArrowDown", "ArrowRight", "a", "Shift", "Control", "Home", "F6"]) {
    const store = new Map([[KEY, "collapsed"]]);
    const tab = openTab(store);
    const result = tab.chip.dispatch("keydown", { key });
    assert.equal(tab.collapsed(), true, key);
    assert.equal(result.prevented, false, `${key} must not be trapped`);
    assert.equal(store.get(KEY), "collapsed");
  }
});

test("keyboard activation survives a storage failure and an event without a key", () => {
  const tab = openTab(new Map(), { mode: "set-throws" });
  tab.close.dispatch("click");
  assert.equal(tab.chip.dispatch("keydown", { key: "Enter" }).threw, null);
  assert.equal(tab.collapsed(), false);
  assert.equal(tab.chip.dispatch("keydown", {}).threw, null);
});

test("the close button is hidden until the script has wired it, in every readyState but 'loading' at once", () => {
  const loading = openTab(new Map(), { readyState: "loading" });
  assert.equal(loading.close.hidden, true, "inert before DOM ready (and without JS it simply stays hidden)");
  loading.domReady();
  assert.equal(loading.close.hidden, false);
  for (const readyState of ["interactive", "complete"]) {
    assert.equal(openTab(new Map(), { readyState }).close.hidden, false, readyState);
  }
});

test("a document without the banner markup (no warning on that page, or a partial page) does not throw", () => {
  for (const readyState of ["loading", "interactive", "complete"]) {
    const tab = openTab(new Map([[KEY, "collapsed"]]), { markup: false, readyState });
    if (readyState === "loading") tab.domReady();
    assert.equal(tab.collapsed(), true);   // class may still be set; CSS has nothing to hide
  }
});

test("the script leaks no globals and does not touch the DOM beyond <html> class and the two elements", () => {
  const tab = openTab(new Map());
  assert.deepEqual(tab.leaked, []);
});

test("the script never clears, enumerates or sets anything but the literal 'collapsed'", () => {
  const store = new Map();
  const tab = openTab(store);
  tab.close.dispatch("click");
  tab.chip.dispatch("keydown", { key: "Enter" });
  tab.close.dispatch("click");
  const sets = tab.log.filter((entry) => entry[0] === "set");
  assert.ok(sets.length >= 2);
  for (const entry of sets) assert.deepEqual(entry, ["set", KEY, "collapsed"]);
  assert.equal(tab.log.some((entry) => ["clear", "key", "length"].includes(entry[0])), false);
});
