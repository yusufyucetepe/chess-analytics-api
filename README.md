# Chessona

**Your Lichess year, read back to you.**

Type a Lichess username. Chessona streams that player's last twelve months of rated
games from the Lichess API, stores them, computes a Spotify-Wrapped-style report —
openings, time behaviour, accuracy, rating progression, a playful player type — and
serves it at a shareable URL. Then it hands back the positions where the engine says
they went wrong, as puzzles to replay.

The backend is the point. The frontend exists so the backend has something to show.

> **Demo:** _not deployed yet_ — `docker compose up --build` brings up the whole
> thing locally (see [Running it](#running-it)).

---

## Contents

- [What it does](#what-it-does)
- [Architecture](#architecture)
- [The life of a request](#the-life-of-a-request)
- [Running it](#running-it)
- [HTTP API](#http-api)
- [Design notes](#design-notes)
- [Data model](#data-model)
- [Tests](#tests)
- [Project layout](#project-layout)
- [Credits](#credits)

---

## What it does

A finished report has seven sections, computed with SQL aggregates against Postgres
rather than Python loops over a year of blitz:

| Section | What's in it |
|---|---|
| **headline** | Games, hours at the board, W/D/L, best win by opponent rating, longest win streak, busiest day |
| **openings** | Top openings per colour, best and worst by score, a repertoire tree from stored opening lines, how many systems cover most of the year |
| **quality** | Accuracy, ACPL, blunders/mistakes/inaccuracies — always carrying the coverage caveat below |
| **time_behaviour** | Games by hour and weekday, win rate by hour, longest session, a tilt signal (win rate right after two losses) |
| **progression** | Rating over time per time control, peak, net change |
| **player_type** | Three normalised scores — positional / aggressive / tactical — against a reference population of players in the same rating band |
| **puzzles** | Positions from their own games where the engine flagged a mistake, playable at `/u/{username}/puzzles` |

### The constraint that shapes half of it

Chessona does not run Stockfish. Every quality number comes only from games the
player themselves asked Lichess to analyse — often a small, self-selected fraction of
their history. So:

1. Every quality stat states its coverage: *"based on 84 of 1,207 games analysed."*
2. Under `quality_min_analysed_games` (20) the section degrades to a soft state
   instead of showing a number that means nothing.
3. Analysed-game accuracy is never extrapolated to the full year. Players analyse
   their losses.

Everything else — openings, time, streaks, progression, player type — needs no engine
and works on 100% of games.

---

## Architecture

One deployable, two commands. The same image runs the API and the worker; which one
you get is the command line.

```mermaid
flowchart LR
    subgraph browser [Browser]
        UI["Jinja2 + htmx<br/>Chart.js"]
    end

    subgraph app [Same image, two commands]
        API["<b>API</b><br/>uvicorn app.main:app"]
        W["<b>Worker</b><br/>arq app.worker.WorkerSettings"]
    end

    subgraph state [State]
        PG[("PostgreSQL<br/>players · games · puzzles · reports")]
        R[("Redis<br/>queue · lock · cache · rate limit")]
    end

    LI["Lichess API<br/>NDJSON export"]

    UI -- "POST /reports" --> API
    UI -- "poll every 2s" --> API
    API -- "enqueue job" --> R
    API -- "read report row" --> PG
    API -. "confirm account exists" .-> LI
    R -- "dequeue" --> W
    W == "stream games (httpx)" ==> LI
    W -- "upsert in batches of 500" --> PG
    W -- "aggregate into payload" --> PG
    W -- "evict cache, release lock" --> R
```

Redis wears four hats and they are deliberately separate keyspaces: the **arq queue**,
the per-username **ingest lock** that stops two exports of the same player, the
**report cache** in front of the reports table, and the **fixed-window rate limiter**
that keeps a public site from being turned into a Lichess scraping proxy.

**Stack:** Python 3.12 · FastAPI · Pydantic v2 · SQLAlchemy 2.0 async (asyncpg) ·
Alembic · Redis + arq · httpx · python-chess · Jinja2 + htmx + Chart.js ·
pytest / ruff / mypy (strict) · Docker · GitHub Actions.

---

## The life of a request

```
POST /api/v1/reports {"username": "..."}
  │
  ├─ 1. validate the username's shape, then confirm the account exists
  │       (the only synchronous Lichess call the API makes)
  │
  ├─ 2. do we already hold a finished report?
  │       younger than 24h  ────────────────────────────► 200, return it
  │       older than 7 days ────────────────────────────► rebuild
  │       in between        ─► has Lichess's rated-game
  │                            count moved since we fetched?
  │                              no ─────────────────────► 200, return it
  │                              yes ────────────────────► rebuild
  │
  ├─ 3. SET NX the ingest lock keyed on the username
  │       lock held by a live report ───────────────────► 202, that report's id
  │       lock held by a dead one ──────────────────────► take it over
  │
  └─ 4. INSERT the report row (status=pending), enqueue, return 202
```

The lock, not the row, is what prevents duplicate exports: "is one already running?"
read from the database is a read followed by a write, and two simultaneous requests
can both win it.

Then the worker's `build_report(report_id)` — which takes the id and nothing else, so
a retried, replayed or hand-enqueued job behaves identically:

```
mark running
  → fetch profile + rating history
  → stream the NDJSON export, parse and upsert in batches of 500
  → mine one puzzle per analysed game into a bounded per-month pool
  → compute the seven sections from Postgres
  → write payload, mark done, evict the cache, release the lock
```

The browser polls `GET /reports/{id}/status`; when the job is done the fragment
answers with `HX-Redirect` so the result arrives as an ordinary page load at a URL
worth sharing, rather than as a fragment swapped into the page it was requested from.

---

## Running it

### Docker (everything)

```bash
git clone https://github.com/yusufyucetepe/chess-analytics-api.git
cd chess-analytics-api
docker compose up --build
```

→ <http://localhost:8000>. Migrations run on API startup; Postgres and Redis come up
as services; the worker starts alongside. Dev ports are deliberately shifted
(**Postgres 5433**, **Redis 6380**) so a Postgres or Redis already installed on the
host doesn't shadow them.

`docker-compose.override.yml` is applied automatically and adds hot reload for both
processes — including CSS and JS, which the default `--reload` watcher ignores.

### Locally, without Docker

```bash
docker compose up -d postgres redis    # still want these two
uv pip install -e ".[dev]"
cp .env.example .env
alembic upgrade head
uvicorn app.main:app --reload          # terminal 1
arq app.worker.WorkerSettings          # terminal 2
```

### Configuration

Everything is environment-driven through `app/core/config.py`. The ones that matter:

| Variable | Default | Notes |
|---|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://chess:chess@localhost:5432/chess` | asyncpg driver |
| `REDIS_URL` | `redis://localhost:6379/0` | queue, cache, locks, limits |
| `LICHESS_TOKEN` | _unset_ | optional; raises Lichess rate limits |
| `LICHESS_USER_AGENT` | `chessona/0.1 (+…)` | Lichess asks for a contact URL — **put yours here** |
| `RATE_LIMIT_PER_HOUR` | `10` | report builds per IP; `0` disables |
| `RATE_LIMIT_READS_PER_HOUR` | `120` | reads that recompute; `0` disables |
| `REPORT_WINDOW_DAYS` | `365` | how much of a year a year is |
| `DEBUG` | `false` | debug logging |

### One username, no browser

```bash
python -m scripts.ingest Zhigalko_Sergei --days 30 --dry-run   # parse, print, touch no database
python -m scripts.ingest Zhigalko_Sergei --days 365            # ingest for real
```

`--dry-run` is the quickest way to see whether today's export has grown a shape the
parser doesn't know about.

---

## HTTP API

Interactive docs at `/docs` (FastAPI's, generated from the same Pydantic models).

| Method | Path | Behaviour |
|---|---|---|
| `POST` | `/api/v1/reports` | `202` with a report id, or `200` with a recent report as-is. `404` if no such Lichess account, `429` over the IP limit |
| `GET` | `/api/v1/reports/{report_id}` | Status while pending or running; the payload once done |
| `GET` | `/api/v1/players/{username}/report` | The latest completed report, served from Redis through to the table. Never starts work — a username we've never seen is a `404`, because work created from a `GET` would be an unmetered path to the Lichess export |
| `GET` | `/api/v1/players/{username}/openings` | The repertoire tree alone, recomputed from the games table (which may cover a window the last report doesn't) |
| `GET` | `/healthz` | Liveness. Checks no dependencies on purpose |
| `GET` | `/readyz` | Readiness. Pings Postgres and Redis, `503` if either is down |

Server-rendered pages, outside the schema:

| Path | |
|---|---|
| `/` | The form |
| `/u/{username}` | The report |
| `/u/{username}/puzzles` | The playable half |

---

## Design notes

The things a reviewer would otherwise have to reconstruct from the code.

**The report row *is* the job record.** No jobs table. Status, error, counts, and the
finished payload all live on `reports`, so there is exactly one place to look and no
way for the two to disagree.

**No moves table.** A year of blitz is a lot of rows for no payoff. The repertoire
tree only needs the first 12 plies, so those live in a `TEXT[]` column on `games` and
the rest is dropped once the derived stats are computed.

**The retry budget is ours, not arq's.** arq only re-runs a job that raises
`arq.Retry`; anything else marks it finished. If arq's own budget ran out first the
job would vanish with the report stuck in `running` and nothing to explain it. So the
job counts its own attempts, retries only on network faults, and only the last attempt
writes `failed` — a poller never sees a report fail and then quietly un-fail.

**Swing is measured on the win curve, not in centipawns.** +900 → +500 is nothing;
+100 → −300 is the game. Raw centipawns rank those the other way round, so puzzle
candidates are scored through Lichess's own win-probability curve
(`2/(1+e^(−0.00368208·cp)) − 1`).

**Puzzles are persisted during ingest, never recomputed.** The per-ply `analysis`
array exists for exactly one moment — while the export is streaming — and getting it
back means asking Lichess for the entire year again. So `evals=true` stays on the
export even though no v1 report section reads it, and the puzzle is mined and stored
as the games go past.

**Puzzles can never fail an ingest.** A game the extractor can't read loses its
puzzle; a fault in the store is logged, rolled back, and the year is kept. It's the
only place in the pipeline that swallows an exception, and the reason is that the
games are the product.

**The board ships a `{from: [to]}` legality map, not an engine.** Computed once at
ingest with python-chess, so the page can refuse an illegal move without shipping a
chess engine to the browser. There's no session and no progress tracking — the answer
travels with the page, and the game it came from is linked right under the board.

**One puzzle per game, dealt one month at a time.** Ten positions from a single
terrible evening is a worse read than ten months of a year, so the selection deals
round-robin across calendar months rather than ranking purely on swing.

**Migrations are what build the test schema.** `conftest.py` runs Alembic rather than
`Base.metadata.create_all`: creating the schema from the models would let a migration
drift from them and still pass the whole suite. CI additionally runs `alembic check`.

**The test suite gets its own Redis database.** Postgres was isolated from the start
because tests drop the schema and that damage is obvious. Redis wasn't, because
flushing a cache looks harmless — and the damage was worse: an arq queue is Redis
keys, so a development worker on the same database picks up jobs the *tests* enqueued,
runs them against its own Postgres, and leaves the test's burst worker draining an
empty queue. Three worker tests then failed about one run in ten, in three different
places, none of them near the cause. Tests now resolve to database 15 and refuse to
start if that lands on the application's own.

**Every tuning number lives in `app/core/config.py`**, each with a comment saying why
it is what it is — swing thresholds, pool sizes, TTLs, classifier bounds. Nothing is
tuned by editing logic.

---

## Data model

Six tables. Every migration in `alembic/versions/`, and CI fails on drift between the
models and the migrations.

```
players ──┬──< games ──< puzzles          openings_meta   (seeded reference data:
          │     (player_id, game_id) PK    eco → family, style tags)
          └──< reports                    style_reference (per rating band, per perf:
                (status, payload JSONB,                    the centre of the population
                 schema_version)                           player_type scores against)
```

- **`games`** keys on `(game_id, player_id)` — the Lichess game id is a natural key,
  which is what makes re-ingest idempotent. Indexed on `(player_id, played_at)`,
  `(player_id, eco)`, `(player_id, analysed)`.
- **`puzzles`** stores FEN, the move played in SAN, the engine's move in UCI *and*
  SAN, its continuation, Lichess's own sentence about the mistake, the win percentage
  either side of it, and the `{from: [to]}` legality map. Cascades from its game.
- **`reports`** carries a `schema_version`, so a payload written by an older build is
  recognisable rather than silently mis-rendered.

---

## Tests

```bash
docker compose up -d postgres redis
pytest                                    # 538 tests
pytest --cov=app --cov-report=term-missing
ruff check . && ruff format --check . && mypy app
```

- **Unit** — report computation against fixture rows, the classifier against
  hand-built players whose type is obvious, puzzle extraction against real recorded
  games (including castling, en passant and promotion), template rendering.
- **Integration** — httpx `AsyncClient` against the real app with a real Postgres and
  Redis, including one end-to-end test that enqueues a job, runs the worker inline,
  and asserts the finished payload.
- **The Lichess client is never called live.** Recorded NDJSON fixtures and `respx`,
  so CI is offline and deterministic.
- **`tests/unit/test_suite_isolation.py`** fails if either database isolation is
  removed. It exists because removing one cost a day.

CI (GitHub Actions) runs lint → typecheck → tests against service containers →
`alembic check` → Docker build, publishing to GHCR from `main` only.

---

## Project layout

```
app/
  api/          FastAPI routers — v1 JSON API, health probes
  core/         config, Redis, Lichess client lifecycle, locks, rate limiting
  lichess/      streaming NDJSON client, typed schemas, error taxonomy
  ingest/       NDJSON → rows, batched upserts, puzzle mining
  puzzles/      candidate extraction (python-chess), the per-month pool
  report/       section builders, payload assembly, cache, player-type weights
  web/          Jinja2 templates, htmx routes, Chart.js glue, static assets
  worker/       arq settings, the build_report job, failure classification
alembic/        migrations, including the openings_meta seed
tests/          unit + integration, recorded fixtures
docs/BRIEF.md   the original spec, and what each decision cost
```

---

## Credits

Game data from the [Lichess API](https://lichess.org/api) — free, open, and
generously rate-limited. Chess piece artwork © 2006 Colin M.L. Burnett, used under
BSD-3-Clause; see [`app/web/static/pieces/NOTICE.md`](app/web/static/pieces/NOTICE.md).

Not affiliated with Lichess.
