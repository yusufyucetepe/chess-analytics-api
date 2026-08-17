"""The player-type section: three scores that sum to 100, and a label.

A heuristic, and presented as one. No engine is involved and none of the inputs
measures strength -- the classifier reads which openings someone chooses and how
their games tend to end, then says which of three caricatures they resemble.

The shape is deliberately uniform: every signal returns a *vote*, a three-way
split that sums to one, and the result is the weighted mean of the votes. That
guarantees the scores sum to 100 without a rescaling step, and it means a signal
with nothing to say can abstain by voting down the middle instead of dragging
the answer somewhere it did not intend.

Two rules govern what the opening signals are allowed to read:

* **One game, one vote.** A game contributes a single unit of tag mass split
  across its tags, so a code carrying four of them does not outvote four games
  on a code carrying one.
* **Only what the player chose.** The side that made the move defining an
  opening owns it. "Sicilian Defense: Smith-Morra Gambit" is White's idea, and
  a Black player who met it gets read on the Sicilian they did choose -- see
  ``app.report.families``.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, NamedTuple

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Color, Game, Result
from app.db.seed import tag_baseline
from app.report.families import eco_tags, family_owner, family_sql, family_tags, owner
from app.report.player_type_weights import (
    AGGRESSIVE,
    AXES,
    AXIS_NOUNS,
    BLENDED_NOUN,
    BUSY_OPENING_CAPTURES,
    CENTRIST_SHARE,
    DISCLAIMER,
    FLAVOUR_MIN_LIFT,
    FLAVOUR_MIN_SHARE,
    FLAVOUR_TAGS,
    LONG_GAME_PLIES,
    MIN_GAMES_FOR_A_LABEL,
    NAME_OVERRIDES,
    OPENNESS_BY_LETTER,
    PLAIN_LABELS,
    POSITIONAL,
    SHORT_GAME_PLIES,
    SIGNAL_WEIGHTS,
    TACTICAL,
    TAG_AXES,
)
from app.report.sections._common import first, rate, window
from app.report.shades import match as shades_for
from app.report.style_reference import Reference
from app.report.style_reference import load as load_reference

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
    #: Games whose opening was the player's own choice, and had tags to read.
    tagged_games: int = 0
    #: Games carrying each tag. Counts games, so the shares it feeds are shares
    #: of a year rather than of some abstract quantity.
    tag_counts: dict[str, int] = field(default_factory=dict)
    #: The same games as fractions: one unit each, split across their tags. What
    #: the vote is built from, so a richly tagged code cannot shout.
    tag_mass: dict[str, float] = field(default_factory=dict)
    #: Mean openness of the chosen openings, 0 (closed) to 1 (open).
    openness: float | None = None
    avg_plies: float | None = None
    decisive: int = 0
    draws: int = 0
    forced_endings: int = 0
    flagged: int = 0
    avg_opening_captures: float | None = None
    #: For finding the population this player belongs to.
    main_perf: str | None = None
    rating: int | None = None

    @property
    def gambit_games(self) -> int:
        return self.tag_counts.get("gambit", 0)


async def build(
    session: AsyncSession, player_id: int, *, since: datetime, until: datetime
) -> dict[str, Any]:
    """Gather the signals for one player and hand them to the classifier."""
    signals = await gather(session, player_id, since=since, until=until)
    reference = (
        await load_reference(session, signals.main_perf, signals.rating)
        if signals.main_perf
        else None
    )
    return classify(signals, reference)


def classify(signals: Signals, reference: Reference | None = None) -> dict[str, Any]:
    """Turn signals into three scores summing to 100, plus a label.

    Two things are withheld rather than guessed. Below ``MIN_GAMES_FOR_A_LABEL``
    the scores stand but the label does not -- a caveated number is more use
    than a blank, while calling someone an attacker on ten games is not. And
    when no signal has anything to say at all, the scores are ``None`` rather
    than an even split: a third each is a verdict of "perfectly balanced", which
    is a different statement from "we cannot tell".

    ``reference`` decides the All-Rounder. Without one the leading axis stands:
    a population we cannot see is not evidence that somebody is in the middle of
    it, and saying "All-Rounder" on that basis is the failure this replaced.
    """
    mean = _weighted_mean(_votes(signals))
    if mean is None:
        return _verdict(
            signals, scores=dict.fromkeys(AXES), leaning=None, label=None, confident=False
        )

    scores = _percentages(mean)
    leaning = max(AXES, key=lambda axis: (scores[axis], axis))
    confident = signals.games >= MIN_GAMES_FOR_A_LABEL

    centre = _centre(scores, reference)
    # `None` is the All-Rounder: near enough to the middle of this player's own
    # population that naming an axis would be reporting noise.
    decided = None if centre and centre["centrist"] else leaning
    flavour = _flavour(signals)
    label = _name(decided, flavour) if confident else None

    return _verdict(
        signals,
        scores=scores,
        leaning=leaning,
        label=label,
        confident=confident,
        flavour=flavour,
        centre=centre,
        # Withheld with the label rather than with the scores: naming three
        # grandmasters off a dozen games would be a stronger claim than the
        # label we already refused to make.
        shades=shades_for(scores, flavour) if label else (),
    )


def _centre(scores: dict[str, int], reference: Reference | None) -> dict[str, Any] | None:
    """How central this player is within their own band, or ``None`` if unknown."""
    if reference is None:
        return None
    percentile = reference.percentile(scores)
    return {
        "percentile": round(percentile, 4),
        "centrist": percentile <= CENTRIST_SHARE,
        "share": CENTRIST_SHARE,
        "perf": reference.perf,
        "rating_band": reference.rating_band,
        "players": reference.players,
    }


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

    The lift ranks tags against each other and goes no further than this
    function. It is a ratio to the catalogue, not to other players, which is why
    the payload publishes the share and not the multiple -- see ``_verdict``.
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
    centre: dict[str, Any] | None = None,
    shades: Sequence[str] = (),
) -> dict[str, Any]:
    share = signals.tag_counts.get(flavour or "", 0) / (signals.tagged_games or 1)
    return {
        "label": label,
        "confident": confident,
        "scores": scores,
        "leaning": leaning,
        # Where the player sits in their own rating band and time control, and
        # therefore whether the label was allowed to be All-Rounder. `None` when
        # no band had enough players to say.
        "centre": centre,
        # Nearest names on the same three axes. Empty rather than absent, so a
        # consumer never has to tell "we withheld these" from "old payload".
        "shades": list(shades),
        # What earned the adjective. Deliberately no multiplier: the only
        # denominator we hold is the share of *ECO codes* carrying the tag, which
        # is a fact about the catalogue and not about anybody's opponents. It
        # still ranks the tags in `_flavour`, where nothing is claimed about
        # other players; published as "5.1x more often" it reads as a comparison
        # that was never measured. A real one needs the opening explorer's
        # aggregates for the player's rating band and time control.
        "signature": (
            None
            if flavour is None
            else {
                "tag": flavour,
                "adjective": FLAVOUR_TAGS[flavour],
                "share": round(share, 4),
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
            "openness": round(signals.openness, 4) if signals.openness is not None else None,
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
    if signals.openness is not None:
        # Open positions are decided by what the pieces do; closed ones by where
        # they stand. Between the two sits the Sicilian, at half a point.
        add(
            "openness",
            _mix({POSITIONAL: 1.0}, {AGGRESSIVE: 0.55, TACTICAL: 0.45}, signals.openness),
        )
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
    if not signals.tag_mass:
        return None
    raw = dict.fromkeys(AXES, 0.0)
    for tag, mass in signals.tag_mass.items():
        for axis, pull in TAG_AXES.get(tag, {}).items():
            raw[axis] += pull * mass
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


@dataclass(frozen=True, slots=True)
class OpeningRow:
    """One (opening, colour) group of games, as the classifier reads it."""

    eco: str | None
    family: str
    color: Color
    ply: int | None
    games: int


class Openings(NamedTuple):
    """What the opening groups add up to, once the two rules have been applied."""

    counts: dict[str, int]
    mass: dict[str, float]
    openness: float | None
    tagged_games: int


def read_openings(rows: Sequence[OpeningRow]) -> Openings:
    """Fold opening groups into tag counts, tag mass, mean openness and a total.

    Pure, and separated from the query for it: every rule about whose opening it
    was and what a game is worth lives here, where a test can state a repertoire
    in four lines and read the answer.
    """
    # A family is defined by its shallowest line: the Sicilian is settled at
    # 1...c5, whatever White does on move two.
    families: dict[str, int | None] = {}
    for row in rows:
        if row.family and row.ply:
            seen = families.get(row.family)
            families[row.family] = row.ply if seen is None else min(seen, row.ply)
    codes, rollup = eco_tags(), family_tags()

    counts: dict[str, int] = {}
    mass: dict[str, float] = {}
    openness = 0.0
    chosen = 0
    tagged = 0

    for row in rows:
        tags = _tags_for(row, families, codes, rollup)
        if tags is None:
            continue
        chosen += row.games
        if row.eco:
            openness += OPENNESS_BY_LETTER.get(row.eco[0], 0.0) * row.games
        if not tags:
            continue
        tagged += row.games
        for tag in tags:
            counts[tag] = counts.get(tag, 0) + row.games
            mass[tag] = mass.get(tag, 0.0) + row.games / len(tags)

    return Openings(counts, mass, openness / chosen if chosen else None, tagged)


def _tags_for(
    row: OpeningRow,
    families: Mapping[str, int | None],
    codes: Mapping[str, tuple[str, ...]],
    rollup: Mapping[str, tuple[str, ...]],
) -> tuple[str, ...] | None:
    """The tags this game is allowed to say about the player. ``None`` to skip it.

    Four cases, in order. The player chose the exact line, so it speaks for
    them. They chose the family and the opponent chose the line inside it, so
    the family speaks for them and the line does not. Nothing is known about
    either, in which case the line stands -- an export with no ply is missing
    data, and silently dropping those games would empty the signal rather than
    sharpen it. Or the opening was the opponent's, and the game says nothing
    about this player's taste at all.
    """
    leaf, whole = owner(row.ply), family_owner(row.family, families.get(row.family))
    if leaf is row.color:
        return codes.get(row.eco or "", ())
    if whole is row.color:
        return rollup.get(row.family, ())
    if leaf is None and whole is None:
        return codes.get(row.eco or "", ())
    return None


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

    family = family_sql(Game.opening_name).label("family")
    opening_rows = (
        await session.execute(
            select(Game.eco, family, Game.color, Game.opening_ply, func.count().label("games"))
            .where(*where)
            .group_by(Game.eco, family, Game.color, Game.opening_ply)
        )
    ).all()
    openings = read_openings(
        [OpeningRow(r.eco, r.family, r.color, r.opening_ply, r.games) for r in opening_rows]
    )

    perf, rating = await _population(session, player_id, since, until)

    return Signals(
        games=totals.games,
        tagged_games=openings.tagged_games,
        tag_counts=openings.counts,
        tag_mass=openings.mass,
        openness=openings.openness,
        avg_plies=float(totals.avg_plies) if totals.avg_plies is not None else None,
        decisive=totals.decisive,
        draws=totals.draws,
        forced_endings=totals.forced,
        flagged=totals.flagged,
        avg_opening_captures=float(totals.captures) if totals.captures is not None else None,
        main_perf=perf,
        rating=rating,
    )


async def _population(
    session: AsyncSession, player_id: int, since: datetime, until: datetime
) -> tuple[str | None, int | None]:
    """The time control the player mostly plays, and where they ended it.

    Which band a player is compared against is their busiest one, not their
    best: a blitz player with four classical games is a blitz player.
    """
    # The rating is the latest settled one in that time control. Provisional
    # ratings are left out for the reason the progression section leaves them
    # out: 1500 is a placeholder, and it would put the player in the wrong band.
    # `settled IS NULL` sorts first so that provisional and missing ratings fall
    # to the back of the array rather than becoming its head.
    settled = case((Game.provisional.is_not(True), Game.player_rating))
    latest = first(settled, settled.is_(None), Game.played_at.desc(), Game.game_id.desc())

    row = (
        await session.execute(
            select(Game.perf, func.count().label("games"), latest.label("rating"))
            .where(*window(player_id, since, until))
            .group_by(Game.perf)
            .order_by(func.count().desc(), Game.perf)
            .limit(1)
        )
    ).first()
    return (row.perf, row.rating) if row else (None, None)
