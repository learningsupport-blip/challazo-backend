"""Schemas for external hackathons and their submissions.

These records never use the ``hackathons`` or ``submissions`` collections.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

from app.models.string_utils import strip_optional, strip_required
from app.utils.video_upload import MAX_VIDEO_UPLOAD_BYTES


ExternalVisibility = Literal["public", "restricted"]
ExternalHackathonStatus = Literal["draft", "published", "closed"]
ExternalSubmissionStatus = Literal["uploaded", "processing", "completed", "failed"]


class ExternalThemeInput(BaseModel):
    """A theme defined on the external hackathon itself."""

    id: Optional[str] = None
    name: str = Field(..., min_length=1, max_length=200)
    description: Optional[str] = Field(None, max_length=4000)

    @field_validator("name", mode="before")
    @classmethod
    def _name(cls, value: object) -> str:
        return strip_required(value)

    @field_validator("description", "id", mode="before")
    @classmethod
    def _optional(cls, value: object) -> str | None:
        return strip_optional(value)


class ExternalTheme(BaseModel):
    id: str
    name: str
    description: str


class ExternalHackathonCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    description: Optional[str] = Field(None, max_length=8000)
    themes: list[ExternalThemeInput] = Field(default_factory=list)
    source_platform: str = Field("launchpad", min_length=1, max_length=80)
    source_platform_url: Optional[str] = Field(None, max_length=500)
    submission_deadline: Optional[str] = None
    visibility: ExternalVisibility = "restricted"
    evaluation_requirement_id: Optional[str] = None

    @field_validator("name", "source_platform", mode="before")
    @classmethod
    def _required(cls, value: object) -> str:
        return strip_required(value)

    @field_validator(
        "description", "source_platform_url", "evaluation_requirement_id", mode="before"
    )
    @classmethod
    def _optional_text(cls, value: object) -> str | None:
        return strip_optional(value)


class ExternalHackathonUpdate(BaseModel):
    """Partial update. Omitted fields stay as they are."""

    name: Optional[str] = Field(None, min_length=1, max_length=200)
    description: Optional[str] = Field(None, max_length=8000)
    themes: Optional[list[ExternalThemeInput]] = None
    source_platform: Optional[str] = Field(None, min_length=1, max_length=80)
    source_platform_url: Optional[str] = Field(None, max_length=500)
    submission_deadline: Optional[str] = None
    visibility: Optional[ExternalVisibility] = None
    evaluation_requirement_id: Optional[str] = None
    status: Optional[ExternalHackathonStatus] = None

    @field_validator("name", "source_platform", mode="before")
    @classmethod
    def _required(cls, value: object) -> str | None:
        if value is None:
            return None
        return strip_required(value)

    @field_validator(
        "description",
        "source_platform_url",
        "evaluation_requirement_id",
        "submission_deadline",
        mode="before",
    )
    @classmethod
    def _optional_text(cls, value: object) -> str | None:
        return strip_optional(value)


class ExternalHackathonPublic(BaseModel):
    """Form configuration. The signing secret is never included."""

    id: str
    name: str
    description: str = ""
    themes: list[ExternalTheme]
    source_platform: str
    submission_deadline: Optional[str] = None
    visibility: ExternalVisibility
    status: ExternalHackathonStatus
    form_url: str


class ExternalHackathonAdmin(ExternalHackathonPublic):
    source_platform_url: Optional[str] = None
    evaluation_requirement_id: Optional[str] = None
    signing_secret: str
    created_by: str
    created_at: str
    updated_at: str


class ExternalTeamLink(BaseModel):
    """
    Team identity forwarded from the form link.

    Restricted hackathons require ``exp`` and ``sig``. Launchpad signs
    ``external_team_id``, ``team_name``, ``leader_name`` (empty if omitted),
    and ``exp`` joined by newlines, with HMAC-SHA256 and the hackathon secret.
    """

    external_team_id: str = Field(..., min_length=1, max_length=128)
    team_name: str = Field(..., min_length=1, max_length=200)
    leader_name: Optional[str] = Field(None, max_length=200)
    exp: Optional[int] = None
    sig: Optional[str] = None

    @field_validator("external_team_id", "team_name", mode="before")
    @classmethod
    def _required(cls, value: object) -> str:
        return strip_required(value)

    @field_validator("leader_name", "sig", mode="before")
    @classmethod
    def _optional(cls, value: object) -> str | None:
        return strip_optional(value)


class ExternalUploadRequest(ExternalTeamLink):
    filename: str = Field(..., min_length=1, max_length=300)
    content_type: Optional[str] = None
    content_length: Optional[int] = Field(None, ge=1, le=MAX_VIDEO_UPLOAD_BYTES)

    @field_validator("filename", mode="before")
    @classmethod
    def _filename(cls, value: object) -> str:
        return strip_required(value)


class ExternalUploadResponse(BaseModel):
    submission_id: str
    video_path: str
    upload_url: str
    content_type: str
    expires_in_seconds: int
    max_upload_bytes: int
    required_headers: dict[str, str]


class ExternalSubmissionCreate(ExternalTeamLink):
    submission_id: str = Field(..., min_length=1, max_length=64)
    video_path: str = Field(..., min_length=1)
    content_type: Optional[str] = None
    source_filename: Optional[str] = None
    theme_id: str = Field(..., min_length=1, max_length=64)
    problem_statement: str = Field(..., min_length=1, max_length=20000)
    solution_description: str = Field(..., min_length=1, max_length=20000)

    @field_validator("submission_id", "video_path", "theme_id", mode="before")
    @classmethod
    def _ids(cls, value: object) -> str:
        return strip_required(value)

    @field_validator("problem_statement", "solution_description", mode="before")
    @classmethod
    def _text(cls, value: object) -> str:
        return strip_required(value)

    @field_validator("content_type", "source_filename", mode="before")
    @classmethod
    def _optional(cls, value: object) -> str | None:
        return strip_optional(value)


class ExternalEvaluationResult(BaseModel):
    checklist: Optional[str] = None
    report: Optional[str] = None
    field_scores: Optional[list[dict]] = None
    scorecard: Optional[dict] = None
    score: Optional[float] = None
    analyzed_at: Optional[str] = None
    error: Optional[str] = None


class ExternalSubmissionResponse(BaseModel):
    id: str
    external_hackathon_id: str
    external_team_id: str
    team_name: str
    leader_name: Optional[str] = None
    theme_id: str
    theme_name: str
    theme_description: str = ""
    problem_statement: str
    solution_description: str
    video_path: Optional[str] = None
    video_url: Optional[str] = None
    content_type: Optional[str] = None
    source_filename: Optional[str] = None
    status: ExternalSubmissionStatus
    error: Optional[str] = None
    submitted_at: str
    created_at: str
    updated_at: str
    assigned_evaluator_id: Optional[str] = None
    assigned_evaluator_name: Optional[str] = None
    assigned_at: Optional[str] = None
    evaluation: Optional[ExternalEvaluationResult] = None


class AssignExternalEvaluatorRequest(BaseModel):
    evaluator_id: str = Field(..., min_length=1, max_length=128)

    @field_validator("evaluator_id", mode="before")
    @classmethod
    def _id(cls, value: object) -> str:
        return strip_required(value)
