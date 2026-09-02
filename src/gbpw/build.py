"""
Top-level orchestration for one week's report:

    metrics.build_week()
        -> narrative (LLM, falling back to rules; or a cached prior narrative)
        -> render.render_week()
        -> write HTML + persist facts/narrative to the reports table

The page renders identically regardless of which narrative source was used
-- render_week() only ever sees the final {"headline","byline","drivers"}
dict, never which path produced it.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

from . import narrative_llm, narrative_rules
from .metrics import build_week
from .render.render import render_week
from .storage import get_report, mark_published, upsert_report

logger = logging.getLogger("gbpw.build")


def _select_narrative(facts: dict, existing: dict | None, regenerate: bool) -> tuple[dict, str]:
    """Returns (narrative, source) where source is 'cached', 'llm', or 'rules'."""
    if existing and existing.get("narrative") and not regenerate:
        return json.loads(existing["narrative"]), "cached"

    narrative = narrative_llm.generate(facts)
    if narrative is not None:
        return narrative, "llm"

    return narrative_rules.generate(facts), "rules"


def build_report(
    conn: sqlite3.Connection,
    week_ending: date,
    out_path: Path,
    regenerate: bool = False,
) -> Path:
    facts = build_week(conn, week_ending)
    existing = get_report(conn, week_ending)

    narrative, source = _select_narrative(facts, existing, regenerate)
    logger.info("week=%s narrative_source=%s", week_ending.isoformat(), source)

    built_at = datetime.now(timezone.utc)
    html = render_week(facts, narrative, built_at)

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")

    # A rebuild (e.g. after an ingest correction) must not silently
    # un-publish a report that was already marked published -- only the
    # explicit `publish` command changes that flag.
    published = existing["published"] if existing else False

    upsert_report(
        conn,
        week_ending=week_ending,
        facts_json=json.dumps(facts),
        narrative=json.dumps(narrative),
        run_basis=facts["run_basis"],
        built_at=built_at,
        published=published,
    )
    logger.info("week=%s wrote %s", week_ending.isoformat(), out_path)
    return out_path


def publish_report(conn: sqlite3.Connection, week_ending: date) -> bool:
    """Marks a built report as published. Returns False if it hasn't been built yet."""
    return mark_published(conn, week_ending, published=True)
