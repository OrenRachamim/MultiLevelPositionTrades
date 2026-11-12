"use strict";
/* MLPT dashboard - plain JS, polls the local API. */

const $ = (sel, root = document) => root.querySelector(sel);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function api(path, { method = "GET", body } = {}) {
  const opts = { method, headers: { "X-MLPT": "1" } };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (_) { /* not json */ }
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return res.json();
}

// ---------------------------------------------------------------- formatting
const usd = (v, digits = 2) => v == null ? "–" : "$" + Number(v).toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
const signed = (v) => v == null ? "–" : (v >= 0 ? "+" : "−") + "$" + Math.abs(v).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const pct = (v, digits = 2) => v == null ? "–" : (v >= 0 ? "+" : "−") + Math.abs(v).toFixed(digits) + "%";
const plain = (v) => v == null ? "–" : String(+Number(v).toFixed(2));
const cls = (v) => !v ? "" : v > 0 ? "pos" : "neg";
const fmtTime = (tz, opts) => new Intl.DateTimeFormat("en-GB", { timeZone: tz, hour12: false, ...opts });
const NY_FULL = fmtTime("America/New_York", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" });
const NY_CLOCK = fmtTime("America/New_York", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
const IL_CLOCK = fmtTime("Asia/Jerusalem", { hour: "2-digit", minute: "2-digit" });
const nyTime = (ts) => ts ? NY_FULL.format(new Date(ts * 1000)).replace(",", "") : "–";

const SESSION = { PRE: ["טרום-מסחר", "warn"], RTH: ["שעות מסחר", "on"], POST: ["אחרי המסחר", "warn"], CLOSED: ["שוק סגור", "off"], HOLIDAY: ["אין מסחר היום", "off"] };
const PHASE = { ENTERING: "בכניסה", INTRADAY: "תוך-יומי", SWING: "סווינג" };
const WSTATUS = { WATCHING: ["במעקב", "on"], ENTERED: ["בוצעה כניסה", ""], NO_SIGNAL: ["אין אות", "off"], SKIPPED: ["דילוג", "warn"] };
const REASON = { TP: "Take Profit", SL: "Stop Loss", MANUAL: "ידני", TIME: "מקס׳ ימים", EXTERNAL: "מחוץ למערכת" };

function table(el, head, rows, empty) {
  const th = head.map((h) => `<th class="${h.num ? "num" : ""}">${esc(h.t)}</th>`).join("");
  const body = rows.length ? rows.join("") : `<tr><td class="empty" colspan="${head.length}">${esc(empty)}</td></tr>`;
  el.innerHTML = `<thead><tr>${th}</tr></thead><tbody>${body}</tbody>`;
}

// ---------------------------------------------------------------------- state
// version of the files this page was loaded with; the server reports the current one
const ASSETS = new URL(document.currentScript.src, location.href).searchParams.get("v");

const state = { tab: "overview", status: null, chartSymbol: null, chartDate: null, listDate: null, settingsLoaded: false, busy: false };

// --------------------------------------------------------------------- header
function renderStatus(s) {
  if (ASSETS && s.assets && s.assets !== ASSETS && sessionStorage.getItem("mlpt-reloaded") !== s.assets) {
    sessionStorage.setItem("mlpt-reloaded", s.assets);     // the system was updated: load the new dashboard
    location.reload();
    return;
  }
  state.status = s;
  const mode = $("#mode");
  mode.textContent = { live: "LIVE · כסף אמיתי", paper: "PAPER", sim: "סימולציה" }[s.mode] || s.mode;
  mode.className = "badge " + s.mode;
  const conn = $("#conn");
  conn.className = "conn " + (s.connected ? "ok" : "bad");
  conn.lastElementChild.textContent = s.connected ? "מחובר לברוקר" : "אין חיבור לברוקר";
  const now = new Date(s.now * 1000);
  $("#t-ny").textContent = NY_CLOCK.format(now);
  $("#t-il").textContent = IL_CLOCK.format(now);
  const [st, sc] = SESSION[s.session] || [s.session, ""];
  $("#session").textContent = st;
  $("#session").className = "badge " + sc;
  const w = s.entry_window;
  $("#window").innerHTML = `חלון כניסה <span class="num">${esc(w.start)}–${esc(w.end)}</span>` + (w.open ? " · פתוח" : "");
  $("#window").className = "badge " + (w.open ? "on" : "off");
  const a = s.account;
  $("#k-net").textContent = a ? usd(a.net_liq) : "–";
  $("#k-cash").textContent = a ? usd(a.for_trading) : "–";
  const real = $("#k-real"), unreal = $("#k-unreal");
  real.textContent = signed(s.realized_today); real.className = "num " + cls(s.realized_today);
  unreal.textContent = signed(s.unrealized_pnl); unreal.className = "num " + cls(s.unrealized_pnl);
  const kill = $("#kill");
  kill.textContent = s.trading_enabled ? "השבת כניסות חדשות" : "הפעל כניסות חדשות";
  kill.className = "btn " + (s.trading_enabled ? "danger" : "go");
  const banner = $("#banner");
  const msgs = [];
  if (s.fatal) msgs.push(s.fatal);
  if (!s.trading_enabled) msgs.push("כניסות חדשות מושבתות. פוזיציות פתוחות ממשיכות להיות מנוהלות.");
  banner.hidden = !msgs.length;
  banner.textContent = msgs.join(" · ");
  $("#version").textContent = `MLPT ${s.version}` + (a ? ` · חשבון ${a.id}` : "") + (s.telegram ? " · טלגרם פעיל" : " · טלגרם לא מוגדר");
}

$("#kill").addEventListener("click", async () => {
  const enable = !state.status.trading_enabled;
  if (!enable && !confirm("להשבית כניסות חדשות? פוזיציות פתוחות ימשיכו להיות מנוהלות.")) return;
  await api("/api/trading", { method: "POST", body: { enabled: enable } });
  refresh();
});

// ------------------------------------------------------------------- overview
function watchRows(items, withRemove) {
  const p = (it, k, unit) => {
    const own = it[k] != null, v = it.effective[k];
    const text = !v ? "–" : unit === "$" ? usd(v, 0) : unit === "d" ? v : plain(v) + "%";
    return `<td class="num ${own ? "" : "default"}" title="${own ? "מהרשימה" : "ברירת מחדל"}">${text}</td>`;
  };
  return items.map((it) => {
    const [label, c] = WSTATUS[it.status] || [it.status, ""];
    return `<tr><td class="sym">${esc(it.symbol)}</td><td><span class="badge ${c}">${label}</span></td>
      <td class="num">${it.last == null ? "–" : usd(it.last)}</td>
      ${p(it, "intraday_tp_pct")}${p(it, "intraday_sl_pct")}${p(it, "swing_tp_pct")}${p(it, "swing_sl_pct")}
      ${p(it, "max_hold_days", "d")}${p(it, "position_pct")}${p(it, "max_position_usd", "$")}
      <td class="wrap">${esc(it.note)}</td>
      <td><button class="btn small ghost" data-chart="${esc(it.symbol)}">גרף</button>
      ${withRemove && it.status !== "ENTERED" ? `<button class="btn small ghost" data-remove="${it.id}">הסר</button>` : ""}</td></tr>`;
  });
}
const WATCH_HEAD = [{ t: "מניה" }, { t: "סטטוס" }, { t: "מחיר אחרון", num: 1 }, { t: "TP תוך-יומי", num: 1 }, { t: "SL תוך-יומי", num: 1 },
  { t: "TP סווינג", num: 1 }, { t: "SL סווינג", num: 1 }, { t: "מקס׳ ימים", num: 1 }, { t: "% מהתיק", num: 1 }, { t: "תקרה $", num: 1 }, { t: "הערה" }, { t: "" }];

function eventRows(items) {
  return items.map((e) => `<tr class="lvl-${esc(e.level)}"><td class="num">${nyTime(e.ts)}</td><td>${esc(e.kind)}</td>
    <td class="sym">${esc(e.symbol || "")}</td><td class="wrap">${esc(e.message)}</td></tr>`);
}
const EVENT_HEAD = [{ t: "זמן (ניו יורק)", num: 1 }, { t: "סוג" }, { t: "מניה" }, { t: "הודעה" }];

async function loadOverview() {
  const [pos, wl, ev] = await Promise.all([api("/api/positions?which=open"), api("/api/watchlist"), api("/api/events?limit=12")]);
  $("#open-count").textContent = pos.items.length ? `(${pos.items.length})` : "";
  table($("#open-table"), [{ t: "מניה" }, { t: "שלב" }, { t: "כמות", num: 1 }, { t: "כניסה", num: 1 }, { t: "אחרון", num: 1 },
    { t: "רווח/הפסד", num: 1 }, { t: "%", num: 1 }, { t: "TP", num: 1 }, { t: "SL", num: 1 }, { t: "ימים", num: 1 }, { t: "הגנה" }, { t: "" }],
    pos.items.map((p) => {
      const guard = p.state === "ENTERING" ? "" : p.halted ? '<span class="badge bad">נעצר – טיפול ידני</span>'
        : p.exit_request ? '<span class="badge warn">ביציאה</span>'
        : p.protected ? '<span class="badge on">TP + SL פעילים</span>' : '<span class="badge bad">לא מוגן</span>';
      return `<tr><td class="sym">${esc(p.symbol)}</td><td><span class="badge">${PHASE[p.state] || p.state}</span></td>
        <td class="num">${p.qty_open || "–"}</td><td class="num">${usd(p.entry_price)}</td><td class="num">${usd(p.last)}</td>
        <td class="num ${cls(p.unrealized)}">${signed(p.unrealized)}</td><td class="num ${cls(p.unrealized_pct)}">${pct(p.unrealized_pct)}</td>
        <td class="num">${usd(p.tp_price)}</td><td class="num">${usd(p.sl_price)}</td><td class="num">${p.days_held ?? "–"}</td><td>${guard}</td>
        <td><button class="btn small ghost" data-chart="${esc(p.symbol)}">גרף</button>
        ${p.state !== "ENTERING" ? `<button class="btn small danger" data-close="${p.id}" data-sym="${esc(p.symbol)}">סגור</button>` : ""}</td></tr>`;
    }), "אין פוזיציות פתוחות");
  $("#today-date").textContent = wl.date;
  table($("#today-table"), WATCH_HEAD, watchRows(wl.items, false), "לא נטענה רשימה להיום – לשונית ״רשימה יומית״");
  table($("#recent-table"), EVENT_HEAD, eventRows(ev.items), "אין אירועים");
}

document.addEventListener("click", async (e) => {
  const t = e.target.closest("button");
  if (!t) return;
  if (t.dataset.chart) {
    state.chartSymbol = t.dataset.chart;
    state.chartDate = t.dataset.date || (state.tab === "list" ? state.listDate : null);
    location.hash = "#charts";
  } else if (t.dataset.close) {
    if (!confirm(`לסגור את הפוזיציה ב-${t.dataset.sym} עכשיו במחיר שוק?`)) return;
    const r = await api(`/api/positions/${t.dataset.close}/close`, { method: "POST" });
    alert(r.message);
    refresh();
  } else if (t.dataset.remove) {
    const r = await api(`/api/watchlist/${t.dataset.remove}`, { method: "DELETE" });
    if (!r.ok) alert(r.message);
    refresh();
  }
});

// --------------------------------------------------------------------- charts
let chart = null, candles = null, maFast = null, maSlow = null, priceLines = [], chartKey = "", lastChart = null;
let macdChart = null, macdHist = null, macdLine = null, macdSignal = null;
let chartSeq = 0, chartHadData = false, syncPaused = false;
let whyKey = "", whyAge = 0, whyData = null;

// "Why was / wasn't there an entry": every trigger of the strategy that day with the verdict of the rules.
async function loadWhy(symbol, date, force) {
  const key = `${symbol}|${date}`;
  if (!force && key === whyKey && ++whyAge < 8) return;      // recomputed on demand, not on every refresh
  whyKey = key; whyAge = 0;
  const w = await api(`/api/explain?symbol=${encodeURIComponent(symbol)}&date=${date}`);
  if (key !== whyKey) return;
  whyData = w;
  $("#why-title").textContent = `${w.symbol} · ${w.date}`;
  const [label, c] = w.watch ? (WSTATUS[w.watch.status] || [w.watch.status, ""]) : ["לא ברשימה של אותו יום", "off"];
  const p = w.position;
  $("#why-status").innerHTML = `<span class="badge ${c}">${esc(label)}</span> ${esc(w.watch ? w.watch.note : "")}`
    + (p && p.entry_time ? ` · <b>בוצעה כניסה</b> <span class="num">${nyTime(p.entry_time).slice(6, 11)}</span>, ${p.qty} מניות במחיר <span class="num">${usd(p.entry_price)}</span>` : "")
    + ` · אסטרטגיה <span class="num">${esc(w.strategy)}</span> · חלון כניסה <span class="num">${esc(w.window[0])}–${esc(w.window[1])}</span> · ${w.bars_today} נרות בחישוב`;
  table($("#why-crosses"), [{ t: "נר (ניו יורק)", num: 1 }, { t: "מחיר סגירה", num: 1 }, { t: "תוצאה" }, { t: "הסבר" }],
    w.crosses.map((x) => `<tr><td class="num">${esc(x.hhmm)}</td><td class="num">${usd(x.close)}</td>
      <td><span class="badge ${x.ok ? "on" : x.in_window ? "warn" : "off"}">${x.ok ? "אות כניסה" : "אין כניסה"}</span></td>
      <td class="wrap">${esc(x.text)}</td></tr>`),
    w.bars_today ? "האסטרטגיה לא זיהתה אף חיתוך בנרות של אותו יום" : "אין נרות לחישוב באותו יום");
  table($("#why-events"), [{ t: "זמן (ניו יורק)", num: 1 }, { t: "סוג" }, { t: "הודעה" }],
    w.events.map((e) => `<tr class="lvl-${esc(e.level)}"><td class="num">${nyTime(e.ts)}</td><td>${esc(e.kind)}</td><td class="wrap">${esc(e.message)}</td></tr>`),
    "אין רישומים בלוג למניה ביום הזה – המערכת לא ראתה בה אות בזמן אמת");
}

// Fit both panes to the data on screen. Dragging a price axis switches the chart
// library to a manual scale, which would otherwise survive a change of stock and
// leave e.g. a $40 stock off-screen on the $400 scale of the previous one.
function resetChartView() {
  if (!chart) return;
  const few = lastChart && lastChart.bars.length < 60;       // a handful of bars: keep normal candle width
  // Both panes are set explicitly, so the pane-to-pane sync is paused meanwhile: the library reports
  // range changes a frame later, and a late report of the old range would undo the fit.
  syncPaused = true;
  for (const ch of [chart, macdChart]) ch.priceScale("right").applyOptions({ autoScale: true });
  for (const ch of [chart, macdChart]) {
    if (few) { ch.timeScale().applyOptions({ barSpacing: 9 }); ch.timeScale().scrollToRealTime(); } else ch.timeScale().fitContent();
  }
  requestAnimationFrame(() => requestAnimationFrame(() => { syncPaused = false; }));
}
const cssVar = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();

function ensureChart() {
  if (chart) return;
  const c = { surface: cssVar("--surface"), text: cssVar("--text-2"), grid: cssVar("--grid"), border: cssVar("--border"), up: cssVar("--up"), down: cssVar("--down") };
  const base = (timeAxis) => ({
    autoSize: true,
    layout: { background: { color: c.surface }, textColor: c.text, fontFamily: "Segoe UI, system-ui, Arial, sans-serif" },
    grid: { vertLines: { color: c.grid }, horzLines: { color: c.grid } },
    rightPriceScale: { borderColor: c.border, minimumWidth: 78 },   // same width: the two panes stay aligned
    timeScale: { borderColor: c.border, timeVisible: true, secondsVisible: false, visible: timeAxis },
    crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
    localization: { locale: "en-GB" },
  });
  const line = (ch, color) => ch.addLineSeries({ color, lineWidth: 2, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false });
  chart = LightweightCharts.createChart($("#chart"), base(false));
  // rising candles are hollow, falling candles are filled: readable without relying on colour
  candles = chart.addCandlestickSeries({ upColor: c.surface, downColor: c.down, borderUpColor: c.up, borderDownColor: c.down, wickUpColor: c.up, wickDownColor: c.down, priceLineVisible: false });
  maFast = line(chart, cssVar("--ma-fast"));
  maSlow = line(chart, cssVar("--ma-slow"));

  // MACD pane: its own chart, kept in step with the price chart
  macdChart = LightweightCharts.createChart($("#chart-macd"), base(true));
  const fmt = { type: "price", precision: 3, minMove: 0.001 };
  macdHist = macdChart.addHistogramSeries({ priceLineVisible: false, lastValueVisible: false, priceFormat: fmt });
  macdLine = line(macdChart, cssVar("--ma-fast"));
  macdSignal = line(macdChart, cssVar("--ma-slow"));
  macdLine.applyOptions({ priceFormat: fmt });
  macdSignal.applyOptions({ priceFormat: fmt });
  macdLine.createPriceLine({ price: 0, color: cssVar("--muted"), lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Solid, axisLabelVisible: false });

  let syncing = false;
  const link = (a, b) => a.timeScale().subscribeVisibleLogicalRangeChange((r) => {
    if (syncing || syncPaused || !r) return;
    syncing = true; b.timeScale().setVisibleLogicalRange(r); syncing = false;
  });
  link(chart, macdChart); link(macdChart, chart);
  chart.subscribeCrosshairMove((p) => {
    renderLegend(p && p.time ? p.time : null);
    const row = p && p.time && lastChart ? lastChart.byTime.get(p.time) : null;
    if (row && row.macd != null) macdChart.setCrosshairPosition(row.macd, p.time, macdLine); else macdChart.clearCrosshairPosition();
  });
  macdChart.subscribeCrosshairMove((p) => {
    renderLegend(p && p.time ? p.time : null);
    const row = p && p.time && lastChart ? lastChart.byTime.get(p.time) : null;
    if (row && p.sourceEvent) chart.setCrosshairPosition(row.bar.close, p.time, candles); else if (!p.time) chart.clearCrosshairPosition();
  });
}

function renderLegend(time) {
  const d = lastChart;
  if (!d || !d.bars.length) { $("#chart-legend").textContent = ""; $("#macd-legend").textContent = ""; return; }
  const row = d.byTime.get(time) || d.byTime.get(d.bars[d.bars.length - 1].time);
  const bar = row.bar, n = (v, k = 2) => v.toFixed(k);
  const tag = (color, text) => `<span style="border-bottom:2px solid ${cssVar(color)}">${text}</span>`;
  const hhmm = new Date(bar.time * 1000).toISOString().slice(11, 16);
  $("#chart-legend").innerHTML = `<b>${esc(d.symbol)}</b><span>${hhmm} NY${bar.rth === false ? " (ext)" : ""}</span>
    <span>O ${n(bar.open)}</span><span>H ${n(bar.high)}</span><span>L ${n(bar.low)}</span><span>C ${n(bar.close)}</span>
    ${bar.volume != null ? `<span>Vol ${Number(bar.volume).toLocaleString("en-US")}</span>` : ""}
    ${row.fast != null ? tag("--ma-fast", `${esc(d.ma_labels[0])} ${n(row.fast)}`) : ""}
    ${row.slow != null ? tag("--ma-slow", `${esc(d.ma_labels[1])} ${n(row.slow)}`) : ""}
    ${d.levels.map((l) => `<span>${{ entry: "Entry", tp: "TP", sl: "SL" }[l.kind]} ${n(l.price)}</span>`).join("")}`;
  $("#macd-legend").innerHTML = `<b>${esc(d.macd_label)}</b>` + (row.macd == null ? "<span>–</span>"
    : `${tag("--ma-fast", `MACD ${n(row.macd, 3)}`)}${row.signal != null ? tag("--ma-slow", `Signal ${n(row.signal, 3)}`) : ""}
       ${row.hist != null ? `<span>Hist ${n(row.hist, 3)}</span>` : ""}`);
}

async function loadCharts() {
  const seq = ++chartSeq;          // a newer request (click or timer) makes this one stale
  const dateInput = $("#chart-date");
  if (!state.chartDate) state.chartDate = state.status.today;
  if (dateInput.value !== state.chartDate) dateInput.value = state.chartDate;
  const sy = await api(`/api/symbols?date=${state.chartDate}`);
  if (seq !== chartSeq) return;
  if (!sy.symbols.includes(state.chartSymbol)) state.chartSymbol = sy.symbols[0] || null;
  $("#chart-symbols").innerHTML = sy.symbols.map((s) => `<button data-sym="${esc(s)}" class="${s === state.chartSymbol ? "active" : ""}">${esc(s)}</button>`).join("")
    || '<span class="muted">אין מניות לתאריך הזה</span>';
  ensureChart();
  const d = state.chartSymbol
    ? await api(`/api/chart?symbol=${encodeURIComponent(state.chartSymbol)}&date=${state.chartDate}`)
    : { symbol: "", date: state.chartDate, bars: [], ma_fast: [], ma_slow: [], macd: [], macd_signal: [], macd_hist: [], markers: [], levels: [], ma_labels: ["", ""], macd_label: "MACD" };
  if (seq !== chartSeq) return;
  const ext = $("#chart-ext").checked, extColor = cssVar("--ext");
  const bars = d.bars.filter((b) => ext || b.rth).map((b) => b.rth ? b
    : { ...b, color: b.close >= b.open ? cssVar("--surface") : extColor, borderColor: extColor, wickColor: extColor });
  const byTime = new Map(bars.map((b) => [b.time, { bar: b }]));
  const attach = (key, points) => points.forEach((p) => { const row = byTime.get(p.time); if (row) row[key] = p.value; });
  attach("fast", d.ma_fast); attach("slow", d.ma_slow);
  attach("macd", d.macd); attach("signal", d.macd_signal); attach("hist", d.macd_hist);
  lastChart = { ...d, bars, byTime };
  // every series carries every bar time (empty where there is no value) so both panes share one time axis
  const series = (key, extra) => bars.map((b) => {
    const v = byTime.get(b.time)[key];
    return v == null ? { time: b.time } : { time: b.time, value: v, ...(extra ? extra(v) : {}) };
  });
  candles.setData(bars);
  maFast.setData(series("fast"));
  maSlow.setData(series("slow"));
  macdLine.setData(series("macd"));
  macdSignal.setData(series("signal"));
  macdHist.setData(series("hist", (v) => ({ color: v >= 0 ? cssVar("--up") + "88" : cssVar("--down") + "88" })));
  // an empty MACD pane always says why
  const macdEmpty = $("#macd-empty");
  const withMacd = bars.filter((b) => byTime.get(b.time).macd != null).length;
  macdEmpty.hidden = withMacd > 0 || !bars.length;
  if (!macdEmpty.hidden) {
    macdEmpty.innerHTML = !d.uses_extended && !d.indicator_bars
      ? 'ה-MACD והממוצעים מחושבים על נרות שעות המסחר הרגילות (<span class="num">09:30–16:00</span> ניו יורק). בתאריך הזה יש כרגע רק נרות מסחר מורחב – הקווים יופיעו עם פתיחת המסחר.'
      : !d.uses_extended && !bars.some((b) => b.rth)
        ? "ה-MACD מחושב על נרות שעות המסחר הרגילות. סמן ״מסחר מורחב״ כדי לראות את כל היום."
        : `אין עדיין מספיק נרות לחישוב ה-MACD: נדרשים ${Number(d.macd_need) + 1} נרות רצופים, כולל היום הקודם.`;
  }
  const marks = d.markers.filter((m) => byTime.has(m.time));
  candles.setMarkers(marks.map((m) => m.kind === "entry"
    ? { time: m.time, position: "belowBar", shape: "arrowUp", color: cssVar("--accent"), text: m.text }
    : { time: m.time, position: "aboveBar", shape: "arrowDown", color: m.win ? cssVar("--up") : cssVar("--down"), text: m.text }));
  await loadWhy(d.symbol, d.date, `${d.symbol}|${d.date}` !== whyKey).catch(() => {});
  if (seq !== chartSeq) return;
  // MACD pane: the buy, and every trigger of the strategy inside the entry window (accepted or not)
  const buys = marks.filter((m) => m.kind === "entry" && byTime.get(m.time).macd != null)
    .map((m) => ({ time: m.time, position: "belowBar", shape: "arrowUp", color: cssVar("--accent"), text: "BUY" }));
  const crosses = (whyData && whyKey === `${d.symbol}|${d.date}` ? whyData.crosses : [])
    .filter((x) => x.in_window && byTime.has(x.time) && byTime.get(x.time).macd != null && !buys.some((b) => b.time === x.time))
    .map((x) => ({ time: x.time, position: "aboveBar", shape: "circle", color: x.ok ? cssVar("--up") : cssVar("--muted"), text: x.ok ? "✓" : "✕" }));
  macdLine.setMarkers([...buys, ...crosses].sort((a, b) => a.time - b.time));
  priceLines.forEach((l) => candles.removePriceLine(l));
  const style = { entry: ["כניסה", cssVar("--accent")], tp: ["TP", cssVar("--up")], sl: ["SL", cssVar("--down")] };
  priceLines = d.levels.filter((l) => l.price).map((l) => candles.createPriceLine({
    price: l.price, color: style[l.kind][1], lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Dashed, axisLabelVisible: true, title: style[l.kind][0] }));
  if (d.ma_labels[0]) { $("#key-fast").textContent = d.ma_labels[0]; $("#key-slow").textContent = d.ma_labels[1]; }
  const empty = $("#chart-empty");
  empty.hidden = bars.length > 0;
  if (!bars.length) {
    const hidden = d.bars.length - bars.length;
    empty.textContent = !d.symbol ? `אין מניות עם נתונים בתאריך ${d.date}`
      : hidden ? `ל-${d.symbol} יש בתאריך ${d.date} רק נרות מסחר מורחב (${hidden}) – סמן "מסחר מורחב" כדי לראות אותם`
      : `אין נרות ל-${d.symbol} בתאריך ${d.date}`;
  }
  const last = bars[bars.length - 1];
  $("#chart-info").textContent = bars.length
    ? `${bars.length} נרות · אחרון ${new Date(last.time * 1000).toISOString().slice(11, 16)} · ${last.close.toFixed(2)}` : "";
  // a new stock, date or bar set - or the first bars of an empty chart - always gets a fresh view
  const key = `${d.symbol}|${d.date}|${ext}`;
  if (key !== chartKey || (bars.length > 0 && !chartHadData)) { chartKey = key; resetChartView(); }
  chartHadData = bars.length > 0;
  renderLegend(null);
}
$("#chart-reset").addEventListener("click", resetChartView);
// choosing a stock or a date - even the one already shown - always re-fits the view
$("#chart-symbols").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) { state.chartSymbol = b.dataset.sym; chartKey = ""; loadCharts(); } });
$("#chart-date").addEventListener("change", (e) => { state.chartDate = e.target.value || null; chartKey = ""; loadCharts(); });
$("#chart-ext").addEventListener("change", loadCharts);

// ----------------------------------------------------------------- daily list
async function loadList() {
  if (!state.listDate) { state.listDate = state.status.suggested_list_date; $("#list-date").value = state.listDate; }
  const wl = await api(`/api/watchlist?date=${state.listDate}`);
  $("#list-shown").textContent = wl.date;
  table($("#list-table"), WATCH_HEAD, watchRows(wl.items, true), "אין רשימה לתאריך הזה");
}
$("#list-date").addEventListener("change", (e) => { state.listDate = e.target.value || null; loadList(); });
$("#list-file").addEventListener("change", (e) => {
  const file = e.target.files[0];
  if (!file) return;
  const reader = new FileReader();
  reader.onload = () => { $("#list-text").value = reader.result; };
  reader.readAsText(file);
});
$("#list-submit").addEventListener("click", async () => {
  const out = $("#list-result");
  const text = $("#list-text").value;
  if (!text.trim()) { out.innerHTML = '<p class="msg-err">הדבק רשימה או בחר קובץ CSV</p>'; return; }
  try {
    const r = await api("/api/watchlist", { method: "POST", body: { date: $("#list-date").value, text, replace: $("#list-replace").checked } });
    if (!r.ok) {
      out.innerHTML = `<p class="msg-err">הרשימה לא נטענה:</p><ul class="msg-err">${r.errors.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>`;
    } else {
      out.innerHTML = `<p class="msg-ok">נטען: ${r.added} חדשות, ${r.updated} עודכנו${r.removed ? `, ${r.removed} הוסרו` : ""}${r.kept ? `, ${r.kept} ללא שינוי` : ""}</p>`
        + (r.warnings.length ? `<ul class="msg-warn">${r.warnings.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : "");
      $("#list-text").value = ""; $("#list-file").value = "";
    }
  } catch (err) { out.innerHTML = `<p class="msg-err">${esc(err.message)}</p>`; }
  loadList();
});

// -------------------------------------------------------------------- history
async function loadHistory() {
  const r = await api("/api/positions?which=closed");
  const s = r.stats;
  const tile = (label, value, sub, c = "") => `<div class="tile"><label>${label}</label><b class="num ${c}">${value}</b><small>${sub}</small></div>`;
  const part = (p) => p.count ? `${p.wins} רווחיות מתוך ${p.count}` : "אין עסקאות";
  $("#stats").innerHTML =
    tile("רווח ממומש כולל", signed(s.all.pnl), `אחרי עמלות (${usd(s.all.commission)})`, cls(s.all.pnl)) +
    tile("עסקאות סגורות", s.all.count, s.all.win_rate == null ? "–" : `${s.all.win_rate}% רווחיות`) +
    tile("נסגרו תוך-יומי", s.intraday.count ? signed(s.intraday.pnl) : "–", part(s.intraday), cls(s.intraday.pnl)) +
    tile("נסגרו בסווינג", s.swing.count ? signed(s.swing.pnl) : "–", part(s.swing), cls(s.swing.pnl));
  table($("#closed-table"), [{ t: "מניה" }, { t: "יום כניסה", num: 1 }, { t: "כמות", num: 1 }, { t: "כניסה", num: 1 }, { t: "יציאה", num: 1 },
    { t: "רווח/הפסד", num: 1 }, { t: "%", num: 1 }, { t: "עמלות", num: 1 }, { t: "נסגרה כ־" }, { t: "סיבה" }, { t: "זמן יציאה (ניו יורק)", num: 1 }, { t: "" }],
    r.items.map((p) => `<tr><td class="sym">${esc(p.symbol)}</td><td class="num">${esc(p.trade_date)}</td><td class="num">${p.qty}</td>
      <td class="num">${usd(p.entry_price)}</td><td class="num">${usd(p.exit_price)}</td>
      <td class="num ${cls(p.pnl)}">${signed(p.pnl)}</td><td class="num ${cls(p.pnl_pct)}">${pct(p.pnl_pct)}</td><td class="num">${usd(p.commission)}</td>
      <td>${PHASE[p.exit_phase] || esc(p.exit_phase)}</td><td>${REASON[p.exit_reason] || esc(p.exit_reason)}</td><td class="num">${nyTime(p.exit_time)}</td>
      <td><button class="btn small ghost" data-chart="${esc(p.symbol)}" data-date="${esc(p.trade_date)}">גרף</button></td></tr>`),
    "עדיין אין עסקאות סגורות");
}

// ------------------------------------------------------------------- settings
const SETTINGS = [
  ["גודל פוזיציה", [
    ["position_pct", "אחוז מהתיק לכל פוזיציה", "number", "מחושב משווי התיק (Net Liquidation)"],
    ["max_position_usd", "תקרה בדולרים לפוזיציה", "number", "0 = ללא תקרה"],
    ["cash_reserve_usd", "מזומן שלא נוגעים בו ($)", "number", ""],
    ["max_open_positions", "מקסימום פוזיציות פתוחות", "number", "0 = ללא הגבלה"],
  ]],
  ["ברירות מחדל לעסקה (כשהתא ברשימה ריק)", [
    ["intraday_tp_pct", "יעד רווח תוך-יומי %", "number", ""],
    ["intraday_sl_pct", "סטופ תוך-יומי %", "number", "0 = הסטופ של הסווינג"],
    ["swing_tp_pct", "יעד רווח סווינג %", "number", ""],
    ["swing_sl_pct", "סטופ סווינג %", "number", ""],
    ["max_hold_days", "מקסימום ימי החזקה", "number", "ימי מסחר, 0 = ללא הגבלה"],
  ]],
  ["חלונות זמן (שעון ניו יורק)", [
    ["entry_start", "תחילת חלון הכניסה", "hhmm", "כניסה לפני 09:30 דורשת גם ״נרות מסחר מורחב״"],
    ["entry_end", "סוף חלון הכניסה", "hhmm", ""],
    ["intraday_end", "סוף השלב התוך-יומי", "hhmm", "אחריו הפוזיציה עוברת ליעדי הסווינג"],
  ]],
  ["ביצוע", [
    ["entry_slippage_pct", "מרווח מחיר בכניסה %", "number", "פקודת Limit במחיר האות + המרווח"],
    ["entry_timeout_sec", "ביטול כניסה שלא התמלאה (שניות)", "number", ""],
    ["max_signal_age_sec", "גיל מקסימלי של אות (שניות)", "number", "נר שהגיע באיחור לא יפעיל כניסה"],
    ["tp_outside_rth", "Take Profit גם מחוץ לשעות המסחר", "checkbox", ""],
    ["sl_outside_rth", "Stop Loss גם מחוץ לשעות המסחר", "checkbox", "לא מומלץ: נזילות נמוכה"],
  ]],
  ["אסטרטגיית כניסה תוך-יומית", [
    ["strategy", "אסטרטגיה", "select", "ברירת המחדל: קו ה-MACD חוצה מעל קו ה-Signal כששניהם מתחת לאפס"],
    ["macd_fast", "MACD – Fast length", "number", ""],
    ["macd_slow", "MACD – Slow length", "number", ""],
    ["macd_signal", "MACD – Signal length", "number", ""],
    ["macd_min_depth_pct", "עומק מינימלי של ה-MACD מתחת לאפס", "number", "באחוזים ממחיר המניה, נמדד בשפל הירידה של היום. 0 = כל חיתוך מתחת לאפס"],
    ["use_extended_bars", "לכלול נרות מסחר מורחב בחישוב", "checkbox", "כמו גרף עם Extended Hours ב-TradingView"],
  ]],
  ["אסטרטגיות חלופיות (ma_cross / ma_trend)", [
    ["ma_type", "סוג ממוצע נע", "select:EMA,SMA", ""],
    ["ma_fast", "ממוצע מהיר (נרות דקה)", "number", ""],
    ["ma_slow", "ממוצע איטי (נרות דקה)", "number", ""],
  ]],
];

async function loadSettings(force) {
  if (state.settingsLoaded && !force) return;
  const r = await api("/api/settings");
  $("#settings-form").innerHTML = SETTINGS.map(([title, fields]) => `<fieldset><legend>${title}</legend>` + fields.map(([key, label, type, hint]) => {
    const v = r.settings[key];
    let input;
    if (type === "checkbox") input = `<input type="checkbox" name="${key}" ${v ? "checked" : ""}>`;
    else if (type.startsWith("select")) {
      const options = type === "select" ? r.strategies : type.split(":")[1].split(",");
      input = `<select name="${key}">${options.map((o) => `<option ${o === v ? "selected" : ""}>${esc(o)}</option>`).join("")}</select>`;
    } else if (type === "hhmm") input = `<input type="text" name="${key}" value="${esc(v)}" placeholder="HH:MM" pattern="[0-2][0-9]:[0-5][0-9]" maxlength="5">`;
    else input = `<input type="number" name="${key}" value="${esc(v)}" step="any" min="0">`;
    return `<div class="field"><label for="${key}">${label}</label>${input}${hint ? `<small>${hint}</small>` : ""}</div>`;
  }).join("") + "</fieldset>").join("");
  state.settingsLoaded = true;
}
$("#settings-save").addEventListener("click", async (e) => {
  e.preventDefault();
  const body = {};
  for (const el of $("#settings-form").elements) if (el.name) body[el.name] = el.type === "checkbox" ? el.checked : el.value;
  const msg = $("#settings-msg");
  try {
    await api("/api/settings", { method: "PUT", body });
    msg.textContent = "נשמר ✓"; msg.className = "msg-ok";
    await loadSettings(true);
  } catch (err) { msg.textContent = "שגיאה: " + err.message; msg.className = "msg-err"; }
});
$("#tg-test").addEventListener("click", async (e) => {
  e.preventDefault();
  const r = await api("/api/telegram/test", { method: "POST" });
  const msg = $("#settings-msg");
  msg.textContent = "טלגרם: " + r.message; msg.className = r.ok ? "msg-ok" : "msg-err";
});

// ------------------------------------------------------------------------ log
async function loadLog() {
  const level = $("#log-level").value, symbol = $("#log-symbol").value.trim();
  const r = await api(`/api/events?limit=400&level=${level}&symbol=${encodeURIComponent(symbol)}`);
  table($("#log-table"), EVENT_HEAD, eventRows(r.items), "אין אירועים");
}
$("#log-level").addEventListener("change", loadLog);
$("#log-symbol").addEventListener("input", loadLog);

// ---------------------------------------------------------------------- shell
const LOADERS = { overview: loadOverview, charts: loadCharts, list: loadList, history: loadHistory, settings: loadSettings, log: loadLog };

function showTab() {
  const tab = (location.hash || "#overview").slice(1);
  state.tab = LOADERS[tab] ? tab : "overview";
  document.querySelectorAll(".tab").forEach((s) => { s.hidden = s.id !== "tab-" + state.tab; });
  document.querySelectorAll(".tabs a").forEach((a) => a.classList.toggle("active", a.dataset.tab === state.tab));
  refresh();
}

async function refresh() {
  if (state.busy) return;
  state.busy = true;
  try {
    renderStatus(await api("/api/status"));
    await LOADERS[state.tab]();
  } catch (err) {
    const conn = $("#conn");
    conn.className = "conn bad";
    conn.lastElementChild.textContent = "אין תקשורת עם המערכת";
    console.error(err);
  } finally { state.busy = false; }
}

window.addEventListener("hashchange", showTab);
showTab();
setInterval(() => { if (!document.hidden) refresh(); }, 2000);
