// WBX-C2 / seam S8: static/dashboard.js against a small DOM stub and stub fetch/timers (no browser). The server renders
// the dashboard complete; this script fetches /api/dashboard every 10 s and redraws the numbers, the two tables, the
// owner-waiting list and the Board card from the JSON (text only, never markup). 401 -> location.replace("/login") and
// stop; a network error, a bad answer or a malformed body keeps the screen as it is and backs off (20 s, 40 s, 60 s,
// then every 60 s; a good answer returns to 10 s). A hidden tab does not fetch; the tab coming back does at once.
// Expected values are the BRIEF WBX-D13/D18 words, SPEC "트랙 C 선택" (session JSON, 10 s) and the JSON shape of
// tests/hub/test_web_dashboard.py. The real browser (layout, timers in a background tab) is the user's Chrome check (S7).
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const SOURCE = readFileSync(new URL("../../../src/workbench/hub/web/static/dashboard.js", import.meta.url), "utf8");

// -- a DOM just big enough: elements with children, attributes, textContent, hidden, className, href -----------------
class El {
  constructor(tag, doc) {
    this.tagName = tag.toUpperCase();
    this.doc = doc;
    this.children = [];
    this.attrs = {};
    this._text = "";
    this.hidden = false;
    this.className = "";
    this.href = undefined;
    this.id = "";
  }
  set innerHTML(_value) { throw new Error("innerHTML must not be used"); }
  set outerHTML(_value) { throw new Error("outerHTML must not be used"); }
  insertAdjacentHTML() { throw new Error("insertAdjacentHTML must not be used"); }
  appendChild(child) { this.children.push(child); return child; }
  append(...nodes) { for (const node of nodes) this.children.push(typeof node === "string" ? textNode(node) : node); }
  replaceChildren(...nodes) { this.children = []; this._text = ""; this.append(...nodes); }
  setAttribute(name, value) {
    if (name === "id") { this.id = String(value); this.doc.ids.set(this.id, this); }
    if (name === "class") { this.className = String(value); }
    this.attrs[name] = String(value);
    if (name === "href") this.href = String(value);
  }
  removeAttribute(name) { delete this.attrs[name]; }
  getAttribute(name) { return name in this.attrs ? this.attrs[name] : null; }
  set textContent(value) { this.children = []; this._text = String(value); }
  get textContent() { return this._text + this.children.map((child) => child.textContent).join(""); }
  all() { return this.children.flatMap((child) => (child.all ? [child, ...child.all()] : [child])); }
}
function textNode(value) { return { textContent: String(value), all: () => [] }; }

function makeDocument() {
  const doc = { ids: new Map(), hidden: false, listeners: {} };
  doc.createElement = (tag) => new El(tag, doc);
  doc.createTextNode = (value) => textNode(value);
  doc.getElementById = (id) => doc.ids.get(id) || null;
  doc.addEventListener = (type, handler) => { (doc.listeners[type] ||= []).push(handler); };
  doc.readyState = "complete";
  return doc;
}

const CARDS = ["projects-running", "projects-closed", "requests-new", "requests-reviewing", "requests-needs-info",
               "requests-running", "attention"];
// The ids the server markup carries (test_the_page_has_every_element_the_dashboard_script_updates).
function skeleton(doc) {
  const make = (id, hidden = false) => {
    const element = doc.createElement("div");
    element.setAttribute("id", id);
    element.hidden = hidden;
    return element;
  };
  const ids = ["dash-time", "projects-note", "projects-body", "recent-body", "attention-count", "attention-list", "board-dot",
               "board-label", "board-open"];
  const hidden = ["dash-auto", "projects-message", "requests-message", "recent-message", "attention-message",
                  "projects-wrap", "recent-wrap"];
  const out = {};
  for (const id of ids) out[id] = make(id);
  for (const id of hidden) out[id] = make(id, true);
  for (const card of CARDS) {
    for (const part of ["", "-value", "-sub"]) out[`card-${card}${part}`] = make(`card-${card}${part}`);
  }
  out["dash-time"].textContent = "10:00:00 UTC";
  out["board-open"].setAttribute("href", "/board");
  return out;
}

// answers: a queue of () => Promise (fetch results). timers: recorded setTimeout calls.
function load({ answers = [], hiddenTab = false } = {}) {
  const doc = makeDocument();
  doc.hidden = hiddenTab;
  const page = skeleton(doc);
  const fetches = [];
  const navigations = [];
  const timers = [];
  let nextTimer = 1;
  const window = {
    document: doc,
    location: {
      replace(url) { navigations.push(["replace", url]); },
      assign(url) { navigations.push(["assign", url]); },
      reload() { navigations.push(["reload"]); },
      set href(url) { navigations.push(["href", url]); },
    },
    history: {
      pushState() { navigations.push(["pushState"]); },
      replaceState() { navigations.push(["replaceState"]); },
    },
    fetch(url, options) {
      fetches.push([url, options]);
      const answer = answers.shift();
      if (!answer) return Promise.reject(new Error("no answer queued"));
      return answer();
    },
    setTimeout(handler, ms) { const id = nextTimer++; timers.push({ id, handler, ms, live: true }); return id; },
    clearTimeout(id) { const timer = timers.find((entry) => entry.id === id); if (timer) timer.live = false; },
  };
  window.window = window;
  window.self = window;
  window.document = doc;
  vm.createContext(window);
  vm.runInContext(SOURCE, window, { filename: "dashboard.js" });
  const live = () => timers.filter((timer) => timer.live);
  async function settle() { for (let i = 0; i < 20; i += 1) await new Promise((resolve) => setImmediate(resolve)); }
  async function tick() {
    const timer = live()[0];
    assert.ok(timer, "a timer is waiting");
    timer.live = false;
    timer.handler();
    await settle();
  }
  async function show(visible) {
    doc.hidden = !visible;
    for (const handler of doc.listeners.visibilitychange || []) handler({ type: "visibilitychange" });
    await settle();
  }
  return { doc, page, fetches, navigations, timers, live, tick, show, settle, answers };
}

const ok = (body) => () => Promise.resolve({ status: 200, ok: true, json: () => Promise.resolve(body) });
const status = (code) => () => Promise.resolve({ status: code, ok: false, json: () => Promise.resolve({}) });
const down = () => Promise.reject(new TypeError("Failed to fetch"));

function model(overrides = {}) {
  const cards = [
    { id: "projects-running", kind: "running", label: "프로젝트 실행 중", value: 2, sub: "전체 4개 중", href: "#projects" },
    { id: "projects-closed", kind: "closed", label: "프로젝트 꺼짐", value: 1, sub: "연결 없음/불명 1", href: "#projects" },
    { id: "requests-new", kind: "new", label: "신규", value: 3, sub: "검토 대기", href: "/requests?state=NEW" },
    { id: "requests-reviewing", kind: "reviewing", label: "검토 중", value: 1, sub: "manager 검토", href: "/requests?state=REVIEWING" },
    { id: "requests-needs-info", kind: "needs", label: "추가 정보 필요", value: null, sub: "질문 답변 대기", href: "/requests?state=NEEDS_INFO" },
    { id: "requests-running", kind: "running", label: "실행 중", value: 0, sub: "승인된 요청", href: "/requests?state=RUNNING" },
    { id: "attention", kind: "attn", label: "내 답이 필요한 요청", value: 2, sub: "추가 정보 필요 1 · Task 결정 대기 1", href: "/requests?state=NEEDS_INFO" }];
  return {
    generated: "10:00:10 UTC",
    messages: { projects: null, requests: null, recent: null, attention: null },
    summary: {}, cards,
    projects: [{ project_id: "p1", url: "/projects/p1", requests_url: "/requests?project=p1", name: "alpha", path: "/srv/work/alpha",
                 power: "켜짐", power_kind: "on", task: "로그인 수정 · running", command: "—", wait: "manager 입력창에 글이 있어 알림 대기",
                 requests: "대기 2 · 질문 1", last_activity: "2분 전", review_mode: "수동" },
               { project_id: "p2", url: "/projects/p2", requests_url: "/requests?project=p2", name: "beta", path: "/srv/work/beta",
                 power: "꺼짐", power_kind: "off", task: "—", command: "—", wait: "—", requests: "0", last_activity: "—", review_mode: "자동" }],
    recent: [{ request_id: "r1", url: "/requests/r1", title: "<b>결제</b> webhook", project: "alpha", state: "NEEDS_INFO",
               state_label: "추가 정보 필요", state_kind: "needs", origin: "Board agent", updated: "3분 전", attention: true },
             { request_id: "r2", url: "/requests/r2", title: "README", project: "beta", state: "CLOSED", state_label: "닫힘(성공)",
               state_kind: "closed", origin: "소유자", updated: "어제", attention: false }],
    attention: [{ request_id: "r1", url: "/requests/r1", title: "<b>결제</b> webhook", project: "alpha", reason: "추가 정보 필요",
                  updated: "3분 전" }],
    board: { state: "on", label: "실행 중", url: "/board" },
    ...overrides,
  };
}

const text = (element) => element.textContent;
const rowTexts = (body) => body.children.map((row) => row.children.map((cell) => text(cell)));

// -- behaviours ---------------------------------------------------------------------------------------------------------
test("loading sends nothing yet, announces the automatic refresh and waits 10 seconds", () => {
  const app = load();
  assert.deepEqual(app.fetches, []);
  assert.deepEqual(app.navigations, []);
  assert.equal(app.page["dash-auto"].hidden, false, "the 'updated automatically' words are shown only with JavaScript");
  assert.deepEqual(app.live().map((timer) => timer.ms), [10000]);
});

test("a tick asks /api/dashboard with the session cookie and no cache, then waits 10 seconds again", async () => {
  const app = load({ answers: [ok(model())] });
  await app.tick();
  assert.equal(app.fetches.length, 1);
  const [url, options] = app.fetches[0];
  assert.equal(url, "/api/dashboard");
  assert.equal(options.credentials, "same-origin");
  assert.equal(options.cache, "no-store");
  assert.deepEqual(app.live().map((timer) => timer.ms), [10000]);
  assert.deepEqual(app.navigations, []);
});

test("the cards take their value and sub line from the JSON, an unknown value is a dash", async () => {
  const app = load({ answers: [ok(model())] });
  await app.tick();
  const value = (id) => text(app.page[`card-${id}-value`]);
  const sub = (id) => text(app.page[`card-${id}-sub`]);
  assert.deepEqual(CARDS.map(value), ["2", "1", "3", "1", "—", "0", "2"]);
  assert.equal(sub("projects-running"), "전체 4개 중");
  assert.equal(sub("projects-closed"), "연결 없음/불명 1");
  assert.equal(sub("attention"), "추가 정보 필요 1 · Task 결정 대기 1");
  assert.equal(text(app.page["dash-time"]), "10:00:10 UTC");
});

test("the project table is redrawn with the server's cells: name and path, chip, Task and command, wait and mode, shortcuts", async () => {
  const app = load({ answers: [ok(model())] });
  app.page["projects-body"].append(app.doc.createElement("tr"));  // the old row the new answer replaces
  await app.tick();
  const body = app.page["projects-body"];
  // 2026-10-28: seven cells (path under the name, command under the Task, review mode under the wait reason), as on the server.
  assert.deepEqual(rowTexts(body), [
    ["alpha/srv/work/alpha", "켜짐", "로그인 수정 · running—", "manager 입력창에 글이 있어 알림 대기수동", "대기 2 · 질문 1", "2분 전", "상태 요청"],
    ["beta/srv/work/beta", "꺼짐", "——", "—자동", "0", "—", "상태 요청"]]);
  const first = body.children[0].children;
  assert.equal(first[0].children[0].className, "proj-link");
  assert.equal(first[0].children[0].getAttribute("href"), "/projects/p1");
  assert.equal(first[0].children[1].className, "path");
  assert.equal(text(first[0].children[1]), "/srv/work/alpha");
  assert.equal(first[1].children[0].className, "chip p-on");
  assert.equal(body.children[1].children[1].children[0].className, "chip p-off");
  assert.equal(first[2].children[1].className, "sub");
  assert.equal(first[2].children[1].children[0].className, "none", "a dash is muted");
  assert.equal(first[3].children[0].className, "wait");
  assert.equal(first[3].children[1].className, "sub");
  assert.equal(text(first[3].children[1]), "수동");
  assert.equal(body.children[1].children[3].children[0].className, "none");
  assert.equal(first[6].className, "links");
  assert.deepEqual(first[6].children.filter((node) => node.tagName === "A").map((node) => node.getAttribute("href")),
                   ["/projects/p1", "/requests?project=p1"]);
  // The narrow-screen card layout shows each cell's column name from data-label (the first cell is the card title).
  assert.deepEqual(body.children[0].children.map((cell) => cell.getAttribute("data-label")),
                   [null, "상태", "Task·명령", "대기·검토", "요청", "마지막 활동", "바로가기"]);
  assert.equal(text(app.page["projects-note"]), "2개 · 이름을 누르면 상세");
  assert.equal(app.page["projects-wrap"].hidden, false, "a table with rows is shown");
});

test("the recent request table and the waiting list are redrawn, titles as text and the flag on answers owed", async () => {
  const app = load({ answers: [ok(model())] });
  await app.tick();
  const rows = app.page["recent-body"].children;
  assert.deepEqual(rowTexts(app.page["recent-body"]), [
    ["<b>결제</b> webhook내 답 필요", "alpha", "추가 정보 필요", "Board agent", "3분 전"],
    ["README", "beta", "닫힘(성공)", "소유자", "어제"]]);
  const title = rows[0].children[0];
  assert.equal(title.children[0].getAttribute("href"), "/requests/r1");
  assert.equal(title.children[0].children[0].tagName, "BDI");
  assert.equal(title.children[1].className, "flag");
  assert.equal(rows[0].children[2].children[0].className, "chip s-needs");
  assert.equal(rows[1].children[2].children[0].className, "chip s-closed");
  assert.equal(rows[1].children[0].children.length, 1, "no flag on a request that waits for nobody");
  assert.deepEqual(rows[0].children.map((cell) => cell.getAttribute("data-label")), [null, "프로젝트", "상태", "출처", "갱신"]);
  assert.equal(app.page["recent-wrap"].hidden, false);
  const items = app.page["attention-list"].children;
  assert.equal(items.length, 1);
  assert.equal(items[0].children[0].getAttribute("href"), "/requests/r1");
  assert.equal(text(items[0].children[0]), "<b>결제</b> webhook");
  assert.equal(text(items[0].children[1]), "alpha · 추가 정보 필요 · 3분 전");
  assert.equal(items[0].children[1].className, "meta");
  assert.equal(text(app.page["attention-count"]), "2");
});

test("the Board agent card shows the state word, the dot class and the open link", async () => {
  const app = load({ answers: [ok(model({ board: { state: "error", label: "오류", url: "/board" } }))] });
  await app.tick();
  assert.equal(app.page["board-dot"].className, "dot dot-error");
  assert.equal(text(app.page["board-label"]), "오류");
  assert.equal(app.page["board-open"].getAttribute("href"), "/board");
});

test("a message is shown with its kind and text, and hidden when there is none", async () => {
  const messages = { projects: { kind: "error", text: "프로젝트 상태를 읽지 못했습니다. 잠시 뒤 다시 시도하세요" },
                     requests: { kind: "error", text: "요청 저장소를 읽지 못했습니다" },
                     recent: { kind: "empty", text: "요청이 없습니다" }, attention: null };
  const app = load({ answers: [ok(model({ messages, projects: [], recent: [], attention: [] })), ok(model())] });
  await app.tick();
  for (const name of ["projects", "requests", "recent"]) assert.equal(app.page[`${name}-message`].hidden, false, name);
  assert.equal(app.page["projects-message"].className, "error");
  assert.equal(app.page["projects-message"].getAttribute("role"), "alert");
  assert.equal(text(app.page["projects-message"]), "프로젝트 상태를 읽지 못했습니다. 잠시 뒤 다시 시도하세요");
  assert.equal(app.page["recent-message"].className, "empty");
  assert.equal(app.page["recent-message"].getAttribute("role"), null, "an empty part is not an alert");
  assert.equal(app.page["attention-message"].hidden, true);
  assert.equal(app.page["projects-wrap"].hidden, true, "no rows, no table");
  assert.equal(app.page["recent-wrap"].hidden, true);
  await app.tick();  // the problem is gone: the messages hide again and the rows return
  for (const name of ["projects", "requests", "recent", "attention"]) {
    assert.equal(app.page[`${name}-message`].hidden, true, name);
    assert.equal(text(app.page[`${name}-message`]), "", name);
  }
  assert.equal(app.page["projects-body"].children.length, 2);
});

test("text from the JSON never becomes markup or a script URL", async () => {
  const evil = model({
    projects: [{ ...model().projects[0], name: '<img src=x onerror="alert(1)">', url: "javascript:alert(1)", requests_url: "//evil.example/x" }],
    recent: [{ ...model().recent[0], url: "https://evil.example/", title: "<script>alert(1)</script>" }],
    attention: [{ ...model().attention[0], url: "javascript:alert(2)" }],
    board: { state: "on\" onmouseover=\"x", label: "<b>on</b>", url: "javascript:alert(3)" } });
  const app = load({ answers: [ok(evil)] });  // El throws if innerHTML/outerHTML/insertAdjacentHTML is used
  await app.tick();
  assert.equal(text(app.page["projects-body"].children[0].children[0].children[0]), '<img src=x onerror="alert(1)">');  // the name, then the path span
  const hrefs = [];
  for (const id of ["projects-body", "recent-body", "attention-list"]) {
    for (const node of app.page[id].all()) if (node.tagName === "A") hrefs.push(node.getAttribute("href"));
  }
  assert.deepEqual(hrefs.filter((href) => href !== null && !href.startsWith("/")), [], "only same-site paths become links");
  assert.deepEqual(hrefs.filter((href) => href !== null && href.startsWith("//")), []);
  assert.equal(app.page["board-open"].getAttribute("href"), "/board", "a bad link leaves the old one");
  assert.equal(app.page["board-dot"].className, "dot dot-error", "a state that is not on/off/error is drawn as error");
});

test("401 means the session ended: replace to /login, draw nothing, stop asking", async () => {
  const app = load({ answers: [status(401), ok(model())] });
  const before = text(app.page["card-requests-new-value"]);
  await app.tick();
  assert.deepEqual(app.navigations, [["replace", "/login"]]);  // replace: Back does not return to a dead dashboard
  assert.equal(text(app.page["card-requests-new-value"]), before);
  assert.deepEqual(app.live(), [], "no timer is left");
  assert.equal(app.fetches.length, 1);
  await app.show(true);  // coming back to the tab does not ask again either
  assert.equal(app.fetches.length, 1);
});

test("a network error keeps the screen and backs off 20 s, 40 s, 60 s, 60 s; a good answer returns to 10 s", async () => {
  const app = load({ answers: [ok(model()), down, down, down, down, ok(model({ generated: "10:05:00 UTC" }))] });
  await app.tick();
  const shown = text(app.page["dash-time"]);
  const delays = [];
  for (let i = 0; i < 4; i += 1) {
    await app.tick();
    delays.push(app.live()[0].ms);
  }
  assert.deepEqual(delays, [20000, 40000, 60000, 60000]);
  assert.equal(text(app.page["dash-time"]), shown, "the last good screen stays");
  assert.deepEqual(app.navigations, []);
  await app.tick();
  assert.equal(text(app.page["dash-time"]), "10:05:00 UTC");
  assert.deepEqual(app.live().map((timer) => timer.ms), [10000]);
});

test("a server error, an unreadable body and a body that is not an object are failures too", async () => {
  const badJson = () => Promise.resolve({ status: 200, ok: true, json: () => Promise.reject(new SyntaxError("bad")) });
  const notObject = ok("not a model");
  const app = load({ answers: [status(500), status(503), badJson, notObject, ok(null), ok(model())] });
  const delays = [];
  for (let i = 0; i < 5; i += 1) {
    await app.tick();
    delays.push(app.live()[0].ms);
  }
  assert.deepEqual(delays, [20000, 40000, 60000, 60000, 60000]);
  assert.equal(text(app.page["card-requests-new-value"]), "", "nothing was drawn from a bad answer");
  assert.deepEqual(app.navigations, []);
  await app.tick();
  assert.equal(text(app.page["card-requests-new-value"]), "3");
  assert.deepEqual(app.live().map((timer) => timer.ms), [10000]);
});

test("a hidden tab does not ask; coming back asks at once, once, and a request in flight is never doubled", async () => {
  let release;
  const slow = () => new Promise((resolve) => { release = () => resolve({ status: 200, ok: true, json: () => Promise.resolve(model()) }); });
  const app = load({ hiddenTab: true, answers: [ok(model()), slow] });
  await app.tick();
  assert.deepEqual(app.fetches, [], "hidden: no request");
  assert.deepEqual(app.live().map((timer) => timer.ms), [10000], "but the timer keeps going");
  await app.show(true);
  assert.equal(app.fetches.length, 1, "visible again: one request now");
  assert.equal(text(app.page["card-requests-new-value"]), "3");
  await app.tick();  // the slow request starts
  assert.equal(app.fetches.length, 2);
  await app.show(false);
  await app.show(true);  // returns while the slow one is still in flight
  assert.equal(app.fetches.length, 2, "no second request while one is in flight");
  release();
  await app.settle();
  assert.deepEqual(app.live().map((timer) => timer.ms), [10000]);
});

test("the refresh never touches the history: no assign, href, reload or pushState", async () => {
  const app = load({ answers: [ok(model()), down, status(500), ok(model())] });
  for (let i = 0; i < 4; i += 1) await app.tick();
  assert.deepEqual(app.navigations, []);
});

test("an item that is not an object is skipped instead of breaking the redraw", async () => {
  const app = load({ answers: [ok(model({ projects: [null, 5, model().projects[0]], recent: ["x"], attention: [null] }))] });
  await app.tick();
  assert.equal(app.page["projects-body"].children.length, 1);
  assert.equal(app.page["recent-body"].children.length, 0);
  assert.deepEqual(app.live().map((timer) => timer.ms), [10000], "still a success");
});
