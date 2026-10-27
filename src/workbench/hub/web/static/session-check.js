/* WB Hub: a logged-in screen restored from the back/forward cache (pageshow with persisted) asks /api/badge whether
   its session still lives and goes to /login on 401. A network error does nothing (the Hub may be restarting).
   State changes from a restored screen are refused by the server anyway (session and CSRF checks). */
(function () {
  "use strict";
  window.addEventListener("pageshow", function (event) {
    if (!event || event.persisted !== true) { return; }
    window.fetch("/api/badge", { credentials: "same-origin", cache: "no-store" })
      .then(function (response) {
        if (response.status === 401) { window.location.replace("/login"); }
      })
      .catch(function () { /* the Hub may be restarting: stay */ });
  });
}());
