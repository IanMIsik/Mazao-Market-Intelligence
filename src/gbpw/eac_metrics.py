"""
Data layer for the EAC half of BESS Analytics -- aggregates over
`eac_results`, parallel to metrics.build_week()'s role for GB Power Weekly.
Pure functions, no web-framework imports; the web layer only composes these.

Real service_type values are Response, Quick Reserve, Slow Reserve, Balancing
Reserve -- kept as 4 real categories rather than the earlier design sketch's
invented 3-bucket Low/High/Reserve framing, since Reserve products have no
Low/High split at all (only Response's auction_product does, via an L/H
suffix: DCL/DCH, DML/DMH, DRL/DRH).

Prices are volume-weighted (SUM(qty*price)/SUM(qty)), not a flat AVG,
consistent with ingest/elexon.py's MID-provider blending -- a single
huge-volume result shouldn't be diluted by many small ones.
"""

from __future__ import annotations

import sqlite3
from datetime import date

DEFAULT_TECHNOLOGY = "Batteries"


def _where_range(start: date, end: date, technology_type: str | None) -> tuple[str, list]:
    where = ["sd >= ?", "sd <= ?"]
    params: list = [start.isoformat(), end.isoformat()]
    if technology_type is not None:
        where.append("technology_type = ?")
        params.append(technology_type)
    return " AND ".join(where), params


def latest_available_date(conn: sqlite3.Connection, technology_type: str | None = DEFAULT_TECHNOLOGY) -> date | None:
    if technology_type is None:
        row = conn.execute("SELECT MAX(sd) FROM eac_results").fetchone()
    else:
        row = conn.execute("SELECT MAX(sd) FROM eac_results WHERE technology_type = ?", (technology_type,)).fetchone()
    return date.fromisoformat(row[0]) if row and row[0] else None


def market_summary(
    conn: sqlite3.Connection, start: date, end: date, technology_type: str | None = DEFAULT_TECHNOLOGY
) -> dict:
    where, params = _where_range(start, end, technology_type)

    by_service_type = [
        {"service_type": r[0], "cleared_mw": r[1] or 0.0, "participants": r[2]}
        for r in conn.execute(
            f"""
            SELECT service_type, SUM(executed_quantity), COUNT(DISTINCT participant)
            FROM eac_results WHERE {where}
            GROUP BY service_type ORDER BY 2 DESC
            """,
            params,
        ).fetchall()
    ]

    response_by_band = [
        {"band": r[0], "cleared_mw": r[1] or 0.0}
        for r in conn.execute(
            f"""
            SELECT
              CASE WHEN auction_product LIKE '%L' THEN 'Low'
                   WHEN auction_product LIKE '%H' THEN 'High'
                   ELSE 'Other' END AS band,
              SUM(executed_quantity)
            FROM eac_results WHERE service_type = 'Response' AND {where}
            GROUP BY band
            """,
            params,
        ).fetchall()
    ]

    participants_active = conn.execute(
        f"SELECT COUNT(DISTINCT participant) FROM eac_results WHERE {where}", params
    ).fetchone()[0]

    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "technology_type": technology_type,
        "by_service_type": by_service_type,
        "response_by_band": response_by_band,
        "participants_active": participants_active,
    }


def distribution(
    conn: sqlite3.Connection,
    start: date,
    end: date,
    technology_type: str | None = DEFAULT_TECHNOLOGY,
    n_buckets: int = 8,
) -> dict:
    """Histogram of total executed_quantity (MW) summed per participant over
    the window. n_buckets equal-width bins spanning [0, max].
    """
    where, params = _where_range(start, end, technology_type)
    totals = [
        r[0] or 0.0
        for r in conn.execute(
            f"SELECT SUM(executed_quantity) FROM eac_results WHERE {where} GROUP BY participant", params
        ).fetchall()
    ]

    if not totals:
        return {"buckets": [], "participant_count": 0}

    max_val = max(totals)
    if max_val <= 0:
        return {"buckets": [{"low_mw": 0.0, "high_mw": None, "count": len(totals)}], "participant_count": len(totals)}

    width = max_val / n_buckets
    counts = [0] * n_buckets
    for v in totals:
        idx = min(int(v / width), n_buckets - 1) if width > 0 else 0
        counts[idx] += 1

    buckets = [
        {"low_mw": round(i * width, 2), "high_mw": round((i + 1) * width, 2), "count": counts[i]}
        for i in range(n_buckets)
    ]
    return {"buckets": buckets, "participant_count": len(totals)}


def leaderboard(
    conn: sqlite3.Connection,
    start: date,
    end: date,
    technology_type: str | None = DEFAULT_TECHNOLOGY,
    top_n: int = 8,
) -> list[dict]:
    where, params = _where_range(start, end, technology_type)
    rows = conn.execute(
        f"""
        SELECT participant, SUM(executed_quantity) AS cleared_mw,
               SUM(executed_quantity * clearing_price) AS weighted_price_sum
        FROM eac_results WHERE {where}
        GROUP BY participant ORDER BY cleared_mw DESC LIMIT ?
        """,
        [*params, top_n],
    ).fetchall()
    return [
        {
            "participant": p,
            "cleared_mw": cleared_mw or 0.0,
            "avg_clearing_price": (weighted_sum / cleared_mw) if cleared_mw else None,
        }
        for p, cleared_mw, weighted_sum in rows
    ]


def search_participants(
    conn: sqlite3.Connection, q: str, technology_type: str | None = DEFAULT_TECHNOLOGY, limit: int = 20
) -> list[str]:
    """Distinct participant names matching a case-insensitive substring of q,
    scoped to technology_type. No date filtering -- searches the whole
    known participant list, not just the current window.

    Queries eac_known_units, not eac_results directly -- a few hundred rows
    versus a table that can run into the millions, kept in sync by
    upsert_eac_results(). Called on every debounced keystroke, so this needs
    to stay fast regardless of how much history gets backfilled.
    """
    where = ["participant LIKE ? ESCAPE '\\'"]
    like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    params: list = [like]
    if technology_type is not None:
        where.append("technology_type = ?")
        params.append(technology_type)
    rows = conn.execute(
        f"SELECT DISTINCT participant FROM eac_known_units WHERE {' AND '.join(where)} "
        f"COLLATE NOCASE ORDER BY participant LIMIT ?",
        [*params, limit],
    ).fetchall()
    return [r[0] for r in rows]


def participant_detail(
    conn: sqlite3.Connection,
    participants: list[str],
    start: date,
    end: date,
    technology_type: str | None = DEFAULT_TECHNOLOGY,
) -> dict:
    if not participants:
        return {}
    where, params = _where_range(start, end, technology_type)
    placeholders = ",".join("?" for _ in participants)
    rows = conn.execute(
        f"""
        SELECT participant, service_type, auction_product, auction_unit,
               SUM(executed_quantity) AS cleared_mw,
               SUM(executed_quantity * clearing_price) AS weighted_price_sum
        FROM eac_results
        WHERE participant IN ({placeholders}) AND {where}
        GROUP BY participant, service_type, auction_product, auction_unit
        ORDER BY participant, cleared_mw DESC
        """,
        [*participants, *params],
    ).fetchall()

    out: dict[str, dict] = {p: {"by_service_type": [], "total_cleared_mw": 0.0, "auction_units": set()} for p in participants}
    for participant, service_type, auction_product, auction_unit, cleared_mw, weighted_sum in rows:
        cleared_mw = cleared_mw or 0.0
        entry = out[participant]
        entry["by_service_type"].append(
            {
                "service_type": service_type,
                "auction_product": auction_product,
                "auction_unit": auction_unit,
                "cleared_mw": cleared_mw,
                "avg_clearing_price": (weighted_sum / cleared_mw) if cleared_mw else None,
            }
        )
        entry["total_cleared_mw"] += cleared_mw
        entry["auction_units"].add(auction_unit)

    for entry in out.values():
        entry["auction_units"] = sorted(entry["auction_units"])

    return out
