"""
PPA Pricing Engine: solves backward from a target IRR to the required
fixed PPA price for a hypothetical Solar project (see
ppa_pricing/solver.py for the calculation engine itself -- this route is
the only place that engine touches FastAPI/the database).

Deliberately has no "wholesale price"/"capture rate" INPUT field: a
Fixed-price PPA's revenue is generation x a flat £/MWh and doesn't
depend on the market price at all, so a market-data input the solver
would silently ignore would be exactly the kind of hidden/misleading
assumption this project avoids elsewhere. Instead, the real historical
solar capture price (see ppa_metrics.capture_price(), the same function
`/ppa` already uses) is shown as read-only context next to the solved
price -- "how does this compare to what the market would actually pay" --
without pretending it fed into the calculation.
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from .. import ppa_metrics
from ..ppa_pricing.cashflow import ProjectAssumptions
from ..ppa_pricing.solver import InfeasiblePrice, solve_ppa_price
from .deps import get_db

router = APIRouter()

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.filters["commas"] = lambda v, decimals=0: f"{v:,.{decimals}f}" if v is not None else "—"

# Illustrative Solar defaults -- not sourced from live data (this project
# doesn't exist yet, see ppa_pricing/__init__.py's docstring), just
# reasonable GB utility-solar ballpark figures the user is expected to
# override. Every one of these is a plain, visible, editable form field,
# never a constant buried inside the calculation engine itself.
DEFAULTS = {
    "capacity_mw": 10.0, "capacity_factor_pct": 11.0, "availability_pct": 98.0,
    "curtailment_pct": 1.0, "degradation_pct": 0.4, "capex_per_mw": 650_000.0,
    "opex_per_mw_year": 8_000.0, "inflation_pct": 2.5, "ppa_term_years": 15,
    "gearing_pct": 70.0, "interest_rate_pct": 6.0, "debt_term_years": 15,
    "target_irr_pct": 8.0, "target": "project", "discount_rate_pct": 5.0,
}


@router.get("/ppa/pricing", response_class=HTMLResponse)
def ppa_pricing_page(
    request: Request,
    capacity_mw: float = DEFAULTS["capacity_mw"],
    capacity_factor_pct: float = DEFAULTS["capacity_factor_pct"],
    availability_pct: float = DEFAULTS["availability_pct"],
    curtailment_pct: float = DEFAULTS["curtailment_pct"],
    degradation_pct: float = DEFAULTS["degradation_pct"],
    capex_per_mw: float = DEFAULTS["capex_per_mw"],
    opex_per_mw_year: float = DEFAULTS["opex_per_mw_year"],
    inflation_pct: float = DEFAULTS["inflation_pct"],
    ppa_term_years: int = DEFAULTS["ppa_term_years"],
    gearing_pct: float = DEFAULTS["gearing_pct"],
    interest_rate_pct: float = DEFAULTS["interest_rate_pct"],
    debt_term_years: int = DEFAULTS["debt_term_years"],
    target_irr_pct: float = DEFAULTS["target_irr_pct"],
    target: str = DEFAULTS["target"],
    discount_rate_pct: float = DEFAULTS["discount_rate_pct"],
    db: sqlite3.Connection = Depends(get_db),
):
    target = target if target in ("project", "equity") else "project"
    inputs = {
        "capacity_mw": capacity_mw, "capacity_factor_pct": capacity_factor_pct,
        "availability_pct": availability_pct, "curtailment_pct": curtailment_pct,
        "degradation_pct": degradation_pct, "capex_per_mw": capex_per_mw,
        "opex_per_mw_year": opex_per_mw_year, "inflation_pct": inflation_pct,
        "ppa_term_years": ppa_term_years, "gearing_pct": gearing_pct,
        "interest_rate_pct": interest_rate_pct, "debt_term_years": debt_term_years,
        "target_irr_pct": target_irr_pct, "target": target, "discount_rate_pct": discount_rate_pct,
    }

    assumptions = ProjectAssumptions(
        capacity_mw=capacity_mw,
        capacity_factor=capacity_factor_pct / 100,
        availability=availability_pct / 100,
        curtailment_pct=curtailment_pct / 100,
        degradation_pct=degradation_pct / 100,
        capex_per_mw=capex_per_mw,
        opex_per_mw_year=opex_per_mw_year,
        inflation_pct=inflation_pct / 100,
        ppa_term_years=ppa_term_years,
        gearing_pct=gearing_pct / 100,
        interest_rate=interest_rate_pct / 100,
        debt_term_years=debt_term_years,
    )

    result = None
    error = None
    try:
        result = solve_ppa_price(
            assumptions, target_irr=target_irr_pct / 100, target=target,
            discount_rate=discount_rate_pct / 100,
        )
    except InfeasiblePrice as e:
        error = str(e)
    except (ValueError, ZeroDivisionError) as e:
        error = f"Couldn't solve with these assumptions: {e}"

    # Real historical solar capture price, last 12 months -- context only,
    # never fed into the Fixed-PPA solve above (see module docstring).
    today = date.today()
    market_context = ppa_metrics.capture_price(db, "solar", today - timedelta(days=365), today)

    context = {
        "request": request,
        "active_nav": "ppa",
        "defaults": DEFAULTS,
        "inputs": inputs,
        "result": result,
        "error": error,
        "market_context": market_context,
    }
    return templates.TemplateResponse(request, "ppa_pricing.html", context)
