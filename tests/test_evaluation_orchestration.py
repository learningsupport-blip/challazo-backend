"""Duplicate evaluation requests, worker overlap, retries, and key invalidation."""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from app.services.evaluation_job_service import EvaluationJobService
from app.services.evaluation_orchestration import (
    EvaluationBusy,
    apply_claim,
    build_evaluation_key,
    call_with_backoff,
    decide,
    pause,
)
from app.services.gemini_output import generate_content


def _key(**overrides: object) -> str:
    payload = dict(
        stage="submission_analysis",
        student_id="student-a",
        submission_id="sub-1",
        problem="Clinics lose the queue.",
        solution="A shared board.",
        field_answers={"notes": "one"},
        theme_id="theme-1",
        video_path="gs://bucket/submissions/student-a/sub-1/video.mp4",
        content_type="video/mp4",
        video_generation="1",
        model="gemini-3.5-flash",
        media_resolution="low",
        fps=1.0,
        criteria="",
        requirement_id="req-1",
        metrics=[{"field_key": "problem_statement", "scoring_prompt": "Be clear."}],
        checklist_template="checklist",
        video_template="watch {context}",
    )
    payload.update(overrides)
    return build_evaluation_key(**payload)


class _Firebase:
    def __init__(self, submission: dict) -> None:
        self.submission = submission
        self.analysis = {"status": "completed"}
        self.lock = threading.Lock()

    def get_document(self, collection: str, _document_id: str):
        if collection == "analysis":
            return self.analysis
        return dict(self.submission)

    def run_transaction(self, callback):
        with self.lock:
            return callback(self)

    def txn_get(self, _transaction, collection: str, document_id: str):
        return self.get_document(collection, document_id)

    def txn_update(self, _transaction, _collection: str, _document_id: str, data: dict) -> None:
        self.submission.update(data)


class _Service:
    collection = "submissions"
    analysis_collection = "analysis"

    def __init__(self, submission: dict) -> None:
        self.firebase = _Firebase(submission)
        self.calls = 0

    def current_evaluation_key(self, _submission, evaluation_criteria=None, submission_id=None):
        _ = (evaluation_criteria, submission_id)
        return "k" * 64

    def evaluate_submission(self, _submission_id, evaluation_criteria=None) -> None:
        _ = evaluation_criteria
        self.calls += 1
        self.firebase.submission["status"] = "completed"
        self.firebase.submission["evaluation_key"] = "k" * 64
        self.firebase.submission["evaluation_lease"] = None


def test_same_inputs_are_deduped_and_a_change_is_not():
    key = _key()
    finished = {"status": "completed", "evaluation_key": key}
    assert decide(finished, key, 0) == "deduped"
    assert decide({"status": "failed", "evaluation_key": key}, key, 0) == "start"
    assert decide(finished, _key(problem="A different problem."), 0) == "start"
    assert decide(finished, _key(student_id="student-b"), 0) == "start"
    assert decide(finished, _key(model="gemini-2.5-flash"), 0) == "start"
    assert (
        decide(
            finished, _key(metrics=[{"field_key": "problem_statement", "scoring_prompt": "v2"}]), 0
        )
        == "start"
    )
    assert decide(finished, _key(video_generation="2"), 0) == "start"
    assert decide({"status": "processing"}, key, 0) == "busy"


def test_overlapping_workers_claim_once():
    document = {"status": "processing"}
    key = _key()
    outcomes: list[str] = []
    lock = threading.Lock()

    def claim() -> None:
        with lock:
            outcome, patch = apply_claim(document, key, 1_000.0)
            if patch:
                document.update(patch)
        outcomes.append(outcome)

    threads = [threading.Thread(target=claim) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert outcomes.count("claimed") == 1
    assert outcomes.count("busy") == 7


def test_worker_skips_a_live_lease_and_runs_a_failed_job():
    busy = _Service(
        {
            "status": "processing",
            "evaluation_lease": {"key": "k" * 64, "expires_at": 10**12},
        }
    )
    jobs = EvaluationJobService(submission_service=busy)
    with pytest.raises(EvaluationBusy):
        jobs.process_evaluation_job("sub-1")
    assert busy.calls == 0

    failed = _Service({"status": "failed"})
    EvaluationJobService(submission_service=failed).process_evaluation_job("sub-1")
    EvaluationJobService(submission_service=failed).process_evaluation_job("sub-1")
    assert failed.calls == 1


def test_quota_is_retried_and_validation_is_not(monkeypatch: pytest.MonkeyPatch):
    slept: list[float] = []
    monkeypatch.setattr(
        "app.services.evaluation_orchestration.pause",
        lambda seconds: slept.append(seconds),
    )

    class Quota(Exception):
        code = 429

        def __init__(self) -> None:
            super().__init__("RESOURCE_EXHAUSTED")
            self.response = SimpleNamespace(headers={"retry-after": "2"})

    attempts = {"n": 0}

    def operation():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise Quota()
        return "ok"

    assert call_with_backoff(operation, task="video") == "ok"
    assert attempts["n"] == 3
    assert slept == [2.0, 2.0]


def test_audio_quota_waits_instead_of_retrying_immediately(monkeypatch: pytest.MonkeyPatch):
    slept: list[float] = []
    monkeypatch.setattr(
        "app.services.evaluation_orchestration.pause",
        lambda seconds: slept.append(seconds),
    )

    class AudioQuota(Exception):
        code = 429

        def __init__(self) -> None:
            super().__init__(
                "RESOURCE_EXHAUSTED. Systemlimit has been reached for generic mime type: 'audio/*'"
            )

    attempts = {"n": 0}

    def operation():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise AudioQuota()
        return "ok"

    assert call_with_backoff(operation, task="video") == "ok"
    assert slept == [30.0, 60.0]

    single = {"n": 0}

    def invalid():
        single["n"] += 1
        raise ValueError("missing video")

    with pytest.raises(ValueError):
        call_with_backoff(invalid, task="video")
    assert single["n"] == 1
    assert pause is not None


def test_generate_content_stops_after_the_attempt_cap(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("app.services.evaluation_orchestration.pause", lambda _seconds: None)
    calls = {"n": 0}

    class Unavailable(Exception):
        code = 503

    class Models:
        def generate_content(self, **_kwargs):
            calls["n"] += 1
            raise Unavailable("UNAVAILABLE")

    with pytest.raises(Unavailable):
        generate_content(
            SimpleNamespace(models=Models()),
            model="gemini-3.5-flash",
            contents=["hello"],
            task="checklist",
        )
    assert calls["n"] == 3
