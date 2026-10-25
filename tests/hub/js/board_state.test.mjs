// TI-18 / seam T10: the Board page's protocol state machine (static/board-state.js), no DOM, no network.
// Expected values come from SPEC "WebSocket terminal 프로토콜" (hello/ping/release shapes, frame layout, the
// backoff 1->2->4..<=15 s, 10 s ping, 20 s pong limit) and P2-AC-35, not from the module under test.
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import * as board from "../../../src/workbench/hub/web/static/board-state.js";

const CSRF = "tok-123";
const SIZES = { agent: [30, 100], shell: [24, 80] };

function boot(extra = {}) {
  let state = board.initialState({ csrf: CSRF, ...extra });
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

test("start opens connection 1 and the opened socket sends a hello without resume", () => {
  const tab = boot();
  assert.equal(tab.state.phase, "idle");
  const started = tab.send({ type: "start", sizes: SIZES });
  assert.equal(tab.state.phase, "connecting");
  assert.deepEqual(effect(started, "open"), { type: "open", conn: 1 });
  const opened = tab.send({ type: "opened", conn: 1 });
  assert.deepEqual(effect(opened, "send").message, {
    t: "hello", v: 1, csrf: CSRF, resume: null, sizes: SIZES,
  });
});

const ATTACHED = {
  t: "attached", v: 1, run: "run-a", epoch: 1, heartbeat: 10,
  channels: { agent: { state: "running" }, shell: { state: "running" } },
};

function attach(tab, message = ATTACHED, conn = 1) {
  tab.send({ type: "start", sizes: SIZES });
  tab.send({ type: "opened", conn });
  return tab.send({ type: "message", conn, message });
}

test("attached makes the tab the holder, schedules the 10 s ping and the 20 s pong limit", () => {
  const tab = boot();
  const effects = attach(tab);
  assert.equal(tab.state.phase, "attached");
  assert.equal(tab.state.epoch, 1);
  assert.deepEqual(tab.state.resume, { run: "run-a", epoch: 1 });
  assert.deepEqual(effectsOf(effects, "set_timer"), [
    { type: "set_timer", name: "ping", ms: 10000 },
    { type: "set_timer", name: "pong", ms: 20000 },
    { type: "set_timer", name: "stable", ms: 10000 },
  ]);
  assert.equal(board.describe(tab.state).channels.agent.state, "running");
  assert.equal(board.describe(tab.state).connection, "연결됨");
});

test("attached resets the terminals first: the server replays the whole ring after every attach", () => {
  const tab = boot();
  const effects = attach(tab);
  assert.equal(effects[0].type, "reset_terminals");
  const again = boot();
  attach(again);
  again.send({ type: "closed", conn: 1, code: 1006 });
  again.send({ type: "timer", name: "reconnect" });
  again.send({ type: "opened", conn: 2 });
  const resumed = again.send({ type: "message", conn: 2, message: { ...ATTACHED, epoch: 1 } });
  assert.equal(resumed[0].type, "reset_terminals");
});

test("output frames after attached are written to their channel; a stale epoch or another connection is dropped", () => {
  const tab = boot();
  attach(tab);
  const frame = (channel, epoch, text) => board.encodeFrame(channel, epoch, new TextEncoder().encode(text));
  const wrote = tab.send({ type: "data", conn: 1, bytes: frame("shell", 1, "hi") });
  assert.equal(effect(wrote, "write").channel, "shell");
  assert.equal(new TextDecoder().decode(effect(wrote, "write").payload), "hi");
  assert.deepEqual(tab.send({ type: "data", conn: 1, bytes: frame("agent", 0, "old") }), []);
  assert.deepEqual(tab.send({ type: "data", conn: 7, bytes: frame("agent", 1, "other") }), []);
  assert.deepEqual(tab.send({ type: "data", conn: 1, bytes: new Uint8Array([9, 0, 0, 0, 1, 65]) }), []);
});

test("a network drop shows 연결 없음 and reconnects with a resume hello after 1 s", () => {
  const tab = boot();
  attach(tab);
  const dropped = tab.send({ type: "closed", conn: 1, code: 1006 });
  assert.equal(tab.state.phase, "disconnected");
  assert.equal(board.describe(tab.state).connection, "연결 없음");
  assert.deepEqual(effectsOf(dropped, "clear_timer").map((item) => item.name).sort(), ["ping", "pong", "stable"]);
  assert.deepEqual(effect(dropped, "set_timer"), { type: "set_timer", name: "reconnect", ms: 1000 });
  const retry = tab.send({ type: "timer", name: "reconnect" });
  assert.deepEqual(effect(retry, "open"), { type: "open", conn: 2 });
  const hello = effect(tab.send({ type: "opened", conn: 2 }), "send").message;
  assert.deepEqual(hello.resume, { run: "run-a", epoch: 1 });
  assert.equal(hello.csrf, CSRF);
  assert.deepEqual(hello.sizes, SIZES);
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
  tab.send({ type: "message", conn, message: { ...ATTACHED, epoch: 1 } });
  // attached, then dropped at once (a flapping server): the delay keeps growing
  const flapped = tab.send({ type: "closed", conn, code: 1006 });
  assert.equal(effect(flapped, "set_timer").ms, 15000);
  assert.deepEqual(effect(flapped, "clear_timer", "stable"), { type: "clear_timer", name: "stable" });
  tab.send({ type: "timer", name: "reconnect" });
  conn += 1;
  tab.send({ type: "opened", conn });
  tab.send({ type: "message", conn, message: { ...ATTACHED, epoch: 1 } });
  tab.send({ type: "timer", name: "stable" }); // 10 s of a held attach
  const held = tab.send({ type: "closed", conn, code: 1006 });
  assert.equal(effect(held, "set_timer").ms, 1000);
});

test("the stable timer does nothing once the tab is no longer attached", () => {
  const tab = boot();
  attach(tab);
  tab.send({ type: "closed", conn: 1, code: 1006 });
  tab.send({ type: "timer", name: "stable" });
  assert.equal(tab.state.attempt, 1);
});

test("heartbeat timeout, send queue overflow and a stopping Hub are reconnected; events of an old socket are ignored", () => {
  for (const code of [4002, 4003, 1001, 1006]) {
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

test("every close code 4400-4499 (bad hello, csrf, any future client error) is 연결 없음 without an automatic reconnect", () => {
  for (const code of [4400, 4401, 4402, 4403, 4499]) {
    const tab = boot();
    tab.send({ type: "start", sizes: SIZES });
    tab.send({ type: "opened", conn: 1 });
    const closed = tab.send({ type: "closed", conn: 1, code });
    assert.equal(tab.state.phase, "disconnected", String(code));
    assert.equal(effect(closed, "set_timer"), undefined, String(code));
  }
});

test("taken_over and taken_elsewhere stop the tab: banner, no timers, no automatic reconnect even after the close", () => {
  for (const kind of ["taken_over", "taken_elsewhere"]) {
    const tab = boot();
    attach(tab);
    const taken = tab.send({ type: "message", conn: 1, message: { t: kind, epoch: 2 } });
    assert.equal(tab.state.phase, "taken_elsewhere", kind);
    assert.deepEqual(effectsOf(taken, "clear_timer").map((item) => item.name).sort(), ["ping", "pong", "stable"], kind);
    const view = board.describe(tab.state);
    assert.equal(view.connection, "다른 곳에서 연결됨", kind);
    assert.equal(view.banner, true, kind);
    assert.equal(view.bannerText, "다른 곳에서 연결됨 — 다시 가져오기", kind);
    assert.equal(view.action, "retake", kind);
    const closed = tab.send({ type: "closed", conn: 1, code: 4001 });
    assert.equal(effect(closed, "set_timer"), undefined, kind);
    assert.equal(effect(closed, "open"), undefined, kind);
    assert.equal(tab.state.phase, "taken_elsewhere", kind);
    assert.deepEqual(tab.send({ type: "timer", name: "reconnect" }), [], kind);
  }
});

test("a resume refused with taken_elsewhere right after reconnect shows the banner and stays stopped", () => {
  const tab = boot();
  attach(tab);
  tab.send({ type: "closed", conn: 1, code: 1006 });
  tab.send({ type: "timer", name: "reconnect" });
  tab.send({ type: "opened", conn: 2 });
  tab.send({ type: "message", conn: 2, message: { t: "taken_elsewhere", epoch: 3 } });
  assert.equal(tab.state.phase, "taken_elsewhere");
  assert.equal(board.describe(tab.state).banner, true);
  assert.equal(effect(tab.send({ type: "closed", conn: 2, code: 4001 }), "set_timer"), undefined);
});

test("다시 가져오기 reconnects with resume null; the new epoch is the only one input uses", () => {
  const tab = boot();
  attach(tab);
  tab.send({ type: "message", conn: 1, message: { t: "taken_over", epoch: 2 } });
  tab.send({ type: "closed", conn: 1, code: 4001 });
  const retake = tab.send({ type: "retake" });
  assert.deepEqual(effect(retake, "open"), { type: "open", conn: 2 });
  assert.equal(tab.state.phase, "connecting");
  assert.equal(board.describe(tab.state).banner, false);
  const hello = effect(tab.send({ type: "opened", conn: 2 }), "send").message;
  assert.equal(hello.resume, null);
  tab.send({ type: "message", conn: 2, message: { ...ATTACHED, run: "run-a", epoch: 3 } });
  const sent = effect(tab.send({ type: "input", channel: "agent", bytes: new Uint8Array([0x68]) }), "send_binary");
  assert.deepEqual(Array.from(sent.bytes), [0, 0, 0, 0, 3, 0x68]);
});

test("the ping timer sends ping with the epoch and a counter and schedules the next one at the heartbeat", () => {
  const tab = boot();
  attach(tab, { ...ATTACHED, epoch: 4, heartbeat: 10 });
  const first = tab.send({ type: "timer", name: "ping" });
  assert.deepEqual(effect(first, "send").message, { t: "ping", epoch: 4, n: 1 });
  assert.deepEqual(effect(first, "set_timer"), { type: "set_timer", name: "ping", ms: 10000 });
  const second = tab.send({ type: "timer", name: "ping" });
  assert.deepEqual(effect(second, "send").message, { t: "ping", epoch: 4, n: 2 });
});

test("a server heartbeat other than 10 s sets the ping interval", () => {
  const tab = boot();
  const effects = attach(tab, { ...ATTACHED, heartbeat: 2 });
  assert.deepEqual(effects.find((item) => item.name === "ping"), { type: "set_timer", name: "ping", ms: 2000 });
});

test("a pong restarts the 20 s limit; no pong for 20 s is 연결 없음, closes the socket and reconnects with resume", () => {
  const tab = boot();
  attach(tab);
  const pong = tab.send({ type: "message", conn: 1, message: { t: "pong", n: 1 } });
  assert.deepEqual(effect(pong, "set_timer"), { type: "set_timer", name: "pong", ms: 20000 });
  const lost = tab.send({ type: "timer", name: "pong" });
  assert.equal(tab.state.phase, "disconnected");
  assert.equal(board.describe(tab.state).connection, "연결 없음");
  assert.deepEqual(effect(lost, "close"), { type: "close", conn: 1, code: 4000 });
  assert.deepEqual(effect(lost, "set_timer"), { type: "set_timer", name: "reconnect", ms: 1000 });
  assert.deepEqual(effectsOf(lost, "clear_timer").map((item) => item.name).sort(), ["ping", "pong", "stable"]);
  assert.deepEqual(tab.send({ type: "closed", conn: 1, code: 1006 }), []);
  tab.send({ type: "timer", name: "reconnect" });
  assert.deepEqual(effect(tab.send({ type: "opened", conn: 2 }), "send").message.resume, { run: "run-a", epoch: 1 });
});

test("timers that fire while not attached do nothing", () => {
  const tab = boot();
  tab.send({ type: "start", sizes: SIZES });
  assert.deepEqual(tab.send({ type: "timer", name: "ping" }), []);
  assert.deepEqual(tab.send({ type: "timer", name: "pong" }), []);
  assert.equal(tab.state.phase, "connecting");
});

test("pagehide sends release with the epoch and the tab does not reconnect; pageshow of a restored page resumes", () => {
  const tab = boot();
  attach(tab, { ...ATTACHED, epoch: 6 });
  const hidden = tab.send({ type: "pagehide" });
  assert.deepEqual(effect(hidden, "send").message, { t: "release", epoch: 6 });
  assert.equal(tab.state.phase, "released");
  assert.deepEqual(effectsOf(hidden, "clear_timer").map((item) => item.name).sort(), ["ping", "pong", "stable"]);
  assert.deepEqual(tab.send({ type: "closed", conn: 1, code: 1000 }), []);
  assert.equal(tab.state.phase, "released");
  const shown = tab.send({ type: "pageshow", persisted: true });
  assert.deepEqual(effect(shown, "open"), { type: "open", conn: 2 });
  assert.deepEqual(effect(tab.send({ type: "opened", conn: 2 }), "send").message.resume, { run: "run-a", epoch: 6 });
});

test("pagehide while connecting closes that socket (no release: nothing was held) and a restored page resumes", () => {
  const tab = boot();
  tab.send({ type: "start", sizes: SIZES });
  const hidden = tab.send({ type: "pagehide" });
  assert.equal(effect(hidden, "send"), undefined);
  assert.deepEqual(effect(hidden, "close"), { type: "close", conn: 1, code: 1000 });
  assert.equal(tab.state.phase, "released");
  assert.deepEqual(tab.send({ type: "closed", conn: 1, code: 1006 }), [], "the late close does not schedule a retry");
  assert.deepEqual(effect(tab.send({ type: "pageshow", persisted: true }), "open"), { type: "open", conn: 2 });
});

test("pagehide while idle or taken elsewhere sends nothing; a plain pageshow does nothing", () => {
  const tab = boot();
  assert.deepEqual(tab.send({ type: "pagehide" }), []);
  attach(tab);
  tab.send({ type: "message", conn: 1, message: { t: "taken_over", epoch: 2 } });
  assert.deepEqual(tab.send({ type: "pagehide" }), []);
  assert.deepEqual(tab.send({ type: "pageshow", persisted: false }), []);
});

test("input is sent only by the holder: never while connecting, disconnected, taken or released", () => {
  const tab = boot();
  const input = () => tab.send({ type: "input", channel: "shell", bytes: new Uint8Array([0x61]) });
  assert.deepEqual(input(), []);
  tab.send({ type: "start", sizes: SIZES });
  assert.deepEqual(input(), []);
  tab.send({ type: "opened", conn: 1 });
  assert.deepEqual(input(), [], "before attached");
  tab.send({ type: "message", conn: 1, message: { ...ATTACHED, epoch: 9 } });
  assert.deepEqual(Array.from(effect(input(), "send_binary").bytes), [1, 0, 0, 0, 9, 0x61]);
  assert.equal(board.describe(tab.state).canInput, true);
  tab.send({ type: "message", conn: 1, message: { t: "taken_over", epoch: 10 } });
  assert.deepEqual(input(), [], "after taken_over");
  assert.equal(board.describe(tab.state).canInput, false);
  const other = boot();
  attach(other);
  other.send({ type: "closed", conn: 1, code: 1006 });
  assert.deepEqual(other.send({ type: "input", channel: "agent", bytes: new Uint8Array([1]) }), [], "after a drop");
  other.send({ type: "timer", name: "reconnect" });
  other.send({ type: "opened", conn: 2 });
  assert.deepEqual(other.send({ type: "input", channel: "agent", bytes: new Uint8Array([1]) }), [], "resuming");
  const released = boot();
  attach(released);
  released.send({ type: "pagehide" });
  assert.deepEqual(released.send({ type: "input", channel: "agent", bytes: new Uint8Array([1]) }), [], "released");
});

test("resize is remembered for the next hello and sent with the epoch only while attached; sizes are clamped to 1..500", () => {
  const tab = boot();
  tab.send({ type: "start", sizes: SIZES });
  assert.deepEqual(tab.send({ type: "resize", channel: "agent", rows: 40, cols: 120 }), []);
  assert.deepEqual(effect(tab.send({ type: "opened", conn: 1 }), "send").message.sizes.agent, [40, 120]);
  tab.send({ type: "message", conn: 1, message: { ...ATTACHED, epoch: 2 } });
  const resized = tab.send({ type: "resize", channel: "shell", rows: 9999, cols: 0 });
  assert.deepEqual(effect(resized, "send").message, { t: "resize", epoch: 2, channel: "shell", rows: 500, cols: 1 });
  tab.send({ type: "closed", conn: 1, code: 1006 });
  tab.send({ type: "timer", name: "reconnect" });
  assert.deepEqual(effect(tab.send({ type: "opened", conn: 2 }), "send").message.sizes.shell, [500, 1]);
});

test("state and error messages update the channels and surface the reason; unknown messages are ignored", () => {
  const tab = boot();
  attach(tab);
  const changed = tab.send({
    type: "message", conn: 1,
    message: { t: "state", channels: { agent: { state: "isolation_failed", detail: "mcp_leak" }, shell: { state: "running" } } },
  });
  assert.equal(board.describe(tab.state).channels.agent.state, "isolation_failed");
  assert.equal(tab.state.phase, "attached");
  assert.equal(effectsOf(changed, "set_timer").length, 0);
  const failed = tab.send({ type: "message", conn: 1, message: { t: "error", reason: "input_refused", channel: "agent" } });
  assert.deepEqual(effect(failed, "notice"), { type: "notice", reason: "input_refused", channel: "agent" });
  assert.deepEqual(tab.send({ type: "message", conn: 1, message: { t: "mystery" } }), []);
});

test("channelLabel names running, exited, failed and isolation failures with their reason", () => {
  assert.equal(board.channelLabel({ state: "running" }), "실행 중");
  assert.equal(board.channelLabel({ state: "starting" }), "시작 중");
  assert.equal(board.channelLabel({ state: "exited", exit_status: 3 }), "종료됨 (종료 코드 3)");
  assert.equal(board.channelLabel({ state: "failed", detail: "omp_missing" }), "실패: omp_missing");
  assert.equal(board.channelLabel({ state: "isolation_failed", detail: "mcp_leak" }), "격리 실패: mcp_leak");
  assert.equal(board.channelLabel({ state: "not_started" }), "시작 전");
  assert.equal(board.channelLabel(null), "알 수 없음");
  assert.equal(board.channelLabel({ state: "weird" }), "weird");
});

test("a manual reconnect from 연결 없음 resumes at once and cancels the pending backoff timer", () => {
  const tab = boot();
  attach(tab);
  tab.send({ type: "closed", conn: 1, code: 4403 });
  const effects = tab.send({ type: "reconnect" });
  assert.deepEqual(effect(effects, "clear_timer"), { type: "clear_timer", name: "reconnect" });
  assert.deepEqual(effect(effects, "open"), { type: "open", conn: 2 });
  assert.deepEqual(effect(tab.send({ type: "opened", conn: 2 }), "send").message.resume, { run: "run-a", epoch: 1 });
  assert.deepEqual(tab.send({ type: "reconnect" }), [], "only from disconnected");
});

test("4401 (the login session ended) shows 연결 없음 — 다시 로그인 with a login action and never reconnects", () => {
  const tab = boot();
  attach(tab);
  const closed = tab.send({ type: "closed", conn: 1, code: 4401 });
  assert.equal(tab.state.phase, "disconnected");
  assert.equal(effect(closed, "set_timer"), undefined);
  const view = board.describe(tab.state);
  assert.equal(view.connection, "연결 없음 — 다시 로그인");
  assert.equal(view.action, "login");
  assert.equal(view.canInput, false);
  assert.deepEqual(tab.send({ type: "reconnect" }), [], "a reconnect cannot help without a new login");
  assert.deepEqual(tab.send({ type: "timer", name: "reconnect" }), []);
  // other 44xx refusals keep the plain label and the manual reconnect
  const other = boot();
  attach(other);
  other.send({ type: "closed", conn: 1, code: 4403 });
  assert.equal(board.describe(other.state).connection, "연결 없음");
  assert.equal(board.describe(other.state).action, "reconnect");
});

test("upgrade refused with 429/503 (the browser reports it as a close 1006 before open) backs off and never hammers", () => {
  const tab = boot();
  tab.send({ type: "start", sizes: SIZES });
  const delays = [];
  for (let conn = 1; conn <= 6; conn += 1) {
    // no `opened` event: the handshake itself was answered with an HTTP error
    const refused = tab.send({ type: "closed", conn, code: 1006 });
    assert.equal(tab.state.phase, "disconnected");
    assert.equal(board.describe(tab.state).connection, "연결 없음");
    delays.push(effect(refused, "set_timer").ms);
    tab.send({ type: "timer", name: "reconnect" });
  }
  assert.deepEqual(delays, [1000, 2000, 4000, 8000, 15000, 15000]);
  // the same delays apply when the refusal comes right after a short-lived attach (capacity flapping)
  tab.send({ type: "opened", conn: 7 });
  tab.send({ type: "message", conn: 7, message: ATTACHED });
  assert.equal(effect(tab.send({ type: "closed", conn: 7, code: 1006 }), "set_timer").ms, 15000);
});

test("encodeFrame lays out channel byte, uint32 big-endian epoch, payload", () => {
  const bytes = board.encodeFrame("shell", 0x01020304, new Uint8Array([0x61, 0x62]));
  assert.deepEqual(Array.from(bytes), [1, 1, 2, 3, 4, 0x61, 0x62]);
  assert.deepEqual(Array.from(board.encodeFrame("agent", 5, new Uint8Array(0))), [0, 0, 0, 0, 5]);
});

// The same table runs against Python pages/board.py channel_label (test_web_pages_board.py): the server-rendered
// first paint and the live updates must read alike.
test("channelLabel matches the shared fixture table (also run against the Python channel_label)", () => {
  const table = JSON.parse(readFileSync(new URL("./channel_labels.json", import.meta.url), "utf8"));
  assert.ok(table.length >= 10);
  for (const { info, label } of table) assert.equal(board.channelLabel(info), label, JSON.stringify(info));
});
