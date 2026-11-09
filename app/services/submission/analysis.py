"""AI analysis queueing, Gemini evaluation, and report publish helpers."""

from __future__ import annotations
from app.utils.time import now_ist_iso

import json
import logging
import re
from typing import Any

from google import genai

from app.models.user_model import CurrentUser
from app.services.gemini_context import (
    drop_instruction_cache,
    lookup_instruction_cache,
    prepare_field_prompt,
)
from app.services.gemini_media import (
    build_media_key,
    build_video_part,
    media_settings,
    run_cached_video,
)
from app.services.evaluation_orchestration import build_evaluation_key, is_retryable_model_error
from app.services.gemini_budget import assert_gemini_enabled, note_submission_evaluation
from app.services.gemini_output import field_score_task, generate_content
from app.services.scorecard import apply_ai_field_scores, build_scorecard_skeleton
from app.services.submission.create import demo_video_required
from app.services.submission.prompts import (
    VIDEO_OUTPUT_CONTRACT,
    VIDEO_SCORE_PROMPT,
    scoring_prompt_has_problem_context,
    scoring_prompt_has_theme_context,
)
from app.utils.ids import new_id


logger = logging.getLogger(__name__)


def _submission_leaked_into_rules(stable: str, answer: str) -> bool:
    """True when this student's answer was copied into the shared rule text."""
    needle = (answer or "").strip()
    if len(needle) < 40:
        return False
    return needle in (stable or "")


def _retryable_model_error(exc: Exception) -> bool:
    """Quota and transient Gemini errors. Kept for existing callers."""
    return is_retryable_model_error(exc)


class AnalysisMixin:
    def mark_queued_for_evaluation(
        self,
        submission_id: str,
        evaluation_criteria: str | None = None,
        analyzed_by: str | None = None,
    ) -> str:
        """
        Create an analysis document and link it to the submission.

        Uses a Firestore transaction so two concurrent evaluate calls cannot
        both move the same submission into ``processing`` / overwrite analysis_id.
        """
        analysis_id = new_id()
        now = now_ist_iso()
        criteria = evaluation_criteria.strip() if evaluation_criteria else None

        def _txn(transaction):
            submission = self.firebase.txn_get(transaction, self.collection, submission_id)
            if not submission:
                raise ValueError("Submission not found")
            if submission.get("status") == "processing":
                raise ValueError("This submission is already being analyzed")

            analysis_doc = {
                "submission_id": submission_id,
                "student_id": submission["student_id"],
                "status": "processing",
                "evaluation_criteria": criteria,
                "checklist": None,
                "report": None,
                "field_scores": None,
                "scorecard": None,
                "analyzed_at": None,
                "error": None,
                "created_at": now,
                "updated_at": now,
            }
            self.firebase.txn_set(transaction, self.analysis_collection, analysis_id, analysis_doc)

            submission_update: dict[str, Any] = {
                "analysis_id": analysis_id,
                "status": "processing",
                "error": None,
                "analyzed_by": analyzed_by,
                "report_published": False,
                "published_at": None,
                "published_by": None,
                "review_status": "none",
                "final_score": None,
                "scorecard": None,
                "manual_scores": None,
                "evaluator_notes": None,
                "submitted_for_review_at": None,
                "submitted_for_review_by": None,
                "reviewed_at": None,
                "reviewed_by": None,
                "review_notes": None,
                "updated_at": now,
            }
            if evaluation_criteria is not None:
                submission_update["evaluation_criteria"] = criteria

            self.firebase.txn_update(transaction, self.collection, submission_id, submission_update)
            return analysis_id

        return self.firebase.run_transaction(_txn)

    def evaluate_submission(
        self,
        submission_id: str,
        evaluation_criteria: str | None = None,
    ) -> None:
        """Run Gemini analysis for a submission (background task)."""
        analysis_id: str | None = None
        try:
            submission = self.firebase.get_document(self.collection, submission_id)
            if not submission:
                logger.error("Submission not found: %s", submission_id)
                return

            analysis_id = submission.get("analysis_id")
            if not analysis_id:
                raise ValueError("No analysis document linked to this submission")

            problem = (submission.get("problem_statement") or "").strip()
            solution = (submission.get("solution_description") or "").strip()
            video_uri = (submission.get("video_path") or "").strip() or None
            content_type = submission.get("content_type") or "video/mp4"

            if not problem or not solution:
                raise ValueError("Submission is missing problem statement or solution description")

            hackathon = None
            hackathon_id = (submission.get("hackathon_id") or "").strip()
            if hackathon_id:
                hackathon = self.hackathon_service.get_hackathon(hackathon_id)

            video_required = demo_video_required(hackathon) if hackathon else bool(video_uri)
            if video_required and not video_uri:
                raise ValueError("Submission is missing video_path (GCS URI)")

            assert_gemini_enabled()
            note_submission_evaluation(submission_id)
            client = self._build_genai_client()
            extra_criteria = evaluation_criteria or submission.get("evaluation_criteria")
            _scoring_config, metric_defs = self._load_scoring_config(hackathon)
            field_scores = self._score_ai_metrics(
                client, submission, metric_defs, problem=problem, solution=solution
            )

            video_report = ""
            if video_uri:
                video_metric = self._find_video_metric(metric_defs)
                video_max = float(video_metric.get("max_score") or 20) if video_metric else None
                checklist, video_result = self._watch_demo_video(
                    client,
                    submission=submission,
                    submission_id=submission_id,
                    collection=self.collection,
                    problem=problem,
                    solution=solution,
                    video_uri=video_uri,
                    content_type=content_type,
                    extra_criteria=extra_criteria,
                    hackathon_id=hackathon_id or None,
                    max_score=video_max,
                )
                video_report, embedded_score = self._video_output(video_result)
                if video_metric and embedded_score is not None:
                    field_scores.append(
                        self._field_score_from_embedded(video_metric, embedded_score)
                    )
                elif video_metric:
                    field_scores.append(
                        self._score_video_metric(
                            client,
                            video_metric,
                            video_report,
                            hackathon_id=hackathon_id or None,
                        )
                    )
            else:
                logger.info(
                    "No video on submission %s — scoring text fields only",
                    submission_id,
                )
                checklist = self._checklist_for_submission(
                    client,
                    problem,
                    solution,
                    extra_criteria,
                    hackathon_id=hackathon_id or None,
                )
                video_metric = self._find_video_metric(metric_defs)
                if video_metric:
                    field_scores.append(
                        {
                            "field_key": video_metric["field_key"],
                            "field_label": video_metric.get("field_label") or "Video Explanation",
                            "score": 0,
                            "max_score": float(video_metric.get("max_score") or 20),
                            "weight": video_metric.get("weight"),
                            "rationale": "No demo video was submitted.",
                            "skipped": True,
                        }
                    )

            field_scores_section = self._format_field_scores_markdown(field_scores)
            scorecard = None
            if metric_defs:
                scorecard = apply_ai_field_scores(
                    build_scorecard_skeleton(metric_defs),
                    field_scores,
                )

            if video_report:
                report = video_report
                if field_scores_section:
                    report = f"{video_report.rstrip()}\n\n{field_scores_section}"
            else:
                report_parts = [
                    "## Text-only evaluation",
                    (
                        "This hackathon does not require a working demo video "
                        "(or none was uploaded). Field scores below are based on "
                        "the problem statement, solution description, and other "
                        "submitted answers."
                    ),
                    "",
                    "## Generated checklist (from problem & solution)",
                    checklist,
                ]
                if field_scores_section:
                    report_parts.extend(["", field_scores_section])
                report = "\n".join(report_parts)

            analyzed_at = now_ist_iso()
            self._commit_analysis_and_submission(
                analysis_id,
                submission_id,
                analysis_data={
                    "status": "completed",
                    "checklist": checklist,
                    "report": report,
                    "field_scores": field_scores or None,
                    "scorecard": scorecard,
                    "analyzed_at": analyzed_at,
                    "error": None,
                },
                submission_data={
                    "status": "completed",
                    "error": None,
                    "scorecard": scorecard,
                    "evaluation_key": self.current_evaluation_key(
                        submission,
                        evaluation_criteria,
                        submission_id=submission_id,
                    ),
                    "evaluation_lease": None,
                },
            )
            logger.info("Analysis %s completed for submission %s", analysis_id, submission_id)

        except Exception as e:
            logger.error("Analysis failed for submission %s: %s", submission_id, str(e))
            self._commit_analysis_and_submission(
                analysis_id,
                submission_id,
                analysis_data={"status": "failed", "error": str(e)} if analysis_id else None,
                submission_data={
                    "status": "failed",
                    "error": str(e),
                    "evaluation_lease": None,
                },
            )

    def get_analysis(
        self,
        analysis_id: str,
        current_user: CurrentUser,
    ) -> dict[str, Any] | None:
        """Fetch an analysis document if the user may access its submission."""
        analysis = self.firebase.get_document(self.analysis_collection, analysis_id)
        if not analysis:
            return None

        submission = self.get_submission(analysis["submission_id"], current_user)
        if not submission:
            return None

        return {"id": analysis_id, **analysis}

    def get_analysis_for_submission(
        self,
        submission_id: str,
        current_user: CurrentUser,
    ) -> dict[str, Any] | None:
        """Fetch the linked analysis document for a submission."""
        submission = self.get_submission(submission_id, current_user)
        if not submission:
            return None

        analysis_id = submission.get("analysis_id")
        if analysis_id:
            analysis = self.firebase.get_document(self.analysis_collection, analysis_id)
            if analysis:
                return {"id": analysis_id, **analysis}

        # Legacy submissions may still have embedded analysis data.
        if submission.get("analysis"):
            legacy = submission["analysis"]
            return {
                "id": analysis_id or submission_id,
                "submission_id": submission_id,
                "student_id": submission["student_id"],
                "status": submission.get("status", "completed"),
                **legacy,
            }

        return None

    def publish_report(
        self,
        submission_id: str,
        publish: bool,
        admin_user_id: str,
    ) -> dict[str, Any]:
        """Publish or unpublish the analysis report for student viewing."""
        submission = self.firebase.get_document(self.collection, submission_id)
        if not submission:
            raise ValueError("Submission not found")

        if publish:
            if (submission.get("review_status") or "") != "approved":
                raise ValueError("Approve the evaluation before publishing the report to students")
            if submission.get("status") != "completed":
                raise ValueError("Report can only be published after analysis has completed")
            analysis_id = submission.get("analysis_id")
            if not analysis_id:
                raise ValueError("No analysis linked to this submission")
            analysis = self.firebase.get_document(self.analysis_collection, analysis_id)
            if not analysis or analysis.get("status") != "completed":
                raise ValueError("Analysis report is not ready to publish")

            self._update_submission(
                submission_id,
                {
                    "report_published": True,
                    "published_at": now_ist_iso(),
                    "published_by": admin_user_id,
                },
            )
        else:
            self._update_submission(
                submission_id,
                {
                    "report_published": False,
                    "published_at": None,
                    "published_by": None,
                },
            )

        updated = self.firebase.get_document(self.collection, submission_id)
        if not updated:
            raise ValueError("Submission not found")
        return {"id": submission_id, **updated}

    def student_can_view_report(self, submission: dict[str, Any]) -> bool:
        """Students may only see the report after an admin publishes it."""
        return bool(submission.get("report_published"))

    def _build_genai_client(self) -> genai.Client:
        if self._genai_client is not None:
            return self._genai_client
        if self.use_enterprise:
            client = genai.Client(
                enterprise=True,
                project=self.project,
                location=self.location,
            )
        else:
            client = genai.Client(
                vertexai=True,
                project=self.project,
                location=self.location,
            )
        # Cache on the process-scoped service instance (one client lifecycle).
        self._genai_client = client
        return client

    def current_evaluation_key(
        self,
        submission: dict[str, Any],
        evaluation_criteria: str | None = None,
        submission_id: str | None = None,
    ) -> str:
        """Key for this student's answers, video, rubric, and model settings."""
        hackathon = None
        hackathon_id = (submission.get("hackathon_id") or "").strip()
        if hackathon_id:
            hackathon = self.hackathon_service.get_hackathon(hackathon_id)
        requirement_id = self._resolve_evaluation_requirement_id(hackathon) or ""
        _scoring, metrics = self._load_scoring_config(hackathon)
        prompt_id = hackathon_id or None
        settings = media_settings()
        criteria = evaluation_criteria
        if criteria is None:
            criteria = submission.get("evaluation_criteria") or ""
        return build_evaluation_key(
            stage="submission_analysis",
            student_id=str(submission.get("student_id") or ""),
            submission_id=str(submission_id or submission.get("id") or ""),
            problem=(submission.get("problem_statement") or "").strip(),
            solution=(submission.get("solution_description") or "").strip(),
            field_answers=submission.get("field_answers")
            if isinstance(submission.get("field_answers"), dict)
            else {},
            theme_id=str(submission.get("theme_id") or ""),
            video_path=(submission.get("video_path") or "").strip(),
            content_type=str(submission.get("content_type") or ""),
            video_generation=str(
                submission.get("video_generation") or submission.get("video_md5") or ""
            ),
            model=self.model,
            media_resolution=settings.resolution,
            fps=settings.fps,
            criteria=str(criteria or "").strip(),
            requirement_id=requirement_id,
            metrics=[
                {
                    "field_key": metric.get("field_key"),
                    "scoring_mode": metric.get("scoring_mode") or "ai",
                    "max_score": metric.get("max_score"),
                    "weight": metric.get("weight"),
                    "scoring_prompt": metric.get("scoring_prompt") or "",
                }
                for metric in metrics
            ],
            checklist_template=self.evaluation_prompt_service.get_template(
                "checklist", hackathon_id=prompt_id
            ),
            video_template=self.evaluation_prompt_service.get_template(
                "analyze_video", hackathon_id=prompt_id
            ),
        )

    def _checklist_for_submission(
        self,
        client: genai.Client,
        problem: str,
        solution: str,
        extra_criteria: str | None,
        hackathon_id: str | None = None,
    ) -> str:
        checklist = self._generate_checklist(client, problem, solution, hackathon_id=hackathon_id)
        extra = (extra_criteria or "").strip()
        if extra:
            checklist = checklist + "\n\n--- ADDITIONAL EVALUATION FOCUS ---\n" + extra
        return checklist

    def _watch_demo_video(
        self,
        client: genai.Client,
        *,
        submission: dict[str, Any],
        submission_id: str,
        collection: str,
        problem: str,
        solution: str,
        video_uri: str,
        content_type: str,
        extra_criteria: str | None,
        hackathon_id: str | None,
        max_score: float | None,
    ) -> tuple[str, dict[str, Any]]:
        """Watch the stored object once for this file and rubric."""
        checklist_template = self.evaluation_prompt_service.get_template(
            "checklist", hackathon_id=hackathon_id
        )
        video_template = self.evaluation_prompt_service.get_template(
            "analyze_video", hackathon_id=hackathon_id
        )
        key = build_media_key(
            video_uri=video_uri,
            content_type=content_type,
            model=self.model,
            checklist_template=checklist_template,
            video_template=video_template,
            problem=problem,
            solution=solution,
            extra_criteria=(extra_criteria or "").strip(),
            max_score=max_score,
        )

        def produce() -> tuple[str, dict[str, Any]]:
            checklist = self._checklist_for_submission(
                client,
                problem,
                solution,
                extra_criteria,
                hackathon_id=hackathon_id,
            )
            logger.info("Analyzing video for submission %s", submission_id)
            result = self._analyze_video(
                client=client,
                video_uri=video_uri,
                content_type=content_type,
                context=checklist,
                hackathon_id=hackathon_id,
                max_score=max_score,
            )
            return checklist, result

        def save(payload: dict[str, Any]) -> None:
            self.firebase.update_document(
                collection,
                submission_id,
                {"media_checkpoint": payload},
            )

        pair, _reused = run_cached_video(
            stored=submission.get("media_checkpoint"),
            key=key,
            produce=produce,
            save=save,
        )
        return pair

    def _generate_checklist(
        self,
        client: genai.Client,
        problem_statement: str,
        solution_description: str,
        hackathon_id: str | None = None,
    ) -> str:
        template = self.evaluation_prompt_service.get_template(
            "checklist", hackathon_id=hackathon_id
        )
        prompt = template.format(
            problem_statement=problem_statement.strip(),
            solution_description=solution_description.strip(),
        )
        response = generate_content(
            client,
            model=self.model,
            contents=[prompt],
            task="checklist",
        )
        text = (response.text or "").strip()
        if not text:
            raise ValueError("Failed to generate validation checklist")
        return text

    def _analyze_video(
        self,
        client: genai.Client,
        video_uri: str,
        content_type: str,
        context: str,
        hackathon_id: str | None = None,
        max_score: float | None = None,
    ) -> dict[str, Any]:
        video_part = build_video_part(video_uri, content_type)
        template = self.evaluation_prompt_service.get_template(
            "analyze_video", hackathon_id=hackathon_id
        )
        prompt = template.format(context=context)
        score_cap = float(max_score) if max_score is not None else 0
        prompt = f"{prompt.rstrip()}\n{VIDEO_OUTPUT_CONTRACT.format(max_score=score_cap)}"

        response = generate_content(
            client,
            model=self.model,
            contents=[video_part, prompt],
            task="video",
            json_response=True,
        )
        raw = (response.text or "").strip()
        if not raw:
            raise ValueError("The analyzer returned an empty report")
        parsed = self._parse_video_analysis(raw, max_score)
        if parsed is None:
            return {"report": raw, "score": None, "rationale": None}
        return parsed

    def _load_scoring_config(
        self, hackathon: dict[str, Any] | None
    ) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        requirement_id = self._resolve_evaluation_requirement_id(hackathon)
        if not requirement_id:
            return None, []
        scoring = self.metric_scoring_service.get_scoring_for_requirement(requirement_id)
        if not scoring:
            logger.info(
                "No metric scoring config for requirement %s — skipping field scores",
                requirement_id,
            )
            return None, []
        return scoring, list(scoring.get("metrics") or [])

    @staticmethod
    def _find_video_metric(metrics: list[dict[str, Any]]) -> dict[str, Any] | None:
        for metric in metrics:
            if metric.get("scoring_mode", "ai") != "ai":
                continue
            key = (metric.get("field_key") or "").lower()
            if key in ("video_explanation", "video"):
                return metric
        return None

    def _score_ai_metrics(
        self,
        client: genai.Client,
        submission: dict[str, Any],
        metrics: list[dict[str, Any]],
        *,
        problem: str,
        solution: str,
    ) -> list[dict[str, Any]]:
        """Score AI text metrics (skips manual + video — video scored separately)."""
        context = self._build_scoring_prompt_context(submission, problem=problem, solution=solution)
        results: list[dict[str, Any]] = []
        for metric in metrics:
            if (metric.get("scoring_mode") or "ai") != "ai":
                continue
            field_key = metric.get("field_key") or ""
            if field_key.lower() in ("video_explanation", "video"):
                continue

            answer = self._resolve_field_answer(submission, field_key)
            if not answer:
                results.append(
                    {
                        "field_key": field_key,
                        "field_label": metric.get("field_label") or field_key,
                        "score": 0,
                        "max_score": float(metric.get("max_score") or 10),
                        "weight": metric.get("weight"),
                        "rationale": "No student answer provided for this field.",
                        "skipped": True,
                    }
                )
                continue

            scored = self._score_single_field(client, metric, answer, context=context)
            results.append(scored)
        return results

    def _resolve_theme_for_scoring(self, submission: dict[str, Any]) -> tuple[str, str]:
        """Theme name + description from the submission snapshot, else live theme."""
        name = (submission.get("theme_name") or "").strip()
        description = (submission.get("theme_description") or "").strip()
        theme_id = (submission.get("theme_id") or "").strip()
        if (not name or not description) and theme_id:
            theme = self.theme_service.get_theme(theme_id)
            if isinstance(theme, dict):
                name = name or (theme.get("name") or "").strip()
                description = description or (theme.get("description") or "").strip()
        return name, description

    def _build_scoring_prompt_context(
        self,
        submission: dict[str, Any],
        *,
        problem: str,
        solution: str,
    ) -> dict[str, str]:
        """Values for ``{problem_statement}`` / ``{solution_description}`` / ``{theme}``."""
        theme_name, theme_description = self._resolve_theme_for_scoring(submission)
        theme_blob = theme_description or theme_name
        context: dict[str, str] = {
            "problem_statement": (problem or "").strip(),
            "solution_description": (solution or "").strip(),
            "theme": theme_blob,
            "theme_name": theme_name,
            "theme_description": theme_description or theme_blob,
        }
        answers = submission.get("field_answers") or {}
        if isinstance(answers, dict):
            for key, value in answers.items():
                if value is None:
                    continue
                text = str(value).strip()
                if text and key not in context:
                    context[str(key)] = text
        return context

    def _score_video_metric(
        self,
        client: genai.Client,
        metric: dict[str, Any],
        video_report: str,
        hackathon_id: str | None = None,
    ) -> dict[str, Any]:
        """Score a video report that did not already include a numeric score."""
        field_key = metric.get("field_key") or "video_explanation"
        field_label = metric.get("field_label") or "Video Explanation"
        max_score = float(metric.get("max_score") or 20)
        prompt = VIDEO_SCORE_PROMPT.format(
            max_score=max_score,
            video_report=video_report.strip()[:8000],
        )
        response = generate_content(
            client,
            model=self.model,
            contents=[prompt],
            task="summary",
            json_response=True,
        )
        raw = (response.text or "").strip()
        parsed = self._parse_field_score_json(raw, max_score)
        return {
            "field_key": field_key,
            "field_label": field_label,
            "score": parsed["score"],
            "max_score": max_score,
            "weight": metric.get("weight"),
            "rationale": parsed["rationale"],
            "skipped": False,
        }

    def _score_single_field(
        self,
        client: genai.Client,
        metric: dict[str, Any],
        student_answer: str,
        context: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        field_key = metric.get("field_key") or ""
        field_label = metric.get("field_label") or field_key
        max_score = float(metric.get("max_score") or 10)
        raw_scoring_prompt = (metric.get("scoring_prompt") or "").strip()
        if not raw_scoring_prompt:
            return {
                "field_key": field_key,
                "field_label": field_label,
                "score": 0,
                "max_score": max_score,
                "weight": metric.get("weight"),
                "rationale": "Scoring prompt is empty for this field.",
                "skipped": True,
            }

        values = context or {}
        answer = student_answer.strip()
        field_key_lower = field_key.lower()
        # Solution metrics without an explicit problem placeholder still get
        # the student's problem statement so the model can judge fit.
        include_problem = field_key_lower in (
            "solution_description",
            "solution",
        ) and not scoring_prompt_has_problem_context(raw_scoring_prompt)
        # Problem Statement and Solution Description always receive the
        # selected theme unless the admin already inserted {theme}.
        include_theme = field_key_lower in (
            "problem_statement",
            "problem",
            "solution_description",
            "solution",
        ) and not scoring_prompt_has_theme_context(raw_scoring_prompt)
        prepared = prepare_field_prompt(
            field_label=field_label,
            max_score=max_score,
            scoring_rules=raw_scoring_prompt,
            student_answer=answer,
            context=values,
            include_problem=include_problem,
            include_theme=include_theme,
        )
        cache_name = None
        if not _submission_leaked_into_rules(prepared.stable, answer):
            cache_name = lookup_instruction_cache(
                client, model=self.model, stable_text=prepared.stable
            )
        task = field_score_task(field_key)
        logger.info(
            "Gemini input task=%s stable_chars=%s dynamic_chars=%s "
            "stable_tokens_est=%s cache=%s",
            task,
            len(prepared.stable),
            len(prepared.dynamic),
            prepared.estimated_stable_tokens,
            "used" if cache_name else "none",
        )
        contents = [prepared.dynamic if cache_name else prepared.full_text]
        try:
            response = generate_content(
                client,
                model=self.model,
                contents=contents,
                task=task,
                json_response=True,
                cached_content=cache_name,
            )
        except ValueError:
            raise
        except Exception:
            if not cache_name:
                raise
            logger.warning(
                "Gemini cached request failed task=%s; sending the full prompt",
                task,
                exc_info=True,
            )
            drop_instruction_cache(model=self.model, stable_text=prepared.stable, client=client)
            response = generate_content(
                client,
                model=self.model,
                contents=[prepared.full_text],
                task=task,
                json_response=True,
            )
        raw = (response.text or "").strip()
        parsed = self._parse_field_score_json(raw, max_score)
        return {
            "field_key": field_key,
            "field_label": field_label,
            "score": parsed["score"],
            "max_score": max_score,
            "weight": metric.get("weight"),
            "rationale": parsed["rationale"],
            "skipped": False,
        }

    @staticmethod
    def _parse_field_score_json(raw: str, max_score: float) -> dict[str, Any]:
        data = AnalysisMixin._load_json_object(raw)
        if "score" not in data:
            raise ValueError("Score JSON did not include a score")
        try:
            score = float(data.get("score"))
        except (TypeError, ValueError) as exc:
            raise ValueError("Score JSON did not include a numeric score") from exc
        score = max(0.0, min(score, max_score))
        rationale = str(data.get("rationale") or "").strip() or "No rationale provided."
        return {"score": score, "rationale": rationale}

    @staticmethod
    def _load_json_object(raw: str) -> dict[str, Any]:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", raw, re.DOTALL)
            if not match:
                raise ValueError("Model returned incomplete score JSON") from None
            try:
                data = json.loads(match.group(0))
            except json.JSONDecodeError as exc:
                raise ValueError("Model returned incomplete score JSON") from exc
        if not isinstance(data, dict):
            raise ValueError("Model returned incomplete score JSON")
        return data

    @staticmethod
    def _video_output(result: Any) -> tuple[str, dict[str, Any] | None]:
        """Report text, plus a score dict when the video call already scored it."""
        if isinstance(result, str):
            return result, None
        if not isinstance(result, dict):
            return "", None
        report = str(result.get("report") or "")
        if result.get("score") is None:
            return report, None
        return report, result

    @staticmethod
    def _field_score_from_embedded(
        metric: dict[str, Any],
        embedded: dict[str, Any],
    ) -> dict[str, Any]:
        max_score = float(metric.get("max_score") or 20)
        score = max(0.0, min(float(embedded["score"]), max_score))
        rationale = str(embedded.get("rationale") or "").strip() or "No rationale provided."
        return {
            "field_key": metric.get("field_key") or "video_explanation",
            "field_label": metric.get("field_label") or "Video Explanation",
            "score": score,
            "max_score": max_score,
            "weight": metric.get("weight"),
            "rationale": rationale,
            "skipped": False,
        }

    @staticmethod
    def _parse_video_analysis(raw: str, max_score: float | None) -> dict[str, Any] | None:
        """Turn compact video JSON into the markdown report the app already stores.

        Returns None when the payload is not this schema, so a legacy prose
        reply can still be scored by the follow-up call. Incomplete JSON raises
        instead of inventing a score.
        """
        try:
            data = AnalysisMixin._load_json_object(raw)
        except ValueError:
            return None
        if not any(key in data for key in ("summary", "coverage", "score")):
            return None
        summary = str(data.get("summary") or "").strip()
        rationale = str(data.get("rationale") or "").strip()
        coverage = data.get("coverage") if isinstance(data.get("coverage"), list) else []
        issues = data.get("issues") if isinstance(data.get("issues"), list) else []
        score: float | None = None
        if data.get("score") is not None:
            try:
                score = float(data.get("score"))
            except (TypeError, ValueError) as exc:
                raise ValueError("Video score JSON did not include a numeric score") from exc
            if max_score is not None:
                score = max(0.0, min(score, float(max_score)))
        report = AnalysisMixin._video_report_markdown(
            summary=summary,
            coverage=coverage,
            issues=issues,
            rationale=rationale,
            score=score,
            max_score=max_score,
        )
        if not report.strip():
            return None
        return {"report": report, "score": score, "rationale": rationale or None}

    @staticmethod
    def _video_report_markdown(
        *,
        summary: str,
        coverage: list[Any],
        issues: list[Any],
        rationale: str,
        score: float | None,
        max_score: float | None,
    ) -> str:
        lines = ["## Video Summary", summary or "No summary returned.", ""]
        lines.extend(["## Comparison Against Context", ""])
        marks = {"covered": "✅", "partial": "⚠️", "missing": "❌"}
        wrote_coverage = False
        for item in coverage:
            if not isinstance(item, dict):
                continue
            point = str(item.get("point") or "").strip()
            if not point:
                continue
            status = str(item.get("status") or "").strip().lower()
            mark = marks.get(status, "⚠️")
            note = str(item.get("note") or "").strip()
            seen_at = str(item.get("at") or "").strip()
            line = f"- {mark} {point}"
            if seen_at:
                line = f"{line} ({seen_at})"
            if note:
                line = f"{line} — {note}"
            lines.append(line)
            wrote_coverage = True
        if not wrote_coverage:
            lines.append("- No checklist comparison was returned.")
        lines.extend(["", "## Discrepancies & Issues", ""])
        wrote_issue = False
        for issue in issues:
            text = str(issue).strip()
            if text:
                lines.append(f"- {text}")
                wrote_issue = True
        if not wrote_issue:
            lines.append("- None noted.")
        lines.extend(["", "## Overall Assessment", rationale or "No rationale returned."])
        if score is not None:
            cap = f" / {max_score:g}" if max_score is not None else ""
            lines.append(f"Score: {score:g}{cap}")
        return "\n".join(lines).strip()

    @staticmethod
    def _format_field_scores_markdown(field_scores: list[dict[str, Any]]) -> str:
        if not field_scores:
            return ""
        lines = ["## Requirement field scores", ""]
        for item in field_scores:
            label = item.get("field_label") or item.get("field_key")
            score = item.get("score", 0)
            max_score = item.get("max_score", 10)
            rationale = item.get("rationale") or ""
            lines.append(f"### {label}")
            lines.append(f"- **Score:** {score} / {max_score}")
            lines.append(f"- **Rationale:** {rationale}")
            lines.append("")
        return "\n".join(lines).rstrip()

    @staticmethod
    def _resolve_evaluation_requirement_id(hackathon: dict[str, Any] | None) -> str | None:
        if not hackathon:
            return None
        for round_ in hackathon.get("timeline") or []:
            if isinstance(round_, dict):
                req_id = (round_.get("evaluation_requirement_id") or "").strip()
            else:
                req_id = (getattr(round_, "evaluation_requirement_id", None) or "").strip()
            if req_id:
                return req_id
        return None

    @staticmethod
    def _resolve_field_answer(submission: dict[str, Any], field_key: str) -> str:
        """Map requirement field keys onto submission answers."""
        key = (field_key or "").strip().lower()
        answers = submission.get("field_answers") or {}
        if isinstance(answers, dict):
            for candidate in (field_key, key):
                value = answers.get(candidate)
                if value and str(value).strip():
                    return str(value).strip()

        aliases = {
            "problem_statement": ["problem_statement", "problem"],
            "solution_description": ["solution_description", "solution"],
            "mvp_link": ["mvp_link", "mvp", "mvp_url"],
            "github_link": ["github_link", "project_github_link", "github", "repo_link"],
            "project_github_link": ["project_github_link", "github_link", "github"],
        }
        for canonical, keys in aliases.items():
            if key in keys or key == canonical:
                for attr in keys + [canonical]:
                    top = submission.get(attr)
                    if top and str(top).strip():
                        return str(top).strip()
                    nested = answers.get(attr) if isinstance(answers, dict) else None
                    if nested and str(nested).strip():
                        return str(nested).strip()

        top_level = submission.get(field_key) or submission.get(key)
        if top_level and str(top_level).strip():
            return str(top_level).strip()
        return ""

    def _commit_analysis_and_submission(
        self,
        analysis_id: str | None,
        submission_id: str,
        analysis_data: dict[str, Any] | None,
        submission_data: dict[str, Any],
    ) -> None:
        """Atomically update analysis + submission (or submission alone on early fail)."""
        now = now_ist_iso()
        operations: list[dict[str, Any]] = []
        if analysis_id and analysis_data is not None:
            analysis_payload = {**analysis_data, "updated_at": now}
            operations.append(
                {
                    "type": "update",
                    "collection": self.analysis_collection,
                    "document_id": analysis_id,
                    "data": analysis_payload,
                }
            )
        submission_payload = {**submission_data, "updated_at": now}
        operations.append(
            {
                "type": "update",
                "collection": self.collection,
                "document_id": submission_id,
                "data": submission_payload,
            }
        )
        self.firebase.batch_write(operations)
