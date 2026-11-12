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
  // the session's decoding key, handed to this page only (removed from the page at once)
  const meta = (n) => { const m = document.querySelector(`meta[name="${n}"]`); const v = m ? m.content : ""; if (m) m.remove(); return v; };
  const KEY = meta("ml-k");
  const KB = new TextEncoder().encode(KEY);
  window.__mlDec = (text) => {  // client data arrives encoded (lens/shield.py): base64, XOR with the session key
    const bin = atob(text.trim()), out = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i) ^ KB[i % KB.length];
    return JSON.parse(new TextDecoder().decode(out));
  };

  // developer tools: while they are open the data is hidden (a "debugger" pause only happens with them open)
  setInterval(() => {
    if (!locked() || !KEY) return;  // real client logins only (the developer's "Client view" has no session key)
    const t = performance.now();
    // eslint-disable-next-line no-debugger
    debugger;
    document.body.classList.toggle("ml-dt", performance.now() - t > 120);
  }, 1000);

  // screen capture (2026-11-12, user: "1 and 2 together"): starting the Snipping Tool, the Mac screenshot tools, Game Bar
  // or a recorder takes focus away from the browser, so the data blanks the moment the window loses focus and comes back
  // when the page is clicked again (a click into the page's own YouTube / Spotify player does not count). Real client
  // logins only. A recording started before the page opened, or running in the background, cannot be seen by a page.
  const player = () => { const a = document.activeElement; return !!(a && a.tagName === "IFRAME"); };
  const away = (on) => { if (document.body) document.body.classList.toggle("ml-away", on); };
  const check = () => { if (!locked() || !KEY) return away(false); away(document.hidden || (!document.hasFocus() && !player())); };
  window.addEventListener("blur", () => setTimeout(check, 0));
  window.addEventListener("focus", check);
  document.addEventListener("visibilitychange", check);
  setInterval(check, 400);  // also when a blur event is missed
  // option 2: the screenshot shortcuts blank the page as they are pressed (Windows key, Win+Shift+S, Cmd+Shift+3/4/5,
  // Alt/Ctrl+PrintScreen), before the capture tool opens
  document.addEventListener("keydown", (e) => {
    if (!locked() || !KEY) return;
    const k = e.key || "";
    if (k === "Meta" || k === "OS" || k === "PrintScreen" || ((e.metaKey || e.ctrlKey) && e.shiftKey) || (e.metaKey && /^[345]$/.test(k))) {
      document.body.classList.add("ml-shot"); clearTimeout(window.__mlShotT);
      window.__mlShotT = setTimeout(() => document.body.classList.remove("ml-shot"), 2500);
    }
  }, true);

  // the screenshot key: the clipboard is emptied and the page blanks for a moment
  document.addEventListener("keyup", (e) => {
    if (!locked() || e.key !== "PrintScreen") return;
    try { navigator.clipboard && navigator.clipboard.writeText && origWriteSafe(" "); } catch (x) {}
    document.body.classList.add("ml-shot"); setTimeout(() => document.body.classList.remove("ml-shot"), 1500);
  }, true);
  const origWriteSafe = (t) => { try { return (window.__mlOrigWrite || ((x) => navigator.clipboard.writeText(x)))(t); } catch (x) {} };
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
    window.__mlOrigWrite = origWrite;
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
