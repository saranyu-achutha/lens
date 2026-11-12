/* Copy protection for client logins (2026-11-12, user rule: no copying from the website).
   Applies while <body> has the class "role-client" (set by app.js from /api/status: the shared client login and every
   named client such as timesnow). The developer login keeps normal copy and paste.

   What is blocked (each layer covers a different way of copying):
   - selecting text: CSS user-select none, selectstart cancelled, any selection that still appears is cleared
   - copy / cut events (menu, keyboard, drag-out): cancelled, the clipboard gets an empty string
   - the right-click menu (copy, select all, save, inspect) and the long-press menu on phones
   - shortcuts: Ctrl/Cmd + C, X, A, S, P, U; Ctrl/Cmd + Shift + I, J, C, K; F12
   - dragging text, images or charts out of the page
   - printing / save as PDF: the printout shows only a notice (CSS @media print + beforeprint)
   Text fields (the search box, filters, number inputs) still allow typing, selecting and pasting.
   The server adds the rest: client data feeds opened directly in the browser are refused, CSV downloads are refused,
   and no other site may embed the Console in a frame. A determined user can still take a screenshot or read the
   page with developer tools; this stops ordinary copying, not a technical attack. */
(function () {
  const locked = () => document.body && document.body.classList.contains("role-client");
  const editable = (el) => !!(el && el.closest && el.closest("input, textarea, select, [contenteditable='true']"));
  let lastNote = 0;
  const note = () => {
    const now = Date.now();
    if (now - lastNote < 4000) return;
    lastNote = now;
    try { if (typeof toast === "function") toast("Copying is turned off for this login."); } catch (e) {}
  };
  const stop = (e) => { e.preventDefault(); e.stopPropagation(); };

  ["copy", "cut"].forEach((type) => document.addEventListener(type, (e) => {
    if (!locked() || editable(e.target)) return;
    try { e.clipboardData && e.clipboardData.setData("text/plain", ""); } catch (x) {}
    stop(e); note();
  }, true));

  document.addEventListener("contextmenu", (e) => { if (locked() && !editable(e.target)) { stop(e); note(); } }, true);
  document.addEventListener("selectstart", (e) => { if (locked() && !editable(e.target)) stop(e); }, true);
  document.addEventListener("dragstart", (e) => { if (locked() && !editable(e.target)) stop(e); }, true);

  // a selection made some other way (e.g. a browser menu "Select all") is cleared at once
  document.addEventListener("selectionchange", () => {
    if (!locked()) return;
    const s = window.getSelection && window.getSelection();
    if (!s || s.isCollapsed) return;
    if (editable(document.activeElement)) return;
    s.removeAllRanges();
  });

  document.addEventListener("keydown", (e) => {
    if (!locked()) return;
    const k = (e.key || "").toLowerCase(), mod = e.ctrlKey || e.metaKey;
    const inField = editable(e.target);
    const blocked =
      e.key === "F12" ||
      (mod && e.shiftKey && ["i", "j", "c", "k"].includes(k)) ||          // developer tools / inspect / console
      (mod && !e.shiftKey && ["s", "p", "u"].includes(k)) ||               // save page, print, view source
      (mod && !e.shiftKey && ["c", "x", "a"].includes(k) && !inField);     // copy, cut, select all (outside text fields)
    if (blocked) { stop(e); if (["c", "x", "a"].includes(k) || e.key === "F12") note(); }
  }, true);

  // printing / "save as PDF": the page is hidden in the printout (CSS) and the user is told why
  window.addEventListener("beforeprint", () => { if (locked()) note(); });

  // nothing in the page may copy for a client either (e.g. a "Copy" button)
  const origWrite = navigator.clipboard && navigator.clipboard.writeText && navigator.clipboard.writeText.bind(navigator.clipboard);
  if (origWrite) {
    try {
      navigator.clipboard.writeText = (t) => locked() ? (note(), Promise.resolve()) : origWrite(t);
    } catch (e) {}
  }
  const origExec = document.execCommand && document.execCommand.bind(document);
  if (origExec) {
    document.execCommand = function (cmd) {
      if (locked() && /^(copy|cut)$/i.test(cmd || "")) { note(); return false; }
      return origExec.apply(document, arguments);
    };
  }
})();
