// Independent verification of Hangul input, copy/paste and scrolling on the Board page (static/board-state.js and
// static/board.js; no browser, no network). Derived from the user requirements (Korean input without bugs, working
// copy and paste, working scroll) and SPEC "입력 한도" (a WebSocket input frame carries at most 64 KiB) and
// "WebSocket terminal 프로토콜", migrated to web-board-ux SPEC s4 "WebSocket v2" (WBX-D1: a frame is the bare payload,
// one socket per terminal; v1's channel byte, epoch and takeover are gone), NOT from the worker's tests.
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

const ATTACHED_V2 = { t: "attached", v: 2, terminal: { id: "s-1", kind: "shell", name: "shell-1", state: "running" },
    size: [30, 100], clients: 1, heartbeat: 10 };

function attachedState() {
  let state = board.initialState({ csrf: "c" });
  const send = (event) => { const out = board.step(state, event); state = out.state; return out.effects; };
  send({ type: "start", size: [30, 100] });
  send({ type: "opened", conn: 1 });
  send({ type: "message", conn: 1, message: ATTACHED_V2 });
  return { send, get state() { return state; } };
}

test("the input effect keeps order on every frame; a frame is the bare payload (v1: channel byte and epoch)", () => {
  const tab = attachedState();
  for (const id of [0, 1]) {
    const bytes = new TextEncoder().encode(mixed(77 + id, (3 << 16) + 11));
    const frames = tab.send({ type: "input", bytes });
    assert.ok(frames.every((effect) => effect.type === "send_binary"));
    assert.ok(frames.length >= 3);
    const payloads = frames.map((effect) => {
      assert.ok(effect.bytes.length <= FRAME_LIMIT, "payload within the server's input frame limit");
      return effect.bytes;
    });
    assert.ok(Buffer.concat(payloads.map((p) => Buffer.from(p))).equals(Buffer.from(bytes)));
  }
});

test("input of exactly 4 MiB is sent whole; one byte more is refused whole with a notice and no frame", () => {
  const tab = attachedState();
  const hangul = new TextEncoder().encode("한".repeat(INPUT_QUEUE / 3)).subarray(0, INPUT_QUEUE - 1);
  const exact = new Uint8Array(INPUT_QUEUE);
  exact.set(hangul);
  exact[INPUT_QUEUE - 1] = 0x21;
  const accepted = tab.send({ type: "input", bytes: exact });
  assert.ok(accepted.length >= INPUT_QUEUE / FRAME_LIMIT && accepted.every((e) => e.type === "send_binary"));
  assert.equal(accepted.reduce((sum, e) => sum + e.bytes.length, 0), INPUT_QUEUE);
  const over = new Uint8Array(INPUT_QUEUE + 1).fill(0x61);
  const refused = tab.send({ type: "input", bytes: over });
  assert.equal(refused.filter((effect) => effect.type === "send_binary").length, 0, "no partial delivery");
  assert.equal(refused.length, 1);
  assert.equal(refused[0].type, "notice");
  assert.equal(refused[0].reason, "too_large");
  // The state stays usable: the next small input still goes out.
  assert.equal(tab.send({ type: "input", bytes: new Uint8Array([0x61]) }).length, 1);
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
  const tab = attachedState();
  assert.deepEqual(tab.send({ type: "input", bytes: new Uint8Array(0) }), []);
});

// ---------------------------------------------------------------------------------------------------------------
// Typing only while attached (pure state). v1: "only the lease holder"; in v2 every attached client types.

test("input is sent only while attached: not before the attach, not after a release, not after a drop", () => {
  let state = board.initialState({ csrf: "c" });
  const send = (event) => { const out = board.step(state, event); state = out.state; return out.effects; };
  const typed = { type: "input", bytes: new TextEncoder().encode("한글") };
  assert.deepEqual(send(typed), [], "idle");
  send({ type: "start", size: [30, 100] });
  assert.deepEqual(send(typed), [], "connecting");
  send({ type: "opened", conn: 1 });
  send({ type: "message", conn: 1, message: { ...ATTACHED_V2, clients: 3 } });
  assert.equal(send(typed).length, 1, "attached, one of three clients");
  assert.equal(board.describe(state).canInput, true);
  send({ type: "pagehide" });
  assert.equal(board.describe(state).canInput, false, "released");
  assert.deepEqual(send(typed), []);
  send({ type: "pageshow", persisted: true });
  assert.deepEqual(send(typed), [], "reconnecting");
  send({ type: "opened", conn: 2 });
  send({ type: "message", conn: 2, message: ATTACHED_V2 });
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
// Glue (board.js) against stub DOM, xterm and WebSocket: output bytes, keys, connection gating, scroll.
// Migrated for web-board-ux WBX-D2: the page is a panel + grid workspace (no tab/panel-agent/term-agent elements);
// board.js builds one widget (one xterm, one socket) per visible terminal, so the stub DOM has to support element
// creation (append, insertBefore, replaceChildren, remove, style.setProperty, classList).

class El {
  constructor(tag, id = "") {
    this.tagName = String(tag).toUpperCase(); this.id = id; this.children = []; this.parent = null; this.listeners = {};
    this.attrs = {}; this.dataset = {}; this._text = ""; this.hidden = false; this.disabled = false; this.value = "";
    const classes = new Set();
    this.classes = classes;
    this.classList = {
      add: (...names) => names.forEach((name) => classes.add(name)), remove: (...names) => names.forEach((name) => classes.delete(name)),
      toggle: (name, on) => { if (on === undefined ? !classes.has(name) : on) classes.add(name); else classes.delete(name); return classes.has(name); },
      contains: (name) => classes.has(name),
    };
    const props = new Map();
    this.style = { setProperty: (name, value) => props.set(name, String(value)), removeProperty: (name) => props.delete(name) };
    this.clientWidth = 600; this.clientHeight = 400;
  }
  get className() { return [...this.classes].join(" "); }
  set className(value) { this.classes.clear(); for (const name of String(value).split(/\s+/).filter(Boolean)) this.classes.add(name); }
  get textContent() { return this._text + this.children.map((child) => child.textContent).join(""); }
  set textContent(value) { this._text = String(value); this.children = []; }
  setAttribute(name, value) { this.attrs[name] = String(value); }
  append(...nodes) { for (const node of nodes) { node.parent = this; this.children.push(node); } }
  insertBefore(node, ref) { node.parent = this; const at = this.children.indexOf(ref); this.children.splice(at < 0 ? this.children.length : at, 0, node); }
  replaceChildren(...nodes) { this.children = []; this.append(...nodes); }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter((child) => child !== this); this.parent = null; }
  addEventListener(type, handler) { (this.listeners[type] ||= []).push(handler); }
  fire(type, event = {}) { for (const handler of this.listeners[type] || []) handler({ target: this, preventDefault() {}, ...event }); }
  closest(selector) { for (let node = this; node; node = node.parent) if (node.tagName === selector.toUpperCase()) return node; return null; }
  getBoundingClientRect() { return { left: 0, top: 0, width: 600, height: 400 }; }
  focus() {}
  select() {}
  find(predicate, out = []) { if (predicate(this)) out.push(this); for (const child of this.children) child.find(predicate, out); return out; }
}
const elements = new Map();
const element = (id) => {
  if (!elements.has(id)) elements.set(id, new El("div", id));
  return elements.get(id);
};

const terminals = [];
const scrollCalls = [];
class FakeTerminal {
  constructor(options) {
    this.rows = 30; this.cols = 100; this.options = {}; this.raw = []; this.resets = 0; this.handlers = {};
    this.resize = (cols, rows) => { this.rows = rows; this.cols = cols; };
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
class FakeFit { proposeDimensions() { return { rows: 30, cols: 100 }; } }
const sockets = [];
const allSent = []; // every socket's sends in order (one socket per terminal in v2)
class FakeWebSocket {
  static OPEN = 1;
  constructor(url) { this.url = url; this.readyState = 1; this.sent = []; sockets.push(this); }
  send(data) { this.sent.push(data); allSent.push(data); }
  close() { this.readyState = 3; }
}
const documentListeners = {};
const clipboard = { copied: [] };
globalThis.document = {
  getElementById: element,
  querySelector: () => ({ content: "csrf-x" }),
  createElement: (tag) => new El(tag),
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
const IDS = ["a-000000000001", "s-000000000002"];
globalThis.fetch = () => Promise.resolve({ status: 200, json: () => Promise.resolve({ ok: true, terminals: [
  { id: IDS[0], kind: "agent", name: "agent-1", state: "running" },
  { id: IDS[1], kind: "shell", name: "shell-1", state: "running" }] }) });
await import("../../../src/workbench/hub/web/static/board.js");
for (let i = 0; i < 8; i += 1) await new Promise((resolve) => setImmediate(resolve)); // GET /api/board, POST /api/board/open, then one widget and one socket per terminal

// v2: one socket per terminal (sockets[0] agent-1, sockets[1] shell-1); a data frame is the bare output.
const control = (index, body) => sockets[index].onmessage({ data: JSON.stringify(body) });
const dataFrame = (channel, bytes) => bytes.slice().buffer;
const deliver = (channel, bytes) => sockets[channel].onmessage({ data: dataFrame(channel, bytes) });
const binaryFrames = () => allSent.filter((data) => typeof data !== "string");
const ATTACHED = (index) => ({ t: "attached", v: 2, terminal: { id: IDS[index], kind: index ? "shell" : "agent",
  name: index ? "shell-1" : "agent-1", state: "running" }, size: [30, 100], clients: 1, heartbeat: 10 });
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
  for (const index of [0, 1]) {
    sockets[index].onopen();
    control(index, ATTACHED(index));
  }
  const text = "\x1b[2J\x1b[1;1H한글 입력: \x1b[38;5;196m안녕하세요\x1b[0m ㄱㅏ 한 👨‍👩‍👧 中文 \x1b]0;제목\x07끝\r\n";
  const bytes = new TextEncoder().encode(text);
  for (let offset = 0; offset <= bytes.length; offset += 1) {
    for (const [channel, term] of [[0, terminals[0]], [1, terminals[1]]]) {
      const before = term.raw.length;
      deliver(channel, bytes.subarray(0, offset));
      deliver(channel, bytes.subarray(offset));
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
      deliver(channel, bytes.subarray(at, at + size));
      at += size;
    }
    const written = term.raw.slice(before);
    assert.ok(written.every((item) => item instanceof Uint8Array));
    assert.ok(Buffer.concat(written.map((item) => Buffer.from(item))).equals(Buffer.from(bytes)), `round ${round}`);
    assert.equal(terminals[1 - channel].raw.length, other, "nothing leaks into the other terminal");
  }
});

test("glue: output of one terminal's socket never writes the other terminal and never scrolls the view", () => {
  const before = terminals[0].raw.length;
  deliver(1, new TextEncoder().encode("다른 terminal"));
  assert.equal(terminals[0].raw.length, before, "v1: a stale epoch; v2: another socket does not write here");
  for (let i = 0; i < 200; i += 1) deliver(i % 2, new TextEncoder().encode(`줄 ${i}\r\n`));
  assert.deepEqual(scrollCalls, [], "no scrollToBottom / scrollLines call came from output");
});

test("glue: typed and pasted Hangul, jamo and emoji reach the PTY frames as exact UTF-8, in order (v1: with the epoch)", () => {
  const sentBefore = binaryFrames().length;
  const text = mixed(31, 3 * FRAME_LIMIT + 100);
  terminals[1].handlers.data(text);
  const frames = binaryFrames().slice(sentBefore);
  assert.ok(frames.length >= 4);
  frames.forEach((frame) => {
    assert.ok(frame.length <= FRAME_LIMIT);
    strict.decode(frame);
  });
  assert.ok(sockets[1].sent.includes(frames[0]), "on the shell's own socket");
  assert.equal(Buffer.concat(frames.map((f) => Buffer.from(f))).toString("utf8"), text);
  // xterm's onBinary hands over a binary string (one char per byte)
  const binaryBefore = binaryFrames().length;
  let raw = "";
  for (let i = 0; i < 256; i += 1) raw += String.fromCharCode(i);
  terminals[0].handlers.binary(raw);
  const binaryFramesNow = binaryFrames().slice(binaryBefore);
  assert.equal(binaryFramesNow.length, 1);
  assert.deepEqual(Array.from(binaryFramesNow[0]), Array.from({ length: 256 }, (_, i) => i));
});

test("glue: a paste over 4 MiB sends nothing and tells the user; a 4 MiB paste goes out whole", () => {
  const before = binaryFrames().length;
  terminals[0].handlers.data("가".repeat(Math.floor(INPUT_QUEUE / 3) + 1));
  assert.equal(binaryFrames().length, before, "refused whole");
  assert.notEqual(element("board-notice").textContent, "");
  assert.equal(element("board-notice").hidden, false);
  terminals[0].handlers.data("a".repeat(INPUT_QUEUE));
  const frames = binaryFrames().slice(before);
  assert.equal(frames.reduce((sum, f) => sum + f.length, 0), INPUT_QUEUE);
});

test("glue: copy fills the browser's copy event with the trimmed Hangul selection and clears it, even without a connection", () => {
  const copyOnce = (selection, mods, extra) => {
    const term = terminals[1];
    term.selection = selection;
    const event = key("C", { ctrlKey: true, shiftKey: true, ...mods }, extra);
    const handled = term.keyHandler(event);
    return { handled, event, term };
  };
  // attached
  let result = copyOnce("한글 😀   \n둘째 줄   ");
  assert.equal(result.handled, false);
  assert.equal(result.event.prevented, 1);
  assert.deepEqual(clipboard.copied.at(-1), ["text/plain", "한글 😀\n둘째 줄"]);
  assert.equal(result.term.selection, "");
  // refused (v1: taken over by another tab): input is off, copying still works
  sockets[0].onclose({ code: 4403 });
  sockets[1].onclose({ code: 4403 });
  assert.equal(terminals[0].options.disableStdin, true);
  assert.equal(terminals[1].options.disableStdin, true);
  const frames = binaryFrames().length;
  terminals[0].handlers.data("x");
  terminals[1].handlers.data("붙여넣기");
  assert.equal(binaryFrames().length, frames, "no typing or pasting without a connection");
  result = copyOnce("가나다   ");
  assert.equal(result.handled, false);
  assert.deepEqual(clipboard.copied.at(-1), ["text/plain", "가나다"]);
  // a dropped connection: same
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

test("glue: collapsing the panel and resetting the layout keep both terminals, their content and their scroll position", () => {
  // v1 intent: switching between the two tabs recreated nothing and moved no view. The tabs are gone (WBX-D42); the
  // equivalent operations that only change the page around the terminals are the panel toggle and the layout reset.
  const resets = terminals.map((t) => t.resets);
  const scrolled = scrollCalls.length;
  const instances = terminals.length;
  element("board-panel-toggle").fire("click");
  assert.equal(element("board-panel").hidden, true, "the panel collapsed");
  assert.equal(element("board-panel-toggle").attrs["aria-expanded"], "false");
  element("board-panel-toggle").fire("click");
  assert.equal(element("board-panel").hidden, false);
  element("board-reset-layout").fire("click");
  assert.equal(terminals.length, instances, "no terminal is recreated");
  assert.deepEqual(terminals.map((t) => t.resets), resets, "no terminal is reset");
  assert.equal(scrollCalls.length, scrolled, "no view moves");
  assert.equal(terminals.every((t) => t.disposed !== true), true, "no terminal is disposed");
});

test("pure state: a state message changes only the view and data only writes it (v1: per-channel state)", () => {
  const tab = attachedState();
  const before = JSON.stringify(board.describe(tab.state).view);
  const effects = tab.send({ type: "data", conn: 1, bytes: new TextEncoder().encode("한글") });
  assert.deepEqual(effects.map((e) => e.type), ["write"]);
  assert.equal(JSON.stringify(board.describe(tab.state).view), before);
  tab.send({ type: "message", conn: 1, message: { t: "state", terminal: { id: "s-1", kind: "shell", name: "빌드", state: "running" } } });
  assert.equal(board.describe(tab.state).view.name, "빌드");
});
