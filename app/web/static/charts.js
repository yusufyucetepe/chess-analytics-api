// Three charts, drawn from a JSON block the server already rendered into the
// page. Nothing here fetches: by the time this runs the report is fully
// present, and a chart that needed a round trip would be a second way for the
// page to fail.
(function () {
  "use strict";

  var el = document.getElementById("chart-data");
  if (!el || typeof Chart === "undefined") return;

  var data = JSON.parse(el.textContent);
  var css = getComputedStyle(document.documentElement);
  var ink = css.getPropertyValue("--ink").trim() || "#1c1917";
  var muted = css.getPropertyValue("--muted").trim() || "#78716c";
  var grid = css.getPropertyValue("--rule").trim() || "#e7e5e4";
  var accent = css.getPropertyValue("--accent").trim() || "#2563eb";

  var palette = ["#2563eb", "#059669", "#d97706", "#dc2626", "#7c3aed", "#0891b2"];

  Chart.defaults.font.family = css.getPropertyValue("--font").trim() ||
    "system-ui, sans-serif";
  Chart.defaults.color = muted;
  Chart.defaults.plugins.legend.labels.boxWidth = 12;

  function hours(spec) {
    var canvas = document.getElementById("chart-hours");
    if (!canvas || !spec.games.length) return;
    new Chart(canvas, {
      data: {
        labels: spec.labels,
        datasets: [
          {
            type: "bar",
            label: "Games",
            data: spec.games,
            backgroundColor: accent,
            borderRadius: 3,
            yAxisID: "y",
            order: 2
          },
          {
            type: "line",
            label: "Win rate %",
            data: spec.win_rate,
            borderColor: "#d97706",
            backgroundColor: "#d97706",
            borderWidth: 2,
            pointRadius: 2,
            tension: 0.3,
            // An hour with no games has no win rate; leave the gap rather than
            // drawing a line down to zero and inventing a bad hour.
            spanGaps: false,
            yAxisID: "rate",
            order: 1
          }
        ]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: "index", intersect: false },
        scales: {
          x: { grid: { display: false }, title: { display: true, text: "Hour (UTC)", color: muted } },
          y: { beginAtZero: true, grid: { color: grid }, title: { display: true, text: "Games", color: muted } },
          rate: {
            position: "right",
            beginAtZero: true,
            max: 100,
            grid: { display: false },
            ticks: { callback: function (v) { return v + "%"; } }
          }
        }
      }
    });
  }

  function rating(spec) {
    var canvas = document.getElementById("chart-rating");
    if (!canvas || !spec.series.length) return;
    new Chart(canvas, {
      type: "line",
      data: {
        labels: spec.labels,
        datasets: spec.series.map(function (line, i) {
          var colour = palette[i % palette.length];
          return {
            label: line.perf,
            data: line.points,
            borderColor: colour,
            backgroundColor: colour,
            borderWidth: 2,
            pointRadius: 2,
            tension: 0.3,
            // Here the gaps are joined: a month without a bullet game does not
            // mean the rating went anywhere, so the line carries across.
            spanGaps: true
          };
        })
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: "index", intersect: false },
        scales: {
          x: { grid: { display: false } },
          y: { grid: { color: grid }, title: { display: true, text: "Rating", color: ink } }
        }
      }
    });
  }

  hours(data.hours);
  rating(data.rating);
})();
