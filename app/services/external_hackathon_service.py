"""External hackathons and submissions, stored apart from the student flow."""

from __future__ import annotations

import hashlib
import hmac
import os
import time
from typing import Any

from google.cloud import storage

from app.exceptions import BadRequestError, ConflictError, ForbiddenError, NotFoundError
from app.models.external_hackathon_model import (
    ExternalHackathonCreate,
    ExternalHackathonUpdate,
    ExternalSubmissionCreate,
    ExternalUploadRequest,
)
from app.services.firebase import FirebaseService
from app.services.user_service import UserService
from app.utils.gcs_video import (
    generate_signed_upload_url,
    generate_signed_url,
    parse_gs_uri,
    resolve_evaluation_bucket_name,
)
from app.utils.ids import new_id
from app.utils.time import now_ist, now_ist_iso, parse_to_ist
from app.utils.video_upload import (
    MAX_VIDEO_UPLOAD_BYTES,
    assert_video_size,
    resolve_video_content_type_from_metadata,
)


EXTERNAL_HACKATHONS = "external_hackathons"
EXTERNAL_SUBMISSIONS = "external_submissions"
_OPEN = "published"


def form_base_url() -> str:
    """Portal origin used in the dedicated submission link."""
    explicit = os.getenv("EXTERNAL_FORM_BASE_URL", "").strip().rstrip("/")
    if explicit:
        return explicit
    for origin in os.getenv("ALLOWED_ORIGINS", "").split(","):
        cleaned = origin.strip().rstrip("/")
        host = cleaned.lower()
        if host.startswith("https://") and "localhost" not in host and "127.0.0.1" not in host:
            return cleaned
    return "https://challazo.in"


def form_path(hackathon_id: str) -> str:
    return f"/external-hackathons/{hackathon_id}/submit"


def team_signature_message(
    external_team_id: str,
    team_name: str,
    leader_name: str | None,
    exp: int,
) -> bytes:
    """Bytes Launchpad signs. Empty leader name is a blank line."""
    leader = leader_name or ""
    text = f"{external_team_id}\n{team_name}\n{leader}\n{int(exp)}"
    return text.encode("utf-8")


def sign_team_link(
    secret: str,
    external_team_id: str,
    team_name: str,
    leader_name: str | None,
    exp: int,
) -> str:
    digest = hmac.new(
        secret.encode("utf-8"),
        team_signature_message(external_team_id, team_name, leader_name, exp),
        hashlib.sha256,
    )
    return digest.hexdigest()


class ExternalHackathonService:
    """Create external hackathons and accept their form submissions."""

    def __init__(
        self,
        firebase: FirebaseService | None = None,
        storage_client: storage.Client | None = None,
        user_service: UserService | None = None,
    ) -> None:
        self.firebase = firebase or FirebaseService()
        self.storage_client = storage_client
        self.user_service = user_service or UserService(firebase=self.firebase)
        self.bucket_name = resolve_evaluation_bucket_name()

    def create(self, payload: ExternalHackathonCreate, *, created_by: str) -> dict[str, Any]:
        hackathon_id = new_id()
        now = now_ist_iso()
        deadline = self._deadline_or_none(payload.submission_deadline)
        document = {
            "id": hackathon_id,
            "name": payload.name,
            "description": payload.description or "",
            "themes": self._normalize_themes(payload.themes),
            "source_platform": payload.source_platform,
            "source_platform_url": payload.source_platform_url,
            "submission_deadline": deadline,
            "visibility": payload.visibility,
            "status": "draft",
            "evaluation_requirement_id": payload.evaluation_requirement_id,
            "signing_secret": self._new_secret(),
            "created_by": created_by,
            "created_at": now,
            "updated_at": now,
        }
        self.firebase.set_document(EXTERNAL_HACKATHONS, hackathon_id, document)
        return self._admin_view(document)

    def list_hackathons(self) -> list[dict[str, Any]]:
        rows = self.firebase.get_collection(EXTERNAL_HACKATHONS)
        rows.sort(key=lambda item: item.get("created_at") or "", reverse=True)
        return [self._admin_view(row) for row in rows]

    def get_admin(self, hackathon_id: str) -> dict[str, Any]:
        return self._admin_view(self._require_hackathon(hackathon_id))

    def get_public(self, hackathon_id: str) -> dict[str, Any]:
        return self._public_view(self._require_hackathon(hackathon_id))

    def update(
        self,
        hackathon_id: str,
        payload: ExternalHackathonUpdate,
    ) -> dict[str, Any]:
        current = self._require_hackathon(hackathon_id)
        changes = payload.model_dump(exclude_unset=True)
        if "themes" in changes:
            changes["themes"] = self._normalize_themes(payload.themes or [])
        if "description" in changes and changes["description"] is None:
            changes["description"] = ""
        if "submission_deadline" in changes:
            changes["submission_deadline"] = self._deadline_or_none(changes["submission_deadline"])
        if changes.get("status") == "published":
            preview = {**current, **changes}
            self._assert_publishable(preview)
        next_doc = {**current, **changes, "updated_at": now_ist_iso()}
        self.firebase.set_document(EXTERNAL_HACKATHONS, hackathon_id, next_doc)
        return self._admin_view(next_doc)

    def publish(self, hackathon_id: str) -> dict[str, Any]:
        current = self._require_hackathon(hackathon_id)
        self._assert_publishable(current)
        current["status"] = "published"
        current["updated_at"] = now_ist_iso()
        if not current.get("signing_secret"):
            current["signing_secret"] = self._new_secret()
        self.firebase.set_document(EXTERNAL_HACKATHONS, hackathon_id, current)
        return self._admin_view(current)

    def close(self, hackathon_id: str) -> dict[str, Any]:
        current = self._require_hackathon(hackathon_id)
        current["status"] = "closed"
        current["updated_at"] = now_ist_iso()
        self.firebase.set_document(EXTERNAL_HACKATHONS, hackathon_id, current)
        return self._admin_view(current)

    def prepare_upload(self, hackathon_id: str, payload: ExternalUploadRequest) -> dict[str, Any]:
        hackathon = self._require_open(hackathon_id)
        team_id, _team_name, _leader_name = self._verified_team(hackathon, payload)
        self._assert_no_duplicate(hackathon_id, team_id)
        if not self.bucket_name:
            raise BadRequestError("Video storage is not configured.")
        if payload.content_length is not None:
            assert_video_size(
                payload.content_length, max_bytes=MAX_VIDEO_UPLOAD_BYTES, via="signed"
            )

        resolved_type, extension = resolve_video_content_type_from_metadata(
            payload.content_type,
            payload.filename,
        )
        submission_id = new_id()
        object_name = (
            f"external-submissions/{hackathon_id}/{team_id}/{submission_id}/video{extension}"
        )
        expires_in = int(os.getenv("VIDEO_UPLOAD_URL_EXPIRY_SECONDS", "3600"))
        upload_url = generate_signed_upload_url(
            self._storage_client(),
            self.bucket_name,
            object_name,
            resolved_type,
            expiry_seconds=expires_in,
        )
        return {
            "submission_id": submission_id,
            "video_path": f"gs://{self.bucket_name}/{object_name}",
            "upload_url": upload_url,
            "content_type": resolved_type,
            "expires_in_seconds": expires_in,
            "max_upload_bytes": MAX_VIDEO_UPLOAD_BYTES,
            "required_headers": {"Content-Type": resolved_type},
        }

    def create_submission(
        self,
        hackathon_id: str,
        payload: ExternalSubmissionCreate,
    ) -> dict[str, Any]:
        hackathon = self._require_open(hackathon_id)
        team_id, team_name, leader_name = self._verified_team(hackathon, payload)
        self._assert_before_deadline(hackathon)
        self._assert_no_duplicate(hackathon_id, team_id)
        theme = self._theme(hackathon, payload.theme_id)
        video = self._verified_video(
            hackathon_id,
            team_id,
            payload.submission_id,
            payload.video_path,
            payload.content_type,
            payload.source_filename,
        )
        now = now_ist_iso()
        document = {
            "id": payload.submission_id,
            "external_hackathon_id": hackathon_id,
            "external_team_id": team_id,
            "team_name": team_name,
            "leader_name": leader_name,
            "theme_id": theme["id"],
            "theme_name": theme["name"],
            "theme_description": theme["description"],
            "problem_statement": payload.problem_statement,
            "solution_description": payload.solution_description,
            "video_path": video["video_path"],
            "content_type": video["content_type"],
            "source_filename": payload.source_filename,
            "status": "uploaded",
            "error": None,
            "submitted_at": now,
            "created_at": now,
            "updated_at": now,
            "assigned_evaluator_id": None,
            "assigned_evaluator_name": None,
            "assigned_at": None,
            "evaluation": None,
        }
        self.firebase.set_document(EXTERNAL_SUBMISSIONS, payload.submission_id, document)
        return self._submission_view(document, include_video_url=False)

    def list_submissions(self, hackathon_id: str) -> list[dict[str, Any]]:
        self._require_hackathon(hackathon_id)
        rows = self.firebase.query_collection(
            EXTERNAL_SUBMISSIONS,
            "external_hackathon_id",
            "==",
            hackathon_id,
        )
        rows.sort(key=lambda item: item.get("submitted_at") or "", reverse=True)
        return [self._submission_view(row, include_video_url=True) for row in rows]

    def assign_evaluator(
        self,
        submission_id: str,
        evaluator_id: str,
        *,
        assigned_by: str,
    ) -> dict[str, Any]:
        _ = assigned_by
        submission = self._require_submission(submission_id)
        evaluator = self.user_service.get_user(evaluator_id)
        if not evaluator or evaluator.get("role") != "evaluator":
            raise BadRequestError("Choose an evaluator account.")
        if evaluator.get("approval_status") != "approved":
            raise BadRequestError("That evaluator is not approved.")
        name = str(evaluator.get("name") or "").strip() or evaluator_id
        now = now_ist_iso()
        submission["assigned_evaluator_id"] = evaluator_id
        submission["assigned_evaluator_name"] = name
        submission["assigned_at"] = now
        submission["updated_at"] = now
        self.firebase.set_document(EXTERNAL_SUBMISSIONS, submission_id, submission)
        return self._submission_view(submission, include_video_url=True)

    def begin_evaluation(self, submission_id: str) -> dict[str, Any]:
        """Mark processing before the job is queued. Already-running jobs stay put."""
        submission = self._require_submission(submission_id)
        if submission.get("status") == "processing":
            raise ConflictError(
                "AI analysis is already running for this submission.",
                code="ALREADY_PROCESSING",
            )
        previous = submission.get("status") or "uploaded"
        submission["status"] = "processing"
        submission["error"] = None
        submission["updated_at"] = now_ist_iso()
        self.firebase.set_document(EXTERNAL_SUBMISSIONS, submission_id, submission)
        return {"submission": submission, "previous_status": previous}

    def restore_status(self, submission_id: str, status: str) -> None:
        submission = self.firebase.get_document(EXTERNAL_SUBMISSIONS, submission_id)
        if not submission:
            return
        submission["status"] = status
        submission["updated_at"] = now_ist_iso()
        self.firebase.set_document(EXTERNAL_SUBMISSIONS, submission_id, submission)

    def _require_hackathon(self, hackathon_id: str) -> dict[str, Any]:
        document = self.firebase.get_document(EXTERNAL_HACKATHONS, hackathon_id)
        if not document:
            raise NotFoundError("External hackathon not found")
        document.setdefault("id", hackathon_id)
        return document

    def _require_submission(self, submission_id: str) -> dict[str, Any]:
        document = self.firebase.get_document(EXTERNAL_SUBMISSIONS, submission_id)
        if not document:
            raise NotFoundError("External submission not found")
        document.setdefault("id", submission_id)
        return document

    def _require_open(self, hackathon_id: str) -> dict[str, Any]:
        hackathon = self._require_hackathon(hackathon_id)
        if hackathon.get("status") != _OPEN:
            raise BadRequestError("This external hackathon is not open for submissions.")
        self._assert_before_deadline(hackathon)
        return hackathon

    def _assert_before_deadline(self, hackathon: dict[str, Any]) -> None:
        raw = hackathon.get("submission_deadline")
        if not raw:
            return
        if now_ist() > parse_to_ist(raw):
            raise BadRequestError("The submission deadline has passed.")

    def _assert_publishable(self, hackathon: dict[str, Any]) -> None:
        if not (hackathon.get("name") or "").strip():
            raise BadRequestError("Add a hackathon name before publishing.")
        if not hackathon.get("themes"):
            raise BadRequestError("Add at least one theme before publishing.")
        if not hackathon.get("submission_deadline"):
            raise BadRequestError("Set a submission deadline before publishing.")

    def _assert_no_duplicate(self, hackathon_id: str, team_id: str) -> None:
        rows = self.firebase.query_collection(
            EXTERNAL_SUBMISSIONS,
            "external_hackathon_id",
            "==",
            hackathon_id,
        )
        for row in rows:
            if row.get("external_team_id") == team_id:
                raise ConflictError(
                    "This team has already submitted for this hackathon.",
                    code="ALREADY_SUBMITTED",
                )

    def _verified_team(
        self,
        hackathon: dict[str, Any],
        payload: ExternalUploadRequest | ExternalSubmissionCreate,
    ) -> tuple[str, str, str | None]:
        team_id = self._safe_team_id(payload.external_team_id)
        team_name = payload.team_name.strip()
        leader_name = payload.leader_name
        if hackathon.get("visibility") == "restricted":
            self._verify_signature(
                secret=str(hackathon.get("signing_secret") or ""),
                external_team_id=team_id,
                team_name=team_name,
                leader_name=leader_name,
                exp=payload.exp,
                sig=payload.sig,
            )
        return team_id, team_name, leader_name

    def _verify_signature(
        self,
        *,
        secret: str,
        external_team_id: str,
        team_name: str,
        leader_name: str | None,
        exp: int | None,
        sig: str | None,
    ) -> None:
        if exp is None or not sig or not secret:
            raise ForbiddenError(
                "This form requires a signed team link.",
                code="INVALID_SIGNATURE",
            )
        if int(exp) < int(time.time()):
            raise ForbiddenError("The submission link has expired.", code="LINK_EXPIRED")
        expected = sign_team_link(secret, external_team_id, team_name, leader_name, int(exp))
        try:
            matches = hmac.compare_digest(expected, sig)
        except ValueError:
            matches = False
        if not matches:
            raise ForbiddenError(
                "The submission link is not valid.",
                code="INVALID_SIGNATURE",
            )

    def _verified_video(
        self,
        hackathon_id: str,
        team_id: str,
        submission_id: str,
        video_path: str,
        content_type: str | None,
        source_filename: str | None,
    ) -> dict[str, str]:
        if not self.bucket_name:
            raise BadRequestError("Video storage is not configured.")
        resolved_type, extension = resolve_video_content_type_from_metadata(
            content_type,
            source_filename or "submission.mp4",
        )
        try:
            bucket_name, object_name = parse_gs_uri(video_path.strip())
        except ValueError as exc:
            raise BadRequestError("Invalid video_path") from exc
        expected = f"external-submissions/{hackathon_id}/{team_id}/{submission_id}/video{extension}"
        if bucket_name != self.bucket_name or object_name != expected:
            raise BadRequestError("video_path is not for this submission.")
        blob = self._storage_client().bucket(bucket_name).blob(object_name)
        if not blob.exists():
            raise BadRequestError(
                "Video has not been uploaded yet. Finish the upload, then submit."
            )
        blob.reload()
        assert_video_size(int(blob.size or 0), max_bytes=MAX_VIDEO_UPLOAD_BYTES, via="signed")
        return {"video_path": f"gs://{bucket_name}/{object_name}", "content_type": resolved_type}

    def _theme(self, hackathon: dict[str, Any], theme_id: str) -> dict[str, str]:
        for theme in hackathon.get("themes") or []:
            if theme.get("id") == theme_id:
                return {
                    "id": theme_id,
                    "name": theme.get("name") or "",
                    "description": theme.get("description") or theme.get("name") or "",
                }
        raise BadRequestError("Choose a theme from this external hackathon.")

    def _submission_view(
        self, document: dict[str, Any], *, include_video_url: bool
    ) -> dict[str, Any]:
        view = dict(document)
        view["video_url"] = None
        if include_video_url and document.get("video_path") and self.storage_client is not None:
            view["video_url"] = generate_signed_url(self.storage_client, document["video_path"])
        return view

    def _admin_view(self, document: dict[str, Any]) -> dict[str, Any]:
        view = self._public_view(document)
        view.update(
            {
                "source_platform_url": document.get("source_platform_url"),
                "evaluation_requirement_id": document.get("evaluation_requirement_id"),
                "signing_secret": document.get("signing_secret") or "",
                "created_by": document.get("created_by") or "",
                "created_at": document.get("created_at") or "",
                "updated_at": document.get("updated_at") or "",
            }
        )
        return view

    def _public_view(self, document: dict[str, Any]) -> dict[str, Any]:
        hackathon_id = document.get("id") or ""
        return {
            "id": hackathon_id,
            "name": document.get("name") or "",
            "description": document.get("description") or "",
            "themes": list(document.get("themes") or []),
            "source_platform": document.get("source_platform") or "",
            "submission_deadline": document.get("submission_deadline"),
            "visibility": document.get("visibility") or "restricted",
            "status": document.get("status") or "draft",
            "form_url": f"{form_base_url()}{form_path(hackathon_id)}",
        }

    def _storage_client(self) -> storage.Client:
        if self.storage_client is not None:
            return self.storage_client
        from app.utils.gcs_video import build_storage_client

        project = os.getenv("GOOGLE_CLOUD_PROJECT") or os.getenv("FIREBASE_PROJECT_ID")
        self.storage_client = build_storage_client(project or None)
        return self.storage_client

    @staticmethod
    def _normalize_themes(themes: list[Any]) -> list[dict[str, str]]:
        cleaned: list[dict[str, str]] = []
        seen: set[str] = set()
        for item in themes:
            name = str(getattr(item, "name", None) or item.get("name") or "").strip()
            if not name:
                raise BadRequestError("Theme name is required.")
            raw_id = getattr(item, "id", None)
            if raw_id is None and isinstance(item, dict):
                raw_id = item.get("id")
            theme_id = str(raw_id or "").strip() or new_id()
            if "/" in theme_id or theme_id in seen:
                raise BadRequestError("Theme ids must be unique.")
            seen.add(theme_id)
            raw_description = getattr(item, "description", None)
            if raw_description is None and isinstance(item, dict):
                raw_description = item.get("description")
            description = str(raw_description or "").strip() or name
            cleaned.append({"id": theme_id, "name": name, "description": description})
        return cleaned

    @staticmethod
    def _deadline_or_none(value: str | None) -> str | None:
        if not value:
            return None
        return parse_to_ist(value).isoformat()

    @staticmethod
    def _safe_team_id(value: str) -> str:
        text = value.strip()
        if not text or "/" in text or "\\" in text or ".." in text:
            raise BadRequestError("external_team_id is not valid.")
        return text

    @staticmethod
    def _new_secret() -> str:
        return hashlib.sha256(os.urandom(32)).hexdigest()
