"""Request and response shapes for the Analysis screen."""

from __future__ import annotations

from datetime import date
from typing import Optional

from pydantic import BaseModel, Field


TOP_N_CHOICES = (3, 5, 10)
MAX_HACKATHONS = 12
SCORE_MAX = 100
RATING_MAX = 10


class HackathonAnalysisRequest(BaseModel):
    """Hackathons to compare, and how many top scores form the project rating."""

    hackathon_ids: list[str] = Field(..., min_length=1, max_length=MAX_HACKATHONS)
    top_n: int = Field(
        5,
        ge=1,
        le=20,
        description="Top 3, top 5, or top 10. Ties at the last included rank are kept.",
    )


class EvaluatorAnalysisRequest(BaseModel):
    """Evaluator speed and daily workload. Empty id lists mean everyone in scope."""

    hackathon_ids: list[str] = Field(default_factory=list, max_length=MAX_HACKATHONS)
    evaluator_ids: list[str] = Field(default_factory=list, max_length=50)
    from_date: Optional[date] = Field(
        None,
        description="IST date, inclusive. Omit to start at the first assignment.",
    )
    to_date: Optional[date] = Field(
        None,
        description="IST date, inclusive. Omit to run through the latest completion.",
    )


class AnalysisHackathonOption(BaseModel):
    id: str
    name: str
    start_date: str = ""
    end_date: str = ""


class AnalysisEvaluatorOption(BaseModel):
    id: str
    name: str
    email: str = ""


class AnalysisOptionsResponse(BaseModel):
    """Filters for the Analysis screen."""

    hackathons: list[AnalysisHackathonOption]
    evaluators: list[AnalysisEvaluatorOption]
    top_n_choices: list[int] = Field(default_factory=lambda: list(TOP_N_CHOICES))
    max_hackathons: int = MAX_HACKATHONS
    score_max: int = SCORE_MAX
    rating_max: int = RATING_MAX


class HackathonComparisonPoint(BaseModel):
    """One group on the hackathon bar chart."""

    hackathon_id: str
    name: str
    start_date: str = ""
    end_date: str = ""
    portal_registered_students: int = 0
    eligible_students: int = 0
    enrolled_students: int = 0
    participating_students: int = 0
    submission_count: int = 0
    niat_campus_count: int = 0
    niat_headcount: Optional[int] = None
    niat_participating: int = 0
    non_niat_participating: int = 0
    winner_count: int = 0
    average_score: Optional[float] = None
    average_rating_10: Optional[float] = None


class CampusSeriesPoint(BaseModel):
    hackathon_id: str
    hackathon_name: str
    registered_students: int = 0
    enrolled_students: int = 0
    participating_students: int = 0
    submission_count: int = 0
    participation_rate_percent: Optional[float] = None


class UniversityComparisonRow(BaseModel):
    university_id: str
    university_name: str
    college_type: str
    headcount: Optional[int] = None
    series: list[CampusSeriesPoint] = Field(default_factory=list)


class BatchComparisonRow(BaseModel):
    batch_id: str
    batch_name: str
    university_id: str
    university_name: str
    college_type: str
    strength: Optional[int] = None
    series: list[CampusSeriesPoint] = Field(default_factory=list)


class CampusCountBlock(BaseModel):
    campus_count: int = 0
    headcount: Optional[int] = None
    campuses_missing_headcount: int = 0
    registered_students: int = 0
    enrolled_students: int = 0
    participating_students: int = 0
    submission_count: int = 0


class CampusParticipationRow(BaseModel):
    university_id: str
    university_name: str
    college_type: str
    headcount: Optional[int] = None
    registered_students: int = 0
    enrolled_students: int = 0
    participating_students: int = 0
    submission_count: int = 0
    participation_rate_percent: Optional[float] = None


class BatchParticipationRow(BaseModel):
    batch_id: str
    batch_name: str
    university_id: str
    university_name: str
    college_type: str
    strength: Optional[int] = None
    registered_students: int = 0
    enrolled_students: int = 0
    participating_students: int = 0
    submission_count: int = 0
    participation_rate_percent: Optional[float] = None


class WinnerRow(BaseModel):
    rank: int
    rank_label: str
    score: float
    rating_10: float
    submission_id: str
    student_id: str
    student_name: str
    team_name: str = ""
    university_name: str = ""
    batch_name: str = ""
    college_type: str = ""


class ScoreBand(BaseModel):
    label: str
    count: int = 0


class ProjectQuality(BaseModel):
    """Top-N quality on the latest round that has an approved score."""

    round_index: Optional[int] = None
    round_title: str = ""
    requested_top_n: int
    approved_count: int = 0
    winner_count: int = 0
    tie_at_cutoff: bool = False
    average_score: Optional[float] = None
    average_rating_10: Optional[float] = None
    field_average_score: Optional[float] = None
    field_average_rating_10: Optional[float] = None
    announced_top_n: Optional[int] = None
    announced_count: Optional[int] = None
    winners: list[WinnerRow] = Field(default_factory=list)
    score_bands: list[ScoreBand] = Field(default_factory=list)


class HackathonAnalysisItem(BaseModel):
    hackathon_id: str
    name: str
    start_date: str = ""
    end_date: str = ""
    all_universities: bool = True
    all_batches: bool = True
    portal_registered_students: int = 0
    eligible_students: int = 0
    enrolled_students: int = 0
    participating_students: int = 0
    submission_count: int = 0
    students_without_campus: int = 0
    assigned_count: int = 0
    review_submitted_count: int = 0
    approved_count: int = 0
    pending_review_count: int = 0
    changes_requested_count: int = 0
    assignment_coverage_percent: Optional[float] = None
    approval_rate_percent: Optional[float] = None
    niat: CampusCountBlock
    non_niat: CampusCountBlock
    project_quality: ProjectQuality
    universities: list[CampusParticipationRow] = Field(default_factory=list)
    batches: list[BatchParticipationRow] = Field(default_factory=list)


class HackathonAnalysisResponse(BaseModel):
    top_n: int
    score_max: int = SCORE_MAX
    rating_max: int = RATING_MAX
    portal_registered_students: int = 0
    selected_count: int = 0
    unique_participating_students: int = 0
    total_submissions: int = 0
    sum_enrolled_students: int = Field(
        0,
        description="Enrollments added across the selection. One student in two hackathons counts twice.",
    )
    average_project_rating_10: Optional[float] = None
    average_winner_score: Optional[float] = None
    comparison: list[HackathonComparisonPoint] = Field(default_factory=list)
    university_comparison: list[UniversityComparisonRow] = Field(default_factory=list)
    batch_comparison: list[BatchComparisonRow] = Field(default_factory=list)
    hackathons: list[HackathonAnalysisItem] = Field(default_factory=list)


class DailyWorkload(BaseModel):
    date: str
    assigned: int = 0
    completed: int = 0


class EvaluatorHackathonWorkload(BaseModel):
    hackathon_id: str
    hackathon_name: str
    assigned: int = 0
    completed: int = 0
    pending: int = 0
    average_hours: Optional[float] = None


class EvaluatorPerformance(BaseModel):
    evaluator_id: str
    name: str
    email: str = ""
    assigned: int = 0
    completed: int = 0
    pending: int = 0
    completion_rate_percent: Optional[float] = None
    same_day_completion_rate_percent: Optional[float] = None
    approved: int = 0
    pending_review: int = 0
    changes_requested: int = 0
    rework_rate_percent: Optional[float] = None
    average_hours: Optional[float] = None
    median_hours: Optional[float] = None
    fastest_hours: Optional[float] = None
    slowest_hours: Optional[float] = None
    average_score: Optional[float] = None
    average_rating_10: Optional[float] = None
    daily: list[DailyWorkload] = Field(default_factory=list)
    by_hackathon: list[EvaluatorHackathonWorkload] = Field(default_factory=list)


class EvaluatorAnalysisResponse(BaseModel):
    from_date: Optional[date] = None
    to_date: Optional[date] = None
    assigned: int = 0
    completed: int = 0
    pending: int = 0
    completion_rate_percent: Optional[float] = None
    same_day_completion_rate_percent: Optional[float] = None
    average_hours: Optional[float] = None
    median_hours: Optional[float] = None
    average_score: Optional[float] = None
    average_rating_10: Optional[float] = None
    undated_assigned: int = 0
    daily: list[DailyWorkload] = Field(default_factory=list)
    evaluators: list[EvaluatorPerformance] = Field(default_factory=list)
