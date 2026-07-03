/* Device dashboard: discovers panels from the API and renders them.
   No sensor list is hardcoded here — whatever the device has reported becomes
   a panel. Line charts show history; gauges show the latest value. */
(function () {
  "use strict";

  var root = document.getElementById("panels");
  var overviewEl = document.getElementById("overview");
  if (!root || !overviewEl || typeof echarts === "undefined") return;

  var uuid = overviewEl.dataset.uuid;
  var base = overviewEl.dataset.base || "";   // URL prefix, e.g. "/dashboard"
  var currentHours = parseInt(overviewEl.dataset.defaultHours, 10) || 168;
  var charts = [];   // { key, type, inst, el }

  // Pull themeable colors from CSS so charts match light/dark automatically.
  function cssVar(name, fallback) {
    var v = getComputedStyle(document.body).getPropertyValue(name).trim();
    return v || fallback;
  }
  function theme() {
    return {
      series: cssVar("--series-1", "#2a78d6"),
      text: cssVar("--text-secondary", "#52514e"),
      muted: cssVar("--text-muted", "#8a897f"),
      border: cssVar("--border", "#e3e3df"),
      surface: cssVar("--surface-1", "#fcfcfb"),
      good: cssVar("--good", "#0ca30c"),
      warning: cssVar("--warning", "#fab219"),
      critical: cssVar("--critical", "#d03b3b"),
    };
  }

  function fmt(v, unit) {
    if (v === null || v === undefined) return "—";
    var n = Math.abs(v) >= 100 ? v.toFixed(0) : v.toFixed(2).replace(/\.?0+$/, "");
    return n + (unit ? " " + unit : "");
  }

  function buildOverview(sensors) {
    overviewEl.innerHTML = "";
    sensors.forEach(function (m) {
      var tile = document.createElement("div");
      tile.className = "stat";
      var label = document.createElement("div");
      label.className = "stat-label";
      label.textContent = m.label;
      var value = document.createElement("div");
      value.className = "stat-value";
      value.textContent = fmt(m.latest, m.unit);
      tile.appendChild(label);
      tile.appendChild(value);
      overviewEl.appendChild(tile);
    });
  }

  function buildPanel(meta) {
    var panel = document.createElement("div");
    panel.className = "panel" + (meta.chart === "gauge" ? " gauge" : "");

    var head = document.createElement("div");
    head.className = "panel-head";
    var title = document.createElement("span");
    title.className = "panel-title";
    title.textContent = meta.label;
    var latest = document.createElement("span");
    latest.className = "panel-latest";
    latest.textContent = fmt(meta.latest, meta.unit);
    head.appendChild(title);
    head.appendChild(latest);

    var chartEl = document.createElement("div");
    chartEl.className = "panel-chart";

    panel.appendChild(head);
    panel.appendChild(chartEl);
    root.appendChild(panel);

    var inst = echarts.init(chartEl, null, { renderer: "canvas" });
    var entry = { key: meta.key, type: meta.chart, inst: inst, meta: meta };
    charts.push(entry);

    if (meta.chart === "gauge") {
      renderGauge(entry);
    } else {
      loadSeries(entry);
    }
  }

  function renderGauge(entry) {
    var t = theme();
    var m = entry.meta;
    var min = (m.min === null || m.min === undefined) ? 0 : m.min;
    var max = (m.max === null || m.max === undefined) ? 100 : m.max;
    entry.inst.setOption({
      series: [{
        type: "gauge",
        min: min,
        max: max,
        startAngle: 210,
        endAngle: -30,
        progress: { show: true, width: 10, itemStyle: { color: t.series } },
        axisLine: { lineStyle: { width: 10, color: [[1, t.border]] } },
        axisTick: { show: false },
        splitLine: { length: 8, lineStyle: { color: t.muted } },
        axisLabel: { color: t.muted, fontSize: 10, distance: 12 },
        pointer: { width: 4, itemStyle: { color: t.series } },
        anchor: { show: true, size: 8, itemStyle: { color: t.series } },
        detail: {
          valueAnimation: true,
          formatter: function (v) { return fmt(v, m.unit); },
          color: t.text,
          fontSize: 18,
          offsetCenter: [0, "70%"],
        },
        data: [{ value: m.latest }],
      }],
    });
  }

  function loadSeries(entry) {
    var t = theme();
    var m = entry.meta;
    entry.inst.showLoading({ text: "", color: t.series, maskColor: "transparent" });
    fetch(base + "/api/device/" + encodeURIComponent(uuid) +
          "/series?sensor=" + encodeURIComponent(m.key) + "&hours=" + currentHours)
      .then(function (r) { return r.json(); })
      .then(function (data) {
        entry.inst.hideLoading();
        entry.inst.setOption({
          grid: { left: 46, right: 14, top: 14, bottom: 24 },
          tooltip: {
            trigger: "axis",
            axisPointer: { type: "cross", label: { backgroundColor: t.series } },
            valueFormatter: function (v) { return fmt(v, m.unit); },
          },
          xAxis: {
            type: "time",
            axisLine: { lineStyle: { color: t.border } },
            axisLabel: { color: t.muted, fontSize: 10, hideOverlap: true },
          },
          yAxis: {
            type: "value",
            scale: true,
            name: m.unit || "",
            nameTextStyle: { color: t.muted, fontSize: 10, align: "left" },
            axisLabel: { color: t.muted, fontSize: 10 },
            splitLine: { lineStyle: { color: t.border, opacity: 0.5 } },
          },
          series: [{
            type: "line",
            name: m.label,
            showSymbol: false,
            smooth: false,
            lineStyle: { width: 2, color: t.series },
            itemStyle: { color: t.series },
            areaStyle: {
              opacity: 0.12,
              color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
                { offset: 0, color: t.series },
                { offset: 1, color: "transparent" },
              ]),
            },
            data: data.points,
          }],
        }, true);
      })
      .catch(function () { entry.inst.hideLoading(); });
  }

  function reloadLineCharts() {
    charts.forEach(function (e) { if (e.type !== "gauge") loadSeries(e); });
  }

  // --- range control ---
  var rangeCtl = document.getElementById("range-control");
  if (rangeCtl) {
    rangeCtl.addEventListener("click", function (ev) {
      var btn = ev.target.closest("button");
      if (!btn) return;
      currentHours = parseInt(btn.dataset.hours, 10);
      rangeCtl.querySelectorAll("button").forEach(function (b) {
        b.classList.toggle("active", b === btn);
      });
      reloadLineCharts();
    });
  }

  window.addEventListener("resize", function () {
    charts.forEach(function (e) { e.inst.resize(); });
  });

  // --- boot ---
  fetch(base + "/api/device/" + encodeURIComponent(uuid) + "/sensors")
    .then(function (r) {
      if (!r.ok) throw new Error("not found");
      return r.json();
    })
    .then(function (data) {
      var empty = document.getElementById("panels-empty");
      if (empty) empty.remove();
      if (!data.sensors || !data.sensors.length) {
        root.innerHTML = '<p class="muted">No data reported for this device yet.</p>';
        return;
      }
      buildOverview(data.sensors);
      data.sensors.forEach(buildPanel);
    })
    .catch(function () {
      root.innerHTML = '<p class="muted">Device not found or no data yet.</p>';
    });
})();
