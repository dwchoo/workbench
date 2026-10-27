// WBX-A4 independent verification / seam S8: static/copy.js against a small fake DOM and stub browser globals.
// Expected behaviour is the web-board-ux SPEC copy bullet (clipboard -> select + execCommand -> manual guidance),
// BRIEF WBX-D04/D21, and the page rule "no inline script, no key leaves the page". The fake DOM here is written
// independently of copy.test.mjs. The real browser is the user's Chrome check (S7).
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const SOURCE = readFileSync(new URL("../../../src/workbench/hub/web/static/copy.js", import.meta.url), "utf8");
const KEY = "wbk_abcdefghij_" + "Z".repeat(43);
const TRICKY = `WB_HUB_URL=http://hub.lan:8780\nWB_API_KEY=${KEY}\n<b>&amp; "q" 'x' é한\u{1F600}  `;
const MANUAL = "직접 복사하세요(Ctrl+C)";

const tick = () => new Promise((resolve) => setTimeout(resolve, 0));
const settle = async () => { for (let i = 0; i < 5; i += 1) await tick(); };

// opts: clipboard "ok" | "absent" | "reject" | "throw" | "pending"; exec true | false | "throw";
//       selection "ok" | "no-api" | "range-throws" | "add-throws"; ready "complete" | "loading"
function page({ clipboard = "ok", exec = true, selection = "ok", ready = "complete", targetText = TRICKY,
                hasTarget = true, hasStatus = true, secure } = {}) {
  const calls = [];
  const consoleCalls = [];
  const trap = (name) => () => { calls.push(["FORBIDDEN", name]); throw new Error(`${name} must not be used`); };
  const status = { textContent: "" };
  const target = {
    textContent: targetText,
    get innerHTML() { calls.push(["FORBIDDEN", "innerHTML"]); return "<escaped>"; },
    get innerText() { calls.push(["FORBIDDEN", "innerText"]); return "different"; },
    get outerHTML() { calls.push(["FORBIDDEN", "outerHTML"]); return "<pre>"; },
  };
  const other = { textContent: "OTHER" };
  const handlers = new Map();
  const make = (id) => {
    let hidden = true;
    const clicks = [];
    return {
      get hidden() { return hidden; },
      set hidden(value) { hidden = Boolean(value); },
      removeAttribute(name) { if (name === "hidden") hidden = false; },
      setAttribute() {},
      getAttribute: (name) => (name === "data-copy-target" ? id : null),
      addEventListener(type, handler) { if (type === "click") clicks.push(handler); },
      async click() {
        const results = [];
        for (const handler of clicks) results.push(handler({ type: "click", preventDefault() {}, stopPropagation() {} }));
        await Promise.all(results);
      },
    };
  };
  const buttons = [make("t"), make("o")];
  let pending;
  const clipboardApi = {
    writeText(text) {
      calls.push(["write", text]);
      if (clipboard === "throw") throw new Error("SecurityError");
      if (clipboard === "reject") return Promise.reject(new DOMException("denied", "NotAllowedError"));
      if (clipboard === "pending") return new Promise((resolve) => { pending = resolve; });
      return Promise.resolve();
    },
  };
  const range = { selectNodeContents(node) { calls.push(["range", node === target ? "target" : node === other ? "other" : "?"]); } };
  const sel = { removeAllRanges() { calls.push(["clear"]); }, addRange() { calls.push(["add"]); } };
  const document = {
    readyState: ready,
    addEventListener(type, handler) { handlers.set(type, handler); },
    querySelectorAll: () => buttons,
    getElementById(id) {
      if (id === "t" && hasTarget) return target;
      if (id === "o") return other;
      if (id === "t-status" && hasStatus) return status;
      return null;
    },
    createElement: trap("createElement"),
    write: trap("document.write"),
    createRange() {
      if (selection === "range-throws") throw new Error("no range");
      return range;
    },
    execCommand(name) {
      calls.push(["exec", name]);
      if (exec === "throw") throw new Error("denied");
      return exec;
    },
    get cookie() { calls.push(["FORBIDDEN", "cookie"]); return ""; },
  };
  const spy = (level) => (...args) => consoleCalls.push([level, args.map(String).join(" ")]);
  const win = {
    document,
    console: { log: spy("log"), info: spy("info"), warn: spy("warn"), error: spy("error"), debug: spy("debug"), trace: spy("trace") },
    navigator: clipboard === "absent" ? {} : { clipboard: clipboardApi },
    fetch: trap("fetch"), XMLHttpRequest: trap("XMLHttpRequest"),
    localStorage: new Proxy({}, { get: trap("localStorage"), set: trap("localStorage") }),
    sessionStorage: new Proxy({}, { get: trap("sessionStorage"), set: trap("sessionStorage") }),
    eval: trap("eval"), Function: trap("Function"),
    setTimeout, clearTimeout, Promise, DOMException,
  };
  if (secure !== undefined) win.isSecureContext = secure;
  if (selection === "ok") win.getSelection = () => sel;
  else if (selection === "add-throws") win.getSelection = () => ({ removeAllRanges() {}, addRange() { throw new Error("no"); } });
  else if (selection === "range-throws") win.getSelection = () => sel;
  win.window = win; win.self = win; win.globalThis = win;
  win.navigator.sendBeacon = trap("sendBeacon");
  vm.createContext(win);
  vm.runInContext(SOURCE, win, { filename: "copy.js" });
  const domReady = () => { const handler = handlers.get("DOMContentLoaded"); if (handler) handler({ type: "DOMContentLoaded" }); };
  return { buttons, status, target, calls, consoleCalls, domReady, resolveClipboard: () => pending(), handlers };
}

const names = (calls) => calls.map((c) => c[0]);
function noForbidden(p) { assert.deepEqual(p.calls.filter((c) => c[0] === "FORBIDDEN"), []); }
function silent(p) {
  assert.deepEqual(p.consoleCalls, []);
  for (const [, text] of p.consoleCalls) assert.ok(!text.includes(KEY));
}

// An unhandled rejection anywhere in this file fails it, which is the "never throws" requirement for async paths.
const unhandled = [];
process.on("unhandledRejection", (reason) => unhandled.push(reason));
test.after(async () => { await settle(); assert.deepEqual(unhandled.map(String), []); });

test("buttons start hidden, unhide at once when the document is ready", async () => {
  const p = page();
  assert.equal(p.buttons.every((b) => b.hidden === false), true);
  noForbidden(p);
});

test("buttons unhide only after DOMContentLoaded while the document is loading", async () => {
  const p = page({ ready: "loading" });
  assert.equal(p.buttons.some((b) => b.hidden === false), false);
  assert.equal(typeof p.handlers.get("DOMContentLoaded"), "function");
  p.domReady();
  assert.equal(p.buttons.every((b) => b.hidden === false), true);
});

test("clipboard success copies the exact textContent (no HTML, no trimming) and uses no fallback", async () => {
  const p = page();
  await p.buttons[0].click();
  assert.deepEqual(p.calls.filter((c) => c[0] === "write"), [["write", TRICKY]]);
  assert.equal(names(p.calls).includes("exec"), false);
  assert.equal(p.status.textContent, "복사했습니다");
  noForbidden(p); silent(p);
});

test("the status never reports success before the clipboard promise settles", async () => {
  const p = page({ clipboard: "pending" });
  const clicked = p.buttons[0].click();
  await settle();
  assert.equal(p.status.textContent, "");
  p.resolveClipboard();
  await clicked;
  assert.equal(p.status.textContent, "복사했습니다");
});

test("clipboard reject falls back to select + execCommand('copy') on the right element", async () => {
  const p = page({ clipboard: "reject" });
  await p.buttons[0].click();
  const order = names(p.calls);
  assert.deepEqual(order.filter((n) => n !== "clear" && n !== "add" && n !== "range"), ["write", "exec"]);
  assert.ok(order.indexOf("range") < order.indexOf("exec"));
  assert.ok(order.indexOf("add") < order.indexOf("exec"));
  assert.deepEqual(p.calls.filter((c) => c[0] === "range"), [["range", "target"]]);
  assert.deepEqual(p.calls.filter((c) => c[0] === "exec"), [["exec", "copy"]]);
  assert.equal(p.status.textContent, "복사했습니다");
  noForbidden(p); silent(p);
});

test("clipboard writeText throwing synchronously is treated like a rejection", async () => {
  const p = page({ clipboard: "throw" });
  await p.buttons[0].click();
  assert.deepEqual(p.calls.filter((c) => c[0] === "exec"), [["exec", "copy"]]);
  assert.equal(p.status.textContent, "복사했습니다");
  silent(p);
});

for (const secure of [undefined, false]) {
  test(`no navigator.clipboard (plain-HTTP LAN page, isSecureContext=${secure}) goes straight to the fallback`, async () => {
    const p = page({ clipboard: "absent", secure });
    await p.buttons[0].click();
    assert.equal(names(p.calls).includes("write"), false);
    assert.deepEqual(p.calls.filter((c) => c[0] === "exec"), [["exec", "copy"]]);
    assert.equal(p.status.textContent, "복사했습니다");
    silent(p);
  });
}

test("execCommand returning false leaves the text selected and shows the manual guidance", async () => {
  for (const clipboard of ["absent", "reject"]) {
    const p = page({ clipboard, exec: false });
    await p.buttons[0].click();
    assert.equal(p.status.textContent, MANUAL, clipboard);
    const order = names(p.calls);
    assert.equal(order.lastIndexOf("add") > -1, true);
    // nothing clears the selection after execCommand ran
    assert.equal(order.slice(order.indexOf("exec") + 1).includes("clear"), false);
    silent(p);
  }
});

test("execCommand throwing shows the manual guidance and the click handler does not throw", async () => {
  const p = page({ clipboard: "absent", exec: "throw" });
  await p.buttons[0].click();
  assert.equal(p.status.textContent, MANUAL);
  silent(p);
});

for (const selection of ["no-api", "range-throws", "add-throws"]) {
  test(`selection API '${selection}' degrades to the manual guidance without throwing`, async () => {
    const p = page({ clipboard: "absent", selection });
    await p.buttons[0].click();
    assert.equal(p.status.textContent, MANUAL);
    silent(p);
  });
}

test("all methods failing at once still ends in the manual guidance", async () => {
  const p = page({ clipboard: "reject", exec: "throw", selection: "range-throws" });
  await p.buttons[0].click();
  assert.equal(p.status.textContent, MANUAL);
  silent(p);
});

test("a missing target element or status line never throws and copies nothing", async () => {
  const noTarget = page({ hasTarget: false });
  await noTarget.buttons[0].click();
  assert.equal(names(noTarget.calls).includes("write"), false);
  assert.equal(names(noTarget.calls).includes("exec"), false);
  const noStatus = page({ hasStatus: false });
  await noStatus.buttons[0].click();
  assert.deepEqual(noStatus.calls.filter((c) => c[0] === "write"), [["write", TRICKY]]);
  const noStatusFail = page({ hasStatus: false, clipboard: "absent", exec: false });
  await noStatusFail.buttons[0].click();
  silent(noTarget); silent(noStatus); silent(noStatusFail);
});

test("each button copies its own target, and a second click copies the current text", async () => {
  const p = page();
  await p.buttons[1].click();
  assert.deepEqual(p.calls.filter((c) => c[0] === "write"), [["write", "OTHER"]]);
  p.calls.length = 0;
  p.target.textContent = "changed";
  await p.buttons[0].click();
  await p.buttons[0].click();
  assert.deepEqual(p.calls.filter((c) => c[0] === "write"), [["write", "changed"], ["write", "changed"]]);
});

test("the key is never logged, stored, sent or put into the status line (every outcome)", async () => {
  for (const clipboard of ["ok", "reject", "throw", "absent"]) {
    for (const exec of [true, false, "throw"]) {
      const p = page({ clipboard, exec });
      await p.buttons[0].click();
      assert.deepEqual(p.consoleCalls, [], `${clipboard}/${exec}`);
      assert.equal(p.status.textContent.includes(KEY), false);
      noForbidden(p);
    }
  }
});

test("the source has no console use, dynamic code, HTML sinks, network, storage or inline script creation", () => {
  const code = SOURCE.replace(/\/\*[\s\S]*?\*\//g, "").replace(/\/\/.*$/gm, "");
  for (const pattern of [/\bconsole\b/, /\beval\s*\(/, /\bnew\s+Function\b/, /\binnerHTML\b/, /\bouterHTML\b/,
                         /\binsertAdjacentHTML\b/, /\bdocument\.write\b/, /\bfetch\s*\(/, /XMLHttpRequest/,
                         /sendBeacon/, /\blocalStorage\b/, /\bsessionStorage\b/, /\bdocument\.cookie\b/,
                         /createElement\s*\(\s*["']script/, /\bimport\s*\(/, /\bWebSocket\b/, /\balert\s*\(/]) {
    assert.doesNotMatch(code, pattern, String(pattern));
  }
});

test("loading the script has no side effect besides wiring (no clicks, no copies)", () => {
  const p = page();
  assert.deepEqual(p.calls, []);
});
