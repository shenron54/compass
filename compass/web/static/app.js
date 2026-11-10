(function () {
  "use strict";
  const charts = [];

  function css(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  function fmt(kind, v) {
    if (v === null || v === undefined || isNaN(v)) return "";
    if (kind === "money") return Math.round(v).toLocaleString();
    if (kind === "pct") return v.toFixed(1) + "%";
    return Math.abs(v) >= 1000 ? Math.round(v).toLocaleString() : (+v.toFixed(2)).toString();
  }

  function logScale(spec) {
    if (spec.y === "pct" || spec.series.length !== 1) return false;
    let lo = Infinity, hi = -Infinity;
    spec.series[0].points.forEach(function (p) { lo = Math.min(lo, p[1]); hi = Math.max(hi, p[1]); });
    return lo > 0 && hi / lo > 20;
  }

  function build(spec) {
    const text = css("--text"), muted = css("--muted"), line = css("--line");
    const palette = [css("--chart-1"), css("--chart-2"), css("--caution")];
    const series = spec.series.map(function (s, i) {
      const out = {
        name: s.name, type: "line", data: s.points, showSymbol: false, step: s.step ? "end" : false,
        lineStyle: { width: spec.spark ? 1.4 : 1.8, color: palette[i % palette.length] },
        itemStyle: { color: palette[i % palette.length] }, emphasis: { disabled: !!spec.spark },
      };
      if (s.area || spec.spark) out.areaStyle = { opacity: spec.spark ? 0.08 : 0.15 };
      return out;
    });
    if (spec.markers && spec.markers.length) {
      series[0].markPoint = {
        symbol: "pin", symbolSize: 34, label: { fontSize: 9, color: "#fff" },
        itemStyle: { color: css("--stress") },
        data: spec.markers.map(function (m) { return { coord: [m.date, m.value], value: m.label }; }),
      };
    }
    if (spec.bands && spec.bands.length) {
      series[0].markArea = {
        silent: true, itemStyle: { color: css("--chart-band") },
        data: spec.bands.map(function (b) { return [{ xAxis: b[0] }, { xAxis: b[1] }]; }),
      };
    }
    if (spec.spark) {
      return {
        animation: false, grid: { left: 0, right: 0, top: 2, bottom: 2 },
        xAxis: { type: "time", show: false }, yAxis: { type: "value", show: false, scale: true },
        tooltip: { show: false }, series: series,
      };
    }
    return {
      animation: false, color: palette,
      grid: { left: 56, right: 18, top: spec.series.length > 1 ? 34 : 14, bottom: spec.zoom ? 62 : 30 },
      legend: spec.series.length > 1 ? { top: 0, textStyle: { color: muted } } : undefined,
      tooltip: {
        trigger: "axis",
        valueFormatter: function (v) { return fmt(spec.y, v); },
      },
      xAxis: { type: "time", axisLine: { lineStyle: { color: line } }, axisLabel: { color: muted } },
      yAxis: {
        // Decades of index history are only readable on a log scale.
        type: logScale(spec) ? "log" : "value", scale: true, splitLine: { lineStyle: { color: line } },
        axisLabel: { color: muted, formatter: function (v) { return fmt(spec.y, v); } },
      },
      dataZoom: spec.zoom ? [{ type: "inside" }, { type: "slider", height: 22, bottom: 8, textStyle: { color: text } }] : [],
      series: series,
    };
  }

  function mount(root) {
    root.querySelectorAll("[data-chart]").forEach(function (el) {
      if (el.dataset.mounted) return;
      const source = document.getElementById(el.dataset.chart);
      if (!source || !window.echarts) return;
      let spec;
      try { spec = JSON.parse(source.textContent); } catch (e) { return; }
      const empty = !spec.series.some(function (s) { return s.points && s.points.length; });
      if (empty) {
        el.classList.add("muted", "small");
        el.style.height = "auto";
        el.textContent = "No data yet.";
        return;
      }
      el.dataset.mounted = "1";
      const c = echarts.init(el, null, { renderer: "canvas" });
      c.setOption(build(spec));
      charts.push(c);
    });
  }

  window.addEventListener("resize", function () { charts.forEach(function (c) { c.resize(); }); });
  document.addEventListener("DOMContentLoaded", function () { mount(document); });
  document.addEventListener("htmx:afterSwap", function (e) { mount(e.target); });
  document.addEventListener("click", function (e) {
    const t = e.target.closest("[data-confirm]");
    if (t && !window.confirm(t.dataset.confirm)) e.preventDefault();
  });
  document.addEventListener("change", function (e) {
    const t = e.target.closest("[data-example-target]");
    if (!t) return;
    const target = document.getElementById(t.dataset.exampleTarget);
    const source = document.getElementById("example-" + t.value);
    if (target && source && (!target.value.trim() || window.confirm("Replace the text with this example?"))) {
      target.value = source.value.trim() + "\n";
    }
  });
})();
