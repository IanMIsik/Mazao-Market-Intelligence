"""
Nord Pool N2EX day-ahead fetcher -- STUB.

Not implemented. Nord Pool's public Data Portal (data.nordpoolgroup.com) is
backed by an unauthenticated internal endpoint, but using it here would
breach Nord Pool's own Terms and Conditions for use of website in two
independent ways: it prohibits automated data extraction outright, and it
prohibits republishing/redistributing the website's contents to any other
party without express written consent -- which is what a client-facing
recap page would do regardless of how the data was fetched.

The report currently uses Elexon's Market Index Data (see ingest/elexon.py,
series 'day_ahead') as the day-ahead reference instead, which is openly
licensed for this use.

Plug in a real implementation here if/when either becomes true:
  - Mazao has a Nord Pool Market Data API subscription (paid, licensed,
    covers redistribution under its own terms), or
  - Nord Pool grants written permission to republish Data Portal content.

Keep the interface below so metrics/render don't need to change.
"""

from __future__ import annotations

from datetime import date

from ..storage import PriceRow


def fetch_day_ahead(d: date) -> tuple[list[PriceRow], str]:
    raise NotImplementedError(
        "Nord Pool N2EX fetch is not implemented -- no licensed public route found. "
        "See this module's docstring. Use elexon.fetch_day_ahead for now."
    )
