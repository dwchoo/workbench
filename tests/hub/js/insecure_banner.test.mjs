// WBX-A3 / seam S8: static/insecure-banner.js against stub browser globals (no browser). The script is a synchronous
// <head> script: a stored localStorage "wb.insecureBanner" = "collapsed" puts class "wb-insecure-collapsed" on <html>
// right away (no flash); the close button (hidden in the markup) is unhidden once the DOM exists and collapses +
// stores; clicking the header chip expands + deletes the stored value. A storage that throws leaves the banner
// expanded. Expected values come from the web-board-ux SPEC "평문 HTTP 배너 접기" bullets. The real browser (refresh,
// cleared storage, CSS) is the user's Chrome check (S7).
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const SOURCE = readFileSync(new URL("../../../src/workbench/hub/web/static/insecure-banner.js", import.meta.url), "utf8");
const KEY = "wb.insecureBanner";
const CLASS = "wb-insecure-collapsed";

function element(extra = {}) {
  const listeners = {};
  return {
    hidden: true,
    listeners,
    addEventListener(type, handler) { (listeners[type] ||= []).push(handler); },
    fire(type, event = {}) {
      let prevented = false;
      for (const handler of listeners[type] || []) handler({ type, preventDefault() { prevented = true; }, ...event });
      return prevented;
    },
    ...extra,
  };
}

// storage: "absent" (no localStorage property), "throws" (the getter throws), "fails-get" / "fails-set" (methods throw),
// or an object of stored values.
function load({ stored = {}, storage = "ok", readyState = "complete", withButton = true, withChip = true } = {}) {
  const classes = new Set();
  const doc = {
    readyState,
    listeners: {},
    documentElement: { classList: {
      add(name) { classes.add(name); }, remove(name) { classes.delete(name); }, contains: (name) => classes.has(name),
    } },
    addEventListener(type, handler) { (doc.listeners[type] ||= []).push(handler); },
  };
  const button = withButton ? element() : null;
  const chip = withChip ? element({ hidden: false }) : null;
  doc.querySelector = (selector) => (selector === ".banner-close" ? button : selector === ".insecure-chip" ? chip : null);
  const values = new Map(Object.entries(stored));
  const calls = [];
  const local = {
    getItem(key) { calls.push(["get", key]); if (storage === "fails-get") throw new Error("denied"); return values.has(key) ? values.get(key) : null; },
    setItem(key, value) { calls.push(["set", key, value]); if (storage === "fails-set") throw new Error("quota"); values.set(key, String(value)); },
    removeItem(key) { calls.push(["remove", key]); if (storage === "fails-set") throw new Error("denied"); values.delete(key); },
  };
  const window = { document: doc };
  if (storage === "throws") Object.defineProperty(window, "localStorage", { get() { throw new Error("SecurityError"); } });
  else if (storage !== "absent") window.localStorage = local;
  window.window = window;
  window.self = window;
  doc.defaultView = window;
  window.document = doc;
  vm.createContext(window);
  vm.runInContext(SOURCE, window, { filename: "insecure-banner.js" });
  const ready = () => { for (const handler of doc.listeners.DOMContentLoaded || []) handler({ type: "DOMContentLoaded" }); };
  return { classes, doc, button, chip, values, calls, ready };
}

test("a stored 'collapsed' puts the class on <html> synchronously, before the DOM is ready", () => {
  const page = load({ stored: { [KEY]: "collapsed" }, readyState: "loading" });
  assert.equal(page.classes.has(CLASS), true);
});

test("nothing stored, or any other stored value, leaves the banner expanded", () => {
  for (const stored of [{}, { [KEY]: "" }, { [KEY]: "expanded" }, { [KEY]: "Collapsed" }, { other: "collapsed" }]) {
    const page = load({ stored, readyState: "loading" });
    assert.equal(page.classes.has(CLASS), false, JSON.stringify(stored));
  }
});

test("a storage that throws (getter, getItem) or is absent leaves the banner expanded and never throws", () => {
  for (const storage of ["throws", "fails-get", "absent"]) {
    const page = load({ storage, readyState: "loading" });
    assert.equal(page.classes.has(CLASS), false, storage);
    page.ready();
    assert.equal(page.button.hidden, false, `${storage}: the close button still works`);
  }
});

test("the close button is unhidden when the DOM is ready, and stays hidden before that", () => {
  const page = load({ readyState: "loading" });
  assert.equal(page.button.hidden, true);
  page.ready();
  assert.equal(page.button.hidden, false);
});

test("a script that runs after the DOM is parsed wires the buttons immediately", () => {
  const page = load({ readyState: "complete" });
  assert.equal(page.button.hidden, false);
  assert.equal((page.doc.listeners.DOMContentLoaded || []).length, 0);
});

test("closing stores 'collapsed' under wb.insecureBanner and adds the class", () => {
  const page = load({ readyState: "complete" });
  page.button.fire("click");
  assert.equal(page.classes.has(CLASS), true);
  assert.equal(page.values.get(KEY), "collapsed");
});

test("clicking the chip expands: the class goes and the stored value is deleted", () => {
  const page = load({ stored: { [KEY]: "collapsed" }, readyState: "complete" });
  assert.equal(page.classes.has(CLASS), true);
  page.chip.fire("click");
  assert.equal(page.classes.has(CLASS), false);
  assert.equal(page.values.has(KEY), false);
});

test("closing, expanding and closing again round-trips", () => {
  const page = load({ readyState: "complete" });
  page.button.fire("click");
  page.chip.fire("click");
  assert.equal(page.classes.has(CLASS), false);
  assert.equal(page.values.has(KEY), false);
  page.button.fire("click");
  assert.equal(page.classes.has(CLASS), true);
  assert.equal(page.values.get(KEY), "collapsed");
});

test("the chip is keyboard reachable: Enter and Space expand, other keys do nothing", () => {
  const page = load({ stored: { [KEY]: "collapsed" }, readyState: "complete" });
  page.chip.fire("keydown", { key: "Tab" });
  assert.equal(page.classes.has(CLASS), true);
  const prevented = page.chip.fire("keydown", { key: " " });
  assert.equal(prevented, true, "Space must not scroll the page");
  assert.equal(page.classes.has(CLASS), false);
  page.button.fire("click");
  page.chip.fire("keydown", { key: "Enter" });
  assert.equal(page.classes.has(CLASS), false);
});

test("closing with a storage that refuses writes still collapses this page and does not throw", () => {
  const page = load({ storage: "fails-set", readyState: "complete" });
  page.button.fire("click");
  assert.equal(page.classes.has(CLASS), true);
  page.chip.fire("click");
  assert.equal(page.classes.has(CLASS), false);
});

test("a throwing or absent storage: closing collapses this page only, expanding works", () => {
  for (const storage of ["throws", "absent"]) {
    const page = load({ storage, readyState: "complete" });
    page.button.fire("click");
    assert.equal(page.classes.has(CLASS), true, storage);
    page.chip.fire("click");
    assert.equal(page.classes.has(CLASS), false, storage);
  }
});

test("a page without the button or the chip does not throw", () => {
  load({ withButton: false, withChip: false, readyState: "complete" });
  load({ withButton: false, withChip: false, readyState: "loading" }).ready();
});

test("the script only touches the one storage key", () => {
  const page = load({ readyState: "complete" });
  page.button.fire("click");
  page.chip.fire("click");
  for (const [, key] of page.calls) assert.equal(key, KEY);
});
