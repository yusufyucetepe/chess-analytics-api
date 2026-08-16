"""The table behind "Shades of". Data only, no logic.

Thirty players, each placed on the same three axes the classifier scores you on
and tagged with the openings they are remembered for. A report's shades are the
nearest three, so the list is the whole personality of the feature: swapping a
name in changes who people are told they resemble, and nothing else.

The numbers are judgement calls about how these players are *remembered*, in the
same spirit as ``player_type_weights`` -- nobody ran an engine over a career to
produce them, and they are not a ranking. They only have to place each player
relative to the others.

Tags are restricted to ``FLAVOUR_TAGS``: those are the only ones a report can
carry as a signature, so any other tag here could never match anything.
"""

from typing import Final

#: ``name -> (positional, aggressive, tactical), tags``. The triple is in ``AXES``
#: order and sums to 100, exactly like the scores it is compared against.
FAMOUS_PLAYERS: Final[tuple[tuple[str, tuple[int, int, int], tuple[str, ...]], ...]] = (
    ("Mikhail Tal", (10, 50, 40), ("gambit", "sharp", "unbalanced")),
    ("Frank Marshall", (12, 48, 40), ("gambit", "sharp")),
    ("Paul Morphy", (15, 50, 35), ("gambit", "open")),
    ("Judit Polgar", (20, 45, 35), ("sharp", "open")),
    ("Baadur Jobava", (20, 40, 40), ("offbeat", "unbalanced")),
    ("Alexander Alekhine", (25, 45, 30), ("sharp", "unbalanced")),
    ("Garry Kasparov", (25, 40, 35), ("sharp", "dynamic", "open")),
    ("Alireza Firouzja", (24, 42, 34), ("sharp", "dynamic")),
    ("Richard Rapport", (30, 30, 40), ("offbeat", "hypermodern")),
    ("Hikaru Nakamura", (30, 35, 35), ("offbeat", "dynamic")),
    ("Ian Nepomniachtchi", (35, 35, 30), ("dynamic", "open")),
    ("Levon Aronian", (40, 30, 30), ("dynamic", "sharp")),
    ("Viswanathan Anand", (40, 25, 35), ("open", "dynamic")),
    ("Boris Spassky", (45, 30, 25), ("flexible", "open")),
    ("Bobby Fischer", (45, 25, 30), ("open", "sharp")),
    ("Fabiano Caruana", (48, 22, 30), ("sharp", "open")),
    ("Emanuel Lasker", (50, 25, 25), ("flexible", "unbalanced")),
    ("Magnus Carlsen", (55, 20, 25), ("offbeat", "flexible")),
    ("Viktor Korchnoi", (55, 25, 20), ("solid", "closed")),
    ("Hou Yifan", (57, 19, 24), ("solid", "flexible")),
    ("Ding Liren", (60, 15, 25), ("solid", "flexible")),
    ("Wesley So", (65, 12, 23), ("solid", "flexible")),
    ("Aron Nimzowitsch", (68, 10, 22), ("hypermodern", "closed")),
    ("Richard Réti", (71, 9, 20), ("hypermodern", "flexible")),
    ("Mikhail Botvinnik", (70, 12, 18), ("closed", "solid")),
    ("Anatoly Karpov", (70, 10, 20), ("solid", "closed")),
    ("Vladimir Kramnik", (72, 10, 18), ("solid", "closed")),
    ("José Raúl Capablanca", (73, 9, 18), ("solid", "open")),
    ("Vasily Smyslov", (75, 8, 17), ("solid", "closed")),
    ("Tigran Petrosian", (78, 8, 14), ("closed", "solid")),
)

#: How many names a report shows.
SHADES_SHOWN: Final = 3

#: Points of distance forgiven when a player shares the report's signature tag.
#: Small on purpose: it breaks ties between similar scores and lets a matching
#: repertoire jump a near neighbour, but it cannot pull an attacker into a
#: strategist's list. Roughly one axis moving by five points.
SHADES_TAG_BONUS: Final = 7.0
