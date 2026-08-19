# Chess Wrapped — Project Brief

A public web app: you enter a Lichess username, it ingests the last 12 months of that
player's games in the background, and returns a "Spotify Wrapped"-style report about
their chess year. Backend is the point of the project; the frontend exists to show the
backend off.

## Stack

- Python 3.12, FastAPI, Pydantic v2
- PostgreSQL + SQLAlchemy 2.0 (async, `asyncpg`) + Alembic migrations
- Redis: arq job queue **and** response cache
- httpx (async, streaming) for the Lichess API
- pytest + pytest-asyncio, ruff, mypy
- Docker (dev compose + multi-stage prod image), GitHub Actions CI
- Frontend: Jinja2 templates + HTMX for job polling + Chart.js. One deployable, no SPA.

## Data source: Lichess public API

No auth token strictly required, but support an optional `LICHESS_TOKEN` env var to
raise rate limits. Always send a descriptive `User-Agent` with a contact URL.

Primary endpoint: `GET https://lichess.org/api/games/user/{username}`
with `Accept: application/x-ndjson` — streams one JSON game per line.

Query params we use:

| param | value | why |
|---|---|---|
| `since` / `until` | epoch ms | 12-month window |
| `rated` | `true` | skip casual noise |
| `opening` | `true` | ECO code + opening name per game |
| `evals` | `true` | include analysis when it exists |
| `accuracy` | `true` | accuracy % when the game was analysed |
| `clocks` | `false` | not needed in v1, keeps payload small |
| `moves` | `true` | needed for the opening tree |
| `sort` | `dateAsc` | stable ingest ordering |

Also used:
- `GET /api/user/{username}` — profile, current ratings, account existence check
- `GET /api/user/{username}/rating-history` — rating progression per time control

Rate limits: anonymous export runs around 20 games/sec and Lichess will 429 if pushed.
Stream, don't buffer; back off on 429 with `Retry-After`; never run more than one
ingest per username concurrently.

## Critical constraint: engine analysis is sparse

We do **not** run Stockfish. Accuracy, ACPL, and blunder/mistake/inaccuracy counts come
only from games the player already requested analysis on — often a small fraction of
their history. Per game, when analysed, the NDJSON has roughly:

```
players.white.analysis = { inaccuracy, mistake, blunder, acpl, accuracy }
```

(Confirm the exact field names against a live response early — this shape drives the
whole quality module.)

Implications, and these are requirements, not nice-to-haves:

1. Every quality stat must carry its coverage: "based on 84 of 1,207 games analysed."
2. If coverage is under a threshold (say 20 games), the quality section degrades to a
   soft state rather than showing a misleading number.
3. Never extrapolate analysed-game accuracy to the full history as if it were the
   player's true average — analysed games are self-selected and biased.

Everything else — openings, time behaviour, streaks, rating progression, player type —
needs no engine and works on 100% of games.

## Database schema

**players**
`id` PK, `username_lower` unique (lookup key), `display_name`, `title`, `country`,
`created_at`, `last_fetched_at`, `ratings` JSONB (per perf type).

**games**
`id` TEXT PK (Lichess game id — natural key, makes re-ingest idempotent),
`player_id` FK, `played_at` TIMESTAMPTZ, `perf` (bullet/blitz/rapid/classical),
`rated` BOOL, `color`, `opponent_name`, `opponent_rating`, `player_rating`,
`rating_diff`, `result` (win/loss/draw), `status` (mate/resign/outoftime/draw/...),
`eco`, `opening_name`, `opening_ply`, `moves_count`,
`clock_initial`, `clock_increment`,
`analysed` BOOL, `accuracy` NUMERIC NULL, `acpl` INT NULL,
`inaccuracies` INT NULL, `mistakes` INT NULL, `blunders` INT NULL,
`opening_line` TEXT[] — first 12 plies in SAN.

Indexes: `(player_id, played_at)`, `(player_id, eco)`, `(player_id, analysed)`.

**No moves table.** Storing full move lists for a year of blitz is a lot of rows for
no payoff. The opening tree only needs the first ~12 plies, so we store them as an
array column on `games` and drop the rest after computing derived stats.

**reports**
`id` UUID PK, `player_id` FK, `period_start`, `period_end`,
`status` ENUM(pending|running|done|failed), `created_at`, `completed_at`,
`error` TEXT NULL, `games_total` INT, `games_analysed` INT,
`payload` JSONB (the finished report), `schema_version` INT.

The report row *is* the job record — no separate jobs table.

**openings_meta** (seeded from a static file, not user data)
`eco` PK, `family`, `style_tags` TEXT[] — e.g. `{sharp, gambit}` or `{solid, closed}`.
Used by the player-type classifier.

## API endpoints

```
POST   /api/v1/reports                     {username} -> 202 {report_id, status}
GET    /api/v1/reports/{report_id}         status, or full payload when done
GET    /api/v1/players/{username}/report   latest completed report (cached)
GET    /api/v1/players/{username}/openings repertoire tree, white and black
GET    /healthz                            liveness
GET    /readyz                             checks Postgres + Redis
```

`POST /api/v1/reports` behaviour:
1. Validate username shape, then confirm it exists via the Lichess user endpoint.
2. If a completed report for this player is younger than 24h, return it immediately
   (`200`, not `202`) — no new job.
3. Otherwise set a Redis lock keyed on the username. If the lock is held, return the
   in-flight report id instead of enqueueing a duplicate.
4. Enqueue the arq job, return `202` with the report id.

IP rate limit on `POST` (Redis fixed-window counter, e.g. 10/hour) so a public site
can't be turned into a Lichess scraping proxy.

## Worker pipeline (arq)

`build_report(report_id)`:
1. mark `running`
2. fetch profile + rating history
3. stream games NDJSON for the window, parse and upsert in batches of ~500
4. compute the report sections from Postgres (SQL aggregates where possible, not
   Python loops over every game)
5. write `payload`, counts, mark `done`, release the lock, cache in Redis

Failures mark `failed` with a user-safe message. arq retries on network errors only,
not on "user doesn't exist".

## Report sections

- **headline** — games played, hours, win/draw/loss, best win by opponent rating,
  longest win streak, busiest day
- **openings** — top openings by colour, best and worst by score, repertoire tree
  built from `opening_line`, breadth stat (how many distinct ECOs)
- **quality** — average accuracy, ACPL, blunders/mistakes/inaccuracies totalled and
  bucketed by week / month / year, always with the coverage caveat above
- **time_behaviour** — games by hour of day and weekday, win rate by hour, longest
  session, tilt signal (win rate in games played right after two losses)
- **progression** — rating over time per perf, peak, net change
- **player_type** — three normalised scores: positional / aggressive / tactical

## Player-type classifier

Heuristic, no engine. Inputs: ECO style tags from `openings_meta`, share of games in
gambit lines, average game length in plies, share of games ending in mate or
resignation versus timeout, decisive-vs-draw ratio, average material-exchange density
in the first 12 plies. Combine into three scores that sum to 100 and pick a label.

Keep the weights in one config module so they can be tuned without touching logic, and
label it in the UI as a playful heuristic rather than a measurement. Unit-test it with
a handful of hand-built fixture players whose type is obvious.

## Docker

- `Dockerfile`: multi-stage, builder installs deps, slim runtime, non-root user,
  healthcheck. Same image runs API and worker via different commands.
- `docker-compose.yml`: `api`, `worker`, `postgres`, `redis`.
- `docker-compose.override.yml`: dev-only hot reload and exposed ports.
- `.dockerignore` that actually excludes `.git`, `.venv`, tests, caches.

## CI (GitHub Actions)

On push and PR: ruff check + format check, mypy, then pytest with `postgres` and
`redis` service containers, then build the Docker image (push to GHCR on main only).
Fail the build on coverage regression once there's a baseline.

## Testing

- Unit: report computation functions against fixture game rows, classifier fixtures.
- Integration: httpx `AsyncClient` against the app with a real test Postgres and Redis.
- Lichess client: recorded NDJSON fixtures, never live calls in CI.
- One end-to-end test that enqueues a job, runs the worker inline, and asserts the
  report payload shape.

## Build order

1. Repo scaffold, Docker compose, Postgres + Alembic, `/healthz`, CI green on an
   empty test suite. Get the boring infrastructure working before any chess logic.
2. Lichess client with streaming + retry + recorded fixtures.
3. Models, migrations, ingest into Postgres, verified on a real username.
4. arq worker and the reports job lifecycle end to end, payload still nearly empty.
5. Report sections one at a time, tests alongside: headline, openings, time, quality,
   progression.
6. Player-type classifier.
7. Redis caching, dedupe lock, rate limiting.
8. Jinja + HTMX frontend with polling and charts.
9. README with architecture diagram, a live demo link, and a GIF of the flow.
10. Puzzles from the player's own mistakes. See the section at the foot of this file.

Ship steps 1–4 before touching anything visual. The demo is worthless if the pipeline
isn't real.

## Non-goals for v1

Accounts and login. Chess.com support. Running our own engine. Comparing two players.
Real-time updates. Anything that isn't the single-username report.

## Step 10: puzzles

Source 1 below is **built**. Source 2 is not, and is the obvious next thing here.

Two sources of puzzles:

1. **The player's own mistakes.** Positions pulled from their real games — the moments
   the engine already flagged as a blunder or mistake. The player replays the position
   and gets the chance to find what they missed. This is the reason the feature exists:
   generic tactics are everywhere, tactics from your own bullet collapse last March
   are not.
2. **Random puzzles with themes.** Fork, pin, back-rank mate, endgame, and so on, so
   the feature still has something to offer a player whose year was mostly clean.
   Not built. `GET /api/puzzle/next?angle=<theme>` answers without a token, so the
   data is a call away — the work is that those puzzles have opponent replies, and a
   sequence to play out is a different board from the single-move one we have.

### What this already constrains

- **`evals=true` stays on the game export.** The per-move `analysis` array is the only
  thing that identifies *where* in a game the player went wrong; the per-player
  `{inaccuracy, mistake, blunder, acpl, accuracy}` summary gives counts but no
  positions. It roughly triples the payload and no v1 report section reads it. Keep it
  anyway. See `lichess_include_evals`.
- **Ingest decides how much of that array survives.** The `games` schema stores only
  the first 12 plies of the opening line and has nowhere to put per-ply evals, so
  puzzles will need their own table rather than a column on `games`. Step 3 should at
  minimum not make this harder — dropping the analysis array on the floor without a
  thought is the thing to avoid.

### Settled when the step was built

- **Persisted during ingest, not recomputed.** The `analysis` array exists for exactly
  one moment — while the export is streaming — and getting it back means asking
  Lichess for the whole year again. A bounded pool (`puzzle_pool_per_month` per
  calendar month, so at most twelve times that per player) is written at the end of
  the ingest and replaced wholesale on the next one.
- **Storage format**: FEN, the move played in SAN, the engine's move in UCI *and* SAN,
  its line after it, Lichess's own sentence about the mistake, and the swing either
  side. Plus `legal_moves`, a `{from: [to]}` map computed once with a real rules
  implementation, which is what lets the browser refuse an illegal move without
  shipping a chess engine to the page.
- **One puzzle per game**, and the report's set is dealt one month at a time rather
  than ranked purely on swing. Ten positions from one terrible evening is a worse read
  than ten months of the year.
- **The boards live at `/u/{username}/puzzles`, not in the report.** Reading and
  playing are different modes of attention, and the report is read top to bottom. The
  report keeps a section that draws the first few positions as plain diagrams and
  links across; the header carries a Puzzles link on any page that knows whose year it
  is. The page computes nothing — the pick was made when the report was built.
- **Swing is measured on the win curve, not in centipawns.** +900 to +500 is nothing;
  +100 to -300 is the game. Raw centipawns rank those the other way round.
- **No progress tracking, no session.** Solving is checked in the browser against an
  answer that travelled with the page. It is inspectable and that is fine — the game
  it came from is linked directly under the board. Accounts remain a non-goal.
- **Puzzles never fail an ingest.** A game the extractor cannot read loses its puzzle;
  a fault in the store is logged and the year is kept. Same lesson as the status
  column: the games are the product.

### Still open

- Themed puzzles (source 2 above), and with them a board that plays a sequence.
- Whether the pool is worth keeping bigger than a report shows. It is today, so the
  selection can be changed without a re-export, but nothing yet reads the surplus.
