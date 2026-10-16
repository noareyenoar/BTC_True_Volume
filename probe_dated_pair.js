/* probe_dated_pair.js — run the BROWSER's custom-pair path in node, no browser.
 *
 * app.js is evaluated in a vm context over a stub DOM, so the functions that a
 * published page would run are the functions under test -- not a transcription
 * of them. The probe then feeds the real manifest and the real cost_basis
 * artifacts from site/ and diffs the resulting payload against the live API's
 * own body (dumped by probe_dated_server.py) field for field.
 *
 * The stub is a universal Proxy: any property is another stub, any call returns
 * a stub, and primitive coercion is 0/"" so arithmetic in the app does not throw.
 * `then` is deliberately absent, so nothing accidentally awaits a stub and hangs.
 *
 * Run:  node BTC_True_Volume/probe_dated_pair.js      (from the project root)
 */
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const zlib = require("zlib");

const HERE = __dirname;
/* PROBE_SITE points the probe at another copy of the export -- the published
 * one, typically, so the check covers what readers actually download. */
const SITE = process.env.PROBE_SITE || path.join(HERE, "site");

let ID = 0;
function stub(name) {
  const f = function () { return mk(name + "()"); };
  return mk(name, f);
}
function mk(name, target) {
  const t = target || function () {};
  const p = new Proxy(t, {
    get(_, k) {
      if (k === "then") return undefined;              // never thenable
      if (k === Symbol.toPrimitive) return () => 0;
      if (k === Symbol.iterator) return [][Symbol.iterator].bind([]);
      if (k === Symbol.toStringTag) return "Stub";
      if (k === "length") return 0;
      if (k === "id" || k === "name") return name;
      // hand back what was WRITTEN, not a constant "": a stub that always answers
      // "" makes every text assertion pass vacuously (see the probe-lies notes)
      if (k === "innerHTML" || k === "textContent" || k === "value") return k in t ? t[k] : "";
      if (k === "classList") return { add() {}, remove() {}, toggle() {}, contains: () => false };
      if (k === "style") return mk("style");
      if (k === "dataset") return mk("dataset");
      if (k === "offsets") return undefined;           // cbArchiveOffsets caches here
      if (!(k in t)) t[k] = stub(name + "." + String(k));
      return t[k];
    },
    set(_, k, v) { t[k] = v; return true; },
    apply() { return stub(name + "()"); },
    construct() { return stub("new " + name); },
  });
  return p;
}

// elements are stubs but must remember what was written to them, for the
// status-strip assertions at the end
const els = new Map();
function el(id) {
  if (!els.has(id)) els.set(id, mk("el#" + id));
  return els.get(id);
}
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
  fs, path, SITE, require,
  zlib: require("zlib"),
  __probe: true,
};
ctx.globalThis = ctx;
vm.createContext(ctx);

/* boot() is an IIFE at the bottom of app.js and runs on evaluation. Its first
 * await is a fetch, which the stub refuses, so nothing else in the app starts;
 * the probe drives the one code path it is here for.
 *
 * The hook below is appended to app.js's OWN source on purpose: `STATIC` is a
 * top-level `let`, and a lexical declaration is not visible to a later
 * vm.runInContext script (it would silently create a global property instead,
 * leaving the app reading its own null). Compiled in the same script, these
 * closures reach the real bindings. */
const hook = `
globalThis.__probeHook = {
  setStatic(v) { STATIC = v; },
  setLoader(fn) { loadJSONFile = fn; },
  readStatic() { return STATIC; },
  // what a reader actually sees: the strip, and the red "missing …" flag beside
  // the Custom controls (empty string while hidden). What the payload says and
  // what the page prints about it are two different claims (see
  // updateCostBasisStatus).
  readCbStrip(payload) {
    costBasisState.data = payload;
    updateCostBasisStatus();
    const el = document.getElementById("status-cb");
    const flag = document.getElementById("cb-missing");
    return {
      strip: el ? el.innerHTML : "(no #status-cb)",
      missing: flag ? (flag.hidden ? "" : String(flag.textContent))
        : "(no #cb-missing)",
    };
  },
};
`;
const src = fs.readFileSync(path.join(HERE, "visualizer", "static", "app.js"), "utf8");
vm.runInContext(src + hook, ctx, { filename: "app.js" });

const g = ctx;

/* The shipped artifact stores each supply value as an integer number of centi-BTC
 * (a JSON array of decimals does not compress), so the browser's numbers are the
 * server's rounded to 0.01 BTC -- the same bound verify_all uses to prove the
 * archive export rebuildable. Everything that is a COUNT, a price, a date or a
 * flag must still match exactly; only these derived quantities get a bound. */
const CENT = 0.005;
// ISO -> the dd/mm/yyyy the page prints (mirrors fmtDay, the reading the eyes get)
const dm = iso => iso.slice(8, 10) + "/" + iso.slice(5, 7) + "/" + iso.slice(0, 4);
/* The red flag's text, mirrored from app.js's updateCostBasisStatus — ONE
 * place, because this ran as two copies and the copies are a trap: the wording
 * changed in app.js on 2026-10-16 (a weekly-archive tail now names its store)
 * and the stale copies here turned a wording edit into a FAILED cloud run whose
 * push step was skipped. A tail the WEEKLY archive could not carry is that
 * store's own grid, so the flag says so and quotes its newest anchor; a tail
 * from the dated store is a curve not stored yet and carries no such note. */
function wantFlag(payload) {
  const m = payload && payload.missing;
  if (!m) return "";
  const newest = payload.as_of ? dm(payload.as_of) : "—";   // fmtDay's own dash
  return "missing " + dm(m.from) + (m.from === m.to ? "" : " – " + dm(m.to))
    + (payload.estimate_kind === "archive-pair"
        ? " · weekly store (newest " + newest + ")" : "");
}
// the payload's own positive/negative totals: the denominators cum_acc/cum_dist
// are shares of, needed to bound their rounding (see tolFor)
const sumPos = rows => rows.reduce((s, r) => s + Math.max(r.d, 0), 0);
const sumNeg = rows => rows.reduce((s, r) => s + Math.max(-r.d, 0), 0);
function tolFor(label, box) {
  const key = label.replace(/\[\d+\]/g, "").split(".").pop();
  if (key === "cum_acc" || key === "cum_dist") {
    // a cumulated sum of rounded per-bucket values divided by the payload's own
    // total: the client's centi-BTC rounding puts up to CENT*(i+1) into the
    // numerator and CENT*n into the denominator, so bound the ratio by that
    // budget over the base (cum <= 1 folds into the n term). The flat 1e-5
    // relative rule that suffices for the dated store's ~250 rows does not
    // absorb 600+ archive buckets of accumulated rounding on tiny mid-array
    // values.
    const i = +(label.match(/\[(\d+)\]/) || [0, 0])[1];
    const base = (key === "cum_acc" ? box.posSum : box.negSum) || 1;
    return { abs: CENT * (i + box.nRows + 1) / base, rel: 1e-5 };
  }
  if (key === "past" || key === "now" || key === "s") return { abs: box.perRow };
  if (key === "d" || key === "delta_btc") return { abs: 2 * box.perRow };
  if (key === "net_delta_btc") return { abs: 2 * box.perRow * box.nRows };
  if (key === "supply_btc") return { abs: 2 * box.perRow };
  return null;                     // exact: prices, counts, spans, dates, flags
}
function close(a, b, tol) {
  if (!tol) return false;
  const okAbs = tol.abs !== undefined && Math.abs(a - b) <= tol.abs;
  const okRel = tol.rel !== undefined
    && Math.abs(a - b) <= tol.rel * Math.max(Math.abs(a), Math.abs(b));
  return okAbs || okRel;
}
function deepDiff(a, b, label, out, box) {
  if (a === b) return;
  const bothObj = a && b && typeof a === "object" && typeof b === "object";
  if (!bothObj) {
    if (typeof a === "number" && typeof b === "number" && close(a, b, tolFor(label, box))) return;
    out.push(`${label}: client=${JSON.stringify(a)} server=${JSON.stringify(b)}`);
    return;
  }
  if (Array.isArray(a) !== Array.isArray(b)) { out.push(`${label}: array-ness differs`); return; }
  if (Array.isArray(a)) {
    if (a.length !== b.length) { out.push(`${label}.length: client=${a.length} server=${b.length}`); return; }
    for (let i = 0; i < a.length; i++) deepDiff(a[i], b[i], `${label}[${i}]`, out, box);
    return;
  }
  const keys = new Set([...Object.keys(a), ...Object.keys(b)]);
  for (const k of keys) deepDiff(a[k], b[k], `${label}.${k}`, out, box);
}
/* How many stored buckets one payload row can be made of: a $1,000 zone drawn
 * from a $200-bucket era sums five of them, so its rounding budget is five
 * hundredths of a BTC, not one. Read off the two curves' own smallest steps. */
function perRowBound(ar, got) {
  const smallestStep = rows => {
    let m = Infinity;
    for (let i = 1; i < rows.p.length; i++) {
      const d = rows.p[i] - rows.p[i - 1];
      if (d > 0 && d < m) m = d;
    }
    return m;
  };
  const zone = got.grid.zone_usd;
  const a = smallestStep(g.cbDatedRows(ar, got.prev_as_of));
  const b = smallestStep(g.cbDatedRows(ar, got.as_of));
  const perZone = zone / Math.min(a, b);
  return CENT * Math.max(1, perZone) * 1.01;
}

(async () => {
  await new Promise(r => setTimeout(r, 50));
  const hook = ctx.__probeHook;
  // the same file reads the browser does, from disk instead of over HTTP
  hook.setLoader(async p => {
    const buf = fs.readFileSync(path.join(SITE, p));
    const text = p.endsWith(".gz") ? zlib.gunzipSync(buf).toString("utf8") : buf.toString("utf8");
    return JSON.parse(text);
  });
  hook.setStatic(JSON.parse(fs.readFileSync(path.join(SITE, "manifest.json"), "utf8")));
  if (!hook.readStatic()) { console.log("probe could not arm STATIC"); process.exit(2); }
  const scenarios = JSON.parse(fs.readFileSync(path.join(HERE, "probe_dated_server.json"), "utf8"));
  let bad = 0;
  for (const sc of scenarios) {
    const label = `${sc.from} -> ${sc.to}`;
    let got;
    try {
      got = await g.cbPairFromDated("BTCUSDT", sc.from, sc.to);
    } catch (e) {
      console.log(`[FAIL] ${label}: client threw ${e.message}`);
      bad++; continue;
    }
    if (!got) {
      const att = await g.cbDatedAttempt("BTCUSDT", sc.from, sc.to);
      const why = att.reason || "no reason recorded — check the guards in cbDatedAttempt";
      console.log(`[ ok ] ${label}: client declines (server answered ${sc.kind}) ${sc.declined_ok ? "" : "<-- expected the client to answer"}`);
      console.log(`        reason: ${why}`);
      if (sc.strip || sc.archive_payload) {
        // exactly what api()'s custom branch then hands the strip
        const arch = await g.cbPairFromArchive("BTCUSDT", sc.from, sc.to);
        if (sc.strip) {
          const body = (att.reason && arch && arch.available === false && !arch.dated_reason)
            ? { ...arch, dated_reason: att.reason } : arch;
          const r = hook.readCbStrip(body);
          const strip = r.strip.replace(/<[^>]*>/g, "").replace(/\s+/g, " ").trim();
          console.log(`        strip: ${strip}`);
          const want = wantFlag(sc.archive_payload);
          if (r.missing !== want) {
            bad++;
            console.log(`[FAIL] ${label}: flag text client="${r.missing}" expected="${want}"`);
          } else if (r.missing) {
            console.log(`        flag: ${r.missing}`);
          }
        }
        // The archive body is a different code path from the dated one and must
        // be reproduced field for field too -- the user's own case is
        // archive-answered. $200 rows are one stored bucket wide each.
        if (sc.archive_payload) {
          const out2 = [];
          if (!arch || !arch.available) {
            out2.push(`client did not answer from the archive: ${arch && arch.note}`);
          } else {
            deepDiff(arch, sc.archive_payload, "archive", out2,
                     { nRows: arch.delta.length, perRow: CENT * 1.01,
                       posSum: sumPos(arch.delta), negSum: sumNeg(arch.delta) });
          }
          if (out2.length) {
            bad++;
            console.log(`[FAIL] ${label} (archive-pair): ${out2.length} field(s) differ`);
            out2.slice(0, 12).forEach(l => console.log("        " + l));
          } else {
            console.log(`[ ok ] ${label}: archive-pair body (${arch.delta.length} rows) `
              + `identical to the API field for field`);
          }
        }
      }
      if (!sc.declined_ok) bad++;
      continue;
    }
    const out = [];
    const ar = await g.cbLoadDated("BTCUSDT");
    deepDiff(got, sc.payload, "payload", out,
             { nRows: got.delta.length, perRow: perRowBound(ar, got),
               posSum: sumPos(got.delta), negSum: sumNeg(got.delta) });
    if (out.length) {
      bad++;
      console.log(`[FAIL] ${label} (${sc.kind}): ${out.length} field(s) differ`);
      out.slice(0, 12).forEach(l => console.log("        " + l));
    } else {
      const n = got.delta.length;
      console.log(`[ ok ] ${label}: ${sc.kind}, ${n} rows, net ${got.net_delta_btc.toFixed(3)}, `
        + `bucket ${got.bucket_usd}, kind ${got.estimate_kind} — identical to the API field for field`);
      if (sc.strip) {
        const r = hook.readCbStrip(got);
        const strip = r.strip.replace(/<[^>]*>/g, "").replace(/\s+/g, " ").trim();
        console.log(`        strip: ${strip}`);
        const want = wantFlag(sc.payload);
        if (r.missing !== want) {
          bad++;
          console.log(`[FAIL] ${label}: flag text client="${r.missing}" expected="${want}"`);
        } else if (r.missing) {
          console.log(`        flag: ${r.missing}`);
        }
      }
    }
  }
  console.log(bad ? `\n${bad} scenario(s) FAILED` : "\nall scenarios identical");
  process.exit(bad ? 1 : 0);
})();
