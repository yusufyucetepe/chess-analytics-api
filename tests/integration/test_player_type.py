"""The signals behind the player-type label, read out of a real Postgres.

The classifier itself is unit-tested against hand-built numbers; what is worth a
database here is the part that produces those numbers -- the join onto the seeded
ECO table, and counting captures inside the stored opening line.
"""

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import OpeningMeta, Player, Result
from app.report.player_type_weights import AGGRESSIVE, POSITIONAL
from app.report.sections import build_sections, player_type
from tests.integration.games import SINCE, UNTIL, seed


async def signals(session: AsyncSession, player: Player) -> player_type.Signals:
    return await player_type.gather(session, player.id, since=SINCE, until=UNTIL)


async def test_the_migration_seeded_every_eco_code(session: AsyncSession) -> None:
    """Reference data, so it survives the per-test truncate."""
    total = (await session.execute(select(func.count()).select_from(OpeningMeta))).scalar_one()

    assert total == 500


async def test_the_alapin_kept_its_tags_through_the_seed(session: AsyncSession) -> None:
    row = await session.get(OpeningMeta, "B22")

    assert row is not None
    assert row.family == "Sicilian Defense: Alapin Variation"
    assert set(row.style_tags) == {"positional", "solid"}


async def test_style_tags_are_counted_per_game_not_per_opening(
    session: AsyncSession, player: Player
) -> None:
    """Three King's Gambits are three votes for 'gambit', not one."""
    await seed(session, player, [{"eco": "C33"}] * 3 + [{"eco": "B22"}] * 2)

    found = await signals(session, player)

    assert found.tagged_games == 5
    assert found.gambit_games == 3
    assert found.tag_counts["positional"] == 2, "the two Alapins"
    assert found.tag_counts["attacking"] == 3


async def test_a_game_with_no_eco_is_not_counted_as_tagged(
    session: AsyncSession, player: Player
) -> None:
    """Variants and odd move orders arrive without one; they must not skew the mix."""
    await seed(session, player, [{"eco": "C33"}] + [{"eco": None}] * 4)

    found = await signals(session, player)

    assert found.games == 5
    assert found.tagged_games == 1


async def test_captures_are_counted_from_the_opening_line(
    session: AsyncSession, player: Player
) -> None:
    """SAN marks a capture with an 'x', and the count never leaves Postgres."""
    await seed(
        session,
        player,
        [
            {"opening_line": ["e4", "c5", "Nf3", "d6"]},
            {"opening_line": ["d4", "d5", "c4", "dxc4", "e3", "Nf6"]},
            {"opening_line": ["e4", "e5", "Nf3", "Nc6", "Nxe5", "Nxe5"]},
        ],
    )

    found = await signals(session, player)

    assert found.avg_opening_captures == 1.0, "0 + 1 + 2 over three games"


async def test_endings_are_split_between_the_board_and_the_clock(
    session: AsyncSession, player: Player
) -> None:
    await seed(
        session,
        player,
        [{"status": "mate"}, {"status": "resign"}, {"status": "resign"}]
        + [{"status": "outoftime"}] * 2
        + [{"status": "draw", "result": Result.DRAW}],
    )

    found = await signals(session, player)

    assert found.forced_endings == 3
    assert found.flagged == 2
    assert found.draws == 1
    assert found.decisive == 5


async def test_an_empty_window_classifies_to_nothing_rather_than_crashing(
    session: AsyncSession, player: Player
) -> None:
    found = await signals(session, player)
    verdict = player_type.classify(found)

    assert found.games == 0
    assert found.avg_plies is None
    assert verdict["scores"] == {"positional": None, "aggressive": None, "tactical": None}
    assert verdict["label"] is None


async def test_a_seeded_gambiteer_comes_out_as_an_attacker(
    session: AsyncSession, player: Player
) -> None:
    """End to end from rows: the ECO table, the endings and the captures agree."""
    await seed(
        session,
        player,
        [
            {
                "eco": "C33",
                "opening_line": ["e4", "e5", "f4", "exf4", "Nf3", "g5"],
                "moves_count": 30,
                "status": "mate",
                "result": Result.WIN,
            }
        ]
        * 60,
    )

    verdict = player_type.classify(await signals(session, player))

    assert verdict["leaning"] == AGGRESSIVE
    assert verdict["label"] == "Gambit Specialist"
    assert verdict["signature"]["tag"] == "gambit"
    assert verdict["confident"] is True


async def test_a_seeded_grinder_comes_out_as_a_squeezer(
    session: AsyncSession, player: Player
) -> None:
    await seed(
        session,
        player,
        [
            {
                "eco": "C88",
                "opening_line": ["e4", "e5", "Nf3", "Nc6", "Bb5", "a6"],
                "moves_count": 110,
                "status": "resign",
                "result": Result.WIN,
            }
        ]
        * 54
        + [
            {
                "eco": "C88",
                "opening_line": ["e4", "e5", "Nf3", "Nc6", "Bb5", "a6"],
                "moves_count": 120,
                "status": "draw",
                "result": Result.DRAW,
            }
        ]
        * 6,
    )

    verdict = player_type.classify(await signals(session, player))

    assert verdict["leaning"] == POSITIONAL
    assert verdict["label"] == "Positional Grinder", "Ruy Lopez is closed, not solid"


async def test_player_type_joins_the_other_sections(session: AsyncSession, player: Player) -> None:
    await seed(session, player, [{"eco": "C33"}] * 40)

    sections: dict[str, Any] = await build_sections(
        session, player.id, since=SINCE, until=UNTIL, games_analysed=0
    )

    assert sections["player_type"]["scores"]["aggressive"] > 0
    assert sections["player_type"]["label"] is not None
