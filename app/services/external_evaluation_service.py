"""Run the existing Gemini helpers against an external submission only."""

from __future__ import annotations

import logging
import os
from typing import Any

from app.exceptions import NotFoundError
from app.services.external_hackathon_service import (
    EXTERNAL_HACKATHONS,
    EXTERNAL_SUBMISSIONS,
)
from app.services.firebase import FirebaseService
from app.services.scorecard import apply_ai_field_scores, build_scorecard_skeleton
from app.services.submission.analysis import _retryable_model_error
from app.services.submission_service import SubmissionService
from app.utils.time import now_ist_iso


logger = logging.getLogger(__name__)

REGULAR_JOB_PATH = "/internal/jobs/evaluate-submission"
EXTERNAL_JOB_PATH = "/internal/jobs/evaluate-external-submission"


def external_job_url(worker_url: str) -> str:
    """Same worker host, separate path, so regular jobs keep their URL."""
    if worker_url.endswith(REGULAR_JOB_PATH):
        return worker_url[: -len(REGULAR_JOB_PATH)] + EXTERNAL_JOB_PATH
    marker = "/internal/jobs/"
    if marker in worker_url:
        return worker_url.split(marker, 1)[0] + EXTERNAL_JOB_PATH
    return worker_url.rstrip("/") + EXTERNAL_JOB_PATH


class ExternalEvaluationService:
    """Adapter. Writes scores onto ``external_submissions`` and nowhere else."""

    def __init__(
        self,
        firebase: FirebaseService | None = None,
        submission_service: SubmissionService | None = None,
    ) -> None:
        self.firebase = firebase or FirebaseService()
        self.analysis = submission_service or SubmissionService(firebase=self.firebase)

    def evaluate(self, external_submission_id: str) -> dict[str, str]:
        submission = self.firebase.get_document(EXTERNAL_SUBMISSIONS, external_submission_id)
        if not submission:
            raise NotFoundError("External submission not found")

        hackathon_id = (submission.get("external_hackathon_id") or "").strip()
        hackathon = self.firebase.get_document(EXTERNAL_HACKATHONS, hackathon_id) or {}
        try:
            self._run(external_submission_id, submission, hackathon)
        except Exception as exc:
            job_mode = os.getenv("EVALUATION_JOB_MODE", "auto").strip().lower()
            if _retryable_model_error(exc) and job_mode == "cloud_tasks":
                logger.warning(
                    "Retryable external analysis error for %s: %s",
                    external_submission_id,
                    exc,
                )
                raise
            logger.error(
                "External analysis failed for %s: %s",
                external_submission_id,
                exc,
            )
            self._write_status(
                external_submission_id,
                status="failed",
                error=str(exc),
                evaluation={"error": str(exc)},
            )
        return {"status": "processed", "external_submission_id": external_submission_id}

    def _run(
        self,
        submission_id: str,
        submission: dict[str, Any],
        hackathon: dict[str, Any],
    ) -> None:
        problem = (submission.get("problem_statement") or "").strip()
        solution = (submission.get("solution_description") or "").strip()
        video_uri = (submission.get("video_path") or "").strip()
        content_type = submission.get("content_type") or "video/mp4"
        if not problem or not solution:
            raise ValueError("Submission is missing problem statement or solution description")
        if not video_uri:
            raise ValueError("Submission is missing video_path (GCS URI)")

        client = self.analysis._build_genai_client()
        checklist = self.analysis._generate_checklist(
            client,
            problem,
            solution,
            hackathon_id=None,
        )
        rubric = self._rubric_hackathon(hackathon)
        _scoring, metric_defs = self.analysis._load_scoring_config(rubric)
        field_scores = self.analysis._score_ai_metrics(
            client,
            submission,
            metric_defs,
            problem=problem,
            solution=solution,
        )
        video_report = self.analysis._analyze_video(
            client=client,
            video_uri=video_uri,
            content_type=content_type,
            context=checklist,
            hackathon_id=None,
        )
        video_metric = self.analysis._find_video_metric(metric_defs)
        if video_metric:
            field_scores.append(
                self.analysis._score_video_metric(
                    client,
                    video_metric,
                    video_report,
                    hackathon_id=None,
                )
            )

        scorecard = None
        score = None
        if metric_defs:
            scorecard = apply_ai_field_scores(
                build_scorecard_skeleton(metric_defs),
                field_scores,
            )
            score = scorecard.get("ai_total")
        section = self.analysis._format_field_scores_markdown(field_scores)
        report = video_report
        if section:
            report = f"{video_report.rstrip()}\n\n{section}"

        analyzed_at = now_ist_iso()
        self._write_status(
            submission_id,
            status="completed",
            error=None,
            evaluation={
                "checklist": checklist,
                "report": report,
                "field_scores": field_scores or None,
                "scorecard": scorecard,
                "score": score,
                "analyzed_at": analyzed_at,
                "error": None,
            },
        )
        logger.info("External analysis completed for %s", submission_id)

    def _write_status(
        self,
        submission_id: str,
        *,
        status: str,
        error: str | None,
        evaluation: dict[str, Any],
    ) -> None:
        current = self.firebase.get_document(EXTERNAL_SUBMISSIONS, submission_id) or {}
        current["status"] = status
        current["error"] = error
        current["evaluation"] = evaluation
        current["updated_at"] = now_ist_iso()
        self.firebase.set_document(EXTERNAL_SUBMISSIONS, submission_id, current)

    @staticmethod
    def _rubric_hackathon(hackathon: dict[str, Any]) -> dict[str, Any] | None:
        """In-memory shape the existing scorer reads. It is never saved."""
        requirement_id = (hackathon.get("evaluation_requirement_id") or "").strip()
        if not requirement_id:
            return None
        return {"timeline": [{"evaluation_requirement_id": requirement_id}]}
