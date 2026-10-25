// TI-18 / seam T10: board.js against stub DOM, xterm and WebSocket globals (no browser). It checks the glue
// wiring only (csrf from the meta tag into the hello, keystrokes to binary frames with the epoch, output to the
// right terminal, banner and 연결 없음 text, pagehide release). Real rendering is a target-host check.
import test from "node:test";
import assert from "node:assert/strict";

const elements = new Map();
function element(id) {
  if (!elements.has(id)) {
    const classes = new Set(id === "panel-agent" ? ["panel", "active"] : []); // the server renders the agent tab first
    elements.set(id, {
      id, textContent: "", hidden: ["board-login", "board-reconnect", "board-banner", "board-notice"].includes(id), dataset: {}, listeners: {}, attrs: {},
      classList: { toggle: (name, on) => (on ? classes.add(name) : classes.delete(name)), contains: (name) => classes.has(name) },
      setAttribute(name, value) { this.attrs[name] = value; },
      addEventListener(type, handler) { this.listeners[type] = handler; },
    });
  }
  return elements.get(id);
}

const terminals = [];
class FakeTerminal {
  constructor() {
    this.rows = 30; this.cols = 100; this.options = {}; this.written = []; this.resets = 0; this.handlers = {};
    terminals.push(this);
  }
  loadAddon() {}
  open() {}
  focus() {}
  reset() { this.resets += 1; }
  write(data) { this.written.push(new TextDecoder().decode(data)); }
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

const timers = new Map();
const windowListeners = {};
let timerSeq = 0;
globalThis.document = {
  getElementById: element,
  querySelector: (selector) => (selector.includes("csrf-token") ? { content: "csrf-from-meta" } : null),
};
globalThis.window = { addEventListener: (type, handler) => { windowListeners[type] = handler; } };
globalThis.location = { protocol: "http:", host: "127.0.0.1:8780" };
globalThis.Terminal = FakeTerminal;
globalThis.FitAddon = { FitAddon: FakeFit };
globalThis.WebSocket = FakeWebSocket;
globalThis.requestAnimationFrame = (callback) => callback();
globalThis.setTimeout = (callback, ms) => { timerSeq += 1; timers.set(timerSeq, { callback, ms }); return timerSeq; };
globalThis.clearTimeout = (id) => { timers.delete(id); };

await import("../../../src/workbench/hub/web/static/board.js");

const json = (socket) => socket.sent.filter((item) => typeof item === "string").map((item) => JSON.parse(item));
const message = (socket, body) => socket.onmessage({ data: JSON.stringify(body) });
const frame = (channel, epoch, text) => {
  const payload = new TextEncoder().encode(text);
  const bytes = new Uint8Array(5 + payload.length);
  bytes[0] = channel;
  new DataView(bytes.buffer).setUint32(1, epoch, false);
  bytes.set(payload, 5);
  return bytes.buffer;
};

test("the page opens /ws/board and its hello carries the csrf token from the meta tag and the terminal sizes", () => {
  assert.equal(sockets.length, 1);
  assert.equal(sockets[0].url, "ws://127.0.0.1:8780/ws/board");
  assert.equal(element("board-connection").textContent, "연결 중");
  sockets[0].onopen();
  assert.deepEqual(json(sockets[0])[0], {
    t: "hello", v: 1, csrf: "csrf-from-meta", resume: null, sizes: { agent: [30, 100], shell: [30, 100] },
  });
});

test("attached enables input; keystrokes become binary frames with the epoch; output goes to its terminal", () => {
  const socket = sockets[0];
  terminals[0].handlers.data("x");
  assert.equal(socket.sent.filter((item) => typeof item !== "string").length, 0, "no input before attached");
  message(socket, { t: "attached", v: 1, run: "r", epoch: 7, heartbeat: 10, channels: { agent: { state: "running" }, shell: { state: "exited", exit_status: 0 } } });
  assert.equal(element("board-connection").textContent, "연결됨");
  assert.equal(terminals[0].resets, 1);
  assert.equal(element("state-shell").textContent, "종료됨 (종료 코드 0)");
  assert.equal(element("state-agent").textContent, "실행 중");
  terminals[1].handlers.data("ls");
  const binary = socket.sent.filter((item) => typeof item !== "string").at(-1);
  assert.deepEqual(Array.from(binary), [1, 0, 0, 0, 7, 0x6c, 0x73]);
  socket.onmessage({ data: frame(0, 7, "hello") });
  socket.onmessage({ data: frame(0, 6, "stale") });
  assert.deepEqual(terminals[0].written, ["hello"]);
  assert.deepEqual([...timers.values()].map((timer) => timer.ms).sort((a, b) => a - b), [10000, 10000, 20000]);
});

test("taken_over shows the banner and 다시 가져오기 reconnects with resume null", () => {
  const first = sockets[0];
  message(first, { t: "taken_over", epoch: 8 });
  first.onclose({ code: 4001 });
  assert.equal(element("board-banner").hidden, false);
  assert.equal(element("board-connection").textContent, "다른 곳에서 연결됨");
  assert.equal(terminals[0].options.disableStdin, true);
  assert.equal(sockets.length, 1, "no automatic reconnect");
  element("board-retake").listeners.click();
  assert.equal(sockets.length, 2);
  assert.equal(element("board-banner").hidden, true);
  sockets[1].onopen();
  assert.equal(json(sockets[1])[0].resume, null);
});

test("a network drop shows 연결 없음 and the reconnect button; pagehide releases with the epoch", () => {
  const socket = sockets[1];
  message(socket, { t: "attached", v: 1, run: "r", epoch: 9, heartbeat: 10, channels: {} });
  windowListeners.pagehide();
  assert.deepEqual(json(socket).at(-1), { t: "release", epoch: 9 });
  windowListeners.pageshow({ persisted: true });
  assert.equal(sockets.length, 3);
  sockets[2].onopen();
  assert.deepEqual(json(sockets[2])[0].resume, { run: "r", epoch: 9 });
  sockets[2].onclose({ code: 1006 });
  assert.equal(element("board-connection").textContent, "연결 없음");
  assert.equal(element("board-reconnect").hidden, false);
});

test("close 4401 shows 연결 없음 — 다시 로그인 with the login link and no reconnect button or timer", () => {
  const [id, pending] = [...timers.entries()].find(([, timer]) => timer.ms === 1000); // the backoff of the last test
  timers.delete(id);
  pending.callback();
  const socket = sockets.at(-1);
  socket.onopen();
  message(socket, { t: "attached", v: 1, run: "r", epoch: 11, heartbeat: 10, channels: {} });
  socket.onclose({ code: 4401 });
  assert.equal(element("board-connection").textContent, "연결 없음 — 다시 로그인");
  assert.equal(element("board-login").hidden, false);
  assert.equal(element("board-reconnect").hidden, true);
  assert.equal([...timers.values()].filter((timer) => timer.ms === 1000).length, 0, "no reconnect timer");
});
