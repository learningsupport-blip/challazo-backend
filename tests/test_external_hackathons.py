"""External hackathons stay out of the student hackathon and submission collections."""

from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest

from app.exceptions import BadRequestError, ConflictError, ForbiddenError
from app.models.external_hackathon_model import (
    ExternalHackathonCreate,
    ExternalSubmissionCreate,
    ExternalThemeInput,
    ExternalUploadRequest,
)
from app.services.evaluation_job_service import EvaluationJobService
from app.services.external_evaluation_service import (
    EXTERNAL_JOB_PATH,
    ExternalEvaluationService,
    external_job_url,
)
from app.services.external_hackathon_service import (
    EXTERNAL_HACKATHONS,
    EXTERNAL_SUBMISSIONS,
    ExternalHackathonService,
    sign_team_link,
)
from app.utils.time import now_ist


class _MemoryFirebase:
    def __init__(self):
        self.docs: dict[tuple[str, str], dict] = {}
        self.writes: list[str] = []

    def set_document(self, collection, document_id, data):
        self.writes.append(collection)
        stored = dict(data)
        stored["id"] = document_id
        self.docs[(collection, document_id)] = stored
        return True

    def get_document(self, collection, document_id):
        stored = self.docs.get((collection, document_id))
        return dict(stored) if stored else None

    def get_collection(self, collection):
        return [dict(value) for (name, _doc_id), value in self.docs.items() if name == collection]

    def query_collection(self, collection, field, operator, value):
        assert operator == "=="
        return [row for row in self.get_collection(collection) if row.get(field) == value]

    def update_document(self, collection, document_id, data):
        current = self.get_document(collection, document_id) or {"id": document_id}
        current.update(data)
        return self.set_document(collection, document_id, current)


def _future_deadline() -> str:
    return (now_ist() + timedelta(days=2)).isoformat()


def _past_deadline() -> str:
    return (now_ist() - timedelta(days=1)).isoformat()


def _service(firebase=None, storage=None) -> tuple[ExternalHackathonService, _MemoryFirebase]:
    firebase = firebase or _MemoryFirebase()
    blob = MagicMock()
    blob.exists.return_value = True
    blob.size = 2048
    if storage is None:
        storage = MagicMock()
        storage.bucket.return_value.blob.return_value = blob
    service = ExternalHackathonService(firebase=firebase, storage_client=storage)
    service.bucket_name = "eval-bucket"
    return service, firebase


def _published(service: ExternalHackathonService, **overrides):
    payload = ExternalHackathonCreate(
        name=overrides.get("name", "Launchpad Cup"),
        description="External cup",
        themes=[ExternalThemeInput(id="theme-1", name="Health", description="Care")],
        submission_deadline=overrides.get("deadline", _future_deadline()),
        visibility=overrides.get("visibility", "restricted"),
        evaluation_requirement_id="req-1",
    )
    created = service.create(payload, created_by="admin-1")
    return service.publish(created["id"])


def _signed_fields(hackathon, team_id="team-9", team_name="Tensor Warriors", leader="Ada"):
    exp = int(now_ist().timestamp()) + 3600
    secret = hackathon["signing_secret"]
    return {
        "external_team_id": team_id,
        "team_name": team_name,
        "leader_name": leader,
        "exp": exp,
        "sig": sign_team_link(secret, team_id, team_name, leader, exp),
    }


def test_publish_requires_a_theme_and_a_deadline():
    service, _firebase = _service()
    created = service.create(
        ExternalHackathonCreate(name="No theme yet", themes=[]),
        created_by="admin-1",
    )
    with pytest.raises(BadRequestError, match="theme"):
        service.publish(created["id"])


def test_public_view_hides_the_signing_secret():
    service, firebase = _service()
    published = _published(service)
    public = service.get_public(published["id"])
    assert "signing_secret" not in public
    assert public["form_url"].endswith(f"/external-hackathons/{published['id']}/submit")
    assert public["themes"][0]["name"] == "Health"
    assert firebase.writes == [EXTERNAL_HACKATHONS, EXTERNAL_HACKATHONS]


def test_restricted_link_rejects_a_bad_signature():
    service, _firebase = _service()
    published = _published(service)
    fields = _signed_fields(published)
    fields["sig"] = "0" * 64
    with pytest.raises(ForbiddenError, match="not valid"):
        service.prepare_upload(
            published["id"], ExternalUploadRequest(filename="demo.mp4", **fields)
        )
    good = _signed_fields(published)
    with patch(
        "app.services.external_hackathon_service.generate_signed_upload_url",
        return_value="https://upload.example/video",
    ):
        planned = service.prepare_upload(
            published["id"],
            ExternalUploadRequest(filename="demo.mp4", content_type="video/mp4", **good),
        )
    assert planned["upload_url"] == "https://upload.example/video"


def test_one_team_can_submit_once_and_the_video_stays_outside_student_paths():
    service, firebase = _service()
    published = _published(service, visibility="public")
    fields = {
        "external_team_id": "team-9",
        "team_name": "Tensor Warriors",
        "leader_name": "Ada",
    }
    with patch(
        "app.services.external_hackathon_service.generate_signed_upload_url",
        return_value="https://upload.example/video",
    ):
        planned = service.prepare_upload(
            published["id"],
            ExternalUploadRequest(filename="demo.mp4", content_type="video/mp4", **fields),
        )
    assert planned["video_path"].startswith(
        f"gs://eval-bucket/external-submissions/{published['id']}/team-9/"
    )
    assert "/submissions/" not in planned["video_path"]
    created = service.create_submission(
        published["id"],
        ExternalSubmissionCreate(
            submission_id=planned["submission_id"],
            video_path=planned["video_path"],
            content_type="video/mp4",
            theme_id="theme-1",
            problem_statement="Clinics lose patient context.",
            solution_description="A shared timeline for each visit.",
            **fields,
        ),
    )
    assert created["status"] == "uploaded"
    assert created["team_name"] == "Tensor Warriors"
    with pytest.raises(ConflictError, match="already submitted"):
        service.create_submission(
            published["id"],
            ExternalSubmissionCreate(
                submission_id="another-id",
                video_path=planned["video_path"],
                content_type="video/mp4",
                theme_id="theme-1",
                problem_statement="Clinics lose patient context.",
                solution_description="A shared timeline for each visit.",
                **fields,
            ),
        )
    assert EXTERNAL_SUBMISSIONS in firebase.writes
    assert "submissions" not in firebase.writes
    assert "hackathons" not in firebase.writes
    assert "analysis" not in firebase.writes


def test_deadline_blocks_a_new_submission():
    service, _firebase = _service()
    published = _published(service, visibility="public", deadline=_past_deadline())
    fields = {"external_team_id": "team-1", "team_name": "Late"}
    with pytest.raises(BadRequestError, match="deadline"):
        service.prepare_upload(
            published["id"],
            ExternalUploadRequest(filename="demo.mp4", content_type="video/mp4", **fields),
        )


def test_evaluation_writes_only_the_external_submission():
    firebase = _MemoryFirebase()
    service, _same = _service(firebase=firebase)
    published = _published(service, visibility="public")
    submission_id = "ext-sub-1"
    firebase.set_document(
        EXTERNAL_SUBMISSIONS,
        submission_id,
        {
            "id": submission_id,
            "external_hackathon_id": published["id"],
            "external_team_id": "team-9",
            "team_name": "Tensor Warriors",
            "theme_id": "theme-1",
            "theme_name": "Health",
            "theme_description": "Care",
            "problem_statement": "Clinics lose patient context.",
            "solution_description": "A shared timeline for each visit.",
            "video_path": "gs://eval-bucket/external-submissions/x/team-9/ext-sub-1/video.mp4",
            "content_type": "video/mp4",
            "status": "processing",
        },
    )
    analysis = MagicMock()
    analysis._generate_checklist.return_value = "checklist"
    analysis._load_scoring_config.return_value = (None, [])
    analysis._score_ai_metrics.return_value = []
    analysis._analyze_video.return_value = "The demo shows the timeline."
    analysis._find_video_metric.return_value = None
    analysis._format_field_scores_markdown.return_value = ""
    firebase.writes.clear()

    result = ExternalEvaluationService(firebase=firebase, submission_service=analysis).evaluate(
        submission_id
    )

    assert result["status"] == "processed"
    saved = firebase.get_document(EXTERNAL_SUBMISSIONS, submission_id)
    assert saved["status"] == "completed"
    assert saved["evaluation"]["report"] == "The demo shows the timeline."
    analysis._generate_checklist.assert_called_once()
    assert analysis._generate_checklist.call_args.kwargs["hackathon_id"] is None
    analysis._analyze_video.assert_called_once()
    assert analysis._analyze_video.call_args.kwargs["hackathon_id"] is None
    assert firebase.writes == [EXTERNAL_SUBMISSIONS]
    rubric = analysis._load_scoring_config.call_args.args[0]
    assert rubric["timeline"][0]["evaluation_requirement_id"] == "req-1"


def test_external_job_uses_a_separate_worker_path():
    personal = "https://personal.example/internal/jobs/evaluate-submission"
    assert external_job_url(personal) == "https://personal.example" + EXTERNAL_JOB_PATH
    jobs = EvaluationJobService()
    jobs.project = "demo-proj"
    jobs.queue = "evaluation-jobs"
    jobs.job_secret = "secret-value"
    with (
        patch.object(jobs, "_select_worker_url", return_value=("personal", personal)),
        patch("google.cloud.tasks_v2.CloudTasksClient") as client_cls,
    ):
        client = client_cls.return_value
        client.queue_path.return_value = "projects/p/locations/asia-south1/queues/evaluation-jobs"
        created = MagicMock()
        created.name = "task-ext"
        client.create_task.return_value = created
        jobs.enqueue_external_cloud_task("ext-sub-1")
    task = client.create_task.call_args.kwargs["request"]["task"]
    assert task["http_request"]["url"].endswith(EXTERNAL_JOB_PATH)
    assert b"external_submission_id" in task["http_request"]["body"]
    assert b"ext-sub-1" in task["http_request"]["body"]
