// The Board page's protocol state machine (Task Inbox SPEC "WebSocket terminal 프로토콜", D-28, P2-AC-35).
// Pure: no DOM, no WebSocket, no timers. board.js feeds events to step() and carries out the effects it returns.
//
// Phases: idle -> connecting -> attached; attached -> disconnected (network, timeout; reconnects with `resume`)
//         attached|connecting -> taken_elsewhere (taken_over / taken_elsewhere; never reconnects by itself)
//         attached -> released (pagehide; pageshow of a restored page resumes)
// Events: start, opened, message, data, closed (all but start carry the socket number `conn`; events of an
//         older socket are ignored), timer {name: ping|pong|reconnect|stable}, retake, reconnect, input, resize,
//         pagehide, pageshow.
// Effects: open {conn}, close {conn, code}, send {message}, send_binary {bytes}, write {channel, payload},
//          reset_terminals,
//          set_timer {name, ms}, clear_timer {name} (names: ping, pong, reconnect, stable), notice {reason, channel}.
export const PROTOCOL_VERSION = 1;
export const PONG_LIMIT_MS = 20000;
export const DEFAULT_HEARTBEAT_MS = 10000;
export const BACKOFF_FIRST_MS = 1000;
export const BACKOFF_MAX_MS = 15000;
export const MIN_SIZE = 1;
export const MAX_SIZE = 500;
export const INPUT_FRAME_MAX = 64 * 1024; // SPEC 입력 한도: the server drops a bigger input frame (too_large)
export const INPUT_MAX = 4 << 20; // one paste at most (the Hub paces it into the PTY; a bigger one is refused whole)
export const BANNER_TEXT = "다른 곳에서 연결됨 — 다시 가져오기";
const CLIENT_TIMEOUT_CODE = 4000; // our own close when no pong came (not a server code)
// 4400-4499: the server refused this page itself (4400 bad hello, 4403 csrf mismatch, any later client error).
// Retrying the same hello cannot succeed, so the tab shows 연결 없음 and waits for the owner's 다시 연결.
const isClientRefusal = (code) => code >= 4400 && code <= 4499;
export const SESSION_ENDED_CODE = 4401; // the login session ended: only a new login helps
export const SESSION_ENDED_TEXT = "연결 없음 — 다시 로그인";
export const STABLE_MS = 10000; // an attach must hold this long before the reconnect delay starts over
const CHANNEL_IDS = { agent: 0, shell: 1 };
const CHANNEL_NAMES = ["agent", "shell"];
const NONE = (state) => ({ state, effects: [] });
const CLEAR_HEARTBEAT = [
  { type: "clear_timer", name: "ping" }, { type: "clear_timer", name: "pong" }, { type: "clear_timer", name: "stable" },
];

export function initialState({ csrf }) {
  return {
    phase: "idle", closeCode: null, csrf, conn: 0, sizes: {}, resume: null, epoch: null, channels: {}, attempt: 0, pings: 0,
  };
}

export function encodeFrame(channel, epoch, payload) {
  const bytes = new Uint8Array(5 + payload.length);
  bytes[0] = CHANNEL_IDS[channel];
  new DataView(bytes.buffer).setUint32(1, epoch, false);
  bytes.set(payload, 5);
  return bytes;
}

export function decodeFrame(bytes) {
  if (bytes.length < 5 || bytes[0] >= CHANNEL_NAMES.length) return null;
  const epoch = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength).getUint32(1, false);
  return { channel: CHANNEL_NAMES[bytes[0]], epoch, payload: bytes.subarray(5) };
}

const CONNECTION_LABELS = {
  attached: "연결됨", disconnected: "연결 없음", taken_elsewhere: "다른 곳에서 연결됨", connecting: "연결 중",
  released: "연결 없음",
};

// What the page shows for a state: the connection label, the takeover banner, the one action offered
// ("retake" = 다시 가져오기, resume null; "reconnect" = 다시 연결, resume) and whether input is sent at all.
const sessionEnded = (state) => state.phase === "disconnected" && state.closeCode === SESSION_ENDED_CODE;

export function describe(state) {
  return {
    connection: sessionEnded(state) ? SESSION_ENDED_TEXT : CONNECTION_LABELS[state.phase] || "",
    banner: state.phase === "taken_elsewhere",
    bannerText: BANNER_TEXT,
    action: state.phase === "taken_elsewhere" ? "retake"
      : sessionEnded(state) ? "login" : state.phase === "disconnected" ? "reconnect" : null,
    canInput: state.phase === "attached",
    channels: state.channels,
  };
}

const CHANNEL_STATE_LABELS = {
  running: "실행 중", starting: "시작 중", not_started: "시작 전",
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
    default: return CHANNEL_STATE_LABELS[info.state] || info.state;
  }
}

export function step(state, event) {
  switch (event.type) {
    case "start":
      return connect({ ...state, sizes: { ...event.sizes } }, state.resume);
    case "retake":
      return state.phase === "taken_elsewhere" ? connect(state, null) : NONE(state);
    case "reconnect":
      return state.phase === "disconnected" && !sessionEnded(state)
        ? withEffects(connect(state, state.resume), [{ type: "clear_timer", name: "reconnect" }]) : NONE(state);
    case "input": return onInput(state, event);
    case "resize": return onResize(state, event);
    case "timer": return onTimer(state, event.name);
    case "pagehide": return onPageHide(state);
    case "pageshow":
      return event.persisted && state.phase === "released" ? connect(state, state.resume) : NONE(state);
    default:
      if (event.conn !== state.conn) return NONE(state);
      return onSocketEvent(state, event);
  }
}

function onSocketEvent(state, event) {
  switch (event.type) {
    case "opened":
      return {
        state,
        effects: [{
          type: "send",
          message: { t: "hello", v: PROTOCOL_VERSION, csrf: state.csrf, resume: state.resume, sizes: state.sizes },
        }],
      };
    case "message": return onMessage(state, event.message);
    case "data": return onData(state, event.bytes);
    case "closed": return onClosed(state, event.code);
    default: return NONE(state);
  }
}

function withEffects(result, before) {
  return { state: result.state, effects: [...before, ...result.effects] };
}

function connect(state, resume) {
  const conn = state.conn + 1;
  return { state: { ...state, phase: "connecting", conn, resume, closeCode: null }, effects: [{ type: "open", conn }] };
}

function onClosed(state, code) {
  if (state.phase !== "connecting" && state.phase !== "attached") return NONE(state);
  const next = { ...state, phase: "disconnected", epoch: null, closeCode: code };
  if (isClientRefusal(code)) return { state: next, effects: [...CLEAR_HEARTBEAT] };
  return disconnected(next, [...CLEAR_HEARTBEAT]);
}

// The network or the server ended the connection: retry with `resume` after 1, 2, 4 ... <= 15 s.
function disconnected(state, effects) {
  const delay = Math.min(BACKOFF_FIRST_MS * 2 ** state.attempt, BACKOFF_MAX_MS);
  return {
    state: { ...state, attempt: state.attempt + 1 },
    effects: [...effects, { type: "set_timer", name: "reconnect", ms: delay }],
  };
}

function onTimer(state, name) {
  if (name === "reconnect" && state.phase === "disconnected" && !sessionEnded(state)) {
    return connect(state, state.resume);
  }
  if (state.phase !== "attached") return NONE(state);
  if (name === "stable") return { state: { ...state, attempt: 0 }, effects: [] };
  if (name === "ping") {
    const n = state.pings + 1;
    return {
      state: { ...state, pings: n },
      effects: [
        { type: "send", message: { t: "ping", epoch: state.epoch, n } },
        { type: "set_timer", name: "ping", ms: state.heartbeatMs },
      ],
    };
  }
  if (name === "pong") { // no pong for 20 s: the connection is dead even if the browser still thinks it is open
    const next = { ...state, phase: "disconnected", epoch: null };
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

// Only the holder with its current epoch sends input (D-28 (2)); anything else is dropped here, not sent.
function onInput(state, event) {
  if (state.phase !== "attached" || event.bytes.length === 0) return NONE(state);
  if (event.bytes.length > INPUT_MAX) {
    return { state, effects: [{ type: "notice", reason: "too_large", channel: event.channel }] };
  }
  return {
    state,
    effects: splitInput(event.bytes).map((piece) => (
      { type: "send_binary", bytes: encodeFrame(event.channel, state.epoch, piece) })),
  };
}

function clamp(value) {
  return Math.min(MAX_SIZE, Math.max(MIN_SIZE, Math.trunc(Number(value)) || MIN_SIZE));
}

function onResize(state, event) {
  const rows = clamp(event.rows);
  const cols = clamp(event.cols);
  const next = { ...state, sizes: { ...state.sizes, [event.channel]: [rows, cols] } };
  if (state.phase !== "attached") return NONE(next);
  return {
    state: next,
    effects: [{ type: "send", message: { t: "resize", epoch: state.epoch, channel: event.channel, rows, cols } }],
  };
}

// Tab close or navigation: give the lease back at once (best effort; the server's heartbeat is the fallback).
function onPageHide(state) {
  if (state.phase === "connecting") { // nothing held yet: do not leave a socket that could attach unseen
    return {
      state: { ...state, phase: "released" },
      effects: [{ type: "close", conn: state.conn, code: 1000 }],
    };
  }
  if (state.phase !== "attached") return NONE(state);
  return {
    state: { ...state, phase: "released", epoch: null },
    effects: [...CLEAR_HEARTBEAT, { type: "send", message: { t: "release", epoch: state.epoch } }],
  };
}

function onMessage(state, message) {
  switch (message.t) {
    case "attached": return onAttached(state, message);
    case "taken_over":
    case "taken_elsewhere":
      return { state: { ...state, phase: "taken_elsewhere", epoch: null }, effects: [...CLEAR_HEARTBEAT] };
    case "pong":
      return state.phase === "attached"
        ? { state, effects: [{ type: "set_timer", name: "pong", ms: PONG_LIMIT_MS }] } : NONE(state);
    case "state":
      return { state: { ...state, channels: message.channels || {} }, effects: [] };
    case "error":
      return { state, effects: [{ type: "notice", reason: message.reason, channel: message.channel }] };
    default:
      return NONE(state);
  }
}

function onAttached(state, message) {
  const heartbeatMs = (Number(message.heartbeat) || 0) * 1000 || DEFAULT_HEARTBEAT_MS;
  return {
    state: {
      ...state, phase: "attached", epoch: message.epoch, channels: message.channels || {}, pings: 0,
      heartbeatMs, resume: { run: message.run, epoch: message.epoch },
    },
    effects: [
      { type: "reset_terminals" }, // the replay that follows is the whole ring: drop what is on screen first
      { type: "set_timer", name: "ping", ms: heartbeatMs },
      { type: "set_timer", name: "pong", ms: PONG_LIMIT_MS },
      { type: "set_timer", name: "stable", ms: STABLE_MS },
    ],
  };
}

function onData(state, bytes) {
  const frame = state.phase === "attached" ? decodeFrame(bytes) : null;
  if (frame === null || frame.epoch !== state.epoch) return NONE(state);
  return { state, effects: [{ type: "write", channel: frame.channel, payload: frame.payload }] };
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
