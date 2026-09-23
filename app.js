/* TA+VSA Visualizer — frontend logic.
 * Candlesticks + multi-source volume histogram over the audited proxy stack.
 * The hover tooltip always shows the FULL breakdown; the selector only changes
 * what the volume panel renders. No external network calls at render time. */

"use strict";

const UP = "#26a69a", DOWN = "#ef5350", MUTED = "#787b86", AMBER = "#f59e0b", PURPLE = "#b07dff";

// Cost-basis windows the API can answer. The first four are the publisher's own
// diffs; "4y" is the one window it never built -- there is no `urpd_diff_1460`
// upstream -- so the backend computes it from two archived curves, and its
// status line carries the coverage caveat that comes with that.
// The 14-day window is retired upstream (the live host 404s it), so it is gone
// here, from the API's own list, and from the buttons; the remembered value is
// validated against this list on every load, so a window this build cannot
// answer falls back to the default rather than selecting a control that can only
// render the unavailable state.
const CB_WINDOWS = ["30d", "90d", "180d", "365d", "4y"];
const CB_DEFAULT = "30d";
// A window of the reader's own choosing: two arbitrary dates, subtracted from
// the weekly dated-curve archive. Not a button in CB_WINDOWS, because it is a
// state the date inputs put the control into rather than one more preset.
const CB_CUSTOM = "custom";
// The archive's own grid, in USD. Its pairs are served at this width, NOT at the
// mixed-store CB_ZONE_USD below: both ends come from the same $200 store, and a
// $200 grid is what makes a fortnight-wide window readable.
const CB_ARCHIVE_ZONE_USD = 200;
// Who Moved still groups into $1,000 levels whatever the payload's grid is: one
// cohort of owners is spread across ~10 adjacent $200 buckets, so ranking raw
// buckets shatters a single wall into fragments that each look small. A finer
// payload must not fragment the table.
const CB_ZONE_MIN_USD = 1000;
const CB_ISO_RE = /^\d{4}-\d{2}-\d{2}$/;

function cbCustomFromStorage() {
  try {
    const c = JSON.parse(localStorage.getItem("vsa_cb_custom_v1") || "null");
    if (c && CB_ISO_RE.test(c.from) && CB_ISO_RE.test(c.to) && c.from < c.to) return c;
  } catch (e) { /* unreadable -> no remembered pair */ }
  return null;
}
// Read once, here, because the two must agree: "custom" is only a valid window
// if there is a remembered date pair to ask for.
const CB_SAVED_PAIR = cbCustomFromStorage();
const CB_SAVED_WINDOW = localStorage.getItem("vsa_cb_window_v2");

const S = {
  // On entry the chart opens on daily bars -- see boot(), which adopts the
  // interval from /api/symbols rather than trusting this literal.
  symbol: "BTCUSDT", interval: "1d",
  bars: [], gapSlots: [], sourcesMeta: [], sourceOpts: [],
  breakdown: new Map(), breakdownPending: new Set(),
  range: { start: 0, end: 0 }, visible: null,
  source: localStorage.getItem("vsa_source") || "onchain_network_volume",
  audited: [], defaultInterval: "1d",
  holder: null, fetchSeq: 0,
  profileWindow: localStorage.getItem("vsa_profile_window") || "90d",
  profileCache: new Map(),
  // The cost-basis window is INDEPENDENT of the profile window: they measure
  // different things (traded notional vs supply that changed hands), so forcing
  // one control to drive both would silently couple two unrelated questions.
  // "custom" is accepted only with a remembered pair to go with it -- a bare
  // "custom" would ask the API for two dates it was never given.
  cbCustom: CB_SAVED_PAIR,
  cbWindow: (CB_SAVED_WINDOW === CB_CUSTOM && CB_SAVED_PAIR)
    ? CB_CUSTOM
    : (CB_WINDOWS.includes(CB_SAVED_WINDOW) ? CB_SAVED_WINDOW : CB_DEFAULT),
  cbCache: new Map(),
  cbArchive: null,
  // status-bar text that only the volume/marker renderers can compute. Held here,
  // not written straight to the DOM: updateStatus() rebuilds the bar wholesale,
  // so a span written before that rebuild is written into nothing. See
  // updateSourceStatus().
  statusSource: "", statusLarge: "",
  // Candle annotations, OFF on entry. Drawn across the whole loaded range they
  // bury the candles at 1h/1d zoom, which is the state the chart should open in;
  // the toggles are there for when they are wanted. A display switch only -- the
  // breakdown data and the status counts show either way.
  // The keys are the authority for the toggle buttons too (MARKER_TOGGLES below),
  // so the write and the read cannot drift apart.
  markersLarge: localStorage.getItem("vsa_markers_large_v2") === "1",
  markersFills: localStorage.getItem("vsa_markers_fills_v2") === "1",
};

// The marker toggles, as built into index.html. `skey` is the S flag, `ls` the
// storage key -- both are read (sync) and written (click) through this one list.
// The `_v2` suffix is deliberate: the v1 keys were written under the old
// default-ON semantics and, for cross-filled, under a name the reader never
// looked at (the id is "mk-fills" but the flag is `markersFills`), so nothing
// stored under v1 can be read as today's intent -- a returning visitor would
// otherwise never see the new default.
const MARKER_TOGGLES = [
  { id: "mk-large", name: "large", skey: "markersLarge", ls: "vsa_markers_large_v2",
    label: "large-trade arrows (top-decile notional)" },
  { id: "mk-fills", name: "cross-filled", skey: "markersFills", ls: "vsa_markers_fills_v2",
    label: "cross-filled / gap labels" },
];

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
/* `sep` is opt-in: the cost-basis movers quote a change against a supply level
 * that can be near zero (365d at $84k: +714.94K BTC off a 6.4K base), and
 * "+11054.8%" is unreadable where "+11,054.8%" scans. Every other caller gets
 * the unchanged bare form. */
function fmtPct(v, sep = false) {
  if (v === null || v === undefined || isNaN(v)) return "—";
  const s = (v * 100).toFixed(1);
  return (sep ? Number(s).toLocaleString("en-US", { minimumFractionDigits: 1, maximumFractionDigits: 1 }) : s) + "%";
}
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
  if (what === "etf_flows") {
    // One file per symbol and no window: the series is daily, so it does not
    // change with the chart interval. ETH's file is the endpoint's own
    // honest-unavailable body, not an absent one the page must interpret.
    const ef = STATIC.files.etf;
    if (!ef) throw new Error("this export has no ETF flows — re-publish the site");
    return loadJSONFile(ef[sym]);
  }
  if (what === "volume_profile") {
    const pf = STATIC.files.profile;
    if (!pf) throw new Error("this export has no volume profile — re-publish the site");
    return loadJSONFile(pf[sym + "_" + (q.window || "90d")]);
  }
  if (what === "cost_basis") {
    // A window of the reader's own choosing has no precomputed file to read --
    // there are infinitely many date pairs. The archive ships whole instead and
    // the pair is subtracted here (see cbPairFromArchive).
    if (q.window === CB_CUSTOM) return cbPairFromArchive(sym, q.from, q.to);
    const cbf = STATIC.files.cost_basis;
    if (!cbf) throw new Error("this export has no cost-basis overlay — re-publish the site");
    return loadJSONFile(cbf[sym + "_" + (q.window || CB_DEFAULT)]);
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
/* `visible` is the eye toggle beside each window control. Both default to on and
 * are remembered, like the source and window choices — a hidden layer is a
 * preference about the chart, not a transient hover state. Stored as "0"/"1" so
 * "absent" (first visit) reads as visible rather than false. */
const LAYER_VISIBLE_KEY = "vsa_layer_visible";
function layerVisible(name) {
  return localStorage.getItem(LAYER_VISIBLE_KEY + "_" + name) !== "0";
}
const profileState = { data: null, visible: layerVisible("profile") };
/* Cost basis (on-chain URPD): supply bucketed by the price each coin last moved
 * at — i.e. what its owner paid — and how those buckets changed between two
 * stamped dates. Same primitive as the profile (one canvas, one repaint path);
 * only the anchor differs. */
/* `drawn` caches the regridded bars the last paint actually drew, so the tooltip
 * names the same price band the user is pointing at (see drawProfile). */
const costBasisState = { data: null, drawn: null, visible: layerVisible("costbasis") };
const AMBER_RGB = [245, 158, 11], GREEN_RGB = [38, 166, 154], RED_RGB = [239, 83, 80];
function mixRgb(a, b, t) {
  t = Math.max(0, Math.min(1, t));
  return `rgb(${Math.round(a[0] + (b[0] - a[0]) * t)},${Math.round(a[1] + (b[1] - a[1]) * t)},${Math.round(a[2] + (b[2] - a[2]) * t)})`;
}
function drawProfile(target) {
  const d = profileState.data;
  const cb = costBasisState.data;
  const hasProfile = !!(d && d.buckets && d.buckets.length);
  const hasCb = !!(cb && cb.available && cb.delta && cb.delta.length && cb.bucket_usd > 0);
  // Two independent payloads share this one primitive. Neither may take the
  // other down with it: if the profile fails to load, the cost-basis layer
  // still has to paint, and vice versa.
  if (!hasProfile && !hasCb) { costBasisState.drawn = null; return; }
  target.useMediaCoordinateSpace(({ context: ctx, mediaSize }) => {
    const right = mediaSize.width, H = mediaSize.height;
    const maxLen = right * 0.34;              // never swamp the candles

    // On-chain URPD layer: cost-basis past vs now and the change between them,
    // or — when cost basis cannot cover the symbol (ETH; URPD is BTC-UTXO-only)
    // or its fetch failed — the one-snapshot silhouette. ONE eye governs both,
    // because they are the same quantity at two levels of detail, and the same
    // thing must not take two switches to turn off.
    //
    // Drawn before the profile body so it sits behind the value-area band and
    // the tape bars, and BEFORE the returns below so it still paints when the
    // profile is hidden or its fetch failed.
    if (costBasisState.visible) {
      if (hasCb) drawCostBasis(ctx, right, H, maxLen);
      else costBasisState.drawn = null;
    } else {
      costBasisState.drawn = null;
    }
    const u = hasProfile ? d.urpd : null;
    if (costBasisState.visible && !hasCb && u && u.buckets && u.buckets.length > 1) {
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
    if (!hasProfile || !profileState.visible) return;

    const w = d.bucket_usd;
    let maxQ = 0;
    for (const b of d.buckets) if (b.q > maxQ) maxQ = b.q;
    if (!(maxQ > 0)) return;


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
/* Cost-basis distribution: how much supply sat at each acquisition price on the
 * two stamped dates, and the change between them. Right-anchored at the same
 * edge as the tape profile — NOT in a gutter of its own — because the whole
 * point is to read a price level's cohort against price itself. It replaces the
 * single-snapshot silhouette that used to occupy this slot (see drawProfile),
 * so `now` costs nothing extra and `past` finally gets drawn at all.
 *
 * Reading it: the hollow outline is where supply sat `prev_as_of`; the filled
 * bar is where it sits `as_of`; the coloured cap between the two ends IS the
 * change, so a long red cap at a price means coins left that level. */
function drawCostBasis(ctx, right, H, maxLen) {
  const cb = costBasisState.data;
  const cw = cb.bucket_usd;
  // 1) Regrid the fine source grid ($200 in the frozen mirror generation, $500 on
  // the live host) up to what this zoom can actually show. At a full-history zoom
  // one such bucket is a fraction of a pixel, so the fine grid
  // would draw nothing at all (every bar culled by the same boundary guard the
  // tape uses). The tape profile solves this server-side with a display width;
  // this layer is a snapshot that has to answer to zoom without a refetch, so it
  // regrids here on every repaint instead.
  const pTop = candleSeries.coordinateToPrice(0);
  const pBot = candleSeries.coordinateToPrice(H);
  let mult = 1;
  if (pTop !== null && pBot !== null && pTop > pBot) {
    mult = Math.max(1, Math.ceil((pTop - pBot) / cw / 140));      // ~140 rows
    const NICE = [1, 2, 5, 10, 20, 25, 50, 100, 125, 250, 500, 1000, 2500];
    mult = NICE.find(m => m >= mult) || mult;
  }
  const aggW = cw * mult;
  const groups = new Map();
  for (const x of cb.delta) {
    const k = Math.floor(x.p / aggW);
    let g = groups.get(k);
    if (!g) { g = { p: k * aggW, d: 0, past: 0, now: 0 }; groups.set(k, g); }
    g.d += x.d;                                       // delta is additive supply
    g.past += (x.past || 0);
    g.now += (x.now || 0);
  }
  const rows = Array.from(groups.values());
  // the tooltip reads the SAME grouping that was drawn, so the price band it
  // names is always the band under the cursor
  costBasisState.drawn = { w: aggW, rows };

  // 2) ONE scale over BOTH curves. Scaling past and now independently would
  // erase the comparison entirely, and scaling on the delta (as the old
  // left-edge bars did) would collapse both curves to slivers.
  let scale = 0;
  for (const g of rows) {
    const m = Math.max(g.past || 0, g.now || 0);
    if (m > scale) scale = m;
  }
  if (!(scale > 0)) return;
  const maxLenC = maxLen * 0.95;
  const PAST_STROKE = "rgba(176, 125, 255, 0.55)";
  const NOW_FILL = "rgba(176, 125, 255, 0.16)";
  for (const g of rows) {
    const yTop = candleSeries.priceToCoordinate(g.p + aggW);
    const yBot = candleSeries.priceToCoordinate(g.p);
    if (yTop === null || yBot === null) continue;
    if (yBot <= 0 || yTop >= H) continue;     // same boundary guard as the tape
    const bh = yBot - yTop;
    if (bh < 0.5) continue;
    const hFill = Math.max(0.5, bh - 0.5);
    const lenPast = Math.min(maxLenC, ((g.past || 0) / scale) * maxLenC);
    const lenNow = Math.min(maxLenC, ((g.now || 0) / scale) * maxLenC);
    // Order matters: `now` is translucent, so the cap has to land on top of it
    // where the level grew. The past outline goes last — it is the reference
    // line the other two are read against, and must stay crisp.
    if (lenNow >= 0.5) {
      ctx.fillStyle = NOW_FILL;
      ctx.fillRect(right - lenNow, yTop, lenNow, hFill);
    }
    if (lenNow > lenPast + 0.5) {              // level grew: coins parked here
      ctx.fillStyle = "rgba(38, 166, 154, 0.75)";
      ctx.fillRect(right - lenNow, yTop, lenNow - lenPast, hFill);
    } else if (lenPast > lenNow + 0.5) {       // level shrank: coins spent here
      ctx.fillStyle = "rgba(239, 83, 80, 0.75)";
      ctx.fillRect(right - lenPast, yTop, lenPast - lenNow, hFill);
    }
    if (lenPast >= 0.5) {
      ctx.strokeStyle = PAST_STROKE;
      ctx.lineWidth = 1;
      ctx.strokeRect(right - lenPast, yTop + 0.5, lenPast, hFill);
    }
  }
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
  S.statusSource = sourceLabel(S.source) +
    (data.length && data.every(d => d.value === null) ? " (no data in loaded range)" : "");
  updateSourceStatus();
}

function sourceLabel(key) {
  const m = S.sourcesMeta.find(s => s.key === key);
  return m ? m.label : key;
}

/* ---------------- markers ----------------
 * Two annotation layers, each with its own switch in the toolbar:
 *   fills  -- below-bar labels ("cross-filled", "gap"). One switch covers both:
 *             they are the same layer and the same kind of statement (how this
 *             bar's data was assembled), and a gap marker only draws on bars
 *             that are not cross-filled, so a third button would be dead weight.
 *   large  -- above-bar arrows on the top-decile large-trade bars.
 * Switching a layer off is a repaint, never a refetch: S.breakdown and the
 * counts in the status strip keep reporting exactly what they reported before. */
function renderMarkers() {
  const markers = [];
  if (S.markersFills) {
    for (const b of S.bars) {
      if (b.cross_filled) markers.push({ time: b.open_time / 1000, position: "belowBar",
        color: AMBER, shape: "circle", text: "cross-filled" });
      else if (b.follows_gap) markers.push({ time: b.open_time / 1000, position: "belowBar",
        color: MUTED, shape: "circle", text: "gap" });
    }
  }
  // large-trade markers: top-decile days of large_trade_notional within loaded data
  const vals = [...S.breakdown.values()].map(bd => bd.large_trade_notional)
    .filter(v => v !== null && v !== undefined && !isNaN(v) && v > 0).sort((a, b) => a - b);
  S.statusLarge = "";                    // too few bars -> no threshold, no note
  if (vals.length >= 20) {
    const thresh = vals[Math.floor(vals.length * 0.90)];
    if (S.markersLarge) {
      for (const b of S.bars) {
        const bd = S.breakdown.get(b.open_time);
        if (bd && bd.large_trade_notional !== null && bd.large_trade_notional !== undefined
            && bd.large_trade_notional > thresh) {
          markers.push({ time: b.open_time / 1000, position: "aboveBar", color: PURPLE,
            shape: "arrowUp", text: "large" });
        }
      }
    }
    S.statusLarge = "large-trade marker: >" + fmtUsd(thresh) +
      " (top decile of loaded range)" + (S.markersLarge ? "" : " — hidden");
  }
  updateLargeStatus();
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
  await loadCostBasis();
  renderEtf();   // its bars are clipped to the range just loaded
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
    <table>${rows.join("")}${etfTooltipRow(openTime)}${profileTooltipRow(param.point.y)}${costBasisTooltipRow(param.point.y)}</table>${liveNote}`;
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
    <span id="status-cb"></span>
    <span id="status-etf"></span>
    <span class="mkt-note">OHLC: spot → 2024-12-31, then futures UM → latest (refreshed before each session)</span>`;
  updateProfileStatus();   // the spans above were just replaced; refill them
  updateCostBasisStatus();
  updateEtfStatus();
  updateSourceStatus();
  updateLargeStatus();
}

/* The four spans above exist only inside the markup updateStatus() just wrote,
 * and the two renderers that know these two strings run BEFORE it in every load
 * path. So the text is kept in S and painted from here rather than written
 * straight into a span that may not exist yet — which is exactly what aborted
 * boot with "Cannot set properties of null (setting 'textContent')". */
function updateSourceStatus() {
  const el = document.getElementById("status-source");
  if (el) el.textContent = S.statusSource;
}
function updateLargeStatus() {
  const el = document.getElementById("status-large");
  if (el) el.textContent = S.statusLarge;
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
  // The ETF flows are a different series per symbol (and absent for ETH), so the
  // layer reloads with the symbol, unlike the interval which never changes it.
  S.symbol = e.target.value; loadInterval(); loadEtf();
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
  // the snapshot's own vintage, NOT captured_utc — that field is the fetch date
  // and is months later, which the old label silently passed off as the vintage
  const ur = d.urpd && d.urpd.buckets
    ? ` · <span class="legend-urpd">on-chain cost basis</span> to `
      + `${d.urpd.data_vintage || "— (frozen fallback store)"}`
    : "";
  el.innerHTML = `<span>Profile <b>${d.window}</b> (${d.n_days}d to ${d.as_of}) — `
    + `bars: traded notional / $${fmtNum(d.bucket_usd, 0)} · `
    + `POC <b>$${fmtNum(d.poc, 0)}</b> · value area <b>$${fmtNum(va.low, 0)}–$${fmtNum(va.high, 0)}</b>`
    + rp + ur + `</span>`;
}
function profileTooltipRow(paneY) {
  const d = profileState.data;
  if (!d || !d.buckets || !d.buckets.length) return "";
  // The eye hides the layer and its readout together: a hover row describing
  // bars that are not on screen is a claim with nothing behind it.
  if (!profileState.visible) return "";
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
/* ---------------- layer eyes ----------------
 * The eye beside each window control shows/hides that chart layer. It is a
 * display switch, not a data switch: the payload stays loaded and the reading
 * surfaces (status strip, the Who Moved card, hover rows) keep naming the same
 * numbers. Hiding the cost-basis layer also drops its two price lines, because
 * those annotate the distribution and would otherwise be a claim with nothing
 * behind it. */
function buildLayerEyes() {
  // `groups`, not `group`: the cost-basis layer is driven by two controls now --
  // the preset buttons and the custom date pair -- and an eye that dimmed only
  // one of them would leave the other looking active over a hidden layer.
  const eyes = [
    { id: "profile-eye", name: "profile", groups: ["profile-window"],
      label: "right-edge volume profile", state: profileState,
      refresh: () => requestRedraw() },
    { id: "cb-eye", name: "costbasis", groups: ["cb-window", "cb-custom"],
      label: "cost-basis distribution", state: costBasisState,
      refresh: () => { applyCostBasisPriceLines(); requestRedraw(); } },
    // No `groups`: this layer has no window control to dim, the eye is the whole
    // control. Hiding it drops the hover row too (etfTooltipRow returns ""), the
    // same contract the cost-basis layer keeps with its price lines.
    { id: "etf-eye", name: "etf", groups: [],
      label: "spot-ETF daily net flows", state: etfState,
      refresh: () => { renderEtf(); } },
  ];
  for (const e of eyes) {
    const btn = document.getElementById(e.id);
    if (!btn) continue;
    const sync = () => {
      const on = e.state.visible;
      btn.classList.toggle("off", !on);
      btn.setAttribute("aria-pressed", String(on));
      btn.title = (on ? "Hide" : "Show") + " the " + e.label;
      for (const g of e.groups) {
        document.getElementById(g)?.classList.toggle("dim", !on);
      }
    };
    btn.addEventListener("click", () => {
      e.state.visible = !e.state.visible;
      localStorage.setItem(LAYER_VISIBLE_KEY + "_" + e.name, e.state.visible ? "1" : "0");
      sync();
      // no refetch: hiding a layer is a repaint, and the payload is already here
      e.refresh();
    });
    sync();
  }
}
/* Marker switches. Same segmented-button look as the window controls, because
 * they are the same kind of control: a remembered display choice. Annotations
 * span the whole loaded range, so at 1h/1d they cover the candles entirely --
 * turning them off must not touch the data, only the paint. Both start off
 * (MARKER_TOGGLES / the S flags above), so `sync` on a fresh visit paints the
 * unpressed state and index.html's markup already agrees with it. */
function buildMarkerToggles() {
  for (const t of MARKER_TOGGLES) {
    const btn = document.getElementById(t.id);
    if (!btn) continue;
    btn.textContent = t.name;
    const sync = () => {
      const on = S[t.skey];
      btn.classList.toggle("active", on);
      btn.setAttribute("aria-pressed", String(on));
      btn.title = (on ? "Hide" : "Show") + " " + t.label;
    };
    btn.addEventListener("click", () => {
      S[t.skey] = !S[t.skey];
      localStorage.setItem(t.ls, S[t.skey] ? "1" : "0");
      sync();
      renderMarkers();   // repaint only: breakdown data is untouched
    });
    sync();
  }
}
/* ---------------- cost basis (on-chain URPD) ----------------
 * Same load/control/status/tooltip shape as the volume profile above, but a
 * separate window control on purpose: this answers "who is selling at what cost"
 * from on-chain supply, not "where did volume trade" from the futures tape, and
 * the two windows are legitimately different questions.
 * BTC-only — URPD needs per-UTXO data, so ETH returns the honest unavailable. */

/* A window of the reader's own choosing, computed in the browser.
 *
 * The server subtracts two dated curves out of urpd_archive.parquet; a published
 * site has no server, so the archive itself ships with the page and the pair is
 * subtracted here. This is a literal port of app.py's _cb_archive_pair and the
 * archive-pair branch of _cb_payload — same snap rule, same per-row summation
 * ORDER, same provenance sentence — and the probe diffs this payload against the
 * live one field-for-field precisely so the two cannot drift apart.
 *
 * Not one line of this runs unless the reader asks for a custom window: the
 * artifact is ~1 MB and decoding it on every visit would be a cost with no
 * question attached to it. */
const CB_ARCHIVE_SOURCE = "checkonchain_bitview_series_api";
const CB_ARCHIVE_CADENCE_DAYS = 7;
// A requested date may sit this far outside the archive and still snap to its
// nearest anchor; further out is a different question, not a date we happen to
// lack. Same value as app.CB_SNAP_TOLERANCE_DAYS.
const CB_SNAP_TOLERANCE_DAYS = 4;
// What an unavailable cost-basis answer looks like, field for field the same as
// the API's `empty` body.
const CB_EMPTY = {
  available: false, as_of: null, data_vintage: null, delta: [], past: null,
  current: null, price_usd: null, price_usd_prev: null,
  excluded_zero_price_btc: null,
};

/* UTC midnight of an ISO date. Parsed by hand, never with new Date("YYYY-MM-DD"):
 * that form is UTC by spec, but the arithmetic here has to be whole days, and a
 * local-time parse would silently shift it by one on the wrong side of midnight. */
function cbUtcMs(iso) {
  return Date.UTC(+iso.slice(0, 4), +iso.slice(5, 7) - 1, +iso.slice(8, 10));
}
function cbDaysBetween(aIso, bIso) {
  return Math.round((cbUtcMs(aIso) - cbUtcMs(bIso)) / 86400000);
}
function cbAddDays(iso, n) {
  return new Date(cbUtcMs(iso) + n * 86400000).toISOString().slice(0, 10);
}
/* Nearest archived date to `target`, ties going to the EARLIER date.
 * app._archive_snap is the other half of this rule: bisect to the insertion
 * point, compare the two neighbours, break ties by date. If the two ever
 * disagreed, the same window would read differently depending on whether the
 * server or the shipped copy answered it. */
function cbNearestDate(days, target) {
  let lo = 0, hi = days.length;
  while (lo < hi) { const m = (lo + hi) >> 1; if (days[m] < target) lo = m + 1; else hi = m; }
  const cands = days.slice(Math.max(0, lo - 1), lo + 1);
  if (!cands.length) return null;
  let best = cands[0];
  for (const d of cands) {
    const a = Math.abs(cbDaysBetween(d, target)), b = Math.abs(cbDaysBetween(best, target));
    if (a < b || (a === b && d < best)) best = d;
  }
  return best;
}
/* The archive is stored as one flat centi-BTC array per date, each date owning
 * `counts[i]` consecutive slots from $0 (see export_static_site._export_cb_archive).
 * Offsets are built once so a lookup is a slice, not a scan. */
function cbArchiveOffsets(ar) {
  if (ar.offsets) return ar.offsets;
  const off = new Array(ar.counts.length);
  let run = 0;
  for (let i = 0; i < ar.counts.length; i++) { off[i] = run; run += ar.counts[i]; }
  ar.offsets = off;
  return off;
}
async function cbLoadArchive(sym) {
  if (S.cbArchive) return S.cbArchive;
  const meta = (STATIC.cost_basis_archive || {})[sym];
  if (!meta) throw new Error("this export ships no dated-curve archive — re-publish the site");
  const ar = await loadJSONFile(meta.path);
  // grid_from_usd must be 0: every price in the payload is derived as
  // index * bucket_usd, so a grid that started anywhere else would put every bar
  // on the wrong level without a single value looking wrong.
  if (!ar || !ar.dates || !ar.counts || !ar.supply
      || ar.counts.length !== ar.dates.length || ar.grid_from_usd !== 0
      || !(ar.bucket_usd > 0)) {
    throw new Error("the shipped dated-curve archive is unreadable — re-publish the site");
  }
  S.cbArchive = ar;
  return ar;
}
/* The provenance sentence, in the API's own words. Duplicated rather than
 * dropped: the static page must not describe a differently-computed number, and
 * the probe compares this whole payload to the live one. */
function cbArchiveNote(prev, asOf, reqFrom, reqTo, coversTo) {
  const provenance = "COMPUTED LOCALLY from two dated curves in the weekly archive "
    + `(${prev} minus ${asOf}), on the archive's own ${fmtPx(CB_ARCHIVE_ZONE_USD)} grid: `
    + "the publisher never built this window. The archive holds one date per week, "
    + "so each end snapped to its nearest stored date"
    + ` — asked ${reqFrom} → ${reqTo}.`
    + (coversTo
        ? ` The ${prev} curve covers only to ${fmtPx(coversTo)}, so every zone above `
          + "that line is supply that arrived since, not a like-for-like pair."
        : " ")
    + ` The newest archive dates are still being restated upstream, so a window `
    + `ending at ${asOf} is provisional. `;
  return `Change in supply by acquisition price, ${prev} to ${asOf}. `
    + "Green = coins whose cost basis is here grew (accumulation); red = that "
    + "cohort was spent/moved (distribution). Coins leaving a bucket at a price "
    + "above their cost basis is realised profit, below it is realised loss. "
    + "cum_acc/cum_dist are cumulative shares measured UP the price axis (large "
    + "at the top of the range), not per bucket. " + provenance
    + `DATA VINTAGE ${asOf} — a dated measurement, not a live tape.`;
}
async function cbPairFromArchive(sym, reqFrom, reqTo) {
  const out = { ...CB_EMPTY, symbol: sym, window: CB_CUSTOM };
  if (!CB_ISO_RE.test(String(reqFrom || "")) || !CB_ISO_RE.test(String(reqTo || "")))
    throw new Error("window=custom needs both from=YYYY-MM-DD and to=YYYY-MM-DD");
  if (reqFrom >= reqTo)
    throw new Error(`from (${reqFrom}) must be earlier than to (${reqTo})`);
  // BTC-only, and the manifest says so by carrying an archive keyed by symbol:
  // URPD buckets supply by the price each UTXO last moved at, so no ETH
  // equivalent exists. The API's own words, not a paraphrase.
  if (!(STATIC.cost_basis_archive || {})[sym])
    return { ...out, note: "URPD buckets supply by the price each UTXO last moved "
      + "at, so it is a BTC-UTXO concept — no ETH equivalent exists." };
  const ar = await cbLoadArchive(sym);
  const days = ar.dates;
  const first = days[0], last = days[days.length - 1];
  const archive = { first: first, last: last, n_dates: days.length,
                    bucket_usd: ar.bucket_usd, cadence_days: ar.cadence_days };
  const refuse = (label, d) => ({ ...out, requested_from: reqFrom, requested_to: reqTo,
    archive: archive, note: `${label} ${d} is outside the archive, which holds one `
      + `date a week from ${first} to ${last}` });
  // ISO dates compare as strings, and so does the API's own bounds test
  const lo = cbAddDays(first, -CB_SNAP_TOLERANCE_DAYS);
  const hi = cbAddDays(last, CB_SNAP_TOLERANCE_DAYS);
  if (reqFrom < lo || reqFrom > hi) return refuse("from", reqFrom);
  if (reqTo < lo || reqTo > hi) return refuse("to", reqTo);
  const prev = cbNearestDate(days, reqFrom), cur = cbNearestDate(days, reqTo);
  const fromSnapped = prev !== reqFrom, toSnapped = cur !== reqTo;
  const asked = cbDaysBetween(reqTo, reqFrom);
  if (!prev || !cur || prev >= cur) {
    return { ...out, requested_from: reqFrom, requested_to: reqTo, archive: archive,
      note: `the archive holds one date a week and ${reqFrom} → ${reqTo} resolves `
        + `to a single anchor (${prev}) — pick a window at least a week wide` };
  }

  const off = cbArchiveOffsets(ar);
  const pi = days.indexOf(prev), ci = days.indexOf(cur);
  const nPast = ar.counts[pi], nNow = ar.counts[ci];
  const n = Math.max(nPast, nNow);         // the union of two grids from $0, step 200
  const past = new Array(n).fill(0), now = new Array(n).fill(0);
  for (let i = 0; i < nPast; i++) past[i] = ar.supply[off[pi] + i] / 100;
  for (let i = 0; i < nNow; i++) now[i] = ar.supply[off[ci] + i] / 100;
  const delta = new Array(n);
  const pos = new Array(n), neg = new Array(n);
  let posSum = 0, negSum = 0, net = 0;
  for (let i = 0; i < n; i++) {
    const d = now[i] - past[i];
    delta[i] = d;
    pos[i] = d > 0 ? d : 0;
    neg[i] = d < 0 ? -d : 0;
    posSum += pos[i]; negSum += neg[i];
    if (i > 0) net += d;                  // the $0 bucket is held out of net (see below)
  }
  const cumAcc = new Array(n), cumDist = new Array(n);
  let aRun = 0, dRun = 0;
  for (let i = 0; i < n; i++) {
    aRun += pos[i]; dRun += neg[i];
    cumAcc[i] = posSum ? aRun / posSum : 0;
    cumDist[i] = negSum ? dRun / negSum : 0;
  }
  // The grid is built from the archive's own bucket width, so the payload reports
  // that width -- which is what app._cb_payload measures back out of the frame's
  // price column (the smallest positive step of a dense $200 lattice is $200).
  const bucketUsd = ar.bucket_usd;
  // highest price at which the EARLIER curve still holds supply: above it the
  // change is supply that arrived since, not a like-for-like pair
  let coversTo = null;
  for (let i = n - 1; i >= 0; i--) if (past[i] > 0) { coversTo = i * ar.bucket_usd; break; }
  // the $0 bucket has no meaningful price: held out of the bars, reported against
  // excluded_zero_price_btc, and excluded from the net
  const row = i => ({ p: i * ar.bucket_usd, d: delta[i], past: past[i], now: now[i],
                      cum_acc: cumAcc[i], cum_dist: cumDist[i] });
  const body = [], pcurve = [], ccurve = [];
  for (let i = 1; i < n; i++) {         // index 0 is the $0 bucket: no meaningful price
    body.push(row(i));
    pcurve.push({ p: i * ar.bucket_usd, s: past[i] });
    ccurve.push({ p: i * ar.bucket_usd, s: now[i] });
  }
  const as_of = cur, prev_as_of = prev;
  return {
    symbol: sym, window: CB_CUSTOM, available: true,
    as_of: as_of, prev_as_of: prev_as_of, data_vintage: as_of,
    source: CB_ARCHIVE_SOURCE, units: "BTC", bucket_usd: bucketUsd,
    span_days: cbDaysBetween(as_of, prev_as_of),
    delta: body,
    past: { date: prev_as_of, buckets: pcurve },
    current: { date: as_of, buckets: ccurve },
    price_usd: ar.price_usd ? ar.price_usd[ci] : null,
    price_usd_prev: ar.price_usd ? ar.price_usd[pi] : null,
    net_delta_btc: net,
    excluded_zero_price_btc: { supply_btc: now[0], delta_btc: delta[0] },
    prev_curve_covers_to_usd: coversTo,
    computed: true,
    note: cbArchiveNote(prev_as_of, as_of, reqFrom, reqTo, coversTo),
    requested_from: reqFrom, requested_to: reqTo, requested_span_days: asked,
    from_snapped: fromSnapped, to_snapped: toSnapped,
    estimate_kind: "archive-pair", window_label: `${prev_as_of} → ${as_of}`,
    archive: archive,
  };
}

/* One place that builds the request path, and one that builds the cache key.
 * A custom window's identity is its date PAIR, not the word "custom" — keyed by
 * the word alone, the second pair asked for would be served the first one's
 * numbers straight out of the cache. */
function cbApiPath() {
  const base = `/api/cost_basis?symbol=${S.symbol}&window=${S.cbWindow}`;
  return (S.cbWindow === CB_CUSTOM && S.cbCustom)
    ? `${base}&from=${S.cbCustom.from}&to=${S.cbCustom.to}` : base;
}
function cbCacheKey() {
  const pair = (S.cbWindow === CB_CUSTOM && S.cbCustom)
    ? `${CB_CUSTOM}_${S.cbCustom.from}_${S.cbCustom.to}` : S.cbWindow;
  return S.symbol + "_" + pair;
}

async function loadCostBasis() {
  const key = cbCacheKey();
  try {
    let d = S.cbCache.get(key);
    if (!d) {
      d = await api(cbApiPath());
      S.cbCache.set(key, d);
    }
    costBasisState.data = d;
  } catch (e) {
    costBasisState.data = null;
    const el = document.getElementById("status-cb");
    if (el) el.innerHTML = `<span class="mkt-note">Cost basis unavailable — ${e.message}</span>`;
    requestRedraw();
    // the ranked card and the price lines are DOM/series state, not canvas, so
    // requestRedraw() does not touch them — they must be cleared explicitly
    renderCostBasisMovers();
    applyCostBasisPriceLines();
    return;
  }
  requestRedraw();
  updateCostBasisStatus();
  renderCostBasisMovers();
  applyCostBasisPriceLines();
}
/* What to call this window in prose. A preset window is its own name. A custom
 * one is the two dates the reader PICKED, because that is the question they
 * asked -- the dates the archive actually answered with are printed immediately
 * beside it, and again in the snap note when the two differ. Using the resolved
 * pair as the label instead would put the same two dates on screen twice and
 * hide which window was requested. */
function cbWindowLabel(d) {
  return d.requested_from
    ? `${d.requested_from} → ${d.requested_to}`
    : (d.window_label || d.window);
}
function updateCostBasisStatus() {
  const el = document.getElementById("status-cb");
  if (!el) return;
  const d = costBasisState.data;
  if (!d) return;
  if (!d.available) {
    el.innerHTML = `<span class="mkt-note">Cost basis unavailable — ${d.note || ""}</span>`;
    return;
  }
  const net = d.net_delta_btc;
  const dir = net >= 0 ? "net accumulation" : "net distribution";
  const px = (d.price_usd === null || d.price_usd === undefined) ? ""
    : ` · price ${fmtPx(d.price_usd)}${d.price_usd_prev === null || d.price_usd_prev === undefined
        ? "" : ` from ${fmtPx(d.price_usd_prev)}`}`;
  // A window the publisher never built is computed here from two archived
  // curves, and the older one's axis can stop below today's price range -- above
  // that line every zone reads as accumulation simply because there is no
  // "then" to compare against. Say so where the numbers are read.
  const cover = (d.computed && d.prev_curve_covers_to_usd)
    ? ` · <span class="mkt-note">computed locally from two archived curves; the `
      + `${d.prev_as_of} curve ends at ${fmtPx(d.prev_curve_covers_to_usd)}, so `
      + `every zone above that is accumulation since — not a like-for-like pair</span>`
    : "";
  // The archive holds one date a week, so the ends a reader picks are almost
  // never dates it holds. Which dates it actually answered with is not a
  // footnote here: every number below is a measurement at those two dates.
  const snap = (d.from_snapped || d.to_snapped)
    ? ` · <span class="mkt-note">the weekly archive stores one date a week, so these `
      + `are the two stored dates nearest what was asked for</span>` : "";
  el.innerHTML = `<span>Cost basis <b>${cbWindowLabel(d)}</b> `
    + `(${d.prev_as_of} → ${d.as_of}, ${d.span_days}d) — `
    + `<span class="legend-cb">hollow = supply then, filled = supply now, cap = the change</span> · `
    + `<b>${net >= 0 ? "+" : ""}${fmtNum(net)} BTC</b> ${dir}${px}${cover}${snap} · `
    + `<span class="mkt-note">on-chain supply, data to ${d.data_vintage} — a dated `
    + `measurement, not the live tape</span></span>`;
}
function costBasisTooltipRow(paneY) {
  const d = costBasisState.data;
  if (!d || !d.available || !d.delta || !d.delta.length) return "";
  if (!costBasisState.visible) return "";   // hidden layer, hidden readout (see profileTooltipRow)
  // read the grouping the last draw actually used: zooming regrids the bars, and
  // a tooltip naming a $200 band while $2,000-wide bars are on screen would lie.
  // Between a repaint and a hover the price range cannot have changed, so the
  // cached grouping is exactly what is under the cursor.
  const drawn = costBasisState.drawn;
  const w = (drawn && drawn.w) || d.bucket_usd;
  const rows = (drawn && drawn.rows) || d.delta;
  if (!(w > 0)) return "";
  const price = candleSeries.coordinateToPrice(paneY);
  if (price === null || price === undefined || !isFinite(price)) return "";
  const lbl = `<tr><td class="k src-label">Cost basis <span class="src-meta">`
    + `${cbWindowLabel(d)} · on-chain URPD · to ${d.data_vintage}</span></td>`;
  const x = rows.find(v => Math.floor(v.p / w) === Math.floor(price / w));
  if (!x) return lbl + `<td class="v na">no supply bucket at this price</td></tr>`;
  const acc = x.d > 0;
  // the outline bar is `past` and the filled bar is `now`, so naming both here
  // is what ties the numbers back to the two shapes on screen
  const pct = x.past > 0 ? ` (${x.d > 0 ? "+" : ""}${fmtPct(x.d / x.past, true)})` : "";
  return lbl
    + `<td class="v"><span class="${acc ? "cb-acc" : "cb-dist"}">`
    + `${acc ? "+" : ""}${fmtNum(x.d)} BTC ${acc ? "accumulated" : "distributed"}</span>${pct}`
    + `<br><span class="src-meta">$${fmtNum(x.p, 0)}–$${fmtNum(x.p + w, 0)} · `
    + `hollow ${fmtNum(x.past)} → filled ${fmtNum(x.now)} BTC · ${d.prev_as_of}→${d.as_of}</span></td></tr>`;
}
/* $1,000 zones, not the source's raw buckets: one cohort of owners is
 * spread across ~10 adjacent buckets, so ranking buckets shatters a single wall
 * into fragments that each look small. Zoning groups them back into one price
 * level a reader can actually point at on the axis.
 *
 * Every consumer reads through this one function so the price line on the chart
 * and the row in the table can never disagree about which level is the biggest
 * seller.
 *
 * The width is the payload's own bucket_usd FLOORED at $1,000 (see
 * CB_ZONE_MIN_USD): a custom pair is served on the archive's finer $200 grid, and
 * ranking those raw buckets would shatter one wall into ten rows that each look
 * small -- the exact failure zoning exists to prevent. The chart bars still get
 * the finer grid and their own zoom-adaptive regrid; only this ranking is coarse. */
function cbZoneWidth() {
  const d = costBasisState.data;
  const w = (d && d.bucket_usd) || 0;
  return Math.max(CB_ZONE_MIN_USD, w);
}
function cbZones() {
  const d = costBasisState.data;
  if (!d || !d.available || !d.delta || !d.delta.length) return null;
  const W = cbZoneWidth();
  const zones = new Map();
  for (const r of d.delta) {
    const z = Math.floor(r.p / W) * W;
    let g = zones.get(z);
    if (!g) { g = { p: z, d: 0, past: 0, now: 0 }; zones.set(z, g); }
    g.d += r.d || 0; g.past += r.past || 0; g.now += r.now || 0;
  }
  return Array.from(zones.values());
}

/* Vintage price + the heaviest distribution level, as native price lines.
 * The price MUST come from the payload, never from the live last close: an
 * on-chain curve describes one dated moment, and anchoring it to today's price
 * would present a measurement taken days or weeks ago as a current one. */
let cbPriceLines = [];
function applyCostBasisPriceLines() {
  for (const l of cbPriceLines) {
    try { candleSeries.removePriceLine(l); } catch (e) { /* already gone */ }
  }
  cbPriceLines = [];
  const d = costBasisState.data;
  // Hidden layer means no lines either: these annotate the distribution, and a
  // dashed "biggest seller" level floating over a chart with no distribution on
  // it would be a claim with nothing behind it. The clear above still runs, so
  // toggling the eye off removes them.
  if (!d || !d.available || !costBasisState.visible) return;
  const add = (price, colour, title) => {
    if (price === null || price === undefined || !isFinite(price)) return;
    cbPriceLines.push(candleSeries.createPriceLine({
      price: price, color: colour, lineWidth: 1, lineStyle: 2,   // dashed
      axisLabelVisible: true, title: title,
    }));
  };
  add(d.price_usd, "#b07dff", `cost-basis vintage ${d.as_of}`);
  // The heaviest distribution level — the cohort doing the most selling, named
  // on the axis so the "sell wall" is a price you can read straight off the
  // chart. Same zones the table ranks, so the two agree on the number.
  const zones = cbZones();
  let worst = null;
  for (const z of (zones || [])) if (z.d < 0 && (!worst || z.d < worst.d)) worst = z;
  if (worst) add(worst.p, "#ef5350", `biggest seller ${fmtPx(worst.p)}`);
}

/* Prices are quoted in full, not through fmtNum's K/M shorthand: the columns
 * that use this exist to name an exact level, and "$84K" is both harder to scan
 * against the chart's axis and lossy if the zone width ever drops below $1,000. */
const fmtPx = v => `$${Math.round(v).toLocaleString("en-US")}`;

/* Ranked "who moved" readout.
 *
 * The bars show the whole distribution at once, so at any usable zoom the
 * smaller cohorts are a few pixels each and the biggest ones all look alike.
 * This names the ones that actually moved, in BTC, at the price they bought,
 * and on which side of the vintage price they sit — which is the difference
 * between capitulation (spent above cost basis) and profit-taking.
 *
 * Rows are $1,000 zones (see cbZones) so the rankings are price levels, not
 * fragments of one wall. */
function renderCostBasisMovers() {
  const box = document.getElementById("cb-movers");
  if (!box) return;
  const d = costBasisState.data;
  if (!d || !d.available || !d.delta || !d.delta.length) {
    box.innerHTML = `<div class="holder-card"><h3>Who Moved</h3>
      <div class="holder-row"><span>Cost-basis change</span>
      <span class="na">${d && d.note ? d.note : "not available"}</span></div></div>`;
    return;
  }
  const all = cbZones();   // non-null: the guard above is cbZones' own condition
  const px = d.price_usd;
  const sellers = all.filter(z => z.d < 0).sort((a, b) => a.d - b.d).slice(0, 6);
  const buyers = all.filter(z => z.d > 0).sort((a, b) => b.d - a.d).slice(0, 6);
  const grossAcc = all.reduce((s, z) => s + (z.d > 0 ? z.d : 0), 0);
  const grossDist = all.reduce((s, z) => s + (z.d < 0 ? z.d : 0), 0);
  // Computed, never assumed: in the 90d window the top accumulation zones all
  // sit ABOVE the vintage price, so a hardcoded "buyers bought low" would lie.
  const side = z => (px === null || px === undefined) ? "" :
    `<span class="side">${z.p >= px ? "above" : "below"}</span>`;
  const row = z => {
    const pct = z.past > 0 ? `${z.d > 0 ? "+" : ""}${fmtPct(z.d / z.past, true)}` : "—";
    return `<div class="mover-row"><span class="px">${fmtPx(z.p)}</span>
      <span class="amt ${z.d > 0 ? "cb-acc" : "cb-dist"}">${z.d > 0 ? "+" : ""}${fmtNum(z.d)}</span>
      <span class="pct">${pct}</span>${side(z)}</div>`;
  };
  box.innerHTML = `<div class="holder-card"><h3>Who Moved — ${cbWindowLabel(d)} change</h3>
    <div class="mover-head">${d.prev_as_of} → ${d.as_of}${
      px === null || px === undefined ? "" : ` · price ${fmtPx(px)}`}</div>
    <div class="mover-group">SOLD OFF — cohorts that shrank</div>${sellers.map(row).join("")}
    <div class="mover-group">BOUGHT IN — cohorts that grew</div>${buyers.map(row).join("")}
    <div class="holder-note">
      Each row is a ${fmtPx(cbZoneWidth())} price zone: BTC of supply that
      changed hands, and the share of what sat there before. <b>above</b> = that
      cohort's cost basis is higher than the price at the end of the window, so
      spending there realises a <span class="cb-dist">loss</span>;
      <b>below</b> = realises a <span class="cb-acc">profit</span>.
      ${fmtNum(grossAcc)} BTC accumulated / ${fmtNum(grossDist)} BTC distributed,
      net ${d.net_delta_btc >= 0 ? "+" : ""}${fmtNum(d.net_delta_btc)} BTC.
      Hollow outline on the chart is this cohort ${d.prev_as_of}, filled is ${d.as_of}.
    </div>
    <div class="vintage">checkonchain, data to ${d.as_of}</div></div>`;
}

/* The preset buttons and the custom date pair are ONE control: choosing either
 * deactivates the other, because they answer the same question ("how far back?")
 * and two things lit at once would say the chart is showing both.
 *
 * The buttons are held in a list rather than re-queried from the DOM: this is
 * the only writer of their pressed state, and a lookup that can come back empty
 * is a lit button that never goes out. */
let cbButtons = [];
/* The row's own explanation, captured in buildCostBasisButtons so the idle-state
 * title can extend it rather than replace it. */
let cbPairTitle = "";
function syncCostBasisControls() {
  for (const b of cbButtons) b.classList.toggle("active", b.dataset.cw === S.cbWindow);
  const pair = document.getElementById("cb-custom");
  const custom = S.cbWindow === CB_CUSTOM;
  if (pair) {
    pair.classList.toggle("active", custom);
    /* An idle pair must not read as the window being drawn. The boxes hold the
     * reader's dates while a preset is what the chart shows, and a row that looks
     * the same either way leaves them comparing two different periods believing
     * they picked one. */
    pair.classList.toggle("idle", !custom);
    pair.title = custom ? cbPairTitle
      : `${cbPairTitle} — not applied: the chart is showing ${S.cbWindow}. `
        + "Press Apply to switch to these dates.";
  }
  /* Written only while the boxes ARE the active control. A write on every sync
   * would overwrite dates the reader had typed but not yet applied: clicking a
   * preset to compare with them would silently restore the last APPLIED pair,
   * and the next Apply would compute a window they never asked for. The stored
   * pair is placed in the boxes once, at boot, by buildCostBasisButtons. */
  if (!custom) return;
  const f = document.getElementById("cb-from"), t = document.getElementById("cb-to");
  if (f && S.cbCustom) f.value = S.cbCustom.from;
  if (t && S.cbCustom) t.value = S.cbCustom.to;
}
function cbStatusNote(msg) {
  const el = document.getElementById("status-cb");
  if (el) el.innerHTML = `<span class="mkt-note">${msg}</span>`;
}
/* Apply validates locally and says what is wrong in the strip rather than
 * sending a request that can only come back as the same complaint. */
function applyCustomCostBasis() {
  const f = document.getElementById("cb-from"), t = document.getElementById("cb-to");
  const from = f ? f.value : "", to = t ? t.value : "";
  if (!CB_ISO_RE.test(from) || !CB_ISO_RE.test(to)) {
    return cbStatusNote("Custom window needs both a start and an end date — pick both, then Apply.");
  }
  if (from >= to) {
    return cbStatusNote(`Custom window: the start date (${from}) must be earlier than the end date (${to}).`);
  }
  S.cbCustom = { from: from, to: to };
  localStorage.setItem("vsa_cb_custom_v1", JSON.stringify(S.cbCustom));
  S.cbWindow = CB_CUSTOM;
  localStorage.setItem("vsa_cb_window_v2", CB_CUSTOM);
  syncCostBasisControls();
  loadCostBasis();
}
function buildCostBasisButtons() {
  const wrap = document.getElementById("cb-window");
  if (!wrap) return;
  wrap.innerHTML = "";
  cbButtons = [];
  for (const w of CB_WINDOWS) {
    const b = document.createElement("button");
    b.textContent = w;
    b.dataset.cw = w;
    if (w === S.cbWindow) b.classList.add("active");
    b.addEventListener("click", () => {
      S.cbWindow = w;
      localStorage.setItem("vsa_cb_window_v2", w);
      syncCostBasisControls();
      loadCostBasis();
    });
    wrap.appendChild(b);
    cbButtons.push(b);
  }
  const apply = document.getElementById("cb-apply");
  if (apply) apply.addEventListener("click", applyCustomCostBasis);
  for (const id of ["cb-from", "cb-to"]) {
    const el = document.getElementById(id);
    // Enter in a date field is the obvious way to commit it; without this the
    // form-less input just does nothing and the reader thinks it is broken.
    if (el) el.addEventListener("keydown", ev => {
      if (ev.key === "Enter") { ev.preventDefault(); applyCustomCostBasis(); }
    });
  }
  // Bounds on the pickers, where the archive's span is known before any request:
  // in static mode the manifest carries it. Live mode leaves the pickers open --
  // the archive is the server's, and an out-of-range date comes back naming the
  // span rather than being silently unclickable.
  const meta = (STATIC && STATIC.cost_basis_archive || {})[S.symbol];
  if (meta) {
    const f = document.getElementById("cb-from"), t = document.getElementById("cb-to");
    if (f) { f.min = meta.first; f.max = meta.last; }
    if (t) { t.min = meta.first; t.max = meta.last; }
  }
  const pairEl = document.getElementById("cb-custom");
  if (pairEl && !cbPairTitle) cbPairTitle = pairEl.title || "";
  /* A returning reader finds their last pair in the boxes -- placed here, once,
   * so that nothing writes into them again until they are the active window
   * (see syncCostBasisControls). */
  if (S.cbCustom) {
    const f = document.getElementById("cb-from"), t = document.getElementById("cb-to");
    if (f) f.value = S.cbCustom.from;
    if (t) t.value = S.cbCustom.to;
  }
  syncCostBasisControls();
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

/* ---------------- spot-ETF daily net flows ----------------
 * The demand side of a move: net creations/redemptions across the US spot BTC
 * ETFs, in USD, one point per calendar day since the first session (2024-01-11).
 * Drawn as bars in the candle pane on their own overlay price scale — the same
 * mechanism the volume histogram uses — so a $937M inflow day sits next to the
 * candle it helped make, with no second pane and no second axis to read.
 *
 * Two things this layer must never imply:
 *   * a 0.0 is a day the US market did not open, NOT a day with no flow
 *     (upstream stamps weekends and holidays explicitly), and
 *   * the newest day is provisional — upstream publishes it through the session,
 *     so on our collection day it is usually still 0.0.
 * BTC-only by construction: these are US ETFs holding BTC. ETH gets no layer.
 *
 * Points are clipped to the loaded range. The series is stamped daily (today)
 * while the Vision candles lag ~1 day, and an unclipped point past the last
 * candle would drag scrollToRealTime() into empty space at the right edge.
 */
const etfState = { data: null, byDay: new Map(), visible: layerVisible("etf"),
                   series: null };
async function loadEtf() {
  const wrap = document.getElementById("etf-wrap");
  try {
    const d = await api(`/api/etf_flows?symbol=${S.symbol}`);
    etfState.data = d && d.available ? d : null;
  } catch (e) { etfState.data = null; }   // offline/blocked -> no layer, not a broken page
  etfState.byDay = new Map((etfState.data ? etfState.data.points : []).map(p => [p[0], p[1]]));
  // A switch that can only do nothing is worse than no switch: hide the control
  // for a symbol the source cannot answer for (ETH), and say nothing else.
  if (wrap) wrap.style.display = etfState.data ? "" : "none";
  renderEtf();
}
function renderEtf() {
  const d = etfState.data;
  if (!etfState.series) {
    if (!d || !S.bars.length) return;
    etfState.series = chart.addHistogramSeries({
      priceScaleId: "etf", base: 0, priceLineVisible: false, lastValueVisible: false,
    });
    // Signed bars grow up from $0 (inflows) and down from it (outflows) inside a
    // band ABOVE the volume histogram, which owns the bottom 18% of the pane.
    chart.priceScale("etf").applyOptions({
      scaleMargins: { top: 0.66, bottom: 0.18 },
      visible: false,   // overlay scale: no axis, the number is read on hover
    });
  }
  const pts = [];
  for (const [day, usd] of (d ? d.points : [])) {
    const t = Date.parse(day + "T00:00:00Z") / 1000;
    if (!S.bars.length || t * 1000 < S.range.start || t * 1000 > S.range.end) continue;
    pts.push({ time: t, value: usd, color: usd >= 0 ? UP : DOWN });
  }
  etfState.series.setData(pts);
  etfState.series.applyOptions({ visible: etfState.visible });
  updateEtfStatus();
}
/* Kept in S and painted from updateEtfStatus(), like the other status spans: the
 * span only exists inside the markup updateStatus() writes, and this can be
 * called from a load path that ran before it. */
function updateEtfStatus() {
  const el = document.getElementById("status-etf");
  const d = etfState.data;
  S.statusEtf = "";
  if (el && d) {
    const ls = d.last_session;
    S.statusEtf = `<span>ETF flows <b>${etfFlowText(ls && ls[1])}</b>`
      + `${ls ? ` · ${ls[0]} (last session)` : ""}`
      + `${etfState.visible ? "" : " <span class=\"mkt-note\">— hidden</span>"}</span>`;
  }
  if (el) el.innerHTML = S.statusEtf;
}
/* One money formatter for the tooltip and the status strip, so the same number
 * never reads two ways. */
function etfFlowText(usd) {
  return (usd === null || usd === undefined || isNaN(usd)) ? "—"
    : (usd > 0 ? "+" : usd < 0 ? "−" : "") + fmtUsd(Math.abs(usd));
}
function etfTooltipRow(openTime) {
  const d = etfState.data;
  if (!d || !etfState.visible) return "";   // a hidden layer makes no claims
  const day = new Date(openTime).toISOString().slice(0, 10);
  const v = etfState.byDay.get(day);
  const key = `<td class="k src-label">ETF net flow (USD)<br><span class="src-meta">daily · US spot BTC ETFs · this day</span></td>`;
  if (v === undefined) return `<tr>${key}<td class="v na">n/a — series starts ${d.first}</td></tr>`;
  // 0.0 is "no session", not "no flow" -- the one reading that would turn a
  // weekend into a signal.
  if (!v) return `<tr>${key}<td class="v na">no US session (weekend/holiday)</td></tr>`;
  return `<tr>${key}<td class="v" style="color:${v > 0 ? UP : DOWN}">${etfFlowText(v)}</td></tr>`;
}

/* ---------------- holder panel ----------------
 * LTH/STH cohorts, supply in profit vs loss, SOPR and STH valuation. This is
 * genuine per-UTXO age data — but computed by checkonchain's node, not ours
 * (this project has never run one; that was the unapproved Phase 8 gate), and
 * it stops at the upstream repo's freeze date. Every card is stamped with that
 * date so a stale reading is never mistaken for a live one. */
function nf(v, d) {
  return (v === null || v === undefined || isNaN(v)) ? "—" : Number(v).toFixed(d);
}
function share(part, whole) {
  return (part === null || part === undefined || !whole) ? null : part / whole;
}
async function loadHolder() {
  const box = document.getElementById("holder-content");
  try {
    const h = await api(`/api/onchain_snapshot?symbol=${S.symbol}`);
    const H = h.holder || {};
    const vintage = H.data_vintage || "—";
    const stamp = `<div class="vintage">checkonchain, data to ${vintage}</div>`;
    let html = `<div class="holder-grid">`;

    if (!H.available) {
      html += `<div class="holder-card"><h3>Long-Term / Short-Term Holder Cohorts</h3>
        <div class="holder-row"><span>Age-cohort data</span><span class="na">not available</span></div>
        <div class="holder-note">${H.note || "not available"}</div></div>`;
    } else {
      const lp = H.lth_supply_pct, sp = H.sth_supply_pct;
      const haveSplit = lp !== null && lp !== undefined && sp !== null && sp !== undefined;
      html += `<div class="holder-card"><h3>Long-Term / Short-Term Holder Cohorts</h3>`;
      if (haveSplit) {
        html += `<div class="split">
          <div class="seg seg-lth" style="width:${(lp * 100).toFixed(2)}%"></div>
          <div class="seg seg-sth" style="width:${(sp * 100).toFixed(2)}%"></div></div>`;
      }
      html += `<div class="holder-row"><span><span class="legend-urpd">LTH</span> — long-term holders</span>
        <span><b>${fmtNum(H.lth_supply_btc)} BTC</b> · ${fmtPct(lp)}</span></div>
      <div class="holder-row"><span><span class="legend-tape">STH</span> — short-term holders</span>
        <span><b>${fmtNum(H.sth_supply_btc)} BTC</b> · ${fmtPct(sp)}</span></div>
      <div class="holder-note">Which coins have not moved for a long time, and which are
        recently acquired. Split by UTXO age, computed by checkonchain from their own
        node — this project has never run one.</div>${stamp}</div>`;

      const lthP = share(H.lth_profit_btc, H.lth_supply_btc);
      const sthP = share(H.sth_profit_btc, H.sth_supply_btc);
      html += `<div class="holder-card"><h3>Supply in Profit vs Loss, by Cohort</h3>
        <div class="holder-row"><span>LTH in profit</span><span><span class="cb-acc">${fmtNum(H.lth_profit_btc)} BTC</span>${lthP !== null ? ` · ${fmtPct(lthP)} of LTH` : ""}</span></div>
        <div class="holder-row"><span>LTH in loss</span><span><span class="cb-dist">${fmtNum(H.lth_loss_btc)} BTC</span></span></div>
        <div class="holder-row"><span>STH in profit</span><span><span class="cb-acc">${fmtNum(H.sth_profit_btc)} BTC</span>${sthP !== null ? ` · ${fmtPct(sthP)} of STH` : ""}</span></div>
        <div class="holder-row"><span>STH in loss</span><span><span class="cb-dist">${fmtNum(H.sth_loss_btc)} BTC</span>${H.sth_underwater_pct !== null && H.sth_underwater_pct !== undefined ? ` · ${fmtPct(H.sth_underwater_pct)} of STH` : ""}</span></div>
        <div class="holder-note">"In profit" means price is above the price the coin last
          moved at — i.e. its owner's cost basis. A cohort mostly in loss is the one whose
          supply is the sell pressure to watch, because selling there realises a loss.</div>${stamp}</div>`;

      const sopr = H.sopr, soprE = H.sopr_ema_7d;
      html += `<div class="holder-card"><h3>Spent Output Profit Ratio + STH Valuation</h3>
        <div class="holder-row"><span>SOPR</span><span><b>${nf(sopr, 4)}</b>${sopr === null || sopr === undefined ? "" : (sopr >= 1 ? " · coins spent in profit" : " · coins spent at a loss")}</span></div>
        <div class="holder-row"><span>SOPR, 7-day EMA</span><span>${nf(soprE, 4)}${soprE === null || soprE === undefined ? "" : (soprE >= 1 ? "" : ` <span class="cb-dist">below 1 — sellers realising losses</span>`)}</span></div>
        <div class="holder-row"><span>STH-MVRV</span><span>${nf(H.sth_mvrv, 3)}</span></div>
        <div class="holder-row"><span>STH realized price</span><span>${fmtUsd(H.sth_realized_price_usd)}</span></div>
        <div class="holder-row"><span>Price</span><span><b>${fmtUsd(H.price_usd)}</b></span></div>
        <div class="holder-note">SOPR &gt; 1: coins spent that day moved at a profit, &lt; 1
          at a loss. STH-MVRV &lt; 1 means the average recent buyer is underwater — price
          below the STH realized price, which is the average cost basis of the STH cohort
          (coins younger than the short-term age threshold) weighted by when each moved.</div>${stamp}</div>`;
    }
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
      html += `<div class="holder-card"><h3>Heaviest Cost-Basis Levels (${h.urpd.data_vintage || "—"}, ${h.urpd.n_buckets} buckets)</h3>
        <div class="urpd-bars">${rows}</div>
        <div class="holder-note">${h.urpd.note} · total supply ${fmtNum(h.urpd.total_supply_btc)} BTC</div>
        <div class="holder-note">Where supply SITS — the ten price levels holding the most
          coins, on one date. For where supply MOVED, see the
          <b>Who Moved</b> card above, which compares two dates and names the cohorts
          being spent.</div></div>`;
    } else {
      html += `<div class="holder-card"><h3>Cost-Basis Buckets</h3>
        <div class="holder-row"><span>Available</span><span class="na">${h.urpd ? h.urpd.note : "not available"}</span></div></div>`;
    }
    html += `</div><div class="holder-note">${h.data_availability}
      <span class="vintage-warn">Panel data is a historical vintage, not a live reading.</span></div>`;
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
    // The interval on entry is the API's, not a literal in this file: daily bars
    // are what the chart is read for, and the API is where the audited interval
    // list lives. Adopted only if it is actually in that list, so a mismatch
    // leaves the chart on a renderable interval instead of none.
    if (sym.default_interval &&
        (sym.audited || []).some(s => (s.intervals || []).includes(sym.default_interval))) {
      S.defaultInterval = sym.default_interval;
      S.interval = sym.default_interval;
    }
    buildIntervalButtons();
    document.getElementById("intervals").querySelector(`[data-iv="${S.interval}"]`)?.classList.add("active");
    buildProfileButtons();
    buildCostBasisButtons();
    buildLayerEyes();
    buildMarkerToggles();
    populateSymbolSelect();
    await loadInterval();
    await loadHolder();
    await loadEtf();
  } catch (e) {
    document.getElementById("statusbar").textContent = "Failed to start: " + e.message;
  }
})();
