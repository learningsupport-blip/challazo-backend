"""Admin setup and public form API for external hackathons."""

from __future__ import annotations

import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status

from app.dependencies import (
    get_evaluation_job_service,
    get_external_evaluation_service,
    get_external_hackathon_service,
)
from app.exceptions import AppError
from app.middleware.auth_middleware import get_admin_user
from app.models.external_hackathon_model import (
    AssignExternalEvaluatorRequest,
    ExternalHackathonAdmin,
    ExternalHackathonCreate,
    ExternalHackathonPublic,
    ExternalHackathonUpdate,
    ExternalSubmissionCreate,
    ExternalSubmissionResponse,
    ExternalUploadRequest,
    ExternalUploadResponse,
)
from app.models.user_model import CurrentUser
from app.services.evaluation_job_service import EvaluationJobService
from app.services.external_evaluation_service import ExternalEvaluationService
from app.services.external_hackathon_service import ExternalHackathonService
from app.utils.async_io import run_sync


logger = logging.getLogger(__name__)
router = APIRouter(tags=["external-hackathons"])


def _http_from_value_error(exc: ValueError) -> HTTPException:
    from app.exceptions import status_code_for_value_error_message

    return HTTPException(
        status_code=status_code_for_value_error_message(str(exc)),
        detail=str(exc),
    )


@router.post(
    "/admin/external-hackathons",
    response_model=ExternalHackathonAdmin,
    status_code=201,
)
async def create_external_hackathon(
    request: ExternalHackathonCreate,
    admin: CurrentUser = Depends(get_admin_user),
    service: ExternalHackathonService = Depends(get_external_hackathon_service),
) -> ExternalHackathonAdmin:
    """Create a draft external hackathon. Existing hackathons are not touched."""
    try:
        created = await run_sync(service.create, request, created_by=admin.user_id)
    except AppError:
        raise
    except ValueError as exc:
        raise _http_from_value_error(exc) from exc
    return ExternalHackathonAdmin(**created)


@router.get("/admin/external-hackathons", response_model=list[ExternalHackathonAdmin])
async def list_external_hackathons(
    _admin: CurrentUser = Depends(get_admin_user),
    service: ExternalHackathonService = Depends(get_external_hackathon_service),
) -> list[ExternalHackathonAdmin]:
    rows = await run_sync(service.list_hackathons)
    return [ExternalHackathonAdmin(**row) for row in rows]


@router.get("/admin/external-hackathons/{hackathon_id}", response_model=ExternalHackathonAdmin)
async def get_external_hackathon(
    hackathon_id: str,
    _admin: CurrentUser = Depends(get_admin_user),
    service: ExternalHackathonService = Depends(get_external_hackathon_service),
) -> ExternalHackathonAdmin:
    try:
        row = await run_sync(service.get_admin, hackathon_id)
    except AppError:
        raise
    return ExternalHackathonAdmin(**row)


@router.put("/admin/external-hackathons/{hackathon_id}", response_model=ExternalHackathonAdmin)
async def update_external_hackathon(
    hackathon_id: str,
    request: ExternalHackathonUpdate,
    _admin: CurrentUser = Depends(get_admin_user),
    service: ExternalHackathonService = Depends(get_external_hackathon_service),
) -> ExternalHackathonAdmin:
    try:
        row = await run_sync(service.update, hackathon_id, request)
    except AppError:
        raise
    except ValueError as exc:
        raise _http_from_value_error(exc) from exc
    return ExternalHackathonAdmin(**row)


@router.post(
    "/admin/external-hackathons/{hackathon_id}/publish",
    response_model=ExternalHackathonAdmin,
)
async def publish_external_hackathon(
    hackathon_id: str,
    _admin: CurrentUser = Depends(get_admin_user),
    service: ExternalHackathonService = Depends(get_external_hackathon_service),
) -> ExternalHackathonAdmin:
    try:
        row = await run_sync(service.publish, hackathon_id)
    except AppError:
        raise
    return ExternalHackathonAdmin(**row)


@router.post(
    "/admin/external-hackathons/{hackathon_id}/close",
    response_model=ExternalHackathonAdmin,
)
async def close_external_hackathon(
    hackathon_id: str,
    _admin: CurrentUser = Depends(get_admin_user),
    service: ExternalHackathonService = Depends(get_external_hackathon_service),
) -> ExternalHackathonAdmin:
    try:
        row = await run_sync(service.close, hackathon_id)
    except AppError:
        raise
    return ExternalHackathonAdmin(**row)


@router.get(
    "/admin/external-hackathons/{hackathon_id}/submissions",
    response_model=list[ExternalSubmissionResponse],
)
async def list_external_submissions(
    hackathon_id: str,
    _admin: CurrentUser = Depends(get_admin_user),
    service: ExternalHackathonService = Depends(get_external_hackathon_service),
) -> list[ExternalSubmissionResponse]:
    try:
        rows = await run_sync(service.list_submissions, hackathon_id)
    except AppError:
        raise
    return [ExternalSubmissionResponse(**row) for row in rows]


@router.post(
    "/admin/external-submissions/{submission_id}/assign",
    response_model=ExternalSubmissionResponse,
)
async def assign_external_submission(
    submission_id: str,
    request: AssignExternalEvaluatorRequest,
    admin: CurrentUser = Depends(get_admin_user),
    service: ExternalHackathonService = Depends(get_external_hackathon_service),
) -> ExternalSubmissionResponse:
    try:
        row = await run_sync(
            service.assign_evaluator,
            submission_id,
            request.evaluator_id,
            assigned_by=admin.user_id,
        )
    except AppError:
        raise
    return ExternalSubmissionResponse(**row)


@router.post(
    "/admin/external-submissions/{submission_id}/evaluate",
    status_code=status.HTTP_202_ACCEPTED,
)
async def evaluate_external_submission(
    submission_id: str,
    background_tasks: BackgroundTasks,
    _admin: CurrentUser = Depends(get_admin_user),
    service: ExternalHackathonService = Depends(get_external_hackathon_service),
    jobs: EvaluationJobService = Depends(get_evaluation_job_service),
    evaluator: ExternalEvaluationService = Depends(get_external_evaluation_service),
) -> dict[str, str]:
    """Queue AI analysis. The worker writes the external submission, not ``submissions``."""
    try:
        started = await run_sync(service.begin_evaluation, submission_id)
    except AppError:
        raise
    previous = started["previous_status"]
    try:
        if jobs.resolve_mode() == "cloud_tasks":
            await run_sync(jobs.enqueue_external_cloud_task, submission_id)
        else:
            background_tasks.add_task(evaluator.evaluate, submission_id)
    except Exception as exc:
        await run_sync(service.restore_status, submission_id, previous)
        logger.exception("Failed to queue external evaluation for %s", submission_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to queue evaluation",
        ) from exc
    return {"status": "queued", "external_submission_id": submission_id}


@router.get("/external-hackathons/{hackathon_id}", response_model=ExternalHackathonPublic)
async def get_external_hackathon_form(
    hackathon_id: str,
    service: ExternalHackathonService = Depends(get_external_hackathon_service),
) -> ExternalHackathonPublic:
    """Public form configuration. Does not include the signing secret."""
    try:
        row = await run_sync(service.get_public, hackathon_id)
    except AppError:
        raise
    return ExternalHackathonPublic(**row)


@router.post(
    "/external-hackathons/{hackathon_id}/upload-url",
    response_model=ExternalUploadResponse,
)
async def prepare_external_upload(
    hackathon_id: str,
    request: ExternalUploadRequest,
    service: ExternalHackathonService = Depends(get_external_hackathon_service),
) -> ExternalUploadResponse:
    try:
        payload = await run_sync(service.prepare_upload, hackathon_id, request)
    except AppError:
        raise
    except ValueError as exc:
        raise _http_from_value_error(exc) from exc
    return ExternalUploadResponse(**payload)


@router.post(
    "/external-hackathons/{hackathon_id}/submissions",
    response_model=ExternalSubmissionResponse,
    status_code=201,
)
async def create_external_submission(
    hackathon_id: str,
    request: ExternalSubmissionCreate,
    service: ExternalHackathonService = Depends(get_external_hackathon_service),
) -> ExternalSubmissionResponse:
    try:
        created = await run_sync(service.create_submission, hackathon_id, request)
    except AppError:
        raise
    except ValueError as exc:
        raise _http_from_value_error(exc) from exc
    return ExternalSubmissionResponse(**created)
