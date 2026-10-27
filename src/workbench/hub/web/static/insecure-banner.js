/* WB Hub plain-HTTP banner (web-board-ux SPEC "평문 HTTP 배너 접기"). A synchronous <head> script: a stored
   localStorage "wb.insecureBanner" = "collapsed" puts class "wb-insecure-collapsed" on <html> before first paint, and
   base.css shows the banner or the header chip by that class (one of them is always visible). Once the DOM exists the
   close button (hidden in the markup, so it stays inert without JavaScript) collapses + stores, and the chip expands
   + deletes the stored value. Every storage access is guarded: a storage that throws leaves the banner expanded. */
(function () {
  "use strict";
  var KEY = "wb.insecureBanner";
  var COLLAPSED = "collapsed";
  var CLASS = "wb-insecure-collapsed";
  var root = document.documentElement;

  function storage() {
    try { return window.localStorage || null; } catch (error) { return null; }
  }
  function remember(collapsed) {
    try {
      var store = storage();
      if (!store) { return; }
      if (collapsed) { store.setItem(KEY, COLLAPSED); } else { store.removeItem(KEY); }
    } catch (error) { /* private window or blocked storage: this page only */ }
  }
  function collapse() { root.classList.add(CLASS); remember(true); }
  function expand() { root.classList.remove(CLASS); remember(false); }

  try {
    var store = storage();
    if (store && store.getItem(KEY) === COLLAPSED) { root.classList.add(CLASS); }
  } catch (error) { /* stay expanded */ }

  function wire() {
    var close = document.querySelector(".banner-close");
    var chip = document.querySelector(".insecure-chip");
    if (close) {
      close.hidden = false;
      close.addEventListener("click", collapse);
    }
    if (chip) {
      chip.addEventListener("click", expand);
      chip.addEventListener("keydown", function (event) {
        if (event.key === "Enter" || event.key === " ") { event.preventDefault(); expand(); }
      });
    }
  }
  if (document.readyState === "loading") { document.addEventListener("DOMContentLoaded", wire); } else { wire(); }
}());
