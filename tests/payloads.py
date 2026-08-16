"""Hand-built report payloads for the frontend tests.

The templates read the payload, not the database, so these are written by hand
rather than computed: it takes one file to cover a player with a full year and a
player with almost nothing, where reaching the same two states through the
pipeline would take two ingests.

Kept honest by ``test_report_payload_shape``, which builds a payload the real
way and asserts these carry the same keys.
"""

from typing import Any

from app.db.models import REPORT_SCHEMA_VERSION


def full_payload(**overrides: Any) -> dict[str, Any]:
    """A busy year: every section populated, quality above the threshold."""
    payload = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "generated_at": "2026-08-15T09:30:00+00:00",
        "player": {
            "username": "Zhigalko_Sergei",
            "title": "GM",
            "country": "BY",
            "ratings": {"bullet": 3201, "blitz": 2884},
        },
        "period": {
            "start": "2025-08-15T00:00:00+00:00",
            "end": "2026-08-15T00:00:00+00:00",
            "days": 365,
        },
        "coverage": {
            "games_total": 357,
            "games_analysed": 45,
            "analysis_rate": 0.1261,
            "min_analysed_games": 20,
            "quality_reliable": True,
            "caveat": "Based on the 45 of 357 games that were analysed.",
        },
        "sections": {
            "headline": {
                "games": 357,
                "wins": 201,
                "draws": 10,
                "losses": 146,
                "win_rate": 0.5630,
                "score": 0.5770,
                "seconds_played": 214200,
                "hours_played": 59.5,
                "moves_played": 12844,
                "days_played": 63,
                "by_perf": [
                    {
                        "perf": "bullet",
                        "games": 300,
                        "wins": 170,
                        "draws": 8,
                        "losses": 122,
                        "win_rate": 0.5667,
                        "score": 0.58,
                    },
                    {
                        "perf": "blitz",
                        "games": 57,
                        "wins": 31,
                        "draws": 2,
                        "losses": 24,
                        "win_rate": 0.5439,
                        "score": 0.5614,
                    },
                ],
                "best_win": {
                    "game_id": "abcd1234",
                    "url": "https://lichess.org/abcd1234",
                    "played_at": "2026-03-03T19:40:00+00:00",
                    "perf": "bullet",
                    "color": "white",
                    "opponent": "Konevlad",
                    "opponent_rating": 3215,
                    "player_rating": 3114,
                    "rating_gap": 101,
                },
                "longest_win_streak": {
                    "games": 32,
                    "start": "2026-01-04T10:00:00+00:00",
                    "end": "2026-01-04T13:20:00+00:00",
                },
                "busiest_day": {
                    "date": "2026-01-04",
                    "games": 90,
                    "wins": 61,
                    "draws": 3,
                    "losses": 26,
                    "win_rate": 0.6778,
                    "score": 0.6944,
                },
            },
            "openings": {
                "distinct_ecos": 70,
                "by_color": {
                    "white": {
                        "games": 180,
                        "distinct_ecos": 38,
                        "top": [
                            {
                                "eco": "B22",
                                "name": "Sicilian Defense: Alapin Variation",
                                "color": "white",
                                "games": 44,
                                "wins": 26,
                                "draws": 2,
                                "losses": 16,
                                "win_rate": 0.5909,
                                "score": 0.6136,
                            }
                        ],
                    },
                    "black": {
                        "games": 177,
                        "distinct_ecos": 32,
                        "top": [
                            {
                                "eco": "B90",
                                "name": "Sicilian Defense: Najdorf",
                                "color": "black",
                                "games": 31,
                                "wins": 18,
                                "draws": 1,
                                "losses": 12,
                                "win_rate": 0.5806,
                                "score": 0.5968,
                            }
                        ],
                    },
                },
                "best": {
                    "eco": "B22",
                    "name": "Sicilian Defense: Alapin Variation",
                    "color": "white",
                    "games": 44,
                    "wins": 26,
                    "draws": 2,
                    "losses": 16,
                    "win_rate": 0.5909,
                    "score": 0.6136,
                },
                "worst": {
                    "eco": "C00",
                    "name": "French Defense",
                    "color": "black",
                    "games": 12,
                    "wins": 3,
                    "draws": 1,
                    "losses": 8,
                    "win_rate": 0.25,
                    "score": 0.2917,
                },
                "min_games_for_ranking": 5,
                "tree": {
                    "plies": 8,
                    "min_games": 2,
                    "white": [
                        {
                            "san": "e4",
                            "games": 150,
                            "wins": 85,
                            "draws": 5,
                            "losses": 60,
                            "score": 0.5833,
                            "children": [
                                {
                                    "san": "c5",
                                    "games": 70,
                                    "wins": 40,
                                    "draws": 2,
                                    "losses": 28,
                                    "score": 0.5857,
                                    "children": [],
                                }
                            ],
                        }
                    ],
                    "black": [
                        {
                            "san": "e4",
                            "games": 120,
                            "wins": 66,
                            "draws": 4,
                            "losses": 50,
                            "score": 0.5667,
                            "children": [],
                        }
                    ],
                },
            },
            "quality": {
                "games_analysed": 45,
                "min_analysed_games": 20,
                "available": True,
                "totals": {
                    "games": 45,
                    "accuracy": 83.81,
                    "acpl": 44.8,
                    "inaccuracies": 132,
                    "mistakes": 61,
                    "blunders": 24,
                    "blunders_per_game": 0.53,
                    "mistakes_per_game": 1.36,
                },
                "by_month": [
                    {
                        "start": "2026-01-01T00:00:00+00:00",
                        "games": 12,
                        "accuracy": 84.2,
                        "acpl": 43.1,
                        "inaccuracies": 30,
                        "mistakes": 14,
                        "blunders": 5,
                        "blunders_per_game": 0.42,
                        "mistakes_per_game": 1.17,
                    }
                ],
                "by_week": [],
            },
            "time_behaviour": {
                "timezone": "UTC",
                "by_hour": [
                    {
                        "hour": hour,
                        "games": (hour * 3) % 40,
                        "wins": (hour * 2) % 25,
                        "draws": 0,
                        "losses": hour % 12,
                        "win_rate": 0.5 + (hour % 5) / 100,
                    }
                    for hour in range(24)
                ],
                "by_weekday": [
                    {
                        "weekday": day,
                        "name": name,
                        "games": day * 9,
                        "wins": day * 5,
                        "draws": 0,
                        "losses": day * 4,
                        "win_rate": 0.55,
                    }
                    for day, name in enumerate(
                        (
                            "Monday",
                            "Tuesday",
                            "Wednesday",
                            "Thursday",
                            "Friday",
                            "Saturday",
                            "Sunday",
                        ),
                        start=1,
                    )
                ],
                "peak_hour": 19,
                "longest_session": {
                    "games": 90,
                    "start": "2026-01-04T10:00:00+00:00",
                    "end": "2026-01-04T12:37:54+00:00",
                    "minutes": 157.9,
                    "gap_minutes": 30,
                },
                "tilt": {
                    "games_after_two_losses": 41,
                    "win_rate": 0.6829,
                    "baseline_win_rate": 0.5630,
                    "delta": 0.1199,
                },
            },
            "progression": {
                "by_perf": {
                    "bullet": {
                        "games": 300,
                        "start": 3201,
                        "end": 3143,
                        "net": -58,
                        "peak": 3230,
                        "low": 3102,
                        "series": [
                            {
                                "month": "2026-01-01T00:00:00+00:00",
                                "games": 120,
                                "average": 3180.0,
                                "peak": 3230,
                                "low": 3140,
                                "end": 3160,
                            },
                            {
                                "month": "2026-02-01T00:00:00+00:00",
                                "games": 180,
                                "average": 3150.0,
                                "peak": 3190,
                                "low": 3102,
                                "end": 3143,
                            },
                        ],
                    },
                    "blitz": {
                        "games": 57,
                        "start": 2860,
                        "end": 2884,
                        "net": 24,
                        "peak": 2901,
                        "low": 2840,
                        "series": [
                            {
                                "month": "2026-02-01T00:00:00+00:00",
                                "games": 57,
                                "average": 2875.0,
                                "peak": 2901,
                                "low": 2840,
                                "end": 2884,
                            }
                        ],
                    },
                },
                "main_perf": "bullet",
                "best_gain": "blitz",
            },
            "player_type": {
                "label": "Offbeat Strategist",
                "confident": True,
                "scores": {"positional": 46, "aggressive": 26, "tactical": 28},
                "leaning": "positional",
                "signature": {
                    "tag": "offbeat",
                    "adjective": "Offbeat",
                    "share": 0.1042,
                    "lift": 10.4,
                },
                "shades": ["Bobby Fischer", "Magnus Carlsen", "Fabiano Caruana"],
                "games": 357,
                "min_games": 30,
                "disclaimer": (
                    "A playful heuristic from your openings and how your games ended "
                    "-- not a measurement."
                ),
                "signals": {
                    "gambit_share": 0.0224,
                    "draw_share": 0.028,
                    "forced_ending_share": 0.6421,
                    "flagged_share": 0.2185,
                    "avg_plies": 62.4,
                    "avg_opening_captures": 1.35,
                    "tagged_games": 336,
                },
            },
        },
    }
    payload.update(overrides)
    return payload


def sparse_payload() -> dict[str, Any]:
    """A player with a handful of games: every degraded branch at once.

    No best win, no streak, no tilt signal, a classifier with no opinion and a
    quality section that refuses to quote a number.
    """
    payload = full_payload()
    payload["coverage"] = {
        "games_total": 6,
        "games_analysed": 5,
        "analysis_rate": 0.8333,
        "min_analysed_games": 20,
        "quality_reliable": False,
        "caveat": (
            "Only 5 of 6 games were analysed -- too few to say anything honest about accuracy."
        ),
    }
    sections = payload["sections"]
    sections["headline"] |= {
        "games": 6,
        "wins": 3,
        "draws": 0,
        "losses": 3,
        "win_rate": 0.5,
        "score": 0.5,
        "hours_played": 0.4,
        "moves_played": 210,
        "days_played": 2,
        "by_perf": [],
        "best_win": None,
        "longest_win_streak": None,
        "busiest_day": None,
    }
    sections["quality"] = {
        "games_analysed": 5,
        "min_analysed_games": 20,
        "available": False,
        "totals": None,
        "by_month": [],
        "by_week": [],
    }
    sections["time_behaviour"] |= {"peak_hour": None, "longest_session": None, "tilt": None}
    sections["openings"] |= {
        "distinct_ecos": 2,
        "best": None,
        "worst": None,
        "by_color": {
            "white": {"games": 3, "distinct_ecos": 1, "top": []},
            "black": {"games": 3, "distinct_ecos": 1, "top": []},
        },
        "tree": {"plies": 8, "min_games": 2, "white": [], "black": []},
    }
    sections["progression"] = {"by_perf": {}, "main_perf": None, "best_gain": None}
    sections["player_type"] |= {
        "label": None,
        "confident": False,
        "scores": {"positional": 33, "aggressive": 34, "tactical": 33},
        "signature": None,
        "shades": [],
        "games": 6,
        "signals": sections["player_type"]["signals"] | {"avg_plies": None},
    }
    return payload


def empty_payload() -> dict[str, Any]:
    """A window with no games at all -- the report exists but has nothing in it."""
    payload = sparse_payload()
    payload["coverage"] |= {"games_total": 0, "games_analysed": 0, "analysis_rate": 0.0}
    payload["sections"]["headline"] |= {
        "games": 0,
        "wins": 0,
        "draws": 0,
        "losses": 0,
        "win_rate": 0.0,
        "score": 0.0,
        "hours_played": 0.0,
        "moves_played": 0,
        "days_played": 0,
    }
    payload["sections"]["player_type"] |= {
        "scores": {"positional": None, "aggressive": None, "tactical": None},
        "leaning": None,
        "games": 0,
    }
    return payload
