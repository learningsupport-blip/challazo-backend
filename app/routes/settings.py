"""
Super Admin Application Settings.

    GET  /super-admin/settings                         -> settings flags (sidebar page)
    GET  /super-admin/settings/evaluation-routing      -> video worker shares
    PUT  /super-admin/settings/evaluation-routing      -> save video worker shares
    GET  /super-admin/settings/registration            -> OTP toggles and waiting counts
    PUT  /super-admin/settings/registration            -> turn email/mobile OTP on or off
    POST /super-admin/settings/registration/release    -> ask the next batch to verify
    GET  /super-admin/settings/hackathons              -> hackathons that can be deleted
    GET  /super-admin/settings/hackathons/{id}         -> delete preview counts
    POST /super-admin/settings/hackathons/{id}/delete  -> delete one hackathon and its work
    POST /super-admin/settings/change-profile-password -> change Profile Password
    POST /super-admin/settings/reset-database          -> wipe app Firestore data

Admin and custom roles receive 403. The page and Reset Database stay the same
for Super Admin.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, status

from app.dependencies import get_app_settings_service, get_hackathon_service
from app.exceptions import AppError
from app.middleware.auth_middleware import get_super_admin_user
from app.models.settings_model import (
    AppSettingsResponse,
    ChangeProfilePasswordRequest,
    DeleteHackathonRequest,
    DeleteHackathonResponse,
    EvaluationRoutingResponse,
    EvaluationRoutingUpdate,
    HackathonDeleteListItem,
    HackathonDeletePreview,
    RegistrationConfigUpdate,
    RegistrationReleaseRequest,
    RegistrationStatusResponse,
    ResetDatabaseRequest,
    ResetDatabaseResponse,
)
from app.models.user_model import CurrentUser
from app.services.app_settings_service import AppSettingsService
from app.services.hackathon_service import HackathonService
from app.utils.async_io import run_sync


logger = logging.getLogger(__name__)
router = APIRouter(prefix="/super-admin/settings", tags=["super-admin-settings"])


@router.get("", response_model=AppSettingsResponse)
async def get_application_settings(
    super_admin: CurrentUser = Depends(get_super_admin_user),
    service: AppSettingsService = Depends(get_app_settings_service),
) -> AppSettingsResponse:
    """
    Application Settings page payload for Super Admin.

    Never returns the Profile Password itself — only configuration flags.
    """
    _ = super_admin
    payload = await run_sync(service.get_settings_public)
    return AppSettingsResponse(**payload)


@router.get("/evaluation-routing", response_model=EvaluationRoutingResponse)
async def get_evaluation_routing(
    super_admin: CurrentUser = Depends(get_super_admin_user),
    service: AppSettingsService = Depends(get_app_settings_service),
) -> EvaluationRoutingResponse:
    """
    Video AI worker shares for Application Settings.

    ``sequence`` is the repeating order. Index 0 is the next video after a save.
    GitHub AI evaluation is not part of this setting.
    """
    _ = super_admin
    payload = await run_sync(service.get_evaluation_routing)
    return EvaluationRoutingResponse(**payload)


@router.put("/evaluation-routing", response_model=EvaluationRoutingResponse)
async def update_evaluation_routing(
    request: EvaluationRoutingUpdate,
    super_admin: CurrentUser = Depends(get_super_admin_user),
    service: AppSettingsService = Depends(get_app_settings_service),
) -> EvaluationRoutingResponse:
    """
    Save how many videos go to each project before the pattern repeats.

    A personal share of 4 and a company share of 1 sends four videos to the
    personal project, then one to the company project. Jobs already queued
    keep the worker they were given.
    """
    try:
        saved = await run_sync(
            service.set_evaluation_routing,
            personal_share=request.personal_share,
            company_share=request.company_share,
            updated_by=super_admin.user_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except AppError:
        raise
    except Exception as exc:
        logger.exception("Saving evaluation routing failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to save evaluation routing",
        ) from exc
    return EvaluationRoutingResponse(**saved)


@router.post("/change-profile-password", status_code=200)
async def change_profile_password(
    request: ChangeProfilePasswordRequest,
    super_admin: CurrentUser = Depends(get_super_admin_user),
    service: AppSettingsService = Depends(get_app_settings_service),
) -> dict:
    """
    Change the admin Profile Password (same UX pattern as ``/auth/change-password``).

    Requires the current Profile Password, then new + confirm. This password is
    separate from the Firebase login password and is used for destructive
    actions such as Reset Database.
    """
    try:
        await run_sync(
            service.change_profile_password,
            request.current_profile_password,
            request.new_profile_password,
            super_admin.user_id,
        )
    except ValueError as e:
        # 400, not 401. A wrong Profile Password is not an expired login, and
        # the SPA treats every 401 as "signed out".
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    except AppError:
        raise
    except Exception as e:
        logger.exception("Change profile password failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to change profile password",
        ) from e

    return {"message": "Profile password changed successfully"}


@router.post("/reset-database", response_model=ResetDatabaseResponse)
async def reset_database(
    request: ResetDatabaseRequest,
    super_admin: CurrentUser = Depends(get_super_admin_user),
    service: AppSettingsService = Depends(get_app_settings_service),
) -> ResetDatabaseResponse:
    """
    Delete application documents from Firestore (hackathons, submissions, …).

    Requires a valid Profile Password and ``confirm_phrase: "RESET"``.
    Admin user accounts and the Profile Password setting are preserved.
    """
    try:
        result = await run_sync(
            service.reset_database,
            request.profile_password,
            preserve_user_id=super_admin.user_id,
            confirm_phrase=request.confirm_phrase,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    except AppError:
        raise
    except Exception as e:
        logger.exception("Database reset failed for super admin %s", super_admin.user_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to reset database",
        ) from e

    return ResetDatabaseResponse(**result)


@router.get("/registration", response_model=RegistrationStatusResponse)
async def get_registration_config(
    super_admin: CurrentUser = Depends(get_super_admin_user),
    service: AppSettingsService = Depends(get_app_settings_service),
) -> RegistrationStatusResponse:
    """Per-role email and mobile flags, plus who is waiting and who was already asked."""
    _ = super_admin
    status_payload = await run_sync(service.registration_status)
    return RegistrationStatusResponse(**status_payload)


@router.put("/registration", response_model=RegistrationStatusResponse)
async def update_registration_config(
    request: RegistrationConfigUpdate,
    super_admin: CurrentUser = Depends(get_super_admin_user),
    service: AppSettingsService = Depends(get_app_settings_service),
) -> RegistrationStatusResponse:
    """
    Save email and mobile verification for students, evaluators, and Application Access.

    Saving does not open the login popup. Call release for that.
    """
    try:
        saved = await run_sync(
            service.set_registration_config,
            student=request.student.model_dump(),
            evaluator=request.evaluator.model_dump(),
            application_access=request.application_access.model_dump(),
            updated_by=super_admin.user_id,
        )
    except AppError:
        raise
    except Exception as exc:
        logger.exception("Saving registration config failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to save registration settings",
        ) from exc
    return RegistrationStatusResponse(**saved)


@router.post("/registration/release", response_model=RegistrationStatusResponse)
async def release_registration_verification(
    request: RegistrationReleaseRequest,
    super_admin: CurrentUser = Depends(get_super_admin_user),
    service: AppSettingsService = Depends(get_app_settings_service),
) -> RegistrationStatusResponse:
    """
    Ask the next batch of unverified students, evaluators, and Application Access
    accounts to verify on login.

    Does not send OTP email. The popup uses the existing verify buttons.
    """
    try:
        saved = await run_sync(
            service.release_verification,
            request.batch_size,
            released_by=super_admin.user_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except AppError:
        raise
    except Exception as exc:
        logger.exception("Releasing registration verification failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to send verification",
        ) from exc
    return RegistrationStatusResponse(**saved)


@router.get("/hackathons", response_model=list[HackathonDeleteListItem])
async def list_hackathons_for_deletion(
    super_admin: CurrentUser = Depends(get_super_admin_user),
    hackathons: HackathonService = Depends(get_hackathon_service),
) -> list[HackathonDeleteListItem]:
    """Live hackathons Super Admin can delete from Application Settings."""
    _ = super_admin
    rows = await run_sync(hackathons.list_hackathons_for_deletion)
    return [HackathonDeleteListItem(**row) for row in rows]


@router.get("/hackathons/{hackathon_id}", response_model=HackathonDeletePreview)
async def preview_hackathon_deletion(
    hackathon_id: str,
    super_admin: CurrentUser = Depends(get_super_admin_user),
    hackathons: HackathonService = Depends(get_hackathon_service),
) -> HackathonDeletePreview:
    """How many submissions, teams, and enrollments this hackathon still has."""
    _ = super_admin
    preview = await run_sync(hackathons.preview_hackathon_deletion, hackathon_id)
    if not preview:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Hackathon not found")
    return HackathonDeletePreview(**preview)


@router.post("/hackathons/{hackathon_id}/delete", response_model=DeleteHackathonResponse)
async def delete_hackathon_and_submissions(
    hackathon_id: str,
    request: DeleteHackathonRequest,
    super_admin: CurrentUser = Depends(get_super_admin_user),
    settings: AppSettingsService = Depends(get_app_settings_service),
    hackathons: HackathonService = Depends(get_hackathon_service),
) -> DeleteHackathonResponse:
    """
    Delete one hackathon, every student submission, and the evaluation stored for it.

    Requires the Profile Password and the hackathon name. Student accounts stay.
    """
    if not await run_sync(settings.verify_profile_password, request.profile_password):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Profile password is incorrect",
        )
    preview = await run_sync(hackathons.preview_hackathon_deletion, hackathon_id)
    if not preview:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Hackathon not found")
    expected = preview["name"] or hackathon_id
    if request.confirm_name != expected:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Hackathon name does not match",
        )
    try:
        result = await run_sync(
            hackathons.purge_hackathon,
            hackathon_id,
            deleted_by=super_admin.user_id,
        )
    except AppError:
        raise
    except Exception as exc:
        logger.exception("Hackathon delete failed for %s", hackathon_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to delete hackathon",
        ) from exc
    if not result:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Hackathon not found")
    return DeleteHackathonResponse(
        message=(
            "Hackathon deleted. Submissions, videos, scores, and AI reports "
            "for this hackathon were removed. Student accounts were kept."
        ),
        **result,
    )
