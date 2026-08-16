"""The slices of a report the charts need, and nothing else.

Built here rather than in the template so the page ships one small JSON block
instead of the whole payload. A year of blitz produces a repertoire tree of a
few hundred nodes and a weekly quality series, none of which any chart reads --
embedding the lot would put tens of kilobytes into the HTML to draw three lines.
"""

from typing import Any

#: The order the donut's slices are drawn in, and the order the key beside it
#: lists them. Matches ``player_type_weights.AXES``, spelled out here so the
#: chart layer does not import the classifier for three strings.
AXES = ("positional", "aggressive", "tactical")


def chart_data(sections: dict[str, Any]) -> dict[str, Any]:
    """Everything ``static/charts.js`` draws, keyed by canvas."""
    return {
        "player_type": _player_type(sections.get("player_type")),
        "rating": _rating(sections.get("progression")),
    }


def _player_type(player_type: dict[str, Any] | None) -> dict[str, Any]:
    """The three scores, or nothing at all.

    ``scores`` is three nulls when no signal had anything to say, which is not
    the same as an even split -- so it draws no chart rather than a third each.
    """
    scores = (player_type or {}).get("scores") or {}
    values = [scores.get(axis) for axis in AXES]
    if any(value is None for value in values):
        return {"labels": [], "values": []}
    return {"labels": list(AXES), "values": values}


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
