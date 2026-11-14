"""A student submission uses the requirement linked to that hackathon round."""

from unittest.mock import MagicMock

from app.services.hackathon_service import HackathonService
from app.services.submission.analysis import AnalysisMixin
from app.utils.hackathon_round import requirement_id_on_round


def _service() -> HackathonService:
    requirements = MagicMock()
    requirements.get_requirement.side_effect = lambda req_id: {
        "req-published": {
            "id": "req-published",
            "name": "Round 2 requirement",
            "description": "Fields for round 2",
            "fields": [{"key": "problem_statement", "label": "Problem", "field_type": "textarea"}],
        },
        "req-draft": {
            "id": "req-draft",
            "name": "Draft requirement",
            "description": None,
            "fields": [{"key": "github_link", "label": "GitHub", "field_type": "url"}],
        },
    }.get(req_id)
    return HackathonService(firebase=MagicMock(), evaluation_requirements=requirements)


def _hackathon() -> dict:
    return {
        "id": "hack-1",
        "name": "Campus hack",
        "start_date": "2026-11-01",
        "end_date": "2026-11-30",
        "timeline": [
            {
                "title": "Draft round",
                "evaluation_requirement_id": "req-draft",
                "published": False,
            },
            {
                "title": "Open round",
                "evaluation_requirement_id": "req-published",
                "published": True,
                "start_date": "2026-11-01",
                "end_date": "2026-11-30",
            },
        ],
    }


def test_student_timeline_keeps_only_the_linked_requirement():
    visible = _service().filter_timeline_for_student(_hackathon())
    assert len(visible["timeline"]) == 1
    round_ = visible["timeline"][0]
    assert round_["round_index"] == 1
    assert round_["evaluation_requirement"]["id"] == "req-published"
    assert round_["evaluation_requirement"]["name"] == "Round 2 requirement"
    assert round_["evaluation_requirement"]["fields"][0]["key"] == "problem_statement"


def test_round_requirement_hides_unpublished_rounds_from_students():
    service = _service()
    hackathon = _hackathon()
    assert service.round_evaluation_requirement(hackathon, 0, published_only=True) is None
    published = service.round_evaluation_requirement(hackathon, 1, published_only=True)
    assert published["id"] == "req-published"
    draft = service.round_evaluation_requirement(hackathon, 0, published_only=False)
    assert draft["name"] == "Draft requirement"


def test_stored_rounds_do_not_keep_the_resolved_requirement():
    stored = HackathonService._normalize_round_for_storage(
        {
            "title": "Round 1",
            "evaluation_requirement_id": "req-published",
            "evaluation_requirement": {"id": "req-published", "name": "Resolved"},
            "round_index": 1,
        },
        published=False,
    )
    assert "evaluation_requirement" not in stored
    assert "round_index" not in stored
    assert stored["evaluation_requirement_id"] == "req-published"


def test_scoring_uses_the_submission_round_requirement():
    hackathon = _hackathon()
    assert AnalysisMixin._resolve_evaluation_requirement_id(hackathon, 1) == "req-published"
    assert AnalysisMixin._resolve_evaluation_requirement_id(hackathon, 0) == "req-draft"
    assert requirement_id_on_round(hackathon["timeline"][1]) == "req-published"
