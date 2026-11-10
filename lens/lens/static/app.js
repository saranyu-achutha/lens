"use strict";
const $ = (s, el = document) => el.querySelector(s);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = (n) => (n == null ? "–" : Number(n).toLocaleString("en-IN"));
const compact = (n) => n == null ? "–" : Math.abs(n) >= 1e9 ? `${(n / 1e9).toFixed(2)}B` : Math.abs(n) >= 1e7 ? `${(n / 1e6).toFixed(0)}M` : Math.abs(n) >= 1e6 ? `${(n / 1e6).toFixed(1)}M`
  : Math.abs(n) >= 1e4 ? `${(n / 1e3).toFixed(0)}K` : Math.abs(n) >= 1e3 ? `${(n / 1e3).toFixed(1)}K` : String(Math.round(n));
// Spotify and YouTube Music figures: always one decimal (1.2K, 12.3M, 1.0B)
const compact1 = (n) => n == null ? "–" : Math.abs(n) >= 1e9 ? `${(n / 1e9).toFixed(1)}B` : Math.abs(n) >= 1e6 ? `${(n / 1e6).toFixed(1)}M`
  : Math.abs(n) >= 1e3 ? `${(n / 1e3).toFixed(1)}K` : String(Math.round(n));
const ONE_DEC = new Set(["spotify", "ytmusic", "sp_plays", "ytm_plays", "audio"]);
const pcompact = (platform, n) => (ONE_DEC.has(platform) ? compact1 : compact)(n);
const dur = (ms) => { if (!ms) return "–"; const t = Math.round(ms / 1000); return `${Math.floor(t / 60)}:${String(t % 60).padStart(2, "0")}`; };
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const monthLabel = (ym) => (ym && ym.length >= 7 ? `${MONTHS[+ym.slice(5, 7) - 1]} ${ym.slice(2, 4)}` : ym || "?");

// Focus: new releases only. Older songs (re-uploads of earlier releases) are always hidden unless the
// "Include older songs" box is ticked; it starts unticked on every load and is not remembered.
let FOCUS_ALL = false;
async function api(path, body) {
  const opt = body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
  if (body === undefined && FOCUS_ALL && path.startsWith("/api/")) path += (path.includes("?") ? "&" : "?") + "focus=all";
  const r = await fetch(path, opt);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);
  return data;
}

function toast(msg, bad = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast" + (bad ? " bad" : "");
  t.style.display = "block";
  clearTimeout(toast._t);
  toast._t = setTimeout(() => (t.style.display = "none"), 4000);
}

let STATUS = null;

/* ---------- Router ---------- */
const PAGES = ["overview", "dashboard", "mgmt", "share", "dev", "status", "missed", "added", "crawl", "apis", "charts", "schedule", "platforms", "reports", "daily", "pdata", "catalogue", "explore", "insights", "review", "runs", "outputs", "sources", "settings"];
// The client login sees only these pages (the server refuses everything else for it).
const CLIENT_PAGES = ["dashboard", "mgmt", "share", "platforms", "reports", "daily", "catalogue"];
// A developer can switch to "Client view" to see exactly what the client sees (menu, pages, header); not remembered.
let CLIENT_VIEW = false;
const isClient = () => STATUS?.role === "client" || CLIENT_VIEW;
const hashParams = () => new URLSearchParams(location.hash.split("?")[1] || "");
// Failure-proof pages: a page whose data could not be loaded (server busy, network gone, a bug) shows a notice with
// Retry at the top instead of staying blank; the rest of the Console keeps working.
function pageGuard(sec, load) {
  const page = document.getElementById(`page-${sec}`);
  page?.querySelector(":scope > .page-error")?.remove();
  Promise.resolve().then(load).catch((e) => {
    if (!page || !page.classList.contains("active")) return;
    const box = document.createElement("div");
    box.className = "card notice page-error";
    box.innerHTML = `<b>This page could not load.</b> <span class="muted">${esc(e && e.message ? e.message : String(e))}</span>
      <span class="spacer"></span><button class="small primary">↻ Retry</button>`;
    box.querySelector("button").onclick = () => route();
    const head = page.querySelector(".page-head");
    head ? head.after(box) : page.prepend(box);
  });
}
window.addEventListener("unhandledrejection", (e) => {  // a background refresh that failed: say so once, never break the page
  const now = Date.now();
  if (now - (window.__lastErrToast || 0) > 15000) { window.__lastErrToast = now; toast(`Could not refresh: ${e.reason && e.reason.message || e.reason}`, true); }
});
function route() {
  const home = isClient() ? "dashboard" : "overview";
  const page = (location.hash.slice(1).split("?")[0]) || home;
  const p = (isClient() ? CLIENT_PAGES : PAGES).includes(page) ? page : home;
  const sec = p === "apis" ? "crawl" : p;  // Crawl stats and API stats share one page layout
  document.querySelectorAll("section.page").forEach((s) => s.classList.toggle("active", s.id === `page-${sec}`));
  document.querySelectorAll("a.nav").forEach((a) => a.classList.toggle("active", a.getAttribute("href") === `#${p}`));
  pageGuard(sec, ({ overview: loadOverview, dashboard: loadClient, dev: loadDev, crawl: loadCrawl, apis: loadCrawl, charts: loadCharts, schedule: loadSchedule, status: loadStatus, missed: loadMissed, added: loadAdded, share: loadShare, mgmt: loadMgmt, platforms: loadPlatform, reports: loadReport, daily: loadDaily, pdata: loadPData, catalogue: loadCatalogue, explore: loadExplore, insights: loadInsights, review: loadReview, runs: loadRuns, outputs: loadOutputs, sources: loadSources, settings: loadSettings })[p]);
  const track = new URLSearchParams(location.hash.split("?")[1] || "").get("track");
  if (track) openTrack(track);
  const meta = new URLSearchParams(location.hash.split("?")[1] || "").get("meta");  // e.g. #explore?meta=singer:arijit singh
  if (meta && meta.includes(":")) openMeta(meta.split(":")[0], meta.split(":").slice(1).join(":"));
  const pl = new URLSearchParams(location.hash.split("?")[1] || "").get("play");  // e.g. #catalogue?play=youtube:VIDEO_ID
  if (pl && pl.includes(":")) play(pl.split(":")[0], pl.split(":")[1], "");
}
window.addEventListener("hashchange", route);
function renderFocus() {
  const b = $("#focus-older"); if (b) b.checked = FOCUS_ALL;
}
document.addEventListener("change", (e) => {
  if (e.target.id !== "focus-older") return;
  FOCUS_ALL = e.target.checked;
  route();
});
renderFocus();

/* ---------- Status / header ---------- */
async function refreshStatus() {
  try { STATUS = await api("/api/status"); } catch { return; }
  document.body.classList.toggle("role-client", isClient());
  $("#who").textContent = STATUS.role === "client" ? "Client" : CLIENT_VIEW ? "Developer · viewing as client" : "Developer";
  const cv = $("#client-view");
  cv.style.display = STATUS.role === "developer" ? "" : "none";
  cv.textContent = CLIENT_VIEW ? "✕ Exit client view" : "👁 Client view";
  if (isClient()) {  // no jobs, schedule, review or settings for the client login
    $("#side-asof").textContent = STATUS.counts.last_capture ? `Data as of ${STATUS.counts.last_capture}` : "";
    $("#side-scope").textContent = STATUS.config.youtube.labels.filter((l) => l.enabled).map((l) => l.name).join(", ") || "none";
    $("#top-lastok").textContent = STATUS.last_ok ? `Updated ${new Date(STATUS.last_ok).toLocaleString([], { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })}` : "";
    return;
  }
  const j = STATUS.job, b = STATUS.browser_job || {}, pill = $("#job-pill");
  const names = [j, b].filter((x) => x.running).map((x) => esc(STATUS.jobs[x.job] || x.job));
  if (names.length) { pill.className = "pill run"; pill.innerHTML = `<span class="dot"></span>Running: ${names.join(" + ")}`; }
  else if (j.status === "failed") { pill.className = "pill bad"; pill.innerHTML = `<span class="dot"></span>Last run failed`; }
  else { pill.className = "pill"; pill.innerHTML = `<span class="dot"></span>Idle`; }
  const sp = $("#sched-pill");
  const sc = STATUS.config, parts = [];
  if (sc.schedule.enabled) parts.push(`pipeline daily ${esc(sc.schedule.time)}`);
  // every_hours: how often the scheduler starts the whole pipeline; fresh_every_hours: how often each video is read
  if (sc.youtube.enabled) {
    const fr = +sc.youtube.fresh_every_hours || 24;
    parts.push(`Pipeline every ${sc.youtube.every_hours} h · YouTube readings ${fr >= 24 ? "daily" : `every ${fr} h (first ${sc.youtube.fresh_days || 7} days)`}`);
  }
  sp.className = "pill" + (parts.length ? " ok" : "");
  sp.innerHTML = `<span class="dot"></span>${parts.length ? parts.join(" · ") : "Schedule off"}`;
  $("#top-lastok").textContent = STATUS.last_ok ? `Last run ${new Date(STATUS.last_ok).toLocaleString([], { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })}` : "";
  $("#side-asof").textContent = STATUS.counts.last_capture ? `Data as of ${STATUS.counts.last_capture}` : "";
  $("#side-scope").textContent = STATUS.config.youtube.labels.filter((l) => l.enabled).map((l) => l.name).join(", ") || "none";
  $("#top-run").disabled = j.running;
  $("#run-start") && ($("#run-start").disabled = j.running);
  $("#run-cancel") && ($("#run-cancel").disabled = !j.running);
  if (j.running) startJobPoll();
  if (location.hash.startsWith("#sources")) renderHealth();
  const badge = $("#review-badge"), n = STATUS.counts.yt_pending || 0;
  badge.textContent = n; badge.style.display = n ? "" : "none";
}

async function startRun(since, until, job) {
  try {
    await api("/api/run", { job: job || $("#run-job").value || "pipeline", since, until });
    toast("Job started");
    location.hash = "#runs";
    CONSOLE.from = 0; $("#console").innerHTML = "";
    await refreshStatus();
  } catch (e) { toast(e.message, true); }
}
$("#top-run").onclick = () => startRun(undefined, undefined, "pipeline");

/* ---------- Charts ---------- */
const NS = "http://www.w3.org/2000/svg";
function svgEl(tag, attrs, parent) {
  const e = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
  parent && parent.appendChild(e);
  return e;
}
// Vertical gradient for bars and areas; the colour can be a CSS variable, so it follows the theme.
function gradient(svg, id, color, top, bottom) {
  const defs = svg.querySelector("defs") || svgEl("defs", {}, svg);
  const g = svgEl("linearGradient", { id, x1: 0, y1: 0, x2: 0, y2: 1 }, defs);
  svgEl("stop", { offset: "0%", style: `stop-color:${color};stop-opacity:${top}` }, g);
  svgEl("stop", { offset: "100%", style: `stop-color:${color};stop-opacity:${bottom}` }, g);
}
function niceMax(v) {
  if (v <= 0) return 4;
  const p = 10 ** Math.floor(Math.log10(v)), n = v / p;
  return (n <= 1 ? 1 : n <= 2 ? 2 : n <= 5 ? 5 : 10) * p;
}
function tooltipFor(el) {
  let t = $(".tooltip", el);
  if (!t) { t = document.createElement("div"); t.className = "tooltip"; el.appendChild(t); }
  return t;
}
// Rounded-top bar anchored to the baseline.
function barPath(x, y, w, h, r = 4) {
  r = Math.min(r, w / 2, h);
  return `M${x},${y + h}V${y + r}Q${x},${y} ${x + r},${y}H${x + w - r}Q${x + w},${y} ${x + w},${y + r}V${y + h}Z`;
}

function barChart(el, data, { height = 230, onClick, tip, color = "var(--series-1)", hover = "var(--series-1-hover)" } = {}) {
  el.innerHTML = "";
  if (!data.length) { el.innerHTML = `<div class="empty">No data</div>`; return; }
  const W = el.clientWidth || 520, H = height, m = { t: 18, r: 8, b: 26, l: 36 };
  const iw = W - m.l - m.r, ih = H - m.t - m.b;
  const max = niceMax(Math.max(...data.map((d) => d.value)));
  const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, height: H, role: "img", "aria-label": "bar chart" }, el);
  // ids are page-wide: each chart needs its own, or url(#id) can point at a hidden chart's gradient and the bars vanish
  const gid = `bar-grad-${Math.random().toString(36).slice(2, 7)}`;
  gradient(svg, gid, color, 1, 0.55);
  const g = svgEl("g", { class: "grid" }, svg), ax = svgEl("g", { class: "axis" }, svg);
  for (let i = 0; i <= 4; i++) {
    const y = m.t + ih - (ih * i) / 4;
    svgEl("line", { x1: m.l, x2: W - m.r, y1: y, y2: y }, g);
    svgEl("text", { x: m.l - 8, y: y + 4, "text-anchor": "end" }, ax).textContent = max >= 1e5 ? compact((max * i) / 4) : fmt((max * i) / 4);
  }
  const step = iw / data.length, bw = Math.max(4, Math.min(44, step - 2 - step * 0.25));
  const tt = tooltipFor(el);
  const maxIdx = data.reduce((bi, d, i) => (d.value > data[bi].value ? i : bi), 0);
  data.forEach((d, i) => {
    const h = (d.value / max) * ih, x = m.l + step * i + (step - bw) / 2, y = m.t + ih - h;
    const bar = svgEl("path", { d: barPath(x, y, bw, Math.max(h, 0.5)), fill: `url(#${gid})`, class: "bar", style: `animation-delay:${Math.min(i * 12, 400)}ms` }, svg);
    if (i === maxIdx) svgEl("text", { x: x + bw / 2, y: y - 6, "text-anchor": "middle", fill: "var(--text-secondary)", "font-size": 11 }, svg).textContent = d.value >= 1e5 ? compact(d.value) : fmt(d.value);
    if (data.length <= 14 || i % Math.ceil(data.length / 12) === 0)
      svgEl("text", { x: x + bw / 2, y: H - 8, "text-anchor": "middle" }, ax).textContent = d.label;
    const hit = svgEl("rect", { x: m.l + step * i, y: m.t, width: step, height: ih, fill: "transparent", style: onClick ? "cursor:pointer" : "" }, svg);
    hit.addEventListener("mouseenter", () => {
      bar.setAttribute("fill", hover);
      tt.innerHTML = tip ? tip(d) : `${esc(d.label)}: <b>${fmt(d.value)}</b>`;
      tt.style.left = `${((x + bw / 2) / W) * 100}%`; tt.style.top = `${(y / H) * el.clientHeight}px`; tt.style.display = "block";
    });
    hit.addEventListener("mouseleave", () => { bar.setAttribute("fill", `url(#${gid})`); tt.style.display = "none"; });
    if (onClick) hit.addEventListener("click", () => onClick(d));
  });
}

function lineChart(el, points, { height = 200, yMax = null, label = "Popularity", marks = [] } = {}) {
  el.innerHTML = "";
  if (!points.length) { el.innerHTML = `<div class="empty">No readings yet</div>`; return; }
  if (yMax == null) yMax = niceMax(Math.max(...points.map((p) => p.y)));
  const W = el.clientWidth || 500, H = height, m = { t: 14, r: 14, b: 26, l: 44 };
  const iw = W - m.l - m.r, ih = H - m.t - m.b;
  const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, height: H, role: "img", "aria-label": `${label} over time` }, el);
  const g = svgEl("g", { class: "grid" }, svg), ax = svgEl("g", { class: "axis" }, svg);
  for (let i = 0; i <= 4; i++) {
    const y = m.t + ih - (ih * i) / 4;
    svgEl("line", { x1: m.l, x2: W - m.r, y1: y, y2: y }, g);
    svgEl("text", { x: m.l - 8, y: y + 4, "text-anchor": "end" }, ax).textContent = compact((yMax * i) / 4);
  }
  const t0 = Date.parse(points[0].x), t1 = Date.parse(points[points.length - 1].x), span = Math.max(t1 - t0, 1);
  const X = (p) => (points.length === 1 ? m.l + iw / 2 : m.l + ((Date.parse(p.x) - t0) / span) * iw);
  const Y = (p) => m.t + ih - (p.y / yMax) * ih;
  const linePath = points.map((p, i) => `${i ? "L" : "M"}${X(p)},${Y(p)}`).join("");
  const aid = `line-area-${Math.random().toString(36).slice(2, 7)}`;
  gradient(svg, aid, "var(--series-1)", 0.28, 0);
  if (points.length > 1) svgEl("path", { d: `${linePath}L${X(points[points.length - 1])},${m.t + ih}L${X(points[0])},${m.t + ih}Z`, fill: `url(#${aid})`, class: "area" }, svg);
  svgEl("path", { d: linePath, fill: "none", stroke: "var(--series-1)", "stroke-width": 2.5, "stroke-linejoin": "round", "stroke-linecap": "round", pathLength: 1, class: "line" }, svg);
  (marks || []).forEach((mk) => { const p = points.find((q) => q.x === mk.x); if (!p) return;
    svgEl("line", { x1: X(p), x2: X(p), y1: m.t, y2: m.t + ih, stroke: "var(--warning)", "stroke-dasharray": "4 4", "stroke-width": 1.2 }, svg);
    svgEl("text", { x: X(p) + 4, y: m.t + 10, fill: "var(--warning)", "font-size": 10.5 }, svg).textContent = mk.label; });
  if (points.length <= 40) points.forEach((p) => svgEl("circle", { cx: X(p), cy: Y(p), r: 4, fill: "var(--series-1)", stroke: "var(--surface-1)", "stroke-width": 2 }, svg));
  [points[0], points[points.length - 1]].forEach((p, i) => {
    if (i && points.length === 1) return;
    svgEl("text", { x: X(p), y: H - 8, "text-anchor": i ? "end" : "start" }, ax).textContent = p.x;
  });
  const cross = svgEl("line", { y1: m.t, y2: m.t + ih, stroke: "var(--text-muted)", "stroke-dasharray": "3 3", visibility: "hidden" }, svg);
  const dot = svgEl("circle", { r: 5, fill: "var(--series-1)", stroke: "var(--surface-1)", "stroke-width": 2, visibility: "hidden" }, svg);
  const tt = tooltipFor(el);
  const hit = svgEl("rect", { x: m.l, y: m.t, width: iw, height: ih, fill: "transparent" }, svg);
  hit.addEventListener("mousemove", (ev) => {
    const r = svg.getBoundingClientRect(), mx = ((ev.clientX - r.left) / r.width) * W;
    const p = points.reduce((a, b) => (Math.abs(X(b) - mx) < Math.abs(X(a) - mx) ? b : a));
    cross.setAttribute("x1", X(p)); cross.setAttribute("x2", X(p)); cross.setAttribute("visibility", "visible");
    dot.setAttribute("cx", X(p)); dot.setAttribute("cy", Y(p)); dot.setAttribute("visibility", "visible");
    tt.innerHTML = `${esc(p.x)}<br>${esc(label)} <b>${fmt(p.y)}</b>`;
    tt.style.left = `${(X(p) / W) * 100}%`; tt.style.top = `${(Y(p) / H) * el.clientHeight}px`; tt.style.display = "block";
  });
  hit.addEventListener("mouseleave", () => { cross.setAttribute("visibility", "hidden"); dot.setAttribute("visibility", "hidden"); tt.style.display = "none"; });
}

const popCell = (v) => v == null ? `<td class="num muted">–</td>` :
  `<td><div class="popbar"><span class="num">${fmt(v)}</span><span class="track"><span class="fill" style="width:${v}%"></span></span></div></td>`;
const deltaCell = (d) => d == null ? `<td class="num muted">–</td>` :
  `<td class="num ${d > 0 ? "delta-up" : d < 0 ? "delta-down" : "muted"}">${d > 0 ? "▲ +" : d < 0 ? "▼ " : ""}${fmt(d)}</td>`;
const VTYPES = [["video", "Video"], ["lyric", "Lyric"], ["audio", "Audio"], ["short", "Shorts"], ["teaser", "Teaser"], ["other", "Other"]];
const typeLine = (r) => VTYPES.filter(([t]) => r[`yt_n_${t}`]).map(([t, l]) => `<b>${l}</b> ${r[`yt_n_${t}`]}×${r[`yt_views_${t}`] != null ? " " + compact(r[`yt_views_${t}`]) : ""}`).join(" · ");
const viewsTypeCell = (r) => `<td class="num">${r.yt_views == null ? '<span class="muted">–</span>' : compact(r.yt_views)}${typeLine(r) ? `<div class="vt">${typeLine(r)}</div>` : ""}</td>`;
const viewsCell = (v) => `<td class="num">${v == null ? '<span class="muted">–</span>' : compact(v)}</td>`;
const ytCell = (r) => `<td class="num">${r.yt_videos ? r.yt_videos : '<span class="muted">0</span>'}${r.yt_pending ? ` <a href="#review" class="pill warn" style="padding:0 6px" title="awaiting review">+${r.yt_pending}</a>` : ""}</td>`;
const metaLine = (m) => !m ? "" : [m.music_director && `<b>Music</b> ${esc(m.music_director)}`, m.film && `<b>Film</b> ${esc(m.film)}`,
  m.language && esc(m.language), m.category && esc(m.category)].filter(Boolean).join(" · ");
// Each tracked label has its own gradient (colours in style.css .lb-*, tuned for light and dark).
const LABEL_KEY = { "Sony Music India": "sony", "T-Series": "tseries", "Saregama": "saregama", "Zee Music Company": "zee", "Aditya Music": "aditya", "Think Music": "think", "Times Music": "times" };
const labelTag = (name) => name ? `<span class="label-tag lb-${LABEL_KEY[name] || "other"}">${esc(name)}</span>` : "";
const trackCell = (r) => `<td><div class="t1">${esc(r.title)}${labelTag(r.music_label)}</div>
  <div class="t2">${esc((r.meta && r.meta.singer) || r.artists || r.yt_channel || "")}</div>${r.meta ? `<div class="meta-line">${metaLine(r.meta)}</div>` : ""}</td>`;

/* ---------- Overview ---------- */
const STATUS_LABEL = { new: "New release", catalogue: "Older release", not_on_spotify: "Not on Spotify", pending: "Spotify match in review", unchecked: "Spotify lookup pending" };
const statusChip = (st) => `<span class="st st-${esc(st || "none")}">${esc(STATUS_LABEL[st] || "–")}</span>`;
async function loadOverview() {
  const [ov] = await Promise.all([api("/api/overview"), STATUS || refreshStatus()]);
  const k = ov.kpi;
  $("#ov-window").textContent = `first uploaded ${k.since} → ${k.until}`;
  const empty = !k.songs;
  $("#ov-empty").style.display = empty ? "block" : "none";
  $("#ov-body").style.display = empty ? "none" : "block";
  if (empty) return;
  // Client-facing headline numbers: output, reach, momentum, hits and the leading label.
  const dmy = (d) => new Date(d + "T00:00:00Z").toLocaleDateString("en-GB", { day: "numeric", month: "short", timeZone: "UTC" });
  const labs = ov.labels.filter((l) => l.label && !l.label.startsWith("("));
  const lead = labs.slice().sort((a, b) => (b.views || 0) - (a.views || 0))[0];
  const pct = (n, d) => (d ? Math.round((100 * (n || 0)) / d) : 0);
  const rise = ov.risers[0];
  const on = k.on_platform || {};
  const tiles = [["Total songs", fmt(k.songs), [["youtube", "YouTube"], ["ytmusic", "YouTube Music"], ["spotify", "Spotify"], ["jiosaavn", "JioSaavn"], ["apple", "Apple Music"], ["deezer", "Deezer"]]
      .map(([p, l]) => `${l} ${fmt(on[p] || 0)}`).join(" · ")],
    ["Fastest riser", rise ? rise.title : "–", rise ? `+${compact(rise.yt_delta)} views in the last day${rise.music_label ? ` · ${rise.music_label}` : ""}` : "shown after the next daily reading"],
    ["Songs with 1M+ views", fmt(k.hits), `${pct(k.hits, k.songs)}% of all songs`],
    ["Top label", lead ? lead.label : "–", lead ? `${pct(lead.views, k.views)}% of all views · ${fmt(lead.songs)} songs` : ""]];
  $("#ov-kpis").innerHTML = tiles.map(([l, v, x], i) => `<div class="card kpi${l === "Fastest riser" && rise ? " kpi-song click" : ""}"${l === "Fastest riser" && rise ? ` data-id="${rise.id}" title="Play ${esc(rise.title)}"` : ""}><div class="label">${l}</div><div class="value">${esc(v)}</div><div class="sub">${esc(x)}</div></div>`).join("");
  const rt = $("#ov-kpis .kpi-song");
  if (rt) rt.onclick = () => { openTrack(rise.id); if (rise.yt_video_id) play("youtube", rise.yt_video_id, rise.title); };
  $("#ov-total").textContent = `${fmt(k.songs)} songs in total`;
  barChart($("#ov-month"), ov.by_day.map((d) => ({ label: d.day.slice(5), value: d.songs, day: d.day, new: d.new })), {
    tip: (d) => `<b>${esc(d.day)}</b><br>${fmt(d.value)} songs · ${fmt(d.new)} new releases`,
    onClick: (d) => { Object.assign(CAT, { day: d.day, status: "", channel: "", label: "", offset: 0 }); location.hash = "#catalogue"; },
  });
  const nums = (c) => `<td class="num">${fmt(c.songs)}</td><td class="num">${fmt(c.new)}</td><td class="num">${compact(c.views)}</td>`;
  $("#ov-labels").innerHTML = `<thead><tr><th>Label / channel</th><th class="num">Songs</th><th class="num">New</th><th class="num">Views</th></tr></thead><tbody>` +
    ov.labels.map((l) => `<tr class="label-row" data-label="${esc(l.label)}" style="cursor:pointer"><td>${labelTag(l.label)}</td>${nums(l)}</tr>` +
      ov.channels.filter((c) => c.label && c.label === l.label).map((c) => `<tr class="ch-row" data-ch="${esc(c.channel)}" style="cursor:pointer"><td>${esc(c.channel)}</td>${nums(c)}</tr>`).join("")).join("") + "</tbody>";
  const tbl = (rows, extra) => `<thead><tr><th>Play</th><th>Song</th><th>First upload</th><th>Spotify</th>${extra ? "<th class='num'>Views gained</th>" : ""}<th class="num">YouTube views</th></tr></thead><tbody>` +
    rows.map((r) => `<tr class="click" data-id="${r.id}">${playBtns(r)}${trackCell(r)}<td class="muted">${esc(r.song_date)}</td><td>${statusChip(r.release_status)}</td>${extra ? `<td class="num delta-up">+${compact(r.yt_delta)}</td>` : ""}${viewsCell(r.yt_views)}</tr>`).join("") + "</tbody>";
  api("/api/client").then((cd) => {
    const render = () => { $("#ov-pf-kpis").innerHTML = glanceTiles(cd, false); wireRatio($("#ov-pf-kpis"), render); };
    render();
    $("#ov-pf-asof").textContent = `figures for ${dayLabel(cd.as_of)}, the last completed day`;
    $("#ov-pf-audio").innerHTML = audioCards(cd);
    growthCharts($("#ov-pf-trend-yt"), $("#ov-pf-trend-au"), $("#ov-pf-legend"), cd);
    // gains on the last completed day, as on the Dashboard: Fastest riser tile and the gainers table
    const r0 = cd.risers[0], tile = [...document.querySelectorAll("#ov-kpis .kpi")].find((x) => x.querySelector(".label")?.textContent === "Fastest riser");
    if (tile && r0) {
      tile.querySelector(".value").textContent = r0.title;
      tile.querySelector(".sub").textContent = `+${compact(r0.yt_gain)} views on ${cd.as_of}${r0.label ? ` · ${r0.label}` : ""}`;
      tile.dataset.id = r0.id; tile.classList.add("kpi-song", "click");
      tile.onclick = () => { openTrack(r0.id); if (r0.yt_video_id) play("youtube", r0.yt_video_id, r0.title); };
    }
    $("#ov-risers-title").textContent = `Biggest YouTube gainers (${cd.as_of || "last completed day"})`;
    $("#ov-risers").innerHTML = cd.risers.length ? `<thead><tr><th>#</th><th></th><th>Song</th><th class="num">Views gained</th><th class="num">Streams gained</th><th class="num">YouTube views</th></tr></thead><tbody>` +
      cd.risers.map((r, i) => songRow(r, i, (r) => `<td class="num delta-up">+${compact(r.yt_gain)}</td><td class="num ${r.audio_gain > 0 ? "delta-up" : "muted"}">${r.audio_gain ? "+" + compact(r.audio_gain) : "–"}</td><td class="num">${compact(r.yt_views)}</td>`)).join("") + "</tbody>"
      : `<tbody><tr><td class="empty">Growth appears after two days of readings.</td></tr></tbody>`;
  }).catch(() => {});
  $("#ov-top").innerHTML = ov.top.length ? tbl(ov.top) : `<tbody><tr><td class="empty">No readings yet. Run the pipeline.</td></tr></tbody>`;
  $("#ov-risers").innerHTML = ov.risers.length ? tbl(ov.risers, true) :
    `<tbody><tr><td class="empty">Needs readings on two different days. Day-on-day gains appear after tomorrow's sync.</td></tr></tbody>`;
}
$("#ov-labels").addEventListener("click", (e) => {
  const lr = e.target.closest("tr[data-label]");
  if (lr && !lr.dataset.label.startsWith("(")) { Object.assign(CAT, { label: lr.dataset.label, channel: "", day: "", status: "", offset: 0 }); location.hash = "#catalogue"; return; }
  const tr = e.target.closest("tr[data-ch]"); if (!tr || tr.dataset.ch.startsWith("(")) return;
  Object.assign(CAT, { channel: tr.dataset.ch, label: "", day: "", status: "", offset: 0 }); location.hash = "#catalogue";
});

/* ---------- Platforms: one dashboard per platform ---------- */
const PF = { p: "all" };
async function loadPlatform() {
  document.querySelectorAll("#pf-tabs .tab").forEach((b) => b.classList.toggle("active", b.dataset.pf === PF.p));
  const d = await api(`/api/platform?p=${PF.p}`);
  $("#pf-window").textContent = `Songs first uploaded ${d.since} → ${d.until} (${fmt(d.songs)} songs)`;
  const num = (v) => typeof v === "number" ? (Math.abs(v) >= 1e5 ? compact(v) : fmt(v)) : (v == null ? "–" : v);
  $("#pf-kpis").innerHTML = d.kpis.map(([l, v, x]) => `<div class="card kpi"><div class="label">${esc(l)}</div><div class="value">${esc(num(v))}</div><div class="sub">${esc(x || "")}</div></div>`).join("");
  $("#pf-legend").innerHTML = "";
  if (d.lines) {  // cross-platform: one coloured line per platform
    $("#pf-chart-title").textContent = d.chart.title;
    const series = d.lines.map((l) => ({ name: l.name, color: PCOLOR[l.platform], points: l.points })).filter((x) => x.points.length);
    multiLine($("#pf-chart"), series);
    $("#pf-legend").innerHTML = d.lines.map((l) => `<span><span style="display:inline-block;width:12px;height:3px;background:${PCOLOR[l.platform]};vertical-align:middle;margin-right:5px"></span>${esc(l.name)}${l.points.length ? "" : ' <span class="muted">(from tomorrow)</span>'}</span>`).join("");
  } else {
    const big = Math.max(0, ...d.chart.bars.map((b) => b.value || 0)) >= 1e6;  // show large counts in millions
    $("#pf-chart-title").textContent = d.chart.title + (big ? " (millions)" : "");
    barChart($("#pf-chart"), d.chart.bars.map((b) => ({ label: b.label.length > 10 ? b.label.slice(0, 9) + "…" : b.label, full: b.label,
      value: big ? Math.round((b.value || 0) / 1e5) / 10 : b.value, raw: b.value })), { tip: (b) => `<b>${esc(b.full)}</b><br>${num(b.raw)}` });
  }
  $("#pf-table-title").textContent = d.table.title;
  $("#pf-table").innerHTML = d.table.rows.length ? `<thead><tr>${d.table.cols.map((c, i) => `<th class="${i ? "num" : ""}">${esc(c)}</th>`).join("")}</tr></thead><tbody>` +
    d.table.rows.map(([id, ...cells]) => `<tr class="click" data-id="${id}">${cells.map((c, i) => i ? `<td class="num">${typeof c === "number" ? num(c) : esc(c ?? "–")}</td>`
      : `<td><div class="t1">${esc(c)}</div></td>`).join("")}</tr>`).join("") + "</tbody>"
    : `<tbody><tr><td class="empty">Nothing yet for this period.</td></tr></tbody>`;
}
document.querySelectorAll("#pf-tabs .tab").forEach((b) => (b.onclick = () => { PF.p = b.dataset.pf; loadPlatform(); }));
$("#pf-table").addEventListener("click", (e) => { const tr = e.target.closest("tr[data-id]"); if (tr && tr.dataset.id !== "null") openTrack(tr.dataset.id); });

/* ---------- Platform numbers shared by the Dashboard and the Overview: YouTube views and audio streams apart ---------- */
const RATIO_KEY = "ml_ratio";
const ratioPick = () => { try { const r = JSON.parse(localStorage.getItem(RATIO_KEY) || "null"); if (r && r.num && r.den) return r; } catch (e) {} return { num: "spotify", den: "youtube" }; };
const ratioSave = (r) => { try { localStorage.setItem(RATIO_KEY, JSON.stringify(r)); } catch (e) {} };
function ratioOptions(d) {  // every platform with a number, plus all audio streams together
  const o = d.platforms.filter((p) => p.total != null).map((p) => ({ key: p.platform, name: p.name.replace(/ (views|plays)$/, ""), total: p.total, gain: p.gain }));
  o.push({ key: "audio", name: "All audio streams", total: d.audio, gain: d.audio_gain });
  return o;
}
const dayLabel = (d) => d ? new Date(d + "T00:00:00Z").toLocaleDateString("en-GB", { day: "numeric", month: "short", timeZone: "UTC" }) : "the last day";
const gainChip = (platform, g, d) => g ? `<span class="gain-chip">+${pcompact(platform, g)} on ${dayLabel(d.as_of)}</span>` : `<span class="gain-chip none">no gain on ${dayLabel(d.as_of)}</span>`;
function ratioTile(d) {
  const opts = ratioOptions(d), r = ratioPick();
  const num = opts.find((o) => o.key === r.num) || opts.find((o) => o.key === "spotify") || opts[0];
  const den = opts.find((o) => o.key === r.den) || opts.find((o) => o.key === "youtube") || opts[0];
  const v = den && den.total ? num.total / den.total : null, vg = den && den.gain ? (num.gain || 0) / den.gain : null;
  const f = (x) => x == null ? "–" : x >= 10 ? x.toFixed(1) : x.toFixed(2);
  const sel = (which, cur) => `<select class="ratio-sel" data-w="${which}">${opts.map((o) =>
    `<option value="${o.key}"${o.key === cur.key ? " selected" : ""}>${esc(o.name)}</option>`).join("")}</select>`;
  return `<div class="card kpi" style="--kpi-c:var(--accent-2)"><div class="label">${esc(num.name)} per 100 ${esc(den.name)}</div>
    <div class="value">${v == null ? "–" : f(v * 100)}</div>
    <div class="gainline">${vg == null ? "" : `<span class="gain-chip none">${f(vg * 100)} on ${dayLabel(d.as_of)}</span>`}</div>
    <div class="explain">Ratio ${f(v)} : 1 over all songs. Compare any two:</div>
    <div class="ratio-row">${sel("num", num)} per 100 ${sel("den", den)}</div></div>`;
}
function wireRatio(root, rerender) {
  root.querySelectorAll(".ratio-sel").forEach((x) => (x.onchange = () => {
    const r = ratioPick(); r[x.dataset.w] = x.value; ratioSave(r); rerender(); }));
}
// At a glance: YouTube views, audio streams, the ratio and (Dashboard) songs released
function glanceTiles(d, withSongs = true) {
  const pf = Object.fromEntries(d.platforms.map((p) => [p.platform, p]));
  const t = (label, platform, color, total, gain, explain) => `<div class="card kpi" style="--kpi-c:${color}"><div class="label">${label}</div>
    <div class="value">${pcompact(platform, total)}</div><div class="gainline">${gainChip(platform, gain, d)}</div><div class="explain">${explain}</div></div>`;
  return t("YouTube views", "youtube", PCOLOR.youtube, d.yt_views, d.yt_gain, `All uploads of ${fmt(pf.youtube?.songs || 0)} songs: videos, lyric, audio and Shorts.`) +
    t("Audio streams", "audio", PCOLOR.spotify, d.audio, d.audio_gain, "Spotify + YouTube Music + JioSaavn plays. Counted apart from YouTube views.") +
    ratioTile(d) +
    (withSongs ? `<div class="card kpi"><div class="label">Songs released</div><div class="value">${fmt(d.songs)}</div>
      <div class="gainline"><span class="gain-chip none">${fmt(d.new)} confirmed new on Spotify</span></div>
      <div class="explain">First uploaded on the labels' YouTube channels from ${esc(d.since)}.</div></div>` : "");
}
// Audio streams by platform: total, the day's gain, songs and share of all streams
function audioCards(d) {
  return d.platforms.filter((p) => ["spotify", "ytmusic", "jiosaavn"].includes(p.platform)).map((p) => {
    const share = d.audio ? Math.round((100 * (p.total || 0)) / d.audio) : 0;
    return `<div class="card pf-card" style="--pc:${PCOLOR[p.platform]}"><div class="pf-name"><span class="pf-dot"></span>${esc(p.name.replace(/ plays$/, ""))}</div>
      <div class="pf-total">${pcompact(p.platform, p.total)}</div>${gainChip(p.platform, p.gain, d)}
      <div class="share-track"><span style="width:${share}%"></span></div>
      <div class="pf-meta">${share}% of audio streams · on ${fmt(p.songs)} songs</div></div>`; }).join("");
}
// Daily growth: YouTube and audio on two charts, each with its own axis
function growthCharts(ytEl, auEl, auLegend, d) {
  const yt = d.trend.filter((l) => l.platform === "youtube"), au = d.trend.filter((l) => l.platform !== "youtube");
  multiLine(ytEl, yt.map((l) => ({ name: l.name, color: PCOLOR[l.platform], points: l.points })).filter((l) => l.points.length), { height: 260 });
  multiLine(auEl, au.map((l) => ({ name: l.name, color: PCOLOR[l.platform], points: l.points })).filter((l) => l.points.length), { height: 260 });
  auLegend.innerHTML = legendHtml(au);
}
// Songs: top and fastest growing, by YouTube views or by audio streams (a switch, not mixed columns)
const SONG_MODE = { top: "yt", rise: "yt" };
function songTables(d, topEl, riseEl, riseTitleEl) {
  const perAudio = (r) => `<td class="num">${compact1(r.sp_plays)}</td><td class="num">${compact1(r.ytm_plays)}</td>${viewsCell(r.js_plays)}`;
  const top = SONG_MODE.top === "yt" ? d.top : d.top_audio;
  topEl.innerHTML = SONG_MODE.top === "yt"
    ? `<thead><tr><th>#</th><th></th><th>Song</th><th class="num">YouTube views</th><th class="num">Gained ${dayLabel(d.as_of)}</th></tr></thead><tbody>` +
      top.map((r, i) => songRow(r, i, (r) => `<td class="num"><b>${compact(r.yt_views)}</b></td><td class="num ${r.yt_gain > 0 ? "delta-up" : "muted"}">${r.yt_gain ? "+" + compact(r.yt_gain) : "–"}</td>`)).join("") + "</tbody>"
    : `<thead><tr><th>#</th><th></th><th>Song</th><th class="num">Streams</th><th class="num">Spotify</th><th class="num">YT Music</th><th class="num">JioSaavn</th></tr></thead><tbody>` +
      top.map((r, i) => songRow(r, i, (r) => `<td class="num"><b>${compact(r.audio)}</b></td>${perAudio(r)}`)).join("") + "</tbody>";
  const rise = SONG_MODE.rise === "yt" ? d.risers : d.risers_audio;
  riseTitleEl.textContent = `Fastest growing on ${dayLabel(d.as_of)}`;
  riseEl.innerHTML = rise.length ? (SONG_MODE.rise === "yt"
    ? `<thead><tr><th>#</th><th></th><th>Song</th><th class="num">Views gained</th><th class="num">YouTube views</th></tr></thead><tbody>` +
      rise.map((r, i) => songRow(r, i, (r) => `<td class="num delta-up"><b>+${compact(r.yt_gain)}</b></td><td class="num">${compact(r.yt_views)}</td>`)).join("") + "</tbody>"
    : `<thead><tr><th>#</th><th></th><th>Song</th><th class="num">Streams gained</th><th class="num">Streams</th></tr></thead><tbody>` +
      rise.map((r, i) => songRow(r, i, (r) => `<td class="num delta-up"><b>+${compact(r.audio_gain)}</b></td><td class="num">${compact(r.audio)}</td>`)).join("") + "</tbody>")
    : `<tbody><tr><td class="empty">Growth appears after two days of readings.</td></tr></tbody>`;
}
function wireSongSwitch(root, rerender) {
  root.querySelectorAll(".song-mode").forEach((g) => g.querySelectorAll("button").forEach((b) => {
    b.classList.toggle("on", SONG_MODE[g.dataset.k] === b.dataset.m);
    b.onclick = () => { SONG_MODE[g.dataset.k] = b.dataset.m; rerender(); };
  }));
}
function platformTiles(d) { return glanceTiles(d, false); }  // kept for older callers
function platformTrend(el, legendEl, d) {
  const lines = d.trend.map((l) => ({ name: l.name, color: PCOLOR[l.platform], axis: l.axis, points: l.points }));
  multiLine(el, lines.filter((l) => l.points.length), { height: 330 });
  legendEl.innerHTML = legendHtml(d.trend);
}

/* ---------- Dashboard (client home): reach and growth in plain terms ---------- */
const legendHtml = (lines) => lines.map((l) => `<span><span style="display:inline-block;width:12px;height:3px;background:${PCOLOR[l.platform]};vertical-align:middle;margin-right:5px"></span>${esc(l.name)}</span>`).join("");
const playOne = (r) => `<td class="play-cell">${r.yt_video_id ? `<button class="pbtn yt" data-play="youtube" data-pid="${esc(r.yt_video_id)}" data-ptitle="${esc(r.title)}" title="Play on YouTube">▶</button>` : `<span class="pbtn yt off">▶</span>`}</td>`;
const songRow = (r, i, extra) => `<tr class="click" data-id="${r.id}"><td class="muted num">${i + 1}</td>${playOne(r)}<td><div class="t1">${esc(r.title)}${labelTag(r.label)}</div><div class="t2">${esc(r.first_upload)}</div></td>${extra(r)}</tr>`;
async function loadClient() {
  const d = await api("/api/client");
  $("#cl-window").textContent = `How the songs first uploaded ${d.since} → ${d.until} are doing, platform by platform`;
  $("#cl-asof").innerHTML = `<span class="dot"></span>Figures for ${dayLabel(d.as_of)} (last completed day)`;
  const pct = (n, t) => (t ? Math.round((100 * n) / t) : 0);
  $("#cl-started").innerHTML = d.started ? `<b>Official daily readings started:</b> ${startedLine(d.started)}.` : "";
  const glance = () => { $("#cl-kpis").innerHTML = glanceTiles(d); wireRatio($("#cl-kpis"), glance); };
  glance();
  $("#cl-audio").innerHTML = audioCards(d);
  growthCharts($("#cl-trend-yt"), $("#cl-trend-au"), $("#cl-legend"), d);
  const songs = () => { songTables(d, $("#cl-top"), $("#cl-risers"), $("#cl-risers-title")); wireSongSwitch($("#page-dashboard"), songs); };
  songs();
  const lead = d.labels[0];
  $("#cl-highlights").innerHTML = `<ul class="hl-list">
    <li><span>Hit songs<div class="muted" style="font-size:12px">${fmt(d.hits_yt)} with 1M+ YouTube views · ${fmt(d.hits_audio)} with 1M+ streams</div></span><b>${fmt(d.hits)}</b></li>
    <li><span>Songs growing<div class="muted" style="font-size:12px">gained views or streams on ${dayLabel(d.as_of)}</div></span><b>${fmt(d.growing)}</b></li>
    <li><span>Top label<div class="muted" style="font-size:12px">${lead ? `${pct(lead.yt_views, d.yt_views)}% of YouTube views · ${fmt(lead.songs)} songs` : ""}</div></span><b>${esc(lead ? lead.label : "–")}</b></li>
    <li><span>New releases<div class="muted" style="font-size:12px">confirmed new on Spotify</div></span><b>${fmt(d.new)}</b></li></ul>`;
  $("#cl-presence").innerHTML = d.platforms.map((p) => `<div class="bar-row"><span class="bar-name">${esc(p.name.replace(/ (views|plays)$/, ""))}</span>
      <span class="bar-track"><span class="bar-fill" style="width:${pct(p.songs, d.songs)}%;background:${PCOLOR[p.platform] || "var(--accent)"}"></span></span>
      <span class="bar-val">${pct(p.songs, d.songs)}% <span class="muted">${fmt(p.songs)}</span></span></div>`).join("");
  [["shorts", "#cl-shorts", "Shorts"], ["reels", "#cl-reels", "Reels"]].forEach(([k, el, what]) => {
    const sh = d[k];
    $(el + "-card").style.display = sh ? "" : "none";
    if (!sh) return;
    $(el + "-sub").textContent = `${compact(sh.total)} ${what} made with the official audio of ${fmt(sh.songs)} songs${sh.gain ? ` · ${sh.gain > 0 ? "+" : ""}${compact(sh.gain)} in the last day` : ""}`;
    $(el).innerHTML = sh.top.length ? `<thead><tr><th>#</th><th>Song</th><th class="num">${what} made</th><th class="num">Last day</th></tr></thead><tbody>` +
      sh.top.map((r, i) => `<tr class="click" data-id="${r.id}"><td class="muted num">${i + 1}</td><td><div class="t1">${esc(r.title)}</div></td><td class="num"><b>${compact(r.shorts)}</b></td>
        <td class="num ${r.gain > 0 ? "delta-up" : r.gain < 0 ? "delta-down" : ""}">${r.gain ? (r.gain > 0 ? "+" : "") + compact(r.gain) : "–"}</td></tr>`).join("") + "</tbody>"
      : `<tbody><tr><td class="empty">None made with the songs' official audio yet.</td></tr></tbody>`;
  });
  const day = dayLabel(d.as_of);
  $("#cl-labels").innerHTML = `<thead><tr><th rowspan="2">Label</th><th rowspan="2" class="num">Songs</th><th rowspan="2" class="num">New</th><th rowspan="2" class="num">Hits</th>
      <th colspan="3" class="th-group" style="--pc:${PCOLOR.youtube}">YouTube views</th><th colspan="2" class="th-group" style="--pc:${PCOLOR.spotify}">Audio streams</th></tr>
      <tr><th class="num">Total</th><th class="num">Share</th><th class="num">Gained ${day}</th><th class="num">Total</th><th class="num">Gained ${day}</th></tr></thead><tbody>` +
    d.labels.map((l) => `<tr class="label-row" data-label="${esc(l.label)}" style="cursor:pointer"><td>${labelTag(l.label)}</td><td class="num">${fmt(l.songs)}</td><td class="num">${fmt(l.new)}</td><td class="num">${fmt(l.hits)}</td>
      <td class="num"><b>${compact(l.yt_views)}</b></td><td class="num">${pct(l.yt_views, d.yt_views)}%</td><td class="num ${l.yt_gain > 0 ? "delta-up" : ""}">${l.yt_gain ? "+" + compact(l.yt_gain) : "–"}</td>
      <td class="num"><b>${compact1(l.audio)}</b></td><td class="num ${l.audio_gain > 0 ? "delta-up" : ""}">${l.audio_gain ? "+" + compact1(l.audio_gain) : "–"}</td></tr>`).join("") + "</tbody>";
}
// song rows (tr.click[data-id]) open through the page-wide handler; no second handler here (it opened every song twice)
$("#cl-labels").addEventListener("click", (e) => { const tr = e.target.closest("tr[data-label]"); if (!tr || tr.dataset.label === "Other") return;
  Object.assign(CAT, { label: tr.dataset.label, channel: "", day: "", status: "", offset: 0 }); location.hash = "#catalogue"; });

/* ---------- Developer dashboard: health, targets, quotas, backlogs ---------- */
const runFor = (r) => { if (!r.finished_at) return "running"; const m = Math.round((new Date(r.finished_at) - new Date(r.started_at)) / 60000); return m < 60 ? `${m} min` : `${(m / 60).toFixed(1)} h`; };
const gb = (b) => b == null ? "–" : `${(b / 1e9).toFixed(1)} GB`;
const kv = (rows) => `<div class="kv">${rows.map(([k, v]) => `<div class="muted">${k}</div><div>${v}</div>`).join("")}</div>`;
/* Server health: CPU, memory, swap, disk, folder sizes, hourly trend (lightweight: counters only) */
const bytes = (b) => b == null ? "–" : b >= 1e9 ? `${(b / 1e9).toFixed(1)} GB` : `${Math.round(b / 1e6)} MB`;
const pctBar = (label, used, total, sub) => { const p = total ? (100 * used) / total : 0;
  return `<div class="bar-row"><span class="bar-name">${label}</span><span class="bar-track"><span class="bar-fill" style="width:${Math.min(100, p)}%;background:${p > 90 ? "var(--critical)" : p > 75 ? "var(--warning)" : "var(--accent)"}"></span></span>
    <span class="bar-val">${Math.round(p)}% <span class="muted">${sub}</span></span></div>`; };
const uptime = (s) => { if (s == null) return "–"; const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600); return d ? `${d} d ${h} h` : `${h} h ${Math.floor((s % 3600) / 60)} min`; };
async function loadSystem() {
  const s = await api("/api/system");
  const h = s.history;
  $("#sys-host").textContent = [s.host, s.distro || s.os, s.arch].filter(Boolean).join(" · ");
  $("#sys-now").innerHTML = pctBar("CPU", s.cpu_pct || 0, 100, `${s.cores} cores`) +
    pctBar("Memory", s.mem_used, s.mem_total, `${bytes(s.mem_used)} of ${bytes(s.mem_total)}`) +
    (s.swap_total ? pctBar("Swap", s.swap_used, s.swap_total, `${bytes(s.swap_used)} of ${bytes(s.swap_total)}`) : "") +
    pctBar("Disk", s.disk_used, s.disk_total, `${bytes(s.disk_free)} free`) +
    kv([["Load (1 / 5 / 15 min)", s.load ? s.load.join(" / ") : "–"], ["Up for", uptime(s.uptime_s)], ["Console memory", bytes(s.console_rss)],
      ["Network since boot", `↓ ${bytes(s.network.rx)} · ↑ ${bytes(s.network.tx)}`], ["Python", esc(s.python)]]);
  const pts = (k) => h.samples.filter((x) => x[k] != null).map((x) => ({ x: x.at.slice(5, 13).replace("T", " "), y: x[k] }));
  const diskPct = h.samples.filter((x) => x.disk_total).map((x) => ({ x: x.at.slice(5, 13).replace("T", " "), y: Math.round((1000 * x.disk_used) / x.disk_total) / 10 }));
  const series = [{ name: "CPU", color: "var(--accent)", points: pts("cpu_pct") }, { name: "Memory", color: "var(--accent-2)", points: pts("mem_pct") },
    { name: "Disk", color: "var(--c-jiosaavn)", points: diskPct }];
  if (h.samples.length >= 2) { multiLine($("#sys-trend"), series, { height: 180 }); $("#sys-legend").innerHTML = series.map((x) => `<span><span style="display:inline-block;width:12px;height:3px;background:${x.color};vertical-align:middle;margin-right:5px"></span>${x.name}</span>`).join(""); }
  else { $("#sys-trend").innerHTML = `<div class="empty">A sample is stored every hour; the trend appears after two.</div>`; $("#sys-legend").innerHTML = ""; }
  const g = h.disk_growth_per_day;
  $("#sys-disk").innerHTML = kv([["Free", `${bytes(s.disk_free)} of ${bytes(s.disk_total)}`],
    ["Growing", g == null ? "measured after 12 hours of samples" : `${g >= 0 ? "+" : ""}${(g / 1e9).toFixed(2)} GB a day`],
    ["Full in", h.disk_days_left == null ? "–" : `about ${fmt(h.disk_days_left)} days${h.disk_days_left < 30 ? ' <span class="pill bad"><span class="dot"></span>soon</span>' : ""}`]]);
  const tot = s.folders.reduce((a, f) => a + f.bytes, 0);
  $("#sys-folders").innerHTML = `<thead><tr><th>Folder</th><th class="num">Size</th><th class="num">Share of disk</th></tr></thead><tbody>` +
    s.folders.map((f) => `<tr><td>${esc(f.name)}</td><td class="num">${bytes(f.bytes)}</td><td class="num">${((100 * f.bytes) / s.disk_total).toFixed(1)}%</td></tr>`).join("") +
    `</tbody><tfoot><tr><td><b>App data</b></td><td class="num"><b>${bytes(tot)}</b></td><td class="num">${((100 * tot) / s.disk_total).toFixed(1)}%</td></tr>
     <tr><td colspan="3" class="muted" style="font-size:12px">Folder sizes are added up at most every 6 hours.</td></tr></tfoot>`;
}
async function loadDev() {
  loadSystem().catch(() => {});
  loadAlerts();
  const d = await api("/api/dev");
  const fr = d.freshness;
  $("#dev-fresh").innerHTML = `<div class="fresh-grid">${fr.sources.map((x) => `<div class="fresh ${x.last == null ? "none" : x.ok ? "ok" : "bad"}"><span class="dot"></span><div><div class="t1">${esc(x.source)}</div>
      <div class="t2">${x.last == null ? "no readings yet" : x.age_days === 0 ? "today" : `${x.age_days} day${x.age_days === 1 ? "" : "s"} old`}${x.last != null && !x.ok ? ` · expected within ${x.max_age} day${x.max_age === 1 ? "" : "s"}` : ""}</div></div></div>`).join("")}</div>` +
    (fr.zero_reads.length ? `<div class="warn-text" style="margin-top:10px;font-size:13px">⚠ Read nothing in the last 24 h: ${fr.zero_reads.map((z) => `<b>${esc(z.job)}</b> (${z.runs} run${z.runs === 1 ? "" : "s"}${z.failed ? `, ${z.failed} pages not readable` : ""})`).join(", ")}. A page layout may have changed, or access is blocked.</div>` : "");
  $("#dev-targets").innerHTML = d.targets.map((t) => {
    const ok = t.value != null && (t.lower ? t.value <= t.target : t.value >= t.target);
    return `<div class="card kpi target ${t.value == null ? "" : ok ? "on" : "off"}"><div class="label">${esc(t.name)}</div><div class="value">${t.value == null ? "–" : t.value + "%"}</div>
      <div class="sub">target ${t.lower ? "≤" : "≥"} ${t.target}% · ${esc(t.detail)}</div></div>`; }).join("");
  renderPause(d);
  const j = d.job;
  const lanePill = (x) => x && x.running ? `<span class="pill run"><span class="dot"></span>${esc(x.job)}</span>` : '<span class="pill"><span class="dot"></span>Idle</span>';
  $("#dev-now").innerHTML = kv([["API jobs", lanePill(j)], ["Browser lane", lanePill(d.browser_job)],
    ["Work loop", d.worker.enabled === false ? '<span class="pill bad"><span class="dot"></span>off</span>' : `<span class="pill ok"><span class="dot"></span>on</span> pause ${d.worker.pause_seconds ?? 60} s between jobs`],
    ["Next in queue", d.next ? `<b>${esc(d.next.job)}</b> <span class="muted">${esc(d.next.why)}</span>` : '<span class="muted">nothing due</span>'],
    ["Spotify API", d.spotify.ok ? `<span class="pill ok"><span class="dot"></span>healthy</span> <span class="muted">checked ${ago(d.spotify.checked_at)}</span>` : `<span class="pill bad"><span class="dot"></span>blocked</span>${d.spotify.retry_at ? ` <span class="muted">retry ${esc(new Date(d.spotify.retry_at).toLocaleString())}</span>` : ""}`]]);
  const y = d.youtube, free = y.keys.filter((k) => k.available).length;
  $("#dev-quota").innerHTML = `<div class="bar-row"><span class="bar-track"><span class="bar-fill" style="width:${Math.min(100, (100 * y.units_today) / y.units_limit)}%"></span></span><span class="bar-val">${Math.round((100 * y.units_today) / y.units_limit)}%</span></div>` +
    kv([["Units used today", `${fmt(y.units_today)} of ${fmt(y.units_limit)}`], ["Keys (one per project)", `${y.keys.length} · ${free} available`],
      ["Searches left today", `about ${fmt(Math.max(0, Math.floor((y.units_limit - y.units_today) / 100)))}`], ["Quota resets", esc(new Date(y.resets).toLocaleString())]]);
  const sq = d.spotify_quota, lim = sq.calls_before_last_block;
  const spPct = lim ? Math.min(100, (100 * sq.calls_24h) / lim) : null;
  $("#dev-spquota").innerHTML = (lim ? `<div class="bar-row"><span class="bar-track"><span class="bar-fill" style="width:${spPct}%;background:${sq.blocked || spPct > 85 ? "var(--critical)" : "var(--accent)"}"></span></span><span class="bar-val">${Math.round(spPct)}%</span></div>` : "") +
    kv([["State", sq.blocked ? `<span class="pill bad"><span class="dot"></span>blocked</span> <span class="muted">until ${esc(new Date(sq.retry_at).toLocaleString())}</span>`
                             : sq.ok ? `<span class="pill ok"><span class="dot"></span>working</span> <span class="muted">checked ${ago(sq.checked_at)}</span>` : '<span class="pill"><span class="dot"></span>not checked yet</span>'],
        ...Object.entries(sq.accounts || {}).map(([a, u]) => [a === "second" ? "Account 2, last 24 h" : "Primary, last 24 h",
          `${fmt(u.used)} of ${fmt(u.limit)} <span class="muted">(our limit)</span>`]),
        ["Calls today (UTC)", fmt(sq.calls_today)], ["Calls, last 24 h", lim ? `${fmt(sq.calls_24h)} of about ${fmt(lim)}` : fmt(sq.calls_24h)],
        ["Daily limit (measured)", lim ? `about ${fmt(lim)} calls <span class="muted">(made in the 24 h before the last block)</span>` : '<span class="muted">not blocked in the last 7 days</span>'],
        ["Blocks, last 7 days", sq.blocks_7d.length ? sq.blocks_7d.map((b) => `${esc(new Date(b.at).toLocaleString())} <span class="muted">→ ${b.until ? esc(new Date(b.until).toLocaleTimeString()) : "?"}</span>`).join("<br>") : "none"]]) +
    `<p class="muted" style="font-size:12px;margin:8px 0 0">Spotify publishes no quota number; it blocks for some hours when the day's share is used. Play counts use the public pages and no quota.</p>`;
  const st = d.storage, used = st.disk_total - st.disk_free;
  $("#dev-storage").innerHTML = `<div class="bar-row"><span class="bar-track"><span class="bar-fill" style="width:${(100 * used) / st.disk_total}%;background:${used / st.disk_total > 0.85 ? "var(--critical)" : "var(--accent)"}"></span></span><span class="bar-val">${Math.round((100 * used) / st.disk_total)}%</span></div>` +
    kv([["Disk free", `${gb(st.disk_free)} of ${gb(st.disk_total)}`], ["Database", gb(st.db_bytes)], ["Readings stored", `${fmt(st.snapshot_rows)} since ${esc(st.first_day || "–")}`],
      ["Song-day rows", fmt(st.song_daily_rows)], ["Last backup", st.backup ? `${esc(st.backup.name)} · ${ago(st.backup.at)}` : '<span class="pill bad"><span class="dot"></span>none found</span>']]);
  const ids = d.ids, dd = (x) => new Date(x + "T00:00:00").toLocaleDateString(undefined, { day: "numeric", month: "short" });
  const ROUTE = { search: "API search", web_search: "web search", same_recording: "same recording", description: "label's link", album: "album", playlist: "playlist",
    musicbrainz_link: "MusicBrainz link", topic_channel: "Topic channel", album_playlist: "album playlist", isrc: "ISRC", manual: "pasted", spotify_source: "Spotify discovery" };
  $("#dev-ids-note").textContent = `${fmt(ids.songs)} songs in the period${FOCUS_ALL ? " (all songs)" : " (new releases)"}`;
  $("#dev-ids").innerHTML = `<thead><tr><th class="num">#</th><th>Platform</th><th class="num">Songs with an ID</th><th class="num">Coverage</th><th class="num">Still without</th>` +
    ids.days.map((x, i) => `<th class="num">${i === ids.days.length - 1 ? "Today" : dd(x)}</th>`).join("") + `<th>Found today by route</th></tr></thead><tbody>` +
    ids.platforms.map((r, i) => `<tr><td class="num muted">${i + 1}</td><td>${esc(r.name)}</td><td class="num">${fmt(r.with_id)}</td>
      <td class="num ${r.share >= 90 ? "delta-up" : r.share < 50 ? "warn-text" : ""}">${r.share == null ? "–" : r.share + "%"}</td><td class="num">${fmt(r.without_id)}</td>` +
      r.days.map((n) => `<td class="num ${n ? "" : "muted"}">${n ? fmt(n) : "–"}</td>`).join("") +
      `<td style="font-size:12.5px">${r.routes_today.length ? r.routes_today.map((x) => `${esc(ROUTE[x.route] || x.route)} <b>${fmt(x.songs)}</b>`).join(" · ") : '<span class="muted">none</span>'}</td></tr>`).join("") +
    `</tbody><tfoot><tr><td colspan="${6 + ids.days.length}" class="muted" style="font-size:12px;line-height:1.5">
      <b>Songs with an ID</b>: the period's songs linked to the platform (accepted links only) · <b>Coverage</b>: share of the period's songs ·
      <b>Still without</b>: songs not yet linked there (many are simply not released on that platform) · <b>Date columns</b>: songs that got their ID that day ·
      <b>Found today by route</b>: how today's IDs were found. Older releases count only with "All songs" on.</td></tr></tfoot>`;
  const NAMES = { youtube: "YouTube", spotify: "Spotify", ytmusic: "YouTube Music", jiosaavn: "JioSaavn", deezer: "Deezer" };
  const cov = (n, t) => { const p = t ? (100 * n) / t : 0; return `<td class="num ${t && p >= 99 ? "delta-up" : p < 90 ? "warn-text" : ""}">${fmt(n)} <span class="muted">${t ? p.toFixed(1) + "%" : ""}</span></td>`; };
  $("#dev-readings").innerHTML = `<thead><tr><th>Platform</th><th class="num">Linked items</th><th class="num">Yesterday (${esc(d.yesterday.slice(5))})</th><th class="num">Today so far</th></tr></thead><tbody>` +
    d.readings.map((r) => `<tr><td>${NAMES[r.platform]}</td><td class="num">${fmt(r.items)}</td>${cov(r.yesterday, r.items)}${cov(r.today, r.items)}</tr>`).join("") +
    `</tbody><tfoot><tr><td colspan="4" class="muted" style="font-size:12px">Spotify hides play counts under 1,000, so those tracks have no reading. Deezer is read once a day by the songs job.</td></tr></tfoot>`;
  $("#dev-backlog").innerHTML = `<thead><tr><th>Backlog</th><th class="num">Count</th><th>How it clears</th></tr></thead><tbody>` +
    d.backlog.map((b) => `<tr><td>${esc(b.name)}</td><td class="num">${b.count == null ? "–" : fmt(b.count)}</td><td class="muted">${esc(b.note)}</td></tr>`).join("") + "</tbody>";
  const chip = (s) => `<span class="pill ${s === "ok" ? "ok" : s === "running" ? "run" : s === "paused" ? "" : s === "partial" || s === "cancelled" ? "warn" : "bad"}"><span class="dot"></span>${esc(s)}</span>`;
  $("#dev-jobs").innerHTML = d.jobs.length ? `<thead><tr><th>Job</th><th class="num">Runs</th><th class="num">OK</th><th>Last run</th><th>Status</th><th>Took</th></tr></thead><tbody>` +
    d.jobs.map((x) => `<tr title="${esc(x.last.note)}"><td>${esc(x.job)}</td><td class="num">${x.runs}</td><td class="num">${x.ok}</td><td class="muted">#${x.last.id} · ${ago(x.last.started_at)}</td><td>${chip(x.last.status)}</td><td class="muted">${runFor(x.last)}</td></tr>`).join("") + "</tbody>"
    : `<tbody><tr><td class="empty">No runs in the last 24 hours.</td></tr></tbody>`;
  multiLine($("#dev-volume"), d.volume.filter((l) => l.points.length).map((l) => ({ name: l.name, color: PCOLOR[l.platform], points: l.points })));
  $("#dev-legend").innerHTML = legendHtml(d.volume.filter((l) => l.points.length));
  $("#dev-keys").innerHTML = `<thead><tr><th>#</th><th>Key</th><th>State</th><th>Until</th></tr></thead><tbody>` +
    y.keys.map((k, i) => `<tr><td class="muted">${i + 1}</td><td class="mono">${esc(k.hint)}</td><td>${k.available ? '<span class="pill ok"><span class="dot"></span>available</span>' : `<span class="pill warn"><span class="dot"></span>${esc(k.reason || "set aside")}</span>`}</td><td class="muted">${k.until ? esc(new Date(k.until).toLocaleString()) : ""}</td></tr>`).join("") + "</tbody>";
  $("#dev-failures").innerHTML = d.failures.length ? `<thead><tr><th>Run</th><th>Job</th><th>When</th><th>What went wrong</th></tr></thead><tbody>` +
    d.failures.map((r) => `<tr><td class="muted">#${r.id}</td><td>${esc(r.job)}</td><td class="muted">${ago(r.started_at)}</td><td style="white-space:normal">${chip(r.status)} ${esc(r.note || "")}</td></tr>`).join("") + "</tbody>"
    : `<tbody><tr><td class="empty">No failed runs in the last 24 hours.</td></tr></tbody>`;
}
$("#dev-refresh").onclick = () => loadDev();

/* Job control: pause (running jobs finish) or pause now (they stop at the next safe point), every job or some;
   a pause always ends by itself; Resume lets paused jobs continue where they stopped. */
const utc = (iso) => iso ? new Date(iso).toLocaleString(undefined, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) : "";
function renderPause(d) {
  const p = d.pause;
  const what = p ? (p.all ? "All jobs" : (p.jobs || []).join(", ")) : "";
  $("#dev-pause-state").innerHTML = p ? `<span class="pill warn"><span class="dot"></span>${esc(what)} paused</span>` : '<span class="pill ok"><span class="dot"></span>running normally</span>';
  $("#dev-pause-info").innerHTML = p
    ? `Paused by <b>${esc(p.by || "?")}</b> ${ago(p.since)}${p.reason ? ` · ${esc(p.reason)}` : ""} · ${p.mode === "now" ? "running jobs were stopped at a safe point" : "running jobs were left to finish"} · <b>resumes by itself at ${esc(utc(p.until))}</b>`
    : "Pause before maintenance or a deploy: <b>Pause</b> lets running jobs finish, <b>Pause now</b> stops them at their next safe point (data read so far is kept). A pause always ends by itself.";
  $("#dev-resume").disabled = !p;
  const box = $("#dev-pause-jobs");
  if (!box.dataset.filled) {
    box.innerHTML = Object.entries(d.job_names || {}).map(([k, v]) => `<label title="${esc(v)}"><input type="checkbox" value="${esc(k)}"> ${esc(k)}</label>`).join("");
    box.dataset.filled = "1";
  }
  const label = { pause: "paused", pause_now: "paused now", resume: "resumed", expired: "ended by itself" };
  $("#dev-pause-log").innerHTML = (d.pause_log || []).length ? `<div class="muted" style="font-size:12px">Recent: ` +
    d.pause_log.slice(0, 5).map((x) => `${esc(utc(x.at))} ${esc(label[x.action] || x.action)}${x.jobs ? ` (${esc(x.jobs.join(", "))})` : ""} by ${esc(x.by || "?")}${x.reason ? ` · ${esc(x.reason)}` : ""}`).join(" · ") + "</div>" : "";
}
async function doPause(now) {
  const jobs = [...document.querySelectorAll("#dev-pause-jobs input:checked")].map((x) => x.value);
  try {
    const r = await api("/api/jobs/pause", { now, minutes: +$("#dev-pause-min").value, reason: $("#dev-pause-reason").value, jobs: jobs.length ? jobs : null });
    toast(`${jobs.length ? jobs.join(", ") : "All jobs"} paused until ${utc(r.pause.until)}${r.stopping.length ? ` · stopping ${r.stopping.join(", ")} at a safe point` : ""}`);
    loadDev();
  } catch (e) { toast(e.message, true); }
}
$("#dev-pause-btn").onclick = () => doPause(false);
$("#dev-pause-now").onclick = () => doPause(true);
$("#dev-resume").onclick = async () => {
  try { await api("/api/jobs/resume", {}); toast("Jobs resumed: paused work continues where it stopped"); loadDev(); }
  catch (e) { toast(e.message, true); }
};
$("#dev-spotify-second").onclick = async () => {
  try {
    await api("/api/run", { job: "spotify_second_account" });
    toast("Second account Spotify lookup started");
    loadDev();
  } catch (e) { toast(e.message, true); }
};

/* ---------- Crawl stats (developer): runs, items, cost and failures by day, week, month or year ---------- */
const CR = { period: "day", source: "", kind: "" };
const CR_COLORS = { youtube: "var(--c-youtube)", ytmusic: "var(--c-ytmusic)", spotify_plays: "var(--c-spotify)", spotify_search: "#22a35a", spotify_playlists: "#7bd88f", spotify_web: "#4fb3a0",
  jiosaavn: "var(--c-jiosaavn)", deezer: "var(--c-deezer)", apple: "var(--c-apple)", musicbrainz: "var(--c-musicbrainz)" };
const crColor = (s, i) => CR_COLORS[s] || ["#4c94ec", "#9a8ff0", "#f5a524", "#38bdf8"][i % 4];
const crKind = (k) => `<span class="pill ${["quota", "cancelled", "interrupted"].includes(k) ? "warn" : "bad"}"><span class="dot"></span>${esc(k)}</span>`;
const crStatus = (s) => `<span class="pill ${s === "ok" ? "ok" : s === "paused" ? "" : ["partial", "cancelled", "interrupted"].includes(s) ? "warn" : "bad"}"><span class="dot"></span>${esc(s)}</span>`;
const crWhen = (t) => t ? new Date(t).toLocaleString("en-GB", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) : "–";
const crNum = (v) => v ? (Math.abs(v) >= 1e5 ? compact(v) : fmt(v)) : '<span class="muted">0</span>';
async function loadCrawl() {
  const group = location.hash.startsWith("#apis") ? "api" : "crawl";
  if (CR.group !== group) Object.assign(CR, { group, source: "", kind: "" });
  const isApi = group === "api";
  const d = await api(`/api/crawl?${new URLSearchParams({ group, period: CR.period, source: CR.source, kind: CR.kind })}`);
  $("#cr-title").textContent = isApi ? "API stats" : "Crawl stats";
  $("#cr-sub").textContent = isApi ? "Every API: calls, what they returned, quota, keys, health and failures." : "Scraping and crawling: public pages, embed pages, web search and internal jobs, with what they read and what failed.";
  $("#cr-src-title").textContent = isApi ? "APIs" : "Crawl sources";
  $("#cr-cov-title").textContent = isApi ? "Keys and quota" : "Readings captured";
  $("#cr-health-card").style.display = isApi ? "" : "none";
  document.querySelectorAll("#cr-period button").forEach((b) => b.classList.toggle("on", b.dataset.p === CR.period));
  if (d.all_sources) { const sel = $("#cr-source"); sel.innerHTML = `<option value="">All sources</option>` + d.all_sources.map((s) => `<option value="${esc(s)}" ${s === CR.source ? "selected" : ""}>${esc(d.names[s] || s)}</option>`).join(""); }
  const k = d.kpi || {};
  const tile = (l, v, x, c) => `<div class="card kpi" style="--kpi-c:${c}"><div class="label">${l}</div><div class="value">${v}</div><div class="sub">${x}</div></div>`;
  $("#cr-kpis").innerHTML = [tile("Runs", fmt(k.runs || 0), `${fmt(k.ok_runs || 0)} with no problems`, "var(--k1)"),
    tile("Success rate", `${k.success ?? "–"}%`, `${fmt(k.problems || 0)} problem steps · ${fmt(k.cancelled || 0)} cancelled`, (k.success ?? 0) >= 95 ? "var(--good)" : "var(--warning)"),
    tile("Items read", compact(k.items_read || 0), `${compact(k.found || 0)} found · ${compact(k.missing || 0)} not found`, "var(--k3)"),
    tile("API calls", compact(k.api_calls || 0), "searches, lookups and pages", "var(--k2)"),
    isApi ? tile("YouTube quota", compact(k.quota_units || 0), "units used", "var(--k4)") : tile("Links found", compact(k.found || 0), "new links and items", "var(--k4)"),
    tile("Busy time", `${fmt(Math.round(k.busy_hours || 0))} h`, "time jobs were running", "var(--k5)")].join("");
  const lines = (d.series || []).map((s, i) => ({ name: s.name, color: crColor(s.source, i), points: s.points }));
  multiLine($("#cr-items"), lines, { height: 260 });
  $("#cr-items-legend").innerHTML = lines.map((l) => `<span><span style="display:inline-block;width:12px;height:3px;background:${l.color};vertical-align:middle;margin-right:5px"></span>${esc(l.name)}</span>`).join("");
  const asc = (d.rows || []).slice().reverse();
  const hl = [{ name: "OK", color: "var(--good)", points: asc.map((r) => ({ x: r.bucket, y: r.ok_runs })) }, { name: "Problems", color: "var(--critical)", points: asc.map((r) => ({ x: r.bucket, y: r.problems })) }];
  multiLine($("#cr-health"), hl, { height: 260 });
  $("#cr-health-legend").innerHTML = hl.map((l) => `<span><span style="display:inline-block;width:12px;height:3px;background:${l.color};vertical-align:middle;margin-right:5px"></span>${l.name}</span>`).join("");
  $("#cr-sources").innerHTML = `<thead><tr><th>Source</th><th>Access</th><th class="num">Runs</th><th class="num">Success</th><th class="num">Read</th><th class="num">Found</th><th class="num">Not found</th><th class="num" title="found per item tried">Yield</th><th class="num">Calls</th><th class="num">Quota</th><th class="num">Avg time</th><th>Last OK</th><th>Last problem</th></tr></thead><tbody>` +
    (d.sources || []).map((s, i) => `<tr class="click" data-src="${esc(s.source)}"><td><i class="cr-dot" style="background:${crColor(s.source, i)}"></i><b>${esc(s.name)}</b></td><td class="muted">${esc(s.access)}</td><td class="num">${fmt(s.runs)}</td>
      <td class="num ${s.success >= 95 ? "delta-up" : s.success < 70 ? "delta-down" : "warn-text"}">${s.success}%</td><td class="num">${crNum(s.items_read)}</td><td class="num">${crNum(s.found)}</td><td class="num">${crNum(s.missing)}</td><td class="num muted">${s.yield == null ? "–" : s.yield + "%"}</td><td class="num">${crNum(s.api_calls)}</td><td class="num">${crNum(s.quota_units)}</td>
      <td class="num muted">${s.avg_seconds == null ? "–" : s.avg_seconds < 90 ? s.avg_seconds + " s" : Math.round(s.avg_seconds / 60) + " min"}</td><td class="muted">${crWhen(s.last_ok)}</td><td class="${s.last_problem ? "warn-text" : "muted"}">${crWhen(s.last_problem)}</td></tr>`).join("") + "</tbody>";
  $("#cr-rows-title").textContent = { day: "By day", week: "By week", month: "By month", year: "By year" }[CR.period];
  $("#cr-rows").innerHTML = `<thead><tr><th>${CR.period === "day" ? "Day" : CR.period === "week" ? "Week" : CR.period === "month" ? "Month" : "Year"}</th><th class="num">Runs</th><th class="num">Problems</th><th class="num">Read</th><th class="num">Found</th><th class="num">Calls</th><th class="num">Quota</th><th class="num">Busy</th></tr></thead><tbody>` +
    (d.rows || []).map((r) => `<tr><td><b>${esc(CR.period === "day" ? ddDay(r.bucket) : r.bucket)}</b></td><td class="num">${fmt(r.runs)}</td><td class="num ${r.problems ? "delta-down" : "muted"}">${fmt(r.problems)}</td><td class="num">${crNum(r.items_read)}</td><td class="num">${crNum(r.found)}</td><td class="num">${crNum(r.api_calls)}</td><td class="num">${crNum(r.quota_units)}</td><td class="num muted">${r.busy_hours} h</td></tr>`).join("") + "</tbody>";
  if (isApi && d.keys) {
    const y = d.keys, pc = Math.min(100, Math.round((100 * y.units_today) / y.units_limit)), free = y.keys.filter((x) => x.available).length;
    $("#cr-quota").innerHTML = `<div class="bar-row"><span class="bar-name">YouTube</span><span class="bar-track"><span class="bar-fill" style="width:${pc}%"></span></span><span class="bar-val">${pc}%</span></div>
      <div class="muted" style="font-size:12.5px;margin-bottom:10px">${fmt(y.units_today)} of ${fmt(y.units_limit)} units today · about ${fmt(Math.max(0, Math.floor((y.units_limit - y.units_today) / 100)))} searches left · ${y.keys.length} keys, ${free} available · resets ${crWhen(y.resets)}<br>Spotify searches today: ${fmt(y.spotify_searches_today)} of ~600</div>`;
    $("#cr-coverage").innerHTML = `<thead><tr><th>#</th><th>YouTube key</th><th>State</th><th>Until</th></tr></thead><tbody>` +
      y.keys.map((x, i) => `<tr><td class="muted">${i + 1}</td><td class="mono">${esc(x.hint)}</td><td>${crStatus(x.available ? "ok" : "partial")}</td><td class="muted">${x.until ? crWhen(x.until) : ""}</td></tr>`).join("") + "</tbody>";
  } else $("#cr-quota").innerHTML = "";
  const plats = [...new Set((d.coverage || []).map((c) => c.platform))], names = { youtube: "YouTube", spotify: "Spotify", ytmusic: "YT Music", jiosaavn: "JioSaavn", deezer: "Deezer" };
  const cov = {}; (d.coverage || []).forEach((c) => ((cov[c.bucket] ||= {})[c.platform] = c));
  if (!isApi) $("#cr-coverage").innerHTML = `<thead><tr><th>Period</th>${plats.map((p) => `<th class="num">${esc(names[p] || p)}</th>`).join("")}</tr></thead><tbody>` +
    Object.keys(cov).sort().reverse().map((b) => `<tr><td><b>${esc(CR.period === "day" ? ddDay(b) : b)}</b></td>${plats.map((p) => { const c = cov[b][p]; return c ? `<td class="num ${CR.period !== "day" ? "" : c.pct >= 99 ? "delta-up" : c.pct < 90 ? "warn-text" : ""}" title="${fmt(c.readings)} of ${fmt(c.items)}">${CR.period === "day" ? c.pct + "%" : compact(c.readings)}</td>` : '<td class="num muted">–</td>'; }).join("")}</tr>`).join("") + "</tbody>";
  $("#cr-kinds").innerHTML = (d.kinds || []).length ? `<thead><tr><th>Type</th><th class="num">Count</th><th class="num">Sources</th><th>Last seen</th><th>Example</th></tr></thead><tbody>` +
    d.kinds.map((x) => `<tr class="click" data-kind="${esc(x.kind)}"><td>${crKind(x.kind)}</td><td class="num">${fmt(x.n)}</td><td class="num">${fmt(x.sources)}</td><td class="muted">${crWhen(x.last_seen)}</td><td class="muted cr-msg">${esc((x.sample || "").slice(0, 110))}</td></tr>`).join("") + "</tbody>"
    : `<tbody><tr><td class="empty">No failures in this period.</td></tr></tbody>`;
  $("#cr-health-table").innerHTML = (d.health || []).length ? `<thead><tr><th>API</th><th>Now</th><th class="num">Checks</th><th class="num">Blocked</th><th>Last blocked</th><th>Retry at</th></tr></thead><tbody>` +
    d.health.map((h) => `<tr title="${esc(h.message || "")}"><td><b>${esc(h.source)}</b></td><td>${crStatus(h.ok_now ? "ok" : "blocked")}</td><td class="num">${fmt(h.checks)}</td><td class="num">${fmt(h.blocked)}</td><td class="muted">${crWhen(h.last_blocked)}</td><td class="muted">${crWhen(h.retry_at)}</td></tr>`).join("") + "</tbody>"
    : `<tbody><tr><td class="empty">No API health checks in this period.</td></tr></tbody>`;
  $("#cr-kind-chip").innerHTML = CR.kind ? `<button class="small" id="cr-kind-clear">${esc(CR.kind)} ✕</button>` : '<span class="muted" style="font-size:12px">click a failure type to filter</span>';
  if ($("#cr-kind-clear")) $("#cr-kind-clear").onclick = () => { CR.kind = ""; loadCrawl(); };
  $("#cr-failures").innerHTML = (d.failures || []).length ? `<thead><tr><th>When</th><th>Run</th><th>Job</th><th>Source</th><th>Status</th><th>Type</th><th>Message</th></tr></thead><tbody>` +
    d.failures.map((f) => `<tr><td class="muted" style="white-space:nowrap">${crWhen(f.started_at)}</td><td class="muted">#${f.run_id}</td><td>${esc(f.job)}</td><td>${esc(f.name)}</td><td>${crStatus(f.status)}</td><td>${crKind(f.kind)}</td><td class="cr-msg">${esc(f.message || "")}</td></tr>`).join("") + "</tbody>"
    : `<tbody><tr><td class="empty">No failures in this period.</td></tr></tbody>`;
}
document.querySelectorAll("#cr-period button").forEach((b) => (b.onclick = () => { CR.period = b.dataset.p; loadCrawl(); }));
$("#cr-source").onchange = (e) => { CR.source = e.target.value; loadCrawl(); };
$("#cr-refresh").onclick = () => loadCrawl();
$("#cr-sources").addEventListener("click", (e) => { const tr = e.target.closest("tr[data-src]"); if (tr) { CR.source = CR.source === tr.dataset.src ? "" : tr.dataset.src; loadCrawl(); } });
$("#cr-kinds").addEventListener("click", (e) => { const tr = e.target.closest("tr[data-kind]"); if (tr) { CR.kind = tr.dataset.kind; loadCrawl(); } });

/* ---------- Reports: readings on two days and the change ---------- */
const RP = { from: "", to: "", label: "", sort: "yt_views_delta", q: "", offset: 0, limit: 100 };
const rpParams = (extra = {}) => new URLSearchParams({ from: RP.from, to: RP.to, label: RP.label, sort: RP.sort, q: RP.q, ...extra });
/* ---------- Reports → Labels: daily (a month day by day) and monthly (completed months), growth not lifetime totals ---------- */
const LR = { tab: "songs", month: "", by: "yt", label: "", mby: "yt" };
const LR_COLS = [["releases", "Releases", null], ["yt", "YouTube views", "youtube"], ["audio", "Audio streams", "audio"], ["sp", "Spotify", "spotify"], ["ytm", "YouTube Music", "ytmusic"], ["js", "JioSaavn", "jiosaavn"]];
const lrNum = (k, v) => k === "releases" ? fmt(v) : v ? (k === "sp" || k === "ytm" || k === "audio" ? compact1(v) : compact(v)) : '<span class="muted">0</span>';
const monthName = (m) => new Date(m + "-01T00:00:00Z").toLocaleDateString("en-GB", { month: "long", year: "numeric", timeZone: "UTC" });
function lrHead(by) {
  return `<tr><th class="num">#</th><th>Label</th>${LR_COLS.map(([k, n]) => `<th class="num"${k === by ? ' style="color:var(--accent)"' : ""}>${n}${k === "releases" ? "" : " gained"}</th>`).join("")}</tr>`;
}
function lrRows(rows, by, noReadings = false) {
  const top = Math.max(1, ...rows.map((r) => r[by] || 0));
  const cell = (k, r) => noReadings && k !== "releases" ? '<span class="muted">–</span>'
    : k === by ? `<span class="share-bar" style="--w:${Math.round((100 * (r[k] || 0)) / top)}%;--c:var(--accent)"><b>${lrNum(k, r[k])}</b></span>` : lrNum(k, r[k]);
  return rows.map((r) => `<tr class="label-row" data-label="${esc(r.label)}" style="cursor:pointer"><td class="num"><b>${r.rank}</b></td><td>${labelTag(r.label)}</td>` +
    LR_COLS.map(([k]) => `<td class="num">${cell(k, r)}</td>`).join("") + "</tr>").join("");
}
function showReportTab(t) {
  LR.tab = t;
  document.querySelectorAll("#rp-tabs button").forEach((b) => b.classList.toggle("on", b.dataset.t === t));
  $("#rp-songs").style.display = t === "songs" ? "" : "none";
  $("#rp-ldaily").style.display = t === "ldaily" ? "" : "none";
  $("#rp-lmonthly").style.display = t === "lmonthly" ? "" : "none";
  $("#rp-intro").textContent = t === "songs" ? "Each song's readings on two days and the change between them. Only readings taken on those exact days are used, so a missed day shows as “–” instead of a wrong change."
    : t === "ldaily" ? "Each label's releases and growth day by day for a month (this month by default), labels ranked over the month so far."
    : "Each completed month: every label's releases and growth over the month, ranked.";
  $("#rp-how").innerHTML = t === "songs" ? "" : `<b>How to read this.</b> <b>Growth, not lifetime totals:</b> each track's gain is its reading minus its own previous reading, counted on the day of the newer reading.
    So when a platform is read late (Spotify play counts often arrive a day later), that day shows little or nothing and the next reading carries it; a month's sum is exact.
    A newly linked track's first reading adds nothing. <b>YouTube views</b> and <b>audio streams</b> (Spotify + YouTube Music + JioSaavn) are counted apart. Releases = songs first uploaded on the labels' YouTube channels that day or month.`;
  $("#rp-how").style.display = t === "songs" ? "none" : "";
  $("#rp-started").style.display = t === "songs" ? "none" : "";
  if (t === "ldaily") loadLabelDaily(); else if (t === "lmonthly") loadLabelMonthly(); else loadReportSongs();
}
document.querySelectorAll("#rp-tabs button").forEach((b) => (b.onclick = () => showReportTab(b.dataset.t)));
async function loadLabelDaily() {
  const d = await api(`/api/labels/report?view=daily&month=${encodeURIComponent(LR.month)}&by=${LR.by}`);
  $("#rp-started").innerHTML = `<b>Official daily readings started:</b> ${startedLine(d.started)}. Days before that have no growth figures.`;
  LR.month = d.month;
  $("#ld-month").innerHTML = d.months.map((m) => `<option value="${m}"${m === d.month ? " selected" : ""}>${monthName(m)}${m === d.today.slice(0, 7) ? " (this month)" : ""}</option>`).join("");
  $("#ld-by").innerHTML = Object.entries(d.metrics).map(([k, n]) => `<option value="${k}"${k === d.by ? " selected" : ""}>${esc(n)}</option>`).join("");
  $("#ld-label").innerHTML = `<option value="">All labels</option>` + d.labels.map((l) => `<option${l === LR.label ? " selected" : ""}>${esc(l)}</option>`).join("");
  $("#ld-csv").href = `/api/labels/report.csv?view=daily&month=${d.month}&by=${d.by}`;
  $("#ld-rank-title").textContent = `Labels ranked by ${d.metrics[d.by]}, ${monthName(d.month)}${d.month === d.today.slice(0, 7) ? " so far" : ""}`;
  $("#ld-rank").innerHTML = `<thead>${lrHead(d.by)}</thead><tbody>${lrRows(d.ranking, d.by)}</tbody>`;
  const lab = LR.label;
  $("#ld-days-title").textContent = `Day by day: ${lab || "all labels"}`;
  const cells = (r, day) => LR_COLS.map(([k]) => {
    const late = (k === "sp" || k === "audio") && (day.recent || (day.read.sp != null && day.read.sp < 90));
    return `<td class="num">${lrNum(k, r[k])}${late ? ` <span title="Spotify counts still coming in: ${day.read.sp ?? 0}% of tracks read that day">⏳</span>` : ""}</td>`; }).join("");
  const leader = (day) => { const best = d.labels.map((l) => [l, day.labels[l][d.by] || 0]).sort((a, b) => b[1] - a[1])[0]; return best && best[1] ? labelTag(best[0]) : '<span class="muted">–</span>'; };
  $("#ld-days").innerHTML = `<thead><tr><th>Day</th>${LR_COLS.map(([k, n]) => `<th class="num">${n}${k === "releases" ? "" : " gained"}</th>`).join("")}${lab ? "" : `<th>Top label</th>`}<th class="num">Spotify read</th></tr></thead><tbody>` +
    d.days.map((day) => `<tr><td><b>${dayLabel(day.day)}</b>${day.recent ? ' <span class="muted" style="font-size:11px">may still change</span>' : ""}</td>${cells(lab ? day.labels[lab] : day.all, day)}${lab ? "" : `<td>${leader(day)}</td>`}
      <td class="num muted">${day.read.sp == null ? "–" : day.read.sp + "%"}</td></tr>`).join("") + "</tbody>";
}
async function loadLabelMonthly() {
  const d = await api(`/api/labels/report?view=monthly&by=${LR.mby}`);
  $("#rp-started").innerHTML = `<b>Official daily readings started:</b> ${startedLine(d.started)}. Months before that show releases only.`;
  $("#lm-by").innerHTML = Object.entries(d.metrics).map(([k, n]) => `<option value="${k}"${k === d.by ? " selected" : ""}>${esc(n)}</option>`).join("");
  $("#lm-csv").href = `/api/labels/report.csv?view=monthly&by=${d.by}`;
  $("#lm-months").innerHTML = d.months.length ? d.months.map((m) => `<div class="card flush" style="margin-bottom:16px"><div class="card-head" style="padding:16px 16px 0"><h2>${monthName(m.month)}</h2><span class="spacer"></span>
      <span class="muted" style="font-size:12px">${m.days_with_readings ? `readings on ${m.days_with_readings} days · ranked by ${esc(d.metrics[m.ranked_by])}` : "no readings yet that month (collection began in late October): releases only"}
      · ${fmt(m.total.releases)} releases${m.total.yt ? ` · +${compact(m.total.yt)} YouTube views · +${compact1(m.total.audio)} audio streams` : ""}</span></div>
      <div class="table-wrap"><table><thead>${lrHead(m.ranked_by)}</thead><tbody>${lrRows(m.ranking, m.ranked_by, !m.days_with_readings)}</tbody></table></div></div>`).join("")
    : `<div class="card empty">No completed month yet.</div>`;
}
["#ld-month", "#ld-by", "#ld-label"].forEach((id) => $(id).addEventListener("change", () => {
  LR.month = $("#ld-month").value; LR.by = $("#ld-by").value; LR.label = $("#ld-label").value; loadLabelDaily(); }));
$("#lm-by").addEventListener("change", () => { LR.mby = $("#lm-by").value; loadLabelMonthly(); });
["#ld-rank", "#lm-months"].forEach((id) => $(id).addEventListener("click", (e) => { const tr = e.target.closest("tr[data-label]"); if (!tr) return;
  Object.assign(CAT, { label: tr.dataset.label, channel: "", day: "", status: "", offset: 0 }); location.hash = isClient() ? "#catalogue" : "#catalogue"; }));

async function loadReport() { showReportTab(LR.tab); }
async function loadReportSongs() {
  const d = await api(`/api/report?${rpParams({ offset: RP.offset, limit: RP.limit })}`);
  RP.from = d.from || ""; RP.to = d.to || "";
  const opts = (sel) => d.days.slice().reverse().map((x) => `<option ${x === sel ? "selected" : ""}>${x}</option>`).join("");
  $("#rp-from").innerHTML = opts(d.from); $("#rp-to").innerHTML = opts(d.to);
  if ($("#rp-label").options.length === 1 && STATUS) (STATUS.config.youtube.labels || []).forEach((l) => $("#rp-label").insertAdjacentHTML("beforeend", `<option>${esc(l.name)}</option>`));
  $("#rp-sort").innerHTML = d.metrics.map((m) => `<option value="${m.key}_delta" ${RP.sort === m.key + "_delta" ? "selected" : ""}>${esc(m.label)} change</option>`).join("");
  $("#rp-csv").href = `/api/report.csv?${rpParams()}` + (FOCUS_ALL ? "&focus=all" : "");
  const sign = (v) => v == null ? "–" : (v > 0 ? "+" : "") + (Math.abs(v) >= 1e5 ? compact(v) : fmt(v));
  const t = d.totals || {};
  const cap = (p) => { const c = (d.capture || []).find((x) => x.platform === p); return c && c.items ? `${Math.round((100 * c.read) / c.items)}% read on ${d.to}` : "no items yet"; };
  $("#rp-kpis").innerHTML = [["YouTube views", sign(t.yt_views_delta), cap("youtube")], ["YouTube likes", sign(t.yt_likes_delta), `comments ${sign(t.yt_comments_delta)}`],
    ["YouTube Music plays", sign(t.ytm_plays_delta), cap("ytmusic")], ["Spotify plays", sign(t.sp_plays_delta), cap("spotify")],
    ["JioSaavn plays", sign(t.js_plays_delta), cap("jiosaavn")],
    ["Songs in report", fmt(d.total), `${d.from} → ${d.to}`]]
    .map(([l, v, x]) => `<div class="card kpi"><div class="label">${esc(l)} ${l === "Songs in report" || l === "Deezer" ? "" : "change"}</div><div class="value">${esc(v)}</div><div class="sub">${esc(x)}</div></div>`).join("");
  const cols = d.metrics;
  $("#rp-table").innerHTML = `<thead><tr><th>Song</th><th>Label</th>${cols.map((m) => `<th class="num">${esc(m.label)}<br><span class="muted">${esc(d.to)}</span></th><th class="num">Change</th>`).join("")}</tr></thead><tbody>` +
    (d.rows.map((r) => `<tr class="click" data-id="${r.id}"><td><div class="t1">${esc(r.title)}</div><div class="t2">${esc(r.channel || "")} · ${esc(r.song_date || "")}</div></td><td>${labelTag(r.label)}</td>` +
      cols.map((m) => `<td class="num">${r[m.key + "_to"] == null ? '<span class="muted">–</span>' : compact(r[m.key + "_to"])}</td><td class="num ${(r[m.key + "_delta"] || 0) > 0 ? "delta-up" : ""}">${sign(r[m.key + "_delta"])}</td>`).join("") + "</tr>").join("")
      || `<tr><td colspan="${2 + cols.length * 2}" class="empty">No songs with readings on both days.</td></tr>`) + "</tbody>";
  $("#rp-count").textContent = d.total ? `${fmt(RP.offset + 1)}–${fmt(Math.min(RP.offset + RP.limit, d.total))} of ${fmt(d.total)}` : "0";
  $("#rp-prev").disabled = RP.offset === 0; $("#rp-next").disabled = RP.offset + RP.limit >= d.total;
}
["from", "to", "label", "sort"].forEach((k) => ($(`#rp-${k}`).onchange = (e) => { RP[k] = e.target.value; RP.offset = 0; loadReport(); }));
let rpTimer;
$("#rp-q").addEventListener("input", (e) => { clearTimeout(rpTimer); rpTimer = setTimeout(() => { RP.q = e.target.value; RP.offset = 0; loadReport(); }, 300); });
$("#rp-prev").onclick = () => { RP.offset = Math.max(0, RP.offset - RP.limit); loadReport(); };
$("#rp-next").onclick = () => { RP.offset += RP.limit; loadReport(); };
$("#rp-table").addEventListener("click", (e) => { const tr = e.target.closest("tr[data-id]"); if (tr) openTrack(tr.dataset.id); });

/* ---------- Daily data: song_daily by day (all songs) or by song (all days) ---------- */
const DD = { day: "", label: "", sort: "yt_views_delta", q: "", song: "", metric: "yt_views", range: 14, offset: 0, limit: 100 };
const GAIN_KEYS = ["yt_views", "yt_likes", "yt_comments", "ytm_plays", "sp_plays", "js_plays", "yt_shorts", "ig_reels"];
const ddChange = (r, k) => GAIN_KEYS.includes(k) ? r[k + "_gain"] : (r[k] != null && r["prev_" + k] != null ? r[k] - r["prev_" + k] : null);
// Daily data: one song's day-by-day history (song view) or every song on one day (day view).
const DD_GROUPS = [["YouTube", ["yt_views", "yt_likes", "yt_comments"], "youtube"], ["YouTube Music", ["ytm_plays"], "ytmusic"], ["Spotify", ["sp_plays"], "spotify"],
  ["JioSaavn", ["js_plays"], "jiosaavn"], ["Deezer", ["dz_rank"], "deezer"], ["Spotify India chart", ["chart_rank"], "spotify"], ["YouTube Shorts", ["yt_shorts"], "youtube"],
  ["Instagram Reels", ["ig_reels"], "instagram"]];
const DD_SHORT = { yt_views: "Views", yt_likes: "Likes", yt_comments: "Comments", ytm_plays: "Plays", sp_plays: "Plays", js_plays: "Plays", dz_rank: "Rank", chart_rank: "Position", yt_shorts: "Shorts made", ig_reels: "Reels made" };
const ddDay = (d) => new Date(d + "T00:00:00Z").toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short", timeZone: "UTC" });
const ddNum = (v) => v == null ? '<span class="muted">–</span>' : (Math.abs(v) >= 1e5 ? compact(v) : fmt(v));
function ddChangeCell(r, k) {
  const v = ddChange(r, k);
  if (v == null) return '<td class="num muted">–</td>';
  if (v === 0) return '<td class="num muted">0</td>';
  const good = k === "chart_rank" ? v < 0 : v > 0;  // a lower chart position is better
  return `<td class="num ${good ? "delta-up" : "delta-down"}">${good ? "▲" : "▼"} ${ddNum(Math.abs(v))}</td>`;
}
// Two header rows: platform groups over (total, change) pairs; only metrics that have data are shown.
function ddHeader(keys, lead) {
  const groups = DD_GROUPS.map(([name, ks, plat]) => [name, ks.filter((k) => keys.includes(k)), plat]).filter((g) => g[1].length);
  return `<thead><tr class="dd-grp">${lead.map(() => "<th></th>").join("")}${groups.map(([n, ks, plat]) =>
      `<th colspan="${ks.length * 2}" class="dd-gh"><i class="pi ic-${plat}"></i>${esc(n)}</th>`).join("")}</tr>
    <tr>${lead.map((l) => `<th class="${l.num ? "num" : ""}">${l.label}</th>`).join("")}${groups.flatMap(([, ks]) => ks.map((k) =>
      `<th class="num dd-first">${DD_SHORT[k]}</th><th class="num">Change</th>`)).join("")}</tr></thead>`;
}
function sparkline(vals) {
  const v = (vals || []).map((x) => x || 0);
  if (v.length < 2) return '<span class="muted">–</span>';
  const max = Math.max(...v, 1), w = 84, h = 24, step = w / (v.length - 1);
  const pts = v.map((x, i) => `${(i * step).toFixed(1)},${(h - 2 - (x / max) * (h - 4)).toFixed(1)}`).join(" ");
  const up = v[v.length - 1] >= v[v.length - 2];
  return `<svg class="spark" viewBox="0 0 ${w} ${h}" width="${w}" height="${h}"><polyline points="${pts}" fill="none" stroke="${up ? "var(--good-text)" : "var(--accent)"}" stroke-width="1.8" stroke-linejoin="round"/></svg>`;
}
async function loadDaily() {
  const linked = hashParams().get("song");  // #daily?song=1049 opens that song's day-by-day history
  if (linked) DD.song = linked;
  const song = DD.song;
  const d = await api(`/api/daily?${new URLSearchParams(song ? { song } : { day: DD.day, label: DD.label, sort: DD.sort, q: DD.q, offset: DD.offset, limit: DD.limit })}`);
  document.querySelectorAll("#dd-controls .dd-day").forEach((e) => (e.style.display = song ? "none" : ""));
  $("#dd-controls").parentElement.style.display = song ? "none" : "";
  ["#dd-head", "#dd-kpis", "#dd-charts", "#dd-compare", "#dd-shorts"].forEach((x) => ($(x).style.display = song ? "" : "none"));
  $("#dd-pager").style.display = song ? "none" : "";
  if (song) return ddSong(d, song);
  DD.day = d.day || "";
  const dayIdx = d.days.indexOf(d.day);
  $("#dd-day").innerHTML = d.days.slice().reverse().map((x) => `<option value="${x}" ${x === d.day ? "selected" : ""}>${ddDay(x)} ${x.slice(0, 4)}</option>`).join("");
  $("#dd-dprev").disabled = dayIdx <= 0; $("#dd-dnext").disabled = dayIdx < 0 || dayIdx >= d.days.length - 1;
  if ($("#dd-label").options.length === 1 && STATUS) (STATUS.config.youtube.labels || []).forEach((l) => $("#dd-label").insertAdjacentHTML("beforeend", `<option>${esc(l.name)}</option>`));
  $("#dd-sort").innerHTML = d.cols.flatMap((c) => [[c.key + "_delta", c.label + " gained"], [c.key, c.label + " (total)"]])
    .map(([k, l]) => `<option value="${k}" ${DD.sort === k ? "selected" : ""}>${esc(l)}</option>`).join("");
  $("#dd-csv").href = `/api/daily.csv?${new URLSearchParams({ day: DD.day, label: DD.label, sort: DD.sort, q: DD.q })}` + (FOCUS_ALL ? "&focus=all" : "");
  $("#dd-sub").textContent = `${d.day ? ddDay(d.day) + " " + d.day.slice(0, 4) : "–"}: each song's totals, and the change since ${d.prev ? ddDay(d.prev) : "the previous day"}. Click a song for its full day-by-day history.`;
  const keys = d.cols.map((c) => c.key).filter((k) => d.rows.some((r) => r[k] != null));
  const lead = [{ label: "#", num: true }, { label: "Song" }, { label: "Last 7 days", num: false }, { label: "Uploads", num: true }];
  $("#dd-table").innerHTML = ddHeader(keys, lead) + "<tbody>" +
    (d.rows.map((r, i) => `<tr class="click" data-song="${r.id}"><td class="num muted">${fmt(DD.offset + i + 1)}</td>
      <td class="dd-song"><div class="t1">${esc(r.title)}${labelTag(r.music_label)}</div><div class="t2">${esc(r.yt_channel || "")} · first upload ${esc(r.song_date || "")}</div></td>
      <td title="YouTube views gained each day">${sparkline(r.spark)}</td><td class="num">${fmt(r.yt_uploads_read)}</td>
      ${DD_GROUPS.flatMap(([, ks]) => ks.filter((k) => keys.includes(k))).map((k) => `<td class="num dd-first">${ddNum(r[k])}</td>${ddChangeCell(r, k)}`).join("")}</tr>`).join("")
      || `<tr><td class="empty" colspan="${4 + keys.length * 2}">No data for this day.</td></tr>`) + "</tbody>";
  $("#dd-count").textContent = d.total ? `${fmt(DD.offset + 1)}–${fmt(Math.min(DD.offset + DD.limit, d.total))} of ${fmt(d.total)} songs` : "0 songs";
  $("#dd-prev").disabled = DD.offset === 0; $("#dd-next").disabled = DD.offset + DD.limit >= d.total;
}
function ddSong(d, song) {
  const s = d.song || { title: "Song" };
  const all = d.rows;  // newest first
  const rows = DD.range ? all.slice(0, DD.range) : all;
  const days = rows.slice().reverse();
  const latest = all[0] || {};
  $("#dd-head").innerHTML = `<div class="row" style="gap:14px;flex-wrap:wrap;align-items:center">
      <button class="small" id="dd-back2">‹ All songs</button>
      ${s.yt_video_id ? `<button class="pbtn yt big" data-play="youtube" data-pid="${esc(s.yt_video_id)}" data-ptitle="${esc(s.title)}" title="Play on YouTube">▶</button>` : ""}
      ${s.spotify_id ? `<button class="pbtn sp big" data-play="spotify" data-pid="${esc(s.spotify_id)}" data-ptitle="${esc(s.title)}" title="Play on Spotify">▶</button>` : ""}
      <div style="flex:1;min-width:220px"><h2 style="margin:0">${esc(s.title)}${labelTag(s.music_label)}</h2>
        <div class="muted" style="font-size:13px;margin-top:3px">${esc(s.yt_channel || "")}${s.first_upload ? ` · first upload ${esc(s.first_upload)}` : ""} · tracked ${fmt(all.length)} days ${statusChip(s.release_status)}</div></div>
      <button class="small" id="dd-profile">Song profile</button><a class="btn small primary" id="dd-csv2" href="/api/daily.csv?song=${song}">⬇ CSV</a></div>
    <div style="margin-top:12px">${openOnButtons(s.links || {})}</div>`;
  $("#dd-back2").onclick = () => { DD.song = ""; if (location.hash.includes("song=")) location.hash = "#daily"; else loadDaily(); };
  $("#dd-profile").onclick = () => openTrack(song);
  // Summary tiles
  const sumGain = (k, n) => all.slice(0, n).reduce((a, r) => a + (ddChange(r, k) || 0), 0);
  const best = all.reduce((b, r) => (ddChange(r, "yt_views") || 0) > (b ? ddChange(b, "yt_views") || 0 : 0) ? r : b, null);
  const tile = (label, value, sub, c) => `<div class="card kpi" style="--kpi-c:${c}"><div class="label">${label}</div><div class="value">${value}</div><div class="sub">${sub}</div></div>`;
  const gainTxt = (k) => { const g = ddChange(latest, k); return g == null ? "no change yet" : `${g >= 0 ? "▲" : "▼"} ${compact(Math.abs(g))} last day`; };
  const tiles = [tile("YouTube views", compact(latest.yt_views), gainTxt("yt_views"), "var(--c-youtube)")];
  if (all.some((r) => r.sp_plays != null)) tiles.push(tile("Spotify plays", compact(all.find((r) => r.sp_plays != null).sp_plays), gainTxt("sp_plays"), "var(--c-spotify)"));
  if (all.some((r) => r.js_plays != null)) tiles.push(tile("JioSaavn plays", compact(all.find((r) => r.js_plays != null).js_plays), gainTxt("js_plays"), "var(--c-jiosaavn)"));
  if (all.some((r) => r.ytm_plays != null)) tiles.push(tile("YouTube Music plays", compact(all.find((r) => r.ytm_plays != null).ytm_plays), gainTxt("ytm_plays"), "var(--c-ytmusic)"));
  tiles.push(tile("YouTube views, last 7 days", `+${compact(sumGain("yt_views", 7))}`, `+${compact(sumGain("yt_likes", 7))} likes`, "var(--accent)"));
  tiles.push(tile("Best day", best ? `+${compact(ddChange(best, "yt_views"))}` : "–", best ? `${ddDay(best.day)} · YouTube views` : "", "var(--accent-2)"));
  $("#dd-kpis").innerHTML = tiles.slice(0, 6).join("");
  // Graphs for the chosen metric, over the chosen range; days where more uploads were linked are marked.
  const avail = d.cols.filter((c) => all.some((r) => r[c.key] != null));
  if (!avail.some((c) => c.key === DD.metric) && avail.length) DD.metric = avail[0].key;
  $("#dd-metric").innerHTML = avail.map((c) => `<button class="small${c.key === DD.metric ? " on" : ""}" data-m="${c.key}">${esc(c.label)}</button>`).join("") || '<span class="muted">No readings yet</span>';
  document.querySelectorAll("#dd-range button").forEach((b) => b.classList.toggle("on", +b.dataset.r === (DD.range || 0)));
  const col = d.cols.find((c) => c.key === DD.metric) || { label: "" };
  const marks = DD.metric.startsWith("yt_") ? days.filter((r, i) => i && days[i - 1].yt_uploads_read != null && r.yt_uploads_read > days[i - 1].yt_uploads_read)
    .map((r, i) => ({ x: r.day, label: ((n) => `+${n} upload${n === 1 ? "" : "s"}`)(r.yt_uploads_read - days[days.indexOf(r) - 1].yt_uploads_read) })) : [];
  $("#dd-marks-note").style.display = marks.length ? "" : "none";
  $("#dd-marks-note").textContent = marks.length ? "Dashed lines: days when more of the song's uploads were linked, so the total jumps. The gain each day only counts uploads read on both days, so it isn't inflated." : "";
  $("#dd-total-title").textContent = `${col.label}: total by day`;
  lineChart($("#dd-total"), days.filter((r) => r[DD.metric] != null).map((r) => ({ x: r.day, y: r[DD.metric] })), { label: col.label, marks });
  const isRank = DD.metric === "dz_rank" || DD.metric === "chart_rank";
  $("#dd-gain-title").textContent = isRank ? `${col.label}: change each day` : `${col.label}: gained each day`;
  barChart($("#dd-gain"), days.map((r) => ({ label: new Date(r.day + "T00:00:00Z").toLocaleDateString("en-GB", { day: "numeric", month: "short", timeZone: "UTC" }), day: r.day, value: ddChange(r, DD.metric) || 0, missing: ddChange(r, DD.metric) == null })),
    { tip: (b) => `<b>${ddDay(b.day)}</b><br>${b.missing ? "no reading the day before" : (b.value > 0 ? "+" : "") + fmt(b.value)}` });
  loadCompare(song);
  loadShorts(song);
  const keys = d.cols.map((c) => c.key).filter((k) => rows.some((r) => r[k] != null));
  $("#dd-table").innerHTML = ddHeader(keys, [{ label: "Day" }, { label: "Uploads", num: true }]) + "<tbody>" +
    (rows.map((r) => `<tr><td class="dd-day" title="${esc(r.day)}"><b>${ddDay(r.day)}</b></td><td class="num">${fmt(r.yt_uploads_read)}</td>
      ${DD_GROUPS.flatMap(([, ks]) => ks.filter((k) => keys.includes(k))).map((k) => `<td class="num dd-first">${ddNum(r[k])}</td>${ddChangeCell(r, k)}`).join("")}</tr>`).join("")
      || `<tr><td class="empty" colspan="${2 + keys.length * 2}">No daily data for this song yet.</td></tr>`) + "</tbody>";
}
["day", "label", "sort"].forEach((k) => ($(`#dd-${k}`).onchange = (e) => { DD[k] = e.target.value; DD.offset = 0; loadDaily(); }));
let ddTimer;
$("#dd-q").addEventListener("input", (e) => { clearTimeout(ddTimer); ddTimer = setTimeout(() => { DD.q = e.target.value; DD.offset = 0; loadDaily(); }, 300); });
$("#dd-prev").onclick = () => { DD.offset = Math.max(0, DD.offset - DD.limit); loadDaily(); };
$("#dd-next").onclick = () => { DD.offset += DD.limit; loadDaily(); };
$("#dd-back").onclick = () => { DD.song = ""; loadDaily(); };
$("#dd-dprev").onclick = () => { const o = $("#dd-day").selectedIndex; if (o < $("#dd-day").options.length - 1) { DD.day = $("#dd-day").options[o + 1].value; DD.offset = 0; loadDaily(); } };
$("#dd-dnext").onclick = () => { const o = $("#dd-day").selectedIndex; if (o > 0) { DD.day = $("#dd-day").options[o - 1].value; DD.offset = 0; loadDaily(); } };
$("#dd-range").addEventListener("click", (e) => { const b = e.target.closest("button[data-r]"); if (b) { DD.range = +b.dataset.r; loadDaily(); } });
$("#dd-metric").addEventListener("click", (e) => { const b = e.target.closest("button[data-m]"); if (b) { DD.metric = b.dataset.m; loadDaily(); } });
$("#dd-table").addEventListener("click", (e) => { const tr = e.target.closest("tr[data-song]"); if (tr) { DD.song = tr.dataset.song; loadDaily(); } });

/* ---------- Shorts and Reels made with the song's official audio (official sounds only) ---------- */
const SH = { period: "day" };
const soundLink = (x) => x.platform === "instagram" ? `https://www.instagram.com/reels/audio/${x.video_id}/` : `https://www.youtube.com/source/${x.video_id}/shorts`;
const clipLink = (x) => x.platform === "instagram" ? `https://www.instagram.com/reel/${x.video_id.replace(/^ig:/, "")}/` : `https://www.youtube.com/shorts/${x.video_id}`;
const usedCount = (x) => x.official == null ? '<span class="muted">reading soon</span>' : x.official ? (x.count_text ? esc(x.count_text.replace(/ (Shorts?|reels?)$/i, "")) : "0") : "not counted";
async function loadShorts(song) {
  const d = await api(`/api/shorts?${new URLSearchParams({ song, period: SH.period })}`);
  $("#dd-shorts").style.display = "";
  document.querySelectorAll("#sh-period button").forEach((b) => b.classList.toggle("on", b.dataset.p === SH.period));
  const dev = !isClient();
  const add = dev ? `<div class="row" style="gap:8px;margin-top:12px;flex-wrap:wrap"><input id="sh-ig-url" placeholder="Paste the song's Instagram audio link (instagram.com/reels/audio/…)" style="flex:1;min-width:260px">
      <button class="small primary" id="sh-ig-add">Add Instagram audio</button></div>` : "";
  const sum = (plat) => d.sounds.filter((x) => x.platform === plat && x.official).reduce((a, x) => a + (x.shorts || 0), 0);
  const nOf = (plat) => d.sounds.filter((x) => x.platform === plat && x.official).length;
  $("#sh-total").textContent = [nOf("youtube") ? `${fmt(sum("youtube"))} YouTube Shorts` : "", nOf("instagram") ? `${fmt(sum("instagram"))} Instagram Reels` : ""].filter(Boolean).join(" · ");
  if (!d.sounds.length) {
    $("#sh-body").innerHTML = `<div class="empty">Counts are read once a day from the official sounds: YouTube's "Shorts using this sound" page of the song's label uploads, and the song's Instagram audio page once its link is added. Nothing read for this song yet.</div>` + add;
  } else {
    const name = { day: "Day", week: "Week", month: "Month", year: "Year" }[SH.period];
    const gainCell = (g) => `<td class="num ${g > 0 ? "delta-up" : ""}">${g == null ? "–" : (g > 0 ? "+" : "") + fmt(g)}</td>`;
    const sounds = `<table><thead><tr><th>Official sound</th><th>Owner / artists</th><th class="num">Made with it</th><th class="num">Views of top clips</th>${dev ? "<th></th>" : ""}</tr></thead><tbody>` +
      d.sounds.map((x) => `<tr${x.official === 0 ? ' class="muted" title="Not the official audio: not counted"' : ""}><td><div class="t1">${x.platform === "instagram" ? "Instagram" : "YouTube"} ·
        <a href="${soundLink(x)}" target="_blank" rel="noopener">${esc(x.sound_title || x.upload || x.video_id)}</a></div>
        <div class="t2">${esc(x.asset_type || (x.platform === "instagram" ? "Reels audio" : ""))}${x.day ? ` · read ${esc(x.day)}` : ""}</div></td><td>${esc(x.sound_owner || "–")}</td>
        <td class="num"><b>${usedCount(x)}</b></td><td class="num">${x.listed_views ? compact(x.listed_views) : "–"}</td>
        ${dev ? `<td>${x.platform === "instagram" ? `<button class="small" data-ig-remove="${x.platform_item_id}" title="Remove this Instagram audio from the song">✕</button>` : ""}</td>` : ""}</tr>`).join("") + "</tbody></table>";
    const series = `<table><thead><tr><th>${name}</th><th class="num">YouTube Shorts</th><th class="num">New</th><th class="num">Instagram Reels</th><th class="num">New</th></tr></thead><tbody>` +
      (d.series.map((r) => `<tr><td>${esc(SH.period === "day" ? ddDay(r.period) : r.period)}</td><td class="num">${r.total == null ? "–" : fmt(r.total)}</td>${gainCell(r.gain)}
        <td class="num">${r.reels == null ? "–" : fmt(r.reels)}</td>${gainCell(r.reels_gain)}</tr>`).join("") || `<tr><td class="empty" colspan="5">No readings yet.</td></tr>`) + "</tbody></table>";
    const top = d.top.length ? `<table><thead><tr><th>Most-viewed clips using the official audio</th><th class="num">Views</th><th class="num">Likes</th><th>First seen</th></tr></thead><tbody>` +
      d.top.map((x) => `<tr><td>${x.platform === "instagram" ? "Instagram Reel" : "YouTube Short"} · <a href="${clipLink(x)}" target="_blank" rel="noopener">${esc(x.title || x.video_id.replace(/^ig:/, ""))}</a></td>
        <td class="num">${x.views == null ? "–" : compact(x.views)}</td><td class="num">${x.likes == null ? "–" : compact(x.likes)}</td><td class="muted">${esc(x.first_seen)}</td></tr>`).join("") + "</tbody></table>" : "";
    $("#sh-body").innerHTML = `<div class="grid g2"><div class="table-wrap">${sounds}</div><div class="table-wrap" style="max-height:320px">${series}</div></div>` +
      (top ? `<div class="table-wrap" style="margin-top:14px;max-height:360px">${top}</div>` : "") + add;
  }
  const btn = $("#sh-ig-add");
  if (btn) btn.onclick = async () => {
    const url = $("#sh-ig-url").value.trim();
    if (!url) return;
    try { await api("/api/instagram/link", { song: +song, url }); loadShorts(song); } catch (e) { alert(e.message || e); }
  };
}
$("#sh-period").addEventListener("click", (e) => { const b = e.target.closest("button[data-p]"); if (b && DD.song) { SH.period = b.dataset.p; loadShorts(DD.song); } });
$("#sh-body").addEventListener("click", async (e) => {
  const b = e.target.closest("[data-ig-remove]");
  if (!b || !DD.song) return;
  if (!(await confirmBox({ title: "Remove this Instagram audio?", text: "The song stops counting its Reels. Readings already taken are kept.", ok: "Remove" }))) return;
  await api("/api/instagram/link", { remove: +b.dataset.igRemove }); loadShorts(DD.song);
});

/* ---------- Cross-platform comparison of one song ---------- */
// Platform colours come from the theme (style.css --c-*), so charts and chips follow light / dark.
const PCOLOR = { instagram: "var(--c-instagram)", youtube: "var(--c-youtube)", ytmusic: "var(--c-ytmusic)", spotify: "var(--c-spotify)", jiosaavn: "var(--c-jiosaavn)", deezer: "var(--c-deezer)", apple: "var(--c-apple)" };
function multiLine(el, series, { height = 220, xfmt = (x) => x.slice(5), tipx = (x) => x, yfmt = compact, vfmt = fmt, empty = "Daily gains appear after two consecutive days of readings.", dots = true } = {}) {  // series: [{name, color, points:[{x, y}]}], shared x (days)
  el.innerHTML = "";
  const xs = [...new Set(series.flatMap((s) => s.points.map((p) => p.x)))].sort();
  const ys = series.flatMap((s) => s.points.map((p) => p.y));
  if (!xs.length || !ys.length) { el.innerHTML = `<div class="empty">${esc(empty)}</div>`; return; }
  const dual = series.some((s) => s.axis === "right") && series.some((s) => s.axis !== "right");
  const ysL = series.filter((s) => !dual || s.axis !== "right").flatMap((s) => s.points.map((p) => p.y));
  const ysR = dual ? series.filter((s) => s.axis === "right").flatMap((s) => s.points.map((p) => p.y)) : [];
  const yMax = niceMax(Math.max(...ysL, 1)), yMin = Math.min(0, ...ysL);
  const yMax2 = dual ? niceMax(Math.max(...ysR, 1)) : 0, yMin2 = dual ? Math.min(0, ...ysR) : 0;
  const W = el.clientWidth || 600, H = height, m = { t: 12, r: dual ? 56 : 14, b: 26, l: 52 }, iw = W - m.l - m.r, ih = H - m.t - m.b;
  const X = (x) => m.l + (xs.length === 1 ? iw / 2 : (iw * xs.indexOf(x)) / (xs.length - 1)), Y = (y) => m.t + ih - (ih * (y - yMin)) / (yMax - yMin || 1);
  const Y2 = (y) => m.t + ih - (ih * (y - yMin2)) / (yMax2 - yMin2 || 1);
  const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, height: H, role: "img", "aria-label": "Daily gain by platform" }, el);
  const g = svgEl("g", { class: "grid" }, svg), ax = svgEl("g", { class: "axis" }, svg);
  for (let i = 0; i <= 4; i++) { const v = yMin + ((yMax - yMin) * i) / 4, y = Y(v);
    svgEl("line", { x1: m.l, x2: W - m.r, y1: y, y2: y }, g); svgEl("text", { x: m.l - 8, y: y + 4, "text-anchor": "end" }, ax).textContent = yfmt(v);
    if (dual) { const v2 = yMin2 + ((yMax2 - yMin2) * i) / 4; svgEl("text", { x: W - m.r + 8, y: y + 4, "text-anchor": "start" }, ax).textContent = compact(v2); } }
  if (dual) {
    const nm = (f) => series.filter(f).map((s) => s.name.replace(/ (views|plays)$/, "")).join(", ");
    svgEl("text", { x: m.l - 8, y: m.t - 2, "text-anchor": "end", style: "font-size:10px" }, ax).textContent = "◀ " + nm((s) => s.axis !== "right");
    svgEl("text", { x: W - m.r + 8, y: m.t - 2, "text-anchor": "start", style: "font-size:10px" }, ax).textContent = "▶";
  }
  const step = Math.max(1, Math.ceil(xs.length / 8));
  xs.forEach((x, i) => { if (i % step === 0) svgEl("text", { x: X(x), y: H - 8, "text-anchor": "middle" }, ax).textContent = xfmt(x); });
  const uid = Math.random().toString(36).slice(2, 7);
  series.forEach((s, si) => {
    const Ys = dual && s.axis === "right" ? Y2 : Y, lo = dual && s.axis === "right" ? yMin2 : yMin;
    const pts = s.points.filter((p) => p.y != null);
    const d = pts.map((p, i) => `${i ? "L" : "M"}${X(p.x)},${Ys(p.y)}`).join(" "), base = Ys(Math.max(lo, 0));
    if (pts.length > 1) {
      gradient(svg, `ml-${uid}-${si}`, s.color, series.length > 2 ? 0.12 : 0.22, 0);
      svgEl("path", { d: `${d} L${X(pts[pts.length - 1].x)},${base} L${X(pts[0].x)},${base} Z`, fill: `url(#ml-${uid}-${si})`, class: "area" }, svg);
      svgEl("path", { d, fill: "none", stroke: s.color, "stroke-width": 2.5, "stroke-linejoin": "round", "stroke-linecap": "round", pathLength: 1, class: "line" }, svg);
    }
    pts.forEach((p) => { const c = svgEl("circle", { cx: X(p.x), cy: Ys(p.y), r: dots ? 3.5 : 2.2, fill: s.color, stroke: "var(--surface-1)", "stroke-width": dots ? 1.5 : 1 }, svg); svgEl("title", {}, c).textContent = `${s.name} ${tipx(p.x)}: ${p.y > 0 && !s.total ? "+" : ""}${vfmt(p.y)}`; });
  });
}
async function loadCompare(song) {
  const d = await api(`/api/song/compare?id=${song}`);
  const sign = (v) => v == null ? '<span class="muted">–</span>' : (v > 0 ? "+" : "") + (Math.abs(v) >= 1e5 ? compact(v) : fmt(v));
  const val = (v) => v == null ? '<span class="muted">–</span>' : (Math.abs(v) >= 1e5 ? compact(v) : fmt(v));
  $("#dd-compare-note").textContent = d.total_plays ? `${compact(d.total_plays)} views and plays in all (Deezer is a rank, not counted)` : "";
  $("#dd-compare-table").innerHTML = `<thead><tr><th>Platform</th><th>Released there</th><th class="num">Latest</th><th class="num">Gain last day</th><th class="num">Gain 7 days</th><th class="num">Share of plays</th><th>Link</th></tr></thead><tbody>` +
    d.platforms.map((p) => `<tr><td><span class="dot" style="display:inline-block;width:9px;height:9px;border-radius:50%;background:${PCOLOR[p.platform]};margin-right:6px"></span>${esc(p.label)}${p.items > 1 ? ` <span class="muted">(${p.items})</span>` : ""}</td>
      <td class="muted">${esc(p.released || "–")}</td><td class="num">${val(p.latest)}${p.unit === "rank" && p.latest != null ? ' <span class="muted">rank</span>' : ""}</td>
      <td class="num">${sign(p.gain_1d)}</td><td class="num">${sign(p.gain_7d)}</td>
      <td class="num">${p.share == null ? '<span class="muted">–</span>' : `<span class="share-bar" style="--w:${p.share}%;--c:${PCOLOR[p.platform]}">${p.share}%</span>`}</td>
      <td>${p.url ? `<a href="${esc(p.url)}" target="_blank" rel="noopener">open ↗</a>` : '<span class="muted">not found yet</span>'}</td></tr>`).join("") + "</tbody>";
  const series = d.platforms.filter((p) => p.unit !== "rank").map((p) => ({ name: p.label, color: PCOLOR[p.platform], points: p.series.filter((x) => x.gain != null).map((x) => ({ x: x.day, y: x.gain })) }))
    .filter((s) => s.points.length);
  multiLine($("#dd-compare-chart"), series);
  $("#dd-compare-legend").innerHTML = series.map((s) => `<span><span style="display:inline-block;width:12px;height:3px;background:${s.color};vertical-align:middle;margin-right:5px"></span>${esc(s.name)}</span>`).join("");
}

/* ---------- Platform data: every item of a platform, its dates and its stats on a day vs another ---------- */
const PD = { platform: "youtube", day: "", cmp: "", label: "", rfrom: "", rto: "", read: "", q: "", sort: "", order: "desc", offset: 0, limit: 100 };
const PD_COLS = { views: "Views", likes: "Likes", comments: "Comments", plays: "Plays", rank: "Rank" };
const pdParams = (extra = {}) => new URLSearchParams(Object.fromEntries(Object.entries({ platform: PD.platform, day: PD.day, cmp: PD.cmp, label: PD.label,
  rel_from: PD.rfrom, rel_to: PD.rto, read: PD.read, q: PD.q, sort: PD.sort, order: PD.order, ...extra }).filter(([, v]) => v !== "" && v != null)));
async function loadPData() {
  const d = await api(`/api/items?${pdParams({ offset: PD.offset, limit: PD.limit })}`);
  PD.day = d.day; PD.cmp = d.cmp || ""; PD.sort = d.sort; PD.order = d.order;
  $("#pd-tabs").innerHTML = d.platforms.map((p) => `<button class="tab${p.key === d.platform ? " active" : ""}" data-pd="${p.key}">${esc(p.label)}</button>`).join("");
  const opts = (sel, withNone) => (withNone ? `<option value="">–</option>` : "") + d.days.slice().reverse().map((x) => `<option ${x === sel ? "selected" : ""}>${x}</option>`).join("");
  $("#pd-day").innerHTML = opts(d.day) || `<option>${esc(d.day)}</option>`; $("#pd-cmp").innerHTML = opts(d.cmp, true);
  if ($("#pd-label").options.length === 1) d.labels.forEach((l) => $("#pd-label").insertAdjacentHTML("beforeend", `<option>${esc(l)}</option>`));
  $("#pd-csv").href = `/api/items.csv?${pdParams()}` + (FOCUS_ALL ? "&focus=all" : "");
  const t = d.totals || {}, sign = (v) => v == null ? "–" : (v > 0 ? "+" : "") + (Math.abs(v) >= 1e5 ? compact(v) : fmt(v));
  const tiles = [["Items", fmt(d.total), `${fmt(d.tracked)} tracked · ${d.tracked ? Math.round((100 * d.total) / d.tracked) : 0}% read on ${d.day}`]];
  d.metrics.filter((m) => m !== "rank").forEach((m) => tiles.push([`${PD_COLS[m]} on ${d.day}`, compact(t[m]), d.cmp ? `${sign(t[m + "_change"])} vs ${d.cmp}` : "no comparison day"]));
  tiles.push(["Released that day", fmt(t.new_on_day || 0), `${d.label} items released on ${d.day}`]);
  $("#pd-kpis").innerHTML = tiles.slice(0, 6).map(([l, v, x]) => `<div class="card kpi"><div class="label">${esc(l)}</div><div class="value">${esc(v)}</div><div class="sub">${esc(x)}</div></div>`).join("");
  const head = [["song", "Song"], ["title", d.platform === "youtube" ? "Upload" : "Title"], ["channel", d.platform === "youtube" ? "Channel" : "Label"], ["released", "Released"], ["first_seen", "First seen"], ["days_tracked", "Days tracked"]]
    .concat(d.metrics.flatMap((m) => [[m, `${PD_COLS[m]} ${d.day.slice(5)}`], [m + "_change", d.cmp ? `Change vs ${d.cmp.slice(5)}` : "Change"]]));
  const arrow = (k) => (k === d.sort ? (d.order === "asc" ? " ↑" : " ↓") : "");
  const num = (v) => v == null ? '<span class="muted">–</span>' : (Math.abs(v) >= 1e5 ? compact(v) : fmt(v));
  $("#pd-table").innerHTML = `<thead><tr>${head.map(([k, l]) => `<th class="sortable${["days_tracked"].includes(k) || d.metrics.some((m) => k.startsWith(m)) ? " num" : ""}" data-sort="${k}">${esc(l)}${arrow(k)}</th>`).join("")}</tr></thead><tbody>` +
    (d.rows.map((r) => `<tr class="click" data-item="${r.id}"><td>${r.song ? `<div class="t1">${esc(r.song)}</div>` : '<span class="muted">not a song</span>'}</td>
      <td><a href="${esc(r.url)}" target="_blank" rel="noopener" onclick="event.stopPropagation()">${esc((r.title || "").slice(0, 80))}</a>${r.asset_type ? `<div class="t2">${esc(r.asset_type)}</div>` : ""}</td>
      <td>${esc(r.channel || "")}${r.label ? `<div class="t2">${labelTag(r.label)}</div>` : ""}</td><td class="muted">${esc(r.released || "–")}</td><td class="muted">${esc(r.first_seen || "–")}</td>
      <td class="num">${fmt(r.days_tracked)}</td>${d.metrics.map((m) => `<td class="num">${num(r[m])}</td><td class="num ${(r[m + "_change"] || 0) > 0 ? "delta-up" : ""}">${r[m + "_change"] == null ? '<span class="muted">–</span>' : sign(r[m + "_change"])}</td>`).join("")}</tr>`).join("")
      || `<tr><td class="empty" colspan="${head.length}">No ${esc(d.label)} items match.</td></tr>`) + "</tbody>";
  $("#pd-count").textContent = d.total ? `${fmt(PD.offset + 1)}–${fmt(Math.min(PD.offset + PD.limit, d.total))} of ${fmt(d.total)}` : "0";
  $("#pd-prev").disabled = $("#pd-first").disabled = PD.offset === 0; $("#pd-next").disabled = PD.offset + PD.limit >= d.total;
}
async function openItem(id) {
  const d = await api(`/api/items/history?id=${id}`), it = d.item, ms = d.metrics;
  $("#drawer").classList.remove("wide");
  let metric = ms[0];
  const render = () => {
    const pts = d.history.filter((h) => h[metric] != null);
    lineChart($("#it-total"), pts.map((h) => ({ x: h.day, y: h[metric] })), { label: PD_COLS[metric] || metric });
    barChart($("#it-gain"), d.history.map((h) => ({ label: h.day.slice(5), day: h.day, value: h[metric + "_change"] || 0, missing: h[metric + "_change"] == null })),
      { tip: (b) => `<b>${esc(b.day)}</b><br>${b.missing ? "no reading the day before" : (b.value > 0 ? "+" : "") + fmt(b.value)}` });
    document.querySelectorAll("#it-metric button").forEach((b) => b.classList.toggle("on", b.dataset.m === metric));
  };
  $("#drawer").innerHTML = `<div class="row"><div style="flex:1"><h1>${esc(it.title || "")}</h1><div class="muted">${esc(it.platform)} · ${esc(it.channel || "")}</div></div>
      ${it.song_id ? `<button class="small" id="it-song">Song details</button>` : ""}<button class="small" id="dr-close">✕</button></div>
    <dl class="kv"><dt>Song</dt><dd>${esc(it.song || "–")}</dd><dt>Released</dt><dd>${esc(it.released || "–")}</dd><dt>First seen</dt><dd>${esc(it.first_seen || "–")}</dd>
      <dt>Link</dt><dd><a href="${esc(it.url)}" target="_blank" rel="noopener">${esc(it.url || "")}</a></dd><dt>Days with stats</dt><dd>${fmt(d.history.length)}</dd></dl>
    ${ms.length ? `<div class="seg" id="it-metric" style="margin:8px 0">${ms.map((m) => `<button class="small" data-m="${m}">${esc(PD_COLS[m] || m)}</button>`).join("")}</div>
      <h3>Total by day</h3><div class="chart" id="it-total"></div><h3>Change each day</h3><div class="chart" id="it-gain"></div>` : '<p class="muted">This platform has no daily numbers (metadata only).</p>'}
    <h3>Every day</h3><div class="card flush"><table><thead><tr><th>Day</th>${ms.map((m) => `<th class="num">${esc(PD_COLS[m] || m)}</th><th class="num">Change</th>`).join("")}</tr></thead><tbody>
      ${d.history.slice().reverse().map((h) => `<tr><td>${esc(h.day)}</td>${ms.map((m) => `<td class="num">${h[m] == null ? "–" : fmt(h[m])}</td><td class="num">${h[m + "_change"] == null ? "–" : (h[m + "_change"] > 0 ? "+" : "") + fmt(h[m + "_change"])}</td>`).join("")}</tr>`).join("") || `<tr><td class="empty">No readings yet.</td></tr>`}</tbody></table></div>`;
  $("#drawer").classList.add("open"); $("#drawer-bg").classList.add("open");
  $("#dr-close").onclick = closeDrawer;
  if ($("#it-song")) $("#it-song").onclick = () => openTrack(it.song_id);
  if (ms.length) { $("#it-metric").onclick = (e) => { const b = e.target.closest("button[data-m]"); if (b) { metric = b.dataset.m; render(); } }; render(); }
}
$("#pd-tabs").addEventListener("click", (e) => { const b = e.target.closest("[data-pd]"); if (b) { Object.assign(PD, { platform: b.dataset.pd, day: "", cmp: "", sort: "", offset: 0 }); loadPData(); } });
[["day", "pd-day"], ["cmp", "pd-cmp"], ["label", "pd-label"], ["rfrom", "pd-rfrom"], ["rto", "pd-rto"], ["read", "pd-read"]]
  .forEach(([k, id]) => ($(`#${id}`).onchange = (e) => { PD[k] = e.target.value; PD.offset = 0; loadPData(); }));
$("#pd-limit").onchange = (e) => { PD.limit = +e.target.value; PD.offset = 0; loadPData(); };
let pdTimer;
$("#pd-q").addEventListener("input", (e) => { clearTimeout(pdTimer); pdTimer = setTimeout(() => { PD.q = e.target.value; PD.offset = 0; loadPData(); }, 300); });
$("#pd-first").onclick = () => { PD.offset = 0; loadPData(); };
$("#pd-prev").onclick = () => { PD.offset = Math.max(0, PD.offset - PD.limit); loadPData(); };
$("#pd-next").onclick = () => { PD.offset += PD.limit; loadPData(); };
$("#pd-table").addEventListener("click", (e) => {
  const th = e.target.closest("th[data-sort]");
  if (th) { const k = th.dataset.sort; PD.order = PD.sort === k && PD.order === "desc" ? "asc" : "desc"; PD.sort = k; PD.offset = 0; loadPData(); return; }
  const tr = e.target.closest("tr[data-item]"); if (tr) openItem(tr.dataset.item);
});

/* ---------- Player ---------- */
// Embedded players: YouTube plays the full video; Spotify plays the full track when the viewer is
// logged in to Spotify in this browser (otherwise a 30-second preview).
function play(kind, id, title) {
  if (!id) return;
  const yt = kind === "youtube";
  const src = yt ? `https://www.youtube.com/embed/${encodeURIComponent(id)}?autoplay=1&rel=0`
                 : `https://open.spotify.com/embed/track/${encodeURIComponent(id)}?utm_source=generator&theme=0`;
  const site = yt ? `https://www.youtube.com/watch?v=${encodeURIComponent(id)}` : `https://open.spotify.com/track/${encodeURIComponent(id)}`;
  $("#player-frame").innerHTML = `<iframe src="${src}" class="${yt ? "pf-yt" : "pf-sp"}" allow="autoplay; clipboard-write; encrypted-media; fullscreen; picture-in-picture"
    allowfullscreen loading="lazy" title="${esc(title || "")}"></iframe>`;
  $("#player-src").className = `player-src ${yt ? "src-yt" : "src-sp"}`;
  $("#player-src").textContent = yt ? "YouTube" : "Spotify";
  $("#player-title").textContent = title || "";
  $("#player-id").textContent = `${yt ? "video" : "track"} id ${id}`;
  const o = $("#player-open"); o.href = site; o.textContent = `Open on ${yt ? "YouTube" : "Spotify"} ↗`;
  $("#player").style.display = "block";
}
$("#player-close").onclick = () => { $("#player-frame").innerHTML = ""; $("#player").style.display = "none"; };
document.addEventListener("click", (e) => {
  const b = e.target.closest("[data-play]");
  if (!b) return;
  e.stopPropagation(); e.preventDefault();
  play(b.dataset.play, b.dataset.pid, b.dataset.ptitle);
}, true);
// Link buttons after the two play buttons: field with the URL, style, label, platform name.
// "Open on" buttons: every platform the song is on, in its colour (new tab); greyed when not found there yet.
const OPEN_ON = [["youtube", "YouTube", "yt"], ["ytmusic", "YouTube Music", "ym"], ["spotify", "Spotify", "sp"], ["jiosaavn", "JioSaavn", "js"],
  ["apple", "Apple Music", "am"], ["deezer", "Deezer", "dz"], ["musicbrainz", "MusicBrainz", "mb"]];
const openOnButtons = (urls) => `<div class="open-on"><span class="open-on-l">Open on</span>${OPEN_ON.map(([k, n, c]) => urls[k]
  ? `<a class="ob ob-${c}" href="${esc(urls[k])}" target="_blank" rel="noopener"><i></i>${n}<span class="ob-arrow">↗</span></a>`
  : `<span class="ob off" title="Not found on ${n} yet"><i></i>${n}</span>`).join("")}</div>`;
const PLATFORM_LINKS = [["ytm_url", "ym", "YM", "YouTube Music"], ["js_url", "js", "J", "JioSaavn"], ["apple_url", "am", "A", "Apple Music"], ["deezer_url", "dz", "D", "Deezer"]];
const playBtns = (r) => `<td class="play-cell">${r.yt_video_id ? `<button class="pbtn yt" data-play="youtube" data-pid="${esc(r.yt_video_id)}" data-ptitle="${esc(r.title)}" title="Play on YouTube (${esc(r.yt_video_id)})">▶</button>` : `<span class="pbtn yt off">▶</span>`}${
  r.spotify_id && ["auto", "approved"].includes(r.spotify_state) ? `<button class="pbtn sp" data-play="spotify" data-pid="${esc(r.spotify_id)}" data-ptitle="${esc(r.title)}" title="Play on Spotify (${esc(r.spotify_id)})">▶</button>` : `<span class="pbtn sp off" title="not on Spotify">▶</span>`}${PLATFORM_LINKS.map(([k, cls, txt, name]) => r[k]
    ? `<a class="pbtn ${cls}" href="${esc(r[k])}" target="_blank" rel="noopener" title="Open on ${name}${k === "js_url" && r.js_plays != null ? ` (${fmt(r.js_plays)} plays)` : ""}">${txt}</a>`
    : `<span class="pbtn ${cls} off" title="not on ${name} yet">${txt}</span>`).join("")}</td>`;

/* ---------- Songs ---------- */
const CAT = { q: "", status: "", label: "", language: "", category: "", meta: "", channel: "", day: "", since: "", until: "", sel: new Set(), sort: "song_date", order: "desc", offset: 0, limit: 50, total: 0 };
const COLS = [[null, "sel"], [null, "Play"], ["title", "Song"], ["song_date", "First upload"], [null, "Spotify"], [null, "Videos"], ["yt_views", "Views"],
  ["yt_delta", "Views Δ day"], ["yt_likes", "Likes"], ["yt_comments", "Comments"], ["deezer_rank", "Deezer rank"]];
const NUMCOLS = new Set(["Videos", "Views", "Views Δ day", "Likes", "Comments", "Deezer rank"]);
async function loadCatalogue() {
  if (!STATUS) await refreshStatus();
  document.querySelectorAll(".since").forEach((e) => (e.textContent = STATUS.config.discovery.since));
  $("#cat-q").value = CAT.q; $("#cat-status").value = CAT.status;
  const d = STATUS.config.discovery;
  if (!CAT.since) CAT.since = d.since;
  if (!CAT.until) CAT.until = d.until || STATUS.today;
  $("#cat-from").value = CAT.since; $("#cat-to").value = CAT.until;
  await fetchTracks();
}
async function fetchTracks() {
  const p = new URLSearchParams({ since: CAT.since, until: CAT.until, q: CAT.q, status: CAT.status, label: CAT.label,
    language: CAT.language, category: CAT.category, meta: CAT.meta, channel: CAT.channel, day: CAT.day, sort: CAT.sort, order: CAT.order, offset: CAT.offset, limit: CAT.limit });
  const d = await api(`/api/tracks?${p}`);
  CAT.total = d.total;
  [["language", "All languages"], ["category", "All categories"]].forEach(([f, all]) => {
    const el = $(`#cat-${f}`);
    el.innerHTML = `<option value="">${all}</option>` + (d.facets[f] || []).map((v) => `<option>${esc(v)}</option>`).join("");
    el.value = CAT[f];
  });
  const ls = $("#cat-label");
  ls.innerHTML = `<option value="">All labels</option>` + d.labels.map((l) => `<option>${esc(l)}</option>`).join(""); ls.value = CAT.label;
  const cs = $("#cat-channel"), shown = d.channels.filter((c) => !CAT.label || c.label === CAT.label);
  cs.innerHTML = `<option value="">All channels</option>` + [...new Set(shown.map((c) => c.label))].map((l) =>
    `<optgroup label="${esc(l)}">${shown.filter((c) => c.label === l).map((c) => `<option>${esc(c.title)}</option>`).join("")}</optgroup>`).join("");
  cs.value = CAT.channel;
  $("#cat-day-chip").innerHTML = (CAT.meta ? `<span class="chip">${esc(CAT.metaName || CAT.meta)}<button id="cat-meta-x">✕</button></span> ` : "") + (CAT.day ? `<span class="chip">first upload ${esc(CAT.day)}<button id="cat-day-x">✕</button></span>` : "");
  if (CAT.meta) $("#cat-meta-x").onclick = () => { CAT.meta = ""; CAT.offset = 0; fetchTracks(); };
  if (CAT.day) $("#cat-day-x").onclick = () => { CAT.day = ""; CAT.offset = 0; fetchTracks(); };
  const head = COLS.map(([k, l]) => {
    if (l === "sel") return `<th class="sel"><input type="checkbox" id="cat-all" title="Select all on this page"></th>`;
    const cls = (k ? "sortable " : "") + (NUMCOLS.has(l) ? "num" : "");
    const arrow = CAT.sort === k ? `<span class="arrow">${CAT.order === "asc" ? " ↑" : " ↓"}</span>` : "";
    return `<th class="${cls}" ${k ? `data-sort="${k}"` : ""}>${l}${arrow}</th>`;
  }).join("");
  const body = d.rows.length ? d.rows.map((r) => `<tr class="click" data-id="${r.id}"><td class="sel"><input type="checkbox" data-sel="${r.id}" ${CAT.sel.has(r.id) ? "checked" : ""}></td>${playBtns(r)}${trackCell(r)}<td class="muted">${esc(r.song_date || "–")}</td>
      <td>${statusChip(r.release_status)}${r.release_date ? `<div class="t2">${esc(r.release_date)}${r.release_title ? ` · ${esc(r.release_title.slice(0, 28))}` : ""}${r.spotify_versions > 1 ? ` · ${r.spotify_versions} Spotify versions` : ""}</div>` : ""}</td>
      ${ytCell(r)}${viewsTypeCell(r)}${deltaCell(r.yt_delta)}${viewsCell(r.yt_likes)}${viewsCell(r.yt_comments)}
      <td class="num">${r.deezer_rank != null ? fmt(r.deezer_rank) : '<span class="muted">–</span>'}</td></tr>`).join("")
    : `<tr><td colspan="${COLS.length}" class="empty">No songs match.</td></tr>`;
  $("#cat-table").innerHTML = `<thead><tr>${head}</tr></thead><tbody>${body}</tbody>`;
  $("#cat-count").textContent = d.total ? `${fmt(CAT.offset + 1)}–${fmt(Math.min(CAT.offset + CAT.limit, d.total))} of ${fmt(d.total)} songs` : "0 songs";
  $("#cat-prev").disabled = CAT.offset === 0;
  $("#cat-next").disabled = CAT.offset + CAT.limit >= d.total;
  CAT.page = d.rows.map((r) => r.id);
  renderSel();
}
function renderSel() {
  const n = CAT.sel.size;
  $("#cat-selbar").style.display = n ? "flex" : "none";
  $("#cat-selcount").textContent = `${n} song${n === 1 ? "" : "s"} selected`;
  $("#cat-merge").disabled = $("#cat-notsame").disabled = n < 2;
  const all = $("#cat-all");
  if (all) all.checked = CAT.page && CAT.page.length > 0 && CAT.page.every((id) => CAT.sel.has(id));
}
$("#cat-table").addEventListener("change", (e) => {
  if (e.target.id === "cat-all") {
    CAT.page.forEach((id) => (e.target.checked ? CAT.sel.add(id) : CAT.sel.delete(id)));
    document.querySelectorAll("#cat-table input[data-sel]").forEach((c) => (c.checked = e.target.checked));
  } else if (e.target.dataset.sel) {
    const id = +e.target.dataset.sel;
    e.target.checked ? CAT.sel.add(id) : CAT.sel.delete(id);
  }
  renderSel();
});
$("#cat-selclear").onclick = () => { CAT.sel.clear(); fetchTracks(); };
$("#cat-merge").onclick = async () => {
  const ids = [...CAT.sel];
  if (!confirm(`Merge ${ids.length} songs into one? Their videos, Spotify versions and readings are combined. You can undo it under Review → Decision history.`)) return;
  try { const r = await api("/api/merge/apply", { song_ids: ids }); toast(`Merged ${r.merged + 1} songs into one`); CAT.sel.clear(); fetchTracks(); }
  catch (err) { toast(err.message, true); }
};
$("#cat-notsame").onclick = async () => {
  try { await api("/api/merge/reject", { song_ids: [...CAT.sel] }); toast("Marked as different songs; merge rules won't pair them again"); CAT.sel.clear(); fetchTracks(); }
  catch (err) { toast(err.message, true); }
};
$("#cat-language").onchange = (e) => { CAT.language = e.target.value; CAT.offset = 0; fetchTracks(); };
$("#cat-category").onchange = (e) => { CAT.category = e.target.value; CAT.offset = 0; fetchTracks(); };

/* ---------- AI Insights ---------- */
function md(text) {  // small, safe Markdown: escape first, then format
  const inline = (t) => esc(t)
    .replace(/\[([^\]]+)\]\(#song:(\d+)\)/g, (m, title, id) => `<a href="#" data-open="${id}">${title}</a>`)
    .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, (m, title, url) => `<a href="${url}" target="_blank" rel="noopener">${title}</a>`)
    .replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>").replace(/`([^`]+)`/g, "<code>$1</code>");
  const lines = String(text || "").split("\n"); let out = "", list = null, table = [];
  const flushList = () => { if (list) { out += `</${list}>`; list = null; } };
  const flushTable = () => {
    if (!table.length) return;
    const rows = table.filter((r) => !/^\s*\|?\s*:?-{2,}/.test(r)).map((r) => r.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim()));
    out += `<table><thead><tr>${rows[0].map((c) => `<th>${inline(c)}</th>`).join("")}</tr></thead><tbody>${rows.slice(1).map((r) => `<tr>${r.map((c) => `<td>${inline(c)}</td>`).join("")}</tr>`).join("")}</tbody></table>`;
    table = [];
  };
  for (const raw of lines) {
    const l = raw.trimEnd();
    if (/^\s*\|.*\|\s*$/.test(l)) { flushList(); table.push(l); continue; } else flushTable();
    let m;
    if ((m = l.match(/^#{1,4}\s+(.*)/))) { flushList(); out += `<h4>${inline(m[1])}</h4>`; }
    else if ((m = l.match(/^\s*[-*•]\s+(.*)/))) { if (list !== "ul") { flushList(); out += "<ul>"; list = "ul"; } out += `<li>${inline(m[1])}</li>`; }
    else if ((m = l.match(/^\s*\d+[.)]\s+(.*)/))) { if (list !== "ol") { flushList(); out += "<ol>"; list = "ol"; } out += `<li>${inline(m[1])}</li>`; }
    else if (!l.trim()) { flushList(); }
    else { flushList(); out += `<p>${inline(l)}</p>`; }
  }
  flushList(); flushTable();
  return out;
}
const INS = { panel: "emerging", field: "singer" };
const INS_DESC = {
  emerging: "Songs not yet in the top 5% by views, ranked by breakout score: speed vs songs of the same age, growth relative to size, acceleration.",
  cross_label: "Labels and languages compared fairly: median percentile of their songs against songs of the same age (50 = typical), not totals.",
  catalog: "Songs first released a year or more before their current YouTube upload that are drawing above-typical views.",
  artists: "Is an artist's performance broad-based or dependent on one song? Artists with 3+ songs in the window.",
  anomalies: "Songs whose latest growth rate departs sharply from their own previous rate (needs three readings per video).",
};
async function loadInsights() {
  if (!STATUS) await refreshStatus();
  const a = await api("/api/ai/assist").catch(() => ({}));
  $("#ai-briefing").innerHTML = a.briefing ? md(a.briefing) : `<span class="muted">No AI-assist briefing yet.</span>`;
  $("#ai-meta").textContent = a.prepared_on ? `Prepared ${a.prepared_on}` : "";
  $("#ai-foot").textContent = a.prepared_by ? `${a.prepared_by} · data: ${a.data_as_of}. ${a.note}` : "";
  await loadPanel();
}
async function loadPanel() {
  document.querySelectorAll("#ins-tabs .tab").forEach((b) => b.classList.toggle("active", b.dataset.p === INS.panel));
  $("#ins-field").style.display = INS.panel === "artists" ? "" : "none"; $("#ins-field").value = INS.field;
  $("#ins-desc").textContent = INS_DESC[INS.panel];
  $("#ins-table").innerHTML = `<tbody><tr><td class="empty">Computing…</td></tr></tbody>`;
  const d = await api(`/api/insights?${new URLSearchParams({ panel: INS.panel, field: INS.field })}`);
  $("#ins-history").innerHTML = `<b>Data history.</b> ${esc(d.meta.history_note)} <span class="muted">${fmt(d.meta.songs)} songs measured (first uploaded ${esc(d.meta.since)} → ${esc(d.meta.until)}); ${fmt(d.meta.with_recent_rate)} have a recent growth rate, ${fmt(d.meta.with_acceleration)} an acceleration.</span>`;
  const songCell = (r) => `<td><a href="#" data-open="${r.id}"><b>${esc(r.title)}</b></a><div class="t2">${esc([r.label, r.language, r.category].filter(Boolean).join(" · "))}${r.singer ? ` · ${esc(r.singer.slice(0, 50))}` : ""}</div></td>`;
  const ev = (r) => `<td class="ev">${(r.evidence || []).map(esc).join("<br>")}</td>`;
  let html = "";
  if (INS.panel === "emerging") {
    html = `<thead><tr><th>Score</th><th>Song</th><th class="num">Views</th><th class="num">Views/day</th><th>Evidence</th><th class="num">Next 7 days (projection)</th><th>Signal</th></tr></thead><tbody>` +
      d.rows.map((r) => `<tr><td><span class="score">${r.score}</span></td>${songCell(r)}<td class="num">${compact(r.views)}</td><td class="num">${compact(r.velocity)}</td>${ev(r)}
        <td class="num">${compact(r.projection_7d[0])}–${compact(r.projection_7d[1])}</td><td class="muted" style="font-size:12px">${esc(r.signal)}</td></tr>`).join("") + "</tbody>";
  } else if (INS.panel === "cross_label") {
    const grp = (title, rows, keyCols) => `<tr class="label-row"><td colspan="7">${title}</td></tr>` + rows.map((r) => `<tr><td>${keyCols(r)}</td><td class="num">${fmt(r.songs)}</td>
        <td class="num"><b>${r.median_percentile}</b><span class="share" style="width:${Math.round(r.median_percentile * 0.6)}px"></span></td><td class="num">${r.share_above_typical}%</td>
        <td class="num">${r.median_likes_per_1k ?? "–"}</td><td class="num">${compact(r.total_views)}</td><td class="muted">${esc(r.top_song)}</td></tr>`).join("");
    html = `<thead><tr><th>Group</th><th class="num">Songs</th><th class="num">Median percentile (50 = typical)</th><th class="num">Above typical</th><th class="num">Likes / 1k views</th><th class="num">Total views</th><th>Fastest song</th></tr></thead><tbody>` +
      grp("Labels", d.labels, (r) => labelTag(r.label)) + grp("Languages (5+ songs)", d.languages, (r) => esc(r.language)) +
      grp("Label × language (5+ songs)", d.label_language, (r) => `${esc(r.label)} · ${esc(r.language)}`) + "</tbody>";
  } else if (INS.panel === "catalog") {
    html = `<thead><tr><th>Song</th><th>First released</th><th class="num">Views</th><th class="num">Views/day</th><th>Evidence</th></tr></thead><tbody>` +
      d.rows.map((r) => `<tr>${songCell(r)}<td>${esc(r.released || "–")}</td><td class="num">${compact(r.views)}</td><td class="num">${compact(r.velocity)}</td>${ev(r)}</tr>`).join("") + "</tbody>";
  } else if (INS.panel === "artists") {
    html = `<thead><tr><th>Artist</th><th>Profile</th><th class="num">Songs</th><th class="num">Median percentile</th><th class="num">Top song share</th><th class="num">Above typical</th><th class="num">Total views</th><th>Top song</th><th>Labels</th></tr></thead><tbody>` +
      d.rows.map((r) => `<tr><td><a href="#" data-meta-field="${INS.field}" data-meta-key="${esc(r.key)}">${esc(r.name)}</a></td><td><span class="pill">${esc(r.profile)}</span></td><td class="num">${r.songs}</td><td class="num"><b>${r.median_percentile}</b></td>
        <td class="num">${r.top_song_share}%</td><td class="num">${r.share_above_typical}%</td><td class="num">${compact(r.total_views)}</td><td class="muted">${esc(r.top_song)}</td><td class="muted">${esc(r.labels.join(", "))}</td></tr>`).join("") + "</tbody>";
  } else {
    html = `<thead><tr><th>Change</th><th>Song</th><th class="num">Views/day now</th><th>Evidence</th></tr></thead><tbody>` +
      (d.rows.map((r) => `<tr><td><span class="pill ${r.direction === "spike" ? "ok" : "warn"}">${r.direction === "spike" ? "▲ spike" : "▼ drop"} ${r.acceleration}x</span></td>${songCell(r)}<td class="num">${compact(r.recent_rate)}</td>${ev(r)}</tr>`).join("")
        || `<tr><td colspan="4" class="empty">No sharp changes yet. This needs several readings per video; it fills in as daily syncs accumulate.</td></tr>`) + "</tbody>";
  }
  $("#ins-table").innerHTML = html || `<tbody><tr><td class="empty">No data.</td></tr></tbody>`;
  const note = d.ai_assist, box = $("#ins-ai");
  box.style.display = note ? "block" : "none";
  if (note) box.innerHTML = `<div class="row" style="justify-content:space-between"><b>✨ AI assist</b><span class="muted" style="font-size:12px">Prepared ${esc(note.prepared_on)}</span></div>`
    + md(note.text) + `<div class="ai-tools">${esc(note.prepared_by)} · data: ${esc(note.data_as_of)}. ${esc(note.note)}</div>`;
}
document.getElementById("ins-tabs").addEventListener("click", (e) => { const b = e.target.closest("[data-p]"); if (b) { INS.panel = b.dataset.p; loadPanel(); } });
$("#ins-field").onchange = (e) => { INS.field = e.target.value; loadPanel(); };
$("#ins-table").addEventListener("click", (e) => { const o = e.target.closest("[data-open]"); if (o) { e.preventDefault(); openTrack(o.dataset.open); } });
$("#ai-briefing").addEventListener("click", (e) => { const o = e.target.closest("[data-open]"); if (o) { e.preventDefault(); openTrack(o.dataset.open); } });
$("#ins-ai").addEventListener("click", (e) => { const o = e.target.closest("[data-open]"); if (o) { e.preventDefault(); openTrack(o.dataset.open); } });

/* ---------- Explore (metrics per metadata value) ---------- */
const SRC_LABEL = { musicbrainz: "MusicBrainz", apple: "Apple Music", spotify: "Spotify", youtube_credits: "YouTube credits", youtube_title: "YouTube title", youtube_language: "YouTube language",
  youtube_title_script: "title script", youtube_tags: "YouTube tags", channel: "channel", rule: "rule" };
const srcTag = (src) => (src || "").split(",").map((x) => `<span class="src" title="source">${esc(SRC_LABEL[x] || x)}</span>`).join(" ");
const metaChip = (field, m, withSrc) => `<a class="mchip" data-meta-field="${esc(field)}" data-meta-key="${esc(m.key)}" title="Metrics for ${esc(m.value)}">${esc(m.value)}</a>${withSrc ? " " + srcTag(m.source) : ""}`;
const EX = { field: "singer", q: "", label: "", language: "", sort: "views", order: "desc", offset: 0, limit: 50 };
const EXCOLS = [[null, "#"], ["value", "Name"], ["songs", "Songs"], ["new", "New"], ["views", "YouTube views"], [null, "Share"], ["views_delta", "Views Δ day"],
  ["likes", "Likes"], ["comments", "Comments"], ["avg_views", "Avg views / song"], [null, "Top song"]];
async function loadExplore() {
  if (!STATUS) await refreshStatus();
  $("#ex-tabs").innerHTML = STATUS.explore_fields.map((f) => `<button class="tab${f === EX.field ? " active" : ""}" data-f="${f}">${esc(STATUS.meta_fields[f])}</button>`).join("");
  $("#ex-label").innerHTML = `<option value="">All labels</option>` + STATUS.config.youtube.labels.map((l) => `<option>${esc(l.name)}</option>`).join("");
  $("#ex-label").value = EX.label;
  const langs = await api("/api/meta/top?field=language&limit=100");
  $("#ex-language").innerHTML = `<option value="">All languages</option>` + langs.rows.map((r) => `<option>${esc(r.value)}</option>`).join("");
  $("#ex-language").value = EX.language;
  await fetchExplore();
}
async function fetchExplore() {
  const d = await api(`/api/meta/top?${new URLSearchParams({ field: EX.field, q: EX.q, label: EX.label, language: EX.language, sort: EX.sort, order: EX.order, offset: EX.offset, limit: EX.limit })}`);
  $("#ex-window").textContent = `${d.since} → ${d.until}`;
  const maxV = Math.max(1, ...d.rows.map((r) => r.views || 0));
  const head = EXCOLS.map(([k, l]) => `<th class="${k ? "sortable " : ""}${["Songs", "New", "YouTube views", "Views Δ day", "Likes", "Comments", "Avg views / song"].includes(l) ? "num" : ""}" ${k ? `data-sort="${k}"` : ""}>${l}${EX.sort === k ? (EX.order === "asc" ? " ↑" : " ↓") : ""}</th>`).join("");
  $("#ex-table").innerHTML = `<thead><tr>${head}</tr></thead><tbody>` + (d.rows.map((r, i) => `<tr class="click-meta" data-meta-field="${esc(d.field)}" data-meta-key="${esc(r.key)}" style="cursor:pointer">
      <td class="muted">${EX.offset + i + 1}</td><td><b>${esc(r.value)}</b></td><td class="num">${fmt(r.songs)}</td><td class="num">${fmt(r.new)}</td>
      <td class="num">${compact(r.views)}</td><td style="white-space:nowrap" class="muted">${d.total_views ? ((100 * (r.views || 0)) / d.total_views).toFixed(1) : 0}%<span class="share" style="width:${Math.round((60 * (r.views || 0)) / maxV)}px"></span></td>
      ${deltaCell(r.views_delta)}<td class="num">${compact(r.likes)}</td><td class="num">${compact(r.comments)}</td><td class="num">${compact(r.avg_views)}</td>
      <td>${r.top_song ? `<a href="#" data-open="${r.top_song.id}">${esc(r.top_song.title)}</a> <span class="muted">${compact(r.top_song.views)}</span>` : "–"}</td></tr>`).join("")
    || `<tr><td colspan="${EXCOLS.length}" class="empty">No values for this field yet.</td></tr>`) + "</tbody>";
  $("#ex-count").textContent = d.total ? `${fmt(EX.offset + 1)}–${fmt(Math.min(EX.offset + EX.limit, d.total))} of ${fmt(d.total)}` : "0";
  $("#ex-prev").disabled = EX.offset === 0; $("#ex-next").disabled = EX.offset + EX.limit >= d.total;
}
$("#ex-tabs").addEventListener("click", (e) => { const b = e.target.closest("[data-f]"); if (!b) return; EX.field = b.dataset.f; EX.offset = 0; EX.q = ""; $("#ex-q").value = ""; loadExplore(); });
$("#ex-table").addEventListener("click", (e) => {
  const th = e.target.closest("th[data-sort]");
  if (th) { const k = th.dataset.sort; EX.order = EX.sort === k && EX.order === "desc" ? "asc" : "desc"; EX.sort = k; EX.offset = 0; return fetchExplore(); }
  const o = e.target.closest("[data-open]"); if (o) { e.preventDefault(); openTrack(o.dataset.open); }
});
let exTimer;
$("#ex-q").addEventListener("input", (e) => { clearTimeout(exTimer); exTimer = setTimeout(() => { EX.q = e.target.value; EX.offset = 0; fetchExplore(); }, 250); });
$("#ex-label").onchange = (e) => { EX.label = e.target.value; EX.offset = 0; fetchExplore(); };
$("#ex-language").onchange = (e) => { EX.language = e.target.value; EX.offset = 0; fetchExplore(); };
$("#ex-prev").onclick = () => { EX.offset = Math.max(0, EX.offset - EX.limit); fetchExplore(); };
$("#ex-next").onclick = () => { EX.offset += EX.limit; fetchExplore(); };
// Any metadata chip or Explore row opens that value's metrics.
document.addEventListener("click", (e) => {
  const m = e.target.closest("[data-meta-key]");
  if (!m || e.target.closest("[data-open], a[href^='http'], button")) return;
  e.preventDefault(); openMeta(m.dataset.metaField, m.dataset.metaKey);
});
async function openMeta(field, key) {
  const d = await api(`/api/meta/detail?${new URLSearchParams({ field, key })}`);
  const k = d.kpi;
  const tiles = [["Songs", fmt(k.songs), `${fmt(k.new)} new releases`], ["YouTube views", compact(k.yt_views), `${fmt(k.yt_videos)} videos`],
    ["Views Δ day", k.yt_delta ? `+${compact(k.yt_delta)}` : "–", "latest day on day"], ["Likes", compact(k.yt_likes), `${compact(k.yt_comments)} comments`],
    ["Avg views / song", compact(k.songs ? k.yt_views / k.songs : 0), "all versions"]];
  const types = VTYPES.filter(([t]) => k[`yt_views_${t}`]).map(([t, l]) => `${l} ${compact(k[`yt_views_${t}`])}`).join(" · ");
  $("#drawer").innerHTML = `
    <div class="row"><div style="flex:1"><div class="muted" style="font-size:12px;text-transform:uppercase;letter-spacing:.05em">${esc(d.field_label)}</div><h1>${esc(d.value)}</h1>
      <div class="muted">Songs first uploaded ${esc(d.since)} → ${esc(d.until)}</div></div><button class="small" id="dr-close">✕</button></div>
    <div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(130px,1fr));margin:14px 0">${tiles.map(([l, v, x]) => `<div class="card kpi"><div class="label">${l}</div><div class="value">${esc(v)}</div><div class="sub">${esc(x)}</div></div>`).join("")}</div>
    ${types ? `<p class="muted" style="font-size:12.5px;margin-top:0">Views by video type: ${esc(types)}</p>` : ""}
    <h3>YouTube views gained per day (all their songs)</h3>
    <div class="card" style="padding:12px 12px 4px"><div class="chart" id="md-chart"></div><div class="muted" style="font-size:12px;padding:0 4px 8px">${d.series.length < 2 ? "Gains appear once videos have readings on two different days." : ""}</div></div>
    <h3 style="margin-top:18px">Songs (${d.songs.length})</h3>
    <div class="card flush"><table><thead><tr><th>Play</th><th>Song</th><th>First upload</th><th class="num">Views</th></tr></thead><tbody>${d.songs.map((r) => `<tr class="click" data-id="${r.id}">${playBtns(r)}
      <td><div class="t1">${esc(r.title)}</div><div class="t2">${esc(r.yt_channel || "")} · ${statusChip(r.release_status)}</div></td>
      <td class="muted" style="white-space:nowrap">${esc(r.song_date || "")}</td><td class="num">${compact(r.yt_views)}${r.yt_delta ? `<div class="vt">+${compact(r.yt_delta)} today</div>` : ""}</td></tr>`).join("")}</tbody></table></div>
    <h3 style="margin-top:18px">Worked with / appears with</h3>
    <div class="collab">${Object.entries(d.collaborators).map(([f, vals]) => `<h4>${esc(STATUS.meta_fields[f] || f)}</h4>
      <div class="chips">${vals.map((v) => `${metaChip(f, v)} <span class="muted" style="font-size:12px">${v.songs}</span>`).join(" ")}</div>`).join("") || '<span class="muted">–</span>'}</div>`;
  $("#drawer").classList.add("open"); $("#drawer-bg").classList.add("open");
  $("#dr-close").onclick = closeDrawer;
  requestAnimationFrame(() => lineChart($("#md-chart"), d.series.map((p) => ({ x: p.day, y: p.views })), { label: "views" }));
  $("#drawer").insertAdjacentHTML("beforeend", `<p style="margin-top:14px"><a href="#catalogue" id="md-songs">Open these songs on the Songs page →</a></p>`);
  $("#md-songs").onclick = () => { Object.assign(CAT, { meta: `${field}:${key}`, metaName: `${d.field_label}: ${d.value}`, offset: 0, day: "" }); closeDrawer(); };
} (e) => { CAT.since = e.target.value; CAT.offset = 0; fetchTracks(); };
$("#cat-to").onchange = (e) => { CAT.until = e.target.value; CAT.offset = 0; fetchTracks(); };
$("#cat-table").addEventListener("click", (e) => {
  const th = e.target.closest("th[data-sort]");
  if (th) {
    const k = th.dataset.sort;
    CAT.order = CAT.sort === k && CAT.order === "desc" ? "asc" : "desc";
    CAT.sort = k; CAT.offset = 0; fetchTracks();
  }
});
let qTimer;
$("#cat-q").addEventListener("input", (e) => { clearTimeout(qTimer); qTimer = setTimeout(() => { CAT.q = e.target.value; CAT.offset = 0; fetchTracks(); }, 250); });
$("#cat-status").onchange = (e) => { CAT.status = e.target.value; CAT.offset = 0; fetchTracks(); };
$("#cat-label").onchange = (e) => { CAT.label = e.target.value; CAT.channel = ""; CAT.offset = 0; fetchTracks(); };
$("#cat-channel").onchange = (e) => { CAT.channel = e.target.value; CAT.offset = 0; fetchTracks(); };
$("#cat-prev").onclick = () => { CAT.offset = Math.max(0, CAT.offset - CAT.limit); fetchTracks(); };
$("#cat-next").onclick = () => { CAT.offset += CAT.limit; fetchTracks(); };

/* ---------- Track drawer ---------- */
document.addEventListener("click", (e) => {
  const tr = e.target.closest("tr.click[data-id]");
  if (tr && !e.target.closest("a, button, input, label")) openTrack(tr.dataset.id);
});
$("#drawer-bg").onclick = closeDrawer;
document.addEventListener("keydown", (e) => e.key === "Escape" && closeDrawer());
function closeDrawer() { $("#drawer").classList.remove("open", "wide"); $("#drawer-bg").classList.remove("open"); }
async function openTrack(id) {
  // One-glance song profile: who/what/when, every platform's numbers with the last day's gain, cross-platform split,
  // details and credits, performance vs similar songs, trend, uploads, versions. Same layout for every song.
  const [d, cmp] = await Promise.all([api(`/api/track?id=${id}`), api(`/api/song/compare?id=${id}`).catch(() => null)]);
  const t = d.track, r = d.releases[0] || {}, m = d.meta || {}, ch = d.chart;
  const yt = d.items.filter((i) => i.platform === "youtube").sort((a, b) => (b.views || 0) - (a.views || 0));
  const sp = d.items.find((i) => i.platform === "spotify");
  const spAll = d.items.filter((i) => i.platform === "spotify" && ["auto", "approved"].includes(i.review_state));
  const totV = d.by_type.reduce((a, b) => a + b.views, 0);
  const pf = Object.fromEntries(((cmp && cmp.platforms) || []).map((p) => [p.platform, p]));
  const first = (f) => (m[f] && m[f][0] ? m[f][0].value : "");
  const names = (f, n = 3) => (m[f] || []).slice(0, n).map((v) => v.value).join(", ");
  const sign = (v) => v == null ? "" : `${v > 0 ? "+" : ""}${Math.abs(v) >= 1e5 ? compact(v) : fmt(v)}`;
  const big = (v) => v == null ? "–" : Math.abs(v) >= 1e5 ? compact(v) : fmt(v);
  const spotifyOk = t.spotify_id && ["auto", "approved"].includes(t.spotify_state);
  const series = (platform, metric) => {
    const byDay = {};
    d.history.filter((h) => h.platform === platform && h.metric === metric).forEach((h) => {
      byDay[h.capture_date] = platform === "spotify" ? Math.max(byDay[h.capture_date] || 0, h.value) : (byDay[h.capture_date] || 0) + h.value; });
    return Object.keys(byDay).sort().map((x) => ({ x, y: byDay[x] }));
  };
  const METRICS = [["views", "YouTube views", "youtube"], ["likes", "YouTube likes", "youtube"], ["comments", "YouTube comments", "youtube"]];
  if (d.history.some((h) => h.platform === "spotify" && h.metric === "plays")) METRICS.push(["plays", "Spotify plays", "spotify"]);
  if (d.history.some((h) => h.platform === "jiosaavn")) METRICS.push(["jsp", "JioSaavn plays", "jiosaavn", "plays"]);
  if (d.history.some((h) => h.platform === "ytmusic")) METRICS.push(["ytm", "YouTube Music plays", "ytmusic", "plays"]);
  if (d.history.some((h) => h.platform === "deezer")) METRICS.push(["rank", "Deezer rank", "deezer"]);
  if (ch) METRICS.push(["chart_rank", "Spotify India rank", "spotify_chart"]);

  const tile = (label, value, sub, color, extra = "") => `<div class="sp-tile" style="--c:${color}"><div class="sp-l">${label}</div><div class="sp-v">${value}</div><div class="sp-s">${sub || "&nbsp;"}</div>${extra}</div>`;
  const gainLine = (p) => !p || p.latest == null ? '<span class="muted">not found yet</span>'
    : p.gain_1d != null ? `<span class="delta-up">${sign(p.gain_1d)}</span> last day` : '<span class="muted">gain from tomorrow</span>';
  const tiles = [
    tile("YouTube views", big(pf.youtube ? pf.youtube.latest : t.yt_views), gainLine(pf.youtube) + ` · ${fmt(t.yt_videos)} upload${t.yt_videos === 1 ? "" : "s"}`, PCOLOR.youtube),
    tile("Spotify plays", big(pf.spotify && pf.spotify.latest), pf.spotify && pf.spotify.latest != null ? gainLine(pf.spotify) : (spotifyOk ? '<span class="muted">under 1,000 or not read yet</span>' : '<span class="muted">not on Spotify yet</span>'), PCOLOR.spotify),
    tile("JioSaavn plays", big(pf.jiosaavn && pf.jiosaavn.latest), gainLine(pf.jiosaavn), PCOLOR.jiosaavn),
    tile("YouTube Music plays", big(pf.ytmusic && pf.ytmusic.latest), pf.ytmusic && pf.ytmusic.latest != null ? gainLine(pf.ytmusic) : '<span class="muted">no separate audio track</span>', PCOLOR.ytmusic),
    tile("Deezer rank", big(pf.deezer && pf.deezer.latest), pf.deezer && pf.deezer.latest != null ? "popularity score (higher = more popular)" : '<span class="muted">not on Deezer yet</span>', "#a238ff"),
    tile("Spotify India chart", ch ? `#${ch.best_rank}` : "–", ch ? `best · ${fmt(ch.days)} ${ch.chart.endsWith("weekly") ? "weeks" : "days"} in Top 200` : '<span class="muted">not in the India Top 200</span>', "#1db954")];
  const listen = openOnButtons({ youtube: t.yt_video_id && `https://www.youtube.com/watch?v=${t.yt_video_id}`, ytmusic: t.ytm_url,
    spotify: spotifyOk && `https://open.spotify.com/track/${t.spotify_id}`, jiosaavn: t.js_url, apple: t.apple_url, deezer: t.deezer_url, musicbrainz: t.mb_url });
  const facts = [first("film") && `🎬 ${esc(first("film"))}`, first("language") && `🗣 ${esc(first("language"))}`, t.music_label && labelTag(t.music_label),
    first("category") && esc(first("category")), first("occasion") && esc(first("occasion"))].filter(Boolean).join(" · ");
  const row = (f) => m[f] && m[f].length ? `<dt>${esc(STATUS.meta_fields[f] || f)}</dt><dd>${m[f].map((v) => metaChip(f, v)).join(" ")} ${srcTag([...new Set(m[f].flatMap((v) => v.source.split(",")))].join(","))}</dd>` : "";
  const plain = (l, v, src) => v ? `<dt>${l}</dt><dd>${v} ${src ? srcTag(src) : ""}</dd>` : "";
  const released = ((cmp && cmp.platforms) || []).filter((p) => p.released).map((p) => `<span class="pill" style="border-color:${PCOLOR[p.platform] || "var(--border)"}">${esc(p.label)} ${esc(p.released)}</span>`).join(" ");
  const details = ["film", "album", "language", "category", "occasion", "music_label", "genre"].map(row).join("")
    + plain("First on YouTube", esc((t.first_published || "").slice(0, 10)) + (t.yt_channel ? ` · ${esc(t.yt_channel)}` : ""))
    + plain("Original release", t.release_check && t.release_check !== "unknown"
        ? `<b>${esc(t.orig_release || "")}</b> <span class="pill ${t.release_check === "new" ? "ok" : t.release_check === "conflict" ? "warn" : ""}">${
            { new: "✔ new release", old: "older song (re-upload)", conflict: "⚠ sources disagree" }[t.release_check]}</span> <span class="muted" style="font-size:11.5px">${esc((t.orig_release_source || "").replaceAll(",", ", "))}</span>`
        : '<span class="muted">not verified yet</span>')
    + plain("Released on", released)
    + plain("Spotify album", r.title ? `${esc(r.title)} <span class="muted">(${esc(r.album_type || "")})</span>` : "", "spotify")
    + plain("ISRC", t.isrc ? `<span class="mono">${esc(t.isrc)}</span>` : "", r.title ? "spotify" : "musicbrainz")
    + plain("Length", t.duration_ms ? dur(t.duration_ms) + (t.explicit ? " · explicit" : "") : "");
  const credits = ["singer", "music_director", "lyricist", "cast", "director", "producer", "video_director", "arranger", "mix_master", "choreographer", "spotify_artist", "apple_artist"].map(row).join("");

  $("#drawer").classList.add("wide");
  $("#drawer").innerHTML = `
    <div class="sp-head">
      <div style="flex:1;min-width:0">
        <h1 style="margin:0">${esc(t.title)}</h1>
        <div class="muted" style="margin-top:2px">${esc(names("singer") || t.artists || t.yt_channel || "")}${first("music_director") ? ` · music ${esc(names("music_director", 2))}` : ""}</div>
        <div style="margin-top:6px;font-size:13px">${facts || ""} ${statusChip(t.release_status)}</div>
      </div>
      <div class="row" style="gap:6px;align-items:flex-start;flex-shrink:0">
        ${t.yt_video_id ? `<button class="pbtn yt big" data-play="youtube" data-pid="${esc(t.yt_video_id)}" data-ptitle="${esc(t.title)}" title="Play on YouTube">▶</button>` : ""}
        ${spotifyOk ? `<button class="pbtn sp big" data-play="spotify" data-pid="${esc(t.spotify_id)}" data-ptitle="${esc(t.title)}" title="Play on Spotify">▶</button>` : ""}
        <button class="small" id="dr-daily" title="Every day's totals and gains, with graphs">📈 Daily history</button><button class="small" id="dr-close">✕</button>
      </div>
    </div>
    <div style="margin:10px 0 14px">${listen}</div>
    ${sp && sp.review_state === "pending" ? `<div class="card warn-card" style="margin-bottom:14px"><b>Spotify match to check:</b> <a href="${esc(sp.url)}" target="_blank" rel="noopener">${esc(sp.title)}</a> (${esc(sp.published || "")}) · score ${(sp.match_score || 0).toFixed(2)}
      <div class="row" style="margin-top:8px"><button class="small primary" data-decide="approve" data-item="${sp.id}">✓ Same song</button><button class="small" data-decide="reject" data-item="${sp.id}">✕ Different</button></div></div>` : ""}

    <h3 class="sp-sec">At a glance${cmp && cmp.days && cmp.days.length ? ` <span class="muted">· latest ${esc(cmp.days[cmp.days.length - 1])}</span>` : ""}</h3>
    <div class="sp-tiles">${tiles.join("")}</div>

    <h3 class="sp-sec">Across platforms</h3>
    <div class="sp-two">
      <div class="card"><div class="sp-share">${((cmp && cmp.platforms) || []).filter((p) => p.share != null).map((p) =>
        `<div class="sp-share-row"><span>${esc(p.label)}</span><div class="sp-bar"><i style="width:${Math.max(p.share, 1)}%;background:${PCOLOR[p.platform]}"></i></div><b>${p.share}%</b></div>`).join("")
        || '<span class="muted">Shares appear once the song has plays on more than one platform.</span>'}</div>
        <div class="muted" style="font-size:11.5px;margin-top:6px">Share of ${cmp && cmp.total_plays ? compact(cmp.total_plays) : "all"} views and plays (Deezer is a rank, not counted).</div></div>
      <div class="card"><div class="chart" id="dr-cmp" style="min-height:160px"></div><div id="dr-cmp-legend" class="row" style="gap:12px;flex-wrap:wrap;font-size:11.5px"></div></div>
    </div>

    <div class="sp-two">
      <div><h3 class="sp-sec">Song details</h3><div class="card"><dl class="meta-grid" style="margin:0">${details}</dl></div></div>
      <div><h3 class="sp-sec">Credits</h3><div class="card"><dl class="meta-grid" style="margin:0">${credits || "<dd class='muted'>No credits found in the descriptions or on Spotify.</dd>"}</dl>
        <div class="muted" style="font-size:11.5px;margin-top:6px">Click a name to see its metrics across songs.</div></div></div>
    </div>

    <h3 class="sp-sec">Performance</h3><div class="bench" id="dr-bench"><span class="muted">…</span></div>

    <div class="row" style="justify-content:space-between;margin-top:18px"><h3 class="sp-sec" style="margin:0">Trend</h3>
      <div class="seg" id="dr-metric">${METRICS.map(([mm, l], i) => `<button class="small${i ? "" : " on"}" data-m="${mm}">${l}</button>`).join("")}</div></div>
    <div class="card" style="padding:12px 12px 4px;margin-top:8px"><div class="chart" id="dr-chart"></div><div class="muted" id="dr-chart-note" style="font-size:12px;padding:0 4px 8px"></div></div>

    <h3 class="sp-sec">YouTube by upload type</h3>
    <div class="card flush">${d.by_type.length ? `<table><thead><tr><th>Type</th><th class="num">Uploads</th><th class="num">Views</th><th class="num">Share</th><th class="num">Likes</th><th class="num">Comments</th></tr></thead><tbody>
      ${d.by_type.map((b) => `<tr><td>${esc((VTYPES.find(([x]) => x === b.type) || [0, b.type])[1])}</td><td class="num">${fmt(b.videos)}</td><td class="num">${compact(b.views)}</td>
        <td class="num">${totV ? Math.round((100 * b.views) / totV) : 0}%</td><td class="num">${compact(b.likes)}</td><td class="num">${compact(b.comments)}</td></tr>`).join("")}
      <tr class="label-row"><td>All</td><td class="num">${fmt(d.by_type.reduce((a, b) => a + b.videos, 0))}</td><td class="num">${compact(totV)}</td><td class="num">100%</td>
        <td class="num">${compact(d.by_type.reduce((a, b) => a + b.likes, 0))}</td><td class="num">${compact(d.by_type.reduce((a, b) => a + b.comments, 0))}</td></tr></tbody></table>`
      : `<div class="empty">No YouTube upload linked.</div>`}</div>

    <h3 class="sp-sec">YouTube uploads (${yt.length})</h3>
    <div class="card flush"><table><tbody id="dr-uploads"></tbody></table>${yt.length > 10 ? `<div class="pager"><button class="small" id="dr-more">Show all ${yt.length}</button></div>` : ""}</div>
    ${(d.removed || []).length ? `<h3 class="sp-sec dev-only">Removed (${d.removed.length})</h3><div class="card flush dev-only"><table><tbody>${d.removed.map((r) => `<tr class="removed-row"><td><a href="${esc(r.url || "#")}" target="_blank" rel="noopener">${esc(r.title || "(no title)")}</a>
      <div class="t2">${esc(r.platform)}${r.channel ? " · " + esc(r.channel) : ""} · removed ${esc((r.decided_at || "").slice(0, 10))} by ${esc((r.decided_by || "").split(" @ ")[0])}</div></td>
      <td style="width:1%"><button class="small" data-restore="${r.decision_id}">↩ Restore</button></td></tr>`).join("")}</tbody></table></div>` : ""}

    ${spAll.length ? `<h3 class="sp-sec">Spotify versions (${spAll.length})</h3><div class="card flush"><table><tbody>${spAll.map((i) => {
        const md = i.match_detail || {};
        return `<tr><td style="width:34px"><button class="pbtn sp" data-play="spotify" data-pid="${esc(i.platform_id)}" data-ptitle="${esc(i.title)}">▶</button></td>
        <td><a href="${esc(i.url)}" target="_blank" rel="noopener">${esc(i.title)}</a><div class="t2">${md.album ? `${esc(md.album)} (${esc(md.album_type || "")}) · ` : ""}${esc(i.published || "")}${md.isrc ? ` · ISRC ${esc(md.isrc)}` : ""} · ${i.match_method === "same_recording" ? "same recording" : "main match"}</div></td></tr>`;
      }).join("")}</tbody></table></div>` : ""}
    ${d.merged_names.length ? `<p class="muted" style="font-size:12px">Merged from: ${d.merged_names.map((k) => esc(k.split("::").pop().split("|")[0])).join(", ")} (undo under Review → Decision history)</p>` : ""}
    <p class="muted" style="font-size:11.5px;margin-top:14px">Spotify plays are read daily from the public track page (Spotify hides counts under 1,000); a song's Spotify plays are the highest of its copies. Gains compare with the previous day's reading.</p>`;

  const upRow = (i) => `<tr><td style="width:34px"><button class="pbtn yt" data-play="youtube" data-pid="${esc(i.platform_id)}" data-ptitle="${esc(i.title)}">▶</button></td>
    <td><a href="${esc(i.url)}" target="_blank" rel="noopener">${esc(i.title)}</a><div class="t2">${esc(i.channel || "")} · ${esc(i.asset_type || "")} · ${esc(i.published || "")} · ${stateChip(i.review_state, i.match_method)}</div></td>
    <td class="num" style="white-space:nowrap">${compact(i.views)} views<div class="t2">${compact(i.likes)} likes · ${compact(i.comments)} comments</div></td>
    <td style="white-space:nowrap">${i.review_state === "pending" ? `<button class="small" data-decide="approve" data-item="${i.id}">✓</button> ` : ""}<button class="small" data-decide="reject" data-item="${i.id}" title="Remove from this song (asks first, can be restored)">✕</button></td></tr>`;
  const showUploads = (all) => { $("#dr-uploads").innerHTML = (all ? yt : yt.slice(0, 10)).map(upRow).join("") || `<tr><td class="empty">No YouTube upload linked.</td></tr>`; bindDecide(); };
  const bindDecide = () => $("#drawer").querySelectorAll("[data-decide]").forEach((b) => (b.onclick = async () => {
    if (b.dataset.decide === "reject") {
      const item = d.items.find((i) => String(i.id) === b.dataset.item);
      const yes = await confirmBox({ title: "Remove from this song?", ok: "Remove",
        text: `<b>${esc(item ? item.title : "This link")}</b> will be removed from <b>${esc(t.title)}</b> and left out of its totals.<br><br>Nothing is deleted: its data is kept, and you can restore it from “Removed” at the bottom of this song.` });
      if (!yes) return;
    }
    await decide(b.dataset.item, b.dataset.decide); openTrack(id); refreshStatus();
  }));
  $("#drawer").querySelectorAll("[data-restore]").forEach((b) => (b.onclick = async () => {
    try { await api("/api/review/undo", { id: +b.dataset.restore }); toast("Restored to this song"); openTrack(id); refreshStatus(); } catch (e) { toast(e.message, true); }
  }));
  showUploads(false);
  if ($("#dr-more")) $("#dr-more").onclick = (e) => { showUploads(true); e.target.closest(".pager").remove(); };

  $("#drawer").classList.add("open"); $("#drawer-bg").classList.add("open");
  $("#dr-close").onclick = closeDrawer;
  $("#dr-daily").onclick = () => { closeDrawer(); DD.song = String(id); if (location.hash.startsWith("#daily")) loadDaily(); else location.hash = "#daily"; };
  api(`/api/insights/song?id=${id}`).then((b) => {
    const el = $("#dr-bench"); if (!el) return;
    const bm = b.benchmark;
    el.innerHTML = bm ? `<span class="pill">${compact(bm.song.velocity)} YouTube views a day · ${esc(bm.song.age_band)}</span>` + bm.comparisons.map((c) =>
      `<span class="pill ${c.percentile >= 70 ? "ok" : c.percentile < 30 ? "warn" : ""}" title="${fmt(c.songs)} songs">faster than ${c.percentile}% of ${esc(c.compared_with)}</span>`).join("")
      : `<span class="muted">No YouTube views in the current period yet.</span>`;
  }).catch(() => {});
  requestAnimationFrame(() => {
    if (cmp) {
      const lines = cmp.platforms.filter((p) => p.unit !== "rank").map((p) => ({ name: p.label, color: PCOLOR[p.platform], points: p.series.filter((x) => x.gain != null).map((x) => ({ x: x.day, y: x.gain })) })).filter((x) => x.points.length);
      multiLine($("#dr-cmp"), lines, { height: 170 });
      $("#dr-cmp-legend").innerHTML = lines.map((l) => `<span><span style="display:inline-block;width:10px;height:3px;background:${l.color};vertical-align:middle;margin-right:4px"></span>${esc(l.name)} daily gain</span>`).join("");
    }
    draw("views");
  });
  const draw = (mm) => {
    const [, label, platform, metric] = METRICS.find((x) => x[0] === mm);
    const pts = series(platform, metric || mm);
    lineChart($("#dr-chart"), pts, { label });
    $("#dr-chart-note").textContent = pts.length === 1 ? "One reading so far; the line builds up with each day." : "";
    document.querySelectorAll("#dr-metric button").forEach((b) => b.classList.toggle("on", b.dataset.m === mm));
  };
  $("#dr-metric").onclick = (e) => { const b = e.target.closest("button[data-m]"); if (b) draw(b.dataset.m); };
}
const stateChip = (st, method) => st === "song" ? "same song (grouped)" : st === "auto" ? "auto-matched" : st === "approved" ? (method === "manual" ? "linked manually" : "approved") : st === "pending" ? "<b>awaiting review</b>" : esc(st || "");
// Confirmation box for anything that changes data: resolves true on the main button, false on Cancel, Esc or a click outside.
function confirmBox({ title, text, ok = "OK" }) {
  return new Promise((done) => {
    const w = document.createElement("div");
    w.className = "cbox-bg";
    w.innerHTML = `<div class="cbox" role="alertdialog" aria-modal="true"><div class="cbox-icon">!</div><h2>${esc(title)}</h2><p>${text}</p>
      <div class="cbox-btns"><button class="cbox-no">Cancel</button><button class="danger-solid cbox-yes">${esc(ok)}</button></div></div>`;
    const close = (v) => { document.removeEventListener("keydown", key); w.remove(); done(v); };
    const key = (e) => { if (e.key === "Escape") close(false); };
    document.addEventListener("keydown", key);
    w.addEventListener("click", (e) => { if (e.target === w) close(false); });
    w.querySelector(".cbox-no").onclick = () => close(false);
    w.querySelector(".cbox-yes").onclick = () => close(true);
    document.body.appendChild(w);
    w.querySelector(".cbox-no").focus();
  });
}
async function decide(itemId, action) {
  try { await api("/api/matching/decide", { item_id: +itemId, action }); toast(action === "approve" ? "Match approved" : "Removed. You can restore it from this song."); }
  catch (e) { toast(e.message, true); }
}

/* ---------- Runs ---------- */
const CONSOLE = { from: 0, timer: null, runId: null };
function consoleLine(s) {
  const cls = /ERROR|Traceback/.test(s) ? "err" : /⚠|cancel/i.test(s) ? "warn" : "";
  return `<div class="${cls}">${esc(s)}</div>`;
}
function startJobPoll() {
  if (CONSOLE.timer) return;
  CONSOLE.timer = setInterval(pollJob, 1000);
  pollJob();
}
async function pollJob() {
  if (CONSOLE.busy) return;  // overlapping polls would append the same lines twice
  CONSOLE.busy = true;
  try { await pollJobOnce(); } finally { CONSOLE.busy = false; }
}
async function pollJobOnce() {
  const j = await api(`/api/job?from=${CONSOLE.from}`).catch(() => null);
  if (!j || !j.job) return;
  const c = $("#console");
  if (CONSOLE.runId !== j.run_id) {  // a different run: restart the tail from its first line
    const fresh = CONSOLE.from === 0;
    CONSOLE.runId = j.run_id; CONSOLE.from = 0; c.innerHTML = "";
    if (!fresh) return;
  }
  if (j.log.length) {
    const stick = c.scrollTop + c.clientHeight >= c.scrollHeight - 30;
    c.insertAdjacentHTML("beforeend", j.log.map(consoleLine).join(""));
    CONSOLE.from = j.log_total;
    if (stick) c.scrollTop = c.scrollHeight;
  }
  $("#run-console-title").textContent = `run #${j.run_id} · ${j.status}`;
  if (j.running) CONSOLE.sawRunning = j.run_id;
  if (!j.running) {
    clearInterval(CONSOLE.timer); CONSOLE.timer = null;
    const watched = CONSOLE.sawRunning === j.run_id;  // only announce runs we saw in progress
    CONSOLE.sawRunning = null;
    await refreshStatus();
    if (location.hash === "#runs") loadRunsTable();
    if (watched) toast(j.status === "ok" ? "Run finished" : `Run ${j.status.replace("_", " ")}`, j.status !== "ok");
  }
}
async function loadRuns() {
  if (!STATUS) await refreshStatus();
  $("#run-job").innerHTML = Object.entries(STATUS.jobs).map(([k, v]) => `<option value="${k}">${esc(v)}</option>`).join("");
  if (!CONSOLE.runId && STATUS.job.job) { CONSOLE.from = 0; pollJob(); }
  loadRunsTable();
}
async function loadRunsTable() {
  const { runs } = await api("/api/runs");
  const pill = (s) => `<span class="pill ${{ ok: "ok", running: "run", failed: "bad", cancelled: "warn", interrupted: "warn", partial: "warn", quota_exceeded: "warn", paused: "" }[s] || ""}"><span class="dot"></span>${esc(s)}</span>`;
  const secs = (a, b) => (a && b ? `${Math.round((Date.parse(b) - Date.parse(a)) / 1000)}s` : "–");
  $("#runs-table").innerHTML = `<thead><tr><th>#</th><th>Job</th><th>Started</th><th class="num">Took</th><th>Status</th><th class="num">Releases</th><th class="num">Tracks</th><th class="num">API calls</th><th>Details</th></tr></thead><tbody>` +
    (runs.length ? runs.map((r) => `<tr><td class="muted">${r.id}</td><td>${esc(STATUS.jobs[r.job] || r.job)}${r.stats.trigger ? `<div class="t2">${esc(r.stats.trigger)}</div>` : ""}</td><td>${new Date(r.started_at).toLocaleString()}</td>
      <td class="num">${secs(r.started_at, r.finished_at)}</td><td>${pill(r.status)}</td><td class="num">${fmt(r.stats.releases)}</td><td class="num">${fmt(r.stats.tracks)}</td>
      <td class="num">${fmt(r.stats.api_calls)}</td><td>${r.stats.error ? `<span class="delta-down">${esc(r.stats.error.slice(0, 80))}</span>` : ""}${r.stats.stop_reason ? `<div class="t2">${esc(r.stats.stop_reason)}</div>` : ""}
      <a href="#outputs?ds=song_daily">Data</a> · <a href="#outputs?raw_run=${r.id}">Raw API</a>
      <details><summary>stats & log</summary><pre class="stats">${esc(JSON.stringify(r.stats, null, 2))}</pre></details></td></tr>`).join("")
      : `<tr><td colspan="9" class="empty">No runs yet.</td></tr>`) + "</tbody>";
}
$("#run-start").onclick = () => startRun($("#run-since").value, $("#run-until").value);
$("#run-cancel").onclick = async () => { await api("/api/cancel", {}); toast("Cancelling… stops after the current step"); };

/* ---------- Sources ---------- */
function secretPill(el, ok, label) { el.className = "pill " + (ok ? "ok" : "warn"); el.innerHTML = `<span class="dot"></span>${label}`; }
async function loadSources() {
  await refreshStatus();
  loadAiSource();
  loadCoverage();
  renderHealth();
  const s = STATUS.secrets;
  $("#sp-id").value = ""; $("#sp-id").placeholder = s.SPOTIFY_CLIENT_ID.set ? `current: ${s.SPOTIFY_CLIENT_ID.hint}` : "paste client ID";
  $("#sp-id-hint").textContent = s.SPOTIFY_CLIENT_ID.set ? "Leave blank to keep the current ID." : "";
  $("#sp-secret-hint").textContent = s.SPOTIFY_CLIENT_SECRET.set ? "A secret is stored." : "No secret stored yet.";
  $("#sp-id-2").value = ""; $("#sp-id-2").placeholder = s.SPOTIFY_CLIENT_ID_2.set ? `current: ${s.SPOTIFY_CLIENT_ID_2.hint}` : "paste account 2 client ID";
  $("#sp-id-2-hint").textContent = s.SPOTIFY_CLIENT_ID_2.set ? "Leave blank to keep the current account 2 ID." : "";
  $("#sp-secret-2-hint").textContent = s.SPOTIFY_CLIENT_SECRET_2.set ? "Account 2 secret is stored." : "No backup secret stored.";
  const spotifyConfigured = (s.SPOTIFY_CLIENT_ID.set && s.SPOTIFY_CLIENT_SECRET.set) || (s.SPOTIFY_CLIENT_ID_2.set && s.SPOTIFY_CLIENT_SECRET_2.set);
  secretPill($("#sp-pill"), spotifyConfigured, spotifyConfigured ? "Configured" : "Needs credentials");
  const nk = s.YOUTUBE_API_KEY.count || 0;
  $("#yt-hint").textContent = nk ? `${nk} key${nk > 1 ? "s" : ""} stored, used in order: when one runs out of quota the next takes over. Keys pasted here are added to the list.` : "Not set";
  const ys = await api("/api/youtube/status");
  const lr = ys.last_run;
  secretPill($("#yt-pill"), s.YOUTUBE_API_KEY.set && lr && lr.status !== "failed",
    !s.YOUTUBE_API_KEY.set ? "Not configured" : !lr ? "Key stored · not synced yet" : lr.status === "failed" ? "Last sync failed" : "Syncing");
  $("#yt-status").innerHTML = `<dl class="kv" style="margin:6px 0 0">
      <dt>Channels</dt><dd>${ys.channels.length} (<a href="#settings">manage</a>)</dd>
      <dt>Last sync</dt><dd>${lr ? `${new Date(lr.started_at).toLocaleString()} · ${esc(lr.status)}` : "never"}</dd>
      <dt>Videos collected</dt><dd>${fmt(lr?.stats?.videos)}</dd>
      <dt>Quota today</dt><dd>${fmt(ys.units_today)} of ${fmt(ys.units_limit)} units (${fmt(ys.keys.length)} × 10,000)</dd></dl>
    ${ys.keys.length ? `<table style="margin-top:8px"><thead><tr><th>#</th><th>Key</th><th>Units today</th><th>Status</th></tr></thead><tbody>
      ${ys.keys.map((k, i) => `<tr><td class="muted">${i + 1}</td><td><code>${esc(k.hint)}</code></td><td>${fmt(k.units_today)}</td>
        <td>${k.available ? "Available" : `Set aside (${esc(k.reason)}) until ${new Date(k.until).toLocaleString()}`}</td></tr>`).join("")}
      </tbody></table>` : ""}`;
}
$("#sp-save").onclick = async () => {
  try {
    await api("/api/secrets", { SPOTIFY_CLIENT_ID: $("#sp-id").value, SPOTIFY_CLIENT_SECRET: $("#sp-secret").value,
      SPOTIFY_CLIENT_ID_2: $("#sp-id-2").value, SPOTIFY_CLIENT_SECRET_2: $("#sp-secret-2").value });
    $("#sp-secret").value = ""; $("#sp-secret-2").value = ""; toast("Spotify credentials saved"); loadSources();
  } catch (e) { toast(e.message, true); }
};
$("#sp-test").onclick = async () => {
  const b = $("#sp-test"); b.disabled = true; $("#sp-test-msg").textContent = "Checking Spotify…";
  try { await api("/api/test/spotify", {}); } catch (e) { toast(e.message, true); }
  b.disabled = false; $("#sp-test-msg").textContent = "";
  renderHealth();
};
const ago = (iso) => {
  const s = Math.round((Date.now() - Date.parse(iso)) / 1000);
  return s < 60 ? `${s}s ago` : s < 3600 ? `${Math.round(s / 60)} min ago` : s < 86400 ? `${Math.round(s / 3600)} h ago` : new Date(iso).toLocaleString();
};
const codeChip = (c) => `<span class="status-code ${c >= 200 && c < 300 ? "ok" : "err"}">${c ?? "–"}</span>`;
async function renderHealth() {
  const h = await api("/api/health").catch(() => null);
  if (!h) return;
  const last = h.spotify.last, box = $("#sp-health");
  if (!last) { box.innerHTML = `<div class="muted">No API status check yet.</div>`; return; }
  const until = last.retry_at && Date.parse(last.retry_at) > Date.now() ? new Date(last.retry_at).toLocaleString() : null;
  const watch = last.ok ? "Re-checked every 6 h." : until ? `Waiting for the quota to reset at ${until}; discovery resumes automatically then.` :
    `Auto re-check every ${h.config.recheck_minutes} min${h.config.auto_run_on_recovery ? "; discovery starts automatically when access returns" : ""}.`;
  box.innerHTML = `
    <h3>API status</h3>
    <div class="health-now ${last.ok ? "good" : "bad"}"><span class="icon">${last.ok ? "✓" : "✕"}</span>
      <div><div>${esc(last.message)}</div><div class="muted" style="font-size:12px;margin-top:4px">
        ${codeChip(last.status)} checked ${ago(last.checked_at)} · ${esc(last.trigger)} · ${watch}</div></div></div>
    <details style="margin-top:10px"><summary>Check history (${h.spotify.history.length})</summary>
      <table style="margin-top:6px"><thead><tr><th>When</th><th>By</th><th>HTTP</th><th>Result</th></tr></thead><tbody>
      ${h.spotify.history.map((c) => `<tr><td class="muted">${new Date(c.checked_at).toLocaleString()}</td><td>${esc(c.trigger)}</td>
        <td>${codeChip(c.status)}</td><td>${c.ok ? "OK" : esc(c.message)}</td></tr>`).join("")}</tbody></table></details>`;
}
const GAPS = [
  ["YouTube", ["Watch time, impressions, click-through rate, audience retention", "Revenue, monetised playbacks, audience age / country", "Dislikes (hidden by YouTube)", "Past daily views: only today's totals, so trends start the day collection starts"],
    "Channel owners' YouTube Analytics; run the pipeline daily without gaps"],
  ["Spotify", ["Play / stream counts (any API mode)", "Popularity, label, artist followers and genres (hidden in development mode)", "More than ~600 searches and ~90 album lookups a day (development mode)"],
    "Spotify Charts CSV (top 200 streams); labels' Spotify for Artists or distributor reports; extended quota application"],
  ["Apple Music (iTunes)", ["ISRC", "Popularity or play counts"], "MusicBrainz / Spotify for ISRC"],
  ["MusicBrainz", ["Most new Indian releases (volunteer data lags)", "Any popularity data"], "Retried weekly as the community adds releases"],
  ["Deezer", ["Search from India (Deezer isn't available in India)", "Stream counts (rank is an undocumented score)"], "Lookup by ISRC only"],
  ["All sources", ["Real Spotify / Apple / JioSaavn play counts", "Reasons behind a spike (public data shows what changed, not why)"], "Label-owned reports; analyst review"],
];
async function loadCoverage() {
  const d = await api("/api/coverage").catch(() => null); if (!d) return;
  $("#cov-window").textContent = `${fmt(d.songs)} songs first uploaded ${d.since} → ${d.until} · ${d.days_of_readings} day(s) of daily readings`;
  $("#cov-table").innerHTML = `<thead><tr><th>Data</th><th>Source</th><th class="num">Songs</th><th>Coverage</th></tr></thead><tbody>` +
    d.rows.map((r) => `<tr><td>${esc(r.item)}</td><td class="muted">${esc(r.source)}</td><td class="num">${fmt(r.songs)}</td>
      <td style="white-space:nowrap"><span class="num">${r.pct}%</span><span class="share" style="width:${Math.round(r.pct * 0.8)}px;${r.item.includes("pending") || r.item.startsWith("Not on") ? "background:var(--warning)" : ""}"></span></td></tr>`).join("") + "</tbody>";
  const v = d.videos;
  $("#cov-videos").textContent = `${fmt(v.total)} linked YouTube videos: ${fmt(v.no_likes)} with likes hidden, ${fmt(v.no_comments)} with comments off or hidden, ${fmt(v.no_lang)} without an audio language set by the label.`;
  $("#gap-list").innerHTML = `<table><thead><tr><th>Source</th><th>Not provided</th><th>Workaround</th></tr></thead><tbody>` +
    GAPS.map(([src, items, fix]) => `<tr><td><b>${esc(src)}</b></td><td>${items.map((i) => `• ${esc(i)}`).join("<br>")}</td><td class="muted">${esc(fix)}</td></tr>`).join("") + "</tbody></table>";
}
function loadAiSource() { secretPill($("#ai-pill"), false, "Planned"); }
$("#yt-sync").onclick = () => startRun(undefined, undefined, "youtube_sync");
$("#yt-save").onclick = async () => {
  try { await api("/api/secrets", { YOUTUBE_API_KEY: $("#yt-key").value }); $("#yt-key").value = ""; toast("YouTube key(s) added"); loadSources(); }
  catch (e) { toast(e.message, true); }
};

/* ---------- Settings ---------- */
let SEEDS = [];
async function loadSettings() {
  await refreshStatus();
  const d = STATUS.config.discovery, s = STATUS.config.schedule;
  $("#set-since").value = d.since; $("#set-until").value = d.until; $("#set-market").value = d.market;
  $("#set-queries").value = d.label_queries.join("\n"); $("#set-regex").value = d.label_regex;
  $("#set-track").checked = d.use_track_search; $("#set-seeds").checked = d.use_artist_seeds; $("#set-raw").checked = d.store_raw; $("#set-refresh").checked = d.refresh_existing;
  $("#set-budget").value = d.max_calls_per_run; $("#set-isrc").value = d.isrc_lookups_per_run;
  $("#set-sched").checked = s.enabled; $("#set-time").value = s.time;
  const y = STATUS.config.youtube;
  $("#set-worker-on").checked = (STATUS.config.worker || {}).enabled !== false;
  $("#set-yt-on").checked = y.enabled; $("#set-yt-every").value = y.every_hours; $("#set-yt-fresh").value = y.fresh_days; $("#set-yt-back").value = y.lookback_days;
  renderLabels(y);
  renderMergeRules(STATUS.config.merge);
  $("#set-recheck").value = STATUS.config.health.recheck_minutes; $("#set-autorun").checked = STATUS.config.health.auto_run_on_recovery;
  $("#set-next").textContent = STATUS.next_run ? `Next run: ${new Date(STATUS.next_run).toLocaleString()}` : "Scheduler is off.";
  SEEDS = (await api("/api/seeds")).seeds; renderSeeds();
}
$("#set-save").onclick = async () => {
  try {
    await api("/api/config", {
      discovery: { since: $("#set-since").value, until: $("#set-until").value, market: $("#set-market").value.toUpperCase(),
        label_queries: $("#set-queries").value.split("\n"), label_regex: $("#set-regex").value,
        use_track_search: $("#set-track").checked, use_artist_seeds: $("#set-seeds").checked, store_raw: $("#set-raw").checked, refresh_existing: $("#set-refresh").checked,
        max_calls_per_run: +$("#set-budget").value || 900, isrc_lookups_per_run: +$("#set-isrc").value || 0 },
      schedule: { enabled: $("#set-sched").checked, time: $("#set-time").value || "06:00" },
      worker: { enabled: $("#set-worker-on").checked },
      youtube: { enabled: $("#set-yt-on").checked, every_hours: +$("#set-yt-every").value || 6, fresh_days: +$("#set-yt-fresh").value || 0,
        lookback_days: +$("#set-yt-back").value || 0 },
      health: { recheck_minutes: +$("#set-recheck").value || 15, auto_run_on_recovery: $("#set-autorun").checked },
      merge: readMergeRules(),
    });
    toast("Settings saved"); loadSettings();
  } catch (e) { toast(e.message, true); }
};
function renderSeeds() {
  $("#seed-list").innerHTML = SEEDS.length ? SEEDS.map((s, i) => `<span class="chip">${esc(s.note || s.id)}<button data-i="${i}" title="Remove">✕</button></span>`).join("")
    : `<span class="muted">No seed artists yet.</span>`;
}
$("#seed-list").addEventListener("click", async (e) => {
  const b = e.target.closest("button[data-i]"); if (!b) return;
  SEEDS.splice(+b.dataset.i, 1);
  SEEDS = (await api("/api/seeds", { seeds: SEEDS })).seeds; renderSeeds();
});
async function searchSeeds() {
  const box = $("#seed-results");
  box.innerHTML = `<div class="muted" style="padding:8px 0">Searching…</div>`;
  try {
    const { artists } = await api(`/api/artist_search?q=${encodeURIComponent($("#seed-q").value)}`);
    box.innerHTML = `<div class="seed-results">` + (artists.map((a) => `<div>${a.image ? `<img src="${esc(a.image)}" alt="">` : "<img alt=''>"}
      <span style="flex:1">${esc(a.name)} <span class="muted">${a.followers != null ? fmt(a.followers) + " followers" : ""}</span></span>
      <button class="small" data-id="${esc(a.id)}" data-name="${esc(a.name)}" ${SEEDS.some((s) => s.id === a.id) ? "disabled" : ""}>+ Add</button></div>`).join("")
      || `<div class="muted">No artists found.</div>`) + `</div>`;
  } catch (e) { box.innerHTML = `<div class="delta-down" style="padding:8px 0">${esc(e.message)}</div>`; }
}
$("#seed-search").onclick = searchSeeds;
$("#seed-q").addEventListener("keydown", (e) => e.key === "Enter" && searchSeeds());
$("#seed-results").addEventListener("click", async (e) => {
  const b = e.target.closest("button[data-id]"); if (!b) return;
  SEEDS.push({ id: b.dataset.id, note: b.dataset.name });
  SEEDS = (await api("/api/seeds", { seeds: SEEDS })).seeds; renderSeeds(); b.disabled = true;
});

/* ---------- Merge rules (Settings) ---------- */
const RULE_PARAMS = { min_similarity: ["Min. name similarity (0.5–1)", 0.01], max_days_apart: ["Max. days apart", 1], max_length_diff_s: ["Max. length difference (s)", 1], min_words: ["Min. words in the shorter name", 1] };
function renderMergeRules(m) {
  $("#set-merge-across").checked = !!m.across_labels;
  $("#merge-rules").innerHTML = Object.entries(m.rules).map(([id, r]) => `<div class="rule-row" data-rule="${id}">
      <div class="row"><label class="check" style="margin:0"><input type="checkbox" class="mr-on" ${r.enabled ? "checked" : ""}><b>${esc(RULE_NAME[id] || id)}</b></label>
        <span class="spacer" style="flex:1"></span>
        <select class="mr-action"><option value="suggest" ${r.action === "suggest" ? "selected" : ""}>Suggest</option><option value="auto" ${r.action === "auto" ? "selected" : ""}>Merge automatically</option></select></div>
      <div class="row" style="margin-top:8px">${Object.entries(RULE_PARAMS).filter(([k]) => k in r).map(([k, [l, step]]) =>
        `<label class="field"><span>${l}</span><input type="number" step="${step}" data-p="${k}" value="${r[k]}"></label>`).join("")}</div></div>`).join("");
}
function readMergeRules() {
  const rules = {};
  document.querySelectorAll("#merge-rules .rule-row").forEach((row) => {
    const r = { enabled: row.querySelector(".mr-on").checked, action: row.querySelector(".mr-action").value };
    row.querySelectorAll("input[data-p]").forEach((i) => (r[i.dataset.p] = +i.value));
    rules[row.dataset.rule] = r;
  });
  return { across_labels: $("#set-merge-across").checked, rules };
}

/* ---------- Labels and YouTube channels (Settings) ---------- */
function renderLabels(y) {
  $("#yt-labels").innerHTML = y.labels.map((l) => {
    const chans = y.channels.filter((c) => c.label === l.name);
    return `<div class="label-block ${l.enabled ? "" : "off"}" data-label="${esc(l.name)}">
      <div class="row"><span class="label-name">${esc(l.name)}</span><span class="muted">${chans.length} channel${chans.length === 1 ? "" : "s"}</span>
        <span class="spacer" style="flex:1"></span>
        <label class="check"><input type="checkbox" class="lb-toggle" ${l.enabled ? "checked" : ""}>Tracked</label>
        <button class="small lb-remove" title="Remove this label and its channels">Remove</button></div>
      <div class="chips">${chans.map((c) => `<span class="chip" title="${esc(c.id)}">${esc(c.title)}<button data-ch="${esc(c.id)}" title="Remove channel">✕</button></span>`).join("")
        || `<span class="muted">No channels yet.</span>`}</div>
      <div class="row"><input class="lb-ch-q" placeholder="@handle, channel URL or UC… id"><button class="small lb-ch-add">Add channel</button></div>
    </div>`;
  }).join("") || `<span class="muted">No labels.</span>`;
}
const labelOf = (el) => el.closest(".label-block").dataset.label;
const afterLabelChange = (r, msg) => { STATUS.config = r.config; renderLabels(r.config.youtube); refreshStatus(); toast(msg); };
$("#yt-labels").addEventListener("click", async (e) => {
  try {
    const ch = e.target.closest("button[data-ch]");
    if (ch) return afterLabelChange(await api("/api/youtube/channel", { remove: ch.dataset.ch }), "Channel removed");
    if (e.target.closest(".lb-remove")) {
      const name = labelOf(e.target);
      if (!confirm(`Remove the label "${name}" and its channels? Songs and readings already collected are kept.`)) return;
      return afterLabelChange(await api("/api/youtube/label", { action: "remove", name }), `Removed ${name}`);
    }
    if (e.target.closest(".lb-ch-add")) {
      const box = e.target.closest(".label-block"), q = box.querySelector(".lb-ch-q").value;
      const r = await api("/api/youtube/channel", { channel: q, label: box.dataset.label });
      afterLabelChange(r, `Added ${r.added.title} (${compact(+r.added.subscribers)} subscribers) to ${box.dataset.label}`);
    }
  } catch (err) { toast(err.message, true); }
});
$("#yt-labels").addEventListener("change", async (e) => {
  if (!e.target.classList.contains("lb-toggle")) return;
  const name = labelOf(e.target), on = e.target.checked;
  try { afterLabelChange(await api("/api/youtube/label", { action: "toggle", name, enabled: on }), `${name}: ${on ? "tracked" : "paused"}`); }
  catch (err) { toast(err.message, true); e.target.checked = !on; }
});
$("#yt-label-add").onclick = async () => {
  const name = $("#yt-label-q").value;
  try { afterLabelChange(await api("/api/youtube/label", { action: "add", name }), `Added label ${name.trim()}; now add its channels`); $("#yt-label-q").value = ""; }
  catch (err) { toast(err.message, true); }
};

/* ---------- Review ---------- */
const REV = { state: "spotify_pending", q: "", offset: 0, limit: 50, picked: false };
async function loadHistory() {
  const { decisions } = await api("/api/review/decisions");
  document.querySelectorAll("#rev-tabs .tab").forEach((b) => b.classList.toggle("active", b.dataset.state === "history"));
  $("#rev-table").innerHTML = `<thead><tr><th>#</th><th>When</th><th>Who</th><th>Decision</th><th>Song ↔ match</th><th></th></tr></thead><tbody>` +
    (decisions.map((d) => `<tr${d.undone_at ? ' style="opacity:.55"' : ""}><td class="muted">${d.id}</td>
      <td style="white-space:nowrap">${new Date(d.decided_at).toLocaleString()}</td><td class="mono">${esc(d.decided_by || "")}</td>
      <td>${{ approve: "✓ approved", reject: "✕ rejected / unlinked", link: "🔗 linked", merge: "⇢ merged songs" }[d.action] || esc(d.action)}</td>
      <td>${esc(d.summary || "")}</td>
      <td style="white-space:nowrap">${d.undone_at ? `<span class="muted">undone ${new Date(d.undone_at).toLocaleString()}<br>by ${esc(d.undone_by || "")}</span>`
        : `<button class="small" data-undo="${d.id}">↶ Undo</button>`}</td></tr>`).join("")
      || `<tr><td colspan="6" class="empty">No review decisions yet. Every approve / reject / link is recorded here and can be undone.</td></tr>`) + "</tbody>";
  $("#rev-count").textContent = `${decisions.length} decisions`;
  $("#rev-prev").disabled = $("#rev-next").disabled = true;
}

/* Duplicate songs (merge rules) */
const RULE_NAME = { same_isrc: "Same ISRC", spotify_same: "Same Spotify title + artist", similar_name: "Spelling variant", name_contains: "Name contains the other" };
let DUP = [];
const evText = (e) => [e.isrc && `ISRC ${e.isrc}`, e.name_similarity != null && `name ${Math.round(e.name_similarity * 100)}% alike`,
  e.contained && `"${e.contained}" in both`, e.days_apart != null && `${e.days_apart} days apart`, e.length_diff_s != null && `length ±${e.length_diff_s}s`].filter(Boolean).join(" · ");
async function loadDuplicates() {
  const d = await api(`/api/merge/suggestions?${new URLSearchParams({ q: REV.q, offset: REV.offset, limit: REV.limit })}`);
  DUP = d.groups;
  const box = $("#dup-rules");
  box.style.display = "grid"; $("#dup-bulk").style.display = "flex"; $("#rev-kpis").style.display = "none";
  box.innerHTML = d.rules.map((r) => `<div class="card kpi"><div class="label">${esc(RULE_NAME[r.id] || r.id)} · ${r.enabled ? (r.action === "auto" ? "merges automatically" : "suggests") : "off"}</div>
      <div class="value">${fmt(r.groups)}</div><div class="sub">${esc(r.description)}</div>
      <div class="row" style="margin-top:8px"><button class="small primary" data-merge-rule="${r.id}" ${r.groups ? "" : "disabled"}>Merge all ${fmt(r.groups)} (${fmt(r.songs)} songs)</button>
      <a href="#settings" style="font-size:12px">edit rule</a></div></div>`).join("");
  document.querySelector('#rev-tabs .tab[data-state="duplicates"] span').textContent = fmt(d.total);
  $("#rev-table").innerHTML = `<thead><tr><th class="sel"><input type="checkbox" id="dup-all"></th><th>Rule</th><th>Songs (the first one is kept)</th><th>Evidence</th><th></th></tr></thead><tbody>` +
    (DUP.map((g, i) => `<tr><td class="sel"><input type="checkbox" data-dup="${i}"></td><td><span class="pill">${esc(RULE_NAME[g.rule] || g.rule)}</span></td>
      <td>${g.songs.map((x) => `<div class="dup-song"><a href="#" data-open="${x.id}">${esc(x.yt_title || x.title)}</a>${x.id === g.keep ? '<span class="keep-tag">keeps</span>' : ""}
        <div class="t2">${esc(x.channel || "")} · ${esc((x.first_published || "").slice(0, 10))} · ${fmt(x.videos)} videos · ${compact(x.views)} views${x.spotify_id ? " · on Spotify" : ""}${x.isrc ? ` · ${esc(x.isrc)}` : ""}${x.duration_ms ? ` · ${dur(x.duration_ms)}` : ""}</div></div>`).join("")}</td>
      <td class="muted" style="font-size:12.5px">${g.evidence.map(evText).map(esc).join("<br>")}</td>
      <td style="white-space:nowrap"><button class="small primary" data-dup-merge="${i}">⇢ Merge</button> <button class="small" data-dup-reject="${i}">Not the same</button></td></tr>`).join("")
      || `<tr><td colspan="5" class="empty">No duplicate songs found by the enabled rules.</td></tr>`) + "</tbody>";
  $("#rev-count").textContent = d.total ? `${fmt(REV.offset + 1)}–${fmt(Math.min(REV.offset + REV.limit, d.total))} of ${fmt(d.total)} groups` : "0";
  $("#rev-prev").disabled = REV.offset === 0; $("#rev-next").disabled = REV.offset + REV.limit >= d.total;
}
const dupGroup = (g) => ({ keep: g.keep, drop: g.songs.map((x) => x.id).filter((id) => id !== g.keep) });
async function dupAction(fn, msg) { try { const r = await fn(); toast(typeof msg === "function" ? msg(r) : msg); loadDuplicates(); } catch (err) { toast(err.message, true); } }
$("#rev-table").addEventListener("click", (e) => {
  if (REV.state !== "duplicates") return;
  const o = e.target.closest("[data-open]"); if (o) { e.preventDefault(); openTrack(o.dataset.open); return; }
  const m = e.target.closest("[data-dup-merge]"), r = e.target.closest("[data-dup-reject]");
  if (m) dupAction(() => api("/api/merge/apply", { groups: [dupGroup(DUP[+m.dataset.dupMerge])] }), "Merged");
  if (r) dupAction(() => api("/api/merge/reject", { song_ids: DUP[+r.dataset.dupReject].songs.map((x) => x.id) }), "Marked as different songs");
});
$("#rev-table").addEventListener("change", (e) => {
  if (e.target.id === "dup-all") document.querySelectorAll("#rev-table input[data-dup]").forEach((c) => (c.checked = e.target.checked));
});
const dupSelected = () => [...document.querySelectorAll("#rev-table input[data-dup]:checked")].map((c) => DUP[+c.dataset.dup]);
$("#dup-merge-sel").onclick = () => { const g = dupSelected(); if (g.length) dupAction(() => api("/api/merge/apply", { groups: g.map(dupGroup) }), (r) => `Merged ${fmt(r.merged)} songs`); };
$("#dup-reject-sel").onclick = async () => {
  const g = dupSelected(); if (!g.length) return;
  for (const x of g) await api("/api/merge/reject", { song_ids: x.songs.map((s) => s.id) });
  toast(`${g.length} groups marked as different`); loadDuplicates();
};
$("#dup-rules").addEventListener("click", (e) => {
  const b = e.target.closest("[data-merge-rule]"); if (!b) return;
  const rule = b.dataset.mergeRule;
  if (!confirm(`Merge every group suggested by "${RULE_NAME[rule] || rule}"? This is one decision you can undo under Decision history.`)) return;
  dupAction(() => api("/api/merge/apply", { rule }), (r) => `Merged ${fmt(r.merged)} songs in ${fmt(r.groups)} groups`);
});

async function loadReview() {
  if (REV.state !== "duplicates") { $("#dup-rules").style.display = "none"; $("#dup-bulk").style.display = "none"; $("#rev-kpis").style.display = ""; }
  if (REV.state === "history") return loadHistory();
  if (REV.state === "duplicates") {
    document.querySelectorAll("#rev-tabs .tab").forEach((b) => b.classList.toggle("active", b.dataset.state === "duplicates"));
    return loadDuplicates();
  }
  const d = await api(`/api/matching?${new URLSearchParams({ state: REV.state, q: REV.q, offset: REV.offset, limit: REV.limit })}`);
  const s = d.summary, ov = (await api("/api/overview")).kpi;
  // Nothing on the default Spotify tab but videos waiting: open the tab the yellow badge counts.
  if (!REV.picked && REV.state === "spotify_pending" && !s.spotify_pending && s.pending) { REV.state = "pending"; return loadReview(); }
  const linked = ov.new + ov.catalogue, decided = linked + ov.spotify_pending + ov.not_on_spotify;
  const share = linked + ov.spotify_pending ? Math.round((100 * ov.spotify_pending) / (linked + ov.spotify_pending)) : 0;
  $("#rev-kpis").innerHTML = [["Songs matched on Spotify", `${decided ? Math.round((100 * linked) / decided) : 0}%`, `${fmt(linked)} of ${fmt(decided)} songs · target ≥ 90%`],
    ["Spotify matches needing review", `${share}%`, `${fmt(ov.spotify_pending)} of ${fmt(linked + ov.spotify_pending)} · target ≤ 10%`],
    ["Videos grouped into songs", fmt(s.grouped), "same song across lyric / video / audio uploads"], ["Approved / linked by you", fmt(s.approved), "kept across runs"]]
    .map(([l, v, x]) => `<div class="card kpi"><div class="label">${l}</div><div class="value">${v}</div><div class="sub">${x}</div></div>`).join("");
  document.querySelectorAll("#rev-tabs .tab").forEach((b) => {
    b.classList.toggle("active", b.dataset.state === REV.state);
    const n = { spotify_pending: s.spotify_pending, spotify_linked: s.spotify_linked, pending: s.pending, auto: s.auto, approved: s.approved, unmatched_tracks: s.unmatched_tracks, unmatched_videos: s.unmatched_videos }[b.dataset.state];
    b.querySelector("span").textContent = fmt(n);
  });
  const chips = (det) => det ? ["title", "artist", "film", "duration", "date"].map((k) => `<span class="score-chip ${det[k] >= 0.7 ? "hi" : det[k] > 0 ? "mid" : "lo"}">${k} ${(+det[k]).toFixed(k === "title" ? 2 : 1)}</span>`).join("") : "";
  let html;
  if (REV.state === "spotify_pending" || REV.state === "spotify_linked") {
    const linked = REV.state === "spotify_linked";
    html = `<thead><tr><th>Song on YouTube</th><th>Spotify candidate</th><th>Evidence</th><th></th></tr></thead><tbody>` + (d.rows.map((r) => `
      <tr><td><div class="t1">${esc(r.yt_title)} <button class="pbtn yt" data-play="youtube" data-pid="${esc((r.video_url || "").split("v=")[1] || "")}" data-ptitle="${esc(r.yt_title)}">▶</button></div><a href="${esc(r.video_url)}" target="_blank" rel="noopener">${esc(r.video_title)}</a>
        <div class="t2">first upload ${esc((r.first_published || "").slice(0, 10))}</div></td>
      <td><a href="${esc(r.url)}" target="_blank" rel="noopener">${esc(r.spotify_title)}</a> <button class="pbtn sp" data-play="spotify" data-pid="${esc(r.spotify_id)}" data-ptitle="${esc(r.spotify_title)}">▶</button><div class="t2">${esc(r.spotify_artists)}</div>
        <div class="t2">${esc(r.spotify_album)} · ${esc(r.spotify_release)} · ${dur(r.spotify_ms)} · ISRC ${esc(r.isrc || "–")}</div></td>
      <td><b>${(r.match_score || 0).toFixed(2)}</b> ${chips(r.match_detail)}</td>
      <td style="white-space:nowrap">${linked ? `<button class="small" data-decide="reject" data-item="${r.item_id}" title="Wrong song: remove this Spotify link">✕ Unlink</button>`
        : `<button class="small primary" data-decide="approve" data-item="${r.item_id}">✓ Same song</button> <button class="small" data-decide="reject" data-item="${r.item_id}">✕ Different</button>`}</td></tr>`).join("")
      || `<tr><td colspan="4" class="empty">${linked ? "No Spotify links yet." : "No Spotify matches waiting."}</td></tr>`) + "</tbody>";
  } else if (REV.state === "unmatched_tracks") {
    html = `<thead><tr><th>Track</th><th>Release</th><th>Released</th><th>Length</th><th></th></tr></thead><tbody>` + (d.rows.map((r) => `
      <tr><td><div class="t1">${esc(r.title)}</div><div class="t2">${esc(r.artists)}</div></td><td>${esc(r.release_title)}<div class="t2">${esc(r.album_type)}</div></td>
      <td class="muted">${esc(r.release_date)}</td><td class="muted">${dur(r.duration_ms)}</td>
      <td style="white-space:nowrap"><button class="small" data-cands="${r.id}">Find video</button></td></tr>
      <tr class="cand-row" id="cands-${r.id}" style="display:none"><td colspan="5"></td></tr>`).join("") || `<tr><td colspan="5" class="empty">Every track has a YouTube video.</td></tr>`) + "</tbody>";
  } else {
    const actions = (r) => REV.state === "pending" ? `<button class="small primary" data-decide="approve" data-item="${r.item_id}">✓ Same song</button> <button class="small" data-decide="reject" data-item="${r.item_id}">✕ Different</button>`
      : REV.state === "unmatched_videos" ? "" : `<button class="small" data-decide="reject" data-item="${r.item_id}">✕ Unlink</button>`;
    html = `<thead><tr><th>Spotify track</th><th>YouTube video</th><th class="num">Views</th><th>Evidence</th><th></th></tr></thead><tbody>` + (d.rows.map((r) => `
      <tr><td>${r.track_id ? `<div class="t1">${esc(r.track_title)}</div><div class="t2">${esc(r.artists)}</div><div class="t2">${esc(r.release_title)} · ${esc(r.release_date)} · ${dur(r.track_ms)}</div>` : '<span class="muted">–</span>'}</td>
      <td><a href="${esc(r.url)}" target="_blank" rel="noopener">${esc(r.video_title)}</a><div class="t2">${esc(r.channel)} · ${esc(r.asset_type)} · ${esc(r.published)} · ${dur(r.video_ms)}</div></td>
      <td class="num">${compact(r.views)}</td>
      <td>${r.match_score != null ? `<b>${r.match_score.toFixed(2)}</b> ` : ""}${chips(r.match_detail)}</td>
      <td style="white-space:nowrap">${actions(r)}</td></tr>`).join("") || `<tr><td colspan="5" class="empty">Nothing here.</td></tr>`) + "</tbody>";
  }
  $("#rev-table").innerHTML = html;
  $("#rev-count").textContent = d.total ? `${fmt(REV.offset + 1)}–${fmt(Math.min(REV.offset + REV.limit, d.total))} of ${fmt(d.total)}` : "0";
  $("#rev-prev").disabled = REV.offset === 0; $("#rev-next").disabled = REV.offset + REV.limit >= d.total;
}
document.querySelectorAll("#rev-tabs .tab").forEach((b) => (b.onclick = () => { REV.state = b.dataset.state; REV.offset = 0; REV.picked = true; loadReview(); }));
let revTimer;
$("#rev-q").addEventListener("input", (e) => { clearTimeout(revTimer); revTimer = setTimeout(() => { REV.q = e.target.value; REV.offset = 0; loadReview(); }, 250); });
$("#rev-prev").onclick = () => { REV.offset = Math.max(0, REV.offset - REV.limit); loadReview(); };
$("#rev-next").onclick = () => { REV.offset += REV.limit; loadReview(); };
$("#rev-table").addEventListener("click", async (e) => {
  const un = e.target.closest("[data-undo]");
  if (un) {
    try { await api("/api/review/undo", { id: +un.dataset.undo }); toast(`Decision #${un.dataset.undo} undone`); }
    catch (err) { toast(err.message, true); }
    loadHistory(); refreshStatus(); return;
  }
  const dec = e.target.closest("[data-decide]");
  if (dec) { await decide(dec.dataset.item, dec.dataset.decide); loadReview(); refreshStatus(); return; }
  const cb = e.target.closest("[data-cands]");
  if (cb) {
    const row = $(`#cands-${cb.dataset.cands}`), cell = row.firstElementChild;
    if (row.style.display !== "none") { row.style.display = "none"; return; }
    row.style.display = ""; cell.innerHTML = '<span class="muted">Scoring collected videos…</span>';
    const { candidates } = await api(`/api/matching/candidates?track_id=${cb.dataset.cands}`);
    cell.innerHTML = `<div class="cands">${candidates.map((c) => `<div class="cand"><div style="flex:1"><a href="${esc(c.url)}" target="_blank" rel="noopener">${esc(c.title)}</a>
        <div class="t2">${esc(c.channel)} · ${esc(c.asset_type)} · ${esc(c.published)} · ${compact(c.views)} views · score ${c.score.toFixed(2)}${c.detail.variant ? ` · variant: ${esc(c.detail.variant.join(", "))}` : ""}${c.linked_elsewhere ? " · <b>linked to another track</b>" : ""}</div></div>
        <button class="small" data-link="${esc(c.video_id)}" data-track="${cb.dataset.cands}">Link</button></div>`).join("") || '<span class="muted">No similar titles among collected videos.</span>'}
      <div class="row" style="margin-top:8px"><input placeholder="…or paste a YouTube URL / video id" data-linkinput="${cb.dataset.cands}"><button class="small" data-linkbtn="${cb.dataset.cands}">Link</button></div></div>`;
    return;
  }
  const lb = e.target.closest("[data-link],[data-linkbtn]");
  if (lb) {
    const track = lb.dataset.track || lb.dataset.linkbtn;
    const video = lb.dataset.link || $(`[data-linkinput="${track}"]`).value;
    try { await api("/api/matching/link", { track_id: +track, video }); toast("Linked"); loadReview(); refreshStatus(); }
    catch (err) { toast(err.message, true); }
  }
});

/* ---------- Outputs ---------- */
// navigator.clipboard only exists on HTTPS/localhost; the studio is often served over plain HTTP.
function copyText(text) {
  if (navigator.clipboard && window.isSecureContext) { navigator.clipboard.writeText(text).then(() => toast("Copied")); return; }
  const ta = document.createElement("textarea");
  ta.value = text; ta.style.position = "fixed"; ta.style.opacity = "0";
  document.body.appendChild(ta); ta.select();
  try { document.execCommand("copy"); toast("Copied"); } catch { toast("Copy failed", true); }
  ta.remove();
}
const OUT = { ds: null, day: "", offset: 0, limit: 100, q: "", total: 0, sets: [] };
const RAW = { run: "", result: "", q: "", offset: 0, limit: 50, total: 0 };
const kb = (n) => (n == null ? "–" : n < 1024 ? `${n} B` : n < 1048576 ? `${(n / 1024).toFixed(1)} KB` : `${(n / 1048576).toFixed(1)} MB`);
function showTab(t) {
  document.querySelectorAll(".tab").forEach((b) => b.classList.toggle("active", b.dataset.tab === t));
  $("#out-files").style.display = t === "files" ? "grid" : "none";
  $("#out-raw").style.display = t === "raw" ? "block" : "none";
  if (t === "raw") loadRaw(); else loadFiles();
}
document.querySelectorAll(".tab[data-tab]").forEach((b) => (b.onclick = () => showTab(b.dataset.tab)));  // Outputs tabs only
async function loadOutputs() {
  if (!STATUS) await refreshStatus();
  const p = hashParams();
  if (p.get("ds")) { OUT.ds = p.get("ds"); OUT.offset = 0; }
  if (p.get("raw_run")) { RAW.run = p.get("raw_run"); RAW.offset = 0; showTab("raw"); return; }
  showTab(p.get("tab") === "raw" ? "raw" : "files");
}
async function loadFiles() {
  const d = await api("/api/datasets");
  OUT.sets = d.datasets;
  if (!OUT.ds || !d.datasets.some((x) => x.key === OUT.ds)) OUT.ds = d.datasets[0]?.key;
  $("#out-list").innerHTML = d.datasets.map((x) => `<div class="file-item ${x.key === OUT.ds ? "active" : ""}" data-ds="${esc(x.key)}">
      <div class="name">${esc(x.name)}</div><div class="meta">${esc(x.what)}</div></div>`).join("");
  $("#out-day").innerHTML = d.days.map((x) => `<option value="${x}">${x}</option>`).join("");
  if (OUT.day) $("#out-day").value = OUT.day;
  $("#out-arch").innerHTML = d.archived.length ? d.archived.map((f) => `<a class="file-item" style="display:block;text-decoration:none;color:inherit" href="/api/datasets/archived?name=${encodeURIComponent(f.name)}">
      <div class="name">${esc(f.name.replace(/\.gz$/, ""))}</div><div class="meta">${kb(f.size)} compressed · ↓ download</div></a>`).join("")
    : `<div class="empty">None.</div>`;
  if (OUT.ds) previewFile();
}
$("#out-list").addEventListener("click", (e) => {
  const it = e.target.closest(".file-item"); if (!it) return;
  OUT.ds = it.dataset.ds; OUT.offset = 0;
  document.querySelectorAll("#out-list .file-item").forEach((x) => x.classList.toggle("active", x === it));
  previewFile();
});
$("#out-day").onchange = (e) => { OUT.day = e.target.value; OUT.offset = 0; previewFile(); };
async function previewFile() {
  const set = OUT.sets.find((x) => x.key === OUT.ds);
  $("#out-day").style.display = set?.by_day ? "" : "none";
  const params = { ds: OUT.ds, q: OUT.q, ...(set?.by_day && OUT.day ? { day: OUT.day } : {}) };
  let d;
  $("#out-title").textContent = set ? set.name : OUT.ds;
  $("#out-table").innerHTML = `<tbody><tr><td class="empty">Loading…</td></tr></tbody>`;
  try { d = await api(`/api/datasets/view?${new URLSearchParams({ ...params, offset: OUT.offset, limit: OUT.limit })}`); }
  catch (e) { $("#out-table").innerHTML = `<tbody><tr><td class="empty">${esc(e.message)}</td></tr></tbody>`; return; }
  OUT.total = d.total;
  if (d.day) { OUT.day = d.day; $("#out-day").value = d.day; }
  $("#out-title").textContent = d.name + (d.day ? ` · ${d.day}` : "");
  const dl = $("#out-dl"); dl.style.display = ""; dl.href = `/api/datasets/download?${new URLSearchParams({ ...params, ...(d.day ? { day: d.day } : {}) })}`;
  dl.textContent = OUT.q ? "↓ Download CSV (filtered)" : "↓ Download CSV";
  const urlCol = d.header.indexOf("url");
  $("#out-table").innerHTML = `<thead><tr><th class="num">#</th>${d.header.map((h) => `<th>${esc(h)}</th>`).join("")}</tr></thead><tbody>` +
    (d.rows.length ? d.rows.map((r, i) => `<tr><td class="num muted">${OUT.offset + i + 1}</td>${r.map((c, j) => j === urlCol && c
      ? `<td><a href="${esc(c)}" target="_blank" rel="noopener">open ↗</a></td>` : `<td style="white-space:nowrap">${esc(c ?? "")}</td>`).join("")}</tr>`).join("")
      : `<tr><td colspan="${d.header.length + 1}" class="empty">No rows.</td></tr>`) + "</tbody>";
  $("#out-count").textContent = d.total ? `${fmt(OUT.offset + 1)}–${fmt(Math.min(OUT.offset + OUT.limit, d.total))} of ${fmt(d.total)} rows` : "0 rows";
  $("#out-prev").disabled = OUT.offset === 0; $("#out-next").disabled = OUT.offset + OUT.limit >= d.total;
}
let oTimer;
$("#out-q").addEventListener("input", (e) => { clearTimeout(oTimer); oTimer = setTimeout(() => { OUT.q = e.target.value; OUT.offset = 0; previewFile(); }, 250); });
$("#out-prev").onclick = () => { OUT.offset = Math.max(0, OUT.offset - OUT.limit); previewFile(); };
$("#out-next").onclick = () => { OUT.offset += OUT.limit; previewFile(); };

async function loadRaw() {
  const d = await api(`/api/raw?${new URLSearchParams({ run_id: RAW.run, result: RAW.result, q: RAW.q, offset: RAW.offset, limit: RAW.limit })}`);
  RAW.total = d.total;
  $("#raw-run").innerHTML = `<option value="">All runs</option>` + d.runs.map((r) => `<option value="${r.run_id ?? ""}">${r.run_id ? `Run #${r.run_id}` : "No run"} · ${fmt(r.n)} calls${r.errors ? ` · ${fmt(r.errors)} errors` : ""}</option>`).join("");
  $("#raw-run").value = RAW.run; $("#raw-result").value = RAW.result; $("#raw-q").value = RAW.q;
  const short = (u) => { try { const x = new URL(u); return decodeURIComponent(x.pathname.replace("/v1", "") + x.search); } catch { return u; } };
  $("#raw-table").innerHTML = `<thead><tr><th>#</th><th>Run</th><th>Captured</th><th>HTTP</th><th>Request</th><th class="num">Size</th></tr></thead><tbody>` +
    (d.rows.length ? d.rows.map((r) => `<tr class="click-raw" data-id="${r.id}" style="cursor:pointer"><td class="muted">${r.id}</td><td>${r.run_id ? `#${r.run_id}` : "–"}</td>
      <td class="muted" style="white-space:nowrap">${new Date(r.captured_at).toLocaleString()}</td><td>${codeChip(r.status)}</td>
      <td class="mono" style="word-break:break-all">${esc(short(r.url))}</td><td class="num muted">${kb(r.size)}</td></tr>`).join("")
      : `<tr><td colspan="6" class="empty">No API responses stored${RAW.run || RAW.result || RAW.q ? " for this filter" : " yet"}.</td></tr>`) + "</tbody>";
  $("#raw-count").textContent = d.total ? `${fmt(RAW.offset + 1)}–${fmt(Math.min(RAW.offset + RAW.limit, d.total))} of ${fmt(d.total)} responses` : "0 responses";
  $("#raw-prev").disabled = RAW.offset === 0; $("#raw-next").disabled = RAW.offset + RAW.limit >= d.total;
}
$("#raw-run").onchange = (e) => { RAW.run = e.target.value; RAW.offset = 0; loadRaw(); };
$("#raw-result").onchange = (e) => { RAW.result = e.target.value; RAW.offset = 0; loadRaw(); };
let rTimer;
$("#raw-q").addEventListener("input", (e) => { clearTimeout(rTimer); rTimer = setTimeout(() => { RAW.q = e.target.value; RAW.offset = 0; loadRaw(); }, 300); });
$("#raw-prev").onclick = () => { RAW.offset = Math.max(0, RAW.offset - RAW.limit); loadRaw(); };
$("#raw-next").onclick = () => { RAW.offset += RAW.limit; loadRaw(); };
$("#raw-table").addEventListener("click", async (e) => {
  const tr = e.target.closest("tr.click-raw"); if (!tr) return;
  const r = await api(`/api/raw/item?id=${tr.dataset.id}`);
  let body = r.body || "";
  try { body = JSON.stringify(JSON.parse(body), null, 2); } catch {}
  $("#drawer").innerHTML = `
    <div class="row"><div style="flex:1"><h1>API response #${r.id}</h1><div class="muted">${r.run_id ? `Run #${r.run_id} · ` : ""}${new Date(r.captured_at).toLocaleString()}</div></div>
      <button class="small" id="dr-copy">Copy</button><button class="small" id="dr-close">✕</button></div>
    <dl class="kv"><dt>HTTP status</dt><dd>${codeChip(r.status)}</dd><dt>Request</dt><dd class="mono">GET ${esc(r.url)}</dd>
      <dt>Size</dt><dd>${kb((r.body || "").length)}</dd></dl>
    <pre class="json">${esc(body)}</pre>`;
  $("#drawer").classList.add("open"); $("#drawer-bg").classList.add("open");
  $("#dr-close").onclick = closeDrawer;
  $("#dr-copy").onclick = () => copyText(body);
});

$("#theme-toggle").onclick = () => {
  const t = document.documentElement.getAttribute("data-theme") === "light" ? "dark" : "light";
  document.documentElement.setAttribute("data-theme", t);
  try { localStorage.setItem("ml-theme", t); } catch (e) { /* private window: theme just isn't remembered */ }
};
$("#client-view").onclick = async () => {
  CLIENT_VIEW = !CLIENT_VIEW;
  await refreshStatus();
  if (CLIENT_VIEW) location.hash = "#dashboard"; else route();
};

/* ---------- Search: every detail of a song, from the top bar ---------- */
const GS = { timer: null, seq: 0, results: [], sel: 0 };
const gsPanel = $("#gsearch-panel"), gsInput = $("#gsearch");
function gsRender(d) {
  GS.results = d.results; GS.sel = 0;
  if (!d.q) { gsPanel.hidden = true; return; }
  const chips = Object.entries(d.by_field || {}).slice(0, 6).map(([f, n]) => `<span class="pill">${esc(f)} ${fmt(n)}</span>`).join("");
  gsPanel.innerHTML = !d.results.length
    ? `<div class="gs-empty">No songs match “${esc(d.q)}”.${FOCUS_ALL ? "" : " Older songs are hidden; tick “Include older songs” to search them too."}</div>`
    : `<div class="gs-head"><b>${fmt(d.total)}</b> song${d.total === 1 ? "" : "s"} · ${chips}</div>` +
      d.results.map((r, i) => `<div class="gs-row${i ? "" : " sel"}" data-i="${i}">
        ${r.yt_video_id ? `<button class="pbtn yt" data-play="youtube" data-pid="${esc(r.yt_video_id)}" data-ptitle="${esc(r.title)}" title="Play">▶</button>` : `<span class="pbtn yt off">▶</span>`}
        <div class="gs-main"><div class="t1">${esc(r.title)}${labelTag(r.music_label)}</div>
          <div class="t2"><span class="gs-why">${esc(r.matched_on)}: ${esc(r.matched)}</span> · ${esc(r.first_upload || "")} · ${statusChip(r.release_status)}</div></div>
        <div class="gs-nums">${r.yt_views != null ? `<span title="YouTube views"><i style="background:var(--c-youtube)"></i>${compact(r.yt_views)}</span>` : ""}${r.sp_plays != null ? `<span title="Spotify plays"><i style="background:var(--c-spotify)"></i>${compact(r.sp_plays)}</span>` : ""}${r.js_plays != null ? `<span title="JioSaavn plays"><i style="background:var(--c-jiosaavn)"></i>${compact(r.js_plays)}</span>` : ""}</div>
      </div>`).join("") + (d.total > d.results.length ? `<div class="gs-foot">Showing the top ${d.results.length}; add a word to narrow it down.</div>` : "");
  gsPanel.hidden = false;
}
function gsOpen(i) { const r = GS.results[i]; if (!r) return; gsPanel.hidden = true; openTrack(r.id); }
gsInput.addEventListener("input", () => {
  clearTimeout(GS.timer);
  const qv = gsInput.value.trim();
  if (qv.length < 2) { gsPanel.hidden = true; return; }
  GS.timer = setTimeout(async () => {
    const seq = ++GS.seq;
    try { const d = await api(`/api/search?q=${encodeURIComponent(qv)}`); if (seq === GS.seq) gsRender(d); } catch (e) { toast(e.message, true); }
  }, 220);
});
gsInput.addEventListener("keydown", (e) => {
  const rows = gsPanel.querySelectorAll(".gs-row");
  if (e.key === "Escape") { gsPanel.hidden = true; gsInput.blur(); return; }
  if (e.key === "Enter") { e.preventDefault(); gsOpen(GS.sel); return; }
  if ((e.key === "ArrowDown" || e.key === "ArrowUp") && rows.length) {
    e.preventDefault(); GS.sel = (GS.sel + (e.key === "ArrowDown" ? 1 : -1) + rows.length) % rows.length;
    rows.forEach((r, i) => r.classList.toggle("sel", i === GS.sel)); rows[GS.sel].scrollIntoView({ block: "nearest" });
  }
});
gsInput.addEventListener("focus", () => { if (GS.results.length && gsInput.value.trim().length >= 2) gsPanel.hidden = false; });
gsPanel.addEventListener("click", (e) => { if (e.target.closest("[data-play]")) return; const row = e.target.closest(".gs-row"); if (row) gsOpen(+row.dataset.i); });
document.addEventListener("click", (e) => { if (!e.target.closest("#gsearch-box")) gsPanel.hidden = true; });
document.addEventListener("keydown", (e) => { if (e.key === "/" && !/^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName)) { e.preventDefault(); gsInput.focus(); gsInput.select(); } });
document.addEventListener("change", (e) => { if (e.target.id === "focus-older" && gsInput.value.trim().length >= 2) gsInput.dispatchEvent(new Event("input")); });

/* ---------- Boot ---------- */
refreshStatus().then(route);
setInterval(refreshStatus, 5000);
// Redraw the current page's charts when the width really changes (rotation, window resize); phones fire resize on scroll.
let rz, lastW = innerWidth;
window.addEventListener("resize", () => { if (innerWidth === lastW) return; lastW = innerWidth; clearTimeout(rz); rz = setTimeout(route, 250); });
// Slide-in menu on tablets and phones
const closeMenu = () => document.body.classList.remove("menu-open");
$("#menu-btn").onclick = () => document.body.classList.toggle("menu-open");
$("#nav-bg").onclick = closeMenu;
document.querySelectorAll("a.nav").forEach((a) => a.addEventListener("click", closeMenu));

/* ---------- Alerts (Developer): bell, panel, desktop notifications, the chart-token form ---------- */
const AL = { data: null, seen: new Set(JSON.parse((() => { try { return localStorage.getItem("ml-alerts-seen") || "[]"; } catch { return "[]"; } })())) };
const SEV = { critical: "Critical", warning: "Needs attention", info: "Info" };
const alertAction = (a) => a.action === "charts_token" ? `<button class="small primary" data-al-open="charts">Paste session token</button>`
  : a.action && a.action.startsWith("open:") ? `<button class="small" data-al-open="${esc(a.action.slice(5))}">Open</button>` : "";
const alertRow = (a) => `<div class="al al-${a.severity}${a.snoozed ? " al-snoozed" : ""}"><span class="dot"></span><div class="al-body">
    <div class="t1">${esc(a.title)}</div><div class="t2">${esc(a.detail || "")}</div>
    <div class="al-meta">${SEV[a.severity] || ""} · since ${ago(a.first_seen)}${a.snoozed ? ` · snoozed until ${new Date(a.snoozed_until).toLocaleString()}` : ""}</div>
    <div class="al-btns">${alertAction(a)}${a.snoozed ? `<button class="small" data-al-snooze="${esc(a.key)}" data-h="0">Unsnooze</button>` : `<button class="small" data-al-snooze="${esc(a.key)}" data-h="24">Snooze 24 h</button>`}</div></div></div>`;
function renderAlerts() {
  const d = AL.data; if (!d) return;
  const n = d.count;
  $("#bell-n").hidden = !n; $("#bell-n").textContent = n > 9 ? "9+" : n;
  $("#bell").classList.toggle("has-critical", d.active.some((a) => a.severity === "critical" && !a.snoozed));
  const charts = d.charts.running ? `<div class="al-note">Importing Spotify chart data…</div>` : d.charts.message ? `<div class="al-note">Last chart import: ${esc(d.charts.message)}</div>` : "";
  const html = (d.active.length ? d.active.map(alertRow).join("") : `<div class="empty" style="padding:14px">All clear: every source is reading normally.</div>`) + charts +
    (d.resolved.length ? `<div class="al-res"><div class="muted" style="font-size:11.5px;margin:10px 0 4px;text-transform:uppercase;letter-spacing:.05em">Closed recently</div>${d.resolved.map((r) =>
      `<div class="t2">✓ ${esc(r.title)} <span class="muted">· ${ago(r.resolved_at)}</span></div>`).join("")}</div>` : "");
  $("#alerts-list").innerHTML = html;
  if ($("#dev-alerts")) { $("#dev-alerts-card").hidden = !d.active.length; $("#dev-alerts").innerHTML = d.active.map(alertRow).join("") + charts; }
  const perm = "Notification" in window ? Notification.permission : "unsupported";
  $("#notif-btn").textContent = perm === "granted" ? "🔔 Desktop alerts on" : perm === "denied" ? "Desktop alerts blocked" : perm === "unsupported" ? "Desktop alerts: not available" : "Enable desktop alerts";
  $("#notif-btn").disabled = perm !== "default";
}
async function loadAlerts() {
  if (!STATUS || STATUS.role !== "developer") return;
  try { AL.data = await api("/api/alerts"); } catch { return; }
  // A desktop notification for each alert that opened since this browser last looked (critical and warning only)
  const fresh = AL.data.active.filter((a) => !a.snoozed && a.severity !== "info" && !AL.seen.has(a.key + "|" + a.first_seen));
  if (fresh.length && "Notification" in window && Notification.permission === "granted" && document.visibilityState !== "visible")
    fresh.forEach((a) => { try { new Notification(`Market Lens: ${a.title}`, { body: a.detail || "", tag: a.key }); } catch {} });
  fresh.forEach((a) => AL.seen.add(a.key + "|" + a.first_seen));
  try { localStorage.setItem("ml-alerts-seen", JSON.stringify([...AL.seen].slice(-200))); } catch {}
  renderAlerts();
}
$("#bell").onclick = (e) => { e.stopPropagation(); const p = $("#alerts-panel"); p.hidden = !p.hidden; if (!p.hidden) loadAlerts(); };
document.addEventListener("click", (e) => { if (!e.target.closest(".bell-wrap") && !e.target.closest(".cbox-bg")) $("#alerts-panel").hidden = true; });
$("#notif-btn").onclick = async () => { if ("Notification" in window) { await Notification.requestPermission(); renderAlerts(); } };
document.addEventListener("click", async (e) => {
  const sn = e.target.closest("[data-al-snooze]");
  if (sn) { AL.data = await api("/api/alerts/snooze", { key: sn.dataset.alSnooze, hours: +sn.dataset.h }); renderAlerts(); return; }
  const op = e.target.closest("[data-al-open]");
  if (op) { $("#alerts-panel").hidden = true; location.hash = "#" + op.dataset.alOpen; return; }
});
/* Spotify chart page: status and the token paste box (the token goes to the server only) */
async function loadCharts() {
  const c = await api("/api/charts");
  const imp = c.last_import || {};
  $("#ch-status").innerHTML = kv([
    ["Newest daily chart", c.daily_latest ? `${esc(c.daily_latest)} ${c.behind ? '<span class="pill bad"><span class="dot"></span>behind</span>' : '<span class="pill ok"><span class="dot"></span>up to date</span>'}` : "none yet"],
    ["Daily charts stored", fmt(c.daily_count)], ["Newest weekly chart", esc(c.weekly_latest || "–")], ["Weekly charts stored", fmt(c.weekly_count)],
    ["Our songs on the newest chart", fmt(c.our_songs_on_latest)],
    ["Token last saved", c.token_saved_at ? ago(c.token_saved_at) : "never"],
    ["Last import", c.running ? "running…" : imp.message ? `${imp.ok ? "✓" : "⚠"} ${esc(imp.message)} · ${ago(imp.at)}` : "–"]]) +
    `<p class="muted" style="font-size:12px;margin:12px 0 0">Spotify publishes the daily chart a day or two late; the reminder (bell) appears when the newest chart is more than 2 days old.</p>`;
  return c;
}
$("#ch-save").onclick = async () => {
  const msg = $("#ch-msg"), box = $("#ch-token");
  if (!box.value.trim()) { msg.textContent = "Paste the token first."; return; }
  $("#ch-save").disabled = true;
  try {
    await api("/api/charts/token", { token: box.value });
    box.value = "";
    msg.textContent = "Saved. Importing the missing chart days…";
    const t = setInterval(async () => {
      const c = await loadCharts();
      if (!c.running) { clearInterval(t); $("#ch-save").disabled = false; const i = c.last_import || {}; msg.textContent = (i.ok ? "✓ " : "⚠ ") + (i.message || "done"); loadAlerts(); }
    }, 3000);
  } catch (e) { msg.textContent = e.message || String(e); $("#ch-save").disabled = false; }
};
setInterval(loadAlerts, 60000);
setTimeout(loadAlerts, 1500);

// Menu: narrow icon bar or full width (wide screens); each link keeps its name as a tooltip for the icon bar
document.querySelectorAll("nav.side a.nav").forEach((a) => { if (!a.title) a.title = a.textContent.trim(); });
const sideLabel = () => { const mini = document.documentElement.classList.contains("side-mini");
  $("#side-toggle span").textContent = mini ? "Keep menu open" : "Collapse menu"; $("#side-toggle").title = mini ? "Keep the full menu open" : "Collapse the menu to icons"; };
$("#side-toggle").onclick = () => {
  const mini = document.documentElement.classList.toggle("side-mini");
  try { localStorage.setItem("ml-side", mini ? "mini" : "full"); } catch {}
  sideLabel();
};
sideLabel();

$("#year").textContent = new Date().getFullYear();

/* ---------- Collection schedule: choose how often, see the daily cost before saving ---------- */
const SCH = { saved: null, pick: {} };
const schLabel = (h) => h >= 24 ? "Once a day" : `Every ${h} hours`;
const VERDICT = { comfortable: "ok", fine: "ok", tight: "warn", "over the limit": "bad" };
function meter(title, used, limit, sub, verdict) {
  const p = limit ? Math.min(100, (100 * used) / limit) : 0;
  return `<div class="sch-meter"><div class="sch-mt">${title}<span class="pill ${VERDICT[verdict] || ""}"><span class="dot"></span>${esc(verdict)}</span></div>
    <div class="sch-bar"><span style="width:${p}%"></span></div><div class="sch-ms">${sub}</div></div>`;
}
async function loadSchedule(preview) {
  const q = new URLSearchParams(Object.fromEntries(Object.entries(SCH.pick).map(([k, v]) => [k, v])));
  const d = await api("/api/collection" + (preview ? "?" + q : ""));
  if (!preview) { SCH.saved = d.settings; SCH.pick = { ...d.settings }; }
  document.querySelectorAll("#page-schedule .seg").forEach((el) => {
    const k = el.dataset.setting;
    el.innerHTML = d.choices[k].map((h) => `<button class="${SCH.pick[k] === h ? "on" : ""}" data-h="${h}">${schLabel(h)}${SCH.saved[k] === h ? '<small>current</small>' : ""}</button>`).join("");
  });
  const y = d.youtube, t = d.time, changed = Object.keys(SCH.pick).some((k) => SCH.pick[k] !== SCH.saved[k]);
  $("#sch-meters").innerHTML =
    meter("YouTube quota for readings", y.reads_units, y.units_limit, `<b>${fmt(y.reads_units)}</b> of ${fmt(y.units_limit)} units a day (${y.keys} keys) · ${Math.round(100 * y.share)}%. With YouTube Music searches (up to ${fmt(y.search_units)}): ${Math.round(100 * y.share_with_search)}%. Used in the last 24 h: ${fmt(y.measured_24h)}.`, y.verdict) +
    meter("API lane time", t.api_lane_hours, 24, `<b>${t.api_lane_hours} h</b> of reading a day (JioSaavn, Shorts, Deezer), beside the songs job and lookups`, t.api_verdict) +
    meter("Browser lane time", t.browser_lane_hours, 24, `<b>${t.browser_lane_hours} h</b> a day of Spotify and Instagram pages`, t.browser_verdict);
  $("#sch-table").innerHTML = `<thead><tr><th>Source</th><th>How often</th><th class="num">Items</th><th class="num">Reads a day</th><th class="num">Daily cost</th></tr></thead><tbody>` +
    d.sources.map((x) => `<tr><td><div class="t1">${esc(x.name)}</div>${x.detail ? `<div class="t2">${esc(x.detail)}</div>` : ""}</td><td>${esc(x.rule)}</td>
      <td class="num">${fmt(x.items)}</td><td class="num">${fmt(x.reads)}</td><td class="num">${esc(x.cost)}</td></tr>`).join("") + "</tbody>";
  $("#sch-save").disabled = !changed; $("#sch-reset").hidden = !changed;
  $("#sch-state").textContent = changed ? "Preview of the new schedule (not saved yet)" : "Current schedule";
}
$("#page-schedule").addEventListener("click", (e) => {
  const b = e.target.closest(".seg button[data-h]");
  if (!b) return;
  SCH.pick[b.closest(".seg").dataset.setting] = +b.dataset.h;
  loadSchedule(true);
});
$("#sch-reset").onclick = () => { SCH.pick = { ...SCH.saved }; loadSchedule(true); };
$("#sch-save").onclick = async () => {
  try { await api("/api/collection", SCH.pick); toast("Schedule saved: it applies from the next run"); loadSchedule(); } catch (e) { toast(e.message || String(e), true); }
};

/* ---------- Status: read today / waiting, quota used / left, backlogs, job lanes (refreshes every 30 s) ---------- */
const stBar = (done, total) => { const p = total ? Math.min(100, (100 * done) / total) : 0;
  return `<div class="st-bar"><span style="width:${p}%"></span></div>`; };
function stCard(c) {
  const rows = [];
  if (c.total != null) {
    const left = Math.max(0, c.total - c.read);
    rows.push(`<div class="st-big"><b>${fmt(c.read)}</b><span>of ${fmt(c.total)} ${esc(c.unit)} read today</span></div>${stBar(c.read, c.total)}
      <div class="st-sub">${left ? `<span class="st-wait">${fmt(left)} still to read today</span>` : `<span class="st-done">✓ all read today</span>`}</div>`);
  }
  if (c.quota) {
    const q = c.quota, left = Math.max(0, q.limit - q.used);
    rows.push(`<div class="st-q"><div class="st-ql"><span>Quota used</span><b>${fmt(q.used)} / ${fmt(q.limit)} ${esc(q.unit)}</b></div>${stBar(q.used, q.limit)}
      <div class="st-sub"><b>${fmt(left)}</b> ${esc(q.unit)} left · ${esc(q.note)}</div></div>`);
  }
  if (c.api) rows.push(`<div class="st-sub">Spotify API: ${c.api.ok ? '<span class="st-done">answering</span>' : `<span class="st-wait">blocked${c.api.retry_at ? " until " + new Date(c.api.retry_at).toLocaleTimeString() : ""}</span>`}</div>`);
  if (c.backlog) rows.push(`<div class="st-back"><b>${fmt(c.backlog.count)}</b> ${esc(c.backlog.label)}${c.backlog.due != null ? ` · <b>${fmt(c.backlog.due)}</b> ${esc(c.backlog.due_label)}` : ""}</div>`);
  if (c.extra && c.extra.length) rows.push(kv(c.extra.map(([k, v]) => [esc(k), esc(typeof v === "number" ? fmt(v) : v)])));
  const last = c.last ? `<div class="st-last">Last run: ${esc(c.last.job)} #${c.last.id} · ${esc(c.last.status)} · ${ago(c.last.started_at)}</div>` : "";
  return `<div class="card st-card"><div class="st-head"><i class="st-ic ic-${c.icon}"></i><div><h2>${esc(c.name)}</h2><div class="muted" style="font-size:12px">${esc(c.what)}</div></div></div>${rows.join("")}${last}</div>`;
}
function stLane(name, l) {
  const run = l.running ? `<span class="pill ok"><span class="dot"></span>running</span> <b>${esc(l.job)}</b> #${l.run_id} · started ${ago(l.started_at)}` : `<span class="pill"><span class="dot"></span>idle</span>`;
  return `<div class="card"><div class="card-head"><h2>${name}</h2></div><div>${run}</div>
    ${l.running && l.log && l.log.length ? `<pre class="st-log">${esc(l.log.join("\n"))}</pre>` : ""}
    <div class="st-sub" style="margin-top:8px">Next: ${l.next ? `<b>${esc(l.next.job)}</b>: ${esc(l.next.why)}` : "nothing due right now"}</div></div>`;
}
let stTimer;
async function loadStatus() {
  clearTimeout(stTimer);
  const d = await api("/api/live");
  $("#st-at").textContent = `Day ${d.day} (server) · updated ${new Date(d.at).toLocaleTimeString()}`;
  $("#st-lanes").innerHTML = stLane("API lane", d.lanes.api) + stLane("Browser lane", d.lanes.browser);
  $("#st-cards").innerHTML = d.cards.map(stCard).join("");
  stTimer = setTimeout(() => { if (location.hash.startsWith("#status")) loadStatus(); }, 30000);
}
$("#st-refresh").onclick = () => loadStatus();


// ---- Missed readings: items of the songs in scope not read within 24 hours (lens/freshness.py), retried by the watchdog
let msTimer, MS_PLAT = "";
const istTime = (iso) => iso ? new Date(iso).toLocaleString("en-IN", { timeZone: "Asia/Kolkata", day: "numeric", month: "short", hour: "numeric", minute: "2-digit", hour12: true }) + " IST" : "never";
const HEAL_STATE = { ok: ["ok", "Working"], healed: ["warn", "Healed: a fallback is reading"], broken: ["bad", "Held: the page changed"], idle: ["", "No reads today"] };
async function loadHeal() {
  const el = $("#ms-heal");
  let d;
  try { d = await api("/api/heal"); } catch (e) { el.innerHTML = `<tbody><tr><td class="empty">Could not load the readers' health: ${esc(e.message)}</td></tr></tbody>`; return; }
  const ways = (by, primary) => Object.entries(by).filter(([k]) => k !== "fail" && k !== "guard")
    .map(([k, v]) => `<span class="pill${k === primary ? "" : " warn"}" style="padding:0 7px" title="${k === primary ? "the usual way" : "a fallback"}">${esc(k)} ${fmt(v)}</span>`).join(" ") || '<span class="muted">–</span>';
  el.innerHTML = `<thead><tr><th>Reader</th><th>State today</th><th>Found today (by way)</th><th class="num">Not readable</th><th class="num">Held by guard</th>
      <th class="num">Suspicious</th><th class="num">Read this week</th><th>Learned marker</th><th>Page sample</th></tr></thead><tbody>` +
    d.readers.map((r) => { const [cls, label] = HEAL_STATE[r.state] || ["", r.state];
      return `<tr><td><b>${esc(r.name)}</b><div class="t2">usual way: ${esc(r.primary)}</div></td>
        <td><span class="pill ${cls}"><span class="dot"></span>${esc(label)}</span></td><td>${ways(r.today.by, r.primary)}</td>
        <td class="num${r.today.fail ? "" : " muted"}">${r.today.fail ? fmt(r.today.fail) : "–"}</td><td class="num${r.today.guard ? "" : " muted"}">${r.today.guard ? fmt(r.today.guard) : "–"}</td>
        <td class="num${r.suspects_today ? "" : " muted"}">${r.suspects_today ? fmt(r.suspects_today) : "–"}</td>
        <td class="num">${fmt(r.week.primary + r.week.fallback)}${r.week.fallback ? ` <span class="muted" style="font-size:11px">${fmt(r.week.fallback)} by fallback</span>` : ""}</td>
        <td class="mono" style="font-size:12px">${r.hint ? esc(r.hint) : '<span class="muted">–</span>'}</td>
        <td class="muted" style="font-size:12px">${r.last_sample ? `kept ${esc(r.last_sample)}` : "–"}</td></tr>`; }).join("") + "</tbody>";
  $("#ms-suspects").innerHTML = d.suspects.length ? `<details style="padding:4px 18px 14px"><summary class="muted" style="cursor:pointer;font-size:12.5px">
      ${fmt(d.suspects.length)} most recent values held as suspicious (not stored: they fell sharply or jumped against the last reading)</summary>
      <div class="table-wrap" style="max-height:300px;margin-top:8px"><table><thead><tr><th>When</th><th>Platform</th><th>Item</th><th>Metric</th><th class="num">Value read</th><th class="num">Last reading</th><th>Why</th></tr></thead><tbody>` +
      d.suspects.map((x) => `<tr><td class="muted">${esc(istTime(x.at))}</td><td>${esc(x.platform)}</td><td>${esc((x.title || "").slice(0, 70))}</td><td>${esc(x.metric)}</td>
        <td class="num">${fmt(x.value)}</td><td class="num">${fmt(x.last)}</td><td>${x.reason === "fell" ? "fell sharply" : "jumped"}</td></tr>`).join("") + "</tbody></table></div></details>" : "";
}
async function loadMissed() {
  clearTimeout(msTimer);
  loadHeal();
  const d = await api("/api/freshness" + (MS_PLAT ? `?platform=${encodeURIComponent(MS_PLAT)}` : ""));
  const s = d.settings, plats = Object.keys(d.names);
  $("#ms-sub").textContent = `Songs first uploaded from ${s.since} (not released before the period), on every platform they are linked to: an item is missed when it was last read more than ${s.hours} hours ago, or linked that long ago and never read. A platform with missed items gets its job started again every ${s.retry_minutes} minutes until they are read.`;
  $("#ms-at").textContent = `updated ${istTime(d.at)}`;
  $("#ms-kpis").innerHTML = plats.map((p) => { const c = d.counts[p] || { total: 0, missed: 0 };
    return `<div class="card kpi click" data-p="${p}" style="--kpi-c:${c.missed ? "var(--bad, #e5484d)" : "var(--good, #30a46c)"}"><div class="label">${esc(d.names[p])}</div>
      <div class="value">${fmt(c.missed)}</div><div class="sub">missed of ${fmt(c.total)} items${d.stuck?.[p] != null ? ' · <b>stuck: its job does not read them</b>' : ""}</div>
      ${c.missed ? `<button class="small ms-retry" data-p="${p}" style="margin-top:8px">Retry now</button>` : ""}</div>`; }).join("");
  document.querySelectorAll(".ms-retry").forEach((b) => (b.onclick = (e) => { e.stopPropagation(); msRetry(b.dataset.p); }));
  document.querySelectorAll("#ms-kpis .kpi").forEach((k) => (k.onclick = () => { MS_PLAT = MS_PLAT === k.dataset.p ? "" : k.dataset.p; $("#ms-platform").value = MS_PLAT; loadMissed(); }));
  $("#ms-runs").innerHTML = `<thead><tr><th>Platform</th><th>Job</th><th>Last run</th><th>Status</th><th>Finished</th><th>Error / stop reason</th></tr></thead><tbody>` +
    plats.map((p) => { const r = d.runs[p]; return `<tr><td>${esc(d.names[p])}</td><td>${esc(r?.job || "")}</td><td>${istTime(r?.started_at)}</td>
      <td>${r ? `<span class="pill ${r.status === "ok" ? "good" : r.status === "running" ? "" : "warn"}">${esc(r.status)}</span>` : "–"}</td>
      <td>${r?.finished_at ? istTime(r.finished_at) : ""}</td><td style="font-size:12.5px">${esc(r?.error || "")}</td></tr>`; }).join("") + "</tbody>";
  const sel = $("#ms-platform");
  sel.innerHTML = `<option value="">All platforms (${fmt(Object.values(d.counts).reduce((a, c) => a + c.missed, 0))})</option>` +
    plats.map((p) => `<option value="${p}">${esc(d.names[p])} (${fmt((d.counts[p] || {}).missed || 0)})</option>`).join("");
  sel.value = MS_PLAT;
  sel.onchange = () => { MS_PLAT = sel.value; loadMissed(); };
  $("#ms-title").textContent = `Not read within ${s.hours} hours: ${fmt(d.missed_total)}${d.missed_total > d.missed.length ? ` (first ${fmt(d.missed.length)} shown)` : ""}`;
  $("#ms-table").innerHTML = d.missed.length ? `<thead><tr><th>Song</th><th>First upload</th><th>Label</th><th>Platform</th><th>Item</th><th>Last read</th><th class="num">Hours since</th><th></th></tr></thead><tbody>` +
    d.missed.map((r) => `<tr class="click" data-id="${r.song_id}"><td>${esc(r.song)}</td><td>${esc(r.first_upload)}</td><td>${esc(r.label)}</td>
      <td>${esc(d.names[r.platform])}</td><td style="font-size:12.5px">${esc(r.item || "")}</td><td>${r.last_read ? istTime(r.last_read) : '<span class="pill warn">never</span>'}</td>
      <td class="num">${r.hours_since == null ? "–" : fmt(r.hours_since)}</td>
      <td><button class="small ms-mark" data-id="${r.item_id}" data-p="${r.platform}" title="Cannot be read (e.g. removed): not shown as missed for 7 days">Mark unavailable</button></td></tr>`).join("") + "</tbody>"
    : `<tbody><tr><td class="muted" style="padding:16px">Every item was read within ${s.hours} hours.</td></tr></tbody>`;
  document.querySelectorAll("#ms-table tr.click").forEach((tr) => (tr.onclick = () => openTrack(tr.dataset.id)));
  document.querySelectorAll(".ms-mark").forEach((b) => (b.onclick = async (e) => {
    e.stopPropagation(); b.disabled = true;
    await api("/api/freshness/mark", { item_id: +b.dataset.id, platform: b.dataset.p }); loadMissed(); }));
  const why = { gone: "not found (removed, private or gone)", unavailable: "marked unavailable here" };
  $("#ms-marked-title").textContent = `Could not be read (last 7 days): ${fmt(d.marked.length)}`;
  $("#ms-marked").innerHTML = d.marked.length ? `<thead><tr><th>Song</th><th>First upload</th><th>Label</th><th>Platform</th><th>Item</th><th>Why</th><th>When</th><th></th></tr></thead><tbody>` +
    d.marked.map((r) => `<tr><td>${esc(r.song)}</td><td>${esc(r.first_upload)}</td><td>${esc(r.label)}</td><td>${esc(d.names[r.platform] || r.platform)}</td>
      <td style="font-size:12.5px">${esc(r.item || "")}</td><td>${esc(why[r.outcome] || r.outcome)}</td><td>${istTime(r.at)}</td>
      <td><button class="small ms-clear" data-id="${r.item_id}" data-m="${r.metric}">Clear</button></td></tr>`).join("") + "</tbody>"
    : `<tbody><tr><td class="muted" style="padding:16px">Nothing.</td></tr></tbody>`;
  document.querySelectorAll(".ms-clear").forEach((b) => (b.onclick = async () => {
    b.disabled = true; await api("/api/freshness/mark", { item_id: +b.dataset.id, metric: b.dataset.m, clear: true }); loadMissed(); }));
  const total = Object.values(d.counts).reduce((a, c) => a + c.missed, 0), b = $("#nav-missed");
  if (b) { b.textContent = fmt(total); b.style.display = total ? "" : "none"; }
  msTimer = setTimeout(() => { if (location.hash.startsWith("#missed")) loadMissed(); }, 60000);
}
$("#ms-refresh").onclick = () => loadMissed();
async function msRetry(platform) {
  const r = await api("/api/freshness/retry", platform ? { platform } : {});
  const st = r.started.map((x) => x.job).join(", "), busy = r.busy.map((x) => x.job).join(", ");
  $("#ms-msg").textContent = (st ? `Started: ${st}. ` : "") + (busy ? `Busy, the watchdog starts it when its lane is free: ${busy}.` : "") || "Nothing missed to retry.";
  setTimeout(loadMissed, 3000);
}
$("#ms-retry-all").onclick = () => msRetry("");


/* ---------- Market share: each song's performance by day N of its life, shared out by label ---------- */
const MS = { days: null, month: "", measure: "yt", label: "" };
const LABEL_COLORS = ["#6366f1", "#ef4444", "#10b981", "#f59e0b", "#06b6d4", "#ec4899", "#8b5cf6", "#84cc16", "#f97316"];
// "Official readings started" notice: removes the doubt when a platform's history is shorter (e.g. Spotify)
const PNAME = { youtube: "YouTube views", spotify: "Spotify plays", ytmusic: "YouTube Music plays", jiosaavn: "JioSaavn plays" };
const startedLine = (st) => st ? Object.entries(st).filter(([, d]) => d).map(([p, d]) => `<b>${PNAME[p]}</b> from ${dayLabel(d)} ${d.slice(0, 4)}`).join(" · ") : "";
const shareNum = (k, v) => v == null ? "–" : (k === "sp" || k === "ytm" || k === "audio" ? compact1 : compact)(v);
async function loadShare() {
  const q = new URLSearchParams({ ...(MS.days ? { days: MS.days } : {}), ...(MS.month ? { month: MS.month } : {}) });
  const d = await api(`/api/market_share?${q}`);
  MS.days = d.days;
  const N = d.days, lcolor = Object.fromEntries(d.labels.map((l, i) => [l.label, LABEL_COLORS[i % LABEL_COLORS.length]]));
  $("#sh-asof").innerHTML = `<span class="dot"></span>Figures for ${dayLabel(d.report_day)} (last completed day)`;
  $("#sh-started").innerHTML = `<b>Official daily readings started:</b> ${startedLine(d.started)}. Songs released before then could not be read from their first day,
    and many labels' Spotify tracks were linked only recently, so the Spotify and audio shares rest on fewer days for those labels for now
    (see "readings since" in the table). They even out as the songs build up history; YouTube views have the longest history.`;
  $("#sh-presets").innerHTML = d.presets.map((n) => `<button class="small${n === N ? " on" : ""}" data-n="${n}">${n} days</button>`).join("");
  $("#sh-presets").querySelectorAll("button").forEach((b) => (b.onclick = () => { MS.days = +b.dataset.n; loadShare(); }));
  $("#sh-days").value = N;
  $("#sh-default").textContent = `Default: ${d.default_days} days`;
  $("#sh-save").style.display = isClient() || N === d.default_days ? "none" : "";
  $("#sh-month").innerHTML = `<option value="">All songs since ${esc(d.period[0])}</option>` + (d.months || []).map((m) => `<option value="${m}"${m === MS.month ? " selected" : ""}>Released in ${monthName(m)}</option>`).join("");
  $("#sh-measure").innerHTML = d.measures.map((m) => `<option value="${m.key}"${m.key === MS.measure ? " selected" : ""}>${esc(m.name)}</option>`).join("");
  const c = d.counts, by = (k) => d.labels.slice().sort((a, b) => (b[k] || 0) - (a[k] || 0)), focus = d.labels.find((l) => l.label === d.focus_label);
  const leadYt = by("yt")[0], leadAu = by("audio")[0];
  $("#sh-kpis").innerHTML = `
    <div class="card kpi"><div class="label">Songs measured</div><div class="value">${fmt(c.final + c.provisional)}</div>
      <div class="gainline"><span class="gain-chip">${fmt(c.final)} final at day ${N}</span> <span class="gain-chip none">${fmt(c.provisional)} provisional</span></div>
      <div class="explain">${fmt(c.not_measurable)} older songs not measurable (no reading by their day ${N}).</div></div>
    <div class="card kpi" style="--kpi-c:${PCOLOR.youtube}"><div class="label">Leader, YouTube views</div><div class="value">${leadYt?.yt_share ?? "–"}%</div>
      <div class="gainline">${leadYt ? labelTag(leadYt.label) : ""}</div><div class="explain">Share of all views the measured songs had by day ${N}.</div></div>
    <div class="card kpi" style="--kpi-c:${PCOLOR.spotify}"><div class="label">Leader, audio streams</div><div class="value">${leadAu?.audio_share ?? "–"}%</div>
      <div class="gainline">${leadAu ? labelTag(leadAu.label) : ""}</div><div class="explain">Spotify + YouTube Music + JioSaavn, by day ${N}.</div></div>
    <div class="card kpi" style="--kpi-c:var(--accent-2)"><div class="label">${esc(d.focus_label)}</div><div class="value">${focus?.yt_share ?? "–"}%</div>
      <div class="gainline"><span class="gain-chip none">${focus?.audio_share ?? "–"}% of audio streams</span></div>
      <div class="explain">Share of YouTube views · rank ${focus?.yt_rank ?? "–"} of ${d.labels.length} on views, ${focus?.audio_rank ?? "–"} on streams.</div></div>`;
  const bars = (k) => by(k).map((l) => `<div class="share-line${l.label === d.focus_label ? " focus" : ""}" style="--lc:${lcolor[l.label]}">
      <div class="sl-top"><span>${labelTag(l.label)}</span><span class="sl-pct">${l[k + "_share"] ?? 0}%</span></div>
      <div class="share-track"><span style="width:${l[k + "_share"] || 0}%;background:${lcolor[l.label]}"></span></div>
      <div class="sl-meta">${shareNum(k, l[k])} · ${fmt(l[k + "_songs"])} songs</div></div>`).join("");
  $("#sh-yt").innerHTML = bars("yt");
  $("#sh-au").innerHTML = bars("audio");
  $("#sh-yt-title").textContent = `Share of YouTube views by day ${N}`;
  $("#sh-au-title").textContent = `Share of audio streams by day ${N}`;
  const ks = ["yt", "audio", "sp", "ytm", "js"], names = Object.fromEntries(d.measures.map((m) => [m.key, m.name]));
  $("#sh-matrix").innerHTML = `<thead><tr><th>Label</th><th class="num">Songs</th>${ks.map((k) => `<th class="num">${esc(names[k])}</th>`).join("")}</tr></thead><tbody>` +
    by("yt").map((l) => `<tr class="${l.label === d.focus_label ? "focus-row" : ""}"><td>${labelTag(l.label)}</td><td class="num">${fmt(l.songs)}<div class="muted" style="font-size:11px">${fmt(l.final)} final · ${fmt(l.provisional)} prov.</div></td>` +
      ks.map((k) => `<td class="num"><b>${l[k + "_share"] ?? "–"}%</b><div class="muted" style="font-size:11px">#${l[k + "_rank"]} · ${shareNum(k, l[k])} · ${fmt(l[k + "_songs"])} songs</div>${l[k + "_since"] ? `<div class="muted" style="font-size:11px">readings since ${dayLabel(l[k + "_since"])}</div>` : ""}</td>`).join("") + "</tr>").join("") +
    `<tr><td><b>All labels</b></td><td class="num">${fmt(c.final + c.provisional)}</td>${ks.map((k) => `<td class="num"><b>100%</b><div class="muted" style="font-size:11px">${shareNum(k, d.total[k])} · ${fmt(d.total_songs[k])} songs</div></td>`).join("")}</tr></tbody>`;
  const cur = d.curves[MS.measure] || {};
  const pad = (n) => "D" + String(n).padStart(4, "0");
  const series = Object.entries(cur).map(([lab, pts]) => ({ name: lab, color: lcolor[lab], total: true, points: pts.map((p) => ({ x: pad(p.x), y: p.y })) }))
    .filter((x) => x.points.length).sort((a, b) => (a.name === d.focus_label) - (b.name === d.focus_label));
  multiLine($("#sh-curve"), series, { height: 300, xfmt: (x) => "day " + (+x.slice(1)), tipx: (x) => "day " + (+x.slice(1)) });
  $("#sh-curve-title").textContent = `Average song by day of life: ${names[MS.measure]}, days 1–${N}`;
  $("#sh-curve-legend").innerHTML = series.map((x) => `<span><span style="display:inline-block;width:12px;height:3px;background:${x.color};vertical-align:middle;margin-right:5px"></span>${esc(x.name)}</span>`).join("");
  $("#sh-label").innerHTML = `<option value="">All labels</option>` + d.labels.map((l) => `<option${l.label === MS.label ? " selected" : ""}>${esc(l.label)}</option>`).join("");
  const on = (r) => r.measured_on ? ` · ${dayLabel(r.measured_on)}` : "";
  const st = (r) => r.status === "final" ? `<span class="gain-chip">final · day ${N}${on(r)}</span>`
    : r.status === "provisional" ? `<span class="gain-chip none" title="Until day ${N}: the latest day with data">provisional · day ${r.day}${on(r)}</span>`
    : `<span class="gain-chip none" style="opacity:.6">not measurable</span>`;
  const rows = d.songs.filter((r) => !MS.label || r.label === MS.label).slice(0, 100);
  $("#sh-songs").innerHTML = `<thead><tr><th>Song</th><th>Released</th><th>Status</th><th class="num">YouTube views</th><th class="num">Audio streams</th><th class="num">Spotify</th><th class="num">YT Music</th><th class="num">JioSaavn</th></tr></thead><tbody>` +
    rows.map((r) => `<tr class="click" data-id="${r.id}"><td><div class="t1">${esc(r.title)}${labelTag(r.label)}</div></td><td class="muted">${esc(r.release)}</td><td>${st(r)}</td>
      ${["yt", "audio", "sp", "ytm", "js"].map((k) => `<td class="num">${shareNum(k, r[k])}</td>`).join("")}</tr>`).join("") + "</tbody>";
  $("#sh-formula").innerHTML = `<b>How it's calculated.</b> Day 1 of a song is its first YouTube upload; day <i>n</i> = upload date + (<i>n</i> − 1).
    <b>P(song, platform, n)</b> = the song's views or plays on that platform at the end of day <i>n</i>: a song starts at zero, so this is its growth over its first <i>n</i> days.
    No reading that day (e.g. Spotify read a day late) → interpolated between the readings before and after.
    <b>Final</b> = P at day ${N} once the song has reached it; <b>provisional</b> = P at the song's latest completed day while it is younger (it turns final on day ${N});
    <b>not measurable</b> = the song's first reading came after its day ${N}, so it is left out.
    <b>Share(label, platform)</b> = Σ P over the label's songs ÷ Σ P over all songs in the selection. YouTube views and audio streams get separate shares.
    The song counts show how many songs each share rests on (Spotify readings began later for some labels).`;
}
$("#sh-days").addEventListener("change", (e) => { const n = Math.max(1, Math.min(365, +e.target.value || 30)); MS.days = n; loadShare(); });
$("#sh-month").addEventListener("change", (e) => { MS.month = e.target.value; loadShare(); });
$("#sh-measure").addEventListener("change", (e) => { MS.measure = e.target.value; loadShare(); });
$("#sh-label").addEventListener("change", (e) => { MS.label = e.target.value; loadShare(); });
$("#sh-save").onclick = async () => { await api("/api/market_share/settings", { days: MS.days }); loadShare(); };


/* ---------- Management summary (POC): ranked by Spotify, the week = last 7 completed days ---------- */
const MG = { genre: "category", method: "split" };
const chg = (v) => v == null ? '<span class="muted">–</span>' : `<span class="${v >= 0 ? "delta-up" : "delta-down"}">${v >= 0 ? "▲" : "▼"} ${Math.abs(v)}%</span>`;
async function loadMgmt() {
  const d = await api(`/api/management?genre=${MG.genre}&method=${MG.method}`);
  const wk = `${dayLabel(d.week[0])} – ${dayLabel(d.week[1])}`;
  $("#mg-week").innerHTML = `<span class="dot"></span>Week ${wk} · ranked by Spotify`;
  $("#mg-started").innerHTML = `<b>Official daily readings started:</b> ${startedLine(d.started)}.${d.full_prev ? "" : " The week before this one began before Spotify readings did, so week-on-week changes show from the next full week."}`;
  const lead = (xs) => xs.find((x) => x.name !== "Unknown") || {};
  const L = lead(d.labels), G = lead(d.languages);
  $("#mg-kpis").innerHTML = `
    <div class="card kpi" style="--kpi-c:${PCOLOR.spotify}"><div class="label">Spotify plays this week</div><div class="value">${compact1(d.sp_week)}</div>
      <div class="gainline">${d.change == null ? '<span class="gain-chip none">change from next week</span>' : `<span class="gain-chip">${d.change >= 0 ? "+" : ""}${d.change}% vs week before</span>`}</div>
      <div class="explain">Growth over ${wk}, all songs of the period · YouTube +${compact(d.yt_week)} views alongside.</div></div>
    <div class="card kpi"><div class="label">Songs on Spotify</div><div class="value">${fmt(d.on_spotify)}</div>
      <div class="gainline"><span class="gain-chip none">of ${fmt(d.songs)} songs</span></div><div class="explain">${fmt(d.releases_week)} songs released this week.</div></div>
    <div class="card kpi" style="--kpi-c:var(--accent-2)"><div class="label">Top label</div><div class="value" style="font-size:22px">${esc(L.name || "–")}</div>
      <div class="gainline"><span class="gain-chip">${L.share ?? "–"}% of Spotify plays</span></div><div class="explain">+${compact1(L.sp_week)} plays this week.</div></div>
    <div class="card kpi" style="--kpi-c:var(--good)"><div class="label">Top language</div><div class="value" style="font-size:22px">${esc(G.name || "–")}</div>
      <div class="gainline"><span class="gain-chip">${G.share ?? "–"}% of Spotify plays</span></div><div class="explain">${fmt(G.songs)} songs · +${compact1(G.sp_week)} plays.</div></div>
    <div class="card kpi" style="--kpi-c:var(--warning)"><div class="label">Top artist</div><div class="value" style="font-size:22px">${esc(d.top_artists[0]?.name || "–")}</div>
      <div class="gainline"><span class="gain-chip">+${compact1(d.top_artists[0]?.sp_week)} plays</span></div><div class="explain">${fmt(d.top_artists[0]?.songs)} songs this period.</div></div>`;
  const songTbl = (xs, el) => { $(el).innerHTML = xs.length ? `<thead><tr><th>#</th><th></th><th>Song</th><th>Language</th><th class="num">Spotify this week</th><th class="num">YouTube this week</th></tr></thead><tbody>` +
    xs.map((r, i) => `<tr class="click" data-id="${r.id}"><td class="muted num">${i + 1}</td>${playOne(r)}<td><div class="t1">${esc(r.title)}${labelTag(r.label)}</div><div class="t2">${esc(r.release)}</div></td>
      <td>${esc(r.language)}</td><td class="num"><b>+${compact1(r.sp_week)}</b></td><td class="num muted">+${compact(r.yt_week)}</td></tr>`).join("") + "</tbody>"
    : `<tbody><tr><td class="empty">No Spotify growth this week yet.</td></tr></tbody>`; };
  songTbl(d.top_songs, "#mg-songs");
  songTbl(d.top_releases, "#mg-releases");
  $("#mg-releases-title").textContent = `Top new releases (last ${d.new_release_days} days)`;
  const grpTbl = (xs, el, first, tag) => { $(el).innerHTML = `<thead><tr><th>#</th><th>${first}</th><th class="num">Songs</th><th class="num">Spotify this week</th><th class="num">Share</th><th class="num">vs week before</th><th class="num">YouTube this week</th></tr></thead><tbody>` +
    xs.map((r) => `<tr><td class="muted num">${r.rank}</td><td>${tag ? labelTag(r.name) : `<b>${esc(r.name)}</b>`}</td><td class="num">${fmt(r.songs)}</td><td class="num"><b>+${compact1(r.sp_week)}</b></td>
      <td class="num"><span class="share-bar" style="--w:${r.share || 0}%;--c:${PCOLOR.spotify}">${r.share ?? "–"}%</span></td><td class="num">${chg(r.change)}</td><td class="num muted">+${compact(r.yt_week)}</td></tr>`).join("") + "</tbody>"; };
  grpTbl(d.top_artists, "#mg-artists", "Artist");
  grpTbl(d.labels, "#mg-labels", "Label", true);
  grpTbl(d.languages, "#mg-langs", "Language");
  const colors = ["#6366f1", "#ef4444", "#10b981", "#f59e0b", "#06b6d4", "#ec4899"];
  const series = d.language_trend.map((l, i) => ({ name: l.name, color: colors[i % colors.length], points: l.points }));
  multiLine($("#mg-lang-trend"), series.filter((x) => x.points.length), { height: 260 });
  $("#mg-lang-legend").innerHTML = series.map((x) => `<span><span style="display:inline-block;width:12px;height:3px;background:${x.color};vertical-align:middle;margin-right:5px"></span>${esc(x.name)}</span>`).join("");
  document.querySelectorAll("#mg-genre-src button").forEach((b) => { b.classList.toggle("on", b.dataset.g === MG.genre); b.onclick = () => { MG.genre = b.dataset.g; loadMgmt(); }; });
  document.querySelectorAll("#mg-genre-method button").forEach((b) => { b.classList.toggle("on", b.dataset.m === MG.method); b.onclick = () => { MG.method = b.dataset.m; loadMgmt(); }; });
  grpTbl(d.genres, "#mg-genres", MG.genre === "apple" ? "Apple Music genre" : "Category");
  const gc = d.genre_check, pc = (n) => Math.round((100 * n) / (gc.songs || 1));
  $("#mg-genre-check").innerHTML = `<b>Genre attribution: findings for review.</b>
    <ul style="margin:6px 0 0 18px;padding:0">
      <li>Spotify gives no genres to apps in development mode, so genre comes from <b>our category</b> (every song, ${fmt(gc.coverage.category)} of ${fmt(gc.songs)}, one value each)
        or <b>Apple Music's genre</b> (${fmt(gc.coverage.genre)} songs, ${pc(gc.coverage.genre)}%, one value each).</li>
      <li>Songs with more than one genre today: our category <b>${fmt(gc.multi.category)}</b>, Apple <b>${fmt(gc.multi.genre)}</b>. So the method below changes nothing yet; it is ready for when a song carries several genres.</li>
      <li>Apple's genres mix in languages (${gc.apple_genres_that_are_languages.map(esc).join(", ")}), so Apple's view overlaps the language view; our category is the default.</li>
      <li>Methods: <b>Split equally</b> (1/k of the song to each of its k genres; totals stay 100%, recommended) · <b>Primary only</b> (its first genre) · <b>Full credit</b> (each genre in full; totals above 100%).</li>
      <li>Artists: ${fmt(gc.multi.spotify_artist)} songs have several Spotify artists; each credited artist gets the song in full (as artist charts do).</li></ul>`;
  $("#mg-how").innerHTML = `<b>How to read this.</b> Everything is ranked by <b>Spotify plays gained</b> in the week (${wk}, the last 7 completed days); YouTube views are shown alongside, not used for ranking.
    A gain is each track's reading minus its own previous reading, so late Spotify counts land on the next reading and nothing is lost. Language-wise growth uses each song's detected language.`;
}


/* ---------- Daily additions (developer): songs added each day, and on each platform ---------- */
const AD = { days: 30, label: "", view: "links" };
async function loadAdded() {
  const q = new URLSearchParams({ days: AD.days }); if (AD.label) q.set("label", AD.label);
  const d = await api("/api/additions?" + q);
  const P = d.platforms, links = AD.view === "links", key = links ? "links" : "now_on";
  const dayName = (x) => new Date(x + "T00:00:00Z").toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short", timeZone: "UTC" });
  $("#ad-sub").textContent = `Songs of the period (first YouTube upload from ${d.since}, not released before it)${d.label ? `, ${d.label} only` : ""}: how many were added each day, and on each platform. Days are server days (UTC); today is still running.`;
  document.querySelectorAll("#ad-days button").forEach((b) => b.classList.toggle("on", +b.dataset.d === AD.days));
  document.querySelectorAll("#ad-view button").forEach((b) => b.classList.toggle("on", b.dataset.v === AD.view));
  const sel = $("#ad-label");
  sel.innerHTML = `<option value="">All labels</option>` + d.labels.map((l) => `<option${l === AD.label ? " selected" : ""}>${esc(l)}</option>`).join("");
  const sp = d.on_now.spotify || 0;
  $("#ad-kpis").innerHTML = [
    ["New songs today", fmt(d.today_so_far), "so far (the server day is still running)", "var(--k1)"],
    ["New songs yesterday", fmt(d.yesterday), `${fmt(d.links_yesterday)} platform links made yesterday`, "var(--k2)"],
    ["7-day average", fmt(d.avg7), "new songs a day (completed days)", "var(--k3)"],
    [`New songs in ${d.days} days`, fmt(d.totals.found), `${fmt(d.period_songs)} songs in the period · ${d.period_songs ? Math.round((100 * sp) / d.period_songs) : 0}% on Spotify`, "var(--k4)"],
  ].map(([l, v, sub, c]) => `<div class="card kpi" style="--kpi-c:${c}"><div class="label">${l}</div><div class="value">${v}</div><div class="sub">${sub}</div></div>`).join("");
  const asc = [...d.rows].reverse();
  barChart($("#ad-chart"), asc.map((r) => ({ label: r.day.slice(5), value: r.found, day: r.day, up: r.uploaded })), {
    tip: (b) => `<b>${esc(dayName(b.day))}</b><br>${fmt(b.value)} new songs · ${fmt(b.up)} first uploaded that day${b.value - b.up > 50 ? "<br>backfill: most were uploaded earlier" : ""}` });
  $("#ad-chart2-title").textContent = links ? "Links made per day, by platform" : "Songs found each day: on each platform now";
  const shown = P.filter((p) => d.totals[key][p.key]);  // platforms with nothing in the range stay off the chart
  multiLine($("#ad-chart2"), shown.map((p) => ({ name: p.name, color: PCOLOR[p.key] || "var(--accent-2)", points: asc.map((r) => ({ x: r.day, y: r[key][p.key] })) })), { height: 230 });
  $("#ad-legend").innerHTML = shown.map((p) => `<span><span style="display:inline-block;width:12px;height:3px;background:${PCOLOR[p.key] || "var(--accent-2)"};vertical-align:middle;margin-right:5px"></span>${esc(p.name)}</span>`).join("");
  $("#ad-table-title").textContent = links ? "Day by day: songs added, and songs first linked on each platform that day" : "Day by day: songs found that day, and how many of them are on each platform now";
  const cell = (n, of) => !links && of ? `<td class="num">${fmt(n)} <span class="muted" style="font-size:11px">${Math.round((100 * n) / of)}%</span></td>` : `<td class="num${n ? "" : " muted"}">${n ? fmt(n) : "–"}</td>`;
  const today = d.today;
  const SHORT = { spotify: "Spotify", ytmusic: "YT Music", jiosaavn: "JioSaavn", deezer: "Deezer", apple: "Apple", musicbrainz: "MusicBrainz", instagram: "Instagram" };
  // a backfill (a new label or channel, a laptop import) stores many older uploads on one day: say so next to the number
  const backfill = (r) => r.found - r.uploaded > 50;
  $("#ad-table").innerHTML = `<thead><tr><th>Day</th><th class="num">New songs</th><th class="num" title="Songs whose first YouTube upload is that day">Uploaded that day</th>${links ? `<th class="num" title="Every upload of these songs stored that day">YT uploads</th>` : ""}
      ${P.map((p) => `<th class="num" title="${esc(p.name)}">${esc(SHORT[p.key] || p.name)}</th>`).join("")}${links ? `<th class="num" title="Spotify matches found that day, still waiting in Review">In Review</th>` : ""}</tr></thead><tbody>` +
    d.rows.map((r) => `<tr><td style="white-space:nowrap"><b>${esc(dayName(r.day))}</b>${r.day === today ? ' <span class="pill run" style="padding:0 6px"><span class="dot"></span>today</span>' : ""}${backfill(r) ? ' <span class="pill warn" style="padding:0 6px" title="Most of these songs were first uploaded earlier: a backfill (new label or channel, or an import)">backfill</span>' : ""}</td>
      <td class="num"><b>${fmt(r.found)}</b></td><td class="num">${fmt(r.uploaded)}</td>${links ? `<td class="num">${fmt(r.uploads)}</td>` : ""}
      ${P.map((p) => cell(r[key][p.key], r.found)).join("")}${links ? `<td class="num${r.review ? "" : " muted"}">${r.review ? fmt(r.review) : "–"}</td>` : ""}</tr>`).join("") +
    `</tbody><tfoot><tr><td><b>${d.days} days</b></td><td class="num"><b>${fmt(d.totals.found)}</b></td><td class="num"><b>${fmt(d.totals.uploaded)}</b></td>${links ? `<td class="num"><b>${fmt(d.totals.uploads)}</b></td>` : ""}
      ${P.map((p) => links ? `<td class="num"><b>${fmt(d.totals.links[p.key])}</b></td>` : cell(d.totals.now_on[p.key], d.totals.found).replace("<td", "<td style='font-weight:650'")).join("")}${links ? `<td class="num"><b>${fmt(d.totals.review)}</b></td>` : ""}</tr></tfoot>`;
  renderPV(d);
  $("#ad-howto").innerHTML = `<b>How to read this.</b> <b>New songs</b>: songs first stored that day (grouped from the labels' YouTube uploads).
    <b>First uploaded that day</b>: songs whose first YouTube upload is that day (a backfill or a new channel puts older uploads on the day they were found).
    <b>YouTube uploads</b>: every upload of these songs stored that day (video, lyric, audio, Shorts). ${links
      ? `<b>Platform columns</b>: songs first linked on that platform that day (a song counts once per platform, on its first link), whatever day the song itself was found; spikes are backfills (e.g. a laptop import or a channel scan). <b>Spotify in Review</b>: matches found that day still waiting for a decision.`
      : `<b>Platform columns</b>: of the songs found that day, how many are on that platform now (and the share). Recent days are lower because links keep coming in over the following days.`}
    Songs released before the period are not counted (they are stored only).`;
}
document.querySelectorAll("#ad-days button").forEach((b) => (b.onclick = () => { AD.days = +b.dataset.d; loadAdded(); }));
document.querySelectorAll("#ad-view button").forEach((b) => (b.onclick = () => { AD.view = b.dataset.v; loadAdded(); }));
$("#ad-label").onchange = (e) => { AD.label = e.target.value; loadAdded(); };
$("#ad-refresh").onclick = () => loadAdded();

/* Songs found on each platform: one platform, compare several, or two head to head (choice remembered per browser) */
const PV_KEY = "ml_pv";
const PV = (() => { const d = { mode: "one", one: "spotify", sel: ["youtube", "spotify", "ytmusic", "jiosaavn"], a: "spotify", b: "jiosaavn", metric: "new", scope: "all" };
  try { return Object.assign(d, JSON.parse(localStorage.getItem(PV_KEY) || "{}")); } catch (e) { return d; } })();
const pvSave = () => { try { localStorage.setItem(PV_KEY, JSON.stringify(PV)); } catch (e) {} };
const pvColor = (p) => `var(--pv-${p}, var(--accent))`;
const PV_METRIC = { new: { name: "Songs first linked per day", f: fmt, y: compact }, total: { name: "Songs on the platform (total so far)", f: fmt, y: compact },
  coverage: { name: "Coverage: share of the songs found by then", f: (v) => `${v}%`, y: (v) => `${Math.round(v)}%` } };
let PV_DATA = null;
function pvLag(l) { return l.median == null ? "–" : l.median === 0 ? "same day" : `${l.median} day${l.median === 1 ? "" : "s"}`; }
function renderPV(d) {
  if (d) PV_DATA = d; d = PV_DATA; if (!d) return;
  const pv = d.pv, K = pv.keys, N = pv.names, S = pv.summary, L = pv.lag;
  if (!K.includes(PV.one)) PV.one = "spotify";
  document.querySelectorAll("#pv-mode button").forEach((b) => b.classList.toggle("on", b.dataset.m === PV.mode));
  document.querySelectorAll("#pv-metric button").forEach((b) => b.classList.toggle("on", b.dataset.k === PV.metric));
  $("#pv-metric").style.visibility = PV.mode === "one" || PV.mode === "vsyt" ? "hidden" : "";
  const chip = (p, on, attr = "") => `<button class="pv-chip${on ? " on" : ""}" data-p="${p}" style="--pc:${pvColor(p)}" ${attr}><span class="pv-dot"></span>${esc(N[p])}<span class="pv-n">${fmt(S[p].on_now)}</span></button>`;
  const pick = $("#pv-pick"), body = $("#pv-body");
  const days = (p) => pv.series[p];
  const pts = (p, k) => days(p).map((r) => ({ x: r.day, y: r[k] }));
  if (PV.mode === "one") {
    pick.innerHTML = `<span class="pv-hint">Platform</span>` + K.map((p) => chip(p, p === PV.one)).join("");
    const p = PV.one, s = S[p], l = L[p];
    body.innerHTML = `<div class="pv-stats">
        <div class="pv-stat"><span>Yesterday</span><b>${fmt(s.yesterday)}</b><small>songs first linked</small></div>
        <div class="pv-stat"><span>7-day average</span><b>${fmt(s.avg7)}</b><small>a day, completed days</small></div>
        <div class="pv-stat"><span>In ${d.days} days</span><b>${fmt(s.range)}</b><small>songs first linked</small></div>
        <div class="pv-stat"><span>On ${esc(N[p])} now</span><b>${fmt(s.on_now)}</b><small>${s.coverage}% of ${fmt(pv.songs)} songs</small><i class="pv-meter"><i style="width:${s.coverage}%;background:${pvColor(p)}"></i></i></div>
        <div class="pv-stat"><span>Time to link</span><b>${pvLag(l)}</b><small>${l.n ? `median · ${l.within1}% within a day · ${l.within7}% within a week` : "no songs first uploaded in the range"}</small></div>
      </div>
      <div class="grid g2" style="margin-top:14px">
        <div><div class="pv-ch-title">${p === "youtube" ? "New songs found per day" : "Songs first linked per day"}</div><div class="chart" id="pv-c1"></div></div>
        <div><div class="pv-ch-title">Coverage over time <span class="muted">share of the songs found by then that are on ${esc(N[p])}</span></div><div class="chart" id="pv-c2"></div></div>
      </div>`;
    barChart($("#pv-c1"), days(p).map((r) => ({ label: r.day.slice(5), value: r.new, day: r.day, total: r.total })), { height: 230, color: pvColor(p), hover: pvColor(p),
      tip: (b) => `<b>${esc(b.day)}</b><br>${fmt(b.value)} songs first linked · ${fmt(b.total)} on ${esc(N[p])} by then` });
    multiLine($("#pv-c2"), [{ name: N[p], color: pvColor(p), points: pts(p, "coverage"), total: true }], { height: 230, yfmt: (v) => `${Math.round(v)}%`, vfmt: (v) => `${v}%`, empty: "No songs yet." });
  } else if (PV.mode === "compare") {
    pick.innerHTML = `<span class="pv-hint">Show</span>` + K.map((p) => chip(p, PV.sel.includes(p))).join("") +
      `<span class="spacer"></span><button class="small" id="pv-all">All</button><button class="small" id="pv-none">Clear</button>`;
    const m = PV_METRIC[PV.metric], sel = K.filter((p) => PV.sel.includes(p));
    body.innerHTML = `<div class="pv-ch-title">${m.name}</div><div class="chart" id="pv-c1"></div>
      <div class="table-wrap" style="margin-top:12px"><table class="pv-table"><thead><tr><th>Platform</th><th class="num">Yesterday</th><th class="num">7-day avg</th><th class="num">In ${d.days} days</th>
        <th class="num">On it now</th><th>Coverage</th><th class="num" title="Median days from the first YouTube upload to the first link, songs first uploaded in the range">Time to link</th><th class="num">Within a day</th></tr></thead><tbody>${
        K.map((p) => `<tr class="pv-row${PV.sel.includes(p) ? " on" : ""}" data-p="${p}"><td><span class="pv-dot" style="--pc:${pvColor(p)}"></span><b>${esc(N[p])}</b></td>
          <td class="num">${fmt(S[p].yesterday)}</td><td class="num">${fmt(S[p].avg7)}</td><td class="num">${fmt(S[p].range)}</td><td class="num"><b>${fmt(S[p].on_now)}</b></td>
          <td><span class="pv-meter wide"><i style="width:${S[p].coverage}%;background:${pvColor(p)}"></i></span> <span class="muted">${S[p].coverage}%</span></td>
          <td class="num">${pvLag(L[p])}</td><td class="num">${L[p].n ? L[p].within1 + "%" : "–"}</td></tr>`).join("")}</tbody></table></div>
      <div class="muted" style="font-size:12px;margin-top:8px">Click a row or a chip to add it to the chart or take it off.</div>`;
    multiLine($("#pv-c1"), sel.map((p) => ({ name: N[p], color: pvColor(p), points: pts(p, PV.metric), total: PV.metric !== "new" })),
      { height: 280, yfmt: m.y, vfmt: m.f, dots: sel.length < 4, empty: "Pick one or more platforms above." });
    body.querySelectorAll(".pv-row").forEach((r) => (r.onclick = () => pvToggle(r.dataset.p)));
    $("#pv-all").onclick = () => { PV.sel = [...K]; pvSave(); renderPV(); };
    $("#pv-none").onclick = () => { PV.sel = []; pvSave(); renderPV(); };
  } else if (PV.mode === "vsyt") {
    pvVsYoutube(d, pv, chip, pick, body);
  } else {
    if (PV.a === PV.b) PV.b = K.find((p) => p !== PV.a);
    const opt = (cur) => K.map((p) => `<option value="${p}"${p === cur ? " selected" : ""}>${esc(N[p])}</option>`).join("");
    pick.innerHTML = `<span class="pv-hint">Compare</span><span class="pv-vs"><span class="pv-dot" style="--pc:${pvColor(PV.a)}"></span><select id="pv-a">${opt(PV.a)}</select></span>
      <button class="small" id="pv-swap" title="Swap A and B">⇄</button>
      <span class="pv-vs"><span class="pv-dot" style="--pc:${pvColor(PV.b)}"></span><select id="pv-b">${opt(PV.b)}</select></span>
      <span class="spacer"></span><div class="seg" id="pv-scope"><button class="small" data-s="all">All songs of the period</button><button class="small" data-s="range">Songs found in ${d.days} days</button></div>`;
    const a = PV.a, b = PV.b, key = K.indexOf(a) < K.indexOf(b) ? `${a}|${b}` : `${b}|${a}`, flip = key !== `${a}|${b}`;
    const o0 = pv.overlap[key][PV.scope], o = flip ? { both: o0.both, a_only: o0.b_only, b_only: o0.a_only, neither: o0.neither } : o0;
    const tot = o.both + o.a_only + o.b_only + o.neither || 1, onA = o.both + o.a_only, onB = o.both + o.b_only;
    const seg = (n, c, label, cls = "") => n ? `<i class="${cls}" style="flex:${n};background:${c}" title="${label}: ${fmt(n)}">${n / tot > .07 ? fmt(n) : ""}</i>` : "";
    const m = PV_METRIC[PV.metric];
    body.innerHTML = `<div class="pv-h2h">
        <div class="pv-side" style="--pc:${pvColor(a)}"><span>${esc(N[a])}</span><b>${fmt(S[a].on_now)}</b><small>${S[a].coverage}% coverage · ${fmt(S[a].avg7)} a day · time to link ${pvLag(L[a])}</small></div>
        <div class="pv-mid">vs</div>
        <div class="pv-side" style="--pc:${pvColor(b)}"><span>${esc(N[b])}</span><b>${fmt(S[b].on_now)}</b><small>${S[b].coverage}% coverage · ${fmt(S[b].avg7)} a day · time to link ${pvLag(L[b])}</small></div>
      </div>
      <div class="pv-ch-title" style="margin-top:14px">Overlap <span class="muted">${PV.scope === "all" ? `all ${fmt(pv.songs)} songs of the period` : `the ${fmt(pv.songs_in_range)} songs found in ${d.days} days`}</span></div>
      <div class="pv-stack">${seg(o.a_only, pvColor(a), `Only on ${N[a]}`)}${seg(o.both, `linear-gradient(90deg, ${pvColor(a)}, ${pvColor(b)})`, "On both")}${seg(o.b_only, pvColor(b), `Only on ${N[b]}`)}${seg(o.neither, "var(--glass-track)", "On neither", "pv-none")}</div>
      <div class="pv-legend"><span><i style="background:${pvColor(a)}"></i>Only ${esc(N[a])} <b>${fmt(o.a_only)}</b></span><span><i style="background:linear-gradient(90deg, ${pvColor(a)}, ${pvColor(b)})"></i>Both <b>${fmt(o.both)}</b></span>
        <span><i style="background:${pvColor(b)}"></i>Only ${esc(N[b])} <b>${fmt(o.b_only)}</b></span><span><i style="background:var(--glass-track)"></i>Neither <b>${fmt(o.neither)}</b></span></div>
      <div class="pv-facts">
        <div><b>${onA ? Math.round((100 * o.both) / onA) : 0}%</b> of the songs on ${esc(N[a])} are also on ${esc(N[b])}</div>
        <div><b>${onB ? Math.round((100 * o.both) / onB) : 0}%</b> of the songs on ${esc(N[b])} are also on ${esc(N[a])}</div>
        <div><b>${fmt(o.a_only)}</b> songs to look for on ${esc(N[b])} · <b>${fmt(o.b_only)}</b> to look for on ${esc(N[a])}</div>
      </div>
      <div class="pv-ch-title" style="margin-top:16px">${m.name}</div><div class="chart" id="pv-c1"></div>`;
    multiLine($("#pv-c1"), [a, b].map((p) => ({ name: N[p], color: pvColor(p), points: pts(p, PV.metric), total: PV.metric !== "new" })), { height: 250, yfmt: m.y, vfmt: m.f, empty: "No songs yet." });
    $("#pv-a").onchange = (e) => { PV.a = e.target.value; if (PV.a === PV.b) PV.b = K.find((p) => p !== PV.a); pvSave(); renderPV(); };
    $("#pv-b").onchange = (e) => { PV.b = e.target.value; if (PV.a === PV.b) PV.a = K.find((p) => p !== PV.b); pvSave(); renderPV(); };
    $("#pv-swap").onclick = () => { [PV.a, PV.b] = [PV.b, PV.a]; pvSave(); renderPV(); };
    document.querySelectorAll("#pv-scope button").forEach((x) => { x.classList.toggle("on", x.dataset.s === PV.scope); x.onclick = () => { PV.scope = x.dataset.s; pvSave(); renderPV(); }; });
  }
  pick.querySelectorAll(".pv-chip").forEach((c) => (c.onclick = () => {
    if (PV.mode === "one") PV.one = c.dataset.p; else pvToggle(c.dataset.p, true);
    pvSave(); renderPV(); }));
}
// YouTube vs the other platforms: of the songs YouTube gave us each day, the share each platform has found (now, by the
// next day, within a week). Young days are left blank for a measure whose wait has not passed (never shown as low).
const PV_RATIO = { now: ["Found so far", "on the platform now"], d1: ["Within a day", "linked by the day after YouTube"], d7: ["Within a week", "linked within 7 days of YouTube"] };
function pvVsYoutube(d, pv, chip, pick, body) {
  const V = pv.vs_youtube, N = pv.names, k = PV.ratio || "d1";
  const sel = V.platforms.filter((p) => PV.sel.includes(p));
  pick.innerHTML = `<span class="pv-hint">Platforms</span>` + V.platforms.map((p) => chip(p, PV.sel.includes(p))).join("") +
    `<span class="spacer"></span><div class="seg" id="pv-ratio">${Object.entries(PV_RATIO).map(([key, [label]]) => `<button class="small${key === k ? " on" : ""}" data-r="${key}">${label}</button>`).join("")}</div>`;
  const pct = (n, of) => n == null ? null : of ? Math.round((1000 * n) / of) / 10 : null;
  const ov = (p) => V.overall[p][k];
  body.innerHTML = `<div class="pv-stats" style="grid-template-columns:repeat(${Math.min(4, sel.length + 1)}, minmax(0, 1fr))">
      <div class="pv-stat" style="--pc:${pvColor("youtube")}"><span>YouTube found</span><b>${fmt(V.found)}</b><small>new songs in ${d.days} days (the base: 100%)</small></div>
      ${sel.map((p) => `<div class="pv-stat"><span>${esc(N[p])} · ${esc(PV_RATIO[k][0].toLowerCase())}</span><b>${ov(p) == null ? "–" : ov(p) + "%"}</b>
        <small>${k === "now" ? `${fmt(V.overall[p].now_n)} of ${fmt(V.found)} songs` : PV_RATIO[k][1]}</small>
        <i class="pv-meter"><i style="width:${ov(p) || 0}%;background:${pvColor(p)}"></i></i></div>`).join("")}
    </div>
    <div class="pv-ch-title" style="margin-top:14px">Share of YouTube's new songs found, ${esc(PV_RATIO[k][1])} <span class="muted">per day of discovery on YouTube</span></div>
    <div class="chart" id="pv-c1"></div>
    <div class="table-wrap" style="margin-top:12px;max-height:520px"><table class="pv-table"><thead><tr><th>Day (found on YouTube)</th><th class="num">YouTube</th>
      ${V.platforms.map((p) => `<th class="num"><span class="pv-dot" style="--pc:${pvColor(p)}"></span>${esc(N[p])}</th>`).join("")}</tr></thead><tbody>${
      [...V.rows].reverse().map((r) => `<tr><td style="white-space:nowrap"><b>${esc(r.day)}</b>${r.day === d.today ? ' <span class="pill run" style="padding:0 6px"><span class="dot"></span>today</span>' : ""}</td>
        <td class="num"><b>${fmt(r.found)}</b></td>${V.platforms.map((p) => { const n = r.p[p][k], v = pct(n, r.found);
          return !r.found ? '<td class="num muted">–</td>' : n == null ? '<td class="num muted" title="too early to judge">…</td>'
            : `<td class="num${PV.sel.includes(p) ? "" : " muted"}">${v}% <span class="muted" style="font-size:11px">${fmt(n)}</span></td>`; }).join("")}</tr>`).join("")}
      </tbody><tfoot><tr><td><b>${d.days} days</b></td><td class="num"><b>${fmt(V.found)}</b></td>${V.platforms.map((p) => `<td class="num"><b>${ov(p) == null ? "–" : ov(p) + "%"}</b></td>`).join("")}</tr></tfoot></table></div>
    <div class="muted" style="font-size:12px;margin-top:8px">100% = every song YouTube gave us that day is on that platform. "…" = too early to judge (a day for "within a day", a week for "within a week").
      A song counts on a platform once linked there (auto, approved or confirmed); songs released before the period are left out.</div>`;
  multiLine($("#pv-c1"), sel.map((p) => ({ name: N[p], color: pvColor(p), total: true,
    points: V.rows.filter((r) => r.found && r.p[p][k] != null).map((r) => ({ x: r.day, y: pct(r.p[p][k], r.found) })) })),
    { height: 260, yfmt: (v) => `${Math.round(v)}%`, vfmt: (v) => `${v}%`, dots: sel.length < 4, empty: "Pick one or more platforms above." });
  document.querySelectorAll("#pv-ratio button").forEach((b) => (b.onclick = () => { PV.ratio = b.dataset.r; pvSave(); renderPV(); }));
}
function pvToggle(p, noRender) {
  PV.sel = PV.sel.includes(p) ? PV.sel.filter((x) => x !== p) : [...PV.sel, p];
  pvSave(); if (!noRender) renderPV();
}
document.querySelectorAll("#pv-mode button").forEach((b) => (b.onclick = () => { PV.mode = b.dataset.m; pvSave(); renderPV(); }));
document.querySelectorAll("#pv-metric button").forEach((b) => (b.onclick = () => { PV.metric = b.dataset.k; pvSave(); renderPV(); }));
