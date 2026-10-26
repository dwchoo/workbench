// CSP style nonce for the Board page (2026-10-26 사용자 결정). /board's CSP is
// `style-src 'self' 'nonce-<N>'; style-src-attr 'unsafe-inline'`: a <style> element applies only when it carries
// this response's nonce. xterm.js 5.5's DOM renderer creates its two <style> elements with
// document.createElement("style") and fills them with textContent, so the page wraps createElement once, before
// term.open(), and sets the nonce on every <style> it returns. Other tags pass through untouched.
// Pure apart from the document object it is given (node-tested with a fake document; no DOM globals here).
const INSTALLED = Symbol.for("wb.styleNonce.installed");

export function installStyleNonce(doc, nonce) {
  if (typeof nonce !== "string" || nonce === "") return;
  if (doc === null || typeof doc !== "object" || typeof doc.createElement !== "function") return;
  if (doc[INSTALLED]) return;
  const original = doc.createElement;
  doc.createElement = function createElement(tagName, options) {
    const element = original.call(this, tagName, options);
    if (typeof tagName === "string" && tagName.toLowerCase() === "style") element.nonce = nonce;
    return element;
  };
  Object.defineProperty(doc, INSTALLED, { value: true });
}
