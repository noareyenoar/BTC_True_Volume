/* TA+VSA Visualizer — frontend logic.
 * Candlesticks + multi-source volume histogram over the audited proxy stack.
 * The hover tooltip always shows the FULL breakdown; the selector only changes
 * what the volume panel renders. No external network calls at render time. */

"use strict";

const UP = "#26a69a", DOWN = "#ef5350", MUTED = "#787b86", AMBER = "#f59e0b", PURPLE = "#b07dff";

const S = {
  symbol: "BTCUSDT", interval: "1h",
  bars: [], gapSlots: [], sourcesMeta: [], sourceOpts: [],
  breakdown: new Map(), breakdownPending: new Set(),
  range: { start: 0, end: 0 }, visible: null,
  source: localStorage.getItem("vsa_source") || "onchain_network_volume",
  audited: [], defaultInterval: "1h",
  holder: null, fetchSeq: 0,
  profileWindow: localStorage.getItem("vsa_profile_window") || "90d",
  profileCache: new Map(),
};

/* ---------------- helpers ---------------- */
function fmtNum(v, digits = 2) {
  if (v === null || v === undefined || isNaN(v)) return "—";
  const a = Math.abs(v);
  if (a >= 1e9) return (v / 1e9).toFixed(digits) + "B";
  if (a >= 1e6) return (v / 1e6).toFixed(digits) + "M";
  if (a >= 1e3) return (v / 1e3).toFixed(digits) + "K";
  return v.toFixed(digits);
}
function fmtUsd(v) { return v === null || v === undefined || isNaN(v) ? "—" : "$" + fmtNum(v, 2); }
function fmtPct(v) { return v === null || v === undefined || isNaN(v) ? "—" : (v * 100).toFixed(1) + "%"; }
function fmtTs(ms, interval) {
  const d = new Date(ms);
  return interval === "1d"
    ? d.toISOString().slice(0, 10)
    : d.toISOString().slice(0, 16).replace("T", " ");
}
function tag(conf) {
  const c = String(conf || "").toLowerCase();
  const cls = c.startsWith("high") ? "high" : c.startsWith("med") ? "medium" : "low";
  return `<span class="tag ${cls}">${conf}</span>`;
}
/* api() — dual mode.
 * STATIC mode (GitHub Pages / any static host): manifest.json exists next to
 *   the page -> answer from precomputed full-history data files, sliced
 *   client-side (same response shapes the server would produce).
 * LIVE mode (local uvicorn): manifest.json 404s -> plain fetch of /api/*. */
let STATIC = null;                    // parsed manifest when in static mode
const fileCache = new Map();          // path -> parsed JSON
async function loadJSONFile(path) {
  if (fileCache.has(path)) return fileCache.get(path);
  const r = await fetch(path);
  if (!r.ok) throw new Error(`${path} -> HTTP ${r.status}`);
  let text;
  if (path.endsWith(".gz")) {
    if (typeof DecompressionStream === "undefined")
      throw new Error(`${path}: browser lacks DecompressionStream — update to a modern browser`);
    const ds = new DecompressionStream("gzip");
    text = await new Response(new Blob([await r.arrayBuffer()]).stream().pipeThrough(ds)).text();
  } else {
    text = await r.text();
  }
  const obj = JSON.parse(text);
  fileCache.set(path, obj);
  return obj;
}
/* ---------------- live tail (static mode only) ---------------- */
/* The static export ends at the last Vision publish (~1 day lag). To show
 * "today" anyway, fetch the in-progress candles straight from Binance's
 * public API (CORS-open) and append them as clearly-marked live/preliminary
 * bars: price + exchange volume only. True volume for those bars arrives
 * with the next Vision publish (~24 h lag) — the tooltip and status bar
 * say so. Futures first (the series tail is futures UM), spot fallback,
 * none if Binance is unreachable (the page then just shows the export). */
const liveCache = new Map();                       // sym_iv -> {t, bars}
const LIVE_TTL = 5 * 60 * 1000;                    // re-fetch the current candle every 5 min
const LIVE_BASES = [
  "https://fapi.binance.com/fapi/v1/klines",
  "https://api.binance.com/api/v3/klines",
];
async function fetchLiveTail(sym, iv) {
  if (iv === "1m") return [];
  const key = sym + "_" + iv;
  const hit = liveCache.get(key);
  if (hit && Date.now() - hit.t < LIVE_TTL) return hit.bars;
  for (const base of LIVE_BASES) {
    try {
      const r = await fetch(`${base}?symbol=${sym}&interval=${iv}&limit=50`);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const kl = await r.json();
      const now = Date.now();
      const bars = kl.map(k => ({
        open_time: k[0], open: +k[1], high: +k[2], low: +k[3], close: +k[4],
        volume: +k[5], taker_buy: +k[9],
        market: base.includes("fapi") ? "futures_um" : "spot",
        resampled: false, cross_filled: false, follows_gap: false,
        live: true,
      })).filter(b => b.open_time <= now);
      liveCache.set(key, { t: Date.now(), bars });
      return bars;
    } catch (e) { /* try the next base */ }
  }
  return [];                                       // offline/blocked -> exported data only
}
async function api(path) {
  if (!STATIC) {                     // live mode: the real backend
    const r = await fetch(path);
    if (!r.ok) throw new Error(`${path} -> HTTP ${r.status}`);
    return r.json();
  }
  const u = new URL(path, location.href);
  const q = Object.fromEntries(u.searchParams.entries());
  const what = u.pathname.split("/").filter(Boolean).pop();
  const sym = q.symbol || STATIC.symbols[0] || "BTCUSDT";
  const iv = q.interval || "1h";
  if (what === "symbols") return loadJSONFile(STATIC.files.symbols);
  if (what === "onchain_snapshot") return loadJSONFile(STATIC.files.onchain[sym]);
  if (what === "volume_profile") {
    const pf = STATIC.files.profile;
    if (!pf) throw new Error("this export has no volume profile — re-publish the site");
    return loadJSONFile(pf[sym + "_" + (q.window || "90d")]);
  }
  if (what === "ohlc") {
    const resp = await loadJSONFile(STATIC.files.bars[sym + "_" + iv]);
    let bars = resp.bars;
    if (q.start !== undefined) bars = bars.filter(b => b.open_time >= Number(q.start));
    if (q.end !== undefined) bars = bars.filter(b => b.open_time <= Number(q.end));
    let truncated = false;
    const limit = q.limit === undefined ? 250000 : Number(q.limit);
    if (bars.length > limit) { bars = bars.slice(-limit); truncated = true; }
    const lastT = bars.length ? bars[bars.length - 1].open_time : 0;
    let gaps = resp.gap_slots;
    if (q.start !== undefined) gaps = gaps.filter(g => g >= Number(q.start));
    if (q.end !== undefined) gaps = gaps.filter(g => g <= Number(q.end));
    // live tail: today's candles from Binance's public API (price only)
    let live = (await fetchLiveTail(sym, iv)).filter(b => b.open_time > lastT);
    if (q.start !== undefined) live = live.filter(b => b.open_time >= Number(q.start));
    if (q.end !== undefined) live = live.filter(b => b.open_time <= Number(q.end));
    if (live.length) bars = bars.concat(live);
    return { symbol: sym, interval: iv, bars, gap_slots: gaps, truncated,
             coverage: { start: bars.length ? bars[0].open_time : null,
                         end: bars.length ? bars[bars.length - 1].open_time : null,
                         n: bars.length } };
  }
  if (what === "volume_breakdown") {
    const resp = await loadJSONFile(STATIC.files.volume[sym + "_" + iv]);
    let bars = resp.bars;
    if (q.start !== undefined) bars = bars.filter(b => b.open_time >= Number(q.start));
    if (q.end !== undefined) bars = bars.filter(b => b.open_time <= Number(q.end));
    const limit = q.limit === undefined ? 30000 : Number(q.limit);
    if (bars.length > limit) bars = bars.slice(-limit);
    // synthetic rows for live candles: exchange volume only; the rest of the
    // true-volume stack is marked pending until the next Vision publish
    const lastT = bars.length ? bars[bars.length - 1].open_time : 0;
    let live = (await fetchLiveTail(sym, iv)).filter(b => b.open_time > lastT);
    if (q.start !== undefined) live = live.filter(b => b.open_time >= Number(q.start));
    if (q.end !== undefined) live = live.filter(b => b.open_time <= Number(q.end));
    if (live.length) {
      bars = bars.concat(live.map(b => ({
        open_time: b.open_time, pending: true,
        sources: {
          exchange_volume: b.volume,
          tape_rollup_volume: null,
          onchain_network_volume: null,
          large_trade_notional: null,
          open_interest_delta: null,
          taker_buy_sell_split: { buy: b.taker_buy, sell: Math.max(0, b.volume - (b.taker_buy || 0)) },
          top_trader_positioning: null,
        },
      })));
    }
    return { symbol: sym, interval: iv, sources: resp.sources, bars, notes: resp.notes };
  }
  throw new Error("unknown route " + path);
}

/* ---------------- chart ---------------- */
const chartEl = document.getElementById("chart");
const chart = LightweightCharts.createChart(chartEl, {
  layout: { background: { color: "#131722" }, textColor: "#d1d4dc" },
  grid: { vertLines: { color: "#1c2230" }, horzLines: { color: "#1c2230" } },
  timeScale: { timeVisible: false, secondsVisible: false, rightOffset: 6 },
  rightPriceScale: { borderColor: "#2a2e39" },
  crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
  autoSize: true,
});
const candleSeries = chart.addCandlestickSeries({
  upColor: UP, downColor: DOWN, wickUpColor: UP, wickDownColor: DOWN,
  borderVisible: false,
});
const volSeries = chart.addHistogramSeries({
  priceFormat: { type: "volume" }, priceScaleId: "",
});
chart.priceScale("").applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
let takerBuySeries = null, takerSellSeries = null;

/* ---------------- volume profile (traded volume by price) ----------------
 * Horizontal bars anchored to the right edge of the plot at the price levels
 * where volume actually traded over the selected window, painted UNDER the
 * candles — "where is there a real, volume-confirmed base?".
 *
 * Drawn as a series primitive (attachPrimitive) rather than an overlay canvas:
 * the library paints it inside the pane, clips it to the plot area for free,
 * and re-invokes the renderer on every repaint — so pan, pinch-zoom, resize and
 * interval changes need no ResizeObserver, no requestAnimationFrame debounce and
 * no coordinate bookkeeping. v4 has no priceScaleWidth(), and #chart is not
 * position:relative, so an external overlay would have needed all of that plus a
 * CSS change just to avoid silently positioning against the viewport.
 *
 * The profile is a SNAPSHOT of the chosen window, not a per-bar series: panning
 * time never changes which price levels are drawn, so this deliberately does not
 * hook the refreshBreakdown() visible-range path (which is range-scoped and would
 * mangle an "all" window). */
const profileState = { data: null };
const AMBER_RGB = [245, 158, 11], GREEN_RGB = [38, 166, 154], RED_RGB = [239, 83, 80];
function mixRgb(a, b, t) {
  t = Math.max(0, Math.min(1, t));
  return `rgb(${Math.round(a[0] + (b[0] - a[0]) * t)},${Math.round(a[1] + (b[1] - a[1]) * t)},${Math.round(a[2] + (b[2] - a[2]) * t)})`;
}
function drawProfile(target) {
  const d = profileState.data;
  if (!d || !d.buckets || !d.buckets.length) return;
  target.useMediaCoordinateSpace(({ context: ctx, mediaSize }) => {
    const w = d.bucket_usd, right = mediaSize.width, H = mediaSize.height;
    let maxQ = 0;
    for (const b of d.buckets) if (b.q > maxQ) maxQ = b.q;
    if (!(maxQ > 0)) return;
    const maxLen = right * 0.34;              // never swamp the candles

    // URPD silhouette: on-chain supply by cost basis, drawn BEHIND the tape bars.
    // A different measure AND a different granularity ($200 buckets, one snapshot),
    // so it is scaled against its own max — a shared axis would silently
    // misrepresent one of the two series.
    const u = d.urpd;
    if (u && u.buckets && u.buckets.length > 1) {
      let maxS = 0;
      for (const x of u.buckets) if (x.s > maxS) maxS = x.s;
      if (maxS > 0) {
        const maxLenU = maxLen * 0.95;
        const pts = [];
        for (const x of u.buckets) {
          const y = candleSeries.priceToCoordinate(x.p);
          if (y === null || y < -2 || y > H + 2) continue;
          pts.push([y, Math.max(1, (x.s / maxS) * maxLenU)]);
        }
        if (pts.length > 1) {
          ctx.beginPath();
          ctx.moveTo(right, pts[0][0]);
          for (const [y, len] of pts) ctx.lineTo(right - len, y);
          ctx.lineTo(right, pts[pts.length - 1][0]);
          ctx.closePath();
          ctx.fillStyle = "rgba(176, 125, 255, 0.16)";
          ctx.fill();
          ctx.strokeStyle = "rgba(176, 125, 255, 0.55)";
          ctx.lineWidth = 1;
          ctx.stroke();
        }
      }
    }

    // value-area band (the 70% of volume around the POC)
    const vaTop = candleSeries.priceToCoordinate(d.value_area.high);
    const vaBot = candleSeries.priceToCoordinate(d.value_area.low);
    if (vaTop !== null && vaBot !== null) {
      ctx.fillStyle = "rgba(41, 98, 255, 0.07)";
      ctx.fillRect(0, vaTop, right, Math.max(1, vaBot - vaTop));
    }

    // bars: right-anchored, one bucket tall, tinted by taker imbalance so a
    // level where sellers were absorbed differs from one where buyers were trapped
    for (const b of d.buckets) {
      const yTop = candleSeries.priceToCoordinate(b.p + w);
      const yBot = candleSeries.priceToCoordinate(b.p);
      if (yTop === null || yBot === null) continue;
      // skip bars entirely outside the pane (>= / <= so a bar whose edge lands
      // exactly on the boundary — a zero-area rect — is skipped too). Bars that
      // merely straddle an edge are kept: the pane canvas clips them, which is
      // more truthful than dropping a partially visible level.
      if (yBot <= 0 || yTop >= H) continue;
      const h = yBot - yTop;
      if (h < 0.5) continue;
      const len = Math.max(1, (b.q / maxQ) * maxLen);
      const tot = b.buy + b.sell;
      const imb = tot > 0 ? (b.buy - b.sell) / tot : 0;
      ctx.fillStyle = imb >= 0 ? mixRgb(AMBER_RGB, GREEN_RGB, imb * 0.75)
                               : mixRgb(AMBER_RGB, RED_RGB, -imb * 0.75);
      ctx.globalAlpha = 0.5;
      ctx.fillRect(right - len, yTop, len, Math.max(0.5, h - 0.5));
    }
    ctx.globalAlpha = 1;

    // POC + realized price as dashed levels with a small right-aligned label
    ctx.font = "10px 'Segoe UI', system-ui, sans-serif";
    const level = (price, colour, text) => {
      const y = candleSeries.priceToCoordinate(price);
      if (y === null || y < 0 || y > H) return;
      ctx.save();
      ctx.setLineDash([4, 4]);
      ctx.strokeStyle = colour;
      ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(0, y + 0.5); ctx.lineTo(right, y + 0.5); ctx.stroke();
      ctx.restore();
      const tw = ctx.measureText(text).width;
      ctx.fillStyle = "rgba(19, 23, 34, 0.85)";
      ctx.fillRect(right - tw - 6, y - 12, tw + 6, 12);
      ctx.fillStyle = colour;
      ctx.fillText(text, right - tw - 3, y - 2.5);
    };
    level(d.poc, "#2962ff", `POC ${fmtNum(d.poc, 0)}`);
    if (d.realized_price)
      level(d.realized_price.value, AMBER, `realized ${fmtNum(d.realized_price.value, 0)}`);
  });
}
const profilePrimitive = {
  paneViews() {
    return [{ zOrder: () => "bottom", renderer: () => ({ draw: drawProfile }) }];
  },
};
candleSeries.attachPrimitive(profilePrimitive);
/* v4 exposes no public invalidate() on a primitive; re-attaching definitely marks
 * the pane dirty, so a window switch repaints without touching the series data. */
function requestRedraw() {
  candleSeries.detachPrimitive(profilePrimitive);
  candleSeries.attachPrimitive(profilePrimitive);
}

/* ---------------- volume panel ---------------- */
function barSourceValue(b) {
  const bd = S.breakdown.get(b.open_time);
  if (S.source === "taker_buy_sell_split") return bd ? bd.taker_buy : null;
  if (!bd) return null;
  return bd[S.source] === undefined ? null : bd[S.source];
}
function volColor(b) {
  if (S.source === "open_interest_delta") {
    const v = barSourceValue(b);
    return v === null || v === undefined || v >= 0 ? UP : DOWN;
  }
  return b.close >= b.open ? UP : DOWN;
}
function renderVolume() {
  const single = S.source !== "taker_buy_sell_split";
  if (takerBuySeries) { chart.removeSeries(takerBuySeries); takerBuySeries = null; }
  if (takerSellSeries) { chart.removeSeries(takerSellSeries); takerSellSeries = null; }
  const data = [], taker = single ? null : { buy: [], sell: [] };
  for (const b of S.bars) {
    const t = Math.floor(b.open_time / 1000);
    const v = barSourceValue(b);
    if (single) {
      data.push({ time: t, value: v === null || v === undefined ? null : v,
                  color: volColor(b) });
    } else {
      const bd = S.breakdown.get(b.open_time);
      const buy = bd ? bd.taker_buy : null;
      const sell = bd ? bd.taker_sell : null;
      taker.buy.push({ time: t, value: buy, color: UP });
      taker.sell.push({ time: t, value: sell, color: DOWN });
    }
  }
  if (single) {
    volSeries.setData(data);
  } else {
    volSeries.setData([]);
    takerBuySeries = chart.addHistogramSeries({ priceFormat: { type: "volume" }, priceScaleId: "" });
    takerSellSeries = chart.addHistogramSeries({ priceFormat: { type: "volume" }, priceScaleId: "" });
    takerBuySeries.setData(taker.buy.map(p => ({ time: p.time, value: p.value === null ? null : p.value, color: UP })));
    takerSellSeries.setData(taker.sell.map(p => ({ time: p.time, value: p.value === null ? null : p.value, color: DOWN })));
  }
  if (!single) {
    chart.priceScale("").applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
  }
  document.getElementById("status-source").textContent =
    sourceLabel(S.source) + (data.length && data.every(d => d.value === null) ? " (no data in loaded range)" : "");
}

function sourceLabel(key) {
  const m = S.sourcesMeta.find(s => s.key === key);
  return m ? m.label : key;
}

/* ---------------- markers ---------------- */
function renderMarkers() {
  const markers = [];
  for (const b of S.bars) {
    if (b.cross_filled) markers.push({ time: b.open_time / 1000, position: "belowBar",
      color: AMBER, shape: "circle", text: "cross-filled" });
    else if (b.follows_gap) markers.push({ time: b.open_time / 1000, position: "belowBar",
      color: MUTED, shape: "circle", text: "gap" });
  }
  // large-trade markers: top-decile days of large_trade_notional within loaded data
  const vals = [...S.breakdown.values()].map(bd => bd.large_trade_notional)
    .filter(v => v !== null && v !== undefined && !isNaN(v) && v > 0).sort((a, b) => a - b);
  if (vals.length >= 20) {
    const thresh = vals[Math.floor(vals.length * 0.90)];
    for (const b of S.bars) {
      const bd = S.breakdown.get(b.open_time);
      if (bd && bd.large_trade_notional !== null && bd.large_trade_notional !== undefined
          && bd.large_trade_notional > thresh) {
        markers.push({ time: b.open_time / 1000, position: "aboveBar", color: PURPLE,
          shape: "arrowUp", text: "large" });
      }
    }
    document.getElementById("status-large").textContent =
      "large-trade marker: >" + fmtUsd(thresh) + " (top decile of loaded range)";
  }
  candleSeries.setMarkers(markers);
}

/* ---------------- data loading ---------------- */
async function loadInterval() {
  S.breakdown = new Map(); S.breakdownPending.clear();
  const limit = S.interval === "1m" ? 250000 : 300000;
  const resp = await api(`/api/ohlc?symbol=${S.symbol}&interval=${S.interval}&limit=${limit}`);
  S.bars = resp.bars; S.gapSlots = resp.gap_slots;
  S.range = { start: S.bars.length ? S.bars[0].open_time : 0,
              end: S.bars.length ? S.bars[S.bars.length - 1].open_time : 0 };
  chart.timeScale().applyOptions({ timeVisible: S.interval !== "1d", secondsVisible: false });
  candleSeries.setData(S.bars.map(b => ({ time: b.open_time / 1000, open: b.open,
    high: b.high, low: b.low, close: b.close })));
  S.sourcesMeta = (await api(`/api/volume_breakdown?symbol=${S.symbol}&interval=${S.interval}&limit=1`)).sources;
  populateSourceSelect();
  await refreshBreakdown();
  renderVolume();
  renderMarkers();
  updateStatus();
  await loadProfile();
  chart.timeScale().scrollToRealTime();
}
async function loadOlder() {
  const span = S.range.end - S.range.start;
  const newStart = S.range.start - Math.ceil(span * 0.75);
  const resp = await api(`/api/ohlc?symbol=${S.symbol}&interval=${S.interval}` +
    `&start=${newStart}&end=${S.range.start - 1}&limit=250000`);
  if (!resp.bars.length) { S.range.start = newStart; return; }
  const older = resp.bars.filter(b => b.open_time < S.range.start);
  S.bars = older.concat(S.bars);
  S.gapSlots = [...resp.gap_slots.filter(g => g < S.range.start), ...S.gapSlots];
  S.range.start = older.length ? older[0].open_time : S.range.start;
  candleSeries.setData(S.bars.map(b => ({ time: b.open_time / 1000, open: b.open,
    high: b.high, low: b.low, close: b.close })));
  renderVolume(); renderMarkers(); updateStatus();
}
async function refreshBreakdown() {
  if (!S.visible) return;
  const seq = ++S.fetchSeq;
  const margin = (S.visible.to - S.visible.from) * 0.5;
  const start = Math.max(0, S.visible.from - margin), end = S.visible.to + margin;
  try {
    const resp = await api(`/api/volume_breakdown?symbol=${S.symbol}&interval=${S.interval}` +
      `&start=${Math.floor(start)}&end=${Math.ceil(end)}&limit=30000`);
    if (seq !== S.fetchSeq) return; // stale response
    if (resp.sources && resp.sources.length) {
      S.sourcesMeta = resp.sources;
      populateSourceSelect(); // e.g. large_trade source appears once computed
    }
    for (const b of resp.bars) {
      const src = b.sources;
      S.breakdown.set(b.open_time, {
        tape_rollup_volume: src.tape_rollup_volume,
        onchain_network_volume: src.onchain_network_volume,
        large_trade_notional: src.large_trade_notional,
        open_interest_delta: src.open_interest_delta,
        taker_buy: src.taker_buy_sell_split ? src.taker_buy_sell_split.buy : null,
        taker_sell: src.taker_buy_sell_split ? src.taker_buy_sell_split.sell : null,
        top_trader: src.top_trader_positioning,
      });
      S.breakdownPending.delete(b.open_time);
    }
    renderVolume(); renderMarkers(); updateStatus();
  } catch (e) { /* transient — next pan retries */ }
}

/* ---------------- tooltip ---------------- */
const tooltip = document.getElementById("tooltip");
chart.subscribeCrosshairMove(param => {
  if (!param.time || !param.point || !S.bars.length) { tooltip.style.display = "none"; return; }
  const bar = param.seriesData.get(candleSeries);
  if (!bar) { tooltip.style.display = "none"; return; }
  const openTime = Math.round(bar.time) * 1000;
  const b = S.bars.find(x => x.open_time === openTime);
  if (!b) { tooltip.style.display = "none"; return; }
  const bd = S.breakdown.get(openTime);
  const rows = [];
  const srcMeta = new Map(S.sourcesMeta.map(m => [m.key, m]));
  for (const m of S.sourcesMeta) {
    const key = m.key;
    let val;
    if (key === "taker_buy_sell_split") {
      val = bd ? `buy ${fmtNum(bd.taker_buy)} / sell ${fmtNum(bd.taker_sell)}` : null;
    } else if (key === "top_trader_positioning") {
      val = bd && bd.top_trader ? `long ${fmtPct(bd.top_trader.long_pct)} / short ${fmtPct(bd.top_trader.short_pct)} (skew ${(bd.top_trader.skew * 100).toFixed(1)}%)` : null;
    } else {
      const v = bd ? bd[key] : undefined;
      val = v === null || v === undefined ? null
        : key === "exchange_volume" || key === "tape_rollup_volume" ? fmtNum(v)
        : key === "onchain_network_volume" ? (m.units === "USD" ? fmtUsd(v) : fmtNum(v, 0))
        : key === "large_trade_notional" ? fmtUsd(v)
        : key === "open_interest_delta" ? (v >= 0 ? "+" : "") + fmtUsd(Math.abs(v))
        : fmtNum(v);
    }
    const row = bd === undefined
      ? `<tr><td class="k src-label">${m.label}</td><td class="v loading">loading…</td></tr>`
      : val === null
        ? `<tr><td class="k src-label">${m.label}</td><td class="v na">n/a</td></tr>`
        : `<tr><td class="k src-label">${m.label}<br><span class="src-meta">${m.units} · ${m.resolution} · ${tag(m.confidence)}</span></td><td class="v">${val}</td></tr>`;
    rows.push(row);
    void srcMeta;
  }
  let mkt = b.market === "spot" ? "spot" : "futures UM" + (b.resampled ? " (resampled from 1h)" : "");
  if (b.live) mkt = "LIVE " + mkt + " (Binance API)";
  const liveNote = b.live
    ? `<div class="holder-note" style="margin-top:6px">⚠ Live candle — preliminary. Only exchange-reported volume
       exists for it; the true-volume sources (tape, on-chain, positioning) arrive with the next Vision
       publish (~24 h lag, usually by ~16:30 UTC+8 the next day).</div>`
    : "";
  tooltip.innerHTML = `<h3>${fmtTs(openTime, S.interval)} · ${mkt}${b.cross_filled ? " · cross-filled" : ""}${b.follows_gap ? " · follows gap" : ""}${b.live ? " · ⚠" : ""}</h3>
    <div class="ohlc"><span>O ${fmtNum(b.open, 4)}</span><span>H <b style="color:${UP}">${fmtNum(b.high, 4)}</b></span>
    <span>L <b style="color:${DOWN}">${fmtNum(b.low, 4)}</b></span><span>C ${fmtNum(b.close, 4)}</span></div>
    <table>${rows.join("")}${profileTooltipRow(param.point.y)}</table>${liveNote}`;
  tooltip.style.display = "block";
  const pad = 14, tw = tooltip.offsetWidth, th = tooltip.offsetHeight;
  let x = param.point.x + pad, y = param.point.y + pad;
  if (x + tw > window.innerWidth - 8) x = param.point.x - tw - pad;
  if (y + th > window.innerHeight - 8) y = param.point.y - th - pad;
  tooltip.style.left = x + "px"; tooltip.style.top = y + "px";
});
chart.subscribeCrosshairMove(p => { if (!p.time) tooltip.style.display = "none"; });

/* ---------------- status bar ---------------- */
function updateStatus() {
  const cov = S.bars.length ? `${fmtTs(S.range.start, S.interval)} → ${fmtTs(S.range.end, S.interval)}` : "—";
  const gapsInView = S.bars.filter(b => b.follows_gap).length;
  const cross = S.bars.filter(b => b.cross_filled).length;
  const resampled = S.bars.filter(b => b.resampled).length;
  const live = S.bars.filter(b => b.live).length;
  const bar = document.getElementById("statusbar");
  bar.innerHTML = `<span>Symbol <b>${S.symbol}</b></span>
    <span>Interval <b>${S.interval}</b></span>
    <span>Loaded <b>${cov}</b> (${fmtNum(S.bars.length, 0)} bars)</span>
    <span>Gap-flagged <b>${gapsInView}</b>${S.gapSlots.length ? ` (+${S.gapSlots.length} empty slots)` : ""}</span>
    <span>Cross-filled <b>${cross}</b></span>
    <span>Resampled <b>${resampled}</b></span>
    ${live ? `<span>⚠ Live today <b>${live} bars</b> — price only (Binance API) · true volume expected with the next Vision publish (~24 h lag)</span>` : ""}
    <span id="status-source"></span>
    <span id="status-large"></span>
    <span id="status-profile"></span>
    <span class="mkt-note">OHLC: spot → 2024-12-31, then futures UM → latest (refreshed before each session)</span>`;
  updateProfileStatus();   // the span above was just replaced; refill it
}

/* ---------------- controls ---------------- */
function populateSymbolSelect() {
  const sel = document.getElementById("symbol");
  sel.innerHTML = "";
  for (const s of S.audited) {
    const o = document.createElement("option");
    o.value = s.symbol; o.textContent = `${s.symbol} — ${s.name} (audited)`;
    sel.appendChild(o);
  }
  sel.value = S.symbol;
}
function populateSourceSelect() {
  const sel = document.getElementById("source");
  const current = sel.value;
  sel.innerHTML = "";
  const order = ["onchain_network_volume", "exchange_volume", "tape_rollup_volume",
                 "large_trade_notional", "taker_buy_sell_split", "open_interest_delta"];
  const present = new Set(S.sourcesMeta.map(m => m.key));
  for (const key of order) {
    if (!present.has(key)) continue;
    const m = S.sourcesMeta.find(x => x.key === key);
    const o = document.createElement("option");
    o.value = key;
    o.textContent = (key === "onchain_network_volume" ? "★ " : "") + m.label;
    sel.appendChild(o);
  }
  if (present.has(current)) sel.value = current; else sel.value = "onchain_network_volume";
  S.source = sel.value;
  localStorage.setItem("vsa_source", S.source);
}
document.getElementById("symbol").addEventListener("change", e => {
  S.symbol = e.target.value; loadInterval();
});
document.getElementById("source").addEventListener("change", e => {
  S.source = e.target.value;
  localStorage.setItem("vsa_source", S.source);
  renderVolume();
});
function buildIntervalButtons() {
  const wrap = document.getElementById("intervals");
  wrap.innerHTML = "";
  const symInfo = S.audited.find(s => s.symbol === S.symbol);
  const intervals = symInfo ? symInfo.intervals : ["1h", "1d"];
  for (const iv of intervals) {
    const b = document.createElement("button");
    b.textContent = iv;
    b.dataset.iv = iv;
    b.addEventListener("click", () => {
      S.interval = iv;
      wrap.querySelectorAll("button").forEach(x => x.classList.toggle("active", x.dataset.iv === iv));
      loadInterval();
    });
    wrap.appendChild(b);
  }
}

/* ---------------- volume profile loading + controls ----------------
 * Deliberately NOT driven by the visible time range: the profile is a summary of
 * the selected window, so it must not be re-fetched or re-shaped as the user pans.
 * (refreshBreakdown() is range-scoped with a 50% margin, which would be wrong for
 * an All-window profile — so this takes the symbol/interval/window path instead.) */
async function loadProfile() {
  const key = S.symbol + "_" + S.profileWindow;
  try {
    let d = S.profileCache.get(key);
    if (!d) {
      d = await api(`/api/volume_profile?symbol=${S.symbol}&window=${S.profileWindow}`);
      S.profileCache.set(key, d);
    }
    profileState.data = d;
  } catch (e) {
    profileState.data = null;
    const el = document.getElementById("status-profile");
    if (el) el.innerHTML = `<span class="mkt-note">Volume profile unavailable — ${e.message}</span>`;
    requestRedraw();
    return;
  }
  requestRedraw();
  updateProfileStatus();
}
function updateProfileStatus() {
  const el = document.getElementById("status-profile");
  if (!el) return;
  const d = profileState.data;
  if (!d) return;
  const va = d.value_area || {};
  const rp = d.realized_price ? ` · <b>$${fmtNum(d.realized_price.value, 0)}</b> realized` : "";
  const ur = d.urpd && d.urpd.buckets
    ? ` · <span class="legend-urpd">on-chain cost basis</span> ${String(d.urpd.captured_utc).slice(0, 10)}`
    : "";
  el.innerHTML = `<span>Profile <b>${d.window}</b> (${d.n_days}d to ${d.as_of}) — `
    + `bars: traded notional / $${fmtNum(d.bucket_usd, 0)} · `
    + `POC <b>$${fmtNum(d.poc, 0)}</b> · value area <b>$${fmtNum(va.low, 0)}–$${fmtNum(va.high, 0)}</b>`
    + rp + ur + `</span>`;
}
function profileTooltipRow(paneY) {
  const d = profileState.data;
  if (!d || !d.buckets || !d.buckets.length) return "";
  const price = candleSeries.coordinateToPrice(paneY);
  if (price === null || price === undefined || !isFinite(price)) return "";
  const w = d.bucket_usd;
  const b = d.buckets.find(x => Math.floor(x.p / w) === Math.floor(price / w));
  if (!b) return `<tr><td class="k src-label">Volume profile <span class="src-meta">${d.window} · traded notional</span></td>`
    + `<td class="v na">no volume at this price</td></tr>`;
  const pct = d.total_quote_usd ? (b.q / d.total_quote_usd) * 100 : 0;
  const tot = b.buy + b.sell;
  const buyPct = tot > 0 ? (b.buy / tot) * 100 : 50;
  const isPoc = Math.abs(b.p - d.poc) < w / 2;
  return `<tr><td class="k src-label">Volume profile${isPoc ? " · POC" : ""}`
    + `<br><span class="src-meta">$${fmtNum(b.p, 0)}–$${fmtNum(b.p + w, 0)} · ${d.window} · futures tape</span></td>`
    + `<td class="v">${fmtUsd(b.q)}<br><span class="src-meta">${pct.toFixed(2)}% of window · `
    + `${fmtNum(b.v)} ${d.base} · ${buyPct.toFixed(0)}% taker-buy</span></td></tr>`;
}
function buildProfileButtons() {
  const wrap = document.getElementById("profile-window");
  if (!wrap) return;
  const windows = ["30d", "90d", "1y", "all"];
  wrap.innerHTML = "";
  for (const w of windows) {
    const b = document.createElement("button");
    b.textContent = w === "all" ? "All" : w;
    b.dataset.pw = w;
    if (w === S.profileWindow) b.classList.add("active");
    b.addEventListener("click", () => {
      S.profileWindow = w;
      localStorage.setItem("vsa_profile_window", w);
      wrap.querySelectorAll("button").forEach(x => x.classList.toggle("active", x.dataset.pw === w));
      loadProfile();
    });
    wrap.appendChild(b);
  }
}
let visibleTimer = null;
chart.timeScale().subscribeVisibleTimeRangeChange(r => {
  if (!r || !r.from || !r.to) return;
  S.visible = { from: r.from * 1000, to: r.to * 1000 };
  if (S.bars.length && S.range.start > 0 &&
      (S.range.start - r.from * 1000) < (S.range.end - S.range.start) * 0.25) {
    loadOlder();
  }
  clearTimeout(visibleTimer);
  visibleTimer = setTimeout(refreshBreakdown, 250);
});

/* ---------------- holder panel ---------------- */
async function loadHolder() {
  const box = document.getElementById("holder-content");
  try {
    const h = await api(`/api/onchain_snapshot?symbol=${S.symbol}`);
    let html = `<div class="holder-grid">`;
    html += `<div class="holder-card"><h3>Long-Term / Short-Term Holder Cohorts</h3>
      <div class="holder-row"><span>LTH supply share</span><span class="na">not yet available</span></div>
      <div class="holder-row"><span>STH supply share</span><span class="na">not yet available</span></div>
      <div class="holder-note">${h.note || h.lth_sth_note || ""}</div></div>`;
    const r = h.exchange_reserve_estimate;
    if (r && r.series && r.series.length) {
      const pts = r.series.map(([ms, v]) => ({ ms, v }));
      const min = Math.min(...pts.map(p => p.v)), max = Math.max(...pts.map(p => p.v));
      const W = 300, H = 40;
      const step = pts.length > 1 ? (pts[pts.length - 1].ms - pts[0].ms) / (pts.length - 1) : 1;
      const path = pts.map((p, i) => {
        const x = i === pts.length - 1 ? W : ((p.ms - pts[0].ms) / step) * (W / Math.max(pts.length - 1, 1));
        const y = H - 3 - (p.v - min) / (max - min || 1) * (H - 6);
        return (i === 0 ? "M" : "L") + x.toFixed(1) + "," + y.toFixed(1);
      }).join(" ");
      html += `<div class="holder-card"><h3>Exchange Reserve Estimate — ${r.units}</h3>
        <svg class="sparkline" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">
          <path d="${path}" fill="none" stroke="${AMBER}" stroke-width="1.5"/></svg>
        <div class="holder-row"><span>Latest</span><span><b>${fmtNum(r.value)} ${r.units}</b> (${r.as_of})</span></div>
        <div class="holder-note">${r.availability}</div></div>`;
    } else {
      html += `<div class="holder-card"><h3>Exchange Reserve Estimate</h3>
        <div class="holder-row"><span>Series</span><span class="na">not yet available</span></div>
        <div class="holder-note">${h.data_availability}</div></div>`;
    }
    if (h.urpd && h.urpd.available) {
      const maxB = Math.max(...h.urpd.top_buckets.map(b => b.supply_btc));
      const rows = h.urpd.top_buckets.map(b =>
        `<div class="urpd-row"><span style="width:110px">≤ $${fmtNum(b.price_bucket_usd, 0)}</span>
         <div class="bar" style="width:${Math.round(b.supply_btc / maxB * 120)}px"></div>
         <span class="amt">${fmtNum(b.supply_btc)} BTC</span></div>`).join("");
      html += `<div class="holder-card"><h3>URPD Snapshot (${h.urpd.captured_utc.slice(0, 10)}, ${h.urpd.n_buckets} buckets)</h3>
        <div class="urpd-bars">${rows}</div>
        <div class="holder-note">${h.urpd.note} · total supply ${fmtNum(h.urpd.total_supply_btc)} BTC</div></div>`;
    } else {
      html += `<div class="holder-card"><h3>URPD Snapshot</h3>
        <div class="holder-row"><span>Available</span><span class="na">${h.urpd ? h.urpd.note : "not available"}</span></div></div>`;
    }
    html += `</div><div class="holder-note">As-of: ${h.as_of || "—"} · ${h.data_availability}</div>`;
    box.innerHTML = html;
  } catch (e) {
    box.innerHTML = `<div class="holder-empty">holder panel failed to load: ${e.message}</div>`;
  }
}
document.getElementById("holder-panel").addEventListener("toggle", () => {
  if (document.getElementById("holder-panel").open && !S.holder) loadHolder();
});

/* ---------------- boot ---------------- */
(async function boot() {
  try {
    try { STATIC = await loadJSONFile("manifest.json"); }
    catch (e) { STATIC = null; }        // no manifest -> local server mode
    if (STATIC && STATIC.data_version)
      document.getElementById("data-note").textContent =
        "Data through " + STATIC.data_version + " — plus live today's candles (price only, Binance API). " +
        "True volume for today arrives with the next Vision publish (~24 h lag, usually by ~16:30 UTC+8 the next day); " +
        "re-open this page after the daily refresh+publish for full data.";
    const sym = await api("/api/symbols");
    S.audited = sym.audited;
    S.defaultInterval = sym.default_interval;
    buildIntervalButtons();
    document.getElementById("intervals").querySelector(`[data-iv="${S.interval}"]`)?.classList.add("active");
    buildProfileButtons();
    populateSymbolSelect();
    await loadInterval();
    await loadHolder();
  } catch (e) {
    document.getElementById("statusbar").textContent = "Failed to start: " + e.message;
  }
})();
