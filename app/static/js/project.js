/* Project dashboard: one chart per sensor, one line per device in the project.
   Sensors are discovered from the data; devices are colored from the validated
   categorical palette. */
(function () {
  "use strict";

  var root = document.getElementById("project-charts");
  if (!root || typeof echarts === "undefined") return;

  var slug = root.dataset.slug;
  var currentHours = parseInt(root.dataset.defaultHours, 10) || 168;
  var charts = [];   // { key, inst, meta }

  // Validated categorical palette (data-viz defaults), light + dark sets.
  var PALETTE_LIGHT = ["#2a78d6", "#1baf7a", "#eda100", "#008300", "#4a3aa7", "#e34948", "#e87ba4", "#eb6834"];
  var PALETTE_DARK  = ["#3987e5", "#199e70", "#c98500", "#008300", "#9085e9", "#e66767", "#d55181", "#d95926"];
  function palette() {
    var dark = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
    return dark ? PALETTE_DARK : PALETTE_LIGHT;
  }

  function cssVar(name, fallback) {
    var v = getComputedStyle(document.body).getPropertyValue(name).trim();
    return v || fallback;
  }
  function theme() {
    return {
      text: cssVar("--text-secondary", "#52514e"),
      muted: cssVar("--text-muted", "#8a897f"),
      border: cssVar("--border", "#e3e3df"),
    };
  }
  function fmt(v, unit) {
    if (v === null || v === undefined) return "—";
    var n = Math.abs(v) >= 100 ? v.toFixed(0) : v.toFixed(2).replace(/\.?0+$/, "");
    return n + (unit ? " " + unit : "");
  }

  function buildPanel(meta) {
    var panel = document.createElement("div");
    panel.className = "panel";
    var head = document.createElement("div");
    head.className = "panel-head";
    var title = document.createElement("span");
    title.className = "panel-title";
    title.textContent = meta.label;
    head.appendChild(title);
    var chartEl = document.createElement("div");
    chartEl.className = "panel-chart";
    panel.appendChild(head);
    panel.appendChild(chartEl);
    root.appendChild(panel);

    var entry = { key: meta.key, meta: meta, inst: echarts.init(chartEl, null, { renderer: "canvas" }) };
    charts.push(entry);
    loadSeries(entry);
  }

  function loadSeries(entry) {
    var t = theme();
    var pal = palette();
    var m = entry.meta;
    entry.inst.showLoading({ text: "", maskColor: "transparent" });
    fetch("/api/project/" + encodeURIComponent(slug) +
          "/series?sensor=" + encodeURIComponent(m.key) + "&hours=" + currentHours)
      .then(function (r) { return r.json(); })
      .then(function (data) {
        entry.inst.hideLoading();
        var names = data.series.map(function (s) { return s.device_name; });
        var series = data.series.map(function (s, i) {
          return {
            name: s.device_name,
            type: "line",
            showSymbol: false,
            lineStyle: { width: 2, color: pal[i % pal.length] },
            itemStyle: { color: pal[i % pal.length] },
            data: s.points,
          };
        });
        entry.inst.setOption({
          color: pal,
          grid: { left: 46, right: 14, top: 10, bottom: 52 },
          legend: { type: "scroll", bottom: 0, textStyle: { color: t.text, fontSize: 11 }, data: names },
          tooltip: {
            trigger: "axis",
            axisPointer: { type: "cross" },
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
          series: series,
        }, true);
      })
      .catch(function () { entry.inst.hideLoading(); });
  }

  var rangeCtl = document.getElementById("range-control");
  if (rangeCtl) {
    rangeCtl.addEventListener("click", function (ev) {
      var btn = ev.target.closest("button");
      if (!btn) return;
      currentHours = parseInt(btn.dataset.hours, 10);
      rangeCtl.querySelectorAll("button").forEach(function (b) {
        b.classList.toggle("active", b === btn);
      });
      charts.forEach(loadSeries);
    });
  }

  window.addEventListener("resize", function () {
    charts.forEach(function (e) { e.inst.resize(); });
  });

  fetch("/api/project/" + encodeURIComponent(slug) + "/sensors")
    .then(function (r) { return r.json(); })
    .then(function (data) {
      var empty = document.getElementById("charts-empty");
      if (empty) empty.remove();
      if (!data.sensors || !data.sensors.length) {
        root.innerHTML = '<p class="muted">No data reported for this project yet.</p>';
        return;
      }
      data.sensors.forEach(buildPanel);
    })
    .catch(function () {
      root.innerHTML = '<p class="muted">Could not load project data.</p>';
    });
})();
