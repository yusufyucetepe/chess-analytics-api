"""The openings section: what the player actually plays, and how it goes.

Two views of the same rows. The tables answer "which openings", grouped by ECO
code. The systems answer "what is your repertoire", grouped by opening family --
a player thinks in Vienna and Sicilian, not in B22 and C26 -- and each one keeps
a drill-down ranked by points lost rather than by frequency, because the line
worth reading about is the one quietly costing the year.

Neither can be a pure aggregate, so Postgres collapses the games into distinct
opening lines first. A year of blitz is tens of thousands of games but only a few
hundred distinct lines, and everything below is folded out of those.
"""

from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Any, NamedTuple

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.models import Color, Game
from app.report.sections._common import points_lost, rate, record, result_counts, score, window


class LineRow(NamedTuple):
    """One distinct opening line and how the games down it went."""

    color: str
    family: str
    line: Sequence[str]
    games: int
    wins: int
    draws: int
    losses: int


class Counts(NamedTuple):
    """What ``record`` reads. Sums over a group of lines, rather than a SQL row."""

    games: int
    wins: int
    draws: int
    losses: int


async def build(
    session: AsyncSession, player_id: int, *, since: datetime, until: datetime
) -> dict[str, Any]:
    """Top openings per colour, the best and worst scoring, and the repertoire."""
    entries = await _by_opening(session, player_id, since, until)
    lines = await _by_line(session, player_id, since, until)
    total_games = await _window_games(session, player_id, since, until)
    ranked = [e for e in entries if e["games"] >= settings.report_opening_min_games]

    systems = build_systems(
        lines,
        shown=settings.report_systems_shown,
        min_games=settings.report_system_min_games,
        branch_plies=settings.report_system_branch_plies,
        branches_shown=settings.report_system_branches_shown,
        tree_min_games=settings.report_tree_min_games,
        dominance=settings.report_system_mainline_dominance,
    )

    return {
        "distinct_ecos": len({e["eco"] for e in entries}),
        "by_color": {color.value: _for_color(entries, color.value) for color in Color},
        "best": max(ranked, key=_ranking) if ranked else None,
        "worst": min(ranked, key=_ranking) if ranked else None,
        "min_games_for_ranking": settings.report_opening_min_games,
        "systems": systems,
        "concentration": concentration(
            systems, total_games, target=settings.report_concentration_target
        ),
        # The whole sorted tree, still built and still served: it is the only
        # place the deep lines survive, and a caller that wants them should not
        # have to re-derive them from the games. The page shows the systems.
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


def build_systems(
    rows: Iterable[LineRow],
    *,
    shown: int,
    min_games: int,
    branch_plies: int,
    branches_shown: int,
    tree_min_games: int,
    dominance: float = 1.0,
) -> list[dict[str, Any]]:
    """The player's opening systems, busiest first, one per family and colour.

    Colour is part of the identity rather than a detail: the Sicilian a player
    answers as white and the Sicilian they choose as black are two different
    habits that happen to share a name.

    Families below ``min_games`` are left out entirely -- they are not systems,
    and ``concentration`` puts their games in the bucket with everything else.
    """
    grouped: dict[tuple[str, str], list[LineRow]] = {}
    for row in rows:
        # No family means Lichess had no name for the line. Those games are real
        # and stay in the totals; they just cannot be a named system.
        if row.family:
            grouped.setdefault((row.color, row.family), []).append(row)

    systems = []
    for (color, family), group in grouped.items():
        counts = Counts(
            games=sum(row.games for row in group),
            wins=sum(row.wins for row in group),
            draws=sum(row.draws for row in group),
            losses=sum(row.losses for row in group),
        )
        if counts.games < min_games:
            continue
        mainline, choices = _split_mainline(
            build_tree(group, min_games=tree_min_games), dominance=dominance
        )
        systems.append(
            {
                "name": family,
                "color": color,
                **record(counts),
                "points_lost": points_lost(counts.draws, counts.losses),
                # The moves all but a handful of games in the system share, which
                # is the line the card prints. It ends at the first real choice.
                "mainline": mainline,
                "branch_ply": len(mainline) + 1,
                "branches": _branches(choices, branch_plies, len(mainline) + 1, branches_shown),
            }
        )
    systems.sort(key=lambda system: (-system["games"], system["name"], system["color"]))
    return systems[:shown]


def concentration(
    systems: Sequence[dict[str, Any]], total_games: int, *, target: float
) -> dict[str, Any]:
    """How much of the year the named systems account for.

    The denominator is every game in the window, not every game that reached a
    named opening, so the bar cannot overstate itself: whatever the systems do
    not cover is visible as the segment on the end.
    """
    segments = [
        {
            "name": system["name"],
            "color": system["color"],
            "games": system["games"],
            "share": rate(system["games"], total_games),
        }
        for system in systems
    ]
    covered = sum(system["games"] for system in systems)
    rest = total_games - covered
    if rest > 0:
        # Unnamed rather than "everything else": the wording is the page's.
        segments.append(
            {"name": None, "color": None, "games": rest, "share": rate(rest, total_games)}
        )

    return {
        "games": total_games,
        "target": target,
        "segments": segments,
        "covering": _covering(systems, total_games, target),
    }


def _covering(systems: Sequence[dict[str, Any]], total_games: int, target: float) -> dict[str, Any]:
    """The fewest systems that add up to ``target`` of the games.

    If they never get there, it says so with the count and share it did reach --
    a scattered repertoire is a finding, not a missing value.
    """
    running = 0
    for index, system in enumerate(systems, start=1):
        running += system["games"]
        if total_games and running / total_games >= target:
            return {"systems": index, "share": rate(running, total_games), "reached": True}
    return {
        "systems": len(systems),
        "share": rate(running, total_games),
        "reached": False,
    }


def _split_mainline(
    tree: list[dict[str, Any]], *, dominance: float = 1.0
) -> tuple[list[str], list[dict[str, Any]]]:
    """Split a system's tree into the shared part and the first real choice.

    Walking down the shared moves is what makes the drill-down readable: nobody
    needs to be told their Vienna started 1.e4 e5 2.Nc3 two hundred times over.
    What follows is where the games actually diverge.

    A move that all but ``dominance`` of the games at its level played counts as
    shared. Transpositions are why: a couple of games arriving at the Vienna by
    another order are a branch in the data and not one in the repertoire, and
    stopping at them leaves a mainline of "1.e4" and a drill-down whose top row
    is the entire system. Those games keep their place in every total; what they
    lose is a row of their own.
    """
    mainline: list[str] = []
    nodes = tree
    while nodes:
        played = sum(node["games"] for node in nodes)
        # The tree sorts children by games, so the first is the busiest.
        if len(nodes) > 1 and (not played or nodes[0]["games"] / played < dominance):
            break
        mainline.append(nodes[0]["san"])
        nodes = nodes[0]["children"]
    return mainline, nodes


def _branches(
    nodes: list[dict[str, Any]], plies: int, first_ply: int, shown: int
) -> list[dict[str, Any]]:
    """The continuations ``plies`` deep that the player actually meets, best first.

    Which ones to keep and how to order them are two separate questions, and
    answering both with one sort gets one of them wrong. The rows worth showing
    are the busiest -- a line met twice all year is not a hole in a repertoire --
    and once chosen they read best score to worst, so the list ends on the one
    going badly. Ordering by score before cutting would keep the five best and
    drop exactly the lines the drill-down exists to show.

    Only the deepest node on each path is reported. A parent's counts include
    its children's, so listing both would charge the same losses twice and the
    ranking would be a list of prefixes of itself.
    """
    branches: list[dict[str, Any]] = []

    def walk(node: dict[str, Any], depth: int, path: list[str]) -> None:
        path = [*path, node["san"]]
        if depth == plies or not node["children"]:
            branches.append(
                {
                    "sans": path,
                    "ply": first_ply,
                    "games": node["games"],
                    "wins": node["wins"],
                    "draws": node["draws"],
                    "losses": node["losses"],
                    "score": node["score"],
                    "points_lost": points_lost(node["draws"], node["losses"]),
                }
            )
            return
        for child in node["children"]:
            walk(child, depth + 1, path)

    for node in nodes:
        walk(node, 1, [])
    met = sorted(branches, key=lambda b: (-b["games"], " ".join(b["sans"])))[:shown]
    # Points lost stays on every row -- it is the honest measure of what a line
    # costs over a year -- but it cannot be the order: it is dominated by volume,
    # so the busiest line came top whatever it scored.
    return sorted(met, key=lambda b: (-b["score"], -b["games"], " ".join(b["sans"])))


def build_tree(rows: Iterable[LineRow], *, min_games: int) -> list[dict[str, Any]]:
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
) -> list[LineRow]:
    # Postgres array slices are 1-based and inclusive, so [1:n] is the first n.
    line = Game.opening_line[1 : settings.report_tree_plies].label("line")
    # "Vienna Game: Stanley Variation, Frankenstein-Dracula Variation" is one
    # system with a long name. The part before the colon is the system.
    family = func.coalesce(func.split_part(Game.opening_name, ":", 1), "").label("family")
    stmt = (
        select(Game.color, family, line, func.count().label("games"), *result_counts())
        .where(*window(player_id, since, until), func.cardinality(Game.opening_line) > 0)
        .group_by(Game.color, family, line)
    )
    rows = (await session.execute(stmt)).all()
    return [
        LineRow(row.color.value, row.family, row.line, row.games, row.wins, row.draws, row.losses)
        for row in rows
    ]


async def _window_games(
    session: AsyncSession, player_id: int, since: datetime, until: datetime
) -> int:
    """Every game in the window, named opening or not -- the honest denominator."""
    stmt = select(func.count()).select_from(Game).where(*window(player_id, since, until))
    return int((await session.execute(stmt)).scalar_one() or 0)
