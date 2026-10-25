// Board page: xterm.js terminals for the Board agent and the shell over /ws/board (TI-18).
// This file is DOM and socket glue only. Every protocol rule lives in board-state.js (pure, node-tested);
// here events are fed to step() and its effects are carried out. No inline script, no external URL.
import { initialState, step, describe, channelLabel } from "./board-state.js";

const CHANNELS = ["agent", "shell"];
const NOTICES = {
  input_refused: "입력이 전달되지 않았습니다 (해당 channel이 실행 중이 아닙니다)",
  too_large: "한 번에 보낼 수 있는 크기를 넘었습니다",
  bad_resize: "화면 크기를 적용하지 못했습니다",
  bad_frame: "서버가 입력 frame을 받아들이지 않았습니다",
};

const byId = (id) => document.getElementById(id);
const csrfMeta = document.querySelector('meta[name="csrf-token"]');
const csrf = csrfMeta ? csrfMeta.content : "";
const TerminalClass = globalThis.Terminal;
const FitAddonClass = globalThis.FitAddon && globalThis.FitAddon.FitAddon;

if (typeof TerminalClass !== "function" || typeof FitAddonClass !== "function") {
  byId("board-connection").textContent = "연결 없음";
  byId("board-notice").textContent = "터미널 구성요소(xterm.js)를 불러오지 못했습니다";
  byId("board-notice").hidden = false;
} else {
  main();
}

function main() {
  const encoder = new TextEncoder();
  const terminals = {};
  const timers = {};
  let state = initialState({ csrf });
  let socket = null;

  function dispatch(event) {
    const result = step(state, event);
    state = result.state;
    for (const effect of result.effects) run(effect);
    render();
  }

  function socketUrl() {
    return `${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}/ws/board`;
  }

  function dropSocket() {
    if (socket === null) return;
    const old = socket;
    socket = null;
    old.onopen = old.onmessage = old.onclose = old.onerror = null;
    try { old.close(); } catch (_) { /* already closed */ }
  }

  function openSocket(conn) {
    dropSocket();
    let ws;
    try {
      ws = new WebSocket(socketUrl());
    } catch (_) {
      queueMicrotask(() => dispatch({ type: "closed", conn, code: 1006 }));
      return;
    }
    ws.binaryType = "arraybuffer";
    socket = ws;
    ws.onopen = () => dispatch({ type: "opened", conn });
    ws.onmessage = (event) => {
      if (typeof event.data === "string") {
        let message = null;
        try { message = JSON.parse(event.data); } catch (_) { return; }
        if (message !== null && typeof message === "object") dispatch({ type: "message", conn, message });
      } else {
        dispatch({ type: "data", conn, bytes: new Uint8Array(event.data) });
      }
    };
    ws.onclose = (event) => dispatch({ type: "closed", conn, code: event.code });
    ws.onerror = () => { /* a close event follows */ };
  }

  function sendOpen(data) {
    if (socket !== null && socket.readyState === WebSocket.OPEN) socket.send(data);
  }

  function run(effect) {
    switch (effect.type) {
      case "open": openSocket(effect.conn); break;
      case "close": dropSocket(); break;
      case "send": sendOpen(JSON.stringify(effect.message)); break;
      case "send_binary": sendOpen(effect.bytes); break;
      case "write": terminals[effect.channel].term.write(effect.payload); break;
      case "reset_terminals":
        for (const name of CHANNELS) terminals[name].term.reset();
        break;
      case "set_timer":
        clearTimeout(timers[effect.name]);
        timers[effect.name] = setTimeout(() => dispatch({ type: "timer", name: effect.name }), effect.ms);
        break;
      case "clear_timer": clearTimeout(timers[effect.name]); break;
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
    const view = describe(state);
    const connection = byId("board-connection");
    connection.textContent = view.connection;
    connection.dataset.phase = state.phase;
    byId("board-banner").hidden = !view.banner;
    byId("board-reconnect").hidden = view.action !== "reconnect";
    for (const name of CHANNELS) {
      const info = view.channels[name];
      if (info) byId(`state-${name}`).textContent = channelLabel(info);
      if (terminals[name]) terminals[name].term.options.disableStdin = !view.canInput;
    }
  }

  function selectTab(name) {
    for (const other of CHANNELS) {
      const active = other === name;
      byId(`tab-${other}`).classList.toggle("active", active);
      byId(`tab-${other}`).setAttribute("aria-selected", String(active));
      byId(`panel-${other}`).classList.toggle("active", active);
    }
    terminals[name].fit.fit();
    terminals[name].term.focus();
  }

  for (const name of CHANNELS) {
    const term = new TerminalClass({
      cursorBlink: true, scrollback: 5000, fontSize: 14, fontFamily: "ui-monospace, Menlo, Consolas, monospace",
    });
    const fit = new FitAddonClass();
    term.loadAddon(fit);
    term.open(byId(`term-${name}`));
    terminals[name] = { term, fit };
    term.onData((data) => dispatch({ type: "input", channel: name, bytes: encoder.encode(data) }));
    term.onBinary((data) => dispatch({ type: "input", channel: name, bytes: Uint8Array.from(data, (c) => c.charCodeAt(0) & 255) }));
    term.onResize(({ rows, cols }) => dispatch({ type: "resize", channel: name, rows, cols }));
    byId(`tab-${name}`).addEventListener("click", () => selectTab(name));
  }

  let fitPending = false;
  const refit = () => {
    if (fitPending) return;
    fitPending = true;
    requestAnimationFrame(() => {
      fitPending = false;
      for (const name of CHANNELS) {
        try { terminals[name].fit.fit(); } catch (_) { /* not laid out yet */ }
      }
    });
  };
  if (typeof ResizeObserver === "function") {
    const observer = new ResizeObserver(refit);
    for (const name of CHANNELS) observer.observe(byId(`term-${name}`));
  }
  window.addEventListener("resize", refit);

  byId("board-retake").addEventListener("click", () => {
    dispatch({ type: "retake" });
    const active = CHANNELS.find((name) => byId(`panel-${name}`).classList.contains("active"));
    terminals[active].term.focus();
  });
  byId("board-reconnect").addEventListener("click", () => dispatch({ type: "reconnect" }));
  window.addEventListener("pagehide", () => dispatch({ type: "pagehide" }));
  window.addEventListener("pageshow", (event) => dispatch({ type: "pageshow", persisted: event.persisted }));

  const sizes = {};
  for (const name of CHANNELS) {
    try { terminals[name].fit.fit(); } catch (_) { /* keep xterm's default size */ }
    sizes[name] = [terminals[name].term.rows, terminals[name].term.cols];
  }
  render();
  dispatch({ type: "start", sizes });
  terminals.agent.term.focus();
}
