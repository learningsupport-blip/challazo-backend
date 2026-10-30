"""
University schemas (stored in the ``universities`` Firestore collection).

Admins create universities (name + location) from the dashboard. Students
pick one from that catalogue when registering. ``display_label`` is
``"{name}, {location}"`` for the registration dropdown.
"""

from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from app.models.string_utils import strip_required
from app.utils.time import ISTDateTime


# Stored on the university and copied onto the student at registration.
# "NIAT" asks for a NIAT ID. "Non NIAT" asks for a College ID.
CollegeType = Literal["NIAT", "Non NIAT"]
COLLEGE_TYPE_NIAT: CollegeType = "NIAT"
COLLEGE_TYPE_NON_NIAT: CollegeType = "Non NIAT"


def university_display_label(name: str, location: str) -> str:
    """Label shown in the student-registration University Name dropdown."""
    return f"{name}, {location}"


def normalize_college_type(value: str | None) -> CollegeType:
    """Missing or unknown values stay NIAT so existing campuses keep a NIAT ID."""
    if value == COLLEGE_TYPE_NON_NIAT:
        return COLLEGE_TYPE_NON_NIAT
    return COLLEGE_TYPE_NIAT


class UniversityCreateRequest(BaseModel):
    """Payload for creating a university."""

    name: str = Field(..., min_length=1, max_length=200)
    location: str = Field(..., min_length=1, max_length=200)
    college_type: CollegeType = Field(
        COLLEGE_TYPE_NIAT,
        description='NIAT asks students for a NIAT ID. "Non NIAT" asks for a College ID.',
    )
    total_students: Optional[int] = Field(
        None,
        ge=1,
        le=1_000_000,
        description="Campus headcount. Required when college_type is NIAT. Omit for Non NIAT.",
    )

    @field_validator("name", "location", mode="before")
    @classmethod
    def normalize_required_text(cls, value: str) -> str:
        return strip_required(value)

    @model_validator(mode="after")
    def total_students_only_for_niat(self) -> "UniversityCreateRequest":
        if self.college_type == COLLEGE_TYPE_NIAT and self.total_students is None:
            raise ValueError("Total students is required when college type is NIAT")
        if self.college_type != COLLEGE_TYPE_NIAT and self.total_students is not None:
            raise ValueError("Total students applies only when college type is NIAT")
        return self


class UniversityUpdateRequest(BaseModel):
    """Partial update payload for a university."""

    name: Optional[str] = Field(None, min_length=1, max_length=200)
    location: Optional[str] = Field(None, min_length=1, max_length=200)
    college_type: Optional[CollegeType] = None
    total_students: Optional[int] = Field(
        None,
        ge=1,
        le=1_000_000,
        description="Campus headcount. Send only when the university is or becomes NIAT.",
    )

    @field_validator("name", "location", mode="before")
    @classmethod
    def normalize_optional_required_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return strip_required(value)


class UniversityResponse(BaseModel):
    """A university returned to clients."""

    id: str
    name: str
    location: str
    display_label: str
    college_type: CollegeType = COLLEGE_TYPE_NIAT
    total_students: Optional[int] = None
    created_by: str
    created_at: ISTDateTime
    updated_at: ISTDateTime


class BatchCreateRequest(BaseModel):
    """A batch offered at one NIAT partnered university."""

    name: str = Field(
        ...,
        min_length=1,
        max_length=100,
        description='Label students pick, such as "2024-2028".',
    )
    total_students: int = Field(
        ...,
        ge=1,
        le=1_000_000,
        description="Strength of this batch: how many students are in it.",
    )

    @field_validator("name", mode="before")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        return strip_required(value)


class BatchUpdateRequest(BaseModel):
    """Rename a batch. It stays on the same university."""

    name: str = Field(..., min_length=1, max_length=100)
    total_students: Optional[int] = Field(
        None,
        ge=1,
        le=1_000_000,
        description="New batch strength. Omit to leave the current number.",
    )

    @field_validator("name", mode="before")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        return strip_required(value)


class BatchResponse(BaseModel):
    """One batch, always tied to a single university."""

    id: str
    university_id: str
    university_name: str = ""
    name: str
    total_students: Optional[int] = None
    created_by: str
    created_at: ISTDateTime
    updated_at: ISTDateTime
