// WBX-D1 part 2, second independent pass on the Board terminal state machine (static/board-state.js, protocol v2).
// Expected values come from web-board-ux SPEC s4 "WebSocket v2" (hello shape, ping/resize/release messages, the
// backoff 1->2->4..<=15 s with a 10 s stability rule, 4400-4499 never reconnect by themselves, 4401 log in again,
// 4404 the widget goes, 4429 the client limit, resize only from the widget's own layout after 150 ms and never as an
// answer to a server `size`/`attached`), BRIEF WBX-AC-32/33; not from the module's own tests. Property style: random
// server message sequences, random UTF-8 text split into frames.
import test from "node:test";
import assert from "node:assert/strict";
import * as board from "../../../src/workbench/hub/web/static/board-state.js";

const CSRF = "csrf-xyz";
const VIEW = { id: "s-abc", kind: "shell", name: "shell-1", state: "running", clients: 1, busy: false };

function boot(csrf = CSRF) {
  let state = board.initialState({ csrf, terminal: "s-abc" });
  const log = [];
  return {
    send(event) {
      const result = board.step(state, event);
      state = result.state;
      log.push(...result.effects);
      return result.effects;
    },
    get state() { return state; },
    log,
  };
}

function attached(tab, message = {}, conn) {
  const c = conn ?? tab.state.conn;
  tab.send({ type: "opened", conn: c });
  return tab.send({ type: "message", conn: c,
    message: { t: "attached", v: 2, terminal: VIEW, size: [24, 80], clients: 1, heartbeat: 10, ...message } });
}

const resizeSends = (effects) => effects.filter((e) => e.type === "send" && e.message && e.message.t === "resize");
const timers = (effects, name) => effects.filter((e) => e.type === "set_timer" && e.name === name);

// Deterministic PRNG (mulberry32) so a failure reproduces.
function rng(seed) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

test("hello is exactly {t, v:2, csrf, size} with the size clamped to 1..500, and omits size when none is known", () => {
  const withSize = boot();
  withSize.send({ type: "start", size: [0, 9999] });
  const hello = withSize.send({ type: "opened", conn: 1 }).find((e) => e.type === "send").message;
  assert.deepEqual(hello, { t: "hello", v: 2, csrf: CSRF, size: [1, 500] });
  const noSize = boot();
  noSize.send({ type: "start" });
  const bare = noSize.send({ type: "opened", conn: 1 }).find((e) => e.type === "send").message;
  assert.deepEqual(bare, { t: "hello", v: 2, csrf: CSRF });
});

test("the reconnect delay doubles 1,2,4,8,15,15 for every network-class code and resets only after a 10 s attach", () => {
  for (const code of [1006, 1001, 4002, 4003, 1009, 1011]) {
    const tab = boot();
    tab.send({ type: "start", size: [24, 80] });
    const delays = [];
    for (let i = 0; i < 6; i += 1) {
      const effects = tab.send({ type: "closed", conn: tab.state.conn, code });
      assert.equal(tab.state.phase, "disconnected", `code ${code}`);
      delays.push(timers(effects, "reconnect")[0].ms);
      tab.send({ type: "timer", name: "reconnect" });
      assert.equal(tab.state.phase, "connecting");
    }
    assert.deepEqual(delays, [1000, 2000, 4000, 8000, 15000, 15000], `code ${code}`);
    // an attach that drops before the stable timer keeps the delay growing; one that holds resets it
    attached(tab);
    let effects = tab.send({ type: "closed", conn: tab.state.conn, code });
    assert.equal(timers(effects, "reconnect")[0].ms, 15000, "dropped before 10 s: not reset");
    tab.send({ type: "timer", name: "reconnect" });
    attached(tab);
    tab.send({ type: "timer", name: "stable" });
    effects = tab.send({ type: "closed", conn: tab.state.conn, code });
    assert.equal(timers(effects, "reconnect")[0].ms, 1000, "held 10 s: starts over");
  }
});

test("every close code 4400..4499 refuses to reconnect by itself, 4404 removes the widget, 4401 and 4429 have their texts", () => {
  for (let code = 4400; code <= 4499; code += 1) {
    const tab = boot();
    tab.send({ type: "start", size: [24, 80] });
    attached(tab);
    const effects = tab.send({ type: "closed", conn: tab.state.conn, code });
    assert.equal(effects.some((e) => e.type === "set_timer" && e.name === "reconnect"), false, `code ${code}`);
    if (code === 4404) {
      assert.equal(tab.state.phase, "gone");
      assert.equal(effects.some((e) => e.type === "gone"), true);
      assert.equal(board.describe(tab.state).gone, true);
    } else {
      assert.equal(tab.state.phase, "refused", `code ${code}`);
      assert.equal(board.describe(tab.state).canInput, false);
    }
    // a late timer must not revive it
    tab.send({ type: "timer", name: "reconnect" });
    assert.notEqual(tab.state.phase, "connecting", `code ${code}`);
  }
  const limit = boot();
  limit.send({ type: "start", size: [24, 80] });
  limit.send({ type: "opened", conn: 1 });
  limit.send({ type: "message", conn: 1, message: { t: "refused", reason: "too_many_clients", limit: 8 } });
  limit.send({ type: "closed", conn: 1, code: 4429 });
  assert.deepEqual([board.describe(limit.state).connection, board.describe(limit.state).action],
    ["연결 수 한도(8개)를 넘었습니다", "reconnect"]);
  const login = boot();
  login.send({ type: "start", size: [24, 80] });
  attached(login);
  login.send({ type: "closed", conn: 1, code: 4401 });
  assert.deepEqual([board.describe(login.state).connection, board.describe(login.state).action],
    ["연결 없음 — 다시 로그인", "login"]);
  assert.equal(login.send({ type: "reconnect" }).length, 0, "4401 cannot be retried without a login");
  assert.equal(login.state.phase, "refused");
  assert.equal(limit.send({ type: "reconnect" }).some((e) => e.type === "open"), true, "4429 can be retried by hand");
});

test("fuzz: no server message sequence ever makes the client send a resize or crash, and attached keeps its phase", () => {
  const random = rng(20261029);
  const pick = (items) => items[Math.floor(random() * items.length)];
  const junk = [null, undefined, "x", 0, -5, 7.5, [], [1], [1, 2, 3], {}, true, NaN, "24", [0, 0], [9999, -3], [1.5, 2.5]];
  const kinds = ["attached", "size", "clients", "state", "pong", "error", "refused", "gone", "hello", "zzz", "", "resize"];
  for (let run = 0; run < 60; run += 1) {
    const tab = boot();
    tab.send({ type: "start", size: [24, 80] });
    attached(tab);
    for (let step = 0; step < 120; step += 1) {
      const message = { t: pick(kinds) };
      for (const key of ["rows", "cols", "size", "count", "clients", "terminal", "heartbeat", "reason", "n", "v"]) {
        if (random() < 0.4) message[key] = pick(junk);
      }
      const effects = tab.send({ type: "message", conn: tab.state.conn, message });
      assert.equal(resizeSends(effects).length, 0, `a server ${message.t} must not answer with a resize`);
      assert.equal(effects.some((e) => e.type === "set_timer" && e.name === "resize"), false);
      for (const effect of effects.filter((e) => e.type === "server_size")) {
        assert.ok(Number.isInteger(effect.rows) && Number.isInteger(effect.cols));
        assert.ok(effect.rows >= 1 && effect.rows <= 500 && effect.cols >= 1 && effect.cols <= 500, "clamped for xterm");
      }
      if (message.t === "gone") break;
      board.describe(tab.state); // must not throw
      if (message.t === "attached") assert.equal(tab.state.phase, "attached");
    }
  }
});

test("a layout resize is debounced: each change re-arms one 150 ms timer, the timer sends only the last size, once", () => {
  const tab = boot();
  tab.send({ type: "start", size: [24, 80] });
  attached(tab);
  const armed = [[30, 90], [31, 91], [40, 120]].map(([rows, cols]) => tab.send({ type: "resize", rows, cols }));
  for (const effects of armed) {
    assert.deepEqual(effects, [{ type: "set_timer", name: "resize", ms: 150 }]);
    assert.equal(resizeSends(effects).length, 0, "nothing is sent before the quiet period");
  }
  const sent = tab.send({ type: "timer", name: "resize" });
  assert.deepEqual(resizeSends(sent).map((e) => e.message), [{ t: "resize", rows: 40, cols: 120 }]);
  assert.equal(resizeSends(tab.send({ type: "timer", name: "resize" })).length, 0, "a second timer fire sends nothing");
  // sizes out of range are clamped and junk becomes 1
  tab.send({ type: "resize", rows: 0, cols: 9999 });
  assert.deepEqual(resizeSends(tab.send({ type: "timer", name: "resize" })).map((e) => e.message),
    [{ t: "resize", rows: 1, cols: 500 }]);
  tab.send({ type: "resize", rows: NaN, cols: "abc" });
  assert.deepEqual(resizeSends(tab.send({ type: "timer", name: "resize" })).map((e) => e.message),
    [{ t: "resize", rows: 1, cols: 1 }]);
});

test("a resize while not attached is never sent late; the next hello carries the latest size instead", () => {
  const tab = boot();
  tab.send({ type: "start", size: [24, 80] });
  attached(tab);
  tab.send({ type: "closed", conn: 1, code: 1006 });
  tab.send({ type: "resize", rows: 33, cols: 77 });
  assert.equal(resizeSends(tab.send({ type: "timer", name: "resize" })).length, 0);
  tab.send({ type: "timer", name: "reconnect" });
  const hello = tab.send({ type: "opened", conn: 2 }).find((e) => e.type === "send").message;
  assert.deepEqual(hello.size, [33, 77]);
});

test("events of an older socket never change a newer one: data, messages and closes are dropped by conn", () => {
  const tab = boot();
  tab.send({ type: "start", size: [24, 80] });
  attached(tab);
  tab.send({ type: "closed", conn: 1, code: 1006 });
  tab.send({ type: "timer", name: "reconnect" });
  assert.equal(tab.state.conn, 2);
  tab.send({ type: "opened", conn: 2 });
  for (const event of [{ type: "data", conn: 1, bytes: new Uint8Array([65]) },
    { type: "message", conn: 1, message: { t: "attached", v: 2, terminal: VIEW, size: [1, 1], clients: 8 } },
    { type: "closed", conn: 1, code: 4404 }, { type: "closed", conn: 1, code: 1006 }]) {
    assert.deepEqual(tab.send(event), [], JSON.stringify(event));
  }
  assert.equal(tab.state.phase, "connecting");
  // data before `attached` is dropped, after it is written in order, and `attached` resets the screen first
  assert.deepEqual(tab.send({ type: "data", conn: 2, bytes: new Uint8Array([66]) }), []);
  const effects = tab.send({ type: "message", conn: 2, message: { t: "attached", v: 2, terminal: VIEW, size: [24, 80], clients: 2, heartbeat: 10 } });
  assert.equal(effects[0].type, "reset_terminal");
  assert.equal(board.describe(tab.state).others, 1);
  const written = [1, 2, 3].map((n) => tab.send({ type: "data", conn: 2, bytes: new Uint8Array([n]) })[0]);
  assert.deepEqual(written.map((e) => [e.type, e.payload[0]]), [["write", 1], ["write", 2], ["write", 3]]);
});

test("input is sent only while attached; splitInput keeps random Korean and emoji text exact, valid and under the limit", () => {
  const tab = boot();
  tab.send({ type: "start", size: [24, 80] });
  assert.deepEqual(tab.send({ type: "input", bytes: new Uint8Array([65]) }), [], "connecting: nothing sent");
  attached(tab);
  assert.equal(tab.send({ type: "input", bytes: new Uint8Array([65]) })[0].type, "send_binary");
  assert.deepEqual(tab.send({ type: "input", bytes: new Uint8Array(0) }), []);
  const random = rng(7);
  const alphabet = ["가", "나", "힣", "한", "글", "a", "Z", " ", "\n", "🙂", "é", "中", "ㅊ"];
  const encoder = new TextEncoder();
  const decoder = new TextDecoder("utf-8", { fatal: true });
  for (let run = 0; run < 200; run += 1) {
    let text = "";
    const length = Math.floor(random() * 400);
    for (let i = 0; i < length; i += 1) text += alphabet[Math.floor(random() * alphabet.length)];
    const bytes = encoder.encode(text);
    const limit = 4 + Math.floor(random() * 60);
    const pieces = board.splitInput(bytes, limit);
    assert.ok(pieces.every((piece) => piece.length <= limit));
    assert.ok(pieces.every((piece) => piece.length > 0) || bytes.length === 0);
    assert.equal(decoder.decode(Uint8Array.from(pieces.flatMap((piece) => [...piece]))), text);
    for (const piece of pieces) decoder.decode(piece); // each frame is valid UTF-8 by itself
  }
  const big = new Uint8Array(board.INPUT_MAX);
  const frames = tab.send({ type: "input", bytes: big });
  assert.equal(frames.length, board.INPUT_MAX / board.INPUT_FRAME_MAX);
  assert.ok(frames.every((e) => e.type === "send_binary" && e.bytes.length <= 64 * 1024));
  const over = tab.send({ type: "input", bytes: new Uint8Array(board.INPUT_MAX + 1) });
  assert.deepEqual(over, [{ type: "notice", reason: "too_large" }], "refused whole, nothing sent");
});

test("pagehide releases once; the server's 1000 afterwards and late closes never reconnect; a restored page connects again", () => {
  const tab = boot();
  tab.send({ type: "start", size: [24, 80] });
  attached(tab);
  const hide = tab.send({ type: "pagehide" });
  assert.deepEqual(hide.filter((e) => e.type === "send").map((e) => e.message), [{ t: "release" }]);
  assert.equal(tab.state.phase, "released");
  for (const code of [1000, 1006, 4002]) {
    assert.deepEqual(tab.send({ type: "closed", conn: 1, code }), []);
    assert.equal(tab.state.phase, "released");
  }
  assert.equal(tab.send({ type: "timer", name: "reconnect" }).length, 0);
  assert.equal(tab.send({ type: "pageshow", persisted: false }).length, 0);
  assert.deepEqual(tab.send({ type: "pageshow", persisted: true }).map((e) => e.type), ["open"]);
  assert.equal(tab.state.conn, 2);
});

test("the pong watchdog: no pong for 20 s closes with 4000 and reconnects; every pong re-arms it; pings count up", () => {
  const tab = boot();
  tab.send({ type: "start", size: [24, 80] });
  const first = attached(tab);
  assert.deepEqual(timers(first, "ping").map((e) => e.ms), [10000]);
  assert.deepEqual(timers(first, "pong").map((e) => e.ms), [20000]);
  const pings = [1, 2, 3].map(() => tab.send({ type: "timer", name: "ping" }).find((e) => e.type === "send").message);
  assert.deepEqual(pings, [{ t: "ping", n: 1 }, { t: "ping", n: 2 }, { t: "ping", n: 3 }]);
  assert.deepEqual(timers(tab.send({ type: "message", conn: 1, message: { t: "pong", n: 3 } }), "pong").map((e) => e.ms), [20000]);
  const dead = tab.send({ type: "timer", name: "pong" });
  assert.deepEqual(dead.filter((e) => e.type === "close"), [{ type: "close", conn: 1, code: 4000 }]);
  assert.deepEqual(timers(dead, "reconnect").map((e) => e.ms), [1000]);
  assert.equal(tab.state.phase, "disconnected");
  assert.equal(board.describe(tab.state).action, "reconnect");
});

test("a state message updates the view and name, a gone message removes the widget and the following 4404 changes nothing", () => {
  const tab = boot();
  tab.send({ type: "start", size: [24, 80] });
  attached(tab);
  tab.send({ type: "message", conn: 1, message: { t: "state", terminal: { ...VIEW, name: "<b>빌드</b>", busy: true } } });
  assert.equal(board.describe(tab.state).view.name, "<b>빌드</b>");
  tab.send({ type: "message", conn: 1, message: { t: "clients", count: 5 } });
  assert.equal(board.describe(tab.state).others, 4);
  const gone = tab.send({ type: "message", conn: 1, message: { t: "gone" } });
  assert.equal(gone.some((e) => e.type === "gone"), true);
  assert.equal(tab.state.phase, "gone");
  assert.equal(tab.send({ type: "closed", conn: 1, code: 4404 }).length, 0);
  assert.equal(tab.state.phase, "gone");
  assert.equal(board.describe(tab.state).others, 0);
  assert.equal(board.describe(tab.state).canInput, false);
});
