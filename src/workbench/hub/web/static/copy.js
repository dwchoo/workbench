/* WB Hub copy buttons (web-board-ux SPEC "Key 발급 결과의 client 설정 안내"). Each `button.copy[data-copy-target=ID]` is
   `hidden` in the markup (inert without JavaScript); once the DOM exists it is shown, and a click copies the text of
   element ID: navigator.clipboard.writeText first (it exists only in a secure context, so not on a plain-HTTP LAN
   page), else - or when it fails - select the text and document.execCommand("copy"), else leave the text selected
   and ask the user to copy by hand. The result goes into the element ID + "-status" when there is one. Nothing here
   may throw out of the handler: a copy problem must never break the page. */
(function () {
  "use strict";
  var DONE = "복사했습니다";
  var MANUAL = "직접 복사하세요(Ctrl+C)";

  function show(id, message) {
    try {
      var status = document.getElementById(id + "-status");
      if (status) { status.textContent = message; }
    } catch (error) { /* no status line */ }
  }
  function select(target) {
    try {
      var selection = window.getSelection && window.getSelection();
      var range = document.createRange();
      range.selectNodeContents(target);
      selection.removeAllRanges();
      selection.addRange(range);
      return true;
    } catch (error) { return false; /* the selection API is missing or refused: nothing to copy */ }
  }
  function viaSelection(target) {
    if (!select(target)) { return false; }
    try { return document.execCommand("copy") === true; } catch (error) { return false; }
  }
  function copy(id) {
    var target = null;
    try { target = document.getElementById(id); } catch (error) { /* handled below */ }
    if (!target) { return Promise.resolve(); }
    var text = String(target.textContent);
    var attempt;
    try {
      var clipboard = window.navigator && window.navigator.clipboard;
      attempt = clipboard && typeof clipboard.writeText === "function" ? Promise.resolve(clipboard.writeText(text)) : null;
    } catch (error) { attempt = Promise.reject(error); }
    var done = function () { show(id, DONE); };
    var fallback = function () { show(id, viaSelection(target) ? DONE : MANUAL); };
    return (attempt ? attempt.then(done, fallback) : Promise.resolve().then(fallback)).catch(function () { show(id, MANUAL); });
  }
  function wire() {
    var buttons = [];
    try { buttons = document.querySelectorAll("button.copy[data-copy-target]"); } catch (error) { return; }
    Array.prototype.forEach.call(buttons, function (button) {
      var id = button.getAttribute("data-copy-target");
      button.hidden = false;
      button.addEventListener("click", function () { return copy(id); });
    });
  }
  if (document.readyState === "loading") { document.addEventListener("DOMContentLoaded", wire); } else { wire(); }
}());
