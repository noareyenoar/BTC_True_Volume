/* probe_orderbook.js — the depth-heat grids, run against the shipped app.js.
 *
 * The heat payloads are sparse source data (spot L2 buckets by day; Vision
 * distance bands by 10-minute bin).  This probe exercises the two pure mapping
 * functions and the tooltip/status null-safety under the same stub-DOM harness
 * the other probes use, so the rasterization cannot drift silently.
 *
 * Run:  node BTC_True_Volume/probe_orderbook.js      (from the project root)
 */
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const HERE = __dirname;

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
      if (k === "style") return mk("style");
      if (k === "dataset") return mk("dataset");
      if (k === "offsets") return undefined;
      if (!(k in t)) t[k] = mk(name + "." + String(k));
      return t[k];
    },
    set(_, k, v) { t[k] = v; return true; },
    apply() { return mk(name + "()"); },
    construct() { return mk("new " + name); },
  });
  return p;
}
function stub(name) { const f = function () { return mk(name + "()"); }; return mk(name, f); }

const els = new Map();
function el(id) { if (!els.has(id)) els.set(id, mk("el#" + id)); return els.get(id); }
const documentStub = mk("document");
documentStub.getElementById = id => el(id);
documentStub.querySelector = () => stub("query");
documentStub.createElement = () => stub("createElement");
documentStub.createElementNS = () => stub("createElementNS");
documentStub.body = stub("body");
documentStub.documentElement = stub("documentElement");
documentStub.fonts = stub("fonts");

const store = new Map();
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
  LightweightCharts: stub("LightweightCharts"),
  devicePixelRatio: 1,
  __probe: true,
};
ctx.globalThis = ctx;
vm.createContext(ctx);

const hook = `
globalThis.__hook = {
  setBars(bars, symbol, interval) {
    S.bars = bars; S.symbol = symbol || "BTCUSDT"; S.interval = interval || "1d";
  },
  setPrice(v) { candleSeries.coordinateToPrice = () => v; },
  barIndex: barIndexForTs,
  spotGrid: buildSpotHeatGrid,
  depthGrid: buildDepthHeatGrid,
  tooltip: heatTooltipRow,
  status() { updateSpotHeatStatus(); updateDepthHeatStatus(); },
  states: () => ({ spot: spotHeatState, depth: depthHeatState }),
};
`;
const src = fs.readFileSync(path.join(HERE, "visualizer", "static", "app.js"), "utf8");
vm.runInContext(src + hook, ctx, { filename: "app.js" });

let bad = 0;
function eq(label, got, want) {
  if (JSON.stringify(got) !== JSON.stringify(want)) {
    bad++;
    console.log(`[FAIL] ${label}\n       got  ${JSON.stringify(got)}\n       want ${JSON.stringify(want)}`);
  } else {
    console.log(`[ ok ] ${label} = ${JSON.stringify(got)}`);
  }
}
function approx(label, got, want, tol) {
  if (Math.abs(got - want) > tol) {
    bad++;
    console.log(`[FAIL] ${label}\n       got  ${got}\n       want ${want} +/- ${tol}`);
  } else {
    console.log(`[ ok ] ${label} = ${got}`);
  }
}

(async () => {
  await new Promise(r => setTimeout(r, 30));
  const h = ctx.__hook;

  h.setBars([{ open_time: 0 }, { open_time: 86400000 }, { open_time: 172800000 }],
            "BTCUSDT", "1d");
  eq("barIndex at first bar", h.barIndex(0), 0);
  eq("barIndex mid-day", h.barIndex(50000000), 0);
  eq("barIndex exact second day", h.barIndex(86400000), 1);
  eq("barIndex before range", h.barIndex(-1), -1);

  const spot = { available: true, bucket_usd: 10, p99_notional: 5000, columns: [
    { t: 0, mid: 80005, bids: [[80000, 1, 5000, 3]], asks: [[80010, 2, 3000, 4]] },
  ] };
  const sg = h.spotGrid(spot);
  eq("spot grid bars", Array.from(sg.byBar.keys()), [0]);
  eq("spot bid value", sg.byBar.get(0).cells.get("bid|80000").v, 5000);
  eq("spot ask value", sg.byBar.get(0).cells.get("ask|80010").v, 3000);
  eq("spot bid wall", sg.byBar.get(0).wallBid, 5000);

  const spot2 = { ...spot, columns: [
    { t: 0, mid: 80005, bids: [[80000, 1, 500, 1]], asks: [] },
    { t: 0, mid: 80005, bids: [[80000, 1, 900, 1]], asks: [] },
  ] };
  eq("spot max across snapshots in one bar",
     h.spotGrid(spot2).byBar.get(0).cells.get("bid|80000").v, 900);

  const depth = { available: true, p99_notional: 1000, columns: [
    { t: 0, mid: 100000,
      bids: [[0, 1, 1000, 10]], asks: [[0, 1, 2000, 20]] },
  ] };
  const dg = h.depthGrid(depth);
  const cells = dg.byBar.get(0).cells;
  const bids = [...cells.values()].filter(c => c.side === "bid");
  const asks = [...cells.values()].filter(c => c.side === "ask");
  // 1% of $100k = $1,000 == 10 buckets of $100 per side.
  eq("depth bid bucket count", bids.length, 10);
  eq("depth ask bucket count", asks.length, 10);
  approx("depth bid notional total", bids.reduce((s, c) => s + c.v, 0), 1000, 1e-6);
  approx("depth ask notional total", asks.reduce((s, c) => s + c.v, 0), 2000, 1e-6);
  eq("depth lowest bid bucket", Math.min(...bids.map(c => c.p)), 99000);
  eq("depth highest ask bucket", Math.max(...asks.map(c => c.p)), 100900);

  const depth2 = { ...depth, columns: [
    { t: 0, mid: 100000, bids: [[0, 0.2, 100, 1]], asks: [] },
    { t: 0, mid: 100000, bids: [[0, 0.2, 700, 1]], asks: [] },
  ] };
  const d2 = [...h.depthGrid(depth2).byBar.get(0).cells.values()]
    .filter(c => c.side === "bid");
  // The 0.2% band is 2 buckets wide; the $700 reading splits into two $350
  // buckets, so the strongest bucket is 350 (not the band total).
  eq("depth max across bins in one bar", Math.max(...d2.map(c => c.v)), 350);
  approx("depth 0.2% band total per bin", d2.reduce((s, c) => s + c.v, 0), 700, 1e-6);

  eq("tooltip with no data", h.tooltip(0, 0), "");
  const states = h.states();
  states.spot.data = spot;
  states.spot.visible = true;
  h.setPrice(80005);
  const row = h.tooltip(0, 0);
  eq("tooltip names the spot bid", row.includes("Spot L2 bid"), true);
  states.depth.data = depth;
  states.depth.visible = true;
  h.setPrice(99500);              // -0.5% from mid 100000 -> bid band [0,1]
  const row2 = h.tooltip(0, 0);
  eq("tooltip names the futures bid band", row2.includes("Futures depth bid"), true);
  h.status();   // must not throw with a stub DOM

  console.log(bad ? `\n${bad} heat check(s) FAILED` : "\nall heat checks pass");
  process.exit(bad ? 1 : 0);
})();
