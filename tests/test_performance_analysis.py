"""Analysis screen: campus participation, project rating, and evaluator speed."""

from datetime import date

import pytest

from app.exceptions import NotFoundError
from app.services.performance_analysis_service import PerformanceAnalysisService


class FakeFirebase:
    def __init__(self) -> None:
        self.store: dict[tuple[str, str], dict] = {}

    def set_document(self, collection, document_id, data):
        self.store[(collection, document_id)] = dict(data)
        return True

    def get_document(self, collection, document_id):
        doc = self.store.get((collection, document_id))
        return dict(doc) if doc is not None else None

    def get_collection(self, collection):
        return [
            {"id": doc_id, **data}
            for (coll, doc_id), data in self.store.items()
            if coll == collection
        ]

    def query_collection(self, collection, field, operator, value):
        assert operator == "=="
        return [row for row in self.get_collection(collection) if row.get(field) == value]


def _seed(firebase: FakeFirebase) -> None:
    firebase.set_document(
        "universities",
        "niat-hyd",
        {
            "name": "NIAT Hyderabad",
            "college_type": "NIAT",
            "location": "Hyderabad",
            "total_students": 120,
        },
    )
    firebase.set_document(
        "universities",
        "other-uni",
        {"name": "City College", "college_type": "Non NIAT", "location": "Pune"},
    )
    firebase.set_document(
        "batches",
        "batch-a",
        {"university_id": "niat-hyd", "name": "2024-2028", "total_students": 40},
    )
    firebase.set_document(
        "batches",
        "batch-b",
        {"university_id": "niat-hyd", "name": "2025-2029", "total_students": 30},
    )
    firebase.set_document(
        "users",
        "ada",
        {
            "role": "student",
            "name": "Ada",
            "university_id": "niat-hyd",
            "batch_id": "batch-a",
            "college_type": "NIAT",
        },
    )
    firebase.set_document(
        "users",
        "grace",
        {
            "role": "student",
            "name": "Grace",
            "university_id": "niat-hyd",
            "batch_id": "batch-b",
            "college_type": "NIAT",
        },
    )
    firebase.set_document(
        "users",
        "lin",
        {
            "role": "student",
            "name": "Lin",
            "university_id": "other-uni",
            "batch_id": "batch-x",
            "college_type": "Non NIAT",
        },
    )
    firebase.set_document(
        "users",
        "eva",
        {"role": "evaluator", "name": "Eva", "email": "eva@nxtwave.co.in"},
    )
    firebase.set_document(
        "hackathons",
        "launchpad",
        {
            "name": "Nxtwave Launchpad",
            "start_date": "2026-08-01",
            "end_date": "2026-08-10",
            "visibility": {
                "all_universities": False,
                "all_batches": False,
                "university_ids": ["niat-hyd"],
                "batch_ids": ["batch-a"],
            },
            "timeline": [
                {"title": "Finals", "winner_announcement": {"top_n": 5, "announced_count": 1}}
            ],
        },
    )
    firebase.set_document(
        "hackathons",
        "open-cup",
        {
            "name": "Open Cup",
            "start_date": "2026-09-01",
            "end_date": "2026-09-02",
            "timeline": [{"title": "Round 1"}],
        },
    )


def test_selected_hackathon_counts_campus_batch_and_rating():
    firebase = FakeFirebase()
    _seed(firebase)
    firebase.set_document(
        "hackathon_enrollments",
        "enroll-ada",
        {"hackathon_id": "launchpad", "round_index": 0, "user_id": "ada"},
    )
    firebase.set_document(
        "submissions",
        "sub-ada",
        {
            "hackathon_id": "launchpad",
            "round_index": 0,
            "student_id": "ada",
            "team_name": "Ada Team",
            "review_status": "approved",
            "final_score": 82,
            "assigned_evaluator_id": "eva",
            "assigned_at": "2026-08-02T10:00:00+05:30",
            "submitted_for_review_at": "2026-08-02T12:12:00+05:30",
        },
    )
    firebase.set_document(
        "submissions",
        "sub-grace",
        {
            "hackathon_id": "open-cup",
            "round_index": 0,
            "student_id": "grace",
            "team_name": "Grace Team",
            "review_status": "approved",
            "final_score": 70,
        },
    )

    service = PerformanceAnalysisService(firebase=firebase)
    report = service.hackathon_analysis(["launchpad", "open-cup"], top_n=5)

    assert report["portal_registered_students"] == 3
    assert report["selected_count"] == 2
    launchpad = report["hackathons"][0]
    assert launchpad["eligible_students"] == 1
    assert launchpad["enrolled_students"] == 1
    assert launchpad["participating_students"] == 1
    assert launchpad["submission_count"] == 1
    assert launchpad["niat"]["campus_count"] == 1
    assert launchpad["niat"]["headcount"] == 120
    assert launchpad["niat"]["participating_students"] == 1
    assert launchpad["batches"][0]["batch_id"] == "batch-a"
    assert launchpad["batches"][0]["strength"] == 40
    assert launchpad["batches"][0]["participation_rate_percent"] == 2.5
    quality = launchpad["project_quality"]
    assert quality["winner_count"] == 1
    assert quality["average_score"] == 82
    assert quality["average_rating_10"] == 8.2
    assert quality["announced_top_n"] == 5
    assert quality["winners"][0]["student_name"] == "Ada"
    assert report["comparison"][0]["submission_count"] == 1
    assert report["comparison"][1]["name"] == "Open Cup"
    assert report["average_project_rating_10"] == 7.6
    assert {row["university_id"] for row in report["university_comparison"]} >= {"niat-hyd"}


def test_top_n_keeps_a_tie_at_the_cutoff():
    firebase = FakeFirebase()
    _seed(firebase)
    for index, score in enumerate((90, 82, 82)):
        firebase.set_document(
            "submissions",
            f"sub-{index}",
            {
                "hackathon_id": "launchpad",
                "round_index": 0,
                "student_id": "ada",
                "team_name": f"Team {index}",
                "review_status": "approved",
                "final_score": score,
            },
        )
    report = PerformanceAnalysisService(firebase=firebase).hackathon_analysis(
        ["launchpad"], top_n=2
    )
    quality = report["hackathons"][0]["project_quality"]
    assert quality["winner_count"] == 3
    assert quality["tie_at_cutoff"] is True
    assert quality["average_rating_10"] == 8.5


def test_missing_hackathon_is_not_found():
    service = PerformanceAnalysisService(firebase=FakeFirebase())
    with pytest.raises(NotFoundError):
        service.hackathon_analysis(["missing"], top_n=3)


def test_evaluator_day_chart_and_review_hours():
    firebase = FakeFirebase()
    _seed(firebase)
    firebase.set_document(
        "submissions",
        "fast",
        {
            "hackathon_id": "launchpad",
            "student_id": "ada",
            "assigned_evaluator_id": "eva",
            "assigned_at": "2026-08-01T10:00:00+05:30",
            "submitted_for_review_at": "2026-08-01T12:30:00+05:30",
            "review_status": "approved",
            "final_score": 80,
        },
    )
    firebase.set_document(
        "submissions",
        "next-day",
        {
            "hackathon_id": "launchpad",
            "student_id": "grace",
            "assigned_evaluator_id": "eva",
            "assigned_at": "2026-08-01T18:00:00+05:30",
            "submitted_for_review_at": "2026-08-02T10:00:00+05:30",
            "review_status": "changes_requested",
        },
    )
    firebase.set_document(
        "submissions",
        "waiting",
        {
            "hackathon_id": "launchpad",
            "student_id": "lin",
            "assigned_evaluator_id": "eva",
            "assigned_at": "2026-08-03T09:00:00+05:30",
        },
    )

    report = PerformanceAnalysisService(firebase=firebase).evaluator_analysis(
        ["launchpad"],
        ["eva"],
        date(2026, 8, 1),
        date(2026, 8, 3),
    )
    assert report["assigned"] == 3
    assert report["completed"] == 2
    assert report["pending"] == 1
    assert report["daily"] == [
        {"date": "2026-08-01", "assigned": 2, "completed": 1},
        {"date": "2026-08-02", "assigned": 0, "completed": 1},
        {"date": "2026-08-03", "assigned": 1, "completed": 0},
    ]
    eva = report["evaluators"][0]
    assert eva["average_hours"] == 9.25
    assert eva["fastest_hours"] == 2.5
    assert eva["slowest_hours"] == 16.0
    assert eva["average_score"] == 80
    assert eva["average_rating_10"] == 8.0
    assert eva["rework_rate_percent"] == 50.0
    assert eva["same_day_completion_rate_percent"] == 33.3
