"""The openings section: what the player actually plays, and how it goes.

The repertoire tree is the one thing here that cannot be a pure aggregate, so
Postgres collapses the games into distinct opening lines first. A year of blitz
is tens of thousands of games but only a few hundred distinct lines, and the
tree is built from those.
"""

from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Any, NamedTuple

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.models import Color, Game
from app.report.sections._common import record, result_counts, score, window


class TreeRow(NamedTuple):
    """One distinct opening line and how the games down it went."""

    color: str
    line: Sequence[str]
    games: int
    wins: int
    draws: int
    losses: int


async def build(
    session: AsyncSession, player_id: int, *, since: datetime, until: datetime
) -> dict[str, Any]:
    """Top openings per colour, the best and worst scoring, and a repertoire tree."""
    entries = await _by_opening(session, player_id, since, until)
    lines = await _by_line(session, player_id, since, until)
    ranked = [e for e in entries if e["games"] >= settings.report_opening_min_games]

    return {
        "distinct_ecos": len({e["eco"] for e in entries}),
        "by_color": {color.value: _for_color(entries, color.value) for color in Color},
        "best": max(ranked, key=_ranking) if ranked else None,
        "worst": min(ranked, key=_ranking) if ranked else None,
        "min_games_for_ranking": settings.report_opening_min_games,
        "tree": {
            "plies": settings.report_tree_plies,
            "min_games": settings.report_tree_min_games,
            **{
                color.value: build_tree(
                    (row for row in lines if row.color == color.value),
                    min_games=settings.report_tree_min_games,
                )
                for color in Color
            },
        },
    }


def build_tree(rows: Iterable[TreeRow], *, min_games: int) -> list[dict[str, Any]]:
    """Fold pre-aggregated opening lines into a move tree.

    Each row contributes its counts to every prefix of its line, so a node's
    totals are those of the whole subtree beneath it. Rare lines are dropped:
    they are most of the JSON and none of the insight.
    """
    root = _blank("")
    for row in rows:
        node = root
        for san in row.line or []:
            node = node["children"].setdefault(san, _blank(san))
            node["games"] += row.games
            node["wins"] += row.wins
            node["draws"] += row.draws
            node["losses"] += row.losses
    return _prune(root, min_games)


def _blank(san: str) -> dict[str, Any]:
    return {"san": san, "games": 0, "wins": 0, "draws": 0, "losses": 0, "children": {}}


def _prune(node: dict[str, Any], min_games: int) -> list[dict[str, Any]]:
    children = [
        {
            "san": child["san"],
            "games": child["games"],
            "wins": child["wins"],
            "draws": child["draws"],
            "losses": child["losses"],
            "score": score(child["wins"], child["draws"], child["games"]),
            "children": _prune(child, min_games),
        }
        for child in node["children"].values()
        if child["games"] >= min_games
    ]
    children.sort(key=lambda child: (-child["games"], child["san"]))
    return children


def _ranking(entry: dict[str, Any]) -> tuple[float, int]:
    # Games break score ties, so the opening with more evidence behind it wins.
    return entry["score"], entry["games"]


def _for_color(entries: list[dict[str, Any]], color: str) -> dict[str, Any]:
    mine = [e for e in entries if e["color"] == color]
    return {
        "games": sum(e["games"] for e in mine),
        "distinct_ecos": len({e["eco"] for e in mine}),
        "top": sorted(mine, key=lambda e: (-e["games"], e["name"] or ""))[
            : settings.report_top_openings
        ],
    }


async def _by_opening(
    session: AsyncSession, player_id: int, since: datetime, until: datetime
) -> list[dict[str, Any]]:
    stmt = (
        select(
            Game.color,
            Game.eco,
            Game.opening_name,
            func.count().label("games"),
            *result_counts(),
        )
        .where(*window(player_id, since, until), Game.eco.is_not(None))
        .group_by(Game.color, Game.eco, Game.opening_name)
    )
    rows = (await session.execute(stmt)).all()
    return [
        {
            "eco": row.eco,
            "name": row.opening_name,
            "color": row.color.value,
            **record(row),
        }
        for row in rows
    ]


async def _by_line(
    session: AsyncSession, player_id: int, since: datetime, until: datetime
) -> list[TreeRow]:
    # Postgres array slices are 1-based and inclusive, so [1:n] is the first n.
    line = Game.opening_line[1 : settings.report_tree_plies].label("line")
    stmt = (
        select(Game.color, line, func.count().label("games"), *result_counts())
        .where(*window(player_id, since, until), func.cardinality(Game.opening_line) > 0)
        .group_by(Game.color, line)
    )
    rows = (await session.execute(stmt)).all()
    return [
        TreeRow(row.color.value, row.line, row.games, row.wins, row.draws, row.losses)
        for row in rows
    ]
