/* WB Hub dashboard refresh (web-board-ux WBX-C2). The server renders `/dashboard` complete; this script asks the session JSON
   `/api/dashboard` every 10 s and redraws the numbers, the two tables, the owner-waiting list and the Board agent card
   from it, in place (no reload, no history entry). Everything the JSON says goes in as text, never as markup. 401 means
   the session ended: go to /login (replace, so Back does not return here) and stop. A network error, a bad answer or a
   malformed body leaves the screen as it is and backs off (20 s, 40 s, 60 s, then 60 s); a good answer is 10 s again.
   A hidden tab does not ask; the tab coming back does at once. */
(function () {
  "use strict";
  var INTERVAL_MS = 10000;
  var MAX_DELAY_MS = 60000;
  var failures = 0;
  var timer = null;
  var inFlight = false;
  var stopped = false;

  function byId(id) { return document.getElementById(id); }
  function setText(id, value) {
    var element = byId(id);
    if (element) { element.textContent = value; }
  }
  function show(value) { return value === null || value === undefined ? "—" : String(value); }

  // The objects of an array (anything else in it is skipped, so one bad item cannot break the whole redraw).
  function list(value) {
    return Array.isArray(value) ? value.filter(function (item) { return item !== null && typeof item === "object"; }) : [];
  }
  function text(value) { return value === null || value === undefined ? "" : String(value); }
  // Only a path on this site becomes a link; anything else (javascript:, //host, https:) is left out.
  function safeHref(value) { return typeof value === "string" && /^\/(?!\/)|^#/.test(value) ? value : null; }
  var BOARD_STATES = { on: true, off: true, error: true };
  var POWER_KINDS = { on: true, off: true, busy: true, unknown: true };
  var STATE_KINDS = { "new": true, reviewing: true, needs: true, ready: true, running: true, closed: true, unknown: true };

  function el(tag, className, content) {
    var element = document.createElement(tag);
    if (className) { element.className = className; }
    if (content !== undefined) { element.textContent = text(content); }
    return element;
  }
  function link(label, href, className) {
    var anchor = el("a", className || "", label);
    var safe = safeHref(href);
    if (safe) { anchor.setAttribute("href", safe); }
    return anchor;
  }
  function cell(content, className) {
    var td = el("td", className || "");
    if (content !== undefined) { td.append(content); }
    return td;
  }
  // The Hub's "—" (nothing to say) is muted, the way the server draws it.
  function plain(value) { return value === "—" ? el("span", "none", value) : text(value); }

  function applyCards(cards) {
    list(cards).forEach(function (card) {
      setText("card-" + card.id + "-value", show(card.value));
      setText("card-" + card.id + "-sub", text(card.sub));
    });
  }

  function projectRow(item) {
    var row = el("tr");
    var name = item.url ? link(item.name, item.url, "proj-link") : text(item.name);
    var kind = POWER_KINDS[item.power_kind] ? item.power_kind : "unknown";
    var wait = item.wait === "—" ? plain(item.wait) : el("span", "wait", item.wait);
    var links = cell(undefined, "links");
    if (item.url) {
      links.append(link("상태", item.url), " ", link("요청", item.requests_url));
    }
    row.append(cell(name), cell(text(item.path), "path"), cell(el("span", "chip p-" + kind, item.power)), cell(plain(item.task)),
               cell(plain(item.command)), cell(wait), cell(plain(item.requests)), cell(plain(item.last_activity)),
               cell(text(item.review_mode)), links);
    return row;
  }

  function requestRow(item) {
    var row = el("tr");
    var title = cell(undefined, "title");
    var anchor = link("", item.url);
    anchor.append(el("bdi", "", item.title));
    title.append(anchor);
    if (item.attention) { title.append(el("span", "flag", "내 답 필요")); }
    var kind = STATE_KINDS[item.state_kind] ? item.state_kind : "unknown";
    row.append(title, cell(text(item.project), "proj"), cell(el("span", "chip s-" + kind, item.state_label)),
               cell(text(item.origin), "src"), cell(text(item.updated), "when"));
    return row;
  }

  function attentionRow(item) {
    var li = el("li");
    var anchor = link("", item.url);
    anchor.append(el("bdi", "", item.title));
    li.append(anchor, el("span", "meta", [item.project, item.reason, item.updated].map(text).join(" · ")));
    return li;
  }

  function fill(bodyId, wrapId, items, build) {
    var body = byId(bodyId);
    var rows = list(items).map(build);
    if (body) { body.replaceChildren.apply(body, rows); }
    var wrap = byId(wrapId);
    if (wrap) { wrap.hidden = rows.length === 0; }
    return rows.length;
  }

  function applyMessage(name, entry) {
    var element = byId(name + "-message");
    if (!element) { return; }
    if (entry && typeof entry === "object") {
      element.className = entry.kind === "error" ? "error" : "empty";
      if (entry.kind === "error") { element.setAttribute("role", "alert"); } else { element.removeAttribute("role"); }
      element.textContent = text(entry.text);
      element.hidden = false;
    } else {
      element.textContent = "";
      element.removeAttribute("role");
      element.hidden = true;
    }
  }

  function applyBoard(board) {
    if (!board || typeof board !== "object") { return; }
    var dot = byId("board-dot");
    if (dot) { dot.className = "dot dot-" + (BOARD_STATES[board.state] ? board.state : "error"); }
    setText("board-label", text(board.label));
    var open = byId("board-open");
    var safe = safeHref(board.url);
    if (open && safe) { open.setAttribute("href", safe); }
  }

  function apply(model) {
    if (!model || typeof model !== "object") { throw new Error("no model"); }
    var messages = model.messages && typeof model.messages === "object" ? model.messages : {};
    applyCards(model.cards);
    var count = fill("projects-body", "projects-wrap", model.projects, projectRow);
    setText("projects-note", count + "개 · 이름을 누르면 상세");
    fill("recent-body", "recent-wrap", model.recent, requestRow);
    var waiting = byId("attention-list");
    if (waiting) { waiting.replaceChildren.apply(waiting, list(model.attention).map(attentionRow)); }
    var attention = list(model.cards).filter(function (card) { return card.id === "attention"; })[0];
    setText("attention-count", show(attention ? attention.value : null));
    ["projects", "requests", "recent", "attention"].forEach(function (name) { applyMessage(name, messages[name]); });
    applyBoard(model.board);
    setText("dash-time", show(model.generated));
  }

  function delay() { return Math.min(INTERVAL_MS * Math.pow(2, failures), MAX_DELAY_MS); }
  function schedule() {
    if (stopped) { return; }
    if (timer !== null) { window.clearTimeout(timer); }
    timer = window.setTimeout(refresh, delay());
  }

  function refresh() {
    if (timer !== null) { window.clearTimeout(timer); }
    timer = null;
    if (stopped || inFlight) { return; }
    if (document.hidden) { schedule(); return; }
    inFlight = true;
    window.fetch("/api/dashboard", { credentials: "same-origin", cache: "no-store" })
      .then(function (response) {
        if (response.status === 401) {
          stopped = true;
          window.location.replace("/login");
          return null;
        }
        if (!response.ok) { throw new Error("status " + response.status); }
        return response.json().then(function (model) { apply(model); failures = 0; });
      })
      .catch(function () { failures += 1; /* keep the screen; the Hub may be restarting */ })
      .then(function () { inFlight = false; schedule(); });
  }

  // A tab that was hidden asks as soon as it is shown again (a request already in flight is never doubled).
  document.addEventListener("visibilitychange", function () {
    if (!document.hidden && !stopped && !inFlight) { refresh(); }
  });

  var auto = byId("dash-auto");
  if (auto) { auto.hidden = false; }
  schedule();
}());
