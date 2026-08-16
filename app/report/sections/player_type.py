"""The player-type section: three scores that sum to 100, and a label.

A heuristic, and presented as one. No engine is involved and none of the inputs
measures strength -- the classifier reads which openings someone chooses and how
their games tend to end, then says which of three caricatures they resemble.

The shape is deliberately uniform: every signal returns a *vote*, a three-way
split that sums to one, and the result is the weighted mean of the votes. That
guarantees the scores sum to 100 without a rescaling step, and it means a signal
with nothing to say can abstain by voting down the middle instead of dragging
the answer somewhere it did not intend.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Game, OpeningMeta, Result
from app.db.seed import tag_baseline
from app.report.player_type_weights import (
    AGGRESSIVE,
    AXES,
    AXIS_NOUNS,
    BLEND_MARGIN,
    BLENDED_NOUN,
    BUSY_OPENING_CAPTURES,
    DISCLAIMER,
    FLAVOUR_MIN_LIFT,
    FLAVOUR_MIN_SHARE,
    FLAVOUR_TAGS,
    LONG_GAME_PLIES,
    MIN_GAMES_FOR_A_LABEL,
    NAME_OVERRIDES,
    PLAIN_LABELS,
    POSITIONAL,
    SHORT_GAME_PLIES,
    SIGNAL_WEIGHTS,
    TACTICAL,
    TAG_AXES,
)
from app.report.sections._common import rate, window
from app.report.shades import match as shades_for

Vote = dict[str, float]

NEUTRAL: Vote = dict.fromkeys(AXES, 1 / 3)

#: Endings that mean somebody was beaten over the board rather than by the clock.
FORCED_ENDINGS = ("mate", "resign")

#: Share of games in gambit lines at which the signal is fully convinced. A
#: quarter is already a lot -- most players never pass a few per cent.
GAMBIT_SATURATION = 0.25

#: Draw rate at which the decisiveness signal reads as fully positional. Online
#: blitz draws are rare, so this is much lower than a classical player's.
DRAWISH_SATURATION = 0.15


@dataclass(frozen=True, slots=True)
class Signals:
    """What the classifier reads. Everything here comes from SQL aggregates."""

    games: int = 0
    tagged_games: int = 0
    tag_counts: dict[str, int] = field(default_factory=dict)
    avg_plies: float | None = None
    decisive: int = 0
    draws: int = 0
    forced_endings: int = 0
    flagged: int = 0
    avg_opening_captures: float | None = None

    @property
    def gambit_games(self) -> int:
        return self.tag_counts.get("gambit", 0)


async def build(
    session: AsyncSession, player_id: int, *, since: datetime, until: datetime
) -> dict[str, Any]:
    """Gather the signals for one player and hand them to the classifier."""
    return classify(await gather(session, player_id, since=since, until=until))


def classify(signals: Signals) -> dict[str, Any]:
    """Turn signals into three scores summing to 100, plus a label.

    Two things are withheld rather than guessed. Below ``MIN_GAMES_FOR_A_LABEL``
    the scores stand but the label does not -- a caveated number is more use
    than a blank, while calling someone an attacker on ten games is not. And
    when no signal has anything to say at all, the scores are ``None`` rather
    than an even split: a third each is a verdict of "perfectly balanced", which
    is a different statement from "we cannot tell".
    """
    mean = _weighted_mean(_votes(signals))
    if mean is None:
        return _verdict(
            signals, scores=dict.fromkeys(AXES), leaning=None, label=None, confident=False
        )

    scores = _percentages(mean)
    ranked = sorted(scores.items(), key=lambda item: -item[1])
    confident = signals.games >= MIN_GAMES_FOR_A_LABEL

    # `None` when the top two axes are within the margin: the player leans
    # somewhere, but not far enough for the noun to claim otherwise.
    decided = ranked[0][0] if ranked[0][1] - ranked[1][1] >= BLEND_MARGIN else None
    flavour = _flavour(signals)
    label = _name(decided, flavour) if confident else None

    return _verdict(
        signals,
        scores=scores,
        leaning=ranked[0][0],
        label=label,
        confident=confident,
        flavour=flavour,
        # Withheld with the label rather than with the scores: naming three
        # grandmasters off a dozen games would be a stronger claim than the
        # label we already refused to make.
        shades=shades_for(scores, flavour) if label else (),
    )


def _name(axis: str | None, flavour: str | None) -> str:
    """``<adjective> <noun>``, unless the pair has earned its own name.

    The noun is how the player wins, the adjective is what they choose to play.
    A repertoire with nothing distinctive in it drops the adjective rather than
    inventing one.
    """
    if flavour is None:
        return PLAIN_LABELS[axis]
    if override := NAME_OVERRIDES.get((flavour, axis)):
        return override
    noun = AXIS_NOUNS[axis] if axis else BLENDED_NOUN
    return f"{FLAVOUR_TAGS[flavour]} {noun}"


def _flavour(signals: Signals) -> str | None:
    """The tag the player plays most out of proportion to how common it is.

    Not the most frequent tag: that is whichever one sits on the most ECO codes,
    which describes the table rather than the player. A tag has to clear both a
    share floor and a lift floor, so a single freak line cannot name someone and
    neither can an unremarkable one.
    """
    if not signals.tagged_games:
        return None
    baseline = tag_baseline()

    best: tuple[float, str] | None = None
    for tag in FLAVOUR_TAGS:
        base = baseline.get(tag)
        share = signals.tag_counts.get(tag, 0) / signals.tagged_games
        if not base or share < FLAVOUR_MIN_SHARE:
            continue
        lift = share / base
        # Ties break on the tag name, so the label never depends on dict order.
        if lift >= FLAVOUR_MIN_LIFT and (best is None or (-lift, tag) < (-best[0], best[1])):
            best = (lift, tag)
    return best[1] if best else None


def _verdict(
    signals: Signals,
    *,
    scores: Mapping[str, int | None],
    leaning: str | None,
    label: str | None,
    confident: bool,
    flavour: str | None = None,
    shades: Sequence[str] = (),
) -> dict[str, Any]:
    baseline = tag_baseline().get(flavour or "", 0.0)
    share = signals.tag_counts.get(flavour or "", 0) / (signals.tagged_games or 1)
    return {
        "label": label,
        "confident": confident,
        "scores": scores,
        "leaning": leaning,
        # Nearest names on the same three axes. Empty rather than absent, so a
        # consumer never has to tell "we withheld these" from "old payload".
        "shades": list(shades),
        # What earned the adjective, so the frontend can say "you play offbeat
        # openings ten times more often than the ECO table would suggest".
        "signature": (
            None
            if flavour is None
            else {
                "tag": flavour,
                "adjective": FLAVOUR_TAGS[flavour],
                "share": round(share, 4),
                "lift": round(share / baseline, 1) if baseline else None,
            }
        ),
        "games": signals.games,
        "min_games": MIN_GAMES_FOR_A_LABEL,
        "disclaimer": DISCLAIMER,
        "signals": {
            "gambit_share": rate(signals.gambit_games, signals.games),
            "draw_share": rate(signals.draws, signals.games),
            "forced_ending_share": rate(signals.forced_endings, signals.decisive),
            "flagged_share": rate(signals.flagged, signals.games),
            "avg_plies": round(signals.avg_plies, 1) if signals.avg_plies else None,
            "avg_opening_captures": (
                round(signals.avg_opening_captures, 2) if signals.avg_opening_captures else None
            ),
            "tagged_games": signals.tagged_games,
        },
    }


def _votes(signals: Signals) -> list[tuple[float, Vote]]:
    """Each signal as a (weight, vote) pair. Signals with no data are dropped."""
    weighed: list[tuple[float, Vote]] = []

    def add(name: str, vote: Vote | None) -> None:
        if vote is not None:
            weighed.append((SIGNAL_WEIGHTS[name], vote))

    add("openings", _openings_vote(signals))
    if signals.games:
        add(
            "gambits",
            _mix(
                NEUTRAL,
                {AGGRESSIVE: 0.6, TACTICAL: 0.4},
                _scale(signals.gambit_games / signals.games, GAMBIT_SATURATION),
            ),
        )
        add(
            "decisiveness",
            _mix(
                {AGGRESSIVE: 0.4, TACTICAL: 0.4, POSITIONAL: 0.2},
                {POSITIONAL: 1.0},
                _scale(signals.draws / signals.games, DRAWISH_SATURATION),
            ),
        )
    if signals.avg_plies is not None:
        # Short games mean someone was mated or threw a piece; long ones mean
        # the position had to be ground down.
        add(
            "game_length",
            _mix(
                {AGGRESSIVE: 0.6, TACTICAL: 0.4},
                {POSITIONAL: 1.0},
                _between(signals.avg_plies, SHORT_GAME_PLIES, LONG_GAME_PLIES),
            ),
        )
    if signals.decisive:
        # Being beaten over the board rather than by the clock. The low pole is
        # not positional: a year decided on the clock is time-scramble chaos.
        add(
            "finish",
            _mix(
                {TACTICAL: 0.6, POSITIONAL: 0.4},
                {AGGRESSIVE: 0.7, TACTICAL: 0.3},
                signals.forced_endings / signals.decisive,
            ),
        )
    if signals.avg_opening_captures is not None:
        add(
            "exchanges",
            _mix(
                {POSITIONAL: 1.0},
                {TACTICAL: 0.7, AGGRESSIVE: 0.3},
                _scale(signals.avg_opening_captures, BUSY_OPENING_CAPTURES),
            ),
        )
    return weighed


def _openings_vote(signals: Signals) -> Vote | None:
    """Tag mass across the player's repertoire, normalised into a vote.

    Abstains when nothing in the repertoire is stylistic -- a player whose games
    are all 'offbeat' and 'flexible' has told us about their taste, not their
    style, and the other signals should decide alone.
    """
    if not signals.tagged_games:
        return None
    raw = dict.fromkeys(AXES, 0.0)
    for tag, count in signals.tag_counts.items():
        for axis, pull in TAG_AXES.get(tag, {}).items():
            raw[axis] += pull * count / signals.tagged_games
    return _normalise(raw) if sum(raw.values()) else None


def _mix(low: Vote, high: Vote, position: float) -> Vote:
    """Interpolate between two poles. Poles are normalised first, so they can be
    written as the readable partial dicts above."""
    left, right = _normalise(low), _normalise(high)
    at = min(max(position, 0.0), 1.0)
    return {axis: left.get(axis, 0.0) * (1 - at) + right.get(axis, 0.0) * at for axis in AXES}


def _normalise(vote: Vote) -> Vote:
    total = sum(vote.values())
    if not total:
        return dict(NEUTRAL)
    return {axis: vote.get(axis, 0.0) / total for axis in AXES}


def _scale(value: float, saturation: float) -> float:
    return min(value / saturation, 1.0) if saturation else 0.0


def _between(value: float, low: float, high: float) -> float:
    return min(max((value - low) / (high - low), 0.0), 1.0) if high > low else 0.0


def _weighted_mean(votes: list[tuple[float, Vote]]) -> Vote | None:
    """``None`` when there is nothing to average.

    Covers both ways that happens: no signal had any data, and every signal was
    weighted to zero in the config module. Neither is a balanced player.
    """
    total = sum(weight for weight, _ in votes)
    if not total:
        return None
    return {
        axis: sum(weight * vote.get(axis, 0.0) for weight, vote in votes) / total for axis in AXES
    }


def _percentages(vote: Vote) -> dict[str, int]:
    """Whole numbers that sum to exactly 100, by largest remainder.

    Rounding each axis on its own would land on 99 or 101 often enough that the
    frontend would have to apologise for it.
    """
    scaled = {axis: vote.get(axis, 0.0) * 100 for axis in AXES}
    floors = {axis: int(value) for axis, value in scaled.items()}
    short = 100 - sum(floors.values())
    by_remainder = sorted(AXES, key=lambda axis: (-(scaled[axis] - floors[axis]), axis))
    for axis in by_remainder[:short]:
        floors[axis] += 1
    return floors


async def gather(
    session: AsyncSession, player_id: int, *, since: datetime, until: datetime
) -> Signals:
    """Read every signal out of Postgres. Three aggregates, no per-game loop."""
    where = window(player_id, since, until)

    # A correlated scalar subquery over the stored opening line: SAN marks a
    # capture with an 'x', so this counts the exchanges in the first plies
    # without the moves ever reaching Python.
    move = func.unnest(Game.opening_line).column_valued("move")
    captures = select(func.count()).where(move.like("%x%")).scalar_subquery()

    totals = (
        await session.execute(
            select(
                func.count().label("games"),
                func.avg(Game.moves_count).label("avg_plies"),
                func.count().filter(Game.result != Result.DRAW).label("decisive"),
                func.count().filter(Game.result == Result.DRAW).label("draws"),
                func.count().filter(Game.status.in_(FORCED_ENDINGS)).label("forced"),
                func.count().filter(Game.status == "outoftime").label("flagged"),
                func.avg(captures).label("captures"),
            ).where(*where)
        )
    ).one()

    tagged = (
        await session.execute(
            select(func.count())
            .select_from(Game)
            .join(OpeningMeta, OpeningMeta.eco == Game.eco)
            .where(*where)
        )
    ).scalar_one()

    # `joins_implicitly` because Postgres treats an unnest that references a
    # FROM item to its left as LATERAL on its own. Without it SQLAlchemy cannot
    # see the correlation and warns about a cartesian product it is not making.
    tag = func.unnest(OpeningMeta.style_tags).column_valued("tag", joins_implicitly=True)
    tag_rows = (
        await session.execute(
            select(tag, func.count().label("games"))
            .select_from(Game)
            .join(OpeningMeta, OpeningMeta.eco == Game.eco)
            .where(*where)
            .group_by(tag)
        )
    ).all()

    return Signals(
        games=totals.games,
        tagged_games=int(tagged),
        tag_counts={row.tag: row.games for row in tag_rows},
        avg_plies=float(totals.avg_plies) if totals.avg_plies is not None else None,
        decisive=totals.decisive,
        draws=totals.draws,
        forced_endings=totals.forced,
        flagged=totals.flagged,
        avg_opening_captures=float(totals.captures) if totals.captures is not None else None,
    )
