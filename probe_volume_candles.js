/* probe_volume_candles.js — volume candles + the browser-built 1W interval, run for real.
 *
 * app.js is evaluated in a vm over a stub DOM (the harness probe_hover_toggle.js
 * and probe_dates.js use), so the week maths, the width mapping and the style
 * switch under test are the SHIPPED functions, not a transcription. The stubs
 * that must remember are the chart pieces: the candlestick series records its
 * applyOptions() calls and attached primitives, the time scale answers the
 * coordinates drawVolumeCandles asks for, and the 2D context records what the
 * primitive paints.
 *
 * Run:  node BTC_True_Volume/probe_volume_candles.js      (from the project root)
 */
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const HERE = __dirname;
const src = fs.readFileSync(path.join(HERE, "visualizer", "static", "app.js"), "utf8");

/* The universal stub, verbatim from probe_hover_toggle.js: any property is
 * another stub, any call returns a stub, primitive coercion is 0/"" so
 * arithmetic in the app does not throw, and `then` is absent so nothing awaits
 * a stub forever. */
function mk(name, target) {
  const t = target || function () {};
  const p = new Proxy(t, {
    get(_, k) {
      if (k === "then") return undefined;
      if (k === Symbol.toPrimitive) return () => 0;
      if (k === Symbol.iterator) return [][Symbol.iterator].bind([]);
      if (k === Symbol.toStringTag) return "Stub";
      if (k === "length") return 0;
      if (k === "id" || k === "name") return name;
      if (k === "innerHTML" || k === "textContent" || k === "value") return k in t ? t[k] : "";
      if (k === "classList") return { add() {}, remove() {}, toggle() {}, contains: () => false };
      if (k === "style") { if (!(k in t)) t[k] = mk(name + ".style"); return t[k]; }
      if (k === "dataset") return mk("dataset");
      if (k === "offsets") return undefined;
      if (!(k in t)) t[k] = stub(name + "." + String(k));
      return t[k];
    },
    set(_, k, v) { t[k] = v; return true; },
    apply() { return mk(name + "()"); },
    construct() { return stub("new " + name); },
  });
  return p;
}
function stub(name) { const f = function () { return mk(name + "()"); }; return mk(name, f); }

/* One page load. `stored` seeds localStorage before app.js is evaluated, which
 * is the only way to see what a returning reader actually opens on. */
function boot(stored = {}) {
  const store = new Map(Object.entries(stored));
  const els = new Map();

  // ---- the chart pieces the probe must be able to read back ----------------
  const styleOptions = [];                       // applyOptions() on the candle series
  const primitives = [];                         // attachPrimitive() on the candle series
  const ts = stub("timescale");
  ts.options = () => ({ barSpacing: 10 });
  ts.getVisibleLogicalRange = () => ({ from: 0, to: 9999 });
  ts.logicalToCoordinate = i => 50 + i * 10;
  ts.applyOptions = () => {};
  ts.scrollToRealTime = () => {};
  ts.subscribeVisibleTimeRangeChange = () => {};
  ts.width = () => 800;

  const candle = stub("candleSeries");
  candle.applyOptions = o => { styleOptions.push(o); };
  candle.attachPrimitive = p => { primitives.push(p); };
  candle.detachPrimitive = () => {};
  candle.priceToCoordinate = p => 400 - p;       // price in dollars onto a 400px pane
  candle.coordinateToPrice = y => 400 - y;
  candle.setData = () => {};
  candle.setMarkers = () => {};

  const chart = stub("chart");
  chart.addCandlestickSeries = () => candle;
  chart.addHistogramSeries = () => stub("histogramSeries");
  chart.priceScale = () => stub("priceScale");
  chart.timeScale = () => ts;
  const lightcharts = stub("LightweightCharts");
  lightcharts.createChart = () => chart;

  // ---- DOM --------------------------------------------------------------
  // #candle-style is a real container: buildCandleStyleButtons() fills it and
  // the probe fires its buttons' click handlers.
  const styleWrap = {
    innerHTML: "", children: [],
    appendChild(c) { this.children.push(c); },
    querySelectorAll() { return this.children; },
  };
  els.set("candle-style", styleWrap);
  function el(id) {
    if (!els.has(id)) els.set(id, mk("el#" + id));
    return els.get(id);
  }
  const created = [];
  const documentStub = mk("document");
  documentStub.getElementById = id => el(id);
  documentStub.querySelector = () => stub("query");
  documentStub.createElement = tag => {
    const b = {
      tag, textContent: "", title: "", type: "", dataset: {},
      classList: { add() {}, remove() {}, toggle() {} },
      addEventListener(type, fn) { if (type === "click") this.click = fn; },
    };
    created.push(b);
    return b;
  };
  documentStub.createElementNS = () => stub("createElementNS");
  documentStub.body = stub("body");
  documentStub.documentElement = stub("documentElement");
  documentStub.fonts = stub("fonts");

  const ctx = {
    console, setTimeout, clearTimeout, setInterval, clearInterval,
    Math, JSON, Date, Map, Set, Array, Object, Number, String, Boolean, Error,
    Promise, isFinite, isNaN, parseFloat, parseInt, encodeURIComponent,
    decodeURIComponent, URL, URLSearchParams, Intl, TextEncoder, TextDecoder,
    document: documentStub,
    window: stub("window"),
    navigator: { userAgent: "node" },
    location: { href: "https://example.test/", search: "", hash: "" },
    localStorage: {
      getItem: k => (store.has(k) ? store.get(k) : null),
      setItem: (k, v) => store.set(k, String(v)),
      removeItem: k => store.delete(k),
    },
    fetch: async () => { throw new Error("live fetch is not available in the probe"); },
    requestAnimationFrame: cb => setTimeout(cb, 0),
    cancelAnimationFrame: () => {},
    getComputedStyle: () => mk("style"),
    matchMedia: () => ({ matches: false, addEventListener() {}, addListener() {} }),
    ResizeObserver: function () { return { observe() {}, disconnect() {} }; },
    IntersectionObserver: function () { return { observe() {}, disconnect() {} }; },
    LightweightCharts: lightcharts,
    devicePixelRatio: 1,
    __probe: true,
  };
  ctx.globalThis = ctx;
  vm.createContext(ctx);

  /* The hook is appended to app.js's OWN source: S, STATIC and the helpers are
   * top-level bindings inside the same script, and a later vm.runInContext
   * would not see them (see probe_dated_pair.js). */
  const hook = `
globalThis.__hook = {
  S,
  weekStartMs, weeklyBars, weeklyBreakdown, volumeWidthValue, drawVolumeCandles,
  buildCandleStyleButtons,
  mode() { return candleStyleState.mode; },
  setMode(m) { candleStyleState.mode = m; },
  setStatic(v) { STATIC = v; },
  setLoader(fn) { loadJSONFile = fn; },
  api,
};
`;
  vm.runInContext(src + hook, ctx, { filename: "app.js" });
  return { ctx, hook: ctx.__hook, styleOptions, primitives, styleWrap, created, ts };
}

/* A fake CanvasRenderingTarget2D. Media coordinates are CSS pixels, which is
 * what the app's other primitives draw in too. */
function recorder() {
  const rec = { rects: [], strokes: 0, segs: 0, fillStyle: null, strokeStyle: null };
  const ctx = {
    fillStyle: null, strokeStyle: null, lineWidth: 1,
    _cur: [],
    beginPath() { this._cur = []; },
    moveTo(x, y) { this._cur.push([x, y]); },
    lineTo(x, y) { this._cur.push([x, y]); },
    stroke() { rec.strokes++; rec.segs += this._cur.length; },
    fillRect(x, y, w, h) { rec.rects.push({ x, y, w, h, fill: this.fillStyle }); },
    drawImage() {},
  };
  const target = {
    useMediaCoordinateSpace(cb) {
      cb({ context: ctx, mediaSize: { width: 800, height: 400 } });
    },
  };
  return { rec, target };
}

let bad = 0;
function eq(label, got, want) {
  if (JSON.stringify(got) !== JSON.stringify(want)) {
    bad++;
    console.log(`[FAIL] ${label}\n       got  ${JSON.stringify(got)}\n       want ${JSON.stringify(want)}`);
  } else {
    console.log(`[ ok ] ${label} = ${JSON.stringify(got)}`);
  }
}
function ok(label, cond, detail) {
  if (!cond) { bad++; console.log(`[FAIL] ${label}${detail ? "\n       " + detail : ""}`); }
  else console.log(`[ ok ] ${label}`);
}

const D = (y, m, d) => Date.UTC(y, m - 1, d);
const DAY = 86400000;

/* 2026-10-05 is a Monday: 2026-01-05 + 39 weeks. The fixture spans three
 * Mondays (05/10, 12/10, 19/10) so week 1 and 2 are complete and week 3 is the
 * in-progress one-day week. */
const MON1 = D(2026, 10, 5), MON2 = D(2026, 10, 12), MON3 = D(2026, 10, 19);

function dailyBars() {
  const out = [];
  for (let i = 0; i < 15; i++) {
    out.push({
      open_time: MON1 + i * DAY,
      open: 100 + i, high: 110 + i, low: 90 + i, close: 105 + i,
      volume: (i + 1) * 10, taker_buy: 5,
      market: "futures_um",
      resampled: i === 3,
      cross_filled: i === 7,
      follows_gap: i === 8,
      live: i === 14,
    });
  }
  return out;
}
function dailyRows() {
  const out = [];
  for (let i = 0; i < 15; i++) {
    out.push({
      open_time: MON1 + i * DAY,
      sources: {
        exchange_volume: (i + 1) * 10,
        tape_rollup_volume: 1,
        onchain_network_volume: 2,
        onchain_network_volume_source: i < 2 ? "blockchain_com" : "checkonchain",
        // i=14 is the lone day of the partial week and has no value, so the
        // null-aware sum is exercised on a whole week, not just a lost day.
        large_trade_notional: (i % 2 === 0 && i !== 14) ? 3 : null,
        open_interest_delta: i % 2 ? -1 : 1,
        taker_buy_sell_split: { buy: 5, sell: 6 },
        top_trader_positioning: { long_pct: i },
      },
      pending: i === 14,
    });
  }
  return out;
}

(async () => {
  await new Promise(r => setTimeout(r, 30));

  /* ================= week arithmetic ================= */
  {
    const h = boot({}).hook;
    eq("Monday is its own week start", h.weekStartMs(MON1 + 12 * 3600000), MON1);
    eq("a Sunday belongs to the Monday six days back", h.weekStartMs(MON1 + 6 * DAY), MON1);
    eq("the next Monday starts a new week", h.weekStartMs(MON2), MON2);
    eq("week starts survive a month boundary", h.weekStartMs(D(2026, 11, 1)), D(2026, 10, 26));
  }

  /* ================= weekly OHLCV ================= */
  {
    const h = boot({}).hook;
    const gaps = [MON1 + 2 * DAY, MON1 + 3 * DAY, MON1 + 9 * DAY];
    const out = h.weeklyBars({ bars: dailyBars(), gap_slots: gaps, truncated: false });
    eq("three weeks from fifteen days", out.bars.length, 3);
    eq("interval is stamped 1W", out.interval, "1W");
    const [w1, w2, w3] = out.bars;
    eq("week 1 opens Monday 05/10", w1.open_time, MON1);
    eq("week 1 OHLC", [w1.open, w1.high, w1.low, w1.close], [100, 116, 90, 111]);
    eq("week 1 volume is the seven days summed", w1.volume, 280);
    eq("week 1 taker buy summed", w1.taker_buy, 35);
    eq("a mid-week resampled day marks the week", w1.resampled, true);
    eq("week 1 has no cross-filled day", w1.cross_filled, false);
    eq("week 1 has no gap day", w1.follows_gap, false);
    eq("week 1 is not live", w1.live, false);
    eq("week 2 opens Monday 12/10", w2.open_time, MON2);
    eq("week 2 close is its last day's close", w2.close, 118);
    eq("week 2 carries the cross-filled day", w2.cross_filled, true);
    eq("week 2 carries the gap day", w2.follows_gap, true);
    eq("week 3 is the one-day in-progress week", [w3.open_time, w3.open, w3.close],
       [MON3, 114, 119]);
    eq("the live day makes the current week live", w3.live, true);
    eq("daily gap slots collapse to their weeks", out.gap_slots, [MON1, MON2]);
    eq("coverage describes the weekly bars",
       out.coverage, { start: MON1, end: MON3, n: 3 });
  }

  /* ================= weekly breakdown ================= */
  {
    const h = boot({}).hook;
    const out = h.weeklyBreakdown({ bars: dailyRows(), sources: [{ key: "exchange_volume" }] });
    const [w1, w2, w3] = out.bars.map(r => r.sources);
    eq("three weekly rows", out.bars.length, 3);
    eq("exchange volume summed", w1.exchange_volume, 280);
    eq("tape rollup summed (7 days x 1)", w1.tape_rollup_volume, 7);
    eq("on-chain summed (7 days x 2)", w1.onchain_network_volume, 14);
    eq("large-trade notional skips null days", w1.large_trade_notional, 12);
    eq("OI deltas sum to the week's net change", w1.open_interest_delta, 1);
    eq("taker split summed", w1.taker_buy_sell_split, { buy: 35, sell: 42 });
    eq("top-trader snapshot is the week's last day", w1.top_trader_positioning, { long_pct: 6 });
    eq("on-chain provenance is the week's last day", w1.onchain_network_volume_source,
       "checkonchain");
    eq("a week of null large trades stays null", w3.large_trade_notional, null);
    eq("week 2 OI deltas", w2.open_interest_delta, -1);
    eq("a pending day makes the week pending", out.bars[2].pending, true);
    eq("source metadata passes through", out.sources, [{ key: "exchange_volume" }]);
  }

  /* ================= api() 1W rewrite ================= */
  {
    const b = boot({});
    const h = b.hook;
    /* app.js's own boot() races this block to the manifest: its fetch is
     * stubbed to reject, so its catch sets STATIC = null as a microtask. Let
     * that settle BEFORE injecting the fake static manifest -- otherwise the
     * first api() call slips through and a later one falls into live mode and
     * hits the throwing stub fetch. */
    await new Promise(r => setTimeout(r, 10));
    const paths = [];
    const daily = { symbol: "BTCUSDT", interval: "1d", bars: dailyBars(),
                    gap_slots: [MON1 + 2 * DAY], truncated: false };
    const vol = { symbol: "BTCUSDT", interval: "1d", bars: dailyRows(),
                  sources: [{ key: "exchange_volume" }], notes: [] };
    h.setStatic({ symbols: ["BTCUSDT"],
                  files: { bars: { BTCUSDT_1d: "ohlc_daily.json" },
                           volume: { BTCUSDT_1d: "vol_daily.json" } } });
    h.setLoader(async p => {
      paths.push(p);
      return JSON.parse(JSON.stringify(p === "ohlc_daily.json" ? daily : vol));
    });

    const got = await h.api("/api/ohlc?symbol=BTCUSDT&interval=1W&limit=1000");
    eq("1W is answered from the 1d file", paths[0], "ohlc_daily.json");
    ok("no request asked the loader for a 1W file",
       !paths.some(p => p.includes("1W")), paths.join(", "));
    eq("browser-side 1W returns weekly bars", got.bars.length, 3);
    eq("...with the weekly interval stamped", got.interval, "1W");

    /* The start of a pair is floored to its Monday: day 3 (Thu 08/10) still
     * yields the week that began Mon 05/10, open 100 -- not open 103. */
    paths.length = 0;
    const clipped = await h.api("/api/ohlc?symbol=BTCUSDT&interval=1W&start="
      + (MON1 + 3 * DAY) + "&limit=1000");
    eq("start was floored to its Monday before fetching",
       paths[0], "ohlc_daily.json");
    eq("the floored start keeps the whole first week", clipped.bars[0].open, 100);

    paths.length = 0;
    const volOut = await h.api("/api/volume_breakdown?symbol=BTCUSDT&interval=1W&limit=1000");
    eq("breakdown 1W reads the daily volume file", paths[0], "vol_daily.json");
    eq("breakdown rows are weekly", volOut.bars.length, 3);
    eq("breakdown sums on the way", volOut.bars[0].sources.exchange_volume, 280);

    paths.length = 0;
    const plain = await h.api("/api/ohlc?symbol=BTCUSDT&interval=1d&limit=5");
    eq("a 1d request is untouched", paths[0], "ohlc_daily.json");
    eq("...and still sliced by its own limit", plain.bars.length, 5);
    eq("...at 1d", plain.interval, "1d");
  }

  /* ================= the width number ================= */
  {
    const h = boot({}).hook;
    const S = h.S;
    const bar = { open_time: MON1, volume: 100, open: 1, high: 2, low: 0.5, close: 1.5 };
    S.breakdown = new Map();
    S.source = "onchain_network_volume";
    eq("a source with no value falls back to exchange volume", h.volumeWidthValue(bar), 100);
    S.breakdown.set(MON1, { onchain_network_volume: 7 });
    eq("the selected source drives it when present", h.volumeWidthValue(bar), 7);
    S.breakdown.set(MON1, { onchain_network_volume: null });
    eq("an explicit null also falls back", h.volumeWidthValue(bar), 100);
    S.source = "exchange_volume";
    eq("exchange volume reads the bar itself", h.volumeWidthValue(bar), 100);
    S.source = "open_interest_delta";
    S.breakdown.set(MON1, { open_interest_delta: -5 });
    eq("a negative OI delta uses its magnitude", h.volumeWidthValue(bar), 5);
  }

  /* ================= the drawing ================= */
  {
    const b = boot({});
    const h = b.hook;
    const S = h.S;
    S.bars = [
      { open_time: MON1, open: 100, high: 110, low: 95, close: 105, volume: 100 },
      { open_time: MON1 + DAY, open: 105, high: 120, low: 100, close: 102, volume: 400 },
    ];
    S.breakdown = new Map();
    S.source = "onchain_network_volume";

    h.setMode("standard");
    let r = recorder();
    h.drawVolumeCandles(r.target);
    eq("standard mode paints no volume candles", r.rec.rects.length, 0);

    h.setMode("volume");
    r = recorder();
    h.drawVolumeCandles(r.target);
    eq("volume mode paints one body per visible bar", r.rec.rects.length, 2);
    // Linear against the visible maximum, like TradingView: 100 is a quarter of
    // 400, so its body is a quarter of the 9px maximum (rounded), not ~half.
    eq("the high-volume body is wider", [r.rec.rects[0].w, r.rec.rects[1].w], [2, 9]);
    ok("bodies are centred on the bar", r.rec.rects[0].x === 49 && r.rec.rects[1].x === 56,
       JSON.stringify(r.rec.rects));
    eq("wicks are stroked for both bars", r.rec.strokes, 2);
    eq("up bar is painted UP", r.rec.rects[0].fill, "#26a69a");
    eq("down bar is painted DOWN", r.rec.rects[1].fill, "#ef5350");

    // fully zoomed out: one batched 1px path per colour, no per-bar rects
    b.ts.options = () => ({ barSpacing: 1 });
    r = recorder();
    h.drawVolumeCandles(r.target);
    eq("dense zoom batches the bodies into paths", r.rec.rects.length, 0);
    eq("...one stroke per colour", r.rec.strokes, 2);
    b.ts.options = () => ({ barSpacing: 10 });
  }

  /* ================= the toggle ================= */
  {
    const b = boot({});
    const h = b.hook;
    h.setMode("standard");
    h.buildCandleStyleButtons();
    eq("two buttons are built", b.styleWrap.children.map(x => x.dataset.cs),
       ["standard", "volume"]);
    const opts = () => b.styleOptions[b.styleOptions.length - 1];
    eq("standard paints the normal candle colours", opts().upColor, "#26a69a");
    b.styleWrap.children[1].click();                 // "volume"
    eq("volume turns the built-in candles transparent", opts().upColor, "rgba(0,0,0,0)");
    eq("...both directions and wicks too",
       [opts().downColor, opts().wickUpColor, opts().wickDownColor],
       ["rgba(0,0,0,0)", "rgba(0,0,0,0)", "rgba(0,0,0,0)"]);
    eq("the choice is remembered", b.ctx.localStorage.getItem("vsa_candle_style"), "volume");
    eq("the mode follows", h.mode(), "volume");
    b.styleWrap.children[0].click();                 // "standard"
    eq("standard restores the candle colours", opts().upColor, "#26a69a");
    eq("...and is remembered", b.ctx.localStorage.getItem("vsa_candle_style"), "standard");

    const again = boot({ vsa_candle_style: "volume" });
    eq("a returning reader opens on the remembered mode", again.hook.mode(), "volume");
  }

  console.log(bad ? `\n${bad} check(s) FAILED` : "\nall volume-candle checks pass");
  process.exit(bad ? 1 : 0);
})();
