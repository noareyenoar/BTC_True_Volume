/* probe_dates.js — the typed date boxes' parsing and validation, run for real.
 *
 * app.js is evaluated in a vm over a stub DOM (the same harness as
 * probe_dated_pair.js), so parseDay/fmtDay/cbWindowLabel and the apply path are
 * the shipped functions, not a transcription. Covers what the dated-pair probe
 * cannot: the dd/mm/yyyy parsers, the calendar check, the store-span refusal
 * (including the fresh-end grace: a `to` past the newest stored date is still
 * answered within CB_FRESH_GRACE_DAYS), the red `missing` flag's render, and
 * that a valid pair is stored ISO (the API's own format) with the boxes
 * normalised back to day-first.
 *
 * Run:  node BTC_True_Volume/probe_dates.js      (from the project root)
 */
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const HERE = __dirname;
const SITE = path.join(HERE, "site");

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

/* the hook is appended to app.js's own source so these closures see the real
 * top-level bindings (STATIC is a `let` inside the same script -- a later
 * vm.runInContext script would not see it; see probe_dated_pair.js) */
const hook = `
globalThis.__hook = {
  setStatic(v) { STATIC = v; },
  fmtDay, fmtDaysIn, parseDay, fmtTs, cbWindowLabel, cbStoreSpan,
  applyCustomCostBasis, cbAddDays,
  state() { return { cbCustom: S.cbCustom, cbWindow: S.cbWindow }; },
  // the Who Moved card's own HTML, so the group-header totals are read from
  // what the page would render rather than from a transcription of it
  moversProbe(payload) {
    costBasisState.data = payload;
    renderCostBasisMovers();
    return String(document.getElementById("cb-movers").innerHTML || "");
  },
  // the red "missing …" flag beside the Custom controls, as rendered: what the
  // eyes get is a separate claim from what the payload says
  statusProbe(payload) {
    costBasisState.data = payload;
    updateCostBasisStatus();
    const flag = document.getElementById("cb-missing");
    return { hidden: !!flag.hidden, text: String(flag.textContent || ""),
             title: String(flag.title || "") };
  },
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

(async () => {
  await new Promise(r => setTimeout(r, 30));
  const h = ctx.__hook;

  eq("fmtDay iso", h.fmtDay("2026-08-24"), "24/08/2026");
  eq("fmtDay null", h.fmtDay(null), "—");
  eq("fmtDay prose passes through", h.fmtDay("30d"), "30d");
  eq("fmtDaysIn prose", h.fmtDaysIn("data to 2026-10-03; from 2016-01-03 to 2026-09-27"),
     "data to 03/10/2026; from 03/01/2016 to 27/09/2026");
  const t = Date.UTC(2026, 7, 24, 15, 4) / 1000;
  eq("fmtTs 1d", h.fmtTs(t * 1000, "1d"), "24/08/2026");
  eq("fmtTs 1h", h.fmtTs(t * 1000, "1h"), "24/08/2026 15:04");

  eq("parseDay dd/mm/yyyy", h.parseDay("24/08/2026"), "2026-08-24");
  eq("parseDay d/m/yyyy", h.parseDay("3/1/2016"), "2016-01-03");
  eq("parseDay dashes", h.parseDay("24-08-2026"), "2026-08-24");
  eq("parseDay dots", h.parseDay("24.08.2026"), "2026-08-24");
  eq("parseDay iso", h.parseDay("2026-08-24"), "2026-08-24");
  eq("parseDay iso relaxed", h.parseDay("2026-8-4"), "2026-08-04");
  eq("parseDay calendar-invalid", h.parseDay("31/02/2026"), null);
  eq("parseDay month 13", h.parseDay("13/13/2026"), null);
  eq("parseDay garbage", h.parseDay("yesterday"), null);
  eq("parseDay empty", h.parseDay(""), null);
  eq("parseDay whitespace", h.parseDay("  24/08/2026 "), "2026-08-24");

  eq("cbWindowLabel custom", h.cbWindowLabel({ requested_from: "2026-08-24", requested_to: "2026-09-24" }),
     "24/08/2026 → 24/09/2026");
  eq("cbWindowLabel preset label", h.cbWindowLabel({ window_label: "30d" }), "30d");
  eq("cbWindowLabel iso prose", h.cbWindowLabel({ window_label: "2026-08-27 → 2026-09-26" }),
     "27/08/2026 → 26/09/2026");

  // live mode (no manifest): the server owns the answer, so no local refusal
  eq("cbStoreSpan live (no manifest)", h.cbStoreSpan(), null);
  const man = JSON.parse(fs.readFileSync(path.join(SITE, "manifest.json"), "utf8"));
  h.setStatic(man);
  const span = h.cbStoreSpan();
  const a = man.cost_basis_archive.BTCUSDT, d = man.cost_basis_dated.BTCUSDT;
  eq("cbStoreSpan union first", span.first, d.first < a.first ? d.first : a.first);
  eq("cbStoreSpan union last", span.last, d.last > a.last ? d.last : a.last);

  const setBoxes = (f, t) => { el("cb-from").value = f; el("cb-to").value = t; };
  const strip = () => (el("status-cb").innerHTML || "").replace(/<[^>]*>/g, "").trim();
  setBoxes("not a date", "24/09/2026"); h.applyCustomCostBasis();
  console.log(`[info] apply bad-format strip: ${strip()}`);
  eq("apply bad format does not store", h.state().cbCustom, null);
  setBoxes("24/09/2026", "24/08/2026"); h.applyCustomCostBasis();
  console.log(`[info] apply reversed strip: ${strip()}`);
  eq("apply reversed does not store", h.state().cbCustom, null);
  setBoxes("01/01/2010", "24/09/2026"); h.applyCustomCostBasis();
  console.log(`[info] apply out-of-span strip: ${strip()}`);
  eq("apply out-of-span does not store", h.state().cbCustom, null);
  // ISO typed by hand still parses; the boxes come back day-first (syncCostBasisControls)
  setBoxes("2026-08-24", "2026-09-24"); h.applyCustomCostBasis();
  eq("apply valid stores ISO", h.state().cbCustom, { from: "2026-08-24", to: "2026-09-24" });
  eq("apply valid switches window", h.state().cbWindow, "custom");
  eq("boxes normalised day-first", [el("cb-from").value, el("cb-to").value],
     ["24/08/2026", "24/09/2026"]);

  // The fresh-end grace: a `to` past the newest stored date is answered from
  // that date up to CB_FRESH_GRACE_DAYS out (the payload's `missing` then names
  // the tail in red), and still refused past it — the drag-to-the-right-edge
  // pick must never hit a local refusal.
  eq("cbStoreSpan grace", span.grace,
     d.fresh_grace_days !== undefined ? d.fresh_grace_days : 7);
  const toWithin = h.cbAddDays(span.last, 5);
  setBoxes("2026-08-24", toWithin); h.applyCustomCostBasis();
  eq("apply within grace stores", h.state().cbCustom, { from: "2026-08-24", to: toWithin });
  const before = h.state().cbCustom;
  setBoxes("2026-08-24", h.cbAddDays(span.last, 9)); h.applyCustomCostBasis();
  console.log(`[info] apply beyond-grace strip: ${strip()}`);
  eq("apply beyond grace keeps the old pair", h.state().cbCustom, before);

  // The red flag: the tail span, or a single day, and hidden for windows that
  // reach their end, presets and refusals (render-driven, cannot go stale)
  const body = { available: true, as_of: "2026-10-04", prev_as_of: "2026-09-20",
    data_vintage: "2026-10-04", span_days: 14, net_delta_btc: 100,
    delta: [{ p: 1000, d: 0, past: 0, now: 0, cum_acc: 0, cum_dist: 0 }],
    price_usd: null, price_usd_prev: null, computed: true,
    estimate_kind: "archive-pair", window_label: "2026-09-20 → 2026-10-04",
    requested_from: "2026-08-21", requested_to: "2026-10-09" };
  const r1 = h.statusProbe({ ...body, missing: { from: "2026-10-05", to: "2026-10-09" } });
  // the answering store is NAMED in the flag: a weekly store's tail is its own
  // grid, not a failed refresh (2026-10-16 -- the reader took it for stale data)
  eq("flag text two days", r1.text,
     "missing 05/10/2026 – 09/10/2026 · weekly store (newest 04/10/2026)");
  eq("flag shown", r1.hidden, false);
  eq("flag title names the newest date", r1.title.includes("04/10/2026"), true);
  eq("flag title says the days are past the curve, not late",
     r1.title.includes("not fail to arrive"), true);
  const r2 = h.statusProbe({ ...body, missing: { from: "2026-10-10", to: "2026-10-10" } });
  eq("flag text single day", r2.text, "missing 10/10/2026 · weekly store (newest 04/10/2026)");
  // ...and a pair the publisher's own DATED curves answered carries no such
  // suffix: that store is daily, so its tail really is a curve not stored yet
  const r2b = h.statusProbe({ ...body, estimate_kind: "dated-pair-custom",
    missing: { from: "2026-10-10", to: "2026-10-12" } });
  eq("dated-pair flag keeps the plain wording", r2b.text,
     "missing 10/10/2026 – 12/10/2026");
  const r3 = h.statusProbe({ ...body, missing: null });
  eq("flag hidden without missing", [r3.hidden, r3.text], [true, ""]);
  const r4 = h.statusProbe({ available: false, as_of: null, data_vintage: null,
    delta: [], note: "some refusal" });
  eq("flag hidden on refusal", r4.hidden, true);

  // The Who Moved group headers carry the window-wide totals: every zone of the
  // window summed by sign (not just the 6 rows each side), rendered with the
  // same grossAcc/grossDist the footnote's prose uses.
  const mv = h.moversProbe({
    available: true, as_of: "2026-09-20", prev_as_of: "2026-08-27",
    data_vintage: "2026-09-20", span_days: 24, net_delta_btc: 100,
    delta: [
      { p: 9000, d: 120, past: 200, now: 320, cum_acc: 0, cum_dist: 0 },
      { p: 10500, d: -50, past: 300, now: 250, cum_acc: 0, cum_dist: 0 },
      { p: 11000, d: 30, past: 100, now: 130, cum_acc: 0, cum_dist: 0 },
    ],
    price_usd: 10000, price_usd_prev: 9800, computed: true,
    estimate_kind: "dated-pair-custom", window_label: "2026-08-27 → 2026-09-20",
    requested_from: "2026-08-27", requested_to: "2026-09-20" });
  const mvs = mv.replace(/\s+/g, " ");
  eq("movers sold-off total on the header",
     mvs.includes("SOLD OFF — cohorts that shrank · 50.00 BTC total"), true);
  eq("movers bought-in total on the header",
     mvs.includes("BOUGHT IN — cohorts that grew · 150.00 BTC total"), true);
  eq("movers footnote agrees with the header totals",
     mvs.includes("150.00 BTC accumulated / -50.00 BTC distributed"), true);

  console.log(bad ? `\n${bad} check(s) FAILED` : "\nall date checks pass");
  process.exit(bad ? 1 : 0);
})();
