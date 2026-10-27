"""
Admin application-settings schemas (profile password + DB reset).
"""

from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator

from app.models.string_utils import strip_required


RESET_CONFIRM_PHRASE = "RESET"


class ChangeProfilePasswordRequest(BaseModel):
    """
    Change the admin Profile Password used to authorize destructive settings.

    Mirrors ``ChangePasswordRequest``: current + new + confirm.
    """

    current_profile_password: str = Field(..., min_length=6, max_length=128)
    new_profile_password: str = Field(..., min_length=6, max_length=128)
    confirm_new_profile_password: str = Field(..., min_length=6, max_length=128)

    @model_validator(mode="after")
    def validate_passwords_match(self) -> "ChangeProfilePasswordRequest":
        if self.new_profile_password != self.confirm_new_profile_password:
            raise ValueError("New profile password and confirm new profile password do not match")
        if self.new_profile_password == self.current_profile_password:
            raise ValueError("New profile password must differ from the current one")
        return self


class ResetDatabaseRequest(BaseModel):
    """Confirm a full application-data wipe with the admin Profile Password."""

    profile_password: str = Field(..., min_length=6, max_length=128)
    confirm_phrase: str = Field(
        ...,
        description=f'Must be exactly "{RESET_CONFIRM_PHRASE}" (case-sensitive).',
    )

    @model_validator(mode="after")
    def validate_confirm_phrase(self) -> "ResetDatabaseRequest":
        phrase = strip_required(self.confirm_phrase)
        if phrase != RESET_CONFIRM_PHRASE:
            raise ValueError(f'confirm_phrase must be exactly "{RESET_CONFIRM_PHRASE}"')
        self.confirm_phrase = phrase
        return self


class AppSettingsResponse(BaseModel):
    """Non-secret settings flags for the Application Settings page."""

    profile_password_configured: bool = True
    default_profile_password_hint: Optional[str] = Field(
        None,
        description=(
            "Only set when the profile password is still the seeded default "
            "(hint for admins to change it)."
        ),
    )
    wipeable_collections: list[str] = Field(default_factory=list)
    evaluation_bucket_name: Optional[str] = Field(
        None,
        description=(
            "GCS bucket whose objects are wiped on Reset Database " "(bucket itself is kept)."
        ),
    )
    reset_confirm_phrase: str = RESET_CONFIRM_PHRASE


class RoleVerificationSettings(BaseModel):
    """Email and mobile checks for one kind of account."""

    email_verification_enabled: bool = True
    mobile_verification_enabled: bool = True


class RegistrationConfigResponse(BaseModel):
    """Which verification each role must pass.

    ``email_verification_enabled`` and ``mobile_verification_enabled`` repeat the
    student flags so an older register screen still hides the right buttons.
    """

    student: RoleVerificationSettings
    evaluator: RoleVerificationSettings
    application_access: RoleVerificationSettings
    email_verification_enabled: bool = True
    mobile_verification_enabled: bool = True


class RegistrationConfigUpdate(BaseModel):
    """Save the email and mobile checkbox for every role."""

    student: RoleVerificationSettings
    evaluator: RoleVerificationSettings
    application_access: RoleVerificationSettings


class RegistrationStatusResponse(RegistrationConfigResponse):
    """Toggles plus how many accounts are waiting or already asked to verify."""

    pending_count: int = Field(
        0,
        ge=0,
        description=(
            "Students, evaluators, and Application Access accounts who still "
            "need a required channel and have not been asked yet."
        ),
    )
    released_count: int = Field(
        0,
        ge=0,
        description="Accounts that will see the verify popup the next time they sign in.",
    )
    released_now: int = Field(
        0,
        ge=0,
        description="How many accounts this request just added to that popup.",
    )


class RegistrationReleaseRequest(BaseModel):
    """Ask the next batch of unverified accounts to verify on login."""

    batch_size: int = Field(..., ge=1, le=500)


class HackathonDeleteListItem(BaseModel):
    """One live hackathon the Super Admin can remove from Application Settings."""

    hackathon_id: str
    name: str


class HackathonDeletePreview(BaseModel):
    """What a hackathon delete will remove, before the Profile Password is sent."""

    hackathon_id: str
    name: str
    submission_count: int = 0
    team_count: int = 0
    enrollment_count: int = 0


class DeleteHackathonRequest(BaseModel):
    """Confirm deletion of one hackathon and everything stored for it."""

    profile_password: str = Field(..., min_length=6, max_length=128)
    confirm_name: str = Field(
        ...,
        min_length=1,
        max_length=200,
        description="Must match the hackathon name exactly.",
    )

    @model_validator(mode="after")
    def normalize_confirm_name(self) -> "DeleteHackathonRequest":
        self.confirm_name = strip_required(self.confirm_name)
        return self


class DeleteHackathonResponse(BaseModel):
    """Result of deleting one hackathon and its submissions."""

    message: str
    hackathon_id: str
    hackathon_name: str
    deleted_counts: dict[str, int]


class ResetDatabaseResponse(BaseModel):
    """Result of a successful database reset."""

    message: str
    deleted_counts: dict[str, int]
    preserved: list[str]


class EvaluationWorkerSetting(BaseModel):
    """One fixed video-evaluation worker the superadmin can give a share of jobs."""

    key: str
    name: str
    project_id: str
    configured: bool
    share: int = Field(..., ge=0, le=20)
    url: Optional[str] = None


class EvaluationRoutingResponse(BaseModel):
    """How the next video analyses are divided. GitHub AI is not included."""

    mode: Literal["personal", "company", "both"] = "personal"
    personal_share: int
    company_share: int
    next_index: int = Field(
        0,
        description="How many videos have been handed out in the current pattern.",
    )
    sequence: list[str]
    summary: str
    scope: str
    workers: list[EvaluationWorkerSetting]


class EvaluationRoutingUpdate(BaseModel):
    """
    Choose one project, or both.

    Shares are used only when ``mode`` is ``both``. Omit ``mode`` to keep the
    older meaning: a share of 0 turns that project off.
    """

    mode: Optional[Literal["personal", "company", "both"]] = None
    personal_share: int = Field(..., ge=0, le=20)
    company_share: int = Field(..., ge=0, le=20)
