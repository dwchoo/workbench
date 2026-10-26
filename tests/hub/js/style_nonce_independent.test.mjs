// Independent check of static/style-nonce.js and its wiring in board.js (2026-10-26 사용자 결정), derived from the
// contract: installStyleNonce(doc, nonce) gives the nonce only to created <style> elements; every other tag, the
// arguments, `this` and the returned element pass through; a bad nonce installs nothing; installing twice wraps
// once; board.js installs it before any xterm Terminal is made or opened. No browser: fake documents only.
import test from "node:test";
import assert from "node:assert/strict";

import { installStyleNonce } from "../../../src/workbench/hub/web/static/style-nonce.js";

function fakeDocument(extra = {}) {
  const calls = [];
  const made = [];
  const doc = {
    calls,
    made,
    createElement(...args) {
      calls.push({ args, self: this });
      const element = { tagName: String(args[0]).toUpperCase(), nonce: "", attrs: [] };
      element.setAttribute = (name, value) => element.attrs.push([name, value]);
      made.push(element);
      return element;
    },
    createElementNS() { return { nonce: "" }; },
    createTextNode() { return { nonce: "" }; },
    ...extra,
  };
  return doc;
}

test("createElement('STYLE') and any case of style get the exact nonce, nothing else does", () => {
  const doc = fakeDocument();
  installStyleNonce(doc, "AbC_-123");
  for (const name of ["STYLE", "style", "Style", "sTyLe"]) {
    assert.equal(doc.createElement(name).nonce, "AbC_-123", name);
  }
  for (const name of ["div", "span", "script", "link", "textarea", "canvas", "styles", "xstyle", "style-x", "", " style", "style "]) {
    assert.equal(doc.createElement(name).nonce, "", JSON.stringify(name));
  }
});

test("createElement('style', {is}) is still a style element and passes the options object through", () => {
  const doc = fakeDocument();
  installStyleNonce(doc, "n1");
  const options = { is: "x-custom-style" };
  const element = doc.createElement("style", options);
  assert.equal(element.nonce, "n1");
  assert.equal(doc.calls.at(-1).args[0], "style");
  assert.equal(doc.calls.at(-1).args[1], options, "the same options object, not a copy");
  const other = doc.createElement("div", options);
  assert.equal(other.nonce, "");
  assert.equal(doc.calls.at(-1).args[1], options);
});

test("the original createElement runs once per call, with the same arguments, this and return value", () => {
  const doc = fakeDocument();
  installStyleNonce(doc, "n2");
  doc.createElement("style");
  assert.equal(doc.calls.length, 1);
  assert.equal(doc.calls[0].args[0], "style");
  assert.equal(doc.calls[0].args[1], undefined);
  assert.equal(doc.calls[0].self, doc, "this is the document");
  const returned = doc.createElement("div");
  assert.equal(returned, doc.made.at(-1), "the element the original made is the one returned");
  assert.equal(doc.calls.length, 2);
  // a detached call keeps the caller's this (the wrapper must not rebind it)
  const stranger = { marker: true };
  doc.createElement.call(stranger, "style");
  assert.equal(doc.calls.at(-1).self, stranger);
});

test("only the style element's nonce property is touched: no other tag, no attribute writes, other doc methods intact", () => {
  const doc = fakeDocument();
  const { createElementNS, createTextNode } = doc;
  installStyleNonce(doc, "n3");
  assert.equal(doc.createElementNS, createElementNS);
  assert.equal(doc.createTextNode, createTextNode);
  const style = doc.createElement("style");
  assert.deepEqual(style.attrs, [], "set as a property, not by setAttribute");
  for (const element of doc.made) if (element !== style) assert.equal(element.nonce, "");
});

test("installing twice wraps once, keeps the first nonce and calls the original once per createElement", () => {
  const doc = fakeDocument();
  installStyleNonce(doc, "first");
  const wrapped = doc.createElement;
  installStyleNonce(doc, "second");
  installStyleNonce(doc, "third");
  assert.equal(doc.createElement, wrapped);
  assert.equal(doc.createElement("style").nonce, "first");
  assert.equal(doc.calls.length, 1, "no double wrapping: the original ran once");
});

test("an empty, missing or non-string nonce installs nothing", () => {
  const values = ["", undefined, null, 0, 123, false, true, {}, [], ["x"], Symbol("x")];
  for (const nonce of values) {
    const doc = fakeDocument();
    const original = doc.createElement;
    installStyleNonce(doc, nonce);
    assert.equal(doc.createElement, original, String(typeof nonce) + ":" + String(nonce?.toString?.()));
    assert.equal(doc.createElement("style").nonce, "");
  }
  const doc = fakeDocument();
  installStyleNonce(doc);
  assert.equal(doc.createElement("style").nonce, "");
});

test("a failed install does not block a later good one", () => {
  const doc = fakeDocument();
  installStyleNonce(doc, "");
  installStyleNonce(doc, "good");
  assert.equal(doc.createElement("style").nonce, "good");
});

test("a document without a usable createElement is left alone and nothing throws", () => {
  for (const doc of [{}, { createElement: 1 }, { createElement: "x" }, null, undefined, 5, "doc"]) {
    assert.doesNotThrow(() => installStyleNonce(doc, "abc"));
  }
  const plain = {};
  installStyleNonce(plain, "abc");
  assert.deepEqual(Reflect.ownKeys(plain), []);
});

test("the install marker is not enumerable and nothing leaks onto the document's visible keys", () => {
  const doc = fakeDocument();
  const before = Object.keys(doc).sort();
  installStyleNonce(doc, "abc");
  assert.deepEqual(Object.keys(doc).sort(), before);
  assert.deepEqual(JSON.parse(JSON.stringify({ k: Object.keys(doc).length })), { k: before.length });
});

test("two documents keep independent nonces (no module-level state)", () => {
  const one = fakeDocument();
  const two = fakeDocument();
  installStyleNonce(one, "ONE");
  installStyleNonce(two, "TWO");
  assert.equal(one.createElement("style").nonce, "ONE");
  assert.equal(two.createElement("style").nonce, "TWO");
  const three = fakeDocument();
  assert.equal(three.createElement("style").nonce, "", "an uninstalled document is untouched");
});

test("a createElement that throws still throws, and the failure is not swallowed", () => {
  const doc = {
    createElement() { throw new TypeError("invalid tag"); },
  };
  installStyleNonce(doc, "abc");
  assert.throws(() => doc.createElement("<bad>"), TypeError);
});

// --- board.js wiring: the nonce from <meta name="style-nonce"> is wired before any Terminal exists -------------

test("board.js wires the meta nonce before the first Terminal is constructed and before any open()", async () => {
  const log = [];
  const elementCache = new Map();
  const element = (id) => {
    if (!elementCache.has(id)) {
      elementCache.set(id, {
        id, textContent: "", hidden: false, dataset: {}, attrs: {},
        classList: { toggle() {}, contains: () => false },
        setAttribute() {}, addEventListener() {},
      });
    }
    return elementCache.get(id);
  };
  const originalCreateElement = (tag) => ({ tagName: String(tag).toUpperCase(), nonce: "" });
  const doc = {
    getElementById: element,
    querySelector: (selector) => (selector.includes("style-nonce") ? { content: "meta-N" } : selector.includes("csrf") ? { content: "csrf" } : null),
    createElement: originalCreateElement,
    addEventListener() {},
    execCommand() { return false; },
  };
  globalThis.document = doc;
  class T {
    constructor() {
      this.rows = 30; this.cols = 100; this.options = {};
      log.push(["construct", doc.createElement !== originalCreateElement, doc.createElement("style").nonce]);
    }
    attachCustomKeyEventHandler() {}
    hasSelection() { return false; }
    loadAddon() {}
    open() { log.push(["open", doc.createElement("style").nonce, doc.createElement("div").nonce]); }
    focus() {}
    reset() {}
    write() {}
    onData() {}
    onBinary() {}
    onResize() {}
  }
  globalThis.Terminal = T;
  globalThis.FitAddon = { FitAddon: class { fit() {} } };
  Object.defineProperty(globalThis, "navigator", { value: { platform: "Linux x86_64", userAgent: "t" }, configurable: true });
  globalThis.isSecureContext = false;
  globalThis.window = { addEventListener() {} };
  globalThis.location = { protocol: "http:", host: "127.0.0.1:1" };
  globalThis.WebSocket = class { static OPEN = 1; constructor() { this.readyState = 1; } send() {} close() {} };
  globalThis.requestAnimationFrame = (cb) => cb();
  globalThis.setTimeout = () => 0;
  globalThis.clearTimeout = () => {};
  await import("../../../src/workbench/hub/web/static/board.js");
  assert.ok(log.filter((item) => item[0] === "construct").length >= 1, "terminals were made");
  for (const [kind, wrapped, nonce] of log.filter((item) => item[0] === "construct")) {
    assert.equal(wrapped, true, "the wrapper is installed before the first Terminal is constructed");
    assert.equal(nonce, "meta-N");
  }
  for (const [kind, style, div] of log.filter((item) => item[0] === "open")) {
    assert.equal(style, "meta-N", "style elements created during open() carry the nonce");
    assert.equal(div, "", "other tags do not");
  }
  assert.ok(log.some((item) => item[0] === "open"));
  const firstOpen = log.findIndex((item) => item[0] === "open");
  assert.ok(log.slice(0, firstOpen).some((item) => item[0] === "construct"));
});
