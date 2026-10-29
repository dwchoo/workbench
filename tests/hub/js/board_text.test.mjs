// Korean (Hangul) text and clipboard on the Board page (static/board-state.js, seam T10; no DOM, no network).
// Expected values come from the UTF-8 encoding of the test's own strings (TextEncoder / Buffer), SPEC
// "입력 한도" (a WebSocket input frame carries at most 64 KiB) and the frame layout of web-board-ux SPEC s4
// "WebSocket v2" (a binary frame is the bare payload; v1's channel byte and epoch are gone), not from the module.
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import * as board from "../../../src/workbench/hub/web/static/board-state.js";

const FRAME_LIMIT = 64 * 1024;
const ATTACHED = {
  t: "attached", v: 2, terminal: { id: "s-1", kind: "shell", name: "shell-1", state: "running" }, size: [30, 100],
  clients: 1, heartbeat: 10,
};

function attachedTab() {
  let state = board.initialState({ csrf: "c" });
  const send = (event) => {
    const result = board.step(state, event);
    state = result.state;
    return result.effects;
  };
  send({ type: "start", size: [30, 100] });
  send({ type: "opened", conn: 1 });
  send({ type: "message", conn: 1, message: ATTACHED });
  return { send, get state() { return state; } };
}

const sent = (effects) => effects.filter((item) => item.type === "send_binary").map((item) => item.bytes);
const payloadOf = (bytes) => bytes; // v2: no header
const concat = (parts) => Buffer.concat(parts.map((part) => Buffer.from(part)));
// A UTF-8 decode that throws on any invalid or cut sequence.
const strictDecode = (bytes) => new TextDecoder("utf-8", { fatal: true }).decode(bytes);

test("typed Hangul syllables, compatibility jamo, conjoining jamo and emoji reach the frame as exact UTF-8", () => {
  const tab = attachedTab();
  const text = "안녕하세요 ㄱㄴㅎㅏ \u1112\u1161\u11ab 👍🏽 한글";
  const frames = sent(tab.send({ type: "input", bytes: new TextEncoder().encode(text) }));
  assert.equal(frames.length, 1);
  assert.deepEqual(Buffer.from(payloadOf(frames[0])), Buffer.from(text, "utf8"));
});

test("a 1 MB Hangul, emoji and ASCII paste becomes in-order frames of at most 64 KiB, each valid UTF-8", () => {
  const tab = attachedTab();
  const unit = "가나다라마바사 abc 😀 \r";
  const text = unit.repeat(Math.ceil((1 << 20) / Buffer.byteLength(unit)));
  const bytes = new TextEncoder().encode(text);
  const frames = sent(tab.send({ type: "input", bytes }));
  assert.ok(frames.length >= 16, `${frames.length} frames`);
  for (const frame of frames) {
    assert.ok(payloadOf(frame).length <= FRAME_LIMIT, `payload ${payloadOf(frame).length}`);
    assert.ok(payloadOf(frame).length > FRAME_LIMIT - 4, "frames are filled up to the limit");
    strictDecode(payloadOf(frame)); // a cut code point would throw here
  }
  assert.deepEqual(concat(frames.map(payloadOf)), Buffer.from(text, "utf8"));
});

test("a split never falls inside a code point, at every offset of a 3- and 4-byte sequence at the limit", () => {
  for (const wide of ["한", "😀"]) {
    for (let pad = 0; pad < 4; pad += 1) {
      const text = "a".repeat(FRAME_LIMIT - pad) + wide + "끝";
      const tab = attachedTab();
      const frames = sent(tab.send({ type: "input", bytes: new TextEncoder().encode(text) }));
      assert.equal(frames.length, 2, `${wide} pad ${pad}`);
      assert.equal(strictDecode(payloadOf(frames[0])) + strictDecode(payloadOf(frames[1])), text);
    }
  }
});

test("one input beyond the Hub's 4 MiB PTY input queue is refused whole with a notice, never sent in part", () => {
  const tab = attachedTab();
  const fits = new Uint8Array(4 << 20).fill(0x61);
  assert.equal(sent(tab.send({ type: "input", bytes: fits })).length, 64);
  const effects = tab.send({ type: "input", bytes: new Uint8Array((4 << 20) + 1).fill(0x61) });
  assert.deepEqual(effects.map((item) => item.type), ["notice"], "nothing is sent");
  assert.deepEqual(effects[0], { type: "notice", reason: "too_large" }); // v1 also named the channel
});

// Clipboard and IME keys (terminalKey): "copy" = the page copies the selection itself (preventDefault),
// "native" = xterm leaves the key to the browser (its paste event reaches xterm's paste handler),
// "pass" = xterm handles the key as usual (sends it to the PTY, scrolls, composes).
const key = (k, mods = {}, extra = {}) => ({
  type: "keydown", key: k, code: k.length === 1 ? `Key${k.toUpperCase()}` : k, keyCode: 0,
  ctrlKey: false, shiftKey: false, altKey: false, metaKey: false, isComposing: false, ...mods, ...extra,
});
const linux = (hasSelection) => ({ mac: false, hasSelection });
const mac = (hasSelection) => ({ mac: true, hasSelection });

test("Ctrl+C copies only while text is selected; without a selection it is ^C for the program", () => {
  assert.equal(board.terminalKey(key("c", { ctrlKey: true }), linux(true)), "copy");
  assert.equal(board.terminalKey(key("c", { ctrlKey: true }), linux(false)), "pass");
  assert.equal(board.terminalKey(key("c", { ctrlKey: true, altKey: true }), linux(true)), "pass");
});

test("Ctrl+Shift+C and Ctrl+Insert copy; Cmd+C on a Mac is the browser's own copy", () => {
  assert.equal(board.terminalKey(key("C", { ctrlKey: true, shiftKey: true }), linux(true)), "copy");
  assert.equal(board.terminalKey(key("C", { ctrlKey: true, shiftKey: true }), linux(false)), "copy");
  assert.equal(board.terminalKey(key("Insert", { ctrlKey: true }), linux(true)), "copy");
  assert.equal(board.terminalKey(key("c", { metaKey: true }), mac(true)), "copy");
  assert.equal(board.terminalKey(key("c", { metaKey: true }), mac(false)), "pass");
  assert.equal(board.terminalKey(key("c", { ctrlKey: true }), mac(true)), "pass", "Ctrl+C on a Mac is ^C");
});

test("Ctrl+V, Ctrl+Shift+V, Shift+Insert and Cmd+V paste through the browser, never navigator.clipboard", () => {
  assert.equal(board.terminalKey(key("v", { ctrlKey: true }), linux(false)), "native");
  assert.equal(board.terminalKey(key("V", { ctrlKey: true, shiftKey: true }), linux(false)), "native");
  assert.equal(board.terminalKey(key("Insert", { shiftKey: true }), linux(false)), "native");
  assert.equal(board.terminalKey(key("v", { metaKey: true }), mac(false)), "native");
  assert.equal(board.terminalKey(key("v", { ctrlKey: true }), mac(false)), "pass", "Ctrl+V on a Mac is ^V");
});

test("with a Korean (Hangul) layout active the shortcuts follow the physical key", () => {
  assert.equal(board.terminalKey(key("ㅊ", { ctrlKey: true }, { code: "KeyC" }), linux(true)), "copy");
  assert.equal(board.terminalKey(key("ㅍ", { ctrlKey: true }, { code: "KeyV" }), linux(false)), "native");
});

test("IME composition, plain keys and scrolling keys are always left to xterm", () => {
  const composing = [
    key("c", { ctrlKey: true }, { isComposing: true }),
    key("Process", { ctrlKey: true }, { keyCode: 229, code: "KeyV" }),
    key("v", { ctrlKey: true }, { keyCode: 229 }),
  ];
  for (const event of composing) assert.equal(board.terminalKey(event, linux(true)), "pass");
  for (const event of [key("ㅎ"), key("a"), key("Enter"), key("Backspace"), key("PageUp"), key("PageDown"),
    key("PageUp", { shiftKey: true }), key("PageDown", { shiftKey: true }), key("Home", { shiftKey: true }),
    key("ArrowUp"), key("Tab")]) {
    assert.equal(board.terminalKey(event, linux(true)), "pass", event.key);
  }
});

test("a copied selection keeps Hangul and loses only the padding spaces at line ends", () => {
  assert.equal(board.selectionText("한글 셀   \n  두 번째 줄 \t \n끝"), "한글 셀\n  두 번째 줄\n끝");
  assert.equal(board.selectionText("가\r\n나  "), "가\r\n나");
  assert.equal(board.selectionText(""), "");
});

test("the board CSS gives the terminal a height and never hides the overflow xterm's viewport scrolls in", () => {
  const css = readFileSync(new URL("../../../src/workbench/hub/web/static/board.css", import.meta.url), "utf8");
  const rule = (selector) => {
    const match = new RegExp(`(^|\\n)${selector.replace(".", "\\.")}\\s*\\{([^}]*)\\}`).exec(css);
    return match ? match[2] : "";
  };
  assert.match(rule(".term"), /(^|;|\s)height:\s*\d/);
  for (const selector of [".term", ".panel", ".panels", ".panel.active"]) {
    assert.doesNotMatch(rule(selector), /overflow\s*:\s*hidden/, selector);
  }
  // A hidden tab keeps its layout (visibility, not display:none): its scroll position and size survive a switch.
  assert.match(rule(".panel"), /visibility:\s*hidden/);
  assert.doesNotMatch(rule(".panel"), /display:\s*none/);
});
