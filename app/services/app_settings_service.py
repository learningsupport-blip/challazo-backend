"""
Application settings — admin Profile Password and Firestore DB reset.
"""

from __future__ import annotations
from app.utils.time import now_ist_iso

import hashlib
import hmac
import logging
import os
import secrets
from typing import Any

from google.cloud import storage

from app.models.settings_model import RESET_CONFIRM_PHRASE
from app.services.email_service import MAIL_COLLECTION
from app.services.evaluation_routing import (
    COMPANY_KEY,
    EVALUATION_ROUTING_DOC_ID,
    PERSONAL_KEY,
    WORKER_CATALOG,
    build_sequence,
    routing_summary,
    shares_from_document,
    worker_urls,
)
from app.services.firebase import FirebaseService
from app.services.team_service import TEAMS, ENROLLMENTS, JOIN_CODES
from app.services.verification_service import RATE_LIMITS, SESSIONS
from app.utils.gcs_video import (
    build_storage_client,
    resolve_evaluation_bucket_name,
    wipe_bucket_objects,
)


logger = logging.getLogger(__name__)

DEFAULT_PROFILE_PASSWORD = "12345678"

# Everything except the Super Admin login and the Profile Password doc.
# Built-in Admin and Student roles are written back after the wipe, as on a
# fresh startup. Admin user accounts are not kept.
WIPEABLE_COLLECTIONS: tuple[str, ...] = (
    "hackathons",
    "hackathon_drafts",
    "themes",
    "universities",
    "batches",
    "evaluation_requirements",
    "ai_evaluation_metric_scoring",
    "ai_evaluation_prompts",
    # Legacy singular name (if an older deploy ever wrote here).
    "ai_evaluation_prompt",
    "submissions",
    "analysis",
    TEAMS,
    ENROLLMENTS,
    JOIN_CODES,
    "application_logs",
    "roles",
    # Job rows left by an older worker. Not written by this service.
    "github_analysis_jobs",
    SESSIONS,
    RATE_LIMITS,
    MAIL_COLLECTION,
)

# Known AI prompt document ids — deleted explicitly so a listing miss cannot
# leave custom prompts behind after reset.
AI_PROMPT_DOCUMENT_IDS: tuple[str, ...] = ("checklist", "analyze_video")

SETTINGS_COLLECTION = "app_settings"
SETTINGS_DOC_ID = "security"
REGISTRATION_DOC_ID = "registration"
_VERIFICATION_ROLE_KEYS = ("student", "evaluator", "application_access")
_REGISTRATION_ROLES = ("student", "evaluator", "admin", "custom")
_BOTH_ON = {
    "email_verification_enabled": True,
    "mobile_verification_enabled": True,
}


def role_verification_key(role: str) -> str | None:
    """Map an account role to the checkbox group that applies to it."""
    if role == "student":
        return "student"
    if role == "evaluator":
        return "evaluator"
    if role in ("admin", "custom"):
        return "application_access"
    return None


def _channel_pair(raw: Any, fallback: dict[str, bool]) -> dict[str, bool]:
    if not isinstance(raw, dict):
        return dict(fallback)
    return {
        "email_verification_enabled": raw.get("email_verification_enabled", True) is not False,
        "mobile_verification_enabled": raw.get("mobile_verification_enabled", True) is not False,
    }


def normalize_registration_config(doc: dict[str, Any] | None) -> dict[str, Any]:
    """Role map, with the old two global switches applied when a role is missing.

    A document saved before per-role checkboxes used ``email_verification_enabled``
    and ``mobile_verification_enabled`` for students and evaluators together.
    Those values fill any role block that is not stored yet. A missing document
    leaves every channel on.
    """
    stored = doc or {}
    if not stored:
        fallback = dict(_BOTH_ON)
    else:
        fallback = {
            "email_verification_enabled": stored.get("email_verification_enabled", True)
            is not False,
            "mobile_verification_enabled": stored.get("mobile_verification_enabled", True)
            is not False,
        }
    student = _channel_pair(stored.get("student"), fallback)
    evaluator = _channel_pair(stored.get("evaluator"), fallback)
    application_access = _channel_pair(stored.get("application_access"), fallback)
    return {
        "student": student,
        "evaluator": evaluator,
        "application_access": application_access,
        "email_verification_enabled": student["email_verification_enabled"],
        "mobile_verification_enabled": student["mobile_verification_enabled"],
    }


def channels_for_role(config: dict[str, Any], role: str) -> dict[str, bool]:
    """Email and mobile flags for this account's role. Unknown roles verify nothing."""
    normalized = normalize_registration_config(config)
    key = role_verification_key(role)
    if key is None:
        return {
            "email_verification_enabled": False,
            "mobile_verification_enabled": False,
        }
    block = normalized[key]
    return {
        "email_verification_enabled": bool(block["email_verification_enabled"]),
        "mobile_verification_enabled": bool(block["mobile_verification_enabled"]),
    }


def _any_channel_enabled(config: dict[str, Any]) -> bool:
    normalized = normalize_registration_config(config)
    return any(
        normalized[key]["email_verification_enabled"]
        or normalized[key]["mobile_verification_enabled"]
        for key in _VERIFICATION_ROLE_KEYS
    )


def verification_due_for(user: dict[str, Any], config: dict[str, Any]) -> dict[str, bool]:
    """Channels this account must verify on the next login.

    Students, evaluators, and Application Access accounts (admin and custom)
    are due only after Super Admin releases them. Each role uses its own
    checkboxes. A missing verified flag is treated as already verified so
    older accounts stay quiet.
    """
    channels = channels_for_role(config, str(user.get("role") or ""))
    if str(user.get("role") or "") not in _REGISTRATION_ROLES:
        return {"email": False, "mobile": False}
    if user.get("verification_required") is not True:
        return {"email": False, "mobile": False}
    return {
        "email": channels["email_verification_enabled"] and user.get("email_verified") is False,
        "mobile": channels["mobile_verification_enabled"] and user.get("phone_verified") is False,
    }


class AppSettingsService:
    """Manages the Profile Password and destructive admin settings actions."""

    def __init__(
        self,
        firebase: FirebaseService | None = None,
        storage_client: storage.Client | None = None,
    ):
        self.firebase = firebase or FirebaseService()
        self.storage_client = storage_client

    def get_settings_public(self) -> dict[str, Any]:
        """Flags for the Application Settings UI (never returns the password)."""
        doc = self._get_security_doc()
        is_default = bool(doc.get("is_default_profile_password"))
        bucket = resolve_evaluation_bucket_name()
        wipeable = list(WIPEABLE_COLLECTIONS) + [
            "users (except super admin)",
            "firebase_auth (except super admin)",
        ]
        if bucket:
            wipeable.append(f"gcs objects in gs://{bucket}")
        return {
            "profile_password_configured": bool(doc.get("profile_password_hash")),
            "default_profile_password_hint": (DEFAULT_PROFILE_PASSWORD if is_default else None),
            "wipeable_collections": wipeable,
            "evaluation_bucket_name": bucket,
            "reset_confirm_phrase": RESET_CONFIRM_PHRASE,
        }

    def get_evaluation_routing(self) -> dict[str, Any]:
        """Shares for the Application Settings video-worker section."""
        doc = self.firebase.get_document(SETTINGS_COLLECTION, EVALUATION_ROUTING_DOC_ID) or {}
        return self._routing_payload(doc)

    def set_evaluation_routing(
        self,
        *,
        personal_share: int,
        company_share: int,
        updated_by: str,
    ) -> dict[str, Any]:
        """Save a new repeating pattern. The next video starts at the beginning of it."""
        urls = worker_urls()
        if personal_share > 0 and PERSONAL_KEY not in urls:
            raise ValueError("The personal evaluation service is not configured on this API.")
        if company_share > 0 and COMPANY_KEY not in urls:
            raise ValueError("The company evaluation service is not configured on this API.")
        if personal_share <= 0 and company_share <= 0:
            raise ValueError("Set a share for at least one evaluation service.")

        now = now_ist_iso()
        existing = self.firebase.get_document(SETTINGS_COLLECTION, EVALUATION_ROUTING_DOC_ID) or {}
        self.firebase.set_document(
            SETTINGS_COLLECTION,
            EVALUATION_ROUTING_DOC_ID,
            {
                "personal_share": personal_share,
                "company_share": company_share,
                "next_index": 0,
                "created_at": existing.get("created_at") or now,
                "updated_at": now,
                "updated_by": updated_by,
            },
        )
        return self.get_evaluation_routing()

    def reserve_evaluation_worker(self) -> str:
        """
        Take the next slot in the saved pattern.

        The chosen worker is stored on the Cloud Task, so a retry stays there.
        """
        urls = worker_urls()

        def _txn(transaction):
            doc = (
                self.firebase.txn_get(transaction, SETTINGS_COLLECTION, EVALUATION_ROUTING_DOC_ID)
                or {}
            )
            personal_share, company_share = shares_from_document(
                doc,
                company_configured=COMPANY_KEY in urls,
            )
            sequence = build_sequence(
                personal_share,
                company_share,
                personal_configured=PERSONAL_KEY in urls,
                company_configured=COMPANY_KEY in urls,
            )
            if not sequence:
                raise ValueError("No evaluation service is configured")
            next_index = doc.get("next_index") or 0
            try:
                next_index = int(next_index)
            except (TypeError, ValueError):
                next_index = 0
            if next_index < 0:
                next_index = 0
            choice = sequence[next_index % len(sequence)]
            now = now_ist_iso()
            self.firebase.txn_set(
                transaction,
                SETTINGS_COLLECTION,
                EVALUATION_ROUTING_DOC_ID,
                {
                    "personal_share": personal_share,
                    "company_share": company_share,
                    "next_index": next_index + 1,
                    "created_at": doc.get("created_at") or now,
                    "updated_at": doc.get("updated_at") or now,
                    "updated_by": doc.get("updated_by") or "",
                },
            )
            return choice

        return self.firebase.run_transaction(_txn)

    def _routing_payload(self, doc: dict[str, Any]) -> dict[str, Any]:
        urls = worker_urls()
        personal_share, company_share = shares_from_document(
            doc,
            company_configured=COMPANY_KEY in urls,
        )
        sequence = build_sequence(
            personal_share,
            company_share,
            personal_configured=PERSONAL_KEY in urls,
            company_configured=COMPANY_KEY in urls,
        )
        workers = []
        for item in WORKER_CATALOG:
            key = item["key"]
            workers.append(
                {
                    "key": key,
                    "name": item["name"],
                    "project_id": item["project_id"],
                    "configured": key in urls,
                    "share": personal_share if key == PERSONAL_KEY else company_share,
                    "url": urls.get(key),
                }
            )
        try:
            next_index = int(doc.get("next_index") or 0)
        except (TypeError, ValueError):
            next_index = 0
        if next_index < 0:
            next_index = 0
        return {
            "personal_share": personal_share,
            "company_share": company_share,
            "next_index": next_index,
            "sequence": sequence,
            "summary": routing_summary(sequence),
            "scope": "Video AI analysis. GitHub AI evaluation stays on this API.",
            "workers": workers,
        }

    def get_registration_config(self) -> dict[str, Any]:
        """Per-role email and mobile flags. A missing document leaves every channel on."""
        doc = self.firebase.get_document(SETTINGS_COLLECTION, REGISTRATION_DOC_ID) or {}
        return normalize_registration_config(doc)

    def set_registration_config(
        self,
        *,
        student: dict[str, bool],
        evaluator: dict[str, bool],
        application_access: dict[str, bool],
        updated_by: str,
    ) -> dict[str, Any]:
        """Save one email and mobile checkbox pair per role.

        Does not itself ask anyone to verify. The two top-level flags are the
        student pair, kept so an older client reading the document still works.
        """
        now = now_ist_iso()
        existing = self.firebase.get_document(SETTINGS_COLLECTION, REGISTRATION_DOC_ID) or {}
        student_pair = _channel_pair(student, _BOTH_ON)
        evaluator_pair = _channel_pair(evaluator, _BOTH_ON)
        access_pair = _channel_pair(application_access, _BOTH_ON)
        self.firebase.set_document(
            SETTINGS_COLLECTION,
            REGISTRATION_DOC_ID,
            {
                "student": student_pair,
                "evaluator": evaluator_pair,
                "application_access": access_pair,
                "email_verification_enabled": student_pair["email_verification_enabled"],
                "mobile_verification_enabled": student_pair["mobile_verification_enabled"],
                "created_at": existing.get("created_at") or now,
                "updated_at": now,
                "updated_by": updated_by,
            },
        )
        return self.registration_status()

    def registration_status(self) -> dict[str, Any]:
        """Toggles plus waiting and already-asked counts."""
        config = self.get_registration_config()
        waiting, released = self._verification_queues(config)
        return {
            **config,
            "pending_count": len(waiting),
            "released_count": len(released),
            "released_now": 0,
        }

    def release_verification(self, batch_size: int, *, released_by: str) -> dict[str, Any]:
        """Mark the next ``batch_size`` unverified accounts so login asks them.

        Does not email codes. Each person verifies with the existing OTP flow
        the next time they sign in.
        """
        if batch_size < 1 or batch_size > 500:
            raise ValueError("batch_size must be between 1 and 500")
        config = self.get_registration_config()
        if not _any_channel_enabled(config):
            raise ValueError("Turn email or mobile verification on before sending it to users")
        waiting, released = self._verification_queues(config)
        batch = waiting[:batch_size]
        now = now_ist_iso()
        for user in batch:
            self.firebase.update_document(
                "users",
                user["id"],
                {
                    "verification_required": True,
                    "verification_released_at": now,
                    "verification_released_by": released_by,
                    "updated_at": now,
                },
            )
        return {
            **config,
            "pending_count": len(waiting) - len(batch),
            "released_count": len(released) + len(batch),
            "released_now": len(batch),
        }

    def _verification_queues(
        self, config: dict[str, Any]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        waiting: list[dict[str, Any]] = []
        released: list[dict[str, Any]] = []
        for user in self.firebase.get_collection("users"):
            if not self._still_needs_verification(user, config):
                continue
            if user.get("verification_required") is True:
                released.append(user)
            else:
                waiting.append(user)
        waiting.sort(
            key=lambda user: (str(user.get("created_at") or ""), str(user.get("id") or ""))
        )
        return waiting, released

    @staticmethod
    def _still_needs_verification(user: dict[str, Any], config: dict[str, Any]) -> bool:
        if str(user.get("role") or "") not in _REGISTRATION_ROLES:
            return False
        channels = channels_for_role(config, str(user.get("role") or ""))
        email_gap = channels["email_verification_enabled"] and user.get("email_verified") is False
        phone_gap = channels["mobile_verification_enabled"] and user.get("phone_verified") is False
        return email_gap or phone_gap

    def ensure_default_profile_password(self) -> None:
        """Idempotently seed Profile Password to ``12345678`` when missing."""
        existing = self.firebase.get_document(SETTINGS_COLLECTION, SETTINGS_DOC_ID)
        if existing and existing.get("profile_password_hash"):
            return
        now = now_ist_iso()
        salt, password_hash = self._hash_password(DEFAULT_PROFILE_PASSWORD)
        self.firebase.set_document(
            SETTINGS_COLLECTION,
            SETTINGS_DOC_ID,
            {
                "profile_password_salt": salt,
                "profile_password_hash": password_hash,
                "is_default_profile_password": True,
                "created_at": (existing or {}).get("created_at") or now,
                "updated_at": now,
            },
        )
        logger.info("Seeded default admin Profile Password")

    def change_profile_password(
        self,
        current_password: str,
        new_password: str,
        changed_by: str,
    ) -> None:
        """Verify current Profile Password, then store a new hash."""
        if not self.verify_profile_password(current_password):
            raise ValueError("Current profile password is incorrect")
        now = now_ist_iso()
        salt, password_hash = self._hash_password(new_password)
        existing = self._get_security_doc()
        self.firebase.set_document(
            SETTINGS_COLLECTION,
            SETTINGS_DOC_ID,
            {
                "profile_password_salt": salt,
                "profile_password_hash": password_hash,
                "is_default_profile_password": False,
                "created_at": existing.get("created_at") or now,
                "updated_at": now,
                "updated_by": changed_by,
            },
        )

    def verify_profile_password(self, password: str) -> bool:
        doc = self._get_security_doc()
        salt = doc.get("profile_password_salt")
        expected = doc.get("profile_password_hash")
        if not salt or not expected:
            # Auto-heal if seeder never ran.
            self.ensure_default_profile_password()
            doc = self._get_security_doc()
            salt = doc.get("profile_password_salt")
            expected = doc.get("profile_password_hash")
        if not salt or not expected:
            return False
        candidate = self._hash_with_salt(password, salt)
        return hmac.compare_digest(candidate, expected)

    def reset_database(
        self,
        profile_password: str,
        *,
        preserve_user_id: str,
        confirm_phrase: str,
    ) -> dict[str, Any]:
        """
        Wipe application Firestore collections, every login except Super
        Admin, and all objects in the evaluation GCS bucket.

        Preserves:
        - ``app_settings/security`` (Profile Password)
        - Firestore ``users`` with ``role == super_admin``
        - Matching Firebase Auth accounts
        - Built-in Admin and Student role documents (rewritten after the wipe)
        - The GCS bucket itself (only objects are deleted)
        """
        if confirm_phrase != RESET_CONFIRM_PHRASE:
            raise ValueError(f'confirm_phrase must be exactly "{RESET_CONFIRM_PHRASE}"')
        if not self.verify_profile_password(profile_password):
            raise ValueError("Profile password is incorrect")

        deleted_counts: dict[str, int] = {}

        for collection in WIPEABLE_COLLECTIONS:
            docs = self.firebase.get_collection(collection)
            ids = [d["id"] for d in docs if d.get("id")]
            # Always target known AI prompt docs even if listing was incomplete.
            if collection in ("ai_evaluation_prompts", "ai_evaluation_prompt"):
                ids = list(dict.fromkeys([*ids, *AI_PROMPT_DOCUMENT_IDS]))
            deleted_counts[collection] = self.firebase.delete_documents(collection, ids)

        # Keep only Super Admin. Admin, custom, evaluator, and student accounts go.
        users = self.firebase.get_collection("users")
        preserve_auth_uids = {
            u["id"] for u in users if u.get("id") and u.get("role") == "super_admin"
        }
        if preserve_user_id:
            caller = next((u for u in users if u.get("id") == preserve_user_id), None)
            if caller and caller.get("role") == "super_admin":
                preserve_auth_uids.add(preserve_user_id)

        non_admin_ids = [
            u["id"] for u in users if u.get("id") and u["id"] not in preserve_auth_uids
        ]
        deleted_counts["users_non_admin"] = self.firebase.delete_documents("users", non_admin_ids)

        # Wipe Firebase Auth for everyone except preserved admins — including
        # Auth-only orphans left from earlier resets (no Firestore doc).
        auth_deleted = 0
        try:
            auth_users = self.firebase.list_auth_users()
        except Exception as exc:
            logger.warning("Could not list Firebase Auth users during reset: %s", exc)
            auth_users = [{"uid": uid} for uid in non_admin_ids]

        for auth_user in auth_users:
            uid = auth_user.get("uid")
            if not uid or uid in preserve_auth_uids:
                continue
            try:
                if self.firebase.delete_user(uid):
                    auth_deleted += 1
            except Exception as exc:
                logger.warning(
                    "Failed to delete Firebase Auth user %s during reset: %s",
                    uid,
                    exc,
                )
        deleted_counts["firebase_auth_non_admin"] = auth_deleted

        # Wipe submission videos + hackathon banners from the evaluation bucket.
        bucket_name = resolve_evaluation_bucket_name()
        deleted_counts["gcs_evaluation_bucket"] = self._wipe_evaluation_bucket(bucket_name)

        # Do not re-seed AI prompts here — reset should leave
        # ``ai_evaluation_prompts`` empty. Evaluation falls back to in-code
        # defaults until an admin saves prompts in the AI prompts UI.

        # Fresh startup writes the built-in Admin and Student roles, and the
        # Profile Password when it is missing.
        from app.services.access_service import AccessService

        AccessService(firebase=self.firebase).seed_builtin_admin_role()
        self.ensure_default_profile_password()

        logger.warning(
            "Admin %s reset Firestore application data. Counts=%s",
            preserve_user_id,
            deleted_counts,
        )
        preserved = [
            "app_settings/security",
            "users (role=super_admin)",
            "firebase_auth (super_admin)",
            "roles (built-in Admin and Student)",
        ]
        if bucket_name:
            preserved.append(f"gcs bucket gs://{bucket_name} (emptied, not deleted)")
        return {
            "message": (
                "Database reset completed. Application collections and every "
                "login except Super Admin were cleared, including evaluation-"
                "bucket objects. The Super Admin account, the Profile "
                "Password, the built-in roles, and the GCS bucket itself "
                "were preserved."
            ),
            "deleted_counts": deleted_counts,
            "preserved": preserved,
        }

    def _wipe_evaluation_bucket(self, bucket_name: str | None) -> int:
        """Delete all objects in the evaluation bucket; never delete the bucket."""
        if not bucket_name:
            logger.warning(
                "EVALUATION_BUCKET_NAME / VIDEO_BUCKET_NAME not set; "
                "skipping GCS wipe during database reset"
            )
            return 0
        try:
            client = self.storage_client or build_storage_client(
                os.getenv("GOOGLE_CLOUD_PROJECT") or os.getenv("FIREBASE_PROJECT_ID") or None
            )
            return wipe_bucket_objects(client, bucket_name)
        except Exception as exc:
            logger.exception(
                "Failed to wipe evaluation bucket gs://%s during reset: %s",
                bucket_name,
                exc,
            )
            # Do not fail the whole reset if GCS wipe fails — Firestore/Auth
            # cleanup already ran. Surface zero so admins can retry / manual wipe.
            return 0

    def _get_security_doc(self) -> dict[str, Any]:
        return self.firebase.get_document(SETTINGS_COLLECTION, SETTINGS_DOC_ID) or {}

    @staticmethod
    def _hash_password(password: str) -> tuple[str, str]:
        salt = secrets.token_hex(16)
        return salt, AppSettingsService._hash_with_salt(password, salt)

    @staticmethod
    def _hash_with_salt(password: str, salt: str) -> str:
        iterations = int(os.getenv("PROFILE_PASSWORD_PBKDF2_ITERATIONS", "120000"))
        digest = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            salt.encode("utf-8"),
            iterations,
        )
        return digest.hex()
