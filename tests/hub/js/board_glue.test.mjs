// TI-18 / WBX-D1 behaviour 24 / seam S8: board.js against stub DOM, fetch, xterm and WebSocket globals (no browser).
// It checks the protocol v2 glue only: the page's first POST /api/board/open (csrf header), one /ws/board/{id} per
// tab, the v2 hello with the tab's size, keystrokes as bare binary frames, output to its own terminal, 연결 없음 text,
// pagehide release, the server size drawn without a resize back, the restart button. Real rendering is a target-host
// check. (v1: one /ws/board, channel+epoch frames, the takeover banner and 다시 가져오기 are gone.)
import test from "node:test";
import assert from "node:assert/strict";

const elements = new Map();
function element(id) {
  if (!elements.has(id)) {
    const classes = new Set(id === "panel-agent" ? ["panel", "active"] : []); // the server renders the agent tab first
    // The server renders agent-1's id on its panel; this run's first shell does not exist before the open call.
    const dataset = id === "panel-agent" ? { terminal: "a-000000000001" } : {};
    elements.set(id, {
      id, textContent: "", hidden: ["board-login", "board-reconnect", "board-notice"].includes(id), dataset, listeners: {}, attrs: {},
      classList: { toggle: (name, on) => (on ? classes.add(name) : classes.delete(name)), contains: (name) => classes.has(name) },
      setAttribute(name, value) { this.attrs[name] = value; },
      addEventListener(type, handler) { this.listeners[type] = handler; },
    });
  }
  return elements.get(id);
}

const terminals = [];
class FakeTerminal {
  constructor(options) {
    this.rows = 30; this.cols = 100; this.options = {}; this.written = []; this.raw = []; this.resets = 0; this.handlers = {};
    this.created = options; this.selection = ""; this.cleared = 0; this.bottomScrolls = 0;
    terminals.push(this);
  }
  attachCustomKeyEventHandler(handler) { this.keyHandler = handler; }
  hasSelection() { return this.selection !== ""; }
  getSelection() { return this.selection; }
  clearSelection() { this.selection = ""; this.cleared += 1; }
  scrollToBottom() { this.bottomScrolls += 1; }
  loadAddon() {}
  // xterm's DOM renderer creates its <style> elements from open() on: the nonce must already be wired by then.
  open() { this.styleNonceAtOpen = document.createElement("style").nonce; }
  focus() {}
  reset() { this.resets += 1; }
  write(data) { this.raw.push(data); this.written.push(new TextDecoder().decode(data)); }
  onData(handler) { this.handlers.data = handler; }
  onBinary(handler) { this.handlers.binary = handler; }
  onResize(handler) { this.handlers.resize = handler; }
  resize(cols, rows) { this.resizedTo = [rows, cols]; this.rows = rows; this.cols = cols; }
}
class FakeFit { fit() {} }

const sockets = [];
class FakeWebSocket {
  static OPEN = 1;
  constructor(url) { this.url = url; this.readyState = 1; this.sent = []; sockets.push(this); }
  send(data) { this.sent.push(data); }
  close() { this.readyState = 3; }
}

const timers = new Map();
const windowListeners = {};
let timerSeq = 0;
const documentListeners = {};
const clipboard = { copied: [], execResult: true };
globalThis.document = {
  getElementById: element,
  querySelector: (selector) => (selector.includes("csrf-token") ? { content: "csrf-from-meta" }
    : selector.includes("style-nonce") ? { content: "nonce-from-meta" } : null),
  createElement: (tagName) => ({ tagName: String(tagName).toUpperCase(), nonce: "" }),
  addEventListener: (type, handler) => { documentListeners[type] = handler; },
  // The browser's copy command: a copy event the page may fill (plain HTTP has no navigator.clipboard).
  execCommand(command) {
    if (command !== "copy" || !clipboard.execResult) return false;
    const event = { clipboardData: { setData: (type, text) => clipboard.copied.push([type, text]) },
      defaultPrevented: false, preventDefault() { this.defaultPrevented = true; } };
    if (documentListeners.copy) documentListeners.copy(event);
    clipboard.lastEventPrevented = event.defaultPrevented;
    return true;
  },
};
Object.defineProperty(globalThis, "navigator", { value: { platform: "Linux x86_64", userAgent: "test" }, configurable: true });
globalThis.isSecureContext = false;
globalThis.window = { addEventListener: (type, handler) => { windowListeners[type] = handler; } };
globalThis.location = { protocol: "http:", host: "127.0.0.1:8780" };
globalThis.Terminal = FakeTerminal;
globalThis.FitAddon = { FitAddon: FakeFit };
globalThis.WebSocket = FakeWebSocket;
globalThis.requestAnimationFrame = (callback) => callback();
globalThis.setTimeout = (callback, ms) => { timerSeq += 1; timers.set(timerSeq, { callback, ms }); return timerSeq; };
globalThis.clearTimeout = (id) => { timers.delete(id); };
const fetches = [];
const TERMINALS = [
  { id: "a-000000000001", kind: "agent", name: "agent-1", state: "starting", clients: 0, busy: false },
  { id: "s-000000000002", kind: "shell", name: "shell-1", state: "running", clients: 0, busy: false },
];
globalThis.fetch = (path, options) => {
  fetches.push({ path, options, body: JSON.parse(options.body) });
  const reply = path === "/api/board/open" ? { ok: true, terminals: TERMINALS }
    : { ok: true, terminal: { ...TERMINALS[1], state: "running", pid: 99 } };
  return Promise.resolve({ status: 200, json: () => Promise.resolve(reply) });
};
const confirms = [];
globalThis.confirm = (text) => { confirms.push(text); return true; };

await import("../../../src/workbench/hub/web/static/board.js");
await new Promise((resolve) => setImmediate(resolve)); // the open call's reply

const json = (socket) => socket.sent.filter((item) => typeof item === "string").map((item) => JSON.parse(item));
const message = (socket, body) => socket.onmessage({ data: JSON.stringify(body) });
const frame = (text) => new TextEncoder().encode(text).buffer;
const binaries = (socket) => socket.sent.filter((item) => typeof item !== "string");
const ATTACHED = (id, extra = {}) => ({ t: "attached", v: 2, terminal: TERMINALS.find((t) => t.id === id), size: [30, 100],
  clients: 1, heartbeat: 10, ...extra });

test("the style nonce from the meta tag is on <style> elements before any terminal opens; other tags get none", () => {
  assert.equal(terminals.length, 2);
  for (const term of terminals) assert.equal(term.styleNonceAtOpen, "nonce-from-meta");
  assert.equal(document.createElement("STYLE").nonce, "nonce-from-meta");
  assert.equal(document.createElement("div").nonce, "");
  assert.equal(document.createElement("textarea").nonce, "", "xterm's IME helper textarea is untouched");
});

test("the page asks POST /api/board/open with agent-1 visible and the csrf header, then opens one socket per tab", () => {
  assert.equal(fetches.length, 1);
  assert.equal(fetches[0].path, "/api/board/open");
  assert.equal(fetches[0].options.method, "POST");
  assert.equal(fetches[0].options.headers["X-CSRF-Token"], "csrf-from-meta");
  assert.deepEqual(fetches[0].body, { visible: ["a-000000000001"] });
  assert.deepEqual(sockets.map((socket) => socket.url),
    ["ws://127.0.0.1:8780/ws/board/a-000000000001", "ws://127.0.0.1:8780/ws/board/s-000000000002"]);
  assert.equal(element("board-connection").textContent, "연결 중");
  for (const socket of sockets) {
    socket.onopen();
    assert.deepEqual(json(socket)[0], { t: "hello", v: 2, csrf: "csrf-from-meta", size: [30, 100] });
  }
  assert.equal(element("name-shell").textContent, "shell-1");
});

test("attached enables input; keystrokes become bare binary frames; output goes to its own terminal", () => {
  const [agent, shell] = sockets;
  terminals[0].handlers.data("x");
  assert.equal(binaries(agent).length, 0, "no input before attached");
  message(agent, ATTACHED("a-000000000001", { terminal: { ...TERMINALS[0], state: "running" } }));
  message(shell, ATTACHED("s-000000000002", { terminal: { ...TERMINALS[1], state: "exited", exit_status: 0 } }));
  assert.equal(element("board-connection").textContent, "연결됨");
  assert.equal(terminals[0].resets, 1);
  assert.equal(element("state-shell").textContent, "종료됨 (종료 코드 0)");
  assert.equal(element("state-agent").textContent, "실행 중");
  terminals[1].handlers.data("ls");
  assert.deepEqual(Array.from(binaries(shell).at(-1)), [0x6c, 0x73]);
  agent.onmessage({ data: frame("hello") });
  assert.deepEqual(terminals[0].written, ["hello"]);
  assert.deepEqual(terminals[1].written, []);
});

test("a server size is drawn on the terminal and never sent back as a resize", () => {
  const shell = sockets[1];
  message(shell, { t: "size", rows: 40, cols: 120 });
  assert.deepEqual(terminals[1].resizedTo, [40, 120]);
  assert.equal(json(shell).filter((item) => item.t === "resize").length, 0);
  assert.equal([...timers.values()].filter((timer) => timer.ms === 150).length, 0, "no resize is pending");
});

test("Hangul output split across frames reaches xterm as bytes (its decoder streams); typed Hangul is UTF-8", () => {
  const socket = sockets[1];
  const bytes = new TextEncoder().encode("한글 출력");
  const at = terminals[1].raw.length;
  socket.onmessage({ data: bytes.slice(0, 2).buffer }); // inside the first syllable's 3 bytes
  socket.onmessage({ data: bytes.slice(2).buffer });
  const written = terminals[1].raw.slice(at);
  assert.ok(written.every((item) => item instanceof Uint8Array), "never a per-frame decoded string");
  assert.equal(new TextDecoder("utf-8", { fatal: true }).decode(Buffer.concat(written.map((item) => Buffer.from(item)))),
    "한글 출력");
  terminals[1].handlers.data("가ㄱ\u1100\u1161😀");
  assert.deepEqual(Buffer.from(binaries(socket).at(-1)), Buffer.from("가ㄱ\u1100\u1161😀", "utf8"));
});

test("the restart button asks first and posts confirm true for its terminal", async () => {
  element("restart-shell").listeners.click();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(confirms.length, 1);
  assert.deepEqual(fetches.at(-1).path, "/api/board/terminals/s-000000000002/restart");
  assert.deepEqual(fetches.at(-1).body, { confirm: true });
});

test("a network drop shows 연결 없음 and the reconnect button; pagehide releases (no epoch)", () => {
  const agent = sockets[0];
  windowListeners.pagehide();
  assert.deepEqual(json(agent).at(-1), { t: "release" });
  windowListeners.pageshow({ persisted: true });
  const again = sockets.at(-2);
  assert.equal(again.url, "ws://127.0.0.1:8780/ws/board/a-000000000001");
  again.onopen();
  assert.equal(json(again)[0].t, "hello");
  again.onclose({ code: 1006 });
  assert.equal(element("board-connection").textContent, "연결 없음");
  assert.equal(element("board-reconnect").hidden, false);
});

test("close 4401 shows 연결 없음 — 다시 로그인 with the login link and no reconnect button or timer", () => {
  for (const [id, timer] of [...timers.entries()]) if (timer.ms === 1000) timers.delete(id); // the drop's backoff
  element("board-reconnect").listeners.click();
  const socket = sockets.at(-1);
  socket.onopen();
  message(socket, ATTACHED("a-000000000001"));
  socket.onclose({ code: 4401 });
  assert.equal(element("board-connection").textContent, "연결 없음 — 다시 로그인");
  assert.equal(element("board-login").hidden, false);
  assert.equal(element("board-reconnect").hidden, true);
  assert.equal([...timers.values()].filter((timer) => timer.ms === 1000).length, 0, "no reconnect timer");
});

const keyEvent = (key, mods = {}, extra = {}) => ({
  type: "keydown", key, code: key.length === 1 ? `Key${key.toUpperCase()}` : key, keyCode: 0,
  ctrlKey: false, shiftKey: false, altKey: false, metaKey: false, isComposing: false, prevented: 0,
  preventDefault() { this.prevented += 1; }, stopPropagation() {}, ...mods, ...extra,
});

test("both terminals keep a 10000-line scrollback, return to the bottom on typing and never on output", () => {
  for (const term of terminals) {
    assert.ok(term.created.scrollback >= 10000, `scrollback ${term.created.scrollback}`);
    assert.notEqual(term.created.scrollOnUserInput, false);
    assert.equal(typeof term.keyHandler, "function", "a key handler for copy and paste");
  }
  assert.equal(terminals[0].bottomScrolls + terminals[1].bottomScrolls, 0, "output never yanks the view down");
});

test("Ctrl+Shift+C copies the selection through the copy event even while disconnected, Hangul intact", () => {
  assert.equal(element("board-connection").textContent, "연결 없음 — 다시 로그인");
  const term = terminals[1];
  term.selection = "한글 출력   \n둘째 줄 ";
  const event = keyEvent("C", { ctrlKey: true, shiftKey: true });
  assert.equal(term.keyHandler(event), false, "xterm does not see the key");
  assert.equal(event.prevented, 1, "the browser's own Ctrl+Shift+C does not run");
  assert.deepEqual(clipboard.copied.at(-1), ["text/plain", "한글 출력\n둘째 줄"]);
  assert.equal(clipboard.lastEventPrevented, true);
  assert.equal(term.selection, "", "the selection is cleared: the next Ctrl+C is ^C");
});

test("Ctrl+C copies only with a selection; Ctrl+V is left to the browser's paste; IME keys go to xterm", () => {
  const term = terminals[0];
  const copies = clipboard.copied.length;
  const plain = keyEvent("c", { ctrlKey: true });
  assert.equal(term.keyHandler(plain), true, "^C reaches the program");
  assert.equal(plain.prevented, 0);
  term.selection = "가나다";
  assert.equal(term.keyHandler(keyEvent("c", { ctrlKey: true })), false);
  assert.deepEqual(clipboard.copied.at(-1), ["text/plain", "가나다"]);
  assert.equal(clipboard.copied.length, copies + 1);
  for (const type of ["keypress", "keyup"]) {
    term.selection = "x";
    assert.equal(term.keyHandler(keyEvent("c", { ctrlKey: true, shiftKey: true }, { type })), false);
  }
  assert.equal(clipboard.copied.length, copies + 1, "only the keydown copies");
  const paste = keyEvent("v", { ctrlKey: true });
  assert.equal(term.keyHandler(paste), false, "xterm does not turn Ctrl+V into ^V");
  assert.equal(paste.prevented, 0, "the browser's paste event still happens");
  term.selection = "sel";
  assert.equal(term.keyHandler(keyEvent("c", { ctrlKey: true }, { keyCode: 229 })), true);
  assert.equal(term.keyHandler(keyEvent("ㅎ", {}, { isComposing: true })), true);
  assert.equal(term.keyHandler(keyEvent("PageUp", { shiftKey: true })), true);
});

test("without a copy command the clipboard API is used only in a secure context", () => {
  const writes = [];
  Object.defineProperty(globalThis, "navigator", {
    value: { platform: "Linux", userAgent: "t", clipboard: { writeText: (text) => { writes.push(text); return Promise.resolve(); } } },
    configurable: true,
  });
  clipboard.execResult = false;
  const term = terminals[0];
  term.selection = "보안 없음";
  term.keyHandler(keyEvent("C", { ctrlKey: true, shiftKey: true }));
  assert.deepEqual(writes, []);
  globalThis.isSecureContext = true;
  term.selection = "보안 있음  ";
  term.keyHandler(keyEvent("C", { ctrlKey: true, shiftKey: true }));
  assert.deepEqual(writes, ["보안 있음"]);
  clipboard.execResult = true;
  globalThis.isSecureContext = false;
});
