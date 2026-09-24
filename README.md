# GB Power Weekly / Mazao Energy Data Analytics

A FastAPI + Jinja2 (server-rendered, no frontend framework) app over a
local SQLite database, ingesting public GB electricity-market data
(Elexon Insights, NESO's Data Portal and Carbon Intensity API, LCCC's
CKAN portal, PV_Live, ENTSO-E/SEMO) and serving four pages:

- **GB Power Weekly** (`/gbpw`) — a weekly recap report, optionally with
  an AI-written narrative (see `ANTHROPIC_API_KEY` below).
- **BESS Analytics** (`/bess`) — battery participation in EAC auctions
  and the Balancing Mechanism.
- **Live Market** (`/live`) — today's generation mix, carbon intensity,
  GB power flow, and fundamentals, refreshed automatically every 5
  minutes.
- **PPA Tools** (`/ppa`, `/ppa/pricing`) — historical wind/solar capture
  price analysis, and an MVP PPA pricing engine for a hypothetical Solar
  project.

## Requirements

- Python **3.11+**
- No external services required to run it — SQLite is a plain file
  (`data/gbpw.db` by default), and every data source used by default is
  a public, no-key API. Two *optional* integrations need a key (below).

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate    # macOS/Linux

pip install -e ".[dev]"
```

This installs the `gbpw` console script (from `pyproject.toml`'s
`[project.scripts]`) along with `pytest` for the test suite. Without
`[dev]` you get the app's own runtime dependencies only.

If `gbpw: command not found` (its install location isn't always on
every shell's `PATH`, e.g. Git Bash on Windows even when the same
install works fine from PowerShell/cmd), run it as a module instead —
`python -m gbpw.cli ...` in place of `gbpw ...` everywhere below; both
invoke the exact same code.

## Optional environment variables

Neither is required to run the app — each just disables one feature
when unset (both fail soft, never crash the server):

| Variable | Enables | Without it |
|---|---|---|
| `ANTHROPIC_API_KEY` | AI-written headline/byline for GB Power Weekly | Falls back to `narrative_rules.py`'s rule-based narrative automatically |
| `ENTSOE_KEY` | Scheduled interconnector flows (ENTSO-E Transparency Platform) on Live Market | That series is simply not ingested |

There's no `.env` file mechanism in this project — set them as plain
environment variables in whatever shell/service starts the app.

## Running the server

```bash
gbpw serve --reload
```

Starts the dev server at `http://127.0.0.1:5000` (defaults; override
with `--host`/`--port`). `--reload` restarts on code changes and is
dev-only — omit it for anything else. On startup the app also launches a
background thread (`background_refresh.py`) that re-ingests BESS/Live
Market/PPA data every 5 minutes for as long as the process runs, so
those three pages populate themselves live once the server is up — no
manual ingest needed for them.

**GB Power Weekly is the one exception**: `/gbpw` shows *built reports*,
not live data, so it needs at least one report built before it has
anything to show:

```bash
gbpw run                       # ingest the most recently completed week + build it
# or, for a specific week:
gbpw ingest --week-ending 2026-09-13
gbpw build --week-ending 2026-09-13 --out out/gbpw-2026-09-13.html
gbpw publish --week-ending 2026-09-13
```

For a deep historical backfill (BESS Analytics/PPA Tools benefit from
more history than the default trailing window), the same `ingest`
command takes an arbitrarily large `--history-days`:

```bash
gbpw ingest --week-ending 2026-09-13 --history-days 3646   # ~10 years
```

This is chunked and resumable — `fetch_log` records every attempt, so
re-running the same command after a partial failure only retries what
didn't already succeed. See `gbpw <command> --help` or `src/gbpw/cli.py`'s
own module docstring for the full command reference (`ingest-eac`,
`ingest-bm`, `status`, `load-fuel-types`, etc.).

## Running the tests

```bash
python -m pytest
```

## Deployment

**Nothing in this repo currently deploys it anywhere** — this section is
a starting point, not documentation of an existing setup, and assumes a
single Linux host (a VPS or similar). Adjust for your actual target.

**Run exactly one server process.** The background-refresh thread starts
inside the app itself (`create_app()`), not as a separate worker — running
multiple `uvicorn` processes/workers would each spin up their own
independent 5-minute polling loop against the same external APIs and the
same SQLite file, multiplying external API load for no benefit (SQLite
doesn't parallelize writes anyway). If you outgrow a single process,
that means moving background_refresh out into its own scheduled job and
SQLite to a real server-based database first — not just adding workers.

A minimal shape:

```
Internet -> reverse proxy (nginx / Caddy, TLS termination) -> the app (single instance) -> SQLite file on disk
```

**Persist `data/` across deploys, however you run it.** `data/*.db*` is
gitignored on purpose (it's a local database file, not source) — a
fresh `git pull`, redeploy, or container rebuild must not wipe or
recreate this directory, or you lose all ingested history. Back it up
like you would any production database.

### Option A: Docker

```bash
docker compose up -d --build
```

`Dockerfile` + `docker-compose.yml` at the repo root build the app and
run it as one container, bind-mounting `./data` and `./out` from the
host (see the volume-mount comments in `docker-compose.yml`) so the
database persists across `docker compose down`/rebuilds exactly like it
would running the app directly. Set `ANTHROPIC_API_KEY`/`ENTSOE_KEY` in
the shell environment, or in an optional `.env` file next to
`docker-compose.yml` (that `.env` is Compose's own variable-substitution
file, not something the app itself reads — it's just how the variables
get into the container's environment here). Not built/run in this repo's
own CI, since there isn't one — verify locally with `docker compose up`
before relying on it.

Put a reverse proxy (nginx, Caddy, etc. — either on the host or as a
second Compose service) in front of it for TLS; don't expose the
container's port directly to the internet.

`deploy/setup.sh` automates this whole option end to end on a fresh
Ubuntu host (24.04/26.04 LTS, full or Minimal) — installs Docker,
clones/updates the repo, brings the app up via `docker-compose.yml`,
and configures the Nginx reverse proxy above. Idempotent, so re-running
it after a `git pull` rebuilds and restarts cleanly:

```bash
curl -fsSL https://raw.githubusercontent.com/IanMIsik/Mazao-Market-Intelligence/master/deploy/setup.sh | bash
```

### Option B: systemd, no container

Example unit (`/etc/systemd/system/gbpw.service`):

```ini
[Unit]
Description=GB Power Weekly / Mazao Energy Data Analytics
After=network.target

[Service]
User=gbpw
WorkingDirectory=/opt/gbpw
Environment=ANTHROPIC_API_KEY=...
Environment=ENTSOE_KEY=...
ExecStart=/opt/gbpw/.venv/bin/gbpw serve --host 127.0.0.1 --port 5000
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Then point a reverse proxy (nginx, Caddy, etc.) at `127.0.0.1:5000` for
TLS and the public domain — do not expose uvicorn directly to the
internet without one.

**Scheduled report builds**: if you want GB Power Weekly to rebuild
itself automatically (rather than running `gbpw run` by hand), add a cron
job or systemd timer calling `gbpw run` on whatever cadence you want new
weekly reports — `scripts/run_weekly.ps1` is the existing Windows/
Task Scheduler equivalent of this, not currently ported to a Linux
cron/timer.
