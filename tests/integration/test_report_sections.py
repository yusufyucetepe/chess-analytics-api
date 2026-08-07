"""The computed report sections, against a real Postgres.

Almost all of this logic is SQL -- window functions, array slices, FILTER
aggregates -- so mocking the database would test nothing at all. The
player-type section has its own module; this one covers the other five.
"""

from datetime import timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.models import Color, Player, Result
from app.report.builder import SECTIONS
from app.report.sections import build_sections, headline, openings, progression, quality
from app.report.sections import time_behaviour as time_section
from tests.integration.games import BASE, SINCE, UNTIL, results, seed


async def build_headline(session: AsyncSession, player: Player) -> dict[str, Any]:
    return await headline.build(session, player.id, since=SINCE, until=UNTIL)


# --- headline ---------------------------------------------------------------


async def test_the_record_counts_every_result(session: AsyncSession, player: Player) -> None:
    await seed(session, player, results("W", "W", "L", "D"))

    section = await build_headline(session, player)

    assert (section["games"], section["wins"], section["losses"], section["draws"]) == (4, 2, 1, 1)
    assert section["win_rate"] == 0.5
    assert section["score"] == 0.625


async def test_hours_played_caps_each_game_and_ignores_missing_durations(
    session: AsyncSession, player: Player
) -> None:
    """Postgres's LEAST ignores NULLs, so an unknown duration must not read as the cap."""
    await seed(
        session,
        player,
        [{"duration_s": None}, {"duration_s": 10 * 86400}, {"duration_s": 1800}],
    )

    section = await build_headline(session, player)

    assert section["seconds_played"] == settings.report_max_game_s + 1800
    assert section["hours_played"] == 4.5


async def test_the_longest_win_streak_is_the_longest_run(
    session: AsyncSession, player: Player
) -> None:
    """Not the total number of wins, and not broken by a draw being ignored."""
    await seed(session, player, results("W", "W", "L", "W", "W", "W", "D", "W"))

    streak = (await build_headline(session, player))["longest_win_streak"]

    assert streak["games"] == 3
    assert streak["start"] == (BASE + timedelta(minutes=3)).isoformat()


async def test_the_best_win_is_the_strongest_opponent_actually_beaten(
    session: AsyncSession, player: Player
) -> None:
    await seed(
        session,
        player,
        [
            {"result": Result.WIN, "opponent_name": "weak", "opponent_rating": 1500},
            {"result": Result.LOSS, "opponent_name": "gm", "opponent_rating": 2700},
            {"result": Result.WIN, "opponent_name": "im", "opponent_rating": 2400},
        ],
    )

    best = (await build_headline(session, player))["best_win"]

    assert best["opponent"] == "im"
    assert best["url"].endswith("/game0002")


async def test_the_busiest_day_is_the_one_with_the_most_games(
    session: AsyncSession, player: Player
) -> None:
    await seed(
        session,
        player,
        [
            {"played_at": BASE},
            {"played_at": BASE + timedelta(days=1)},
            {"played_at": BASE + timedelta(days=1, minutes=5)},
            {"played_at": BASE + timedelta(days=1, minutes=9)},
        ],
    )

    busiest = (await build_headline(session, player))["busiest_day"]

    assert busiest["date"] == (BASE + timedelta(days=1)).date().isoformat()
    assert busiest["games"] == 3


async def test_an_empty_window_is_zeroes_rather_than_a_crash(
    session: AsyncSession, player: Player
) -> None:
    """A player whose year is outside the window still gets a report."""
    await seed(session, player, [{"played_at": BASE - timedelta(days=400)}])

    section = await build_headline(session, player)

    assert section["games"] == 0
    assert section["hours_played"] == 0.0
    assert section["best_win"] is None
    assert section["longest_win_streak"] is None
    assert section["busiest_day"] is None


# --- openings ---------------------------------------------------------------


async def test_openings_are_ranked_within_each_colour(
    session: AsyncSession, player: Player
) -> None:
    await seed(
        session,
        player,
        [{"color": Color.WHITE, "eco": "B22", "opening_name": "Alapin"}] * 3
        + [{"color": Color.WHITE, "eco": "C50", "opening_name": "Italian"}]
        + [{"color": Color.BLACK, "eco": "B10", "opening_name": "Caro-Kann"}] * 2,
    )

    section = await openings.build(session, player.id, since=SINCE, until=UNTIL)

    assert section["distinct_ecos"] == 3
    white = section["by_color"]["white"]
    assert white["games"] == 4
    assert [entry["eco"] for entry in white["top"]] == ["B22", "C50"]
    assert [entry["eco"] for entry in section["by_color"]["black"]["top"]] == ["B10"]


async def test_an_opening_needs_evidence_before_it_can_be_ranked(
    session: AsyncSession, player: Player
) -> None:
    """A single lucky win is not the player's best opening."""
    await seed(
        session,
        player,
        [{"eco": "A00", "opening_name": "Fluke", "result": Result.WIN}]
        + [{"eco": "B22", "opening_name": "Alapin", "result": Result.WIN}]
        * settings.report_opening_min_games,
    )

    section = await openings.build(session, player.id, since=SINCE, until=UNTIL)

    assert section["best"]["eco"] == "B22"
    assert section["worst"]["eco"] == "B22", "the fluke is not eligible either"


async def test_the_tree_is_built_from_the_stored_opening_lines(
    session: AsyncSession, player: Player
) -> None:
    await seed(
        session,
        player,
        [{"color": Color.WHITE, "opening_line": ["e4", "e5", "Nf3"]}] * 3
        + [{"color": Color.WHITE, "opening_line": ["e4", "c5"]}] * 2
        + [{"color": Color.BLACK, "opening_line": ["d4", "Nf6"]}] * 2,
    )

    tree = (await openings.build(session, player.id, since=SINCE, until=UNTIL))["tree"]

    assert [node["san"] for node in tree["white"]] == ["e4"]
    assert tree["white"][0]["games"] == 5
    assert [node["san"] for node in tree["white"][0]["children"]] == ["e5", "c5"]
    assert [node["san"] for node in tree["black"]] == ["d4"]


async def test_the_tree_only_reads_the_plies_it_was_told_to(
    session: AsyncSession, player: Player
) -> None:
    """Deeper moves are stored but must not reach the payload."""
    line = [f"m{i}" for i in range(settings.opening_line_plies)]
    await seed(session, player, [{"opening_line": line}] * 3)

    tree = (await openings.build(session, player.id, since=SINCE, until=UNTIL))["tree"]

    depth, node = 0, tree["white"]
    while node:
        depth += 1
        node = node[0]["children"]
    assert depth == settings.report_tree_plies


# --- quality ----------------------------------------------------------------


async def test_quality_reports_nothing_when_too_little_was_analysed(
    session: AsyncSession, player: Player
) -> None:
    """The brief's hard rule: no averages off a handful of self-selected games."""
    section = await quality.build(session, player.id, since=SINCE, until=UNTIL, games_analysed=3)

    assert section["available"] is False
    assert section["totals"] is None
    assert section["by_month"] == []
    assert section["min_analysed_games"] == settings.quality_min_analysed_games


async def test_quality_averages_only_the_analysed_games(
    session: AsyncSession, player: Player
) -> None:
    analysed = settings.quality_min_analysed_games
    await seed(
        session,
        player,
        [
            {
                "analysed": True,
                "accuracy": Decimal("80.00"),
                "acpl": 40,
                "blunders": 2,
                "mistakes": 1,
                "inaccuracies": 3,
            }
        ]
        * analysed
        + [{"analysed": False}] * 50,
    )

    section = await quality.build(
        session, player.id, since=SINCE, until=UNTIL, games_analysed=analysed
    )

    assert section["available"] is True
    assert section["totals"]["games"] == analysed
    assert section["totals"]["accuracy"] == 80.0
    assert section["totals"]["blunders"] == 2 * analysed
    assert section["totals"]["blunders_per_game"] == 2.0


async def test_quality_buckets_by_week_and_by_month(session: AsyncSession, player: Player) -> None:
    analysed = settings.quality_min_analysed_games
    half = analysed // 2
    game = {"analysed": True, "accuracy": Decimal("70.00"), "acpl": 50}
    await seed(
        session,
        player,
        [{**game, "played_at": BASE + timedelta(minutes=i)} for i in range(half)]
        + [{**game, "played_at": BASE + timedelta(days=45, minutes=i)} for i in range(half)],
    )

    section = await quality.build(
        session, player.id, since=SINCE, until=UNTIL, games_analysed=analysed
    )

    assert len(section["by_month"]) == 2
    assert len(section["by_week"]) == 2
    assert sum(bucket["games"] for bucket in section["by_month"]) == analysed


# --- time behaviour ---------------------------------------------------------


async def test_every_hour_and_weekday_is_present_even_when_unplayed(
    session: AsyncSession, player: Player
) -> None:
    """Charts should not have to cope with holes in their own axis."""
    await seed(session, player, results("W", "L"))

    section = await time_section.build(session, player.id, since=SINCE, until=UNTIL)

    assert len(section["by_hour"]) == 24
    assert len(section["by_weekday"]) == 7
    assert section["peak_hour"] == BASE.hour
    assert section["by_weekday"][BASE.isoweekday() - 1]["games"] == 2
    assert sum(slot["games"] for slot in section["by_hour"]) == 2


async def test_the_longest_session_ends_at_the_first_long_gap(
    session: AsyncSession, player: Player
) -> None:
    gap = timedelta(seconds=settings.report_session_gap_s * 2)
    first = [{"played_at": BASE + timedelta(minutes=5 * i)} for i in range(3)]
    second = [{"played_at": BASE + gap + timedelta(minutes=5 * i)} for i in range(5)]
    await seed(session, player, first + second)

    session_stat = (await time_section.build(session, player.id, since=SINCE, until=UNTIL))[
        "longest_session"
    ]

    assert session_stat["games"] == 5
    assert session_stat["start"] == (BASE + gap).isoformat()
    assert session_stat["minutes"] == 20.0


async def test_tilt_compares_the_games_after_two_losses_to_the_baseline(
    session: AsyncSession, player: Player
) -> None:
    await seed(session, player, results("L", "L", "W", "L", "L", "L"))

    tilt = (await time_section.build(session, player.id, since=SINCE, until=UNTIL))["tilt"]

    assert tilt["games_after_two_losses"] == 2
    assert tilt["win_rate"] == 0.5
    assert tilt["baseline_win_rate"] == round(1 / 6, 4)


async def test_tilt_is_absent_when_the_player_never_lost_twice_running(
    session: AsyncSession, player: Player
) -> None:
    await seed(session, player, results("W", "L", "W", "L", "W"))

    section = await time_section.build(session, player.id, since=SINCE, until=UNTIL)

    assert section["tilt"] is None


# --- progression ------------------------------------------------------------


async def test_progression_tracks_start_end_peak_and_net(
    session: AsyncSession, player: Player
) -> None:
    """`player_rating` is the rating going in, so the close needs the last diff."""
    await seed(
        session,
        player,
        [
            {"player_rating": 1500, "rating_diff": 10},
            {"player_rating": 1510, "rating_diff": 40},
            {"player_rating": 1550, "rating_diff": -30},
        ],
    )

    perf = (await progression.build(session, player.id, since=SINCE, until=UNTIL))["by_perf"]

    assert perf["blitz"]["start"] == 1500
    assert perf["blitz"]["end"] == 1520
    assert perf["blitz"]["peak"] == 1550
    assert perf["blitz"]["net"] == 20


async def test_progression_keeps_the_perfs_apart(session: AsyncSession, player: Player) -> None:
    await seed(
        session,
        player,
        [{"perf": "bullet", "player_rating": 2000, "rating_diff": -100}] * 2
        + [{"perf": "rapid", "player_rating": 1200, "rating_diff": 50}] * 5,
    )

    section = await progression.build(session, player.id, since=SINCE, until=UNTIL)

    assert set(section["by_perf"]) == {"bullet", "rapid"}
    assert section["main_perf"] == "rapid", "the one they actually played"
    assert section["best_gain"] == "rapid"
    assert section["by_perf"]["bullet"]["net"] < 0


async def test_progression_splits_the_series_by_month(
    session: AsyncSession, player: Player
) -> None:
    await seed(
        session,
        player,
        [
            {"played_at": BASE, "player_rating": 1500, "rating_diff": 5},
            {"played_at": BASE + timedelta(days=40), "player_rating": 1600, "rating_diff": 5},
        ],
    )

    series = (await progression.build(session, player.id, since=SINCE, until=UNTIL))["by_perf"][
        "blitz"
    ]["series"]

    assert len(series) == 2
    assert series[0]["end"] == 1505
    assert series[1]["end"] == 1605


# --- the whole set ----------------------------------------------------------


async def test_every_section_is_built_and_is_json_native(
    session: AsyncSession, player: Player
) -> None:
    """The payload goes into JSONB, so nothing may need a custom encoder."""
    import json

    await seed(
        session,
        player,
        [
            {
                "eco": "B22",
                "opening_name": "Alapin",
                "opening_line": ["e4", "c5", "c3"],
                "player_rating": 1500,
                "rating_diff": 8,
                "opponent_rating": 1490,
                "analysed": True,
                "accuracy": Decimal("77.50"),
                "acpl": 45,
                "blunders": 1,
                "mistakes": 2,
                "inaccuracies": 4,
            }
        ]
        * settings.quality_min_analysed_games,
    )

    sections = await build_sections(
        session,
        player.id,
        since=SINCE,
        until=UNTIL,
        games_analysed=settings.quality_min_analysed_games,
    )

    assert set(sections) == set(SECTIONS), "every declared section is computed"
    assert all(value is not None for value in sections.values())
    assert json.loads(json.dumps(sections))["quality"]["available"] is True


async def test_a_player_who_never_won_has_no_streak(session: AsyncSession, player: Player) -> None:
    await seed(session, player, results("L", "D", "L"))

    assert (await build_headline(session, player))["longest_win_streak"] is None


async def test_the_time_section_survives_a_window_with_no_games(
    session: AsyncSession, player: Player
) -> None:
    section = await time_section.build(session, player.id, since=SINCE, until=UNTIL)

    assert section["longest_session"] is None
    assert section["tilt"] is None
    assert section["peak_hour"] is None
    assert len(section["by_hour"]) == 24
