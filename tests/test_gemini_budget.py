"""Gemini switch, budgets, and proof that health and startup do not call the model."""

from __future__ import annotations

import asyncio
import inspect
import threading
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.main import health_check
from app.services import gemini_budget
from app.services.gemini_budget import (
    GeminiBudgetExceeded,
    clear_usage,
    note_submission_evaluation,
    reserve_call,
    usage_snapshot,
)
from app.services.gemini_output import generate_content
from app.utils import seeder


@pytest.fixture(autouse=True)
def _reset_budget():
    clear_usage()
    yield
    clear_usage()


def test_health_and_startup_do_not_call_gemini():
    assert "generate_content" not in inspect.getsource(health_check)
    assert "genai" not in inspect.getsource(health_check)
    assert "generate_content" not in inspect.getsource(seeder)
    assert "genai" not in inspect.getsource(seeder)
    body = asyncio.run(health_check())
    assert body["status"] == "healthy"


def test_disabled_switch_does_not_call_the_sdk(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GEMINI_ENABLED", "false")
    calls = {"n": 0}

    class Models:
        def generate_content(self, **_kwargs):
            calls["n"] += 1
            raise AssertionError("SDK was called")

    with pytest.raises(GeminiBudgetExceeded, match="disabled"):
        generate_content(
            SimpleNamespace(models=Models()),
            model="gemini-3.5-flash",
            contents=["prompt"],
            task="checklist",
        )
    assert calls["n"] == 0


def test_request_budget_stops_the_second_call(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GEMINI_DAILY_REQUEST_BUDGET", "1")
    monkeypatch.setenv("ENVIRONMENT", "staging")
    first = reserve_call("checklist", 10)
    gemini_budget.commit_call(first, 4)
    with pytest.raises(GeminiBudgetExceeded, match="request budget"):
        reserve_call("checklist", 10)
    assert usage_snapshot()["requests"] == 1
    assert usage_snapshot()["environment"] == "staging"


def test_token_reservation_blocks_a_call_that_would_exceed_the_budget(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("GEMINI_DAILY_TOKEN_BUDGET", "100")
    monkeypatch.setenv("GEMINI_BUDGET_ALERT_RATIO", "0.8")
    with pytest.raises(GeminiBudgetExceeded, match="token budget"):
        reserve_call("video", 1024)
    assert usage_snapshot()["circuit_open"] is True


def test_alert_logs_once_when_usage_crosses_the_threshold(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    monkeypatch.setenv("GEMINI_DAILY_TOKEN_BUDGET", "100")
    monkeypatch.setenv("GEMINI_BUDGET_ALERT_RATIO", "0.8")
    reservation = reserve_call("field_score", 10)
    with caplog.at_level("WARNING"):
        gemini_budget.commit_call(reservation, 80)
        gemini_budget.commit_call(reserve_call("field_score", 10), 5)
    alerts = [line for line in caplog.messages if "Gemini budget alert" in line]
    assert len(alerts) == 1
    assert "student" not in alerts[0].lower()


def test_submission_limit_allows_one_run(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GEMINI_MAX_EVALUATIONS_PER_SUBMISSION", "1")
    note_submission_evaluation("sub-1")
    with pytest.raises(GeminiBudgetExceeded, match="Submission evaluation limit"):
        note_submission_evaluation("sub-1")
    note_submission_evaluation("sub-2")


def test_concurrency_limit_holds_the_second_call(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GEMINI_MAX_CONCURRENT_CHECKLIST", "1")
    overlap = {"now": 0, "max": 0}
    lock = threading.Lock()

    def run() -> None:
        reservation = reserve_call("checklist", 1)
        with lock:
            overlap["now"] += 1
            overlap["max"] = max(overlap["max"], overlap["now"])
        time.sleep(0.05)
        with lock:
            overlap["now"] -= 1
        gemini_budget.commit_call(reservation, 1)

    threads = [threading.Thread(target=run) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert overlap["max"] == 1


def test_budget_resets_on_the_next_utc_day(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GEMINI_DAILY_REQUEST_BUDGET", "1")
    gemini_budget._now = lambda: datetime(2026, 10, 30, tzinfo=timezone.utc)
    reserve_call("checklist", 1)
    gemini_budget._now = lambda: datetime(2026, 10, 31, tzinfo=timezone.utc)
    reserve_call("checklist", 1)
    assert usage_snapshot()["requests"] == 1
