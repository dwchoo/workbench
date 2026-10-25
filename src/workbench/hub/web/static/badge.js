/* WB Hub: poll /api/badge every 30 s; tab title "(<pending>·<needs_info>) WB Hub" and the header badge. */
(function () {
  "use strict";
  var INTERVAL_MS = 30000;
  var BRAND = "WB Hub";
  var original = document.title;
  var timer = null;

  function show(counts) {
    var pending = Number(counts.pending) || 0;
    var needsInfo = Number(counts.needs_info) || 0;
    var slot = document.getElementById("wb-badge");
    var text = pending + "·" + needsInfo;
    if (pending + needsInfo > 0) {
      document.title = "(" + text + ") " + BRAND;
      if (slot) { slot.textContent = text; }
    } else {
      document.title = original;
      if (slot) { slot.textContent = ""; }
    }
  }

  function refresh() {
    fetch("/api/badge", { credentials: "same-origin", cache: "no-store" })
      .then(function (response) {
        if (response.status === 401 && timer !== null) { clearInterval(timer); timer = null; }
        return response.ok ? response.json() : null;
      })
      .then(function (counts) { if (counts) { show(counts); } })
      .catch(function () { /* the Hub may be restarting: keep the last badge */ });
  }

  refresh();
  timer = setInterval(refresh, INTERVAL_MS);
}());
