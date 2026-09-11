"""
SQLite storage for GB Power Weekly. One file, gitignored, at data/gbpw.db.

Schema:
    prices(series, sd, sp, run, value, fetched_at)  PK (series, sd, sp, run)
    fetch_log(series, sd, ok, note, ts)
    reports(week_ending, facts_json, narrative, run_basis, built_at, published)
    eac_results(...)  -- NESO Enduring Auction Capability, see ingest/eac.py
    bm_unit_reference(national_grid_bm_unit, ...)  -- Elexon BM unit metadata,
        refreshed wholesale (not date-scoped); national_grid_bm_unit is the
        same code space as eac_results.auction_unit -- confirmed live -- so
        a battery's EAC auction_unit joins directly to its Elexon BM data.
    bm_cashflows(sd, sp, national_grid_bm_unit, bid_offer, total_cashflow, ...)
        PK (sd, sp, national_grid_bm_unit, bid_offer) -- Elexon EBOCF, real £
        cashflow per BM unit per settlement period. 'bid' and 'offer' are
        genuinely different, independently-nonzero data (confirmed live) and
        both must be fetched and summed for a unit's total BM revenue. This
        is cashflow only -- no accepted-volume (MWh) data; that needs a
        separate ISPSTACK ingest, not built yet. See ingest/elexon_bm.py.
        idx_bm_cashflows_bmu_sd exists because bm_metrics.bm_activity()'s
        join is driven from the (small) battery-unit side, not the date
        range -- confirmed by EXPLAIN QUERY PLAN and measured live: without
        this index that join takes ~2.5s even for a 7-day window; with it,
        ~100ms. A plain sd index alone doesn't help because the query
        planner doesn't choose to use it in this join shape.

Series stored in `prices`:
    day_ahead        -- Elexon Market Index Data, GBP/MWh          (run='NA')
    imbalance         -- Elexon settlement system price, GBP/MWh    (run=<see below>)
    wind              -- wind generation, MW                       (run='NA')
    total_generation  -- generation-type total (excl. interconnectors), MW (run='NA')
    demand            -- Initial National Demand Outturn, MW        (run='NA')

`run` is a real settlement-run identifier only for `imbalance`. Elexon's
convenient system-prices endpoint never tells us which run a figure came
from -- its own docs say it always returns "the latest available settlement
run" -- so we store it under the sentinel run='latest'. Re-ingesting a period
overwrites that row with whatever Elexon currently considers latest, which
is exactly the "metrics take the latest available run per period" behaviour
asked for, within the limits of what this endpoint exposes. See
ingest/elexon.py for the longer note.

Non-imbalance series don't have a run concept; they use the constant 'NA'
so the primary key still applies uniformly.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

DEFAULT_DB_PATH = Path(__file__).resolve().parents[2] / "data" / "gbpw.db"

NA_RUN = "NA"

SCHEMA = """
CREATE TABLE IF NOT EXISTS prices (
    series      TEXT NOT NULL,
    sd          TEXT NOT NULL,
    sp          INTEGER NOT NULL,
    run         TEXT NOT NULL,
    value       REAL NOT NULL,
    fetched_at  TEXT NOT NULL,
    PRIMARY KEY (series, sd, sp, run)
);

CREATE TABLE IF NOT EXISTS fetch_log (
    series  TEXT NOT NULL,
    sd      TEXT NOT NULL,
    ok      INTEGER NOT NULL,
    note    TEXT,
    ts      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reports (
    week_ending  TEXT PRIMARY KEY,
    facts_json   TEXT NOT NULL,
    narrative    TEXT,
    run_basis    TEXT,
    built_at     TEXT NOT NULL,
    published    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS eac_results (
    neso_id            INTEGER NOT NULL,
    unit_result_id     TEXT,
    service_type       TEXT NOT NULL,
    auction_product    TEXT NOT NULL,
    technology_type    TEXT,
    auction_unit       TEXT NOT NULL,
    participant        TEXT NOT NULL,
    executed_quantity  REAL,
    clearing_price     REAL,
    delivery_start     TEXT NOT NULL,
    delivery_end       TEXT NOT NULL,
    sd                 TEXT NOT NULL,
    sp                 INTEGER NOT NULL,
    post_code          TEXT,
    fetched_at         TEXT NOT NULL,
    PRIMARY KEY (neso_id, sd, sp)
);
CREATE INDEX IF NOT EXISTS idx_eac_sd ON eac_results(sd);
CREATE INDEX IF NOT EXISTS idx_eac_participant ON eac_results(participant);
CREATE INDEX IF NOT EXISTS idx_eac_technology ON eac_results(technology_type);
CREATE INDEX IF NOT EXISTS idx_eac_tech_sd ON eac_results(technology_type, sd);
CREATE INDEX IF NOT EXISTS idx_eac_tech_participant ON eac_results(technology_type, participant);

-- Distinct (technology_type, auction_unit) -> participant, kept in sync by
-- upsert_eac_results(). At a few hundred rows this stays fast regardless of
-- how large eac_results grows -- see eac_metrics.search_participants() and
-- bm_metrics.battery_bm_units(), both of which used to run this lookup
-- against the full multi-million-row eac_results table on every request
-- (measured: ~500ms standalone, and inside bm_activity()'s join the query
-- planner abandoned its indexes entirely and fell back to a full table scan,
-- ~5s). Neither "which units are batteries" nor "known participant names"
-- needs per-settlement-period granularity or a date filter.
CREATE TABLE IF NOT EXISTS eac_known_units (
    technology_type  TEXT NOT NULL,
    auction_unit     TEXT NOT NULL,
    participant      TEXT NOT NULL,
    PRIMARY KEY (technology_type, auction_unit)
);
CREATE INDEX IF NOT EXISTS idx_eac_known_units_participant ON eac_known_units(technology_type, participant);

CREATE TABLE IF NOT EXISTS bm_unit_reference (
    national_grid_bm_unit  TEXT PRIMARY KEY,
    elexon_bm_unit          TEXT,
    lead_party_name          TEXT,
    bm_unit_type              TEXT,
    generation_capacity_mw    REAL,
    fetched_at                TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bm_cashflows (
    sd                      TEXT NOT NULL,
    sp                      INTEGER NOT NULL,
    national_grid_bm_unit   TEXT NOT NULL,
    bid_offer               TEXT NOT NULL,
    total_cashflow          REAL NOT NULL,
    fetched_at               TEXT NOT NULL,
    PRIMARY KEY (sd, sp, national_grid_bm_unit, bid_offer)
);
CREATE INDEX IF NOT EXISTS idx_bm_cashflows_bmu ON bm_cashflows(national_grid_bm_unit);
CREATE INDEX IF NOT EXISTS idx_bm_cashflows_bmu_sd ON bm_cashflows(national_grid_bm_unit, sd);
"""


@dataclass(frozen=True)
class PriceRow:
    series: str
    sd: date
    sp: int
    run: str
    value: float


@dataclass(frozen=True)
class EacRow:
    neso_id: int
    unit_result_id: str | None
    service_type: str
    auction_product: str
    technology_type: str | None
    auction_unit: str
    participant: str
    executed_quantity: float | None
    clearing_price: float | None
    delivery_start: str  # local (Europe/London) ISO datetime, naive
    delivery_end: str
    sd: date
    sp: int
    post_code: str | None


@dataclass(frozen=True)
class BmUnitReferenceRow:
    national_grid_bm_unit: str
    elexon_bm_unit: str | None
    lead_party_name: str | None
    bm_unit_type: str | None
    generation_capacity_mw: float | None


@dataclass(frozen=True)
class BmCashflowRow:
    sd: date
    sp: int
    national_grid_bm_unit: str
    bid_offer: str  # 'bid' | 'offer'
    total_cashflow: float


def connect(db_path: Path | str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # timeout=30 (vs sqlite3's 5s default) plus WAL mode: the web app now
    # has a long-running write-heavy request (/gbpw/build, hundreds of
    # commits over several minutes) that can genuinely overlap with normal
    # page loads opening their own connection -- WAL lets those reads
    # proceed without waiting on the writer, and the longer timeout covers
    # the brief windows where two writers really do collide, instead of
    # raising "database is locked".
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


def upsert_prices(conn: sqlite3.Connection, rows: Iterable[PriceRow], fetched_at: datetime | None = None) -> int:
    fetched_at = fetched_at or datetime.now(timezone.utc)
    ts = fetched_at.isoformat()
    data = [(r.series, r.sd.isoformat(), r.sp, r.run, r.value, ts) for r in rows]
    conn.executemany(
        """
        INSERT INTO prices (series, sd, sp, run, value, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT (series, sd, sp, run) DO UPDATE SET
            value = excluded.value,
            fetched_at = excluded.fetched_at
        """,
        data,
    )
    conn.commit()
    return len(data)


def upsert_eac_results(conn: sqlite3.Connection, rows: Iterable[EacRow], fetched_at: datetime | None = None) -> int:
    fetched_at = fetched_at or datetime.now(timezone.utc)
    ts = fetched_at.isoformat()
    rows = list(rows)  # consumed twice below (raw upsert + known-units derivation)
    data = [
        (
            r.neso_id, r.unit_result_id, r.service_type, r.auction_product, r.technology_type,
            r.auction_unit, r.participant, r.executed_quantity, r.clearing_price,
            r.delivery_start, r.delivery_end, r.sd.isoformat(), r.sp, r.post_code, ts,
        )
        for r in rows
    ]
    conn.executemany(
        """
        INSERT INTO eac_results (
            neso_id, unit_result_id, service_type, auction_product, technology_type,
            auction_unit, participant, executed_quantity, clearing_price,
            delivery_start, delivery_end, sd, sp, post_code, fetched_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (neso_id, sd, sp) DO UPDATE SET
            unit_result_id = excluded.unit_result_id,
            service_type = excluded.service_type,
            auction_product = excluded.auction_product,
            technology_type = excluded.technology_type,
            auction_unit = excluded.auction_unit,
            participant = excluded.participant,
            executed_quantity = excluded.executed_quantity,
            clearing_price = excluded.clearing_price,
            delivery_start = excluded.delivery_start,
            delivery_end = excluded.delivery_end,
            post_code = excluded.post_code,
            fetched_at = excluded.fetched_at
        """,
        data,
    )

    known_units = {(r.technology_type, r.auction_unit, r.participant) for r in rows if r.technology_type}
    conn.executemany(
        """
        INSERT INTO eac_known_units (technology_type, auction_unit, participant) VALUES (?, ?, ?)
        ON CONFLICT (technology_type, auction_unit) DO UPDATE SET participant = excluded.participant
        """,
        list(known_units),
    )

    conn.commit()
    return len(data)


def upsert_bm_unit_reference(
    conn: sqlite3.Connection, rows: Iterable[BmUnitReferenceRow], fetched_at: datetime | None = None
) -> int:
    fetched_at = fetched_at or datetime.now(timezone.utc)
    ts = fetched_at.isoformat()
    data = [
        (r.national_grid_bm_unit, r.elexon_bm_unit, r.lead_party_name, r.bm_unit_type, r.generation_capacity_mw, ts)
        for r in rows
    ]
    conn.executemany(
        """
        INSERT INTO bm_unit_reference (
            national_grid_bm_unit, elexon_bm_unit, lead_party_name, bm_unit_type,
            generation_capacity_mw, fetched_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT (national_grid_bm_unit) DO UPDATE SET
            elexon_bm_unit = excluded.elexon_bm_unit,
            lead_party_name = excluded.lead_party_name,
            bm_unit_type = excluded.bm_unit_type,
            generation_capacity_mw = excluded.generation_capacity_mw,
            fetched_at = excluded.fetched_at
        """,
        data,
    )
    conn.commit()
    return len(data)


def upsert_bm_cashflows(
    conn: sqlite3.Connection, rows: Iterable[BmCashflowRow], fetched_at: datetime | None = None
) -> int:
    fetched_at = fetched_at or datetime.now(timezone.utc)
    ts = fetched_at.isoformat()
    data = [
        (r.sd.isoformat(), r.sp, r.national_grid_bm_unit, r.bid_offer, r.total_cashflow, ts)
        for r in rows
    ]
    conn.executemany(
        """
        INSERT INTO bm_cashflows (sd, sp, national_grid_bm_unit, bid_offer, total_cashflow, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT (sd, sp, national_grid_bm_unit, bid_offer) DO UPDATE SET
            total_cashflow = excluded.total_cashflow,
            fetched_at = excluded.fetched_at
        """,
        data,
    )
    conn.commit()
    return len(data)


def log_fetch(conn: sqlite3.Connection, series: str, sd: date, ok: bool, note: str = "") -> None:
    conn.execute(
        "INSERT INTO fetch_log (series, sd, ok, note, ts) VALUES (?, ?, ?, ?, ?)",
        (series, sd.isoformat(), int(ok), note, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


def series_for_week(
    conn: sqlite3.Connection, series: str, week_dates: list[date], run: str | None = None
) -> dict[tuple[str, int], float]:
    """(sd_iso, sp) -> value for a series across a set of dates.

    If `run` is None, returns the max-run row per (sd, sp) -- for series with
    only NA_RUN that's a no-op; for `imbalance` there is currently only ever
    one row per period (see module docstring) so this is also a no-op, but
    the query is written to do the right thing if that ever changes.
    """
    sd_list = [d.isoformat() for d in week_dates]
    placeholders = ",".join("?" for _ in sd_list)
    query = f"""
        SELECT sd, sp, value FROM prices
        WHERE series = ? AND sd IN ({placeholders})
        AND run = (
            SELECT p2.run FROM prices p2
            WHERE p2.series = prices.series AND p2.sd = prices.sd AND p2.sp = prices.sp
            ORDER BY p2.run DESC LIMIT 1
        )
    """
    rows = conn.execute(query, [series, *sd_list]).fetchall()
    return {(sd, sp): value for sd, sp, value in rows}


def upsert_report(
    conn: sqlite3.Connection,
    week_ending: date,
    facts_json: str,
    narrative: str,
    run_basis: str,
    built_at: datetime | None = None,
    published: bool = False,
) -> None:
    built_at = built_at or datetime.now(timezone.utc)
    conn.execute(
        """
        INSERT INTO reports (week_ending, facts_json, narrative, run_basis, built_at, published)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT (week_ending) DO UPDATE SET
            facts_json = excluded.facts_json,
            narrative = excluded.narrative,
            run_basis = excluded.run_basis,
            built_at = excluded.built_at,
            published = excluded.published
        """,
        (week_ending.isoformat(), facts_json, narrative, run_basis, built_at.isoformat(), int(published)),
    )
    conn.commit()


def get_report(conn: sqlite3.Connection, week_ending: date) -> dict | None:
    row = conn.execute(
        "SELECT week_ending, facts_json, narrative, run_basis, built_at, published FROM reports WHERE week_ending = ?",
        (week_ending.isoformat(),),
    ).fetchone()
    if row is None:
        return None
    keys = ("week_ending", "facts_json", "narrative", "run_basis", "built_at", "published")
    report = dict(zip(keys, row))
    report["published"] = bool(report["published"])
    return report


def latest_report_week(conn: sqlite3.Connection) -> date | None:
    row = conn.execute("SELECT week_ending FROM reports ORDER BY week_ending DESC LIMIT 1").fetchone()
    return date.fromisoformat(row[0]) if row else None


def list_report_weeks(conn: sqlite3.Connection) -> list[dict]:
    """Every week a report has been built for, most recent first -- backs
    the GB Power Weekly week picker so past weeks are reachable, not just
    whatever latest_report_week() resolves to.
    """
    rows = conn.execute(
        "SELECT week_ending, published FROM reports ORDER BY week_ending DESC"
    ).fetchall()
    return [{"week_ending": date.fromisoformat(w), "published": bool(p)} for w, p in rows]


def mark_published(conn: sqlite3.Connection, week_ending: date, published: bool = True) -> bool:
    """Returns False if no report row exists yet for that week (build it first)."""
    cur = conn.execute(
        "UPDATE reports SET published = ? WHERE week_ending = ?",
        (int(published), week_ending.isoformat()),
    )
    conn.commit()
    return cur.rowcount > 0
