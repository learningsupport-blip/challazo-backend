"""In-process Gemini switch, concurrency cap, and daily budget.

Counters live in this process. They reset at the next UTC day. They are not a
Cloud Billing cap, and they are not shared across Cloud Run instances. A
budget of 0 means unlimited. Tokens are reserved before the SDK call so
concurrent requests cannot all pass a nearly empty budget.
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable


logger = logging.getLogger(__name__)

_lock = threading.RLock()
_semaphores: dict[str, threading.BoundedSemaphore] = {}
_state: dict[str, int | str | bool] = {}
_submission_runs: dict[str, int] = {}
_alerted = False
_opened = False


class GeminiBudgetExceeded(RuntimeError):
    """The process must not call Gemini. The message has no student content."""


@dataclass
class Reservation:
    task: str
    tokens: int
    day: str


_now: Callable[[], datetime] | None = None


def clear_usage() -> None:
    """Reset counters and semaphores. Tests use this between cases."""
    global _alerted, _opened, _now
    with _lock:
        _state.clear()
        _submission_runs.clear()
        _semaphores.clear()
        _alerted = False
        _opened = False
        _now = None


def gemini_enabled() -> bool:
    flag = os.getenv("GEMINI_ENABLED", "true").strip().lower()
    return flag not in ("0", "false", "off", "no")


def environment_name() -> str:
    return (os.getenv("ENVIRONMENT") or "development").strip() or "development"


def assert_gemini_enabled() -> None:
    if gemini_enabled():
        return
    logger.warning("Gemini circuit open reason=disabled environment=%s", environment_name())
    raise GeminiBudgetExceeded("Gemini is disabled for this environment")


def note_submission_evaluation(submission_id: str) -> None:
    """Count one full evaluation for this submission. A limit of 0 is unlimited."""
    limit = _positive_int("GEMINI_MAX_EVALUATIONS_PER_SUBMISSION")
    if limit <= 0:
        return
    key = (submission_id or "").strip()
    if not key:
        return
    day = _day()
    with _lock:
        _roll_day(day)
        runs = int(_submission_runs.get(key) or 0)
        if runs >= limit:
            _open("submission_limit")
            raise GeminiBudgetExceeded("Submission evaluation limit reached")
        _submission_runs[key] = runs + 1


def reserve_call(task: str, output_cap: int) -> Reservation:
    """Hold a request slot and the output cap before the SDK is called."""
    assert_gemini_enabled()
    day = _day()
    cap = max(0, int(output_cap))
    with _lock:
        _roll_day(day)
        _take_request(task)
        _take_tokens(cap)
    _semaphore(task).acquire()
    return Reservation(task=task, tokens=cap, day=day)


def commit_call(reservation: Reservation, actual_tokens: int) -> None:
    """Replace the reserved cap with the tokens the response reported."""
    actual = max(0, int(actual_tokens))
    with _lock:
        reserved = int(_state.get("tokens_reserved") or 0)
        _state["tokens_reserved"] = max(0, reserved - reservation.tokens)
        used = int(_state.get("tokens_used") or 0) + actual
        _state["tokens_used"] = used
        _maybe_alert(used)
    _semaphore(reservation.task).release()
    logger.info(
        "Gemini budget environment=%s task=%s requests=%s tokens=%s circuit=%s",
        environment_name(),
        reservation.task,
        _state.get("requests"),
        _state.get("tokens_used"),
        "open" if _opened else "closed",
    )


def release_call(reservation: Reservation) -> None:
    """Give back a reservation when the SDK call fails. The request still counts."""
    with _lock:
        reserved = int(_state.get("tokens_reserved") or 0)
        _state["tokens_reserved"] = max(0, reserved - reservation.tokens)
    _semaphore(reservation.task).release()


def usage_snapshot() -> dict[str, int | str | bool]:
    with _lock:
        return {
            "environment": environment_name(),
            "requests": int(_state.get("requests") or 0),
            "tokens_used": int(_state.get("tokens_used") or 0),
            "tokens_reserved": int(_state.get("tokens_reserved") or 0),
            "circuit_open": _opened,
        }


def _take_request(task: str) -> None:
    budget = _positive_int("GEMINI_DAILY_REQUEST_BUDGET")
    task_budget = _positive_int(f"GEMINI_DAILY_REQUESTS_{task.upper()}")
    requests = int(_state.get("requests") or 0)
    task_requests = int(_state.get(f"task:{task}") or 0)
    if (budget and requests >= budget) or (task_budget and task_requests >= task_budget):
        _open("request_budget")
        raise GeminiBudgetExceeded("Gemini daily request budget is exhausted")
    _state["requests"] = requests + 1
    _state[f"task:{task}"] = task_requests + 1


def _take_tokens(cap: int) -> None:
    budget = _positive_int("GEMINI_DAILY_TOKEN_BUDGET")
    if not budget:
        return
    used = int(_state.get("tokens_used") or 0)
    reserved = int(_state.get("tokens_reserved") or 0)
    if used + reserved + cap > budget:
        _open("token_budget")
        raise GeminiBudgetExceeded("Gemini daily token budget is exhausted")
    _state["tokens_reserved"] = reserved + cap


def _maybe_alert(used: int) -> None:
    global _alerted
    budget = _positive_int("GEMINI_DAILY_TOKEN_BUDGET")
    if not budget or _alerted:
        return
    ratio = _ratio()
    if used < budget * ratio:
        return
    _alerted = True
    logger.warning(
        "Gemini budget alert environment=%s tokens=%s budget=%s ratio=%.2f",
        environment_name(),
        used,
        budget,
        ratio,
    )


def _open(reason: str) -> None:
    global _opened
    if _opened:
        return
    _opened = True
    logger.error(
        "Gemini circuit open reason=%s environment=%s requests=%s tokens=%s",
        reason,
        environment_name(),
        _state.get("requests"),
        _state.get("tokens_used"),
    )


def _roll_day(day: str) -> None:
    global _alerted, _opened
    if _state.get("day") == day:
        return
    _state.clear()
    _submission_runs.clear()
    _state["day"] = day
    _alerted = False
    _opened = False


def _day() -> str:
    clock = _clock()
    return clock().astimezone(timezone.utc).date().isoformat()


def _clock() -> Callable[[], datetime]:
    if _now is not None:
        return _now
    return lambda: datetime.now(timezone.utc)


def _semaphore(task: str) -> threading.BoundedSemaphore:
    limit = _concurrency(task)
    with _lock:
        current = _semaphores.get(task)
        if current is None or getattr(current, "_initial_value", None) != limit:
            current = threading.BoundedSemaphore(limit)
            setattr(current, "_initial_value", limit)
            _semaphores[task] = current
        return current


def _concurrency(task: str) -> int:
    specific = _positive_int(f"GEMINI_MAX_CONCURRENT_{task.upper()}")
    if specific:
        return specific
    shared = _positive_int("GEMINI_MAX_CONCURRENT")
    return shared or 4


def _ratio() -> float:
    raw = os.getenv("GEMINI_BUDGET_ALERT_RATIO", "0.8").strip()
    try:
        value = float(raw)
    except ValueError:
        return 0.8
    return min(1.0, max(0.0, value))


def _positive_int(name: str) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return 0
    try:
        return max(0, int(raw))
    except ValueError:
        return 0
