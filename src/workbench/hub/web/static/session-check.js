/* WB Hub: a logged-in screen shown again by Back/Forward asks /api/badge whether its session still lives and goes to
   /login on 401. Two ways back: restored from the back/forward cache (pageshow with persisted), or loaded afresh from
   the HTTP cache (bfcache not used; WBX-A6) - then this document's navigation timing entry says "back_forward".
   WBX-A7 (2026-10-28 사용자 결정): until the answer comes such a page is hidden - class "wb-checking" on <html>, and
   base.css hides <body> by it. This is a synchronous <head> script, so the class is set before first paint; a page
   going into the bfcache (pagehide with persisted) is hidden on the way out, so it comes back hidden. A 2xx, another
   status or a network error (the Hub may be restarting) reveal the page - fail open, since state changes from such a
   screen are refused by the server anyway (session and CSRF checks); a 401 replaces it with /login while still
   hidden; a safety timer reveals it if no answer comes, so the screen never stays blank. A normal load, a reload or
   a browser without JavaScript never hides anything. */
(function () {
  "use strict";
  var CHECKING = "wb-checking";
  var REVEAL_AFTER_MS = 4000;  // no answer by then: show the page anyway, never a blank screen
  var timer = null;
  var latest = 0;  // the newest check: only its answer reveals the page

  function classList() {
    try {
      var root = window.document && window.document.documentElement;
      return root && root.classList ? root.classList : null;
    } catch (error) {
      return null;
    }
  }
  function stopTimer() {
    if (timer !== null && typeof window.clearTimeout === "function") { window.clearTimeout(timer); }
    timer = null;
  }
  function cover() {
    var list = classList();
    if (list) { list.add(CHECKING); }
    return Boolean(list);
  }
  function reveal() {
    stopTimer();
    var list = classList();
    if (list) { list.remove(CHECKING); }
  }

  function check() {
    var mine = latest += 1;
    stopTimer();
    if (cover() && typeof window.setTimeout === "function") { timer = window.setTimeout(reveal, REVEAL_AFTER_MS); }
    function answered() { if (mine === latest) { reveal(); } }
    var asked;
    try {
      asked = Promise.resolve(window.fetch("/api/badge", { credentials: "same-origin", cache: "no-store" }));
    } catch (error) {
      answered();  // no fetch or blocked: nothing to wait for
      return;
    }
    asked.then(function (response) {
      if (response.status === 401) {
        stopTimer();  // stay hidden until /login replaces this page
        window.location.replace("/login");
        return;
      }
      answered();
    }).catch(answered);  // the Hub may be restarting: stay, visible
  }

  function loadedByBackForward() {
    try {
      var entries = window.performance && window.performance.getEntriesByType
        ? window.performance.getEntriesByType("navigation") : [];
      return Boolean(entries && entries[0] && entries[0].type === "back_forward");
    } catch (error) {
      return false;
    }
  }

  // Into the bfcache: hide now (no timer: a frozen page runs none), so a restore is hidden at its first frame.
  window.addEventListener("pagehide", function (event) {
    if (event && event.persisted === true) { stopTimer(); cover(); }
  });
  window.addEventListener("pageshow", function (event) {
    if (event && event.persisted === true) { check(); }
  });
  if (loadedByBackForward()) { check(); }
}());
