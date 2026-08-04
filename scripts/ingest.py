"""Run an ingest against the live Lichess API, by hand.

This is the tool that checks the assumption against today's API.

    python -m scripts.ingest Zhigalko_Sergei --days 30 --dry-run
    python -m scripts.ingest Zhigalko_Sergei --days 365

``--dry-run`` parses without a database and prints what would be written, which
is also the quickest way to see whether the export has grown a shape the parser
does not know about.
"""

import argparse
import asyncio
import logging
from collections import Counter
from datetime import UTC, datetime, timedelta

from app.core.config import settings
from app.db.base import SessionLocal
from app.ingest import ingest_player, parse_game
from app.lichess import LichessClient


async def dry_run(username: str, since: datetime, until: datetime) -> None:
    kept: Counter[str] = Counter()
    skipped = 0
    analysed = 0
    total = 0

    async with LichessClient() as client:
        profile = await client.get_profile(username)
        print(f"{profile.username} ({profile.title or 'no title'}) -- {profile.ratings()}")

        async for raw in client.stream_games(profile.username, since=since, until=until):
            total += 1
            row = parse_game(raw, player_id=0, username_lower=profile.id.lower())
            if row is None:
                skipped += 1
                continue
            kept[row["perf"]] += 1
            analysed += bool(row["analysed"])
            if total == 1:
                print("\nfirst parsed row:")
                for key, value in row.items():
                    print(f"  {key:<16} {value!r}")

    print(f"\n{total} fetched, {total - skipped} parsed, {skipped} skipped, {analysed} analysed")
    print(f"by perf: {dict(kept)}")


async def ingest(username: str, since: datetime, until: datetime) -> None:
    async with LichessClient() as client, SessionLocal() as session:
        player, stats = await ingest_player(session, client, username, since=since, until=until)

    print(f"player {player.id} ({player.display_name})")
    print(
        f"{stats.fetched} fetched, {stats.stored} stored, "
        f"{stats.skipped} skipped, {stats.analysed} analysed"
    )
    if stats.stored:
        coverage = 100 * stats.analysed / stats.stored
        print(f"analysis coverage: {coverage:.1f}% -- the quality section's caveat")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("username")
    parser.add_argument("--days", type=int, default=settings.report_window_days)
    parser.add_argument("--dry-run", action="store_true", help="parse only, touch no database")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO)
    until = datetime.now(UTC)
    since = until - timedelta(days=args.days)

    runner = dry_run if args.dry_run else ingest
    asyncio.run(runner(args.username, since, until))


if __name__ == "__main__":
    main()
