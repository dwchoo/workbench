// 2026-10-26 사용자 결정: /board's CSP allows <style> only with the response's nonce. installStyleNonce
// (static/style-nonce.js) wraps document.createElement so the <style> elements xterm.js creates carry it.
// A fake document stands in for the browser (no jsdom): only createElement matters here.
import test from "node:test";
import assert from "node:assert/strict";

import { installStyleNonce } from "../../../src/workbench/hub/web/static/style-nonce.js";

function fakeDocument() {
  const calls = [];
  const doc = {
    calls,
    createElement(tagName, options) {
      calls.push([tagName, options, this]);
      return { tagName: String(tagName).toUpperCase(), nonce: "" };
    },
  };
  return doc;
}

test("created style elements carry the nonce whatever the tag name's case", () => {
  const doc = fakeDocument();
  installStyleNonce(doc, "N0nce_-abc");
  for (const name of ["style", "STYLE", "Style"]) {
    assert.equal(doc.createElement(name).nonce, "N0nce_-abc", name);
  }
});

test("other tags get no nonce, and the tag name, options and this pass through unchanged", () => {
  const doc = fakeDocument();
  installStyleNonce(doc, "abc");
  const options = { is: "x-thing" };
  for (const name of ["div", "span", "textarea", "canvas", "styles", "xstyle", "script", "link"]) {
    assert.equal(doc.createElement(name).nonce, "", name);
  }
  const element = doc.createElement("div", options);
  assert.equal(element.tagName, "DIV");
  const last = doc.calls.at(-1);
  assert.equal(last[0], "div");
  assert.equal(last[1], options);
  assert.equal(last[2], doc, "the original createElement runs with the document as this");
  doc.createElement("style", options);
  assert.equal(doc.calls.at(-1)[1], options);
});

test("installing twice wraps once and keeps the first nonce", () => {
  const doc = fakeDocument();
  installStyleNonce(doc, "first");
  const wrapped = doc.createElement;
  installStyleNonce(doc, "second");
  assert.equal(doc.createElement, wrapped, "no second wrapper");
  const before = doc.calls.length;
  assert.equal(doc.createElement("style").nonce, "first");
  assert.equal(doc.calls.length, before + 1, "the original createElement ran exactly once");
});

test("an empty or missing nonce installs nothing", () => {
  for (const nonce of ["", undefined, null]) {
    const doc = fakeDocument();
    const original = doc.createElement;
    installStyleNonce(doc, nonce);
    assert.equal(doc.createElement, original, String(nonce));
    assert.equal(doc.createElement("style").nonce, "", String(nonce));
  }
});

test("a document without createElement is left alone", () => {
  const doc = {};
  installStyleNonce(doc, "abc");
  assert.deepEqual(Object.keys(doc), []);
});
