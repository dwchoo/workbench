// One Board terminal's WebSocket, protocol v2 (web-board-ux SPEC s4 "WebSocket v2: terminal마다 공유 연결").
// Pure: no DOM, no WebSocket, no timers. board.js keeps one state per terminal, feeds events to step() and carries out
// the effects it returns.
//
// Phases: idle -> connecting -> attached; attached|connecting -> disconnected (network, heartbeat, queue, Hub stop:
//         reconnects with a new hello after a backoff) | refused (4400-4499: no automatic reconnect; 4401 log in
//         again, 4429 too many clients) | gone (4404: the terminal was closed or deleted) | released (pagehide or a
//         hidden widget; pageshow of a restored page connects again).
// Events: start {size}, opened, message, data, closed (these carry the socket number `conn`; events of an older socket
//         are ignored), timer {name: ping|pong|reconnect|stable|resize}, reconnect, input {bytes}, resize {rows, cols}
//         (a layout change of this widget only, never a server `size`), pagehide, pageshow {persisted}, release.
// Effects: open {conn}, close {conn, code}, send {message}, send_binary {bytes}, write {payload}, reset_terminal,
//          server_size {rows, cols} (draw at the terminal's size: letterbox or scroll when it is not ours), gone,
//          set_timer {name, ms}, clear_timer {name}, notice {reason}.
export const PROTOCOL_VERSION = 2;
export const PONG_LIMIT_MS = 20000;
export const DEFAULT_HEARTBEAT_MS = 10000;
export const BACKOFF_FIRST_MS = 1000;
export const BACKOFF_MAX_MS = 15000;
export const RESIZE_DEBOUNCE_MS = 150;
export const MIN_SIZE = 1;
export const MAX_SIZE = 500;
export const CLIENT_LIMIT = 8;
export const INPUT_FRAME_MAX = 64 * 1024; // SPEC 입력 한도: the server drops a bigger input frame (too_large)
export const INPUT_MAX = 4 << 20; // one paste at most (the Hub paces it into the PTY; a bigger one is refused whole)
const CLIENT_TIMEOUT_CODE = 4000; // our own close when no pong came (not a server code)
const isClientRefusal = (code) => code >= 4400 && code <= 4499;
export const SESSION_ENDED_CODE = 4401; // the login session ended: only a new login helps
export const GONE_CODE = 4404; // the terminal was closed or deleted: the widget goes
export const TOO_MANY_CODE = 4429; // the terminal has its 8 clients
export const SESSION_ENDED_TEXT = "연결 없음 — 다시 로그인";
export const TOO_MANY_TEXT = `연결 수 한도(${CLIENT_LIMIT}개)를 넘었습니다`;
export const GONE_TEXT = "terminal이 없어졌습니다";
export const STABLE_MS = 10000; // an attach must hold this long before the reconnect delay starts over
const NONE = (state) => ({ state, effects: [] });
const CLEAR_HEARTBEAT = [
  { type: "clear_timer", name: "ping" }, { type: "clear_timer", name: "pong" }, { type: "clear_timer", name: "stable" },
];

export function initialState({ csrf, terminal = null }) {
  return {
    phase: "idle", closeCode: null, csrf, terminal, conn: 0, size: null, pendingSize: null, attempt: 0, pings: 0,
    heartbeatMs: DEFAULT_HEARTBEAT_MS, view: null, clients: 0, serverSize: null,
  };
}

const CONNECTION_LABELS = {
  idle: "", connecting: "연결 중", attached: "연결됨", disconnected: "연결 없음", released: "연결 없음",
  gone: GONE_TEXT,
};

// What the widget shows for a state: the connection label, the one action offered ("reconnect" = 다시 연결,
// "login" = 다시 로그인), whether input is sent at all and how many other screens share the terminal.
export function describe(state) {
  let connection = CONNECTION_LABELS[state.phase] || "";
  let action = state.phase === "disconnected" ? "reconnect" : null;
  if (state.phase === "refused") {
    if (state.closeCode === SESSION_ENDED_CODE) {
      connection = SESSION_ENDED_TEXT;
      action = "login";
    } else if (state.closeCode === TOO_MANY_CODE) {
      connection = TOO_MANY_TEXT;
      action = "reconnect";
    } else {
      connection = "연결 없음";
      action = "reconnect";
    }
  }
  return {
    connection, action, canInput: state.phase === "attached", gone: state.phase === "gone", view: state.view,
    others: state.phase === "attached" ? Math.max(0, state.clients - 1) : 0,
  };
}

const TERMINAL_STATE_LABELS = {
  running: "실행 중", starting: "시작 중", not_started: "시작 전", stopped: "중지됨",
};

export function channelLabel(info) {
  if (!info || typeof info.state !== "string") return "알 수 없음";
  const detail = info.detail ? `: ${info.detail}` : "";
  switch (info.state) {
    case "exited":
      return info.exit_status === null || info.exit_status === undefined
        ? "종료됨" : `종료됨 (종료 코드 ${info.exit_status})`;
    case "failed": return `실패${detail}`;
    case "isolation_failed": return `격리 실패${detail}`;
    default: return TERMINAL_STATE_LABELS[info.state] || info.state;
  }
}

function clamp(value) {
  return Math.min(MAX_SIZE, Math.max(MIN_SIZE, Math.trunc(Number(value)) || MIN_SIZE));
}

export function step(state, event) {
  switch (event.type) {
    case "start":
      return connect(event.size ? { ...state, size: [clamp(event.size[0]), clamp(event.size[1])] } : state);
    case "reconnect":
      return (state.phase === "disconnected" || (state.phase === "refused" && state.closeCode !== SESSION_ENDED_CODE))
        ? withEffects(connect(state), [{ type: "clear_timer", name: "reconnect" }]) : NONE(state);
    case "input": return onInput(state, event);
    case "resize": return onResize(state, event);
    case "timer": return onTimer(state, event.name);
    case "pagehide":
    case "release": return onRelease(state);
    case "pageshow":
      return event.persisted && state.phase === "released" ? connect(state) : NONE(state);
    default:
      if (event.conn !== state.conn) return NONE(state);
      return onSocketEvent(state, event);
  }
}

function onSocketEvent(state, event) {
  switch (event.type) {
    case "opened": {
      const message = { t: "hello", v: PROTOCOL_VERSION, csrf: state.csrf };
      if (state.size) message.size = state.size;
      return { state, effects: [{ type: "send", message }] };
    }
    case "message": return onMessage(state, event.message);
    case "data":
      return state.phase === "attached" ? { state, effects: [{ type: "write", payload: event.bytes }] } : NONE(state);
    case "closed": return onClosed(state, event.code);
    default: return NONE(state);
  }
}

function withEffects(result, before) {
  return { state: result.state, effects: [...before, ...result.effects] };
}

function connect(state) {
  const conn = state.conn + 1;
  return { state: { ...state, phase: "connecting", conn, closeCode: null }, effects: [{ type: "open", conn }] };
}

function onClosed(state, code) {
  if (state.phase !== "connecting" && state.phase !== "attached") return NONE(state);
  if (code === GONE_CODE) {
    return { state: { ...state, phase: "gone", closeCode: code }, effects: [...CLEAR_HEARTBEAT, { type: "gone" }] };
  }
  if (isClientRefusal(code)) {
    return { state: { ...state, phase: "refused", closeCode: code }, effects: [...CLEAR_HEARTBEAT] };
  }
  return disconnected({ ...state, phase: "disconnected", closeCode: code }, [...CLEAR_HEARTBEAT]);
}

// The network or the server ended the connection: a new hello after 1, 2, 4 ... <= 15 s.
function disconnected(state, effects) {
  const delay = Math.min(BACKOFF_FIRST_MS * 2 ** state.attempt, BACKOFF_MAX_MS);
  return {
    state: { ...state, attempt: state.attempt + 1 },
    effects: [...effects, { type: "set_timer", name: "reconnect", ms: delay }],
  };
}

function onTimer(state, name) {
  if (name === "reconnect" && state.phase === "disconnected") return connect(state);
  if (name === "resize") return sendResize(state);
  if (state.phase !== "attached") return NONE(state);
  if (name === "stable") return { state: { ...state, attempt: 0 }, effects: [] };
  if (name === "ping") {
    const n = state.pings + 1;
    return {
      state: { ...state, pings: n },
      effects: [
        { type: "send", message: { t: "ping", n } },
        { type: "set_timer", name: "ping", ms: state.heartbeatMs },
      ],
    };
  }
  if (name === "pong") { // no pong for 20 s: the connection is dead even if the browser still thinks it is open
    const next = { ...state, phase: "disconnected", closeCode: CLIENT_TIMEOUT_CODE };
    return disconnected(next, [...CLEAR_HEARTBEAT, { type: "close", conn: state.conn, code: CLIENT_TIMEOUT_CODE }]);
  }
  return NONE(state);
}

// UTF-8 continuation bytes are 10xxxxxx: a cut before one would split a code point.
const isContinuation = (byte) => (byte & 0xc0) === 0x80;

// ``bytes`` in order as pieces of at most ``limit`` bytes, each cut on a code point boundary (a long paste stays
// valid UTF-8 in every frame). Bytes that are not UTF-8 (onBinary) are cut at the limit after at most 3 steps back.
export function splitInput(bytes, limit = INPUT_FRAME_MAX) {
  const pieces = [];
  let start = 0;
  while (bytes.length - start > limit) {
    let end = start + limit;
    for (let back = 0; back < 3 && isContinuation(bytes[end]); back += 1) end -= 1;
    if (isContinuation(bytes[end])) end = start + limit;
    pieces.push(bytes.subarray(start, end));
    start = end;
  }
  pieces.push(bytes.subarray(start));
  return pieces;
}

// Any connection may type (shared terminal, WBX-D54); input is sent only while attached.
function onInput(state, event) {
  if (state.phase !== "attached" || event.bytes.length === 0) return NONE(state);
  if (event.bytes.length > INPUT_MAX) return { state, effects: [{ type: "notice", reason: "too_large" }] };
  return { state, effects: splitInput(event.bytes).map((piece) => ({ type: "send_binary", bytes: piece })) };
}

// A layout change of this widget: remembered for the next hello and, after a 150 ms quiet, sent while attached.
// A server `size` or `attached` never leads here (no resize ping-pong between two screens).
function onResize(state, event) {
  const size = [clamp(event.rows), clamp(event.cols)];
  return {
    state: { ...state, size, pendingSize: size },
    effects: [{ type: "set_timer", name: "resize", ms: RESIZE_DEBOUNCE_MS }],
  };
}

function sendResize(state) {
  if (state.pendingSize === null) return NONE(state);
  const [rows, cols] = state.pendingSize;
  const next = { ...state, pendingSize: null };
  if (state.phase !== "attached") return NONE(next);
  return { state: next, effects: [{ type: "send", message: { t: "resize", rows, cols } }] };
}

// Tab close, navigation or a hidden widget: close this connection at once (best effort; the server's heartbeat is
// the fallback).
function onRelease(state) {
  if (state.phase === "connecting") { // nothing attached yet: do not leave a socket that could attach unseen
    return { state: { ...state, phase: "released" }, effects: [{ type: "close", conn: state.conn, code: 1000 }] };
  }
  if (state.phase === "disconnected") {
    return { state: { ...state, phase: "released" }, effects: [{ type: "clear_timer", name: "reconnect" }] };
  }
  if (state.phase !== "attached") return NONE(state);
  return {
    state: { ...state, phase: "released" },
    effects: [...CLEAR_HEARTBEAT, { type: "send", message: { t: "release" } }],
  };
}

function sizeOf(value) {
  return Array.isArray(value) && value.length === 2 ? [clamp(value[0]), clamp(value[1])] : null;
}

function onMessage(state, message) {
  switch (message.t) {
    case "attached": return onAttached(state, message);
    case "pong":
      return state.phase === "attached"
        ? { state, effects: [{ type: "set_timer", name: "pong", ms: PONG_LIMIT_MS }] } : NONE(state);
    case "clients":
      return { state: { ...state, clients: Number(message.count) || 0 }, effects: [] };
    case "size": {
      const size = sizeOf([message.rows, message.cols]);
      if (size === null) return NONE(state);
      return { state: { ...state, serverSize: size }, effects: [{ type: "server_size", rows: size[0], cols: size[1] }] };
    }
    case "state":
      return { state: { ...state, view: message.terminal || state.view }, effects: [] };
    case "gone":
      return { state: { ...state, phase: "gone" }, effects: [...CLEAR_HEARTBEAT, { type: "gone" }] };
    case "refused":
      return { state, effects: [{ type: "notice", reason: message.reason }] };
    case "error":
      return { state, effects: [{ type: "notice", reason: message.reason }] };
    default:
      return NONE(state);
  }
}

function onAttached(state, message) {
  const heartbeatMs = (Number(message.heartbeat) || 0) * 1000 || DEFAULT_HEARTBEAT_MS;
  const size = sizeOf(message.size);
  const effects = [{ type: "reset_terminal" }]; // the replay that follows is the whole ring: drop what is on screen
  if (size !== null) effects.push({ type: "server_size", rows: size[0], cols: size[1] });
  effects.push(
    { type: "set_timer", name: "ping", ms: heartbeatMs },
    { type: "set_timer", name: "pong", ms: PONG_LIMIT_MS },
    { type: "set_timer", name: "stable", ms: STABLE_MS },
  );
  return {
    state: {
      ...state, phase: "attached", view: message.terminal || null, clients: Number(message.clients) || 1, pings: 0,
      heartbeatMs, serverSize: size,
    },
    effects,
  };
}

// Clipboard and IME keys for xterm's custom key handler: "copy" (the page copies the selection itself),
// "native" (xterm leaves the key alone, so the browser's own paste event reaches xterm's paste handler: no
// navigator.clipboard, which plain HTTP does not have) or "pass" (xterm handles it: the PTY, scrolling, IME).
// Ctrl+C copies only while something is selected; otherwise it is ^C for the program. A Mac uses Cmd.
// The letter is the physical key when the layout gives another character (Hangul mode: Ctrl+ㅊ is Ctrl+C).
function letterOf(event) {
  const key = typeof event.key === "string" ? event.key : "";
  if (/^[A-Za-z]$/.test(key)) return key.toLowerCase();
  const match = /^Key([A-Z])$/.exec(event.code || "");
  return match ? match[1].toLowerCase() : key;
}

export function terminalKey(event, { mac, hasSelection }) {
  if (event.isComposing || event.keyCode === 229) return "pass"; // an IME composition belongs to xterm
  const { ctrlKey: ctrl, shiftKey: shift, altKey: alt, metaKey: meta } = event;
  const letter = letterOf(event);
  if (mac) {
    if (meta && !ctrl && !alt && letter === "c") return hasSelection ? "copy" : "pass";
    if (meta && !ctrl && !alt && letter === "v") return "native";
    return "pass";
  }
  if (alt || meta) return "pass";
  if (ctrl && letter === "c") return shift || hasSelection ? "copy" : "pass";
  if (ctrl && letter === "v") return "native";
  if (event.key === "Insert" && ctrl && !shift) return "copy";
  if (event.key === "Insert" && shift && !ctrl) return "native";
  return "pass";
}

// What a copy puts on the clipboard: xterm's selection without the padding spaces at line ends.
export function selectionText(text) {
  return text.replace(/[ \t]+(?=\r?\n|$)/g, "");
}
