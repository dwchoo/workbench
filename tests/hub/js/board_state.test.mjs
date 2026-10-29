// WBX-D1 behaviour 23 / seam S8: one Board terminal's protocol v2 state machine (static/board-state.js), no DOM, no
// network. Expected values come from web-board-ux SPEC s4 "WebSocket v2" (hello/ping/resize/release shapes, bare
// binary frames, the backoff 1->2->4..<=15 s, 10 s ping, 20 s pong limit, 4400-4499 never reconnect by themselves,
// 4401 log in again, 4404 the widget goes, 4429 the client limit, resize only from the widget's own layout after
// 150 ms), not from the module under test.
//
// Migrated from v1: the lease, epochs, resume, encodeFrame/decodeFrame, taken_over/taken_elsewhere and 다시 가져오기
// are gone; each v1 case is kept as its v2 counterpart (noted where the expectation changed).
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import * as board from "../../../src/workbench/hub/web/static/board-state.js";

const CSRF = "tok-123";
const SIZE = [24, 80];

function boot(extra = {}) {
  let state = board.initialState({ csrf: CSRF, terminal: "s-0123456789ab", ...extra });
  const log = [];
  const send = (event) => {
    const result = board.step(state, event);
    state = result.state;
    log.push(...result.effects);
    return result.effects;
  };
  return { send, get state() { return state; }, log };
}

const effect = (effects, type, name) => effects.find((item) => item.type === type && (name === undefined || item.name === name));
const effectsOf = (effects, type) => effects.filter((item) => item.type === type);
const VIEW = { id: "s-0123456789ab", kind: "shell", name: "shell-1", state: "running", clients: 1, busy: false };
const ATTACHED = { t: "attached", v: 2, terminal: VIEW, size: [24, 80], clients: 1, heartbeat: 10 };

function attach(tab, message = ATTACHED, conn = 1) {
  if (tab.state.phase === "idle") tab.send({ type: "start", size: SIZE });
  tab.send({ type: "opened", conn });
  return tab.send({ type: "message", conn, message });
}

test("start opens connection 1 and the opened socket sends a v2 hello with this widget's size", () => {
  const tab = boot();
  assert.equal(tab.state.phase, "idle");
  const started = tab.send({ type: "start", size: SIZE });
  assert.equal(tab.state.phase, "connecting");
  assert.deepEqual(effect(started, "open"), { type: "open", conn: 1 });
  const opened = tab.send({ type: "opened", conn: 1 });
  assert.deepEqual(effect(opened, "send").message, { t: "hello", v: 2, csrf: CSRF, size: [24, 80] });
});

test("attached schedules the 10 s ping and the 20 s pong limit and keeps the view and the client count", () => {
  const tab = boot();
  const effects = attach(tab, { ...ATTACHED, clients: 3 });
  assert.equal(tab.state.phase, "attached");
  assert.deepEqual(effectsOf(effects, "set_timer"), [
    { type: "set_timer", name: "ping", ms: 10000 },
    { type: "set_timer", name: "pong", ms: 20000 },
    { type: "set_timer", name: "stable", ms: 10000 },
  ]);
  const view = board.describe(tab.state);
  assert.equal(view.connection, "연결됨");
  assert.equal(view.view.name, "shell-1");
  assert.equal(view.others, 2, "다른 화면 N개 = clients - 1");
});

test("attached resets the terminal first and draws at the terminal's size: the server replays the whole ring", () => {
  const tab = boot();
  const effects = attach(tab, { ...ATTACHED, size: [40, 120] });
  assert.equal(effects[0].type, "reset_terminal");
  assert.deepEqual(effect(effects, "server_size"), { type: "server_size", rows: 40, cols: 120 });
  const again = boot();
  attach(again);
  again.send({ type: "closed", conn: 1, code: 1006 });
  again.send({ type: "timer", name: "reconnect" });
  again.send({ type: "opened", conn: 2 });
  const reattached = again.send({ type: "message", conn: 2, message: ATTACHED });
  assert.equal(reattached[0].type, "reset_terminal");
});

test("output frames after attached are written as they are; another connection's or a not attached one's are dropped", () => {
  const tab = boot();
  tab.send({ type: "start", size: SIZE });
  tab.send({ type: "opened", conn: 1 });
  assert.deepEqual(tab.send({ type: "data", conn: 1, bytes: new Uint8Array([65]) }), [], "before attached");
  tab.send({ type: "message", conn: 1, message: ATTACHED });
  const bytes = new TextEncoder().encode("hi");
  assert.deepEqual(tab.send({ type: "data", conn: 1, bytes }), [{ type: "write", payload: bytes }]);
  assert.deepEqual(tab.send({ type: "data", conn: 7, bytes }), []);
});

test("a network drop shows 연결 없음 and reconnects with a new hello after 1 s (v1: a resume hello)", () => {
  const tab = boot();
  attach(tab);
  const dropped = tab.send({ type: "closed", conn: 1, code: 1006 });
  assert.equal(tab.state.phase, "disconnected");
  assert.equal(board.describe(tab.state).connection, "연결 없음");
  assert.equal(board.describe(tab.state).action, "reconnect");
  assert.deepEqual(effectsOf(dropped, "clear_timer").map((item) => item.name).sort(), ["ping", "pong", "stable"]);
  assert.deepEqual(effect(dropped, "set_timer"), { type: "set_timer", name: "reconnect", ms: 1000 });
  const retry = tab.send({ type: "timer", name: "reconnect" });
  assert.deepEqual(effect(retry, "open"), { type: "open", conn: 2 });
  assert.deepEqual(effect(tab.send({ type: "opened", conn: 2 }), "send").message,
    { t: "hello", v: 2, csrf: CSRF, size: [24, 80] });
});

test("failed reconnects back off 1, 2, 4, 8, 15, 15 s; only an attach that held for 10 s resets the delay", () => {
  const tab = boot();
  attach(tab);
  const delays = [];
  let conn = 1;
  for (let attempt = 0; attempt < 6; attempt += 1) {
    const dropped = tab.send({ type: "closed", conn, code: 1006 });
    delays.push(effect(dropped, "set_timer").ms);
    tab.send({ type: "timer", name: "reconnect" });
    conn += 1;
  }
  assert.deepEqual(delays, [1000, 2000, 4000, 8000, 15000, 15000]);
  tab.send({ type: "opened", conn });
  tab.send({ type: "message", conn, message: ATTACHED });
  const flapped = tab.send({ type: "closed", conn, code: 1006 });
  assert.equal(effect(flapped, "set_timer").ms, 15000);
  assert.deepEqual(effect(flapped, "clear_timer", "stable"), { type: "clear_timer", name: "stable" });
  tab.send({ type: "timer", name: "reconnect" });
  conn += 1;
  tab.send({ type: "opened", conn });
  tab.send({ type: "message", conn, message: ATTACHED });
  tab.send({ type: "timer", name: "stable" });
  assert.equal(effect(tab.send({ type: "closed", conn, code: 1006 }), "set_timer").ms, 1000);
});

test("the stable timer does nothing once the connection is no longer attached", () => {
  const tab = boot();
  attach(tab);
  tab.send({ type: "closed", conn: 1, code: 1006 });
  tab.send({ type: "timer", name: "stable" });
  assert.equal(tab.state.attempt, 1);
});

test("heartbeat timeout, send queue overflow, a stopping Hub, a too big message and a drop are reconnected; old sockets are ignored", () => {
  for (const code of [4002, 4003, 1001, 1006, 1009]) {
    const tab = boot();
    attach(tab);
    const dropped = tab.send({ type: "closed", conn: 1, code });
    assert.equal(tab.state.phase, "disconnected", String(code));
    assert.equal(effect(dropped, "set_timer").name, "reconnect", String(code));
  }
  const tab = boot();
  attach(tab);
  tab.send({ type: "closed", conn: 1, code: 1006 });
  tab.send({ type: "timer", name: "reconnect" });
  assert.deepEqual(tab.send({ type: "closed", conn: 1, code: 1006 }), []);
  assert.equal(tab.state.phase, "connecting");
  assert.deepEqual(tab.send({ type: "message", conn: 1, message: ATTACHED }), []);
});

test("every close code 4400-4499 stops without an automatic reconnect (v1: 4001 taken over is gone)", () => {
  for (const code of [4400, 4401, 4403, 4404, 4429, 4499]) {
    const tab = boot();
    tab.send({ type: "start", size: SIZE });
    tab.send({ type: "opened", conn: 1 });
    const closed = tab.send({ type: "closed", conn: 1, code });
    assert.notEqual(tab.state.phase, "disconnected", String(code));
    assert.equal(effect(closed, "set_timer"), undefined, String(code));
    assert.deepEqual(tab.send({ type: "timer", name: "reconnect" }), [], String(code));
  }
});

test("4429 (the terminal has 8 clients) shows the limit, offers 다시 연결 and never retries by itself", () => {
  const tab = boot();
  tab.send({ type: "start", size: SIZE });
  tab.send({ type: "opened", conn: 1 });
  const refused = tab.send({ type: "message", conn: 1, message: { t: "refused", reason: "too_many_clients", limit: 8 } });
  assert.deepEqual(effect(refused, "notice"), { type: "notice", reason: "too_many_clients" });
  tab.send({ type: "closed", conn: 1, code: 4429 });
  const view = board.describe(tab.state);
  assert.equal(view.connection, "연결 수 한도(8개)를 넘었습니다");
  assert.equal(view.action, "reconnect");
  assert.deepEqual(effect(tab.send({ type: "reconnect" }), "open"), { type: "open", conn: 2 });
});

test("gone and 4404: the widget goes, no timer and no reconnect (v1 had no closed terminals)", () => {
  const tab = boot();
  attach(tab);
  const gone = tab.send({ type: "message", conn: 1, message: { t: "gone" } });
  assert.equal(tab.state.phase, "gone");
  assert.ok(effect(gone, "gone"));
  assert.deepEqual(effectsOf(gone, "clear_timer").map((item) => item.name).sort(), ["ping", "pong", "stable"]);
  assert.deepEqual(tab.send({ type: "closed", conn: 1, code: 4404 }), []);
  assert.equal(board.describe(tab.state).gone, true);
  assert.deepEqual(tab.send({ type: "reconnect" }), []);
  const late = boot();  // a 4404 without the gone message (the terminal went before the hello)
  late.send({ type: "start", size: SIZE });
  late.send({ type: "opened", conn: 1 });
  assert.ok(effect(late.send({ type: "closed", conn: 1, code: 4404 }), "gone"));
  assert.equal(late.state.phase, "gone");
});

test("the ping timer sends ping with a counter (no epoch) and schedules the next one at the heartbeat", () => {
  const tab = boot();
  attach(tab);
  const first = tab.send({ type: "timer", name: "ping" });
  assert.deepEqual(effect(first, "send").message, { t: "ping", n: 1 });
  assert.deepEqual(effect(first, "set_timer"), { type: "set_timer", name: "ping", ms: 10000 });
  assert.deepEqual(effect(tab.send({ type: "timer", name: "ping" }), "send").message, { t: "ping", n: 2 });
});

test("a server heartbeat other than 10 s sets the ping interval", () => {
  const tab = boot();
  const effects = attach(tab, { ...ATTACHED, heartbeat: 2 });
  assert.deepEqual(effects.find((item) => item.name === "ping"), { type: "set_timer", name: "ping", ms: 2000 });
});

test("a pong restarts the 20 s limit; no pong for 20 s is 연결 없음, closes the socket (4000) and reconnects", () => {
  const tab = boot();
  attach(tab);
  const pong = tab.send({ type: "message", conn: 1, message: { t: "pong", n: 1 } });
  assert.deepEqual(effect(pong, "set_timer"), { type: "set_timer", name: "pong", ms: 20000 });
  const lost = tab.send({ type: "timer", name: "pong" });
  assert.equal(tab.state.phase, "disconnected");
  assert.equal(board.describe(tab.state).connection, "연결 없음");
  assert.deepEqual(effect(lost, "close"), { type: "close", conn: 1, code: 4000 });
  assert.deepEqual(effect(lost, "set_timer"), { type: "set_timer", name: "reconnect", ms: 1000 });
  assert.deepEqual(tab.send({ type: "closed", conn: 1, code: 1006 }), []);
  tab.send({ type: "timer", name: "reconnect" });
  assert.equal(effect(tab.send({ type: "opened", conn: 2 }), "send").message.t, "hello");
});

test("timers that fire while not attached do nothing", () => {
  const tab = boot();
  tab.send({ type: "start", size: SIZE });
  assert.deepEqual(tab.send({ type: "timer", name: "ping" }), []);
  assert.deepEqual(tab.send({ type: "timer", name: "pong" }), []);
  assert.equal(tab.state.phase, "connecting");
});

test("pagehide sends release (no epoch) and does not reconnect; pageshow of a restored page connects again", () => {
  const tab = boot();
  attach(tab);
  const hidden = tab.send({ type: "pagehide" });
  assert.deepEqual(effect(hidden, "send").message, { t: "release" });
  assert.equal(tab.state.phase, "released");
  assert.deepEqual(effectsOf(hidden, "clear_timer").map((item) => item.name).sort(), ["ping", "pong", "stable"]);
  assert.deepEqual(tab.send({ type: "closed", conn: 1, code: 1000 }), []);
  assert.deepEqual(effect(tab.send({ type: "pageshow", persisted: true }), "open"), { type: "open", conn: 2 });
  assert.equal(effect(tab.send({ type: "opened", conn: 2 }), "send").message.t, "hello");
});

test("release (a hidden widget) is the same as pagehide; while disconnected it cancels the pending retry", () => {
  const tab = boot();
  attach(tab);
  assert.deepEqual(effect(tab.send({ type: "release" }), "send").message, { t: "release" });
  const other = boot();
  attach(other);
  other.send({ type: "closed", conn: 1, code: 1006 });
  assert.deepEqual(other.send({ type: "release" }), [{ type: "clear_timer", name: "reconnect" }]);
  assert.equal(other.state.phase, "released");
  assert.deepEqual(other.send({ type: "timer", name: "reconnect" }), []);
});

test("pagehide while connecting closes that socket (nothing attached yet) and a restored page connects again", () => {
  const tab = boot();
  tab.send({ type: "start", size: SIZE });
  const hidden = tab.send({ type: "pagehide" });
  assert.equal(effect(hidden, "send"), undefined);
  assert.deepEqual(effect(hidden, "close"), { type: "close", conn: 1, code: 1000 });
  assert.equal(tab.state.phase, "released");
  assert.deepEqual(tab.send({ type: "closed", conn: 1, code: 1006 }), [], "the late close does not schedule a retry");
  assert.deepEqual(effect(tab.send({ type: "pageshow", persisted: true }), "open"), { type: "open", conn: 2 });
});

test("pagehide while idle, refused or gone sends nothing; a plain pageshow does nothing", () => {
  const tab = boot();
  assert.deepEqual(tab.send({ type: "pagehide" }), []);
  attach(tab);
  tab.send({ type: "closed", conn: 1, code: 4403 });
  assert.deepEqual(tab.send({ type: "pagehide" }), []);
  assert.deepEqual(tab.send({ type: "pageshow", persisted: false }), []);
});

test("input is sent while attached only, as bare bytes (v1: a channel and epoch header); any client may type", () => {
  const tab = boot();
  const input = () => tab.send({ type: "input", bytes: new Uint8Array([0x61]) });
  assert.deepEqual(input(), []);
  tab.send({ type: "start", size: SIZE });
  assert.deepEqual(input(), []);
  tab.send({ type: "opened", conn: 1 });
  assert.deepEqual(input(), [], "before attached");
  tab.send({ type: "message", conn: 1, message: { ...ATTACHED, clients: 5 } });
  assert.deepEqual(Array.from(effect(input(), "send_binary").bytes), [0x61], "one of five clients types too");
  assert.equal(board.describe(tab.state).canInput, true);
  const other = boot();
  attach(other);
  other.send({ type: "closed", conn: 1, code: 1006 });
  assert.deepEqual(other.send({ type: "input", bytes: new Uint8Array([1]) }), [], "after a drop");
  other.send({ type: "timer", name: "reconnect" });
  other.send({ type: "opened", conn: 2 });
  assert.deepEqual(other.send({ type: "input", bytes: new Uint8Array([1]) }), [], "reconnecting");
  const released = boot();
  attach(released);
  released.send({ type: "pagehide" });
  assert.deepEqual(released.send({ type: "input", bytes: new Uint8Array([1]) }), [], "released");
});

test("a long paste is split into frames of at most 64 KiB on code point boundaries; over 4 MiB is refused whole", () => {
  const tab = boot();
  attach(tab);
  const text = "가".repeat(30000); // 90000 bytes
  const sent = effectsOf(tab.send({ type: "input", bytes: new TextEncoder().encode(text) }), "send_binary");
  assert.equal(sent.length, 2);
  assert.ok(sent.every((item) => item.bytes.length <= 64 * 1024));
  assert.equal(sent.map((item) => new TextDecoder("utf-8", { fatal: true }).decode(item.bytes)).join(""), text);
  const huge = tab.send({ type: "input", bytes: new Uint8Array((4 << 20) + 1) });
  assert.deepEqual(huge, [{ type: "notice", reason: "too_large" }]);
});

test("a layout resize is remembered for the next hello and sent after 150 ms quiet only while attached; clamped to 1..500", () => {
  const tab = boot();
  tab.send({ type: "start", size: SIZE });
  assert.deepEqual(tab.send({ type: "resize", rows: 40, cols: 120 }),
    [{ type: "set_timer", name: "resize", ms: 150 }]);
  assert.deepEqual(tab.send({ type: "timer", name: "resize" }), [], "not attached: nothing to send");
  assert.deepEqual(effect(tab.send({ type: "opened", conn: 1 }), "send").message.size, [40, 120]);
  tab.send({ type: "message", conn: 1, message: ATTACHED });
  tab.send({ type: "resize", rows: 30, cols: 90 });
  tab.send({ type: "resize", rows: 9999, cols: 0 }); // the debounce: only the last one goes
  assert.deepEqual(effect(tab.send({ type: "timer", name: "resize" }), "send").message, { t: "resize", rows: 500, cols: 1 });
  assert.deepEqual(tab.send({ type: "timer", name: "resize" }), [], "sent once");
  tab.send({ type: "closed", conn: 1, code: 1006 });
  tab.send({ type: "timer", name: "reconnect" });
  assert.deepEqual(effect(tab.send({ type: "opened", conn: 2 }), "send").message.size, [500, 1]);
});

test("a server size message only redraws: it never sends a resize back (no ping-pong between two screens)", () => {
  const tab = boot();
  attach(tab);
  for (const [rows, cols] of [[40, 120], [24, 80], [40, 120]]) {
    const effects = tab.send({ type: "message", conn: 1, message: { t: "size", rows, cols } });
    assert.deepEqual(effects, [{ type: "server_size", rows, cols }]);
  }
  assert.deepEqual(tab.send({ type: "timer", name: "resize" }), [], "no resize is pending");
  assert.equal(tab.log.filter((item) => item.type === "send" && item.message.t === "resize").length, 0);
});

test("clients and state messages update the count and the view; errors surface; unknown messages are ignored", () => {
  const tab = boot();
  attach(tab);
  tab.send({ type: "message", conn: 1, message: { t: "clients", count: 4 } });
  assert.equal(board.describe(tab.state).others, 3);
  const changed = tab.send({ type: "message", conn: 1, message: { t: "state", terminal: { ...VIEW, name: "빌드", state: "exited" } } });
  assert.equal(board.describe(tab.state).view.name, "빌드");
  assert.equal(tab.state.phase, "attached");
  assert.equal(effectsOf(changed, "set_timer").length, 0);
  const failed = tab.send({ type: "message", conn: 1, message: { t: "error", reason: "input_refused" } });
  assert.deepEqual(effect(failed, "notice"), { type: "notice", reason: "input_refused" });
  assert.deepEqual(tab.send({ type: "message", conn: 1, message: { t: "mystery" } }), []);
});

test("channelLabel names running, stopped, exited, failed and isolation failures with their reason", () => {
  assert.equal(board.channelLabel({ state: "running" }), "실행 중");
  assert.equal(board.channelLabel({ state: "starting" }), "시작 중");
  assert.equal(board.channelLabel({ state: "stopped" }), "중지됨");
  assert.equal(board.channelLabel({ state: "exited", exit_status: 3 }), "종료됨 (종료 코드 3)");
  assert.equal(board.channelLabel({ state: "failed", detail: "omp_missing" }), "실패: omp_missing");
  assert.equal(board.channelLabel({ state: "isolation_failed", detail: "mcp_leak" }), "격리 실패: mcp_leak");
  assert.equal(board.channelLabel({ state: "not_started" }), "시작 전");
  assert.equal(board.channelLabel(null), "알 수 없음");
  assert.equal(board.channelLabel({ state: "weird" }), "weird");
});

test("a manual reconnect from 연결 없음 connects at once and cancels the pending backoff timer", () => {
  const tab = boot();
  attach(tab);
  tab.send({ type: "closed", conn: 1, code: 1006 });
  const effects = tab.send({ type: "reconnect" });
  assert.deepEqual(effect(effects, "clear_timer"), { type: "clear_timer", name: "reconnect" });
  assert.deepEqual(effect(effects, "open"), { type: "open", conn: 2 });
  assert.deepEqual(tab.send({ type: "reconnect" }), [], "only from disconnected or refused");
});

test("4401 (the login session ended) shows 연결 없음 — 다시 로그인 with a login action and never reconnects", () => {
  const tab = boot();
  attach(tab);
  const closed = tab.send({ type: "closed", conn: 1, code: 4401 });
  assert.equal(effect(closed, "set_timer"), undefined);
  const view = board.describe(tab.state);
  assert.equal(view.connection, "연결 없음 — 다시 로그인");
  assert.equal(view.action, "login");
  assert.equal(view.canInput, false);
  assert.deepEqual(tab.send({ type: "reconnect" }), [], "a reconnect cannot help without a new login");
  assert.deepEqual(tab.send({ type: "timer", name: "reconnect" }), []);
  const other = boot();  // other 44xx refusals: the plain label and the manual reconnect
  attach(other);
  other.send({ type: "closed", conn: 1, code: 4403 });
  assert.equal(board.describe(other.state).connection, "연결 없음");
  assert.equal(board.describe(other.state).action, "reconnect");
});

test("upgrade refused with 429/503 (the browser reports it as a close 1006 before open) backs off and never hammers", () => {
  const tab = boot();
  tab.send({ type: "start", size: SIZE });
  const delays = [];
  for (let conn = 1; conn <= 6; conn += 1) {
    const refused = tab.send({ type: "closed", conn, code: 1006 });
    assert.equal(tab.state.phase, "disconnected");
    delays.push(effect(refused, "set_timer").ms);
    tab.send({ type: "timer", name: "reconnect" });
  }
  assert.deepEqual(delays, [1000, 2000, 4000, 8000, 15000, 15000]);
  tab.send({ type: "opened", conn: 7 });
  tab.send({ type: "message", conn: 7, message: ATTACHED });
  assert.equal(effect(tab.send({ type: "closed", conn: 7, code: 1006 }), "set_timer").ms, 15000);
});

test("v1 frame helpers are gone: frames carry no channel or epoch", () => {
  assert.equal(board.encodeFrame, undefined);
  assert.equal(board.decodeFrame, undefined);
  assert.equal(board.PROTOCOL_VERSION, 2);
});

// The same table runs against Python pages/board.py channel_label (test_web_pages_board.py): the server-rendered
// first paint and the live updates must read alike.
test("channelLabel matches the shared fixture table (also run against the Python channel_label)", () => {
  const table = JSON.parse(readFileSync(new URL("./channel_labels.json", import.meta.url), "utf8"));
  assert.ok(table.length >= 10);
  for (const { info, label } of table) assert.equal(board.channelLabel(info), label, JSON.stringify(info));
});
