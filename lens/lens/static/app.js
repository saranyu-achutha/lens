"use strict";
const $ = (s, el = document) => el.querySelector(s);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = (n) => (n == null ? "–" : Number(n).toLocaleString("en-IN"));
const compact = (n) => n == null ? "–" : Math.abs(n) >= 1e9 ? `${(n / 1e9).toFixed(2)}B` : Math.abs(n) >= 1e7 ? `${(n / 1e6).toFixed(0)}M` : Math.abs(n) >= 1e6 ? `${(n / 1e6).toFixed(1)}M`
  : Math.abs(n) >= 1e4 ? `${(n / 1e3).toFixed(0)}K` : Math.abs(n) >= 1e3 ? `${(n / 1e3).toFixed(1)}K` : String(Math.round(n));
const dur = (ms) => { if (!ms) return "–"; const t = Math.round(ms / 1000); return `${Math.floor(t / 60)}:${String(t % 60).padStart(2, "0")}`; };
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const monthLabel = (ym) => (ym && ym.length >= 7 ? `${MONTHS[+ym.slice(5, 7) - 1]} ${ym.slice(2, 4)}` : ym || "?");

async function api(path, body) {
  const opt = body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
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
const PAGES = ["overview", "platforms", "reports", "daily", "catalogue", "explore", "insights", "review", "runs", "outputs", "sources", "settings"];
const hashParams = () => new URLSearchParams(location.hash.split("?")[1] || "");
function route() {
  const page = (location.hash.slice(1).split("?")[0]) || "overview";
  const p = PAGES.includes(page) ? page : "overview";
  document.querySelectorAll("section.page").forEach((s) => s.classList.toggle("active", s.id === `page-${p}`));
  document.querySelectorAll("a.nav").forEach((a) => a.classList.toggle("active", a.getAttribute("href") === `#${p}`));
  ({ overview: loadOverview, platforms: loadPlatform, reports: loadReport, daily: loadDaily, catalogue: loadCatalogue, explore: loadExplore, insights: loadInsights, review: loadReview, runs: loadRuns, outputs: loadOutputs, sources: loadSources, settings: loadSettings })[p]();
  const track = new URLSearchParams(location.hash.split("?")[1] || "").get("track");
  if (track) openTrack(track);
  const meta = new URLSearchParams(location.hash.split("?")[1] || "").get("meta");  // e.g. #explore?meta=singer:arijit singh
  if (meta && meta.includes(":")) openMeta(meta.split(":")[0], meta.split(":").slice(1).join(":"));
  const pl = new URLSearchParams(location.hash.split("?")[1] || "").get("play");  // e.g. #catalogue?play=youtube:VIDEO_ID
  if (pl && pl.includes(":")) play(pl.split(":")[0], pl.split(":")[1], "");
}
window.addEventListener("hashchange", route);

/* ---------- Status / header ---------- */
async function refreshStatus() {
  try { STATUS = await api("/api/status"); } catch { return; }
  const j = STATUS.job, pill = $("#job-pill");
  if (j.running) { pill.className = "pill run"; pill.innerHTML = `<span class="dot"></span>Running: ${esc(STATUS.jobs[j.job] || j.job)}`; }
  else if (j.status === "failed") { pill.className = "pill bad"; pill.innerHTML = `<span class="dot"></span>Last run failed`; }
  else { pill.className = "pill"; pill.innerHTML = `<span class="dot"></span>Idle`; }
  const sp = $("#sched-pill");
  const sc = STATUS.config, parts = [];
  if (sc.schedule.enabled) parts.push(`pipeline daily ${esc(sc.schedule.time)}`);
  if (sc.youtube.enabled) parts.push(`YouTube every ${sc.youtube.every_hours} h`);
  sp.className = "pill" + (parts.length ? " ok" : "");
  sp.innerHTML = `<span class="dot"></span>${parts.length ? parts.join(" · ") : "Schedule off"}`;
  $("#top-lastok").textContent = STATUS.last_ok ? `Last successful run ${new Date(STATUS.last_ok).toLocaleString()}` : "";
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

function barChart(el, data, { height = 230, onClick, tip } = {}) {
  el.innerHTML = "";
  if (!data.length) { el.innerHTML = `<div class="empty">No data</div>`; return; }
  const W = el.clientWidth || 520, H = height, m = { t: 18, r: 8, b: 26, l: 36 };
  const iw = W - m.l - m.r, ih = H - m.t - m.b;
  const max = niceMax(Math.max(...data.map((d) => d.value)));
  const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, height: H, role: "img", "aria-label": "bar chart" }, el);
  const g = svgEl("g", { class: "grid" }, svg), ax = svgEl("g", { class: "axis" }, svg);
  for (let i = 0; i <= 4; i++) {
    const y = m.t + ih - (ih * i) / 4;
    svgEl("line", { x1: m.l, x2: W - m.r, y1: y, y2: y }, g);
    svgEl("text", { x: m.l - 8, y: y + 4, "text-anchor": "end" }, ax).textContent = fmt((max * i) / 4);
  }
  const step = iw / data.length, bw = Math.max(4, Math.min(44, step - 2 - step * 0.25));
  const tt = tooltipFor(el);
  const maxIdx = data.reduce((bi, d, i) => (d.value > data[bi].value ? i : bi), 0);
  data.forEach((d, i) => {
    const h = (d.value / max) * ih, x = m.l + step * i + (step - bw) / 2, y = m.t + ih - h;
    const bar = svgEl("path", { d: barPath(x, y, bw, Math.max(h, 0.5)), fill: "var(--series-1)" }, svg);
    if (i === maxIdx) svgEl("text", { x: x + bw / 2, y: y - 6, "text-anchor": "middle", fill: "var(--text-secondary)", "font-size": 11 }, svg).textContent = fmt(d.value);
    if (data.length <= 14 || i % Math.ceil(data.length / 12) === 0)
      svgEl("text", { x: x + bw / 2, y: H - 8, "text-anchor": "middle" }, ax).textContent = d.label;
    const hit = svgEl("rect", { x: m.l + step * i, y: m.t, width: step, height: ih, fill: "transparent", style: onClick ? "cursor:pointer" : "" }, svg);
    hit.addEventListener("mouseenter", () => {
      bar.setAttribute("fill", "#5598e7");
      tt.innerHTML = tip ? tip(d) : `${esc(d.label)}: <b>${fmt(d.value)}</b>`;
      tt.style.left = `${((x + bw / 2) / W) * 100}%`; tt.style.top = `${(y / H) * el.clientHeight}px`; tt.style.display = "block";
    });
    hit.addEventListener("mouseleave", () => { bar.setAttribute("fill", "var(--series-1)"); tt.style.display = "none"; });
    if (onClick) hit.addEventListener("click", () => onClick(d));
  });
}

function lineChart(el, points, { height = 200, yMax = null, label = "Popularity" } = {}) {
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
  svgEl("path", { d: points.map((p, i) => `${i ? "L" : "M"}${X(p)},${Y(p)}`).join(""), fill: "none", stroke: "var(--series-1)", "stroke-width": 2, "stroke-linejoin": "round" }, svg);
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
const trackCell = (r) => `<td><div class="t1">${esc(r.title)}${r.music_label ? `<span class="label-tag">${esc(r.music_label)}</span>` : ""}</div>
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
    ["Total YouTube views", compact(k.views), `about ${compact(k.songs ? k.views / k.songs : 0)} per song`],
    ["Views in the last day", k.views_delta ? `+${compact(k.views_delta)}` : "–",
      k.views_delta ? `${fmt(k.growing)} songs still growing` : "shown after the next daily reading"],
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
    ov.labels.map((l) => `<tr class="label-row" data-label="${esc(l.label)}" style="cursor:pointer"><td>${esc(l.label)}</td>${nums(l)}</tr>` +
      ov.channels.filter((c) => c.label && c.label === l.label).map((c) => `<tr class="ch-row" data-ch="${esc(c.channel)}" style="cursor:pointer"><td>${esc(c.channel)}</td>${nums(c)}</tr>`).join("")).join("") + "</tbody>";
  const tbl = (rows, extra) => `<thead><tr><th>Play</th><th>Song</th><th>First upload</th><th>Spotify</th>${extra ? "<th class='num'>Views gained</th>" : ""}<th class="num">YouTube views</th></tr></thead><tbody>` +
    rows.map((r) => `<tr class="click" data-id="${r.id}">${playBtns(r)}${trackCell(r)}<td class="muted">${esc(r.song_date)}</td><td>${statusChip(r.release_status)}</td>${extra ? `<td class="num delta-up">+${compact(r.yt_delta)}</td>` : ""}${viewsCell(r.yt_views)}</tr>`).join("") + "</tbody>";
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
const PF = { p: "youtube" };
async function loadPlatform() {
  document.querySelectorAll("#pf-tabs .tab").forEach((b) => b.classList.toggle("active", b.dataset.pf === PF.p));
  const d = await api(`/api/platform?p=${PF.p}`);
  $("#pf-window").textContent = `Songs first uploaded ${d.since} → ${d.until} (${fmt(d.songs)} songs)`;
  const num = (v) => typeof v === "number" ? (Math.abs(v) >= 1e5 ? compact(v) : fmt(v)) : (v == null ? "–" : v);
  $("#pf-kpis").innerHTML = d.kpis.map(([l, v, x]) => `<div class="card kpi"><div class="label">${esc(l)}</div><div class="value">${esc(num(v))}</div><div class="sub">${esc(x || "")}</div></div>`).join("");
  const big = Math.max(0, ...d.chart.bars.map((b) => b.value || 0)) >= 1e6;  // show large counts in millions
  $("#pf-chart-title").textContent = d.chart.title + (big ? " (millions)" : "");
  barChart($("#pf-chart"), d.chart.bars.map((b) => ({ label: b.label.length > 10 ? b.label.slice(0, 9) + "…" : b.label, full: b.label,
    value: big ? Math.round((b.value || 0) / 1e5) / 10 : b.value, raw: b.value })), { tip: (b) => `<b>${esc(b.full)}</b><br>${num(b.raw)}` });
  $("#pf-table-title").textContent = d.table.title;
  $("#pf-table").innerHTML = d.table.rows.length ? `<thead><tr>${d.table.cols.map((c, i) => `<th class="${i ? "num" : ""}">${esc(c)}</th>`).join("")}</tr></thead><tbody>` +
    d.table.rows.map(([id, ...cells]) => `<tr class="click" data-id="${id}">${cells.map((c, i) => i ? `<td class="num">${typeof c === "number" ? num(c) : esc(c ?? "–")}</td>`
      : `<td><div class="t1">${esc(c)}</div></td>`).join("")}</tr>`).join("") + "</tbody>"
    : `<tbody><tr><td class="empty">Nothing yet for this period.</td></tr></tbody>`;
}
document.querySelectorAll("#pf-tabs .tab").forEach((b) => (b.onclick = () => { PF.p = b.dataset.pf; loadPlatform(); }));
$("#pf-table").addEventListener("click", (e) => { const tr = e.target.closest("tr[data-id]"); if (tr) openTrack(tr.dataset.id); });

/* ---------- Reports: readings on two days and the change ---------- */
const RP = { from: "", to: "", label: "", sort: "yt_views_delta", q: "", offset: 0, limit: 100 };
const rpParams = (extra = {}) => new URLSearchParams({ from: RP.from, to: RP.to, label: RP.label, sort: RP.sort, q: RP.q, ...extra });
async function loadReport() {
  const d = await api(`/api/report?${rpParams({ offset: RP.offset, limit: RP.limit })}`);
  RP.from = d.from || ""; RP.to = d.to || "";
  const opts = (sel) => d.days.slice().reverse().map((x) => `<option ${x === sel ? "selected" : ""}>${x}</option>`).join("");
  $("#rp-from").innerHTML = opts(d.from); $("#rp-to").innerHTML = opts(d.to);
  if ($("#rp-label").options.length === 1 && STATUS) (STATUS.config.youtube.labels || []).forEach((l) => $("#rp-label").insertAdjacentHTML("beforeend", `<option>${esc(l.name)}</option>`));
  $("#rp-sort").innerHTML = d.metrics.map((m) => `<option value="${m.key}_delta" ${RP.sort === m.key + "_delta" ? "selected" : ""}>${esc(m.label)} change</option>`).join("");
  $("#rp-csv").href = `/api/report.csv?${rpParams()}`;
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
    (d.rows.map((r) => `<tr class="click" data-id="${r.id}"><td><div class="t1">${esc(r.title)}</div><div class="t2">${esc(r.channel || "")} · ${esc(r.song_date || "")}</div></td><td>${esc(r.label || "")}</td>` +
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
const DD = { day: "", label: "", sort: "yt_views_delta", q: "", song: "", offset: 0, limit: 100 };
const GAIN_KEYS = ["yt_views", "yt_likes", "yt_comments", "ytm_plays", "sp_plays", "js_plays"];
const ddChange = (r, k) => GAIN_KEYS.includes(k) ? r[k + "_gain"] : (r[k] != null && r["prev_" + k] != null ? r[k] - r["prev_" + k] : null);
async function loadDaily() {
  const song = DD.song;
  const d = await api(`/api/daily?${new URLSearchParams(song ? { song } : { day: DD.day, label: DD.label, sort: DD.sort, q: DD.q, offset: DD.offset, limit: DD.limit })}`);
  document.querySelectorAll("#dd-controls .dd-day").forEach((e) => (e.style.display = song ? "none" : ""));
  $("#dd-back").style.display = $("#dd-song").style.display = song ? "" : "none";
  $("#dd-pager").style.display = song ? "none" : "";
  const sign = (v) => v == null ? '<span class="muted">–</span>' : (v > 0 ? "+" : "") + (Math.abs(v) >= 1e5 ? compact(v) : fmt(v));
  const val = (v) => v == null ? '<span class="muted">–</span>' : (Math.abs(v) >= 1e5 ? compact(v) : fmt(v));
  const head = d.cols.map((c) => `<th class="num">${esc(c.label)}</th><th class="num">Change</th>`).join("");
  const cells = (r) => d.cols.map((c) => `<td class="num">${val(r[c.key])}</td><td class="num ${(ddChange(r, c.key) || 0) > 0 ? "delta-up" : ""}">${sign(ddChange(r, c.key))}</td>`).join("");
  if (song) {
    $("#dd-song").textContent = d.song ? d.song.title : "Song";
    $("#dd-csv").href = `/api/daily.csv?song=${song}`;
    $("#dd-table").innerHTML = `<thead><tr><th>Day</th><th class="num">Uploads read</th>${head}</tr></thead><tbody>` +
      (d.rows.map((r) => `<tr><td>${esc(r.day)}</td><td class="num">${fmt(r.yt_uploads_read)}</td>${cells(r)}</tr>`).join("") || `<tr><td class="empty" colspan="${2 + d.cols.length * 2}">No daily data for this song yet.</td></tr>`) + "</tbody>";
    return;
  }
  DD.day = d.day || "";
  $("#dd-day").innerHTML = d.days.slice().reverse().map((x) => `<option ${x === d.day ? "selected" : ""}>${x}</option>`).join("");
  if ($("#dd-label").options.length === 1 && STATUS) (STATUS.config.youtube.labels || []).forEach((l) => $("#dd-label").insertAdjacentHTML("beforeend", `<option>${esc(l.name)}</option>`));
  $("#dd-sort").innerHTML = d.cols.flatMap((c) => [[c.key + "_delta", c.label + " change"], [c.key, c.label]])
    .map(([k, l]) => `<option value="${k}" ${DD.sort === k ? "selected" : ""}>${esc(l)}</option>`).join("");
  $("#dd-csv").href = `/api/daily.csv?${new URLSearchParams({ day: DD.day, label: DD.label, sort: DD.sort, q: DD.q })}`;
  $("#dd-sub").textContent = `Totals for ${d.day || "–"}${d.prev ? `; change compared with ${d.prev}` : ""}. Stored indefinitely; click a song for its full day-by-day history.`;
  $("#dd-table").innerHTML = `<thead><tr><th>Song</th><th>Label</th><th class="num">Uploads read</th>${head}</tr></thead><tbody>` +
    (d.rows.map((r) => `<tr class="click" data-song="${r.id}"><td><div class="t1">${esc(r.title)}</div><div class="t2">${esc(r.yt_channel || "")} · ${esc(r.song_date || "")}</div></td>
      <td>${esc(r.music_label || "")}</td><td class="num">${fmt(r.yt_uploads_read)}</td>${cells(r)}</tr>`).join("") || `<tr><td class="empty" colspan="${3 + d.cols.length * 2}">No data for this day.</td></tr>`) + "</tbody>";
  $("#dd-count").textContent = d.total ? `${fmt(DD.offset + 1)}–${fmt(Math.min(DD.offset + DD.limit, d.total))} of ${fmt(d.total)}` : "0";
  $("#dd-prev").disabled = DD.offset === 0; $("#dd-next").disabled = DD.offset + DD.limit >= d.total;
}
["day", "label", "sort"].forEach((k) => ($(`#dd-${k}`).onchange = (e) => { DD[k] = e.target.value; DD.offset = 0; loadDaily(); }));
let ddTimer;
$("#dd-q").addEventListener("input", (e) => { clearTimeout(ddTimer); ddTimer = setTimeout(() => { DD.q = e.target.value; DD.offset = 0; loadDaily(); }, 300); });
$("#dd-prev").onclick = () => { DD.offset = Math.max(0, DD.offset - DD.limit); loadDaily(); };
$("#dd-next").onclick = () => { DD.offset += DD.limit; loadDaily(); };
$("#dd-back").onclick = () => { DD.song = ""; loadDaily(); };
$("#dd-table").addEventListener("click", (e) => { const tr = e.target.closest("tr[data-song]"); if (tr) { DD.song = tr.dataset.song; loadDaily(); } });

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
const PLATFORM_LINKS = [["ytm_url", "ym", "YM", "YouTube Music"], ["js_url", "js", "J", "JioSaavn"], ["apple_url", "am", "A", "Apple Music"], ["deezer_url", "dz", "D", "Deezer"]];
const playBtns = (r) => `<td class="play-cell">${r.yt_video_id ? `<button class="pbtn yt" data-play="youtube" data-pid="${esc(r.yt_video_id)}" data-ptitle="${esc(r.title)}" title="Play on YouTube (${esc(r.yt_video_id)})">▶</button>` : `<span class="pbtn off">▶</span>`}${
  r.spotify_id && ["auto", "approved"].includes(r.spotify_state) ? `<button class="pbtn sp" data-play="spotify" data-pid="${esc(r.spotify_id)}" data-ptitle="${esc(r.title)}" title="Play on Spotify (${esc(r.spotify_id)})">▶</button>` : `<span class="pbtn off" title="not on Spotify">▶</span>`}${PLATFORM_LINKS.map(([k, cls, txt, name]) => r[k]
    ? `<a class="pbtn ${cls}" href="${esc(r[k])}" target="_blank" rel="noopener" title="Open on ${name}${k === "js_url" && r.js_plays != null ? ` (${fmt(r.js_plays)} plays)` : ""}">${txt}</a>`
    : `<span class="pbtn off" title="not on ${name} yet">${txt}</span>`).join("")}</td>`;

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
      grp("Labels", d.labels, (r) => `<b>${esc(r.label)}</b>`) + grp("Languages (5+ songs)", d.languages, (r) => esc(r.language)) +
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
function closeDrawer() { $("#drawer").classList.remove("open"); $("#drawer-bg").classList.remove("open"); }
async function openTrack(id) {
  const d = await api(`/api/track?id=${id}`);
  const t = d.track, r = d.releases[0] || {};
  const yt = d.items.filter((i) => i.platform === "youtube");
  const sp = d.items.find((i) => i.platform === "spotify");
  const spAll = d.items.filter((i) => i.platform === "spotify" && ["auto", "approved"].includes(i.review_state));
  const totV = d.by_type.reduce((a, b) => a + b.views, 0);
  const dz = d.items.find((i) => i.platform === "deezer");
  const series = (platform, metric) => {
    const byDay = {};
    d.history.filter((h) => h.platform === platform && h.metric === metric).forEach((h) => { byDay[h.capture_date] = (byDay[h.capture_date] || 0) + h.value; });
    return Object.keys(byDay).sort().map((x) => ({ x, y: byDay[x] }));
  };
  const METRICS = [["views", "YouTube views", "youtube"], ["likes", "YouTube likes", "youtube"], ["comments", "YouTube comments", "youtube"], ["rank", "Deezer rank", "deezer"]];
  if (d.history.some((h) => h.platform === "spotify" && h.metric === "plays")) METRICS.push(["plays", "Spotify plays", "spotify"]);
  if (d.history.some((h) => h.platform === "ytmusic")) METRICS.push(["ytm", "YouTube Music plays", "ytmusic", "plays"]);
  if (d.history.some((h) => h.platform === "jiosaavn")) METRICS.push(["jsp", "JioSaavn plays", "jiosaavn", "plays"]);
  if (d.chart) METRICS.push(["chart_rank", "Spotify India rank", "spotify_chart"]);
  const ch = d.chart;
  $("#drawer").innerHTML = `
    <div class="row"><div style="flex:1"><h1>${esc(t.title)}</h1><div class="muted">${esc(t.artists || t.yt_channel || "")}</div></div><button class="small" id="dr-close">✕</button></div>
    <div style="margin-top:8px">${statusChip(t.release_status)}</div>
    <dl class="kv">
      <dt>First on YouTube</dt><dd>${esc(t.first_published ? t.first_published.slice(0, 10) : "–")}${t.yt_channel ? ` · ${esc(t.yt_channel)}` : ""}</dd>
      <dt>Spotify release</dt><dd>${r.title ? `${esc(r.title)} <span class="muted">(${esc(r.album_type)})</span> · ${esc(r.release_date)}` : '<span class="muted">not linked</span>'}</dd>
      <dt>ISRC</dt><dd class="mono">${esc(t.isrc || "–")}</dd>
      <dt>Length</dt><dd>${dur(t.duration_ms)}${t.explicit ? " · explicit" : ""}</dd>
      <dt>Spotify track id</dt><dd>${t.spotify_id && ["auto", "approved"].includes(t.spotify_state)
        ? `<span class="mono">${esc(t.spotify_id)}</span> <button class="pbtn sp" data-play="spotify" data-pid="${esc(t.spotify_id)}" data-ptitle="${esc(t.title)}">▶</button> <a href="https://open.spotify.com/track/${esc(t.spotify_id)}" target="_blank" rel="noopener">open ↗</a>`
        : '<span class="muted">–</span>'}</dd>
      <dt>YouTube video id</dt><dd>${t.yt_video_id ? `<span class="mono">${esc(t.yt_video_id)}</span> <button class="pbtn yt" data-play="youtube" data-pid="${esc(t.yt_video_id)}" data-ptitle="${esc(t.title)}">▶</button> <a href="https://www.youtube.com/watch?v=${esc(t.yt_video_id)}" target="_blank" rel="noopener">open ↗</a> <span class="muted">(most viewed)</span>` : '<span class="muted">–</span>'}</dd>
      <dt>Listen on</dt><dd class="listen-on">${[
        [t.yt_video_id && `https://www.youtube.com/watch?v=${t.yt_video_id}`, "YouTube", "yt"], [t.ytm_url, "YouTube Music", "ym"],
        [t.spotify_id && ["auto", "approved"].includes(t.spotify_state) && `https://open.spotify.com/track/${t.spotify_id}`, "Spotify", "sp"],
        [t.js_url, "JioSaavn", "js"], [t.apple_url, "Apple Music", "am"], [t.deezer_url, "Deezer", "dz"], [t.mb_url, "MusicBrainz", "mb"]]
        .map(([u, n, c]) => u ? `<a class="lo ${c}" href="${esc(u)}" target="_blank" rel="noopener">${n} ↗</a>` : `<span class="lo off" title="not found on ${n} yet">${n}</span>`).join(" ")}</dd>
      <dt>JioSaavn</dt><dd>${t.js_url ? `${esc(t.js_title || t.title)}${t.js_plays != null ? ` · <b>${fmt(t.js_plays)}</b> plays` : ""} <a class="pbtn js" href="${esc(t.js_url)}" target="_blank" rel="noopener" title="Open on JioSaavn">J</a> <a href="${esc(t.js_url)}" target="_blank" rel="noopener">open ↗</a>` : '<span class="muted">not found yet</span>'}</dd>
      ${t.deezer_url ? `<dt>Deezer</dt><dd><a href="${esc(t.deezer_url)}" target="_blank" rel="noopener">open ↗</a></dd>` : ""}
      ${ch ? `<dt>Spotify India chart</dt><dd>best <b>#${ch.best_rank}</b> on ${esc(ch.best_date)} · ${fmt(ch.days)} ${ch.chart.endsWith("weekly") ? "weeks in the weekly" : "days in the daily"} Top 200 <span class="muted">(${esc(ch.first)} → ${esc(ch.last)})</span></dd>` : ""}
    </dl>
    ${sp && sp.review_state === "pending" ? `<div class="card" style="margin-bottom:16px"><b>Possible Spotify match:</b> <a href="${esc(sp.url)}" target="_blank" rel="noopener">${esc(sp.title)}</a> (${esc(sp.published || "")}) · score ${(sp.match_score || 0).toFixed(2)}
      <div class="row" style="margin-top:8px"><button class="small primary" data-decide="approve" data-item="${sp.id}">✓ Same song</button><button class="small" data-decide="reject" data-item="${sp.id}">✕ Different</button></div></div>` : ""}
    <h3>Compared with similar songs</h3><div class="bench" id="dr-bench"><span class="muted">…</span></div>
    <h3>Song details</h3>
    ${(() => {
      const m = d.meta || {};
      const row = (f) => m[f] && m[f].length ? `<dt>${esc(STATUS.meta_fields[f] || f)}</dt><dd>${m[f].map((v) => metaChip(f, v)).join(" ")} ${srcTag([...new Set(m[f].flatMap((v) => v.source.split(",")))].join(","))}</dd>` : "";
      const plain = (l, v, src) => v ? `<dt>${l}</dt><dd>${esc(v)} ${src ? srcTag(src) : ""}</dd>` : "";
      const rel = ["film", "album", "song_title", "language", "category", "occasion", "label", "channel", "music_label", "apple_album", "genre", "release_date"].map(row).join("")
        + plain("Spotify album", r.title ? `${r.title} (${r.album_type || ""})` : "", "spotify") + plain("Release date", r.release_date, "spotify")
        + plain("ISRC", t.isrc, r.title ? "spotify" : "musicbrainz") + plain("Length", t.duration_ms ? dur(t.duration_ms) + (t.explicit ? " · explicit" : "") : "", t.isrc ? "spotify" : "");
      const credits = ["singer", "music_director", "lyricist", "cast", "director", "producer", "video_director", "arranger", "mix_master", "choreographer", "spotify_artist", "apple_artist"].map(row).join("");
      return `<div class="card" style="margin-bottom:16px"><h4 class="muted" style="margin:0 0 8px;font-weight:500">Release</h4><dl class="meta-grid">${rel}</dl>
        <h4 class="muted" style="margin:6px 0 8px;font-weight:500">Credits</h4><dl class="meta-grid">${credits || "<dd class='muted'>No credits found in the descriptions or on Spotify.</dd>"}</dl>
        <div class="muted" style="font-size:11.5px">Click any name to see its metrics across songs. Sources are shown next to each field.</div></div>`;
    })()}
    <h3>YouTube by video type</h3>
    <div class="card flush" style="margin-bottom:16px">${d.by_type.length ? `<table><thead><tr><th>Type</th><th class="num">Videos</th><th class="num">Views</th><th class="num">Share</th><th class="num">Likes</th><th class="num">Comments</th></tr></thead><tbody>
      ${d.by_type.map((b) => `<tr><td>${esc((VTYPES.find(([t]) => t === b.type) || [0, b.type])[1])}</td><td class="num">${fmt(b.videos)}</td><td class="num">${compact(b.views)}</td>
        <td class="num">${totV ? Math.round((100 * b.views) / totV) : 0}%</td><td class="num">${compact(b.likes)}</td><td class="num">${compact(b.comments)}</td></tr>`).join("")}
      <tr class="label-row"><td>All versions</td><td class="num">${fmt(d.by_type.reduce((a, b) => a + b.videos, 0))}</td><td class="num">${compact(totV)}</td><td class="num">100%</td>
        <td class="num">${compact(d.by_type.reduce((a, b) => a + b.likes, 0))}</td><td class="num">${compact(d.by_type.reduce((a, b) => a + b.comments, 0))}</td></tr></tbody></table>`
      : `<div class="empty">No YouTube video linked.</div>`}</div>
    ${spAll.length > 1 ? `<h3>Spotify versions (${spAll.length})</h3><div class="card flush" style="margin-bottom:16px"><table><tbody>${spAll.map((i) => {
        const md = i.match_detail || {};
        return `<tr><td style="width:34px"><button class="pbtn sp" data-play="spotify" data-pid="${esc(i.platform_id)}" data-ptitle="${esc(i.title)}">▶</button></td>
        <td><a href="${esc(i.url)}" target="_blank" rel="noopener">${esc(i.title)}</a><div class="t2"><span class="mono">${esc(i.platform_id)}</span>${md.album ? ` · ${esc(md.album)} (${esc(md.album_type || "")})` : ""} · ${esc(i.published || "")}${md.isrc ? ` · ISRC ${esc(md.isrc)}` : ""} · ${i.match_method === "same_recording" ? "same recording" : "main match"}</div></td></tr>`;
      }).join("")}</tbody></table></div>` : ""}
    ${d.merged_names.length ? `<p class="muted" style="font-size:12px">Merged from: ${d.merged_names.map((k) => esc(k.split("::").pop().split("|")[0])).join(", ")} (undo under Review → Decision history)</p>` : ""}
    <div class="row" style="justify-content:space-between"><h3 style="margin:0">Trend</h3>
      <div class="seg" id="dr-metric">${METRICS.map(([m, l], i) => `<button class="small${i ? "" : " on"}" data-m="${m}">${l.replace("YouTube ", "")}</button>`).join("")}</div></div>
    <div class="card" style="padding:12px 12px 4px;margin-top:8px"><div class="chart" id="dr-chart"></div><div class="muted" id="dr-chart-note" style="font-size:12px;padding:0 4px 8px"></div></div>
    <h3 style="margin-top:20px">YouTube videos (${yt.length})</h3>
    <div class="card flush">${yt.length ? `<table><tbody>${yt.map((i) => `<tr><td style="width:34px"><button class="pbtn yt" data-play="youtube" data-pid="${esc(i.platform_id)}" data-ptitle="${esc(i.title)}">▶</button></td><td><a href="${esc(i.url)}" target="_blank" rel="noopener">${esc(i.title)}</a>
        <div class="t2"><span class="mono">${esc(i.platform_id)}</span> · ${esc(i.channel)} · ${esc(i.asset_type)} · ${esc(i.published)} · ${stateChip(i.review_state, i.match_method)}</div></td>
        <td class="num" style="white-space:nowrap">${compact(i.views)} views<div class="t2">${compact(i.likes)} likes · ${compact(i.comments)} comments</div></td>
        <td style="white-space:nowrap">${i.review_state === "pending" ? `<button class="small" data-decide="approve" data-item="${i.id}">✓</button> ` : ""}<button class="small" data-decide="reject" data-item="${i.id}" title="Not this song">✕</button></td></tr>`).join("")}</tbody></table>`
      : `<div class="empty">No YouTube video linked.</div>`}</div>
    <p class="muted" style="font-size:12px;margin-top:14px">Spotify gives no play counts or popularity to this app (development mode); Deezer rank is Deezer's own popularity score (higher = more popular).</p>`;
  $("#drawer").classList.add("open"); $("#drawer-bg").classList.add("open");
  $("#dr-close").onclick = closeDrawer;
  api(`/api/insights/song?id=${id}`).then((b) => {
    const el = $("#dr-bench"); if (!el) return;
    const bm = b.benchmark;
    el.innerHTML = bm ? `<span class="pill">${compact(bm.song.velocity)} views/day · ${esc(bm.song.age_band)}</span>` + bm.comparisons.map((c) =>
      `<span class="pill ${c.percentile >= 70 ? "ok" : c.percentile < 30 ? "warn" : ""}" title="${fmt(c.songs)} songs">faster than ${c.percentile}% of ${esc(c.compared_with)}</span>`).join("")
      : `<span class="muted">Not in the current window or no views yet.</span>`;
  }).catch(() => {});
  const draw = (m) => {
    const [, label, platform, metric] = METRICS.find((x) => x[0] === m);  // optional 4th: stored metric name
    const pts = series(platform, metric || m);
    lineChart($("#dr-chart"), pts, { label });
    $("#dr-chart-note").textContent = pts.length === 1 ? "One reading so far; the line builds up with each sync." : "";
    document.querySelectorAll("#dr-metric button").forEach((b) => b.classList.toggle("on", b.dataset.m === m));
  };
  $("#dr-metric").onclick = (e) => { const b = e.target.closest("button[data-m]"); if (b) draw(b.dataset.m); };
  requestAnimationFrame(() => draw("views"));
  $("#drawer").querySelectorAll("[data-decide]").forEach((b) => (b.onclick = async () => {
    await decide(b.dataset.item, b.dataset.decide); openTrack(id); refreshStatus();
  }));
}
const stateChip = (st, method) => st === "song" ? "same song (grouped)" : st === "auto" ? "auto-matched" : st === "approved" ? (method === "manual" ? "linked manually" : "approved") : st === "pending" ? "<b>awaiting review</b>" : esc(st || "");
async function decide(itemId, action) {
  try { await api("/api/matching/decide", { item_id: +itemId, action }); toast(action === "approve" ? "Match approved" : "Match rejected"); }
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
  const pill = (s) => `<span class="pill ${{ ok: "ok", running: "run", failed: "bad", cancelled: "warn", interrupted: "warn", partial: "warn", quota_exceeded: "warn" }[s] || ""}"><span class="dot"></span>${esc(s)}</span>`;
  const secs = (a, b) => (a && b ? `${Math.round((Date.parse(b) - Date.parse(a)) / 1000)}s` : "–");
  $("#runs-table").innerHTML = `<thead><tr><th>#</th><th>Job</th><th>Started</th><th class="num">Took</th><th>Status</th><th class="num">Releases</th><th class="num">Tracks</th><th class="num">API calls</th><th>Details</th></tr></thead><tbody>` +
    (runs.length ? runs.map((r) => `<tr><td class="muted">${r.id}</td><td>${esc(STATUS.jobs[r.job] || r.job)}${r.stats.trigger ? `<div class="t2">${esc(r.stats.trigger)}</div>` : ""}</td><td>${new Date(r.started_at).toLocaleString()}</td>
      <td class="num">${secs(r.started_at, r.finished_at)}</td><td>${pill(r.status)}</td><td class="num">${fmt(r.stats.releases)}</td><td class="num">${fmt(r.stats.tracks)}</td>
      <td class="num">${fmt(r.stats.api_calls)}</td><td>${r.stats.error ? `<span class="delta-down">${esc(r.stats.error.slice(0, 80))}</span>` : ""}${r.stats.stop_reason ? `<div class="t2">${esc(r.stats.stop_reason)}</div>` : ""}
      ${(r.stats.csvs || (r.stats.csv ? [r.stats.csv] : [])).map((f) => `<a href="#outputs?file=${encodeURIComponent(f)}">${f.includes("songs") ? "Songs CSV" : f.includes("youtube") ? "YouTube CSV" : "Spotify CSV"}</a> · `).join("")}<a href="#outputs?raw_run=${r.id}">Raw API</a>
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
  secretPill($("#sp-pill"), s.SPOTIFY_CLIENT_ID.set && s.SPOTIFY_CLIENT_SECRET.set, s.SPOTIFY_CLIENT_ID.set && s.SPOTIFY_CLIENT_SECRET.set ? "Configured" : "Needs credentials");
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
    await api("/api/secrets", { SPOTIFY_CLIENT_ID: $("#sp-id").value, SPOTIFY_CLIENT_SECRET: $("#sp-secret").value });
    $("#sp-secret").value = ""; toast("Spotify credentials saved"); loadSources();
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
const OUT = { file: null, offset: 0, limit: 100, q: "", total: 0 };
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
  if (p.get("file")) { OUT.file = p.get("file"); OUT.offset = 0; }
  if (p.get("raw_run")) { RAW.run = p.get("raw_run"); RAW.offset = 0; showTab("raw"); return; }
  showTab(p.get("tab") === "raw" ? "raw" : "files");
}
async function loadFiles() {
  const { files } = await api("/api/outputs");
  if (!OUT.file && files.length) OUT.file = files[0].name;
  $("#out-list").innerHTML = files.length ? files.map((f) => `<div class="file-item ${f.name === OUT.file ? "active" : ""}" data-name="${esc(f.name)}">
      <div class="name">${esc(f.name)}</div>
      <div class="meta">${fmt(f.rows)} rows · ${kb(f.size)} · ${new Date(f.modified).toLocaleString()}${f.run_id ? ` · run #${f.run_id}` : ""}</div></div>`).join("")
    : `<div class="empty">No output files yet. Each successful run writes one CSV.</div>`;
  if (OUT.file) previewFile(); else { $("#out-title").textContent = "No file selected"; $("#out-table").innerHTML = ""; $("#out-dl").style.display = "none"; }
}
$("#out-list").addEventListener("click", (e) => {
  const it = e.target.closest(".file-item"); if (!it) return;
  OUT.file = it.dataset.name; OUT.offset = 0;
  document.querySelectorAll(".file-item").forEach((x) => x.classList.toggle("active", x === it));
  previewFile();
});
async function previewFile() {
  let d;
  try { d = await api(`/api/outputs/preview?${new URLSearchParams({ name: OUT.file, offset: OUT.offset, limit: OUT.limit, q: OUT.q })}`); }
  catch (e) { $("#out-title").textContent = OUT.file; $("#out-table").innerHTML = `<tbody><tr><td class="empty">${esc(e.message)}</td></tr></tbody>`; return; }
  OUT.total = d.total;
  $("#out-title").textContent = d.name;
  const dl = $("#out-dl"); dl.style.display = ""; dl.href = `/api/outputs/download?name=${encodeURIComponent(d.name)}`;
  const urlCol = d.header.indexOf("url");
  $("#out-table").innerHTML = `<thead><tr><th class="num">#</th>${d.header.map((h) => `<th>${esc(h)}</th>`).join("")}</tr></thead><tbody>` +
    (d.rows.length ? d.rows.map((r, i) => `<tr><td class="num muted">${OUT.offset + i + 1}</td>${r.map((c, j) => j === urlCol && c
      ? `<td><a href="${esc(c)}" target="_blank" rel="noopener">open ↗</a></td>` : `<td style="white-space:nowrap">${esc(c)}</td>`).join("")}</tr>`).join("")
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

/* ---------- Boot ---------- */
refreshStatus().then(route);
setInterval(refreshStatus, 5000);
let rz; window.addEventListener("resize", () => { clearTimeout(rz); rz = setTimeout(() => location.hash.startsWith("#overview") || !location.hash ? loadOverview() : null, 200); });
