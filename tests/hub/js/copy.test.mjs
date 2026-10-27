// WBX-A4 / seam S8: static/copy.js against stub browser globals (no browser). The copy buttons are `hidden` in the
// markup; the script unhides them once the DOM exists and, on click, copies the text of the element named by
// data-copy-target: navigator.clipboard.writeText (only present in a secure context, so not on a plain-HTTP LAN
// page) -> else, or when it fails, select the text and document.execCommand("copy") -> else leave the text selected
// and say "직접 복사하세요(Ctrl+C)". Nothing may throw out of the click handler. Expected values come from the
// web-board-ux SPEC "Key 발급 결과의 client 설정 안내" copy bullet. The real browser (clipboard permission, selection
// look, plain-HTTP LAN page) is the user's Chrome check (S7).
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const SOURCE = readFileSync(new URL("../../../src/workbench/hub/web/static/copy.js", import.meta.url), "utf8");
const TEXT = "WB_HUB_URL=http://hub.lan:8780\nWB_API_KEY=wbk_fake";
const DONE = "복사했습니다";
const MANUAL = "직접 복사하세요(Ctrl+C)";

// clipboard: "ok" | "absent" | "no-write" (object without writeText) | "rejects" | "throws"
// exec: true | false | "throws"; selection: "ok" | "absent" | "throws"
function load({ clipboard = "ok", exec = true, selection = "ok", withTarget = true, withStatus = true,
                readyState = "complete", buttons = 1 } = {}) {
  const log = [];
  const listeners = {};
  const button = (n) => {
    const handlers = [];
    return {
      hidden: true,
      getAttribute: (name) => (name === "data-copy-target" ? (n ? `t${n}` : "t") : null),
      addEventListener(type, handler) { if (type === "click") handlers.push(handler); },
      click() { return Promise.all(handlers.map((handler) => handler({ type: "click", preventDefault() {} }))); },
    };
  };
  const list = Array.from({ length: buttons }, (_, i) => button(i === 0 ? 0 : i));
  const target = { textContent: TEXT };
  const status = { textContent: "" };
  const doc = {
    readyState,
    addEventListener(type, handler) { (listeners[type] ||= []).push(handler); },
    querySelectorAll: () => list,
    getElementById(id) {
      if (id === "t" && withTarget) return target;
      if (id === "t-status" && withStatus) return status;
      return null;
    },
    createRange: () => ({ selectNodeContents(node) { log.push(["range", node === target]); } }),
    execCommand(name) {
      log.push(["exec", name]);
      if (exec === "throws") throw new Error("denied");
      return exec;
    },
  };
  const sel = {
    removeAllRanges() { log.push(["clear"]); },
    addRange() { log.push(["select"]); },
  };
  const window = { document: doc };
  if (selection === "ok") window.getSelection = () => sel;
  else if (selection === "throws") window.getSelection = () => { throw new Error("nope"); };
  const writeText = (text) => {
    log.push(["write", text]);
    if (clipboard === "throws") throw new Error("sync failure");
    return clipboard === "rejects" ? Promise.reject(new Error("NotAllowedError")) : Promise.resolve();
  };
  const nav = {};
  if (clipboard === "no-write") nav.clipboard = {};
  else if (clipboard !== "absent") nav.clipboard = { writeText };
  window.navigator = nav;
  window.window = window;
  window.self = window;
  doc.defaultView = window;
  vm.createContext(window);
  vm.runInContext(SOURCE, window, { filename: "copy.js" });
  const ready = () => { for (const handler of listeners.DOMContentLoaded || []) handler({ type: "DOMContentLoaded" }); };
  return { buttons: list, button: list[0], target, status, log, ready, listeners };
}

// The text is selected (the target's contents were ranged, then the range was added) before execCommand runs, and
// the selection is left in place afterwards.
function assertSelectedThenCopied(log, { writeFirst = false } = {}) {
  const kinds = log.map((entry) => entry[0]);
  assert.deepEqual(kinds.filter((kind) => kind === "write"), writeFirst ? ["write"] : []);
  assert.ok(log.some((entry) => entry[0] === "range" && entry[1] === true), "the target's contents were ranged");
  const select = kinds.lastIndexOf("select");
  const exec = kinds.indexOf("exec");
  assert.ok(select >= 0 && select < exec, "selected before execCommand");
  assert.deepEqual(log[exec], ["exec", "copy"]);
  assert.equal(kinds.slice(select + 1).includes("clear"), false, "the selection stays after the copy");
  assert.equal(exec, kinds.length - 1, "execCommand is the last step");
}

test("the buttons are unhidden once the DOM exists (immediately when it already does)", () => {
  const page = load({ buttons: 2 });
  assert.deepEqual(page.buttons.map((b) => b.hidden), [false, false]);
  const loading = load({ readyState: "loading" });
  assert.equal(loading.button.hidden, true, "still hidden while the DOM is loading");
  loading.ready();
  assert.equal(loading.button.hidden, false);
});

test("with a clipboard API the text is written and nothing else is tried", async () => {
  const page = load();
  await page.button.click();
  assert.deepEqual(page.log, [["write", TEXT]]);
  assert.equal(page.status.textContent, DONE);
});

test("without navigator.clipboard (plain HTTP) the text is selected and execCommand('copy') runs after it", async () => {
  const page = load({ clipboard: "absent" });
  await page.button.click();
  assertSelectedThenCopied(page.log);
  assert.equal(page.status.textContent, DONE);
});

test("a clipboard object without writeText falls back the same way", async () => {
  const page = load({ clipboard: "no-write" });
  await page.button.click();
  assertSelectedThenCopied(page.log);
  assert.equal(page.status.textContent, DONE);
});

test("a rejected writeText falls back to execCommand", async () => {
  const page = load({ clipboard: "rejects" });
  await page.button.click();
  assert.deepEqual(page.log[0], ["write", TEXT], "the clipboard API is tried first");
  assertSelectedThenCopied(page.log, { writeFirst: true });
  assert.equal(page.status.textContent, DONE);
});

test("a writeText that throws synchronously falls back to execCommand", async () => {
  const page = load({ clipboard: "throws" });
  await page.button.click();
  assertSelectedThenCopied(page.log, { writeFirst: true });
  assert.equal(page.status.textContent, DONE);
});

test("when execCommand says no, the text stays selected and the manual hint shows", async () => {
  const page = load({ clipboard: "absent", exec: false });
  await page.button.click();
  assertSelectedThenCopied(page.log);
  assert.equal(page.status.textContent, MANUAL);
});

test("when execCommand throws, the manual hint shows", async () => {
  const page = load({ clipboard: "absent", exec: "throws" });
  await page.button.click();
  assert.equal(page.status.textContent, MANUAL);
});

test("when nothing works (no clipboard, no selection API, execCommand false) the click still ends in the hint", async () => {
  const page = load({ clipboard: "absent", selection: "absent", exec: false });
  await page.button.click();
  assert.equal(page.status.textContent, MANUAL);
  const throwing = load({ clipboard: "rejects", selection: "throws", exec: "throws" });
  await throwing.button.click();
  assert.equal(throwing.status.textContent, MANUAL);
});

test("a missing target or status element never throws and never reaches the clipboard", async () => {
  const noTarget = load({ withTarget: false });
  await noTarget.button.click();
  assert.deepEqual(noTarget.log, []);
  const noStatus = load({ withStatus: false });
  await noStatus.button.click();
  assert.deepEqual(noStatus.log, [["write", TEXT]]);
});

test("a page without copy buttons is left alone", () => {
  const page = load({ buttons: 0 });
  assert.equal(page.buttons.length, 0);
});

test("execCommand 'succeeding' with nothing selected is not a copy: the manual hint shows", async () => {
  const page = load({ clipboard: "absent", selection: "absent", exec: true });
  await page.button.click();
  assert.equal(page.status.textContent, MANUAL);
});
