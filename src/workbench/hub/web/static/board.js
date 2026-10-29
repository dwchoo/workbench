// Board workspace page (web-board-ux SPEC s4 "화면: workspace와 panel", WBX-D2): a management panel and a grid of
// widgets, one xterm.js terminal and one /ws/board/{id} (protocol v2) per visible widget.
// This file is DOM, fetch, socket and pointer glue only. The rules live in pure, node-tested modules: the protocol in
// board-state.js, the grid, storage and reconcile logic in board-layout.js, the panel rows, API calls, confirm rule
// and widget texts in board-panel.js. Names and states reach the DOM through textContent only (AC-38). Widget
// positions are written with CSSOM (style.setProperty): no inline <style>, no style attribute markup, no inline
// script, no library, no external URL.
import { initialState, step, describe, channelLabel, terminalKey, selectionText } from "./board-state.js";
import * as Layout from "./board-layout.js";
import * as Panel from "./board-panel.js";
import { installStyleNonce } from "./style-nonce.js";

const NOTICES = {
  input_refused: "입력이 전달되지 않았습니다 (실행 중이 아닙니다)",
  too_large: "한 번에 보낼 수 있는 크기를 넘었습니다",
  bad_resize: "화면 크기를 적용하지 못했습니다",
  too_many_clients: "연결 수 한도(8개)를 넘었습니다",
};
const FLOOR_ROWS = 4; // free rows kept under the lowest widget so there is room to drop into

const byId = (id) => document.getElementById(id);
const csrfMeta = document.querySelector('meta[name="csrf-token"]');
const csrf = csrfMeta ? csrfMeta.content : "";
// The CSP admits only <style> elements with this response's nonce; wire it before any term.open() (style-nonce.js).
const styleNonceMeta = document.querySelector('meta[name="style-nonce"]');
installStyleNonce(document, styleNonceMeta ? styleNonceMeta.content : "");
const TerminalClass = globalThis.Terminal;
const FitAddonClass = globalThis.FitAddon && globalThis.FitAddon.FitAddon;

if (typeof TerminalClass !== "function" || typeof FitAddonClass !== "function") {
  const notice = byId("board-notice");
  notice.textContent = "터미널 구성요소(xterm.js)를 불러오지 못했습니다";
  notice.hidden = false;
} else {
  main();
}

// A small element builder: text goes in as textContent, never as markup.
function h(tag, props = {}, ...children) {
  const element = document.createElement(tag);
  if (props.class) element.className = props.class;
  if (props.text !== undefined) element.textContent = props.text;
  for (const [name, value] of Object.entries(props.attrs || {})) element.setAttribute(name, value);
  for (const child of children) if (child) element.append(child);
  return element;
}

function main() {
  const storage = Layout.browserStorage(globalThis);
  const api = Panel.createApi({ fetch: (...args) => globalThis.fetch(...args), csrf });
  const encoder = new TextEncoder();
  const workspace = byId("board-workspace");
  const shell = byId("board-shell");
  const panelEl = byId("board-panel");

  let terminals = []; // the Hub's list (agents first), kept fresh by the poll, WebSocket state messages and actions
  let options = null;
  const savedLayout = Layout.loadLayout(storage); // this browser's layout from an earlier visit, or null
  let widgets = savedLayout || {}; // the layout: {id: {x, y, w, h, visible}}
  let useStored = savedLayout !== null; // false: this browser shows every terminal, placed automatically
  let narrow = Layout.isNarrow(globalThis);
  let chosen = Layout.loadNarrow(storage);
  let panelOpen = Layout.loadPanel(storage) === "open";
  const live = new Map(); // terminal id -> widget record
  const floor = h("div", { class: "workspace-floor" });
  workspace.append(floor);
  let pageGone = false; // between pagehide and pageshow: nothing is opened or refreshed
  let renaming = null; // {id, error, saving} while a panel row shows its rename input

  // ---- notice, confirm, errors ------------------------------------------------------------------------------
  let noticeTimer = null;
  function showNotice(text) {
    const element = byId("board-notice");
    element.textContent = text;
    element.hidden = false;
    clearTimeout(noticeTimer);
    noticeTimer = setTimeout(() => { element.hidden = true; }, 6000);
  }

  function askConfirm(text) {
    const dialog = byId("board-confirm");
    if (!dialog || typeof dialog.showModal !== "function") return Promise.resolve(Boolean(globalThis.confirm(text)));
    return new Promise((resolve) => {
      byId("board-confirm-text").textContent = text;
      dialog.returnValue = "";
      dialog.addEventListener("close", () => resolve(dialog.returnValue === "ok"), { once: true });
      dialog.showModal();
    });
  }
  byId("board-confirm-ok").addEventListener("click", () => byId("board-confirm").close("ok"));
  byId("board-confirm-cancel").addEventListener("click", () => byId("board-confirm").close("cancel"));

  function toLogin() { location.href = "/login"; }

  // ---- the terminal list ----------------------------------------------------------------------------------------
  const viewOf = (id) => terminals.find((terminal) => terminal.id === id) || null;

  function noteView(view) {
    if (!view || !view.id) return;
    const index = terminals.findIndex((terminal) => terminal.id === view.id);
    if (index < 0) return;
    terminals[index] = { ...terminals[index], ...view };
    renderAll();
  }

  // Take a list reply: reconcile the layout (WBX-D57), choose the narrow terminal, then draw.
  function adopt(json, created = []) {
    if (json.board && json.board.state === "registry_invalid") {
      showNotice(json.board.message || "Board 목록 파일을 읽을 수 없습니다");
      terminals = [];
    } else {
      terminals = json.terminals || [];
    }
    options = json.options || options;
    widgets = Layout.reconcile(useStored ? widgets : null, terminals, created);
    chosen = Layout.pickNarrow(terminals, chosen);
    sync();
  }

  function saveLayout() {
    useStored = true; // from the first change on this browser's layout is its own
    Layout.saveLayout(storage, widgets);
  }

  // ---- widgets ---------------------------------------------------------------------------------------------------
  function gridMetrics() {
    const box = workspace.getBoundingClientRect();
    const style = globalThis.getComputedStyle ? globalThis.getComputedStyle(workspace) : null;
    const gapX = style ? parseFloat(style.columnGap) || 0 : 0;
    const gapY = style ? parseFloat(style.rowGap) || 0 : 0;
    const rowPx = style ? parseFloat(style.gridAutoRows) || 0 : 0;
    return { pitchX: Layout.pitchOf(box.width, gapX, Layout.COLS), pitchY: rowPx + gapY };
  }

  function place(element, rect) {
    element.style.setProperty("grid-column", `${rect.x + 1} / span ${rect.w}`);
    element.style.setProperty("grid-row", `${rect.y + 1} / span ${rect.h}`);
  }

  function placeAll() {
    shell.classList.toggle("board-narrow", narrow);
    let bottom = 0;
    for (const [id, rec] of live) {
      const entry = widgets[id];
      if (narrow || !entry) {
        rec.el.style.removeProperty("grid-column");
        rec.el.style.removeProperty("grid-row");
      } else {
        place(rec.el, entry);
        bottom = Math.max(bottom, entry.y + entry.h);
      }
    }
    floor.style.setProperty("grid-row", String(bottom + FLOOR_ROWS));
  }

  function createWidget(view) {
    const dot = h("span", { class: "dot" });
    const name = h("span", { class: "widget-name" });
    const others = h("span", { class: "widget-others" });
    const status = h("span", { class: "widget-status" });
    const hide = h("button", { class: "btn btn-sm widget-hide", text: "숨기기", attrs: { type: "button" } });
    const close = h("button", { class: "btn btn-sm widget-close", text: "×", attrs: { type: "button" } });
    const bar = h("div", { class: "widget-bar" }, dot, name, others, status, hide, close);
    const host = h("div", { class: "widget-term" });
    const scroller = h("div", { class: "widget-scroll" }, host);
    const coverText = h("p", { class: "cover-text" });
    const coverAction = h("button", { class: "btn", attrs: { type: "button" } });
    const cover = h("div", { class: "widget-cover" }, coverText, coverAction);
    cover.hidden = true;
    const body = h("div", { class: "widget-body" }, scroller, cover);
    const handle = h("div", { class: "widget-handle", attrs: { "aria-hidden": "true" } });
    const el = h("div", { class: "board-widget", attrs: { "data-terminal": view.id, role: "group" } }, bar, body, handle);
    workspace.insertBefore(el, floor);

    // scrollOnUserInput: typing or a paste returns to the bottom; output never moves a scrolled-up view.
    const term = new TerminalClass({
      cursorBlink: true, scrollback: 10000, scrollOnUserInput: true, fontSize: 14,
      fontFamily: "ui-monospace, Menlo, Consolas, monospace",
    });
    const fit = new FitAddonClass();
    term.loadAddon(fit);
    term.open(host);
    const rec = { id: view.id, el, bar, body, host, term, fit, dot, name, others, status, hide, close, cover, coverText, coverAction,
      handle, socket: null, timers: {}, lastSize: null, state: initialState({ csrf, terminal: view.id }), coverKey: "" };
    live.set(view.id, rec);
    // Copy works without a connection (scrollback stays readable); typing needs one (disableStdin).
    term.attachCustomKeyEventHandler((event) => {
      const action = terminalKey(event, { mac, hasSelection: term.hasSelection() });
      if (action === "pass") return true;
      if (action === "copy" && event.type === "keydown") {
        event.preventDefault();
        copySelection(term);
      }
      return false; // "native": the browser's paste event reaches xterm's own paste handler
    });
    term.onData((data) => dispatch(rec, { type: "input", bytes: encoder.encode(data) }));
    term.onBinary((data) => dispatch(rec, { type: "input", bytes: Uint8Array.from(data, (c) => c.charCodeAt(0) & 255) }));
    el.addEventListener("pointerdown", () => focusWidget(rec));
    el.addEventListener("focusin", () => markFocus(rec));
    hide.addEventListener("click", () => setVisible(rec.id, false));
    close.addEventListener("click", () => runTerminal(Panel.closeAction(viewOf(rec.id) || view), rec.id));
    coverAction.addEventListener("click", () => onCover(rec));
    bar.addEventListener("pointerdown", (event) => beginGesture(rec, "move", event, bar));
    handle.addEventListener("pointerdown", (event) => beginGesture(rec, "resize", event, handle));
    for (const target of [bar, handle]) {
      target.addEventListener("pointermove", (event) => moveGesture(event));
      target.addEventListener("pointerup", (event) => endGesture(event, true));
      target.addEventListener("pointercancel", (event) => endGesture(event, false));
    }
    if (observer) observer.observe(body);
    return rec;
  }

  function destroyWidget(id) {
    const rec = live.get(id);
    if (!rec) return;
    rec.leaving = true; // pump() must not connect it again
    dispatch(rec, { type: "release" }); // a hidden widget gives its connection back at once
    dropSocket(rec);
    for (const timer of Object.values(rec.timers)) clearTimeout(timer);
    if (observer) observer.unobserve(rec.body);
    try { rec.term.dispose(); } catch (_) { /* already gone */ }
    rec.el.remove();
    live.delete(id);
    if (gesture && gesture.rec === rec) gesture = null;
  }

  // Which widgets exist follows what is wanted (visible, or the one chosen on a narrow screen) and what the Hub lists.
  function sync() {
    const wanted = new Set(Layout.wantedIds({ widgets, narrow, chosen }).filter((id) => viewOf(id)));
    for (const id of [...live.keys()]) if (!wanted.has(id)) destroyWidget(id);
    for (const id of wanted) if (!live.has(id)) createWidget(viewOf(id));
    placeAll();
    renderAll();
    pump();
  }

  // ---- sockets and the protocol state machine ---------------------------------------------------------------------
  function dispatch(rec, event) {
    const before = rec.state.view;
    const result = step(rec.state, event);
    rec.state = result.state;
    for (const effect of result.effects) run(rec, effect);
    if (rec.state.view && rec.state.view !== before) noteView(rec.state.view);
    renderWidget(rec);
    pump(); // an attach or a refusal frees one of the four places before the hello
  }

  const socketUrl = (id) => `${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}/ws/board/${encodeURIComponent(id)}`;

  function dropSocket(rec) {
    if (rec.socket === null) return;
    const old = rec.socket;
    rec.socket = null;
    old.onopen = old.onmessage = old.onclose = old.onerror = null;
    try { old.close(); } catch (_) { /* already closed */ }
  }

  function openSocket(rec, conn) {
    dropSocket(rec);
    let ws;
    try {
      ws = new WebSocket(socketUrl(rec.id));
    } catch (_) {
      queueMicrotask(() => dispatch(rec, { type: "closed", conn, code: 1006 }));
      return;
    }
    ws.binaryType = "arraybuffer";
    rec.socket = ws;
    ws.onopen = () => dispatch(rec, { type: "opened", conn });
    ws.onmessage = (event) => {
      if (typeof event.data === "string") {
        let message = null;
        try { message = JSON.parse(event.data); } catch (_) { return; }
        if (message !== null && typeof message === "object") dispatch(rec, { type: "message", conn, message });
      } else {
        dispatch(rec, { type: "data", conn, bytes: new Uint8Array(event.data) });
      }
    };
    ws.onclose = (event) => {
      dispatch(rec, { type: "closed", conn, code: event.code });
      pump(); // a slot before the hello may be free now
    };
    ws.onerror = () => { /* a close event follows */ };
  }

  function sendOpen(rec, data) {
    if (rec.socket !== null && rec.socket.readyState === WebSocket.OPEN) rec.socket.send(data);
  }

  function run(rec, effect) {
    switch (effect.type) {
      case "open": openSocket(rec, effect.conn); break;
      case "close": dropSocket(rec); break;
      case "send": sendOpen(rec, JSON.stringify(effect.message)); break;
      case "send_binary": sendOpen(rec, effect.bytes); break;
      case "write": rec.term.write(effect.payload); break;
      case "reset_terminal": rec.term.reset(); break;
      case "server_size": // the terminal's size (the last screen that typed or resized): draw at it, never answer it
        if (rec.term.rows !== effect.rows || rec.term.cols !== effect.cols) rec.term.resize(effect.cols, effect.rows);
        break;
      case "gone": dropSocket(rec); refreshSoon(); break;
      case "set_timer":
        clearTimeout(rec.timers[effect.name]);
        rec.timers[effect.name] = setTimeout(() => dispatch(rec, { type: "timer", name: effect.name }), effect.ms);
        break;
      case "clear_timer": clearTimeout(rec.timers[effect.name]); break;
      case "notice": {
        const view = viewOf(rec.id);
        const text = NOTICES[effect.reason] || `서버 오류: ${effect.reason}`;
        showNotice(view ? `${view.name}: ${text}` : text);
        break;
      }
      default: break;
    }
  }

  // At most 4 sockets wait for their hello; hidden widgets were destroyed (released) in sync().
  let pumping = false;
  function pump() {
    if (pumping || pageGone) return; // a page that is going away opens nothing
    pumping = true;
    try {
      const list = [...live.values()].map((rec) => ({ id: rec.id, wanted: !rec.leaving, phase: rec.state.phase }));
      for (const id of Panel.planConnections(list).connect) {
        const rec = live.get(id);
        const size = measure(rec) || [24, 80];
        rec.lastSize = size;
        if (rec.term.rows !== size[0] || rec.term.cols !== size[1]) rec.term.resize(size[1], size[0]);
        dispatch(rec, { type: "start", size });
      }
    } finally {
      pumping = false;
    }
  }

  // ---- size (WBX-D54): only a layout change of this widget sends a resize ---------------------------------------
  // This widget's own size in rows and cols, measured without touching the terminal (a terminal drawn at another
  // screen's size keeps it until the Hub says otherwise): the host is as big as the widget body whatever the terminal
  // is drawn at, and a bigger server size scrolls inside .widget-scroll while a smaller one leaves empty room.
  function measure(rec) {
    // Whole pixels, rounded down: a host 0.4 px bigger than the body would make .widget-scroll show scrollbars.
    const box = rec.body.getBoundingClientRect ? rec.body.getBoundingClientRect() : { width: rec.body.clientWidth, height: rec.body.clientHeight };
    const width = Math.floor(box.width);
    const height = Math.floor(box.height);
    if (!width || !height) return null;
    rec.host.style.setProperty("width", `${width}px`);
    rec.host.style.setProperty("height", `${height}px`);
    let proposed = null;
    try { proposed = rec.fit.proposeDimensions(); } catch (_) { proposed = null; }
    if (!proposed || !proposed.rows || !proposed.cols) return null;
    return [proposed.rows, proposed.cols];
  }

  function layoutChanged(rec) {
    if (!live.has(rec.id)) return;
    const size = measure(rec);
    if (size === null) return;
    if (rec.lastSize && rec.lastSize[0] === size[0] && rec.lastSize[1] === size[1]) return;
    rec.lastSize = size;
    dispatch(rec, { type: "resize", rows: size[0], cols: size[1] });
  }

  const pendingFit = new Set();
  function scheduleFit(rec) {
    if (pendingFit.has(rec)) return;
    pendingFit.add(rec);
    requestAnimationFrame(() => {
      pendingFit.delete(rec);
      layoutChanged(rec);
    });
  }
  const observer = typeof ResizeObserver === "function" ? new ResizeObserver((entries) => {
    for (const entry of entries) {
      for (const rec of live.values()) if (rec.body === entry.target) scheduleFit(rec);
    }
  }) : null;
  window.addEventListener("resize", () => { for (const rec of live.values()) scheduleFit(rec); });

  // ---- drawing -----------------------------------------------------------------------------------------------------
  const DOT_CLASS = { on: "dot-on", warn: "dot-warn", error: "dot-error", off: "" };

  function renderWidget(rec) {
    const view = viewOf(rec.id);
    const desc = describe(rec.state);
    rec.term.options.disableStdin = !desc.canInput;
    if (!view) return;
    rec.name.textContent = view.name;
    rec.dot.className = `dot ${DOT_CLASS[Panel.rowModel(view, { narrow, widgets, chosen }).dot]}`.trim();
    rec.others.textContent = Panel.othersText(desc.others);
    const connection = desc.connection && rec.state.phase !== "attached" ? ` · ${desc.connection}` : "";
    rec.status.textContent = `${channelLabel(view)}${connection}`;
    const cover = Panel.overlayFor(view, desc);
    const key = cover ? `${cover.action}|${cover.text}|${cover.label}` : "";
    if (key !== rec.coverKey) {
      rec.coverKey = key;
      rec.cover.hidden = cover === null;
      if (cover) {
        rec.coverText.textContent = cover.text;
        rec.coverAction.textContent = cover.label || "";
        rec.coverAction.hidden = cover.action === null;
        rec.cover.dataset.action = cover.action || "";
      }
    }
    rec.close.setAttribute("aria-label", view.kind === "agent" ? `${view.name} 중지` : `${view.name} 닫기`);
    rec.close.disabled = view.busy === true;
  }

  function onCover(rec) {
    const action = rec.cover.dataset.action;
    const view = viewOf(rec.id);
    if (action === "reconnect") dispatch(rec, { type: "reconnect" });
    else if (action === "login") toLogin();
    else if (view && (action === "start" || action === "restart")) runTerminal(action, rec.id);
  }

  function renderAll() {
    for (const rec of live.values()) renderWidget(rec);
    renderPanel();
  }

  // ---- panel -------------------------------------------------------------------------------------------------------
  function setPanel(open, persist = true) {
    panelOpen = open;
    panelEl.hidden = !open;
    byId("board-panel-toggle").setAttribute("aria-expanded", String(open));
    if (persist) Layout.savePanel(storage, open ? "open" : "closed");
  }

  function rowButton(label, onClick, { disabled = false, danger = false, aria = null } = {}) {
    const button = h("button", { class: danger ? "btn btn-sm btn-danger" : "btn btn-sm", text: label, attrs: { type: "button" } });
    button.disabled = disabled;
    if (aria) button.setAttribute("aria-label", aria);
    button.addEventListener("click", onClick);
    return button;
  }

  function renameRow(row, view) {
    const input = h("input", { class: "rename-input", attrs: { type: "text", "aria-label": `${view.name} 새 이름`, autocomplete: "off" } });
    input.value = view.name;
    const error = h("p", { class: "row-error", attrs: { role: "alert" } });
    error.hidden = renaming.error === null;
    error.textContent = renaming.error || "";
    input.addEventListener("keydown", (event) => {
      const key = Panel.renameKey(event);
      if (key === "cancel") { event.preventDefault(); stopRename(); }
      if (key === "save") { event.preventDefault(); saveRename(view, input.value, error); }
    });
    input.addEventListener("blur", () => { if (renaming && renaming.id === view.id && !renaming.saving) stopRename(); });
    row.append(input, error);
    queueMicrotask(() => { input.focus(); input.select(); });
  }

  function stopRename() {
    renaming = null;
    renderPanel();
  }

  async function saveRename(view, value, errorEl) {
    const problem = Panel.nameProblem(value, { kind: view.kind, terminals, selfId: view.id });
    if (problem) {
      errorEl.textContent = problem.text;
      errorEl.hidden = false;
      renaming.error = problem.text;
      return;
    }
    renaming.saving = true;
    const result = await api.rename(view.id, value.trim());
    if (result.unauthorized) { toLogin(); return; }
    if (!result.ok) {
      renaming.saving = false;
      renaming.error = Panel.errorText(result);
      errorEl.textContent = renaming.error;
      errorEl.hidden = false;
      return;
    }
    renaming = null;
    noteView(result.json.terminal);
    renderPanel();
  }

  function panelRow(view) {
    const model = Panel.rowModel(view, { narrow, widgets, chosen });
    const row = h("li", { class: "panel-row" });
    row.dataset.terminal = view.id;
    const top = h("div", { class: "panel-row-top" }, h("span", { class: `dot ${DOT_CLASS[model.dot]}`.trim() }));
    if (renaming && renaming.id === view.id) {
      renaming.drawn = true; // from now on renderPanel leaves this list alone until the edit ends
      row.append(top);
      renameRow(row, view);
      return row;
    }
    top.append(h("span", { class: "panel-name", text: view.name }), h("span", { class: "panel-state", text: model.stateText }));
    row.append(top);
    const actions = h("div", { class: "panel-actions" });
    actions.append(rowButton(model.toggle.label, () => onToggle(view, model.toggle.kind), { disabled: model.toggle.disabled, aria: `${view.name} ${model.toggle.label}` }));
    if (model.run) actions.append(rowButton(model.run.label, () => runTerminal(model.run.action, view.id), { disabled: model.run.disabled, aria: `${view.name} ${model.run.label}` }));
    actions.append(rowButton("이름 바꾸기", () => { renaming = { id: view.id, error: null, saving: false }; renderPanel(); }, { disabled: !model.canRename, aria: `${view.name} 이름 바꾸기` }));
    if (view.kind === "agent") actions.append(rowButton("삭제", () => runTerminal("delete", view.id), { disabled: !model.canDelete, danger: true, aria: `${view.name} 삭제` }));
    else actions.append(rowButton("닫기", () => runTerminal("close", view.id), { disabled: !model.canClose, danger: true, aria: `${view.name} 닫기` }));
    row.append(actions);
    return row;
  }

  function renderPanel() {
    const agents = terminals.filter((terminal) => terminal.kind === "agent");
    const shells = terminals.filter((terminal) => terminal.kind === "shell");
    // A list with a row being renamed is left alone (rebuilding it would wipe what is typed and end an IME
    // composition); what changed meanwhile is drawn when the edit ends (stopRename and the save call this again).
    for (const [listId, items] of [["board-agents", agents], ["board-shells", shells]]) {
      const list = byId(listId);
      const editing = renaming !== null && renaming.drawn === true && items.some((terminal) => terminal.id === renaming.id);
      if (!editing) list.replaceChildren(...items.map(panelRow));
    }
    const limits = Panel.limitModel(options, terminals);
    for (const [kind, button, note] of [["agent", "board-new-agent", "board-agent-note"], ["shell", "board-new-shell", "board-shell-note"]]) {
      byId(button).disabled = limits[kind].full;
      byId(note).textContent = limits[kind].note;
    }
    if (options) {
      const agentInput = byId("board-agent-limit");
      const shellInput = byId("board-shell-limit");
      if (document.activeElement !== agentInput) agentInput.value = String(options.agent_limit);
      if (document.activeElement !== shellInput) shellInput.value = String(options.shell_limit);
    }
  }

  function onToggle(view, kind) {
    if (kind === "choose") {
      chosen = view.id;
      Layout.saveNarrow(storage, chosen);
      setPanel(false, false); // a narrow screen shows the one terminal; the stored open/closed choice is for wide screens
      sync();
    } else {
      setVisible(view.id, !(widgets[view.id] && widgets[view.id].visible));
    }
  }

  function setVisible(id, visible) {
    widgets = visible ? Layout.showWidget(widgets, id) : Layout.hideWidget(widgets, id);
    saveLayout();
    sync();
  }

  // ---- actions on terminals -----------------------------------------------------------------------------------
  async function runTerminal(action, id) {
    const view = viewOf(id);
    if (!view) return;
    const result = await Panel.terminalAction(api, action, view, { confirm: askConfirm });
    if (result.cancelled) return;
    if (result.unauthorized) { toLogin(); return; }
    if (!result.ok) showNotice(`${view.name}: ${Panel.errorText(result)}`);
    else if (result.json && result.json.terminal) noteView(result.json.terminal);
    refresh();
  }

  async function create(kind) {
    const result = await (kind === "agent" ? api.createAgent() : api.createShell());
    if (result.unauthorized) { toLogin(); return; }
    if (!result.ok) { showNotice(Panel.errorText(result)); return; }
    const view = result.json.terminal;
    const at = kind === "agent" ? terminals.filter((terminal) => terminal.kind === "agent").length : terminals.length;
    terminals = [...terminals.slice(0, at), view, ...terminals.slice(at)];
    widgets = Layout.reconcile(widgets, terminals, [view.id]); // made here: placed and shown (WBX-D56 (5))
    if (narrow) { chosen = view.id; Layout.saveNarrow(storage, chosen); setPanel(false, false); }
    saveLayout();
    sync();
    refresh();
  }
  byId("board-new-agent").addEventListener("click", () => create("agent"));
  byId("board-new-shell").addEventListener("click", () => create("shell"));
  byId("board-reset-layout").addEventListener("click", () => {
    widgets = Layout.resetLayout(terminals);
    saveLayout();
    sync();
  });
  byId("board-panel-toggle").addEventListener("click", () => setPanel(!panelOpen));

  byId("board-options").addEventListener("submit", async (event) => {
    event.preventDefault();
    const error = byId("board-options-error");
    const request = Panel.optionsRequest({ agent: byId("board-agent-limit").value, shell: byId("board-shell-limit").value }, options, terminals);
    if (!request.ok) { error.textContent = request.text; error.hidden = false; return; }
    const result = await api.setOptions(request.body);
    if (result.unauthorized) { toLogin(); return; }
    if (!result.ok) { error.textContent = Panel.errorText(result); error.hidden = false; return; }
    error.hidden = true;
    options = { ...options, ...result.json.options };
    renderPanel();
  });

  // ---- focus (WBX-D53): the clicked widget has the highlighted border and the keyboard; no shortcuts ---------------
  function markFocus(rec) {
    for (const other of live.values()) other.el.classList.toggle("is-focused", other === rec);
  }
  function focusWidget(rec) {
    markFocus(rec);
    rec.term.focus();
  }

  // ---- drag and resize (WBX-D52, D57): pointer events, whole cells, no overlap ----------------------------------
  let gesture = null;
  const preview = h("div", { class: "drop-preview" });
  preview.hidden = true;
  workspace.insertBefore(preview, floor);

  function beginGesture(rec, mode, event, target) {
    if (narrow || gesture || (event.button !== undefined && event.button > 0)) return;
    if (mode === "move" && event.target && event.target.closest && event.target.closest("button")) return; // the bar's buttons click
    const entry = widgets[rec.id];
    if (!entry) return;
    event.preventDefault();
    const { pitchX, pitchY } = gridMetrics();
    if (!(pitchX > 0 && pitchY > 0)) return;
    gesture = { rec, mode, target, pointerId: event.pointerId, x: event.clientX, y: event.clientY, pitchX, pitchY,
      origin: { x: entry.x, y: entry.y, w: entry.w, h: entry.h }, proposal: null };
    try { target.setPointerCapture(event.pointerId); } catch (_) { /* the pointer is gone */ }
    rec.el.classList.add(mode === "move" ? "is-dragging" : "is-resizing");
    showPreview(gesture.origin, true);
  }

  function showPreview(rect, ok) {
    place(preview, rect);
    preview.classList.toggle("is-bad", !ok);
    preview.textContent = ok ? "" : "놓을 수 없음";
    preview.hidden = false;
  }

  function moveGesture(event) {
    if (!gesture || event.pointerId !== gesture.pointerId) return;
    const dxPx = event.clientX - gesture.x;
    const dyPx = event.clientY - gesture.y;
    const dx = Layout.snapCells(dxPx, gesture.pitchX);
    const dy = Layout.snapCells(dyPx, gesture.pitchY);
    gesture.proposal = gesture.mode === "move" ? Layout.moveRect(gesture.origin, dx, dy) : Layout.resizeRect(gesture.origin, dx, dy);
    const verdict = Layout.settle(gesture.rec.id, gesture.proposal, widgets);
    showPreview(gesture.proposal, verdict.ok);
    if (gesture.mode === "move") gesture.rec.el.style.setProperty("transform", `translate(${dxPx}px, ${dyPx}px)`);
  }

  function endGesture(event, commit) {
    if (!gesture || event.pointerId !== gesture.pointerId) return;
    const { rec, target, proposal } = gesture;
    gesture = null;
    try { target.releasePointerCapture(event.pointerId); } catch (_) { /* already released */ }
    rec.el.classList.remove("is-dragging", "is-resizing");
    rec.el.style.removeProperty("transform");
    preview.hidden = true;
    if (commit && proposal) {
      const verdict = Layout.settle(rec.id, proposal, widgets);
      if (verdict.ok) {
        widgets = Layout.setRect(widgets, rec.id, verdict.rect);
        saveLayout();
      }
      // a refused drop leaves the widget where it was: the last valid place (WBX-D57)
    }
    placeAll();
  }

  // ---- copy and paste (the page's own copy command; works on plain HTTP) ------------------------------------------
  let copying = null;
  document.addEventListener("copy", (event) => {
    if (copying === null || !event.clipboardData) return;
    event.clipboardData.setData("text/plain", copying);
    event.preventDefault();
  });
  function copySelection(term) {
    const text = selectionText(term.getSelection());
    if (text === "") return;
    copying = text;
    let done = false;
    try { done = document.execCommand("copy"); } catch (_) { done = false; } finally { copying = null; }
    const clipboardApi = globalThis.navigator && globalThis.navigator.clipboard;
    if (!done && globalThis.isSecureContext && clipboardApi && typeof clipboardApi.writeText === "function") {
      clipboardApi.writeText(text).catch(() => { /* nothing more to try */ });
    }
    term.clearSelection(); // the next Ctrl+C is ^C again
  }
  const nav = globalThis.navigator || {};
  const mac = /Mac|iPhone|iPad/.test(nav.platform || nav.userAgent || "");

  // ---- the list: first open, then every 5 s and after each action; pagehide stops it (WBX-D34) -----------------
  let pollTimer = null;
  let pollAbort = null;

  function schedulePoll() {
    clearTimeout(pollTimer);
    if (!pageGone) pollTimer = setTimeout(refresh, Panel.POLL_MS);
  }
  function refreshSoon() {
    clearTimeout(pollTimer);
    if (!pageGone) pollTimer = setTimeout(refresh, 300);
  }
  async function refresh() {
    clearTimeout(pollTimer);
    if (pollAbort) pollAbort.abort();
    pollAbort = typeof AbortController === "function" ? new AbortController() : null;
    const result = await api.list({ signal: pollAbort ? pollAbort.signal : undefined });
    if (result.aborted) return;
    if (result.unauthorized) { toLogin(); return; }
    if (result.ok) adopt(result.json);
    else if (result.status !== 0) showNotice(Panel.errorText(result));
    schedulePoll();
  }

  window.addEventListener("pagehide", () => {
    pageGone = true;
    clearTimeout(pollTimer);
    if (pollAbort) pollAbort.abort();
    for (const rec of live.values()) dispatch(rec, { type: "pagehide" });
  });
  window.addEventListener("pageshow", (event) => {
    if (!pageGone) return;
    pageGone = false;
    if (event.persisted) {
      refresh();
      pump(); // the released widgets connect again, within the hello budget
    }
  });

  // ---- narrow screens (WBX-D51) ---------------------------------------------------------------------------------
  function onMediaChange() {
    const now = Layout.isNarrow(globalThis);
    if (now === narrow) return;
    narrow = now;
    chosen = Layout.pickNarrow(terminals, chosen);
    if (narrow) setPanel(false, false);
    else setPanel(Layout.loadPanel(storage) === "open", false);
    sync();
  }
  if (typeof globalThis.matchMedia === "function") {
    const query = globalThis.matchMedia(Layout.NARROW_QUERY);
    if (query && typeof query.addEventListener === "function") query.addEventListener("change", onMediaChange);
  }
  window.addEventListener("resize", onMediaChange);

  // ---- first open: list -> layout -> open (starts a not started visible agent, makes this run's first shell) -------
  async function boot() {
    setPanel(narrow ? false : panelOpen, false);
    const first = await api.list();
    if (first.unauthorized) { toLogin(); return; }
    if (!first.ok) {
      showNotice(Panel.errorText(first));
      schedulePoll();
      return;
    }
    if (first.json.board && first.json.board.state === "registry_invalid") {
      adopt(first.json);
      schedulePoll();
      return;
    }
    // What would be visible now: asked of the Hub first (it starts a not started agent among them and makes this
    // run's first shell), and only then drawn and connected, so no widget shows "시작 전" for a moment.
    const preview = Layout.reconcile(useStored ? widgets : null, first.json.terminals || []);
    const wanted = narrow ? [Layout.pickNarrow(first.json.terminals || [], chosen)].filter(Boolean) : Layout.visibleIds(preview);
    const opened = await api.open(wanted);
    if (opened.unauthorized) { toLogin(); return; }
    if (opened.ok) {
      const known = new Set((first.json.terminals || []).map((terminal) => terminal.id));
      const made = (opened.json.terminals || []).filter((terminal) => !known.has(terminal.id)).map((terminal) => terminal.id);
      adopt({ ...first.json, terminals: opened.json.terminals || first.json.terminals }, made);
    } else {
      showNotice(Panel.errorText(opened));
      adopt(first.json);
    }
    schedulePoll();
  }
  boot();
}
