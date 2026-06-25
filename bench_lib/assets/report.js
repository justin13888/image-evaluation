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

  var PALETTE = [
    "#4C72B0", "#DD8452", "#55A868", "#C44E52", "#8172B3", "#937860",
    "#DA8BC3", "#8C8C8C", "#CCB974", "#64B5CD", "#E377C2", "#17BECF",
  ];
  var FORMAT_COLORS = {
    jpeg: "#DD8452", webp: "#4C72B0", avif: "#55A868",
    jxl: "#C44E52", png: "#8172B3",
  };
  var DASHES = ["", "7 4", "2 4", "9 4 2 4", "1 4"];

  var METRICS = readJSON("quality-metrics") || [];
  var PARETO = readJSON("quality-pareto") || {};
  var BDRATE = readJSON("quality-bdrate") || {};
  // Precomputed lossless compression-efficiency summary (issue #26):
  // { impl: {format, best_bpp, best_label, ratio, points:[{label,value,bpp}]} }.
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

  // state
  var state = {
    xKey: "bpp",               // X axis: "bpp" | "time_s" | "decode_time_s"
    xLog: true,                // X scale: log (true) or linear (false)
    metricsOn: {},             // metric key -> bool (shown)
    implsOff: {},              // impl name -> true (hidden globally)
    formatsOff: {},            // lowercase format key -> true (format hidden everywhere)
    barCollapsed: false,       // floating filter bar minimised to its header
    showTime: { rd: true, lossless: true, decoder: true },
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
      var s = (steps[m.label] = steps[m.label] || { label: m.label, q: m.quality_value, n: 0, acc: {} });
      s.n += 1;
      AXIS_KEYS.forEach(function (k) {
        var v = m[k];
        if (!isNum(v)) return;
        var a = s.acc[k] || (s.acc[k] = { sum: 0, sq: 0, n: 0 });
        a.sum += v; a.sq += v * v; a.n += 1;
      });
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
          return { label: s.label, q: s.q, count: s.n, m: mean, sd: sd };
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

  // series: [{key,label,color,dash?,points:[{x,y,std,t,step,q,count}]}]
  // opts: {xLog, xAxis (X_AXES entry), yInfo (METRIC_INFO entry), showTime, title}
  function renderXYChart(container, series, opts) {
    var info = opts.yInfo, xAxis = opts.xAxis, log = !!opts.xLog;
    var showTime = opts.showTime && xAxis.key === "bpp"; // time-as-bubble only on size charts
    var vis = series.filter(function (s) { return !state.implsOff[s.implName]; });
    container._hits = [];

    var visPts = [];
    vis.forEach(function (s) { s.points.forEach(function (p) { visPts.push(p); }); });
    var scale = showTime ? timeScale(visPts) : null;

    var xs = [], ys = [];
    vis.forEach(function (s) { s.points.forEach(function (p) { xs.push(p.x); ys.push(p.y); }); });
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
    svg.push('<text class="q-axis-title" x="' + ((X0 + X1) / 2) + '" y="' + (VBH - 8) + '" text-anchor="middle">' + esc(xAxis.title) + (log ? " — log" : "") + "</text>");
    svg.push('<text class="q-axis-title" transform="translate(16,' + ((Y0 + Y1) / 2) + ') rotate(-90)" text-anchor="middle">' + esc(info.y) + "</text>");

    var hits = [];
    vis.forEach(function (s) {
      var pts = [];
      s.points.forEach(function (p) {
        var px = sx(p.x), py = sy(p.y);
        pts.push({ x: px, y: py });
        var r = scale ? scale.r(p.t) : PT_R;
        hits.push({ sx: px, sy: py, r: r, color: s.color, label: s.label, impl: s.implName, format: s.fmt, x: p.x, y: p.y, q: p.q, step: p.step, count: p.count, std: p.std, t: p.t });
      });
      svg.push('<path class="q-line" d="' + smoothPath(pts) + '" stroke="' + s.color + '"' + (s.dash ? ' stroke-dasharray="' + s.dash + '"' : "") + "/>");
      s.points.forEach(function (p) {
        var r = scale ? scale.r(p.t) : PT_R;
        svg.push('<circle class="q-pt" cx="' + sx(p.x).toFixed(1) + '" cy="' + sy(p.y).toFixed(1) + '" r="' + r.toFixed(1) + '" fill="' + s.color + '"/>');
      });
    });
    svg.push('<circle class="q-hl" r="7.5" visibility="hidden"/>');

    plotHTML = '<div class="q-plot"><svg role="img" aria-label="' + esc(ariaLabel) +
      '" viewBox="0 0 ' + VBW + " " + VBH + '" preserveAspectRatio="xMidYMid meet"><title>' +
      esc(ariaLabel) + "</title>" + svg.join("") + "</svg></div>";
    container._hits = hits;

    container.innerHTML = plotHTML +
      (scale ? sizeLegendHTML(scale, "encode time") : "") +
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
      tip.innerHTML = "<b>" + esc(best.label) + "</b><br>" +
        '<span class="k">step</span> ' + esc(best.step) + "<br>" +
        '<span class="k">' + esc(xAxis.name) + (agg ? " (mean)" : "") + "</span> " + xAxis.fmt(best.x) + "<br>" +
        '<span class="k">' + esc(info.name) + (agg ? " (mean)" : "") + "</span> " + best.y.toFixed(2) +
        (agg && best.std > 0 ? " ± " + best.std.toFixed(2) : "") +
        (isNum(best.t) && best.t > 0 ? '<br><span class="k">encode time</span> ' + fmtTime(best.t) : "") +
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
  // mapped to the (x,y) of the active view, coloured by format and dashed per
  // encoder within a format. `fmt` is carried on each series so a clicked point
  // can resolve its image group. The Tests filter (state.implsOff) is the
  // authoritative series selector — renderXYChart drops the hidden ones.
  function seriesForView(AGG, xKey, yKey) {
    var series = [];
    Object.keys(AGG).sort().forEach(function (fmt) {
      if (state.formatsOff[fmt]) return;   // format filtered out in the bar
      var di = 0;
      AGG[fmt].forEach(function (s) {
        var points = s.points.map(function (p) {
          var x = p.m[xKey], y = p.m[yKey];
          if (!isNum(x) || !isNum(y)) return null;
          return { x: x, y: y, std: p.sd[yKey] || 0, t: p.m.time_s, step: p.label, q: p.q, count: p.count };
        }).filter(Boolean).sort(function (a, b) { return a.x - b.x; });
        if (points.length) {
          series.push({
            key: fmt + "/" + s.impl, implName: s.impl, fmt: fmt,
            label: fmt.toUpperCase() + " · " + s.impl,
            color: FORMAT_COLORS[fmt] || PALETTE[0], dash: DASHES[di % DASHES.length],
            points: points,
          });
          di++;
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

  // Clickable legend chips shared by every line chart. items: [{name,label,color}]
  // where `name` keys the global state.implsOff visibility map (so toggling a
  // series here also toggles it on the RD charts and in the filter bar). `rerender`
  // redraws the owning chart; a hidden series stays in the legend (struck-through)
  // so it can be brought back.
  function interactiveLegend(items, rerender) {
    var wrap = el("div", { class: "q-legend", role: "group", "aria-label": "Series — activate to show or hide" });
    items.forEach(function (it) {
      var off = !!state.implsOff[it.name];
      var chip = el("button", { class: "q-chip" + (off ? " off" : ""), type: "button", "aria-pressed": off ? "false" : "true" });
      chip.innerHTML = '<span class="sw" style="background:' + it.color + '"></span>' + esc(it.label);
      chip.addEventListener("click", function () {
        if (state.implsOff[it.name]) delete state.implsOff[it.name];
        else state.implsOff[it.name] = true;
        rerender();
        renderFilterBar();   // keep the floating bar's checkboxes in sync
      });
      wrap.appendChild(chip);
    });
    return wrap;
  }

  function legendFor(series) {
    return interactiveLegend(
      series.map(function (s) { return { name: s.implName, label: s.label, color: s.color }; }),
      renderRD
    );
  }

  // Rate-distortion: one full-width chart per selected metric, each overlaying
  // every shown format's encoders on the X axis chosen in the controls box.
  function renderRD() {
    var host = document.getElementById("q-rd");
    if (!host) return;
    host.innerHTML = "";
    var xAxis = X_AXES[state.xKey];
    var metrics = availableMetrics().filter(function (k) { return state.metricsOn[k]; });
    if (!metrics.length) {
      host.appendChild(el("p", { class: "q-note" }, "No metrics selected — enable one in the filter bar."));
      return;
    }
    var legendSeries = null;
    metrics.forEach(function (metricKey) {
      var series = seriesForView(AGG_ALL, state.xKey, metricKey);
      if (!legendSeries && series.length) legendSeries = series;
      var sec = el("div", { class: "q-stack-item" });
      sec.appendChild(el("div", { class: "q-metric-cap" }, METRIC_INFO[metricKey].name));
      var chart = el("div", { class: "q-chart" });
      sec.appendChild(chart);
      host.appendChild(sec);
      renderXYChart(chart, series, {
        xLog: state.xLog, xAxis: xAxis, yInfo: METRIC_INFO[metricKey],
        showTime: state.showTime.rd,
      });
    });
    if (legendSeries) host.insertBefore(legendFor(legendSeries), host.firstChild);
  }

  // ---- centralized floating filter bar -------------------------------------

  // Every implementation grouped by format, unioning the lossy-encoder, lossless
  // and decoder datasets so toggling a "test" is consistent everywhere it appears.
  function implsByFormat() {
    var map = {};
    function add(f, n) { if (!f) return; (map[f] = map[f] || {})[n] = 1; }
    if (AGG_ALL) Object.keys(AGG_ALL).forEach(function (f) {
      AGG_ALL[f].forEach(function (s) { add(f, s.impl); });
    });
    Object.keys(LOSSLESS).forEach(function (n) { add(LOSSLESS[n].format, n); });
    Object.keys(DECODERS).forEach(function (n) { add(DECODERS[n].format, n); });
    var out = {};
    Object.keys(map).forEach(function (f) { out[f] = Object.keys(map[f]).sort(); });
    return out;
  }

  // Formats present in the quality data and/or the static galleries (minus the
  // non-format "other" gallery group).
  function allFormats() {
    var s = {};
    Object.keys(implsByFormat()).forEach(function (f) { s[f] = 1; });
    [].forEach.call(document.querySelectorAll("[data-img-tabs] [data-format]"), function (n) {
      var f = (n.getAttribute("data-format") || "").toLowerCase();
      if (f && f !== "other") s[f] = 1;
    });
    return Object.keys(s).sort();
  }

  // Re-render everything the filters touch: the interactive quality charts (when
  // present) plus the static galleries.
  function rerenderAll() {
    if (document.getElementById("quality-app") && AGG_ALL) {
      renderRD();
      renderLossless();
      renderDecoders();
      renderBdRate();
    }
    applyGalleryFilter();
  }

  // The Tests groups depend on which formats are shown, so the bar rebuilds too.
  function onFormatChange() { renderFilterBar(); rerenderAll(); }

  // Show/hide static gallery charts (pre-rendered PNGs can't be re-plotted, only
  // shown/hidden) by their data-format; collapse a fully-empty suite section.
  function applyGalleryFilter() {
    [].forEach.call(document.querySelectorAll("[data-img-tabs]"), function (box) {
      function hiddenF(n) {
        var f = (n.getAttribute("data-format") || "").toLowerCase();
        return f && f !== "other" && state.formatsOff[f];
      }
      var tabs = [].slice.call(box.querySelectorAll('[role="tab"][data-format]'));
      var panels = [].slice.call(box.querySelectorAll('[role="tabpanel"][data-format]'));
      var anyVis = false, firstVis = -1;
      tabs.forEach(function (t, i) {
        var hide = hiddenF(t);
        t.hidden = hide;
        if (panels[i] && hide) panels[i].hidden = true;
        if (!hide) { anyVis = true; if (firstVis < 0) firstVis = i; }
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
      if (!tabs.length && figs.length) anyVis = figs.some(function (f) { return !f.hidden; });
      var section = box.closest("[data-chart-section]");
      if (section) section.hidden = !anyVis;
    });
  }

  // Keep page content clear of the fixed bar regardless of how tall it wraps.
  function syncBarPadding() {
    var bar = document.getElementById("q-filterbar");
    document.body.style.paddingBottom = (bar && !bar.hidden ? bar.offsetHeight + 24 : 0) + "px";
  }

  // Build the bar: Formats + per-format Tests + Metrics + View. Format toggles
  // reach every chart (interactive + static galleries); Tests/Metrics/View drive
  // the interactive quality charts. Rebuilt on any change that reshapes it.
  function renderFilterBar() {
    var bar = document.getElementById("q-filterbar");
    if (!bar) return;
    var fmts = allFormats();
    var hasQuality = !!document.getElementById("quality-app") && !!AGG_ALL;
    if (!fmts.length && !hasQuality) { bar.hidden = true; return; }
    bar.hidden = false;
    bar.innerHTML = "";

    var head = el("div", { class: "q-fb-row" });
    var toggle = el("button", {
      type: "button", class: "q-fb-toggle", "aria-controls": "q-fb-body",
      "aria-expanded": state.barCollapsed ? "false" : "true",
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

    function group(legendText) {
      var ff = el("fieldset", { class: "q-fieldset" });
      ff.appendChild(el("legend", null, legendText));
      body.appendChild(ff);
      return ff;
    }
    function allNone(ff, names, off, after) {
      var lg = ff.querySelector("legend");
      var a = el("button", { type: "button", class: "q-mini" }, "all");
      var n = el("button", { type: "button", class: "q-mini" }, "none");
      a.addEventListener("click", function () { names.forEach(function (k) { delete off[k]; }); after(); });
      n.addEventListener("click", function () { names.forEach(function (k) { off[k] = true; }); after(); });
      lg.appendChild(document.createTextNode(" ")); lg.appendChild(a); lg.appendChild(n);
    }
    function check(ff, label, checked, onToggle) {
      var lab = el("label", { class: "q-check" });
      var cb = el("input", { type: "checkbox" });
      cb.checked = checked;
      cb.addEventListener("change", function () { onToggle(cb.checked); });
      lab.appendChild(cb);
      lab.appendChild(document.createTextNode(" " + label));
      ff.appendChild(lab);
    }

    // Formats — apply everywhere (interactive charts + static galleries).
    if (fmts.length) {
      var ff = group("Formats");
      allNone(ff, fmts, state.formatsOff, onFormatChange);
      fmts.forEach(function (f) {
        check(ff, f.toUpperCase(), !state.formatsOff[f], function (on) {
          if (on) delete state.formatsOff[f]; else state.formatsOff[f] = true;
          onFormatChange();
        });
      });
    }

    // Tests (implementations) for each shown format — drive the interactive charts.
    var ibf = implsByFormat();
    Object.keys(ibf).sort().forEach(function (fmt) {
      if (state.formatsOff[fmt]) return;   // hidden-format groups self-prune
      var impls = ibf[fmt];
      if (!impls.length) return;
      var tf = group(fmt.toUpperCase() + " tests");
      allNone(tf, impls, state.implsOff, function () { rerenderAll(); renderFilterBar(); });
      impls.forEach(function (n) {
        check(tf, n, !state.implsOff[n], function (on) {
          if (on) delete state.implsOff[n]; else state.implsOff[n] = true;
          rerenderAll();
        });
      });
    });

    if (hasQuality) {
      // Metrics — which IQA charts to stack.
      var metrics = availableMetrics();
      if (metrics.length > 1) {
        var mf = group("Metrics");
        metrics.forEach(function (k) {
          check(mf, METRIC_INFO[k].name, !!state.metricsOn[k], function (on) {
            state.metricsOn[k] = on; renderRD();
          });
        });
      }
      // X axis + scale now live in the controls box (renderControls), not here.
    }

    syncBarPadding();
  }

  // ---- controls (view preset + show-time + download) -----------------------

  function renderControls() {
    var host = document.getElementById("q-controls");
    if (!host) return;
    host.innerHTML = "";

    // X-axis chooser — quality vs size / encode time / decode time. Only shown
    // when more than one axis was measured.
    var axes = availableXAxes();
    if (axes.length > 1) {
      host.appendChild(el("span", { class: "q-label" }, "Quality vs"));
      var axGroup = el("div", { class: "q-group", role: "group", "aria-label": "X axis" });
      axes.forEach(function (k) {
        var on = state.xKey === k;
        var b = el("button", { type: "button", class: on ? "active" : "", "aria-pressed": on ? "true" : "false" }, axisLabel(k));
        b.addEventListener("click", function () { setAxis(k); });
        axGroup.appendChild(b);
      });
      host.appendChild(axGroup);
    }

    // X-scale toggle — logarithmic vs linear (independent of the axis choice).
    host.appendChild(el("span", { class: "q-label" }, "Scale"));
    var scGroup = el("div", { class: "q-group", role: "group", "aria-label": "X scale" });
    [["Logarithmic", true], ["Linear", false]].forEach(function (opt) {
      var on = state.xLog === opt[1];
      var b = el("button", { type: "button", class: on ? "active" : "", "aria-pressed": on ? "true" : "false" }, opt[0]);
      b.addEventListener("click", function () { setScale(opt[1]); });
      scGroup.appendChild(b);
    });
    host.appendChild(scGroup);

    // Show-time toggle (encode-time bubbles; only meaningful on the bpp views).
    var tWrap = el("span", { class: "q-ctl" });
    var tId = "q-showtime";
    var tcb = el("input", { type: "checkbox", id: tId });
    tcb.checked = state.showTime.rd;
    tcb.addEventListener("change", function () { state.showTime.rd = tcb.checked; renderRD(); });
    var tlab = el("label", { class: "q-label", for: tId });
    tlab.appendChild(tcb);
    tlab.appendChild(document.createTextNode(" Encode-time bubbles"));
    tWrap.appendChild(tlab);
    host.appendChild(tWrap);

    // Download embedded raw metrics.
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

    host.appendChild(el("span", { class: "q-hint" }, "Tip: ← → switch format tabs; Alt+[ / Alt+] cycle the X axis."));
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
    host.innerHTML =
      '<div class="q-metric-cap">Best bits per pixel — lower is better</div>' +
      '<div id="q-lossless-bars"></div>' +
      '<div class="q-metric-cap">Size vs compression effort</div>' +
      '<div id="q-lossless-effort" class="q-chart"></div>';
    renderLosslessBars(document.getElementById("q-lossless-bars"),
      impls.filter(function (n) { return !state.implsOff[n]; }));
    renderLosslessEffort(document.getElementById("q-lossless-effort"), impls);
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
      var color = FORMAT_COLORS[r.fmt] || PALETTE[0];
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
    var dashByFmt = {};
    var series = impls.map(function (impl) {
      var d = LOSSLESS[impl];
      var n = d.points.length;
      var points = d.points.map(function (p, i) {
        return { x: n > 1 ? i / (n - 1) : 1, y: p.bpp, setting: p.value || p.label, t: p.time_s };
      });
      var di = dashByFmt[d.format] || 0;
      dashByFmt[d.format] = di + 1;
      return {
        impl: impl, color: FORMAT_COLORS[d.format] || PALETTE[0],
        dash: DASHES[di % DASHES.length], points: points,
      };
    });
    // Interactive legend (toggle a line); hidden series drop from the plot/axis
    // but stay in the legend so they can be brought back.
    var legend = interactiveLegend(
      series.map(function (s) { return { name: s.impl, label: s.impl, color: s.color }; }),
      renderLossless
    );
    var vis = series.filter(function (s) { return !state.implsOff[s.impl]; });
    if (!vis.length) {
      host.innerHTML = '<div class="q-plot"><svg role="img" aria-label="Bits per pixel versus compression effort (nothing shown)" viewBox="0 0 ' +
        VBW + " " + VBH + '"><text x="' + VBW / 2 + '" y="' + VBH / 2 +
        '" text-anchor="middle" class="q-tick">No series selected</text></svg></div>';
      host.appendChild(legend);
      return;
    }
    var allPts = [];
    vis.forEach(function (s) { s.points.forEach(function (p) { allPts.push(p); }); });
    var scale = state.showTime.lossless ? timeScale(allPts) : null;
    var ys = [];
    vis.forEach(function (s) { s.points.forEach(function (p) { ys.push(p.y); }); });
    var ymin = Math.min.apply(null, ys), ymax = Math.max.apply(null, ys);
    var yticks = linTicks(ymin, ymax, 6);
    var dymin = Math.min(ymin, yticks[0]), dymax = Math.max(ymax, yticks[yticks.length - 1]);
    if (dymax === dymin) dymax = dymin + 1;
    function sx(x) { return X0 + x * (X1 - X0); }
    function sy(y) { return Y1 - (y - dymin) / (dymax - dymin) * (Y1 - Y0); }

    var svg = [], hits = [];
    yticks.forEach(function (t) {
      if (t < dymin - 1e-9 || t > dymax + 1e-9) return;
      var y = sy(t).toFixed(1);
      svg.push('<line class="q-grid" x1="' + X0 + '" y1="' + y + '" x2="' + X1 + '" y2="' + y + '"/>');
      svg.push('<text class="q-tick" x="' + (X0 - 8) + '" y="' + (+y + 4) + '" text-anchor="end">' + esc(fmtNum(t)) + "</text>");
    });
    [[0, "low"], [1, "high"]].forEach(function (tk) {
      var x = sx(tk[0]).toFixed(1);
      svg.push('<line class="q-grid" x1="' + x + '" y1="' + Y0 + '" x2="' + x + '" y2="' + Y1 + '"/>');
      svg.push('<text class="q-tick" x="' + x + '" y="' + (Y1 + 18) + '" text-anchor="middle">' + tk[1] + "</text>");
    });
    svg.push('<line class="q-axis" x1="' + X0 + '" y1="' + Y1 + '" x2="' + X1 + '" y2="' + Y1 + '"/>');
    svg.push('<line class="q-axis" x1="' + X0 + '" y1="' + Y0 + '" x2="' + X0 + '" y2="' + Y1 + '"/>');
    svg.push('<text class="q-axis-title" x="' + ((X0 + X1) / 2) + '" y="' + (VBH - 8) +
      '" text-anchor="middle">Compression effort (low → high)</text>');
    svg.push('<text class="q-axis-title" transform="translate(16,' + ((Y0 + Y1) / 2) +
      ') rotate(-90)" text-anchor="middle">Bits per pixel (lower is better)</text>');
    vis.forEach(function (s) {
      var pts = [];
      s.points.forEach(function (p) {
        var px = sx(p.x), py = sy(p.y);
        pts.push({ x: px, y: py });
        var r = scale ? scale.r(p.t) : PT_R;
        hits.push({ sx: px, sy: py, r: r, color: s.color, impl: s.impl, setting: p.setting, bpp: p.y, t: p.t });
      });
      if (s.points.length > 1) {
        svg.push('<path class="q-line" d="' + smoothPath(pts) + '" stroke="' + s.color + '"' +
          (s.dash ? ' stroke-dasharray="' + s.dash + '"' : "") + "/>");
      }
      s.points.forEach(function (p) {
        var r = scale ? scale.r(p.t) : PT_R;
        svg.push('<circle class="q-pt" cx="' + sx(p.x).toFixed(1) + '" cy="' + sy(p.y).toFixed(1) +
          '" r="' + r.toFixed(1) + '" fill="' + s.color + '"/>');
      });
    });
    svg.push('<circle class="q-hl" r="7.5" visibility="hidden"/>');
    host.innerHTML = '<div class="q-plot"><svg role="img" aria-label="Bits per pixel versus compression effort" viewBox="0 0 ' + VBW + " " + VBH +
      '" preserveAspectRatio="xMidYMid meet">' + svg.join("") + "</svg></div>" +
      (scale ? sizeLegendHTML(scale, "encode time") : "") +
      '<div class="q-tooltip" hidden></div>';
    host.insertBefore(legend, host.children[1] || null);   // legend right after the plot

    attachScatterHover(host, hits, function (best) {
      return "<b>" + esc(best.impl) + "</b><br>" +
        '<span class="k">setting</span> ' + esc(best.setting) + "<br>" +
        '<span class="k">bpp</span> ' + best.bpp.toFixed(3) +
        (isNum(best.t) && best.t > 0 ? '<br><span class="k">encode time</span> ' + fmtTime(best.t) : "");
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
    // Format filter applies to the table + chart; the table additionally drops
    // impl-hidden decoders, while the chart keeps them in its interactive legend.
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
    var tableImpls = impls.filter(function (k) { return !state.implsOff[k]; });
    var html = '<table class="q-table"><thead><tr>' +
      '<th scope="col">Format</th><th scope="col">Decoder</th><th scope="col">Mean decode</th>' +
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
    var chart = state.showTime.decoder ? '<div id="q-decoder-chart" class="q-chart"></div>' : "";
    host.innerHTML = chart + html;
    if (state.showTime.decoder) {
      renderDecoderChart(document.getElementById("q-decoder-chart"), impls);
    }
  }

  // Speed-vs-bitrate scatter: X = input bpp, Y = one-pass decode time. Bit-exact
  // points are filled; approximate-decode points (finite PSNR vs the reference)
  // are hollow rings.
  function renderDecoderChart(host, impls) {
    if (!host) return;
    var dashByFmt = {};
    var series = impls.map(function (impl) {
      var d = DECODERS[impl];
      var byLabel = {};
      (d.points || []).forEach(function (p) {
        var a = byLabel[p.label] || (byLabel[p.label] =
          { bpp: 0, t: 0, n: 0, approx: false, worst: null, label: p.label });
        a.bpp += p.bpp; a.t += isNum(p.time_s) ? p.time_s : 0; a.n += 1;
        var notExact = p.bit_exact === false || (p.bit_exact == null && isNum(p.psnr));
        if (notExact) { a.approx = true; if (isNum(p.psnr)) a.worst = a.worst == null ? p.psnr : Math.min(a.worst, p.psnr); }
      });
      var points = Object.keys(byLabel).map(function (k) {
        var a = byLabel[k];
        return { x: a.bpp / a.n, y: a.t / a.n, label: a.label, approx: a.approx, worst: a.worst };
      }).sort(function (a, b) { return a.x - b.x; });
      var di = dashByFmt[d.format] || 0;
      dashByFmt[d.format] = di + 1;
      return {
        impl: impl, color: FORMAT_COLORS[d.format] || PALETTE[0],
        dash: DASHES[di % DASHES.length], points: points,
        approxExpected: d.approx_expected,
      };
    }).filter(function (s) { return s.points.length > 0; });
    if (!series.length) { host.innerHTML = ""; return; }
    // Interactive legend (toggle a decoder line); hidden series leave the plot but
    // stay in the legend so they can be brought back.
    var legend = interactiveLegend(
      series.map(function (s) { return { name: s.impl, label: s.impl, color: s.color }; }),
      renderDecoders
    );
    var vis = series.filter(function (s) { return !state.implsOff[s.impl]; });
    if (!vis.length) {
      host.innerHTML = '<div class="q-plot"><svg role="img" aria-label="Decode time versus input bits per pixel (nothing shown)" viewBox="0 0 ' +
        VBW + " " + VBH + '"><text x="' + VBW / 2 + '" y="' + VBH / 2 +
        '" text-anchor="middle" class="q-tick">No series selected</text></svg></div>';
      host.appendChild(legend);
      return;
    }

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
    svg.push('<text class="q-axis-title" transform="translate(16,' + ((Y0 + Y1) / 2) + ') rotate(-90)" text-anchor="middle">Decode time (lower is better)</text>');
    vis.forEach(function (s) {
      var pts = [];
      s.points.forEach(function (p) {
        var px = sx(p.x), py = sy(p.y);
        pts.push({ x: px, y: py });
        hits.push({ sx: px, sy: py, r: PT_R, color: s.color, impl: s.impl, step: p.label, bpp: p.x, t: p.y, approx: p.approx, worst: p.worst, approxExpected: s.approxExpected });
      });
      if (s.points.length > 1) {
        svg.push('<path class="q-line" d="' + smoothPath(pts) + '" stroke="' + s.color + '"' + (s.dash ? ' stroke-dasharray="' + s.dash + '"' : "") + "/>");
      }
      s.points.forEach(function (p) {
        var cx = sx(p.x).toFixed(1), cy = sy(p.y).toFixed(1);
        if (p.approx) {
          svg.push('<circle class="q-pt q-pt-approx" cx="' + cx + '" cy="' + cy + '" r="4.5" fill="#fff" stroke="' + s.color + '"/>');
        } else {
          svg.push('<circle class="q-pt" cx="' + cx + '" cy="' + cy + '" r="4.5" fill="' + s.color + '"/>');
        }
      });
    });
    svg.push('<circle class="q-hl" r="7.5" visibility="hidden"/>');
    host.innerHTML = '<div class="q-plot"><svg role="img" aria-label="Decode time versus input bits per pixel" viewBox="0 0 ' + VBW + " " + VBH +
      '" preserveAspectRatio="xMidYMid meet">' + svg.join("") + "</svg></div>" +
      '<p class="q-note">Hollow markers = approximate decode (differs from the reference it is scored against); filled = bit-exact.</p>' +
      '<div class="q-tooltip" hidden></div>';
    host.insertBefore(legend, host.children[1] || null);   // legend right after the plot

    attachScatterHover(host, hits, function (best) {
      return "<b>" + esc(best.impl) + "</b><br>" +
        '<span class="k">step</span> ' + esc(best.step) + "<br>" +
        '<span class="k">input bpp</span> ' + best.bpp.toFixed(3) + "<br>" +
        '<span class="k">decode time</span> ' + fmtTime(best.t) + "<br>" +
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

  // Per-section "show time" toggle, mounted beside a section heading.
  function mountSectionToggle(mountId, key, rerender) {
    var host = document.getElementById(mountId);
    if (!host) return;
    host.innerHTML = "";
    var id = "q-secshow-" + key;
    var cb = el("input", { type: "checkbox", id: id });
    cb.checked = state.showTime[key];
    cb.addEventListener("change", function () { state.showTime[key] = cb.checked; rerender(); });
    var lab = el("label", { class: "q-label", for: id });
    lab.appendChild(cb);
    lab.appendChild(document.createTextNode(" Show time"));
    host.appendChild(lab);
  }

  // ---- init ----------------------------------------------------------------

  // Generic tabbed image galleries (performance / scaling / effort): the static
  // SVGs are grouped into ARIA tabs by report.py so each is full browser width.
  // Reuses the same roving-tabindex + arrow-key pattern as the quality tabs.
  // ---- per-point image lightbox -------------------------------------------
  // Clicking a data point opens the exact images aggregated into it (the run's
  // assets/, referenced by relative URL — never embedded, so report.html stays
  // small and a multi-GB tree loads only the on-screen thumbnails, lazily).

  var _lb = null;   // the open lightbox, or null

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

  function closeLightbox() {
    if (!_lb) return;
    document.removeEventListener("keydown", _lb.onKey, true);
    if (_lb.overlay.parentNode) _lb.overlay.parentNode.removeChild(_lb.overlay);
    document.body.classList.remove("q-lb-open");
    if (_lb.prevFocus && _lb.prevFocus.focus) _lb.prevFocus.focus();
    _lb = null;
  }

  function openLightbox(fmt, impl, label) {
    var rows = METRICS.filter(function (m) {
      return m.format === fmt && m.impl === impl && m.label === label && m.asset_path;
    });
    if (!rows.length) return;
    closeLightbox();
    var prevFocus = document.activeElement;
    var showOrig = false;

    var overlay = el("div", { class: "q-lightbox", role: "dialog", "aria-modal": "true",
      "aria-label": impl + " — " + fmt + " " + label + " images" });
    var dialog = el("div", { class: "q-lb-dialog" });
    overlay.appendChild(dialog);

    var head = el("div", { class: "q-lb-head" });
    head.appendChild(el("h3", { class: "q-lb-title" },
      impl + " · " + fmt.toUpperCase() + " · " + label +
      " (" + rows.length + " image" + (rows.length === 1 ? "" : "s") + ")"));
    var actions = el("div", { class: "q-lb-actions" });
    var hasSources = rows.some(function (r) { return r.source_asset; });
    var toggle = null;
    if (hasSources) {
      toggle = el("button", { type: "button", class: "q-lb-toggle", "aria-pressed": "false" }, "Show original");
      toggle.addEventListener("click", function () {
        showOrig = !showOrig;
        toggle.textContent = showOrig ? "Show reconstruction" : "Show original";
        toggle.setAttribute("aria-pressed", showOrig ? "true" : "false");
        applyMode();
      });
      actions.appendChild(toggle);
    }
    var closeBtn = el("button", { type: "button", class: "q-lb-close", "aria-label": "Close gallery" }, "✕");
    closeBtn.addEventListener("click", closeLightbox);
    actions.appendChild(closeBtn);
    head.appendChild(actions);
    dialog.appendChild(head);

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
    dialog.appendChild(grid);

    function applyMode() {
      cells.forEach(function (c) {
        var src = (showOrig && c.row.source_asset) ? c.row.source_asset : c.row.asset_path;
        var err = c.fig.querySelector(".q-lb-err"); if (err) err.parentNode.removeChild(err);
        c.img.style.display = "";
        c.img.alt = (showOrig ? "Original of " : "Reconstruction of ") + shortName(c.row.source_path || c.row.name);
        if (c.img.getAttribute("src") !== src) c.img.setAttribute("src", src);
      });
    }

    overlay.addEventListener("click", function (ev) { if (ev.target === overlay) closeLightbox(); });
    document.body.appendChild(overlay);
    document.body.classList.add("q-lb-open");
    _lb = { overlay: overlay, prevFocus: prevFocus, onKey: function (ev) {
      if (ev.key === "Escape") { ev.preventDefault(); closeLightbox(); }
      else if (ev.key === "Tab") { trapFocus(ev, dialog); }
    } };
    document.addEventListener("keydown", _lb.onKey, true);
    (toggle || closeBtn).focus();
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
    // Build the bar up front so the format filter governs the static galleries
    // even in a perf-only bundle (no quality app); it is rebuilt below with the
    // impl/metric/view groups once the quality data resolves.
    renderFilterBar();
    applyGalleryFilter();
    if (!document.getElementById("quality-app")) return;
    AGG_ALL = aggregateAll(validRows(METRICS));
    availableMetrics().forEach(function (k) { state.metricsOn[k] = true; });

    renderControls();
    renderAggregationNote();
    renderFilterBar();
    renderRD();
    renderLossless();
    renderDecoders();
    renderBdRate();
    mountSectionToggle("q-toggle-lossless", "lossless", renderLossless);
    mountSectionToggle("q-toggle-decoder", "decoder", renderDecoders);

    // Alt+[ / Alt+] cycle the X axis from anywhere (the button group also takes
    // direct clicks; this is the global accelerator).
    document.addEventListener("keydown", function (e) {
      if (!e.altKey) return;
      var axes = availableXAxes();
      var i = axes.indexOf(state.xKey); if (i < 0) i = 0;
      if (e.key === "]") { e.preventDefault(); setAxis(axes[(i + 1) % axes.length]); }
      else if (e.key === "[") { e.preventDefault(); setAxis(axes[(i - 1 + axes.length) % axes.length]); }
    });
    window.addEventListener("resize", syncBarPadding);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
