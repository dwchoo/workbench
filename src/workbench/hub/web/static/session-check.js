/* WB Hub: a logged-in screen shown again by Back/Forward asks /api/badge whether its session still lives and goes to
   /login on 401. Two ways back: restored from the back/forward cache (pageshow with persisted), or loaded afresh from
   the HTTP cache (bfcache not used; WBX-A6) - then this document's navigation timing entry says "back_forward". A
   network error does nothing (the Hub may be restarting). State changes from such a screen are refused by the server
   anyway (session and CSRF checks). */
(function () {
  "use strict";
  function check() {
    window.fetch("/api/badge", { credentials: "same-origin", cache: "no-store" })
      .then(function (response) {
        if (response.status === 401) { window.location.replace("/login"); }
      })
      .catch(function () { /* the Hub may be restarting: stay */ });
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

  window.addEventListener("pageshow", function (event) {
    if (event && event.persisted === true) { check(); }
  });
  if (loadedByBackForward()) { check(); }
}());
