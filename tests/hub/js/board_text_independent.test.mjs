// Independent verification of Hangul input, copy/paste and scrolling on the Board page (static/board-state.js and
// static/board.js; no browser, no network). Derived from the user requirements (Korean input without bugs, working
// copy and paste, working scroll) and SPEC "입력 한도" (a WebSocket input frame carries at most 64 KiB) and
// "WebSocket terminal 프로토콜" (frame = channel byte | epoch uint32 BE | payload), NOT from the worker's tests.
// Expected bytes always come from Node's own UTF-8 (Buffer / TextEncoder / TextDecoder), never from the module.
import test from "node:test";
import assert from "node:assert/strict";
import * as board from "../../../src/workbench/hub/web/static/board-state.js";

const FRAME_LIMIT = 64 * 1024;
const INPUT_QUEUE = 4 * 1024 * 1024;
const strict = new TextDecoder("utf-8", { fatal: true });

// ---------------------------------------------------------------------------------------------------------------
// Seeded random mixed text.

function prng(seed) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

const EMOJI = ["😀", "👍🏽", "👨‍👩‍👧‍👦", "🇰🇷", "❤️", "1️⃣", "🧑‍💻", "\u{1F9D1}‍\u{1F91D}‍\u{1F9D1}"];
const TWO_BYTE = ["é", "ñ", "ß", "Ω", "я", "א", "£"];
const pick = (rand, list) => list[Math.floor(rand() * list.length)];
const between = (rand, lo, hi) => lo + Math.floor(rand() * (hi - lo + 1));

const GENERATORS = [
  (r) => String.fromCodePoint(between(r, 0xac00, 0xd7a3)), // Hangul syllable (3 bytes)
  (r) => String.fromCodePoint(between(r, 0xac00, 0xd7a3)).normalize("NFD"), // conjoining jamo (NFD)
  (r) => String.fromCodePoint(between(r, 0x3131, 0x318e)), // compatibility jamo
  (r) => String.fromCodePoint(between(r, 0x1100, 0x11ff)), // lone conjoining jamo
  (r) => pick(r, EMOJI), // 4-byte emoji and ZWJ / flag / keycap sequences
  (r) => String.fromCodePoint(between(r, 0x4e00, 0x9fff)), // CJK (3 bytes)
  (r) => String.fromCodePoint(between(r, 0x20000, 0x2a6df)), // CJK extension B (4 bytes)
  (r) => String.fromCodePoint(between(r, 0x20, 0x7e)), // ASCII
  (r) => String.fromCodePoint(between(r, 0x00, 0x1f)), // C0 control (CR, ESC, ^C ...)
  () => "\x7f",
  (r) => pick(r, TWO_BYTE),
  () => "\r",
  () => "\x1b[200~",
];

/** Mixed text of exactly ``size`` UTF-8 bytes (ASCII fills the last gap). */
function mixed(seed, size) {
  const rand = prng(seed);
  const parts = [];
  let used = 0;
  while (used < size) {
    const token = GENERATORS[Math.floor(rand() * GENERATORS.length)](rand);
    const bytes = Buffer.byteLength(token);
    if (used + bytes > size) {
      parts.push("a"); // a token that does not fit: one ASCII byte, so the size is hit exactly
      used += 1;
      continue;
    }
    parts.push(token);
    used += bytes;
  }
  const text = parts.join("");
  assert.equal(Buffer.byteLength(text), size, "generator hits the size exactly");
  return text;
}

// ---------------------------------------------------------------------------------------------------------------
// Input: splitInput, step({type:"input"}) and the 4 MiB refusal.

function assertSplit(bytes, label) {
  const pieces = board.splitInput(bytes);
  const joined = Buffer.concat(pieces.map((piece) => Buffer.from(piece)));
  assert.equal(joined.length, bytes.length, `${label}: lengths`);
  assert.ok(joined.equals(Buffer.from(bytes)), `${label}: concatenation is byte-exact`);
  pieces.forEach((piece, index) => {
    assert.ok(piece.length <= FRAME_LIMIT, `${label}: piece ${index} is ${piece.length} bytes`);
    assert.ok(piece.length > 0 || bytes.length === 0, `${label}: no empty piece`);
    strict.decode(piece); // throws on a cut or invalid sequence
  });
  const minimumSize = FRAME_LIMIT - 3; // a cut steps back over at most 3 continuation bytes
  pieces.slice(0, -1).forEach((piece) => assert.ok(piece.length >= minimumSize, `${label}: pieces stay near full`));
  return pieces;
}

test("random mixed text of 1 byte, around 64 KiB and 1 MB splits into valid UTF-8 pieces of at most 64 KiB, byte-exact", () => {
  const sizes = [1, 2, 3, 4, 5, 63, 1000, FRAME_LIMIT - 4, FRAME_LIMIT - 3, FRAME_LIMIT - 2, FRAME_LIMIT - 1,
    FRAME_LIMIT, FRAME_LIMIT + 1, FRAME_LIMIT + 2, FRAME_LIMIT + 3, FRAME_LIMIT + 4, 2 * FRAME_LIMIT - 1,
    2 * FRAME_LIMIT, 2 * FRAME_LIMIT + 1, 3 * FRAME_LIMIT + 7, 1 << 20, (1 << 20) + 1];
  let seed = 1;
  for (const size of sizes) {
    for (let round = 0; round < (size >= 1 << 20 ? 2 : 6); round += 1) {
      const text = mixed(seed, size);
      seed += 1;
      assertSplit(new TextEncoder().encode(text), `size ${size} seed ${seed - 1}`);
    }
  }
});

test("a 2-, 3- and 4-byte character straddling the limit at every alignment is never cut", () => {
  for (const wide of ["é", "한", "ㄱ", "ᅡ", "😀", "𠀀"]) {
    const width = Buffer.byteLength(wide);
    for (let pad = FRAME_LIMIT - 2 * width - 2; pad <= FRAME_LIMIT + 2; pad += 1) {
      // 'a' x pad, then wide characters, twice the limit worth of text so a second boundary is crossed as well
      const text = "a".repeat(pad) + wide.repeat(Math.ceil((FRAME_LIMIT + 8) / width)) + "끝";
      assertSplit(new TextEncoder().encode(text), `${wide} pad ${pad}`);
    }
  }
});

test("the input effect keeps order, the channel byte and the epoch on every frame", () => {
  let state = board.initialState({ csrf: "c" });
  const send = (event) => { const out = board.step(state, event); state = out.state; return out.effects; };
  send({ type: "start", sizes: {} });
  send({ type: "opened", conn: 1 });
  send({ type: "message", conn: 1, message: { t: "attached", v: 1, run: "r", epoch: 0x01020304, heartbeat: 10,
    channels: { agent: { state: "running" }, shell: { state: "running" } } } });
  for (const [channel, id] of [["agent", 0], ["shell", 1]]) {
    const bytes = new TextEncoder().encode(mixed(77 + id, (3 << 16) + 11));
    const frames = send({ type: "input", channel, bytes });
    assert.ok(frames.every((effect) => effect.type === "send_binary"));
    assert.ok(frames.length >= 3);
    const payloads = frames.map((effect) => {
      const data = effect.bytes;
      assert.equal(data[0], id);
      assert.deepEqual(Array.from(data.subarray(1, 5)), [1, 2, 3, 4]);
      assert.ok(data.length - 5 <= FRAME_LIMIT, "payload within the server's input frame limit");
      return data.subarray(5);
    });
    assert.ok(Buffer.concat(payloads.map((p) => Buffer.from(p))).equals(Buffer.from(bytes)));
  }
});

test("input of exactly 4 MiB is sent whole; one byte more is refused whole with a notice and no frame", () => {
  let state = board.initialState({ csrf: "c" });
  const send = (event) => { const out = board.step(state, event); state = out.state; return out.effects; };
  send({ type: "start", sizes: {} });
  send({ type: "opened", conn: 1 });
  send({ type: "message", conn: 1, message: { t: "attached", v: 1, run: "r", epoch: 2, heartbeat: 10,
    channels: { agent: { state: "running" }, shell: { state: "running" } } } });
  const hangul = new TextEncoder().encode("한".repeat(INPUT_QUEUE / 3)).subarray(0, INPUT_QUEUE - 1);
  const exact = new Uint8Array(INPUT_QUEUE);
  exact.set(hangul);
  exact[INPUT_QUEUE - 1] = 0x21;
  const accepted = send({ type: "input", channel: "shell", bytes: exact });
  assert.ok(accepted.length >= INPUT_QUEUE / FRAME_LIMIT && accepted.every((e) => e.type === "send_binary"));
  assert.equal(accepted.reduce((sum, e) => sum + e.bytes.length - 5, 0), INPUT_QUEUE);
  const over = new Uint8Array(INPUT_QUEUE + 1).fill(0x61);
  const refused = send({ type: "input", channel: "shell", bytes: over });
  assert.equal(refused.filter((effect) => effect.type === "send_binary").length, 0, "no partial delivery");
  assert.equal(refused.length, 1);
  assert.equal(refused[0].type, "notice");
  assert.equal(refused[0].reason, "too_large");
  // The state stays usable: the next small input still goes out.
  assert.equal(send({ type: "input", channel: "shell", bytes: new Uint8Array([0x61]) }).length, 1);
});

test("bytes that are not UTF-8 (xterm onBinary) still split exactly, in bounded pieces, without stalling", () => {
  const rand = prng(4242);
  for (let round = 0; round < 12; round += 1) {
    const length = FRAME_LIMIT * 3 + between(rand, 0, 5000);
    const bytes = new Uint8Array(length);
    for (let i = 0; i < length; i += 1) {
      // a heavy share of continuation bytes (0x80-0xBF) and 0xFF to hit the fall-back branch
      bytes[i] = rand() < 0.6 ? between(rand, 0x80, 0xbf) : between(rand, 0, 255);
    }
    const pieces = board.splitInput(bytes);
    assert.ok(pieces.every((piece) => piece.length > 0 && piece.length <= FRAME_LIMIT));
    assert.ok(Buffer.concat(pieces.map((p) => Buffer.from(p))).equals(Buffer.from(bytes)));
  }
  const allContinuation = new Uint8Array(FRAME_LIMIT * 2 + 5).fill(0x80);
  const pieces = board.splitInput(allContinuation);
  assert.ok(pieces.every((piece) => piece.length > 0 && piece.length <= FRAME_LIMIT));
  assert.equal(pieces.reduce((sum, piece) => sum + piece.length, 0), allContinuation.length);
});

test("an empty input produces no frame", () => {
  let state = board.initialState({ csrf: "c" });
  const send = (event) => { const out = board.step(state, event); state = out.state; return out.effects; };
  send({ type: "start", sizes: {} });
  send({ type: "opened", conn: 1 });
  send({ type: "message", conn: 1, message: { t: "attached", v: 1, run: "r", epoch: 1, heartbeat: 10,
    channels: { agent: { state: "running" }, shell: { state: "running" } } } });
  assert.deepEqual(send({ type: "input", channel: "agent", bytes: new Uint8Array(0) }), []);
});

// ---------------------------------------------------------------------------------------------------------------
// Typing only for the lease holder (pure state).

test("input is sent only while attached: not before the attach, not after a takeover, not after a drop", () => {
  let state = board.initialState({ csrf: "c" });
  const send = (event) => { const out = board.step(state, event); state = out.state; return out.effects; };
  const typed = { type: "input", channel: "agent", bytes: new TextEncoder().encode("한글") };
  assert.deepEqual(send(typed), [], "idle");
  send({ type: "start", sizes: {} });
  assert.deepEqual(send(typed), [], "connecting");
  send({ type: "opened", conn: 1 });
  send({ type: "message", conn: 1, message: { t: "attached", v: 1, run: "r", epoch: 5, heartbeat: 10,
    channels: { agent: { state: "running" }, shell: { state: "running" } } } });
  assert.equal(send(typed).length, 1, "attached");
  assert.equal(board.describe(state).canInput, true);
  send({ type: "message", conn: 1, message: { t: "taken_over", epoch: 6 } });
  assert.equal(board.describe(state).canInput, false, "taken over");
  assert.deepEqual(send(typed), []);
  send({ type: "retake" });
  assert.deepEqual(send(typed), [], "reconnecting after retake");
  send({ type: "opened", conn: 2 });
  send({ type: "message", conn: 2, message: { t: "attached", v: 1, run: "r", epoch: 7, heartbeat: 10,
    channels: { agent: { state: "running" }, shell: { state: "running" } } } });
  assert.equal(send(typed).length, 1);
  send({ type: "closed", conn: 2, code: 1006 });
  assert.equal(board.describe(state).canInput, false, "network drop");
  assert.deepEqual(send(typed), []);
});

// ---------------------------------------------------------------------------------------------------------------
// Keys: terminalKey and selectionText (pure).

const ev = (key, mods = {}, extra = {}) => ({
  type: "keydown", key, code: /^[A-Za-z]$/.test(key) ? `Key${key.toUpperCase()}` : key, keyCode: 0,
  ctrlKey: false, shiftKey: false, altKey: false, metaKey: false, isComposing: false, ...mods, ...extra,
});
const LINUX = (hasSelection) => ({ mac: false, hasSelection });
const MAC = (hasSelection) => ({ mac: true, hasSelection });

test("copy shortcuts per platform; Ctrl+C is ^C for the program unless text is selected", () => {
  // Linux / Windows
  assert.equal(board.terminalKey(ev("c", { ctrlKey: true }), LINUX(true)), "copy");
  assert.equal(board.terminalKey(ev("C", { ctrlKey: true }), LINUX(true)), "copy", "Caps Lock");
  assert.equal(board.terminalKey(ev("c", { ctrlKey: true }), LINUX(false)), "pass", "^C reaches the PTY");
  assert.equal(board.terminalKey(ev("C", { ctrlKey: true, shiftKey: true }), LINUX(false)), "copy");
  assert.equal(board.terminalKey(ev("C", { ctrlKey: true, shiftKey: true }), LINUX(true)), "copy");
  assert.equal(board.terminalKey(ev("Insert", { ctrlKey: true }), LINUX(true)), "copy");
  // a plain C with a selection is just the letter "c"
  assert.equal(board.terminalKey(ev("c"), LINUX(true)), "pass");
  // AltGr (Ctrl+Alt on Windows) and Meta combinations belong to the program / the OS
  assert.equal(board.terminalKey(ev("c", { ctrlKey: true, altKey: true }), LINUX(true)), "pass");
  assert.equal(board.terminalKey(ev("c", { ctrlKey: true, metaKey: true }), LINUX(true)), "pass");
  // macOS: Cmd+C copies with a selection; Ctrl+C is always ^C
  assert.equal(board.terminalKey(ev("c", { metaKey: true }), MAC(true)), "copy");
  assert.equal(board.terminalKey(ev("c", { metaKey: true }), MAC(false)), "pass");
  assert.equal(board.terminalKey(ev("c", { ctrlKey: true }), MAC(true)), "pass");
  assert.equal(board.terminalKey(ev("c", { ctrlKey: true }), MAC(false)), "pass");
});

test("paste shortcuts go to the browser's paste event, never to the PTY as a control character", () => {
  for (const [event, platform] of [
    [ev("v", { ctrlKey: true }), LINUX(false)],
    [ev("V", { ctrlKey: true, shiftKey: true }), LINUX(false)],
    [ev("v", { ctrlKey: true }), LINUX(true)],
    [ev("Insert", { shiftKey: true }), LINUX(false)],
    [ev("v", { metaKey: true }), MAC(false)],
    [ev("v", { metaKey: true }), MAC(true)],
  ]) {
    assert.equal(board.terminalKey(event, platform), "native", JSON.stringify([event.key, platform]));
  }
  assert.equal(board.terminalKey(ev("v", { ctrlKey: true }), MAC(false)), "pass", "Ctrl+V on a Mac is ^V");
});

test("a Hangul layout leaves the physical key in event.code, which decides the shortcut", () => {
  assert.equal(board.terminalKey(ev("ㅊ", { ctrlKey: true }, { code: "KeyC" }), LINUX(true)), "copy");
  assert.equal(board.terminalKey(ev("ㅊ", { ctrlKey: true }, { code: "KeyC" }), LINUX(false)), "pass");
  assert.equal(board.terminalKey(ev("ㅊ", { ctrlKey: true, shiftKey: true }, { code: "KeyC" }), LINUX(false)), "copy");
  assert.equal(board.terminalKey(ev("ㅍ", { ctrlKey: true }, { code: "KeyV" }), LINUX(false)), "native");
  assert.equal(board.terminalKey(ev("ㅊ", { metaKey: true }, { code: "KeyC" }), MAC(true)), "copy");
  assert.equal(board.terminalKey(ev("ㅍ", { metaKey: true }, { code: "KeyV" }), MAC(false)), "native");
  assert.equal(board.terminalKey(ev("ㅋ", { ctrlKey: true }, { code: "KeyZ" }), LINUX(true)), "pass", "other keys unchanged");
  // A Latin layout that moves letters (Dvorak): the character decides, not the physical position.
  assert.equal(board.terminalKey(ev("j", { ctrlKey: true }, { code: "KeyC" }), LINUX(true)), "pass");
  assert.equal(board.terminalKey(ev("c", { ctrlKey: true }, { code: "KeyJ" }), LINUX(true)), "copy");
});

test("an IME composition is never intercepted, for every key and modifier combination", () => {
  const keys = ["c", "C", "v", "V", "ㅊ", "ㅍ", "Process", "Unidentified", "Insert", "Enter", "Backspace", "a", "ㅎ", "한",
    "PageUp", "PageDown", "ArrowLeft", " ", "Tab", "Escape"];
  const codes = ["KeyC", "KeyV", "Insert", "KeyA", "Enter", "Space", ""];
  for (const key of keys) {
    for (const code of codes) {
      for (let mask = 0; mask < 16; mask += 1) {
        const mods = { ctrlKey: !!(mask & 1), shiftKey: !!(mask & 2), altKey: !!(mask & 4), metaKey: !!(mask & 8) };
        for (const platform of [LINUX(true), LINUX(false), MAC(true), MAC(false)]) {
          for (const extra of [{ isComposing: true }, { keyCode: 229 }, { isComposing: true, keyCode: 229 }]) {
            for (const type of ["keydown", "keypress", "keyup"]) {
              const event = { type, key, code, keyCode: 0, isComposing: false, ...mods, ...extra };
              assert.equal(board.terminalKey(event, platform), "pass", JSON.stringify([event, platform]));
            }
          }
        }
      }
    }
  }
});

test("scroll keys, plain typing and Hangul keys always stay with xterm, with any modifier but Ctrl+C/V/Insert rules", () => {
  const keys = ["PageUp", "PageDown", "Home", "End", "ArrowUp", "ArrowDown", "Enter", "Backspace", "Delete", "Tab",
    "Escape", " ", "a", "ㅎ", "한", "1", "F5"];
  for (const key of keys) {
    for (let mask = 0; mask < 16; mask += 1) {
      const mods = { ctrlKey: !!(mask & 1), shiftKey: !!(mask & 2), altKey: !!(mask & 4), metaKey: !!(mask & 8) };
      for (const platform of [LINUX(true), LINUX(false), MAC(true), MAC(false)]) {
        const verdict = board.terminalKey(ev(key, mods, { code: key === "ㅎ" ? "KeyG" : key }), platform);
        assert.equal(verdict, "pass", JSON.stringify([key, mods, platform]));
      }
    }
  }
});

test("selectionText strips only padding at line ends and never duplicates or drops wide characters", () => {
  assert.equal(board.selectionText("한글   "), "한글");
  assert.equal(board.selectionText("한글   \n둘째 줄  \n"), "한글\n둘째 줄\n");
  assert.equal(board.selectionText("가\r\n나  \r\n다"), "가\r\n나\r\n다");
  assert.equal(board.selectionText("  들여쓰기 유지"), "  들여쓰기 유지");
  assert.equal(board.selectionText("가 나   다"), "가 나   다", "inner spaces stay");
  assert.equal(board.selectionText("\n\n한\n\n"), "\n\n한\n\n", "blank lines stay");
  assert.equal(board.selectionText(""), "");
  // wide, jamo and emoji characters are not touched at all
  const wide = "한글ㄱㅏ한😀👍🏽👨‍👩‍👧中文";
  assert.equal(board.selectionText(wide), wide);
  assert.equal(board.selectionText(`${wide}   \n${wide}  `), `${wide}\n${wide}`);
  // content spaces that are not ASCII padding (ideographic space, Hangul filler) are content
  assert.equal(board.selectionText("값　"), "값　");
  assert.equal(board.selectionText("값ㅤ\n"), "값ㅤ\n");
});

// ---------------------------------------------------------------------------------------------------------------
// Glue (board.js) against stub DOM, xterm and WebSocket: output bytes, keys, lease gating, scroll.

const elements = new Map();
function element(id) {
  if (!elements.has(id)) {
    const classes = new Set(id === "panel-agent" ? ["panel", "active"] : []);
    elements.set(id, {
      id, textContent: "", hidden: false, dataset: {}, listeners: {}, attrs: {},
      classList: { toggle: (name, on) => (on ? classes.add(name) : classes.delete(name)), contains: (n) => classes.has(n) },
      setAttribute(name, value) { this.attrs[name] = value; },
      addEventListener(type, handler) { this.listeners[type] = handler; },
    });
  }
  return elements.get(id);
}

const terminals = [];
const scrollCalls = [];
class FakeTerminal {
  constructor(options) {
    this.rows = 30; this.cols = 100; this.options = {}; this.raw = []; this.resets = 0; this.handlers = {};
    this.created = options; this.selection = ""; this.cleared = 0; this.focused = 0;
    const proxy = new Proxy(this, {
      get(target, prop, receiver) {
        if (typeof prop === "string" && /^scroll/i.test(prop) && !(prop in target)) {
          return (...args) => { scrollCalls.push([terminals.indexOf(proxy), prop, ...args]); };
        }
        return Reflect.get(target, prop, receiver);
      },
    });
    terminals.push(proxy);
    return proxy;
  }
  attachCustomKeyEventHandler(handler) { this.keyHandler = handler; }
  hasSelection() { return this.selection !== ""; }
  getSelection() { return this.selection; }
  clearSelection() { this.selection = ""; this.cleared += 1; }
  loadAddon() {}
  open() {}
  focus() { this.focused += 1; }
  reset() { this.resets += 1; }
  write(data) { this.raw.push(data); }
  onData(handler) { this.handlers.data = handler; }
  onBinary(handler) { this.handlers.binary = handler; }
  onResize(handler) { this.handlers.resize = handler; }
}
class FakeFit { fit() {} }
const sockets = [];
class FakeWebSocket {
  static OPEN = 1;
  constructor(url) { this.url = url; this.readyState = 1; this.sent = []; sockets.push(this); }
  send(data) { this.sent.push(data); }
  close() { this.readyState = 3; }
}
const documentListeners = {};
const clipboard = { copied: [] };
globalThis.document = {
  getElementById: element,
  querySelector: () => ({ content: "csrf-x" }),
  addEventListener: (type, handler) => { documentListeners[type] = handler; },
  execCommand(command) {
    if (command !== "copy") return false;
    const event = { clipboardData: { setData: (type, text) => clipboard.copied.push([type, text]) },
      preventDefault() {} };
    if (documentListeners.copy) documentListeners.copy(event);
    return true;
  },
};
Object.defineProperty(globalThis, "navigator", { value: { platform: "Linux x86_64", userAgent: "t" }, configurable: true });
globalThis.isSecureContext = false;
globalThis.window = { addEventListener() {} };
globalThis.location = { protocol: "http:", host: "127.0.0.1:1" };
globalThis.Terminal = FakeTerminal;
globalThis.FitAddon = { FitAddon: FakeFit };
globalThis.WebSocket = FakeWebSocket;
globalThis.requestAnimationFrame = (callback) => callback();
globalThis.setTimeout = () => 1;
globalThis.clearTimeout = () => {};
await import("../../../src/workbench/hub/web/static/board.js");

const socket = sockets[0];
const control = (body) => socket.onmessage({ data: JSON.stringify(body) });
const dataFrame = (channel, epoch, bytes) => {
  const out = new Uint8Array(5 + bytes.length);
  out[0] = channel;
  new DataView(out.buffer).setUint32(1, epoch, false);
  out.set(bytes, 5);
  return out.buffer;
};
const binaryFrames = () => socket.sent.filter((item) => typeof item !== "string");
const ATTACHED = (epoch) => ({ t: "attached", v: 1, run: "r", epoch, heartbeat: 10,
  channels: { agent: { state: "running" }, shell: { state: "running" } } });
const key = (k, mods = {}, extra = {}) => ({
  type: "keydown", key: k, code: /^[A-Za-z]$/.test(k) ? `Key${k.toUpperCase()}` : k, keyCode: 0,
  ctrlKey: false, shiftKey: false, altKey: false, metaKey: false, isComposing: false, prevented: 0,
  preventDefault() { this.prevented += 1; }, ...mods, ...extra,
});

test("glue: both terminals keep at least 10000 lines of scrollback and scroll to the bottom on user input", () => {
  assert.equal(terminals.length, 2);
  for (const term of terminals) {
    assert.ok(term.created.scrollback >= 10000, `scrollback ${term.created.scrollback}`);
    assert.equal(term.created.scrollOnUserInput, true);
    assert.equal(typeof term.keyHandler, "function");
  }
});

test("glue: Hangul output split at every byte offset across two frames reaches xterm as bytes and decodes whole", () => {
  socket.onopen();
  control(ATTACHED(9));
  const text = "\x1b[2J\x1b[1;1H한글 입력: \x1b[38;5;196m안녕하세요\x1b[0m ㄱㅏ 한 👨‍👩‍👧 中文 \x1b]0;제목\x07끝\r\n";
  const bytes = new TextEncoder().encode(text);
  for (let offset = 0; offset <= bytes.length; offset += 1) {
    for (const [channel, term] of [[0, terminals[0]], [1, terminals[1]]]) {
      const before = term.raw.length;
      socket.onmessage({ data: dataFrame(channel, 9, bytes.subarray(0, offset)) });
      socket.onmessage({ data: dataFrame(channel, 9, bytes.subarray(offset)) });
      const written = term.raw.slice(before);
      assert.ok(written.every((item) => item instanceof Uint8Array), `offset ${offset}: bytes, not strings`);
      assert.ok(Buffer.concat(written.map((item) => Buffer.from(item))).equals(Buffer.from(bytes)), `offset ${offset}`);
      const decoder = new TextDecoder("utf-8", { fatal: true });
      let rebuilt = "";
      for (const piece of written) rebuilt += decoder.decode(piece, { stream: true });
      rebuilt += decoder.decode();
      assert.equal(rebuilt, text, `streamed decode at offset ${offset}`);
    }
  }
});

test("glue: random multi-frame splits (including 1-byte frames) keep every byte, in order, on the right terminal", () => {
  const rand = prng(99);
  const bytes = new TextEncoder().encode(mixed(5, 3000));
  for (let round = 0; round < 40; round += 1) {
    const channel = round % 2;
    const term = terminals[channel];
    const before = term.raw.length;
    const other = terminals[1 - channel].raw.length;
    let at = 0;
    while (at < bytes.length) {
      const size = round === 0 ? 1 : between(rand, 1, 400);
      socket.onmessage({ data: dataFrame(channel, 9, bytes.subarray(at, at + size)) });
      at += size;
    }
    const written = term.raw.slice(before);
    assert.ok(written.every((item) => item instanceof Uint8Array));
    assert.ok(Buffer.concat(written.map((item) => Buffer.from(item))).equals(Buffer.from(bytes)), `round ${round}`);
    assert.equal(terminals[1 - channel].raw.length, other, "nothing leaks into the other terminal");
  }
});

test("glue: stale-epoch output is dropped and output never scrolls the view", () => {
  const before = terminals[0].raw.length;
  socket.onmessage({ data: dataFrame(0, 8, new TextEncoder().encode("오래됨")) });
  assert.equal(terminals[0].raw.length, before, "an old epoch does not write");
  for (let i = 0; i < 200; i += 1) socket.onmessage({ data: dataFrame(i % 2, 9, new TextEncoder().encode(`줄 ${i}\r\n`)) });
  assert.deepEqual(scrollCalls, [], "no scrollToBottom / scrollLines call came from output");
});

test("glue: typed and pasted Hangul, jamo and emoji reach the PTY frames as exact UTF-8, in order, with the epoch", () => {
  const sentBefore = binaryFrames().length;
  const text = mixed(31, 3 * FRAME_LIMIT + 100);
  terminals[1].handlers.data(text);
  const frames = binaryFrames().slice(sentBefore);
  assert.ok(frames.length >= 4);
  frames.forEach((frame) => {
    assert.equal(frame[0], 1);
    assert.equal(new DataView(frame.buffer, frame.byteOffset).getUint32(1, false), 9);
    assert.ok(frame.length - 5 <= FRAME_LIMIT);
    strict.decode(frame.subarray(5));
  });
  assert.equal(Buffer.concat(frames.map((f) => Buffer.from(f.subarray(5)))).toString("utf8"), text);
  // xterm's onBinary hands over a binary string (one char per byte)
  const binaryBefore = binaryFrames().length;
  let raw = "";
  for (let i = 0; i < 256; i += 1) raw += String.fromCharCode(i);
  terminals[0].handlers.binary(raw);
  const binaryFramesNow = binaryFrames().slice(binaryBefore);
  assert.equal(binaryFramesNow.length, 1);
  assert.deepEqual(Array.from(binaryFramesNow[0].subarray(5)), Array.from({ length: 256 }, (_, i) => i));
});

test("glue: a paste over 4 MiB sends nothing and tells the user; a 4 MiB paste goes out whole", () => {
  const before = binaryFrames().length;
  terminals[0].handlers.data("가".repeat(Math.floor(INPUT_QUEUE / 3) + 1));
  assert.equal(binaryFrames().length, before, "refused whole");
  assert.notEqual(element("board-notice").textContent, "");
  assert.equal(element("board-notice").hidden, false);
  terminals[0].handlers.data("a".repeat(INPUT_QUEUE));
  const frames = binaryFrames().slice(before);
  assert.equal(frames.reduce((sum, f) => sum + f.length - 5, 0), INPUT_QUEUE);
});

test("glue: copy fills the browser's copy event with the trimmed Hangul selection and clears it, even without the lease", () => {
  const copyOnce = (selection, mods, extra) => {
    const term = terminals[1];
    term.selection = selection;
    const event = key("C", { ctrlKey: true, shiftKey: true, ...mods }, extra);
    const handled = term.keyHandler(event);
    return { handled, event, term };
  };
  // holder
  let result = copyOnce("한글 😀   \n둘째 줄   ");
  assert.equal(result.handled, false);
  assert.equal(result.event.prevented, 1);
  assert.deepEqual(clipboard.copied.at(-1), ["text/plain", "한글 😀\n둘째 줄"]);
  assert.equal(result.term.selection, "");
  // taken over by another tab: input is off, copying still works
  control({ t: "taken_over", epoch: 10 });
  assert.equal(terminals[0].options.disableStdin, true);
  assert.equal(terminals[1].options.disableStdin, true);
  const frames = binaryFrames().length;
  terminals[0].handlers.data("x");
  terminals[1].handlers.data("붙여넣기");
  assert.equal(binaryFrames().length, frames, "no typing or pasting without the lease");
  result = copyOnce("가나다   ");
  assert.equal(result.handled, false);
  assert.deepEqual(clipboard.copied.at(-1), ["text/plain", "가나다"]);
  // a dropped connection after a retake: same
  socket.onclose({ code: 1006 });
  assert.equal(terminals[1].options.disableStdin, true);
  result = copyOnce("연결 없음   ");
  assert.deepEqual(clipboard.copied.at(-1), ["text/plain", "연결 없음"]);
  // Ctrl+C with a selection copies, without one it is ^C for the program (handler lets xterm see it)
  terminals[1].selection = "선택";
  assert.equal(terminals[1].keyHandler(key("c", { ctrlKey: true })), false);
  assert.deepEqual(clipboard.copied.at(-1), ["text/plain", "선택"]);
  const plain = key("c", { ctrlKey: true });
  assert.equal(terminals[1].keyHandler(plain), true);
  assert.equal(plain.prevented, 0);
});

test("glue: IME and scroll keys reach xterm untouched; paste keys are left to the browser without preventDefault", () => {
  for (const term of terminals) {
    term.selection = "있음";
    for (const event of [
      key("c", { ctrlKey: true }, { isComposing: true }),
      key("Process", { ctrlKey: true }, { keyCode: 229, code: "KeyC" }),
      key("ㅎ", {}, { isComposing: true }),
      key("PageUp"), key("PageDown"), key("PageUp", { shiftKey: true }), key("PageDown", { shiftKey: true }),
      key("Home", { shiftKey: true }), key("End", { shiftKey: true }),
    ]) {
      assert.equal(term.keyHandler(event), true, event.key);
      assert.equal(event.prevented, 0, event.key);
    }
    for (const event of [key("v", { ctrlKey: true }), key("V", { ctrlKey: true, shiftKey: true }),
      key("Insert", { shiftKey: true }), key("ㅍ", { ctrlKey: true }, { code: "KeyV" })]) {
      assert.equal(term.keyHandler(event), false);
      assert.equal(event.prevented, 0, "the browser's own paste still runs");
    }
    term.selection = "";
  }
});

test("glue: switching tabs keeps both terminals, their content and their scroll position", () => {
  const resets = terminals.map((t) => t.resets);
  const scrolled = scrollCalls.length;
  const instances = terminals.length;
  element("tab-shell").listeners.click();
  assert.equal(element("panel-shell").classList.contains("active"), true);
  assert.equal(element("panel-agent").classList.contains("active"), false);
  assert.equal(element("tab-shell").attrs["aria-selected"], "true");
  element("tab-agent").listeners.click();
  assert.equal(element("panel-agent").classList.contains("active"), true);
  assert.equal(element("panel-shell").classList.contains("active"), false);
  assert.equal(terminals.length, instances, "no terminal is recreated");
  assert.deepEqual(terminals.map((t) => t.resets), resets, "no terminal is reset by a tab switch");
  assert.equal(scrollCalls.length, scrolled, "a tab switch does not move any view");
});

test("pure state: channel state and output of one channel never touch the other channel's state", () => {
  let state = board.initialState({ csrf: "c" });
  const send = (event) => { const out = board.step(state, event); state = out.state; return out.effects; };
  send({ type: "start", sizes: {} });
  send({ type: "opened", conn: 1 });
  send({ type: "message", conn: 1, message: ATTACHED(3) });
  const before = JSON.stringify(board.describe(state).channels);
  const effects = send({ type: "data", conn: 1, bytes: board.encodeFrame("shell", 3, new TextEncoder().encode("한글")) });
  assert.deepEqual(effects.map((e) => [e.type, e.channel]), [["write", "shell"]]);
  assert.equal(JSON.stringify(board.describe(state).channels), before);
  const agent = send({ type: "data", conn: 1, bytes: board.encodeFrame("agent", 3, new TextEncoder().encode("한글")) });
  assert.deepEqual(agent.map((e) => [e.type, e.channel]), [["write", "agent"]]);
});
