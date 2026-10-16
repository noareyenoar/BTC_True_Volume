/* probe_hover_toggle.js — the click-to-toggle hover readout, run for real.
 *
 * app.js is evaluated in a vm over a stub DOM (the same harness probe_dates.js
 * uses), so the handlers under test are the SHIPPED ones, not a transcription.
 * What this probe adds to that harness is the two things a mouse needs: a #chart
 * and a window that RECORD their listeners and can fire them by hand, and a
 * chart whose subscribeCrosshairMove keeps its callbacks -- the real stub
 * swallows both, so a probe without them would assert against nothing.
 *
 * Covers: the readout boots on; a click turns it off and back on; a drag, a
 * press-and-hold, a right-click, a click while the pencil is armed, and a
 * synthesised tap after touchstart all leave it alone; the clicked bar is
 * painted immediately, with its x clamped to the plot; the status strip says
 * so while it is off; and the remembered choice is what the next visit opens on.
 *
 * Run:  node BTC_True_Volume/probe_hover_toggle.js      (from the project root)
 */
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const HERE = __dirname;
const src = fs.readFileSync(path.join(HERE, "visualizer", "static", "app.js"), "utf8");

/* The universal stub, verbatim from probe_dates.js: any property is another
 * stub, any call returns a stub, and primitive coercion is 0/"" so arithmetic in
 * the app does not throw. `then` is deliberately absent so nothing awaits one. */
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
      // CACHED, unlike probe_dates.js: the app reads back what it wrote to
      // style.display, and a fresh stub on every read answers "" for everything
      // -- which is exactly the kind of probe lie that makes assertions vacuous.
      if (k === "style") { if (!(k in t)) t[k] = mk(name + ".style"); return t[k]; }
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

/* One page load. `stored` seeds localStorage before app.js is evaluated, which
 * is the only way to see what a returning reader actually opens on. */
function boot(stored = {}) {
  const store = new Map(Object.entries(stored));
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

  // listeners are recorded, not swallowed: the probe fires them itself
  const chartListeners = new Map(), winListeners = new Map();
  const record = map => (type, fn) => {
    if (!map.has(type)) map.set(type, []);
    if (fn) map.get(type).push(fn);
  };
  const windowStub = stub("window");
  windowStub.addEventListener = record(winListeners);
  const chartEl = el("chart");
  chartEl.addEventListener = record(chartListeners);
  // a plot of known geometry, so the clamp below is a number and not a stub
  chartEl.getBoundingClientRect = () => ({ left: 0, top: 0, width: 800, height: 400 });

  // the one stub that must remember: the crosshair callbacks
  const crosshairCbs = [];
  let coordTime = () => null;
  const ts = stub("timescale");
  ts.coordinateToTime = x => coordTime(x);
  ts.width = () => 800;
  const chart = stub("chart");
  chart.subscribeCrosshairMove = cb => crosshairCbs.push(cb);
  chart.timeScale = () => ts;
  const lightcharts = stub("LightweightCharts");
  lightcharts.createChart = () => chart;

  const ctx = {
    console, setTimeout, clearTimeout, setInterval, clearInterval,
    Math, JSON, Date, Map, Set, Array, Object, Number, String, Boolean, Error,
    Promise, isFinite, isNaN, parseFloat, parseInt, encodeURIComponent,
    decodeURIComponent, URL, URLSearchParams, Intl, TextEncoder, TextDecoder,
    document: documentStub,
    window: windowStub,
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

  /* The hook is appended to app.js's OWN source: `S`, `candleSeries` and
   * `pickState` are top-level bindings inside the same script, and a later
   * vm.runInContext would not see them (see probe_dated_pair.js). */
  const hook = `
globalThis.__hook = {
  setBars(bars) { S.bars = bars; },
  series() { return candleSeries; },
  hoverOn() { return S.hoverInfo; },
  stored() { return localStorage.getItem("vsa_hover_info_v1"); },
  display() { return document.getElementById("tooltip").style.display; },
  body() { return String(document.getElementById("tooltip").innerHTML || ""); },
  setPickMode,
  // what the eyes get: the strip is rebuilt by updateStatus(), so the note has
  // to survive that rebuild to be worth anything
  strip() {
    updateStatus();
    return String(document.getElementById("status-hover").innerHTML || "");
  },
};
`;
  vm.runInContext(src + hook, ctx, { filename: "app.js" });
  return { ctx, chartListeners, winListeners, crosshairCbs, setCoordTime: f => { coordTime = f; } };
}

function fireList(fns, ev) {
  for (const fn of fns) fn(ev);
  return fns.length;
}
function fire(map, type, ev) {
  return fireList(map.get(type) || [], ev);
}
// an event needs the methods other handlers on the same gesture will call (the
// pencil's mousedown stops propagation); a bare object would throw there and
// take the probe down with it
const evt = (o = {}) => Object.assign({
  button: 0, clientX: 0, clientY: 0, touches: [],
  stopPropagation() {}, stopImmediatePropagation() {}, preventDefault() {},
}, o);
function down(hh, x, y, button = 0) {
  fire(hh.chartListeners, "mousedown", evt({ clientX: x, clientY: y, button }));
}
function up(hh, x, y, button = 0) {
  fire(hh.winListeners, "mouseup", evt({ clientX: x, clientY: y, button }));
}
function click(hh, x, y, o = {}) {
  const b = o.button === undefined ? 0 : o.button;
  down(hh, x, y, b);
  up(hh, o.upX === undefined ? x : o.upX, o.upY === undefined ? y : o.upY, b);
}
// touchstart, then the mousedown/mouseup the browser synthesises from it. The
// clock is then moved past the tap window so the NEXT click is a mouse again --
// otherwise every click after this one would be read as synthetic too.
function tap(hh, x, y) {
  fire(hh.winListeners, "touchstart", evt());
  down(hh, x, y);
  up(hh, x, y);
  NOW += TAP_WINDOW_MS * 2;
}
function holdClick(hh, ms) {
  down(hh, 100, 100);
  NOW += ms;
  up(hh, 100, 100);
}

const BAR_SEC = Date.UTC(2026, 9, 14) / 1000;   // 14/10/2026, the newest daily bar
const BAR_MS = BAR_SEC * 1000;

/* A clock the probe owns. Two of the rules under test are measured in
 * milliseconds -- the tap that must not be read as a mouse, and the
 * press-and-hold that must not be read as a click -- and a probe that sleeps
 * real seconds to prove them is slower and flakier than the code it checks.
 * HOVER_TAP_MS is app.js's own window, restated here so the two cannot drift. */
const TAP_WINDOW_MS = 1000;
let NOW = Date.now();
Date.now = () => NOW;

let bad = 0;
function eq(label, got, want) {
  if (JSON.stringify(got) !== JSON.stringify(want)) {
    bad++;
    console.log(`[FAIL] ${label}\n       got  ${JSON.stringify(got)}\n       want ${JSON.stringify(want)}`);
  } else {
    console.log(`[ ok ] ${label} = ${JSON.stringify(got)}`);
  }
}
// the crosshair event LightweightCharts hands the handler: a bar time, a pane
// point, and the series data keyed by the SAME candleSeries object the app made
const crosshair = (h, x = 100, y = 100) => ({
  time: BAR_SEC, point: { x, y },
  seriesData: new Map([[h.series(), { time: BAR_SEC }]]),
});

(async () => {
  await new Promise(r => setTimeout(r, 30));

  /* ---------------- the page as it opens ---------------- */
  const hh = boot({});
  const h = hh.ctx.__hook;
  h.setBars([{ open_time: BAR_MS, market: "spot", open: 1, high: 2, low: 0.5, close: 1.5 }]);

  eq("boot: readout is ON", h.hoverOn(), true);
  eq("boot: nothing stored yet", h.stored(), null);
  eq("boot: strip is silent while on", h.strip(), "");

  fireList(hh.crosshairCbs, crosshair(h));
  eq("hover with the readout on paints the box", h.display(), "block");
  eq("...and it is the bar under the cursor", h.body().includes("14/10/2026"), true);

  /* ---------------- click 1: off ---------------- */
  click(hh, 100, 100);
  eq("click switches it off", h.hoverOn(), false);
  eq("click takes the box off screen at once", h.display(), "none");
  eq("click remembers the choice", h.stored(), "0");
  eq("strip names the switch while off", h.strip().includes("Hover readout off"), true);
  fireList(hh.crosshairCbs, crosshair(h));       // a mouse move, still hovering
  eq("hover with the readout off paints nothing", h.display(), "none");

  /* ---------------- click 2: back on, painted at the click ---------------- */
  let seenX = null;
  hh.setCoordTime(x => { seenX = x; return BAR_SEC; });
  click(hh, 250, 120);
  eq("click switches it back on", h.hoverOn(), true);
  eq("click remembers that too", h.stored(), "1");
  eq("the clicked bar is painted without waiting for a move", h.display(), "block");
  eq("...at the clicked x", seenX, 250);
  eq("strip goes quiet again", h.strip(), "");

  // the clamp: a click right of the loaded range is answered from the edge
  click(hh, 250, 120);                            // off again (the clamp is read on the way back on)
  eq("the next click switches it off again", h.hoverOn(), false);
  hh.setCoordTime(x => { seenX = x; return BAR_SEC; });
  click(hh, 990, 120);
  eq("a click past the plot is clamped to the edge", seenX, 799);
  eq("...and still paints the bar", h.display(), "block");

  // ...and a click over whitespace paints nothing (no bar at that time)
  click(hh, 400, 120);                            // off again
  hh.setCoordTime(() => null);
  click(hh, 400, 120);
  eq("a click on no time still turns the readout on", h.hoverOn(), true);
  eq("...and shows no box for it", h.display(), "none");

  /* ---------------- the gestures that must NOT switch ---------------- */
  hh.setCoordTime(() => null);
  click(hh, 100, 100, { upX: 160 });               // a pan: down and up 60px apart
  eq("a drag does not switch", h.hoverOn(), true);
  holdClick(hh, 5000);                             // a press-and-hold
  eq("a press-and-hold does not switch", h.hoverOn(), true);
  click(hh, 100, 100, { button: 2 });              // right button
  eq("a right-click does not switch", h.hoverOn(), true);
  click(hh, 100, 100, { upY: 140 });               // vertical drag
  eq("a vertical drag does not switch", h.hoverOn(), true);
  up(hh, 100, 100);
  eq("a mouseup with no mousedown does not switch", h.hoverOn(), true);

  // a tap: the browser synthesises mousedown/mouseup AFTER touchstart, so the
  // touchstart has to poison the mousedown that follows rather than cancel a
  // click that has already been registered
  tap(hh, 100, 100);
  eq("a synthesised tap does not switch", h.hoverOn(), true);

  // the pencil owns the chart while it is armed
  h.setPickMode(true);
  click(hh, 100, 100);
  eq("a click with the pencil armed does not switch", h.hoverOn(), true);
  h.setPickMode(false);
  click(hh, 100, 100);
  eq("...and works again once it is disarmed", h.hoverOn(), false);

  /* ---------------- what the next visit opens on ---------------- */
  const off = boot({ vsa_hover_info_v1: "0" });
  eq("a stored off opens off", off.ctx.__hook.hoverOn(), false);
  eq("...and the strip says how to get it back",
     off.ctx.__hook.strip().includes("click the chart"), true);
  const on = boot({ vsa_hover_info_v1: "1" });
  eq("a stored on opens on", on.ctx.__hook.hoverOn(), true);

  console.log(bad ? `\n${bad} FAILED` : "\nall ok");
  process.exit(bad ? 1 : 0);
})();
