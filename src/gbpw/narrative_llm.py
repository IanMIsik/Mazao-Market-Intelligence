"""
LLM-authored narrative stage: headline, byline, and 3 driver sentences,
generated strictly from metrics.build_week()'s facts dict.

Two things keep this honest:

1. The model is never sent facts["half_hourly"] or facts["heatmap"] -- those
   are the settlement-period-level price series (hundreds of readings each).
   Only the aggregate view built by _narrative_payload() goes in the request:
   KPIs, daily rows, driver extremes. That's the literal "no price series or
   raw data" requirement, not just a word we take the model's on.

2. Every number in the model's response is checked against that *same*
   reduced payload before we trust it -- not against the full facts dict.
   Checking against the full dict (which includes the withheld heatmap, ~350
   individual delta values) would make the check nearly meaningless: a
   hallucinated two-digit number has a good chance of coincidentally
   matching *something* in a pool that size. Checking against only what the
   model actually saw is the check that can actually catch a fabrication.

   The allowed-numbers set is built by running the same number extractor
   used on the model's prose over the payload's own JSON text, rather than
   walking only its typed int/float leaves. That's deliberate: day labels
   like "Wed 26 Aug" are legitimate strings in the payload, and the model is
   expected to reproduce the "26" in them -- a typed-leaf-only walk would
   never have allowed that and would reject correct output.

No conversation history: each call is a single stateless request.
"""

from __future__ import annotations

import json
import os
import re

import anthropic

MODEL = "claude-sonnet-5"
TEMPERATURE = 0.3
MAX_TOKENS = 600
ATTEMPTS = 2  # one original call + one retry, per spec ("retry once, then None")

# Guardrails against runaway or degenerate output. Not specified numerically
# in the brief -- chosen to keep copy roughly sketch-length; tune freely.
MAX_WORDS = {"headline": 30, "byline": 45, "driver": 30}
MIN_WORDS = {"headline": 4, "byline": 4, "driver": 4}

SYSTEM_PROMPT = """You write three short pieces of copy for a weekly GB power market report, from a single JSON object of already-computed facts about one week.

Rules, all mandatory:
- Use only the figures present in the JSON you are given. Never calculate, estimate, round differently, or introduce any number -- price, percentage, count, or otherwise -- that is not already a value in that JSON.
- For "drivers": state each one as our conclusion about what happened that week, not as a proposed cause. Do not use causal language such as "because", "due to", "as a result of", "which explains", "likely caused by", "driving", "meant that".
- Do not reference anything outside the JSON: no settlement runs, methodology, data sources, or commentary about the report itself.
- Reply with a single JSON object only -- no prose, no markdown code fences, nothing before or after it. Exactly these keys:
  "headline": one sentence, at most 30 words.
  "byline": one or two sentences, at most 45 words.
  "drivers": a list of exactly 3 strings, in this order -- wind output, peak demand, periods above £100 -- each one sentence, at most 30 words.
"""

# Negative lookahead excludes digits that are part of a hyphenated compound
# like "1-hour" or "30-day" -- those are units of phrasing, not figures.
_NUMBER_RE = re.compile(r"[-+]?£?\$?\d[\d,]*\.?\d*%?(?!-[A-Za-z])")


class ValidationError(Exception):
    """Raised internally when a model response fails validation. Never escapes generate()."""


def _narrative_payload(facts: dict) -> dict:
    """The reduced, aggregate-only view of facts the model is allowed to see."""
    return {
        "week_ending": facts["week_ending"],
        "week_start": facts["week_start"],
        "kpi": facts["kpi"],
        "days": [{k: v for k, v in day.items() if k != "date"} for day in facts["days"]],
        "totals": facts["totals"],
        "drivers": facts["drivers"],
    }


def _extract_numbers(text: str) -> list[float]:
    """Pull every numeric token out of prose, normalising £/$, commas and %."""
    numbers = []
    for raw in _NUMBER_RE.findall(text):
        cleaned = raw.replace("£", "").replace("$", "").replace(",", "").replace("%", "").strip()
        if cleaned in ("", "-", "+", "."):
            continue
        try:
            numbers.append(round(float(cleaned), 2))
        except ValueError:
            continue
    return numbers


def _word_count(s: str) -> int:
    return len(s.split())


def _validate(parsed: object, allowed_numbers: set[float]) -> dict:
    """Raises ValidationError on any problem; otherwise returns the checked dict."""
    if not isinstance(parsed, dict) or set(parsed.keys()) != {"headline", "byline", "drivers"}:
        raise ValidationError(f"unexpected shape: {parsed!r}")

    drivers = parsed["drivers"]
    if not isinstance(drivers, list) or len(drivers) != 3 or not all(isinstance(d, str) for d in drivers):
        raise ValidationError("drivers must be a list of exactly 3 strings")

    fields = {"headline": parsed["headline"], "byline": parsed["byline"]}
    fields.update({f"driver[{i}]": d for i, d in enumerate(drivers)})

    for key, text in fields.items():
        if not isinstance(text, str) or not text.strip():
            raise ValidationError(f"{key} is empty or not a string")

        bucket = "driver" if key.startswith("driver") else key
        wc = _word_count(text)
        if not (MIN_WORDS[bucket] <= wc <= MAX_WORDS[bucket]):
            raise ValidationError(f"{key} word count {wc} outside [{MIN_WORDS[bucket]}, {MAX_WORDS[bucket]}]")

        for n in _extract_numbers(text):
            if n not in allowed_numbers:
                raise ValidationError(f"{key} contains number {n} not present in the facts payload")

    return parsed


def _call_model(client: "anthropic.Anthropic", payload_json: str) -> str:
    resp = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        temperature=TEMPERATURE,
        system=SYSTEM_PROMPT,
        messages=[
            {"role": "user", "content": payload_json},
            {"role": "assistant", "content": "{"},  # prefill biases the reply toward JSON-only
        ],
    )
    body = resp.content[0].text
    return "{" + body  # the prefilled "{" is not echoed back by the API


def generate(facts: dict) -> dict | None:
    """Try to produce {"headline", "byline", "drivers": [3 strings]} via the
    Anthropic API. Returns None if the API key is missing, both attempts
    fail to validate, or both attempts error out. Never raises.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None

    payload = _narrative_payload(facts)
    payload_json = json.dumps(payload, sort_keys=True)
    allowed_numbers = set(_extract_numbers(payload_json))

    client = anthropic.Anthropic(api_key=api_key)

    for _attempt in range(ATTEMPTS):
        try:
            raw = _call_model(client, payload_json)
        except Exception:
            continue  # API/network error on this attempt -- try again if attempts remain

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            continue  # did not parse on this attempt -- no repair, just retry

        try:
            validated = _validate(parsed, allowed_numbers)
        except ValidationError:
            continue

        return {"headline": validated["headline"], "byline": validated["byline"], "drivers": list(validated["drivers"])}

    return None
