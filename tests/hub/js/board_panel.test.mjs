// WBX-D2 / seam S8: the panel model, the panel's API calls (a mock fetch), the confirm rule, name feedback and the
// widget texts of static/board-panel.js (no DOM, no browser). Expected strings come from SPEC s4 "화면" and the
// session API error table (the Hub's fixed sentences); the HTTP shapes from SPEC "Session API".
import test from "node:test";
import assert from "node:assert/strict";
import * as P from "../../../src/workbench/hub/web/static/board-panel.js";
import { initialState, step, describe, GONE_TEXT, TOO_MANY_TEXT, SESSION_ENDED_TEXT } from "../../../src/workbench/hub/web/static/board-state.js";

const view = (over = {}) => ({ id: "a-0a1", kind: "agent", name: "agent-1", state: "running", detail: null, exit_status: null,
  clients: 1, busy: false, ...over });
const agents = (n) => Array.from({ length: n }, (_, i) => view({ id: `a-0${i}`, name: `agent-${i + 1}` }));
const shells = (n) => Array.from({ length: n }, (_, i) => view({ id: `s-0${i}`, kind: "shell", name: `shell-${i + 1}` }));
const OPTIONS = { agent_limit: 4, shell_limit: 10, agent_range: [1, 8], shell_range: [1, 32] };

// ---- (8) panel state ----------------------------------------------------------------------------------------
test("an agent row: dot by state, show/hide, start or stop, rename and delete; shells have rename, show/hide and close", () => {
  const widgets = { "a-0a1": { visible: true } };
  const ctx = { narrow: false, widgets, chosen: null };
  const running = P.rowModel(view(), ctx);
  assert.deepEqual([running.dot, running.toggle.label, running.run, running.canRename, running.canDelete],
    ["on", "숨기기", { action: "stop", label: "중지", disabled: false }, true, true]);
  assert.equal(P.rowModel(view({ state: "starting" }), ctx).run.action, "stop");
  assert.equal(P.rowModel(view({ state: "starting" }), ctx).dot, "warn");
  for (const state of ["not_started", "stopped", "exited", "failed", "isolation_failed"]) {
    assert.deepEqual(P.rowModel(view({ state }), ctx).run, { action: "start", label: "시작", disabled: false }, state);
  }
  assert.equal(P.rowModel(view({ state: "failed" }), ctx).dot, "error");
  assert.equal(P.rowModel(view({ state: "isolation_failed" }), ctx).dot, "error");
  assert.equal(P.rowModel(view({ state: "stopped" }), ctx).dot, "off");
  const hidden = P.rowModel(view(), { ...ctx, widgets: { "a-0a1": { visible: false } } });
  assert.equal(hidden.toggle.label, "보이기");
  const shell = P.rowModel(view({ id: "s-1", kind: "shell", name: "shell-1" }), { ...ctx, widgets: { "s-1": { visible: true } } });
  assert.equal(shell.run, null, "a shell has no start/stop");
  assert.equal(shell.canDelete, false);
  assert.equal(shell.canClose, true);
  assert.equal(running.canClose, false);
});

test("a busy terminal's row disables every action (the Hub answers terminal_busy to them anyway)", () => {
  const row = P.rowModel(view({ busy: true }), { narrow: false, widgets: { "a-0a1": { visible: true } }, chosen: null });
  assert.equal(row.busy, true);
  assert.equal(row.run.disabled, true);
  assert.equal(row.toggle.disabled, false, "show/hide is this browser's own");
  assert.equal(row.canRename, false);
  assert.equal(row.canDelete, false);
});

test("on a narrow screen the toggle chooses the one terminal instead of showing or hiding", () => {
  const ctx = { narrow: true, widgets: { "a-0a1": { visible: false } }, chosen: "a-0a2" };
  assert.deepEqual(P.rowModel(view(), ctx).toggle, { label: "보기", disabled: false, kind: "choose" });
  assert.deepEqual(P.rowModel(view(), { ...ctx, chosen: "a-0a1" }).toggle, { label: "보는 중", disabled: true, kind: "choose" });
  assert.equal(P.rowModel(view(), { ...ctx, narrow: false }).toggle.kind, "visibility");
});

test("the row's state text names the state with its reason", () => {
  const ctx = { narrow: false, widgets: {}, chosen: null };
  assert.equal(P.rowModel(view({ state: "isolation_failed", detail: "mcp_leak" }), ctx).stateText, "격리 실패: mcp_leak");
  assert.equal(P.rowModel(view({ state: "exited", exit_status: 3 }), ctx).stateText, "종료됨 (종료 코드 3)");
});

test("limits: creation is disabled at the limit with the reason 한도 N개; below it the count shows", () => {
  const some = P.limitModel(OPTIONS, [...agents(3), ...shells(2)]);
  assert.deepEqual(some.agent, { count: 3, limit: 4, full: false, note: "3/4" });
  assert.deepEqual(some.shell, { count: 2, limit: 10, full: false, note: "2/10" });
  const full = P.limitModel(OPTIONS, [...agents(4), ...shells(10)]);
  assert.deepEqual([full.agent.full, full.agent.note, full.shell.full, full.shell.note], [true, "한도 4개", true, "한도 10개"]);
  assert.equal(P.limitModel(null, agents(1)).agent.full, false, "no options yet: not disabled, the Hub decides");
});

test("the limit form is checked before it is sent: whole numbers in range and not under what exists now", () => {
  const existing = [...agents(3), ...shells(2)];
  assert.deepEqual(P.optionsRequest({ agent: "5", shell: "12" }, OPTIONS, existing), { ok: true, body: { agent_limit: 5, shell_limit: 12 } });
  assert.deepEqual(P.optionsRequest({ agent: " 3 ", shell: "2" }, OPTIONS, existing), { ok: true, body: { agent_limit: 3, shell_limit: 2 } });
  for (const bad of ["", "0", "9", "-1", "1.5", "abc", "1e1", "４"]) {
    const result = P.optionsRequest({ agent: bad, shell: "10" }, OPTIONS, existing);
    assert.equal(result.ok, false, bad);
    assert.equal(result.text, "한도는 agent 1~8, shell 1~32입니다.", bad);
  }
  assert.equal(P.optionsRequest({ agent: "4", shell: "33" }, OPTIONS, existing).text, "한도는 agent 1~8, shell 1~32입니다.");
  const low = P.optionsRequest({ agent: "2", shell: "10" }, OPTIONS, existing);
  assert.deepEqual([low.ok, low.text], [false, "지금 개수보다 작은 한도는 정할 수 없습니다. 먼저 닫거나 삭제하세요."]);
});

test("name feedback: the Hub's rules (trimmed 1-40 code points, no control characters, unique ignoring case within a kind)", () => {
  const list = [...agents(2), ...shells(1)];
  const check = (name, kind = "agent", selfId = "a-00") => P.nameProblem(name, { kind, terminals: list, selfId });
  assert.equal(check("전처리"), null);
  assert.equal(check("  전처리  "), null, "trimmed");
  assert.equal(check("agent-1"), null, "its own name is fine (unchanged)");
  const invalid = "이름은 앞뒤 공백을 뺀 1~40자이고 제어 문자가 없어야 합니다.";
  for (const name of ["", "   ", "x".repeat(41), "a\tb", "a\nb", "a\u0000b", "a​b", "a b", "a b"]) {
    assert.deepEqual(check(name), { reason: "name_invalid", text: invalid }, JSON.stringify(name));
  }
  assert.equal(check("x".repeat(40)), null, "40 is allowed");
  assert.equal(check("😀".repeat(40)), null, "length is counted in code points, not UTF-16 units");
  assert.equal(check("😀".repeat(41))?.reason, "name_invalid");
  const taken = { reason: "name_taken", text: "같은 종류에 같은 이름(대소문자 무시)이 이미 있습니다." };
  assert.deepEqual(check("AGENT-2"), taken, "ignoring case");
  assert.deepEqual(check(" agent-2 "), taken, "after trimming");
  assert.equal(check("shell-1"), null, "another kind may use the name");
  assert.equal(check("shell-1", "shell", "s-09")?.reason, "name_taken");
  assert.equal(check(null)?.reason, "name_invalid");
});

test("rename input keys: Enter saves, Esc cancels, Enter inside an IME composition does neither", () => {
  assert.equal(P.renameKey({ key: "Enter", isComposing: false, keyCode: 13 }), "save");
  assert.equal(P.renameKey({ key: "Escape", isComposing: false, keyCode: 27 }), "cancel");
  assert.equal(P.renameKey({ key: "Enter", isComposing: true, keyCode: 13 }), null, "Hangul composition commit");
  assert.equal(P.renameKey({ key: "Enter", isComposing: false, keyCode: 229 }), null, "keyCode 229: an IME key");
  assert.equal(P.renameKey({ key: "Escape", isComposing: true, keyCode: 27 }), null, "Esc ends a composition first");
  assert.equal(P.renameKey({ key: "a", isComposing: false, keyCode: 65 }), null);
});

test("the Hub's reason becomes the text shown: its own sentence, else a fixed fallback; never an exception text", () => {
  assert.equal(P.errorText({ ok: false, reason: "name_taken", detail: "같은 종류에 같은 이름(대소문자 무시)이 이미 있습니다." }),
    "같은 종류에 같은 이름(대소문자 무시)이 이미 있습니다.");
  assert.equal(P.errorText({ ok: false, reason: "limit_reached" }), "한도에 닿아 더 만들 수 없습니다.");
  assert.equal(P.errorText({ ok: false, reason: "network" }), "서버에 닿지 않았습니다. 잠시 뒤 다시 시도하세요.");
  assert.equal(P.errorText({ ok: false, reason: "zzz" }), "동작하지 못했습니다.");
  assert.equal(P.errorText(null), "동작하지 못했습니다.");
});

// ---- the API calls -------------------------------------------------------------------------------------------
function mockFetch(replies) {
  const calls = [];
  const fetch = (path, options = {}) => {
    calls.push({ path, method: options.method || "GET", headers: options.headers || {}, body: options.body === undefined ? undefined : JSON.parse(options.body), options });
    const next = typeof replies === "function" ? replies(path, options) : replies;
    if (next instanceof Error) return Promise.reject(next);
    return Promise.resolve({ status: next.status ?? 200, json: next.json ? () => next.json() : () => Promise.resolve(next.body ?? { ok: true }) });
  };
  return { fetch, calls };
}

test("every call is a same-origin fetch with the CSRF header; reads are GET, changes are POST with a JSON body", async () => {
  const { fetch, calls } = mockFetch({ body: { ok: true, terminals: [] } });
  const api = P.createApi({ fetch, csrf: "tok" });
  await api.list();
  await api.open(["a-0a1", "s-0b2"]);
  await api.createAgent();
  await api.createShell();
  await api.start("a-0a1");
  await api.stop("a-0a1");
  await api.restart("a-0a1");
  await api.close("s-0b2");
  await api.remove("a-0a1");
  await api.rename("a-0a1", "전처리");
  await api.setOptions({ agent_limit: 5, shell_limit: 12 });
  assert.deepEqual(calls.map((call) => [call.method, call.path, call.body]), [
    ["GET", "/api/board", undefined],
    ["POST", "/api/board/open", { visible: ["a-0a1", "s-0b2"] }],
    ["POST", "/api/board/agents", {}],
    ["POST", "/api/board/shells", {}],
    ["POST", "/api/board/terminals/a-0a1/start", {}],
    ["POST", "/api/board/terminals/a-0a1/stop", { confirm: true }],
    ["POST", "/api/board/terminals/a-0a1/restart", { confirm: true }],
    ["POST", "/api/board/terminals/s-0b2/close", { confirm: true }],
    ["POST", "/api/board/terminals/a-0a1/delete", { confirm: true }],
    ["POST", "/api/board/terminals/a-0a1/rename", { name: "전처리" }],
    ["POST", "/api/board/options", { agent_limit: 5, shell_limit: 12 }],
  ]);
  for (const call of calls) {
    assert.equal(call.options.credentials, "same-origin", call.path);
    assert.equal(call.headers["X-CSRF-Token"], "tok", call.path);
    if (call.method === "POST") assert.equal(call.headers["Content-Type"], "application/json", call.path);
  }
});

test("a terminal id is URL-encoded into the path and a name is sent as JSON text, never in the URL", async () => {
  const { fetch, calls } = mockFetch({});
  const api = P.createApi({ fetch, csrf: "t" });
  await api.start("a/../x?y");
  assert.equal(calls[0].path, "/api/board/terminals/a%2F..%2Fx%3Fy/start");
  await api.rename("a-1", '<img src=x onerror=1>"');
  assert.equal(calls[1].body.name, '<img src=x onerror=1>"');
  assert.ok(!calls[1].path.includes("img"));
});

test("results are normalized: ok only when the Hub says ok, reason and detail from its body, 401 flagged for /login", async () => {
  let reply = { status: 200, body: { ok: true, terminal: { id: "a-1" } } };
  const api = P.createApi({ fetch: () => Promise.resolve({ status: reply.status, json: () => Promise.resolve(reply.body) }), csrf: "t" });
  const good = await api.start("a-1");
  assert.deepEqual([good.ok, good.status, good.unauthorized, good.json.terminal.id], [true, 200, false, "a-1"]);
  reply = { status: 409, body: { ok: false, reason: "name_taken", detail: "같은 종류에 같은 이름(대소문자 무시)이 이미 있습니다." } };
  const taken = await api.rename("a-1", "x");
  assert.deepEqual([taken.ok, taken.reason, taken.detail], [false, "name_taken", "같은 종류에 같은 이름(대소문자 무시)이 이미 있습니다."]);
  reply = { status: 401, body: { ok: false } };
  assert.equal((await api.list()).unauthorized, true);
  reply = { status: 200, body: "not an object" };
  assert.equal((await api.list()).ok, false, "a body that is not the Hub's JSON object is not a success");
});

test("a network failure, a bad body and an aborted request become results, never exceptions", async () => {
  const down = P.createApi({ fetch: () => Promise.reject(new TypeError("Failed to fetch")), csrf: "t" });
  const result = await down.start("a-1");
  assert.deepEqual([result.ok, result.reason, result.networkError], [false, "network", true]);
  const garbage = P.createApi({ fetch: () => Promise.resolve({ status: 502, json: () => Promise.reject(new SyntaxError("x")) }), csrf: "t" });
  const bad = await garbage.list();
  assert.deepEqual([bad.ok, bad.status, bad.reason], [false, 502, "bad_response"]);
  const aborted = P.createApi({ fetch: () => Promise.reject(Object.assign(new Error("a"), { name: "AbortError" })), csrf: "t" });
  assert.equal((await aborted.list()).aborted, true);
});

test("list passes the abort signal to fetch (pagehide aborts the refresh)", async () => {
  const { fetch, calls } = mockFetch({});
  const controller = new AbortController();
  await P.createApi({ fetch, csrf: "t" }).list({ signal: controller.signal });
  assert.equal(calls[0].options.signal, controller.signal);
});

// ---- (9) confirm dialog before stop, delete, close, restart ---------------------------------------------------
test("the confirm texts are the SPEC sentences with the terminal's name", () => {
  assert.equal(P.confirmText("stop", "전처리"), "전처리을 중지합니다. 메모리는 남습니다."); // the SPEC sentence, 을 as written
  assert.equal(P.confirmText("delete", "agent-2"), "agent-2의 세션과 메모리를 지웁니다. 되돌릴 수 없습니다.");
  assert.equal(P.confirmText("close", "shell-1"), "shell-1을 닫습니다. 실행 중인 명령이 끝납니다.");
  assert.match(P.confirmText("restart", "agent-1"), /^agent-1을 끝내고 다시 시작합니다\./);
  assert.equal(P.confirmText("start", "agent-1"), null);
  assert.equal(P.confirmText("rename", "agent-1"), null);
});

test("the confirm text takes the name as plain text (no markup interpretation is possible; the caller uses textContent)", () => {
  assert.ok(P.confirmText("close", "<b>x</b>").includes("<b>x</b>"));
});

test("stop, delete, close and restart call the Hub only after the dialog says yes; start needs none", async () => {
  for (const [action, kind, path, body] of [
    ["stop", "agent", "/api/board/terminals/t-1/stop", { confirm: true }],
    ["delete", "agent", "/api/board/terminals/t-1/delete", { confirm: true }],
    ["close", "shell", "/api/board/terminals/t-1/close", { confirm: true }],
    ["restart", "agent", "/api/board/terminals/t-1/restart", { confirm: true }],
  ]) {
    const { fetch, calls } = mockFetch({ body: { ok: true } });
    const api = P.createApi({ fetch, csrf: "t" });
    const asked = [];
    const target = view({ id: "t-1", kind, name: "n<1>" });
    const no = await P.terminalAction(api, action, target, { confirm: async (text) => { asked.push(text); return false; } });
    assert.equal(no.cancelled, true, action);
    assert.equal(calls.length, 0, `${action}: nothing is sent when the dialog is declined`);
    assert.equal(asked.length, 1);
    assert.equal(asked[0], P.confirmText(action, "n<1>"));
    const yes = await P.terminalAction(api, action, target, { confirm: async () => true });
    assert.equal(yes.ok, true, action);
    assert.deepEqual(calls.map((call) => [call.path, call.body]), [[path, body]], action);
  }
  const { fetch, calls } = mockFetch({ body: { ok: true } });
  let asked = 0;
  const started = await P.terminalAction(P.createApi({ fetch, csrf: "t" }), "start", view({ id: "t-1" }), { confirm: async () => { asked += 1; return true; } });
  assert.equal(started.ok, true);
  assert.equal(asked, 0, "no dialog for start");
  assert.equal(calls[0].path, "/api/board/terminals/t-1/start");
});

test("an action that does not fit the kind (stop/delete on a shell, close on an agent) is refused without a call", async () => {
  const { fetch, calls } = mockFetch({});
  const api = P.createApi({ fetch, csrf: "t" });
  const confirm = async () => true;
  for (const [action, kind] of [["stop", "shell"], ["delete", "shell"], ["close", "agent"], ["bogus", "agent"]]) {
    const result = await P.terminalAction(api, action, view({ kind }), { confirm });
    assert.deepEqual([result.ok, result.reason], [false, "wrong_kind"], `${action} ${kind}`);
  }
  assert.equal(calls.length, 0);
});

test("a terminal that is busy is not asked about and not called", async () => {
  const { fetch, calls } = mockFetch({});
  let asked = 0;
  const result = await P.terminalAction(P.createApi({ fetch, csrf: "t" }), "stop", view({ busy: true }), { confirm: async () => { asked += 1; return true; } });
  assert.deepEqual([result.ok, result.reason, asked, calls.length], [false, "terminal_busy", 0, 0]);
});

test("the widget's × is stop for an agent and close for a shell (WBX-D56), both behind the dialog", () => {
  assert.equal(P.closeAction(view()), "stop");
  assert.equal(P.closeAction(view({ kind: "shell" })), "close");
});

// ---- (11) widget texts ----------------------------------------------------------------------------------------
test("다른 화면 N개 연결됨 shows for N >= 1 only, with N = clients - 1", () => {
  assert.equal(P.othersText(0), "");
  assert.equal(P.othersText(1), "다른 화면 1개 연결됨");
  assert.equal(P.othersText(7), "다른 화면 7개 연결됨");
  assert.equal(P.othersText(-2), "");
  assert.equal(P.othersText(NaN), "");
  // through the state machine: clients 1 (only us) -> none; clients 3 -> 2 others; before attach -> none
  let state = initialState({ csrf: "c", terminal: "a-1" });
  state = step(state, { type: "start", size: [24, 80] }).state;
  assert.equal(P.othersText(describe(state).others), "");
  state = step(state, { type: "message", conn: 1, message: { t: "attached", v: 2, terminal: view(), size: [24, 80], clients: 1, heartbeat: 10 } }).state;
  assert.equal(P.othersText(describe(state).others), "");
  state = step(state, { type: "message", conn: 1, message: { t: "clients", count: 3 } }).state;
  assert.equal(P.othersText(describe(state).others), "다른 화면 2개 연결됨");
});

const connected = (over = {}) => ({ connection: "연결됨", action: null, canInput: true, gone: false, view: null, others: 0, ...over });

test("the widget cover: a stopped agent offers 중지됨 — 시작, exited and failed offer 재시작, running covers nothing", () => {
  assert.equal(P.overlayFor(view({ state: "running" }), connected()), null);
  assert.deepEqual(P.overlayFor(view({ state: "stopped" }), connected()), { text: "중지됨 — 시작", action: "start", label: "중지됨 — 시작" });
  assert.deepEqual(P.overlayFor(view({ state: "not_started" }), connected()), { text: "시작 전 — 시작", action: "start", label: "시작 전 — 시작" });
  assert.deepEqual(P.overlayFor(view({ state: "exited", exit_status: 0 }), connected()), { text: "종료됨 (종료 코드 0)", action: "restart", label: "재시작" });
  assert.deepEqual(P.overlayFor(view({ state: "failed", detail: "spawn_failed" }), connected()), { text: "실패: spawn_failed", action: "restart", label: "재시작" });
  assert.equal(P.overlayFor(view({ state: "isolation_failed", detail: "mcp_leak" }), connected()).action, "restart");
  assert.deepEqual(P.overlayFor(view({ state: "starting" }), connected()), { text: "시작 중", action: null, label: null });
  assert.equal(P.overlayFor(null, connected()), null);
});

test("the failure message of a terminal is shown under its state text", () => {
  const cover = P.overlayFor(view({ state: "failed", detail: "omp_unavailable", message: "OMP를 찾을 수 없습니다" }), connected());
  assert.equal(cover.text, "실패: omp_unavailable — OMP를 찾을 수 없습니다");
});

test("a connection problem comes before the process state: 4404 closed, 4429, 4401 and a network drop", () => {
  let state = initialState({ csrf: "c", terminal: "a-1" });
  state = step(state, { type: "start", size: [24, 80] }).state;
  const closedWith = (code) => describe(step(state, { type: "closed", conn: 1, code }).state);
  assert.deepEqual(P.overlayFor(view(), closedWith(4404)), { text: GONE_TEXT, action: null, label: null });
  assert.deepEqual(P.overlayFor(view(), closedWith(4429)), { text: TOO_MANY_TEXT, action: "reconnect", label: "다시 연결" });
  assert.deepEqual(P.overlayFor(view(), closedWith(4401)), { text: SESSION_ENDED_TEXT, action: "login", label: "다시 로그인" });
  const drop = P.overlayFor(view({ state: "stopped" }), closedWith(1006));
  assert.deepEqual([drop.text, drop.action, drop.label], ["연결 없음", "reconnect", "다시 연결"]);
});

// ---- connection budget: at most 4 sockets before hello, only visible widgets, hidden ones released --------------
test("planConnections starts wanted idle or released widgets, at most 4 waiting for their hello at a time", () => {
  const widgets = ["a", "b", "c", "d", "e", "f"].map((id) => ({ id, wanted: true, phase: "idle" }));
  assert.deepEqual(P.planConnections(widgets).connect, ["a", "b", "c", "d"]);
  widgets[0].phase = "connecting";
  widgets[1].phase = "attached";
  assert.deepEqual(P.planConnections(widgets).connect, ["c", "d", "e"], "one is still waiting for its hello, so three more fit");
});

test("planConnections never starts more than HELLO_MAX in total, counting those already connecting", () => {
  const widgets = Array.from({ length: 10 }, (_, i) => ({ id: `w${i}`, wanted: true, phase: i < 3 ? "connecting" : "idle" }));
  assert.deepEqual(P.planConnections(widgets).connect, ["w3"]);
  assert.equal(P.HELLO_MAX, 4);
  const full = widgets.map((widget, i) => ({ ...widget, phase: i < 4 ? "connecting" : "idle" }));
  assert.deepEqual(P.planConnections(full).connect, []);
});

test("planConnections releases a widget that is no longer wanted and never connects one that is not", () => {
  const widgets = [
    { id: "hidden-attached", wanted: false, phase: "attached" },
    { id: "hidden-connecting", wanted: false, phase: "connecting" },
    { id: "hidden-retrying", wanted: false, phase: "disconnected" },
    { id: "hidden-idle", wanted: false, phase: "idle" },
    { id: "hidden-released", wanted: false, phase: "released" },
    { id: "hidden-gone", wanted: false, phase: "gone" },
    { id: "shown", wanted: true, phase: "released" },
    { id: "shown-refused", wanted: true, phase: "refused" },
    { id: "shown-gone", wanted: true, phase: "gone" },
  ];
  const plan = P.planConnections(widgets);
  assert.deepEqual(plan.release, ["hidden-attached", "hidden-connecting", "hidden-retrying"]);
  assert.deepEqual(plan.connect, ["shown"], "refused and gone are not retried by themselves");
});

test("the open sockets never exceed the wanted widgets: after the plan runs, attached+connecting <= wanted", () => {
  const widgets = Array.from({ length: 12 }, (_, i) => ({ id: `w${i}`, wanted: i % 3 !== 0, phase: i % 2 ? "attached" : "idle" }));
  const plan = P.planConnections(widgets);
  const live = widgets.filter((widget) => ["attached", "connecting", "disconnected"].includes(widget.phase) && !plan.release.includes(widget.id)).length + plan.connect.length;
  assert.ok(live <= widgets.filter((widget) => widget.wanted).length);
  assert.ok(plan.connect.every((id) => widgets.find((widget) => widget.id === id).wanted));
});

test("the list refresh runs every 5 s", () => {
  assert.equal(P.POLL_MS, 5000);
});
