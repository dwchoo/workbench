/* WB Hub: a login screen restored from the back/forward cache (pageshow with persisted; some browsers restore even a
   no-store page) reloads, so the form gets a fresh login token and cookie pair instead of a stale one. */
(function () {
  "use strict";
  window.addEventListener("pageshow", function (event) {
    if (event && event.persisted === true) { window.location.reload(); }
  });
}());
