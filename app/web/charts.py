"""The slices of a report the charts need, and nothing else.

Built here rather than in the template so the page ships one small JSON block
instead of the whole payload. A year of blitz produces a repertoire tree of a
few hundred nodes and a weekly quality series, none of which any chart reads --
embedding the lot would put tens of kilobytes into the HTML to draw three lines.
"""

from typing import Any

#: One entry per hour, so the bar chart has no holes. The section already fills
#: them; this is only the labels.
HOUR_LABELS = [f"{hour:02d}" for hour in range(24)]


def chart_data(sections: dict[str, Any]) -> dict[str, Any]:
    """Everything ``static/charts.js`` draws, keyed by canvas."""
    return {
        "hours": _hours(sections.get("time_behaviour")),
        "rating": _rating(sections.get("progression")),
    }


def _hours(timing: dict[str, Any] | None) -> dict[str, Any]:
    by_hour = (timing or {}).get("by_hour") or []
    return {
        "labels": HOUR_LABELS,
        "games": [slot["games"] for slot in by_hour],
        # Win rate is drawn on its own axis, and an hour with no games has no
        # win rate -- `None` becomes `null`, which Chart.js leaves as a gap
        # rather than plotting as a zero.
        "win_rate": [
            round(slot["win_rate"] * 100, 1) if slot["games"] else None for slot in by_hour
        ],
    }


def _rating(progression: dict[str, Any] | None) -> dict[str, Any]:
    by_perf = (progression or {}).get("by_perf") or {}
    # Every month any perf was played, so the lines share one x axis even where
    # a perf has gaps in it.
    months = sorted({point["month"] for perf in by_perf.values() for point in perf["series"]})
    index = {month: position for position, month in enumerate(months)}

    series = []
    for perf, stats in by_perf.items():
        points: list[float | None] = [None] * len(months)
        for point in stats["series"]:
            points[index[point["month"]]] = point["average"]
        series.append({"perf": perf, "points": points})

    # Busiest perf first: it is the one the eye should land on, and Chart.js
    # colours datasets in order.
    series.sort(key=lambda line: -by_perf[line["perf"]]["games"])
    return {"labels": [month[:7] for month in months], "series": series}
