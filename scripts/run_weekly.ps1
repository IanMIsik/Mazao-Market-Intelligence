# Weekly entry point for a scheduler (Windows Task Scheduler, or run manually).
# Ingests the trailing window and builds the report for the most recently
# completed week, writing to <repo>\out\gbpw-<week-ending>.html.
#
# Set ANTHROPIC_API_KEY in the environment (or the scheduled task's action)
# for the LLM narrative stage; without it, the pipeline still runs and falls
# back to narrative_rules.py automatically.

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot\..
python -m gbpw.cli run --week-ending auto
