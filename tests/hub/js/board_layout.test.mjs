// WBX-D2 / seam S8: the pure grid, storage and reconcile logic of static/board-layout.js (no DOM, no browser).
// Expected values come from SPEC s4 "화면: workspace와 panel": 12 columns, minimum widget 3 x 6 cells, integer cell
// positions, touching widgets do not overlap, stored layout rules (v:1, junk dropped), 720 px boundary.
import test from "node:test";
import assert from "node:assert/strict";
import * as L from "../../../src/workbench/hub/web/static/board-layout.js";

const r = (x, y, w, h) => ({ x, y, w, h });

// ---- (1) snapping to cells -------------------------------------------------------------------------------------
test("the grid constants are the SPEC values: 12 columns, minimum 3 x 6 cells", () => {
  assert.equal(L.COLS, 12);
  assert.equal(L.MIN_W, 3);
  assert.equal(L.MIN_H, 6);
});

test("a pointer distance snaps to the nearest whole cell (half a cell rounds up, negative distances too)", () => {
  assert.equal(L.snapCells(0, 100), 0);
  assert.equal(L.snapCells(49, 100), 0);
  assert.equal(L.snapCells(50, 100), 1);
  assert.equal(L.snapCells(149, 100), 1);
  assert.equal(L.snapCells(151, 100), 2);
  assert.equal(L.snapCells(-49, 100), 0);
  assert.equal(L.snapCells(-51, 100), -1);
  assert.equal(L.snapCells(-151, 100), -2);
});

test("a bad pitch never produces NaN or Infinity: it snaps to 0 cells", () => {
  for (const pitch of [0, -5, NaN, undefined, Infinity]) assert.equal(L.snapCells(300, pitch), 0, String(pitch));
  assert.equal(L.snapCells(NaN, 100), 0);
});

test("the pixel pitch of a cell is the column or row size plus the gap", () => {
  // 1200 px wide, 12 columns, 8 px gap: each column (1200 - 11 * 8) / 12, pitch column + gap = (1200 + 8) / 12
  assert.equal(L.pitchOf(1200, 8, 12), (1200 + 8) / 12);
  assert.equal(L.pitchOf(0, 8, 12), 0);
});

// ---- (2) overlap ---------------------------------------------------------------------------------------------
test("rectangles that share area overlap; touching edges and corners do not", () => {
  assert.equal(L.overlaps(r(0, 0, 4, 6), r(2, 2, 4, 6)), true);
  assert.equal(L.overlaps(r(0, 0, 4, 6), r(0, 0, 4, 6)), true, "identical");
  assert.equal(L.overlaps(r(0, 0, 12, 12), r(3, 3, 3, 6)), true, "contained");
  assert.equal(L.overlaps(r(0, 0, 4, 6), r(4, 0, 4, 6)), false, "right neighbour touches");
  assert.equal(L.overlaps(r(0, 0, 4, 6), r(0, 6, 4, 6)), false, "bottom neighbour touches");
  assert.equal(L.overlaps(r(0, 0, 4, 6), r(4, 6, 4, 6)), false, "corner touches");
  assert.equal(L.overlaps(r(4, 0, 4, 6), r(0, 0, 4, 6)), false, "symmetric");
  assert.equal(L.overlaps(r(0, 0, 4, 6), r(3, 5, 4, 6)), true, "one cell of area is an overlap");
});

test("collides: a rectangle against a list, skipping the widget that is being moved", () => {
  const others = { a: r(0, 0, 4, 6), b: r(4, 0, 4, 6) };
  assert.equal(L.collides(r(2, 0, 4, 6), others), true);
  assert.equal(L.collides(r(8, 0, 4, 6), others), false);
  assert.equal(L.collides(r(0, 0, 4, 6), others, "a"), false, "itself is skipped");
  assert.equal(L.collides(r(2, 0, 4, 6), others, "a"), true, "still hits b");
  assert.equal(L.collides(r(0, 0, 4, 6), {}), false);
});

test("rectangles of hidden widgets are not obstacles: only visible entries are passed in by obstacles()", () => {
  const widgets = { a: { ...r(0, 0, 4, 6), visible: true }, b: { ...r(4, 0, 4, 6), visible: false } };
  assert.deepEqual(Object.keys(L.obstacles(widgets)), ["a"]);
  assert.deepEqual(Object.keys(L.obstacles(widgets, "a")), [], "except the widget itself");
});

// ---- (3) automatic placement ---------------------------------------------------------------------------------
test("the default widget is 6 x 10 cells; the first one goes to the top left", () => {
  assert.deepEqual([L.DEFAULT_W, L.DEFAULT_H], [6, 10]);
  assert.deepEqual(L.findSpot({}), r(0, 0, 6, 10));
});

test("the next widget takes the first free place scanning rows from the top, columns from the left", () => {
  const placed = { a: r(0, 0, 6, 10) };
  assert.deepEqual(L.findSpot(placed), r(6, 0, 6, 10), "beside the first");
  placed.b = r(6, 0, 6, 10);
  assert.deepEqual(L.findSpot(placed), r(0, 10, 6, 10), "a full row: the next row, touching, not overlapping");
  placed.c = r(0, 10, 6, 10);
  assert.deepEqual(L.findSpot(placed), r(6, 10, 6, 10));
});

test("a gap on the top row is filled when it is big enough, and skipped when it is too small", () => {
  assert.deepEqual(L.findSpot({ a: r(0, 0, 3, 10), b: r(9, 0, 3, 10) }, 6, 10), r(3, 0, 6, 10));
  assert.deepEqual(L.findSpot({ a: r(0, 0, 3, 10), b: r(8, 0, 4, 10) }, 6, 10), r(0, 10, 6, 10), "5 columns left: too small");
});

test("a size is clamped into the grid before looking for a place (never wider than 12, never under the minimum)", () => {
  assert.deepEqual(L.findSpot({}, 20, 3), r(0, 0, 12, 6));
  assert.deepEqual(L.findSpot({}, 1, 1), r(0, 0, 3, 6));
});

test("autoPlace puts every id in order, agents first as given, none overlapping, all visible", () => {
  const widgets = L.autoPlace(["a-1", "a-2", "s-1", "s-2", "s-3"]);
  assert.deepEqual(Object.keys(widgets), ["a-1", "a-2", "s-1", "s-2", "s-3"]);
  assert.deepEqual({ ...widgets["a-1"] }, { ...r(0, 0, 6, 10), visible: true });
  assert.deepEqual({ ...widgets["a-2"] }, { ...r(6, 0, 6, 10), visible: true });
  assert.deepEqual({ ...widgets["s-1"] }, { ...r(0, 10, 6, 10), visible: true });
  const list = Object.values(widgets);
  for (let i = 0; i < list.length; i += 1) {
    for (let j = i + 1; j < list.length; j += 1) assert.equal(L.overlaps(list[i], list[j]), false, `${i} ${j}`);
  }
});

// ---- (4) move and resize checks, revert ----------------------------------------------------------------------
test("moveRect shifts by whole cells and keeps the widget inside the 12 columns and below row 0", () => {
  const origin = r(2, 3, 4, 6);
  assert.deepEqual(L.moveRect(origin, 1, 2), r(3, 5, 4, 6));
  assert.deepEqual(L.moveRect(origin, -5, -9), r(0, 0, 4, 6), "not left of column 0 or above row 0");
  assert.deepEqual(L.moveRect(origin, 20, 0), r(8, 3, 4, 6), "right edge at column 12");
  assert.deepEqual(L.moveRect(origin, 0, 0), origin);
});

test("resizeRect changes w and h from the bottom right corner, never under 3 x 6 or past column 12; x and y stay", () => {
  const origin = r(2, 3, 4, 6);
  assert.deepEqual(L.resizeRect(origin, 2, 3), r(2, 3, 6, 9));
  assert.deepEqual(L.resizeRect(origin, -9, -9), r(2, 3, 3, 6), "minimum size");
  assert.deepEqual(L.resizeRect(origin, 50, 0), r(2, 3, 10, 6), "right edge at column 12");
});

test("settle accepts a free place and sends an overlapping or invalid one back to the last valid place", () => {
  const widgets = { a: { ...r(0, 0, 6, 10), visible: true }, b: { ...r(6, 0, 6, 10), visible: true } };
  const free = L.settle("a", r(0, 10, 6, 10), widgets);
  assert.deepEqual(free, { ok: true, rect: r(0, 10, 6, 10) });
  const hit = L.settle("a", r(3, 0, 6, 10), widgets);
  assert.deepEqual(hit, { ok: false, rect: r(0, 0, 6, 10) }, "back to where a was");
  const touching = L.settle("a", r(0, 0, 6, 11), widgets);
  assert.equal(touching.ok, true, "touching b is not overlapping");
  const outside = L.settle("a", r(8, 0, 6, 10), widgets);
  assert.deepEqual(outside, { ok: false, rect: r(0, 0, 6, 10) }, "past column 12");
  const small = L.settle("a", r(0, 0, 2, 10), widgets);
  assert.equal(small.ok, false, "under the minimum width");
});

test("a hidden widget does not block a drop", () => {
  const widgets = { a: { ...r(0, 0, 6, 10), visible: true }, b: { ...r(6, 0, 6, 10), visible: false } };
  assert.equal(L.settle("a", r(6, 0, 6, 10), widgets).ok, true);
});

// ---- (5) browser storage: parse, check, serialize, exceptions -------------------------------------------------
const stored = (widgets, v = 1) => JSON.stringify({ v, widgets });
const E = (x, y, w, h, visible = true) => ({ x, y, w, h, visible });

test("the three storage keys are wb.board.layout, wb.board.panel and wb.board.narrow", () => {
  assert.deepEqual([L.KEY_LAYOUT, L.KEY_PANEL, L.KEY_NARROW], ["wb.board.layout", "wb.board.panel", "wb.board.narrow"]);
});

test("serialize writes {v:1, widgets:{id:{x,y,w,h,visible}}} and parse reads it back unchanged", () => {
  const widgets = { "a-0a1": E(0, 0, 6, 10), "s-0b2": E(6, 0, 4, 8, false) };
  const text = L.serializeLayout(widgets);
  assert.deepEqual(JSON.parse(text), { v: 1, widgets });
  assert.deepEqual(L.parseLayout(text), widgets);
});

test("parse refuses text that is not a layout: null for absent, empty, non-JSON, other versions and wrong shapes", () => {
  for (const bad of [null, undefined, "", "not json", "[]", "null", "42", '"x"', stored({}, 2), stored({}, "1"),
    JSON.stringify({ widgets: {} }), JSON.stringify({ v: 1 }), JSON.stringify({ v: 1, widgets: [] }),
    JSON.stringify({ v: 1, widgets: null }), "{".repeat(5000), 12345]) {
    assert.equal(L.parseLayout(bad), null, JSON.stringify(bad)?.slice(0, 30));
  }
});

test("parse keeps a valid entry and sends an entry with junk geometry to automatic placement (position dropped)", () => {
  const junk = [
    E(-1, 0, 4, 6), E(0, -1, 4, 6), E(0, 0, 2, 6), E(0, 0, 4, 5), E(10, 0, 4, 6), E(0.5, 0, 4, 6),
    { x: "0", y: 0, w: 4, h: 6, visible: true }, { x: 0, y: 0, w: 4, h: 6 }, { x: null, y: 0, w: 4, h: 6, visible: true },
    E(0, 0, 4, 1e9), E(0, 0, Infinity, 6), { y: 0, w: 4, h: 6, visible: true }, "text", 7, null, [],
  ];
  for (const entry of junk) {
    const parsed = L.parseLayout(stored({ "a-1": E(0, 0, 4, 6), "s-2": entry }));
    assert.deepEqual(parsed["a-1"], E(0, 0, 4, 6), "the good neighbour is kept");
    assert.ok(parsed["s-2"], JSON.stringify(entry));
    assert.equal("x" in parsed["s-2"], false, `placed again, not ${JSON.stringify(entry)}`);
  }
});

test("a junk entry keeps its hidden flag; a flag that is not a boolean counts as visible", () => {
  assert.deepEqual(L.parseLayout(stored({ "a-1": { x: -3, y: 0, w: 4, h: 6, visible: false } })), { "a-1": { visible: false } });
  assert.deepEqual(L.parseLayout(stored({ "a-1": { x: -3, y: 0, w: 4, h: 6, visible: "no" } })), { "a-1": { visible: true } });
  assert.deepEqual(L.parseLayout(stored({ "a-1": { x: 0, y: 0, w: 4, h: 6, visible: 0 } })), { "a-1": E(0, 0, 4, 6, true) });
});

test("visible entries that overlap are not trusted: the later one is sent to automatic placement; touching is fine", () => {
  const parsed = L.parseLayout(stored({ "a-1": E(0, 0, 6, 10), "a-2": E(3, 3, 6, 10), "a-3": E(6, 0, 6, 10) }));
  assert.deepEqual(parsed["a-1"], E(0, 0, 6, 10));
  assert.deepEqual(parsed["a-2"], { visible: true });
  assert.deepEqual(parsed["a-3"], E(6, 0, 6, 10), "touches a-1: valid");
  const hidden = L.parseLayout(stored({ "a-1": E(0, 0, 6, 10), "a-2": E(3, 3, 6, 10, false) }));
  assert.deepEqual(hidden["a-2"], E(3, 3, 6, 10, false), "a hidden entry overlapping a visible one is fine");
});

test("ids that are not Board terminal ids are dropped (__proto__, long or odd keys never become entries)", () => {
  const text = '{"v":1,"widgets":{"__proto__":{"x":0,"y":0,"w":4,"h":6,"visible":true},"constructor":{"x":0,"y":0,"w":4,"h":6,"visible":true},"a-0f":{"x":0,"y":0,"w":4,"h":6,"visible":true}}}';
  const parsed = L.parseLayout(text);
  assert.deepEqual(Object.keys(parsed), ["a-0f"]);
  assert.equal(({}).x, undefined, "Object.prototype is untouched");
});

function memoryStorage(initial = {}) {
  const data = { ...initial };
  return { data, getItem: (key) => (key in data ? data[key] : null), setItem: (key, value) => { data[key] = String(value); } };
}
const throwing = () => { throw new Error("SecurityError"); };

test("loadLayout/saveLayout round trip through storage; an absent key is null", () => {
  const storage = memoryStorage();
  assert.equal(L.loadLayout(storage), null);
  const widgets = { "a-1": E(2, 3, 5, 7) };
  assert.equal(L.saveLayout(storage, widgets), true);
  assert.deepEqual(L.loadLayout(storage), widgets);
  assert.equal(storage.data["wb.board.layout"], L.serializeLayout(widgets));
});

test("a storage that is missing, blocked or throwing on read or write never throws: defaults and false", () => {
  const blocked = { getItem: throwing, setItem: throwing };
  for (const storage of [null, undefined, {}, blocked]) {
    assert.equal(L.loadLayout(storage), null);
    assert.equal(L.saveLayout(storage, { "a-1": E(0, 0, 4, 6) }), false);
    assert.equal(L.loadPanel(storage), "open");
    assert.equal(L.savePanel(storage, "closed"), false);
    assert.equal(L.loadNarrow(storage), null);
    assert.equal(L.saveNarrow(storage, "a-1"), false);
  }
  const quota = { getItem: () => null, setItem: () => { throw new Error("QuotaExceededError"); } };
  assert.equal(L.saveLayout(quota, { "a-1": E(0, 0, 4, 6) }), false);
});

test("browserStorage returns localStorage or null when merely reading the property throws", () => {
  const ok = { localStorage: memoryStorage() };
  assert.equal(L.browserStorage(ok), ok.localStorage);
  const bad = {};
  Object.defineProperty(bad, "localStorage", { get: throwing });
  assert.equal(L.browserStorage(bad), null);
  assert.equal(L.browserStorage({}), null);
  assert.equal(L.browserStorage(null), null);
});

test("the panel is open unless the stored value is exactly closed; corrupt values mean open", () => {
  const storage = memoryStorage();
  assert.equal(L.loadPanel(storage), "open");
  L.savePanel(storage, "closed");
  assert.equal(storage.data["wb.board.panel"], "closed");
  assert.equal(L.loadPanel(storage), "closed");
  L.savePanel(storage, "open");
  assert.equal(L.loadPanel(storage), "open");
  assert.equal(L.loadPanel(memoryStorage({ "wb.board.panel": "maybe" })), "open");
  assert.equal(L.savePanel(storage, "maybe"), false, "only open or closed are stored");
});

test("the narrow choice is the stored terminal id, null when absent", () => {
  const storage = memoryStorage();
  L.saveNarrow(storage, "s-0c3");
  assert.equal(storage.data["wb.board.narrow"], "s-0c3");
  assert.equal(L.loadNarrow(storage), "s-0c3");
  assert.equal(L.loadNarrow(memoryStorage({ "wb.board.narrow": "" })), null);
});

// ---- (6) reconcile with the terminal list ---------------------------------------------------------------------
const T = (id) => ({ id, kind: id[0] === "a" ? "agent" : "shell" });

test("nothing stored: every terminal is placed automatically, agents first, all visible", () => {
  const widgets = L.reconcile(null, [T("a-1"), T("a-2"), T("s-1")]);
  assert.deepEqual(widgets, L.autoPlace(["a-1", "a-2", "s-1"]));
  assert.ok(Object.values(widgets).every((entry) => entry.visible));
});

test("stored layout: ids that are gone are dropped, kept ids keep place and visibility", () => {
  const saved = { "a-1": E(0, 0, 6, 10), "s-1": E(6, 0, 4, 8, false), "s-9": E(0, 10, 6, 10) };
  const widgets = L.reconcile(saved, [T("a-1"), T("s-1")]);
  assert.deepEqual(widgets, { "a-1": E(0, 0, 6, 10), "s-1": E(6, 0, 4, 8, false) });
});

test("stored layout: a terminal that is new here is hidden, and does not disturb the visible ones", () => {
  const saved = { "a-1": E(0, 0, 6, 10), "s-1": E(6, 0, 6, 10) };
  const widgets = L.reconcile(saved, [T("a-1"), T("a-2"), T("s-1")]);
  assert.equal(widgets["a-2"].visible, false);
  assert.deepEqual(widgets["a-1"], saved["a-1"]);
  assert.deepEqual(widgets["s-1"], saved["s-1"]);
  assert.deepEqual(Object.keys(widgets), ["a-1", "a-2", "s-1"], "terminal order");
  assert.ok(L.inGrid(widgets["a-2"]), "it still has a valid place to show up in");
});

test("a terminal made in this browser is placed automatically in a free spot and shown", () => {
  const saved = { "a-1": E(0, 0, 6, 10) };
  const widgets = L.reconcile(saved, [T("a-1"), T("s-2")], ["s-2"]);
  assert.deepEqual(widgets["s-2"], E(6, 0, 6, 10));
  assert.deepEqual(widgets["a-1"], saved["a-1"]);
});

test("parse leftovers (no geometry) are placed in a free spot, keeping their hidden flag", () => {
  const saved = L.parseLayout(stored({ "a-1": E(0, 0, 6, 10), "s-1": E(-4, 0, 6, 10), "s-2": { x: 9, y: 0, w: 6, h: 6, visible: false } }));
  const widgets = L.reconcile(saved, [T("a-1"), T("s-1"), T("s-2")]);
  assert.deepEqual(widgets["s-1"], E(6, 0, 6, 10), "placed beside a-1");
  assert.equal(widgets["s-2"].visible, false);
  assert.ok(L.inGrid(widgets["s-2"]));
});

test("reconcile never returns overlapping visible widgets, even from a bad in-memory map", () => {
  const widgets = L.reconcile({ "a-1": E(0, 0, 6, 10), "a-2": E(0, 0, 6, 10), "a-3": E(0, 0, 6, 10) }, [T("a-1"), T("a-2"), T("a-3")]);
  const list = Object.values(widgets).filter((entry) => entry.visible);
  assert.equal(list.length, 3);
  for (let i = 0; i < list.length; i += 1) for (let j = i + 1; j < list.length; j += 1) assert.equal(L.overlaps(list[i], list[j]), false);
});

test("no terminals: an empty layout", () => {
  assert.deepEqual(L.reconcile({ "a-1": E(0, 0, 6, 10) }, []), {});
  assert.deepEqual(L.reconcile(null, []), {});
});

test("showWidget keeps the old place when it is free and finds a new one when a visible widget took it", () => {
  const widgets = { "a-1": E(0, 0, 6, 10), "s-1": E(6, 0, 6, 10, false) };
  assert.deepEqual(L.showWidget(widgets, "s-1")["s-1"], E(6, 0, 6, 10, true));
  const taken = { "a-1": E(0, 0, 6, 10), "s-1": E(3, 0, 6, 10, false) };
  const shown = L.showWidget(taken, "s-1");
  assert.equal(shown["s-1"].visible, true);
  assert.equal(L.overlaps(shown["s-1"], shown["a-1"]), false);
  assert.deepEqual([shown["s-1"].w, shown["s-1"].h], [6, 10], "its size is kept");
  assert.deepEqual(taken["s-1"], E(3, 0, 6, 10, false), "the input is not changed");
});

test("hideWidget keeps the place for later; visibleIds lists the visible ones in order", () => {
  const widgets = { "a-1": E(0, 0, 6, 10), "s-1": E(6, 0, 6, 10) };
  const hidden = L.hideWidget(widgets, "s-1");
  assert.deepEqual(hidden["s-1"], E(6, 0, 6, 10, false));
  assert.deepEqual(L.visibleIds(hidden), ["a-1"]);
  assert.deepEqual(L.visibleIds(widgets), ["a-1", "s-1"]);
  assert.deepEqual(L.showWidget(hidden, "s-1")["s-1"], E(6, 0, 6, 10));
});

test("setRect stores a settled rectangle on a widget without touching its visibility or the other widgets", () => {
  const widgets = { "a-1": E(0, 0, 6, 10), "s-1": E(6, 0, 6, 10) };
  const next = L.setRect(widgets, "s-1", { x: 6, y: 10, w: 5, h: 7 });
  assert.deepEqual(next["s-1"], E(6, 10, 5, 7));
  assert.deepEqual(next["a-1"], widgets["a-1"]);
  assert.deepEqual(widgets["s-1"], E(6, 0, 6, 10), "the input is not changed");
});

test("resetLayout is the automatic placement of the current terminals, all visible, agents first", () => {
  assert.deepEqual(L.resetLayout([T("a-1"), T("s-1")]), L.autoPlace(["a-1", "s-1"]));
});

// ---- (7) narrow screen selection ----------------------------------------------------------------------------
test("the narrow media query is exactly under 720 px (719.98 so a 720 px wide window is not narrow)", () => {
  assert.equal(L.NARROW_QUERY, "(max-width: 719.98px)");
});

test("isNarrow reads matchMedia and is false where there is none", () => {
  const seen = [];
  const match = (matches) => (query) => { seen.push(query); return { matches }; };
  assert.equal(L.isNarrow({ matchMedia: match(true) }), true);
  assert.equal(L.isNarrow({ matchMedia: match(false) }), false);
  assert.deepEqual(seen, ["(max-width: 719.98px)", "(max-width: 719.98px)"]);
  assert.equal(L.isNarrow({}), false);
  assert.equal(L.isNarrow({ matchMedia: () => { throw new Error("x"); } }), false);
});

test("pickNarrow: the stored terminal if it exists, else the first agent, else the first terminal, else none", () => {
  const list = [T("s-1"), T("a-1"), T("a-2")];
  assert.equal(L.pickNarrow(list, "a-2"), "a-2");
  assert.equal(L.pickNarrow(list, "gone"), "a-1");
  assert.equal(L.pickNarrow(list, null), "a-1");
  assert.equal(L.pickNarrow([T("s-1"), T("s-2")], null), "s-1");
  assert.equal(L.pickNarrow([], "a-1"), null);
});

test("wanted: wide shows the visible widgets; narrow shows only the chosen terminal", () => {
  const widgets = { "a-1": E(0, 0, 6, 10), "a-2": E(6, 0, 6, 10, false), "s-1": E(0, 10, 6, 10) };
  assert.deepEqual(L.wantedIds({ widgets, narrow: false, chosen: "a-2" }), ["a-1", "s-1"]);
  assert.deepEqual(L.wantedIds({ widgets, narrow: true, chosen: "a-2" }), ["a-2"], "chosen, visible or not");
  assert.deepEqual(L.wantedIds({ widgets, narrow: true, chosen: null }), []);
  assert.deepEqual(L.wantedIds({ widgets, narrow: true, chosen: "gone" }), []);
});
