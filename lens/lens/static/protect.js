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
  // the session's decoding key and the watermark text, handed to this page only (removed from the page at once)
  const meta = (n) => { const m = document.querySelector(`meta[name="${n}"]`); const v = m ? m.content : ""; if (m) m.remove(); return v; };
  const KEY = meta("ml-k"), MARK = meta("ml-w");
  const KB = new TextEncoder().encode(KEY);
  window.__mlDec = (text) => {  // client data arrives encoded (lens/shield.py): base64, XOR with the session key
    const bin = atob(text.trim()), out = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i) ^ KB[i % KB.length];
    return JSON.parse(new TextDecoder().decode(out));
  };

  // watermark: the login, IP and time over every page, so a screenshot or photo shows whose login it came from
  function watermark() {
    if (!locked() || !MARK) return;
    let wm = document.getElementById("ml-wm");
    if (!wm) { wm = document.createElement("div"); wm.id = "ml-wm"; wm.setAttribute("aria-hidden", "true"); document.body.appendChild(wm); }
    const when = new Date().toLocaleString("en-GB", { day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" });
    const dark = document.documentElement.getAttribute("data-theme") !== "light";
    const txt = `${MARK} · ${when}`.replace(/[<&>"]/g, "");
    const svg = `<svg xmlns='http://www.w3.org/2000/svg' width='460' height='220'><text x='20' y='120' transform='rotate(-22 230 110)' ` +
      `font-family='Inter, Arial, sans-serif' font-size='15' font-weight='600' fill='${dark ? "#ffffff" : "#1e2a4a"}' fill-opacity='${dark ? 0.075 : 0.07}'>${txt}</text></svg>`;
    wm.style.backgroundImage = `url("data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}")`;
  }
  setInterval(watermark, 30000);
  new MutationObserver(watermark).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
  const bodyWatch = setInterval(() => { if (document.body) { new MutationObserver(watermark).observe(document.body, { attributes: true, attributeFilter: ["class"] }); watermark(); clearInterval(bodyWatch); } }, 50);

  // developer tools: while they are open the data is hidden (a "debugger" pause only happens with them open)
  setInterval(() => {
    if (!locked() || !KEY) return;  // real client logins only (the developer's "Client view" has no session key)
    const t = performance.now();
    // eslint-disable-next-line no-debugger
    debugger;
    document.body.classList.toggle("ml-dt", performance.now() - t > 120);
  }, 1000);

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
