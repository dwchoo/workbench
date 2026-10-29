// Board workspace layout: the pure grid, storage and reconcile logic (web-board-ux SPEC s4 "화면: workspace와 panel").
// No DOM, no timers: board.js reads the pointer and the sizes, calls these and writes the result with CSSOM.
// A rectangle is {x, y, w, h} in whole grid cells: 12 columns, rows without end, at least MIN_W x MIN_H. Widgets
// that only touch do not overlap. A "widget entry" is a rectangle plus `visible`.
export const COLS = 12;
export const MIN_W = 3;
export const MIN_H = 6;
export const DEFAULT_W = 6;
export const DEFAULT_H = 10;
export const ROW_MAX = 100000; // a stored y + h beyond this is junk, not a layout
export const MAX_ENTRIES = 64; // a stored layout names at most this many terminals (the Hub has at most 40): the rest is dropped
export const MAX_TEXT = 1 << 20; // a stored layout longer than this (1 MiB) is not one: nothing is parsed
export const NARROW_QUERY = "(max-width: 719.98px)"; // WBX-D51: under 720 px there is no grid
export const KEY_LAYOUT = "wb.board.layout";
export const KEY_PANEL = "wb.board.panel";
export const KEY_NARROW = "wb.board.narrow";

const isInt = (value) => Number.isInteger(value);
const rectOf = (entry) => ({ x: entry.x, y: entry.y, w: entry.w, h: entry.h });
const finitePositive = (value) => typeof value === "number" && Number.isFinite(value) && value > 0;

// ---- cells ----------------------------------------------------------------------------------------------------
// A pointer distance in px -> whole cells (the nearest; half a cell rounds up). A bad pitch is 0 cells.
export function snapCells(distancePx, pitchPx) {
  if (!finitePositive(pitchPx) || typeof distancePx !== "number" || !Number.isFinite(distancePx)) return 0;
  return Math.floor(distancePx / pitchPx + 0.5);
}

// The size of one cell step along an axis: cell size + gap, from the whole extent: (extent + gap) / count.
export function pitchOf(extentPx, gapPx, count) {
  if (!finitePositive(extentPx) || !finitePositive(count)) return 0;
  return (extentPx + (Number.isFinite(gapPx) ? gapPx : 0)) / count;
}

// ---- overlap --------------------------------------------------------------------------------------------------
export function overlaps(a, b) {
  return a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h;
}

// `others`: {id: rect}. `skip`: the id of the widget being moved (it does not block itself).
export function collides(rect, others, skip = null) {
  for (const [id, other] of Object.entries(others)) {
    if (id !== skip && overlaps(rect, other)) return true;
  }
  return false;
}

// The rectangles that block: visible entries only (a hidden widget takes no room), minus `skip`.
export function obstacles(widgets, skip = null) {
  const out = {};
  for (const [id, entry] of Object.entries(widgets)) {
    if (id !== skip && entry.visible) out[id] = rectOf(entry);
  }
  return out;
}

// In the grid: integer cells, inside the 12 columns, not above row 0, at least the minimum size.
export function inGrid(rect) {
  return [rect.x, rect.y, rect.w, rect.h].every(isInt) && rect.x >= 0 && rect.y >= 0 && rect.w >= MIN_W
    && rect.h >= MIN_H && rect.x + rect.w <= COLS && rect.y + rect.h <= ROW_MAX;
}

const clampSize = (w, h) => ({
  w: Math.min(COLS, Math.max(MIN_W, isInt(w) ? w : DEFAULT_W)),
  h: Math.max(MIN_H, isInt(h) ? h : DEFAULT_H),
});

// ---- automatic placement --------------------------------------------------------------------------------------
// The first free place for a w x h widget: rows from the top, columns from the left. `placed`: {id: rect}.
export function findSpot(placed, w = DEFAULT_W, h = DEFAULT_H) {
  const size = clampSize(w, h);
  const rects = Object.values(placed);
  const bottom = rects.reduce((most, rect) => Math.max(most, rect.y + rect.h), 0);
  for (let y = 0; y <= bottom; y += 1) {
    for (let x = 0; x + size.w <= COLS; x += 1) {
      const rect = { x, y, ...size };
      if (!rects.some((other) => overlaps(rect, other))) return rect;
    }
  }
  return { x: 0, y: bottom, ...size }; // unreachable: the row under everything is free
}

// Every id in the given order, visible, placed one after another. (Used when nothing is stored and for a reset.)
export function autoPlace(ids) {
  const widgets = {};
  const placed = {};
  for (const id of ids) {
    const rect = findSpot(placed);
    placed[id] = rect;
    widgets[id] = { ...rect, visible: true };
  }
  return widgets;
}

// ---- move and resize ------------------------------------------------------------------------------------------
// A drag by (dx, dy) cells: the widget keeps its size and stays inside columns 0..11 and rows >= 0.
export function moveRect(origin, dx, dy) {
  const x = Math.min(COLS - origin.w, Math.max(0, origin.x + dx));
  const y = Math.max(0, origin.y + dy);
  return { x, y, w: origin.w, h: origin.h };
}

// A drag of the bottom right handle by (dx, dy) cells: x and y stay; w and h between the minimum and the grid edge.
export function resizeRect(origin, dx, dy) {
  const w = Math.min(COLS - origin.x, Math.max(MIN_W, origin.w + dx));
  const h = Math.max(MIN_H, origin.h + dy);
  return { x: origin.x, y: origin.y, w, h };
}

// The drop of widget `id` at `rect`: ok when it is inside the grid and overlaps no visible widget; otherwise it goes
// back to its last valid place (its current entry) and nothing else moves (WBX-D57).
export function settle(id, rect, widgets) {
  if (inGrid(rect) && !collides(rect, obstacles(widgets, id))) return { ok: true, rect: { ...rect } };
  return { ok: false, rect: rectOf(widgets[id]) };
}

// ---- browser storage (WBX-D50: layout, panel and narrow choice live in this browser only) ----------------------
const ID_PATTERN = /^[as]-[0-9a-z]{1,62}$/; // a Board terminal id; also keeps "__proto__" and friends out of the maps

export function browserStorage(win) {
  try {
    return (win && win.localStorage) || null;
  } catch (_) {
    return null; // reading the property itself can throw (blocked site data)
  }
}

function readItem(storage, key) {
  try {
    const value = storage.getItem(key);
    return typeof value === "string" ? value : null;
  } catch (_) {
    return null;
  }
}

function writeItem(storage, key, value) {
  try {
    storage.setItem(key, value);
    return true;
  } catch (_) {
    return false;
  }
}

export function serializeLayout(widgets) {
  const out = {};
  for (const [id, entry] of Object.entries(widgets)) {
    out[id] = { x: entry.x, y: entry.y, w: entry.w, h: entry.h, visible: entry.visible === true };
  }
  return JSON.stringify({ v: 1, widgets: out });
}

// Stored text -> {id: entry}. An entry is {x,y,w,h,visible}, or only {visible} when its geometry is junk (out of the
// grid, not an integer, overlapping a visible entry before it): reconcile() places those again. null when the whole
// text is not a v:1 layout.
export function parseLayout(text) {
  if (typeof text !== "string" || text === "" || text.length > MAX_TEXT) return null;
  let value;
  try {
    value = JSON.parse(text);
  } catch (_) {
    return null;
  }
  if (value === null || typeof value !== "object" || Array.isArray(value) || value.v !== 1) return null;
  const raw = value.widgets;
  if (raw === null || typeof raw !== "object" || Array.isArray(raw)) return null;
  const out = {};
  const placed = {};
  let kept = 0;
  for (const [id, entry] of Object.entries(raw)) {
    if (kept >= MAX_ENTRIES) break; // bounded work: the overlap check below is quadratic in the entries kept
    if (!ID_PATTERN.test(id)) continue;
    kept += 1;
    const isObject = entry !== null && typeof entry === "object" && !Array.isArray(entry);
    const visible = !(isObject && entry.visible === false);
    const rect = isObject ? { x: entry.x, y: entry.y, w: entry.w, h: entry.h } : null;
    const good = rect !== null && inGrid(rect) && (!visible || !collides(rect, placed));
    if (good && visible) placed[id] = rect;
    out[id] = good ? { ...rect, visible } : { visible };
  }
  return out;
}

export function loadLayout(storage) {
  return storage ? parseLayout(readItem(storage, KEY_LAYOUT)) : null;
}

export function saveLayout(storage, widgets) {
  return storage ? writeItem(storage, KEY_LAYOUT, serializeLayout(widgets)) : false;
}

export function loadPanel(storage) {
  return storage && readItem(storage, KEY_PANEL) === "closed" ? "closed" : "open";
}

export function savePanel(storage, value) {
  return storage && (value === "open" || value === "closed") ? writeItem(storage, KEY_PANEL, value) : false;
}

export function loadNarrow(storage) {
  const value = storage ? readItem(storage, KEY_NARROW) : null;
  return value ? value : null;
}

export function saveNarrow(storage, id) {
  return storage && typeof id === "string" && id !== "" ? writeItem(storage, KEY_NARROW, id) : false;
}

// ---- reconcile with the terminal list (WBX-D57) -----------------------------------------------------------------
// `stored`: {id: entry} from parseLayout() or the layout in memory, null when this browser has none. `terminals`:
// the list in display order (agents first). `created`: ids made from this browser (shown at once). Rules: nothing
// stored -> all placed and visible; stored -> an id that is gone is dropped, a new one is hidden (the layout is not
// disturbed), one made here is placed and shown. Visible entries never overlap in the result.
export function reconcile(stored, terminals, created = []) {
  const ids = terminals.map((terminal) => terminal.id);
  if (stored === null || stored === undefined) return autoPlace(ids);
  const madeHere = new Set(created);
  const placed = {};
  const keep = {};
  for (const id of ids) { // pass 1: entries with a valid place keep it (hidden ones always; visible ones if free)
    const entry = stored[id];
    if (!entry || !("x" in entry) || !inGrid(entry)) continue;
    if (entry.visible === true && collides(rectOf(entry), placed)) continue;
    keep[id] = { ...rectOf(entry), visible: entry.visible === true };
    if (keep[id].visible) placed[id] = rectOf(entry);
  }
  const out = {};
  for (const id of ids) { // pass 2, in terminal order: everything else gets a free place
    if (keep[id]) {
      out[id] = keep[id];
      continue;
    }
    const entry = stored[id];
    const visible = entry ? entry.visible !== false : madeHere.has(id);
    const size = entry && inGrid({ x: 0, y: 0, w: entry.w, h: entry.h }) ? [entry.w, entry.h] : [DEFAULT_W, DEFAULT_H];
    const rect = findSpot(placed, ...size);
    if (visible) placed[id] = rect;
    out[id] = { ...rect, visible };
  }
  return out;
}

export function visibleIds(widgets) {
  return Object.keys(widgets).filter((id) => widgets[id].visible);
}

export function hideWidget(widgets, id) {
  return widgets[id] ? { ...widgets, [id]: { ...widgets[id], visible: false } } : widgets;
}

// Show a widget: its old place when no visible widget took it meanwhile, otherwise a free place of the same size.
export function showWidget(widgets, id) {
  const entry = widgets[id];
  if (!entry) return widgets;
  const rect = collides(rectOf(entry), obstacles(widgets, id)) ? findSpot(obstacles(widgets, id), entry.w, entry.h) : rectOf(entry);
  return { ...widgets, [id]: { ...rect, visible: true } };
}

export function setRect(widgets, id, rect) {
  return widgets[id] ? { ...widgets, [id]: { ...rectOf(rect), visible: widgets[id].visible } } : widgets;
}

// "배치 초기화": the automatic placement of the terminals there are now.
export function resetLayout(terminals) {
  return autoPlace(terminals.map((terminal) => terminal.id));
}

// ---- narrow screen (WBX-D51) ----------------------------------------------------------------------------------
export function isNarrow(win) {
  try {
    return Boolean(win && typeof win.matchMedia === "function" && win.matchMedia(NARROW_QUERY).matches);
  } catch (_) {
    return false;
  }
}

// The terminal a narrow screen shows: the one chosen before if it still exists, else the first agent, else the first.
export function pickNarrow(terminals, chosen) {
  if (terminals.some((terminal) => terminal.id === chosen)) return chosen;
  const agent = terminals.find((terminal) => terminal.kind === "agent");
  if (agent) return agent.id;
  return terminals.length > 0 ? terminals[0].id : null;
}

// The terminals that are shown (and connected): the visible widgets, or on a narrow screen just the chosen one.
export function wantedIds({ widgets, narrow, chosen }) {
  if (narrow) return chosen && widgets[chosen] ? [chosen] : [];
  return visibleIds(widgets);
}
