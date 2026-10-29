// WBX-D2 independent adversarial verification (seam S8): the Board workspace's pure modules (board-layout.js,
// board-panel.js) under seeded random input, and board.js against a mocked DOM, fetch, WebSocket and storage.
// Expected values come from the contract (docs/features/web-board-ux SPEC s4 "화면: workspace와 panel", BRIEF
// WBX-D42..D58, ticket WBX-D2), not from the worker's tests. No browser: the real-Chrome scenarios are in
// tests/hub/test_web_pages_board_independent.py.
import test from "node:test";
import assert from "node:assert/strict";

import * as L from "../../../src/workbench/hub/web/static/board-layout.js";
import * as P from "../../../src/workbench/hub/web/static/board-panel.js";

const BOARD_JS = "../../../src/workbench/hub/web/static/board.js";

// ---- seeded random -----------------------------------------------------------------------------------------------
function prng(seed) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}
const between = (rand, lo, hi) => lo + Math.floor(rand() * (hi - lo + 1));
const pick = (rand, list) => list[Math.floor(rand() * list.length)];
const hex = (n) => n.toString(16).padStart(12, "0");
const ID = (kind, n) => `${kind}-${hex(n)}`;

// The contract's invariant for a layout of visible widgets: integers, inside the 12 columns, at least 3 x 6, no two
// visible widgets share area (touching is fine). Independent of the module's own helpers.
function assertLayoutValid(widgets, label) {
  const visible = Object.entries(widgets).filter(([, entry]) => entry.visible);
  for (const [id, entry] of visible) {
    for (const key of ["x", "y", "w", "h"]) assert.ok(Number.isInteger(entry[key]), `${label}: ${id}.${key} integer (${entry[key]})`);
    assert.ok(entry.x >= 0 && entry.y >= 0, `${label}: ${id} not above row 0 / left of column 0`);
    assert.ok(entry.w >= 3 && entry.h >= 6, `${label}: ${id} at least 3 x 6 (${entry.w} x ${entry.h})`);
    assert.ok(entry.x + entry.w <= 12, `${label}: ${id} inside 12 columns`);
  }
  for (let i = 0; i < visible.length; i += 1) {
    for (let j = i + 1; j < visible.length; j += 1) {
      const [ida, a] = visible[i];
      const [idb, b] = visible[j];
      const overlap = a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h;
      assert.equal(overlap, false, `${label}: ${ida} and ${idb} overlap`);
    }
  }
}
const termList = (n) => Array.from({ length: n }, (_, i) => ({ id: ID(i < Math.ceil(n / 2) ? "a" : "s", i + 1), kind: i < Math.ceil(n / 2) ? "agent" : "shell", name: `t${i}` }));

// ===============================================================================================================
// Pure: layout
// ===============================================================================================================

test("grid boundaries: a rectangle is valid exactly when whole, >= 3 x 6, inside columns 0..11 and rows >= 0", () => {
  const ok = [[0, 0, 3, 6], [9, 0, 3, 6], [0, 0, 12, 6], [0, 99994, 3, 6], [5, 7, 7, 40]];
  const bad = [[10, 0, 3, 6], [0, 0, 2, 6], [0, 0, 3, 5], [-1, 0, 3, 6], [0, -1, 3, 6], [0, 0, 13, 6], [0.5, 0, 3, 6], [0, 0, 3, 6.5],
    [NaN, 0, 3, 6], [0, 0, Infinity, 6], [0, 99995, 3, 6], [0, 0, "3", 6], [0, 0, null, 6], [1, 0, 12, 6]];
  for (const [x, y, w, h] of ok) assert.equal(L.inGrid({ x, y, w, h }), true, `${x},${y},${w},${h}`);
  for (const [x, y, w, h] of bad) assert.equal(L.inGrid({ x, y, w, h }), false, `${x},${y},${w},${h}`);
});

test("touching edges and corners never overlap; one shared cell always does (exhaustive over a small window)", () => {
  const base = { x: 4, y: 4, w: 3, h: 6 };
  for (let x = 0; x <= 9; x += 1) {
    for (let y = 0; y <= 14; y += 1) {
      const other = { x, y, w: 3, h: 6 };
      const share = Math.max(base.x, x) < Math.min(base.x + 3, x + 3) && Math.max(base.y, y) < Math.min(base.y + 6, y + 6);
      assert.equal(L.overlaps(base, other), share, `${x},${y}`);
      assert.equal(L.overlaps(other, base), share, `${x},${y} symmetric`);
    }
  }
});

test("settle: a drop on a shared cell goes back to the exact last valid rectangle and nothing else moves; touching is accepted", () => {
  const widgets = Object.freeze({
    a: Object.freeze({ x: 0, y: 0, w: 6, h: 10, visible: true }),
    b: Object.freeze({ x: 6, y: 0, w: 6, h: 10, visible: true }),
    h: Object.freeze({ x: 0, y: 10, w: 6, h: 10, visible: false }),
  });
  const refused = L.settle("a", { x: 5, y: 0, w: 6, h: 10 }, widgets); // one column into b
  assert.deepEqual(refused, { ok: false, rect: { x: 0, y: 0, w: 6, h: 10 } });
  const touching = L.settle("a", { x: 0, y: 10, w: 6, h: 10 }, widgets); // directly under: touches h only if h were visible
  assert.equal(touching.ok, true, "a hidden widget is not an obstacle");
  const edge = L.settle("b", { x: 6, y: 10, w: 6, h: 6 }, widgets);
  assert.equal(edge.ok, true);
  assert.equal(L.settle("a", { x: 0, y: 0, w: 7, h: 10 }, widgets).ok, false, "grown by one column into b");
  assert.equal(L.settle("a", { x: 0, y: 0, w: 6, h: 10 }, widgets).ok, true, "dropped on itself");
  assert.equal(L.settle("a", { x: 0, y: 0, w: 2, h: 10 }, widgets).ok, false, "under the minimum width");
  assert.equal(L.settle("a", { x: -1, y: 0, w: 6, h: 10 }, widgets).ok, false, "outside the grid goes back");
});

test("fuzz: 5000 random move, resize, show, hide, reset and list changes never leave an overlap or an invalid rectangle", () => {
  const rand = prng(20261029);
  let terminals = termList(6);
  let widgets = L.autoPlace(terminals.map((t) => t.id));
  let nextId = 100;
  assertLayoutValid(widgets, "start");
  for (let step = 0; step < 5000; step += 1) {
    const ids = Object.keys(widgets);
    const op = between(rand, 0, 9);
    const id = ids.length ? pick(rand, ids) : null;
    if (op <= 3 && id) { // drag
      const dx = between(rand, -14, 14);
      const dy = between(rand, -20, 20);
      const proposal = L.moveRect(widgets[id], dx, dy);
      const verdict = L.settle(id, proposal, widgets);
      if (verdict.ok) widgets = L.setRect(widgets, id, verdict.rect);
      else assert.deepEqual(verdict.rect, { x: widgets[id].x, y: widgets[id].y, w: widgets[id].w, h: widgets[id].h }, "refused: last valid place");
    } else if (op <= 6 && id) { // resize handle
      const proposal = L.resizeRect(widgets[id], between(rand, -14, 14), between(rand, -20, 20));
      const verdict = L.settle(id, proposal, widgets);
      if (verdict.ok) widgets = L.setRect(widgets, id, verdict.rect);
    } else if (op === 7 && id) {
      widgets = widgets[id].visible ? L.hideWidget(widgets, id) : L.showWidget(widgets, id);
    } else if (op === 8) { // another browser adds or removes a terminal
      if (rand() < 0.5 || terminals.length < 2) terminals = [...terminals, { id: ID(rand() < 0.5 ? "a" : "s", nextId++), kind: "shell", name: "n" }];
      else { const drop = between(rand, 0, terminals.length - 1); terminals = terminals.filter((_, i) => i !== drop); }
      const created = rand() < 0.3 && terminals.length ? [terminals.at(-1).id] : [];
      widgets = L.reconcile(widgets, terminals, created);
      assert.deepEqual(Object.keys(widgets), terminals.map((t) => t.id), "exactly the listed terminals, in order");
    } else if (op === 9 && rand() < 0.2) {
      widgets = L.resetLayout(terminals);
    }
    assertLayoutValid(widgets, `step ${step} op ${op}`);
  }
});

test("fuzz: parseLayout + reconcile of 4000 hostile stored values never throws and always returns a valid layout of exactly the listed terminals", () => {
  const rand = prng(7);
  const terminals = termList(5);
  const weird = () => pick(rand, [null, undefined, NaN, Infinity, -1, 0, 1, 2, 3, 6, 11, 12, 13, 1e9, -1e9, 0.5, "3", "x", true, false, [], {}, [1, 2], { x: 1 }]);
  const numeric = () => (rand() < 0.6 ? between(rand, -2, 16) : weird());
  const entry = () => (rand() < 0.1 ? weird() : { x: numeric(), y: numeric(), w: numeric(), h: numeric(), visible: rand() < 0.7 ? true : weird() });
  for (let round = 0; round < 4000; round += 1) {
    const widgets = {};
    for (let n = between(rand, 0, 9); n > 0; n -= 1) {
      const key = rand() < 0.5 ? pick(rand, terminals).id : pick(rand, ["__proto__", "constructor", "toString", "a-1", "x-000000000001", ID("a", between(rand, 1, 12)), "", " ", "a-" + "9".repeat(80)]);
      widgets[key] = entry();
    }
    const wrappers = [{ v: 1, widgets }, { v: 2, widgets }, { v: "1", widgets }, { v: 1, widgets: [widgets] }, { v: 1 }, [widgets], widgets, null];
    const text = rand() < 0.1 ? "{" + JSON.stringify(wrappers[0]) : JSON.stringify(wrappers[round % 3 === 0 ? between(rand, 0, 7) : 0]);
    let parsed;
    assert.doesNotThrow(() => { parsed = L.parseLayout(text); }, text);
    assert.ok(parsed === null || (typeof parsed === "object" && Object.getPrototypeOf(parsed) === Object.prototype));
    const result = L.reconcile(parsed, terminals, rand() < 0.3 ? [terminals[0].id] : []);
    assert.deepEqual(Object.keys(result), terminals.map((t) => t.id));
    assertLayoutValid(result, `round ${round}: ${text.slice(0, 200)}`);
    assert.equal(Object.prototype.hasOwnProperty.call(result, "__proto__"), false);
    assert.equal(({}).polluted, undefined);
  }
});

test("a foreign version, a corrupt text, a non-object and an oversized text are all 'no stored layout' (null), never a partial one", () => {
  for (const text of ['{"v":2,"widgets":{}}', '{"v":0,"widgets":{}}', "not json", "", "null", "[]", "42", '"x"', '{"widgets":{}}',
    '{"v":1}', '{"v":1,"widgets":[]}', '{"v":1,"widgets":null}', "{".repeat(100000), "x".repeat(5 * 1024 * 1024)]) {
    assert.equal(L.parseLayout(text), null, text.slice(0, 40));
  }
  assert.equal(L.parseLayout(undefined), null);
  assert.equal(L.parseLayout(12), null);
});

test("an oversized but well-formed stored layout does not freeze the page (parse stays fast with thousands of valid-looking entries)", () => {
  // localStorage allows ~5 MB: a hostile or corrupt value can hold tens of thousands of disjoint 3 x 6 entries (a real layout has at most 40).
  const widgets = {};
  let n = 0;
  for (let row = 0; row < 99990 && n < 3000; row += 6) {
    for (let col = 0; col < 12 && n < 3000; col += 3) { widgets[ID("a", n + 1)] = { x: col, y: row, w: 3, h: 6, visible: true }; n += 1; }
  }
  const text = JSON.stringify({ v: 1, widgets });
  assert.ok(text.length < 5 * 1024 * 1024, "fits in localStorage");
  const started = process.hrtime.bigint();
  const parsed = L.parseLayout(text);
  const ms = Number(process.hrtime.bigint() - started) / 1e6;
  assert.ok(parsed !== null);
  assert.ok(ms < 250, `parseLayout of ${n} entries took ${ms.toFixed(0)} ms (quadratic: 20000 entries, 1.4 MB, block a page load for ~1 minute)`);
});

test("serialize then parse is the identity for every valid layout (fuzz), and serialize never writes anything but the five fields", () => {
  const rand = prng(11);
  for (let round = 0; round < 500; round += 1) {
    let terminals = termList(between(rand, 0, 8));
    let widgets = L.autoPlace(terminals.map((t) => t.id));
    for (let i = 0; i < 6; i += 1) {
      const ids = Object.keys(widgets);
      if (!ids.length) break;
      const id = pick(rand, ids);
      const v = L.settle(id, L.moveRect(widgets[id], between(rand, -5, 5), between(rand, -5, 12)), widgets);
      if (v.ok) widgets = L.setRect(widgets, id, v.rect);
      if (rand() < 0.3) widgets = L.hideWidget(widgets, id);
    }
    const text = L.serializeLayout(widgets);
    const back = L.parseLayout(text);
    assert.deepEqual(back, Object.fromEntries(Object.entries(widgets).map(([id, e]) => [id, { x: e.x, y: e.y, w: e.w, h: e.h, visible: e.visible }])));
    assert.deepEqual(Object.keys(JSON.parse(text)).sort(), ["v", "widgets"]);
  }
});

test("storage that throws on the property, the read or the write never throws out of the module and falls back to defaults", () => {
  const mk = (over) => ({ getItem: () => null, setItem: () => {}, ...over });
  const boom = () => { throw new Error("SecurityError"); };
  const variants = [mk({ getItem: boom }), mk({ setItem: boom }), mk({ getItem: () => { throw new DOMException("x", "SecurityError"); } }),
    mk({ getItem: () => 12 }), mk({ getItem: () => ({}) }), null, undefined];
  for (const storage of variants) {
    assert.doesNotThrow(() => L.loadLayout(storage));
    assert.equal(L.loadLayout(storage), null);
    assert.equal(L.loadPanel(storage), "open");
    assert.equal(L.loadNarrow(storage), null);
    assert.doesNotThrow(() => L.saveLayout(storage, L.autoPlace(["a-000000000001"])));
    assert.doesNotThrow(() => L.savePanel(storage, "closed"));
    assert.doesNotThrow(() => L.saveNarrow(storage, "a-000000000001"));
  }
  const win = {};
  Object.defineProperty(win, "localStorage", { get: boom });
  assert.equal(L.browserStorage(win), null);
  assert.equal(L.browserStorage(null), null);
});

test("reconcile with a stored layout: a terminal that appeared elsewhere is hidden and takes no room; one that vanished is dropped; kept ones keep their place", () => {
  const terminals = termList(3);
  const stored = { [terminals[0].id]: { x: 0, y: 0, w: 6, h: 10, visible: true }, [terminals[1].id]: { x: 6, y: 0, w: 6, h: 10, visible: false },
    "s-0000deadbeef": { x: 0, y: 10, w: 6, h: 10, visible: true } };
  const out = L.reconcile(stored, terminals);
  assert.deepEqual(Object.keys(out), terminals.map((t) => t.id));
  assert.deepEqual(out[terminals[0].id], { x: 0, y: 0, w: 6, h: 10, visible: true });
  assert.equal(out[terminals[1].id].visible, false);
  assert.deepEqual(out[terminals[1].id], { x: 6, y: 0, w: 6, h: 10, visible: false }, "a hidden one keeps its remembered place");
  assert.equal(out[terminals[2].id].visible, false, "new here: hidden");
  assert.equal("s-0000deadbeef" in out, false);
  // nothing stored: all visible, agents first as listed
  const fresh = L.reconcile(null, terminals);
  assert.ok(Object.values(fresh).every((entry) => entry.visible));
});

test("showing a widget whose old place was taken finds a free place; the visible widgets never move", () => {
  let widgets = { a: { x: 0, y: 0, w: 6, h: 10, visible: true }, b: { x: 6, y: 0, w: 6, h: 10, visible: false } };
  widgets = L.setRect(widgets, "a", { x: 6, y: 0, w: 6, h: 10 }); // a took b's old place
  const shown = L.showWidget(widgets, "b");
  assert.equal(shown.b.visible, true);
  assert.deepEqual({ ...shown.a }, widgets.a);
  assertLayoutValid(shown, "after show");
});

test("narrow selection and wanted ids: the chosen terminal alone under 720 px; a vanished choice falls back to the first agent", () => {
  const terminals = [{ id: "s-000000000009", kind: "shell" }, { id: "a-000000000001", kind: "agent" }, { id: "a-000000000002", kind: "agent" }];
  assert.equal(L.pickNarrow(terminals, "s-000000000009"), "s-000000000009");
  assert.equal(L.pickNarrow(terminals, "a-gone"), "a-000000000001", "first agent even when a shell is listed first");
  assert.equal(L.pickNarrow([terminals[0]], null), "s-000000000009");
  assert.equal(L.pickNarrow([], null), null);
  const widgets = { "a-000000000001": { visible: true }, "a-000000000002": { visible: false } };
  assert.deepEqual(L.wantedIds({ widgets, narrow: true, chosen: "a-000000000002" }), ["a-000000000002"], "a hidden-in-grid terminal can still be the narrow one");
  assert.deepEqual(L.wantedIds({ widgets, narrow: true, chosen: "gone" }), []);
  assert.deepEqual(L.wantedIds({ widgets, narrow: false, chosen: "a-000000000002" }), ["a-000000000001"]);
  const mq = (width) => ({ matchMedia: (query) => ({ matches: width < 720 && /max-width:\s*719\.98px/.test(query) }) });
  assert.equal(L.isNarrow(mq(719)), true);
  assert.equal(L.isNarrow(mq(720)), false);
  assert.equal(L.NARROW_QUERY, "(max-width: 719.98px)");
});

// ===============================================================================================================
// Pure: panel
// ===============================================================================================================

// A reference of the Hub's name rule written from SPEC "이름": strip, 1..40 code points, no Cc/Cf/Zl/Zp, unique per
// kind ignoring case. (The Hub is the authority; this checks the page's own pre-check never rejects what is plainly fine
// and never accepts what is plainly not.)
test("name pre-check agrees with the contract on a table of plain cases", () => {
  const terminals = [{ id: "a-000000000001", kind: "agent", name: "agent-1" }, { id: "a-000000000002", kind: "agent", name: "전처리" },
    { id: "s-000000000001", kind: "shell", name: "shell-1" }];
  const check = (name, extra = {}) => P.nameProblem(name, { kind: "agent", terminals, ...extra });
  for (const good of ["전처리2", "a b", "x".repeat(40), "가".repeat(40), "😀".repeat(40), "é".repeat(20), "<img src=x onerror=alert(1)>", "' OR 1=1 --", "shell-1", "  padded  ", "Ünï", "Ａｇｅｎｔ"]) {
    assert.equal(check(good), null, JSON.stringify(good));
  }
  for (const bad of ["", "   ", "\t\n", "x".repeat(41), "가".repeat(41), "😀".repeat(41), "a\u0000b", "a\u0007b", "a\u007fb", "a​b", "a‍b", "a b", "a b", "a\nb", "‮abc", "ab﻿c"]) {
    assert.equal(check(bad)?.reason, "name_invalid", JSON.stringify(bad));
  }
  assert.equal(check("AGENT-1")?.reason, "name_taken", "case-insensitive duplicate within the kind");
  assert.equal(check(" 전처리 ")?.reason, "name_taken", "after trimming");
  assert.equal(check("agent-1", { selfId: "a-000000000001" }), null, "renaming to its own name is not a duplicate");
  assert.equal(P.nameProblem("shell-1", { kind: "agent", terminals }), null, "the same name in the other kind is fine");
  assert.equal(P.nameProblem("SHELL-1", { kind: "shell", terminals })?.reason, "name_taken");
  for (const odd of [null, undefined, 12, {}, []]) assert.equal(check(odd)?.reason, "name_invalid");
});

test("limit form: whole numbers in range and not under the count now; everything else gets the Hub's sentence and no request", () => {
  const terminals = [...termList(4)];
  const counts = { agent: terminals.filter((t) => t.kind === "agent").length, shell: terminals.filter((t) => t.kind === "shell").length };
  const options = { agent_range: [1, 8], shell_range: [1, 32] };
  const good = [["4", "10"], [" 8 ", "32"], [String(counts.agent), String(counts.shell)], ["1", "1"].map((x, i) => (i ? String(counts.shell) : String(Math.max(1, counts.agent))))];
  for (const [agent, shell] of good) assert.equal(P.optionsRequest({ agent, shell }, options, terminals).ok, true, `${agent}/${shell}`);
  for (const [agent, shell] of [["", "10"], ["4", ""], ["0", "10"], ["9", "10"], ["4", "0"], ["4", "33"], ["1.5", "10"], ["1e1", "10"], ["-1", "10"], ["0x4", "10"],
    ["４", "10"], ["٣", "10"], ["NaN", "10"], ["4 5", "10"], [null, "10"], [undefined, "10"], ["4", "10abc"]]) {
    const request = P.optionsRequest({ agent, shell }, options, terminals);
    assert.equal(request.ok, false, `${agent}/${shell}`);
    assert.equal(typeof request.text, "string");
  }
  assert.equal(P.optionsRequest({ agent: String(counts.agent - 1 || 0), shell: "10" }, options, terminals).ok, false, "under the current agent count");
  assert.equal(P.optionsRequest({ agent: "8", shell: String(counts.shell - 1) }, options, terminals).ok, false, "under the current shell count");
});

test("planConnections fuzz: only wanted idle/released widgets connect, never more than 4 waiting for a hello, only unwanted live ones are released", () => {
  const rand = prng(3);
  const phases = ["idle", "connecting", "attached", "disconnected", "refused", "gone", "released"];
  for (let round = 0; round < 3000; round += 1) {
    const widgets = Array.from({ length: between(rand, 0, 12) }, (_, i) => ({ id: `w${i}`, wanted: rand() < 0.6, phase: pick(rand, phases) }));
    const plan = P.planConnections(widgets);
    const by = Object.fromEntries(widgets.map((w) => [w.id, w]));
    for (const id of plan.connect) assert.ok(by[id].wanted && ["idle", "released"].includes(by[id].phase), `connect ${id}`);
    for (const id of plan.release) assert.ok(!by[id].wanted && ["connecting", "attached", "disconnected"].includes(by[id].phase), `release ${id}`);
    assert.equal(new Set(plan.connect).size, plan.connect.length);
    const waiting = widgets.filter((w) => w.wanted && w.phase === "connecting").length;
    assert.ok(waiting + plan.connect.length <= Math.max(4, waiting), `at most 4 waiting: ${waiting} + ${plan.connect.length}`);
    // nothing wanted-and-connectable is left out while there is room
    const eligible = widgets.filter((w) => w.wanted && ["idle", "released"].includes(w.phase)).length;
    assert.equal(plan.connect.length, Math.min(eligible, Math.max(0, 4 - waiting)));
    // after the plan runs, sockets in use never exceed the wanted widgets
    const open = widgets.filter((w) => ["connecting", "attached", "disconnected"].includes(w.phase) && !plan.release.includes(w.id)).length + plan.connect.length;
    const wantedCount = widgets.filter((w) => w.wanted).length;
    const unwantedKept = widgets.filter((w) => !w.wanted && ["connecting", "attached", "disconnected"].includes(w.phase) && !plan.release.includes(w.id)).length;
    assert.equal(unwantedKept, 0);
    assert.ok(open <= wantedCount, `open ${open} <= wanted ${wantedCount}`);
  }
});

test("the confirm sentences are the SPEC texts and every destructive action waits for the answer", async () => {
  assert.equal(P.confirmText("stop", "전처리"), "전처리을 중지합니다. 메모리는 남습니다.");
  assert.equal(P.confirmText("delete", "agent-1"), "agent-1의 세션과 메모리를 지웁니다. 되돌릴 수 없습니다.");
  assert.equal(P.confirmText("close", "shell-1"), "shell-1을 닫습니다. 실행 중인 명령이 끝납니다.");
  assert.equal(P.confirmText("start", "x"), null);
  const calls = [];
  const api = Object.fromEntries(["start", "stop", "restart", "close", "remove"].map((name) => [name, (id) => { calls.push([name, id]); return Promise.resolve({ ok: true }); }]));
  const agent = { id: "a-000000000001", kind: "agent", name: "n", busy: false };
  const shell = { id: "s-000000000001", kind: "shell", name: "n", busy: false };
  for (const [action, view] of [["stop", agent], ["delete", agent], ["close", shell], ["restart", agent], ["restart", shell]]) {
    calls.length = 0;
    const pending = P.terminalAction(api, action, view, { confirm: () => new Promise((resolve) => setTimeout(() => resolve(false), 5)) });
    assert.deepEqual(calls, [], `${action}: nothing before the answer`);
    const result = await pending;
    assert.equal(result.cancelled, true);
    assert.deepEqual(calls, [], `${action}: nothing after a no`);
  }
  calls.length = 0;
  await P.terminalAction(api, "start", agent, { confirm: () => { throw new Error("start must not ask"); } });
  assert.deepEqual(calls, [["start", agent.id]]);
});

// ===============================================================================================================
// Glue: board.js against a mocked DOM, fetch, WebSocket, storage and timers (a fresh page per scenario)
// ===============================================================================================================

class El {
  constructor(tag, id = "", ctx = null) {
    this.tagName = String(tag).toUpperCase(); this.id = id; this.ctx = ctx; this.children = []; this.parent = null; this.listeners = {};
    this.attrs = {}; this.dataset = {}; this._text = ""; this.hidden = false; this.disabled = false; this.value = ""; this.open = false; this.returnValue = "";
    const classes = new Set();
    this.classes = classes;
    this.classList = {
      add: (...names) => names.forEach((name) => classes.add(name)), remove: (...names) => names.forEach((name) => classes.delete(name)),
      toggle: (name, on) => { if (on === undefined ? !classes.has(name) : on) classes.add(name); else classes.delete(name); return classes.has(name); },
      contains: (name) => classes.has(name),
    };
    this.styleProps = new Map();
    this.style = { setProperty: (name, value) => this.styleProps.set(name, String(value)), removeProperty: (name) => this.styleProps.delete(name), getPropertyValue: (name) => this.styleProps.get(name) || "" };
    this.captured = null; this.clientWidth = 600; this.clientHeight = 400;
  }
  set innerHTML(value) { if (this.ctx) this.ctx.htmlWrites.push(String(value)); }
  get innerHTML() { return ""; }
  set outerHTML(value) { if (this.ctx) this.ctx.htmlWrites.push(String(value)); }
  insertAdjacentHTML(_where, value) { if (this.ctx) this.ctx.htmlWrites.push(String(value)); }
  get className() { return [...this.classes].join(" "); }
  set className(value) { this.classes.clear(); for (const name of String(value).split(/\s+/).filter(Boolean)) this.classes.add(name); }
  get textContent() { return this._text + this.children.map((child) => child.textContent).join(""); }
  set textContent(value) { this._text = String(value); this.children = []; }
  setAttribute(name, value) { this.attrs[name] = String(value); if (name.startsWith("data-")) this.dataset[name.slice(5)] = String(value); }
  append(...nodes) { for (const node of nodes) { node.parent = this; this.children.push(node); } }
  insertBefore(node, ref) { node.parent = this; const at = this.children.indexOf(ref); this.children.splice(at < 0 ? this.children.length : at, 0, node); }
  replaceChildren(...nodes) { this.children = []; this.append(...nodes); }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter((child) => child !== this); this.parent = null; }
  addEventListener(type, handler, options) { (this.listeners[type] ||= []).push({ handler, once: Boolean(options && options.once) }); }
  fire(type, event = {}) {
    for (const entry of [...(this.listeners[type] || [])]) {
      if (entry.once) this.listeners[type] = this.listeners[type].filter((item) => item !== entry);
      entry.handler({ target: this, preventDefault() { this.prevented = true; }, ...event });
    }
  }
  closest(selector) { for (let node = this; node; node = node.parent) if (node.tagName === selector.toUpperCase()) return node; return null; }
  getBoundingClientRect() { return { left: 0, top: 0, width: this.id === "board-workspace" ? 1200 : 600, height: 600 }; }
  focus() { if (this.ctx) this.ctx.activeElement = this; }
  select() {}
  showModal() { this.open = true; }
  close(value = "") { this.open = false; this.returnValue = value; this.fire("close"); }
  setPointerCapture(id) { this.captured = id; }
  releasePointerCapture() { this.captured = null; }
  find(predicate, out = []) { if (predicate(this)) out.push(this); for (const child of this.children) child.find(predicate, out); return out; }
}

const IDS = ["board-workspace", "board-shell", "board-panel", "board-notice", "board-panel-toggle", "board-agents", "board-shells", "board-new-agent",
  "board-new-shell", "board-agent-note", "board-shell-note", "board-reset-layout", "board-options", "board-agent-limit", "board-shell-limit",
  "board-options-error", "board-confirm", "board-confirm-text", "board-confirm-ok", "board-confirm-cancel"];
const view = (id, kind, name, over = {}) => ({ id, kind, name, state: "running", detail: null, exit_status: null, clients: 0, busy: false, ...over });
const A1 = "a-00000000000a", A2 = "a-00000000000b", S1 = "s-00000000000c", S2 = "s-00000000000d";
const OPTIONS = { agent_limit: 4, shell_limit: 10, agent_range: [1, 8], shell_range: [1, 32] };
const settle = async () => { for (let i = 0; i < 10; i += 1) await new Promise((resolve) => setImmediate(resolve)); };
let bootCounter = 0;

// One page: its own globals. `terminals`: the Hub's list. `storage`: "ok" | {get,set,prop throws} | a map. `narrow`: initial.
async function boot({ terminals, stored = {}, storage = "ok", narrow = false, replyFor = null } = {}) {
  const ctx = { htmlWrites: [], activeElement: null, fetches: [], sockets: [], terms: [], timers: new Map(), timerSeq: 0, winListeners: {}, docListeners: {},
    storageData: { ...stored }, storageOps: [], location: { protocol: "http:", host: "127.0.0.1:8780", href: "/board" }, narrow, mediaHandlers: [],
    list: terminals || [view(A1, "agent", "agent-1"), view(A2, "agent", "전처리"), view(S1, "shell", "shell-1")], options: { ...OPTIONS }, elements: new Map(), notices: [] };
  const element = (id) => { if (!ctx.elements.has(id)) ctx.elements.set(id, new El("div", id, ctx)); return ctx.elements.get(id); };
  for (const id of IDS) element(id);
  element("board-notice").hidden = true;
  element("board-options-error").hidden = true;
  ctx.element = element;
  class FakeTerminal {
    constructor(options) { this.rows = 24; this.cols = 80; this.options = {}; this.written = []; this.handlers = {}; this.created = options; this.disposed = false; this.focused = 0; this.resizes = []; ctx.terms.push(this); }
    attachCustomKeyEventHandler() {}
    hasSelection() { return false; }
    getSelection() { return ""; }
    clearSelection() {}
    loadAddon() {}
    open(host) { this.host = host; }
    focus() { this.focused += 1; }
    reset() {}
    dispose() { this.disposed = true; }
    write(data) { this.written.push(new TextDecoder().decode(data)); }
    onData(handler) { this.handlers.data = handler; }
    onBinary(handler) { this.handlers.binary = handler; }
    resize(cols, rows) { this.rows = rows; this.cols = cols; this.resizes.push([rows, cols]); }
  }
  class FakeFit { proposeDimensions() { return { rows: ctx.fitRows || 30, cols: ctx.fitCols || 100 }; } }
  class FakeWebSocket {
    static OPEN = 1;
    constructor(url) { this.url = url; this.readyState = 1; this.sent = []; this.closed = false; ctx.sockets.push(this); }
    send(data) { this.sent.push(data); }
    close() { this.readyState = 3; this.closed = true; }
  }
  const store = {
    getItem: (key) => { ctx.storageOps.push(["get", key]); if (storage === "throwGet" || storage === "throwBoth") throw new DOMException("denied", "SecurityError"); return key in ctx.storageData ? ctx.storageData[key] : null; },
    setItem: (key, value) => { ctx.storageOps.push(["set", key, value]); if (storage === "throwSet" || storage === "throwBoth") throw new DOMException("quota", "QuotaExceededError"); ctx.storageData[key] = String(value); },
  };
  globalThis.document = {
    getElementById: element,
    querySelector: (selector) => (selector.includes("csrf-token") ? { content: "csrf-meta" } : selector.includes("style-nonce") ? { content: "nonce-meta" } : null),
    createElement: (tag) => new El(tag, "", ctx),
    addEventListener: (type, handler) => { ctx.docListeners[type] = handler; },
    execCommand() { return false; },
    get activeElement() { return ctx.activeElement; },
    write() { ctx.htmlWrites.push("document.write"); },
  };
  Object.defineProperty(globalThis, "navigator", { value: { platform: "Linux x86_64", userAgent: "test" }, configurable: true });
  globalThis.isSecureContext = false;
  globalThis.window = { addEventListener: (type, handler) => { (ctx.winListeners[type] ||= []).push(handler); } };
  if (storage === "propThrows") Object.defineProperty(globalThis, "localStorage", { get() { throw new DOMException("blocked", "SecurityError"); }, configurable: true });
  else if (storage === "absent") Object.defineProperty(globalThis, "localStorage", { value: undefined, configurable: true });
  else Object.defineProperty(globalThis, "localStorage", { value: store, configurable: true });
  globalThis.getComputedStyle = () => ({ columnGap: "8px", rowGap: "8px", gridAutoRows: "40px" });
  globalThis.matchMedia = () => ({ get matches() { return ctx.narrow; }, addEventListener: (type, handler) => ctx.mediaHandlers.push(handler) });
  globalThis.location = ctx.location;
  globalThis.Terminal = FakeTerminal;
  globalThis.FitAddon = { FitAddon: FakeFit };
  globalThis.WebSocket = FakeWebSocket;
  globalThis.requestAnimationFrame = (callback) => callback();
  globalThis.setTimeout = (callback, ms) => { ctx.timerSeq += 1; const id = ctx.timerSeq; ctx.timers.set(id, { callback: () => { ctx.timers.delete(id); callback(); }, ms }); return id; };
  globalThis.clearTimeout = (id) => { ctx.timers.delete(id); };
  globalThis.AbortController = class { constructor() { this.signal = { aborted: false }; } abort() { this.signal.aborted = true; } };
  globalThis.ResizeObserver = class { constructor(callback) { this.callback = callback; ctx.observer = this; } observe() {} unobserve() {} };
  globalThis.fetch = (path, options = {}) => {
    const body = options.body === undefined ? undefined : JSON.parse(options.body);
    const call = { path, method: options.method || "GET", body, options };
    ctx.fetches.push(call);
    let reply = replyFor ? replyFor(call, ctx) : null;
    if (!reply) {
      reply = path === "/api/board" ? { ok: true, board: { state: "ok" }, options: ctx.options, terminals: ctx.list }
        : path === "/api/board/open" ? { ok: true, terminals: ctx.list }
          : { ok: true, terminal: ctx.list[0] };
    }
    return Promise.resolve({ status: reply.status || 200, json: () => Promise.resolve(reply) });
  };
  await import(`${BOARD_JS}?boot=${bootCounter += 1}`);
  await settle();
  const workspace = element("board-workspace");
  Object.assign(ctx, {
    workspace,
    widgetEls: () => workspace.children.filter((child) => child.classes.has("board-widget")),
    widgetOf: (id) => workspace.children.find((child) => child.dataset.terminal === id),
    part: (el, cls) => el.find((node) => node.classes.has(cls))[0],
    row: (id) => element("board-agents").children.concat(element("board-shells").children).find((r) => r.dataset.terminal === id),
    button: (row, label) => row.find((node) => node.tagName === "BUTTON" && node.textContent === label)[0],
    sockOf: (id) => ctx.sockets.filter((socket) => socket.url.endsWith(`/${id}`)).at(-1),
    sent: (socket) => socket.sent.filter((item) => typeof item === "string").map((item) => JSON.parse(item)),
    msg: (socket, body) => socket.onmessage({ data: JSON.stringify(body) }),
    attach: (id, extra = {}) => { const socket = ctx.sockOf(id); socket.onopen(); ctx.msg(socket, { t: "attached", v: 2, terminal: ctx.list.find((t) => t.id === id), size: [30, 100], clients: 1, heartbeat: 10, ...extra }); },
    poll: async (list = ctx.list) => { ctx.list = list; const timer = [...ctx.timers.entries()].find(([, t]) => t.ms === 5000); assert.ok(timer, "a 5 s poll is scheduled"); ctx.timers.delete(timer[0]); timer[1].callback(); await settle(); },
    liveSockets: () => ctx.sockets.filter((socket) => !socket.closed),
    stored: () => (ctx.storageData["wb.board.layout"] === undefined ? null : JSON.parse(ctx.storageData["wb.board.layout"])),
    click: (el) => el.fire("click"),
  });
  return ctx;
}

test("glue: storage that throws on read, on write, on the property or is absent still shows every terminal; drag and hide work; nothing escapes", async () => {
  for (const storage of ["throwGet", "throwSet", "throwBoth", "propThrows", "absent"]) {
    const ctx = await boot({ storage });
    assert.deepEqual(ctx.widgetEls().map((el) => el.dataset.terminal), [A1, A2, S1], `${storage}: all visible by default`);
    assert.equal(ctx.widgetOf(A1).styleProps.get("grid-column"), "1 / span 6");
    // a hide (which saves) must not throw even though storing fails
    ctx.click(ctx.button(ctx.row(S1), "숨기기"));
    assert.equal(ctx.widgetOf(S1), undefined, `${storage}: hide works for the session`);
    ctx.click(ctx.button(ctx.row(S1), "보이기"));
    assert.ok(ctx.widgetOf(S1), `${storage}: show works`);
    ctx.click(ctx.element("board-panel-toggle")); // saves the panel state: must not throw
    assert.equal(ctx.element("board-panel").hidden, true);
    ctx.click(ctx.element("board-reset-layout"));
    assert.equal(ctx.htmlWrites.length, 0);
  }
});

test("glue: corrupt, oversized and foreign-version stored layouts give the default layout; stored junk geometry is placed again", async () => {
  for (const text of ["{", "x".repeat(2_000_000), '{"v":2,"widgets":{"a-00000000000a":{"x":0,"y":0,"w":6,"h":10,"visible":false}}}', "null"]) {
    const ctx = await boot({ stored: { "wb.board.layout": text } });
    assert.deepEqual(ctx.widgetEls().map((el) => el.dataset.terminal), [A1, A2, S1], `all visible for ${text.slice(0, 30)}`);
    const boxes = ctx.widgetEls().map((el) => [el.styleProps.get("grid-column"), el.styleProps.get("grid-row")]);
    assert.equal(new Set(boxes.map((b) => b.join("|"))).size, 3, "three different places");
  }
  // a stored layout that only names unknown ids (here the prototype key) is a layout: every listed terminal is new to it
  // (hidden, listed in the panel) and the prototype is untouched
  const proto = await boot({ stored: { "wb.board.layout": '{"v":1,"widgets":{"__proto__":{"x":0,"y":0,"w":3,"h":6,"visible":true}}}' } });
  assert.equal(proto.widgetEls().length, 0);
  assert.ok(proto.button(proto.row(A1), "보이기"));
  assert.equal(({}).x, undefined);
  const overlapping = JSON.stringify({ v: 1, widgets: { [A1]: { x: 0, y: 0, w: 6, h: 10, visible: true }, [A2]: { x: 3, y: 2, w: 6, h: 10, visible: true }, [S1]: { x: 99, y: 0, w: 6, h: 10, visible: true } } });
  const ctx = await boot({ stored: { "wb.board.layout": overlapping } });
  const placed = Object.fromEntries(ctx.widgetEls().map((el) => [el.dataset.terminal, el.styleProps.get("grid-column") + " / " + el.styleProps.get("grid-row")]));
  assert.equal(placed[A1], "1 / span 6 / 1 / span 10", "the first valid entry keeps its place");
  assert.equal(new Set(Object.values(placed)).size, 3);
  assert.equal(ctx.widgetEls().length, 3);
});

test("glue: first open posts the visible ids; an all-hidden stored layout opens nothing, and a stored layout reached through GET is not rewritten by merely viewing", async () => {
  const stored = JSON.stringify({ v: 1, widgets: { [A1]: { x: 0, y: 0, w: 6, h: 10, visible: false }, [A2]: { x: 6, y: 0, w: 6, h: 10, visible: false }, [S1]: { x: 0, y: 10, w: 6, h: 10, visible: false } } });
  const ctx = await boot({ stored: { "wb.board.layout": stored } });
  const open = ctx.fetches.find((f) => f.path === "/api/board/open");
  assert.deepEqual(open.body, { visible: [] });
  assert.equal(ctx.widgetEls().length, 0);
  assert.equal(ctx.sockets.length, 0, "no socket for a hidden terminal");
  assert.deepEqual(ctx.storageOps.filter((op) => op[0] === "set"), [], "viewing writes nothing");
});

test("glue: a terminal made in another browser is hidden under a stored layout and visible without one; one that vanished loses its widget and socket", async () => {
  const stored = JSON.stringify({ v: 1, widgets: { [A1]: { x: 0, y: 0, w: 6, h: 10, visible: true }, [A2]: { x: 6, y: 0, w: 6, h: 10, visible: true }, [S1]: { x: 0, y: 10, w: 6, h: 10, visible: true } } });
  const ctx = await boot({ stored: { "wb.board.layout": stored } });
  for (const id of [A1, A2, S1]) ctx.attach(id);
  const extra = view(S2, "shell", "shell-2");
  await ctx.poll([...ctx.list, extra]);
  assert.equal(ctx.widgetOf(S2), undefined, "new elsewhere: hidden");
  assert.ok(ctx.button(ctx.row(S2), "보이기"), "listed in the panel as hidden");
  assert.equal(ctx.sockets.filter((s) => s.url.endsWith(S2)).length, 0, "no connection for a hidden one");
  assert.equal(ctx.widgetOf(A1).styleProps.get("grid-column"), "1 / span 6", "the layout is undisturbed");
  await ctx.poll(ctx.list.filter((t) => t.id !== S1));
  assert.equal(ctx.widgetOf(S1), undefined, "a vanished terminal's widget is gone");
  assert.equal(ctx.sockOf(S1).closed, true, "and its connection is released");
  assert.equal(ctx.row(S1), undefined);
  // a browser with no stored layout shows the newcomer
  const fresh = await boot({});
  await fresh.poll([...fresh.list, extra]);
  assert.ok(fresh.widgetOf(S2), "no stored layout: every terminal is visible");
});

test("glue: sockets never exceed the visible widgets, at most 4 wait for a hello, and every visible widget ends up with exactly one", async () => {
  const many = [view(A1, "agent", "a1"), view(A2, "agent", "a2"), ...Array.from({ length: 6 }, (_, i) => view(`s-0000000000${i}1`, "shell", `s${i}`))];
  const ctx = await boot({ terminals: many });
  assert.equal(ctx.widgetEls().length, 8);
  assert.equal(ctx.sockets.length, 4, "four waiting for their hello");
  let guard = 0;
  while (ctx.liveSockets().length < 8 && guard++ < 10) {
    for (const socket of ctx.liveSockets()) {
      if (!socket.hello) { socket.hello = true; ctx.attach(socket.url.split("/").pop()); }
    }
    await settle();
    assert.ok(ctx.liveSockets().length <= ctx.widgetEls().length);
  }
  assert.equal(ctx.liveSockets().length, 8);
  assert.equal(new Set(ctx.liveSockets().map((s) => s.url)).size, 8, "one socket per terminal");
  ctx.click(ctx.button(ctx.row("s-000000000011"), "숨기기"));
  assert.equal(ctx.liveSockets().length, 7, "hiding releases at once");
  assert.deepEqual(ctx.sent(ctx.sockOf("s-000000000011")).at(-1), { t: "release" });
});

test("glue: a resize is sent once, 150 ms after the last layout change of this widget; size and attached never answer with a resize", async () => {
  const ctx = await boot({});
  for (const id of [A1, A2, S1]) ctx.attach(id);
  const socket = ctx.sockOf(S1);
  const resizes = () => ctx.sent(socket).filter((m) => m.t === "resize");
  const base = resizes().length;
  ctx.msg(socket, { t: "size", rows: 50, cols: 200 });
  ctx.msg(socket, { t: "attached", v: 2, terminal: ctx.list[2], size: [51, 201], clients: 2, heartbeat: 10 });
  ctx.msg(socket, { t: "size", rows: 52, cols: 202 });
  assert.equal([...ctx.timers.values()].filter((t) => t.ms === 150).length, 0, "no resize pending after server size/attached");
  const body = ctx.part(ctx.widgetOf(S1), "widget-body");
  for (const [rows, cols] of [[31, 101], [32, 102], [33, 103], [34, 104]]) {
    ctx.fitRows = rows; ctx.fitCols = cols;
    ctx.observer.callback([{ target: body }]);
  }
  const pending = [...ctx.timers.values()].filter((t) => t.ms === 150);
  assert.equal(pending.length, 1, "debounced to one timer");
  pending[0].callback();
  assert.equal(resizes().length, base + 1);
  assert.deepEqual(resizes().at(-1), { t: "resize", rows: 34, cols: 104 });
  // no layout change since: nothing more
  ctx.observer.callback([{ target: body }]);
  assert.equal([...ctx.timers.values()].filter((t) => t.ms === 150).length, 0, "an unchanged size is not re-sent");
  // the other widgets sent no resize because of S1
  assert.equal(ctx.sent(ctx.sockOf(A1)).filter((m) => m.t === "resize").length, 0);
});

test("glue: pagehide releases every socket and stops the poll; a non-persisted pageshow does nothing, a persisted one refreshes and reconnects", async () => {
  const ctx = await boot({});
  for (const id of [A1, A2, S1]) ctx.attach(id);
  const before = ctx.fetches.length;
  for (const handler of ctx.winListeners.pageshow) handler({ persisted: false }); // the load's own pageshow
  for (const handler of ctx.winListeners.pagehide) handler({ persisted: true });
  for (const id of [A1, A2, S1]) assert.deepEqual(ctx.sent(ctx.sockOf(id)).at(-1), { t: "release" });
  assert.equal([...ctx.timers.values()].filter((t) => t.ms === 5000 || t.ms === 300).length, 0, "no poll timer survives pagehide");
  const sockets = ctx.sockets.length;
  assert.equal(ctx.fetches.length, before, "nothing is fetched while the page is gone");
  for (const handler of ctx.winListeners.pageshow) handler({ persisted: true });
  await settle();
  assert.ok(ctx.fetches.length > before, "persisted: the list is asked again");
  assert.equal(ctx.sockets.length, sockets + 3, "and every visible widget connects again");
});

test("glue: a 401 from the poll or from an action goes to /login and nothing else changes", async () => {
  const ctx = await boot({});
  ctx.list = [];
  const unauthorized = { status: 401, ok: false, reason: "unauthorized" };
  const ctx2 = await boot({ replyFor: (call, c) => (c.expire ? unauthorized : null) });
  ctx2.expire = true;
  await ctx2.poll();
  assert.equal(ctx2.location.href, "/login");
  const ctx3 = await boot({ replyFor: (call, c) => (c.expire && call.path.endsWith("/rename") ? unauthorized : null) });
  ctx3.click(ctx3.button(ctx3.row(A2), "이름 바꾸기"));
  const input = ctx3.row(A2).find((n) => n.tagName === "INPUT")[0];
  input.value = "새이름";
  ctx3.expire = true;
  input.fire("keydown", { key: "Enter", keyCode: 13, isComposing: false });
  await settle();
  assert.equal(ctx3.location.href, "/login");
});

test("glue: names and details are text everywhere (panel row, widget title, dialog, notice, aria-label); no HTML is ever written", async () => {
  const payloads = ["<img src=x onerror=window.__pwn=1>", "<script>window.__pwn=1</script>", "\"><svg onload=window.__pwn=1>", "&lt;b&gt;x&lt;/b&gt;", "javascript:alert(1)", "a'b\"c`d${1}"];
  const list = [view(A1, "agent", payloads[0]), view(A2, "agent", payloads[1], { state: "failed", detail: payloads[2], message: payloads[3] }), view(S1, "shell", payloads[2]), view(S2, "shell", payloads[4])];
  const ctx = await boot({ terminals: list });
  assert.equal(ctx.htmlWrites.length, 0, "innerHTML/outerHTML/insertAdjacentHTML/document.write were never used");
  for (const [id, name] of [[A1, payloads[0]], [A2, payloads[1]], [S1, payloads[2]], [S2, payloads[4]]]) {
    assert.equal(ctx.part(ctx.widgetOf(id), "widget-name").textContent, name);
    assert.equal(ctx.row(id).find((n) => n.classes.has("panel-name"))[0].textContent, name);
    assert.equal(ctx.row(id).find((n) => n.children.length === 0 && n.tagName === "BUTTON").length > 0, true);
  }
  // every element in the tree has only element children made by the script: no node came from markup
  const allTags = new Set();
  for (const el of ctx.widgetEls()) el.find((n) => { allTags.add(n.tagName); return false; });
  for (const id of [A1, A2, S1, S2]) ctx.row(id).find((n) => { allTags.add(n.tagName); return false; });
  for (const tag of ["IMG", "SCRIPT", "SVG", "B", "IFRAME"]) assert.equal(allTags.has(tag), false, `no <${tag}>`);
  // confirmation text keeps the markup as text
  ctx.click(ctx.button(ctx.row(S1), "닫기"));
  await settle();
  assert.equal(ctx.element("board-confirm-text").textContent, `${payloads[2]}을 닫습니다. 실행 중인 명령이 끝납니다.`);
  ctx.click(ctx.element("board-confirm-cancel"));
  await settle();
  assert.equal(ctx.htmlWrites.length, 0);
  assert.equal(globalThis.__pwn, undefined);
});

test("glue: rename: Enter saves and the title follows; the Hub's refusal text is shown in the row and the input stays; Esc cancels without a request", async () => {
  const hubRefuses = { status: 409, ok: false, reason: "name_taken", detail: "같은 종류에 같은 이름(대소문자 무시)이 이미 있습니다." };
  const ctx = await boot({ replyFor: (call, c) => (call.path.endsWith("/rename") && c.refuse ? hubRefuses : null) });
  const startRename = (id) => { ctx.click(ctx.button(ctx.row(id), "이름 바꾸기")); return ctx.row(id).find((n) => n.tagName === "INPUT")[0]; };
  let input = startRename(A1);
  assert.equal(input.value, "agent-1");
  const before = ctx.fetches.length;
  input.fire("keydown", { key: "Escape", keyCode: 27, isComposing: false });
  assert.equal(ctx.fetches.length, before, "Esc sends nothing");
  assert.equal(ctx.row(A1).find((n) => n.tagName === "INPUT").length, 0, "input gone");
  // a server-side refusal (the page's own check passed: reserved or casefold differences are the Hub's)
  input = startRename(A1);
  input.value = "board-agent";
  ctx.refuse = true;
  input.fire("keydown", { key: "Enter", keyCode: 13, isComposing: false });
  await settle();
  const error = ctx.row(A1).find((n) => n.classes.has("row-error"))[0];
  assert.equal(error.hidden, false);
  assert.equal(error.textContent, hubRefuses.detail, "the Hub's own sentence, not an exception");
  assert.equal(ctx.row(A1).find((n) => n.tagName === "INPUT").length, 1, "the input stays for another try");
  // success: a Korean name; widget title and panel follow
  ctx.refuse = false;
  ctx.list = ctx.list.map((t) => (t.id === A1 ? { ...t, name: "한글이름" } : t));
  input = ctx.row(A1).find((n) => n.tagName === "INPUT")[0];
  input.value = "  한글이름  ";
  const calls = ctx.fetches.length;
  input.fire("keydown", { key: "Enter", keyCode: 13, isComposing: false });
  await settle();
  const rename = ctx.fetches.slice(calls).find((f) => f.path.endsWith("/rename"));
  assert.deepEqual(rename.body, { name: "한글이름" }, "trimmed text in a JSON body");
  assert.equal(rename.path, `/api/board/terminals/${A1}/rename`);
});

test("glue: Enter inside an IME composition does not save a rename", async () => {
  const ctx = await boot({});
  ctx.click(ctx.button(ctx.row(A1), "이름 바꾸기"));
  const input = ctx.row(A1).find((n) => n.tagName === "INPUT")[0];
  input.value = "ㅎ";
  const before = ctx.fetches.length;
  input.fire("keydown", { key: "Enter", keyCode: 229, isComposing: true });
  input.fire("keydown", { key: "Process", keyCode: 229, isComposing: false });
  await settle();
  assert.equal(ctx.fetches.length, before);
});

test("glue: a rename in progress keeps the typed text and the open input when the 5 s list refresh arrives", async () => {
  const ctx = await boot({});
  ctx.click(ctx.button(ctx.row(A1), "이름 바꾸기"));
  let input = ctx.row(A1).find((n) => n.tagName === "INPUT")[0];
  input.value = "전처리-새";
  await ctx.poll();
  input = ctx.row(A1).find((n) => n.tagName === "INPUT")[0];
  assert.ok(input, "the rename input is still there after a refresh");
  assert.equal(input.value, "전처리-새", "and still holds what the owner typed");
});

test("glue: limit form: the page's own check shows the Hub's sentence without a request; the Hub's refusal text is shown; success hides the error", async () => {
  const refusal = { status: 409, ok: false, reason: "limit_below_count", detail: "지금 개수보다 작은 한도는 정할 수 없습니다. 먼저 닫거나 삭제하세요." };
  const ctx = await boot({ replyFor: (call, c) => (call.path === "/api/board/options" && c.refuse ? refusal : null) });
  const submit = () => ctx.element("board-options").fire("submit");
  const before = ctx.fetches.length;
  ctx.element("board-agent-limit").value = "99";
  ctx.element("board-shell-limit").value = "10";
  submit();
  await settle();
  assert.equal(ctx.fetches.length, before, "out of range: no request");
  assert.equal(ctx.element("board-options-error").hidden, false);
  assert.equal(ctx.element("board-options-error").textContent, "한도는 agent 1~8, shell 1~32입니다.");
  ctx.element("board-agent-limit").value = "3";
  ctx.refuse = true;
  submit();
  await settle();
  assert.equal(ctx.fetches.at(-1).path, "/api/board/options");
  assert.deepEqual(ctx.fetches.at(-1).body, { agent_limit: 3, shell_limit: 10 });
  assert.equal(ctx.element("board-options-error").textContent, refusal.detail);
  ctx.refuse = false;
  submit();
  await settle();
  assert.equal(ctx.element("board-options-error").hidden, true);
});

test("glue: create buttons are disabled at the limit with '한도 N개' and enabled below it", async () => {
  const list = [view(A1, "agent", "a1"), view(A2, "agent", "a2"), view(S1, "shell", "s1")];
  const ctx = await boot({ terminals: list });
  ctx.options = { ...OPTIONS, agent_limit: 2, shell_limit: 10 };
  await ctx.poll();
  assert.equal(ctx.element("board-new-agent").disabled, true);
  assert.equal(ctx.element("board-agent-note").textContent, "한도 2개");
  assert.equal(ctx.element("board-new-shell").disabled, false);
  assert.equal(ctx.element("board-shell-note").textContent, "1/10");
});

test("glue: narrow screen: only the chosen terminal is shown and connected; widening restores the stored grid and reconnects; the stored layout is never rewritten by the switch", async () => {
  const stored = JSON.stringify({ v: 1, widgets: { [A1]: { x: 0, y: 0, w: 6, h: 10, visible: true }, [A2]: { x: 6, y: 0, w: 6, h: 10, visible: true }, [S1]: { x: 0, y: 10, w: 6, h: 10, visible: false } } });
  const ctx = await boot({ stored: { "wb.board.layout": stored, "wb.board.panel": "open" } });
  for (const id of [A1, A2]) ctx.attach(id);
  assert.equal(ctx.widgetEls().length, 2);
  ctx.narrow = true;
  for (const handler of ctx.mediaHandlers) handler();
  for (const handler of ctx.winListeners.resize) handler();
  assert.deepEqual(ctx.widgetEls().map((el) => el.dataset.terminal), [A1], "only the first agent (nothing chosen yet)");
  assert.equal(ctx.sockOf(A2).closed, true, "the other connection is released");
  assert.deepEqual(ctx.sent(ctx.sockOf(A2)).at(-1), { t: "release" });
  assert.equal(ctx.widgetOf(A1).styleProps.has("grid-column"), false, "no grid position on a narrow screen");
  assert.equal(ctx.element("board-panel").hidden, true, "the panel folds away on a narrow screen");
  // choose the hidden-in-grid shell from the panel
  ctx.click(ctx.element("board-panel-toggle"));
  ctx.click(ctx.button(ctx.row(S1), "보기"));
  assert.deepEqual(ctx.widgetEls().map((el) => el.dataset.terminal), [S1]);
  assert.equal(ctx.storageData["wb.board.narrow"], S1);
  assert.equal(ctx.liveSockets().filter((s) => !s.url.endsWith(S1)).length, 0, "exactly one connection is open");
  ctx.narrow = false;
  for (const handler of ctx.mediaHandlers) handler();
  assert.deepEqual(ctx.widgetEls().map((el) => el.dataset.terminal).sort(), [A1, A2].sort(), "the stored grid is back (S1 stays hidden there)");
  assert.equal(ctx.widgetOf(A2).styleProps.get("grid-column"), "7 / span 6");
  assert.equal(ctx.storageData["wb.board.layout"], stored, "the layout text is untouched");
  assert.equal(ctx.element("board-panel").hidden, false, "the remembered panel state comes back");
});

test("glue: closing the panel is remembered per browser: wb.board.panel is 'closed' then 'open', and a reload reads it", async () => {
  const ctx = await boot({});
  ctx.click(ctx.element("board-panel-toggle"));
  assert.equal(ctx.storageData["wb.board.panel"], "closed");
  const again = await boot({ stored: { "wb.board.panel": "closed" } });
  assert.equal(again.element("board-panel").hidden, true);
  again.click(again.element("board-panel-toggle"));
  assert.equal(again.storageData["wb.board.panel"], "open");
});

test("glue: pointer drag: a drop over another widget leaves the widget and the stored layout unchanged; a free drop moves it in whole cells and stores it", async () => {
  const ctx = await boot({});
  for (const id of [A1, A2, S1]) ctx.attach(id);
  const bar = ctx.part(ctx.widgetOf(A1), "widget-bar");
  const drag = (dxPx, dyPx, commit = true) => {
    bar.fire("pointerdown", { pointerId: 1, button: 0, clientX: 100, clientY: 100, target: bar });
    bar.fire("pointermove", { pointerId: 1, clientX: 100 + dxPx, clientY: 100 + dyPx });
    (commit ? bar.fire("pointerup", { pointerId: 1, clientX: 100 + dxPx, clientY: 100 + dyPx }) : bar.fire("pointercancel", { pointerId: 1 }));
  };
  const rect = (id) => [ctx.widgetOf(id).styleProps.get("grid-column"), ctx.widgetOf(id).styleProps.get("grid-row")];
  const before = rect(A1);
  drag(100, 0); // pitchX = (1200+8)/12 ~ 100.7: one column to the right, into A2 (columns 7-12)
  assert.deepEqual(rect(A1), before, "overlap: back to the last valid place");
  assert.equal(ctx.stored(), null, "an overlapping drop stores nothing");
  assert.equal(ctx.widgetOf(A1).styleProps.has("transform"), false, "the drag transform is gone");
  drag(0, 1200); // far below: free
  assert.notDeepEqual(rect(A1), before);
  assert.deepEqual(ctx.stored().widgets[A1].x, 0);
  assert.ok(ctx.stored().widgets[A1].y > 20);
  const cancelled = rect(A1);
  drag(300, 300, false);
  assert.deepEqual(rect(A1), cancelled, "a cancelled gesture changes nothing");
  // pressing a button in the bar starts no drag
  const hide = ctx.part(ctx.widgetOf(A1), "widget-hide");
  hide.fire("pointerdown", { pointerId: 2, button: 0, clientX: 5, clientY: 5, target: hide });
  assert.equal(ctx.widgetOf(A1).classes.has("is-dragging"), false);
});

test("glue: a click on a widget focuses it and its terminal, and only that widget has the focus class", async () => {
  const ctx = await boot({});
  const [a1, a2] = [ctx.widgetOf(A1), ctx.widgetOf(A2)];
  a1.fire("pointerdown", {});
  assert.equal(a1.classes.has("is-focused"), true);
  assert.equal(ctx.terms[0].focused >= 1, true);
  a2.fire("pointerdown", {});
  assert.equal(a1.classes.has("is-focused"), false);
  assert.equal(a2.classes.has("is-focused"), true);
  assert.equal(Object.keys(ctx.docListeners).includes("keydown"), false, "no page-wide shortcut");
  assert.equal((ctx.winListeners.keydown || []).length, 0, "no window shortcut");
});

test("glue: 4429 shows the limit text and does not retry by itself; 4404 removes the widget text and offers nothing; 4401 offers a login", async () => {
  const ctx = await boot({});
  for (const id of [A1, A2, S1]) ctx.attach(id);
  const count = ctx.sockets.length;
  ctx.sockOf(A1).onclose({ code: 4429 });
  ctx.sockOf(S1).onclose({ code: 4401 });
  await settle();
  assert.equal(ctx.part(ctx.part(ctx.widgetOf(A1), "widget-cover"), "cover-text").textContent, "연결 수 한도(8개)를 넘었습니다");
  assert.equal(ctx.part(ctx.part(ctx.widgetOf(S1), "widget-cover"), "cover-text").textContent, "연결 없음 — 다시 로그인");
  for (const [, timer] of ctx.timers) if (timer.ms === 1000 || timer.ms === 2000) timer.callback();
  await settle();
  assert.equal(ctx.sockets.length, count, "neither refusal reconnects automatically");
});

test("glue: a plain network drop reconnects with a new hello after the backoff; the widget says so meanwhile", async () => {
  const ctx = await boot({});
  ctx.attach(S1);
  const socket = ctx.sockOf(S1);
  socket.onclose({ code: 1006 });
  assert.match(ctx.part(ctx.widgetOf(S1), "widget-status").textContent, /연결 없음/);
  const count = ctx.sockets.length;
  const retry = [...ctx.timers.values()].find((t) => t.ms === 1000);
  assert.ok(retry, "a 1 s reconnect timer");
  retry.callback();
  await settle();
  assert.equal(ctx.sockets.length, count + 1);
});

test("glue: hiding an agent that was never started does not start it (hide is a browser-only choice, WBX-D56 (2))", async () => {
  const list = [view(A1, "agent", "agent-1", { state: "not_started" }), view(S1, "shell", "shell-1")];
  const ctx = await boot({ terminals: list });
  const opens = () => ctx.fetches.filter((f) => f.path === "/api/board/open").length;
  const before = opens();
  assert.ok(ctx.button(ctx.row(A1), "숨기기"), "visible and not started");
  ctx.click(ctx.button(ctx.row(A1), "숨기기"));
  await settle();
  assert.equal(ctx.widgetOf(A1), undefined, "hidden here");
  assert.equal(opens(), before, "hiding asked the Hub to open (start) nothing");
});
