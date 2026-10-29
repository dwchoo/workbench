// Board panel and widget model (web-board-ux SPEC s4 "화면: workspace와 panel"): the rows, the limits, name feedback,
// the session API calls, the confirm rule, the widget cover texts and the connection budget. Pure apart from the
// `fetch` it is given. board.js draws what these return (textContent only: a terminal name is text, never markup).
import { channelLabel, GONE_TEXT } from "./board-state.js";

export const POLL_MS = 5000; // GET /api/board refresh
export const HELLO_MAX = 4; // sockets waiting for their hello at one time (the Hub allows 8 per login session)
export const NAME_MAX = 40;

// The Hub's fixed sentences (pages/board_api.py DETAIL), used when a reply carries only a reason.
const FALLBACK_TEXT = {
  bad_input: "요청 형식이 맞지 않습니다.",
  name_invalid: "이름은 앞뒤 공백을 뺀 1~40자이고 제어 문자가 없어야 합니다.",
  confirm_required: "확인이 필요한 동작입니다.",
  limit_out_of_range: "한도는 agent 1~8, shell 1~32입니다.",
  wrong_kind: "이 terminal 종류에는 할 수 없는 동작입니다.",
  bad_state: "지금 상태에서는 할 수 없는 동작입니다.",
  unknown_terminal: "그런 terminal이 없습니다.",
  name_taken: "같은 종류에 같은 이름(대소문자 무시)이 이미 있습니다.",
  limit_reached: "한도에 닿아 더 만들 수 없습니다.",
  limit_below_count: "지금 개수보다 작은 한도는 정할 수 없습니다. 먼저 닫거나 삭제하세요.",
  terminal_busy: "이 terminal의 다른 동작이 진행 중입니다.",
  registry_write_failed: "Board 목록 파일을 쓰지 못했습니다. 아무것도 바뀌지 않았습니다.",
  delete_failed: "Board agent의 home을 지우지 못했습니다. 목록에 남아 있으니 고친 뒤 다시 삭제하세요.",
  board_unavailable: "Board를 사용할 수 없습니다 (웹이 꺼졌거나 Hub가 멈추는 중입니다).",
  registry_invalid: "Board 목록 파일을 읽을 수 없습니다. wb hub status의 안내를 따르세요.",
  network: "서버에 닿지 않았습니다. 잠시 뒤 다시 시도하세요.",
  bad_response: "서버 응답을 읽을 수 없습니다.",
};
const GENERIC_ERROR = "동작하지 못했습니다.";

export function errorText(result) {
  if (result && typeof result.detail === "string" && result.detail !== "") return result.detail;
  return (result && FALLBACK_TEXT[result.reason]) || GENERIC_ERROR;
}

// ---- rows ---------------------------------------------------------------------------------------------------
const DOT = { running: "on", starting: "warn", failed: "error", isolation_failed: "error" };
const RUNNING = new Set(["running", "starting"]);

// One panel row. ctx: {narrow, widgets: {id: {visible}}, chosen}.
export function rowModel(view, ctx) {
  const agent = view.kind === "agent";
  const busy = view.busy === true;
  let toggle;
  if (ctx.narrow) {
    const chosen = ctx.chosen === view.id;
    toggle = { label: chosen ? "보는 중" : "보기", disabled: chosen, kind: "choose" };
  } else {
    const shown = Boolean(ctx.widgets[view.id] && ctx.widgets[view.id].visible);
    toggle = { label: shown ? "숨기기" : "보이기", disabled: false, kind: "visibility" };
  }
  let run = null;
  if (agent) {
    const running = RUNNING.has(view.state);
    run = { action: running ? "stop" : "start", label: running ? "중지" : "시작", disabled: busy };
  }
  return {
    id: view.id, kind: view.kind, name: view.name, dot: DOT[view.state] || "off", stateText: channelLabel(view), busy, toggle,
    run, canRename: !busy, canDelete: agent && !busy, canClose: !agent && !busy,
  };
}

export function limitModel(options, terminals) {
  const make = (kind, key) => {
    const count = terminals.filter((terminal) => terminal.kind === kind).length;
    const limit = options && Number.isInteger(options[key]) ? options[key] : null;
    const full = limit !== null && count >= limit;
    return { count, limit, full, note: limit === null ? "" : full ? `한도 ${limit}개` : `${count}/${limit}` };
  };
  return { agent: make("agent", "agent_limit"), shell: make("shell", "shell_limit") };
}

// The limit form -> the request body, or the text to show when it can not be right (the Hub decides in the end).
export function optionsRequest(values, options, terminals) {
  const [agentMin, agentMax] = (options && options.agent_range) || [1, 8];
  const [shellMin, shellMax] = (options && options.shell_range) || [1, 32];
  const whole = (value) => (/^\s*[0-9]+\s*$/.test(String(value)) ? Number(String(value).trim()) : null);
  const agent = whole(values.agent);
  const shell = whole(values.shell);
  if (agent === null || shell === null || agent < agentMin || agent > agentMax || shell < shellMin || shell > shellMax) {
    return { ok: false, text: FALLBACK_TEXT.limit_out_of_range };
  }
  const counts = limitModel(null, terminals);
  if (agent < counts.agent.count || shell < counts.shell.count) return { ok: false, text: FALLBACK_TEXT.limit_below_count };
  return { ok: true, body: { agent_limit: agent, shell_limit: shell } };
}

// ---- names --------------------------------------------------------------------------------------------------
const FORBIDDEN = /[\p{Cc}\p{Cf}\p{Zl}\p{Zp}]/u;
const fold = (text) => text.toLowerCase();

// null when the name is acceptable, else {reason, text} with the Hub's own reason and sentence.
export function nameProblem(name, { kind, terminals, selfId = null }) {
  const invalid = { reason: "name_invalid", text: FALLBACK_TEXT.name_invalid };
  if (typeof name !== "string") return invalid;
  const text = name.trim();
  const length = [...text].length;
  if (length < 1 || length > NAME_MAX || FORBIDDEN.test(text)) return invalid;
  const same = terminals.some((terminal) => terminal.kind === kind && terminal.id !== selfId && fold(terminal.name) === fold(text));
  return same ? { reason: "name_taken", text: FALLBACK_TEXT.name_taken } : null;
}

// Keys of the in-place rename input. Enter during an IME composition (Hangul) commits the syllable: it saves nothing.
export function renameKey(event) {
  if (event.isComposing || event.keyCode === 229) return null;
  if (event.key === "Enter") return "save";
  if (event.key === "Escape") return "cancel";
  return null;
}

// ---- the session API ------------------------------------------------------------------------------------------
export function createApi({ fetch, csrf }) {
  async function call(method, path, body, signal) {
    const headers = { "X-CSRF-Token": csrf };
    const init = { method, credentials: "same-origin", headers };
    if (method !== "GET") {
      headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(body || {});
    }
    if (signal) init.signal = signal;
    let response;
    try {
      response = await fetch(path, init);
    } catch (error) {
      const aborted = Boolean(error) && error.name === "AbortError";
      return { ok: false, status: 0, json: null, reason: aborted ? "aborted" : "network", detail: null, unauthorized: false,
        networkError: !aborted, aborted };
    }
    let json = null;
    try {
      json = await response.json();
    } catch (_) {
      json = null;
    }
    const object = json !== null && typeof json === "object" && !Array.isArray(json);
    const ok = object && json.ok === true && response.status >= 200 && response.status < 300;
    return {
      ok, status: response.status, json: object ? json : null, unauthorized: response.status === 401, networkError: false, aborted: false,
      reason: ok ? null : object && typeof json.reason === "string" ? json.reason : "bad_response",
      detail: object && typeof json.detail === "string" ? json.detail : null,
    };
  }
  const terminal = (id, action, body) => call("POST", `/api/board/terminals/${encodeURIComponent(id)}/${action}`, body);
  return {
    list: ({ signal } = {}) => call("GET", "/api/board", null, signal),
    open: (visible) => call("POST", "/api/board/open", { visible }),
    createAgent: () => call("POST", "/api/board/agents", {}),
    createShell: () => call("POST", "/api/board/shells", {}),
    start: (id) => terminal(id, "start", {}),
    stop: (id) => terminal(id, "stop", { confirm: true }),
    restart: (id) => terminal(id, "restart", { confirm: true }),
    close: (id) => terminal(id, "close", { confirm: true }),
    remove: (id) => terminal(id, "delete", { confirm: true }),
    rename: (id, name) => terminal(id, "rename", { name }),
    setOptions: (body) => call("POST", "/api/board/options", body),
  };
}

// ---- actions behind the confirm dialog -------------------------------------------------------------------------
export function confirmText(action, name) {
  switch (action) {
    case "stop": return `${name}을 중지합니다. 메모리는 남습니다.`;
    case "delete": return `${name}의 세션과 메모리를 지웁니다. 되돌릴 수 없습니다.`;
    case "close": return `${name}을 닫습니다. 실행 중인 명령이 끝납니다.`;
    case "restart": return `${name}을 끝내고 다시 시작합니다. 진행 중인 응답과 명령은 끊깁니다.`;
    default: return null;
  }
}

const ACTION_KIND = { stop: "agent", delete: "agent", close: "shell", start: null, restart: null };
const ACTION_CALL = { start: "start", stop: "stop", restart: "restart", close: "close", delete: "remove" };

// Run one terminal action. stop, delete, close and restart ask `confirm(text)` first and send nothing on "no".
export async function terminalAction(api, action, view, { confirm }) {
  const refused = (reason) => ({ ok: false, reason, detail: null, status: 0, json: null, unauthorized: false });
  if (!(action in ACTION_CALL) || (ACTION_KIND[action] !== null && ACTION_KIND[action] !== view.kind)) return refused("wrong_kind");
  if (view.busy === true) return refused("terminal_busy");
  const text = confirmText(action, view.name);
  if (text !== null && !(await confirm(text))) return { ok: false, cancelled: true, reason: "cancelled", status: 0, json: null };
  return api[ACTION_CALL[action]](view.id);
}

// The widget's × : an agent is stopped, a shell is closed (WBX-D56 (2)).
export const closeAction = (view) => (view.kind === "agent" ? "stop" : "close");

// ---- widget texts ----------------------------------------------------------------------------------------------
export function othersText(count) {
  return Number.isFinite(count) && count >= 1 ? `다른 화면 ${count}개 연결됨` : "";
}

const LOGIN_LABEL = "다시 로그인";
const RECONNECT_LABEL = "다시 연결";

// What covers the terminal body, if anything: {text, action, label}. `desc` is board-state describe(). A connection
// problem comes first (the process state shown would be old); then a stopped, finished or failed terminal.
export function overlayFor(view, desc) {
  if (desc.gone) return { text: GONE_TEXT, action: null, label: null };
  if (desc.action === "login") return { text: desc.connection, action: "login", label: LOGIN_LABEL };
  if (desc.action === "reconnect") return { text: desc.connection, action: "reconnect", label: RECONNECT_LABEL };
  if (!view) return null;
  switch (view.state) {
    case "not_started": return { text: "시작 전 — 시작", action: "start", label: "시작 전 — 시작" };
    case "stopped": return { text: "중지됨 — 시작", action: "start", label: "중지됨 — 시작" };
    case "starting": return { text: "시작 중", action: null, label: null };
    case "exited":
    case "failed":
    case "isolation_failed": {
      const note = view.message ? ` — ${view.message}` : "";
      return { text: `${channelLabel(view)}${note}`, action: "restart", label: "재시작" };
    }
    default: return null;
  }
}

// ---- connection budget ------------------------------------------------------------------------------------------
// widgets: [{id, wanted, phase}] (phase from board-state). Hidden widgets are released; wanted idle or released ones
// connect, at most HELLO_MAX waiting for a hello; refused and gone ones are left alone (no automatic retry).
export function planConnections(widgets, max = HELLO_MAX) {
  const live = new Set(["connecting", "attached", "disconnected"]);
  const release = widgets.filter((widget) => !widget.wanted && live.has(widget.phase)).map((widget) => widget.id);
  let room = max - widgets.filter((widget) => widget.wanted && widget.phase === "connecting").length;
  const connect = [];
  for (const widget of widgets) {
    if (room <= 0) break;
    if (widget.wanted && (widget.phase === "idle" || widget.phase === "released")) {
      connect.push(widget.id);
      room -= 1;
    }
  }
  return { connect, release };
}
