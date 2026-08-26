/* tv_chart_render runtime: builds a Lightweight Charts v5 candlestick chart from a
   spec and overlays SMC primitives (FVG/OB boxes, BOS/CHoCH lines, killzone bands,
   optional coordinate grid) as absolutely-positioned divs on top of the canvas.

   The browser is driven headlessly by Playwright. Data uses Unix seconds (UTC) for
   times so rendering is deterministic across machines/timezones. Exposed as
   window.__tvmcp_render(spec) -> Promise<{rendered, bars}>. */
(function () {
  function niceStep(rough) {
    var mag = Math.pow(10, Math.floor(Math.log10(rough)));
    var norm = rough / mag;
    var m = norm < 1.5 ? 1 : norm < 3 ? 2 : norm < 7 ? 5 : 10;
    return m * mag;
  }
  function nicePriceLevels(min, max, count) {
    if (max - min <= 0) return [min];
    var step = niceStep((max - min) / count);
    var out = [];
    for (var v = Math.ceil(min / step) * step; v <= max; v += step) out.push(v);
    return out;
  }
  function fmtPrice(v) {
    return v.toFixed(5).replace(/0+$/, "").replace(/\.$/, "");
  }
  function fmtTime(ts) {
    var d = new Date(ts * 1000);
    var p = function (n) { return String(n).padStart(2, "0"); };
    return p(d.getUTCHours()) + ":" + p(d.getUTCMinutes());
  }
  function label(ov, x, y, text, color) {
    var d = document.createElement("div");
    d.style.position = "absolute";
    d.style.left = x + "px";
    d.style.top = y + "px";
    d.style.font = "11px Arial";
    d.style.color = color || "#333";
    d.style.background = "rgba(255,255,255,0.85)";
    d.style.padding = "0 3px";
    d.style.lineHeight = "14px";
    d.textContent = text;
    ov.appendChild(d);
  }

  window.__tvmcp_render = function (spec) {
    return new Promise(function (resolve, reject) {
      try {
        var width = spec.width || 1200;
        var height = spec.height || 700;
        var container = document.getElementById("chart");
        container.style.width = width + "px";
        container.style.height = height + "px";
        container.innerHTML =
          '<div id="overlay" style="position:absolute;top:0;left:0;width:100%;height:100%;pointer-events:none;z-index:10;"></div>';
        var ov = document.getElementById("overlay");

        var chart = LightweightCharts.createChart(container, {
          width: width,
          height: height,
          layout: { background: { type: "solid", color: "#ffffff" }, textColor: "#444", fontFamily: "Arial" },
          grid: { vertLines: { color: "#eceff1" }, horzLines: { color: "#eceff1" } },
          rightPriceScale: { borderVisible: false },
          timeScale: { timeVisible: true, secondsVisible: false, borderVisible: false, rightOffset: 2 },
        });
        var cs = chart.addSeries(LightweightCharts.CandlestickSeries, {
          upColor: "#26a69a",
          downColor: "#ef5350",
          borderVisible: false,
          wickUpColor: "#26a69a",
          wickDownColor: "#ef5350",
        });
        cs.setData(spec.bars);
        chart.timeScale().fitContent();

        setTimeout(function () {
          try {
            drawMarkup(chart, cs, ov, spec.bars, spec.markup || [], spec.grid !== false, width, height);
            resolve({ rendered: (spec.markup || []).length, bars: spec.bars.length });
          } catch (e) {
            reject(e);
          }
        }, 80);
      } catch (e) {
        reject(e);
      }
    });
  };

  function timeIndex(bars) {
    var m = {};
    bars.forEach(function (b, i) { m[b.time] = i; });
    return m;
  }
  function xOf(chart, ts) { return chart.timeScale().timeToCoordinate(ts); }
  function yOf(cs, price) { return cs.priceToCoordinate(price); }

  function drawMarkup(chart, cs, ov, bars, markup, grid, width, height) {
    var idx = timeIndex(bars);
    function nextTime(ts) {
      var i = idx[ts];
      return i !== undefined && i + 1 < bars.length ? bars[i + 1].time : ts;
    }

    for (var k = 0; k < markup.length; k++) {
      var m = markup[k];
      if (m.type === "fvg" || m.type === "ob") {
        var x1 = xOf(chart, m.time);
        var x2 = xOf(chart, nextTime(m.time));
        var yT = yOf(cs, Math.max(m.top, m.bottom));
        var yB = yOf(cs, Math.min(m.top, m.bottom));
        if (x1 == null || yT == null || yB == null) continue;
        var bull = m.direction === "bullish";
        var d = document.createElement("div");
        d.style.position = "absolute";
        d.style.left = x1 + "px";
        d.style.width = Math.max(2, (x2 != null ? x2 : x1 + 15) - x1) + "px";
        d.style.top = yT + "px";
        d.style.height = Math.max(2, yB - yT) + "px";
        d.style.background = bull ? "rgba(38,166,154,0.18)" : "rgba(239,83,80,0.18)";
        d.style.border = bull ? "1px solid #26a69a" : "1px solid #ef5350";
        ov.appendChild(d);
      } else if (m.type === "line" || m.type === "bos" || m.type === "choch") {
        var y = yOf(cs, m.level);
        if (y == null) continue;
        var lx = xOf(chart, m.time);
        var c1 = document.createElement("div");
        c1.style.position = "absolute";
        c1.style.left = (lx != null ? lx : 0) + "px";
        c1.style.width = (width - (lx != null ? lx : 0)) + "px";
        c1.style.top = y + "px";
        c1.style.height = "2px";
        c1.style.background = m.type === "bos" ? "#1e88e5" : m.type === "choch" ? "#8e24aa" : "#333";
        ov.appendChild(c1);
        if (m.label) label(ov, (lx != null ? lx : 0) + 4, y - 16, m.label, c1.style.background);
      } else if (m.type === "killzone") {
        var kx1 = xOf(chart, m.start);
        var kx2 = xOf(chart, m.end);
        if (kx1 == null) continue;
        var kd = document.createElement("div");
        kd.style.position = "absolute";
        kd.style.left = kx1 + "px";
        kd.style.width = Math.max(2, (kx2 != null ? kx2 : kx1 + 15) - kx1) + "px";
        kd.style.top = "0px";
        kd.style.height = height + "px";
        kd.style.background = "rgba(33,150,243,0.12)";
        kd.style.borderLeft = "1px dashed #2196f3";
        kd.style.borderRight = "1px dashed #2196f3";
        ov.appendChild(kd);
        if (m.label) label(ov, kx1 + 4, 6, m.label, "#2196f3");
      }
    }

    if (grid) {
      var lo = Infinity, hi = -Infinity;
      for (var bi = 0; bi < bars.length; bi++) {
        lo = Math.min(lo, bars[bi].low);
        hi = Math.max(hi, bars[bi].high);
      }
      var levels = nicePriceLevels(lo, hi, 5);
      for (var li = 0; li < levels.length; li++) {
        var gy = yOf(cs, levels[li]);
        if (gy == null) continue;
        var gd = document.createElement("div");
        gd.style.position = "absolute";
        gd.style.left = "0px";
        gd.style.width = width + "px";
        gd.style.top = gy + "px";
        gd.style.height = "1px";
        gd.style.background = "rgba(0,0,0,0.12)";
        ov.appendChild(gd);
        label(ov, width - 70, gy - 14, fmtPrice(levels[li]), "#666");
      }
      var step = Math.max(1, Math.floor(bars.length / 5));
      for (var ti = 0; ti < bars.length; ti += step) {
        var gx = xOf(chart, bars[ti].time);
        if (gx == null) continue;
        var vd = document.createElement("div");
        vd.style.position = "absolute";
        vd.style.left = gx + "px";
        vd.style.top = "0px";
        vd.style.width = "1px";
        vd.style.height = height + "px";
        vd.style.background = "rgba(0,0,0,0.08)";
        ov.appendChild(vd);
        label(ov, gx + 3, height - 18, fmtTime(bars[ti].time), "#888");
      }
    }
  }
})();
