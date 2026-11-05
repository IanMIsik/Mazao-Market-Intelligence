import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest  # noqa: E402

from gbpw.storage import FuelInstRow, connect, upsert_fuelinst, upsert_report  # noqa: E402
from gbpw.web import alerts  # noqa: E402

NOW = datetime(2026, 11, 5, 14, 0, tzinfo=timezone.utc)  # a Thursday
FRESH = {"last_ok": NOW - timedelta(minutes=3), "last_error": None, "cycle_started": None}


def _db_with_fresh_fuelinst(tmp_path):
    db = tmp_path / "gbpw.db"
    conn = connect(db)
    upsert_fuelinst(conn, [FuelInstRow(NOW - timedelta(minutes=10), "WIND", 5000.0)])
    upsert_report(conn, datetime(2026, 11, 1).date(), "{}", "", "", built_at=NOW, published=True)
    conn.close()
    return db


@pytest.fixture(autouse=True)
def _no_disk_alarm(monkeypatch):
    monkeypatch.setattr(alerts.shutil, "disk_usage", lambda p: type("U", (), {"free": 50 * 1024**3})())


def test_healthy_system_has_no_issues(tmp_path):
    assert alerts.check_all(_db_with_fresh_fuelinst(tmp_path), FRESH, NOW) == {}


def test_a_refresh_that_stopped_completing_is_reported_with_the_stuck_cycle_and_last_error(tmp_path):
    health = {"last_ok": NOW - timedelta(hours=31), "last_error": "ReadTimeout: boom", "cycle_started": NOW - timedelta(hours=31)}
    issues = alerts.check_all(_db_with_fresh_fuelinst(tmp_path), health, NOW)
    assert "31h" in issues["refresh_stale"] and "looks stuck" in issues["refresh_stale"] and "ReadTimeout" in issues["refresh_stale"]


def test_a_refresh_that_has_not_finished_a_cycle_since_startup_gets_a_grace_period(tmp_path, monkeypatch):
    monkeypatch.setattr(alerts, "_STARTED", NOW - timedelta(minutes=5))
    issues = alerts.check_all(_db_with_fresh_fuelinst(tmp_path), {"last_ok": None, "last_error": None, "cycle_started": NOW}, NOW)
    assert "refresh_stale" not in issues


def test_stale_generation_data_is_reported_even_if_the_refresh_claims_success(tmp_path):
    db = tmp_path / "gbpw.db"
    conn = connect(db)
    upsert_fuelinst(conn, [FuelInstRow(NOW - timedelta(hours=5), "WIND", 5000.0)])
    upsert_report(conn, datetime(2026, 11, 1).date(), "{}", "", "", built_at=NOW, published=True)
    conn.close()
    assert "data_stale" in alerts.check_all(db, FRESH, NOW)


def test_missing_weekly_report_is_reported_only_after_the_grace_period(tmp_path):
    db = tmp_path / "gbpw.db"
    conn = connect(db)
    upsert_fuelinst(conn, [FuelInstRow(NOW - timedelta(minutes=10), "WIND", 5000.0)])
    conn.close()
    # Week ending Sun 1 Nov was due Mon 2 Nov 06:00; Tue 3 Nov 05:00 is inside the 24h grace.
    inside = datetime(2026, 11, 3, 5, 0, tzinfo=timezone.utc)
    assert "weekly_missing" not in alerts.check_all(db, {"last_ok": inside, "last_error": None, "cycle_started": None}, inside)
    issues = alerts.check_all(db, FRESH, NOW)
    assert "not built" in issues["weekly_missing"] and "2026-11-01" in issues["weekly_missing"]


def test_low_disk_is_reported(tmp_path, monkeypatch):
    monkeypatch.setattr(alerts.shutil, "disk_usage", lambda p: type("U", (), {"free": 500 * 1024**2})())
    assert "disk_low" in alerts.check_all(_db_with_fresh_fuelinst(tmp_path), FRESH, NOW)


def test_alerter_emails_once_on_start_reminds_every_six_hours_and_says_when_resolved():
    sent = []
    a = alerts.Alerter(send=lambda subject, body: sent.append(subject))
    t = NOW

    a.process({"refresh_stale": "stuck"}, t)
    a.process({"refresh_stale": "stuck"}, t + timedelta(minutes=1))   # quiet: no repeat
    a.process({"refresh_stale": "stuck"}, t + timedelta(hours=5))     # still inside the reminder window
    assert sent == ["[Mazao] ISSUE: refresh_stale"]

    a.process({"refresh_stale": "stuck"}, t + timedelta(hours=6))
    assert sent[-1] == "[Mazao] STILL OPEN: refresh_stale"

    a.process({}, t + timedelta(hours=7))
    a.process({}, t + timedelta(hours=7, minutes=1))                  # resolved only announced once
    assert sent[-1] == "[Mazao] RESOLVED: refresh_stale" and len(sent) == 3


def test_send_email_uses_smtp_with_starttls_login_and_never_raises(monkeypatch):
    for k, v in {"ALERT_EMAIL_TO": "a@example.com, b@example.com", "SMTP_HOST": "smtp.test", "SMTP_PORT": "587",
                 "SMTP_USER": "me@example.com", "SMTP_PASSWORD": "pw"}.items():
        monkeypatch.setenv(k, v)
    log = []

    class FakeSMTP:
        def __init__(self, host, port, timeout=None): log.append(("connect", host, port))
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def starttls(self): log.append(("starttls",))
        def login(self, u, p): log.append(("login", u))
        def send_message(self, msg): log.append(("send", msg["To"], msg["From"], msg["Subject"]))

    monkeypatch.setattr(alerts.smtplib, "SMTP", FakeSMTP)
    assert alerts.send_email("subj", "body") is True
    assert log == [("connect", "smtp.test", 587), ("starttls",), ("login", "me@example.com"),
                   ("send", "a@example.com, b@example.com", "me@example.com", "subj")]

    class Broken(FakeSMTP):
        def send_message(self, msg): raise OSError("network down")

    monkeypatch.setattr(alerts.smtplib, "SMTP", Broken)
    assert alerts.send_email("subj", "body") is False  # logged, not raised


def test_without_configuration_nothing_is_sent(monkeypatch):
    for k in ("ALERT_EMAIL_TO", "SMTP_HOST"):
        monkeypatch.delenv(k, raising=False)
    assert alerts.send_email("s", "b") is False


# ---- Portfolio Risk KPI tool checks -------------------------------------------------

def _status(last_ok, enabled=True, result="ok: 0 new or changed"):
    return {"scheduler": {"enabled": enabled, "jobs": [{"job": "purely", "last_ok": last_ok, "last_result": result}]}}


@pytest.fixture
def kpi(monkeypatch):
    monkeypatch.setattr(alerts, "_kpi_first_failure", None)
    return alerts.check_kpi


def test_kpi_check_is_skipped_when_no_url_is_configured(kpi):
    assert kpi(None, NOW, fetch=lambda u: 1 / 0) == {}


def test_kpi_container_not_answering_alerts_only_after_five_minutes_and_clears_on_recovery(kpi):
    def down(url):
        raise OSError("connection refused")

    assert kpi("http://risk/", NOW, fetch=down) == {}                                  # a rebuild takes ~1 min: no alarm yet
    assert kpi("http://risk/", NOW + timedelta(minutes=4), fetch=down) == {}
    issues = kpi("http://risk/", NOW + timedelta(minutes=6), fetch=down)
    assert "kpi_down" in issues and "connection refused" in issues["kpi_down"]
    healthy = _status("2026-11-05T19:05:00Z")
    assert kpi("http://risk/", NOW + timedelta(minutes=8), fetch=lambda u: healthy) == {}
    assert alerts._kpi_first_failure is None                                           # recovery resets the clock


def test_a_missed_daily_price_collection_is_reported_with_the_last_result(kpi):
    # Thu 5 Nov 2026 23:00 UTC = 23:00 UK: the 19:00 run is over 3 hours old.
    now = datetime(2026, 11, 5, 23, 0, tzinfo=timezone.utc)
    stale = _status("2026-11-04T19:05:00Z", result="failed: HTTP 503")
    issues = kpi("http://risk/", now, fetch=lambda u: stale)
    assert "kpi_prices_missed" in issues and "HTTP 503" in issues["kpi_prices_missed"] and "Wed 04 Nov" in issues["kpi_prices_missed"]
    fresh = _status("2026-11-05T19:05:00Z")
    assert kpi("http://risk/", now, fetch=lambda u: fresh) == {}


def test_price_collection_gets_three_hours_grace_and_weekends_do_not_count(kpi):
    # Thu 20:00 UK: today's run is only an hour old, so yesterday's success is still the expectation.
    thu_evening = datetime(2026, 11, 5, 20, 0, tzinfo=timezone.utc)
    assert kpi("http://risk/", thu_evening, fetch=lambda u: _status("2026-11-04T19:05:00Z")) == {}
    # Sunday: nothing is scheduled at weekends; Friday's run is the one that counts.
    sunday = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)
    assert kpi("http://risk/", sunday, fetch=lambda u: _status("2026-11-06T19:05:00Z")) == {}
    assert "kpi_prices_missed" in kpi("http://risk/", sunday, fetch=lambda u: _status("2026-11-05T19:05:00Z"))


def test_a_disabled_scheduler_is_not_reported_as_missed_runs(kpi):
    assert kpi("http://risk/", NOW, fetch=lambda u: _status(None, enabled=False)) == {}


def test_check_all_includes_the_kpi_issues(tmp_path, kpi):
    def down(url):
        raise OSError("refused")

    db = _db_with_fresh_fuelinst(tmp_path)
    alerts.check_all(db, FRESH, NOW, kpi_url="http://risk/")  # starts the outage clock
    issues = alerts.check_all(db, FRESH, NOW + timedelta(minutes=10), kpi_url="http://nowhere.invalid:1/")
    assert "kpi_down" in issues


def test_the_test_alert_command_runs_and_reports_missing_settings_instead_of_crashing(monkeypatch, tmp_path, capsys):
    # Regression: `gbpw test-alert` died with AttributeError ('Namespace' has no attribute 'db').
    from gbpw import cli

    for k in ("ALERT_EMAIL_TO", "SMTP_HOST"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["test-alert", "--db", str(tmp_path / "t.db")])
    assert exit_info.value.code == 1
    assert "Not configured" in capsys.readouterr().out


def test_the_test_alert_command_sends_when_configured(monkeypatch, tmp_path, capsys):
    from gbpw import cli

    monkeypatch.setenv("ALERT_EMAIL_TO", "a@example.com")
    monkeypatch.setenv("SMTP_HOST", "smtp.test")
    sent = []
    monkeypatch.setattr(alerts, "send_email", lambda subject, body: sent.append(subject) or True)
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["test-alert", "--db", str(tmp_path / "t.db")])
    assert exit_info.value.code == 0 and sent == ["[Mazao] Test alert"]
    assert "Test email sent" in capsys.readouterr().out
