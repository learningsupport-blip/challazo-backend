"""Hackathon and evaluator numbers for the Analysis screen.

Counts come from universities, batches, student profiles, enrollments, and
submissions. Nothing is stored separately. Project rating is the approved
final score divided by 10, so 82 out of 100 is 8.2 out of 10.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime
from typing import Any

from app.exceptions import NotFoundError
from app.models.performance_analysis_model import MAX_HACKATHONS, TOP_N_CHOICES
from app.services.firebase import FirebaseService
from app.services.leaderboard_service import assign_competition_ranks, select_top_entries
from app.services.university_service import _stored_total_students
from app.utils.hackathon_round import submission_round_index
from app.utils.hackathon_visibility import normalize_visibility, student_can_see
from app.utils.time import parse_to_ist


SCORE_BANDS: tuple[tuple[str, float, float], ...] = (
    ("90-100", 90, 100),
    ("75-89", 75, 90),
    ("60-74", 60, 75),
    ("40-59", 40, 60),
    ("0-39", 0, 40),
)


def _percent(part: int, whole: int) -> float | None:
    if whole <= 0:
        return None
    return round(100 * part / whole, 1)


def _mean(values: list[float], digits: int = 1) -> float | None:
    if not values:
        return None
    return round(sum(values) / len(values), digits)


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return round(ordered[mid], 2)
    return round((ordered[mid - 1] + ordered[mid]) / 2, 2)


def _rating(score: float) -> float:
    return round(score / 10, 1)


def _rating_from(values: list[float]) -> float | None:
    if not values:
        return None
    return round((sum(values) / len(values)) / 10, 1)


def _person_name(user: dict[str, Any] | None) -> str:
    if not user:
        return "Unknown"
    name = str(user.get("name") or "").strip()
    if name:
        return name
    joined = f"{user.get('first_name') or ''} {user.get('last_name') or ''}".strip()
    return joined or str(user.get("email") or "Unknown")


def _college_type(student: dict[str, Any] | None, university: dict[str, Any] | None) -> str:
    raw = str((student or {}).get("college_type") or "").strip()
    if raw in ("NIAT", "Non NIAT"):
        return raw
    uni = str((university or {}).get("college_type") or "").strip()
    if uni == "Non NIAT":
        return "Non NIAT"
    if university or uni == "NIAT":
        return "NIAT"
    return "Unknown"


def _score(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _ist_day(value: Any) -> date | None:
    if not value:
        return None
    try:
        return parse_to_ist(value).date()
    except (TypeError, ValueError):
        return None


def _hours_between(start: Any, end: Any) -> float | None:
    try:
        opened = parse_to_ist(start)
        closed = parse_to_ist(end)
    except (TypeError, ValueError):
        return None
    if not isinstance(opened, datetime) or closed < opened:
        return None
    return round((closed - opened).total_seconds() / 3600, 2)


def _in_window(day: date | None, start: date | None, end: date | None) -> bool:
    if day is None:
        return False
    if start is not None and day < start:
        return False
    if end is not None and day > end:
        return False
    return True


def _dedupe(values: list[str], *, limit: int, label: str) -> list[str]:
    seen: list[str] = []
    for raw in values:
        text = str(raw or "").strip()
        if text and text not in seen:
            seen.append(text)
    if len(seen) > limit:
        raise ValueError(f"Choose at most {limit} {label}")
    return seen


def _band_label(score: float) -> str:
    if score >= 100:
        return "90-100"
    for label, low, high in SCORE_BANDS:
        if low <= score < high or (label == "90-100" and score == 100):
            return label
    return "0-39"


class PerformanceAnalysisService:
    """Read-only aggregates. Safe to call on every filter change."""

    def __init__(self, firebase: FirebaseService | None = None):
        self.firebase = firebase or FirebaseService()

    def options(self) -> dict[str, Any]:
        hackathons = []
        for row in self.firebase.get_collection("hackathons"):
            hackathons.append(
                {
                    "id": row.get("id"),
                    "name": str(row.get("name") or "Hackathon"),
                    "start_date": str(row.get("start_date") or ""),
                    "end_date": str(row.get("end_date") or ""),
                }
            )
        hackathons.sort(key=lambda item: (item["start_date"], item["name"]), reverse=True)
        evaluators = []
        for row in self.firebase.get_collection("users"):
            if row.get("role") != "evaluator":
                continue
            evaluators.append(
                {
                    "id": row.get("id"),
                    "name": _person_name(row),
                    "email": str(row.get("email") or ""),
                }
            )
        evaluators.sort(key=lambda item: item["name"].lower())
        return {
            "hackathons": hackathons,
            "evaluators": evaluators,
            "top_n_choices": list(TOP_N_CHOICES),
            "max_hackathons": MAX_HACKATHONS,
            "score_max": 100,
            "rating_max": 10,
        }

    def hackathon_analysis(self, hackathon_ids: list[str], top_n: int) -> dict[str, Any]:
        ids = _dedupe(hackathon_ids, limit=MAX_HACKATHONS, label="hackathons")
        if not ids:
            raise ValueError("Select at least one hackathon")
        if top_n < 1 or top_n > 20:
            raise ValueError("top_n must be between 1 and 20")

        universities = self.firebase.get_collection("universities")
        batches = self.firebase.get_collection("batches")
        students = [
            row for row in self.firebase.get_collection("users") if row.get("role") == "student"
        ]
        students_by_id = {str(row.get("id")): row for row in students}
        uni_by_id = {str(row.get("id")): row for row in universities}
        batch_by_id = {str(row.get("id")): row for row in batches}
        portal = len(students)

        items = []
        for hackathon_id in ids:
            hackathon = self.firebase.get_document("hackathons", hackathon_id)
            if not hackathon:
                raise NotFoundError("Hackathon not found", code="HACKATHON_NOT_FOUND")
            hackathon = {**hackathon, "id": hackathon.get("id") or hackathon_id}
            submissions = self.firebase.query_collection(
                "submissions", "hackathon_id", "==", hackathon_id
            )
            enrollments = self.firebase.query_collection(
                "hackathon_enrollments", "hackathon_id", "==", hackathon_id
            )
            items.append(
                self._one_hackathon(
                    hackathon,
                    submissions,
                    enrollments,
                    students,
                    students_by_id,
                    uni_by_id,
                    batch_by_id,
                    universities,
                    batches,
                    portal,
                    top_n,
                )
            )

        participant_ids: set[str] = set()
        for item in items:
            participant_ids.update(item.pop("_participant_ids"))
        ratings = [
            item["project_quality"]["average_rating_10"]
            for item in items
            if item["project_quality"]["average_rating_10"] is not None
        ]
        scores = [
            item["project_quality"]["average_score"]
            for item in items
            if item["project_quality"]["average_score"] is not None
        ]
        return {
            "top_n": top_n,
            "score_max": 100,
            "rating_max": 10,
            "portal_registered_students": portal,
            "selected_count": len(items),
            "unique_participating_students": len(participant_ids),
            "total_submissions": sum(item["submission_count"] for item in items),
            "sum_enrolled_students": sum(item["enrolled_students"] for item in items),
            "average_project_rating_10": _mean(ratings),
            "average_winner_score": _mean(scores),
            "comparison": [self._comparison_point(item) for item in items],
            "university_comparison": self._university_comparison(items),
            "batch_comparison": self._batch_comparison(items),
            "hackathons": items,
        }

    def evaluator_analysis(
        self,
        hackathon_ids: list[str],
        evaluator_ids: list[str],
        from_date: date | None,
        to_date: date | None,
    ) -> dict[str, Any]:
        if from_date and to_date and from_date > to_date:
            raise ValueError("from_date must be on or before to_date")
        selected_hackathons = set(_dedupe(hackathon_ids, limit=MAX_HACKATHONS, label="hackathons"))
        selected_evaluators = _dedupe(evaluator_ids, limit=50, label="evaluators")
        selected_evaluator_set = set(selected_evaluators)

        users = {str(row.get("id")): row for row in self.firebase.get_collection("users")}
        hackathon_names = {
            str(row.get("id")): str(row.get("name") or "Hackathon")
            for row in self.firebase.get_collection("hackathons")
        }
        if selected_hackathons:
            submissions: list[dict[str, Any]] = []
            for hackathon_id in selected_hackathons:
                if hackathon_id not in hackathon_names and not self.firebase.get_document(
                    "hackathons", hackathon_id
                ):
                    raise NotFoundError("Hackathon not found", code="HACKATHON_NOT_FOUND")
                submissions.extend(
                    self.firebase.query_collection(
                        "submissions", "hackathon_id", "==", hackathon_id
                    )
                )
        else:
            submissions = self.firebase.get_collection("submissions")

        buckets: dict[str, dict[str, Any]] = {}
        for evaluator_id in selected_evaluators:
            buckets[evaluator_id] = self._empty_bucket(evaluator_id, users.get(evaluator_id))

        for submission in submissions:
            evaluator_id = str(submission.get("assigned_evaluator_id") or "").strip()
            if not evaluator_id:
                continue
            if selected_evaluator_set and evaluator_id not in selected_evaluator_set:
                continue
            bucket = buckets.get(evaluator_id)
            if bucket is None:
                bucket = self._empty_bucket(evaluator_id, users.get(evaluator_id))
                buckets[evaluator_id] = bucket
            self._add_assignment(
                bucket,
                submission,
                hackathon_names.get(str(submission.get("hackathon_id") or ""), "Hackathon"),
                from_date,
                to_date,
            )

        evaluators = [self._finish_bucket(bucket) for bucket in buckets.values()]
        evaluators = [
            row
            for row in evaluators
            if row["assigned"]
            or row["completed"]
            or evaluator_id_requested(row, selected_evaluators)
        ]
        evaluators.sort(key=lambda row: (-row["assigned"], row["name"].lower()))
        return self._rollup_evaluators(evaluators, from_date, to_date)

    def _one_hackathon(
        self,
        hackathon: dict[str, Any],
        submissions: list[dict[str, Any]],
        enrollments: list[dict[str, Any]],
        students: list[dict[str, Any]],
        students_by_id: dict[str, dict[str, Any]],
        uni_by_id: dict[str, dict[str, Any]],
        batch_by_id: dict[str, dict[str, Any]],
        universities: list[dict[str, Any]],
        batches: list[dict[str, Any]],
        portal: int,
        top_n: int,
    ) -> dict[str, Any]:
        rule = normalize_visibility(hackathon)
        scoped_unis, scoped_batches = _scope(hackathon, universities, batches, rule)
        enrolled_ids = {
            str(row.get("user_id") or "").strip()
            for row in enrollments
            if str(row.get("user_id") or "").strip()
        }
        participant_ids = {
            str(row.get("student_id") or "").strip()
            for row in submissions
            if str(row.get("student_id") or "").strip()
        }
        eligible = [
            student
            for student in students
            if student_can_see(hackathon, student.get("university_id"), student.get("batch_id"))
        ]
        eligible_ids = {str(student.get("id")) for student in eligible}

        def _student_type(student: dict[str, Any]) -> str:
            uni = uni_by_id.get(str(student.get("university_id") or ""))
            return _college_type(student, uni)

        niat_students = [student for student in eligible if _student_type(student) == "NIAT"]
        other_students = [student for student in eligible if _student_type(student) != "NIAT"]

        def _block(
            campus_rows: list[dict[str, Any]], people: list[dict[str, Any]]
        ) -> dict[str, Any]:
            people_ids = {str(student.get("id")) for student in people}
            headcounts = [_stored_total_students(row.get("total_students")) for row in campus_rows]
            known = [value for value in headcounts if value is not None]
            return {
                "campus_count": len(campus_rows),
                "headcount": sum(known) if known else None,
                "campuses_missing_headcount": sum(1 for value in headcounts if value is None),
                "registered_students": len(people),
                "enrolled_students": len(people_ids & enrolled_ids),
                "participating_students": len(people_ids & participant_ids),
                "submission_count": sum(
                    1 for row in submissions if str(row.get("student_id") or "") in people_ids
                ),
            }

        niat_unis = [row for row in scoped_unis if _college_type(None, row) == "NIAT"]
        other_unis = [row for row in scoped_unis if _college_type(None, row) != "NIAT"]
        without_campus = sum(
            1
            for student in eligible
            if not str(student.get("university_id") or "").strip()
            or not str(student.get("batch_id") or "").strip()
        )

        assigned = 0
        reviewed = 0
        approved = 0
        pending_review = 0
        changes = 0
        for row in submissions:
            if str(row.get("assigned_evaluator_id") or "").strip():
                assigned += 1
            if row.get("submitted_for_review_at"):
                reviewed += 1
            status = row.get("review_status") or "none"
            if status == "approved":
                approved += 1
            elif status == "pending_review":
                pending_review += 1
            elif status == "changes_requested":
                changes += 1

        quality = self._project_quality(
            hackathon, submissions, students_by_id, uni_by_id, batch_by_id, top_n
        )
        university_rows = [
            self._campus_row(
                uni, scoped_batches, eligible, enrolled_ids, participant_ids, submissions
            )
            for uni in scoped_unis
        ]
        batch_rows = [
            self._batch_row(
                batch,
                uni_by_id.get(str(batch.get("university_id") or "")),
                eligible,
                enrolled_ids,
                participant_ids,
                submissions,
            )
            for batch in scoped_batches
        ]
        university_rows.sort(
            key=lambda row: (-row["participating_students"], row["university_name"])
        )
        batch_rows.sort(key=lambda row: (-row["participating_students"], row["batch_name"]))
        submission_count = len(submissions)
        return {
            "hackathon_id": hackathon["id"],
            "name": str(hackathon.get("name") or "Hackathon"),
            "start_date": str(hackathon.get("start_date") or ""),
            "end_date": str(hackathon.get("end_date") or ""),
            "all_universities": bool(rule["all_universities"]),
            "all_batches": bool(rule["all_batches"]),
            "portal_registered_students": portal,
            "eligible_students": len(eligible_ids),
            "enrolled_students": len(enrolled_ids),
            "participating_students": len(participant_ids),
            "submission_count": submission_count,
            "students_without_campus": without_campus,
            "assigned_count": assigned,
            "review_submitted_count": reviewed,
            "approved_count": approved,
            "pending_review_count": pending_review,
            "changes_requested_count": changes,
            "assignment_coverage_percent": _percent(assigned, submission_count),
            "approval_rate_percent": _percent(approved, submission_count),
            "niat": _block(niat_unis, niat_students),
            "non_niat": _block(other_unis, other_students),
            "project_quality": quality,
            "universities": university_rows,
            "batches": batch_rows,
            "_participant_ids": participant_ids,
        }

    def _campus_row(
        self,
        university: dict[str, Any],
        scoped_batches: list[dict[str, Any]],
        eligible: list[dict[str, Any]],
        enrolled_ids: set[str],
        participant_ids: set[str],
        submissions: list[dict[str, Any]],
    ) -> dict[str, Any]:
        uni_id = str(university.get("id"))
        people = [
            student for student in eligible if str(student.get("university_id") or "") == uni_id
        ]
        people_ids = {str(student.get("id")) for student in people}
        headcount = _stored_total_students(university.get("total_students"))
        if headcount is None:
            strengths = [
                _stored_total_students(batch.get("total_students"))
                for batch in scoped_batches
                if str(batch.get("university_id") or "") == uni_id
            ]
            known = [value for value in strengths if value is not None]
            headcount = sum(known) if known else None
        participating = len(people_ids & participant_ids)
        return {
            "university_id": uni_id,
            "university_name": str(university.get("name") or "University"),
            "college_type": _college_type(None, university),
            "headcount": headcount,
            "registered_students": len(people),
            "enrolled_students": len(people_ids & enrolled_ids),
            "participating_students": participating,
            "submission_count": sum(
                1 for row in submissions if str(row.get("student_id") or "") in people_ids
            ),
            "participation_rate_percent": _percent(participating, headcount or 0),
        }

    def _batch_row(
        self,
        batch: dict[str, Any],
        university: dict[str, Any] | None,
        eligible: list[dict[str, Any]],
        enrolled_ids: set[str],
        participant_ids: set[str],
        submissions: list[dict[str, Any]],
    ) -> dict[str, Any]:
        batch_id = str(batch.get("id"))
        people = [student for student in eligible if str(student.get("batch_id") or "") == batch_id]
        people_ids = {str(student.get("id")) for student in people}
        strength = _stored_total_students(batch.get("total_students"))
        participating = len(people_ids & participant_ids)
        return {
            "batch_id": batch_id,
            "batch_name": str(batch.get("name") or "Batch"),
            "university_id": str(batch.get("university_id") or ""),
            "university_name": str((university or {}).get("name") or ""),
            "college_type": _college_type(None, university),
            "strength": strength,
            "registered_students": len(people),
            "enrolled_students": len(people_ids & enrolled_ids),
            "participating_students": participating,
            "submission_count": sum(
                1 for row in submissions if str(row.get("student_id") or "") in people_ids
            ),
            "participation_rate_percent": _percent(participating, strength or 0),
        }

    def _project_quality(
        self,
        hackathon: dict[str, Any],
        submissions: list[dict[str, Any]],
        students_by_id: dict[str, dict[str, Any]],
        uni_by_id: dict[str, dict[str, Any]],
        batch_by_id: dict[str, dict[str, Any]],
        top_n: int,
    ) -> dict[str, Any]:
        timeline = hackathon.get("timeline") or []
        scored_rounds: list[tuple[int, dict[str, Any], list[dict[str, Any]]]] = []
        if isinstance(timeline, list):
            for index, round_ in enumerate(timeline):
                if not isinstance(round_, dict):
                    continue
                approved = [
                    row
                    for row in submissions
                    if submission_round_index(row) == index
                    and (row.get("review_status") or "none") == "approved"
                    and _score(row.get("final_score")) is not None
                ]
                if approved:
                    scored_rounds.append((index, round_, approved))
        if not scored_rounds:
            approved = [
                row
                for row in submissions
                if (row.get("review_status") or "none") == "approved"
                and _score(row.get("final_score")) is not None
            ]
            if approved:
                scored_rounds.append((0, {}, approved))

        empty = {
            "round_index": None,
            "round_title": "",
            "requested_top_n": top_n,
            "approved_count": 0,
            "winner_count": 0,
            "tie_at_cutoff": False,
            "average_score": None,
            "average_rating_10": None,
            "field_average_score": None,
            "field_average_rating_10": None,
            "announced_top_n": None,
            "announced_count": None,
            "winners": [],
            "score_bands": [{"label": label, "count": 0} for label, _, _ in SCORE_BANDS],
        }
        if not scored_rounds:
            return empty

        index, round_, approved = scored_rounds[-1]
        ranked = assign_competition_ranks(approved)
        selected = select_top_entries(ranked, top_n)
        scores = [_score(row.get("final_score")) or 0 for row in selected]
        field_scores = [_score(row.get("final_score")) or 0 for row in approved]
        bands = {label: 0 for label, _, _ in SCORE_BANDS}
        for value in field_scores:
            bands[_band_label(value)] += 1
        announcement = round_.get("winner_announcement") if isinstance(round_, dict) else None
        if not isinstance(announcement, dict):
            announcement = {}
        winners = []
        for row in selected:
            student = students_by_id.get(str(row.get("student_id") or ""))
            uni = uni_by_id.get(str((student or {}).get("university_id") or ""))
            batch = batch_by_id.get(str((student or {}).get("batch_id") or ""))
            score = _score(row.get("final_score")) or 0
            winners.append(
                {
                    "rank": int(row["rank"]),
                    "rank_label": row["rank_label"],
                    "score": round(score, 1),
                    "rating_10": _rating(score),
                    "submission_id": str(row.get("id") or ""),
                    "student_id": str(row.get("student_id") or ""),
                    "student_name": _person_name(student),
                    "team_name": str(row.get("team_name") or ""),
                    "university_name": str((uni or {}).get("name") or ""),
                    "batch_name": str((batch or {}).get("name") or ""),
                    "college_type": _college_type(student, uni),
                }
            )
        natural_cutoff = min(top_n, len(ranked))
        return {
            "round_index": index,
            "round_title": str(round_.get("title") or ""),
            "requested_top_n": top_n,
            "approved_count": len(approved),
            "winner_count": len(selected),
            "tie_at_cutoff": len(selected) > natural_cutoff,
            "average_score": _mean(scores),
            "average_rating_10": _rating_from(scores),
            "field_average_score": _mean(field_scores),
            "field_average_rating_10": _rating_from(field_scores),
            "announced_top_n": announcement.get("top_n"),
            "announced_count": announcement.get("announced_count"),
            "winners": winners,
            "score_bands": [{"label": label, "count": bands[label]} for label, _, _ in SCORE_BANDS],
        }

    @staticmethod
    def _comparison_point(item: dict[str, Any]) -> dict[str, Any]:
        quality = item["project_quality"]
        return {
            "hackathon_id": item["hackathon_id"],
            "name": item["name"],
            "start_date": item["start_date"],
            "end_date": item["end_date"],
            "portal_registered_students": item["portal_registered_students"],
            "eligible_students": item["eligible_students"],
            "enrolled_students": item["enrolled_students"],
            "participating_students": item["participating_students"],
            "submission_count": item["submission_count"],
            "niat_campus_count": item["niat"]["campus_count"],
            "niat_headcount": item["niat"]["headcount"],
            "niat_participating": item["niat"]["participating_students"],
            "non_niat_participating": item["non_niat"]["participating_students"],
            "winner_count": quality["winner_count"],
            "average_score": quality["average_score"],
            "average_rating_10": quality["average_rating_10"],
        }

    @staticmethod
    def _university_comparison(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        order: list[str] = []
        rows: dict[str, dict[str, Any]] = {}
        for item in items:
            for campus in item["universities"]:
                uni_id = campus["university_id"]
                if uni_id not in rows:
                    order.append(uni_id)
                    rows[uni_id] = {
                        "university_id": uni_id,
                        "university_name": campus["university_name"],
                        "college_type": campus["college_type"],
                        "headcount": campus["headcount"],
                        "series": [],
                    }
                rows[uni_id]["series"].append(_series_point(item, campus))
        return [rows[uni_id] for uni_id in order]

    @staticmethod
    def _batch_comparison(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        order: list[str] = []
        rows: dict[str, dict[str, Any]] = {}
        for item in items:
            for batch in item["batches"]:
                batch_id = batch["batch_id"]
                if batch_id not in rows:
                    order.append(batch_id)
                    rows[batch_id] = {
                        "batch_id": batch_id,
                        "batch_name": batch["batch_name"],
                        "university_id": batch["university_id"],
                        "university_name": batch["university_name"],
                        "college_type": batch["college_type"],
                        "strength": batch["strength"],
                        "series": [],
                    }
                rows[batch_id]["series"].append(_series_point(item, batch))
        return [rows[batch_id] for batch_id in order]

    def _empty_bucket(self, evaluator_id: str, user: dict[str, Any] | None) -> dict[str, Any]:
        return {
            "evaluator_id": evaluator_id,
            "name": _person_name(user) if user else "Former evaluator",
            "email": str((user or {}).get("email") or ""),
            "assigned": 0,
            "completed": 0,
            "pending": 0,
            "same_day": 0,
            "approved": 0,
            "pending_review": 0,
            "changes_requested": 0,
            "hours": [],
            "scores": [],
            "daily_assigned": defaultdict(int),
            "daily_completed": defaultdict(int),
            "by_hackathon": {},
            "undated_assigned": 0,
        }

    def _add_assignment(
        self,
        bucket: dict[str, Any],
        submission: dict[str, Any],
        hackathon_name: str,
        from_date: date | None,
        to_date: date | None,
    ) -> None:
        assigned_day = _ist_day(submission.get("assigned_at"))
        completed_day = _ist_day(submission.get("submitted_for_review_at"))
        assigned_here = _in_window(assigned_day, from_date, to_date) or (
            assigned_day is None and from_date is None and to_date is None
        )
        completed_here = bool(submission.get("submitted_for_review_at")) and _in_window(
            completed_day, from_date, to_date
        )
        if assigned_day is None and (from_date or to_date):
            assigned_here = False
        if not assigned_here and not completed_here:
            return
        hackathon_id = str(submission.get("hackathon_id") or "")
        per_hackathon = bucket["by_hackathon"].setdefault(
            hackathon_id,
            {
                "hackathon_id": hackathon_id,
                "hackathon_name": hackathon_name,
                "assigned": 0,
                "completed": 0,
                "pending": 0,
                "hours": [],
            },
        )
        if assigned_here:
            bucket["assigned"] += 1
            per_hackathon["assigned"] += 1
            if assigned_day is None:
                bucket["undated_assigned"] += 1
            else:
                bucket["daily_assigned"][assigned_day.isoformat()] += 1
        if completed_here:
            bucket["completed"] += 1
            per_hackathon["completed"] += 1
            if completed_day is not None:
                bucket["daily_completed"][completed_day.isoformat()] += 1
            hours = _hours_between(
                submission.get("assigned_at"), submission.get("submitted_for_review_at")
            )
            if hours is not None:
                bucket["hours"].append(hours)
                per_hackathon["hours"].append(hours)
            if assigned_day is not None and assigned_day == completed_day:
                bucket["same_day"] += 1
            status = submission.get("review_status") or "none"
            if status == "approved":
                bucket["approved"] += 1
                score = _score(submission.get("final_score"))
                if score is not None:
                    bucket["scores"].append(score)
            elif status == "pending_review":
                bucket["pending_review"] += 1
            elif status == "changes_requested":
                bucket["changes_requested"] += 1
        elif assigned_here:
            bucket["pending"] += 1
            per_hackathon["pending"] += 1

    def _finish_bucket(self, bucket: dict[str, Any]) -> dict[str, Any]:
        days = sorted(set(bucket["daily_assigned"]) | set(bucket["daily_completed"]))
        by_hackathon = []
        for row in bucket["by_hackathon"].values():
            by_hackathon.append(
                {
                    "hackathon_id": row["hackathon_id"],
                    "hackathon_name": row["hackathon_name"],
                    "assigned": row["assigned"],
                    "completed": row["completed"],
                    "pending": row["pending"],
                    "average_hours": _mean(row["hours"], 2),
                }
            )
        by_hackathon.sort(key=lambda row: row["hackathon_name"].lower())
        average_score = _mean(bucket["scores"])
        return {
            "evaluator_id": bucket["evaluator_id"],
            "name": bucket["name"],
            "email": bucket["email"],
            "assigned": bucket["assigned"],
            "completed": bucket["completed"],
            "pending": bucket["pending"],
            "completion_rate_percent": _percent(bucket["completed"], bucket["assigned"]),
            "same_day_completion_rate_percent": _percent(bucket["same_day"], bucket["assigned"]),
            "approved": bucket["approved"],
            "pending_review": bucket["pending_review"],
            "changes_requested": bucket["changes_requested"],
            "rework_rate_percent": _percent(bucket["changes_requested"], bucket["completed"]),
            "average_hours": _mean(bucket["hours"], 2),
            "median_hours": _median(bucket["hours"]),
            "fastest_hours": round(min(bucket["hours"]), 2) if bucket["hours"] else None,
            "slowest_hours": round(max(bucket["hours"]), 2) if bucket["hours"] else None,
            "average_score": average_score,
            "average_rating_10": _rating(average_score) if average_score is not None else None,
            "daily": [
                {
                    "date": day,
                    "assigned": bucket["daily_assigned"][day],
                    "completed": bucket["daily_completed"][day],
                }
                for day in days
            ],
            "by_hackathon": by_hackathon,
            "_hours": bucket["hours"],
            "_scores": bucket["scores"],
            "_same_day": bucket["same_day"],
            "_undated_assigned": bucket["undated_assigned"],
        }

    def _rollup_evaluators(
        self,
        evaluators: list[dict[str, Any]],
        from_date: date | None,
        to_date: date | None,
    ) -> dict[str, Any]:
        daily_assigned: dict[str, int] = defaultdict(int)
        daily_completed: dict[str, int] = defaultdict(int)
        hours: list[float] = []
        scores: list[float] = []
        assigned = completed = pending = same_day = undated = 0
        public = []
        for row in evaluators:
            assigned += row["assigned"]
            completed += row["completed"]
            pending += row["pending"]
            same_day += row.pop("_same_day")
            undated += row.pop("_undated_assigned")
            hours.extend(row.pop("_hours"))
            scores.extend(row.pop("_scores"))
            for point in row["daily"]:
                daily_assigned[point["date"]] += point["assigned"]
                daily_completed[point["date"]] += point["completed"]
            public.append(row)
        days = sorted(set(daily_assigned) | set(daily_completed))
        average_score = _mean(scores)
        return {
            "from_date": from_date,
            "to_date": to_date,
            "assigned": assigned,
            "completed": completed,
            "pending": pending,
            "completion_rate_percent": _percent(completed, assigned),
            "same_day_completion_rate_percent": _percent(same_day, assigned),
            "average_hours": _mean(hours, 2),
            "median_hours": _median(hours),
            "average_score": average_score,
            "average_rating_10": _rating(average_score) if average_score is not None else None,
            "undated_assigned": undated,
            "daily": [
                {
                    "date": day,
                    "assigned": daily_assigned[day],
                    "completed": daily_completed[day],
                }
                for day in days
            ],
            "evaluators": public,
        }


def evaluator_id_requested(row: dict[str, Any], requested: list[str]) -> bool:
    return row["evaluator_id"] in requested


def _scope(
    hackathon: dict[str, Any],
    universities: list[dict[str, Any]],
    batches: list[dict[str, Any]],
    rule: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if rule["all_universities"]:
        scoped_unis = list(universities)
    else:
        allowed = set(rule["university_ids"])
        scoped_unis = [row for row in universities if row.get("id") in allowed]
    uni_ids = {row.get("id") for row in scoped_unis}
    if rule["all_batches"]:
        scoped_batches = [row for row in batches if row.get("university_id") in uni_ids]
    else:
        allowed_batches = set(rule["batch_ids"])
        scoped_batches = [row for row in batches if row.get("id") in allowed_batches]
        if rule["all_universities"]:
            owner_ids = {row.get("university_id") for row in scoped_batches}
            scoped_unis = [row for row in universities if row.get("id") in owner_ids]
        else:
            scoped_batches = [row for row in scoped_batches if row.get("university_id") in uni_ids]
    return scoped_unis, scoped_batches


def _series_point(item: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
    return {
        "hackathon_id": item["hackathon_id"],
        "hackathon_name": item["name"],
        "registered_students": row["registered_students"],
        "enrolled_students": row["enrolled_students"],
        "participating_students": row["participating_students"],
        "submission_count": row["submission_count"],
        "participation_rate_percent": row["participation_rate_percent"],
    }
