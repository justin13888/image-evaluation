/* Interactive quality report. Reads the raw metrics embedded in report.html
   (#quality-metrics) and draws rate-distortion curves as inline SVG — no
   third-party library, no network. Everything is recomputed in the browser, so
   the embedded data is the single source of truth.

   Charts:
     - one rate-distortion chart per format (all encoders), and
     - a combined chart overlaying the Pareto-front encoders of every format.
   Plus a sortable BD-rate table. Metric (SSIMULACRA2 / PSNR / SSIM /
   Butteraugli) and linear/log-x are global toggles. */
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

  var METRIC_INFO = {
    ssimulacra2: { key: "ssimulacra2", name: "SSIMULACRA2", y: "SSIMULACRA2 (higher is better)" },
    psnr: { key: "psnr", name: "PSNR", y: "PSNR dB (higher is better)" },
    ssim: { key: "ssim", name: "SSIM", y: "SSIM (higher is better)" },
    butteraugli: { key: "butteraugli", name: "Butteraugli", y: "Butteraugli (lower is better)" },
  };

  var state = { metric: "ssimulacra2", xscale: "linear" };
  var HIDDEN = {}; // chartId -> Set of hidden series keys (persisted across re-renders)

  // ---- data helpers --------------------------------------------------------

  function readJSON(id) {
    var el = document.getElementById(id);
    if (!el) return null;
    try { return JSON.parse(el.textContent); } catch (e) { return null; }
  }
  function isNum(v) { return typeof v === "number" && isFinite(v); }

  function validRows(rows) {
    return rows.filter(function (m) {
      return m.type === "encode" && !m.error && m.bpp > 0 && isNum(m.ssimulacra2);
    });
  }

  // Aggregate to one mean point per (format, impl, quality-step) across images.
  // Points whose chosen metric is non-finite (e.g. null PSNR) are skipped.
  // Returns { fmt: [ {impl, points:[{x,y,label,q,count}]} ] }.
  function aggregate(rows, metric) {
    var byFmt = {};
    rows.forEach(function (m) {
      var y = m[metric];
      if (!isNum(y)) return;
      var impls = (byFmt[m.format] = byFmt[m.format] || {});
      var steps = (impls[m.impl] = impls[m.impl] || {});
      var s = (steps[m.label] = steps[m.label] ||
        { bpp: 0, y: 0, n: 0, label: m.label, q: m.quality_value });
      s.bpp += m.bpp; s.y += y; s.n += 1;
    });
    var out = {};
    Object.keys(byFmt).forEach(function (fmt) {
      out[fmt] = Object.keys(byFmt[fmt]).sort().map(function (impl) {
        var steps = byFmt[fmt][impl];
        var points = Object.keys(steps).map(function (k) {
          var s = steps[k];
          return { x: s.bpp / s.n, y: s.y / s.n, label: s.label, q: s.q, count: s.n };
        }).sort(function (a, b) { return a.x - b.x; });
        return { impl: impl, points: points };
      }).filter(function (s) { return s.points.length > 0; });
    });
    return out;
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

  // ---- chart rendering -----------------------------------------------------

  var VBW = 840, VBH = 460, ML = 66, MR = 18, MT = 14, MB = 54;
  var X0 = ML, X1 = VBW - MR, Y0 = MT, Y1 = VBH - MB;

  function esc(s) {
    return String(s).replace(/[&<>"]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
    });
  }

  // series: [{key,label,color,dash?,points:[{x,y,label,q,count}]}]
  function renderRDChart(container, series, chartId, metric) {
    var hidden = HIDDEN[chartId] || (HIDDEN[chartId] = {});
    var info = METRIC_INFO[metric];
    var log = state.xscale === "log";
    var vis = series.filter(function (s) { return !hidden[s.key]; });
    container._hits = [];

    // domain over visible points
    var xs = [], ys = [];
    vis.forEach(function (s) {
      s.points.forEach(function (p) { xs.push(p.x); ys.push(p.y); });
    });
    var plotHTML;
    if (!xs.length) {
      plotHTML = '<div class="q-plot"><svg viewBox="0 0 ' + VBW + " " + VBH +
        '"><text x="' + VBW / 2 + '" y="' + VBH / 2 +
        '" text-anchor="middle" class="q-tick">All series hidden</text></svg></div>';
    } else {
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
      var yticks = linTicks(ymin, ymax, 6);
      var dymin = Math.min(ymin, yticks[0]), dymax = Math.max(ymax, yticks[yticks.length - 1]);
      if (dymax === dymin) dymax = dymin + 1;

      var lx0 = log ? Math.log10(dxmin) : dxmin, lx1 = log ? Math.log10(dxmax) : dxmax;
      function sx(x) {
        var t = ((log ? Math.log10(x) : x) - lx0) / (lx1 - lx0 || 1);
        return X0 + t * (X1 - X0);
      }
      function sy(y) { return Y1 - (y - dymin) / (dymax - dymin) * (Y1 - Y0); }

      var svg = [];
      // gridlines + ticks
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
        svg.push('<text class="q-tick" x="' + (X0 - 8) + '" y="' + (+y + 4) + '" text-anchor="end">' + esc(fmtNum(t)) + "</text>");
      });
      // axes
      svg.push('<line class="q-axis" x1="' + X0 + '" y1="' + Y1 + '" x2="' + X1 + '" y2="' + Y1 + '"/>');
      svg.push('<line class="q-axis" x1="' + X0 + '" y1="' + Y0 + '" x2="' + X0 + '" y2="' + Y1 + '"/>');
      // axis titles
      svg.push('<text class="q-axis-title" x="' + ((X0 + X1) / 2) + '" y="' + (VBH - 8) + '" text-anchor="middle">Bits per pixel (bpp)' + (log ? " — log" : "") + "</text>");
      svg.push('<text class="q-axis-title" transform="translate(16,' + ((Y0 + Y1) / 2) + ') rotate(-90)" text-anchor="middle">' + esc(info.y) + "</text>");

      // series + collect hit-test points
      var hits = [];
      vis.forEach(function (s) {
        var d = "";
        s.points.forEach(function (p, i) {
          var px = sx(p.x), py = sy(p.y);
          d += (i ? "L" : "M") + px.toFixed(1) + " " + py.toFixed(1) + " ";
          hits.push({ sx: px, sy: py, color: s.color, label: s.label, x: p.x, y: p.y, q: p.q, step: p.label, count: p.count });
        });
        svg.push('<path class="q-line" d="' + d + '" stroke="' + s.color + '"' + (s.dash ? ' stroke-dasharray="' + s.dash + '"' : "") + "/>");
        s.points.forEach(function (p) {
          svg.push('<circle class="q-pt" cx="' + sx(p.x).toFixed(1) + '" cy="' + sy(p.y).toFixed(1) + '" r="4.5" fill="' + s.color + '"/>');
        });
      });
      svg.push('<circle class="q-hl" r="7.5" visibility="hidden"/>');

      plotHTML = '<div class="q-plot"><svg viewBox="0 0 ' + VBW + " " + VBH +
        '" preserveAspectRatio="xMidYMid meet">' + svg.join("") + "</svg></div>";
      container._hits = hits;
    }

    // legend
    var chips = series.map(function (s) {
      return '<span class="q-chip' + (hidden[s.key] ? " off" : "") + '" data-key="' + esc(s.key) +
        '"><span class="sw" style="background:' + s.color + '"></span>' + esc(s.label) + "</span>";
    }).join("");

    container.innerHTML = plotHTML + '<div class="q-legend">' + chips + "</div>" +
      '<div class="q-tooltip" hidden></div>';

    // legend toggles
    container.querySelectorAll(".q-chip").forEach(function (chip) {
      chip.addEventListener("click", function () {
        var k = chip.getAttribute("data-key");
        if (hidden[k]) delete hidden[k]; else hidden[k] = 1;
        renderRDChart(container, series, chartId);
      });
    });

    // hover
    var svgEl = container.querySelector("svg");
    var tip = container.querySelector(".q-tooltip");
    var hl = container.querySelector(".q-hl");
    if (svgEl && container._hits && container._hits.length) {
      svgEl.addEventListener("mousemove", function (ev) {
        var r = svgEl.getBoundingClientRect();
        var vx = (ev.clientX - r.left) * (VBW / r.width);
        var vy = (ev.clientY - r.top) * (VBH / r.height);
        var best = null, bd = 1e9;
        container._hits.forEach(function (h) {
          var dd = (h.sx - vx) * (h.sx - vx) + (h.sy - vy) * (h.sy - vy);
          if (dd < bd) { bd = dd; best = h; }
        });
        if (best && bd <= 26 * 26) {
          hl.setAttribute("cx", best.sx); hl.setAttribute("cy", best.sy);
          hl.setAttribute("stroke", best.color); hl.setAttribute("visibility", "visible");
          var crect = container.getBoundingClientRect();
          tip.hidden = false;
          tip.innerHTML = "<b>" + esc(best.label) + "</b><br>" +
            '<span class="k">step</span> ' + esc(best.step) + "<br>" +
            '<span class="k">bpp</span> ' + best.x.toFixed(3) + "<br>" +
            '<span class="k">' + esc(info.name) + "</span> " + best.y.toFixed(2) +
            (best.count > 1 ? '<br><span class="k">images</span> ' + best.count : "");
          var tx = ev.clientX - crect.left + 14, ty = ev.clientY - crect.top + 12;
          if (tx + 200 > crect.width) tx = ev.clientX - crect.left - 14 - 200;
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
  }

  // ---- top-level views -----------------------------------------------------

  function renderCombined(agg) {
    var host = document.getElementById("q-combined");
    if (!host) return;
    host.innerHTML = "";
    var chart = document.createElement("div");
    chart.className = "q-chart";
    host.appendChild(chart);
    var series = [];
    Object.keys(agg).sort().forEach(function (fmt) {
      var keep = PARETO[fmt] || [];
      var di = 0;
      agg[fmt].forEach(function (s) {
        if (keep.length && keep.indexOf(s.impl) < 0) return;
        series.push({
          key: "c/" + fmt + "/" + s.impl,
          label: fmt.toUpperCase() + " · " + s.impl,
          color: FORMAT_COLORS[fmt] || PALETTE[0],
          dash: DASHES[di % DASHES.length],
          points: s.points,
        });
        di++;
      });
    });
    if (!series.length) { host.innerHTML = '<p class="q-note">No rate-distortion data.</p>'; return; }
    renderRDChart(chart, series, "combined", state.metric);
  }

  // Metrics that actually carry finite encode data. SSIMULACRA2 always present
  // (it gates validRows); the rest appear only when measured. Order follows
  // METRIC_INFO.
  function availableMetrics() {
    function hasMetric(key) {
      return METRICS.some(function (m) { return isNum(m[key]) && m.type === "encode" && !m.error; });
    }
    var metrics = ["ssimulacra2"];
    ["psnr", "ssim", "butteraugli"].forEach(function (k) {
      if (hasMetric(k)) metrics.push(k);
    });
    return metrics;
  }

  // Per format, a small-multiples grid: one rate-distortion chart per available
  // metric (each metric has its own y-scale). Independent of the global toggle.
  function renderPerFormat(rows, metrics) {
    var host = document.getElementById("q-charts");
    if (!host) return;
    host.innerHTML = "";
    // Aggregate once per metric, then pivot to per-format below.
    var aggByMetric = {};
    metrics.forEach(function (metric) { aggByMetric[metric] = aggregate(rows, metric); });
    var formats = {};
    metrics.forEach(function (metric) {
      Object.keys(aggByMetric[metric]).forEach(function (fmt) { formats[fmt] = 1; });
    });
    Object.keys(formats).sort().forEach(function (fmt) {
      var title = document.createElement("h4");
      title.className = "q-chart-title";
      title.textContent = fmt.toUpperCase();
      host.appendChild(title);
      var grid = document.createElement("div");
      grid.className = "q-metric-grid";
      host.appendChild(grid);
      metrics.forEach(function (metric) {
        var fmtAgg = aggByMetric[metric][fmt];
        if (!fmtAgg) return;
        var cell = document.createElement("div");
        cell.className = "q-metric-cell";
        var cap = document.createElement("div");
        cap.className = "q-metric-cap";
        cap.textContent = METRIC_INFO[metric].name;
        cell.appendChild(cap);
        var chart = document.createElement("div");
        chart.className = "q-chart";
        cell.appendChild(chart);
        grid.appendChild(cell);
        var series = fmtAgg.map(function (s, i) {
          return { key: fmt + "/" + s.impl, label: s.impl, color: PALETTE[i % PALETTE.length], points: s.points };
        });
        renderRDChart(chart, series, "fmt:" + fmt + ":" + metric, metric);
      });
    });
  }

  function renderAll() {
    var rows = validRows(METRICS);
    renderCombined(aggregate(rows, state.metric));
    renderPerFormat(rows, availableMetrics());
  }

  // ---- BD-rate table (sortable) -------------------------------------------

  function renderBdRate() {
    var host = document.getElementById("q-bdrate");
    if (!host) return;
    var rows = [];
    Object.keys(BDRATE).forEach(function (fmt) {
      Object.keys(BDRATE[fmt]).forEach(function (impl) {
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
        '<th data-k="fmt">Format ' + arrow("fmt") + "</th>" +
        '<th data-k="impl">Implementation ' + arrow("impl") + "</th>" +
        '<th data-k="bd">BD-rate vs ref ' + arrow("bd") + "</th></tr></thead><tbody>";
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

  // ---- controls ------------------------------------------------------------

  function group(labelText, opts, current, onPick) {
    var wrap = document.createElement("span");
    wrap.innerHTML = '<span class="q-label">' + esc(labelText) + "</span>";
    var g = document.createElement("span");
    g.className = "q-group";
    opts.forEach(function (o) {
      var b = document.createElement("button");
      b.textContent = o.label;
      if (o.value === current()) b.className = "active";
      b.addEventListener("click", function () {
        onPick(o.value);
        g.querySelectorAll("button").forEach(function (x) { x.className = ""; });
        b.className = "active";
      });
      g.appendChild(b);
    });
    wrap.appendChild(g);
    return wrap;
  }

  function renderControls() {
    var host = document.getElementById("q-controls");
    if (!host) return;
    host.innerHTML = "";
    // The metric toggle drives only the cross-format Pareto chart; the per-format
    // grid below always shows every available metric.
    var metricOpts = availableMetrics().map(function (k) {
      return { label: METRIC_INFO[k].name, value: k };
    });
    if (metricOpts.length > 1) {
      host.appendChild(group("Pareto metric", metricOpts,
        function () { return state.metric; },
        function (v) { state.metric = v; renderAll(); }));
    }
    host.appendChild(group("X-axis", [
      { label: "Linear", value: "linear" }, { label: "Log", value: "log" },
    ], function () { return state.xscale; }, function (v) { state.xscale = v; renderAll(); }));

    // download embedded raw metrics
    var dl = document.createElement("a");
    dl.className = "q-dl";
    dl.textContent = "⤓ raw metrics (JSON)";
    dl.href = "#";
    dl.addEventListener("click", function (e) {
      e.preventDefault();
      var blob = new Blob([JSON.stringify(METRICS)], { type: "application/json" });
      var url = URL.createObjectURL(blob);
      var a = document.createElement("a");
      a.href = url; a.download = "metrics.json"; a.click();
      URL.revokeObjectURL(url);
    });
    host.appendChild(dl);
  }

  function init() {
    if (!document.getElementById("quality-app")) return;
    renderControls();
    renderAll();
    renderBdRate();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
