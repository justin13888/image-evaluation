/* Interactive quality report. Reads the raw metrics embedded in report.html
   (#quality-metrics) and draws charts as inline SVG — no third-party library, no
   network. Everything is recomputed in the browser, so the embedded data is the
   single source of truth.

   Layout:
     - An X-axis chooser + a log/linear scale toggle (button groups) set the X
       axis shared by every rate-distortion chart: quality vs size (bpp), vs
       encode time, or vs decode time. Y is always an IQA metric.
     - Rate-distortion charts overlay every selected format's encoders, one
       full-width chart stacked per metric.
     - A filter matrix toggles which metrics and which implementations are shown.
     - Alongside: lossless compression efficiency, decoder fidelity/speed, and a
       sortable BD-rate table.
   Accessibility: the X-axis chooser is a button group (Alt+[ / Alt+] also step
   it), the filter matrix uses fieldset/legend groups, charts expose role=img +
   <title>, and view changes are announced via an aria-live region. */
(function () {
  "use strict";

  // ---- colour system -------------------------------------------------------
  // Every implementation gets its own deterministic colour, so series never
  // collapse into one shade per format (they used to, differing only by dash).
  // A format is a hue *family*: the five base hues are spaced ~72° apart around
  // the OKLCH wheel for maximum between-format separation. Implementations within
  // a family slide along a bounded hue arc paired with a lightness ramp. OKLCH is
  // perceptually uniform, so an even numeric spread reads as an even *perceived*
  // spread; the lightness component keeps series apart under colour-vision
  // deficiency / greyscale — the sole non-hue cue now that the lines are solid.

  // Base {L,C,H} per format — hues at ~72° spacing, kept near each format's prior
  // identity where the grid allowed (jxl≈red, avif≈green, webp≈blue, png≈purple;
  // jpeg shifts toward gold). Unknown / ppm / null fall back to neutral grey.
  var FORMAT_OKLCH = {
    jxl:  { L: 0.62, C: 0.11, H: 13 },
    jpeg: { L: 0.62, C: 0.11, H: 85 },
    avif: { L: 0.62, C: 0.11, H: 157 },
    webp: { L: 0.62, C: 0.11, H: 229 },
    png:  { L: 0.62, C: 0.11, H: 301 },
  };
  var FORMAT_OKLCH_FALLBACK = { L: 0.62, C: 0.0, H: 0 };
  var TONE_ARC = 40;      // total hue arc (deg) spanned by a format's impls
  var TONE_LSPAN = 0.22;  // total lightness spread across a format's impls

  // OKLab → linear sRGB (Björn Ottosson's matrices).
  function oklabToLinearRGB(L, a, b) {
    var l_ = L + 0.3963377774 * a + 0.2158037573 * b;
    var m_ = L - 0.1055613458 * a - 0.0638541728 * b;
    var s_ = L - 0.0894841775 * a - 1.2914855480 * b;
    var l = l_ * l_ * l_, m = m_ * m_ * m_, s = s_ * s_ * s_;
    return [
      4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
      -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
      -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s,
    ];
  }
  function inGamut(rgb) {
    return rgb.every(function (c) { return c >= -1e-4 && c <= 1 + 1e-4; });
  }
  function srgbHex(c) {
    c = c <= 0.0031308 ? 12.92 * c : 1.055 * Math.pow(c, 1 / 2.4) - 0.055;
    var v = Math.max(0, Math.min(255, Math.round(c * 255)));
    return (v < 16 ? "0" : "") + v.toString(16);
  }
  // OKLCH → sRGB hex, gamut-mapped by reducing chroma at constant L,H so an
  // out-of-gamut request desaturates rather than skewing hue or lightness.
  function oklchToHex(L, C, H) {
    var hr = H * Math.PI / 180, ca = Math.cos(hr), sa = Math.sin(hr);
    function rgbAt(c) { return oklabToLinearRGB(L, c * ca, c * sa); }
    var rgb = rgbAt(C);
    if (!inGamut(rgb)) {
      var lo = 0, hi = C;
      for (var i = 0; i < 18; i++) {
        var mid = (lo + hi) / 2;
        if (inGamut(rgbAt(mid))) lo = mid; else hi = mid;
      }
      rgb = rgbAt(lo);
    }
    return "#" + srgbHex(rgb[0]) + srgbHex(rgb[1]) + srgbHex(rgb[2]);
  }

  // An implementation's colour key: drop the -encode/-decode action suffix so a
  // library reads as one tone across the encode and decoder charts, while tuned
  // "@variant" runs keep their own tone.
  function toneKey(impl) { return String(impl).replace(/-(encode|decode)(?=@|$)/, ""); }

  // Per-format sorted roster of tone keys, built once from every embedded dataset
  // (not the visible subset) so a series keeps the same colour across charts and
  // however the filters are toggled. { fmt: [key, …] }.
  var TONE_ROSTER = null;
  function toneRoster() {
    if (TONE_ROSTER) return TONE_ROSTER;
    var byFmt = {};
    function add(fmt, impl) {
      if (!fmt || !impl) return;
      fmt = String(fmt).toLowerCase();
      (byFmt[fmt] = byFmt[fmt] || {})[toneKey(impl)] = 1;
    }
    METRICS.forEach(function (m) { if (m) add(m.format, m.impl); });
    Object.keys(LOSSLESS).forEach(function (n) { add(LOSSLESS[n] && LOSSLESS[n].format, n); });
    Object.keys(DECODERS).forEach(function (n) { add(DECODERS[n] && DECODERS[n].format, n); });
    TONE_ROSTER = {};
    Object.keys(byFmt).forEach(function (f) { TONE_ROSTER[f] = Object.keys(byFmt[f]).sort(); });
    return TONE_ROSTER;
  }

  // The family (format) anchor colour — the arc centre at base lightness.
  var FMT_COLOR_MEMO = {};
  function formatColor(fmt) {
    fmt = String(fmt).toLowerCase();
    if (FMT_COLOR_MEMO[fmt]) return FMT_COLOR_MEMO[fmt];
    var b = FORMAT_OKLCH[fmt] || FORMAT_OKLCH_FALLBACK;
    return (FMT_COLOR_MEMO[fmt] = oklchToHex(b.L, b.C, b.H));
  }

  // A single implementation's tone within its family. Even spacing across the
  // roster (t = i/(n-1)) maximises the minimum pairwise separation along the
  // hue-arc + lightness-ramp locus.
  var TONE_MEMO = {};
  function implTone(fmt, impl) {
    fmt = String(fmt).toLowerCase();
    var key = toneKey(impl), memoKey = fmt + "|" + key;
    if (TONE_MEMO[memoKey]) return TONE_MEMO[memoKey];
    var b = FORMAT_OKLCH[fmt] || FORMAT_OKLCH_FALLBACK;
    var roster = toneRoster()[fmt] || [key];
    var i = roster.indexOf(key); if (i < 0) i = 0;
    var n = roster.length;
    var t = n > 1 ? i / (n - 1) : 0.5;
    var hex = oklchToHex(b.L + (t - 0.5) * TONE_LSPAN, b.C, b.H + (t - 0.5) * TONE_ARC);
    return (TONE_MEMO[memoKey] = hex);
  }

  var METRICS = readJSON("quality-metrics") || [];
  var PARETO = readJSON("quality-pareto") || {};
  var BDRATE = readJSON("quality-bdrate") || {};
  // Precomputed lossless compression-efficiency summary (issue #26):
  // { impl: {format, axis, best_bpp, best_label, ratio, points:[{label,value,bpp}]} }.
  // `axis` is the swept effort knob ("" = single-knob encoder, no effort sweep).
  var LOSSLESS = readJSON("quality-lossless") || {};
  // Precomputed decoder fidelity/speed summary:
  // { impl: {format, mean_time_s, mean_bpp, count, bit_exact, worst_psnr, basis, points} }.
  var DECODERS = readJSON("quality-decoders") || {};

  // Whether this bundle persisted per-result images (asset_path on the rows; see
  // BenchmarkTask.asset_relpath). When false (legacy bundle / --no-report-images)
  // data points are not made clickable and no gallery is offered.
  var HAS_ASSETS = METRICS.some(function (m) { return m && m.asset_path; });

  // Each metric's y-axis is anchored to the metric's *known* range rather than to
  // the data, so the same metric reads on identical axes across every chart (and
  // whether you ran one image or a whole dataset). iqa-cli does not report the
  // theoretical bounds, so they are hard-coded here:
  //   lo/hi      preferred display band; the axis expands past it only to keep
  //              out-of-band points on screen (never clips).
  //   hardLo/hi  absolute theoretical bound the axis must never cross (null =
  //              that side is unbounded, so it tracks the data).
  var METRIC_INFO = {
    ssimulacra2: { key: "ssimulacra2", name: "SSIMULACRA2", y: "SSIMULACRA2 (higher is better)", lo: 0, hi: 100, hardLo: null, hardHi: 100 },
    psnr: { key: "psnr", name: "PSNR", y: "PSNR dB (higher is better)", lo: 20, hi: 50, hardLo: 0, hardHi: null },
    ssim: { key: "ssim", name: "SSIM", y: "SSIM (higher is better)", lo: 0, hi: 1, hardLo: 0, hardHi: 1 },
    butteraugli: { key: "butteraugli", name: "Butteraugli", y: "Butteraugli (lower is better)", lo: 0, hi: 3, hardLo: 0, hardHi: null },
  };
  var METRIC_ORDER = ["ssimulacra2", "psnr", "ssim", "butteraugli"];

  // Numeric axes aggregated per operating point so any of them can be an axis.
  var AXIS_KEYS = ["bpp", "ssimulacra2", "psnr", "ssim", "butteraugli", "time_s", "decode_time_s"];

  // X-axis descriptors used by the view presets. `log` is the default scale.
  var X_AXES = {
    bpp: { key: "bpp", name: "bpp", title: "Bits per pixel (bpp)", fmt: fmtNum },
    time_s: { key: "time_s", name: "encode time", title: "Encode time (s)", fmt: fmtTime },
    decode_time_s: { key: "decode_time_s", name: "decode time", title: "Decode time (s)", fmt: fmtTime },
  };

  // Interactive timing is a single-pass wall-clock (one run, no warmup) measured
  // under the parallel pool — indicative, not statistically rigorous. Any axis that
  // plots encode/decode time is flagged with a '*' + this footnote; the rigorous,
  // repeated-trial numbers live in the Performance suite. The lossless size-vs-effort
  // chart overrides the text when its endpoints are rigorously anchored (issue #26).
  var TIMING_CAVEAT =
    "* Encode/decode time is a single-pass wall-clock (one run, no warmup) — " +
    "indicative, not statistically rigorous. See the Performance suite for " +
    "isolated, repeated-trial timing pinned to a single dedicated CPU core.";
  function timingNoteHTML(text) {
    return '<p class="q-note q-timing-note">' + (text || TIMING_CAVEAT) + "</p>";
  }

  // state
  var state = {
    cat: null,                 // active category id (quality | lossless | … | sec:perf)
    graph: null,               // active graph id within the category
    graphKind: null,           // active graph kind — drives the controls box
    metric: "ssimulacra2",     // active rate-distortion metric (single-select nav step)
    xKey: "bpp",               // X axis: "bpp" | "time_s" | "decode_time_s"
    xLog: true,                // X scale: log (true) or linear (false)
    losslessView: "bars",      // lossless graph: "bars" | "effort"
    decoderView: "table",      // decoder graph: "table" | "chart"
    implsOff: {},              // impl name -> true (hidden everywhere; its legend swatch off)
    formatsOff: {},            // lowercase format key -> true (format hidden everywhere)
    barCollapsed: false,       // filter panel minimised to its header
    showTime: { rd: true },    // encode-time bubbles on the RD size views
  };

  // ---- data helpers --------------------------------------------------------

  function readJSON(id) {
    var el = document.getElementById(id);
    if (!el) return null;
    try { return JSON.parse(el.textContent); } catch (e) { return null; }
  }
  function isNum(v) { return typeof v === "number" && isFinite(v); }

  // Rows for the rate-distortion views: lossy encode rows only. Lossless rows
  // (issue #26) have no distortion axis and are shown in the lossless section.
  function validRows(rows) {
    return rows.filter(function (m) {
      return m.type === "encode" && !m.lossless && !m.error && m.bpp > 0 && isNum(m.ssimulacra2);
    });
  }

  // Aggregate to one mean point per (format, impl, quality-step) across images,
  // computing the mean (and population std) of EVERY numeric axis at once so a
  // chart can plot any (x, y) pair without re-aggregating.
  // Returns { fmt: [ {impl, points:[{label,q,count,m:{axis:mean},sd:{axis:std}}]} ] }.
  function aggregateAll(rows) {
    var byFmt = {};
    rows.forEach(function (m) {
      var impls = (byFmt[m.format] = byFmt[m.format] || {});
      var steps = (impls[m.impl] = impls[m.impl] || {});
      var s = (steps[m.label] = steps[m.label] || { label: m.label, q: m.quality_value, n: 0, acc: {}, rig: { sum: 0, sq: 0, vsum: 0, n: 0, runs: 0 } });
      s.n += 1;
      AXIS_KEYS.forEach(function (k) {
        var v = m[k];
        if (!isNum(v)) return;
        var a = s.acc[k] || (s.acc[k] = { sum: 0, sq: 0, n: 0 });
        a.sum += v; a.sq += v * v; a.n += 1;
      });
      // Fold the rigorous-timing overlay's isolated single-threaded measurement
      // back in (only the timed anchor rows carry it, and only repeated runs > 1
      // count as significant — matching plotting.lossless_efficiency). Pools the
      // per-image rigorous means: between-image spread (sq) + within-image σ (vsum).
      var rt = m.time_rigorous_s, runs = m.time_runs || 0;
      if (isNum(rt) && runs > 1) {
        var rg = s.rig, rsd = m.time_rigorous_stddev_s || 0;
        rg.sum += rt; rg.sq += rt * rt; rg.vsum += rsd * rsd; rg.n += 1;
        rg.runs = rg.runs === 0 ? runs : Math.min(rg.runs, runs);
      }
    });
    var out = {};
    Object.keys(byFmt).forEach(function (fmt) {
      out[fmt] = Object.keys(byFmt[fmt]).sort().map(function (impl) {
        var steps = byFmt[fmt][impl];
        var points = Object.keys(steps).map(function (k) {
          var s = steps[k], mean = {}, sd = {};
          Object.keys(s.acc).forEach(function (ax) {
            var a = s.acc[ax], mu = a.sum / a.n;
            mean[ax] = mu;
            sd[ax] = a.n > 1 ? Math.sqrt(Math.max(0, a.sq / a.n - mu * mu)) : 0;
          });
          // Rigorous encode-time summary across the timed images (null when none
          // were rigorously timed at this step): isolated mean + pooled σ + runs.
          var rg = s.rig, rig = null;
          if (rg.n > 0) {
            var rmu = rg.sum / rg.n;
            var within = rg.vsum / rg.n;
            var between = Math.max(rg.sq / rg.n - rmu * rmu, 0);
            rig = { t: rmu, sd: Math.sqrt(within + between), runs: rg.runs, n: rg.n };
          }
          return { label: s.label, q: s.q, count: s.n, m: mean, sd: sd, rig: rig };
        });
        return { impl: impl, points: points };
      });
    });
    return out;
  }

  // Metrics that actually carry finite encode data. SSIMULACRA2 always present
  // (it gates validRows); the rest appear only when measured. Order follows
  // METRIC_ORDER.
  function availableMetrics() {
    function hasMetric(key) {
      return METRICS.some(function (m) { return isNum(m[key]) && m.type === "encode" && !m.lossless && !m.error; });
    }
    return METRIC_ORDER.filter(function (k) { return k === "ssimulacra2" || hasMetric(k); });
  }
  // Whether any lossy encode row carries a finite value for an X axis (so the
  // matching preset is worth offering).
  function hasAxisData(key) {
    return METRICS.some(function (m) { return isNum(m[key]) && m.type === "encode" && !m.lossless && !m.error; });
  }

  // ---- scales / ticks ------------------------------------------------------

  function niceNum(range, round) {
    var exp = Math.floor(Math.log10(range));
    var f = range / Math.pow(10, exp), nf;
    if (round) nf = f < 1.5 ? 1 : f < 3 ? 2 : f < 7 ? 5 : 10;
    else nf = f <= 1 ? 1 : f <= 2 ? 2 : f <= 5 ? 5 : 10;
    return nf * Math.pow(10, exp);
  }
  function linTicks(min, max, count) {
    if (min === max) { min -= 1; max += 1; }
    var step = niceNum(niceNum(max - min, false) / (count - 1), true);
    var lo = Math.floor(min / step) * step, hi = Math.ceil(max / step) * step;
    var ticks = [];
    for (var v = lo; v <= hi + step * 0.5; v += step) ticks.push(+v.toFixed(10));
    return ticks;
  }
  function logTicks(min, max) {
    var ticks = [];
    var p0 = Math.floor(Math.log10(min)), p1 = Math.ceil(Math.log10(max));
    for (var p = p0; p <= p1; p++) {
      [1, 2, 5].forEach(function (m) {
        var v = m * Math.pow(10, p);
        if (v >= min * 0.999 && v <= max * 1.001) ticks.push(v);
      });
    }
    return ticks.length >= 2 ? ticks : [min, max];
  }
  function fmtNum(v) {
    var a = Math.abs(v);
    if (a === 0) return "0";
    if (a >= 100) return v.toFixed(0);
    if (a >= 10) return v.toFixed(1);
    if (a >= 1) return v.toFixed(2);
    return v.toFixed(3);
  }
  // Single-pass wall-clock seconds -> compact human string for tooltips/ticks.
  function fmtTime(s) {
    if (s >= 100) return s.toFixed(0) + " s";
    if (s >= 1) return s.toFixed(2) + " s";
    return (s * 1000).toFixed(0) + " ms";
  }

  // ---- tiny DOM helper -----------------------------------------------------

  function el(tag, attrs, text) {
    var e = document.createElement(tag);
    if (attrs) Object.keys(attrs).forEach(function (k) {
      if (attrs[k] != null) e.setAttribute(k, attrs[k]);
    });
    if (text != null) e.textContent = text;
    return e;
  }
  function esc(s) {
    return String(s).replace(/[&<>"]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
    });
  }
  function announce(msg) {
    var st = document.getElementById("q-status");
    if (st) st.textContent = msg;
  }

  // ---- chart geometry / time bubbles --------------------------------------

  var VBW = 1100, VBH = 460, ML = 70, MR = 20, MT = 16, MB = 56;
  var X0 = ML, X1 = VBW - MR, Y0 = MT, Y1 = VBH - MB;

  var PT_R = 4.5;                 // uniform point radius (time dimension off)
  var BUB_RMIN = 3, BUB_RMAX = 14;

  // Build a time -> radius scale over the given points' mean encode time (p.t).
  // Area ~proportional to time (radius ∝ √time). Null when nothing to encode.
  function timeScale(points) {
    var ts = [];
    points.forEach(function (p) { if (isNum(p.t) && p.t > 0) ts.push(p.t); });
    if (ts.length < 2) return null;
    var tmin = Math.min.apply(null, ts), tmax = Math.max.apply(null, ts);
    if (!(tmax > tmin)) return null;
    var s0 = Math.sqrt(tmin), s1 = Math.sqrt(tmax);
    return {
      tmin: tmin, tmax: tmax,
      r: function (t) {
        if (!isNum(t) || t <= 0) return BUB_RMIN;
        var f = (Math.sqrt(t) - s0) / (s1 - s0);
        return BUB_RMIN + Math.max(0, Math.min(1, f)) * (BUB_RMAX - BUB_RMIN);
      },
    };
  }

  function sizeLegendHTML(scale, label) {
    var refs = [scale.tmin, Math.sqrt(scale.tmin * scale.tmax), scale.tmax];
    var d = 2 * BUB_RMAX + 2;
    var items = refs.map(function (t) {
      return '<span class="q-size-item"><svg aria-hidden="true" width="' + d + '" height="' + d +
        '" viewBox="0 0 ' + d + " " + d + '"><circle cx="' + (d / 2) + '" cy="' +
        (d / 2) + '" r="' + scale.r(t).toFixed(1) + '" class="q-size-bub"/></svg>' +
        esc(fmtTime(t)) + "</span>";
    }).join("");
    return '<div class="q-size-legend"><span class="q-size-cap">' + esc(label) +
      " (bubble size)</span>" + items + "</div>";
  }

  // SVG path through screen-space points [{x,y}] (sorted by x) as a smooth cubic
  // spline (Fritsch–Carlson monotone tangents, like d3 curveMonotoneX): no
  // overshoot, so the curve never implies a sample between data points.
  function smoothPath(pts) {
    var n = pts.length;
    if (n === 0) return "";
    var p = function (q) { return q.x.toFixed(1) + " " + q.y.toFixed(1); };
    if (n === 1) return "M" + p(pts[0]) + " ";
    if (n === 2) return "M" + p(pts[0]) + " L" + p(pts[1]) + " ";
    var dx = [], dy = [], m = [];
    for (var i = 0; i < n - 1; i++) {
      dx[i] = pts[i + 1].x - pts[i].x;
      dy[i] = pts[i + 1].y - pts[i].y;
      m[i] = dx[i] !== 0 ? dy[i] / dx[i] : 0;
    }
    var t = [m[0]];
    for (var j = 1; j < n - 1; j++) {
      if (m[j - 1] * m[j] <= 0) { t[j] = 0; }
      else {
        var tj = (m[j - 1] + m[j]) / 2;
        var lim = 3 * Math.min(Math.abs(m[j - 1]), Math.abs(m[j]));
        t[j] = Math.abs(tj) > lim ? (tj > 0 ? lim : -lim) : tj;
      }
    }
    t[n - 1] = m[n - 2];
    var d = "M" + p(pts[0]) + " ";
    for (var k = 0; k < n - 1; k++) {
      if (dx[k] === 0) { d += "L" + p(pts[k + 1]) + " "; continue; }
      var c1x = pts[k].x + dx[k] / 3, c1y = pts[k].y + t[k] * dx[k] / 3;
      var c2x = pts[k + 1].x - dx[k] / 3, c2y = pts[k + 1].y - t[k + 1] * dx[k] / 3;
      d += "C" + c1x.toFixed(1) + " " + c1y.toFixed(1) + " " +
        c2x.toFixed(1) + " " + c2y.toFixed(1) + " " + p(pts[k + 1]) + " ";
    }
    return d;
  }

  // ---- generalized X/Y chart ----------------------------------------------

  // series: [{key,label,color,points:[{x,y,std,t,step,q,count}]}]
  // opts: {xLog, xAxis (X_AXES entry), yInfo (METRIC_INFO entry), showTime, title}
  function renderXYChart(container, series, opts) {
    var info = opts.yInfo, xAxis = opts.xAxis, log = !!opts.xLog;
    var showTime = opts.showTime && xAxis.key === "bpp"; // time-as-bubble only on size charts
    var vis = series.filter(function (s) { return !state.implsOff[s.implName]; });
    container._hits = [];

    // The encode-time axis can carry rigorously-timed points (the timed anchor
    // step, isolated on a dedicated core): plot those at their repeated-trial mean
    // and mark them, falling back to the single-pass wall-clock everywhere else.
    var encTime = xAxis.key === "time_s";
    function xOf(p) { return (encTime && p.rig && isNum(p.rig.t)) ? p.rig.t : p.x; }
    var nRig = 0, nTot = 0, minRuns = Infinity;
    if (encTime) {
      vis.forEach(function (s) { s.points.forEach(function (p) {
        nTot++;
        if (p.rig && isNum(p.rig.t)) { nRig++; if (p.rig.runs) minRuns = Math.min(minRuns, p.rig.runs); }
      }); });
    }

    var visPts = [];
    vis.forEach(function (s) { s.points.forEach(function (p) { visPts.push(p); }); });
    var scale = showTime ? timeScale(visPts) : null;

    var xs = [], ys = [];
    vis.forEach(function (s) { s.points.forEach(function (p) { xs.push(xOf(p)); ys.push(p.y); }); });
    var ariaLabel = info.name + " versus " + xAxis.name + (opts.title ? " — " + opts.title : "");
    var plotHTML;
    if (!xs.length) {
      plotHTML = '<div class="q-plot"><svg role="img" aria-label="' + esc(ariaLabel) +
        ' (no data)" viewBox="0 0 ' + VBW + " " + VBH + '"><title>' + esc(ariaLabel) +
        '</title><text x="' + VBW / 2 + '" y="' + VBH / 2 +
        '" text-anchor="middle" class="q-tick">No data for this view</text></svg></div>';
      container.innerHTML = plotHTML;
      return;
    }
    var xmin = Math.min.apply(null, xs), xmax = Math.max.apply(null, xs);
    var ymin = Math.min.apply(null, ys), ymax = Math.max.apply(null, ys);
    var xticks, dxmin, dxmax;
    if (log) {
      if (xmin <= 0) xmin = 1e-6;
      dxmin = xmin * 0.9; dxmax = xmax * 1.1;
      xticks = logTicks(dxmin, dxmax);
    } else {
      xticks = linTicks(xmin, xmax, 6);
      dxmin = Math.min(xmin, xticks[0]); dxmax = Math.max(xmax, xticks[xticks.length - 1]);
    }
    var ylo = (info.lo != null) ? Math.min(info.lo, ymin) : ymin;
    var yhi = (info.hi != null) ? Math.max(info.hi, ymax) : ymax;
    if (info.hardLo != null) ylo = Math.max(ylo, info.hardLo);
    if (info.hardHi != null) yhi = Math.min(yhi, info.hardHi);
    var yticks = linTicks(ylo, yhi, 6);
    var dymin = Math.min(ylo, yticks[0]), dymax = Math.max(yhi, yticks[yticks.length - 1]);
    if (info.hardLo != null) dymin = Math.max(dymin, info.hardLo);
    if (info.hardHi != null) dymax = Math.min(dymax, info.hardHi);
    if (dymax === dymin) dymax = dymin + 1;

    var lx0 = log ? Math.log10(dxmin) : dxmin, lx1 = log ? Math.log10(dxmax) : dxmax;
    function sx(x) {
      var t = ((log ? Math.log10(x) : x) - lx0) / (lx1 - lx0 || 1);
      return X0 + t * (X1 - X0);
    }
    function sy(y) { return Y1 - (y - dymin) / (dymax - dymin) * (Y1 - Y0); }

    var svg = [];
    xticks.forEach(function (tk) {
      if (tk < dxmin - 1e-9 || tk > dxmax + 1e-9) return;
      var x = sx(tk).toFixed(1);
      svg.push('<line class="q-grid" x1="' + x + '" y1="' + Y0 + '" x2="' + x + '" y2="' + Y1 + '"/>');
      svg.push('<text class="q-tick" x="' + x + '" y="' + (Y1 + 18) + '" text-anchor="middle">' + esc(xAxis.fmt(tk)) + "</text>");
    });
    yticks.forEach(function (tk) {
      if (tk < dymin - 1e-9 || tk > dymax + 1e-9) return;
      var y = sy(tk).toFixed(1);
      svg.push('<line class="q-grid" x1="' + X0 + '" y1="' + y + '" x2="' + X1 + '" y2="' + y + '"/>');
      svg.push('<text class="q-tick" x="' + (X0 - 8) + '" y="' + (+y + 4) + '" text-anchor="end">' + esc(fmtNum(tk)) + "</text>");
    });
    svg.push('<line class="q-axis" x1="' + X0 + '" y1="' + Y1 + '" x2="' + X1 + '" y2="' + Y1 + '"/>');
    svg.push('<line class="q-axis" x1="' + X0 + '" y1="' + Y0 + '" x2="' + X0 + '" y2="' + Y1 + '"/>');
    var timeAxis = xAxis.key === "time_s" || xAxis.key === "decode_time_s";
    // A '*' only where some plotted time is single-pass: the decode-time axis is
    // always single-pass; the encode-time axis drops it once every point is rigorous.
    var timeMarked = timeAxis && (!encTime || nRig < nTot);
    svg.push('<text class="q-axis-title" x="' + ((X0 + X1) / 2) + '" y="' + (VBH - 8) + '" text-anchor="middle">' + esc(xAxis.title) + (log ? " — log" : "") + (timeMarked ? " *" : "") + "</text>");
    svg.push('<text class="q-axis-title" transform="translate(16,' + ((Y0 + Y1) / 2) + ') rotate(-90)" text-anchor="middle">' + esc(info.y) + "</text>");

    // A horizontal ±σ whisker marks each rigorously-timed encode-time point.
    function whisker(p, color) {
      if (!(encTime && p.rig && p.rig.sd > 0)) return;
      var rx = p.rig.t;
      var wx0 = sx(Math.max(dxmin, rx - p.rig.sd)).toFixed(1);
      var wx1 = sx(Math.min(dxmax, rx + p.rig.sd)).toFixed(1);
      var wy = sy(p.y).toFixed(1);
      svg.push('<line class="q-whisker" x1="' + wx0 + '" y1="' + wy + '" x2="' + wx1 + '" y2="' + wy +
        '" stroke="' + color + '" stroke-width="1.5" stroke-linecap="round"/>');
    }
    var hits = [];
    vis.forEach(function (s) {
      var pts = [];
      s.points.forEach(function (p) {
        var px = sx(xOf(p)), py = sy(p.y);
        pts.push({ x: px, y: py });
        var r = scale ? scale.r(p.t) : PT_R;
        hits.push({ sx: px, sy: py, r: r, color: s.color, label: s.label, impl: s.implName, format: s.fmt, x: xOf(p), y: p.y, q: p.q, step: p.step, count: p.count, std: p.std, t: p.t, rig: encTime ? (p.rig || null) : null });
      });
      svg.push('<path class="q-line" d="' + smoothPath(pts) + '" stroke="' + s.color + '"/>');
      s.points.forEach(function (p) {
        var r = scale ? scale.r(p.t) : PT_R;
        whisker(p, s.color);
        svg.push('<circle class="q-pt" cx="' + sx(xOf(p)).toFixed(1) + '" cy="' + sy(p.y).toFixed(1) + '" r="' + r.toFixed(1) + '" fill="' + s.color + '"' + (encTime && p.rig ? ' stroke="#1a1a1a" stroke-width="1.2"' : "") + "/>");
      });
    });
    svg.push('<circle class="q-hl" r="7.5" visibility="hidden"/>');

    plotHTML = '<div class="q-plot"><svg role="img" aria-label="' + esc(ariaLabel) +
      '" viewBox="0 0 ' + VBW + " " + VBH + '" preserveAspectRatio="xMidYMid meet"><title>' +
      esc(ariaLabel) + "</title>" + svg.join("") + "</svg></div>";
    container._hits = hits;

    // Footnote mirrors the lossless chart: blanket caveat for single-pass axes,
    // adaptive text once the encode-time axis has rigorously-anchored points.
    var runsTxt = minRuns === Infinity ? "" : minRuns + " ";
    var timeNote = "";
    if (timeAxis) {
      if (!encTime || nRig === 0) timeNote = timingNoteHTML();
      else if (nRig === nTot) timeNote = timingNoteHTML("Encode times are isolated, repeated-trial measurements (" + runsTxt + "runs each, pinned to one core).");
      else timeNote = timingNoteHTML("* Ringed/whiskered points are rigorously timed (" + runsTxt + "runs, ±σ, isolated on a dedicated core); the rest are single-pass wall-clocks.");
    }
    container.innerHTML = plotHTML +
      (scale ? sizeLegendHTML(scale, "encode time") : "") +
      timeNote +
      '<div class="q-tooltip" hidden></div>';

    // hover / click / keyboard — all share one nearest-point selection
    var svgEl = container.querySelector("svg");
    var tip = container.querySelector(".q-tooltip");
    var hl = container.querySelector(".q-hl");
    var clickable = HAS_ASSETS;   // points open their image group when captured

    function nearest(vx, vy) {
      var best = null, bd = 1e9;
      container._hits.forEach(function (h) {
        var dd = (h.sx - vx) * (h.sx - vx) + (h.sy - vy) * (h.sy - vy);
        if (dd < bd) { bd = dd; best = h; }
      });
      return (best && bd <= 26 * 26) ? best : null;
    }
    function showHit(best, clientX, clientY) {
      hl.setAttribute("cx", best.sx); hl.setAttribute("cy", best.sy);
      hl.setAttribute("r", Math.max(7.5, (best.r || PT_R) + 3).toFixed(1));
      hl.setAttribute("stroke", best.color); hl.setAttribute("visibility", "visible");
      var crect = container.getBoundingClientRect();
      tip.hidden = false;
      var agg = best.count > 1;
      // On the encode-time axis the x value IS the time, so fold the rigor marker
      // into it ("(N runs ±σ)" vs "(single-pass)") and drop the redundant line.
      var xLine;
      if (encTime) {
        xLine = '<span class="k">encode time</span> ' + xAxis.fmt(best.x) +
          (best.rig
            ? " (" + best.rig.runs + " runs ±" + fmtTime(best.rig.sd || 0) +
              (best.rig.n < best.count ? ", rigorous on " + best.rig.n + "/" + best.count + " imgs" : "") + ")"
            : " (single-pass)");
      } else {
        xLine = '<span class="k">' + esc(xAxis.name) + (agg ? " (mean)" : "") + "</span> " + xAxis.fmt(best.x);
      }
      tip.innerHTML = "<b>" + esc(best.label) + "</b><br>" +
        '<span class="k">step</span> ' + esc(best.step) + "<br>" +
        xLine + "<br>" +
        '<span class="k">' + esc(info.name) + (agg ? " (mean)" : "") + "</span> " + best.y.toFixed(2) +
        (agg && best.std > 0 ? " ± " + best.std.toFixed(2) : "") +
        (!encTime && isNum(best.t) && best.t > 0 ? '<br><span class="k">encode time</span> ' + fmtTime(best.t) : "") +
        '<br><span class="k">images</span> ' + (agg ? best.count : "1 (single)") +
        (clickable && best.format ? '<br><span class="q-open-hint">click to view images</span>' : "");
      var tx = clientX - crect.left + 14, ty = clientY - crect.top + 12;
      if (tx + 220 > crect.width) tx = clientX - crect.left - 14 - 220;
      tip.style.left = Math.max(0, tx) + "px";
      tip.style.top = ty + "px";
    }
    function clearHit() { hl.setAttribute("visibility", "hidden"); tip.hidden = true; }
    function openHit(h) { if (h && h.format && clickable) openLightbox(h.format, h.impl, h.step); }

    if (svgEl && container._hits.length) {
      svgEl.addEventListener("mousemove", function (ev) {
        var r = svgEl.getBoundingClientRect();
        var best = nearest((ev.clientX - r.left) * (VBW / r.width), (ev.clientY - r.top) * (VBH / r.height));
        if (best) showHit(best, ev.clientX, ev.clientY); else clearHit();
      });
      svgEl.addEventListener("mouseleave", clearHit);

      if (clickable) {
        // The plot is a keyboard-navigable widget: Tab focuses it, arrow keys
        // step a cursor between points (highlighting + tooltip), Enter/Space
        // opens the focused point's image group; the mouse path is the click.
        svgEl.style.cursor = "pointer";
        svgEl.setAttribute("tabindex", "0");
        svgEl.setAttribute("aria-label", ariaLabel +
          " — interactive: arrow keys move between data points, Enter opens that point's images.");
        var cur = -1;
        function focusHit(i) {
          var n = container._hits.length; if (!n) return;
          cur = (i % n + n) % n;
          var h = container._hits[cur];
          var r = svgEl.getBoundingClientRect();
          showHit(h, r.left + h.sx * (r.width / VBW), r.top + h.sy * (r.height / VBH));
        }
        svgEl.addEventListener("click", function (ev) {
          var r = svgEl.getBoundingClientRect();
          openHit(nearest((ev.clientX - r.left) * (VBW / r.width), (ev.clientY - r.top) * (VBH / r.height)));
        });
        svgEl.addEventListener("keydown", function (ev) {
          if (ev.key === "ArrowRight" || ev.key === "ArrowDown") { ev.preventDefault(); focusHit(cur + 1); }
          else if (ev.key === "ArrowLeft" || ev.key === "ArrowUp") { ev.preventDefault(); focusHit(cur - 1); }
          else if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); openHit(container._hits[cur]); }
        });
        svgEl.addEventListener("blur", clearHit);
      }
    }
  }

  // ---- series builders -----------------------------------------------------

  // Cross-format rate-distortion: every implementation of every shown format,
  // mapped to the (x,y) of the active view, coloured by a per-implementation tone
  // within its format's hue family (solid lines). `fmt` is carried on each series so a clicked point
  // can resolve its image group. The Tests filter (state.implsOff) is the
  // authoritative series selector — renderXYChart drops the hidden ones.
  function seriesForView(AGG, xKey, yKey) {
    var series = [];
    Object.keys(AGG).sort().forEach(function (fmt) {
      if (state.formatsOff[fmt]) return;   // format filtered out in the bar
      AGG[fmt].forEach(function (s) {
        var points = s.points.map(function (p) {
          var x = p.m[xKey], y = p.m[yKey];
          if (!isNum(x) || !isNum(y)) return null;
          return { x: x, y: y, std: p.sd[yKey] || 0, t: p.m.time_s, step: p.label, q: p.q, count: p.count, rig: p.rig || null };
        }).filter(Boolean).sort(function (a, b) { return a.x - b.x; });
        if (points.length) {
          series.push({
            key: fmt + "/" + s.impl, implName: s.impl, fmt: fmt,
            label: fmt.toUpperCase() + " · " + s.impl,
            color: implTone(fmt, s.impl),
            points: points,
          });
        }
      });
    });
    return series;
  }

  // ---- rate-distortion view ------------------------------------------------

  var AGG_ALL = null;   // aggregateAll(validRows), lazy

  // X axes offered by the controls box: bpp always, time axes only when measured.
  function availableXAxes() {
    var keys = ["bpp"];
    if (hasAxisData("time_s")) keys.push("time_s");
    if (hasAxisData("decode_time_s")) keys.push("decode_time_s");
    return keys;
  }
  // Short label for an X-axis chooser button.
  function axisLabel(k) {
    return k === "bpp" ? "Size" : k === "time_s" ? "Encode time"
      : k === "decode_time_s" ? "Decode time" : X_AXES[k].name;
  }

  // Series visibility is driven entirely by the Tests filter (state.implsOff):
  // its checkboxes carry the format colour swatch and double as the legend, so
  // there are no separate per-chart legend chips. Every renderer reads implsOff
  // to drop hidden series, and toggling a Test re-renders all charts + tables.

  // Rate-distortion: the active metric (chosen on the nav rail) as one full-width
  // chart overlaying every shown format's encoders on the chosen X axis.
  function renderRD() {
    var host = document.getElementById("q-rd");
    if (!host) return;
    host.innerHTML = "";
    var avail = availableMetrics();
    var m = avail.indexOf(state.metric) >= 0 ? state.metric : avail[0];
    if (!m) { host.appendChild(el("p", { class: "q-note" }, "No metric data.")); return; }
    host.appendChild(el("div", { class: "q-metric-cap" },
      METRIC_INFO[m].name + " vs " + X_AXES[state.xKey].name));
    var chart = el("div", { class: "q-chart" });
    host.appendChild(chart);
    renderXYChart(chart, seriesForView(AGG_ALL, state.xKey, m), {
      xLog: state.xLog, xAxis: X_AXES[state.xKey], yInfo: METRIC_INFO[m],
      showTime: state.showTime.rd,
    });
  }

  // ---- centralized floating filter bar -------------------------------------

  // The variants the ACTIVE figure plots — the single source the top-bar lists from,
  // so it shows strictly what the current graph contains (not a global union across
  // every view). Derived from the very datasets each renderer plots: AGG_ALL for the
  // rate-distortion / BD-rate views, LOSSLESS for the lossless views, DECODERS for the
  // decoder views, and the active section's format tabs for a static gallery. The full
  // (unfiltered) membership is returned — a variant the user toggled off still belongs
  // to the figure and stays listed (unchecked) so it can be re-enabled.
  // Returns { formats:[fmt...], byFormat:{fmt:[impl...]}, hasTests:bool }.
  function figureVariants() {
    var kind = state.graphKind;
    var byFmt = {}, fmtsOnly = {};
    function addImpl(f, n) { if (!f || !n) return; (byFmt[f] = byFmt[f] || {})[n] = 1; }
    function addFmt(f) { if (f && f !== "other") fmtsOnly[f] = 1; }
    if (kind === "rd" || kind === "bdrate") {
      if (AGG_ALL) Object.keys(AGG_ALL).forEach(function (f) {
        AGG_ALL[f].forEach(function (s) { addImpl(f, s.impl); });
      });
    } else if (kind === "lossless-bars" || kind === "lossless-effort") {
      Object.keys(LOSSLESS).forEach(function (n) { addImpl(LOSSLESS[n].format, n); });
    } else if (kind === "decoder-table" || kind === "decoder-chart") {
      Object.keys(DECODERS).forEach(function (n) { addImpl(DECODERS[n].format, n); });
    } else if (kind === "gallery") {
      // Static galleries carry no per-impl tests; their formats are the active
      // section's tabs/figures (so the bar offers Formats-only filtering).
      var sec = state.cat ? panelEl(state.cat) : null;
      if (sec) [].forEach.call(sec.querySelectorAll("[data-format]"), function (n) {
        addFmt((n.getAttribute("data-format") || "").toLowerCase());
      });
    }
    var byList = {}, hasTests = false;
    Object.keys(byFmt).forEach(function (f) {
      byList[f] = Object.keys(byFmt[f]).sort(); hasTests = true; addFmt(f);
    });
    return { formats: Object.keys(fmtsOnly).sort(), byFormat: byList, hasTests: hasTests };
  }

  // A filter changed: prune the galleries, then rebuild the rail (categories /
  // graphs may have dropped) and re-render the active graph in the stage. Hidden
  // graphs re-render lazily when next shown, so they always reflect the filters.
  function rerenderAll() {
    applyGalleryFilter();
    if (state.cat) showGraph(state.cat, state.graph);
    else renderNav();
  }

  // The Tests groups depend on which formats are shown; rerenderAll re-renders the
  // active graph, whose showGraph() rebuilds the bar (once) — no separate call.
  function onFormatChange() { rerenderAll(); }

  // Show/hide static gallery charts (pre-rendered PNGs can't be re-plotted, only
  // shown/hidden) by their data-format within each suite section.
  function applyGalleryFilter() {
    [].forEach.call(document.querySelectorAll("[data-img-tabs]"), function (box) {
      function hiddenF(n) {
        var f = (n.getAttribute("data-format") || "").toLowerCase();
        return f && f !== "other" && state.formatsOff[f];
      }
      var tabs = [].slice.call(box.querySelectorAll('[role="tab"][data-format]'));
      var panels = [].slice.call(box.querySelectorAll('[role="tabpanel"][data-format]'));
      var firstVis = -1;
      tabs.forEach(function (t, i) {
        var hide = hiddenF(t);
        t.hidden = hide;
        if (panels[i] && hide) panels[i].hidden = true;
        if (!hide && firstVis < 0) firstVis = i;
      });
      // If the active tab was hidden, activate the first visible one (its own
      // gallery handler shows the panel).
      if (tabs.length) {
        var activeVisible = tabs.some(function (t) {
          return t.getAttribute("aria-selected") === "true" && !t.hidden;
        });
        if (!activeVisible && firstVis >= 0) tabs[firstVis].click();
      }
      // Flat (single-group) galleries carry data-format on the figures directly.
      var figs = [].slice.call(box.querySelectorAll("figure[data-format]"));
      figs.forEach(function (fig) { fig.hidden = hiddenF(fig); });
      // Section show/hide is owned by the dashboard (one panel at a time); here we
      // only prune the per-format tabs/figures within it. buildCategories consults
      // sectionHasVisible() to drop a category whose formats are all filtered out.
    });
  }

  // Whether a static suite section still has any visible per-format tab/figure
  // under the current format filter (used to prune empty dashboard categories).
  function sectionHasVisible(section) {
    if (!section) return false;
    var tabs = [].slice.call(section.querySelectorAll('[role="tab"][data-format]'));
    if (tabs.length) return tabs.some(function (t) { return !t.hidden; });
    var figs = [].slice.call(section.querySelectorAll("figure[data-format]"));
    if (figs.length) return figs.some(function (f) { return !f.hidden; });
    return true;   // no per-format grouping → always present
  }


  // Build the bar for the ACTIVE figure: Formats + its per-format Tests. The lists
  // come from figureVariants(), so the bar shows strictly the variants the current
  // graph plots. Toggling stays a global hide (state.formatsOff / state.implsOff reach
  // every figure where a variant appears) — only the listing is per-figure. Rebuilt on
  // every figure switch (showGraph) and on any change that reshapes it.
  function renderFilterBar() {
    var bar = document.getElementById("q-filterbar");
    if (!bar) return;
    var fv = figureVariants();
    var fmts = fv.formats;
    if (!fmts.length) { bar.hidden = true; bar.innerHTML = ""; return; }
    bar.hidden = false;

    // Rebuilding the bar on every toggle would otherwise reset the body's scroll
    // offset and drop keyboard focus (both accessibility regressions). Snapshot
    // the scroll position (both axes) and the focused control's stable key, then
    // restore them after the fresh DOM is in place.
    var prevBody = document.getElementById("q-fb-body");
    var prevScroll = prevBody ? { l: prevBody.scrollLeft, t: prevBody.scrollTop } : null;
    var act = document.activeElement;
    var prevFocusKey = (act && bar.contains(act)) ? act.getAttribute("data-fbkey") : null;
    bar.innerHTML = "";

    var head = el("div", { class: "q-fb-row" });
    var toggle = el("button", {
      type: "button", class: "q-fb-toggle", "aria-controls": "q-fb-body",
      "aria-expanded": state.barCollapsed ? "false" : "true", "data-fbkey": "toggle",
    }, (state.barCollapsed ? "▸" : "▾") + " Filters");
    toggle.addEventListener("click", function () {
      state.barCollapsed = !state.barCollapsed;
      renderFilterBar();
    });
    head.appendChild(toggle);
    bar.appendChild(head);

    var body = el("div", { class: "q-fb-body", id: "q-fb-body" });
    if (state.barCollapsed) body.hidden = true;
    bar.appendChild(body);

    // A one-line pill group: a label span followed by inline checkboxes.
    function group(labelText) {
      var g = el("div", { class: "q-fb-group", role: "group", "aria-label": labelText });
      g.appendChild(el("span", { class: "q-fb-lab" }, labelText));
      body.appendChild(g);
      return g;
    }
    function allNone(g, names, off, after, groupId) {
      var lg = g.querySelector(".q-fb-lab");
      var a = el("button", { type: "button", class: "q-mini", "data-fbkey": "all:" + groupId }, "all");
      var n = el("button", { type: "button", class: "q-mini", "data-fbkey": "none:" + groupId }, "none");
      a.addEventListener("click", function () { names.forEach(function (k) { delete off[k]; }); after(); });
      n.addEventListener("click", function () { names.forEach(function (k) { off[k] = true; }); after(); });
      lg.appendChild(document.createTextNode(" ")); lg.appendChild(a); lg.appendChild(n);
    }
    // A filter checkbox; an optional colour swatch makes the Tests groups double
    // as the chart legend (the charts colour every series by its per-impl tone).
    // `key` is the stable id used to restore focus across a rebuild.
    function check(ff, label, checked, onToggle, swColor, key) {
      var lab = el("label", { class: "q-check" });
      var cb = el("input", { type: "checkbox", "data-fbkey": key });
      cb.checked = checked;
      cb.addEventListener("change", function () { onToggle(cb.checked); });
      lab.appendChild(cb);
      if (swColor) lab.appendChild(el("span", { class: "q-fb-sw", style: "background:" + swColor }));
      lab.appendChild(document.createTextNode(" " + label));
      ff.appendChild(lab);
    }

    // Formats present on this figure — toggling is a global hide (also governs the
    // static galleries), so the whole bar follows from the active figure. The
    // Formats swatch stays the family (format) colour; the Tests swatches below
    // carry each implementation's own tone.
    var ff = group("Formats");
    allNone(ff, fmts, state.formatsOff, onFormatChange, "fmt");
    fmts.forEach(function (f) {
      check(ff, f.toUpperCase(), !state.formatsOff[f], function (on) {
        if (on) delete state.formatsOff[f]; else state.formatsOff[f] = true;
        onFormatChange();
      }, formatColor(f), "fmt:" + f);
    });

    // Tests (implementations) the active figure plots, per shown format — they drive
    // the interactive charts and double as their legend (swatch = the series tone).
    var ibf = fv.byFormat;
    Object.keys(ibf).sort().forEach(function (fmt) {
      if (state.formatsOff[fmt]) return;   // hidden-format groups self-prune
      var impls = ibf[fmt];
      if (!impls.length) return;
      var tf = group(fmt.toUpperCase() + " tests");
      allNone(tf, impls, state.implsOff, rerenderAll, "impl:" + fmt);
      impls.forEach(function (n) {
        check(tf, n, !state.implsOff[n], function (on) {
          if (on) delete state.implsOff[n]; else state.implsOff[n] = true;
          rerenderAll();
        }, implTone(fmt, n), "impl:" + n);
      });
    });
    // Metric is a Quality nav step (single-select on the rail); X axis + scale
    // live in the controls box. The filter bar is just Formats + Tests.

    // Restore the pre-rebuild scroll offset and keyboard focus (see snapshot above).
    // focus() uses preventScroll so refocusing an off-screen control can't undo the
    // scroll we just restored.
    if (prevScroll) { body.scrollLeft = prevScroll.l; body.scrollTop = prevScroll.t; }
    if (prevFocusKey) {
      var refocus = bar.querySelector('[data-fbkey="' + prevFocusKey + '"]');
      if (refocus) { try { refocus.focus({ preventScroll: true }); } catch (e) { refocus.focus(); } }
    }
  }

  // ---- controls box (axis + scale + show-time + download) ------------------

  // The controls box adapts to the active graph: the X-axis + scale choosers and
  // the encode-time-bubble toggle only apply to charts that carry those axes.
  function renderControls() {
    var host = document.getElementById("q-controls");
    if (!host) return;
    host.innerHTML = "";
    var kind = state.graphKind;

    if (kind === "rd") {
      // X-axis chooser — quality vs size / encode time / decode time (if measured).
      var axes = availableXAxes();
      if (axes.length > 1) {
        host.appendChild(el("span", { class: "q-label" }, "Quality vs"));
        var axGroup = el("div", { class: "q-group", role: "group", "aria-label": "X axis" });
        axes.forEach(function (k) {
          var onA = state.xKey === k;
          var b = el("button", { type: "button", class: onA ? "active" : "", "aria-pressed": onA ? "true" : "false" }, axisLabel(k));
          b.addEventListener("click", function () { setAxis(k); });
          axGroup.appendChild(b);
        });
        host.appendChild(axGroup);
      }
      // X-scale toggle — logarithmic vs linear (independent of the axis choice).
      host.appendChild(el("span", { class: "q-label" }, "Scale"));
      var scGroup = el("div", { class: "q-group", role: "group", "aria-label": "X scale" });
      [["Logarithmic", true], ["Linear", false]].forEach(function (opt) {
        var onS = state.xLog === opt[1];
        var b = el("button", { type: "button", class: onS ? "active" : "", "aria-pressed": onS ? "true" : "false" }, opt[0]);
        b.addEventListener("click", function () { setScale(opt[1]); });
        scGroup.appendChild(b);
      });
      host.appendChild(scGroup);
    }

    // Encode-time bubbles — meaningful on the size RD chart (bubble size encodes
    // mean encode time). The lossless size-vs-effort chart puts time on its x-axis
    // instead, so it has no bubble toggle.
    var timeKey = kind === "rd" ? "rd" : null;
    if (timeKey) {
      var tWrap = el("span", { class: "q-ctl" });
      var tcb = el("input", { type: "checkbox", id: "q-showtime" });
      tcb.checked = state.showTime[timeKey];
      tcb.addEventListener("change", function () {
        state.showTime[timeKey] = tcb.checked;
        if (timeKey === "rd") renderRD(); else renderLossless();
      });
      var tlab = el("label", { class: "q-label", for: "q-showtime" });
      tlab.appendChild(tcb);
      tlab.appendChild(document.createTextNode(" Encode-time bubbles"));
      tWrap.appendChild(tlab);
      host.appendChild(tWrap);
    }

    // Download embedded raw metrics (available whenever quality data is present).
    if (METRICS.length) {
      var dl = el("a", { class: "q-dl", href: "#" }, "⤓ raw metrics (JSON)");
      dl.addEventListener("click", function (e) {
        e.preventDefault();
        var blob = new Blob([JSON.stringify(METRICS)], { type: "application/json" });
        var url = URL.createObjectURL(blob);
        var a = el("a", { href: url, download: "metrics.json" });
        a.click();
        URL.revokeObjectURL(url);
      });
      host.appendChild(dl);
    }
  }

  function setAxis(k) {
    if (!X_AXES[k] || availableXAxes().indexOf(k) < 0) return;
    state.xKey = k;
    renderControls();
    renderRD();
    announce("X axis: quality vs " + X_AXES[k].name);
  }

  function setScale(log) {
    state.xLog = !!log;
    renderControls();
    renderRD();
    announce(log ? "Logarithmic X axis" : "Linear X axis");
  }

  // ---- aggregation disclosure ----------------------------------------------

  function imageCount() {
    var seen = {};
    METRICS.forEach(function (m) {
      if (!m || m.error) return;
      var p = m.source_path || m.input_path;
      if (p) seen[p] = 1;
    });
    return Object.keys(seen).length;
  }

  function renderAggregationNote() {
    var host = document.getElementById("q-aggregation");
    if (!host) return;
    var n = imageCount();
    var cfg = (readJSON("quality-manifest") || {}).benchmark_config || {};
    var ds = cfg.dataset ? "the <b>" + esc(String(cfg.dataset)) + "</b> dataset" : "the dataset";
    if (n <= 1) {
      host.innerHTML = "<b>Single image.</b> Every plotted point is that one " +
        "image's measured value at one operating point — not an average. Run " +
        "over a multi-image dataset to summarise across images.";
    } else {
      host.innerHTML = "Every plotted point is the <b>mean (arithmetic average)</b> " +
        "of the metric across the <b>" + n + " images</b> in " + ds + ", at one " +
        "operating point (the X value is likewise the per-image mean). Hover a point " +
        "for its image count and spread (±1σ across images); BD-rate below is computed " +
        "per image, then averaged.";
    }
  }

  // ---- BD-rate table (sortable) -------------------------------------------

  function renderBdRate() {
    var host = document.getElementById("q-bdrate");
    if (!host) return;
    var rows = [];
    Object.keys(BDRATE).forEach(function (fmt) {
      if (state.formatsOff[fmt]) return;
      Object.keys(BDRATE[fmt]).forEach(function (impl) {
        if (state.implsOff[impl]) return;
        rows.push({ fmt: fmt, impl: impl, bd: BDRATE[fmt][impl] });
      });
    });
    if (!rows.length) { host.innerHTML = ""; return; }
    var sortKey = "fmt", sortDir = 1;
    function draw() {
      rows.sort(function (a, b) {
        var av, bv;
        if (sortKey === "bd") {
          av = a.bd == null ? Infinity : a.bd; bv = b.bd == null ? Infinity : b.bd;
        } else { av = a[sortKey]; bv = b[sortKey]; }
        if (av < bv) return -1 * sortDir;
        if (av > bv) return 1 * sortDir;
        return 0;
      });
      function arrow(k) { return sortKey === k ? '<span class="arrow">' + (sortDir > 0 ? "▲" : "▼") + "</span>" : ""; }
      var html = '<table class="q-table"><thead><tr>' +
        '<th data-k="fmt" scope="col">Format ' + arrow("fmt") + "</th>" +
        '<th data-k="impl" scope="col">Implementation ' + arrow("impl") + "</th>" +
        '<th data-k="bd" scope="col">BD-rate vs ref ' + arrow("bd") + "</th></tr></thead><tbody>";
      rows.forEach(function (r) {
        var cls = r.bd == null ? "" : r.bd < 0 ? "good" : "bad";
        var txt = r.bd == null ? "N/A" : (r.bd > 0 ? "+" : "") + r.bd.toFixed(1) + "%";
        html += "<tr><td>" + esc(r.fmt.toUpperCase()) + "</td><td>" + esc(r.impl) +
          '</td><td class="num ' + cls + '">' + txt + "</td></tr>";
      });
      html += "</tbody></table>";
      host.innerHTML = html;
      host.querySelectorAll("th").forEach(function (th) {
        th.addEventListener("click", function () {
          var k = th.getAttribute("data-k");
          if (k === sortKey) sortDir = -sortDir; else { sortKey = k; sortDir = 1; }
          draw();
        });
      });
    }
    draw();
  }

  // ---- lossless compression efficiency ------------------------------------

  function renderLossless() {
    var host = document.getElementById("q-lossless");
    if (!host) return;
    if (!Object.keys(LOSSLESS).length) {
      host.innerHTML = '<p class="q-note">No lossless encoders measured.</p>';
      return;
    }
    // The format filter applies to both views; the bars additionally drop
    // impl-hidden encoders, while the effort chart keeps them in its interactive
    // legend (so they can be toggled back in place).
    var impls = Object.keys(LOSSLESS).filter(function (n) { return !state.formatsOff[LOSSLESS[n].format]; });
    if (!impls.length) {
      host.innerHTML = '<p class="q-note">No lossless encoders for the selected formats.</p>';
      return;
    }
    host.innerHTML = "";
    if (state.losslessView === "effort") {
      host.appendChild(el("div", { class: "q-metric-cap" }, "Size vs compression effort"));
      var eff = el("div", { id: "q-lossless-effort", class: "q-chart" });
      host.appendChild(eff);
      renderLosslessEffort(eff, impls);
    } else {
      host.appendChild(el("div", { class: "q-metric-cap" }, "Best bits per pixel — lower is better"));
      var bars = el("div", { id: "q-lossless-bars" });
      host.appendChild(bars);
      renderLosslessBars(bars, impls.filter(function (n) { return !state.implsOff[n]; }));
    }
  }

  function renderLosslessBars(host, impls) {
    if (!impls.length) { host.innerHTML = '<p class="q-note">All lossless encoders hidden.</p>'; return; }
    var rows = impls.map(function (impl) {
      var d = LOSSLESS[impl];
      return { impl: impl, fmt: d.format, bpp: d.best_bpp, ratio: d.ratio };
    }).sort(function (a, b) { return a.bpp - b.bpp; });
    var maxBpp = Math.max.apply(null, rows.map(function (r) { return r.bpp; }));
    var rowH = 26, padT = 8, padB = 8, valW = 150;
    var longest = rows.reduce(function (m, r) { return Math.max(m, r.impl.length); }, 0);
    var labelW = Math.min(320, Math.max(160, longest * 7 + 16));
    var W = Math.max(VBW, labelW + 380 + valW), H = padT + padB + rows.length * rowH;
    var x0 = labelW, x1 = W - valW;
    var svg = [];
    rows.forEach(function (r, i) {
      var cy = padT + i * rowH + rowH / 2;
      var bw = maxBpp > 0 ? (r.bpp / maxBpp) * (x1 - x0) : 0;
      var color = implTone(r.fmt, r.impl);
      var ratio = r.ratio ? " · " + r.ratio.toFixed(2) + "×" : "";
      svg.push('<text class="q-ll-name" x="' + (labelW - 8) + '" y="' + (cy + 4) +
        '" text-anchor="end">' + esc(r.impl) + "</text>");
      svg.push('<rect class="q-ll-bar" x="' + x0 + '" y="' + (cy - rowH / 2 + 3) +
        '" width="' + bw.toFixed(1) + '" height="' + (rowH - 6) + '" fill="' + color + '"/>');
      svg.push('<text class="q-ll-val" x="' + (x1 + 6) + '" y="' + (cy + 4) + '">' +
        r.bpp.toFixed(3) + " bpp" + ratio + "</text>");
    });
    host.innerHTML = '<div class="q-plot"><svg role="img" aria-label="Best bits per pixel per lossless encoder" viewBox="0 0 ' + W + " " + H +
      '" preserveAspectRatio="xMidYMid meet">' + svg.join("") + "</svg></div>";
  }

  function renderLosslessEffort(host, impls) {
    var series = impls.map(function (impl) {
      var d = LOSSLESS[impl];
      var n = d.points.length;
      // A single-knob encoder has no effort axis — one operating point, not a sweep.
      // Trust the raw-data metadata (empty `axis`); fall back to the point count only
      // for a legacy blob that predates the field. These render as a distinct labeled
      // marker instead of a lone dot lost among the curve endpoints.
      var single = d.axis != null ? d.axis === "" : n < 2;
      // X is the time each effort cost, so the curve is honestly scaled by cost
      // (issue #26): rigorously-timed endpoints (and any --perf all points) use the
      // isolated mean; interior points fall back to the single-pass wall-clock.
      var points = d.points.map(function (p) {
        var rig = isNum(p.time_rigorous_s);
        var x = rig ? p.time_rigorous_s : isNum(p.time_s) ? p.time_s : 0;
        return {
          x: x, y: p.bpp, setting: p.value || p.label, t: x,
          rig: rig, sd: rig ? p.time_stddev_s || 0 : 0, runs: rig ? p.runs || 0 : 0,
        };
      });
      // Draw the spline in time order so adjacent points connect left→right; the
      // effort progression still reads along the curve and lives in the tooltip.
      points.sort(function (a, b) { return a.x - b.x; });
      return {
        impl: impl, color: implTone(d.format, impl),
        points: points, single: single,
      };
    });
    // Series visibility follows the Tests filter (state.implsOff); the swatched
    // checkboxes there are the legend, so no per-chart legend is drawn here.
    var vis = series.filter(function (s) { return !state.implsOff[s.impl]; });
    if (!vis.length) {
      host.innerHTML = '<div class="q-plot"><svg role="img" aria-label="Bits per pixel versus encode time (nothing shown)" viewBox="0 0 ' +
        VBW + " " + VBH + '"><text x="' + VBW / 2 + '" y="' + VBH / 2 +
        '" text-anchor="middle" class="q-tick">No series selected</text></svg></div>';
      return;
    }
    var allPts = [];
    vis.forEach(function (s) { s.points.forEach(function (p) { allPts.push(p); }); });
    var xs = allPts.map(function (p) { return p.x; });
    var xmin = Math.min.apply(null, xs), xmax = Math.max.apply(null, xs);
    var xticks = linTicks(xmin, xmax, 6);
    var dxmin = Math.min(xmin, xticks[0]), dxmax = Math.max(xmax, xticks[xticks.length - 1]);
    if (dxmax === dxmin) dxmax = dxmin + 1;
    var ys = allPts.map(function (p) { return p.y; });
    var ymin = Math.min.apply(null, ys), ymax = Math.max.apply(null, ys);
    var yticks = linTicks(ymin, ymax, 6);
    var dymin = Math.min(ymin, yticks[0]), dymax = Math.max(ymax, yticks[yticks.length - 1]);
    if (dymax === dymin) dymax = dymin + 1;
    // Significance of the visible points drives the axis mark + footnote.
    var nRig = 0, nTot = 0, minRuns = Infinity;
    allPts.forEach(function (p) {
      nTot++;
      if (p.rig) { nRig++; if (p.runs) minRuns = Math.min(minRuns, p.runs); }
    });
    var marked = nRig < nTot;   // a '*' only when some plotted time is single-pass
    function sx(x) { return X0 + (x - dxmin) / (dxmax - dxmin) * (X1 - X0); }
    function sy(y) { return Y1 - (y - dymin) / (dymax - dymin) * (Y1 - Y0); }

    var svg = [], hits = [];
    yticks.forEach(function (t) {
      if (t < dymin - 1e-9 || t > dymax + 1e-9) return;
      var y = sy(t).toFixed(1);
      svg.push('<line class="q-grid" x1="' + X0 + '" y1="' + y + '" x2="' + X1 + '" y2="' + y + '"/>');
      svg.push('<text class="q-tick" x="' + (X0 - 8) + '" y="' + (+y + 4) + '" text-anchor="end">' + esc(fmtNum(t)) + "</text>");
    });
    xticks.forEach(function (tk) {
      if (tk < dxmin - 1e-9 || tk > dxmax + 1e-9) return;
      var x = sx(tk).toFixed(1);
      svg.push('<line class="q-grid" x1="' + x + '" y1="' + Y0 + '" x2="' + x + '" y2="' + Y1 + '"/>');
      svg.push('<text class="q-tick" x="' + x + '" y="' + (Y1 + 18) + '" text-anchor="middle">' + esc(fmtTime(tk)) + "</text>");
    });
    svg.push('<line class="q-axis" x1="' + X0 + '" y1="' + Y1 + '" x2="' + X1 + '" y2="' + Y1 + '"/>');
    svg.push('<line class="q-axis" x1="' + X0 + '" y1="' + Y0 + '" x2="' + X0 + '" y2="' + Y1 + '"/>');
    svg.push('<text class="q-axis-title" x="' + ((X0 + X1) / 2) + '" y="' + (VBH - 8) +
      '" text-anchor="middle">Encode time (s)' + (marked ? " *" : "") + "</text>");
    svg.push('<text class="q-axis-title" transform="translate(16,' + ((Y0 + Y1) / 2) +
      ') rotate(-90)" text-anchor="middle">Bits per pixel (lower is better)</text>');
    // Swept encoders draw a curve + filled points now; single-knob encoders (no effort
    // axis) are collected and drawn afterwards as labelled diamonds with de-collided
    // labels, so they don't pile up on the high-effort endpoints.
    // A horizontal ±σ whisker marks each rigorously-timed (anchored) point.
    function whisker(p, color) {
      if (!(p.rig && p.sd > 0)) return;
      var wx0 = sx(Math.max(dxmin, p.x - p.sd)).toFixed(1);
      var wx1 = sx(Math.min(dxmax, p.x + p.sd)).toFixed(1);
      var wy = sy(p.y).toFixed(1);
      svg.push('<line class="q-whisker" x1="' + wx0 + '" y1="' + wy + '" x2="' + wx1 + '" y2="' + wy +
        '" stroke="' + color + '" stroke-width="1.5" stroke-linecap="round"/>');
    }
    var singles = [];
    vis.forEach(function (s) {
      var pts = [];
      s.points.forEach(function (p) {
        var px = sx(p.x), py = sy(p.y);
        pts.push({ x: px, y: py });
        hits.push({ sx: px, sy: py, r: PT_R, color: s.color, impl: s.impl, setting: p.setting, bpp: p.y, t: p.t, rig: p.rig, runs: p.runs, sd: p.sd });
        if (s.single) singles.push({ cx: px, cy: py, r: PT_R, color: s.color, impl: s.impl, p: p });
      });
      if (s.single) return;
      if (s.points.length > 1) {
        svg.push('<path class="q-line" d="' + smoothPath(pts) + '" stroke="' + s.color + '"/>');
      }
      s.points.forEach(function (p) {
        whisker(p, s.color);
        svg.push('<circle class="q-pt" cx="' + sx(p.x).toFixed(1) + '" cy="' + sy(p.y).toFixed(1) +
          '" r="' + PT_R.toFixed(1) + '" fill="' + s.color + '"' +
          (p.rig ? ' stroke="#1a1a1a" stroke-width="1.2"' : "") + "/>");
      });
    });
    // A single-knob encoder is one operating point: a hollow diamond at the high end +
    // a direct label, so it's unmistakable and visibly matches its Best-bpp row. Labels
    // are nudged down to a minimum gap when bpps coincide (a thin leader links each).
    singles.sort(function (a, b) { return a.cy - b.cy; });
    var lastLabelY = -1e9, LABEL_GAP = 13;
    singles.forEach(function (m) {
      whisker(m.p, m.color);
      var rr = m.r;
      svg.push('<path class="q-pt-single" d="M' + m.cx.toFixed(1) + "," + (m.cy - rr).toFixed(1) +
        "L" + (m.cx + rr).toFixed(1) + "," + m.cy.toFixed(1) + "L" + m.cx.toFixed(1) + "," + (m.cy + rr).toFixed(1) +
        "L" + (m.cx - rr).toFixed(1) + "," + m.cy.toFixed(1) + 'Z" fill="#fff" stroke="' + m.color + '" stroke-width="2"/>');
      var ly = Math.max(m.cy + 4, lastLabelY + LABEL_GAP);
      lastLabelY = ly;
      var lx = m.cx - rr - 5;
      if (ly - (m.cy + 4) > 1) {
        svg.push('<line class="q-grid" x1="' + (m.cx - rr).toFixed(1) + '" y1="' + m.cy.toFixed(1) +
          '" x2="' + (lx + 2).toFixed(1) + '" y2="' + (ly - 4).toFixed(1) + '"/>');
      }
      svg.push('<text class="q-ll-name" x="' + lx.toFixed(1) + '" y="' + ly.toFixed(1) +
        '" text-anchor="end">' + esc(m.impl) + "</text>");
    });
    svg.push('<circle class="q-hl" r="7.5" visibility="hidden"/>');

    var runsTxt = minRuns === Infinity ? "" : minRuns + " ";
    var note;
    if (nRig === 0) {
      note = timingNoteHTML();
    } else if (nRig === nTot) {
      note = timingNoteHTML("Encode times are isolated, repeated-trial measurements (" + runsTxt + "runs each).");
    } else {
      note = timingNoteHTML("* Whiskered points are rigorously timed (" + runsTxt +
        "runs, ±σ); interior points are single-pass wall-clocks, joined by a spline — the trusted extremes anchor the curve.");
    }
    host.innerHTML = '<div class="q-plot"><svg role="img" aria-label="Bits per pixel versus encode time" viewBox="0 0 ' + VBW + " " + VBH +
      '" preserveAspectRatio="xMidYMid meet">' + svg.join("") + "</svg></div>" + note +
      '<div class="q-tooltip" hidden></div>';

    attachScatterHover(host, hits, function (best) {
      return "<b>" + esc(best.impl) + "</b><br>" +
        '<span class="k">setting</span> ' + esc(best.setting) + "<br>" +
        '<span class="k">bpp</span> ' + best.bpp.toFixed(3) +
        (isNum(best.t) && best.t > 0
          ? '<br><span class="k">encode time</span> ' + fmtTime(best.t) +
            (best.rig ? " (" + best.runs + " runs ±" + fmtTime(best.sd || 0) + ")" : " (single-pass)")
          : "");
    });
  }

  // ---- decoder fidelity & speed -------------------------------------------

  function renderDecoders() {
    var host = document.getElementById("q-decoders");
    if (!host) return;
    if (!Object.keys(DECODERS).length) {
      host.innerHTML = '<p class="q-note">No decoders measured.</p>';
      return;
    }
    // Format filter applies to both views; the table additionally drops
    // impl-hidden decoders (the chart honours implsOff per series).
    var impls = Object.keys(DECODERS).filter(function (k) { return !state.formatsOff[DECODERS[k].format]; });
    if (!impls.length) {
      host.innerHTML = '<p class="q-note">No decoders for the selected formats.</p>';
      return;
    }
    impls.sort(function (a, b) {
      var da = DECODERS[a], db = DECODERS[b];
      if (da.format !== db.format) return da.format < db.format ? -1 : 1;
      return da.mean_time_s - db.mean_time_s;
    });
    if (state.decoderView === "chart") {
      host.innerHTML = '<div id="q-decoder-chart" class="q-chart"></div>';
      renderDecoderChart(document.getElementById("q-decoder-chart"), impls);
      return;
    }
    var tableImpls = impls.filter(function (k) { return !state.implsOff[k]; });
    var html = '<table class="q-table"><thead><tr>' +
      '<th scope="col">Format</th><th scope="col">Decoder</th><th scope="col">Mean decode *</th>' +
      '<th scope="col">Mean input bpp</th><th scope="col">Fidelity</th><th scope="col">Basis</th></tr></thead><tbody>';
    tableImpls.forEach(function (impl) {
      var d = DECODERS[impl];
      // Three fidelity states (so expected lossy non-exactness is not flagged as a
      // failure): bit-exact -> good; a finite PSNR that is EXPECTED for a
      // non-normative lossy format (JPEG, lossy JXL) -> neutral "faithful"; a
      // finite PSNR where bit-exact IS required (lossless path, or AV1/VP8) -> bad.
      // The class goes on the <td> so .good/.bad/.q-approx actually match.
      var fidClass, fidText;
      if (d.bit_exact) {
        fidClass = "good"; fidText = "∞ (bit-exact)";
      } else if (d.approx_expected) {
        fidClass = "q-approx";
        fidText = d.worst_psnr != null
          ? "≈ " + d.worst_psnr.toFixed(2) + " dB vs golden (faithful)"
          : "≈ vs golden (faithful)";
      } else {
        fidClass = "bad";
        fidText = d.worst_psnr != null
          ? d.worst_psnr.toFixed(2) + " dB (worst)" : "not exact";
      }
      var basis = d.basis === "source" ? "source (ground truth)" : "golden decoder";
      html += "<tr><td>" + esc(d.format.toUpperCase()) + "</td><td>" + esc(impl) +
        '</td><td class="num">' + fmtTime(d.mean_time_s) +
        '</td><td class="num">' + d.mean_bpp.toFixed(3) +
        '</td><td class="' + fidClass + '">' + fidText +
        "</td><td>" + esc(basis) + "</td></tr>";
    });
    html += "</tbody></table>";
    host.innerHTML = html + timingNoteHTML();
  }

  // Speed-vs-bitrate scatter: X = input bpp, Y = one-pass decode time. Bit-exact
  // points are filled; approximate-decode points (finite PSNR vs the reference)
  // are hollow rings.
  function renderDecoderChart(host, impls) {
    if (!host) return;
    var series = impls.map(function (impl) {
      var d = DECODERS[impl];
      var byLabel = {};
      (d.points || []).forEach(function (p) {
        var a = byLabel[p.label] || (byLabel[p.label] =
          { bpp: 0, t: 0, n: 0, approx: false, worst: null, label: p.label, rig: { sum: 0, sq: 0, vsum: 0, n: 0, runs: 0 } });
        a.bpp += p.bpp; a.t += isNum(p.time_s) ? p.time_s : 0; a.n += 1;
        var notExact = p.bit_exact === false || (p.bit_exact == null && isNum(p.psnr));
        if (notExact) { a.approx = true; if (isNum(p.psnr)) a.worst = a.worst == null ? p.psnr : Math.min(a.worst, p.psnr); }
        // Pool the isolated, single-core rigorous decode timings (already gated to
        // runs > 1 in plotting.decoder_fidelity) across this label's images.
        if (isNum(p.time_rigorous_s) && (p.runs || 0) > 1) {
          var rg = a.rig, rsd = p.time_stddev_s || 0;
          rg.sum += p.time_rigorous_s; rg.sq += p.time_rigorous_s * p.time_rigorous_s; rg.vsum += rsd * rsd; rg.n += 1;
          rg.runs = rg.runs === 0 ? p.runs : Math.min(rg.runs, p.runs);
        }
      });
      var points = Object.keys(byLabel).map(function (k) {
        var a = byLabel[k], rig = null;
        if (a.rig.n > 0) {
          var rmu = a.rig.sum / a.rig.n, within = a.rig.vsum / a.rig.n, between = Math.max(a.rig.sq / a.rig.n - rmu * rmu, 0);
          rig = { t: rmu, sd: Math.sqrt(within + between), runs: a.rig.runs, n: a.rig.n };
        }
        // Plot the rigorous mean where the overlay anchored this step, else the
        // single-pass mean decode time.
        return { x: a.bpp / a.n, y: rig ? rig.t : a.t / a.n, label: a.label, approx: a.approx, worst: a.worst, rig: rig, count: a.n };
      }).sort(function (a, b) { return a.x - b.x; });
      return {
        impl: impl, color: implTone(d.format, impl),
        points: points,
        approxExpected: d.approx_expected,
      };
    }).filter(function (s) { return s.points.length > 0; });
    if (!series.length) { host.innerHTML = ""; return; }
    // Series visibility follows the Tests filter (state.implsOff); its swatched
    // checkboxes are the legend, so no per-chart legend is drawn here.
    var vis = series.filter(function (s) { return !state.implsOff[s.impl]; });
    if (!vis.length) {
      host.innerHTML = '<div class="q-plot"><svg role="img" aria-label="Decode time versus input bits per pixel (nothing shown)" viewBox="0 0 ' +
        VBW + " " + VBH + '"><text x="' + VBW / 2 + '" y="' + VBH / 2 +
        '" text-anchor="middle" class="q-tick">No series selected</text></svg></div>';
      return;
    }

    // Rigor coverage of the visible points drives the axis mark + footnote.
    var nRig = 0, nTot = 0, minRuns = Infinity;
    vis.forEach(function (s) { s.points.forEach(function (p) {
      nTot++; if (p.rig) { nRig++; if (p.rig.runs) minRuns = Math.min(minRuns, p.rig.runs); }
    }); });

    var xs = [], ys = [];
    vis.forEach(function (s) { s.points.forEach(function (p) { xs.push(p.x); ys.push(p.y); }); });
    var xmin = Math.min.apply(null, xs), xmax = Math.max.apply(null, xs);
    var ymax = Math.max.apply(null, ys);
    var xticks = linTicks(xmin, xmax, 6);
    var dxmin = Math.min(xmin, xticks[0]), dxmax = Math.max(xmax, xticks[xticks.length - 1]);
    var yticks = linTicks(0, ymax, 6);
    var dymin = 0, dymax = Math.max(ymax, yticks[yticks.length - 1]);
    if (dymax === dymin) dymax = dymin + 1;
    function sx(x) { return X0 + (x - dxmin) / (dxmax - dxmin || 1) * (X1 - X0); }
    function sy(y) { return Y1 - (y - dymin) / (dymax - dymin) * (Y1 - Y0); }

    var svg = [], hits = [];
    xticks.forEach(function (t) {
      if (t < dxmin - 1e-9 || t > dxmax + 1e-9) return;
      var x = sx(t).toFixed(1);
      svg.push('<line class="q-grid" x1="' + x + '" y1="' + Y0 + '" x2="' + x + '" y2="' + Y1 + '"/>');
      svg.push('<text class="q-tick" x="' + x + '" y="' + (Y1 + 18) + '" text-anchor="middle">' + esc(fmtNum(t)) + "</text>");
    });
    yticks.forEach(function (t) {
      if (t < dymin - 1e-9 || t > dymax + 1e-9) return;
      var y = sy(t).toFixed(1);
      svg.push('<line class="q-grid" x1="' + X0 + '" y1="' + y + '" x2="' + X1 + '" y2="' + y + '"/>');
      svg.push('<text class="q-tick" x="' + (X0 - 8) + '" y="' + (+y + 4) + '" text-anchor="end">' + esc(fmtTime(t)) + "</text>");
    });
    svg.push('<line class="q-axis" x1="' + X0 + '" y1="' + Y1 + '" x2="' + X1 + '" y2="' + Y1 + '"/>');
    svg.push('<line class="q-axis" x1="' + X0 + '" y1="' + Y0 + '" x2="' + X0 + '" y2="' + Y1 + '"/>');
    svg.push('<text class="q-axis-title" x="' + ((X0 + X1) / 2) + '" y="' + (VBH - 8) + '" text-anchor="middle">Input bits per pixel (bpp)</text>');
    // '*' only while some plotted decode time is single-pass (drops once all rigorous).
    var decMarked = nRig < nTot;
    svg.push('<text class="q-axis-title" transform="translate(16,' + ((Y0 + Y1) / 2) + ') rotate(-90)" text-anchor="middle">Decode time (lower is better)' + (decMarked ? " *" : "") + "</text>");
    // A vertical ±σ whisker marks each rigorously-timed (anchored) decode point.
    function vwhisker(p, color) {
      if (!(p.rig && p.rig.sd > 0)) return;
      var wx = sx(p.x).toFixed(1);
      var wy0 = sy(Math.max(dymin, p.y - p.rig.sd)).toFixed(1);
      var wy1 = sy(Math.min(dymax, p.y + p.rig.sd)).toFixed(1);
      svg.push('<line class="q-whisker" x1="' + wx + '" y1="' + wy0 + '" x2="' + wx + '" y2="' + wy1 +
        '" stroke="' + color + '" stroke-width="1.5" stroke-linecap="round"/>');
    }
    vis.forEach(function (s) {
      var pts = [];
      s.points.forEach(function (p) {
        var px = sx(p.x), py = sy(p.y);
        pts.push({ x: px, y: py });
        hits.push({ sx: px, sy: py, r: PT_R, color: s.color, impl: s.impl, step: p.label, bpp: p.x, t: p.y, approx: p.approx, worst: p.worst, approxExpected: s.approxExpected, rig: p.rig || null, count: p.count });
      });
      if (s.points.length > 1) {
        svg.push('<path class="q-line" d="' + smoothPath(pts) + '" stroke="' + s.color + '"/>');
      }
      s.points.forEach(function (p) {
        var cx = sx(p.x).toFixed(1), cy = sy(p.y).toFixed(1);
        vwhisker(p, s.color);
        if (p.approx) {
          svg.push('<circle class="q-pt q-pt-approx" cx="' + cx + '" cy="' + cy + '" r="4.5" fill="#fff" stroke="' + s.color + '"/>');
        } else {
          svg.push('<circle class="q-pt" cx="' + cx + '" cy="' + cy + '" r="4.5" fill="' + s.color + '"' + (p.rig ? ' stroke="#1a1a1a" stroke-width="1.2"' : "") + "/>");
        }
      });
    });
    svg.push('<circle class="q-hl" r="7.5" visibility="hidden"/>');
    var dRunsTxt = minRuns === Infinity ? "" : minRuns + " ";
    var decNote;
    if (nRig === 0) decNote = timingNoteHTML();
    else if (nRig === nTot) decNote = timingNoteHTML("Decode times are isolated, repeated-trial measurements (" + dRunsTxt + "runs each, pinned to one core).");
    else decNote = timingNoteHTML("* Ringed/whiskered points are rigorously timed (" + dRunsTxt + "runs, ±σ, isolated on a dedicated core); the rest are single-pass wall-clocks.");
    host.innerHTML = '<div class="q-plot"><svg role="img" aria-label="Decode time versus input bits per pixel" viewBox="0 0 ' + VBW + " " + VBH +
      '" preserveAspectRatio="xMidYMid meet">' + svg.join("") + "</svg></div>" +
      '<p class="q-note">Hollow markers = approximate decode (differs from the reference it is scored against); filled = bit-exact.</p>' +
      decNote +
      '<div class="q-tooltip" hidden></div>';

    attachScatterHover(host, hits, function (best) {
      return "<b>" + esc(best.impl) + "</b><br>" +
        '<span class="k">step</span> ' + esc(best.step) + "<br>" +
        '<span class="k">input bpp</span> ' + best.bpp.toFixed(3) + "<br>" +
        '<span class="k">decode time</span> ' + fmtTime(best.t) +
        (best.rig
          ? " (" + best.rig.runs + " runs ±" + fmtTime(best.rig.sd || 0) +
            (best.rig.n < best.count ? ", rigorous on " + best.rig.n + "/" + best.count + " imgs" : "") + ")"
          : " (single-pass)") + "<br>" +
        '<span class="k">fidelity</span> ' +
        (best.approx
          ? (best.worst != null
              ? (best.approxExpected
                  ? "≈ " + best.worst.toFixed(2) + " dB vs golden (faithful)"
                  : best.worst.toFixed(2) + " dB (worst)")
              : "not bit-exact")
          : "∞ (bit-exact)");
    });
  }

  // Shared nearest-point hover for the scatter charts (lossless effort, decoder).
  function attachScatterHover(host, hits, tipHTML) {
    var svgEl = host.querySelector("svg");
    var tip = host.querySelector(".q-tooltip");
    var hl = host.querySelector(".q-hl");
    if (!svgEl) return;
    svgEl.addEventListener("mousemove", function (ev) {
      var r = svgEl.getBoundingClientRect();
      var vx = (ev.clientX - r.left) * (VBW / r.width);
      var vy = (ev.clientY - r.top) * (VBH / r.height);
      var best = null, bd = 1e9;
      hits.forEach(function (h) {
        var dd = (h.sx - vx) * (h.sx - vx) + (h.sy - vy) * (h.sy - vy);
        if (dd < bd) { bd = dd; best = h; }
      });
      if (best && bd <= 26 * 26) {
        hl.setAttribute("cx", best.sx); hl.setAttribute("cy", best.sy);
        hl.setAttribute("r", Math.max(7.5, (best.r || PT_R) + 3).toFixed(1));
        hl.setAttribute("stroke", best.color); hl.setAttribute("visibility", "visible");
        var crect = host.getBoundingClientRect();
        tip.hidden = false;
        tip.innerHTML = tipHTML(best);
        var tx = ev.clientX - crect.left + 14, ty = ev.clientY - crect.top + 12;
        if (tx + 220 > crect.width) tx = ev.clientX - crect.left - 14 - 220;
        tip.style.left = Math.max(0, tx) + "px";
        tip.style.top = ty + "px";
      } else {
        hl.setAttribute("visibility", "hidden"); tip.hidden = true;
      }
    });
    svgEl.addEventListener("mouseleave", function () {
      hl.setAttribute("visibility", "hidden"); tip.hidden = true;
    });
  }

  // ---- per-point image lightbox -------------------------------------------
  // Clicking a data point opens the exact images aggregated into it (the run's
  // assets/, referenced by relative URL — never embedded, so report.html stays
  // small and a multi-GB tree loads only the on-screen thumbnails, lazily).

  var _modal = null;   // the open modal (image lightbox or Information card), or null

  function fmtBytes(n) {
    if (!isNum(n) || n <= 0) return "—";
    var u = ["B", "KB", "MB", "GB"], i = 0;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return (i === 0 ? n : n.toFixed(n < 10 ? 1 : 0)) + " " + u[i];
  }
  function shortName(s) {
    s = String(s || "").split("/").pop();
    return s.length > 16 ? s.slice(0, 13) + "…" : s;
  }
  function capHTML(r) {
    function num(v, d) { return isNum(v) ? v.toFixed(d) : "—"; }
    var parts = ['<span class="k">' + esc(shortName(r.source_path || r.name)) + "</span>",
      "bpp " + num(r.bpp, 3), "S2 " + num(r.ssimulacra2, 1)];
    if (r.psnr != null) parts.push("PSNR " + num(r.psnr, 1));
    if (r.ssim != null) parts.push("SSIM " + num(r.ssim, 3));
    if (r.butteraugli != null) parts.push("BA " + num(r.butteraugli, 2));
    parts.push(fmtBytes(r.filesize));
    if (r.bit_exact === true) parts.push("bit-exact");
    else if (r.bit_exact === false) parts.push("not bit-exact");
    return parts.join(" · ");
  }

  function trapFocus(ev, root) {
    var f = [].filter.call(
      root.querySelectorAll('button, [tabindex]:not([tabindex="-1"])'),
      function (e) { return e.offsetParent !== null; });
    if (!f.length) return;
    var first = f[0], last = f[f.length - 1];
    if (ev.shiftKey && document.activeElement === first) { ev.preventDefault(); last.focus(); }
    else if (!ev.shiftKey && document.activeElement === last) { ev.preventDefault(); first.focus(); }
  }

  function closeModal() {
    if (!_modal) return;
    document.removeEventListener("keydown", _modal.onKey, true);
    if (_modal.overlay.parentNode) _modal.overlay.parentNode.removeChild(_modal.overlay);
    document.body.classList.remove("q-lb-open");
    if (_modal.prevFocus && _modal.prevFocus.focus) _modal.prevFocus.focus();
    _modal = null;
  }

  // Shared modal scaffold (overlay, header, focus trap, Escape, background
  // freeze, focus restore) for the image lightbox and the Information card.
  // opts: {title, ariaLabel?, actions?:[el], body?:el, focus?:el}.
  function openModal(opts) {
    closeModal();
    var prevFocus = document.activeElement;
    var overlay = el("div", { class: "q-lightbox", role: "dialog", "aria-modal": "true",
      "aria-label": opts.ariaLabel || opts.title || "Dialog" });
    var dialog = el("div", { class: "q-lb-dialog" });
    overlay.appendChild(dialog);
    var head = el("div", { class: "q-lb-head" });
    head.appendChild(el("h3", { class: "q-lb-title" }, opts.title || ""));
    var actions = el("div", { class: "q-lb-actions" });
    (opts.actions || []).forEach(function (a) { actions.appendChild(a); });
    var closeBtn = el("button", { type: "button", class: "q-lb-close", "aria-label": "Close" }, "✕");
    closeBtn.addEventListener("click", closeModal);
    actions.appendChild(closeBtn);
    head.appendChild(actions);
    dialog.appendChild(head);
    if (opts.body) dialog.appendChild(opts.body);
    overlay.addEventListener("click", function (ev) { if (ev.target === overlay) closeModal(); });
    document.body.appendChild(overlay);
    document.body.classList.add("q-lb-open");
    _modal = { overlay: overlay, prevFocus: prevFocus, onKey: function (ev) {
      if (ev.key === "Escape") { ev.preventDefault(); closeModal(); }
      else if (ev.key === "Tab") { trapFocus(ev, dialog); }
    } };
    document.addEventListener("keydown", _modal.onKey, true);
    (opts.focus || closeBtn).focus();
    return dialog;
  }

  function openLightbox(fmt, impl, label) {
    var rows = METRICS.filter(function (m) {
      return m.format === fmt && m.impl === impl && m.label === label && m.asset_path;
    });
    if (!rows.length) return;
    var showOrig = false;

    var grid = el("div", { class: "q-lb-grid" });
    var cells = [];
    rows.forEach(function (r) {
      var fig = el("figure", { class: "q-lb-fig" });
      var img = el("img", { loading: "lazy", decoding: "async", src: r.asset_path,
        alt: "Reconstruction of " + shortName(r.source_path || r.name) });
      img.addEventListener("error", function () {
        if (fig.querySelector(".q-lb-err")) return;
        img.style.display = "none";
        fig.insertBefore(el("div", { class: "q-lb-err" },
          "Preview unavailable — this browser may not support " + fmt.toUpperCase() + "."), fig.firstChild);
      });
      fig.appendChild(img);
      var cap = el("figcaption", { class: "q-lb-cap" });
      cap.innerHTML = capHTML(r);
      fig.appendChild(cap);
      grid.appendChild(fig);
      cells.push({ img: img, fig: fig, row: r });
    });

    function applyMode() {
      cells.forEach(function (c) {
        var src = (showOrig && c.row.source_asset) ? c.row.source_asset : c.row.asset_path;
        var err = c.fig.querySelector(".q-lb-err"); if (err) err.parentNode.removeChild(err);
        c.img.style.display = "";
        c.img.alt = (showOrig ? "Original of " : "Reconstruction of ") + shortName(c.row.source_path || c.row.name);
        if (c.img.getAttribute("src") !== src) c.img.setAttribute("src", src);
      });
    }

    var toggle = null;
    if (rows.some(function (r) { return r.source_asset; })) {
      toggle = el("button", { type: "button", class: "q-lb-toggle", "aria-pressed": "false" }, "Show original");
      toggle.addEventListener("click", function () {
        showOrig = !showOrig;
        toggle.textContent = showOrig ? "Show reconstruction" : "Show original";
        toggle.setAttribute("aria-pressed", showOrig ? "true" : "false");
        applyMode();
      });
    }
    openModal({
      title: impl + " · " + fmt.toUpperCase() + " · " + label +
        " (" + rows.length + " image" + (rows.length === 1 ? "" : "s") + ")",
      ariaLabel: impl + " — " + fmt + " " + label + " images",
      actions: toggle ? [toggle] : [], body: grid, focus: toggle,
    });
  }

  // The Information hero card: clone the hidden #dash-info body into a modal.
  function openInfoModal() {
    var src = document.getElementById("dash-info");
    if (!src) return;
    var body = el("div", { class: "q-info-body" });
    body.innerHTML = src.innerHTML;
    openModal({ title: "Run information", ariaLabel: "Run information", body: body });
  }

  // ---- dashboard navigation ------------------------------------------------
  // A left rail of categories + per-category graphs drives a single-graph stage
  // (the only scroll region). Graphs render on demand, so each reflects the live
  // filter state when shown; switching never reloads the page.

  var SECTION_LABELS = { perf: "Performance", scaling: "Scaling", effort: "Effort" };

  // Categories actually present in this bundle, in a fixed order. A static suite
  // drops out when the format filter hides every chart in it.
  function buildCategories() {
    var cats = [];
    if (document.getElementById("quality-app") && AGG_ALL && Object.keys(AGG_ALL).length)
      cats.push({ id: "quality", label: "Quality" });
    if (Object.keys(LOSSLESS).length) cats.push({ id: "lossless", label: "Lossless" });
    if (Object.keys(DECODERS).length) cats.push({ id: "decoder", label: "Decoder" });
    ["perf", "scaling", "effort"].forEach(function (s) {
      var sec = document.querySelector("[data-chart-section='" + s + "']");
      if (sec && sectionHasVisible(sec))
        cats.push({ id: "sec:" + s, label: SECTION_LABELS[s] });
    });
    return cats;
  }

  // The graphs (rail steps) within a category. Each carries the stage panel
  // group it lives in and the kind that drives the controls box + renderer.
  function buildGraphList(catId) {
    if (catId === "quality") {
      var L = availableMetrics().map(function (m) {
        return { id: "rd-" + m, label: METRIC_INFO[m].name, group: "rd", kind: "rd", metric: m };
      });
      if (Object.keys(BDRATE).length)
        L.push({ id: "bdrate", label: "BD-rate", group: "bdrate", kind: "bdrate" });
      return L;
    }
    if (catId === "lossless") return [
      { id: "ll-bars", label: "Best bpp", group: "lossless", kind: "lossless-bars", view: "bars" },
      { id: "ll-effort", label: "Size vs effort", group: "lossless", kind: "lossless-effort", view: "effort" },
    ];
    if (catId === "decoder") return [
      { id: "dec-table", label: "Fidelity & speed", group: "decoder", kind: "decoder-table", view: "table" },
      { id: "dec-chart", label: "Speed vs bitrate", group: "decoder", kind: "decoder-chart", view: "chart" },
    ];
    return galleryGraphs(catId);
  }

  // Static gallery category -> one rail step per visible format group (or a
  // single step for a flat, ungrouped gallery).
  function galleryGraphs(catId) {
    var s = catId.indexOf("sec:") === 0 ? catId.slice(4) : catId;
    var sec = document.querySelector("[data-chart-section='" + s + "']");
    if (!sec) return [];
    var tabs = [].slice.call(sec.querySelectorAll('[role="tab"][data-format]'))
      .filter(function (t) { return !t.hidden; });
    if (!tabs.length)
      return [{ id: s + "-all", label: SECTION_LABELS[s] || s, group: "sec:" + s, kind: "gallery" }];
    return tabs.map(function (t, i) {
      return { id: s + "-" + i, label: t.textContent || ("Group " + (i + 1)),
        group: "sec:" + s, kind: "gallery", tab: t };
    });
  }

  // The stage panel hosting a graph group.
  function panelEl(group) {
    if (group.indexOf("sec:") === 0)
      return document.querySelector("[data-chart-section='" + group.slice(4) + "']");
    return document.querySelector("#dash-stage [data-graph-group='" + group + "']");
  }

  function navButton(cls, label, on, onClick) {
    var attrs = { type: "button", class: cls + (on ? " active" : "") };
    if (on) attrs["aria-current"] = "true";
    var b = el("button", attrs, label);
    b.addEventListener("click", onClick);
    return b;
  }

  function renderNav() {
    var nav = document.getElementById("dash-nav");
    if (!nav) return;
    var cats = buildCategories();
    nav.innerHTML = "";
    if (!cats.length) { nav.hidden = true; return; }
    nav.hidden = false;
    if (!cats.some(function (c) { return c.id === state.cat; })) state.cat = cats[0].id;

    nav.appendChild(el("div", { class: "dash-title" }, "Image Evaluation"));

    var catWrap = el("div", { class: "dash-cats" });
    cats.forEach(function (c) {
      catWrap.appendChild(navButton("dash-cat", c.label, c.id === state.cat,
        function () { selectCategory(c.id); }));
    });
    nav.appendChild(catWrap);

    var list = buildGraphList(state.cat);
    if (!list.some(function (g) { return g.id === state.graph; }))
      state.graph = list[0] ? list[0].id : null;
    var gWrap = el("div", { class: "dash-graphs" });
    list.forEach(function (g) {
      gWrap.appendChild(navButton("dash-graph", g.label, g.id === state.graph,
        function () { showGraph(state.cat, g.id); }));
    });
    nav.appendChild(gWrap);

    if (list.length > 1) {
      var pn = el("div", { class: "dash-prevnext" });
      var prev = el("button", { type: "button", class: "dash-step", "aria-label": "Previous graph" }, "‹ Prev");
      var next = el("button", { type: "button", class: "dash-step", "aria-label": "Next graph" }, "Next ›");
      prev.addEventListener("click", function () { stepGraph(-1); });
      next.addEventListener("click", function () { stepGraph(1); });
      pn.appendChild(prev); pn.appendChild(next);
      nav.appendChild(pn);
    }
  }

  function selectCategory(catId) {
    state.cat = catId;
    var list = buildGraphList(catId);
    showGraph(catId, list[0] ? list[0].id : null);
  }

  // Reveal one graph: show its stage panel, configure + render it, refresh the
  // rail + controls. Falls back to the first category/graph if the requested one
  // is gone (e.g. filtered out).
  function showGraph(catId, graphId) {
    var cats = buildCategories();
    var cat = cats.filter(function (c) { return c.id === catId; })[0] || cats[0];
    if (!cat) { renderNav(); return; }
    var list = buildGraphList(cat.id);
    var g = list.filter(function (x) { return x.id === graphId; })[0] || list[0];
    state.cat = cat.id;
    state.graph = g ? g.id : null;
    state.graphKind = g ? g.kind : null;

    var stage = document.getElementById("dash-stage");
    var target = g ? panelEl(g.group) : null;
    if (stage) [].forEach.call(stage.children, function (p) { p.hidden = p !== target; });

    if (g) {
      if (g.kind === "rd") { state.metric = g.metric; renderRD(); }
      else if (g.kind === "bdrate") renderBdRate();
      else if (g.kind === "lossless-bars" || g.kind === "lossless-effort") { state.losslessView = g.view; renderLossless(); }
      else if (g.kind === "decoder-table" || g.kind === "decoder-chart") { state.decoderView = g.view; renderDecoders(); }
      else if (g.kind === "gallery" && g.tab) g.tab.click();
    }

    renderControls();
    renderFilterBar();   // the bar lists strictly this figure's variants
    renderNav();
    if (stage) stage.scrollTop = 0;
    if (g) announce("Showing " + cat.label + " — " + g.label);
  }

  // Prev / Next step within the active category, crossing into the adjacent
  // category at the ends.
  function stepGraph(d) {
    var list = buildGraphList(state.cat);
    var i = list.map(function (g) { return g.id; }).indexOf(state.graph);
    var ni = (i < 0 ? 0 : i) + d;
    if (ni >= 0 && ni < list.length) { showGraph(state.cat, list[ni].id); return; }
    var cats = buildCategories().map(function (c) { return c.id; });
    var ci = cats.indexOf(state.cat) + d;
    if (ci < 0 || ci >= cats.length) return;
    var l2 = buildGraphList(cats[ci]);
    var pick = d < 0 && l2.length ? l2[l2.length - 1] : l2[0];
    showGraph(cats[ci], pick ? pick.id : null);
  }

  function initDashboard() {
    var cats = buildCategories();
    if (cats.length) {
      var list = buildGraphList(cats[0].id);
      showGraph(cats[0].id, list[0] ? list[0].id : null);
    } else {
      renderNav();
    }
    var info = document.getElementById("dash-info-btn");
    if (info) info.addEventListener("click", openInfoModal);
  }

  function initGalleries() {
    var boxes = document.querySelectorAll("[data-img-tabs]");
    [].forEach.call(boxes, function (box) {
      var tabs = [].slice.call(box.querySelectorAll('[role="tab"]'));
      var panels = [].slice.call(box.querySelectorAll('[role="tabpanel"]'));
      function select(i, focus) {
        tabs.forEach(function (t, j) {
          var sel = j === i;
          t.setAttribute("aria-selected", sel ? "true" : "false");
          t.setAttribute("tabindex", sel ? "0" : "-1");
          t.classList.toggle("active", sel);
          if (panels[j]) panels[j].hidden = !sel;
          if (sel && focus) t.focus();
        });
      }
      tabs.forEach(function (t, i) {
        t.addEventListener("click", function () { select(i, false); });
        t.addEventListener("keydown", function (e) {
          var d = { ArrowRight: 1, ArrowLeft: -1 }[e.key];
          if (d) { e.preventDefault(); select((i + d + tabs.length) % tabs.length, true); }
          else if (e.key === "Home") { e.preventDefault(); select(0, true); }
          else if (e.key === "End") { e.preventDefault(); select(tabs.length - 1, true); }
        });
      });
    });
  }

  function init() {
    initGalleries();
    // Build the filter bar up front so the format filter governs the static
    // galleries even in a perf-only bundle; it is rebuilt with the Tests groups
    // once the quality data resolves.
    renderFilterBar();
    applyGalleryFilter();
    if (document.getElementById("quality-app")) {
      AGG_ALL = aggregateAll(validRows(METRICS));
      renderAggregationNote();
      renderFilterBar();
    }
    // Build the navigation rail and show the first graph (works with or without
    // a quality app — a perf-only bundle just gets the static categories).
    initDashboard();

    // Alt+[ / Alt+] cycle the X axis from anywhere while a rate-distortion graph
    // is active (the button group also takes direct clicks).
    document.addEventListener("keydown", function (e) {
      if (!e.altKey || state.graphKind !== "rd") return;
      var axes = availableXAxes();
      var i = axes.indexOf(state.xKey); if (i < 0) i = 0;
      if (e.key === "]") { e.preventDefault(); setAxis(axes[(i + 1) % axes.length]); }
      else if (e.key === "[") { e.preventDefault(); setAxis(axes[(i - 1 + axes.length) % axes.length]); }
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
