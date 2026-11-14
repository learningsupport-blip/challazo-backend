"""Decide whether an evaluation should call Gemini, and how a transient error retries.

The key covers the student, the submission text, the stored video object, the
rubric, and the model settings. The same key on a finished submission is not
run again. A different answer, video, rubric, or model is.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import time
from typing import Any


logger = logging.getLogger(__name__)

LEASE_SECONDS = 1800
_MAX_IN_PROCESS_ATTEMPTS = 5


class EvaluationBusy(Exception):
    """Another worker already holds this submission. The caller should back off."""


def retry_attempts() -> int:
    raw = os.getenv("GEMINI_RETRY_ATTEMPTS", "3").strip()
    try:
        value = int(raw)
    except ValueError:
        return 3
    return max(1, min(value, _MAX_IN_PROCESS_ATTEMPTS))


def is_retryable_model_error(exc: Exception) -> bool:
    """Quota and transient transport errors. Validation errors are not retried."""
    if isinstance(exc, (ValueError, EvaluationBusy)):
        return False
    code = getattr(exc, "code", None)
    if code in (429, 500, 502, 503, 504):
        return True
    text = str(exc)
    return "RESOURCE_EXHAUSTED" in text or "UNAVAILABLE" in text


def is_audio_quota_error(exc: Exception) -> bool:
    """Vertex rejected the video because the audio track hit a system limit."""
    return "audio/*" in str(exc)


def retry_after_seconds(exc: Exception) -> float | None:
    """Honor a server Retry-After when the SDK exposes one. Cap so a worker can finish."""
    for source in (
        getattr(exc, "retry_after", None),
        _header(exc, "retry-after"),
    ):
        if source is None:
            continue
        try:
            seconds = float(source)
        except (TypeError, ValueError):
            continue
        if seconds >= 0:
            return min(seconds, 30.0)
    return None


def retry_delay_seconds(attempt: int, exc: Exception) -> float:
    """Wait before another try. ``attempt`` is zero for the first failure.

    An audio system limit stays closed if the next video is sent immediately.
    Those waits are longer than the normal backoff.
    """
    if is_audio_quota_error(exc):
        return min(30.0 * (attempt + 1), 120.0)
    guided = retry_after_seconds(exc)
    if guided is not None:
        return guided
    base = min(0.5 * (2**attempt), 8.0)
    return base + random.uniform(0, base * 0.25)


def pause(seconds: float) -> None:
    time.sleep(seconds)


def call_with_backoff(operation: Any, *, task: str) -> Any:
    """Run ``operation`` up to the attempt cap. Permanent errors are not repeated."""
    attempts = retry_attempts()
    attempt = 0
    while True:
        try:
            logger.info("Gemini call task=%s attempt=%s", task, attempt + 1)
            return operation()
        except Exception as exc:
            limit = attempts
            if is_audio_quota_error(exc):
                limit = max(attempts, _MAX_IN_PROCESS_ATTEMPTS)
            if not is_retryable_model_error(exc) or attempt + 1 >= limit:
                raise
            delay = retry_delay_seconds(attempt, exc)
            logger.info(
                "Gemini retry task=%s attempt=%s sleep=%.2f",
                task,
                attempt + 1,
                delay,
            )
            pause(delay)
            attempt += 1


def build_evaluation_key(
    *,
    stage: str,
    student_id: str,
    submission_id: str,
    problem: str,
    solution: str,
    field_answers: dict[str, Any] | None,
    theme_id: str,
    video_path: str,
    content_type: str,
    video_generation: str,
    model: str,
    media_resolution: str,
    fps: float,
    criteria: str,
    requirement_id: str,
    metrics: list[dict[str, Any]],
    checklist_template: str,
    video_template: str,
) -> str:
    """Stable id for one evaluation. Another student or rubric does not collide."""
    answers = field_answers if isinstance(field_answers, dict) else {}
    payload = {
        "stage": stage,
        "student_id": student_id,
        "submission_id": submission_id,
        "problem": problem,
        "solution": solution,
        "field_answers": {str(key): answers[key] for key in sorted(answers)},
        "theme_id": theme_id,
        "video_path": video_path,
        "content_type": content_type,
        "video_generation": video_generation,
        "model": model,
        "media_resolution": media_resolution,
        "fps": fps,
        "criteria": criteria,
        "requirement_id": requirement_id,
        "metrics": metrics,
        "checklist_template": checklist_template,
        "video_template": video_template,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def decide(submission: dict[str, Any] | None, key: str, now: float) -> str:
    """``deduped`` keeps a finished match. ``busy`` means a run is already in progress."""
    if not submission or not key:
        return "start"
    status = submission.get("status")
    if status == "completed" and submission.get("evaluation_key") == key:
        return "deduped"
    if status == "processing":
        return "busy"
    return "start"


def apply_claim(
    submission: dict[str, Any] | None,
    key: str,
    now: float,
    lease_seconds: int = LEASE_SECONDS,
) -> tuple[str, dict[str, Any] | None]:
    """Claim a worker turn. A live lease for this key does not start a second run."""
    if not submission:
        raise ValueError("Submission not found")
    if submission.get("status") == "completed":
        return "deduped", None
    lease = submission.get("evaluation_lease") or {}
    expires = float(lease.get("expires_at") or 0)
    if lease.get("key") == key and expires > now:
        return "busy", None
    return "claimed", {
        "evaluation_lease": {"key": key, "expires_at": now + lease_seconds},
    }


def _header(exc: Exception, name: str) -> str | None:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    getter = getattr(headers, "get", None)
    if not callable(getter):
        return None
    value = getter(name)
    if value is None:
        return None
    return str(value)
