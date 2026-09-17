"""
PPA Pricing Engine -- solves backward from a target project/equity IRR to
the required fixed PPA price (£/MWh) for a hypothetical Solar project.

Own subpackage, not a single flat module like ppa_metrics.py: this is a
genuinely separate concern from ppa_metrics.py's job (Sigma(generation x
price)/Sigma(generation) capture-price analysis of an asset that already
has real historical dispatch data). This engine prices a project that
doesn't exist yet, so it can only use assumed capacity-factor generation
and an assumed capture rate -- never a real half-hourly join -- see
solver.py's own docstring for how that assumed capture rate is sensibly
defaulted from ppa_metrics.py's real historical figure rather than
invented.

Pure calculation layer: no database, no HTTP, no FastAPI imports anywhere
in this subpackage -- see web/routes_ppa_pricing.py for the one place
that wires it to a request.

    finance.py   -- npv()/irr(), pure time-value-of-money math
    cashflow.py  -- generation, revenue, costs, debt, annual cashflow
    solver.py    -- bisection over cashflow.py+finance.py to find the
                    PPA price that hits a target IRR
"""
