// Board page: xterm.js terminals for the first Board agent and the first shell, one /ws/board/{id} each (protocol
// v2, web-board-ux SPEC s4; WBX-D2 replaces this two-tab page with the workspace).
// This file is DOM, fetch and socket glue only. Every protocol rule lives in board-state.js (pure, node-tested);
// here events are fed to step() and its effects are carried out. No inline script, no external URL.
import { initialState, step, describe, channelLabel, terminalKey, selectionText } from "./board-state.js";
import { installStyleNonce } from "./style-nonce.js";

const TABS = ["agent", "shell"];
const NOTICES = {
  input_refused: "입력이 전달되지 않았습니다 (실행 중이 아닙니다)",
  too_large: "한 번에 보낼 수 있는 크기를 넘었습니다",
  bad_resize: "화면 크기를 적용하지 못했습니다",
  too_many_clients: "연결 수 한도(8개)를 넘었습니다",
  open_failed: "Board 목록을 받지 못했습니다",
  restart_failed: "재시작하지 못했습니다",
};
const RESTART_QUESTION = {
  agent: "Board agent를 끝내고 이전 세션을 이어서 다시 띄웁니다. 진행 중인 응답은 끊깁니다. 재시작할까요?",
  shell: "Shell을 끝내고 새로 띄웁니다. 실행 중인 명령은 종료됩니다. 재시작할까요?",
};

const byId = (id) => document.getElementById(id);
const csrfMeta = document.querySelector('meta[name="csrf-token"]');
const csrf = csrfMeta ? csrfMeta.content : "";
// The CSP admits only <style> elements with this response's nonce; wire it before any term.open() (style-nonce.js).
const styleNonceMeta = document.querySelector('meta[name="style-nonce"]');
installStyleNonce(document, styleNonceMeta ? styleNonceMeta.content : "");
const TerminalClass = globalThis.Terminal;
const FitAddonClass = globalThis.FitAddon && globalThis.FitAddon.FitAddon;

if (typeof TerminalClass !== "function" || typeof FitAddonClass !== "function") {
  byId("board-connection").textContent = "연결 없음";
  byId("board-notice").textContent = "터미널 구성요소(xterm.js)를 불러오지 못했습니다";
  byId("board-notice").hidden = false;
} else {
  main();
}

function api(path, body) {
  return fetch(path, {
    method: "POST", credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
    body: JSON.stringify(body || {}),
  }).then((response) => response.json().catch(() => ({ ok: false })).then((json) => ({ status: response.status, json })));
}

function main() {
  const encoder = new TextEncoder();
  const tabs = {};
  let active = "agent";

  function dispatch(name, event) {
    const tab = tabs[name];
    const result = step(tab.state, event);
    tab.state = result.state;
    for (const effect of result.effects) run(tab, effect);
    render();
  }

  function socketUrl(id) {
    return `${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}/ws/board/${encodeURIComponent(id)}`;
  }

  function dropSocket(tab) {
    if (tab.socket === null) return;
    const old = tab.socket;
    tab.socket = null;
    old.onopen = old.onmessage = old.onclose = old.onerror = null;
    try { old.close(); } catch (_) { /* already closed */ }
  }

  function openSocket(tab, conn) {
    dropSocket(tab);
    let ws;
    try {
      ws = new WebSocket(socketUrl(tab.id));
    } catch (_) {
      queueMicrotask(() => dispatch(tab.name, { type: "closed", conn, code: 1006 }));
      return;
    }
    ws.binaryType = "arraybuffer";
    tab.socket = ws;
    ws.onopen = () => dispatch(tab.name, { type: "opened", conn });
    ws.onmessage = (event) => {
      if (typeof event.data === "string") {
        let message = null;
        try { message = JSON.parse(event.data); } catch (_) { return; }
        if (message !== null && typeof message === "object") dispatch(tab.name, { type: "message", conn, message });
      } else {
        dispatch(tab.name, { type: "data", conn, bytes: new Uint8Array(event.data) });
      }
    };
    ws.onclose = (event) => dispatch(tab.name, { type: "closed", conn, code: event.code });
    ws.onerror = () => { /* a close event follows */ };
  }

  function sendOpen(tab, data) {
    if (tab.socket !== null && tab.socket.readyState === WebSocket.OPEN) tab.socket.send(data);
  }

  function run(tab, effect) {
    switch (effect.type) {
      case "open": openSocket(tab, effect.conn); break;
      case "close": dropSocket(tab); break;
      case "send": sendOpen(tab, JSON.stringify(effect.message)); break;
      case "send_binary": sendOpen(tab, effect.bytes); break;
      case "write": tab.term.write(effect.payload); break;
      case "reset_terminal": tab.term.reset(); break;
      case "server_size": // the terminal's size (the last screen that typed or resized): draw at it
        if (tab.term.rows !== effect.rows || tab.term.cols !== effect.cols) tab.term.resize(effect.cols, effect.rows);
        break;
      case "gone": dropSocket(tab); break;
      case "set_timer":
        clearTimeout(tab.timers[effect.name]);
        tab.timers[effect.name] = setTimeout(() => dispatch(tab.name, { type: "timer", name: effect.name }), effect.ms);
        break;
      case "clear_timer": clearTimeout(tab.timers[effect.name]); break;
      case "notice": showNotice(NOTICES[effect.reason] || `서버 오류: ${effect.reason}`); break;
      default: break;
    }
  }

  let noticeTimer = null;
  function showNotice(text) {
    const element = byId("board-notice");
    element.textContent = text;
    element.hidden = false;
    clearTimeout(noticeTimer);
    noticeTimer = setTimeout(() => { element.hidden = true; }, 6000);
  }

  function render() {
    const view = describe(tabs[active].state);
    const connection = byId("board-connection");
    connection.textContent = view.connection || "연결 중";
    connection.dataset.phase = tabs[active].state.phase;
    byId("board-reconnect").hidden = view.action !== "reconnect";
    byId("board-login").hidden = view.action !== "login";
    for (const name of TABS) {
      const tab = tabs[name];
      const info = describe(tab.state);
      const terminal = info.view || tab.view;
      if (terminal) {
        byId(`state-${name}`).textContent = channelLabel(terminal);
        if (terminal.name) byId(`name-${name}`).textContent = terminal.name; // textContent only (AC-38)
      }
      tab.term.options.disableStdin = !info.canInput;
    }
  }

  function ownSize(tab) {
    try { tab.fit.fit(); } catch (_) { /* not laid out yet */ }
    return [tab.term.rows, tab.term.cols];
  }

  function layoutChanged(name) { // this widget's own layout: the only source of a resize (never a server size)
    const tab = tabs[name];
    const [rows, cols] = ownSize(tab);
    if (tab.lastSize && tab.lastSize[0] === rows && tab.lastSize[1] === cols) return;
    tab.lastSize = [rows, cols];
    dispatch(name, { type: "resize", rows, cols });
  }

  function selectTab(name) {
    active = name;
    for (const other of TABS) {
      const on = other === name;
      byId(`tab-${other}`).classList.toggle("active", on);
      byId(`tab-${other}`).setAttribute("aria-selected", String(on));
      byId(`panel-${other}`).classList.toggle("active", on);
    }
    layoutChanged(name);
    tabs[name].term.focus();
    render();
  }

  // Copy: the page's own copy command fills the browser's copy event (works on plain HTTP, where
  // navigator.clipboard is missing); the clipboard API is only a fallback in a secure context.
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

  for (const name of TABS) {
    // scrollOnUserInput: typing or a paste returns to the bottom; output never moves a scrolled-up view.
    const term = new TerminalClass({
      cursorBlink: true, scrollback: 10000, scrollOnUserInput: true, fontSize: 14,
      fontFamily: "ui-monospace, Menlo, Consolas, monospace",
    });
    const fit = new FitAddonClass();
    term.loadAddon(fit);
    term.open(byId(`term-${name}`));
    const panel = byId(`panel-${name}`);
    const id = (panel.dataset && panel.dataset.terminal) || null;
    tabs[name] = { name, id, term, fit, socket: null, timers: {}, view: null, lastSize: null,
      state: initialState({ csrf, terminal: id }) };
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
    term.onData((data) => dispatch(name, { type: "input", bytes: encoder.encode(data) }));
    term.onBinary((data) => dispatch(name, { type: "input", bytes: Uint8Array.from(data, (c) => c.charCodeAt(0) & 255) }));
    byId(`tab-${name}`).addEventListener("click", () => selectTab(name));
    byId(`restart-${name}`).addEventListener("click", () => restart(name));
  }

  function restart(name) {
    const tab = tabs[name];
    if (!tab.id || !globalThis.confirm(RESTART_QUESTION[name])) return;
    api(`/api/board/terminals/${encodeURIComponent(tab.id)}/restart`, { confirm: true }).then(({ json }) => {
      if (json && json.ok && json.terminal) {
        tab.view = json.terminal;
        render();
      } else {
        showNotice((json && json.detail) || NOTICES.restart_failed);
      }
    }, () => showNotice(NOTICES.restart_failed));
  }

  let fitPending = false;
  const refit = () => {
    if (fitPending) return;
    fitPending = true;
    requestAnimationFrame(() => {
      fitPending = false;
      layoutChanged(active);
    });
  };
  if (typeof ResizeObserver === "function") {
    const observer = new ResizeObserver(refit);
    for (const name of TABS) observer.observe(byId(`term-${name}`));
  }
  window.addEventListener("resize", refit);

  byId("board-reconnect").addEventListener("click", () => dispatch(active, { type: "reconnect" }));
  window.addEventListener("pagehide", () => { for (const name of TABS) dispatch(name, { type: "pagehide" }); });
  window.addEventListener("pageshow", (event) => {
    for (const name of TABS) dispatch(name, { type: "pageshow", persisted: event.persisted });
  });

  function begin(terminals) {
    const first = { agent: null, shell: null };
    for (const view of terminals || []) {
      if (view && first[view.kind] === null) first[view.kind] = view;
    }
    for (const name of TABS) {
      const tab = tabs[name];
      const view = first[name];
      if (!view) continue;
      tab.id = view.id;
      tab.view = view;
      tab.state = { ...tab.state, terminal: view.id };
      tab.lastSize = ownSize(tab);
      dispatch(name, { type: "start", size: tab.lastSize });
    }
    render();
  }

  render();
  // The first open of this page: the Hub makes this run's first shell and starts a not yet started agent-1.
  const visible = tabs.agent.id ? [tabs.agent.id] : [];
  api("/api/board/open", { visible }).then(({ status, json }) => {
    if (status === 401) {
      location.href = "/login";
      return;
    }
    if (!json || !json.ok) {
      showNotice((json && json.detail) || NOTICES.open_failed);
      return;
    }
    begin(json.terminals);
  }, () => showNotice(NOTICES.open_failed));
  tabs.agent.term.focus();
}
