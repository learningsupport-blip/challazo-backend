"""Superadmin video-worker shares. GitHub AI and single-worker deploys stay put."""

from unittest.mock import MagicMock, patch

from app.services.app_settings_service import SETTINGS_COLLECTION, AppSettingsService
from app.services.evaluation_job_service import EvaluationJobService
from app.services.evaluation_routing import (
    COMPANY_KEY,
    EVALUATION_ROUTING_DOC_ID,
    PERSONAL_KEY,
    build_sequence,
    default_shares,
    routing_summary,
)


class _RoutingFirebase:
    def __init__(self, doc=None):
        self.doc = doc

    def get_document(self, collection, document_id):
        assert collection == SETTINGS_COLLECTION
        assert document_id == EVALUATION_ROUTING_DOC_ID
        return self.doc

    def set_document(self, collection, document_id, data):
        assert collection == SETTINGS_COLLECTION
        assert document_id == EVALUATION_ROUTING_DOC_ID
        self.doc = data
        return True

    def run_transaction(self, callback):
        return callback(object())

    def txn_get(self, transaction, collection, document_id):
        _ = transaction
        return self.get_document(collection, document_id)

    def txn_set(self, transaction, collection, document_id, data):
        _ = transaction
        self.set_document(collection, document_id, data)


def test_default_pattern_is_four_personal_then_one_company():
    assert default_shares(company_configured=True) == (4, 1)
    sequence = build_sequence(4, 1, personal_configured=True, company_configured=True)
    assert sequence == [PERSONAL_KEY, PERSONAL_KEY, PERSONAL_KEY, PERSONAL_KEY, COMPANY_KEY]
    assert "4 videos use the personal project" in routing_summary(sequence)
    assert "1 video uses the company project" in routing_summary(sequence)


def test_missing_company_worker_keeps_every_video_on_the_personal_project():
    assert default_shares(company_configured=False) == (1, 0)
    sequence = build_sequence(4, 1, personal_configured=True, company_configured=False)
    assert sequence == [PERSONAL_KEY, PERSONAL_KEY, PERSONAL_KEY, PERSONAL_KEY]
    assert routing_summary(sequence) == "Every video uses the personal project."


def test_reserve_hands_out_four_personal_then_company(monkeypatch):
    monkeypatch.setenv(
        "CLOUD_TASKS_TARGET_URL",
        "https://personal.example/internal/jobs/evaluate-submission",
    )
    monkeypatch.setenv(
        "CLOUD_TASKS_COMPANY_TARGET_URL",
        "https://company.example/internal/jobs/evaluate-submission",
    )
    service = AppSettingsService(firebase=_RoutingFirebase())
    choices = [service.reserve_evaluation_worker() for _ in range(6)]
    assert choices == [
        PERSONAL_KEY,
        PERSONAL_KEY,
        PERSONAL_KEY,
        PERSONAL_KEY,
        COMPANY_KEY,
        PERSONAL_KEY,
    ]


def test_save_rejects_a_share_for_a_missing_company_worker(monkeypatch):
    monkeypatch.delenv("CLOUD_TASKS_COMPANY_TARGET_URL", raising=False)
    monkeypatch.setenv("CLOUD_TASKS_TARGET_URL", "https://personal.example/job")
    service = AppSettingsService(firebase=_RoutingFirebase())
    try:
        service.set_evaluation_routing(personal_share=4, company_share=1, updated_by="admin")
    except ValueError as exc:
        assert "company evaluation service" in str(exc)
    else:
        raise AssertionError("company share should be rejected when that worker is unset")


def test_save_starts_the_pattern_over(monkeypatch):
    monkeypatch.setenv("CLOUD_TASKS_TARGET_URL", "https://personal.example/job")
    monkeypatch.setenv("CLOUD_TASKS_COMPANY_TARGET_URL", "https://company.example/job")
    firebase = _RoutingFirebase({"personal_share": 4, "company_share": 1, "next_index": 3})
    service = AppSettingsService(firebase=firebase)
    saved = service.set_evaluation_routing(personal_share=2, company_share=2, updated_by="admin")
    assert saved["sequence"] == [PERSONAL_KEY, PERSONAL_KEY, COMPANY_KEY, COMPANY_KEY]
    assert firebase.doc["next_index"] == 0
    assert service.reserve_evaluation_worker() == PERSONAL_KEY


def test_single_worker_enqueue_does_not_read_settings(monkeypatch):
    monkeypatch.delenv("CLOUD_TASKS_COMPANY_TARGET_URL", raising=False)
    monkeypatch.delenv("CLOUD_TASKS_PERSONAL_TARGET_URL", raising=False)
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "demo-proj")
    monkeypatch.setenv("CLOUD_TASKS_QUEUE", "evaluation-jobs")
    monkeypatch.setenv("CLOUD_TASKS_TARGET_URL", "https://personal.example/job")
    monkeypatch.setenv("INTERNAL_JOB_SECRET", "secret-value")
    jobs = EvaluationJobService()
    with (
        patch("google.cloud.tasks_v2.CloudTasksClient") as client_cls,
        patch("app.services.app_settings_service.AppSettingsService") as settings_cls,
    ):
        client = client_cls.return_value
        client.queue_path.return_value = "projects/p/locations/asia-south1/queues/evaluation-jobs"
        created = MagicMock()
        created.name = "task-1"
        client.create_task.return_value = created
        jobs.enqueue_cloud_task("sub-1", None)
    settings_cls.assert_not_called()
    task = client.create_task.call_args.kwargs["request"]["task"]
    assert task["http_request"]["url"] == "https://personal.example/job"


def test_two_workers_enqueue_uses_the_reserved_worker(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "demo-proj")
    monkeypatch.setenv("CLOUD_TASKS_QUEUE", "evaluation-jobs")
    monkeypatch.setenv("CLOUD_TASKS_TARGET_URL", "https://personal.example/job")
    monkeypatch.setenv("CLOUD_TASKS_COMPANY_TARGET_URL", "https://company.example/job")
    monkeypatch.setenv("INTERNAL_JOB_SECRET", "secret-value")
    jobs = EvaluationJobService()
    with (
        patch("google.cloud.tasks_v2.CloudTasksClient") as client_cls,
        patch("app.services.app_settings_service.AppSettingsService") as settings_cls,
    ):
        settings_cls.return_value.reserve_evaluation_worker.return_value = COMPANY_KEY
        client = client_cls.return_value
        client.queue_path.return_value = "projects/p/locations/asia-south1/queues/evaluation-jobs"
        created = MagicMock()
        created.name = "task-2"
        client.create_task.return_value = created
        jobs.enqueue_cloud_task("sub-2", None)
    task = client.create_task.call_args.kwargs["request"]["task"]
    assert task["http_request"]["url"] == "https://company.example/job"
