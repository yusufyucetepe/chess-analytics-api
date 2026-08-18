// Two charts, drawn from a JSON block the server already rendered into the
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

  var palette = ["#2563eb", "#059669", "#d97706", "#dc2626", "#7c3aed", "#0891b2"];

  Chart.defaults.font.family = css.getPropertyValue("--font").trim() ||
    "system-ui, sans-serif";
  Chart.defaults.color = muted;
  Chart.defaults.plugins.legend.labels.boxWidth = 12;

  function playerType(spec) {
    var canvas = document.getElementById("chart-player-type");
    if (!canvas || !spec || !spec.values || !spec.values.length) return;
    var card = css.getPropertyValue("--card").trim() || "#ffffff";
    new Chart(canvas, {
      type: "doughnut",
      data: {
        labels: spec.labels,
        datasets: [{
          data: spec.values,
          backgroundColor: spec.labels.map(function (axis) {
            // The same three variables the key beside the chart is coloured
            // with, so the two can never drift apart.
            return css.getPropertyValue("--" + axis).trim();
          }),
          // The gap between slices is the card showing through, which is why
          // this is a colour rather than a spacing option.
          borderColor: card,
          borderWidth: 3,
          hoverOffset: 6
        }]
      },
      options: {
        responsive: true,
        // A square it sizes itself, rather than filling a container whose own
        // height comes from the canvas -- that circle resolves to zero and the
        // chart never appears.
        maintainAspectRatio: true,
        aspectRatio: 1,
        cutout: "62%",
        // The key under the canvas already names the three axes and gives the
        // numbers; a legend would say it a second time.
        plugins: {
          legend: { display: false },
          tooltip: {
            callbacks: {
              label: function (item) { return item.label + " " + item.parsed; }
            }
          }
        }
      }
    });
  }

  function rating(spec) {
    var canvas = document.getElementById("chart-rating");
    if (!canvas || !spec || !spec.series || !spec.series.length) return;
    new Chart(canvas, {
      type: "line",
      data: {
        labels: spec.labels,
        datasets: spec.series.map(function (line, i) {
          var colour = palette[i % palette.length];
          return {
            label: line.label || line.perf,
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

  playerType(data.player_type);
  rating(data.rating);
})();
