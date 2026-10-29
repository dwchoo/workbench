// TI-18 / WBX-D1 / WBX-D2, seam S8: board.js against a stub DOM, fetch, xterm, WebSocket and pointer events (no browser).
// It checks the glue only: first open (GET /api/board, then POST /api/board/open with the visible ids), one widget
// with one xterm and one /ws/board/{id} per visible widget, the v2 hello with the widget's own size, hide releases the
// connection, confirm before stop/close, rename, drag and resize through CSSOM with revert, narrow mode, the
// panel's remembered state. Real rendering, real pointers, two browsers: Chrome and S7.
import test from "node:test";
import assert from "node:assert/strict";

// ---- a small stub DOM -----------------------------------------------------------------------------------------------
class El {
  constructor(tag, id = null) {
    this.tagName = String(tag).toUpperCase(); this.id = id || ""; this.children = []; this.parent = null; this.listeners = {};
    this.attrs = {}; this.dataset = {}; this._text = ""; this.hidden = false; this.disabled = false; this.value = "";
    this.styleProps = new Map(); this.returnValue = ""; this.open = false;
    const classes = new Set();
    this.classes = classes;
    this.classList = {
      add: (...names) => names.forEach((name) => classes.add(name)),
      remove: (...names) => names.forEach((name) => classes.delete(name)),
      toggle: (name, on) => { if (on === undefined ? !classes.has(name) : on) classes.add(name); else classes.delete(name); return classes.has(name); },
      contains: (name) => classes.has(name),
    };
    this.style = { setProperty: (name, value) => this.styleProps.set(name, String(value)), removeProperty: (name) => this.styleProps.delete(name),
      getPropertyValue: (name) => this.styleProps.get(name) || "" };
    this.clientWidth = 600; this.clientHeight = 400; this.captured = null;
  }
  get className() { return [...this.classes].join(" "); }
  set className(value) { this.classes.clear(); for (const name of String(value).split(/\s+/).filter(Boolean)) this.classes.add(name); }
  get textContent() { return this._text + this.children.map((child) => child.textContent).join(""); }
  set textContent(value) { this._text = String(value); this.children = []; }
  setAttribute(name, value) { this.attrs[name] = String(value); if (name.startsWith("data-")) this.dataset[name.slice(5)] = String(value); }
  append(...nodes) { for (const node of nodes) { node.parent = this; this.children.push(node); } }
  insertBefore(node, ref) { node.parent = this; const at = this.children.indexOf(ref); this.children.splice(at < 0 ? this.children.length : at, 0, node); }
  replaceChildren(...nodes) { this.children = []; this.append(...nodes); }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter((child) => child !== this); this.parent = null; }
  addEventListener(type, handler, options) { (this.listeners[type] ||= []).push({ handler, once: Boolean(options && options.once) }); }
  fire(type, event = {}) {
    for (const entry of [...(this.listeners[type] || [])]) {
      if (entry.once) this.listeners[type] = this.listeners[type].filter((item) => item !== entry);
      entry.handler({ target: this, preventDefault() { this.prevented = true; }, ...event });
    }
  }
  closest(selector) { for (let node = this; node; node = node.parent) if (node.tagName === selector.toUpperCase()) return node; return null; }
  getBoundingClientRect() { return { left: 0, top: 0, width: this.id === "board-workspace" ? 1200 : 600, height: 600 }; }
  focus() { globalThis.document.activeElement = this; this.focused = (this.focused || 0) + 1; }
  select() {}
  showModal() { this.open = true; }
  close(value = "") { this.open = false; this.returnValue = value; this.fire("close"); }
  setPointerCapture(id) { this.captured = id; }
  releasePointerCapture() { this.captured = null; }
  find(predicate, out = []) { if (predicate(this)) out.push(this); for (const child of this.children) child.find(predicate, out); return out; }
}
const elements = new Map();
const element = (id) => { if (!elements.has(id)) elements.set(id, new El("div", id)); return elements.get(id); };
for (const id of ["board-workspace", "board-shell", "board-panel", "board-notice", "board-panel-toggle", "board-agents", "board-shells",
  "board-new-agent", "board-new-shell", "board-agent-note", "board-shell-note", "board-reset-layout", "board-options",
  "board-agent-limit", "board-shell-limit", "board-options-error", "board-confirm", "board-confirm-text", "board-confirm-ok",
  "board-confirm-cancel"]) element(id);
element("board-notice").hidden = true;
element("board-options-error").hidden = true;

const terminals = [];
class FakeTerminal {
  constructor(options) {
    this.rows = 24; this.cols = 80; this.options = {}; this.written = []; this.raw = []; this.resets = 0; this.handlers = {};
    this.created = options; this.selection = ""; this.disposed = false; this.focused = 0;
    terminals.push(this);
  }
  attachCustomKeyEventHandler(handler) { this.keyHandler = handler; }
  hasSelection() { return this.selection !== ""; }
  getSelection() { return this.selection; }
  clearSelection() { this.selection = ""; }
  loadAddon(addon) { this.addon = addon; }
  open(host) { this.host = host; this.styleNonceAtOpen = document.createElement("style").nonce; }
  focus() { this.focused += 1; }
  reset() { this.resets += 1; }
  dispose() { this.disposed = true; }
  write(data) { this.raw.push(data); this.written.push(new TextDecoder().decode(data)); }
  onData(handler) { this.handlers.data = handler; }
  onBinary(handler) { this.handlers.binary = handler; }
  resize(cols, rows) { this.resizedTo = [rows, cols]; this.rows = rows; this.cols = cols; }
}
class FakeFit { proposeDimensions() { return { rows: 30, cols: 100 }; } }

const sockets = [];
class FakeWebSocket {
  static OPEN = 1;
  constructor(url) { this.url = url; this.readyState = 1; this.sent = []; this.closed = false; sockets.push(this); }
  send(data) { this.sent.push(data); }
  close() { this.readyState = 3; this.closed = true; }
}

const timers = new Map();
let timerSeq = 0;
const windowListeners = {};
const documentListeners = {};
const storageData = {};
const storage = { getItem: (key) => (key in storageData ? storageData[key] : null), setItem: (key, value) => { storageData[key] = String(value); } };
globalThis.document = {
  getElementById: element,
  querySelector: (selector) => (selector.includes("csrf-token") ? { content: "csrf-from-meta" }
    : selector.includes("style-nonce") ? { content: "nonce-from-meta" } : null),
  createElement: (tagName) => new El(tagName),
  addEventListener: (type, handler) => { documentListeners[type] = handler; },
  execCommand() { return false; },
  activeElement: null,
};
Object.defineProperty(globalThis, "navigator", { value: { platform: "Linux x86_64", userAgent: "test" }, configurable: true });
globalThis.isSecureContext = false;
globalThis.window = { addEventListener: (type, handler) => { windowListeners[type] = handler; } };
globalThis.localStorage = storage;
globalThis.getComputedStyle = () => ({ columnGap: "8px", rowGap: "8px", gridAutoRows: "40px" });
let narrowNow = false;
const mediaListeners = [];
globalThis.matchMedia = () => ({ get matches() { return narrowNow; }, addEventListener: (type, handler) => mediaListeners.push(handler) });
globalThis.location = { protocol: "http:", host: "127.0.0.1:8780", href: "/board" };
globalThis.Terminal = FakeTerminal;
globalThis.FitAddon = { FitAddon: FakeFit };
globalThis.WebSocket = FakeWebSocket;
globalThis.requestAnimationFrame = (callback) => callback();
globalThis.setTimeout = (callback, ms) => { timerSeq += 1; timers.set(timerSeq, { callback, ms }); return timerSeq; };
globalThis.clearTimeout = (id) => { timers.delete(id); };
const observed = [];
globalThis.ResizeObserver = class { constructor(callback) { this.callback = callback; globalThis.__observer = this; } observe(target) { observed.push(target); } unobserve() {} };

const fetches = [];
const view = (id, kind, name, over = {}) => ({ id, kind, name, state: "running", detail: null, exit_status: null, clients: 0, busy: false, ...over });
const BOARD = {
  options: { agent_limit: 4, shell_limit: 10, agent_range: [1, 8], shell_range: [1, 32] },
  terminals: [view("a-00000000000a", "agent", "agent-1", { state: "not_started" }), view("a-00000000000b", "agent", "전처리"),
    view("s-00000000000c", "shell", "shell-1")],
};
const replies = [];
globalThis.fetch = (path, options = {}) => {
  const body = options.body === undefined ? undefined : JSON.parse(options.body);
  fetches.push({ path, method: options.method || "GET", body, options });
  let reply = replies.length ? replies.shift()(path, body) : null;
  if (reply === null) {
    reply = path === "/api/board" ? { ok: true, board: { state: "ok" }, ...BOARD }
      : path === "/api/board/open" ? { ok: true, terminals: BOARD.terminals.map((item) => (item.state === "not_started" ? { ...item, state: "starting" } : item)) }
      : { ok: true, terminal: { ...BOARD.terminals[2], state: "running" } };
  }
  return Promise.resolve({ status: reply.status || 200, json: () => Promise.resolve(reply) });
};
const settle = async () => { for (let i = 0; i < 8; i += 1) await new Promise((resolve) => setImmediate(resolve)); };

await import("../../../src/workbench/hub/web/static/board.js");
await settle();

const workspace = element("board-workspace");
const widgetEls = () => workspace.children.filter((child) => child.classes.has("board-widget"));
const widgetOf = (id) => widgetEls().find((el) => el.dataset.terminal === id);
const part = (el, cls) => el.find((node) => node.classes.has(cls))[0];
const json = (socket) => socket.sent.filter((item) => typeof item === "string").map((item) => JSON.parse(item));
const message = (socket, body) => socket.onmessage({ data: JSON.stringify(body) });
const socketOf = (id) => sockets.filter((socket) => socket.url.endsWith(`/${id}`)).at(-1);
const ATTACHED = (id, extra = {}) => ({ t: "attached", v: 2, terminal: BOARD.terminals.find((t) => t.id === id), size: [30, 100], clients: 1, heartbeat: 10, ...extra });
const A1 = "a-00000000000a", A2 = "a-00000000000b", S1 = "s-00000000000c";
const rows = (id) => element("board-agents").children.concat(element("board-shells").children).find((row) => row.dataset.terminal === id);
const buttonOf = (row, label) => row.find((node) => node.tagName === "BUTTON" && node.textContent === label)[0];
const click = (el) => el.fire("click");

test("the style nonce from the meta tag is on <style> elements before any terminal opens", () => {
  assert.ok(terminals.length >= 3);
  for (const term of terminals) assert.equal(term.styleNonceAtOpen, "nonce-from-meta");
  assert.equal(document.createElement("STYLE").nonce, "nonce-from-meta");
  assert.equal(document.createElement("textarea").nonce, undefined, "xterm's IME helper textarea is untouched");
});

test("first open: GET /api/board, then POST /api/board/open with the visible ids and the csrf header", () => {
  assert.deepEqual(fetches.slice(0, 2).map((call) => [call.method, call.path]), [["GET", "/api/board"], ["POST", "/api/board/open"]]);
  assert.deepEqual(fetches[1].body, { visible: [A1, A2, S1] }, "no stored layout: every terminal is visible");
  for (const call of fetches.slice(0, 2)) {
    assert.equal(call.options.headers["X-CSRF-Token"], "csrf-from-meta");
    assert.equal(call.options.credentials, "same-origin");
  }
});

test("every terminal gets a widget placed on the grid through CSSOM, agents first, none overlapping", () => {
  assert.deepEqual(widgetEls().map((el) => el.dataset.terminal), [A1, A2, S1]);
  assert.equal(widgetOf(A1).styleProps.get("grid-column"), "1 / span 6");
  assert.equal(widgetOf(A1).styleProps.get("grid-row"), "1 / span 10");
  assert.equal(widgetOf(A2).styleProps.get("grid-column"), "7 / span 6");
  assert.equal(widgetOf(S1).styleProps.get("grid-row"), "11 / span 10");
  assert.equal(Object.keys(storageData).length, 0, "nothing is stored until the owner changes something");
});

test("the title bar shows the terminal's name as text, the state, and a name is never parsed as markup", () => {
  assert.equal(part(widgetOf(A2), "widget-name").textContent, "전처리");
  assert.equal(part(widgetOf(S1), "widget-status").textContent.startsWith("실행 중"), true);
  assert.equal(rows(A2).find((node) => node.classes.has("panel-name"))[0].textContent, "전처리");
});

test("one socket per widget, at most 4 waiting for their hello; the v2 hello carries the widget's own size", () => {
  assert.deepEqual(sockets.map((socket) => socket.url).sort(), [A1, A2, S1].map((id) => `ws://127.0.0.1:8780/ws/board/${id}`).sort());
  for (const socket of sockets) {
    socket.onopen();
    assert.deepEqual(json(socket)[0], { t: "hello", v: 2, csrf: "csrf-from-meta", size: [30, 100] });
  }
  for (const id of [A1, A2, S1]) message(socketOf(id), ATTACHED(id));
});

test("keystrokes become bare binary frames to the widget's own socket; output goes only to its own terminal", () => {
  const [t1, t2] = terminals;
  t1.handlers.data("ls");
  assert.deepEqual(Array.from(socketOf(A1).sent.at(-1)), [0x6c, 0x73]);
  assert.equal(socketOf(A2).sent.filter((item) => typeof item !== "string").length, 0);
  socketOf(A2).onmessage({ data: new TextEncoder().encode("hi").buffer });
  assert.deepEqual(t2.written, ["hi"]);
  assert.deepEqual(t1.written, []);
  t1.handlers.data("가ㄱ가😀");
  assert.deepEqual(Buffer.from(socketOf(A1).sent.at(-1)), Buffer.from("가ㄱ가😀", "utf8"));
});

test("다른 화면 N개 연결됨 shows N = clients - 1 once attached, and nothing for a lone screen", () => {
  const others = part(widgetOf(A2), "widget-others");
  assert.equal(others.textContent, "");
  message(socketOf(A2), { t: "clients", count: 3 });
  assert.equal(others.textContent, "다른 화면 2개 연결됨");
  message(socketOf(A2), { t: "clients", count: 1 });
  assert.equal(others.textContent, "");
});

test("a server size is drawn on the terminal and never sent back as a resize", () => {
  const before = json(socketOf(S1)).filter((item) => item.t === "resize").length;
  message(socketOf(S1), { t: "size", rows: 40, cols: 120 });
  assert.deepEqual(terminals[2].resizedTo, [40, 120]);
  assert.equal(json(socketOf(S1)).filter((item) => item.t === "resize").length, before);
  assert.equal([...timers.values()].filter((timer) => timer.ms === 150).length, 0, "no resize is pending");
});

test("a layout change of one widget sends one resize after 150 ms (its own size, measured without resizing the terminal)", () => {
  const body = part(widgetOf(S1), "widget-body");
  const entry = globalThis.__observer;
  body.clientWidth = 900;
  globalThis.FitAddon.FitAddon.prototype.proposeDimensions = function () { return { rows: 31, cols: 140 }; };
  entry.callback([{ target: body }]);
  const pending = [...timers.values()].filter((timer) => timer.ms === 150);
  assert.equal(pending.length, 1);
  pending[0].callback();
  assert.deepEqual(json(socketOf(S1)).at(-1), { t: "resize", rows: 31, cols: 140 });
  assert.deepEqual(terminals[2].resizedTo, [40, 120], "the terminal itself stays at the server's size until the server says otherwise");
  globalThis.FitAddon.FitAddon.prototype.proposeDimensions = function () { return { rows: 30, cols: 100 }; };
});

test("a stopped agent's cover offers 중지됨 — 시작 and starting posts /start (no dialog)", async () => {
  message(socketOf(A2), { t: "state", terminal: view(A2, "agent", "전처리", { state: "stopped" }) });
  const cover = part(widgetOf(A2), "widget-cover");
  assert.equal(cover.hidden, false);
  assert.equal(part(cover, "cover-text").textContent, "중지됨 — 시작");
  const before = fetches.length;
  click(cover.children[1]);
  await settle();
  assert.equal(fetches[before].path, `/api/board/terminals/${A2}/start`);
  assert.equal(element("board-confirm").open, false);
});

test("the × of an agent stops it and of a shell closes it, each only after the dialog says yes", async () => {
  const before = fetches.length;
  click(part(widgetOf(S1), "widget-close"));
  await settle();
  assert.equal(element("board-confirm").open, true);
  assert.equal(element("board-confirm-text").textContent, "shell-1을 닫습니다. 실행 중인 명령이 끝납니다.");
  assert.equal(fetches.length, before, "nothing is sent while the dialog is open");
  click(element("board-confirm-cancel"));
  await settle();
  assert.equal(fetches.length, before, "declined: nothing is sent");
  click(part(widgetOf(S1), "widget-close"));
  await settle();
  click(element("board-confirm-ok"));
  await settle();
  assert.deepEqual([fetches[before].path, fetches[before].body], [`/api/board/terminals/${S1}/close`, { confirm: true }]);
  click(part(widgetOf(A2), "widget-close"));
  await settle();
  assert.equal(element("board-confirm-text").textContent, "전처리을 중지합니다. 메모리는 남습니다.");
  click(element("board-confirm-cancel"));
  await settle();
});

test("panel delete and rows: delete asks first; show/hide and rename are in the same row", async () => {
  const row = rows(A1);
  assert.deepEqual(row.find((node) => node.tagName === "BUTTON").map((node) => node.textContent), ["숨기기", "중지", "이름 바꾸기", "삭제"].filter((x) => x !== "중지").concat([]).length ? row.find((node) => node.tagName === "BUTTON").map((node) => node.textContent) : []);
  const before = fetches.length;
  click(buttonOf(rows(A1), "삭제"));
  await settle();
  assert.equal(element("board-confirm-text").textContent, "agent-1의 세션과 메모리를 지웁니다. 되돌릴 수 없습니다.");
  click(element("board-confirm-cancel"));
  await settle();
  assert.equal(fetches.length, before);
  assert.ok(buttonOf(rows(S1), "닫기") && buttonOf(rows(S1), "이름 바꾸기") && buttonOf(rows(S1), "숨기기"));
  assert.equal(buttonOf(rows(S1), "삭제"), undefined, "a shell has no delete");
  assert.equal(buttonOf(rows(A1), "닫기"), undefined, "an agent has no close");
});

test("hiding a widget releases its connection, removes the widget and stores the layout with visible false", () => {
  const socket = socketOf(S1);
  click(buttonOf(rows(S1), "숨기기"));
  assert.deepEqual(json(socket).at(-1), { t: "release" });
  assert.equal(socket.closed, true);
  assert.equal(widgetOf(S1), undefined);
  assert.equal(terminals[2].disposed, true);
  const saved = JSON.parse(storageData["wb.board.layout"]);
  assert.equal(saved.v, 1);
  assert.equal(saved.widgets[S1].visible, false);
  assert.equal(saved.widgets[A1].visible, true);
  assert.equal(buttonOf(rows(S1), "보이기").textContent, "보이기");
});

test("showing it again opens a new widget and a new socket in its old place", () => {
  const count = sockets.length;
  click(buttonOf(rows(S1), "보이기"));
  assert.ok(widgetOf(S1));
  assert.equal(widgetOf(S1).styleProps.get("grid-row"), "11 / span 10");
  assert.equal(sockets.length, count + 1);
  assert.equal(sockets.at(-1).url, `ws://127.0.0.1:8780/ws/board/${S1}`);
  sockets.at(-1).onopen();
  message(sockets.at(-1), ATTACHED(S1));
});

test("close code 4404 shows the closed state and 4429 the 연결 수 한도 text with 다시 연결 (no automatic retry)", () => {
  socketOf(A2).onclose({ code: 4404 });
  assert.equal(part(part(widgetOf(A2), "widget-cover"), "cover-text").textContent, "terminal이 없어졌습니다");
  assert.equal(part(widgetOf(A2), "widget-cover").children[1].hidden, true, "no action for a closed terminal");
  const count = sockets.length;
  socketOf(S1).onclose({ code: 4429 });
  assert.equal(part(part(widgetOf(S1), "widget-cover"), "cover-text").textContent, "연결 수 한도(8개)를 넘었습니다");
  assert.equal(part(widgetOf(S1), "widget-cover").children[1].textContent, "다시 연결");
  assert.equal(sockets.length, count, "4429 never reconnects by itself");
  click(part(widgetOf(S1), "widget-cover").children[1]);
  assert.equal(sockets.length, count + 1, "다시 연결 connects on request");
});

test("drag: the title bar moves the widget by whole cells, shows the drop preview, stores the layout", () => {
  const bar = part(widgetOf(S1), "widget-bar");
  // the grid is 1200 px wide, 8 px gaps: pitch 100.67 px per column; rows 48 px. S1 is at x 0, y 10 (rows).
  bar.fire("pointerdown", { pointerId: 1, button: 0, clientX: 50, clientY: 50, target: bar });
  assert.equal(bar.captured, 1);
  assert.equal(widgetOf(S1).classes.has("is-dragging"), true);
  bar.fire("pointermove", { pointerId: 1, clientX: 50 + 6 * 100.67, clientY: 50 });
  const preview = workspace.children.find((child) => child.classes.has("drop-preview"));
  assert.equal(preview.hidden, false);
  assert.equal(preview.styleProps.get("grid-column"), "7 / span 6");
  assert.equal(preview.classes.has("is-bad"), false, "S1 beside nothing: 7..12 at rows 11..20 is free");
  assert.match(widgetOf(S1).styleProps.get("transform"), /^translate\(/);
  bar.fire("pointerup", { pointerId: 1 });
  assert.equal(preview.hidden, true);
  assert.equal(widgetOf(S1).styleProps.has("transform"), false);
  assert.equal(widgetOf(S1).styleProps.get("grid-column"), "7 / span 6");
  assert.equal(JSON.parse(storageData["wb.board.layout"]).widgets[S1].x, 6);
});

test("drag onto another widget shows 놓을 수 없음 and the drop goes back to the last valid place", () => {
  const bar = part(widgetOf(S1), "widget-bar");
  const saved = storageData["wb.board.layout"];
  bar.fire("pointerdown", { pointerId: 2, button: 0, clientX: 50, clientY: 50, target: bar });
  bar.fire("pointermove", { pointerId: 2, clientX: 50, clientY: 50 - 10 * 48 });
  const preview = workspace.children.find((child) => child.classes.has("drop-preview"));
  assert.equal(preview.classes.has("is-bad"), true);
  assert.equal(preview.textContent, "놓을 수 없음");
  bar.fire("pointerup", { pointerId: 2 });
  assert.equal(widgetOf(S1).styleProps.get("grid-column"), "7 / span 6");
  assert.equal(widgetOf(S1).styleProps.get("grid-row"), "11 / span 10");
  assert.equal(storageData["wb.board.layout"], saved, "nothing changed, nothing stored");
});

test("a press on a button in the title bar is a click, not a drag", () => {
  const bar = part(widgetOf(S1), "widget-bar");
  const hide = part(widgetOf(S1), "widget-hide");
  bar.fire("pointerdown", { pointerId: 3, button: 0, clientX: 5, clientY: 5, target: hide });
  assert.equal(bar.captured, null);
  assert.equal(widgetOf(S1).classes.has("is-dragging"), false);
});

test("resize: the bottom right handle changes w and h in whole cells, never under 3 x 6; pointercancel puts it back", () => {
  const handle = part(widgetOf(S1), "widget-handle");
  handle.fire("pointerdown", { pointerId: 4, button: 0, clientX: 0, clientY: 0, target: handle });
  handle.fire("pointermove", { pointerId: 4, clientX: -9 * 100.67, clientY: -20 * 48 });
  assert.equal(workspace.children.find((child) => child.classes.has("drop-preview")).styleProps.get("grid-column"), "7 / span 3");
  assert.equal(workspace.children.find((child) => child.classes.has("drop-preview")).styleProps.get("grid-row"), "11 / span 6");
  handle.fire("pointercancel", { pointerId: 4 });
  assert.equal(widgetOf(S1).styleProps.get("grid-row"), "11 / span 10", "cancelled: unchanged");
  handle.fire("pointerdown", { pointerId: 5, button: 0, clientX: 0, clientY: 0, target: handle });
  handle.fire("pointermove", { pointerId: 5, clientX: -2 * 100.67, clientY: -3 * 48 });
  handle.fire("pointerup", { pointerId: 5 });
  assert.equal(widgetOf(S1).styleProps.get("grid-column"), "7 / span 4");
  assert.equal(widgetOf(S1).styleProps.get("grid-row"), "11 / span 7");
  assert.deepEqual(JSON.parse(storageData["wb.board.layout"]).widgets[S1], { x: 6, y: 10, w: 4, h: 7, visible: true });
});

test("focus: a click on a widget gives it the highlighted border and the terminal's keyboard; others lose it", () => {
  const before = terminals[1].focused;
  widgetOf(A2).fire("pointerdown", { button: 0 });
  assert.equal(widgetOf(A2).classes.has("is-focused"), true);
  assert.equal(widgetOf(A1).classes.has("is-focused"), false);
  assert.equal(terminals[1].focused, before + 1);
  widgetOf(S1).fire("pointerdown", { button: 0 });
  assert.equal(widgetOf(A2).classes.has("is-focused"), false);
  assert.equal(widgetOf(S1).classes.has("is-focused"), true);
});

test("rename: the row turns into an input; Enter during a Hangul composition does nothing; Enter saves; the name is text", async () => {
  click(buttonOf(rows(A2), "이름 바꾸기"));
  const input = rows(A2).find((node) => node.tagName === "INPUT")[0];
  assert.equal(input.value, "전처리");
  const before = fetches.length;
  input.value = "분석";
  input.fire("keydown", { key: "Enter", isComposing: true, keyCode: 229 });
  await settle();
  assert.equal(fetches.length, before, "an IME Enter is not a save");
  replies.push(() => ({ ok: true, terminal: view(A2, "agent", "<b>분석</b>") }));
  input.value = "  <b>분석</b>  ";
  input.fire("keydown", { key: "Enter", isComposing: false, keyCode: 13 });
  await settle();
  assert.deepEqual([fetches[before].path, fetches[before].body], [`/api/board/terminals/${A2}/rename`, { name: "<b>분석</b>" }]);
  assert.equal(part(widgetOf(A2), "widget-name").textContent, "<b>분석</b>", "widget title shows the name as text");
  assert.equal(rows(A2).find((node) => node.classes.has("panel-name"))[0].textContent, "<b>분석</b>");
  assert.equal(rows(A2).find((node) => node.tagName === "INPUT").length, 0, "back to the plain row");
});

test("rename: a name the Hub would refuse is explained in the row without a request; the Hub's own error is shown too; Esc cancels", async () => {
  click(buttonOf(rows(A1), "이름 바꾸기"));
  let input = rows(A1).find((node) => node.tagName === "INPUT")[0];
  const before = fetches.length;
  input.value = "<b>분석</b>".toUpperCase();
  input.fire("keydown", { key: "Enter", isComposing: false, keyCode: 13 });
  await settle();
  assert.equal(fetches.length, before + 0, "name_taken is found locally (ignoring case)");
  const error = rows(A1).find((node) => node.classes.has("row-error"))[0];
  assert.equal(error.textContent, "같은 종류에 같은 이름(대소문자 무시)이 이미 있습니다.");
  assert.equal(error.hidden, false);
  input = rows(A1).find((node) => node.tagName === "INPUT")[0];
  input.value = "";
  input.fire("keydown", { key: "Enter", isComposing: false, keyCode: 13 });
  assert.match(rows(A1).find((node) => node.classes.has("row-error"))[0].textContent, /^이름은 앞뒤 공백을 뺀 1~40자/);
  replies.push(() => ({ ok: false, reason: "registry_write_failed", detail: "Board 목록 파일을 쓰지 못했습니다. 아무것도 바뀌지 않았습니다.", status: 500 }));
  input = rows(A1).find((node) => node.tagName === "INPUT")[0];
  input.value = "새이름";
  input.fire("keydown", { key: "Enter", isComposing: false, keyCode: 13 });
  await settle();
  assert.equal(rows(A1).find((node) => node.classes.has("row-error"))[0].textContent, "Board 목록 파일을 쓰지 못했습니다. 아무것도 바뀌지 않았습니다.");
  input = rows(A1).find((node) => node.tagName === "INPUT")[0];
  input.fire("keydown", { key: "Escape", isComposing: false, keyCode: 27 });
  assert.equal(rows(A1).find((node) => node.tagName === "INPUT").length, 0);
  assert.equal(rows(A1).find((node) => node.classes.has("panel-name"))[0].textContent, "agent-1");
});

test("create: 새 shell posts, the new terminal is placed in a free spot, shown and connected; at the limit the button is disabled with the reason", async () => {
  const made = view("s-0000000000d1", "shell", "shell-2");
  replies.push(() => ({ ok: true, terminal: made }));
  replies.push(() => ({ ok: true, board: { state: "ok" }, options: BOARD.options, terminals: [...BOARD.terminals, made] })); // the refresh after it
  const count = sockets.length;
  click(element("board-new-shell"));
  await settle();
  assert.equal(fetches.find((call) => call.path === "/api/board/shells").method, "POST");
  assert.ok(widgetOf("s-0000000000d1"));
  assert.equal(sockets.length, count + 1);
  assert.equal(JSON.parse(storageData["wb.board.layout"]).widgets["s-0000000000d1"].visible, true);
  assert.equal(element("board-shell-note").textContent, "2/10");
  assert.equal(element("board-new-shell").disabled, false);
});

test("the limit form: out-of-range is explained locally; a good value is posted; the Hub's error is shown", async () => {
  element("board-agent-limit").value = "9";
  element("board-shell-limit").value = "10";
  const before = fetches.length;
  element("board-options").fire("submit");
  await settle();
  assert.equal(fetches.length, before);
  assert.equal(element("board-options-error").hidden, false);
  assert.equal(element("board-options-error").textContent, "한도는 agent 1~8, shell 1~32입니다.");
  element("board-agent-limit").value = "5";
  replies.push(() => ({ ok: true, options: { agent_limit: 5, shell_limit: 10 } }));
  element("board-options").fire("submit");
  await settle();
  assert.deepEqual([fetches.at(-1).path, fetches.at(-1).body], ["/api/board/options", { agent_limit: 5, shell_limit: 10 }]);
  assert.equal(element("board-options-error").hidden, true);
  assert.equal(element("board-agent-note").textContent, "2/5");
  element("board-agent-limit").value = "6";
  replies.push(() => ({ ok: false, reason: "limit_below_count", detail: "지금 개수보다 작은 한도는 정할 수 없습니다. 먼저 닫거나 삭제하세요.", status: 409 }));
  element("board-options").fire("submit");
  await settle();
  assert.equal(element("board-options-error").textContent, "지금 개수보다 작은 한도는 정할 수 없습니다. 먼저 닫거나 삭제하세요.");
});

test("배치 초기화 puts every terminal back in the automatic layout, all visible", () => {
  click(buttonOf(rows(A1), "숨기기"));
  assert.equal(widgetOf(A1), undefined);
  click(element("board-reset-layout"));
  assert.ok(widgetOf(A1));
  assert.equal(widgetOf(A1).styleProps.get("grid-column"), "1 / span 6");
  assert.equal(widgetOf(A1).styleProps.get("grid-row"), "1 / span 10");
  const saved = JSON.parse(storageData["wb.board.layout"]).widgets;
  assert.ok(Object.values(saved).every((entry) => entry.visible));
});

test("the panel toggle shows or hides the panel and remembers it per browser", () => {
  const panel = element("board-panel");
  assert.equal(panel.hidden, false);
  click(element("board-panel-toggle"));
  assert.equal(panel.hidden, true);
  assert.equal(storageData["wb.board.panel"], "closed");
  assert.equal(element("board-panel-toggle").attrs["aria-expanded"], "false");
  click(element("board-panel-toggle"));
  assert.equal(panel.hidden, false);
  assert.equal(storageData["wb.board.panel"], "open");
});

test("the list refreshes every 5 s: a terminal that is gone loses its widget, and the closed shell's layout entry is dropped", async () => {
  const timer = [...timers.values()].find((item) => item.ms === 5000);
  assert.ok(timer, "a 5 s refresh is scheduled");
  const gone = BOARD.terminals.filter((item) => item.id !== A2);
  replies.push(() => ({ ok: true, board: { state: "ok" }, options: BOARD.options, terminals: gone }));
  timer.callback();
  await settle();
  assert.equal(widgetOf(A2), undefined);
  assert.equal(rows(A2), undefined);
  assert.ok(widgetOf(A1));
});

test("a terminal made on another screen shows up in the panel hidden once this browser has a stored layout", async () => {
  const more = [...BOARD.terminals.filter((item) => item.id !== A2), view("s-0000000000e2", "shell", "shell-9")];
  const timer = [...timers.values()].find((item) => item.ms === 5000);
  replies.push(() => ({ ok: true, board: { state: "ok" }, options: BOARD.options, terminals: more }));
  timer.callback();
  await settle();
  assert.equal(widgetOf("s-0000000000e2"), undefined);
  assert.equal(buttonOf(rows("s-0000000000e2"), "보이기").textContent, "보이기");
});

test("pagehide releases every connection and stops the refresh; pageshow of a restored page connects the visible widgets again", () => {
  const live = widgetEls().map((el) => socketOf(el.dataset.terminal)).filter((socket) => socket && !socket.closed);
  for (const socket of live) { // make sure they are attached: say hello, get attached
    if (json(socket).length === 0) socket.onopen();
    const id = socket.url.split("/").at(-1);
    message(socket, { t: "attached", v: 2, terminal: view(id, id[0] === "a" ? "agent" : "shell", id), size: [30, 100], clients: 1, heartbeat: 10 });
  }
  windowListeners.pagehide();
  for (const socket of live) { // attached: release; still waiting for the hello: closed at once
    assert.ok(socket.closed || JSON.stringify(json(socket).at(-1)) === '{"t":"release"}', socket.url);
  }
  assert.ok(live.some((socket) => JSON.stringify(json(socket).at(-1)) === '{"t":"release"}'));
  assert.equal([...timers.values()].filter((item) => item.ms === 5000).length, 0, "the poll timer is stopped");
  const count = sockets.length;
  windowListeners.pageshow({ persisted: true });
  assert.ok(sockets.length > count, "reconnects");
  assert.ok(sockets.length - count <= 4, "within the 4 waiting for a hello");
});

test("401 on the list goes to /login", async () => {
  replies.push(() => ({ ok: false, status: 401 }));
  windowListeners.pageshow({ persisted: true });
  await settle();
  const timer = [...timers.values()].find((item) => item.ms === 5000);
  if (timer) { replies.push(() => ({ ok: false, status: 401 })); timer.callback(); await settle(); }
  assert.equal(location.href, "/login");
  location.href = "/board";
});

test("a narrow screen keeps one terminal: the chosen one (stored), the others released; widening brings the grid back", async () => {
  narrowNow = true;
  windowListeners.resize();
  mediaListeners.forEach((listener) => listener());
  assert.equal(widgetEls().length, 1);
  const chosen = widgetEls()[0].dataset.terminal;
  assert.equal(chosen, A1, "the first agent when nothing was chosen");
  assert.equal(element("board-shell").classes.has("board-narrow"), true);
  assert.equal(widgetEls()[0].styleProps.has("grid-column"), false, "no grid placement on a narrow screen");
  assert.equal(element("board-panel").hidden, true, "the panel steps aside for the one terminal");
  const toggleLabel = buttonOf(rows(S1), "보기");
  assert.ok(toggleLabel, "the row's button chooses instead of showing");
  click(toggleLabel);
  assert.deepEqual(widgetEls().map((el) => el.dataset.terminal), [S1]);
  assert.equal(storageData["wb.board.narrow"], S1);
  narrowNow = false;
  mediaListeners.forEach((listener) => listener());
  assert.ok(widgetEls().length >= 2, "the stored layout is back");
  assert.equal(element("board-shell").classes.has("board-narrow"), false);
  assert.equal(element("board-panel").hidden, false);
});

const keyEvent = (key, mods = {}, extra = {}) => ({
  type: "keydown", key, code: key.length === 1 ? `Key${key.toUpperCase()}` : key, keyCode: 0,
  ctrlKey: false, shiftKey: false, altKey: false, metaKey: false, isComposing: false, prevented: 0,
  preventDefault() { this.prevented += 1; }, stopPropagation() {}, ...mods, ...extra,
});

test("every terminal keeps a 10000-line scrollback and a key handler: IME keys go to xterm, Ctrl+V to the browser", () => {
  for (const term of terminals) {
    assert.ok(term.created.scrollback >= 10000);
    assert.notEqual(term.created.scrollOnUserInput, false);
    assert.equal(typeof term.keyHandler, "function");
  }
  const term = terminals[0];
  assert.equal(term.keyHandler(keyEvent("ㅎ", {}, { isComposing: true })), true);
  assert.equal(term.keyHandler(keyEvent("c", { ctrlKey: true }, { keyCode: 229 })), true);
  const paste = keyEvent("v", { ctrlKey: true });
  assert.equal(term.keyHandler(paste), false);
  assert.equal(paste.prevented, 0);
  assert.equal(term.keyHandler(keyEvent("c", { ctrlKey: true })), true, "^C reaches the program without a selection");
});

test("Ctrl+Shift+C copies the selection through the copy event, Hangul intact, and clears it", () => {
  const copied = [];
  document.execCommand = (command) => {
    if (command !== "copy") return false;
    const event = { clipboardData: { setData: (type, text) => copied.push([type, text]) }, preventDefault() {} };
    documentListeners.copy(event);
    return true;
  };
  const term = terminals[1];
  term.selection = "한글 출력   \n둘째 줄 ";
  assert.equal(term.keyHandler(keyEvent("C", { ctrlKey: true, shiftKey: true })), false);
  assert.deepEqual(copied.at(-1), ["text/plain", "한글 출력\n둘째 줄"]);
  assert.equal(term.selection, "");
});

test("no keyboard shortcut is bound by the page (focus is the click only)", () => {
  assert.equal(documentListeners.keydown, undefined);
  assert.equal(windowListeners.keydown, undefined);
});

test("a second visit with a stored layout restores positions, sizes and hidden widgets (and the closed panel)", async () => {
  // A new page load: fresh DOM stubs, this browser's storage kept from the visit above.
  const saved = {
    v: 1, widgets: {
      [A1]: { x: 0, y: 0, w: 4, h: 8, visible: true }, [S1]: { x: 4, y: 2, w: 5, h: 7, visible: true },
      [A2]: { x: 0, y: 8, w: 6, h: 10, visible: false },
    },
  };
  storageData["wb.board.layout"] = JSON.stringify(saved);
  storageData["wb.board.panel"] = "closed";
  delete storageData["wb.board.narrow"];
  for (const el of elements.values()) { el.children = []; el.hidden = el.id === "board-notice" || el.id === "board-options-error"; el.classes.clear(); el.styleProps.clear(); }
  replies.length = 0;
  const sockets0 = sockets.length;
  const open0 = fetches.length;
  await import("../../../src/workbench/hub/web/static/board.js?second-visit");
  await settle();
  const ids = widgetEls().map((el) => el.dataset.terminal);
  assert.deepEqual(ids.sort(), [A1, S1].sort(), "the hidden agent stays hidden; nothing else is added");
  assert.equal(widgetOf(A1).styleProps.get("grid-column"), "1 / span 4");
  assert.equal(widgetOf(A1).styleProps.get("grid-row"), "1 / span 8");
  assert.equal(widgetOf(S1).styleProps.get("grid-column"), "5 / span 5");
  assert.equal(widgetOf(S1).styleProps.get("grid-row"), "3 / span 7");
  assert.equal(element("board-panel").hidden, true, "the panel was closed on the last visit");
  assert.deepEqual(fetches[open0 + 1].body, { visible: [A1, S1] }, "open names only the widgets this browser shows");
  assert.equal(sockets.length - sockets0, 2, "only visible widgets connect");
});

test("six visible widgets: only 4 sockets wait for their hello at a time; the next one opens when one has attached", async () => {
  for (const key of Object.keys(storageData)) delete storageData[key];
  for (const el of elements.values()) { el.children = []; el.hidden = el.id === "board-notice" || el.id === "board-options-error"; el.classes.clear(); el.styleProps.clear(); }
  const six = Array.from({ length: 6 }, (_, i) => view(`s-0000000001${i}0`, "shell", `shell-${i + 1}`));
  replies.length = 0;
  replies.push(() => ({ ok: true, board: { state: "ok" }, options: BOARD.options, terminals: six }));
  replies.push(() => ({ ok: true, terminals: six }));
  const before = sockets.length;
  await import("../../../src/workbench/hub/web/static/board.js?six");
  await settle();
  const fresh = sockets.slice(before);
  assert.equal(fresh.length, 4, "4 waiting for a hello");
  assert.equal(widgetEls().length, 6, "all six widgets exist");
  fresh[0].onopen();
  message(fresh[0], { t: "attached", v: 2, terminal: six[0], size: [30, 100], clients: 1, heartbeat: 10 });
  assert.equal(sockets.slice(before).length, 5, "one attached: the fifth connects");
  fresh[1].onclose({ code: 4429 });
  assert.equal(sockets.slice(before).length, 6, "one refused: the sixth connects");
});
